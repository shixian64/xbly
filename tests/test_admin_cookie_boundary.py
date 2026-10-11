from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bbw_web import admin_api


class AdminCookieBoundaryTests(TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(
            environment="production", admin_cookie_name="__Host-bbw_admin_sid",
            cookie_secure=True, trust_proxy_headers=False,
        )
        self.app.state.persistence = Mock()
        self.app.include_router(admin_api.router)

    @staticmethod
    def headers(cookie: bytes):
        return [(b"cookie", b"__Host-bbw_admin_sid=" + cookie),
                (b"origin", b"https://admin.test")]

    def assert_cookie_cleared(self, response):
        cookie = response.headers["set-cookie"]
        self.assertIn("__Host-bbw_admin_sid=", cookie)
        for attribute in ("Max-Age=0", "HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            self.assertIn(attribute, cookie)

    def test_logout_non_ascii_cookie_clears_cookie_without_database(self):
        for cookie in (b"caf\xe9", b'"caf\\351"', "fake-用户".encode("utf-8")):
            with self.subTest(cookie=cookie), patch.object(
                admin_api, "session_scope", side_effect=AssertionError("unexpected database access")
            ) as sessions, TestClient(
                self.app, base_url="https://admin.test", raise_server_exceptions=False
            ) as client:
                response = client.post("/api/admin/logout", headers=self.headers(cookie))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"ok": True})
                self.assert_cookie_cleared(response)
                sessions.assert_not_called()
                self.app.state.persistence.rate_limit.assert_not_called()

    def test_logout_valid_ascii_cookie_still_revokes_session(self):
        for sid in ("fake-admin-session", "x"):
            auth = Mock()
            with self.subTest(sid=sid), patch.object(
                admin_api, "session_scope", return_value=nullcontext(Mock())
            ) as sessions, patch.object(
                admin_api, "_admin_auth", return_value=auth
            ), patch.object(admin_api, "_audit_service", return_value=Mock()), TestClient(
                self.app, base_url="https://admin.test"
            ) as client:
                response = client.post("/api/admin/logout", headers=self.headers(sid.encode("ascii")))
                self.assertEqual(response.status_code, 200)
                auth.revoke_session.assert_called_once_with(sid, client_ip="unknown", reason="logout")
                sessions.assert_called_once()
                self.assert_cookie_cleared(response)

    def test_protected_route_non_ascii_cookie_is_unauthorized_without_database(self):
        with patch.object(
            admin_api, "session_scope", side_effect=AssertionError("unexpected database access")
        ) as sessions, TestClient(
            self.app, base_url="https://admin.test", raise_server_exceptions=False
        ) as client:
            response = client.get("/api/admin/me", headers=self.headers(b"caf\xe9"))
            self.assertEqual(response.status_code, 401)
            sessions.assert_not_called()
            self.app.state.persistence.rate_limit.assert_not_called()
