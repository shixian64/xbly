from __future__ import annotations

import inspect
import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from bbw_web import jobs  # noqa: E402
from bbw_web.media_native import repository as media_repository  # noqa: E402
from bbw_web.media_native import r2_adapter  # noqa: E402


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _Repository:
    def __init__(self, intent, asset_id, owner_id) -> None:
        self.intent = intent
        self.asset_id = asset_id
        self.owner_id = owner_id
        self.intent_delete_calls = 0
        self.asset_delete_calls = 0
        self.observed_grace_seconds = 0

    def claim_expired_upload_intents(self, **_kwargs):
        return [self.intent]

    def delete_cancelled_upload_intent(self, intent_id):
        self.intent_delete_calls += 1
        return intent_id == self.intent.id

    def asset_cleanup_candidates(self, *, unsent_grace, **_kwargs):
        self.observed_grace_seconds = int(unsent_grace.total_seconds())
        return [(self.asset_id, self.owner_id)]

    def delete_asset_and_release_quota(self, *, delete_object, **_kwargs):
        self.asset_delete_calls += 1
        delete_object(SimpleNamespace(id=self.asset_id))
        return True


class _Adapter:
    def __init__(self) -> None:
        self.intent_calls = 0
        self.asset_calls = 0

    def delete_upload_artifacts(self, _intent):
        self.intent_calls += 1
        return 2

    def delete_private_asset(self, _asset):
        self.asset_calls += 1


class MediaNativeLifecycleJobTests(unittest.TestCase):
    def test_cleanup_job_cancels_artifacts_then_deletes_assets_and_reports_counts(self) -> None:
        intent = SimpleNamespace(id=uuid.uuid4())
        asset_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        repository = _Repository(intent, asset_id, owner_id)
        adapter = _Adapter()

        @contextmanager
        def fake_session_scope():
            yield object()

        settings = SimpleNamespace(
            environment="production",
            media_native_unsent_grace_seconds=48 * 60 * 60,
        )
        with patch.object(jobs, "session_scope", fake_session_scope), patch.object(
            media_repository,
            "SqlAlchemyMediaNativeRepository",
            return_value=repository,
        ), patch.object(
            r2_adapter,
            "R2PrivateMediaAdapter",
            return_value=adapter,
        ):
            result = jobs._cleanup_web_native_media(
                settings,
                SimpleNamespace(),
                at=NOW,
            )

        self.assertEqual(
            result,
            {
                "upload_intents_deleted": 1,
                "upload_intent_errors": 0,
                "assets_deleted": 1,
                "asset_errors": 0,
            },
        )
        self.assertEqual(repository.intent_delete_calls, 1)
        self.assertEqual(repository.asset_delete_calls, 1)
        self.assertEqual(repository.observed_grace_seconds, 48 * 60 * 60)
        self.assertEqual(adapter.intent_calls, 1)
        self.assertEqual(adapter.asset_calls, 1)

    def test_cleanup_job_keeps_failed_deletes_retryable(self) -> None:
        intent = SimpleNamespace(id=uuid.uuid4())
        repository = _Repository(intent, uuid.uuid4(), uuid.uuid4())

        class FailingAdapter(_Adapter):
            def delete_upload_artifacts(self, _intent):
                raise RuntimeError("R2 delete failed")

            def delete_private_asset(self, _asset):
                raise RuntimeError("R2 delete failed")

        @contextmanager
        def fake_session_scope():
            yield object()

        with patch.object(jobs, "session_scope", fake_session_scope), patch.object(
            media_repository,
            "SqlAlchemyMediaNativeRepository",
            return_value=repository,
        ), patch.object(
            r2_adapter,
            "R2PrivateMediaAdapter",
            return_value=FailingAdapter(),
        ), patch.object(jobs.LOGGER, "warning"):
            result = jobs._cleanup_web_native_media(
                SimpleNamespace(environment="production"),
                SimpleNamespace(),
                at=NOW,
            )

        self.assertEqual(result["upload_intent_errors"], 1)
        self.assertEqual(result["asset_errors"], 1)
        self.assertEqual(repository.intent_delete_calls, 0)
        self.assertEqual(repository.asset_delete_calls, 1)

    def test_global_retention_job_runs_native_cleanup_after_message_purge(self) -> None:
        source = inspect.getsource(jobs.cleanup_expired_data)
        self.assertLess(
            source.index("retention.purge_expired_canonical_messages"),
            source.index("_cleanup_web_native_media("),
        )
        self.assertIn('"native_assets_deleted"', source)
        self.assertIn('"native_asset_errors"', source)


