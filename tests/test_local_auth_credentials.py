from __future__ import annotations

import io
import json
import sys
import threading
import types
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # noqa: E402
    from bbw_prod import credential_backfill
    from bbw_prod import services as prod_services
    from bbw_prod.models import ExternalAccount, User, UserCredential
    from bbw_prod.security import UserPasswordHasher, is_safe_user_password_hash
    from bbw_prod.services import (
        AuthenticationFailed,
        CredentialBackfillReport,
        CredentialBackfillService,
        LocalAuthenticationUnavailable,
        LoginAccountService,
        PermissionDenied,
        UserCredentialService,
    )
except ImportError as exc:  # pragma: no cover - dependency-less contract runner
    DEPENDENCY_IMPORT_ERROR: ImportError | None = exc
else:
    DEPENDENCY_IMPORT_ERROR = None


class FakeDB:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = list(rows or [])
        self.added: list[object] = []
        self.flush_count = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        self.flush_count += 1

    def scalars(self, _statement: object) -> list[object]:
        return self.rows

    @contextmanager
    def begin_nested(self):
        yield


class FakePasswordHasher:
    def __init__(self) -> None:
        self.dummy_calls: list[str] = []
        self.hashed: list[str] = []
        self.force_rehash = False

    def hash(self, password: str) -> str:
        self.hashed.append(password)
        return f"local-hash:{password}"

    def verify(self, encoded_hash: str, password: str) -> bool:
        return encoded_hash == f"local-hash:{password}"

    def verify_or_dummy(self, encoded_hash: str | None, password: str) -> bool:
        if encoded_hash is None:
            self.dummy_calls.append(password)
            return False
        return self.verify(encoded_hash, password)

    def needs_rehash(self, _encoded_hash: str) -> bool:
        return self.force_rehash


class FakeUserRepository:
    def __init__(self, users: dict[uuid.UUID, User]) -> None:
        self.users = users

    def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
        del for_update
        return self.users.get(user_id)


class FakeCredentialRepository:
    def __init__(self, rows: dict[uuid.UUID, UserCredential] | None = None) -> None:
        self.rows = dict(rows or {})

    def get_for_user(
        self, user_id: uuid.UUID, *, for_update: bool = False
    ) -> UserCredential | None:
        del for_update
        return self.rows.get(user_id)

    def add(self, item: UserCredential, *, flush: bool = True) -> UserCredential:
        del flush
        self.rows[item.user_id] = item
        return item


class FakeAccountRepository:
    def __init__(self, account: ExternalAccount | None) -> None:
        self.account = account

    def get_by_phone_hmac(
        self,
        _phone_hmac: str,
        *,
        provider: str = "beibeiwu",
        for_update: bool = False,
    ) -> ExternalAccount | None:
        del provider, for_update
        return self.account


def make_user(*, status: str = "active") -> User:
    return User(id=uuid.uuid4(), status=status, profile={})


def make_account(user: User) -> ExternalAccount:
    return ExternalAccount(
        id=uuid.uuid4(),
        user_id=user.id,
        provider="beibeiwu",
        upstream_uid="42",
        phone_hmac="digest",
        login_account_encrypted={},
        password_encrypted={"ciphertext": "kept"},
        device_data={},
    )


