"""Privileged administration API.

The router deliberately keeps the single-super-administrator invariant.  It
never directly serializes database ciphertext, lookup/session hashes, R2
bucket/object-key fields or raw cookies.  An authorized short-lived R2 access
URL necessarily contains its opaque object path.  Plain upstream credentials
are available only through the explicit password + TOTP unlock flow provided
by :mod:`bbw_prod.services`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping, NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import func, or_, select, text as sql_text

from bbw_prod.db import session_scope
from bbw_prod.models import (
    ActivityEvent,
    AdminSession,
    AdminUser,
    AuditLog,
    Conversation,
    ExternalAccount,
    InviteCode,
    MediaObject,
    Message,
    RawUpstreamResponse,
    Relationship,
    SystemStorageQuota,
    User,
    WebSession,
    utcnow,
)
from bbw_prod.repositories import (
    AdminUserRepository,
    ExternalAccountRepository,
    MediaObjectRepository,
    UserRepository,
)
from bbw_prod.security import SessionTokenManager, normalize_username
from bbw_prod.services import (
    AdminAuthService,
    AuditService,
    AuthenticationFailed,
    ConflictError,
    CredentialAccessService,
    InviteService,
    NotFoundError,
    PermissionDenied,
    RawResponseService,
    ServiceError,
    UserSessionService,
)


__all__ = ["bootstrap_initial_admin", "router"]


_MAX_PAGE = 1_000
_MAX_LIMIT = 50
_PUBLIC_JSON_MAX_NODES = 5_000
_PUBLIC_JSON_MAX_CHARS = 64_000
_PUBLIC_LIST_JSON_MAX_NODES = 1_000
_PUBLIC_LIST_JSON_MAX_CHARS = 16_000
_PUBLIC_RAW_JSON_MAX_CHARS = 256_000
_HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PUBLIC_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PUBLIC_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_BLOCKED_JSON_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "authortoken",
    "businesstoken",
    "checksum",
    "cookie",
    "credential",
    "digest",
    "encryptedpayload",
    "etag",
    "expiretoken",
    "hash",
    "idcard",
    "iphash",
    "loginaccount",
    "loginaccountencrypted",
    "objectkey",
    "passwd",
    "password",
    "passwordencrypted",
    "passwordhash",
    "phone",
    "phoneencrypted",
    "phonehmac",
    "phonenumber",
    "pwd",
    "r2bucket",
    "r2objectkey",
    "refreshtoken",
    "secret",
    "secretkey",
    "sha256",
    "sidhash",
    "sourceurl",
    "signtoken",
    "token",
    "tokenencrypted",
    "totpsecret",
    "totpsecretencrypted",
    "useraccount",
    "usersig",
    "mobile",
}
_BLOCKED_JSON_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "cookie",
    "objectkey",
    "passwd",
    "password",
    "phonehmac",
    "privatekey",
    "secret",
    "sidhash",
    "token",
    "usersig",
)
_URL_JSON_KEY_FRAGMENTS = ("url", "uri", "link", "href")


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminLoginBody(_StrictBody):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username_input(cls, value: str) -> str:
        return value.strip()


class TotpStartBody(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)
    current_totp: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class TotpConfirmBody(_StrictBody):
    code: str = Field(pattern=r"^[0-9]{6}$")


class CredentialUnlockBody(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str = Field(pattern=r"^[0-9]{6}$")
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class CredentialViewBody(_StrictBody):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class InviteCreateBody(_StrictBody):
    label: str | None = Field(default=None, max_length=120)
    max_uses: int = Field(default=1, ge=1, le=10_000)
    expires_at: datetime | None = None

    @field_validator("label", mode="before")
    @classmethod
    def strip_label(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("invite expiry must include a timezone")
        return value


class UserStatusBody(_StrictBody):
    status: Literal["active", "disabled"]
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class UserMatchPoolOnlineListBody(_StrictBody):
    enabled: StrictBool
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class UserNearbyCustomCityBody(_StrictBody):
    enabled: StrictBool
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class PasswordChangeBody(_StrictBody):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


@dataclass(frozen=True, slots=True)
class AdminContext:
    sid: str
    session_id: uuid.UUID
    admin_user_id: uuid.UUID
    username: str
    totp_enabled: bool
    client_ip: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


def _is_production(settings: Any) -> bool:
    return str(getattr(settings, "environment", "production") or "").strip().lower() in {
        "prod",
        "production",
    }


def _validate_strong_password(password: str, *, username: str = "") -> None:
    """Apply a small policy on top of the Argon2 service length check."""

    if len(password) < 12 or len(password.encode("utf-8")) > 1024:
        raise ValueError("administrator password must contain 12 to 1024 UTF-8 bytes")
    if len(set(password)) < 5:
        raise ValueError("administrator password is too repetitive")
    lowered = password.casefold()
    if lowered in {
        "password1234",
        "administrator",
        "admin123456",
        "123456789012",
        "qwertyuiop12",
    }:
        raise ValueError("administrator password is too common")
    if username and len(username) >= 3 and username.casefold() in lowered:
        raise ValueError("administrator password must not contain the username")
    categories = sum(
        (
            any(char.islower() for char in password),
            any(char.isupper() for char in password),
            any(char.isdigit() for char in password),
            any(not char.isalnum() for char in password),
            any(char.isalpha() and not char.isascii() for char in password),
        )
    )
    if len(password) < 20 and categories < 3:
        raise ValueError(
            "administrator password must use at least three character categories or be 20 characters long"
        )


def _client_ip(request: Request) -> str:
    settings = getattr(getattr(request.app, "state", None), "settings", None)
    trust_proxy = bool(getattr(settings, "trust_proxy_headers", False))
    forwarded = (
        request.headers.get("CF-Connecting-IP"),
        request.headers.get("X-Real-IP"),
        (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0],
    )
    candidates = (*forwarded, request.client.host if request.client else None) if trust_proxy else (
        request.client.host if request.client else None,
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return "unknown"


def _enforce_same_origin(request: Request, response: Response) -> None:
    """Apply no-store headers and require exact Origin on state changes."""

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"

    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    origin = str(request.headers.get("Origin") or "").strip()
    host = str(request.headers.get("Host") or "").strip().lower()
    scheme = str(request.headers.get("X-Forwarded-Proto") or request.url.scheme).strip().lower()
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    try:
        parsed = urlsplit(origin)
    except ValueError:
        parsed = None
    if (
        not origin
        or not host
        or scheme not in {"http", "https"}
        or parsed is None
        or parsed.scheme.lower() != scheme
        or parsed.netloc.lower() != host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (fetch_site and fetch_site != "same-origin")
    ):
        raise HTTPException(status_code=403, detail="cross-origin administrator request rejected")


router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(_enforce_same_origin)],
)


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _persistence(request: Request) -> Any:
    return request.app.state.persistence


def _audit_service(db: Any, request: Request) -> AuditService:
    persistence = _persistence(request)
    return AuditService(db, _settings(request), persistence.session_hmac_key)


def _admin_auth(db: Any, request: Request, audit: AuditService | None = None) -> AdminAuthService:
    persistence = _persistence(request)
    return AdminAuthService(
        db,
        persistence.redis,
        _settings(request),
        persistence.cipher,
        persistence.session_hmac_key,
        audit,
    )


def _admin_cookie_name(request: Request) -> str:
    settings = _settings(request)
    name = str(settings.admin_cookie_name or "").strip()
    if not name:
        raise RuntimeError("administrator cookie name is not configured")
    if _is_production(settings) and not name.startswith("__Host-"):
        raise RuntimeError("production administrator cookie must use the __Host- prefix")
    return name


def _admin_cookie(request: Request) -> str:
    return str(request.cookies.get(_admin_cookie_name(request)) or "")


def _set_admin_cookie(response: Response, request: Request, sid: str) -> None:
    settings = _settings(request)
    response.set_cookie(
        key=_admin_cookie_name(request),
        value=sid,
        max_age=int(settings.admin_absolute_ttl_seconds),
        path="/",
        secure=bool(settings.cookie_secure),
        httponly=True,
        samesite="strict",
    )


def _clear_admin_cookie(response: Response, request: Request) -> None:
    settings = _settings(request)
    response.delete_cookie(
        key=_admin_cookie_name(request),
        path="/",
        secure=bool(settings.cookie_secure),
        httponly=True,
        samesite="strict",
    )


def _raise_service_error(exc: Exception) -> NoReturn:
    if isinstance(exc, AuthenticationFailed):
        raise HTTPException(status_code=401, detail="administrator authentication failed") from exc
    if isinstance(exc, PermissionDenied):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ServiceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def _admin_context(request: Request) -> AdminContext:
    sid = _admin_cookie(request)
    if not sid:
        raise HTTPException(status_code=401, detail="administrator login required")
    client_ip = _client_ip(request)
    try:
        sid_hash = SessionTokenManager.hash_sid(sid)
    except (UnicodeEncodeError, ValueError):
        raise HTTPException(status_code=401, detail="administrator session is invalid or expired")
    # Reject floods before opening a database transaction.  The authenticated
    # admin-id limiter below remains the authoritative per-account ceiling.
    pre_limits = (
        (f"admin-api-pre-ip:{client_ip}", 120),
        (f"admin-api-pre-sid:{sid_hash}", 90),
    )
    if not all(
        _persistence(request).rate_limit(key, limit=limit, window_seconds=60)
        for key, limit in pre_limits
    ):
        raise HTTPException(
            status_code=429,
            detail="administrator API rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    context: AdminContext | None = None
    invalid_reason = "administrator session is invalid or expired"
    with session_scope() as db:
        auth = _admin_auth(db, request)
        state = auth.recover_session(sid, client_ip=client_ip, touch=True)
        if state is not None:
            count = int(db.scalar(select(func.count()).select_from(AdminUser)) or 0)
            admin = AdminUserRepository(db).get(state.admin_user_id)
            if count != 1:
                invalid_reason = "single administrator invariant is not satisfied"
            elif admin is None or not admin.is_active:
                auth.revoke_session(sid, client_ip=client_ip, reason="administrator_inactive")
            else:
                context = AdminContext(
                    sid=sid,
                    session_id=state.session_id,
                    admin_user_id=admin.id,
                    username=admin.username,
                    totp_enabled=bool(admin.totp_enabled),
                    client_ip=client_ip,
                    idle_expires_at=state.idle_expires_at,
                    absolute_expires_at=state.absolute_expires_at,
                )
    if context is None:
        status_code = 503 if "invariant" in invalid_reason else 401
        raise HTTPException(status_code=status_code, detail=invalid_reason)
    if not _persistence(request).rate_limit(
        f"admin-api:{context.admin_user_id}",
        limit=60,
        window_seconds=60,
    ):
        raise HTTPException(
            status_code=429,
            detail="administrator API rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    return context


def _failure_key(request: Request, scope: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()
    return f"{_settings(request).redis_prefix}:admin-fail:{scope}:{digest}"


def _failure_count(request: Request, keys: list[str]) -> int:
    redis = _persistence(request).redis
    return max((int(redis.get(key) or 0) for key in keys), default=0)


def _record_failure(request: Request, keys: list[str]) -> None:
    redis = _persistence(request).redis
    pipe = redis.pipeline()
    for key in keys:
        pipe.incr(key)
        pipe.expire(key, 15 * 60)
    pipe.execute()


def _clear_failures(request: Request, keys: list[str]) -> None:
    if keys:
        _persistence(request).redis.delete(*keys)


def _login_failure_keys(request: Request, username: str, client_ip: str) -> dict[str, str]:
    normalized = _rate_username(username)
    return {
        "name": _failure_key(request, "login-name", normalized),
        "ip": _failure_key(request, "login-ip", client_ip),
        "combo": _failure_key(request, "login-name-ip", f"{normalized}\n{client_ip}"),
    }


def _check_login_failure_limit(request: Request, keys: Mapping[str, str]) -> None:
    redis = _persistence(request).redis
    counts = {name: int(redis.get(key) or 0) for name, key in keys.items()}
    if counts.get("combo", 0) >= 5 or counts.get("ip", 0) >= 20 or counts.get("name", 0) >= 50:
        raise HTTPException(
            status_code=429,
            detail="too many administrator login failures; retry in 15 minutes",
            headers={"Retry-After": "900"},
        )


def _clear_login_failures(request: Request, keys: Mapping[str, str]) -> None:
    # Keep the IP-only failure history so one successful login cannot erase
    # evidence of unrelated attempts from the same source.
    _clear_failures(request, [keys["name"], keys["combo"]])


def _password_lock_key(request: Request) -> str:
    return f"{_settings(request).redis_prefix}:admin-lock:password-verification"


@contextmanager
def _password_verification_guard(request: Request):
    """Serialize memory-hard Argon2 work on the small production host."""

    lock = _persistence(request).redis.lock(
        _password_lock_key(request),
        timeout=30,
        blocking_timeout=1,
        thread_local=False,
    )
    if not lock.acquire(blocking=True):
        raise HTTPException(
            status_code=429,
            detail="another administrator password verification is in progress",
            headers={"Retry-After": "2"},
        )
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _password_verification_dependency(request: Request):
    with _password_verification_guard(request):
        yield None


def _rate_username(username: str) -> str:
    try:
        return normalize_username(username)
    except ValueError:
        return unicodedata.normalize("NFKC", str(username or "")).strip().casefold()[:80] or "-"


def _sensitive_failure_key(request: Request, context: AdminContext, scope: str) -> list[str]:
    sid_hash = SessionTokenManager.hash_sid(context.sid)
    admin_id = str(context.admin_user_id)
    return [
        _failure_key(request, f"{scope}-admin", admin_id),
        _failure_key(request, f"{scope}-ip", context.client_ip),
        _failure_key(request, f"{scope}-admin-ip", f"{admin_id}\n{context.client_ip}"),
        _failure_key(request, f"{scope}-sid", sid_hash),
    ]


def _check_failure_limit(request: Request, keys: list[str]) -> None:
    if _failure_count(request, keys) >= 5:
        raise HTTPException(
            status_code=429,
            detail="too many administrator verification failures; retry in 15 minutes",
            headers={"Retry-After": "900"},
        )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.isoformat()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _offset(page: int, limit: int) -> int:
    return (int(page) - 1) * int(limit)


def _normalized_json_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _public_json(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
    key_hint: str = "",
) -> Any:
    """Bound untrusted JSON and redact credential/hash-like keys."""

    if budget is None:
        budget = [_PUBLIC_JSON_MAX_NODES, _PUBLIC_JSON_MAX_CHARS]
    if depth >= 10 or budget[0] <= 0 or budget[1] <= 0:
        return "[TRUNCATED]"
    budget[0] -= 1
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            raw_key_text = str(key)
            key_text = raw_key_text[:160]
            normalized_key = _normalized_json_key(raw_key_text)
            if normalized_key in _BLOCKED_JSON_KEYS or any(
                fragment in normalized_key for fragment in _BLOCKED_JSON_KEY_FRAGMENTS
            ):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _public_json(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    key_hint=normalized_key,
                )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _public_json(item, depth=depth + 1, budget=budget, key_hint=key_hint)
            for item in list(value)[:200]
        ]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, bytes):
        return "[BINARY]"
    if isinstance(value, str):
        text = _PUBLIC_BEARER_RE.sub("Bearer [REDACTED]", value)
        text = _PUBLIC_JWT_RE.sub("[REDACTED_JWT]", text)
        if any(
            fragment in key_hint for fragment in _URL_JSON_KEY_FRAGMENTS
        ) or text.lstrip().lower().startswith(("https://", "http://")):
            text = _safe_endpoint(text)
        stripped = text.strip()
        if depth < 9 and len(stripped) <= 128_000 and stripped[:1] in {"{", "["}:
            try:
                nested = json.loads(stripped)
            except (TypeError, ValueError):
                nested = None
            if isinstance(nested, (dict, list)):
                text = json.dumps(
                    _public_json(
                        nested,
                        depth=depth + 1,
                        budget=budget,
                        key_hint=key_hint,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        available = max(0, min(20_000, budget[1]))
        result = text[:available]
        budget[1] -= len(result)
        return result if len(text) <= available else result + "[TRUNCATED]"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:20_000]


def _safe_endpoint(value: str) -> str:
    return str(value or "").split("?", 1)[0].split("#", 1)[0][:512]


def _public_list_json(value: Any) -> Any:
    return _public_json(
        value,
        budget=[_PUBLIC_LIST_JSON_MAX_NODES, _PUBLIC_LIST_JSON_MAX_CHARS],
    )


def _safe_resource_id(value: str | None) -> str | None:
    normalized = str(value or "")[:128]
    if not normalized:
        return None
    return "[REDACTED]" if _HEX_HASH_RE.fullmatch(normalized) else normalized


def _admin_public(admin: AdminUser) -> dict[str, Any]:
    return {
        "id": str(admin.id),
        "username": admin.username,
        "role": "superadmin",
        "is_active": bool(admin.is_active),
        "totp_enabled": bool(admin.totp_enabled),
        "last_login_at": _iso(admin.last_login_at),
        "password_changed_at": _iso(admin.password_changed_at),
        "created_at": _iso(admin.created_at),
        "updated_at": _iso(admin.updated_at),
    }


def _invite_public(row: InviteCode) -> dict[str, Any]:
    now = utcnow()
    if row.disabled_at is not None:
        state = "disabled"
    elif row.expires_at is not None and _aware(row.expires_at) <= now:
        state = "expired"
    elif row.use_count >= row.max_uses:
        state = "exhausted"
    else:
        state = "active"
    return {
        "id": str(row.id),
        "label": row.label,
        "status": state,
        "max_uses": int(row.max_uses),
        "use_count": int(row.use_count),
        "expires_at": _iso(row.expires_at),
        "disabled_at": _iso(row.disabled_at),
        "last_used_at": _iso(row.last_used_at),
        "created_by_admin_id": str(row.created_by_admin_id) if row.created_by_admin_id else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _user_public(user: User, account: ExternalAccount | None) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "status": user.status,
        "display_name": user.display_name,
        "upstream_uid": account.upstream_uid if account else None,
        "provider": account.provider if account else None,
        "sync_enabled": bool(account.sync_enabled) if account else False,
        "media_used_bytes": int(user.media_used_bytes),
        "media_quota_bytes": int(user.media_quota_bytes),
        "chat_retention_days": int(user.chat_retention_days),
        "match_pool_online_list_enabled": bool(
            user.match_pool_online_list_enabled
        ),
        "nearby_custom_city_enabled": bool(user.nearby_custom_city_enabled),
        "last_login_at": _iso(user.last_login_at),
        "last_authenticated_at": _iso(account.last_authenticated_at) if account else None,
        "last_sync_at": _iso(account.last_sync_at) if account else None,
        "disabled_at": _iso(user.disabled_at),
        "created_at": _iso(user.created_at),
        "updated_at": _iso(user.updated_at),
    }


def _conversation_public(row: Conversation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "provider": row.provider,
        "upstream_conversation_id": row.upstream_conversation_id,
        "peer_upstream_uid": row.peer_upstream_uid,
        "kind": row.kind,
        "title": row.title,
        "unread_count": int(row.unread_count),
        "last_message_at": _iso(row.last_message_at),
        "metadata": _public_list_json(row.extra_data),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _message_public(row: Message) -> dict[str, Any]:
    body = str(row.body or "")
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id),
        "provider": row.provider,
        "upstream_message_id": row.upstream_message_id,
        "direction": row.direction,
        "sender_upstream_uid": row.sender_upstream_uid,
        "recipient_upstream_uid": row.recipient_upstream_uid,
        "message_type": row.message_type,
        "body": body[:50_000] if row.body is not None else None,
        "body_truncated": len(body) > 50_000,
        "status": row.status,
        "occurred_at": _iso(row.occurred_at),
        "retention_expires_at": _iso(row.retention_expires_at),
        "metadata": _public_list_json(row.extra_data),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _media_public(row: MediaObject) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "message_id": str(row.message_id) if row.message_id else None,
        "kind": row.kind,
        "status": row.status,
        "original_filename": row.original_filename,
        "content_type": row.content_type,
        "size_bytes": int(row.size_bytes),
        "counts_toward_quota": bool(row.counts_toward_quota),
        "retention_expires_at": _iso(row.retention_expires_at),
        "deleted_at": _iso(row.deleted_at),
        "download_available": bool(
            row.status == "available"
            and row.deleted_at is None
            and _aware(row.retention_expires_at) > utcnow()
        ),
        "metadata": _public_list_json(row.extra_data),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _relationship_public(row: Relationship) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "provider": row.provider,
        "subject_upstream_uid": row.subject_upstream_uid,
        "kind": row.kind,
        "status": row.status,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "metadata": _public_list_json(row.extra_data),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _activity_public(row: ActivityEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "provider": row.provider,
        "upstream_event_id": row.upstream_event_id,
        "event_type": row.event_type,
        "actor_upstream_uid": row.actor_upstream_uid,
        "subject_upstream_uid": row.subject_upstream_uid,
        "occurred_at": _iso(row.occurred_at),
        "details": _public_list_json(row.details),
        "created_at": _iso(row.created_at),
    }


def _raw_public(row: RawUpstreamResponse) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "endpoint": _safe_endpoint(row.endpoint),
        "http_status": row.http_status,
        "received_at": _iso(row.received_at),
        "expires_at": _iso(row.expires_at),
    }


def _audit_public(row: AuditLog) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "admin_user_id": str(row.admin_user_id) if row.admin_user_id else None,
        "actor_type": row.actor_type,
        "action": row.action,
        "target_user_id": str(row.target_user_id) if row.target_user_id else None,
        "resource_type": row.resource_type,
        "resource_id": _safe_resource_id(row.resource_id),
        "reason": row.reason,
        "details": _public_list_json(row.details),
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
    }


def _require_user(db: Any, user_id: uuid.UUID, *, for_update: bool = False) -> User:
    user = UserRepository(db).get(user_id, for_update=for_update)
    if user is None:
        raise NotFoundError("user was not found")
    return user


def _record_read(
    audit: AuditService,
    context: AdminContext,
    *,
    action: str,
    target_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    audit.record(
        actor_type="admin",
        action=action,
        admin_user_id=context.admin_user_id,
        target_user_id=target_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        client_ip=context.client_ip,
        details=details,
    )


def bootstrap_initial_admin(settings: Any, persistence: Any) -> bool:
    """Create the only administrator when the database is still empty.

    The password is read exclusively from ``BBW_ADMIN_INITIAL_PASSWORD_FILE``.
    Existing installations do not need the bootstrap file after an admin row
    exists, but more than one row is always treated as a configuration error.
    """

    password_env = os.getenv("BBW_ADMIN_INITIAL_PASSWORD")
    password_file = str(os.getenv("BBW_ADMIN_INITIAL_PASSWORD_FILE") or "").strip()
    username = str(os.getenv("BBW_ADMIN_INITIAL_USERNAME") or "admin").strip()
    if password_env is not None:
        raise RuntimeError(
            "BBW_ADMIN_INITIAL_PASSWORD is not allowed; use BBW_ADMIN_INITIAL_PASSWORD_FILE"
        )
    try:
        username = normalize_username(username)
    except ValueError as exc:
        raise RuntimeError(f"invalid initial administrator username: {exc}") from exc
    with session_scope() as db:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            db.execute(sql_text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 1111643987})
        count = int(db.scalar(select(func.count()).select_from(AdminUser)) or 0)
        if count > 1:
            raise RuntimeError("single-super-administrator invariant violated: multiple administrators exist")
        if count == 1:
            return False
        if not password_file:
            qualifier = "production " if _is_production(settings) else ""
            raise RuntimeError(
                f"{qualifier}bootstrap requires BBW_ADMIN_INITIAL_PASSWORD_FILE"
            )
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"cannot read BBW_ADMIN_INITIAL_PASSWORD_FILE: {password_file}"
            ) from exc
        if not password:
            raise RuntimeError("initial administrator password file is empty")
        try:
            _validate_strong_password(password, username=username)
            audit = AuditService(db, settings, persistence.session_hmac_key)
            auth = AdminAuthService(
                db,
                persistence.redis,
                settings,
                persistence.cipher,
                persistence.session_hmac_key,
                audit,
            )
            admin = auth.create_admin(username=username, password=password)
            audit.record(
                actor_type="system",
                action="admin.bootstrap",
                admin_user_id=admin.id,
                resource_type="admin_user",
                resource_id=str(admin.id),
                details={"username": admin.username},
            )
        except ValueError as exc:
            prefix = "production " if _is_production(settings) else ""
            raise RuntimeError(f"{prefix}initial administrator password is weak: {exc}") from exc
    return True


@router.post("/login")
def admin_login(
    body: AdminLoginBody,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    client_ip = _client_ip(request)
    keys = _login_failure_keys(request, body.username, client_ip)
    normalized_username = _rate_username(body.username)
    attempt_limits = (
        (f"admin-login-attempt-ip:{client_ip}", 20),
        (f"admin-login-attempt-combo:{normalized_username}:{client_ip}", 10),
    )
    if not all(
        _persistence(request).rate_limit(key, limit=limit, window_seconds=60)
        for key, limit in attempt_limits
    ):
        raise HTTPException(
            status_code=429,
            detail="administrator login rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    _check_login_failure_limit(request, keys)
    with _password_verification_guard(request):
        # Recheck after serialization so concurrent failures cannot all pass the
        # pre-hash check and exhaust the small server with Argon2 work.
        _check_login_failure_limit(request, keys)
        failure: Exception | None = None
        issued: Any = None
        admin_public: dict[str, Any] | None = None
        with session_scope() as db:
            audit = _audit_service(db, request)
            auth = _admin_auth(db, request, audit)
            count = int(db.scalar(select(func.count()).select_from(AdminUser)) or 0)
            if count != 1:
                raise HTTPException(status_code=503, detail="administrator bootstrap is incomplete")
            try:
                admin = auth.authenticate(
                    username=body.username,
                    password=body.password,
                    client_ip=client_ip,
                )
                issued = auth.issue_session(
                    admin_user_id=admin.id,
                    client_ip=client_ip,
                    user_agent=str(request.headers.get("User-Agent") or "")[:512],
                )
                admin_public = _admin_public(admin)
            except AuthenticationFailed as exc:
                failure = exc
        if failure is not None:
            _record_failure(request, list(keys.values()))
            _raise_service_error(failure)
        _clear_login_failures(request, keys)
        assert issued is not None and admin_public is not None
        _set_admin_cookie(response, request, issued.sid)
        return {
            "ok": True,
            "admin": admin_public,
            "idle_expires_at": _iso(issued.idle_expires_at),
            "absolute_expires_at": _iso(issued.absolute_expires_at),
        }


@router.post("/logout")
def admin_logout(request: Request, response: Response) -> dict[str, Any]:
    sid = _admin_cookie(request)
    if sid:
        with session_scope() as db:
            audit = _audit_service(db, request)
            _admin_auth(db, request, audit).revoke_session(
                sid,
                client_ip=_client_ip(request),
                reason="logout",
            )
    _clear_admin_cookie(response, request)
    return {"ok": True}


@router.get("/me")
def admin_me(
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    with session_scope() as db:
        admin = AdminUserRepository(db).get(context.admin_user_id)
        if admin is None:
            raise HTTPException(status_code=401, detail="administrator is unavailable")
        _record_read(
            _audit_service(db, request),
            context,
            action="admin.me_view",
            resource_type="admin_user",
            resource_id=str(admin.id),
        )
        public = _admin_public(admin)
    return {
        "ok": True,
        "admin": public,
        "session": {
            "id": str(context.session_id),
            "idle_expires_at": _iso(context.idle_expires_at),
            "absolute_expires_at": _iso(context.absolute_expires_at),
        },
    }


@router.post("/totp/start")
def start_totp(
    body: TotpStartBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
    _password_guard: None = Depends(_password_verification_dependency),
) -> dict[str, Any]:
    keys = _sensitive_failure_key(request, context, "totp-verification")
    _check_failure_limit(request, keys)
    failure: Exception | None = None
    enrollment: Any = None
    was_enabled = False
    with session_scope() as db:
        audit = _audit_service(db, request)
        auth = _admin_auth(db, request, audit)
        admin = AdminUserRepository(db).get(context.admin_user_id, for_update=True)
        if admin is None or not auth.passwords.verify(admin.password_hash, body.password):
            failure = AuthenticationFailed("administrator password is invalid")
        else:
            was_enabled = bool(admin.totp_enabled)
            try:
                if was_enabled:
                    if admin.totp_secret_encrypted is None or not body.current_totp:
                        raise AuthenticationFailed("current authenticator code is required")
                    secret = auth.cipher.decrypt_text(
                        admin.totp_secret_encrypted,
                        purpose="admin.totp-secret",
                        context=auth._totp_context(admin.id),
                    )
                    verified = auth.totp.verify(
                        secret,
                        body.current_totp,
                        after_counter=admin.last_totp_counter,
                    )
                    if not verified.valid:
                        raise AuthenticationFailed("current authenticator code is invalid")
                    admin.last_totp_counter = verified.counter
                enrollment = auth.begin_totp_enrollment(
                    admin_user_id=admin.id,
                    raw_admin_sid=context.sid,
                    client_ip=context.client_ip,
                    account_label=admin.username,
                )
            except (AuthenticationFailed, NotFoundError, PermissionDenied, ValueError) as exc:
                failure = exc
        audit.record(
            actor_type="admin",
            action=(
                "admin.totp_enrollment_started"
                if enrollment is not None
                else "admin.totp_start_failed"
            ),
            admin_user_id=admin.id if admin is not None else context.admin_user_id,
            client_ip=context.client_ip,
            details={"re_enrollment": was_enabled} if enrollment is not None else None,
        )
    if failure is not None:
        _record_failure(request, keys)
        _raise_service_error(failure)
    _clear_failures(request, keys)
    assert enrollment is not None
    return {
        "ok": True,
        "secret": enrollment.secret,
        "provisioning_uri": enrollment.provisioning_uri,
    }


@router.post("/totp/confirm")
def confirm_totp(
    body: TotpConfirmBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    keys = _sensitive_failure_key(request, context, "totp-verification")
    _check_failure_limit(request, keys)
    failure: Exception | None = None
    with session_scope() as db:
        audit = _audit_service(db, request)
        auth = _admin_auth(db, request, audit)
        try:
            auth.confirm_totp_enrollment(
                admin_user_id=context.admin_user_id,
                raw_admin_sid=context.sid,
                code=body.code,
                client_ip=context.client_ip,
            )
        except (AuthenticationFailed, NotFoundError, PermissionDenied) as exc:
            audit.record(
                actor_type="admin",
                action="admin.totp_confirm_failed",
                admin_user_id=context.admin_user_id,
                client_ip=context.client_ip,
            )
            failure = exc
    if failure is not None:
        _record_failure(request, keys)
        _raise_service_error(failure)
    _clear_failures(request, keys)
    return {"ok": True, "totp_enabled": True}


@router.post("/totp/cancel")
def cancel_totp(
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            audit = _audit_service(db, request)
            cancelled = _admin_auth(db, request, audit).cancel_totp_enrollment(
                admin_user_id=context.admin_user_id,
                raw_admin_sid=context.sid,
                client_ip=context.client_ip,
            )
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "cancelled": cancelled}


@router.post("/credentials/unlock")
def unlock_credentials(
    body: CredentialUnlockBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
    _password_guard: None = Depends(_password_verification_dependency),
) -> dict[str, Any]:
    keys = _sensitive_failure_key(request, context, "totp-verification")
    _check_failure_limit(request, keys)
    failure: Exception | None = None
    expires_at: datetime | None = None
    with session_scope() as db:
        audit = _audit_service(db, request)
        auth = _admin_auth(db, request, audit)
        try:
            expires_at = auth.unlock_credentials(
                raw_admin_sid=context.sid,
                client_ip=context.client_ip,
                password=body.password,
                totp_code=body.totp_code,
                reason=body.reason,
            )
        except (AuthenticationFailed, PermissionDenied, ValueError) as exc:
            audit.record(
                actor_type="admin",
                action="credentials.unlock_failed",
                admin_user_id=context.admin_user_id,
                resource_type="admin_session",
                resource_id=str(context.session_id),
                reason=body.reason,
                client_ip=context.client_ip,
            )
            failure = exc
    if failure is not None:
        _record_failure(request, keys)
        _raise_service_error(failure)
    _clear_failures(request, keys)
    return {"ok": True, "unlocked_until": _iso(expires_at)}


@router.post("/credentials/lock")
def lock_credentials(
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    with session_scope() as db:
        audit = _audit_service(db, request)
        was_unlocked = _admin_auth(db, request, audit).lock_credentials(
            raw_admin_sid=context.sid,
            client_ip=context.client_ip,
        )
    return {"ok": True, "was_unlocked": was_unlocked}


@router.post("/password")
def change_admin_password(
    body: PasswordChangeBody,
    request: Request,
    response: Response,
    context: AdminContext = Depends(_admin_context),
    _password_guard: None = Depends(_password_verification_dependency),
) -> dict[str, Any]:
    keys = _sensitive_failure_key(request, context, "password-change")
    _check_failure_limit(request, keys)
    failure: Exception | None = None
    issued: Any = None
    revoked_other = 0
    with session_scope() as db:
        audit = _audit_service(db, request)
        auth = _admin_auth(db, request, audit)
        admin = AdminUserRepository(db).get(context.admin_user_id, for_update=True)
        if admin is None or not auth.passwords.verify(admin.password_hash, body.current_password):
            audit.record(
                actor_type="admin",
                action="admin.password_change_failed",
                admin_user_id=context.admin_user_id,
                client_ip=context.client_ip,
            )
            failure = AuthenticationFailed("current administrator password is invalid")
        else:
            try:
                _validate_strong_password(body.new_password, username=admin.username)
                if auth.passwords.verify(admin.password_hash, body.new_password):
                    raise ValueError("new administrator password must differ from the current password")
                admin.password_hash = auth.passwords.hash(body.new_password)
                admin.password_changed_at = utcnow()
                rows = list(
                    db.scalars(
                        select(AdminSession)
                        .where(
                            AdminSession.admin_user_id == admin.id,
                            AdminSession.id != context.session_id,
                            AdminSession.revoked_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                now = utcnow()
                redis_keys: list[str] = []
                for row in rows:
                    row.revoked_at = now
                    row.revoke_reason = "password_changed"
                    row.sensitive_unlocked_until = None
                    redis_keys.extend(
                        (
                            SessionTokenManager.redis_key_from_hash(
                                _settings(request).redis_prefix,
                                "admin",
                                row.sid_hash,
                            ),
                            f"{_settings(request).redis_prefix}:unlock:admin:{row.sid_hash}",
                        )
                    )
                if redis_keys:
                    _persistence(request).redis.delete(*redis_keys)
                revoked_other = len(rows)
                auth.revoke_session(
                    context.sid,
                    client_ip=context.client_ip,
                    reason="password_changed_rotate",
                )
                issued = auth.issue_session(
                    admin_user_id=admin.id,
                    client_ip=context.client_ip,
                    user_agent=str(request.headers.get("User-Agent") or "")[:512],
                )
                audit.record(
                    actor_type="admin",
                    action="admin.password_changed",
                    admin_user_id=admin.id,
                    resource_type="admin_user",
                    resource_id=str(admin.id),
                    client_ip=context.client_ip,
                    details={"revoked_other_sessions": revoked_other, "session_rotated": True},
                )
            except ValueError as exc:
                audit.record(
                    actor_type="admin",
                    action="admin.password_change_failed",
                    admin_user_id=admin.id,
                    client_ip=context.client_ip,
                    details={"reason": "new_password_policy"},
                )
                failure = exc
    if failure is not None:
        _record_failure(request, keys)
        _raise_service_error(failure)
    _clear_failures(request, keys)
    assert issued is not None
    _set_admin_cookie(response, request, issued.sid)
    return {
        "ok": True,
        "revoked_other_sessions": revoked_other,
        "session_rotated": True,
        "idle_expires_at": _iso(issued.idle_expires_at),
        "absolute_expires_at": _iso(issued.absolute_expires_at),
    }


@router.get("/invites")
def list_invites(
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    state: Literal["all", "active", "disabled", "expired", "exhausted"] = "all",
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    now = utcnow()
    conditions: list[Any] = []
    if state == "active":
        conditions.extend(
            (
                InviteCode.disabled_at.is_(None),
                or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
                InviteCode.use_count < InviteCode.max_uses,
            )
        )
    elif state == "disabled":
        conditions.append(InviteCode.disabled_at.is_not(None))
    elif state == "expired":
        conditions.extend((InviteCode.disabled_at.is_(None), InviteCode.expires_at <= now))
    elif state == "exhausted":
        conditions.extend(
            (
                InviteCode.disabled_at.is_(None),
                or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
                InviteCode.use_count >= InviteCode.max_uses,
            )
        )
    with session_scope() as db:
        stmt = select(InviteCode)
        count_stmt = select(func.count()).select_from(InviteCode)
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        rows = list(
            db.scalars(
                stmt.order_by(InviteCode.created_at.desc())
                .offset(_offset(page, limit))
                .limit(limit)
            )
        )
        total = int(db.scalar(count_stmt) or 0)
        _record_read(
            _audit_service(db, request),
            context,
            action="invite.list",
            resource_type="invite_code",
            details={"page": page, "limit": limit, "state": state},
        )
        items = [_invite_public(row) for row in rows]
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.post("/invites")
def create_invite(
    body: InviteCreateBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            audit = _audit_service(db, request)
            row, raw_code = InviteService(db, _settings(request), audit).create(
                admin_user_id=context.admin_user_id,
                label=body.label,
                max_uses=body.max_uses,
                expires_at=body.expires_at,
                client_ip=context.client_ip,
            )
            item = _invite_public(row)
    except Exception as exc:
        if isinstance(exc, (ServiceError, ValueError)):
            _raise_service_error(exc)
        raise
    return {"ok": True, "invite": item, "code": raw_code}


@router.post("/invites/{invite_id}/disable")
def disable_invite(
    invite_id: uuid.UUID,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            audit = _audit_service(db, request)
            row = InviteService(db, _settings(request), audit).disable(
                invite_id,
                admin_user_id=context.admin_user_id,
                client_ip=context.client_ip,
            )
            item = _invite_public(row)
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "invite": item}


@router.get("/users")
def list_users(
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    status_filter: Literal["all", "active", "disabled"] = Query("all", alias="status"),
    search: str = Query("", max_length=160),
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    normalized_search = search.strip()
    with session_scope() as db:
        stmt = select(User, ExternalAccount).outerjoin(
            ExternalAccount, ExternalAccount.user_id == User.id
        )
        count_stmt = select(func.count()).select_from(User).outerjoin(
            ExternalAccount, ExternalAccount.user_id == User.id
        )
        conditions: list[Any] = []
        if status_filter != "all":
            conditions.append(User.status == status_filter)
        if normalized_search:
            escaped = (
                normalized_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            search_terms: list[Any] = [
                User.display_name.ilike(pattern, escape="\\"),
                ExternalAccount.upstream_uid.ilike(pattern, escape="\\"),
            ]
            try:
                search_terms.append(User.id == uuid.UUID(normalized_search))
            except ValueError:
                pass
            conditions.append(or_(*search_terms))
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        rows = list(
            db.execute(
                stmt.order_by(User.created_at.desc())
                .offset(_offset(page, limit))
                .limit(limit)
            )
        )
        total = int(db.scalar(count_stmt) or 0)
        _record_read(
            _audit_service(db, request),
            context,
            action="user.list",
            resource_type="user",
            details={
                "page": page,
                "limit": limit,
                "status": status_filter,
                "has_search": bool(normalized_search),
            },
        )
        items = [_user_public(user, account) for user, account in rows]
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/users/{user_id}")
def user_detail(
    user_id: uuid.UUID,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            user = _require_user(db, user_id)
            account = ExternalAccountRepository(db).get_for_user(user_id)
            now = utcnow()
            counts = {
                "active_sessions": int(
                    db.scalar(
                        select(func.count())
                        .select_from(WebSession)
                        .where(
                            WebSession.user_id == user_id,
                            WebSession.revoked_at.is_(None),
                            WebSession.idle_expires_at > now,
                            WebSession.absolute_expires_at > now,
                        )
                    )
                    or 0
                ),
                "conversations": int(
                    db.scalar(select(func.count()).select_from(Conversation).where(Conversation.owner_user_id == user_id))
                    or 0
                ),
                "messages": int(
                    db.scalar(select(func.count()).select_from(Message).where(Message.owner_user_id == user_id))
                    or 0
                ),
                "media": int(
                    db.scalar(select(func.count()).select_from(MediaObject).where(MediaObject.owner_user_id == user_id))
                    or 0
                ),
                "relationships": int(
                    db.scalar(select(func.count()).select_from(Relationship).where(Relationship.owner_user_id == user_id))
                    or 0
                ),
                "activities": int(
                    db.scalar(select(func.count()).select_from(ActivityEvent).where(ActivityEvent.owner_user_id == user_id))
                    or 0
                ),
                "raw_responses": int(
                    db.scalar(
                        select(func.count())
                        .select_from(RawUpstreamResponse)
                        .where(
                            RawUpstreamResponse.owner_user_id == user_id,
                            RawUpstreamResponse.expires_at > now,
                        )
                    )
                    or 0
                ),
            }
            item = {
                **_user_public(user, account),
                "profile": _public_json(user.profile),
                "device": _public_json(account.device_data) if account else {},
                "invite_code_id": str(user.invite_code_id) if user.invite_code_id else None,
                "counts": counts,
            }
            _record_read(
                _audit_service(db, request),
                context,
                action="user.view",
                target_user_id=user_id,
                resource_type="user",
                resource_id=str(user_id),
            )
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "user": item}


@router.post("/users/{user_id}/status")
def set_user_status(
    user_id: uuid.UUID,
    body: UserStatusBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            account = ExternalAccountRepository(db).get_for_user(user_id, for_update=True)
            # Login completion locks the external account before the user row;
            # keep the same order to avoid an account-disable/login deadlock.
            user = _require_user(db, user_id, for_update=True)
            old_status = user.status
            user.status = body.status
            user.disabled_at = utcnow() if body.status == "disabled" else None
            if account is not None:
                account.sync_enabled = body.status == "active"
            revoked = 0
            if body.status == "disabled":
                revoked = UserSessionService(
                    db,
                    _persistence(request).redis,
                    _settings(request),
                    _persistence(request).session_hmac_key,
                ).revoke_all_for_user(user_id, reason="administrator_disabled_user")
            audit = _audit_service(db, request)
            audit.record(
                actor_type="admin",
                action="user.status_changed",
                admin_user_id=context.admin_user_id,
                target_user_id=user_id,
                resource_type="user",
                resource_id=str(user_id),
                reason=body.reason,
                client_ip=context.client_ip,
                details={
                    "old_status": old_status,
                    "new_status": body.status,
                    "revoked_sessions": revoked,
                },
            )
            item = _user_public(user, account)
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "user": item, "revoked_sessions": revoked}


@router.post("/users/{user_id}/match-pool-online-list")
def set_user_match_pool_online_list(
    user_id: uuid.UUID,
    body: UserMatchPoolOnlineListBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            account = ExternalAccountRepository(db).get_for_user(user_id)
            user = _require_user(db, user_id, for_update=True)
            old_enabled = bool(user.match_pool_online_list_enabled)
            new_enabled = bool(body.enabled)
            changed = old_enabled != new_enabled
            user.match_pool_online_list_enabled = new_enabled
            _audit_service(db, request).record(
                actor_type="admin",
                action="user.match_pool_online_list_changed",
                admin_user_id=context.admin_user_id,
                target_user_id=user_id,
                resource_type="user_feature",
                resource_id="match_pool_online_list",
                reason=body.reason,
                client_ip=context.client_ip,
                details={
                    "old_enabled": old_enabled,
                    "new_enabled": new_enabled,
                    "changed": changed,
                    "scope": ["proactive_private_message"],
                    "online_list_always_available": True,
                },
            )
            item = _user_public(user, account)
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "user": item, "changed": changed}


@router.post("/users/{user_id}/nearby-custom-city")
def set_user_nearby_custom_city(
    user_id: uuid.UUID,
    body: UserNearbyCustomCityBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            account = ExternalAccountRepository(db).get_for_user(user_id)
            user = _require_user(db, user_id, for_update=True)
            old_enabled = bool(user.nearby_custom_city_enabled)
            new_enabled = bool(body.enabled)
            changed = old_enabled != new_enabled
            user.nearby_custom_city_enabled = new_enabled
            _audit_service(db, request).record(
                actor_type="admin",
                action="user.nearby_custom_city_changed",
                admin_user_id=context.admin_user_id,
                target_user_id=user_id,
                resource_type="user_feature",
                resource_id="nearby_custom_city",
                reason=body.reason,
                client_ip=context.client_ip,
                details={
                    "old_enabled": old_enabled,
                    "new_enabled": new_enabled,
                    "changed": changed,
                    "scope": ["nearby_custom_city_filter"],
                },
            )
            item = _user_public(user, account)
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "user": item, "changed": changed}


@router.post("/users/{user_id}/credentials")
def view_user_credentials(
    user_id: uuid.UUID,
    body: CredentialViewBody,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    failure: Exception | None = None
    result: dict[str, Any] | None = None
    with session_scope() as db:
        audit = _audit_service(db, request)
        try:
            _require_user(db, user_id)
            auth = _admin_auth(db, request, audit)
            credentials = CredentialAccessService(
                db,
                _persistence(request).cipher,
                auth,
                audit,
            ).view(
                raw_admin_sid=context.sid,
                client_ip=context.client_ip,
                target_user_id=user_id,
                reason=body.reason,
            )
            result = {
                "login_account": credentials.login_account,
                "phone": credentials.phone,
                "password": credentials.password,
                "token": credentials.token,
                "token_expires_at": _iso(credentials.token_expires_at),
            }
        except (ServiceError, ValueError) as exc:
            audit.record(
                actor_type="admin",
                action="credentials.view_failed",
                admin_user_id=context.admin_user_id,
                target_user_id=user_id,
                resource_type="external_account",
                reason=body.reason,
                client_ip=context.client_ip,
                details={"error_type": getattr(exc, "code", exc.__class__.__name__)},
            )
            failure = exc
    if failure is not None:
        _raise_service_error(failure)
    assert result is not None
    return {"ok": True, "credentials": result}


@router.get("/users/{user_id}/conversations")
def list_user_conversations(
    user_id: uuid.UUID,
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            rows = list(
                db.scalars(
                    select(Conversation)
                    .where(Conversation.owner_user_id == user_id)
                    .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc())
                    .offset(_offset(page, limit))
                    .limit(limit)
                )
            )
            total = int(
                db.scalar(select(func.count()).select_from(Conversation).where(Conversation.owner_user_id == user_id))
                or 0
            )
            _record_read(
                _audit_service(db, request),
                context,
                action="conversation.list",
                target_user_id=user_id,
                resource_type="conversation",
                details={"page": page, "limit": limit},
            )
            items = [_conversation_public(row) for row in rows]
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/users/{user_id}/messages")
def list_user_messages(
    user_id: uuid.UUID,
    request: Request,
    conversation_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            conditions: list[Any] = [Message.owner_user_id == user_id]
            if conversation_id is not None:
                conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.id == conversation_id,
                        Conversation.owner_user_id == user_id,
                    )
                )
                if conversation is None:
                    raise NotFoundError("conversation was not found")
                conditions.append(Message.conversation_id == conversation_id)
            rows = list(
                db.scalars(
                    select(Message)
                    .where(*conditions)
                    .order_by(Message.occurred_at.desc(), Message.id.desc())
                    .offset(_offset(page, limit))
                    .limit(limit)
                )
            )
            total = int(
                db.scalar(select(func.count()).select_from(Message).where(*conditions)) or 0
            )
            _record_read(
                _audit_service(db, request),
                context,
                action="message.list",
                target_user_id=user_id,
                resource_type="message",
                details={
                    "page": page,
                    "limit": limit,
                    "conversation_id": str(conversation_id) if conversation_id else None,
                },
            )
            items = [_message_public(row) for row in rows]
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/users/{user_id}/media")
def list_user_media(
    user_id: uuid.UUID,
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    state: Literal[
        "all", "pending", "uploading", "available", "failed", "delete_failed", "deleted"
    ] = "all",
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            conditions: list[Any] = [MediaObject.owner_user_id == user_id]
            if state == "deleted":
                conditions.append(MediaObject.deleted_at.is_not(None))
            elif state != "all":
                conditions.append(MediaObject.status == state)
            rows = list(
                db.scalars(
                    select(MediaObject)
                    .where(*conditions)
                    .order_by(MediaObject.created_at.desc())
                    .offset(_offset(page, limit))
                    .limit(limit)
                )
            )
            total = int(
                db.scalar(select(func.count()).select_from(MediaObject).where(*conditions)) or 0
            )
            _record_read(
                _audit_service(db, request),
                context,
                action="media.list",
                target_user_id=user_id,
                resource_type="media_object",
                details={"page": page, "limit": limit, "state": state},
            )
            items = [_media_public(row) for row in rows]
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.post("/users/{user_id}/media/{media_id}/access")
def access_user_media(
    user_id: uuid.UUID,
    media_id: uuid.UUID,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            media = MediaObjectRepository(db).get(user_id, media_id)
            if (
                media is None
                or media.status != "available"
                or media.deleted_at is not None
                or _aware(media.retention_expires_at) <= utcnow()
            ):
                raise NotFoundError("media object is not available")
            try:
                storage = _persistence(request).get_r2_storage()
                if media.r2_bucket != storage.bucket:
                    raise RuntimeError("configured media bucket mismatch")
                ttl = max(30, min(300, int(_settings(request).r2_presign_ttl_seconds)))
                signed_url = storage.presigned_get(media.r2_object_key, expires_seconds=ttl)
            except Exception as storage_error:
                raise HTTPException(
                    status_code=503,
                    detail="media storage is temporarily unavailable",
                ) from storage_error
            audit = _audit_service(db, request)
            _record_read(
                audit,
                context,
                action="media.access",
                target_user_id=user_id,
                resource_type="media_object",
                resource_id=str(media.id),
                details={"kind": media.kind, "size_bytes": int(media.size_bytes), "ttl": ttl},
            )
            item = _media_public(media)
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "url": signed_url, "expires_in": ttl, "media": item}


@router.get("/users/{user_id}/relationships")
def list_user_relationships(
    user_id: uuid.UUID,
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    kind: str = Query("", max_length=32),
    state: str = Query("", max_length=24),
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            conditions: list[Any] = [Relationship.owner_user_id == user_id]
            if kind.strip():
                conditions.append(Relationship.kind == kind.strip())
            if state.strip():
                conditions.append(Relationship.status == state.strip())
            rows = list(
                db.scalars(
                    select(Relationship)
                    .where(*conditions)
                    .order_by(Relationship.updated_at.desc())
                    .offset(_offset(page, limit))
                    .limit(limit)
                )
            )
            total = int(
                db.scalar(select(func.count()).select_from(Relationship).where(*conditions)) or 0
            )
            _record_read(
                _audit_service(db, request),
                context,
                action="relationship.list",
                target_user_id=user_id,
                resource_type="relationship",
                details={"page": page, "limit": limit, "kind": kind.strip(), "state": state.strip()},
            )
            items = [_relationship_public(row) for row in rows]
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/users/{user_id}/activities")
def list_user_activities(
    user_id: uuid.UUID,
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    event_type: str = Query("", max_length=64),
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            conditions: list[Any] = [ActivityEvent.owner_user_id == user_id]
            if event_type.strip():
                conditions.append(ActivityEvent.event_type == event_type.strip())
            rows = list(
                db.scalars(
                    select(ActivityEvent)
                    .where(*conditions)
                    .order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc())
                    .offset(_offset(page, limit))
                    .limit(limit)
                )
            )
            total = int(
                db.scalar(select(func.count()).select_from(ActivityEvent).where(*conditions)) or 0
            )
            _record_read(
                _audit_service(db, request),
                context,
                action="activity.list",
                target_user_id=user_id,
                resource_type="activity_event",
                details={"page": page, "limit": limit, "event_type": event_type.strip()},
            )
            items = [_activity_public(row) for row in rows]
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/users/{user_id}/raw-responses")
def list_raw_responses(
    user_id: uuid.UUID,
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(25, ge=1, le=50),
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    try:
        with session_scope() as db:
            _require_user(db, user_id)
            conditions = (
                RawUpstreamResponse.owner_user_id == user_id,
                RawUpstreamResponse.expires_at > utcnow(),
            )
            rows = list(
                db.scalars(
                    select(RawUpstreamResponse)
                    .where(*conditions)
                    .order_by(RawUpstreamResponse.received_at.desc())
                    .offset(_offset(page, limit))
                    .limit(limit)
                )
            )
            total = int(
                db.scalar(select(func.count()).select_from(RawUpstreamResponse).where(*conditions))
                or 0
            )
            _record_read(
                _audit_service(db, request),
                context,
                action="raw_response.list",
                target_user_id=user_id,
                resource_type="raw_upstream_response",
                details={"page": page, "limit": limit},
            )
            items = [_raw_public(row) for row in rows]
    except Exception as exc:
        if isinstance(exc, ServiceError):
            _raise_service_error(exc)
        raise
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/users/{user_id}/raw-responses/{response_id}")
def raw_response_detail(
    user_id: uuid.UUID,
    response_id: uuid.UUID,
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    failure: Exception | None = None
    item: dict[str, Any] | None = None
    with session_scope() as db:
        audit = _audit_service(db, request)
        try:
            _require_user(db, user_id)
            _admin_auth(db, request, audit).require_credentials_unlocked(
                raw_admin_sid=context.sid,
                client_ip=context.client_ip,
            )
            row = db.scalar(
                select(RawUpstreamResponse).where(
                    RawUpstreamResponse.id == response_id,
                    RawUpstreamResponse.owner_user_id == user_id,
                    RawUpstreamResponse.expires_at > utcnow(),
                )
            )
            if row is None:
                raise NotFoundError("raw upstream response was not found")
            payload = RawResponseService(
                db,
                _settings(request),
                _persistence(request).cipher,
            ).decrypt(row)
            _record_read(
                audit,
                context,
                action="raw_response.view",
                target_user_id=user_id,
                resource_type="raw_upstream_response",
                resource_id=str(row.id),
            )
            item = {
                **_raw_public(row),
                "payload": _public_json(
                    payload,
                    budget=[_PUBLIC_JSON_MAX_NODES, _PUBLIC_RAW_JSON_MAX_CHARS],
                ),
            }
        except (ServiceError, ValueError) as exc:
            audit.record(
                actor_type="admin",
                action="raw_response.view_failed",
                admin_user_id=context.admin_user_id,
                target_user_id=user_id,
                resource_type="raw_upstream_response",
                resource_id=str(response_id),
                client_ip=context.client_ip,
                details={"error_type": getattr(exc, "code", exc.__class__.__name__)},
            )
            failure = exc
    if failure is not None:
        _raise_service_error(failure)
    assert item is not None
    return {"ok": True, "raw_response": item}


@router.get("/audits")
def list_audits(
    request: Request,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    action: str = Query("", max_length=96),
    actor_type: str = Query("", max_length=24),
    target_user_id: uuid.UUID | None = None,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    with session_scope() as db:
        conditions: list[Any] = []
        if action.strip():
            conditions.append(AuditLog.action == action.strip())
        if actor_type.strip():
            conditions.append(AuditLog.actor_type == actor_type.strip())
        if target_user_id is not None:
            conditions.append(AuditLog.target_user_id == target_user_id)
        stmt = select(AuditLog)
        count_stmt = select(func.count()).select_from(AuditLog)
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        rows = list(
            db.scalars(
                stmt.order_by(AuditLog.created_at.desc())
                .offset(_offset(page, limit))
                .limit(limit)
            )
        )
        total = int(db.scalar(count_stmt) or 0)
        _record_read(
            _audit_service(db, request),
            context,
            action="audit.list",
            resource_type="audit_log",
            details={
                "page": page,
                "limit": limit,
                "action": action.strip(),
                "actor_type": actor_type.strip(),
                "target_user_id": str(target_user_id) if target_user_id else None,
            },
        )
        items = [_audit_public(row) for row in rows]
    return {"ok": True, "items": items, "total": total, "page": page, "limit": limit}


@router.get("/overview")
def admin_overview(
    request: Request,
    context: AdminContext = Depends(_admin_context),
) -> dict[str, Any]:
    now = utcnow()
    with session_scope() as db:
        quota = db.scalar(select(SystemStorageQuota).where(SystemStorageQuota.id == 1))

        def count(model: Any, *conditions: Any) -> int:
            stmt = select(func.count()).select_from(model)
            if conditions:
                stmt = stmt.where(*conditions)
            return int(db.scalar(stmt) or 0)

        overview = {
            "users": {
                "total": count(User),
                "active": count(User, User.status == "active"),
                "disabled": count(User, User.status == "disabled"),
            },
            "sessions": {
                "web_active": count(
                    WebSession,
                    WebSession.revoked_at.is_(None),
                    WebSession.idle_expires_at > now,
                    WebSession.absolute_expires_at > now,
                ),
                "admin_active": count(
                    AdminSession,
                    AdminSession.revoked_at.is_(None),
                    AdminSession.idle_expires_at > now,
                    AdminSession.absolute_expires_at > now,
                ),
            },
            "invites": {
                "total": count(InviteCode),
                "active": count(
                    InviteCode,
                    InviteCode.disabled_at.is_(None),
                    or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
                    InviteCode.use_count < InviteCode.max_uses,
                ),
            },
            "data": {
                "conversations": count(Conversation),
                "messages": count(Message),
                "media": count(MediaObject),
                "relationships": count(Relationship),
                "activities": count(ActivityEvent),
                "raw_responses_active": count(
                    RawUpstreamResponse, RawUpstreamResponse.expires_at > now
                ),
                "audit_logs_active": count(AuditLog, AuditLog.expires_at > now),
            },
            "storage": {
                "used_bytes": int(quota.used_bytes) if quota else 0,
                "quota_bytes": (
                    int(quota.quota_bytes)
                    if quota
                    else int(_settings(request).system_media_quota_bytes)
                ),
            },
            "generated_at": _iso(now),
        }
        _record_read(
            _audit_service(db, request),
            context,
            action="admin.overview_view",
            resource_type="system_overview",
        )
    return {"ok": True, "overview": overview}