class OutboxRetentionMediaArchiveTests(unittest.TestCase):
    """retention 清理不得删除未完成的 media.archive 归档行。"""

    def _captured_outbox_delete(self) -> object:
        statements: list[object] = []

        class CaptureDb:
            def execute(self, statement: object) -> object:
                statements.append(statement)
                return SimpleNamespace(rowcount=0)

        class FakeRetention:
            def __init__(self, _db: object, _settings: object) -> None:
                pass

            def purge_expired_raw_responses(self, **_kwargs) -> int:
                return 0

            def purge_expired_audit_logs(self, **_kwargs) -> int:
                return 0

            def purge_stale_sessions(self, **_kwargs) -> tuple[int, int]:
                return 0, 0

            def purge_expired_messages(self, **_kwargs) -> int:
                return 0

            def purge_expired_canonical_messages(self, **_kwargs) -> int:
                return 0

        @contextmanager
        def fake_session_scope():
            yield CaptureDb()

        with patch.object(
            jobs,
            "get_settings",
            return_value=SimpleNamespace(message_retention_days=180),
        ), patch.object(
            jobs, "R2Storage", side_effect=RuntimeError("r2 unavailable")
        ), patch.object(jobs, "session_scope", fake_session_scope), patch.object(
            jobs, "RetentionService", FakeRetention
        ), patch.object(jobs, "_purge_expired_match_history", return_value=0):
            result = jobs.cleanup_expired_data()

        self.assertTrue(result["ok"])
        outbox_deletes = [
            statement
            for statement in statements
            if getattr(getattr(statement, "table", None), "name", "")
            == "operation_outbox"
        ]
        self.assertEqual(len(outbox_deletes), 1)
        return outbox_deletes[0]

    def test_retention_keeps_unfinished_media_archive_rows_and_reclaims_completed(
        self,
    ) -> None:
        statement = self._captured_outbox_delete()
        old = "2000-01-01 00:00:00.000000"
        recent = "2099-01-01 00:00:00.000000"
        rows = [
            # media.archive 门禁只认 completed 为终态：failed/cancelled 必须保留。
            ("media-failed-old", "media.archive", "failed", old),
            ("media-cancelled-old", "media.archive", "cancelled", old),
            (
                "compat-media-failed-old",
                "compatibility.media.archive.legacy",
                "failed",
                old,
            ),
            # completed 到期仍可回收。
            ("media-completed-old", "media.archive", "completed", old),
            (
                "compat-media-completed-old",
                "compatibility.media.archive.legacy",
                "completed",
                old,
            ),
            # 其他 operation_type 的行为保持不变。
            ("social-failed-old", "compatibility.social.follow", "failed", old),
            ("social-cancelled-old", "compatibility.social.follow", "cancelled", old),
            # 未过期或非终态的行本来就不在回收范围内。
            ("media-failed-recent", "media.archive", "failed", recent),
            ("media-pending-old", "media.archive", "pending", old),
        ]
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE operation_outbox ("
                "id TEXT PRIMARY KEY, operation_type TEXT, "
                "status TEXT, updated_at DATETIME)"
            )
            for row in rows:
                connection.exec_driver_sql(
                    "INSERT INTO operation_outbox "
                    "(id, operation_type, status, updated_at) VALUES (?, ?, ?, ?)",
                    row,
                )
            connection.execute(statement)
            remaining = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT id FROM operation_outbox"
                )
            }
        self.assertEqual(
            remaining,
            {
                "media-failed-old",
                "media-cancelled-old",
                "compat-media-failed-old",
                "media-failed-recent",
                "media-pending-old",
            },
        )


class MediaOutboxDeadRowWatchdogTests(unittest.TestCase):
    """claim 阶段必须把「锁过期 + 预算耗尽」的 processing 死行转为 failed。"""

    def test_claim_marks_exhausted_expired_processing_rows_as_failed(self) -> None:
        dead_plain = SimpleNamespace(
            status="processing",
            attempt_count=8,
            max_attempts=8,
            locked_by="worker-crashed",
            locked_until=NOW,
            last_error=None,
        )
        dead_with_error = SimpleNamespace(
            status="processing",
            attempt_count=8,
            max_attempts=8,
            locked_by="worker-crashed",
            locked_until=NOW,
            last_error="download timed out",
        )

        class FakeScalarResult:
            def __init__(self, rows) -> None:
                self._rows = rows

            def __iter__(self):
                return iter(self._rows)

        class FakeDb:
            def __init__(self) -> None:
                self.calls = 0

            def scalars(self, _statement: object) -> FakeScalarResult:
                self.calls += 1
                if self.calls == 1:
                    return FakeScalarResult([dead_plain, dead_with_error])
                return FakeScalarResult([])

            def flush(self) -> None:
                pass

        @contextmanager
        def fake_session_scope():
            yield FakeDb()

        with patch.object(jobs, "session_scope", fake_session_scope):
            claimed = jobs._claim_media_outboxes(limit=5)

        self.assertEqual(claimed, [])
        for row in (dead_plain, dead_with_error):
            self.assertEqual(row.status, "failed")
            self.assertIsNone(row.locked_by)
            self.assertIsNone(row.locked_until)
            # 预算不回补：死行只做终态收敛，readiness 仍计入 unfinished。
            self.assertEqual(row.attempt_count, 8)
            self.assertEqual(row.max_attempts, 8)
        # 已有错误信息保留，缺失时补默认说明；说明文字不得命中配置错误
        # 标记，避免被凭据恢复通道误复活。
        self.assertEqual(dead_with_error.last_error, "download timed out")
        self.assertTrue(dead_plain.last_error)
        for marker in jobs.MEDIA_CONFIGURATION_ERROR_MARKERS:
            self.assertNotIn(marker, str(dead_plain.last_error).lower())

    def test_watchdog_query_is_scoped_to_expired_exhausted_processing(self) -> None:
        source = inspect.getsource(jobs._claim_media_outboxes)
        self.assertIn('OperationOutbox.status == "processing"', source)
        self.assertIn(
            "OperationOutbox.attempt_count >= OperationOutbox.max_attempts", source
        )
        self.assertIn("OperationOutbox.locked_until <= now", source)
        self.assertIn("OperationOutbox.locked_until.is_not(None)", source)


if __name__ == "__main__":
    unittest.main()
