from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from bbw_web import admin_api


MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class FakeDb:
    def __init__(self, scalar_values):
        self.scalar_values = list(scalar_values)
        self.flushes = 0

    def scalar(self, _statement):
        return self.scalar_values.pop(0)

    def flush(self):
        self.flushes += 1


class FakeAudit:
    def __init__(self):
        self.records = []

    def record(self, **values):
        self.records.append(values)


def user_row(*, used_bytes: int = 90 * MEBIBYTE, quota_bytes: int = 100 * MEBIBYTE):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="active",
        display_name="测试用户",
        media_used_bytes=used_bytes,
        media_quota_bytes=quota_bytes,
        chat_retention_days=180,
        match_pool_online_list_enabled=False,
        nearby_custom_city_enabled=False,
        byok_model_runner_enabled=False,
        byok_account_actions_enabled=False,
        byok_autonomous_agent_enabled=False,
        last_login_at=NOW,
        disabled_at=None,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW,
    )


def request_stub():
    settings = SimpleNamespace(global_media_quota_bytes=8 * GIBIBYTE)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings))
    )


def admin_context():
    return admin_api.AdminContext(
        sid="session",
        session_id=uuid.uuid4(),
        admin_user_id=uuid.uuid4(),
        username="administrator",
        totp_enabled=True,
        client_ip="127.0.0.1",
        idle_expires_at=NOW + timedelta(minutes=10),
        absolute_expires_at=NOW + timedelta(hours=1),
    )


class AdminMediaQuotaContractTests(unittest.TestCase):
    def invoke(self, db, user, *, quota_bytes: int):
        audit = FakeAudit()

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch.object(admin_api, "session_scope", fake_session_scope),
            patch.object(admin_api, "_audit_service", return_value=audit),
            patch.object(admin_api, "utcnow", return_value=NOW),
        ):
            result = admin_api.set_user_media_quota(
                user.id,
                admin_api.UserMediaQuotaBody(
                    quota_bytes=quota_bytes,
                    reason="  管理员批准扩容  ",
                ),
                request_stub(),
                admin_context(),
            )
        return result, audit

    def test_quota_change_uses_database_value_and_records_audit(self) -> None:
        user = user_row()
        system = SimpleNamespace(quota_bytes=8 * GIBIBYTE)
        db = FakeDb([system, user, 5 * MEBIBYTE, None])

        result, audit = self.invoke(
            db,
            user,
            quota_bytes=200 * MEBIBYTE,
        )

        self.assertTrue(result["changed"])
        self.assertEqual(user.media_quota_bytes, 200 * MEBIBYTE)
        self.assertEqual(result["user"]["media_pending_bytes"], 5 * MEBIBYTE)
        self.assertEqual(result["user"]["media_quota_limit_bytes"], 8 * GIBIBYTE)
        self.assertEqual(db.flushes, 1)
        self.assertEqual(len(audit.records), 1)
        self.assertEqual(audit.records[0]["action"], "user.media_quota_changed")
        self.assertEqual(audit.records[0]["reason"], "管理员批准扩容")

    def test_quota_cannot_drop_below_used_and_pending_reservations(self) -> None:
        user = user_row()
        system = SimpleNamespace(quota_bytes=8 * GIBIBYTE)
        db = FakeDb([system, user, 5 * MEBIBYTE])

        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                db,
                user,
                quota_bytes=94 * MEBIBYTE,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(user.media_quota_bytes, 100 * MEBIBYTE)
        self.assertEqual(db.flushes, 0)

    def test_quota_cannot_exceed_effective_system_limit(self) -> None:
        user = user_row()
        system = SimpleNamespace(quota_bytes=16 * GIBIBYTE)
        db = FakeDb([system, user, 0])

        with self.assertRaises(HTTPException) as raised:
            self.invoke(
                db,
                user,
                quota_bytes=9 * GIBIBYTE,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(user.media_quota_bytes, 100 * MEBIBYTE)
        self.assertEqual(db.flushes, 0)


if __name__ == "__main__":
    unittest.main()
