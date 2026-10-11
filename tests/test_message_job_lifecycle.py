from __future__ import annotations

import unittest
import uuid
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bbw_prod.compatibility import CompatibilityMode
from bbw_web import jobs


class MessageJobTransportLifecycleTests(unittest.TestCase):
    def mirror(self, transport, *, injected=False):
        delivery_id = uuid.uuid4()
        claimed = SimpleNamespace(attempt=1, payload={
            "from": "42", "to": "9", "text": "hello",
            "canonical_message_id": str(uuid.uuid4()), "client_message_id": "test-client-1",
        })
        with patch.object(jobs, "compatibility_mode", return_value=CompatibilityMode.ENABLED), patch.object(
            jobs, "_claim_tim_message_delivery", return_value=claimed,
        ), patch.object(jobs, "_default_message_send_transport", return_value=transport), patch.object(
            jobs, "_tim_mirror_delivery_is_processing", return_value=True,
        ), patch.object(jobs, "_mark_tim_mirror_delivered", return_value="delivered"), patch.object(
            jobs, "_mark_tim_mirror_failed", return_value=("retry", 30),
        ):
            return jobs.mirror_tim_message_delivery(
                str(delivery_id), transport=transport if injected else None,
            )

    def test_owned_mirror_transport_closes_on_success_rejection_and_exception(self):
        for outcome in (
            SimpleNamespace(ok=True, data={"MsgKey": "upstream-key"}),
            SimpleNamespace(ok=False, error_code=500, error_info="unavailable"),
            RuntimeError("connection lost"),
        ):
            with self.subTest(outcome=type(outcome).__name__):
                sender = Mock()
                if isinstance(outcome, Exception):
                    sender.send_text.side_effect = outcome
                else:
                    sender.send_text.return_value = outcome
                result = self.mirror(sender)
                self.assertEqual(result["ok"], bool(getattr(outcome, "ok", False)))
                sender.close.assert_called_once_with()

    def test_borrowed_mirror_transport_is_not_closed(self):
        sender = Mock()
        sender.send_text.return_value = SimpleNamespace(ok=True, data={"MsgKey": "upstream-key"})
        self.assertTrue(self.mirror(sender, injected=True)["ok"])
        sender.close.assert_not_called()

    def test_cleanup_failure_does_not_retry_a_successful_mirror_send(self):
        sender = Mock()
        sender.send_text.return_value = SimpleNamespace(ok=True, data={"MsgKey": "upstream-key"})
        sender.close.side_effect = RuntimeError("cleanup failed")
        self.assertTrue(self.mirror(sender)["ok"])
        sender.send_text.assert_called_once()
        sender.close.assert_called_once_with()

    def history(self, client, *, fail_ingest=False):
        owner_id, account_id = uuid.uuid4(), uuid.uuid4()
        settings = SimpleNamespace(
            active_sync_seconds=300, inactive_sync_seconds=3600, message_retention_days=180,
        )
        with ExitStack() as stack:
            patches = {
                "compatibility_mode": Mock(return_value=CompatibilityMode.ENABLED),
                "get_settings": Mock(return_value=settings),
                "session_scope": Mock(side_effect=lambda: nullcontext(SimpleNamespace())),
                "_load_owner_binding": Mock(return_value=(None, SimpleNamespace(sync_enabled=True, upstream_uid="42"))),
                "SyncCursorRepository": Mock(return_value=SimpleNamespace(get=lambda *_args: None)),
                "_active_owner_ids": Mock(return_value={owner_id}),
                "_durable_message_conversations": Mock(return_value=[]),
                "_tim_recent_conversations": Mock(return_value=[]),
                "ingest_history_response": Mock(return_value={"conversations": 0}),
                "_save_sync_result": Mock(),
            }
            if fail_ingest:
                patches["ingest_history_response"].side_effect = RuntimeError("database unavailable")
            for name, value in patches.items():
                stack.enter_context(patch.object(jobs, name, value))
            return jobs.sync_account_history(
                str(owner_id), str(account_id), transport_factory=lambda: client,
            )

    def test_history_transport_closes_after_success(self):
        client = SimpleNamespace(close=Mock())
        self.assertTrue(self.history(client)["ok"])
        client.close.assert_called_once_with()

    def test_history_transport_closes_after_ingest_error(self):
        client = SimpleNamespace(close=Mock())
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.history(client, fail_ingest=True)
        client.close.assert_called_once_with()

    def test_history_cleanup_error_preserves_job_result(self):
        client = SimpleNamespace(close=Mock(side_effect=RuntimeError("cleanup failed")))
        self.assertTrue(self.history(client)["ok"])
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
