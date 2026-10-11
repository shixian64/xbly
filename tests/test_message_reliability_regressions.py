from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bbw_protocol.adapters import jd_chat
from bbw_protocol.adapters.jd_chat import JdChatClient
from bbw_web import jobs
from bbw_web.persistence import RuntimePersistence


class _MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kwargs):
        self.values[key] = value
        return True

    def lock(self, *_args, **_kwargs):
        return SimpleNamespace(acquire=lambda **_kwargs: True, release=lambda: None)

    def close(self):
        pass


class TimReadIntentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.persistence = RuntimePersistence.__new__(RuntimePersistence)
        self.persistence.settings = SimpleNamespace(redis_prefix="test", redis_url="redis://unused")
        self.persistence.redis = _MemoryRedis()
        self.persistence.default_queue = Mock()
        self.transport = SimpleNamespace(
            mark_c2c_read=Mock(return_value=SimpleNamespace(ok=True)),
            sync_c2c_message_read_receipts=Mock(return_value=SimpleNamespace(ok=True, data={})),
            close=Mock(),
        )
        self.state_key = self.persistence._tim_read_state_key("42", "9")

    def enqueue(self, receipts=()):
        return self.persistence.enqueue_tim_read_sync(
            account_uid="42", peers=["9"], receipt_messages=receipts,
        )

    def run_worker(self):
        with patch.object(jobs, "get_settings", return_value=self.persistence.settings), patch.object(
            jobs.Redis, "from_url", return_value=self.persistence.redis,
        ), patch.object(jobs, "_default_message_read_transport", return_value=self.transport):
            return jobs.sync_tim_conversation_read(self.state_key)

    def test_later_read_with_identical_receipts_is_a_new_intent(self):
        # A later visit must clear newly arrived messages even when the browser
        # has no per-message receipt snapshot, including within the same second.
        with patch("bbw_web.persistence.time.time", return_value=1800000000.0):
            first = self.enqueue()
            self.run_worker()
            second = self.enqueue()
            self.run_worker()
        self.assertEqual(self.transport.mark_c2c_read.call_count, 2)
        self.assertNotEqual(first["job_ids"], second["job_ids"])

    def test_replaying_one_queued_intent_does_not_repeat_upstream_calls(self):
        self.enqueue()
        self.run_worker()
        replay = self.run_worker()
        self.assertEqual(replay["reason"], "already_synced")
        self.transport.mark_c2c_read.assert_called_once_with("42", "9")

    def test_pending_jobs_coalesce_to_latest_intent(self):
        self.enqueue()
        self.enqueue()
        self.run_worker()
        self.run_worker()
        self.transport.mark_c2c_read.assert_called_once_with("42", "9")

    def test_new_identical_intent_during_dispatch_is_not_lost(self):
        self.enqueue()
        def dispatch(*_args):
            if self.transport.mark_c2c_read.call_count == 1:
                self.enqueue()
            return SimpleNamespace(ok=True)
        self.transport.mark_c2c_read.side_effect = dispatch
        result = self.run_worker()
        self.assertEqual(self.transport.mark_c2c_read.call_count, 2)
        self.assertEqual(len(result["processed_versions"]), 2)
        self.assertEqual(len(set(result["processed_versions"])), 2)

    def test_receipt_snapshot_aba_does_not_reuse_old_intent(self):
        self.enqueue([{"MsgSequence": 1}])
        self.run_worker()
        self.enqueue([{"MsgSequence": 2}])
        self.enqueue([{"MsgSequence": 1}])
        self.run_worker()
        self.assertEqual(self.transport.mark_c2c_read.call_count, 2)


class JdAckCorrelationRegressionTests(unittest.TestCase):
    def send(self, frames):
        sent = []
        pending = list(frames)
        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def send(self, value):
                sent.append(json.loads(value))

            async def recv(self):
                item = pending.pop(0)
                if callable(item):
                    item = item(sent[0]["messageId"])
                return json.dumps(item)

        client = JdChatClient("test-only-token", base_url="https://example.invalid")
        try:
            with patch.dict(sys.modules, {"websockets": SimpleNamespace(connect=lambda *_args, **_kwargs: Socket())}), patch.object(
                client, "_request", side_effect=AssertionError("HTTP fallback would duplicate the message"),
            ):
                return client.send_text("42", "9", "hello", client_message_id="client-1")
        finally:
            client.close()

    def test_unrelated_ack_is_skipped_until_current_ack(self):
        result = self.send([
            {"type": "ACK", "content": "previous-message"},
            lambda message_id: {"type": "ACK", "content": message_id},
        ])
        self.assertTrue(result.ok, result.error_info)

    def test_unrelated_negative_ack_cannot_reject_current_message(self):
        result = self.send([
            {"type": "NACK", "messageId": "previous-message", "content": "rejected"},
            lambda message_id: {"type": "ACK", "content": message_id},
        ])
        self.assertTrue(result.ok, result.error_info)

    def test_explicit_ack_identity_takes_precedence_over_human_content(self):
        result = self.send([
            lambda message_id: {"type": "ACK", "messageId": message_id, "content": "accepted"},
        ])
        self.assertTrue(result.ok, result.error_info)

    def test_notifications_do_not_consume_the_ack_wait_budget(self):
        result = self.send([
            *[{"type": "PRESENCE", "content": "online"}] * 4,
            lambda message_id: {"type": "ACK", "content": message_id},
        ])
        self.assertTrue(result.ok, result.error_info)

    def test_busy_socket_cannot_extend_the_total_ack_deadline(self):
        ticks = iter(range(10))
        clock = SimpleNamespace(time=lambda: 1800000000.0, monotonic=lambda: next(ticks))
        with patch.object(jd_chat, "time", clock):
            result = self.send([{"type": "PRESENCE", "content": "online"}] * 10)
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 504)
        self.assertTrue(result.data["delivery_uncertain"])

    def test_apk_content_identity_is_preserved_when_ack_has_own_id(self):
        result = self.send([
            lambda message_id: {"type": "ACK", "messageId": "ack-frame-id", "content": message_id},
        ])
        self.assertTrue(result.ok, result.error_info)

    def test_nested_ack_and_structured_content_are_correlated(self):
        result = self.send([
            {"data": {"type": "ACK", "content": {"messageId": "previous-message"}}},
            lambda message_id: {"data": {"type": "ACK", "content": {"messageId": message_id}}},
        ])
        self.assertTrue(result.ok, result.error_info)

    def test_unrelated_ack_without_confirmation_remains_uncertain(self):
        result = self.send([{"type": "ACK", "content": "previous-message"}] * 3)
        self.assertFalse(result.ok)
        self.assertTrue(result.data["delivery_uncertain"])

    def test_current_negative_ack_still_reports_rejection(self):
        result = self.send([
            lambda message_id: {"type": "NACK", "messageId": message_id, "content": "rejected"},
        ])
        self.assertFalse(result.ok)
        self.assertTrue(result.data["delivery_rejected"])
        self.assertFalse(result.data["delivery_uncertain"])


if __name__ == "__main__":
    unittest.main()
