"""供 FastAPI、RQ worker 与管理端调用的同步业务服务。"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
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
    ChatMessage,
    ExternalAccount,
    InviteCode,
    MediaObject,
    Message,
    RawUpstreamResponse,
    User,
    UserCredential,
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
    UserCredentialRepository,
    WebSessionRepository,
)
from .security import (
    InviteCodeManager,
    PasswordHasher,
    SessionTokenManager,
    TOTPManager,
    UserPasswordHasher,
    keyed_identifier_hash,
    normalize_username,
)


LOGGER = logging.getLogger(__name__)


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


class LocalAuthenticationUnavailable(ServiceError):
    """账号存在，但尚未通过一次上游密码登录建立本地验证材料。"""

    code = "local_authentication_unavailable"


class PasswordPolicyError(ServiceError):
    code = "password_policy_error"


class PermissionDenied(ServiceError):
    code = "permission_denied"


class InviteInvalid(ServiceError):
    code = "invite_invalid"


class QuotaExceeded(ServiceError):
    code = "quota_exceeded"


WEB_LOCAL_PROFILE_FIELDS_KEY = "_web_local_fields"
WEB_LOCAL_PROFILE_UPDATED_AT_KEY = "_web_local_updated_at"
WEB_LOCAL_PROFILE_FIELDS = frozenset(
    {"nickname", "avatar", "signature", "city", "gender", "privacy"}
)
# 与 bbw_web.legacy_media_reference.LOCAL_MEDIA_KEY 保持一致（bbw_prod 不能
# 反向依赖 bbw_web）。媒体迁移器写入的 sidecar 是本地权威投影，上游快照
# 合并时必须保留，否则一次上游登录就会丢弃已归档媒体的本地引用。
LOCAL_MEDIA_SIDECAR_KEY = "_local_media"
_PRESERVED_LOCAL_PROFILE_KEYS = frozenset({LOCAL_MEDIA_SIDECAR_KEY})

# 机会式凭据登记与 bbw_web 的本地认证闸门（_LocalPasswordAuthGate）是同进程
# 内两个独立的信号量：同进程 Argon2 峰值为 bbw_web 本地认证闸门容量 + 本闸门
# 容量之和；本闸门取配置值的一半（至少 1）以控制总内存预算。登记失败不影响
# 上游登录成功，处置策略见 _enroll_password_opportunistically。进程级单例，
# 容量与本地认证闸门同源配置。
_ENROLLMENT_GATE_LOCK = threading.Lock()
_ENROLLMENT_GATE: threading.BoundedSemaphore | None = None


def _opportunistic_enrollment_gate(capacity: int) -> threading.BoundedSemaphore:
    global _ENROLLMENT_GATE
    with _ENROLLMENT_GATE_LOCK:
        if _ENROLLMENT_GATE is None:
            _ENROLLMENT_GATE = threading.BoundedSemaphore(
                max(1, min(8, int(capacity)) // 2)
            )
        return _ENROLLMENT_GATE


def _reset_enrollment_gate_for_tests() -> None:
    """仅供测试重置进程级闸门单例；生产代码不得调用。"""

    global _ENROLLMENT_GATE
    with _ENROLLMENT_GATE_LOCK:
        _ENROLLMENT_GATE = None


def merge_provider_profile_preserving_local(
    current: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge a provider snapshot without overwriting Web-local authority.

    Provider login remains useful for refreshing fields that Web has never
    claimed.  Once a field is changed locally, the marker written alongside
    the profile makes that field authoritative even when a later Banghua
    response is stale.
    """

    current_values = dict(current or {})
    merged = dict(incoming or {})
    raw_local_fields = current_values.get(WEB_LOCAL_PROFILE_FIELDS_KEY)
    local_fields = {
        str(field)
        for field in raw_local_fields
        if str(field) in WEB_LOCAL_PROFILE_FIELDS
    } if isinstance(raw_local_fields, (list, tuple, set, frozenset)) else set()
    for field in local_fields:
        if field in current_values:
            merged[field] = current_values[field]
        else:
            merged.pop(field, None)
    if local_fields:
        merged[WEB_LOCAL_PROFILE_FIELDS_KEY] = sorted(local_fields)
        if WEB_LOCAL_PROFILE_UPDATED_AT_KEY in current_values:
            merged[WEB_LOCAL_PROFILE_UPDATED_AT_KEY] = current_values[
                WEB_LOCAL_PROFILE_UPDATED_AT_KEY
            ]
    for key, value in current_values.items():
        if (
            str(key).startswith("_web_")
            and key != WEB_LOCAL_PROFILE_FIELDS_KEY
        ) or str(key) in _PRESERVED_LOCAL_PROFILE_KEYS:
            merged[key] = value
    return merged


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
    auth_source: str = "provider"


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
            auth_source=str(getattr(row, "auth_source", "provider") or "provider"),
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
            "auth_source": state.auth_source,
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
                auth_source=str(payload.get("auth_source") or "provider"),
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
        auth_source: str = "provider",
        now: datetime | None = None,
    ) -> IssuedSession:
        normalized_auth_source = str(auth_source or "provider").strip().lower()
        if normalized_auth_source not in {"provider", "web-local"}:
            raise ValueError("invalid web session authentication source")
        current = now or utcnow()
        absolute = current + timedelta(seconds=self.settings.user_absolute_ttl_seconds)
        idle = min(current + timedelta(seconds=self.settings.user_idle_ttl_seconds), absolute)
        raw_sid = raw_sid or SessionTokenManager.generate()
        row = WebSession(
            user_id=user_id,
            external_account_id=external_account_id,
            sid_hash=SessionTokenManager.hash_sid(raw_sid),
            auth_source=normalized_auth_source,
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
        auth_source: str = "provider",
        now: datetime | None = None,
    ) -> IssuedSession:
        """持久化 legacy/BFF 已经写入 Cookie 的 SID，库中仍只保存摘要。"""

        return self.issue(
            raw_sid=raw_sid,
            user_id=user_id,
            external_account_id=external_account_id,
            client_ip=client_ip,
            user_agent=user_agent,
            auth_source=auth_source,
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
    local_password_available: bool = False


@dataclass(frozen=True, slots=True)
class LoginCompletion:
    user: User
    external_account: ExternalAccount
    created: bool


@dataclass(frozen=True, slots=True)
class LocalAuthentication:
    user: User
    external_account: ExternalAccount
    credential: UserCredential
    password_rehashed: bool


@dataclass(frozen=True, slots=True)
class CredentialEnrollment:
    credential: UserCredential
    created: bool
    password_changed: bool
    password_rehashed: bool


class UserCredentialService:
    """独立 Web 用户凭据的登记与认证边界。"""

    UPSTREAM_PASSWORD_SOURCE = "upstream_password_login"
    BACKFILL_SOURCE = "encrypted_password_backfill"
    LOCAL_PASSWORD_CHANGE_SOURCE = "web_local_password_change"
    NEW_PASSWORD_MIN_LENGTH = 8
    NEW_PASSWORD_MAX_LENGTH = 128
    CURRENT_PASSWORD_MAX_LENGTH = 4096

    def __init__(self, db: Session, phone_hmac_key: bytes | None = None):
        self.db = db
        self.phone_hmac_key = phone_hmac_key
        self.accounts = ExternalAccountRepository(db)
        self.users = UserRepository(db)
        self.credentials = UserCredentialRepository(db)
        self.passwords = UserPasswordHasher()

    def available_for_user(self, user_id: uuid.UUID) -> bool:
        credential = self.credentials.get_for_user(user_id)
        return credential is not None and credential.disabled_at is None

    def suspend_stale_password(
        self,
        *,
        user_id: uuid.UUID,
        verified_at: datetime,
    ) -> bool | None:
        """Disable an older local digest until a verified refresh can hash it.

        ``None`` means no credential exists, ``False`` means another concurrent
        verified login has already refreshed the row, and ``True`` means this
        call disabled an older digest.  The tri-state lets the login path keep
        first-time enrollment blocking without leaving a revoked password
        usable during an upstream outage.
        """

        confirmed_at = _aware(verified_at)
        credential = self.credentials.get_for_user(user_id, for_update=True)
        if credential is None:
            return None
        if _aware(credential.verified_at) >= confirmed_at:
            return False
        credential.disabled_at = confirmed_at
        self.db.flush()
        return True

    def enroll_verified_password(
        self,
        *,
        user_id: uuid.UUID,
        password: str,
        enrollment_source: str = UPSTREAM_PASSWORD_SOURCE,
        verified_at: datetime | None = None,
    ) -> CredentialEnrollment:
        """登记已由可信来源确认的密码，不接受未经认证的任意密码。"""

        source = str(enrollment_source or "").strip()
        if not source or len(source) > 48:
            raise ValueError("invalid credential enrollment source")
        confirmed_at = _aware(verified_at) if verified_at is not None else utcnow()
        user = self.users.get(user_id, for_update=True)
        if user is None:
            raise NotFoundError("user account was not found")

        credential = self.credentials.get_for_user(user_id, for_update=True)
        if credential is None:
            credential = UserCredential(
                id=uuid.uuid4(),
                user_id=user_id,
                password_hash=self.passwords.hash(password),
                credential_version=1,
                enrollment_source=source,
                verified_at=confirmed_at,
                password_changed_at=confirmed_at,
            )
            self.credentials.add(credential)
            return CredentialEnrollment(credential, True, True, False)

        password_matches = self.passwords.verify(credential.password_hash, password)
        if not password_matches:
            credential.password_hash = self.passwords.hash(password)
            credential.credential_version = max(1, credential.credential_version) + 1
            credential.enrollment_source = source
            credential.password_changed_at = confirmed_at
            credential.verified_at = confirmed_at
            credential.disabled_at = None
            self.db.flush()
            return CredentialEnrollment(credential, False, True, False)

        rehashed = self.passwords.needs_rehash(credential.password_hash)
        if rehashed:
            credential.password_hash = self.passwords.hash(password)
        if confirmed_at > _aware(credential.verified_at):
            credential.verified_at = confirmed_at
        credential.disabled_at = None
        self.db.flush()
        return CredentialEnrollment(credential, False, False, rehashed)

    def authenticate_password(
        self,
        *,
        phone: str,
        password: str,
        provider: str = "beibeiwu",
    ) -> LocalAuthentication:
        """仅访问本地数据库；登录编排层负责限制何时允许降级调用。"""

        if self.phone_hmac_key is None:
            raise RuntimeError("phone HMAC key is required for local authentication")
        normalized = normalize_phone(phone)
        digest = phone_lookup_hmac(normalized, self.phone_hmac_key)
        account = self.accounts.get_by_phone_hmac(
            digest,
            provider=provider,
            for_update=True,
        )
        if account is None:
            self.passwords.verify_or_dummy(None, password)
            raise AuthenticationFailed("invalid login credentials")

        user = self.users.get(account.user_id, for_update=True)
        if user is None:
            self.passwords.verify_or_dummy(None, password)
            raise AuthenticationFailed("invalid login credentials")
        if user.status != "active":
            self.passwords.verify_or_dummy(None, password)
            raise PermissionDenied("user account is not active")

        credential = self.credentials.get_for_user(user.id, for_update=True)
        if credential is None:
            self.passwords.verify_or_dummy(None, password)
            raise LocalAuthenticationUnavailable(
                "local password authentication has not been established"
            )
        if credential.disabled_at is not None:
            self.passwords.verify_or_dummy(None, password)
            raise PermissionDenied("local password authentication is disabled")
        if not self.passwords.verify_or_dummy(credential.password_hash, password):
            raise AuthenticationFailed("invalid login credentials")

        rehashed = self.passwords.needs_rehash(credential.password_hash)
        now = utcnow()
        if rehashed:
            credential.password_hash = self.passwords.hash(password)
        credential.last_authenticated_at = now
        user.last_login_at = now
        self.db.flush()
        return LocalAuthentication(user, account, credential, rehashed)

    @classmethod
    def validate_new_local_password(cls, password: str) -> None:
        """Validate only passwords newly chosen by Web users.

        Imported APK passwords deliberately keep their historical semantics;
        applying this policy while authenticating or backfilling them would
        lock out existing accounts.  New Web-local passwords, however, can use
        a bounded policy without changing any legacy verifier.
        """

        if not isinstance(password, str):
            raise PasswordPolicyError("新密码格式无效")
        if not cls.NEW_PASSWORD_MIN_LENGTH <= len(password) <= cls.NEW_PASSWORD_MAX_LENGTH:
            raise PasswordPolicyError(
                f"新密码长度必须为 {cls.NEW_PASSWORD_MIN_LENGTH} 至 "
                f"{cls.NEW_PASSWORD_MAX_LENGTH} 个字符"
            )
        if password.isspace():
            raise PasswordPolicyError("新密码不能只包含空白字符")
        if "\x00" in password:
            raise PasswordPolicyError("新密码包含无效字符")

    def change_local_password(
        self,
        *,
        user_id: uuid.UUID,
        current_password: str,
        new_password: str,
    ) -> UserCredential:
        """Change one migrated credential after verifying the current password.

        This operation is intentionally local-canonical.  It does not mutate
        ``ExternalAccount.password_encrypted`` because that field represents
        APK/Banghua compatibility material and must not pretend an upstream
        password change succeeded.
        """

        if not isinstance(current_password, str) or not current_password:
            raise AuthenticationFailed("当前密码验证失败")
        if len(current_password) > self.CURRENT_PASSWORD_MAX_LENGTH:
            raise AuthenticationFailed("当前密码验证失败")
        self.validate_new_local_password(new_password)
        if current_password == new_password:
            raise PasswordPolicyError("新密码不能与当前密码相同")

        user = self.users.get(user_id, for_update=True)
        credential = self.credentials.get_for_user(user_id, for_update=True)
        if user is None or user.status != "active":
            self.passwords.verify_or_dummy(None, current_password)
            raise PermissionDenied("账号当前不可用")
        if credential is None or credential.disabled_at is not None:
            self.passwords.verify_or_dummy(None, current_password)
            raise LocalAuthenticationUnavailable("当前账号尚未建立可用的本地密码")
        if not self.passwords.verify_or_dummy(
            credential.password_hash, current_password
        ):
            raise AuthenticationFailed("当前密码验证失败")

        changed_at = utcnow()
        credential.password_hash = self.passwords.hash(new_password)
        credential.credential_version = max(1, credential.credential_version) + 1
        credential.enrollment_source = self.LOCAL_PASSWORD_CHANGE_SOURCE
        credential.password_changed_at = changed_at
        credential.last_authenticated_at = changed_at
        self.db.flush()
        return credential


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
        self.user_credentials = UserCredentialService(db, phone_hmac_key)

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
                False,
                self.user_credentials.available_for_user(user.id),
            )
        return LoginPrecheck(
            digest,
            normalized,
            None,
            None,
            None,
            bool(self.settings.invite_required),
            False,
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
            context.local_password_available,
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
        password_verified: bool,
        token: str | None,
        token_expires_at: datetime | None,
    ) -> datetime:
        authenticated_at = utcnow()
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
        # 只有明确成功的上游密码认证才能更新可逆上游密码。SMS 请求即使夹带
        # password 字段也不能污染它；正常 SMS 登录仍保留原值。
        if password and password_verified:
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
        account.last_authenticated_at = authenticated_at
        return authenticated_at

    def authenticate_local_password(
        self,
        *,
        phone: str,
        password: str,
        provider: str = "beibeiwu",
    ) -> LocalAuthentication:
        """认证一个已机会式迁移的账号，不与上游网络交互。

        登录编排层只能在确认上游不可用时调用。上游明确返回密码错误、封禁或
        其他业务拒绝时不得降级到本方法，否则旧密码可能绕过上游状态。
        """

        return self.user_credentials.authenticate_password(
            phone=phone,
            password=password,
            provider=provider,
        )

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
        password_verified: bool = False,
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
            authenticated_at = self._set_credentials(
                by_phone,
                normalized_phone=normalized,
                login_account=login_account,
                password=password,
                password_verified=password_verified,
                token=token,
                token_expires_at=token_expires_at,
            )
            user.last_login_at = authenticated_at
            current_profile = dict(user.profile or {})
            local_profile_fields = set(
                current_profile.get(WEB_LOCAL_PROFILE_FIELDS_KEY) or ()
            )
            if display_name and "nickname" not in local_profile_fields:
                user.display_name = display_name[:160]
            if profile is not None:
                user.profile = merge_provider_profile_preserving_local(
                    current_profile,
                    profile,
                )
            invite = None
            if require_invite and self.settings.invite_required:
                if not invite_code:
                    raise InviteInvalid("an invitation code is required")
                invite = self.invites.validate(invite_code, for_update=True)
            elif invite_code:
                invite = self.invites.validate(invite_code, for_update=True)
            if invite is not None:
                self.invites.consume_locked(invite)
            if password and password_verified:
                self._enroll_password_opportunistically(
                    user_id=user.id,
                    password=password,
                    verified_at=authenticated_at,
                )
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
        authenticated_at = self._set_credentials(
            account,
            normalized_phone=normalized,
            login_account=login_account,
            password=password,
            password_verified=password_verified,
            token=token,
            token_expires_at=token_expires_at,
        )
        user.last_login_at = authenticated_at
        # There is intentionally no ORM relationship between these security
        # boundary models.  Flush the parent explicitly so PostgreSQL never
        # sees the external account before its referenced user row.
        self.db.add(user)
        self.db.flush()
        self.db.add(account)
        if password and password_verified:
            self._enroll_password_opportunistically(
                user_id=user.id,
                password=password,
                verified_at=authenticated_at,
            )
        if invite is not None:
            self.invites.consume_locked(invite)
        self.db.flush()
        return LoginCompletion(user, account, True)

    def _enroll_password_opportunistically(
        self,
        *,
        user_id: uuid.UUID,
        password: str,
        verified_at: datetime,
    ) -> bool:
        """在进程级闸门内执行机会式登记的 Argon2 运算。

        登录本身已由上游认证成功，不能为登记让内存硬哈希在闸门之外无界并
        发。闸门短暂拥挤（短超时未取得槽位）时按用户是否已有凭据行区分：

        - 已有凭据行：立即禁用比本次上游认证更旧的本地摘要并记 WARNING，
          下一次可信密码登录完成哈希后再恢复本地兜底；
        - 尚无凭据行（新注册或存量未迁移用户）：升级为阻塞等待后强制登记，
          保证任何成功登录的用户至少有一行本地凭据，否则私信、媒体等依赖
          凭据行的主体解析全部失效，本地兜底登录也无从建立。等待上界为
          槽位数 × 单次 Argon2 时长，秒级。
        """

        gate = _opportunistic_enrollment_gate(
            int(getattr(self.settings, "local_password_auth_concurrency", 2))
        )
        if not gate.acquire(timeout=1.0):
            suspended = self.user_credentials.suspend_stale_password(
                user_id=user_id,
                verified_at=verified_at,
            )
            if suspended is not None:
                if suspended is False:
                    return True
                LOGGER.warning(
                    "opportunistic credential refresh deferred for user %s: "
                    "enrollment gate is saturated; the stale local digest was "
                    "disabled until the next verified login refreshes it",
                    user_id,
                )
                return False
            gate.acquire()
        try:
            self.user_credentials.enroll_verified_password(
                user_id=user_id,
                password=password,
                verified_at=verified_at,
            )
            return True
        finally:
            gate.release()


