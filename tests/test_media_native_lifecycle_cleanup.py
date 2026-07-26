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


if __name__ == "__main__":
    unittest.main()