def make_credential(
    user: User,
    *,
    password: str = "secret",
    disabled: bool = False,
) -> UserCredential:
    now = datetime.now(UTC) - timedelta(days=1)
    return UserCredential(
        id=uuid.uuid4(),
        user_id=user.id,
        password_hash=f"local-hash:{password}",
        credential_version=1,
        enrollment_source=UserCredentialService.UPSTREAM_PASSWORD_SOURCE,
        verified_at=now,
        password_changed_at=now,
        disabled_at=datetime.now(UTC) if disabled else None,
    )


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class UserPasswordHasherTests(unittest.TestCase):
    @staticmethod
    def replace_parameters(encoded_hash: str, **updates: int) -> str:
        parts = encoded_hash.split("$")
        parameters = dict(item.split("=", 1) for item in parts[3].split(","))
        parameters.update({name: str(value) for name, value in updates.items()})
        parts[3] = ",".join(
            f"{name}={parameters[name]}" for name in ("m", "t", "p")
        )
        return "$".join(parts)

    def test_user_password_policy_preserves_short_and_long_upstream_passwords(self) -> None:
        hasher = UserPasswordHasher()
        password = "短" + "x" * 2048

        encoded = hasher.hash(password)

        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(is_safe_user_password_hash(encoded))
        self.assertTrue(hasher.verify(encoded, password))
        self.assertFalse(hasher.verify(encoded, password + "x"))

    def test_hash_parameter_guard_rejects_resource_exhaustion_and_invalid_bounds(self) -> None:
        hasher = UserPasswordHasher()
        encoded = hasher.hash("upstream-password")
        parts = encoded.split("$")

        self.assertFalse(
            is_safe_user_password_hash(
                self.replace_parameters(encoded, m=1024 * 1024)
            )
        )
        self.assertFalse(
            is_safe_user_password_hash(self.replace_parameters(encoded, t=1000))
        )
        self.assertFalse(
            is_safe_user_password_hash(self.replace_parameters(encoded, p=64))
        )

        too_short_salt = parts.copy()
        too_short_salt[4] = "A" * 10
        self.assertFalse(is_safe_user_password_hash("$".join(too_short_salt)))
        too_long_salt = parts.copy()
        too_long_salt[4] = "A" * 87
        self.assertFalse(is_safe_user_password_hash("$".join(too_long_salt)))

        too_short_hash = parts.copy()
        too_short_hash[5] = "A" * 20
        self.assertFalse(is_safe_user_password_hash("$".join(too_short_hash)))
        too_long_hash = parts.copy()
        too_long_hash[5] = "A" * 87
        self.assertFalse(is_safe_user_password_hash("$".join(too_long_hash)))

    def test_unsafe_hash_never_reaches_expensive_verifier(self) -> None:
        hasher = UserPasswordHasher()
        encoded = hasher.hash("upstream-password")
        unsafe = self.replace_parameters(encoded, m=1024 * 1024)
        verifier = Mock()
        hasher._hasher = verifier

        self.assertFalse(hasher.verify(unsafe, "guess"))

        verifier.verify.assert_not_called()

    def test_unsafe_hash_uses_dummy_verify_and_always_needs_rehash(self) -> None:
        hasher = UserPasswordHasher()
        safe_dummy = hasher.hash("timing-only-dummy")
        unsafe = self.replace_parameters(safe_dummy, t=1000)
        verifier = Mock()
        verifier.verify.return_value = False
        verifier.check_needs_rehash.return_value = False
        hasher._hasher = verifier

        self.assertFalse(hasher.verify_or_dummy(unsafe, "guess"))
        self.assertTrue(hasher.needs_rehash(unsafe))

        verifier.verify.assert_called_once()
        dummy_hash, dummy_password = verifier.verify.call_args.args
        self.assertTrue(is_safe_user_password_hash(dummy_hash))
        self.assertNotEqual(dummy_hash, unsafe)
        self.assertEqual(dummy_password, "guess")
        verifier.hash.assert_not_called()
        verifier.check_needs_rehash.assert_not_called()

    def test_first_missing_hash_request_only_verifies_precomputed_dummy_once(self) -> None:
        hasher = UserPasswordHasher()
        verifier = Mock()
        verifier.verify.return_value = False
        hasher._hasher = verifier

        self.assertFalse(hasher.verify_or_dummy(None, "first-request-guess"))

        verifier.hash.assert_not_called()
        verifier.verify.assert_called_once()
        dummy_hash, dummy_password = verifier.verify.call_args.args
        self.assertTrue(is_safe_user_password_hash(dummy_hash))
        self.assertEqual(dummy_password, "first-request-guess")


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class UserCredentialServiceTests(unittest.TestCase):
    def make_service(
        self,
        user: User,
        *,
        account: ExternalAccount | None = None,
        credential: UserCredential | None = None,
    ) -> tuple[UserCredentialService, FakePasswordHasher, FakeCredentialRepository, FakeDB]:
        db = FakeDB()
        service = UserCredentialService.__new__(UserCredentialService)
        service.db = db
        service.phone_hmac_key = b"p" * 32
        service.users = FakeUserRepository({user.id: user})
        service.accounts = FakeAccountRepository(account)
        credentials = FakeCredentialRepository(
            {user.id: credential} if credential is not None else None
        )
        service.credentials = credentials
        passwords = FakePasswordHasher()
        service.passwords = passwords
        return service, passwords, credentials, db

    def test_verified_upstream_password_creates_independent_credential(self) -> None:
        user = make_user()
        service, passwords, credentials, _db = self.make_service(user)
        confirmed_at = datetime.now(UTC)

        result = service.enroll_verified_password(
            user_id=user.id,
            password="a",
            verified_at=confirmed_at,
        )

        self.assertTrue(result.created)
        self.assertTrue(result.password_changed)
        self.assertEqual(result.credential.user_id, user.id)
        self.assertEqual(result.credential.password_hash, "local-hash:a")
        self.assertEqual(result.credential.credential_version, 1)
        self.assertEqual(result.credential.verified_at, confirmed_at)
        self.assertIs(credentials.rows[user.id], result.credential)
        self.assertEqual(passwords.hashed, ["a"])

    def test_verified_password_change_increments_version_but_rehash_does_not(self) -> None:
        user = make_user()
        credential = make_credential(user, password="old")
        service, passwords, _credentials, _db = self.make_service(
            user, credential=credential
        )

        changed = service.enroll_verified_password(
            user_id=user.id,
            password="new",
            verified_at=datetime.now(UTC),
        )
        self.assertTrue(changed.password_changed)
        self.assertEqual(credential.credential_version, 2)
        self.assertEqual(credential.enrollment_source, "upstream_password_login")

        passwords.force_rehash = True
        rehashed = service.enroll_verified_password(
            user_id=user.id,
            password="new",
            verified_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        self.assertFalse(rehashed.password_changed)
        self.assertTrue(rehashed.password_rehashed)
        self.assertEqual(credential.credential_version, 2)

    def test_local_auth_unknown_or_unmigrated_account_uses_dummy_verify(self) -> None:
        user = make_user()
        service, passwords, _credentials, _db = self.make_service(user, account=None)
        with self.assertRaises(AuthenticationFailed):
            service.authenticate_password(phone="13800138000", password="guess")
        self.assertEqual(passwords.dummy_calls, ["guess"])

        account = make_account(user)
        service.accounts = FakeAccountRepository(account)
        with self.assertRaises(LocalAuthenticationUnavailable):
            service.authenticate_password(phone="13800138000", password="guess")
        self.assertEqual(passwords.dummy_calls, ["guess", "guess"])

    def test_local_auth_verifies_credential_and_rejects_disabled_or_wrong_password(self) -> None:
        user = make_user()
        account = make_account(user)
        credential = make_credential(user)
        service, passwords, _credentials, db = self.make_service(
            user,
            account=account,
            credential=credential,
        )

        authenticated = service.authenticate_password(
            phone="13800138000", password="secret"
        )
        self.assertIs(authenticated.user, user)
        self.assertIs(authenticated.external_account, account)
        self.assertIs(authenticated.credential, credential)
        self.assertIsNotNone(credential.last_authenticated_at)
        self.assertLess(credential.verified_at, credential.last_authenticated_at)
        self.assertGreaterEqual(db.flush_count, 1)

        with self.assertRaises(AuthenticationFailed):
            service.authenticate_password(phone="13800138000", password="wrong")

        credential.disabled_at = datetime.now(UTC)
        with self.assertRaises(PermissionDenied):
            service.authenticate_password(phone="13800138000", password="secret")
        self.assertIn("secret", passwords.dummy_calls)


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class OpportunityEnrollmentTests(unittest.TestCase):
    class Cipher:
        def encrypt_text(self, value: str, **_kwargs: object) -> dict[str, str]:
            return {"encrypted": value}

    class Accounts:
        def __init__(self, account: ExternalAccount) -> None:
            self.account = account

        def get_by_phone_hmac(self, *_args: object, **_kwargs: object) -> ExternalAccount:
            return self.account

        def get_by_upstream_uid(self, *_args: object, **_kwargs: object) -> ExternalAccount:
            return self.account

    class Invites:
        pass

    class EnrollmentRecorder:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def available_for_user(self, _user_id: uuid.UUID) -> bool:
            return False

        def enroll_verified_password(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))

    def make_service(
        self,
    ) -> tuple[LoginAccountService, ExternalAccount, EnrollmentRecorder]:
        user = make_user()
        account = make_account(user)
        service = LoginAccountService.__new__(LoginAccountService)
        service.db = FakeDB()
        service.settings = types.SimpleNamespace(invite_required=False)
        service.cipher = self.Cipher()
        service.phone_hmac_key = b"p" * 32
        service.accounts = self.Accounts(account)
        service.users = FakeUserRepository({user.id: user})
        service.invites = self.Invites()
        recorder = self.EnrollmentRecorder()
        service.user_credentials = recorder
        return service, account, recorder

    def test_password_login_enrolls_but_sms_login_does_not_overwrite(self) -> None:
        service, account, recorder = self.make_service()

        service.complete_login(
            phone="13800138000",
            invite_code=None,
            upstream_uid="42",
            login_account="13800138000",
            password="upstream-password",
            token="token",
            password_verified=True,
            require_invite=False,
        )
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["password"], "upstream-password")
        saved_ciphertext = account.password_encrypted

        service.complete_login(
            phone="13800138000",
            invite_code=None,
            upstream_uid="42",
            login_account="13800138000",
            password="attacker-supplied-but-not-upstream-verified",
            token="sms-token",
            password_verified=False,
            require_invite=False,
        )
        self.assertEqual(len(recorder.calls), 1)
        self.assertIs(account.password_encrypted, saved_ciphertext)

    def test_phone_only_login_rechecks_user_permission_during_completion(self) -> None:
        service, account, _recorder = self.make_service()
        user = service.users.get(account.user_id)
        assert user is not None
        user.phone_only_login_enabled = False

        with self.assertRaises(PermissionDenied):
            service.complete_login(
                phone="13800138000",
                invite_code=None,
                upstream_uid="42",
                login_account="13800138000",
                password="",
                token="onekey-token",
                password_verified=False,
                require_invite=False,
                phone_only_login=True,
            )

        user.phone_only_login_enabled = True
        completion = service.complete_login(
            phone="13800138000",
            invite_code=None,
            upstream_uid="42",
            login_account="13800138000",
            password="",
            token="onekey-token",
            password_verified=False,
            require_invite=False,
            phone_only_login=True,
        )
        self.assertIs(completion.user, user)

    def test_phone_only_login_requires_a_prebound_matching_upstream_uid(self) -> None:
        service, account, _recorder = self.make_service()
        user = service.users.get(account.user_id)
        assert user is not None
        user.phone_only_login_enabled = True

        account.upstream_uid = None
        context = service.precheck_credentials(phone="13800138000")
        self.assertFalse(context.phone_only_login_enabled)

        for saved_uid in (None, "different-upstream-user"):
            with self.subTest(saved_uid=saved_uid):
                account.upstream_uid = saved_uid
                with self.assertRaises(PermissionDenied):
                    service.complete_login(
                        phone="13800138000",
                        invite_code=None,
                        upstream_uid="42",
                        login_account="13800138000",
                        password="",
                        token="onekey-token",
                        password_verified=False,
                        require_invite=False,
                        phone_only_login=True,
                    )
                self.assertEqual(account.upstream_uid, saved_uid)

        account.upstream_uid = "42"
        context = service.precheck_credentials(phone="13800138000")
        self.assertTrue(context.phone_only_login_enabled)
        completion = service.complete_login(
            phone="13800138000",
            invite_code=None,
            upstream_uid="42",
            login_account="13800138000",
            password="",
            token="onekey-token",
            password_verified=False,
            require_invite=False,
            phone_only_login=True,
        )
        self.assertIs(completion.external_account, account)


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class OpportunisticEnrollmentGateTests(unittest.TestCase):
    """机会式登记闸门的容量、超时降级与首次登记阻塞语义。"""

    class GateEnrollmentRecorder:
        """带凭据行读通道的登记记录器，模拟 UserCredentialService。"""

        def __init__(
            self, credential_rows: dict[uuid.UUID, UserCredential] | None
        ) -> None:
            self.credentials = FakeCredentialRepository(credential_rows)
            self.calls: list[dict[str, object]] = []
            self.suspensions: list[dict[str, object]] = []

        def enroll_verified_password(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))

        def suspend_stale_password(self, **kwargs: object) -> bool | None:
            self.suspensions.append(dict(kwargs))
            credential = self.credentials.get_for_user(
                kwargs["user_id"], for_update=True
            )
            if credential is None:
                return None
            verified_at = kwargs["verified_at"]
            if credential.verified_at >= verified_at:
                return False
            credential.disabled_at = verified_at
            return True

    def setUp(self) -> None:
        prod_services._reset_enrollment_gate_for_tests()
        self.addCleanup(prod_services._reset_enrollment_gate_for_tests)

    def make_service(
        self, *, has_credential: bool
    ) -> tuple[LoginAccountService, User, GateEnrollmentRecorder]:
        user = make_user()
        service = LoginAccountService.__new__(LoginAccountService)
        service.settings = types.SimpleNamespace(local_password_auth_concurrency=2)
        recorder = self.GateEnrollmentRecorder(
            {user.id: make_credential(user)} if has_credential else None
        )
        service.user_credentials = recorder
        return service, user, recorder

    def test_gate_capacity_is_half_of_configured_bounded_concurrency(self) -> None:
        # max(1, min(8, capacity) // 2)：与 bbw_web 本地认证闸门叠加后的
        # 同进程 Argon2 峰值受控在约 1.5 倍配置值以内。
        for configured, expected in {0: 1, 1: 1, 2: 1, 5: 2, 8: 4, 20: 4}.items():
            with self.subTest(configured=configured):
                prod_services._reset_enrollment_gate_for_tests()
                gate = prod_services._opportunistic_enrollment_gate(configured)
                acquired = 0
                while gate.acquire(blocking=False):
                    acquired += 1
                self.assertEqual(acquired, expected)
                for _ in range(acquired):
                    gate.release()

    def test_reset_hook_replaces_process_level_singleton(self) -> None:
        first = prod_services._opportunistic_enrollment_gate(2)
        self.assertIs(prod_services._opportunistic_enrollment_gate(8), first)

        prod_services._reset_enrollment_gate_for_tests()

        self.assertIsNone(prod_services._ENROLLMENT_GATE)
        self.assertIsNot(prod_services._opportunistic_enrollment_gate(2), first)

    def test_saturated_gate_with_existing_credential_disables_stale_digest(self) -> None:
        service, user, recorder = self.make_service(has_credential=True)
        credential = recorder.credentials.get_for_user(user.id)
        self.assertIsNotNone(credential)
        gate = prod_services._opportunistic_enrollment_gate(2)  # 容量 1
        self.assertTrue(gate.acquire(blocking=False))
        verified_at = datetime.now(UTC)
        try:
            with self.assertLogs("bbw_prod.services", level="WARNING") as logs:
                enrolled = service._enroll_password_opportunistically(
                    user_id=user.id,
                    password="upstream-password",
                    verified_at=verified_at,
                )
        finally:
            gate.release()

        self.assertFalse(enrolled)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(recorder.suspensions, [{"user_id": user.id, "verified_at": verified_at}])
        self.assertEqual(credential.disabled_at, verified_at)
        self.assertIn(str(user.id), logs.output[0])

    def test_saturated_gate_without_credential_row_blocks_until_enrolled(self) -> None:
        service, user, recorder = self.make_service(has_credential=False)
        gate = prod_services._opportunistic_enrollment_gate(2)  # 容量 1
        self.assertTrue(gate.acquire(blocking=False))
        results: list[bool] = []
        verified_at = datetime.now(UTC)
        worker = threading.Thread(
            target=lambda: results.append(
                service._enroll_password_opportunistically(
                    user_id=user.id,
                    password="upstream-password",
                    verified_at=verified_at,
                )
            ),
            daemon=True,
        )
        worker.start()
        try:
            # 超过 1 秒短超时后仍未放弃：说明首次登记已升级为阻塞等待。
            worker.join(timeout=1.4)
            self.assertTrue(worker.is_alive())
            self.assertEqual(recorder.calls, [])
        finally:
            gate.release()
        worker.join(timeout=5.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["user_id"], user.id)
        self.assertEqual(recorder.calls[0]["password"], "upstream-password")
        self.assertEqual(recorder.calls[0]["verified_at"], verified_at)


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class CredentialBackfillTests(unittest.TestCase):
    class Cipher:
        def __init__(self, plaintext: str) -> None:
            self.plaintext = plaintext
            self.calls = 0

        def decrypt_text(self, *_args: object, **_kwargs: object) -> str:
            self.calls += 1
            return self.plaintext

    class Enrollment:
        def __init__(self) -> None:
            self.passwords = FakePasswordHasher()
            self.calls: list[dict[str, object]] = []

        def enroll_verified_password(self, **kwargs: object) -> object:
            self.calls.append(dict(kwargs))
            return types.SimpleNamespace(created=True)

    def make_service(
        self, *, existing: UserCredential | None = None
    ) -> tuple[CredentialBackfillService, ExternalAccount, Cipher, Enrollment]:
        user = make_user()
        account = make_account(user)
        now = datetime.now(UTC)
        account.created_at = now
        account.updated_at = now
        account.last_authenticated_at = now
        cipher = self.Cipher("historical-plaintext")
        service = CredentialBackfillService.__new__(CredentialBackfillService)
        service.db = FakeDB([account])
        service.cipher = cipher
        service.credentials = FakeCredentialRepository(
            {user.id: existing} if existing is not None else None
        )
        enrollment = self.Enrollment()
        service.user_credentials = enrollment
        return service, account, cipher, enrollment

    def test_default_dry_run_only_counts_without_decrypting_or_hashing(self) -> None:
        service, account, cipher, enrollment = self.make_service()
        original_ciphertext = account.password_encrypted
        service.cipher = None

        report = service.run()

        self.assertTrue(report.dry_run)
        self.assertEqual(report.eligible, 1)
        self.assertEqual(report.created, 0)
        self.assertEqual(cipher.calls, 0)
        self.assertEqual(enrollment.calls, [])
        self.assertEqual(enrollment.passwords.hashed, [])
        self.assertIs(account.password_encrypted, original_ciphertext)

    def test_apply_is_explicit_and_existing_credentials_are_never_overwritten(self) -> None:
        service, account, _cipher, enrollment = self.make_service()
        original_ciphertext = account.password_encrypted

        report = service.run(
            apply=True,
            limit=1,
            approved_account_ids={account.id},
        )

        self.assertFalse(report.dry_run)
        self.assertEqual(report.created, 1)
        self.assertEqual(
            enrollment.calls[0]["enrollment_source"],
            UserCredentialService.BACKFILL_SOURCE,
        )
        self.assertIs(account.password_encrypted, original_ciphertext)

        existing = make_credential(make_user())
        service, existing_account, cipher, enrollment = self.make_service(
            existing=existing
        )
        report = service.run(
            apply=True,
            limit=1,
            approved_account_ids={existing_account.id},
        )
        self.assertEqual(report.skipped_existing, 1)
        self.assertEqual(cipher.calls, 0)
        self.assertEqual(enrollment.calls, [])

    def test_apply_requires_a_reviewed_allowlist_and_bounded_batch(self) -> None:
        service, account, _cipher, _enrollment = self.make_service()

        with self.assertRaises(ValueError):
            service.run(apply=True, limit=1)
        with self.assertRaises(ValueError):
            service.run(apply=True, approved_account_ids={account.id})
        with self.assertRaises(ValueError):
            service.run(
                apply=True,
                limit=2,
                approved_account_ids={account.id},
            )
        with self.assertRaises(ValueError):
            service.run(
                apply=True,
                limit=CredentialBackfillService.MAX_APPLY_LIMIT + 1,
                approved_account_ids={account.id},
            )

    def test_cli_defaults_to_dry_run_and_never_prints_plaintext(self) -> None:
        calls: list[dict[str, object]] = []

        class Backfill:
            MAX_APPLY_LIMIT = 50

            def __init__(self, _db: object, _cipher: object) -> None:
                pass

            def run(self, **kwargs: object) -> CredentialBackfillReport:
                calls.append(dict(kwargs))
                applied = bool(kwargs["apply"])
                return CredentialBackfillReport(
                    not applied,
                    1,
                    1,
                    int(applied),
                    0,
                    0,
                )

        @contextmanager
        def fake_scope():
            yield object()

        output = io.StringIO()
        approved_id = uuid.uuid4()
        with (
            patch.object(credential_backfill, "get_settings", return_value=object()),
            patch.object(
                credential_backfill.CredentialCipher,
                "from_settings",
                return_value=object(),
            ) as cipher_factory,
            patch.object(credential_backfill, "session_scope", fake_scope),
            patch.object(credential_backfill, "CredentialBackfillService", Backfill),
        ):
            exit_code = credential_backfill.main([], stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(calls[0]["apply"])
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["upstream_password_ciphertext_retained"])
        self.assertNotIn("historical-plaintext", output.getvalue())
        cipher_factory.assert_not_called()

        output = io.StringIO()
        with (
            patch.object(credential_backfill, "get_settings", return_value=object()),
            patch.object(
                credential_backfill.CredentialCipher,
                "from_settings",
                return_value=object(),
            ) as cipher_factory,
            patch.object(credential_backfill, "session_scope", fake_scope),
            patch.object(credential_backfill, "CredentialBackfillService", Backfill),
        ):
            credential_backfill.main(
                [
                    "--apply",
                    "--limit",
                    "1",
                    "--approved-account-ids-file",
                    "-",
                ],
                stdout=output,
                stdin=io.StringIO(f"{approved_id}\n"),
            )
        self.assertTrue(calls[1]["apply"])
        self.assertEqual(calls[1]["approved_account_ids"], {approved_id})
        self.assertFalse(json.loads(output.getvalue())["dry_run"])
        self.assertNotIn(str(approved_id), output.getvalue())
        cipher_factory.assert_called_once()

    def test_cli_refuses_unbounded_or_unapproved_apply(self) -> None:
        with self.assertRaises(SystemExit):
            credential_backfill.main(["--apply"])
        with self.assertRaises(SystemExit):
            credential_backfill.main(["--apply", "--limit", "1"])
        with self.assertRaises(SystemExit):
            credential_backfill.main(
                [
                    "--apply",
                    "--limit",
                    str(CredentialBackfillService.MAX_APPLY_LIMIT + 1),
                    "--approved-account-ids-file",
                    "-",
                ],
                stdin=io.StringIO(f"{uuid.uuid4()}\n"),
            )


