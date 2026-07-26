from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
import uuid

from bbw_web import archive_api, bff_server


ROOT = Path(__file__).resolve().parents[1]


class MessageHistoryPaginationBackendTests(unittest.TestCase):
    def test_roaming_history_respects_before_time_and_returns_cursor(self) -> None:
        calls = []
        before_time = int(time.time()) - 100
        message_time = before_time - 10

        class Client:
            def roaming_messages(self, sender, recipient, **kwargs):
                calls.append((sender, recipient, kwargs))
                rows = []
                if sender == "42":
                    rows.append(
                        {
                            "From_Account": "42",
                            "To_Account": "9",
                            "MsgTimeStamp": message_time,
                            "MsgKey": "older-message",
                            "MsgBody": [
                                {
                                    "MsgType": "TIMTextElem",
                                    "MsgContent": {"Text": "更早消息"},
                                }
                            ],
                        }
                    )
                return SimpleNamespace(
                    ok=True,
                    data={"MsgList": rows, "Complete": 1},
                )

        payload = bff_server._tim_roaming_message_envelope(
            Client(),
            "42",
            "9",
            before_time=before_time,
            include_read_state=False,
        )

        self.assertEqual([(call[0], call[1]) for call in calls], [("9", "42"), ("42", "9")])
        self.assertTrue(all(call[2]["max_time"] == before_time for call in calls))
        self.assertEqual(payload["items"][0]["msg_key"], "older-message")
        self.assertEqual(payload["next_before"], str(message_time))
        self.assertFalse(payload["has_more"])

    def test_native_flash_history_marks_existing_claim_without_exposing_media(self) -> None:
        claimed_id = uuid.uuid4()

        class Database:
            @staticmethod
            def scalars(_statement):
                return [claimed_id]

        items = [
            {
                "kind": "flash",
                "flow": "in",
                "provider": "web-local",
                "flash_id": str(claimed_id),
                "media": {"attachment_id": str(claimed_id)},
            }
        ]

        archive_api._annotate_native_flash_claims(Database(), uuid.uuid4(), items)

        self.assertTrue(items[0]["native_flash_claimed"])
        self.assertNotIn("url", items[0]["media"])


class MessageHistoryPaginationFrontendTests(unittest.TestCase):
    def test_chat_top_scroll_loads_older_pages_without_losing_position(self) -> None:
        app_js = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        app_css = (ROOT / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8-sig")
        index_html = (ROOT / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8-sig")

        for marker in (
            "imMessageOlderLoadingPeers: new Set()",
            "imMessageHistoryExhaustedPeers: new Set()",
            "imMessageArchiveCursors: new Map()",
            "async function loadOlderConversationMessages",
            "&before=${encodeURIComponent(beforeSeconds)}",
            'archiveParams.set("cursor", archiveCursor)',
            'archiveParams.set("before", beforeIso)',
            "data?.next_cursor",
            "previousTop + log.scrollHeight - previousHeight",
            '{ passive: true, capture: true }',
            "没有更早的消息",
        ):
            self.assertIn(marker, app_js)
        self.assertIn(".chat-history-status", app_css)
        self.assertIn(
            f'/static/app.js?v={hashlib.sha256((ROOT / "bbw_web" / "static" / "app.js").read_bytes()).hexdigest()[:16]}',
            index_html,
        )

    def test_canonical_web_message_does_not_offer_tim_only_recall(self) -> None:
        app_js = (ROOT / "bbw_web" / "static" / "app.js").read_text(
            encoding="utf-8-sig"
        )
        revoke_action = app_js.split("function revokeActionInfo(entry)", 1)[1].split(
            "function recalledMessageText", 1
        )[0]

        self.assertIn("entry?.canonicalMessageId", revoke_action)
        self.assertIn('String(entry?.source || "").toLowerCase() === "web-local"', revoke_action)
        self.assertLess(
            revoke_action.index("entry?.canonicalMessageId"),
            revoke_action.index("const eligible"),
        )

    def test_local_mode_keeps_native_media_and_never_requests_coordinates(self) -> None:
        app_js = (ROOT / "bbw_web" / "static" / "app.js").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("const directMediaActions = S.messagePolicyReady", app_js)
        self.assertIn("const richMessageActionsAvailable = S.messagePolicyReady", app_js)
        self.assertNotIn("navigator.geolocation", app_js)
        self.assertNotIn('params.set("latitude"', app_js)
        self.assertNotIn('params.set("longitude"', app_js)
        self.assertNotIn("image/bmp", app_js)
        self.assertIn("image/avif", app_js)
        self.assertIn("video/webm", app_js)


if __name__ == "__main__":
    unittest.main()
