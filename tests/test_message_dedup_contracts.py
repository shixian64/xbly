from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
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


class FrontendMessageDeduplicationContracts(unittest.TestCase):
    def test_frontend_upserts_realtime_and_loaded_messages_by_tim_random(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")

        self.assertIn("function timMessageRandom(message)", source)
        self.assertIn("function messageIdentityKey(entry)", source)
        self.assertIn("const key = messageIdentityKey(entry);", source)
        self.assertIn("messagesReferToSameMessage(previous, entry)", source)
        self.assertIn("[left.id, right.sequence || timMessageSequence(right)]", source)
        self.assertIn("entry.recalledText ||", source)
        self.assertIn("mergePeerMessages(entry.peer, [entry]);", source)
        self.assertIn("message_random: messageRandom", source)


if __name__ == "__main__":
    unittest.main()
