from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
import time
import unittest

from bbw_web import bff_server


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


class MessageHistoryPaginationFrontendTests(unittest.TestCase):
    def test_chat_top_scroll_loads_older_pages_without_losing_position(self) -> None:
        app_js = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        app_css = (ROOT / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8-sig")
        index_html = (ROOT / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8-sig")

        for marker in (
            "imMessageOlderLoadingPeers: new Set()",
            "imMessageHistoryExhaustedPeers: new Set()",
            "async function loadOlderConversationMessages",
            "&before=${encodeURIComponent(beforeSeconds)}",
            "&before=${encodeURIComponent(beforeIso)}",
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


if __name__ == "__main__":
    unittest.main()
