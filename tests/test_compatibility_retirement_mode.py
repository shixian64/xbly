from __future__ import annotations

import io
import json
import types
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from bbw_prod import compatibility_retirement
from bbw_prod.compatibility import (
    CompatibilityMode,
    compatibility_mode,
    new_compatibility_outbox_status,
    new_optional_tim_status,
)
from bbw_prod.config import ConfigurationError, Settings
from bbw_prod.migration_readiness import _outbox_summary
from bbw_prod.repositories import OperationOutboxRepository
from bbw_web import compatibility_outbox, jobs
from bbw_web.media_native.contracts import MediaAccount, MediaAttachmentPayload
from bbw_web.media_native.repository import SqlAlchemyMediaNativeRepository
from bbw_web.messaging.contracts import LocalAccount
from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _InsertResult:
    def __init__(self, db: "_InsertCaptureDb", statement: object) -> None:
        self.db = db
        self.statement = statement

    def first(self) -> object:
        params = self.statement.compile(dialect=postgresql.dialect()).params
        self.db.params.append(params)
        return types.SimpleNamespace(
            id=uuid.uuid4(),
            status=params.get("status", "pending"),
        )


class _InsertCaptureDb:
    def __init__(self) -> None:
        self.params: list[dict[str, object]] = []

    def scalars(self, statement: object) -> _InsertResult:
        return _InsertResult(self, statement)

    def scalar(self, _statement: object) -> object:
        raise AssertionError("a created row must not require the conflict lookup")


class CompatibilityModeTests(unittest.TestCase):
    def test_settings_accept_all_modes_and_reject_unknown_values(self) -> None:
        for value in ("enabled", "PAUSED", "retired"):
            with self.subTest(value=value), patch.dict(
                "os.environ", {"BBW_COMPATIBILITY_MODE": value}, clear=False
            ):
                self.assertEqual(
                    Settings.from_env().compatibility_mode,
                    value.lower(),
                )
        with patch.dict(
            "os.environ", {"BBW_COMPATIBILITY_MODE": "automatic"}, clear=False
        ):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_retired_creates_terminal_optional_audits_but_not_media_archive(self) -> None:
        with patch.dict(
            "os.environ", {"BBW_COMPATIBILITY_MODE": "retired"}, clear=False
        ):
            self.assertEqual(compatibility_mode(), CompatibilityMode.RETIRED)
            self.assertEqual(
                new_compatibility_outbox_status("compatibility.social.follow"),
                "cancelled",
            )
            self.assertEqual(
                new_compatibility_outbox_status(
                    "compatibility.media.archive.legacy"
                ),
                "pending",
            )
            self.assertEqual(new_optional_tim_status(), "cancelled")

    def test_operation_outbox_repository_writes_cancelled_retirement_record(self) -> None:
        db = _InsertCaptureDb()
        with patch.dict(
            "os.environ", {"BBW_COMPATIBILITY_MODE": "retired"}, clear=False
        ):
            row, created = OperationOutboxRepository(db).enqueue(
                owner_user_id=uuid.uuid4(),
                operation_type="compatibility.social.follow",
                aggregate_type="social-native",
                aggregate_id="peer",
                idempotency_key="operation-one",
                payload={"schema": 1},
                status="pending",
            )

        self.assertTrue(created)
        self.assertEqual(row.status, "cancelled")
        self.assertEqual(db.params[0]["status"], "cancelled")
        self.assertEqual(
            db.params[0]["last_error"], "legacy_compatibility_retired"
        )
        self.assertIsNotNone(db.params[0]["completed_at"])

    def test_retired_text_and_media_deliveries_are_local_first_and_tim_cancelled(self) -> None:
        sender_user_id = uuid.uuid4()
        recipient_user_id = uuid.uuid4()
        sender = LocalAccount(
            sender_user_id, uuid.uuid4(), "sender", "发送者", 180
        )
        recipient = LocalAccount(
            recipient_user_id, uuid.uuid4(), "recipient", "接收者", 180
        )
        canonical = types.SimpleNamespace(
            id=uuid.uuid4(),
            client_message_id="text-one",
            body="本地文本",
            extra_data={},
        )
        text_db = _InsertCaptureDb()
        text_delivery = SqlAlchemyCanonicalMessageStore(
            text_db, compatibility_mode="retired"
        )._ensure_delivery(
            canonical=canonical,
            sender=sender,
            recipient=recipient,
            channel="tim",
            occurred_at=NOW,
            projection_ids=(uuid.uuid4(), uuid.uuid4()),
        )
        self.assertEqual(text_delivery.status, "cancelled")
        self.assertEqual(text_db.params[0]["required"], False)

        media_db = _InsertCaptureDb()
        media_sender = MediaAccount(sender_user_id, uuid.uuid4(), "sender")
        media_recipient = MediaAccount(
            recipient_user_id, uuid.uuid4(), "recipient"
        )
        payload = MediaAttachmentPayload(
            asset_id=uuid.uuid4(),
            kind="image",
            filename="photo.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            sha256="a" * 64,
            width=10,
            height=10,
        )
        media_delivery = SqlAlchemyMediaNativeRepository(
            media_db, compatibility_mode="retired"
        )._ensure_delivery(
            canonical=types.SimpleNamespace(
                id=uuid.uuid4(),
                client_message_id="media-one",
                occurred_at=NOW,
            ),
            attachment=types.SimpleNamespace(id=uuid.uuid4()),
            sender=media_sender,
            recipient=media_recipient,
            payload=payload,
            channel="tim",
            projection_ids=(uuid.uuid4(), uuid.uuid4()),
        )
        self.assertEqual(media_delivery.status, "cancelled")
        self.assertEqual(media_db.params[0]["required"], False)


