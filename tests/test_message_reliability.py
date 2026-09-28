from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bbw_protocol.adapters.jd_chat import JdChatClient, JdChatResult
from bbw_web import bff_server, jobs
from bbw_web.persistence import RuntimePersistence


ROOT = Path(__file__).resolve().parents[1]


class ReadSyncReliabilityTests(unittest.TestCase):
    def test_suspended_read_is_not_acknowledged_without_durable_state(self) -> None:
        class Redis:
            def __init__(self) -> None:
                self.values = {"test:tim-rest:suspended": b"60020"}

            def get(self, key):
                return self.values.get(key)

        persistence = RuntimePersistence.__new__(RuntimePersistence)
        persistence.settings = SimpleNamespace(redis_prefix="test")
        persistence.redis = Redis()
        with self.assertRaisesRegex(RuntimeError, "TIM service suspended"):
            persistence.enqueue_tim_read_sync(account_uid="42", peers=["9"])
        self.assertEqual(list(persistence.redis.values), ["test:tim-rest:suspended"])

    def test_worker_retries_instead_of_finishing_a_suspended_read(self) -> None:
        class Lock:
            def acquire(self, **_kwargs):
                return True

            def release(self):
                pass

        class Redis:
            def lock(self, *_args, **_kwargs):
                return Lock()

            def get(self, _key):
                return b"60020"

            def close(self):
                pass

        with patch.object(jobs, "get_settings", return_value=SimpleNamespace(redis_prefix="test", redis_url="redis://unused")):
            with patch.object(jobs.Redis, "from_url", return_value=Redis()):
                with self.assertRaisesRegex(RuntimeError, "TIM service suspended"):
                    jobs.sync_tim_conversation_read("test:im-read:identity")

    def test_default_worker_promotes_scheduled_retries(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        worker = compose.split("\n  worker:\n", 1)[1].split("\n  im-ingest-worker:\n", 1)[0]
        self.assertIn('BBW_RQ_WITH_SCHEDULER: "true"', worker)


class JdDeliveryReliabilityTests(unittest.TestCase):
    def test_jd_read_routes_match_current_openapi_contract(self) -> None:
        client = JdChatClient("token", base_url="https://example.invalid")
        try:
            with patch.object(client, "_request", return_value=JdChatResult(True, 200)) as request:
                self.assertTrue(client.mark_conversation_read(peer_id="9").ok)
                request.assert_called_with(
                    "POST", "/api/messages/conversations/9/read", timeout=None,
                )
                self.assertTrue(client.mark_all_conversations_read().ok)
                request.assert_called_with(
                    "POST", "/api/messages/conversations/read-all", timeout=None,
                )
                self.assertTrue(client.health().ok)
                request.assert_called_with(
                    "GET", "/api/messages/unread-count", timeout=None,
                )
        finally:
            client.close()

    def test_client_message_id_has_stable_provider_identity(self) -> None:
        first = JdChatClient.message_id_for_client("42", "9", "client-1")
        self.assertEqual(first, JdChatClient.message_id_for_client("42", "9", "client-1"))
        self.assertNotEqual(first, JdChatClient.message_id_for_client("42", "9", "client-2"))
        self.assertNotEqual(first, JdChatClient.message_id_for_client("42", "10", "client-1"))

    def test_negative_ack_is_distinct_from_unknown_delivery(self) -> None:
        result = JdChatResult(
            False,
            502,
            {"delivery_rejected": True, "delivery_uncertain": False, "ws_sent": True},
            "rejected",
        )
        self.assertTrue(bff_server._jd_delivery_rejected(result))
        self.assertEqual(
            bff_server._jd_result_message_id({"data": {"data": {"messageId": "jd-1"}}}),
            "jd-1",
        )

    def test_websocket_negative_ack_does_not_fall_back_to_http(self) -> None:
        sent = []

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def send(self, value):
                sent.append(json.loads(value))

            async def recv(self):
                return json.dumps({"type": "NACK", "content": "rejected"})

        fake_websockets = SimpleNamespace(connect=lambda *_args, **_kwargs: Socket())
        client = JdChatClient("token", base_url="https://example.invalid")
        try:
            with patch.dict(sys.modules, {"websockets": fake_websockets}):
                with patch.object(client, "_request", side_effect=AssertionError("HTTP fallback")):
                    result = client.send_text("42", "9", "hello", client_message_id="client-1")
            self.assertFalse(result.ok)
            self.assertTrue(result.data["delivery_rejected"])
            self.assertFalse(result.data["delivery_uncertain"])
            self.assertEqual(
                sent[0]["messageId"],
                JdChatClient.message_id_for_client("42", "9", "client-1"),
            )
        finally:
            client.close()


class BrowserReadReliabilityTests(unittest.TestCase):
    def test_failed_authoritative_read_restores_unread_badge(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required")
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        report = "function scheduleConversationReadReport" + source.split(
            "function scheduleConversationReadReport", 1
        )[1].split("function markConversationRead", 1)[0]
        self.assertNotIn("sdkReadTask", report)
        script = (
            "const S = {conversationReadReportTimers: new Map(), "
            "conversationReadReportVersions: new Map(), readConversationPeers: new Map([['9', 1]]), "
            "conversations: [{peer: '9', unread_count: 0}]};\n"
            "function reportConversationRead() { return Promise.resolve(false); }\n"
            "function conversationPeer(item) { return item.peer; }\n"
            "function conversationUnreadCount(item) { return Number(item.unread_count || 0); }\n"
            "function recalculateUnreadTotal() {}\n"
            "function isVisibleActiveConversation() { return false; }\n"
            "function toast() {}\n"
            + report
            + "scheduleConversationReadReport('9', 0, 2);\n"
            + "setTimeout(() => { if (S.conversations[0].unread_count !== 2 || "
            "S.readConversationPeers.has('9')) throw new Error('failed read kept optimistic zero'); }, 25);\n"
        )
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True,
            timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_tab_lease_is_not_treated_as_completed_read(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required")
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        batch_report = "async function reportConversationsRead" + source.split(
            "async function reportConversationsRead", 1
        )[1].split("function isVisibleActiveConversation", 1)[0]
        script = (
            "async function runCrossTabOperation() { return null; }\n"
            "function archiveHash(value) { return value; }\n"
            + batch_report
            + "reportConversationsRead(['9']).then((result) => {"
            "if (result !== false) throw new Error('unknown cross-tab read claimed success');"
            "}).catch((error) => { console.error(error); process.exitCode = 1; });\n"
        )
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True,
            timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_per_message_receipt_does_not_mark_other_messages_read(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required")
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        receipt_handler = "function applyPeerReadEvent" + source.split(
            "function applyPeerReadEvent", 1
        )[1].split("function mergeRevokedMessage", 1)[0]
        visibility = "function isVisibleActiveConversation" + source.split(
            "function isVisibleActiveConversation", 1
        )[1].split("function scheduleConversationReadReport", 1)[0]
        script = (
            "const S = {route: 'msg', activePeer: '9', messageSearchOpen: false, "
            "imMessages: [{id: 'old', peer: '9', type: 'mine', timestamp: 1000, peerRead: false}, "
            "{id: 'new', peer: '9', type: 'mine', timestamp: 2000, peerRead: false}]};\n"
            "const document = {hidden: false, hasFocus: () => true, modalOpen: false, "
            "querySelector: () => document.modalOpen ? {} : null};\n"
            "function timEventRows(event) { return event.data; }\n"
            "function timMessageReadTimestamp() { return 0; }\n"
            "function timMessagePeer(item) { return item.peer; }\n"
            "function messageTimestampMs(item) { return item.timestamp; }\n"
            "function refreshChatMessageEntries() { return true; }\n"
            "function refreshChatLog() {}\n"
            + visibility + receipt_handler
            + "if (!isVisibleActiveConversation('9')) throw new Error('visible chat not recognized');\n"
            + "document.hidden = true; if (isVisibleActiveConversation('9')) throw new Error('hidden chat marked visible');\n"
            + "document.hidden = false; S.route = 'nearby'; if (isVisibleActiveConversation('9')) throw new Error('other route marked visible');\n"
            + "S.route = 'msg'; document.modalOpen = true; if (isVisibleActiveConversation('9')) throw new Error('covered chat marked visible');\n"
            + "document.modalOpen = false;\n"
            + "S.route = 'msg'; applyPeerReadEvent({data: [{id: 'old', peer: '9'}]});\n"
            + "if (!S.imMessages[0].peerRead || S.imMessages[1].peerRead) throw new Error('receipt broadened to peer');\n"
        )
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True,
            timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
