from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from bbw_prod.services import (
    AuthenticationFailed,
    PasswordPolicyError,
    UserCredentialService,
)


ROOT = Path(__file__).resolve().parents[1]


class _DB:
    def __init__(self) -> None:
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1


class _Users:
    def __init__(self, user: object | None) -> None:
        self.user = user
        self.for_update = False

    def get(self, _user_id: uuid.UUID, *, for_update: bool = False) -> object | None:
        self.for_update = for_update
        return self.user


class _Credentials:
    def __init__(self, credential: object | None) -> None:
        self.credential = credential
        self.for_update = False

    def get_for_user(
        self, _user_id: uuid.UUID, *, for_update: bool = False
    ) -> object | None:
        self.for_update = for_update
        return self.credential


class _Passwords:
    def __init__(self, expected: str = "current-password") -> None:
        self.expected = expected
        self.verify_calls: list[tuple[str | None, str]] = []
        self.hash_calls: list[str] = []

    def verify_or_dummy(self, encoded_hash: str | None, password: str) -> bool:
        self.verify_calls.append((encoded_hash, password))
        return encoded_hash == "old-hash" and password == self.expected

    def hash(self, password: str) -> str:
        self.hash_calls.append(password)
        return f"new-hash:{password}"


class LocalPasswordChangeServiceTests(unittest.TestCase):
    def make_service(self) -> tuple[UserCredentialService, object, _Passwords, _DB]:
        db = _DB()
        service = UserCredentialService(db)
        verified_at = datetime(2026, 7, 20, tzinfo=UTC)
        credential = SimpleNamespace(
            password_hash="old-hash",
            credential_version=3,
            enrollment_source=UserCredentialService.UPSTREAM_PASSWORD_SOURCE,
            verified_at=verified_at,
            password_changed_at=verified_at,
            last_authenticated_at=None,
            disabled_at=None,
        )
        service.users = _Users(SimpleNamespace(status="active"))
        service.credentials = _Credentials(credential)
        passwords = _Passwords()
        service.passwords = passwords
        return service, credential, passwords, db

    def test_change_verifies_current_and_preserves_upstream_verified_at(self) -> None:
        service, credential, passwords, db = self.make_service()
        verified_at = credential.verified_at

        changed = service.change_local_password(
            user_id=uuid.uuid4(),
            current_password="current-password",
            new_password="new-password-123",
        )

        self.assertIs(changed, credential)
        self.assertEqual(passwords.verify_calls, [("old-hash", "current-password")])
        self.assertEqual(passwords.hash_calls, ["new-password-123"])
        self.assertEqual(credential.password_hash, "new-hash:new-password-123")
        self.assertEqual(credential.credential_version, 4)
        self.assertEqual(
            credential.enrollment_source,
            UserCredentialService.LOCAL_PASSWORD_CHANGE_SOURCE,
        )
        self.assertEqual(credential.verified_at, verified_at)
        self.assertGreater(credential.password_changed_at, verified_at)
        self.assertEqual(credential.last_authenticated_at, credential.password_changed_at)
        self.assertTrue(service.users.for_update)
        self.assertTrue(service.credentials.for_update)
        self.assertEqual(db.flush_count, 1)

    def test_wrong_current_password_does_not_hash_new_password(self) -> None:
        service, _credential, passwords, db = self.make_service()

        with self.assertRaises(AuthenticationFailed):
            service.change_local_password(
                user_id=uuid.uuid4(),
                current_password="wrong-password",
                new_password="new-password-123",
            )

        self.assertEqual(passwords.hash_calls, [])
        self.assertEqual(db.flush_count, 0)

    def test_new_password_policy_is_bounded_and_separate_from_import_policy(self) -> None:
        for rejected in ("short", " " * 8, "x" * 129, "valid\x00bad"):
            with self.subTest(rejected=rejected[:16]):
                with self.assertRaises(PasswordPolicyError):
                    UserCredentialService.validate_new_local_password(rejected)

        UserCredentialService.validate_new_local_password("八个字符密码安全")

    def test_same_password_is_rejected_before_argon_hash(self) -> None:
        service, _credential, passwords, _db = self.make_service()

        with self.assertRaises(PasswordPolicyError):
            service.change_local_password(
                user_id=uuid.uuid4(),
                current_password="current-password",
                new_password="current-password",
            )

        self.assertEqual(passwords.verify_calls, [])
        self.assertEqual(passwords.hash_calls, [])


class LocalPasswordChangeApiContractTests(unittest.TestCase):
    def test_authenticated_endpoint_uses_argon_gate_and_revokes_sessions(self) -> None:
        api_source = (ROOT / "bbw_web" / "api.py").read_text(encoding="utf-8-sig")
        persistence_source = (ROOT / "bbw_web" / "persistence.py").read_text(
            encoding="utf-8-sig"
        )
        block = api_source.split('path == "/api/auth/password"', 1)[1].split(
            "message_policy_allowed_peers", 1
        )[0]

        self.assertIn("_auth_json_request_error(request)", block)
        self.assertIn("_local_password_auth_gate(request)", block)
        self.assertIn('upstream_auth_mode != "local-only"', block)
        self.assertIn('"LOCAL_PASSWORD_CHANGE_REQUIRES_LOCAL_ONLY"', block)
        self.assertIn("persistence.change_local_password(", block)
        self.assertIn('"CURRENT_PASSWORD_REJECTED"', block)
        self.assertIn('"PASSWORD_POLICY_REJECTED"', block)
        self.assertIn('"compatibility_sync": "not_available"', block)
        self.assertIn("legacy.STORE.drop(sid)", block)
        self.assertIn("revoke_all_for_user", persistence_source)
        self.assertNotIn("password_encrypted =", persistence_source.split(
            "def change_local_password", 1
        )[1].split("def require_identity", 1)[0])

    def test_profile_page_exposes_password_change_without_persisting_secrets(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(
            encoding="utf-8-sig"
        )
        form = source.split('data-form="local-password-change"', 1)[1].split(
            "</form>", 1
        )[0]
        handler = source.split('if (kind === "local-password-change")', 1)[1].split(
            'if (kind === "referral-set")', 1
        )[0]

        self.assertIn('autocomplete="current-password"', form)
        self.assertEqual(form.count('autocomplete="new-password"'), 2)
        self.assertIn('api("/api/auth/password"', handler)
        self.assertIn("S.localPasswordChangeEnabled", source)
        self.assertIn('"local_password_change"', source)
        self.assertIn("newPassword !== confirmPassword", handler)
        self.assertIn("logout({ notifyServer: false })", handler)
        self.assertNotIn("localStorage", handler)
        self.assertNotIn("sessionStorage", handler)


if __name__ == "__main__":
    unittest.main()