class CompatibilityDispatchGateTests(unittest.TestCase):
    def test_paused_workers_do_not_claim_or_construct_external_transports(self) -> None:
        delivery_id = uuid.uuid4()
        with (
            patch.dict(
                "os.environ", {"BBW_COMPATIBILITY_MODE": "paused"}, clear=False
            ),
            patch.object(jobs, "_claim_tim_message_delivery") as tim_claim,
            patch.object(jobs, "_default_message_send_transport") as transport,
            patch.object(compatibility_outbox, "_claim_due") as outbox_claim,
            patch.object(
                compatibility_outbox, "_default_runtime_provider"
            ) as provider,
        ):
            tim_result = jobs.mirror_tim_message_delivery(str(delivery_id))
            outbox_result = compatibility_outbox.dispatch_due()

        self.assertTrue(tim_result["ignored"])
        self.assertEqual(tim_result["compatibility_mode"], "paused")
        self.assertEqual(outbox_result["claimed"], 0)
        tim_claim.assert_not_called()
        transport.assert_not_called()
        outbox_claim.assert_not_called()
        provider.assert_not_called()

    def test_retirement_cancellation_is_rechecked_immediately_before_external_calls(self) -> None:
        claim = compatibility_outbox.ClaimedOperation(
            id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            operation_type="compatibility.social.follow",
            aggregate_type="social-native",
            aggregate_id="peer",
            payload={"actor_uid": "sender"},
            attempt_count=1,
            max_attempts=8,
            locked_by="worker-one",
        )
        runtime = types.SimpleNamespace(
            app=types.SimpleNamespace(
                client=types.SimpleNamespace(close=lambda: None)
            )
        )

        @contextmanager
        def fake_db_scope():
            yield object()

        with (
            patch.object(
                compatibility_outbox,
                "_prepare_call",
                return_value=types.SimpleNamespace(binding=None),
            ),
            patch.object(
                compatibility_outbox,
                "_load_runtime_account",
                return_value=types.SimpleNamespace(upstream_uid="sender"),
            ),
            patch.object(
                compatibility_outbox,
                "_claim_still_processing",
                side_effect=(True, False),
            ),
            patch.object(
                compatibility_outbox, "_create_runtime", return_value=runtime
            ),
            patch.object(compatibility_outbox, "_invoke") as invoke,
        ):
            with self.assertRaisesRegex(
                compatibility_outbox.PermanentCompatibilityError,
                "compatibility_claim_cancelled",
            ):
                compatibility_outbox._execute_claim(
                    claim,
                    db_scope=fake_db_scope,
                    cipher=types.SimpleNamespace(),
                    runtime_provider=types.SimpleNamespace(),
                    now=NOW,
                )
        invoke.assert_not_called()

        delivery_id = uuid.uuid4()
        tim_claim = jobs._ClaimedTimMirror(
            delivery_id,
            {
                "canonical_message_id": str(uuid.uuid4()),
                "client_message_id": "text-one",
                "from": "sender",
                "to": "recipient",
                "text": "本地消息",
            },
            1,
            8,
        )
        send_calls: list[object] = []
        sender = types.SimpleNamespace(
            send_text=lambda *_args, **_kwargs: send_calls.append(object())
        )
        with (
            patch.dict(
                "os.environ", {"BBW_COMPATIBILITY_MODE": "enabled"}, clear=False
            ),
            patch.object(
                jobs, "_claim_tim_message_delivery", return_value=tim_claim
            ),
            patch.object(
                jobs, "_default_message_send_transport", return_value=sender
            ),
            patch.object(
                jobs, "_tim_mirror_delivery_is_processing", return_value=False
            ),
            patch.object(
                jobs, "_mark_tim_mirror_failed", return_value=("cancelled", 30)
            ),
        ):
            result = jobs.mirror_tim_message_delivery(str(delivery_id))
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["retry"])
        self.assertEqual(send_calls, [])

    def test_paused_dispatchers_do_not_scan_or_enqueue(self) -> None:
        with (
            patch.dict(
                "os.environ", {"BBW_COMPATIBILITY_MODE": "paused"}, clear=False
            ),
            patch.object(jobs, "_due_tim_mirror_deliveries") as scanner,
            patch.object(jobs, "Queue") as queue,
        ):
            tim = jobs.dispatch_due_tim_message_deliveries(
                connection=object(), limit=20
            )
            compatibility = jobs._enqueue_compatibility_outbox_dispatch(
                connection=object()
            )
            history = jobs.sync_account_history("invalid", "invalid")

        self.assertEqual(tim["dispatched"], 0)
        self.assertEqual(compatibility["dispatched"], 0)
        self.assertTrue(history["ignored"])
        scanner.assert_not_called()
        queue.assert_not_called()

    def test_paused_archive_media_job_returns_claimed_outbox_to_retry(self) -> None:
        # 行在派发阶段已被 _claim_media_outboxes 置为 processing 并消耗
        # 一次 attempt；paused 下放弃执行必须退回 retry 并补回预算，
        # 否则反复暂停/恢复会耗尽预算，行永久卡在 processing。
        row = types.SimpleNamespace(
            status="processing",
            attempt_count=3,
            max_attempts=8,
            available_at=None,
            locked_by="worker-one",
            locked_until=NOW,
            last_error=None,
        )

        class FakeDb:
            def scalar(self, _statement: object) -> object:
                return row

        @contextmanager
        def fake_session_scope():
            yield FakeDb()

        with (
            patch.dict(
                "os.environ", {"BBW_COMPATIBILITY_MODE": "paused"}, clear=False
            ),
            patch.object(jobs, "session_scope", fake_session_scope),
        ):
            result = jobs.archive_media_job(str(uuid.uuid4()))

        self.assertTrue(result["ok"])
        self.assertTrue(result["ignored"])
        self.assertEqual(result["compatibility_mode"], "paused")
        self.assertEqual(row.status, "retry")
        # 补回本次 claim 消耗的预算而非回退 attempt_count，
        # 保持 attempt 序号单调、RQ job id 唯一。
        self.assertEqual(row.attempt_count, 3)
        self.assertEqual(row.max_attempts, 9)
        self.assertIsNotNone(row.available_at)
        self.assertIsNone(row.locked_by)
        self.assertIsNone(row.locked_until)
        # 说明文字不得命中配置错误标记，避免被凭据恢复流程误判。
        self.assertTrue(row.last_error)
        for marker in jobs.MEDIA_CONFIGURATION_ERROR_MARKERS:
            self.assertNotIn(marker, str(row.last_error).lower())

    def test_paused_archive_media_job_keeps_terminal_outbox_untouched(self) -> None:
        row = types.SimpleNamespace(
            status="failed",
            attempt_count=8,
            max_attempts=8,
            available_at=None,
            locked_by=None,
            locked_until=None,
            last_error="permanent error",
        )

        class FakeDb:
            def scalar(self, _statement: object) -> object:
                return row

        @contextmanager
        def fake_session_scope():
            yield FakeDb()

        with (
            patch.dict(
                "os.environ", {"BBW_COMPATIBILITY_MODE": "paused"}, clear=False
            ),
            patch.object(jobs, "session_scope", fake_session_scope),
        ):
            result = jobs.archive_media_job(str(uuid.uuid4()))

        self.assertTrue(result["ignored"])
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.max_attempts, 8)
        self.assertEqual(row.last_error, "permanent error")


