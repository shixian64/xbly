from __future__ import annotations

import json
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response
from starlette.requests import Request

from bbw_prod.services import ConflictError
from bbw_web import admin_api
from bbw_web.providers import (
    ProviderAuthenticationRejected,
    ProviderUnavailable,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeAudit:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.records: list[dict[str, object]] = []

    def record(self, **values: object) -> None:
        if self.error is not None:
            raise self.error
        self.records.append(dict(values))


class FakePersistence:
    def __init__(self, login_context: object) -> None:
        self.phone_hmac_key = b"p" * 32
        self.login_context = login_context
        self.rate_limit_calls: list[dict[str, object]] = []
        self.precheck_calls: list[str] = []
        self.complete_calls: list[dict[str, object]] = []
        self.cancelled_pending: list[str] = []
        self.revoked_sessions: list[dict[str, str]] = []
        self.precheck_error: Exception | None = None
        self.complete_error: Exception | None = None

    def rate_limit(self, key: str, **values: object) -> bool:
        self.rate_limit_calls.append({"key": key, **values})
        return True

    def precheck_account(self, *, phone: str) -> object:
        self.precheck_calls.append(phone)
        if self.precheck_error is not None:
            raise self.precheck_error
        return self.login_context

    def complete_login(self, **values: object) -> object:
        self.complete_calls.append(dict(values))
        if self.complete_error is not None:
            raise self.complete_error
        return SimpleNamespace(
            user_id=self.login_context.existing_user_id,
            external_account_id=self.login_context.existing_external_account_id,
            upstream_uid=self.login_context.existing_upstream_uid,
        )

    def cancel_pending_login(self, sid: str) -> bool:
        self.cancelled_pending.append(sid)
        return True

    def revoke_session(self, sid: str, *, reason: str = "logout") -> bool:
        self.revoked_sessions.append({"sid": sid, "reason": reason})
        return True


class FakeStore:
    def __init__(self, web_user: object, error: Exception | None = None) -> None:
        self.web_user = web_user
        self.error = error
        self.login_calls: list[dict[str, object]] = []
        self.drop_calls: list[str] = []

    def login_onekey(self, *args: object, **kwargs: object) -> object:
        self.login_calls.append({"args": args, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.web_user

    def drop(self, sid: str) -> None:
        self.drop_calls.append(sid)


def login_context(*, upstream_uid: str | None = "42") -> object:
    return SimpleNamespace(
        normalized_phone="13800138000",
        existing_user_id=uuid.uuid4(),
        existing_external_account_id=uuid.uuid4(),
        existing_upstream_uid=upstream_uid,
        requires_invite=False,
        local_password_available=True,
    )


def web_user(*, upstream_uid: str = "42") -> object:
    return SimpleNamespace(
        web_sid="new-user-session-0123456789abcdef",
        app=SimpleNamespace(session=SimpleNamespace(uid=upstream_uid)),
    )


def admin_context() -> admin_api.AdminContext:
    return admin_api.AdminContext(
        sid="administrator-session-0123456789abcdef",
        session_id=uuid.uuid4(),
        admin_user_id=uuid.uuid4(),
        username="administrator",
        totp_enabled=True,
        client_ip="127.0.0.1",
        idle_expires_at=NOW + timedelta(minutes=10),
        absolute_expires_at=NOW + timedelta(hours=1),
    )


def request_stub(persistence: FakePersistence) -> Request:
    settings = SimpleNamespace(
        environment="test",
        user_cookie_name="bbw_sid",
        admin_cookie_name="bbw_admin_sid",
        cookie_secure=False,
    )
    application = SimpleNamespace(
        state=SimpleNamespace(settings=settings, persistence=persistence)
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/admin/user-login",
        "raw_path": b"/api/admin/user-login",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"user-agent", b"admin-user-login-test"),
            (
                b"cookie",
                b"bbw_admin_sid=administrator-session; "
                b"bbw_sid=old-user-session; "
                b"bbw_sid_pending_login=pending-user-session",
            ),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "app": application,
    }
    return Request(scope)


class AdminUserLoginTests(unittest.TestCase):
    def invoke(
        self,
        *,
        persistence: FakePersistence | None = None,
        store: FakeStore | None = None,
        audit: FakeAudit | None = None,
        session_error: Exception | None = None,
    ) -> tuple[dict[str, object], Response, FakePersistence, FakeStore, FakeAudit]:
        persistence = persistence or FakePersistence(login_context())
        store = store or FakeStore(web_user())
        audit = audit or FakeAudit()

        @contextmanager
        def fake_session_scope():
            yield object()
            if session_error is not None:
                raise session_error

        response = Response()
        with (
            patch.object(admin_api, "session_scope", fake_session_scope),
            patch.object(admin_api, "_audit_service", return_value=audit),
            patch.object(admin_api.legacy, "STORE", store),
        ):
            result = admin_api.admin_user_login(
                admin_api.AdminUserLoginBody(
                    phone=" 13800138000 ",
                    reason=" 管理员协助排障 ",
                ),
                request_stub(persistence),
                response,
                admin_context(),
            )
        return result, response, persistence, store, audit

    def test_success_issues_only_the_user_cookie_and_records_audit(self) -> None:
        result, response, persistence, store, audit = self.invoke()

        self.assertEqual(result, {"ok": True, "redirect": "/"})
        self.assertEqual(len(store.login_calls), 1)
        self.assertEqual(store.login_calls[0]["args"], (None, "13800138000"))
        self.assertIs(
            store.login_calls[0]["kwargs"]["request_authorized"],
            True,
        )
        self.assertEqual(
            store.login_calls[0]["kwargs"]["label"],
            "administrator-user-login",
        )
        self.assertEqual(len(persistence.complete_calls), 1)
        completion = persistence.complete_calls[0]
        self.assertIs(completion["require_existing_upstream_binding"], True)
        self.assertIs(completion["password_verified"], False)
        self.assertIs(completion["clear_login_failures"], False)
        self.assertIsNone(completion["old_sid"])
        self.assertEqual(
            persistence.revoked_sessions,
            [{"sid": "old-user-session", "reason": "rotated"}],
        )
        self.assertEqual(store.drop_calls, ["old-user-session", "pending-user-session"])
        self.assertEqual(persistence.cancelled_pending, ["pending-user-session"])

        cookie_headers = "\n".join(response.headers.getlist("set-cookie"))
        self.assertIn("bbw_sid=new-user-session-0123456789abcdef", cookie_headers)
        self.assertIn("bbw_sid_pending_login=", cookie_headers)
        self.assertIn("Max-Age=0", cookie_headers)
        self.assertIn("HttpOnly", cookie_headers)
        self.assertIn("SameSite=strict", cookie_headers)
        self.assertNotIn("bbw_admin_sid", cookie_headers)

        self.assertEqual(len(audit.records), 1)
        record = audit.records[0]
        self.assertEqual(record["action"], "user.admin_phone_login")
        self.assertEqual(record["reason"], "管理员协助排障")
        self.assertEqual(record["resource_type"], "user")
        self.assertEqual(record["resource_id"], str(record["target_user_id"]))
        serialized = json.dumps(record, ensure_ascii=False, default=str)
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("new-user-session", serialized)

    def test_missing_existing_uid_is_rejected_before_provider_authentication(self) -> None:
        persistence = FakePersistence(login_context(upstream_uid=None))
        store = FakeStore(web_user())

        with self.assertRaises(HTTPException) as raised:
            self.invoke(persistence=persistence, store=store)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(store.login_calls, [])
        self.assertEqual(persistence.complete_calls, [])

    def test_mismatched_provider_uid_is_rejected_and_runtime_is_discarded(self) -> None:
        persistence = FakePersistence(login_context(upstream_uid="42"))
        store = FakeStore(web_user(upstream_uid="different-user"))

        with self.assertRaises(HTTPException) as raised:
            self.invoke(persistence=persistence, store=store)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(persistence.complete_calls, [])
        self.assertEqual(store.drop_calls, ["new-user-session-0123456789abcdef"])
        self.assertEqual(
            persistence.revoked_sessions,
            [
                {
                    "sid": "new-user-session-0123456789abcdef",
                    "reason": "administrator_phone_login_failed",
                }
            ],
        )

    def test_provider_failures_keep_admin_auth_semantics(self) -> None:
        cases = (
            (ProviderUnavailable("network unavailable"), 503),
            (ProviderAuthenticationRejected("account rejected"), 409),
        )
        for error, expected_status in cases:
            with self.subTest(error=type(error).__name__):
                persistence = FakePersistence(login_context())
                store = FakeStore(web_user(), error=error)
                with self.assertRaises(HTTPException) as raised:
                    self.invoke(persistence=persistence, store=store)
                self.assertEqual(raised.exception.status_code, expected_status)
                self.assertEqual(persistence.complete_calls, [])

    def test_binding_race_revokes_the_new_session(self) -> None:
        persistence = FakePersistence(login_context())
        persistence.complete_error = ConflictError("binding changed")
        store = FakeStore(web_user())

        with self.assertRaises(HTTPException) as raised:
            self.invoke(persistence=persistence, store=store)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("new-user-session-0123456789abcdef", store.drop_calls)
        self.assertEqual(
            persistence.revoked_sessions,
            [
                {
                    "sid": "new-user-session-0123456789abcdef",
                    "reason": "administrator_phone_login_failed",
                }
            ],
        )

    def test_audit_failure_revokes_the_new_session(self) -> None:
        persistence = FakePersistence(login_context())
        store = FakeStore(web_user())

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.invoke(
                persistence=persistence,
                store=store,
                audit=FakeAudit(RuntimeError("audit unavailable")),
            )

        self.assertIn("new-user-session-0123456789abcdef", store.drop_calls)
        self.assertEqual(
            persistence.revoked_sessions,
            [
                {
                    "sid": "new-user-session-0123456789abcdef",
                    "reason": "administrator_phone_login_failed",
                }
            ],
        )

    def test_audit_commit_failure_preserves_the_old_session(self) -> None:
        persistence = FakePersistence(login_context())
        store = FakeStore(web_user())
        audit = FakeAudit()

        with self.assertRaisesRegex(RuntimeError, "audit commit unavailable"):
            self.invoke(
                persistence=persistence,
                store=store,
                audit=audit,
                session_error=RuntimeError("audit commit unavailable"),
            )

        self.assertEqual(len(audit.records), 1)
        self.assertIsNone(persistence.complete_calls[0]["old_sid"])
        self.assertEqual(
            persistence.revoked_sessions,
            [
                {
                    "sid": "new-user-session-0123456789abcdef",
                    "reason": "administrator_phone_login_failed",
                }
            ],
        )
        self.assertEqual(store.drop_calls, ["new-user-session-0123456789abcdef"])


if __name__ == "__main__":
    unittest.main()
