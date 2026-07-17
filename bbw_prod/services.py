"""供 FastAPI、RQ worker 与管理端调用的同步业务服务。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Protocol

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .crypto import CredentialCipher, normalize_phone, phone_lookup_hmac, redact_raw_payload
from .models import (
    AdminSession,
    AdminUser,
    AuditLog,
    ExternalAccount,
    InviteCode,
    MediaObject,
    Message,
    RawUpstreamResponse,
    User,
    WebSession,
    utcnow,
)
from .repositories import (
    AdminSessionRepository,
    AdminUserRepository,
    AuditLogRepository,
    ExternalAccountRepository,
    InviteCodeRepository,
    MediaObjectRepository,
    RawUpstreamResponseRepository,
    UserRepository,
    WebSessionRepository,
)
from .security import (
    InviteCodeManager,
    PasswordHasher,
    SessionTokenManager,
    TOTPManager,
    keyed_identifier_hash,
    normalize_username,
)


class RedisClient(Protocol):
    def get(self, name: str) -> bytes | str | None: ...

    def set(self, name: str, value: str, *args: Any, **kwargs: Any) -> Any: ...

    def delete(self, *names: str) -> Any: ...


class ServiceError(RuntimeError):
    code = "service_error"


class NotFoundError(ServiceError):
    code = "not_found"


class ConflictError(ServiceError):
    code = "conflict"


class AuthenticationFailed(ServiceError):
    code = "authentication_failed"


class PermissionDenied(ServiceError):
    code = "permission_denied"


class InviteInvalid(ServiceError):
    code = "invite_invalid"


class QuotaExceeded(ServiceError):
    code = "quota_exceeded"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _json_loads(value: bytes | str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _redis_set_json(redis: RedisClient, key: str, value: Mapping[str, Any], ttl: int) -> None:
    redis.set(key, json.dumps(value, separators=(",", ":"), sort_keys=True), ex=max(1, ttl))


@dataclass(frozen=True, slots=True)
class IssuedSession:
    sid: str
    session_id: uuid.UUID
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class UserSessionState:
    session_id: uuid.UUID
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


class UserSessionService:
    """Redis 热状态 + PostgreSQL 可恢复状态。

    Redis key 只包含 SHA-256(SID)，value 不保存原始 SID。数据库和 Redis 中
    都不会出现 Cookie 明文。
    """

    def __init__(self, db: Session, redis: RedisClient, settings: Settings, identifier_key: bytes):
        self.db = db
        self.redis = redis
        self.settings = settings
        self.identifier_key = identifier_key
        self.repo = WebSessionRepository(db)

    def _key(self, raw_sid: str) -> str:
        return SessionTokenManager.redis_key(self.settings.redis_prefix, "web", raw_sid)

    def _key_from_hash(self, sid_hash: str) -> str:
        return SessionTokenManager.redis_key_from_hash(
            self.settings.redis_prefix, "web", sid_hash
        )

    @staticmethod
    def _state_from_row(row: WebSession) -> UserSessionState:
        return UserSessionState(
            session_id=row.id,
            user_id=row.user_id,
            external_account_id=row.external_account_id,
            created_at=_aware(row.created_at),
            last_seen_at=_aware(row.last_seen_at),
            idle_expires_at=_aware(row.idle_expires_at),
            absolute_expires_at=_aware(row.absolute_expires_at),
        )

    @staticmethod
    def _state_payload(state: UserSessionState) -> dict[str, str]:
        return {
            "session_id": str(state.session_id),
            "user_id": str(state.user_id),
            "external_account_id": str(state.external_account_id),
            "created_at": state.created_at.isoformat(),
            "last_seen_at": state.last_seen_at.isoformat(),
            "idle_expires_at": state.idle_expires_at.isoformat(),
            "absolute_expires_at": state.absolute_expires_at.isoformat(),
        }

    @staticmethod
    def _state_from_payload(payload: Mapping[str, Any]) -> UserSessionState | None:
        try:
            return UserSessionState(
                session_id=uuid.UUID(str(payload["session_id"])),
                user_id=uuid.UUID(str(payload["user_id"])),
                external_account_id=uuid.UUID(str(payload["external_account_id"])),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                last_seen_at=datetime.fromisoformat(str(payload["last_seen_at"])),
                idle_expires_at=datetime.fromisoformat(str(payload["idle_expires_at"])),
                absolute_expires_at=datetime.fromisoformat(str(payload["absolute_expires_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _cache(self, raw_sid: str, state: UserSessionState, *, at: datetime | None = None) -> None:
        now = at or utcnow()
        ttl = int(min(state.idle_expires_at, state.absolute_expires_at).timestamp() - now.timestamp())
        if ttl > 0:
            _redis_set_json(self.redis, self._key(raw_sid), self._state_payload(state), ttl)

    def issue(
        self,
        *,
        user_id: uuid.UUID,
        external_account_id: uuid.UUID,
        client_ip: str | None,
        user_agent: str | None,
        raw_sid: str | None = None,
        now: datetime | None = None,
    ) -> IssuedSession:
        current = now or utcnow()
        absolute = current + timedelta(seconds=self.settings.user_absolute_ttl_seconds)
        idle = min(current + timedelta(seconds=self.settings.user_idle_ttl_seconds), absolute)
        raw_sid = raw_sid or SessionTokenManager.generate()
        row = WebSession(
            user_id=user_id,
            external_account_id=external_account_id,
            sid_hash=SessionTokenManager.hash_sid(raw_sid),
            ip_hash=(
                keyed_identifier_hash(client_ip, self.identifier_key, purpose="ip")
                if client_ip
                else None
            ),
            user_agent=(user_agent or "")[:512] or None,
            created_at=current,
            last_seen_at=current,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
        )
        self.repo.add(row)
        return IssuedSession(raw_sid, row.id, idle, absolute)

    def issue_with_sid(
        self,
        *,
        raw_sid: str,
        user_id: uuid.UUID,
        external_account_id: uuid.UUID,
        client_ip: str | None,
        user_agent: str | None,
        now: datetime | None = None,
    ) -> IssuedSession:
        """持久化 legacy/BFF 已经写入 Cookie 的 SID，库中仍只保存摘要。"""

        return self.issue(
            raw_sid=raw_sid,
            user_id=user_id,
            external_account_id=external_account_id,
            client_ip=client_ip,
            user_agent=user_agent,
            now=now,
        )

    def recover(self, raw_sid: str, *, now: datetime | None = None) -> UserSessionState | None:
        current = now or utcnow()
        key = self._key(raw_sid)
        cached = self._state_from_payload(_json_loads(self.redis.get(key)) or {})
        if cached is not None:
            if current < cached.idle_expires_at and current < cached.absolute_expires_at:
                return cached
            self.redis.delete(key)

        row = self.repo.get_by_hash(SessionTokenManager.hash_sid(raw_sid))
        if (
            row is None
            or row.revoked_at is not None
            or current >= _aware(row.idle_expires_at)
            or current >= _aware(row.absolute_expires_at)
        ):
            return None
        state = self._state_from_row(row)
        self._cache(raw_sid, state, at=current)
        return state

    restore = recover

    def touch(self, raw_sid: str, *, now: datetime | None = None) -> UserSessionState | None:
        current = now or utcnow()
        cached = self._state_from_payload(_json_loads(self.redis.get(self._key(raw_sid))) or {})
        if (
            cached is not None
            and current < cached.idle_expires_at
            and current < cached.absolute_expires_at
            and (current - cached.last_seen_at).total_seconds() < 300
        ):
            return cached
        row = self.repo.get_by_hash(SessionTokenManager.hash_sid(raw_sid), for_update=True)
        if (
            row is None
            or row.revoked_at is not None
            or current >= _aware(row.idle_expires_at)
            or current >= _aware(row.absolute_expires_at)
        ):
            self.redis.delete(self._key(raw_sid))
            return None
        row.last_seen_at = current
        row.idle_expires_at = min(
            current + timedelta(seconds=self.settings.user_idle_ttl_seconds),
            _aware(row.absolute_expires_at),
        )
        self.db.flush()
        state = self._state_from_row(row)
        self._cache(raw_sid, state, at=current)
        return state

    def revoke(self, raw_sid: str, *, reason: str = "logout", now: datetime | None = None) -> bool:
        row = self.repo.get_by_hash(SessionTokenManager.hash_sid(raw_sid), for_update=True)
        self.redis.delete(self._key(raw_sid))
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = now or utcnow()
        row.revoke_reason = reason[:160]
        self.db.flush()
        return True

    def revoke_all_for_user(self, user_id: uuid.UUID, *, reason: str) -> int:
        rows = self.repo.active_for_user(user_id)
        now = utcnow()
        for row in rows:
            row.revoked_at = now
            row.revoke_reason = reason[:160]
            self.redis.delete(self._key_from_hash(row.sid_hash))
        self.db.flush()
        return len(rows)


class AuditService:
    def __init__(self, db: Session, settings: Settings, identifier_key: bytes):
        self.db = db
        self.settings = settings
        self.identifier_key = identifier_key
        self.repo = AuditLogRepository(db)

    def record(
        self,
        *,
        actor_type: str,
        action: str,
        admin_user_id: uuid.UUID | None = None,
        target_user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        reason: str | None = None,
        client_ip: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> AuditLog:
        now = utcnow()
        sanitized_details = redact_raw_payload(dict(details or {}))
        sanitized_reason = redact_raw_payload(str(reason or ""))
        row = AuditLog(
            actor_type=actor_type[:24],
            action=action[:96],
            admin_user_id=admin_user_id,
            target_user_id=target_user_id,
            resource_type=(resource_type or "")[:80] or None,
            resource_id=(resource_id or "")[:128] or None,
            reason=str(sanitized_reason or "")[:500] or None,
            ip_hash=(
                keyed_identifier_hash(client_ip, self.identifier_key, purpose="ip")
                if client_ip
                else None
            ),
            details=sanitized_details if isinstance(sanitized_details, dict) else {},
            created_at=now,
            expires_at=now + timedelta(days=self.settings.audit_retention_days),
        )
        return self.repo.add(row)


class InviteService:
    def __init__(self, db: Session, settings: Settings, audit: AuditService | None = None):
        self.db = db
        self.settings = settings
        self.audit = audit
        self.repo = InviteCodeRepository(db)

    @staticmethod
    def _usable(row: InviteCode, now: datetime) -> bool:
        return (
            row.disabled_at is None
            and (row.expires_at is None or now < _aware(row.expires_at))
            and row.use_count < row.max_uses
        )

    def create(
        self,
        *,
        admin_user_id: uuid.UUID,
        label: str | None = None,
        max_uses: int = 1,
        expires_at: datetime | None = None,
        client_ip: str | None = None,
    ) -> tuple[InviteCode, str]:
        if not 1 <= max_uses <= 10000:
            raise ValueError("max_uses must be between 1 and 10000")
        if expires_at is not None and _aware(expires_at) <= utcnow():
            raise ValueError("invite expiry must be in the future")
        raw_code = InviteCodeManager.generate()
        row = InviteCode(
            code_hash=InviteCodeManager.hash(raw_code),
            label=(label or "")[:120] or None,
            max_uses=max_uses,
            expires_at=expires_at,
            created_by_admin_id=admin_user_id,
        )
        self.repo.add(row)
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="invite.create",
                admin_user_id=admin_user_id,
                resource_type="invite_code",
                resource_id=str(row.id),
                client_ip=client_ip,
                details={"max_uses": max_uses, "expires_at": expires_at.isoformat() if expires_at else None},
            )
        return row, raw_code

    def validate(self, raw_code: str, *, for_update: bool = False) -> InviteCode:
        try:
            digest = InviteCodeManager.hash(raw_code)
        except ValueError as exc:
            raise InviteInvalid("invitation code is invalid") from exc
        row = self.repo.get_by_hash(digest, for_update=for_update)
        if row is None or not self._usable(row, utcnow()):
            raise InviteInvalid("invitation code is unavailable")
        return row

    def consume_locked(self, row: InviteCode) -> None:
        now = utcnow()
        if not self._usable(row, now):
            raise InviteInvalid("invitation code is unavailable")
        row.use_count += 1
        row.last_used_at = now
        self.db.flush()

    def disable(
        self, invite_id: uuid.UUID, *, admin_user_id: uuid.UUID, client_ip: str | None = None
    ) -> InviteCode:
        row = self.db.scalar(select(InviteCode).where(InviteCode.id == invite_id).with_for_update())
        if row is None:
            raise NotFoundError("invitation code was not found")
        row.disabled_at = utcnow()
        self.db.flush()
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="invite.disable",
                admin_user_id=admin_user_id,
                resource_type="invite_code",
                resource_id=str(row.id),
                client_ip=client_ip,
            )
        return row


@dataclass(frozen=True, slots=True)
class LoginPrecheck:
    phone_hmac: str
    normalized_phone: str
    existing_user_id: uuid.UUID | None
    existing_external_account_id: uuid.UUID | None
    invite_code_id: uuid.UUID | None
    requires_invite: bool


@dataclass(frozen=True, slots=True)
class LoginCompletion:
    user: User
    external_account: ExternalAccount
    created: bool


class LoginAccountService:
    """邀请码登录预检与上游认证成功后的账号落库。"""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        cipher: CredentialCipher,
        phone_hmac_key: bytes,
    ):
        self.db = db
        self.settings = settings
        self.cipher = cipher
        self.phone_hmac_key = phone_hmac_key
        self.accounts = ExternalAccountRepository(db)
        self.users = UserRepository(db)
        self.invites = InviteService(db, settings)

    def precheck_credentials(
        self, *, phone: str, provider: str = "beibeiwu"
    ) -> LoginPrecheck:
        """检查账号是否允许登录，但不在上游认证前要求邀请码。"""
        normalized = normalize_phone(phone)
        digest = phone_lookup_hmac(normalized, self.phone_hmac_key)
        existing = self.accounts.get_by_phone_hmac(digest, provider=provider)
        if existing is not None:
            user = self.users.get(existing.user_id)
            if user is None or user.status != "active":
                raise PermissionDenied("user account is not active")
            return LoginPrecheck(
                digest,
                normalized,
                user.id,
                existing.id,
                None,
                bool(self.settings.invite_required),
            )
        return LoginPrecheck(
            digest,
            normalized,
            None,
            None,
            None,
            bool(self.settings.invite_required),
        )

    def precheck(
        self, *, phone: str, invite_code: str | None, provider: str = "beibeiwu"
    ) -> LoginPrecheck:
        context = self.precheck_credentials(phone=phone, provider=provider)
        if not context.requires_invite:
            return context
        if not invite_code:
            raise InviteInvalid("an invitation code is required")
        invite = self.invites.validate(invite_code)
        return LoginPrecheck(
            context.phone_hmac,
            context.normalized_phone,
            context.existing_user_id,
            context.existing_external_account_id,
            invite.id,
            True,
        )

    @staticmethod
    def _credential_context(account_id: uuid.UUID, field: str) -> str:
        return f"external-account:{account_id}:{field}"

    def _set_credentials(
        self,
        account: ExternalAccount,
        *,
        normalized_phone: str,
        login_account: str,
        password: str,
        token: str | None,
        token_expires_at: datetime | None,
    ) -> None:
        account.phone_encrypted = self.cipher.encrypt_text(
            normalized_phone,
            purpose="external-account.phone",
            context=self._credential_context(account.id, "phone"),
        )
        account.login_account_encrypted = self.cipher.encrypt_text(
            login_account,
            purpose="external-account.login",
            context=self._credential_context(account.id, "login"),
        )
        # SMS login does not supply the upstream password.  Do not erase a
        # previously captured password merely because this login used SMS.
        if password:
            account.password_encrypted = self.cipher.encrypt_text(
                password,
                purpose="external-account.password",
                context=self._credential_context(account.id, "password"),
            )
        account.token_encrypted = (
            self.cipher.encrypt_text(
                token,
                purpose="external-account.token",
                context=self._credential_context(account.id, "token"),
            )
            if token
            else None
        )
        account.token_expires_at = token_expires_at
        account.last_authenticated_at = utcnow()

    def complete_login(
        self,
        *,
        phone: str,
        invite_code: str | None,
        upstream_uid: str,
        login_account: str,
        password: str,
        token: str | None,
        token_expires_at: datetime | None = None,
        provider: str = "beibeiwu",
        display_name: str | None = None,
        profile: Mapping[str, Any] | None = None,
        device_data: Mapping[str, Any] | None = None,
        require_invite: bool = True,
    ) -> LoginCompletion:
        normalized = normalize_phone(phone)
        digest = phone_lookup_hmac(normalized, self.phone_hmac_key)
        by_phone = self.accounts.get_by_phone_hmac(digest, provider=provider, for_update=True)
        by_uid = self.accounts.get_by_upstream_uid(upstream_uid, provider=provider, for_update=True)
        if by_phone is not None:
            if by_uid is not None and by_uid.id != by_phone.id:
                raise ConflictError("upstream account is already bound to another user")
            if by_phone.upstream_uid not in (None, upstream_uid):
                raise ConflictError("phone is already bound to a different upstream account")
            user = self.users.get(by_phone.user_id, for_update=True)
            if user is None or user.status != "active":
                raise PermissionDenied("user account is not active")
            by_phone.upstream_uid = upstream_uid
            by_phone.phone_hmac = digest
            by_phone.device_data = dict(device_data or by_phone.device_data or {})
            self._set_credentials(
                by_phone,
                normalized_phone=normalized,
                login_account=login_account,
                password=password,
                token=token,
                token_expires_at=token_expires_at,
            )
            user.last_login_at = utcnow()
            if display_name:
                user.display_name = display_name[:160]
            if profile is not None:
                user.profile = dict(profile)
            invite = None
            if require_invite and self.settings.invite_required:
                if not invite_code:
                    raise InviteInvalid("an invitation code is required")
                invite = self.invites.validate(invite_code, for_update=True)
            elif invite_code:
                invite = self.invites.validate(invite_code, for_update=True)
            if invite is not None:
                self.invites.consume_locked(invite)
            self.db.flush()
            return LoginCompletion(user, by_phone, False)

        if by_uid is not None:
            raise ConflictError("upstream account is already bound to another phone")
        if require_invite and self.settings.invite_required and not invite_code:
            raise InviteInvalid("an invitation code is required")
        invite = (
            self.invites.validate(invite_code, for_update=True)
            if invite_code
            else None
        )
        user = User(
            id=uuid.uuid4(),
            status="active",
            display_name=(display_name or "")[:160] or None,
            profile=dict(profile or {}),
            invite_code_id=invite.id if invite is not None else None,
            media_quota_bytes=self.settings.per_user_media_quota_bytes,
            chat_retention_days=self.settings.message_retention_days,
            last_login_at=utcnow(),
        )
        account = ExternalAccount(
            id=uuid.uuid4(),
            user_id=user.id,
            provider=provider,
            upstream_uid=upstream_uid,
            phone_hmac=digest,
            # The non-null login account field is replaced with an encrypted
            # envelope in this same transaction before the first flush.
            login_account_encrypted={},
            password_encrypted=None,
            device_data=dict(device_data or {}),
        )
        self._set_credentials(
            account,
            normalized_phone=normalized,
            login_account=login_account,
            password=password,
            token=token,
            token_expires_at=token_expires_at,
        )
        # There is intentionally no ORM relationship between these security
        # boundary models.  Flush the parent explicitly so PostgreSQL never
        # sees the external account before its referenced user row.
        self.db.add(user)
        self.db.flush()
        self.db.add(account)
        if invite is not None:
            self.invites.consume_locked(invite)
        self.db.flush()
        return LoginCompletion(user, account, True)


@dataclass(frozen=True, slots=True)
class AdminSessionState:
    session_id: uuid.UUID
    admin_user_id: uuid.UUID
    ip_hash: str
    created_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class TOTPEnrollment:
    secret: str
    provisioning_uri: str


class AdminAuthService:
    MAX_ACTIVE_SESSIONS = 3
    TOTP_PENDING_SECONDS = 10 * 60

    def __init__(
        self,
        db: Session,
        redis: RedisClient,
        settings: Settings,
        cipher: CredentialCipher,
        identifier_key: bytes,
        audit: AuditService | None = None,
    ):
        self.db = db
        self.redis = redis
        self.settings = settings
        self.cipher = cipher
        self.identifier_key = identifier_key
        self.audit = audit
        self.passwords = PasswordHasher()
        self.totp = TOTPManager()
        self.admins = AdminUserRepository(db)
        self.sessions = AdminSessionRepository(db)

    def _session_key(self, raw_sid: str) -> str:
        return SessionTokenManager.redis_key(self.settings.redis_prefix, "admin", raw_sid)

    def _unlock_key(self, raw_sid: str) -> str:
        return (
            f"{self.settings.redis_prefix}:unlock:admin:"
            f"{SessionTokenManager.hash_sid(raw_sid)}"
        )

    def _unlock_key_from_hash(self, sid_hash: str) -> str:
        return f"{self.settings.redis_prefix}:unlock:admin:{sid_hash}"

    def _pending_totp_key(self, admin_user_id: uuid.UUID) -> str:
        return f"{self.settings.redis_prefix}:totp-pending:admin:{admin_user_id}"

    @staticmethod
    def _totp_context(admin_id: uuid.UUID) -> str:
        return f"admin-user:{admin_id}:totp"

    @staticmethod
    def _pending_totp_context(admin_id: uuid.UUID, sid_hash: str) -> str:
        return f"admin-user:{admin_id}:totp-pending:{sid_hash}"

    @staticmethod
    def _totp_version(encrypted_secret: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            encrypted_secret,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _revoke_sensitive_unlocks(self, admin_user_id: uuid.UUID) -> int:
        rows = list(
            self.db.scalars(
                select(AdminSession)
                .where(
                    AdminSession.admin_user_id == admin_user_id,
                    AdminSession.revoked_at.is_(None),
                    AdminSession.sensitive_unlocked_until.is_not(None),
                )
                .with_for_update()
            )
        )
        redis_keys: list[str] = []
        for row in rows:
            row.sensitive_unlocked_until = None
            redis_keys.append(self._unlock_key_from_hash(row.sid_hash))
        if redis_keys:
            self.redis.delete(*redis_keys)
        if rows:
            self.db.flush()
        return len(rows)

    def _require_active_admin_session(
        self,
        *,
        admin_user_id: uuid.UUID,
        raw_admin_sid: str,
        client_ip: str | None,
    ) -> AdminSession:
        row = self.sessions.get_by_hash(
            SessionTokenManager.hash_sid(raw_admin_sid),
            for_update=True,
        )
        now = utcnow()
        expected_ip = (
            keyed_identifier_hash(client_ip, self.identifier_key, purpose="ip")
            if client_ip is not None
            else None
        )
        if (
            row is None
            or row.admin_user_id != admin_user_id
            or row.revoked_at is not None
            or now >= _aware(row.idle_expires_at)
            or now >= _aware(row.absolute_expires_at)
            or (expected_ip is not None and row.ip_hash != expected_ip)
        ):
            raise AuthenticationFailed("administrator session is invalid")
        return row

    def create_admin(self, *, username: str, password: str) -> AdminUser:
        normalized = normalize_username(username)
        if self.admins.get_by_username(normalized) is not None:
            raise ConflictError("administrator username already exists")
        row = AdminUser(username=normalized, password_hash=self.passwords.hash(password))
        self.admins.add(row)
        return row

    def authenticate(
        self, *, username: str, password: str, client_ip: str | None = None
    ) -> AdminUser:
        try:
            normalized = normalize_username(username)
        except ValueError as exc:
            self.passwords.verify_or_dummy(None, password)
            raise AuthenticationFailed("invalid administrator credentials") from exc
        row = self.admins.get_by_username(normalized, for_update=True)
        password_valid = self.passwords.verify_or_dummy(
            row.password_hash if row is not None and row.is_active else None,
            password,
        )
        if row is None or not row.is_active or not password_valid:
            if row is not None:
                row.failed_login_count += 1
                self.db.flush()
            if self.audit:
                self.audit.record(
                    actor_type="anonymous",
                    action="admin.login_failed",
                    admin_user_id=row.id if row else None,
                    client_ip=client_ip,
                )
            raise AuthenticationFailed("invalid administrator credentials")
        if self.passwords.needs_rehash(row.password_hash):
            row.password_hash = self.passwords.hash(password)
            row.password_changed_at = utcnow()
        row.failed_login_count = 0
        row.last_login_at = utcnow()
        self.db.flush()
        return row

    def issue_session(
        self,
        *,
        admin_user_id: uuid.UUID,
        client_ip: str,
        user_agent: str | None,
        now: datetime | None = None,
    ) -> IssuedSession:
        current = now or utcnow()
        admin = self.admins.get(admin_user_id)
        if admin is None or not admin.is_active:
            raise PermissionDenied("administrator is not active")
        active_rows = list(
            self.db.scalars(
                select(AdminSession)
                .where(
                    AdminSession.admin_user_id == admin_user_id,
                    AdminSession.revoked_at.is_(None),
                )
                .order_by(AdminSession.last_seen_at.desc(), AdminSession.created_at.desc())
                .with_for_update()
            )
        )
        valid_rows: list[AdminSession] = []
        revoked_rows: list[AdminSession] = []
        for existing in active_rows:
            if current >= _aware(existing.idle_expires_at) or current >= _aware(
                existing.absolute_expires_at
            ):
                existing.revoked_at = current
                existing.revoke_reason = "expired_during_session_issue"
                existing.sensitive_unlocked_until = None
                revoked_rows.append(existing)
            else:
                valid_rows.append(existing)
        keep_existing = max(0, self.MAX_ACTIVE_SESSIONS - 1)
        for existing in valid_rows[keep_existing:]:
            existing.revoked_at = current
            existing.revoke_reason = "administrator_session_limit"
            existing.sensitive_unlocked_until = None
            revoked_rows.append(existing)
        redis_keys: list[str] = []
        for existing in revoked_rows:
            redis_keys.extend(
                (
                    SessionTokenManager.redis_key_from_hash(
                        self.settings.redis_prefix, "admin", existing.sid_hash
                    ),
                    self._unlock_key_from_hash(existing.sid_hash),
                )
            )
        if redis_keys:
            self.redis.delete(*redis_keys)
        pending_key = self._pending_totp_key(admin_user_id)
        pending = _json_loads(self.redis.get(pending_key))
        if pending is not None and str(pending.get("sid_hash") or "") in {
            existing.sid_hash for existing in revoked_rows
        }:
            self.redis.delete(pending_key)

        absolute = current + timedelta(seconds=self.settings.admin_absolute_ttl_seconds)
        idle = min(current + timedelta(seconds=self.settings.admin_idle_ttl_seconds), absolute)
        raw_sid = SessionTokenManager.generate()
        ip_hash = keyed_identifier_hash(client_ip, self.identifier_key, purpose="ip")
        row = AdminSession(
            admin_user_id=admin_user_id,
            sid_hash=SessionTokenManager.hash_sid(raw_sid),
            ip_hash=ip_hash,
            user_agent=(user_agent or "")[:512] or None,
            created_at=current,
            last_seen_at=current,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
        )
        self.sessions.add(row)
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="admin.login",
                admin_user_id=admin_user_id,
                resource_type="admin_session",
                resource_id=str(row.id),
                client_ip=client_ip,
                details={"sessions_revoked_during_issue": len(revoked_rows)},
            )
        return IssuedSession(raw_sid, row.id, idle, absolute)

    def recover_session(
        self, raw_sid: str, *, client_ip: str, touch: bool = False
    ) -> AdminSessionState | None:
        now = utcnow()
        ip_hash = keyed_identifier_hash(client_ip, self.identifier_key, purpose="ip")
        row = self.sessions.get_by_hash(SessionTokenManager.hash_sid(raw_sid), for_update=touch)
        if (
            row is None
            or row.revoked_at is not None
            or row.ip_hash != ip_hash
            or now >= _aware(row.idle_expires_at)
            or now >= _aware(row.absolute_expires_at)
        ):
            self.redis.delete(self._session_key(raw_sid), self._unlock_key(raw_sid))
            if row is not None:
                pending_key = self._pending_totp_key(row.admin_user_id)
                pending = _json_loads(self.redis.get(pending_key))
                if pending is not None and str(pending.get("sid_hash") or "") == row.sid_hash:
                    self.redis.delete(pending_key)
            return None
        if touch:
            row.last_seen_at = now
            row.idle_expires_at = min(
                now + timedelta(seconds=self.settings.admin_idle_ttl_seconds),
                _aware(row.absolute_expires_at),
            )
            self.db.flush()
        state = AdminSessionState(
            session_id=row.id,
            admin_user_id=row.admin_user_id,
            ip_hash=row.ip_hash,
            created_at=_aware(row.created_at),
            idle_expires_at=_aware(row.idle_expires_at),
            absolute_expires_at=_aware(row.absolute_expires_at),
        )
        payload = {
            "session_id": str(state.session_id),
            "admin_user_id": str(state.admin_user_id),
            "ip_hash": state.ip_hash,
            "created_at": state.created_at.isoformat(),
            "idle_expires_at": state.idle_expires_at.isoformat(),
            "absolute_expires_at": state.absolute_expires_at.isoformat(),
        }
        ttl = int((min(state.idle_expires_at, state.absolute_expires_at) - now).total_seconds())
        _redis_set_json(self.redis, self._session_key(raw_sid), payload, ttl)
        return state

    def revoke_session(
        self, raw_sid: str, *, client_ip: str, reason: str = "logout"
    ) -> bool:
        row = self.sessions.get_by_hash(SessionTokenManager.hash_sid(raw_sid), for_update=True)
        self.redis.delete(self._session_key(raw_sid), self._unlock_key(raw_sid))
        if row is None or row.revoked_at is not None:
            return False
        pending_key = self._pending_totp_key(row.admin_user_id)
        pending = _json_loads(self.redis.get(pending_key))
        if pending is not None and str(pending.get("sid_hash") or "") == row.sid_hash:
            self.redis.delete(pending_key)
        row.revoked_at = utcnow()
        row.revoke_reason = reason[:160]
        row.sensitive_unlocked_until = None
        self.db.flush()
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="admin.logout",
                admin_user_id=row.admin_user_id,
                resource_type="admin_session",
                resource_id=str(row.id),
                client_ip=client_ip,
            )
        return True

    def begin_totp_enrollment(
        self,
        *,
        admin_user_id: uuid.UUID,
        raw_admin_sid: str,
        client_ip: str | None = None,
        account_label: str | None = None,
    ) -> TOTPEnrollment:
        admin = self.admins.get(admin_user_id, for_update=True)
        if admin is None or not admin.is_active:
            raise NotFoundError("administrator was not found")
        self._require_active_admin_session(
            admin_user_id=admin.id,
            raw_admin_sid=raw_admin_sid,
            client_ip=client_ip,
        )
        sid_hash = SessionTokenManager.hash_sid(raw_admin_sid)
        secret = self.totp.generate_secret()
        encrypted_secret = self.cipher.encrypt_text(
            secret,
            purpose="admin.totp-pending-secret",
            context=self._pending_totp_context(admin.id, sid_hash),
        )
        self._revoke_sensitive_unlocks(admin.id)
        _redis_set_json(
            self.redis,
            self._pending_totp_key(admin.id),
            {
                "admin_user_id": str(admin.id),
                "sid_hash": sid_hash,
                "encrypted_secret": encrypted_secret,
            },
            self.TOTP_PENDING_SECONDS,
        )
        return TOTPEnrollment(
            secret=secret,
            provisioning_uri=self.totp.provisioning_uri(
                secret, account_name=account_label or admin.username
            ),
        )

    def confirm_totp_enrollment(
        self,
        *,
        admin_user_id: uuid.UUID,
        raw_admin_sid: str,
        code: str,
        client_ip: str | None = None,
    ) -> None:
        admin = self.admins.get(admin_user_id, for_update=True)
        if admin is None or not admin.is_active:
            raise NotFoundError("administrator was not found")
        self._require_active_admin_session(
            admin_user_id=admin.id,
            raw_admin_sid=raw_admin_sid,
            client_ip=client_ip,
        )
        sid_hash = SessionTokenManager.hash_sid(raw_admin_sid)
        pending_key = self._pending_totp_key(admin.id)
        pending = _json_loads(self.redis.get(pending_key))
        if (
            pending is None
            or str(pending.get("admin_user_id") or "") != str(admin.id)
            or str(pending.get("sid_hash") or "") != sid_hash
            or not isinstance(pending.get("encrypted_secret"), Mapping)
        ):
            raise NotFoundError("TOTP enrollment was not started")
        secret = self.cipher.decrypt_text(
            pending["encrypted_secret"],
            purpose="admin.totp-pending-secret",
            context=self._pending_totp_context(admin.id, sid_hash),
        )
        result = self.totp.verify(secret, code)
        if not result.valid:
            raise AuthenticationFailed("invalid authenticator code")
        admin.totp_secret_encrypted = self.cipher.encrypt_text(
            secret,
            purpose="admin.totp-secret",
            context=self._totp_context(admin.id),
        )
        admin.totp_enabled = True
        admin.last_totp_counter = result.counter
        self._revoke_sensitive_unlocks(admin.id)
        self.redis.delete(pending_key)
        self.db.flush()
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="admin.totp_enabled",
                admin_user_id=admin.id,
                client_ip=client_ip,
            )

    def cancel_totp_enrollment(
        self,
        *,
        admin_user_id: uuid.UUID,
        raw_admin_sid: str,
        client_ip: str | None = None,
    ) -> bool:
        admin = self.admins.get(admin_user_id, for_update=True)
        if admin is None or not admin.is_active:
            raise NotFoundError("administrator was not found")
        self._require_active_admin_session(
            admin_user_id=admin.id,
            raw_admin_sid=raw_admin_sid,
            client_ip=client_ip,
        )
        sid_hash = SessionTokenManager.hash_sid(raw_admin_sid)
        pending_key = self._pending_totp_key(admin_user_id)
        pending = _json_loads(self.redis.get(pending_key))
        if pending is None:
            return False
        if (
            str(pending.get("admin_user_id") or "") != str(admin_user_id)
            or str(pending.get("sid_hash") or "") != sid_hash
        ):
            raise PermissionDenied("TOTP enrollment belongs to another administrator session")
        self.redis.delete(pending_key)
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="admin.totp_enrollment_cancelled",
                admin_user_id=admin_user_id,
                client_ip=client_ip,
            )
        return True

    def unlock_credentials(
        self,
        *,
        raw_admin_sid: str,
        client_ip: str,
        password: str,
        totp_code: str,
        reason: str,
    ) -> datetime:
        if not reason.strip():
            raise ValueError("a credential access reason is required")
        # The request dependency has already refreshed the session.  Avoid
        # taking the session row lock before the administrator row lock here;
        # TOTP enrollment and password rotation consistently lock admin first
        # and then sessions.
        state = self.recover_session(raw_admin_sid, client_ip=client_ip, touch=False)
        if state is None:
            raise AuthenticationFailed("administrator session is invalid")
        admin = self.admins.get(state.admin_user_id, for_update=True)
        if admin is None or not admin.is_active or not self.passwords.verify(admin.password_hash, password):
            raise AuthenticationFailed("administrator password is invalid")
        if not admin.totp_enabled or admin.totp_secret_encrypted is None:
            raise PermissionDenied("TOTP must be enabled before credentials can be unlocked")
        if _json_loads(self.redis.get(self._pending_totp_key(admin.id))) is not None:
            raise PermissionDenied("TOTP enrollment must be completed or allowed to expire first")
        secret = self.cipher.decrypt_text(
            admin.totp_secret_encrypted,
            purpose="admin.totp-secret",
            context=self._totp_context(admin.id),
        )
        result = self.totp.verify(secret, totp_code, after_counter=admin.last_totp_counter)
        if not result.valid:
            raise AuthenticationFailed("authenticator code is invalid or was already used")
        admin.last_totp_counter = result.counter
        row = self.sessions.get_by_hash(SessionTokenManager.hash_sid(raw_admin_sid), for_update=True)
        current = utcnow()
        if (
            row is None
            or row.id != state.session_id
            or row.revoked_at is not None
            or current >= _aware(row.idle_expires_at)
            or current >= _aware(row.absolute_expires_at)
            or row.ip_hash != state.ip_hash
        ):
            raise AuthenticationFailed("administrator session is invalid")
        expires_at = min(
            current + timedelta(seconds=self.settings.credential_unlock_seconds),
            state.absolute_expires_at,
        )
        row.sensitive_unlocked_until = expires_at
        unlock_payload = {
            "admin_session_id": str(row.id),
            "admin_user_id": str(admin.id),
            "ip_hash": state.ip_hash,
            "expires_at": expires_at.isoformat(),
            "totp_version": self._totp_version(admin.totp_secret_encrypted),
        }
        _redis_set_json(
            self.redis,
            self._unlock_key(raw_admin_sid),
            unlock_payload,
            int((expires_at - utcnow()).total_seconds()),
        )
        self.db.flush()
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="credentials.unlock",
                admin_user_id=admin.id,
                resource_type="admin_session",
                resource_id=str(row.id),
                reason=reason,
                client_ip=client_ip,
                details={"expires_at": expires_at.isoformat()},
            )
        return expires_at

    def require_credentials_unlocked(
        self, *, raw_admin_sid: str, client_ip: str
    ) -> AdminSessionState:
        state = self.recover_session(raw_admin_sid, client_ip=client_ip)
        if state is None:
            raise AuthenticationFailed("administrator session is invalid")
        payload = _json_loads(self.redis.get(self._unlock_key(raw_admin_sid)))
        expected_ip = keyed_identifier_hash(client_ip, self.identifier_key, purpose="ip")
        try:
            valid = (
                payload is not None
                and uuid.UUID(str(payload["admin_session_id"])) == state.session_id
                and uuid.UUID(str(payload["admin_user_id"])) == state.admin_user_id
                and str(payload["ip_hash"]) == expected_ip
                and utcnow() < datetime.fromisoformat(str(payload["expires_at"]))
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            self.redis.delete(self._unlock_key(raw_admin_sid))
            raise PermissionDenied("credential access is locked")
        session_row = self.sessions.get_by_hash(
            SessionTokenManager.hash_sid(raw_admin_sid),
            for_update=True,
        )
        admin = self.admins.get(state.admin_user_id)
        if (
            session_row is None
            or session_row.id != state.session_id
            or session_row.sensitive_unlocked_until is None
            or utcnow() >= _aware(session_row.sensitive_unlocked_until)
            or admin is None
            or not admin.is_active
            or not admin.totp_enabled
            or admin.totp_secret_encrypted is None
            or str(payload.get("totp_version") or "")
            != self._totp_version(admin.totp_secret_encrypted)
        ):
            self.redis.delete(self._unlock_key(raw_admin_sid))
            raise PermissionDenied("credential access is locked")
        return state

    def lock_credentials(
        self,
        *,
        raw_admin_sid: str,
        client_ip: str,
        reason: str = "administrator_requested_lock",
    ) -> bool:
        state = self.recover_session(raw_admin_sid, client_ip=client_ip)
        if state is None:
            raise AuthenticationFailed("administrator session is invalid")
        row = self.sessions.get_by_hash(
            SessionTokenManager.hash_sid(raw_admin_sid),
            for_update=True,
        )
        current = utcnow()
        if (
            row is None
            or row.id != state.session_id
            or row.revoked_at is not None
            or current >= _aware(row.idle_expires_at)
            or current >= _aware(row.absolute_expires_at)
            or row.ip_hash != state.ip_hash
        ):
            raise AuthenticationFailed("administrator session is invalid")
        was_unlocked = row.sensitive_unlocked_until is not None
        row.sensitive_unlocked_until = None
        self.redis.delete(self._unlock_key(raw_admin_sid))
        self.db.flush()
        if self.audit:
            self.audit.record(
                actor_type="admin",
                action="credentials.lock",
                admin_user_id=state.admin_user_id,
                resource_type="admin_session",
                resource_id=str(state.session_id),
                client_ip=client_ip,
                details={"reason": str(reason or "")[:80], "was_unlocked": was_unlocked},
            )
        return was_unlocked


@dataclass(frozen=True, slots=True)
class ExternalCredentials:
    login_account: str
    phone: str | None
    password: str | None
    token: str | None
    token_expires_at: datetime | None


class CredentialAccessService:
    def __init__(
        self,
        db: Session,
        cipher: CredentialCipher,
        admin_auth: AdminAuthService,
        audit: AuditService,
    ):
        self.db = db
        self.cipher = cipher
        self.admin_auth = admin_auth
        self.audit = audit
        self.accounts = ExternalAccountRepository(db)

    @staticmethod
    def _context(account_id: uuid.UUID, field: str) -> str:
        return f"external-account:{account_id}:{field}"

    def view(
        self,
        *,
        raw_admin_sid: str,
        client_ip: str,
        target_user_id: uuid.UUID,
        reason: str,
    ) -> ExternalCredentials:
        if not reason.strip():
            raise ValueError("a credential access reason is required")
        admin_state = self.admin_auth.require_credentials_unlocked(
            raw_admin_sid=raw_admin_sid, client_ip=client_ip
        )
        account = self.accounts.get_for_user(target_user_id)
        if account is None:
            raise NotFoundError("external account was not found")
        result = ExternalCredentials(
            login_account=self.cipher.decrypt_text(
                account.login_account_encrypted,
                purpose="external-account.login",
                context=self._context(account.id, "login"),
            ),
            phone=(
                self.cipher.decrypt_text(
                    account.phone_encrypted,
                    purpose="external-account.phone",
                    context=self._context(account.id, "phone"),
                )
                if account.phone_encrypted
                else None
            ),
            password=(
                self.cipher.decrypt_text(
                    account.password_encrypted,
                    purpose="external-account.password",
                    context=self._context(account.id, "password"),
                )
                if account.password_encrypted
                else None
            ),
            token=(
                self.cipher.decrypt_text(
                    account.token_encrypted,
                    purpose="external-account.token",
                    context=self._context(account.id, "token"),
                )
                if account.token_encrypted
                else None
            ),
            token_expires_at=account.token_expires_at,
        )
        self.audit.record(
            actor_type="admin",
            action="credentials.view",
            admin_user_id=admin_state.admin_user_id,
            target_user_id=target_user_id,
            resource_type="external_account",
            resource_id=str(account.id),
            reason=reason,
            client_ip=client_ip,
            details={"fields": ["login_account", "phone", "password", "token"]},
        )
        return result


class RawResponseService:
    def __init__(self, db: Session, settings: Settings, cipher: CredentialCipher):
        self.db = db
        self.settings = settings
        self.cipher = cipher
        self.repo = RawUpstreamResponseRepository(db)

    @staticmethod
    def _context(row_id: uuid.UUID) -> str:
        return f"raw-upstream-response:{row_id}:payload"

    def store(
        self,
        *,
        owner_user_id: uuid.UUID,
        endpoint: str,
        payload: Any,
        http_status: int | None = None,
        request_correlation_id: str | None = None,
        received_at: datetime | None = None,
    ) -> RawUpstreamResponse:
        now = received_at or utcnow()
        row_id = uuid.uuid4()
        sanitized = redact_raw_payload(payload)
        row = RawUpstreamResponse(
            id=row_id,
            owner_user_id=owner_user_id,
            # Query strings frequently carry temporary signatures or tokens.
            # They are not needed to identify the upstream operation and must
            # not be persisted as plaintext metadata.
            endpoint=str(endpoint or "").split("?", 1)[0].split("#", 1)[0][:512],
            request_correlation_id=(request_correlation_id or "")[:128] or None,
            http_status=http_status,
            encrypted_payload=self.cipher.encrypt_json(
                sanitized,
                purpose="raw-upstream-response.payload",
                context=self._context(row_id),
            ),
            received_at=now,
            expires_at=now + timedelta(days=self.settings.raw_response_retention_days),
        )
        return self.repo.add(row)

    def decrypt(self, row: RawUpstreamResponse) -> Any:
        return self.cipher.decrypt_json(
            row.encrypted_payload,
            purpose="raw-upstream-response.payload",
            context=self._context(row.id),
        )


@dataclass(frozen=True, slots=True)
class MediaReservation:
    media: MediaObject
    user_used_bytes: int
    system_used_bytes: int


class MediaQuotaService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.users = UserRepository(db)
        self.media = MediaObjectRepository(db)

    def reserve(
        self,
        *,
        owner_user_id: uuid.UUID,
        kind: str,
        size_bytes: int,
        r2_bucket: str,
        r2_object_key: str,
        content_type: str,
        sha256: str,
        message_id: uuid.UUID | None = None,
        original_filename: str | None = None,
        source_url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MediaReservation:
        if size_bytes < 0:
            raise ValueError("media size cannot be negative")
        limits = {
            "image": self.settings.media_max_image_bytes,
            "audio": self.settings.media_max_audio_bytes,
            "voice": self.settings.media_max_audio_bytes,
            "video": self.settings.media_max_video_bytes,
            "attachment": self.settings.media_max_attachment_bytes,
        }
        limit = limits.get(kind)
        if limit is None:
            raise ValueError("unsupported media kind")
        if size_bytes > limit:
            raise QuotaExceeded(f"{kind} exceeds the per-file limit")
        if len(sha256) != 64:
            raise ValueError("invalid SHA-256 digest")

        if message_id is not None:
            owned_message = self.db.scalar(
                select(Message).where(
                    Message.id == message_id,
                    Message.owner_user_id == owner_user_id,
                )
            )
            if owned_message is None:
                raise NotFoundError("owned message was not found")

        # 固定锁顺序：系统账本 -> 用户，避免并发上传发生死锁。
        system = self.media.get_system_quota_for_update()
        user = self.users.get(owner_user_id, for_update=True)
        if user is None or user.status != "active":
            raise NotFoundError("active user was not found")
        # 配置只允许缩小已有配额，不会在未审计的情况下意外扩大数据库限额。
        effective_user_quota = min(user.media_quota_bytes, self.settings.per_user_media_quota_bytes)
        effective_system_quota = min(system.quota_bytes, self.settings.global_media_quota_bytes)
        if user.media_used_bytes + size_bytes > effective_user_quota:
            raise QuotaExceeded("user media quota exceeded")
        if system.used_bytes + size_bytes > effective_system_quota:
            raise QuotaExceeded("system media quota exceeded")

        now = utcnow()
        item = MediaObject(
            owner_user_id=owner_user_id,
            message_id=message_id,
            kind=kind,
            status="pending",
            r2_bucket=r2_bucket,
            r2_object_key=r2_object_key,
            original_filename=(original_filename or "")[:255] or None,
            content_type=content_type[:160],
            size_bytes=size_bytes,
            sha256=sha256.lower(),
            source_url=source_url,
            counts_toward_quota=True,
            retention_expires_at=now + timedelta(days=max(1, int(user.chat_retention_days))),
            extra_data=dict(metadata or {}),
        )
        user.media_used_bytes += size_bytes
        system.used_bytes += size_bytes
        system.version += 1
        self.media.add(item)
        return MediaReservation(item, user.media_used_bytes, system.used_bytes)

    def mark_available(self, owner_user_id: uuid.UUID, media_id: uuid.UUID) -> MediaObject:
        item = self.media.get(owner_user_id, media_id)
        if item is None:
            raise NotFoundError("media object was not found")
        if item.status not in {"pending", "uploading", "available"}:
            raise ConflictError("media object cannot become available from its current state")
        item.status = "available"
        self.db.flush()
        return item

    def release(
        self,
        *,
        owner_user_id: uuid.UUID,
        media_id: uuid.UUID,
        final_status: str = "deleted",
    ) -> MediaObject:
        system = self.media.get_system_quota_for_update()
        user = self.users.get(owner_user_id, for_update=True)
        item = self.db.scalar(
            select(MediaObject)
            .where(MediaObject.id == media_id, MediaObject.owner_user_id == owner_user_id)
            .with_for_update()
        )
        if user is None or item is None:
            raise NotFoundError("media object was not found")
        if item.counts_toward_quota:
            user.media_used_bytes = max(0, user.media_used_bytes - item.size_bytes)
            system.used_bytes = max(0, system.used_bytes - item.size_bytes)
            system.version += 1
            item.counts_toward_quota = False
        item.status = final_status[:32]
        item.deleted_at = utcnow()
        item.message_id = None
        self.db.flush()
        return item


class RetentionService:
    """只删除数据库记录；R2 对象须由 worker 先删除后再调用媒体 release。"""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def purge_expired_raw_responses(self, *, at: datetime | None = None) -> int:
        return RawUpstreamResponseRepository(self.db).purge_expired(at=at)

    def purge_expired_audit_logs(self, *, at: datetime | None = None) -> int:
        return AuditLogRepository(self.db).purge_expired(at=at)

    def purge_stale_sessions(
        self,
        *,
        at: datetime | None = None,
        grace_days: int = 7,
    ) -> tuple[int, int]:
        cutoff = (at or utcnow()) - timedelta(days=max(1, int(grace_days)))
        web_result = self.db.execute(
            delete(WebSession).where(
                or_(
                    WebSession.absolute_expires_at <= cutoff,
                    WebSession.revoked_at <= cutoff,
                )
            )
        )
        admin_result = self.db.execute(
            delete(AdminSession).where(
                or_(
                    AdminSession.absolute_expires_at <= cutoff,
                    AdminSession.revoked_at <= cutoff,
                )
            )
        )
        return int(web_result.rowcount or 0), int(admin_result.rowcount or 0)

    def purge_expired_messages(self, *, at: datetime | None = None, limit: int = 1000) -> int:
        now = at or utcnow()
        ids = list(
            self.db.scalars(
                select(Message.id)
                .where(
                    Message.retention_expires_at <= now,
                    ~select(MediaObject.id)
                    .where(MediaObject.message_id == Message.id)
                    .exists(),
                )
                .order_by(Message.retention_expires_at)
                .limit(min(limit, 5000))
            )
        )
        if not ids:
            return 0
        result = self.db.execute(delete(Message).where(Message.id.in_(ids)))
        return int(result.rowcount or 0)
