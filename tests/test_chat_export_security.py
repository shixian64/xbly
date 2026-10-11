from __future__ import annotations

import csv
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bbw_web import bff_server


class ChatExportCsvSecurityTests(unittest.TestCase):
    def export(self, raw, *, allowed=True):
        result = SimpleNamespace(ok=True, raw=raw, kind="text", headers={"content-type": "text/csv"})
        upstream = Mock(return_value=result)
        user = SimpleNamespace(app=SimpleNamespace(im=SimpleNamespace(export_chat_record=upstream)))

        class Handler:
            path = "/api/im/export"
            response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return {"peer": "9"}

            def sid(self):
                return "test-session"

            def user(self, _sid):
                return user

            def _send(self, status, body, content_type, **kwargs):
                self.response = (status, body, content_type, kwargs)

            def ok(self, payload, status=200, **kwargs):
                self.response = (status, payload, "application/json", kwargs)

        handler = Handler()
        with patch.object(bff_server, "STORE", SimpleNamespace()), patch.object(
            bff_server.Handler, "can_view_message_peer", return_value=allowed,
        ):
            bff_server.Handler.do_POST(handler)
        return handler.response, upstream

    def test_upstream_csv_formula_cells_are_neutralized(self):
        malicious = ["=1+1", "+1+1", "-1+1", "@SUM(1)", "  =1+1", "\t=1+1", "\r=1+1", "\n=1+1"]
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows([["nickname", "message"], *[[text, text] for text in malicious]])
        response, _upstream = self.export("\ufeff" + buffer.getvalue())
        self.assertEqual(response[0], 200)
        decoded = response[1].decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(decoded, newline="")))
        for original, row in zip(malicious, rows[1:]):
            self.assertEqual(row, ["'" + original, "'" + original])
        self.assertTrue(response[1].startswith(b"\xef\xbb\xbf"))
        self.assertEqual(response[2], "text/csv; charset=utf-8")

    def test_normal_quotes_commas_and_multiline_content_are_preserved(self):
        rows = [["nickname", "message"], ["normal", 'line 1, "quoted"\r\nline 2'], ["other", "'already text"]]
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows(rows)
        response, upstream = self.export(buffer.getvalue())
        actual = list(csv.reader(io.StringIO(response[1].decode("utf-8"), newline="")))
        self.assertEqual(actual, rows)
        upstream.assert_called_once_with(to_account="9", page="1", page_size="100", export="1")

    def test_malformed_upstream_csv_is_not_served_unsanitized(self):
        response, _upstream = self.export('nickname,message\r\nname,"=1+1')
        self.assertEqual(response[0], 502)
        self.assertFalse(response[1]["ok"])
        self.assertEqual(response[2], "application/json")

    def test_unauthorized_export_never_calls_provider(self):
        response, upstream = self.export('nickname,message\r\nname,=1+1', allowed=False)
        self.assertEqual(response[0], 403)
        upstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
