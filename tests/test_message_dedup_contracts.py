from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from bbw_web.archive_api import (
    _archived_message_item,
    _deduplicate_archived_message_items,
)
from bbw_web.jobs import (
    _ingest_message,
    _merge_message_metadata,
    _message_identifier,
    _tim_message_random,
    _tim_message_sequence,
    archive_message_batch_job,
)
from bbw_web.normalize import normalize_message


ROOT = Path(__file__).resolve().parents[1]


class TimMessageDeduplicationTests(unittest.TestCase):
    def test_sdk_and_roaming_ids_share_one_canonical_identifier(self) -> None:
        sdk = {"upstream_message_id": "144115244515357923-1784548811-44783465"}
        history = {"upstream_message_id": "2000110001_44783465_1784548811"}

        self.assertEqual(_tim_message_random(sdk), "44783465")
        self.assertEqual(_tim_message_random(history), "44783465")
        self.assertEqual(
            _message_identifier(
                sdk,
                "24564",
                sender_uid="467615",
                recipient_uid="24564",
            ),
            _message_identifier(
                history,
                "24564",
                sender_uid="467615",
                recipient_uid="24564",
            ),
        )

    def test_direction_and_participants_keep_random_collisions_separate(self) -> None:
        report = {"message_random": "44783465"}

        outgoing = _message_identifier(
            report,
            "24564",
            sender_uid="467615",
            recipient_uid="24564",
        )
        incoming = _message_identifier(
            report,
            "24564",
            sender_uid="24564",
            recipient_uid="467615",
        )

        self.assertNotEqual(outgoing, incoming)

    def test_unrecognized_ids_keep_existing_upstream_identity(self) -> None:
        report = {"upstream_message_id": "custom-message-id"}

        self.assertEqual(
            _message_identifier(
                report,
                "24564",
                sender_uid="467615",
                recipient_uid="24564",
            ),
            "custom-message-id",
        )

    def test_metadata_merge_preserves_history_key_and_sdk_client_key(self) -> None:
        sdk = {
            "source": "tim_sdk",
            "message_key": "web-message:abc",
            "message_sequence": "",
            "client_message_key": "web-message:abc",
            "raw_upstream_message_id": "144115244515357923-1784548811-44783465",
            "raw_upstream_message_ids": ["144115244515357923-1784548811-44783465"],
            "message_random": "44783465",
            "is_peer_read": False,
            "revoked": False,
        }
        history = {
            "source": "history",
            "message_key": "2000110001_44783465_1784548811",
            "message_sequence": "2000110001",
            "client_message_key": "",
            "raw_upstream_message_id": "2000110001_44783465_1784548811",
            "raw_upstream_message_ids": ["2000110001_44783465_1784548811"],
            "message_random": "44783465",
            "is_peer_read": True,
            "revoked": True,
        }

        merged = _merge_message_metadata(sdk, history)
        repeated_sdk = _merge_message_metadata(merged, sdk)

        self.assertEqual(repeated_sdk["source"], "history")
        self.assertEqual(repeated_sdk["message_key"], "2000110001_44783465_1784548811")
        self.assertEqual(repeated_sdk["message_sequence"], "2000110001")
        self.assertEqual(repeated_sdk["client_message_key"], "web-message:abc")
        self.assertTrue(repeated_sdk["is_peer_read"])
        self.assertTrue(repeated_sdk["revoked"])
        self.assertEqual(len(repeated_sdk["raw_upstream_message_ids"]), 2)

    def test_normalizer_keeps_explicit_tim_random(self) -> None:
        normalized = normalize_message(
            {
                "From_Account": "467615",
                "To_Account": "24564",
                "MsgTimeStamp": 1784548811,
                "MsgKey": "2000110001_44783465_1784548811",
                "MsgSeq": 2000110001,
                "MsgRandom": 44783465,
                "MsgBody": [
                    {
                        "MsgType": "TIMTextElem",
                        "MsgContent": {"Text": "你好你好"},
                    }
                ],
            }
        )

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["message_random"], "44783465")

    def test_revoked_numeric_id_is_recovered_as_message_sequence(self) -> None:
        self.assertEqual(
            _tim_message_sequence(
                {
                    "upstream_message_id": "1819180101",
                    "revoked": True,
                }
            ),
            "1819180101",
        )
        self.assertEqual(
            _tim_message_sequence(
                {
                    "upstream_message_id": "1819180101",
                    "revoked": False,
                }
            ),
            "",
        )

    def test_revoke_event_merges_into_existing_complete_message(self) -> None:
        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        occurred_at = datetime(2026, 7, 21, 10, 52, 57, tzinfo=UTC)
        canonical_id = _message_identifier(
            {"message_random": "38490833"},
            "24564",
            sender_uid="467615",
            recipient_uid="24564",
        )
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id=canonical_id,
            direction="outgoing",
            sender_upstream_uid="467615",
            recipient_upstream_uid="24564",
            message_type="text",
            body="aa？！?",
            status="sent",
            occurred_at=occurred_at,
            extra_data={
                "message_sequence": "1819180101",
                "message_random": "38490833",
                "revoked": False,
            },
        )
        repository = Mock()
        db = Mock()
        report = {
            "peer_uid": "24564",
            "direction": "outgoing",
            "upstream_message_id": "1819180101",
            "revoked": True,
            "source": "tim_sdk",
            "sent_at": occurred_at.isoformat(),
            "message_type": "text",
            "text": "",
        }

        with (
            patch(
                "bbw_web.jobs._upsert_conversation",
                return_value=SimpleNamespace(id=conversation_id),
            ),
            patch("bbw_web.jobs.MessageRepository", return_value=repository),
            patch("bbw_web.jobs._find_message_by_sequence", return_value=existing),
        ):
            row, created, outbox_id = _ingest_message(
                db,
                settings=SimpleNamespace(message_retention_days=180),
                user=SimpleNamespace(id=owner_id, chat_retention_days=180),
                account=SimpleNamespace(upstream_uid="467615"),
                report=report,
            )

        self.assertIs(row, existing)
        self.assertFalse(created)
        self.assertIsNone(outbox_id)
        self.assertEqual(row.upstream_message_id, canonical_id)
        self.assertEqual(row.body, "aa？！?")
        self.assertEqual(row.status, "revoked")
        self.assertEqual(row.extra_data["message_sequence"], "1819180101")
        self.assertEqual(row.extra_data["message_random"], "38490833")
        repository.insert_idempotent.assert_not_called()

    def test_complete_message_upgrades_sequence_only_revoke_row(self) -> None:
        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        occurred_at = datetime(2026, 7, 21, 10, 52, 57, tzinfo=UTC)
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="1819180101",
            direction="outgoing",
            sender_upstream_uid="467615",
            recipient_upstream_uid="24564",
            message_type="text",
            body=None,
            status="revoked",
            occurred_at=occurred_at,
            extra_data={
                "message_sequence": "1819180101",
                "message_random": "",
                "revoked": True,
            },
        )
        repository = Mock()
        repository.get_by_upstream.return_value = None
        db = Mock()
        report = {
            "peer_uid": "24564",
            "direction": "outgoing",
            "upstream_message_id": "1819180101_38490833_1784631177",
            "message_sequence": "1819180101",
            "message_random": "38490833",
            "revoked": False,
            "source": "history",
            "sent_at": occurred_at.isoformat(),
            "message_type": "text",
            "text": "aa？！?",
        }
        canonical_id = _message_identifier(
            report,
            "24564",
            sender_uid="467615",
            recipient_uid="24564",
        )

        with (
            patch(
                "bbw_web.jobs._upsert_conversation",
                return_value=SimpleNamespace(id=conversation_id),
            ),
            patch("bbw_web.jobs.MessageRepository", return_value=repository),
            patch("bbw_web.jobs._find_message_by_sequence", return_value=existing),
        ):
            row, created, outbox_id = _ingest_message(
                db,
                settings=SimpleNamespace(message_retention_days=180),
                user=SimpleNamespace(id=owner_id, chat_retention_days=180),
                account=SimpleNamespace(upstream_uid="467615"),
                report=report,
            )

        self.assertIs(row, existing)
        self.assertFalse(created)
        self.assertIsNone(outbox_id)
        self.assertEqual(row.upstream_message_id, canonical_id)
        self.assertEqual(row.body, "aa？！?")
        self.assertEqual(row.status, "revoked")
        self.assertEqual(row.extra_data["message_random"], "38490833")
        repository.insert_idempotent.assert_not_called()

    def test_archive_output_collapses_complete_and_sequence_only_revoke_rows(self) -> None:
        occurred_at = datetime(2026, 7, 21, 10, 52, 57, tzinfo=UTC)
        complete = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="tim-c2c:canonical",
            extra_data={
                "message_key": "1819180101_38490833_1784631177",
                "message_sequence": "1819180101",
                "message_random": "38490833",
                "revoked": True,
            },
            body="aa？！?",
            message_type="text",
            sender_upstream_uid="467615",
            recipient_upstream_uid="24564",
            direction="outgoing",
            status="revoked",
            occurred_at=occurred_at,
        )
        weak = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="1819180101",
            extra_data={},
            body=None,
            message_type="text",
            sender_upstream_uid="467615",
            recipient_upstream_uid="24564",
            direction="outgoing",
            status="revoked",
            occurred_at=occurred_at,
        )

        items = _deduplicate_archived_message_items(
            [_archived_message_item(complete), _archived_message_item(weak)]
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "tim-c2c:canonical")
        self.assertEqual(items[0]["sequence"], "1819180101")
        self.assertEqual(items[0]["message_random"], "38490833")
        self.assertEqual(items[0]["text"], "aa？！?")
        self.assertTrue(items[0]["revoked"])

    def test_archive_output_hides_received_flash_source_addresses(self) -> None:
        received = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="received-flash",
            extra_data={
                "flash_id": "flash-1",
                "media_report": {
                    "url": "https://oss.banghua.xin/images/received.jpg",
                    "thumbnail": "https://oss.banghua.xin/images/received-thumb.jpg",
                    "mime": "image/jpeg",
                    "size": 128,
                },
            },
            body="[闪图]",
            message_type="flash",
            sender_upstream_uid="467615",
            recipient_upstream_uid="24564",
            direction="incoming",
            status="sent",
            occurred_at=datetime(2026, 7, 21, 10, 52, 57, tzinfo=UTC),
        )
        sent = SimpleNamespace(
            **{
                **received.__dict__,
                "id": uuid.uuid4(),
                "upstream_message_id": "sent-flash",
                "sender_upstream_uid": "24564",
                "recipient_upstream_uid": "467615",
                "direction": "outgoing",
            }
        )

        received_item = _archived_message_item(received)
        sent_item = _archived_message_item(sent)

        self.assertNotIn("url", received_item["media"])
        self.assertNotIn("thumbnail", received_item["media"])
        self.assertEqual(received_item["media"]["mime"], "image/jpeg")
        self.assertEqual(
            sent_item["media"]["url"],
            "https://oss.banghua.xin/images/received.jpg",
        )

    def test_data_migration_uses_runtime_canonical_algorithm(self) -> None:
        path = ROOT / "migrations" / "versions" / "20260720_0005_deduplicate_tim_messages.py"
        spec = importlib.util.spec_from_file_location("message_dedup_migration", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        expected = _message_identifier(
            {"message_random": "44783465"},
            "24564",
            sender_uid="467615",
            recipient_uid="24564",
        )
        self.assertEqual(module._canonical_id("467615", "24564", "44783465"), expected)

    def test_archive_batch_reuses_binding_transaction_and_media_dispatch(self) -> None:
        owner_id = uuid.uuid4()
        account_id = uuid.uuid4()
        first_conversation = SimpleNamespace(id=uuid.uuid4())
        second_conversation = SimpleNamespace(id=uuid.uuid4())
        first_message = SimpleNamespace(id=uuid.uuid4())
        second_message = SimpleNamespace(id=uuid.uuid4())
        first_outbox = uuid.uuid4()
        second_outbox = uuid.uuid4()
        db = Mock()
        user = SimpleNamespace(id=owner_id, chat_retention_days=180)
        account = SimpleNamespace(id=account_id, upstream_uid="467615")
        payloads = [
            {
                "idempotency_key": "message-one",
                "peer_uid": "24564",
                "conversation_id": "C2C24564",
                "source": "tim_sdk",
                "sent_at": "2026-07-21T10:52:57Z",
            },
            {
                "idempotency_key": "message-two",
                "peer_uid": "24565",
                "conversation_id": "C2C24565",
                "source": "history",
                "sent_at": "2026-07-21T10:53:57Z",
            },
        ]

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.jobs.session_scope", new=fake_session_scope),
            patch("bbw_web.jobs.get_settings", return_value=SimpleNamespace()),
            patch(
                "bbw_web.jobs._load_owner_binding",
                return_value=(user, account),
            ) as load_binding,
            patch(
                "bbw_web.jobs._upsert_conversations",
                return_value={
                    "C2C24564": first_conversation,
                    "C2C24565": second_conversation,
                },
            ) as upsert_conversations,
            patch(
                "bbw_web.jobs._ingest_message",
                side_effect=[
                    (first_message, True, first_outbox),
                    (second_message, False, second_outbox),
                ],
            ) as ingest_message,
            patch(
                "bbw_web.jobs._dispatch_media_outboxes",
                return_value=2,
            ) as dispatch_media,
        ):
            result = archive_message_batch_job(
                str(owner_id), str(account_id), payloads
            )

        load_binding.assert_called_once_with(db, owner_id, account_id)
        upsert_conversations.assert_called_once()
        self.assertEqual(len(upsert_conversations.call_args.kwargs["candidates"]), 2)
        self.assertEqual(ingest_message.call_count, 2)
        self.assertIs(
            ingest_message.call_args_list[0].kwargs["conversation"],
            first_conversation,
        )
        self.assertIs(
            ingest_message.call_args_list[1].kwargs["conversation"],
            second_conversation,
        )
        dispatch_media.assert_called_once_with(
            [first_outbox, second_outbox], limit=2
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["media_dispatched"], 2)


