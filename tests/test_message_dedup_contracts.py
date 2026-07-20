from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from bbw_web.jobs import (
    _merge_message_metadata,
    _message_identifier,
    _tim_message_random,
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
        self.assertIn("mergePeerMessages(entry.peer, [entry]);", source)
        self.assertIn("message_random: messageRandom", source)


if __name__ == "__main__":
    unittest.main()