class CredentialSchemaContractTests(unittest.TestCase):
    def test_0009_creates_separate_table_without_altering_external_passwords(self) -> None:
        migration = (
            ROOT
            / "migrations/versions/20260725_0009_local_password_credentials.py"
        ).read_text(encoding="utf-8")

        self.assertIn('op.create_table(\n        "user_credentials"', migration)
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "20260724_0008"', migration)
        self.assertNotIn('op.add_column(\n        "external_accounts"', migration)
        self.assertNotIn("password_encrypted =", migration)

    def test_user_credential_serialization_hides_password_hash(self) -> None:
        if DEPENDENCY_IMPORT_ERROR is not None:
            self.skipTest(
                f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}"
            )
        user = make_user()
        credential = make_credential(user)

        public = credential.to_dict()

        self.assertNotIn("password_hash", public)
        self.assertEqual(public["credential_version"], 1)

    def test_0011_persists_local_authentication_mode_across_process_restart(self) -> None:
        migration = (
            ROOT
            / "migrations/versions/20260725_0011_web_session_auth_source.py"
        ).read_text(encoding="utf-8")
        services = (ROOT / "bbw_prod/services.py").read_text(encoding="utf-8")
        persistence = (ROOT / "bbw_web/persistence.py").read_text(encoding="utf-8")

        self.assertIn('revision: str = "20260725_0011"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0010"',
            migration,
        )
        self.assertIn('"auth_source"', migration)
        self.assertIn("web_session_auth_source_valid", migration)
        self.assertIn('auth_source="web-local"', persistence)
        self.assertIn('auth_source: str = "provider"', services)
        self.assertIn('"auth_source": state.auth_source', services)


if __name__ == "__main__":
    unittest.main()