class RetirementCliTests(unittest.TestCase):
    def test_preview_is_aggregate_only_and_default_cli_is_read_only(self) -> None:
        class PreviewDb:
            def __init__(self) -> None:
                self.execute_calls = 0
                self.scalar_calls = 0
                self.scalar_statements: list[object] = []

            def execute(self, _statement: object) -> list[object]:
                self.execute_calls += 1
                if self.execute_calls == 1:
                    return [
                        types.SimpleNamespace(status="pending", item_count=3),
                        types.SimpleNamespace(status="completed", item_count=5),
                        types.SimpleNamespace(status="cancelled", item_count=7),
                    ]
                return [
                    types.SimpleNamespace(status="retry", item_count=2),
                    types.SimpleNamespace(status="delivered", item_count=11),
                ]

            def scalar(self, statement: object) -> int:
                self.scalar_calls += 1
                self.scalar_statements.append(statement)
                return 13 if self.scalar_calls == 1 else 17

        db = PreviewDb()
        preview = compatibility_retirement.inspect_retirement(
            db, mode="paused"
        )
        self.assertTrue(preview.dry_run)
        self.assertEqual(preview.ordinary_outbox_total, 15)
        self.assertEqual(preview.ordinary_outbox_cancellable, 3)
        self.assertEqual(preview.optional_tim_cancellable, 2)
        self.assertEqual(preview.media_archive_excluded, 13)
        self.assertEqual(preview.required_tim_excluded, 17)
        media_archive_sql = str(
            db.scalar_statements[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("operation_type = 'media.archive'", media_archive_sql)
        self.assertIn(
            "operation_type LIKE 'compatibility.media.archive%%'",
            media_archive_sql,
        )

        @contextmanager
        def fake_scope():
            yield db

        output = io.StringIO()
        with (
            patch.object(
                compatibility_retirement,
                "get_settings",
                return_value=types.SimpleNamespace(
                    compatibility_mode="paused"
                ),
            ),
            patch.object(
                compatibility_retirement, "session_scope", fake_scope
            ),
            patch.object(
                compatibility_retirement, "archive_retired_work"
            ) as archive,
        ):
            self.assertEqual(
                compatibility_retirement.main([], stdout=output), 0
            )
        archive.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["dry_run"])

    def test_archive_requires_retired_mode_and_cancels_only_optional_work(self) -> None:
        class Result:
            def __init__(self, rowcount: int) -> None:
                self.rowcount = rowcount

        class ArchiveDb:
            def __init__(self) -> None:
                self.calls = 0
                self.flushed = False

            def execute(self, _statement: object) -> Result:
                self.calls += 1
                return Result(4 if self.calls == 1 else 6)

            def flush(self) -> None:
                self.flushed = True

        with self.assertRaises(RuntimeError):
            compatibility_retirement.archive_retired_work(
                ArchiveDb(), mode="paused", now=NOW
            )

        db = ArchiveDb()
        archived = compatibility_retirement.archive_retired_work(
            db, mode="retired", now=NOW
        )
        self.assertTrue(db.flushed)
        self.assertEqual(archived.compatibility_outbox_cancelled, 4)
        self.assertEqual(archived.optional_tim_cancelled, 6)
        self.assertEqual(archived.archived_at, NOW.isoformat())

    def test_cancelled_outboxes_are_terminal_for_readiness(self) -> None:
        summary = _outbox_summary(
            [
                types.SimpleNamespace(
                    status="completed", item_count=3, oldest_at=NOW
                ),
                types.SimpleNamespace(
                    status="cancelled", item_count=5, oldest_at=NOW
                ),
                types.SimpleNamespace(
                    status="failed", item_count=7, oldest_at=NOW
                ),
            ]
        )
        self.assertEqual(summary["total"], 15)
        self.assertEqual(summary["unfinished"], 7)


if __name__ == "__main__":
    unittest.main()
