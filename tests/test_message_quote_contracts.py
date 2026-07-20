from __future__ import annotations

import json
from pathlib import Path
import unittest

from bbw_protocol.adapters.tim_rest import RestResult, TimRestClient
from bbw_web.archive_api import MessageReport
from bbw_web.jobs import _history_message_report
from bbw_web.message_quote import (
    encode_message_quote,
    extract_message_quote,
    normalize_message_quote,
)


ROOT = Path(__file__).resolve().parents[1]


class MessageQuoteNormalizerTests(unittest.TestCase):
    def test_nested_cloud_custom_data_round_trips(self) -> None:
        quote = {
            "message_id": "message-1",
            "message_random": "778899",
            "sender_uid": "42",
            "sender_name": "测试用户",
            "text": "被引用的消息",
            "kind": "text",
            "sent_at": "1784550000",
        }

        encoded = encode_message_quote(quote)
        payload = json.loads(encoded)

        self.assertEqual(payload["bbw_message"]["version"], 1)
        self.assertEqual(extract_message_quote(encoded), quote)
        self.assertEqual(normalize_message_quote(payload), quote)

    def test_aliases_and_length_limits_are_normalized(self) -> None:
        quote = normalize_message_quote(
            {
                "quote": {
                    "messageId": "m" * 600,
                    "messageRandom": "7" * 100,
                    "senderUid": "u" * 160,
                    "senderName": "n" * 160,
                    "preview": "t" * 700,
                    "message_type": "image",
                    "timestamp": "1" * 100,
                }
            }
        )

        self.assertEqual(len(quote["message_id"]), 512)
        self.assertEqual(len(quote["message_random"]), 80)
        self.assertEqual(len(quote["sender_uid"]), 128)
        self.assertEqual(len(quote["sender_name"]), 120)
        self.assertEqual(len(quote["text"]), 500)
        self.assertEqual(quote["kind"], "image")
        self.assertEqual(len(quote["sent_at"]), 80)

    def test_empty_or_unrelated_cloud_data_is_ignored(self) -> None:
        self.assertEqual(normalize_message_quote({}), {})
        self.assertEqual(extract_message_quote("flash-photo-id"), {})
        self.assertEqual(encode_message_quote(None), "")


class CapturingTimRestClient(TimRestClient):
    def __init__(self) -> None:
        self.calls = []

    def call(self, command, body, *, admin=None):
        self.calls.append((command, body, admin))
        return RestResult(ok=True, action=command, data=body)


class MessageQuoteTransportTests(unittest.TestCase):
    def test_tim_rest_text_send_includes_cloud_custom_data(self) -> None:
        client = CapturingTimRestClient()
        cloud_data = encode_message_quote(
            {
                "message_id": "message-1",
                "sender_uid": "9",
                "text": "原消息",
            }
        )

        result = client.send_text("42", "9", "回复内容", cloud_custom_data=cloud_data)

        self.assertTrue(result.ok)
        command, body, _ = client.calls[-1]
        self.assertEqual(command, "openim/sendmsg")
        self.assertEqual(body["CloudCustomData"], cloud_data)
        self.assertEqual(body["MsgBody"][0]["MsgContent"]["Text"], "回复内容")

    def test_history_ingestion_extracts_quote_snapshot(self) -> None:
        cloud_data = encode_message_quote(
            {
                "message_id": "message-1",
                "message_random": "778899",
                "sender_uid": "9",
                "sender_name": "对方",
                "text": "原消息",
            }
        )

        report = _history_message_report(
            {
                "id": "message-2",
                "from": "42",
                "to": "9",
                "type": "text",
                "text": "回复内容",
                "timestamp": 1784550000,
                "cloud_custom_data": cloud_data,
            },
            account_uid="42",
            requested_peer="9",
        )

        self.assertIsNotNone(report)
        self.assertEqual(report["quote"]["message_id"], "message-1")
        self.assertEqual(report["quote"]["message_random"], "778899")

    def test_archive_report_accepts_quote_snapshot(self) -> None:
        report = MessageReport.model_validate(
            {
                "idempotency_key": "quote-message-1",
                "peer_uid": "9",
                "direction": "outgoing",
                "text": "回复内容",
                "quote": {
                    "message_id": "message-1",
                    "sender_uid": "9",
                    "sender_name": "对方",
                    "text": "原消息",
                },
            }
        )

        self.assertIsNotNone(report.quote)
        self.assertEqual(report.quote.message_id, "message-1")


class FrontendMessageQuoteContracts(unittest.TestCase):
    def test_frontend_supports_quote_actions_transport_and_rendering(self) -> None:
        app_js = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        app_css = (ROOT / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8-sig")
        index_html = (ROOT / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8-sig")
        bff_source = (ROOT / "bbw_web" / "bff_server.py").read_text(encoding="utf-8-sig")

        for marker in (
            'data-action="quote-chat-message"',
            'data-action="cancel-chat-quote"',
            'data-action="jump-to-quoted-message"',
            "function messageQuoteCloudCustomData(quote)",
            "options.cloudCustomData = cloudCustomData",
            "quote: messageQuote",
            "restoreChatMessageQuote(uid)",
            "function toggleChatMessageActions(row)",
            "data-chat-message-bubble",
            'class="chat-message-action quote contextual"',
        ):
            self.assertIn(marker, app_js)
        self.assertIn(".chat-message-quote", app_css)
        self.assertIn(".chat-compose-quote", app_css)
        self.assertIn(".chat-message-row.is-quote-target", app_css)
        self.assertIn(".chat-message-action.contextual", app_css)
        self.assertIn(".chat-message-row.is-actions-open", app_css)
        self.assertIn("message-dedup-message-quote", index_html)
        self.assertIn("message-actions-on-tap", index_html)
        self.assertIn('normalize_message_quote(data.get("quote"))', bff_source)
        self.assertIn('{"cloud_custom_data": quote_cloud_data}', bff_source)


if __name__ == "__main__":
    unittest.main()