class FrontendMessageDeduplicationContracts(unittest.TestCase):
    def test_frontend_prefers_web_canonical_ids_and_tim_mirror_namespace(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        canonical = source.split("function canonicalMessageID", 1)[1].split(
            "function numericMessageValue", 1
        )[0]
        identity = source.split("function messageIdentityKey(entry)", 1)[1].split(
            "function messagesReferToSameMessage", 1
        )[0]
        same_message = source.split("function messagesReferToSameMessage", 1)[1].split(
            "function compareMessageOrder", 1
        )[0]
        entry = source.split("function timMessageEntry", 1)[1].split(
            "function chatMessageReadState", 1
        )[0]

        self.assertIn("parsedCloud?.bbw_message", canonical)
        self.assertIn('["message_id", "messageId"]', canonical)
        self.assertIn("const canonicalID = canonicalMessageID(entry);", identity)
        self.assertIn("return `canonical|${canonicalID}`;", identity)
        self.assertIn(
            "if (leftCanonicalID && rightCanonicalID) return leftCanonicalID === rightCanonicalID;",
            same_message,
        )
        self.assertIn("canonicalMessageId: canonicalID", entry)
        self.assertIn("clientMessageId:", entry)
        self.assertIn("compatibility_sync:", entry)
        self.assertIn("tim_mirror_status:", entry)
        self.assertIn("keys.add(`canonical|${canonicalID}`)", source)
        self.assertIn("keys.add(`client-message|${clientMessageID}`)", source)

    def test_web_local_canonical_revoke_state_wins_over_tim_compatibility(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        authority = source.split("function isWebLocalCanonicalMessage", 1)[1].split(
            "function numericMessageValue", 1
        )[0]
        entry = source.split("function timMessageEntry", 1)[1].split(
            "function chatMessageReadState", 1
        )[0]
        merge = source.split("function mergePeerMessages(peer, incoming)", 1)[1].split(
            "async function loadConversationMessages", 1
        )[0]
        pending_revoke = source.split("function mergePendingMessageRevocations", 1)[1].split(
            "function deferReplayedMessageRevocation", 1
        )[0]
        realtime_revoke = source.split("function applyMessageRevokedEvent", 1)[1].split(
            "function attachTimHandlers", 1
        )[0]

        self.assertIn("provider,", entry)
        self.assertIn("canonicalAuthority,", entry)
        self.assertIn("preferredCanonicalMessageAuthority(previous, entry)", merge)
        self.assertIn("const authorityOwnsRevocationState = Boolean(", merge)
        self.assertIn("revoked: mergedMessageRevoked(previous, entry)", merge)
        self.assertIn("text: authorityOwnsRevocationState", merge)
        self.assertIn("media: authorityOwnsRevocationState", merge)
        self.assertIn("canonicalAuthority: Boolean(authority)", merge)
        self.assertGreaterEqual(
            pending_revoke.count("shouldApplyCompatibilityRevocation(previous, revoked)"),
            2,
        )
        self.assertIn(
            "if (!shouldApplyCompatibilityRevocation(previous, revoked)) return;",
            realtime_revoke,
        )
        self.assertLess(
            realtime_revoke.index("shouldApplyCompatibilityRevocation(previous, revoked)"),
            realtime_revoke.index("releaseMessageLocalMedia(previous)"),
        )
        self.assertLess(
            realtime_revoke.index("shouldApplyCompatibilityRevocation(previous, revoked)"),
            realtime_revoke.index("archiveMessageBestEffort(archived)"),
        )

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = (
            "function canonicalMessageID(entry) { return String(entry?.canonicalMessageId || entry?.canonical_message_id || ''); }\n"
            + "function isWebLocalCanonicalMessage"
            + authority
            + "\n"
            + r"""
const canonicalActive = {
  canonicalMessageId: "canonical-1",
  provider: "web-local",
  source: "archive",
  revoked: false,
};
const canonicalRevoked = { ...canonicalActive, revoked: true };
const localAckActive = {
  canonicalMessageId: "canonical-1",
  source: "web-local",
  revoked: false,
};
const timRevoked = {
  canonicalMessageId: "canonical-1",
  provider: "tim",
  source: "tim",
  revoked: true,
};
const timActive = { ...timRevoked, revoked: false };
if (mergedMessageRevoked(canonicalActive, timRevoked) !== false) {
  throw new Error("TIM revoked state overrode active canonical state");
}
if (mergedMessageRevoked(timRevoked, canonicalActive) !== false) {
  throw new Error("canonical state depended on merge order");
}
if (mergedMessageRevoked(canonicalRevoked, timActive) !== true) {
  throw new Error("canonical revoked state was lost");
}
if (mergedMessageRevoked(localAckActive, timRevoked) !== false) {
  throw new Error("local canonical acknowledgement was not authoritative");
}
if (shouldApplyCompatibilityRevocation(canonicalActive, timRevoked) !== false) {
  throw new Error("TIM revoke event was allowed to overwrite canonical state");
}
if (shouldApplyCompatibilityRevocation(timActive, timRevoked) !== true) {
  throw new Error("ordinary TIM revoke event was incorrectly ignored");
}
"""
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_upserts_realtime_and_loaded_messages_by_tim_random(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")

        self.assertIn("function timMessageRandom(message)", source)
        self.assertIn("function messageIdentityKey(entry)", source)
        self.assertIn("function messageIdentityLookupKeys(entry)", source)
        self.assertIn("function findIndexedMessage(", source)
        self.assertIn("const key = messageIdentityKey(entry);", source)
        self.assertIn("messagesReferToSameMessage(previous, entry)", source)
        merge = source.split("function mergePeerMessages(peer, incoming)", 1)[1].split(
            "async function loadConversationMessages", 1
        )[0]
        self.assertNotIn("[...byKey.entries()].find", merge)
        self.assertIn("[left.id, right.sequence || timMessageSequence(right)]", source)
        self.assertIn("entry.recalledText ||", source)
        self.assertIn("mergePeerMessages(entry.peer, [entry]);", source)
        self.assertIn("message_random: messageRandom", source)

    def test_chat_updates_use_indexed_incremental_dom_rendering(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        incremental = source.split("function renderChatMessageIncrementally", 1)[1].split(
            "function renderChatLog", 1
        )[0]
        full_render = source.split("function renderChatLog", 1)[1].split(
            "function cancelChatLogAutoScroll", 1
        )[0]
        add_message = source.split("function addImMessage", 1)[1].split(
            "function trimChatMessages", 1
        )[0]

        self.assertIn("function chatMessageRowHtml(entry)", source)
        self.assertIn("const CHAT_LOG_MESSAGE_NODES = new WeakMap();", source)
        self.assertIn("findRenderedChatMessageNode(log, entry)", incremental)
        self.assertIn("previousRow.replaceWith(nextRow)", incremental)
        self.assertIn("log.append(nextRow)", incremental)
        self.assertIn("lastRenderedChatMessageRow(log)", incremental)
        self.assertIn("chatMessageFitsRenderedOrder(entry, previousRow)", incremental)
        self.assertNotIn('log.querySelectorAll(".chat-message-row")', incremental)
        self.assertIn("rebuildChatMessageNodeIndex(log);", full_render)
        self.assertIn(
            "if (!renderChatMessageIncrementally(log, renderedEntry)) renderChatLog(log);",
            add_message,
        )
        self.assertNotIn("\n    renderChatLog(log);", add_message)

    def test_send_ack_reconciles_the_optimistic_and_realtime_dom_rows(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        incremental = source.split("function renderChatMessageIncrementally", 1)[1].split(
            "function renderChatLog", 1
        )[0]
        refresh = source.split("function refreshChatMessageEntries", 1)[1].split(
            "function closeChatMessageActions", 1
        )[0]
        update_local = source.split("function updateLocalMessage", 1)[1].split(
            "function appendLocalMessage", 1
        )[0]

        self.assertIn("previousEntry = null", incremental)
        self.assertIn("findRenderedChatMessageNode(log, previousEntry)", incremental)
        self.assertIn(
            "if (entryRow && previousEntryRow && entryRow !== previousEntryRow) return false;",
            incremental,
        )
        self.assertIn("const previousRow = entryRow || previousEntryRow;", incremental)
        self.assertIn("previousEntries = []", refresh)
        self.assertIn(
            "renderChatMessageIncrementally(log, entry, previousEntry)", refresh
        )
        self.assertIn(
            "refreshChatMessageEntry(merged, { previousEntry: current })",
            update_local,
        )

    def test_long_chats_render_a_bounded_expandable_dom_window(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        chat_log = source.split("function chatLogHtml()", 1)[1].split(
            "function chatMediaNodeIdentity", 1
        )[0]
        incremental = source.split("function renderChatMessageIncrementally", 1)[1].split(
            "function renderChatLog", 1
        )[0]
        older_loader = source.split("async function loadOlderConversationMessages", 1)[1].split(
            "function activeConversation", 1
        )[0]
        search_jump = source.split("async function jumpToMessageSearchResult", 1)[1].split(
            "function clearConversationBatchDeleteConfirmation", 1
        )[0]

        self.assertIn("const CHAT_MESSAGE_RENDER_WINDOW = 400;", source)
        self.assertIn("const CHAT_MESSAGE_RENDER_PAGE = 200;", source)
        self.assertIn("imMessageRenderLimits: new Map()", source)
        self.assertIn("allEntries.slice(hiddenCount)", chat_log)
        self.assertIn("chatHistoryStatusHtml(hiddenCount)", chat_log)
        self.assertIn("enforceChatMessageRenderWindow(log, entry.peer)", incremental)
        self.assertIn("revealOlderRenderedMessages(target, log)", older_loader)
        self.assertLess(
            older_loader.index("revealOlderRenderedMessages(target, log)"),
            older_loader.index("S.imMessageHistoryExhaustedPeers.has(target)"),
        )
        self.assertIn("expandChatMessageRenderLimit(target, newCount)", older_loader)
        self.assertIn("ensureChatMessageRenderWindowIncludes(entry)", search_jump)


if __name__ == "__main__":
    unittest.main()