@dataclass(frozen=True, slots=True)
class CredentialBackfillReport:
    dry_run: bool
    scanned: int
    eligible: int
    created: int
    skipped_existing: int
    failed: int


class CredentialBackfillService:
    """从现有上游密文机会式建立独立凭据；绝不删除或改写原密文。"""

    MAX_APPLY_LIMIT = 50

    def __init__(self, db: Session, cipher: CredentialCipher | None = None):
        self.db = db
        self.cipher = cipher
        self.credentials = UserCredentialRepository(db)
        self.user_credentials = UserCredentialService(db)

    @staticmethod
    def _password_context(account_id: uuid.UUID) -> str:
        return f"external-account:{account_id}:password"

    def run(
        self,
        *,
        apply: bool = False,
        provider: str = "beibeiwu",
        limit: int | None = None,
        approved_account_ids: set[uuid.UUID] | frozenset[uuid.UUID] | None = None,
    ) -> CredentialBackfillReport:
        if limit is not None and limit < 1:
            raise ValueError("backfill limit must be positive")
        approved_ids = {
            value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            for value in (approved_account_ids or ())
        }
        if apply and not approved_ids:
            raise ValueError(
                "applying credential backfill requires a non-empty approved account allowlist"
            )
        if apply and limit is None:
            raise ValueError("applying credential backfill requires an explicit limit")
        if apply and limit > self.MAX_APPLY_LIMIT:
            raise ValueError(
                f"credential backfill apply limit must not exceed {self.MAX_APPLY_LIMIT}"
            )
        if apply and limit != 1:
            raise ValueError(
                "credential backfill service applies one account per transaction"
            )
        if apply and self.cipher is None:
            raise RuntimeError("credential cipher is required when applying backfill")
        stmt = (
            select(ExternalAccount)
            .join(User, User.id == ExternalAccount.user_id)
            .where(
                ExternalAccount.provider == provider,
                User.status == "active",
                ExternalAccount.password_encrypted.is_not(None),
                ~select(UserCredential.id)
                .where(UserCredential.user_id == ExternalAccount.user_id)
                .exists(),
            )
            .order_by(ExternalAccount.created_at, ExternalAccount.id)
        )
        if approved_account_ids is not None:
            stmt = stmt.where(ExternalAccount.id.in_(approved_ids))
        if limit is not None:
            stmt = stmt.limit(limit)

        scanned = eligible = created = skipped_existing = failed = 0
        for account in self.db.scalars(stmt):
            scanned += 1
            if self.credentials.get_for_user(account.user_id) is not None:
                skipped_existing += 1
                continue

            if not apply:
                # 默认演练只盘点候选；不触碰解密密钥，不把历史明文带入内存，
                # 也不消耗 Argon2 资源。
                eligible += 1
                continue

            password = ""
            try:
                encrypted = account.password_encrypted
                if encrypted is None:
                    continue
                cipher = self.cipher
                assert cipher is not None
                password = cipher.decrypt_text(
                    encrypted,
                    purpose="external-account.password",
                    context=self._password_context(account.id),
                )
                with self.db.begin_nested():
                    if self.credentials.get_for_user(
                        account.user_id,
                        for_update=True,
                    ) is not None:
                        skipped_existing += 1
                        continue
                    enrollment = self.user_credentials.enroll_verified_password(
                        user_id=account.user_id,
                        password=password,
                        enrollment_source=UserCredentialService.BACKFILL_SOURCE,
                        verified_at=utcnow(),
                    )
                eligible += 1
                created += int(enrollment.created)
            except Exception:
                # 不传播可能包含敏感上下文的异常消息，也不输出账号或明文。
                failed += 1
            finally:
                password = ""

        return CredentialBackfillReport(
            dry_run=not apply,
            scanned=scanned,
            eligible=eligible,
            created=created,
            skipped_existing=skipped_existing,
            failed=failed,
        )


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

    def purge_expired_canonical_messages(
        self,
        *,
        at: datetime | None = None,
        limit: int = 1000,
    ) -> int:
        """Delete expired Web-local authority rows in bounded batches.

        ``message_receipts`` and ``message_deliveries`` reference the canonical
        row with ``ON DELETE CASCADE``.  Once the longest participant retention
        window has elapsed, keeping those compatibility/outbox records would
        both violate retention and leave an unbounded TIM retry backlog.
        """

        now = at or utcnow()
        ids = list(
            self.db.scalars(
                select(ChatMessage.id)
                .where(ChatMessage.retention_expires_at <= now)
                .order_by(ChatMessage.retention_expires_at, ChatMessage.id)
                .limit(min(max(1, int(limit)), 5000))
            )
        )
        if not ids:
            return 0
        result = self.db.execute(delete(ChatMessage).where(ChatMessage.id.in_(ids)))
        return int(result.rowcount or 0)
