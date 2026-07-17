"""Production persistence bridge between the legacy BFF and PostgreSQL/Redis."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from redis import Redis
from rq import Queue
from rq.exceptions import InvalidJobOperation
from sqlalchemy import text

from bbw_prod.config import Settings
from bbw_prod.crypto import CredentialCipher, normalize_phone
from bbw_prod.db import session_scope
from bbw_prod.models import ExternalAccount
from bbw_prod.repositories import ExternalAccountRepository, UserRepository
from bbw_prod.services import (
    InviteInvalid,
    LoginAccountService,
    LoginPrecheck,
    PermissionDenied,
    RawResponseService,
    UserSessionService,
)
from bbw_protocol.adapters import NativeBundle
from bbw_protocol.app import BeibeiwuApp
from bbw_protocol.client import ApiResult
from bbw_protocol.session import Session
from bbw_web.store import WebUser
from bbw_web.turnstile import TurnstileVerifier


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str


def _aware_timestamp(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _safe_json(raw: str) -> Any:
    value = str(raw or "")
    if len(value.encode("utf-8", errors="ignore")) > 1024 * 1024:
        value = value[:1024 * 1024]
    try:
        return json.loads(value)
    except Exception:
        return value


def _redact_request(value: Any) -> Any:
    blocked = {
        "password",
        "userpassword",
        "passwd",
        "pwd",
        "token",
        "accesstoken",
        "refreshtoken",
        "authorization",
        "useraccount",
        "loginaccount",
        "phone",
        "phonenumber",
        "mobile",
        "usersig",
        "invite_code",
        "invitecode",
        "code",
        "cert_no",
        "certno",
        "cert_name",
        "certname",
        "meta_info",
        "metainfo",
        "alipay",
        "alilogonid",
        "prepay",
        "order_params",
        "orderspec",
    }
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if (
                    str(key).casefold() in blocked
                    or re.sub(r"[^a-z0-9]", "", str(key).casefold()) in blocked
                )
                else _redact_request(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_request(item) for item in value[:200]]
    if isinstance(value, str):
        return value[:20_000]
    return value


_PROFILE_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "userpassword",
    "token",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "phone",
    "phonenumber",
    "mobile",
    "useraccount",
    "loginaccount",
    "usersig",
    "secret",
    "secretkey",
    "cookie",
    "sessionid",
    "certno",
    "certname",
    "idcard",
    "identitynumber",
}


def _sanitize_profile(value: Any) -> Any:
    """Keep useful profile fields while preventing plaintext credential copies."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:500]:
            key_text = str(key)[:160]
            normalized = re.sub(r"[^a-z0-9]", "", key_text.casefold())
            result[key_text] = (
                "[REDACTED]"
                if normalized in _PROFILE_SECRET_KEYS
                else _sanitize_profile(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_profile(item) for item in list(value)[:500]]
    if isinstance(value, str):
        return value[:20_000]
    return value


class RuntimePersistence:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        self.cipher = CredentialCipher.from_settings(settings)
        self.phone_hmac_key = settings.load_phone_hmac_key()
        self.session_hmac_key = settings.load_session_hmac_key()
        self.turnstile = TurnstileVerifier(settings)
        self.default_queue = Queue("default", connection=self.redis)
        self.media_queue = Queue("media", connection=self.redis)
        self.sync_queue = Queue("sync", connection=self.redis)
        self._r2_storage: Any = None

    def startup(self) -> None:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
        self.redis.ping()

    def close(self) -> None:
        try:
            self.redis.close()
        except Exception:
            pass

    def health(self) -> dict[str, Any]:
        database_ok = False
        redis_ok = False
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            pass
        try:
            redis_ok = bool(self.redis.ping())
        except Exception:
            pass
        return {
            "ok": bool(database_ok and redis_ok),
            "service": "bbw-web",
            "database": database_ok,
            "redis": redis_ok,
        }

    def _limit_key(self, key: str, window_seconds: int) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        bucket = int(time.time() // max(1, window_seconds))
        return f"{self.settings.redis_prefix}:limit:{digest}:{bucket}"

    def rate_limit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        redis_key = self._limit_key(key, window_seconds)
        pipe = self.redis.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, int(window_seconds) + 2)
        count, _ = pipe.execute()
        return int(count) <= int(limit)

    def login_security_state(self, *, phone: str, client_ip: str) -> dict[str, Any]:
        """Return only the browser-facing Turnstile state, never failure counts."""
        failures = 0
        if phone:
            failures = self._login_failure_count(phone, client_ip)
        return {
            "enabled": bool(self.turnstile.enabled),
            "required": bool(self.turnstile.enabled and failures >= 2),
            "site_key": self.turnstile.site_key if self.turnstile.enabled else "",
        }

    def get_r2_storage(self) -> Any:
        if self._r2_storage is None:
            from bbw_web.r2 import R2Storage

            self._r2_storage = R2Storage(self.settings)
        return self._r2_storage

    def _failure_key(self, phone: str, client_ip: str) -> str:
        digest = hashlib.sha256(f"{phone.strip()}\n{client_ip}".encode("utf-8")).hexdigest()
        return f"{self.settings.redis_prefix}:login-fail:{digest}"

    def _failure_keys(self, phone: str, client_ip: str) -> tuple[str, str, str]:
        try:
            normalized_phone = normalize_phone(phone)
        except ValueError:
            normalized_phone = phone.strip()
        phone_digest = hashlib.sha256(normalized_phone.encode("utf-8")).hexdigest()
        ip_digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
        return (
            self._failure_key(phone, client_ip),
            f"{self.settings.redis_prefix}:login-fail-phone:{phone_digest}",
            f"{self.settings.redis_prefix}:login-fail-ip:{ip_digest}",
        )

    def _login_failure_count(self, phone: str, client_ip: str) -> int:
        values = self.redis.mget(self._failure_keys(phone, client_ip))
        return max((int(value or 0) for value in values), default=0)

    def precheck_login(
        self,
        *,
        phone: str,
        invite_code: str,
        client_ip: str,
        turnstile_token: str = "",
    ) -> LoginPrecheck:
        if not phone:
            raise PermissionError("请输入手机号")
        failures = self._login_failure_count(phone, client_ip)
        if failures >= 5:
            raise RuntimeError("登录失败次数过多，请在 15 分钟后重试")
        if failures >= 2 and self.turnstile.enabled:
            if not self.turnstile.verify(turnstile_token, remote_ip=client_ip):
                raise PermissionError("需要完成人机验证后才能继续登录")
        try:
            with session_scope() as db:
                service = LoginAccountService(
                    db, self.settings, self.cipher, self.phone_hmac_key
                )
                return service.precheck(phone=phone, invite_code=invite_code or None)
        except (InviteInvalid, PermissionDenied) as exc:
            raise PermissionError(str(exc)) from exc

    def record_login_failure(self, *, phone: str, client_ip: str) -> None:
        if not phone:
            return
        pipe = self.redis.pipeline()
        for key in self._failure_keys(phone, client_ip):
            pipe.incr(key)
            pipe.expire(key, 15 * 60)
        pipe.execute()

    def _clear_login_failures(self, *, phone: str, client_ip: str) -> None:
        if phone:
            combo, phone_key, _ip_key = self._failure_keys(phone, client_ip)
            self.redis.delete(combo, phone_key)

    def precheck_invite(self, *, phone: str, invite_code: str) -> LoginPrecheck:
        try:
            with session_scope() as db:
                service = LoginAccountService(
                    db, self.settings, self.cipher, self.phone_hmac_key
                )
                return service.precheck(phone=phone, invite_code=invite_code or None)
        except (InviteInvalid, PermissionDenied) as exc:
            raise PermissionError(str(exc)) from exc

    @staticmethod
    def _device_data(web_user: WebUser) -> dict[str, Any]:
        session = web_user.app.session
        return {
            **session.device_dict(),
            "user_role": session.user_role,
            "rp_verify_time": session.rp_verify_time,
            "vip": session.vip,
            "svip": session.svip,
            "portrait": session.portrait,
        }

    def complete_login(
        self,
        *,
        web_user: WebUser,
        phone: str,
        invite_code: str,
        password: str,
        login_context: LoginPrecheck | None,
        old_sid: str | None,
        client_ip: str,
        user_agent: str,
    ) -> UserIdentity:
        upstream = web_user.app.session
        if not upstream.logged_in:
            raise RuntimeError("upstream login did not produce a usable session")
        with session_scope() as db:
            account_service = LoginAccountService(
                db, self.settings, self.cipher, self.phone_hmac_key
            )
            completion = account_service.complete_login(
                phone=phone,
                invite_code=invite_code or None,
                upstream_uid=str(upstream.uid),
                login_account=phone,
                password=password,
                token=str(upstream.token or "") or None,
                display_name=str(upstream.nickname or "") or None,
                profile=_sanitize_profile(dict(upstream.raw_user or {})),
                device_data=self._device_data(web_user),
            )
            sessions = UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            )
            sessions.issue_with_sid(
                raw_sid=web_user.web_sid,
                user_id=completion.user.id,
                external_account_id=completion.external_account.id,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        if old_sid and old_sid != web_user.web_sid:
            self.revoke_session(old_sid, reason="rotated")
        self._clear_login_failures(phone=phone, client_ip=client_ip)
        identity = UserIdentity(
            completion.user.id,
            completion.external_account.id,
            str(completion.external_account.upstream_uid or upstream.uid),
        )
        self._attach_runtime(web_user, identity)
        return identity

    def _decrypt_account(
        self, account: ExternalAccount, *, include_password: bool = True
    ) -> tuple[str, str, str]:
        context = lambda field: f"external-account:{account.id}:{field}"  # noqa: E731
        login = self.cipher.decrypt_text(
            account.login_account_encrypted,
            purpose="external-account.login",
            context=context("login"),
        )
        password = (
            self.cipher.decrypt_text(
                account.password_encrypted,
                purpose="external-account.password",
                context=context("password"),
            )
            if include_password and account.password_encrypted
            else ""
        )
        token = (
            self.cipher.decrypt_text(
                account.token_encrypted,
                purpose="external-account.token",
                context=context("token"),
            )
            if account.token_encrypted
            else "0"
        )
        return login, password, token

    def restore_web_user(self, sid: str) -> Optional[WebUser]:
        if not sid:
            return None
        with session_scope() as db:
            sessions = UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            )
            state = sessions.recover(sid)
            if state is None:
                return None
            user = UserRepository(db).get(state.user_id)
            account = ExternalAccountRepository(db).get_for_user(state.user_id)
            if (
                user is None
                or user.status != "active"
                or account is None
                or account.id != state.external_account_id
            ):
                sessions.revoke(sid, reason="account_unavailable")
                return None
            login, _password, token = self._decrypt_account(
                account, include_password=False
            )
            data = dict(account.device_data or {})
            protocol_session = Session(
                uid=str(account.upstream_uid or "0"),
                token=token or "0",
                phone=login,
                password="",
                nickname=str(user.display_name or ""),
                user_role=str(data.get("user_role") or ""),
                rp_verify_time=str(data.get("rp_verify_time") or "0"),
                vip=str(data.get("vip") or "0"),
                svip=str(data.get("svip") or "0"),
                portrait=str(data.get("portrait") or ""),
                user_sign=str(data.get("user_sign") or ""),
                login_id=str(data.get("login_id") or ""),
                raw_user=dict(user.profile or {}),
            )
            protocol_session.apply_device(data)
            app = BeibeiwuApp(protocol_session)
            web_user = WebUser(
                web_sid=sid,
                app=app,
                native=NativeBundle(app),
                label=str(user.display_name or account.upstream_uid or ""),
                created_at=state.created_at.timestamp(),
                last_seen=state.last_seen_at.timestamp(),
                persist_sessions=False,
            )
            identity = UserIdentity(user.id, account.id, str(account.upstream_uid or ""))
        self._attach_runtime(web_user, identity)
        return web_user

    def _attach_runtime(self, web_user: WebUser, identity: UserIdentity) -> None:
        web_user.internal_user_id = str(identity.user_id)
        web_user.external_account_id = str(identity.external_account_id)
        web_user.app.client.response_hook = lambda meta, result: self.capture_upstream_response(
            identity=identity, request_meta=meta, result=result
        )
        web_user.app.client.reauth_callback = lambda: self._reauthenticate(
            identity=identity, app=web_user.app
        )

    def _reauthenticate(self, *, identity: UserIdentity, app: BeibeiwuApp) -> bool:
        with session_scope() as db:
            account = ExternalAccountRepository(db).get_for_user(identity.user_id)
            if account is None or account.id != identity.external_account_id:
                return False
            login, password, _token = self._decrypt_account(account)
        if not login or not password:
            return False
        result = app.auth.login_password(login, password)
        if not result.ok or not app.session.logged_in:
            app.session.password = ""
            return False
        app.session.password = ""
        with session_scope() as db:
            service = LoginAccountService(
                db, self.settings, self.cipher, self.phone_hmac_key
            )
            service.complete_login(
                phone=login,
                invite_code=None,
                upstream_uid=str(app.session.uid),
                login_account=login,
                password=password,
                token=str(app.session.token or "") or None,
                display_name=str(app.session.nickname or "") or None,
                profile=_sanitize_profile(dict(app.session.raw_user or {})),
                device_data={**app.session.device_dict()},
            )
        return True

    def touch_session(self, sid: str, *, client_ip: str = "") -> Optional[UserIdentity]:
        if not sid:
            return None
        with session_scope() as db:
            state = UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).touch(sid)
            if state is None:
                return None
            account = ExternalAccountRepository(db).get_for_user(state.user_id)
            return UserIdentity(
                state.user_id,
                state.external_account_id,
                str(account.upstream_uid if account else ""),
            )

    def revoke_session(self, sid: str, *, reason: str = "logout") -> bool:
        if not sid:
            return False
        with session_scope() as db:
            return UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).revoke(sid, reason=reason)

    def require_identity(self, sid: str) -> Optional[UserIdentity]:
        if not sid:
            return None
        with session_scope() as db:
            state = UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).touch(sid)
            if state is None:
                return None
            user = UserRepository(db).get(state.user_id)
            account = ExternalAccountRepository(db).get_for_user(state.user_id)
            if user is None or user.status != "active" or account is None:
                return None
            return UserIdentity(user.id, account.id, str(account.upstream_uid or ""))

    def enqueue_message_archive(
        self,
        *,
        identity: UserIdentity,
        payload: dict[str, Any],
        client_ip: str,
    ) -> bool:
        idempotency_key = str(payload.get("idempotency_key") or "")
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        job_id = f"archive-message:{identity.user_id}:{digest}"
        try:
            self.default_queue.enqueue(
                "bbw_web.jobs.archive_message_job",
                str(identity.user_id),
                str(identity.external_account_id),
                payload,
                job_id=job_id,
                job_timeout=180,
                result_ttl=600,
                failure_ttl=7 * 86400,
            )
            return True
        except InvalidJobOperation:
            return False
        except Exception as exc:
            # RQ raises different duplicate-job classes across supported releases.
            if "already exists" in str(exc).lower():
                return False
            raise

    def capture_upstream_response(
        self,
        *,
        identity: UserIdentity,
        request_meta: dict[str, Any],
        result: ApiResult,
    ) -> None:
        payload = {
            "request": _redact_request(request_meta),
            "response": _safe_json(result.raw),
            "code": result.code,
            "message": result.message,
            "kind": result.kind,
        }
        with session_scope() as db:
            RawResponseService(db, self.settings, self.cipher).store(
                owner_user_id=identity.user_id,
                endpoint=str(request_meta.get("url") or "")[:512],
                payload=payload,
                http_status=int(result.status or 0),
            )

    def capture_product_response(
        self,
        *,
        sid: str | None,
        identity: UserIdentity | None = None,
        method: str,
        path: str,
        query: dict[str, Any],
        request_data: dict[str, Any],
        response_data: dict[str, Any],
        status: int,
        client_ip: str,
    ) -> None:
        identity = identity or self.require_identity(str(sid or ""))
        if identity is None:
            return
        if path in {"/api/im/messages", "/api/im/conversations"} and response_data:
            digest = hashlib.sha256(
                json.dumps(response_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            try:
                self.default_queue.enqueue(
                    "bbw_web.jobs.ingest_history_response",
                    str(identity.user_id),
                    str(identity.external_account_id),
                    path,
                    query,
                    response_data,
                    job_id=f"history-response:{identity.user_id}:{digest}",
                    result_ttl=600,
                    failure_ttl=86400,
                )
            except Exception:
                pass
        social_snapshot_paths = {
            "/api/social/follows",
            "/api/social/fans",
            "/api/social/follow-list",
            "/api/social/friend-apply",
            "/api/social/friends",
            "/api/social/visitors",
            "/api/social/blacklist",
            "/api/social/blacklist-me",
        }
        if method.upper() == "GET" and path in social_snapshot_paths and response_data:
            snapshot_digest = hashlib.sha256(
                json.dumps(
                    {"path": path, "query": query, "response": response_data},
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            try:
                self.default_queue.enqueue(
                    "bbw_web.jobs.ingest_social_snapshot",
                    str(identity.user_id),
                    str(identity.external_account_id),
                    path,
                    query,
                    response_data,
                    job_id=f"social-snapshot:{identity.user_id}:{snapshot_digest}",
                    result_ttl=600,
                    failure_ttl=86400,
                )
            except Exception:
                pass
        method_upper = method.upper()
        excluded_post_paths = {
            "/api/auth/login",
            "/api/auth/sms-login",
            "/api/auth/sms-send",
            "/api/heartbeat/once",
            "/api/frontback",
            "/api/online",
        }
        meaningful_get_paths = {
            "/api/profile/user",
            "/api/social/follows",
            "/api/social/fans",
            "/api/social/follow-list",
            "/api/social/friends",
            "/api/social/friend-apply",
            "/api/social/visitors",
            "/api/social/blacklist",
            "/api/social/blacklist-me",
            "/api/moments/posts",
            "/api/moments/comments",
            "/api/im/conversations",
            "/api/im/messages",
            "/api/media/access",
        }
        should_record_event = (
            method_upper == "POST"
            and path.startswith("/api/")
            and path not in excluded_post_paths
        ) or (method_upper == "GET" and path in meaningful_get_paths)
        if should_record_event:
            response_for_event: Any
            if method_upper == "GET":
                response_for_event = {
                    "ok": bool(response_data.get("ok")),
                    "code": response_data.get("code"),
                    "message": str(response_data.get("message") or "")[:500],
                }
            else:
                response_for_event = _redact_request(response_data)
            event_payload = {
                "method": method_upper,
                "path": path,
                "request": _redact_request(request_data),
                "query": _redact_request(query),
                "response": response_for_event,
                "status": int(status),
                "client_ip_hash": hashlib.sha256(client_ip.encode("utf-8")).hexdigest(),
            }
            digest = hashlib.sha256(
                json.dumps(event_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            event_payload["idempotency_key"] = digest
            try:
                self.default_queue.enqueue(
                    "bbw_web.jobs.record_product_event",
                    str(identity.user_id),
                    event_payload,
                    job_id=f"product-event:{identity.user_id}:{digest}",
                    result_ttl=600,
                    failure_ttl=86400,
                )
            except Exception:
                pass
