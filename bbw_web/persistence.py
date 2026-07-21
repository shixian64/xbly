"""Production persistence bridge between the legacy BFF and PostgreSQL/Redis."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from redis import Redis
from rq import Queue
from rq.exceptions import InvalidJobOperation
from sqlalchemy import and_, or_, select, text, union_all

from bbw_prod.config import Settings
from bbw_prod.crypto import CredentialCipher, normalize_phone
from bbw_prod.db import session_scope
from bbw_prod.models import Conversation, ExternalAccount, Relationship, utcnow
from bbw_prod.repositories import (
    ConversationRepository,
    ExternalAccountRepository,
    MessageRepository,
    RelationshipRepository,
    UserRepository,
)
from bbw_prod.security import SessionTokenManager, keyed_identifier_hash
from bbw_prod.services import (
    ConflictError,
    InviteInvalid,
    InviteService,
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
from bbw_web.match_history import load_match_history, record_match_history_response
from bbw_web.store import WebUser
from bbw_web.turnstile import TurnstileVerifier


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    match_pool_online_list_enabled: bool = False
    nearby_custom_city_enabled: bool = False


@dataclass(frozen=True, slots=True)
class PendingLogin:
    phone: str = field(repr=False)
    mode: str
    upstream_uid: str
    password: str = field(repr=False)


class PendingLoginExpired(PermissionError):
    pass


class PendingLoginRejected(PermissionError):
    pass


class PendingLoginConflict(RuntimeError):
    pass


MESSAGE_POLICY_PROVIDER = "web-policy"
MESSAGE_POLICY_MATCH_KIND = "match"
MESSAGE_POLICY_CONVERSATION_KIND = "message_peer"
SOCIAL_RELATIONSHIP_PROVIDER = "beibeiwu"
SOCIAL_FRIEND_KIND = "friend"


def _message_peer_uid(value: Any) -> str:
    peer = str(value or "").strip()
    if (
        not peer
        or peer.lower() in {"0", "none", "null"}
        or len(peer) > 128
        or any(ord(char) < 33 for char in peer)
    ):
        return ""
    return peer


def _response_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("items", "list", "users"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    nested = payload.get("data")
    if nested is not payload:
        return _response_items(nested)
    return []


def _item_peer_uid(item: Mapping[str, Any]) -> str:
    for key in (
        "peer_id",
        "conversation_user",
        "user_id",
        "uid",
        "id",
        "yourid",
    ):
        peer = _message_peer_uid(item.get(key))
        if peer:
            return peer
    nested = item.get("user")
    return _item_peer_uid(nested) if isinstance(nested, Mapping) else ""


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


def _conversation_message_preview(message: Any) -> str:
    if message is None:
        return ""
    body = str(message.body or "").strip()
    if body:
        return body[:500]
    return {
        "image": "图片",
        "audio": "语音",
        "video": "视频",
        "file": "文件",
        "location": "位置",
        "face": "表情",
        "custom": "消息",
    }.get(str(message.message_type or "").lower(), "消息")


class RuntimePersistence:
    PENDING_LOGIN_SECONDS = 5 * 60
    WEB_PRESENCE_TTL_SECONDS = 120
    PRESENCE_REST_FAILURE_TTL_SECONDS = 10 * 60
    MOMENT_VIDEO_GRANT_SECONDS = 2 * 60 * 60

    def __init__(self, settings: Settings):
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        self.cipher = CredentialCipher.from_settings(settings)
        self.phone_hmac_key = settings.load_phone_hmac_key()
        self.session_hmac_key = settings.load_session_hmac_key()
        self.turnstile = TurnstileVerifier(settings)
        self.default_queue = Queue("default", connection=self.redis)
        self.media_queue = Queue("media", connection=self.redis)
        self.im_ingest_queue = Queue("im-ingest", connection=self.redis)
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

    def _web_presence_key(self, upstream_uid: str) -> str:
        digest = hashlib.sha256(str(upstream_uid or "").encode("utf-8")).hexdigest()
        return f"{self.settings.redis_prefix}:presence:web:{digest}"

    def set_web_presence(self, upstream_uid: str, *, active: bool) -> None:
        uid = str(upstream_uid or "").strip()
        if not uid:
            return
        key = self._web_presence_key(uid)
        if active:
            self.redis.set(key, b"1", ex=self.WEB_PRESENCE_TTL_SECONDS)
        else:
            self.redis.delete(key)

    def read_web_presence(self, upstream_uids: list[str]) -> set[str]:
        uids = [str(uid or "").strip() for uid in upstream_uids]
        keys = [self._web_presence_key(uid) for uid in uids if uid]
        if not keys:
            return set()
        values = self.redis.mget(keys)
        return {
            uid
            for uid, value in zip((uid for uid in uids if uid), values)
            if value is not None
        }

    def _presence_rest_failure_key(self) -> str:
        return f"{self.settings.redis_prefix}:presence:tim-rest-unavailable"

    def mark_presence_rest_unavailable(self, error_code: int = 0) -> None:
        ttl = self.PRESENCE_REST_FAILURE_TTL_SECONDS if int(error_code or 0) == 70009 else 60
        self.redis.set(
            self._presence_rest_failure_key(),
            str(int(error_code or 0)).encode("ascii"),
            ex=ttl,
        )

    def presence_rest_retry_after(self) -> int:
        ttl = int(self.redis.ttl(self._presence_rest_failure_key()) or 0)
        return max(0, ttl)

    def _claim_response_digest(
        self,
        namespace: str,
        identity: UserIdentity,
        digest: str,
        *,
        ttl_seconds: int = 600,
    ) -> bool:
        key = self._response_digest_key(namespace, identity, digest)
        return bool(self.redis.set(key, b"1", nx=True, ex=max(1, int(ttl_seconds))))

    def _response_digest_key(
        self, namespace: str, identity: UserIdentity, digest: str
    ) -> str:
        return (
            f"{self.settings.redis_prefix}:dedupe:{namespace}:"
            f"{identity.user_id}:{digest}"
        )

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

    def _moment_video_feed_grant_key(
        self, identity: UserIdentity, post_id: str, asset_id: str
    ) -> str:
        post_digest = hashlib.sha256(str(post_id).encode("utf-8")).hexdigest()
        return (
            f"{self.settings.redis_prefix}:moment-video-feed-grant:"
            f"{identity.user_id}:{post_digest}:{asset_id}"
        )

    def _moment_video_access_key(self, identity: UserIdentity, asset_id: str) -> str:
        return (
            f"{self.settings.redis_prefix}:moment-video-access:"
            f"{identity.user_id}:{asset_id}"
        )

    def remember_moment_video_grants(
        self, identity: UserIdentity, response_data: dict[str, Any]
    ) -> int:
        """Remember only videos that appeared in this user's successful feed response."""
        from bbw_web.moment_video import (
            MomentVideoError,
            asset_id_for_url,
            canonical_source_identity,
        )

        raw_items = response_data.get("items") or response_data.get("list") or []
        if not isinstance(raw_items, list):
            return 0
        grants: list[tuple[str, str]] = []
        for item in raw_items[:200]:
            if not isinstance(item, dict):
                continue
            post_id = str(item.get("id") or item.get("post_id") or "").strip()
            source = item.get("video") or item.get("postvideo")
            if not post_id or not source:
                continue
            try:
                canonical = canonical_source_identity(source)
            except MomentVideoError:
                continue
            grants.append((post_id, asset_id_for_url(canonical)))
        if not grants:
            return 0
        pipe = self.redis.pipeline()
        for post_id, asset_id in grants:
            pipe.set(
                self._moment_video_feed_grant_key(identity, post_id, asset_id),
                b"1",
                ex=self.MOMENT_VIDEO_GRANT_SECONDS,
            )
        pipe.execute()
        return len(grants)

    def authorize_moment_video(
        self, identity: UserIdentity, *, post_id: str, asset_id: str
    ) -> bool:
        feed_key = self._moment_video_feed_grant_key(identity, post_id, asset_id)
        if not self.redis.exists(feed_key):
            return False
        self.redis.set(
            self._moment_video_access_key(identity, asset_id),
            b"1",
            ex=self.MOMENT_VIDEO_GRANT_SECONDS,
        )
        return True

    def can_access_moment_video(self, identity: UserIdentity, asset_id: str) -> bool:
        key = self._moment_video_access_key(identity, asset_id)
        return bool(self.redis.exists(key))

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

    def precheck_account(self, *, phone: str) -> LoginPrecheck:
        if not phone:
            raise PermissionError("请输入手机号")
        try:
            with session_scope() as db:
                service = LoginAccountService(
                    db, self.settings, self.cipher, self.phone_hmac_key
                )
                return service.precheck_credentials(phone=phone)
        except PermissionDenied as exc:
            raise PermissionError("账号不可用") from exc

    def precheck_login_credentials(
        self,
        *,
        phone: str,
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
        return self.precheck_account(phone=phone)

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

    def validate_invite_code(self, invite_code: str) -> None:
        if not invite_code:
            raise PermissionError("请输入有效邀请码")
        try:
            with session_scope() as db:
                InviteService(db, self.settings).validate(invite_code)
        except InviteInvalid as exc:
            raise PermissionError("邀请码无效或已不可用") from exc

    def _pending_login_key(self, raw_sid: str) -> str:
        return SessionTokenManager.redis_key(
            self.settings.redis_prefix, "pending-login", raw_sid
        )

    def _pending_login_context(self, raw_sid: str) -> str:
        return f"pending-login:{SessionTokenManager.hash_sid(raw_sid)}"

    def _pending_client_hash(self, *, client_ip: str, user_agent: str) -> str:
        return keyed_identifier_hash(
            f"{client_ip}\n{user_agent[:512]}",
            self.session_hmac_key,
            purpose="pending-login-client",
        )

    def begin_pending_login(
        self,
        *,
        raw_sid: str,
        phone: str,
        password: str,
        mode: str,
        upstream_uid: str,
        client_ip: str,
        user_agent: str,
    ) -> int:
        if not raw_sid or not upstream_uid:
            raise ValueError("pending login requires an authenticated upstream session")
        payload = {
            "phone": normalize_phone(phone),
            "password": password,
            "mode": "sms" if mode == "sms" else "password",
            "upstream_uid": str(upstream_uid),
            "client_hash": self._pending_client_hash(
                client_ip=client_ip, user_agent=user_agent
            ),
            "created_at": time.time(),
        }
        encrypted = self.cipher.encrypt_json(
            payload,
            purpose="web-login.pending",
            context=self._pending_login_context(raw_sid),
        )
        self.redis.set(
            self._pending_login_key(raw_sid),
            json.dumps(encrypted, ensure_ascii=True, separators=(",", ":")),
            ex=self.PENDING_LOGIN_SECONDS,
        )
        self._clear_login_failures(phone=phone, client_ip=client_ip)
        return self.PENDING_LOGIN_SECONDS

    def _decode_pending_login(
        self,
        raw_sid: str,
        raw_payload: Any,
        *,
        client_ip: str,
        user_agent: str,
    ) -> PendingLogin:
        if raw_payload is None:
            raise PendingLoginExpired("登录验证已失效，请重新登录")
        try:
            if isinstance(raw_payload, bytes):
                raw_payload = raw_payload.decode("utf-8")
            encrypted = json.loads(str(raw_payload))
            payload = self.cipher.decrypt_json(
                encrypted,
                purpose="web-login.pending",
                context=self._pending_login_context(raw_sid),
            )
            expected_client = self._pending_client_hash(
                client_ip=client_ip, user_agent=user_agent
            )
            if not hmac.compare_digest(
                str(payload.get("client_hash") or ""), expected_client
            ):
                raise PendingLoginExpired("登录环境已变化，请重新登录")
            phone = normalize_phone(str(payload.get("phone") or ""))
            upstream_uid = str(payload.get("upstream_uid") or "").strip()
            if not upstream_uid:
                raise ValueError("pending login has no upstream identity")
            return PendingLogin(
                phone=phone,
                password=str(payload.get("password") or ""),
                mode="sms" if payload.get("mode") == "sms" else "password",
                upstream_uid=upstream_uid,
            )
        except PendingLoginExpired:
            raise
        except Exception as exc:
            raise PendingLoginExpired("登录验证已失效，请重新登录") from exc

    def peek_pending_login(
        self,
        raw_sid: str,
        *,
        client_ip: str,
        user_agent: str,
    ) -> PendingLogin:
        return self._decode_pending_login(
            raw_sid,
            self.redis.get(self._pending_login_key(raw_sid)),
            client_ip=client_ip,
            user_agent=user_agent,
        )

    def claim_pending_login(
        self,
        raw_sid: str,
        *,
        client_ip: str,
        user_agent: str,
    ) -> PendingLogin:
        return self._decode_pending_login(
            raw_sid,
            self.redis.getdel(self._pending_login_key(raw_sid)),
            client_ip=client_ip,
            user_agent=user_agent,
        )

    def cancel_pending_login(self, raw_sid: str) -> bool:
        if not raw_sid:
            return False
        return bool(self.redis.delete(self._pending_login_key(raw_sid)))

    def finish_pending_login(
        self,
        *,
        raw_sid: str,
        web_user: WebUser,
        invite_code: str,
        old_sid: str | None,
        client_ip: str,
        user_agent: str,
    ) -> UserIdentity:
        pending = self.peek_pending_login(
            raw_sid, client_ip=client_ip, user_agent=user_agent
        )
        if str(web_user.app.session.uid or "") != pending.upstream_uid:
            self.cancel_pending_login(raw_sid)
            raise PendingLoginExpired("登录验证已失效，请重新登录")
        self.validate_invite_code(invite_code)
        pending = self.claim_pending_login(
            raw_sid, client_ip=client_ip, user_agent=user_agent
        )
        if str(web_user.app.session.uid or "") != pending.upstream_uid:
            raise PendingLoginExpired("登录验证已失效，请重新登录")
        try:
            return self.complete_login(
                web_user=web_user,
                phone=pending.phone,
                invite_code=invite_code,
                password=pending.password,
                login_context=None,
                old_sid=old_sid,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        except InviteInvalid as exc:
            raise PermissionError("邀请码无效或已不可用") from exc
        except PermissionDenied as exc:
            raise PendingLoginRejected("账号不可用，请重新登录") from exc
        except ConflictError as exc:
            raise PendingLoginConflict("账号绑定状态已变化，请重新登录") from exc

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
        invite_code: str | None,
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
                require_invite=(
                    login_context.requires_invite
                    if login_context is not None
                    else True
                ),
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
            user_id=completion.user.id,
            external_account_id=completion.external_account.id,
            upstream_uid=str(completion.external_account.upstream_uid or upstream.uid),
            match_pool_online_list_enabled=bool(
                completion.user.match_pool_online_list_enabled
            ),
            nearby_custom_city_enabled=bool(
                completion.user.nearby_custom_city_enabled
            ),
        )
        web_user.clear_pending()
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
            identity = UserIdentity(
                user_id=user.id,
                external_account_id=account.id,
                upstream_uid=str(account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(user.nearby_custom_city_enabled),
            )
        self._attach_runtime(web_user, identity)
        return web_user

    def _attach_runtime(self, web_user: WebUser, identity: UserIdentity) -> None:
        web_user.internal_user_id = str(identity.user_id)
        web_user.external_account_id = str(identity.external_account_id)
        web_user.match_pool_online_list_enabled = bool(
            identity.match_pool_online_list_enabled
        )
        web_user.nearby_custom_city_enabled = bool(identity.nearby_custom_city_enabled)
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
                require_invite=False,
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
            user = UserRepository(db).get(state.user_id)
            account = ExternalAccountRepository(db).get_for_user(state.user_id)
            if (
                user is None
                or user.status != "active"
                or account is None
                or account.id != state.external_account_id
            ):
                return None
            return UserIdentity(
                user_id=state.user_id,
                external_account_id=state.external_account_id,
                upstream_uid=str(account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(user.nearby_custom_city_enabled),
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
            if (
                user is None
                or user.status != "active"
                or account is None
                or account.id != state.external_account_id
            ):
                return None
            return UserIdentity(
                user_id=user.id,
                external_account_id=account.id,
                upstream_uid=str(account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(user.nearby_custom_city_enabled),
            )

    def grant_message_peers(
        self,
        *,
        identity: UserIdentity,
        peers: list[str] | tuple[str, ...] | set[str],
        kind: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Persist server-owned message grants used by the send guard.

        ``kind=match`` is written only from a successful server-side match
        response. ``kind=message_peer`` records a conversation observed from a
        trusted upstream response or a send already authorized by this server.
        Browser-provided origin/source fields are never sufficient on their own.
        """

        if kind not in {
            MESSAGE_POLICY_MATCH_KIND,
            MESSAGE_POLICY_CONVERSATION_KIND,
        }:
            raise ValueError("unsupported message policy grant kind")
        normalized = list(
            dict.fromkeys(
                peer
                for peer in (_message_peer_uid(value) for value in peers)
                if peer and peer != _message_peer_uid(identity.upstream_uid)
            )
        )
        if not normalized:
            return []
        safe_evidence = {
            str(key)[:80]: value
            for key, value in dict(evidence or {}).items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        with session_scope() as db:
            repo = RelationshipRepository(db)
            for peer in normalized:
                existing = db.scalar(
                    select(Relationship).where(
                        Relationship.owner_user_id == identity.user_id,
                        Relationship.provider == MESSAGE_POLICY_PROVIDER,
                        Relationship.subject_upstream_uid == peer,
                        Relationship.kind == kind,
                    )
                )
                metadata = dict(existing.extra_data or {}) if existing else {}
                metadata.update(safe_evidence)
                metadata["server_owned"] = True
                repo.upsert(
                    owner_user_id=identity.user_id,
                    provider=MESSAGE_POLICY_PROVIDER,
                    subject_upstream_uid=peer,
                    kind=kind,
                    status="active",
                    started_at=existing.started_at if existing else utcnow(),
                    ended_at=None,
                    extra_data=metadata,
                )
        return normalized

    def can_message_peer(self, identity: UserIdentity, peer: Any) -> bool:
        """Authorize one private-message target from live durable state."""

        target = _message_peer_uid(peer)
        if not target or target == _message_peer_uid(identity.upstream_uid):
            return False
        if identity.match_pool_online_list_enabled:
            return True
        with session_scope() as db:
            grant = db.scalar(
                select(Relationship.id).where(
                    Relationship.owner_user_id == identity.user_id,
                    Relationship.subject_upstream_uid == target,
                    or_(
                        and_(
                            Relationship.provider == MESSAGE_POLICY_PROVIDER,
                            Relationship.kind.in_(
                                [
                                    MESSAGE_POLICY_MATCH_KIND,
                                    MESSAGE_POLICY_CONVERSATION_KIND,
                                ]
                            ),
                        ),
                        and_(
                            Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                            Relationship.kind == SOCIAL_FRIEND_KIND,
                        ),
                    ),
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                )
            )
            if grant is not None:
                return True
            return ConversationRepository(db).exists_for_peer(
                identity.user_id,
                target,
            )

    def message_policy_allowed_peers(
        self, identity: UserIdentity, *, limit: int = 2000
    ) -> list[str]:
        """Return durable peers whose existing or matched conversations may continue."""

        with session_scope() as db:
            relationship_peers = select(
                Relationship.subject_upstream_uid.label("peer"),
                Relationship.updated_at.label("observed_at"),
            ).where(
                Relationship.owner_user_id == identity.user_id,
                or_(
                    and_(
                        Relationship.provider == MESSAGE_POLICY_PROVIDER,
                        Relationship.kind.in_(
                            [
                                MESSAGE_POLICY_MATCH_KIND,
                                MESSAGE_POLICY_CONVERSATION_KIND,
                            ]
                        ),
                    ),
                    and_(
                        Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                        Relationship.kind == SOCIAL_FRIEND_KIND,
                    ),
                ),
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
            )
            conversation_peers = select(
                Conversation.peer_upstream_uid.label("peer"),
                Conversation.updated_at.label("observed_at"),
            ).where(
                Conversation.owner_user_id == identity.user_id,
                Conversation.provider == "tim",
                Conversation.kind == "direct",
                Conversation.peer_upstream_uid.is_not(None),
            )
            allowed_peers = union_all(
                relationship_peers,
                conversation_peers,
            ).subquery()
            values = db.scalars(
                select(allowed_peers.c.peer)
                .order_by(allowed_peers.c.observed_at.desc())
                .limit(max(1, min(int(limit), 5000)))
            )
            return list(
                dict.fromkeys(
                    peer
                    for peer in (_message_peer_uid(value) for value in values)
                    if peer and peer != _message_peer_uid(identity.upstream_uid)
                )
            )

    def message_policy_match_peers(
        self, identity: UserIdentity, *, limit: int = 2000
    ) -> list[str]:
        """Return durable match grants for restoring the browser allowlist."""

        with session_scope() as db:
            values = db.scalars(
                select(Relationship.subject_upstream_uid)
                .where(
                    Relationship.owner_user_id == identity.user_id,
                    Relationship.provider == MESSAGE_POLICY_PROVIDER,
                    Relationship.kind == MESSAGE_POLICY_MATCH_KIND,
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                )
                .order_by(Relationship.updated_at.desc())
                .limit(max(1, min(int(limit), 5000)))
            )
            return [
                peer
                for peer in (_message_peer_uid(value) for value in values)
                if peer and peer != _message_peer_uid(identity.upstream_uid)
            ]

    def remember_message_policy_response(
        self,
        *,
        identity: UserIdentity,
        method: str,
        path: str,
        request_data: Mapping[str, Any],
        response_data: Mapping[str, Any],
        status: int,
    ) -> list[str]:
        """Derive trusted grants before the product response reaches the browser."""

        if int(status) >= 400 or response_data.get("ok") is not True:
            return []
        method_upper = str(method or "").upper()
        if method_upper == "POST" and path in {
            "/api/match/online",
            "/api/match/local",
            "/api/match/voice/start",
        }:
            peers = [_item_peer_uid(item) for item in _response_items(response_data)]
            return self.grant_message_peers(
                identity=identity,
                peers=peers,
                kind=MESSAGE_POLICY_MATCH_KIND,
                evidence={"source_path": path, "grant_reason": "match_result"},
            )
        if method_upper == "GET" and path == "/api/im/conversations":
            peers = [_item_peer_uid(item) for item in _response_items(response_data)]
            return self.grant_message_peers(
                identity=identity,
                peers=peers,
                kind=MESSAGE_POLICY_CONVERSATION_KIND,
                evidence={
                    "source_path": path,
                    "grant_reason": "upstream_conversation",
                },
            )
        if method_upper == "POST" and path in {
            "/api/im/rest/send",
            "/api/im/flash/send",
        }:
            peer = _message_peer_uid(
                request_data.get("to")
                or request_data.get("peer")
                or request_data.get("uid")
                or request_data.get("targetId")
                or request_data.get("target_id")
                or response_data.get("target_id")
                or response_data.get("to")
            )
            return self.grant_message_peers(
                identity=identity,
                peers=[peer],
                kind=MESSAGE_POLICY_CONVERSATION_KIND,
                evidence={"source_path": path, "grant_reason": "authorized_send"},
            )
        return []

    def remember_match_history_response(
        self,
        *,
        identity: UserIdentity,
        method: str,
        path: str,
        response_data: Mapping[str, Any],
        status: int,
        request_id: str,
    ) -> list[str]:
        return record_match_history_response(
            owner_user_id=identity.user_id,
            upstream_uid=identity.upstream_uid,
            method=method,
            path=path,
            response_data=response_data,
            status=status,
            request_id=request_id,
        )

    def match_history(
        self,
        identity: UserIdentity,
        *,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        return load_match_history(
            owner_user_id=identity.user_id,
            page=page,
            page_size=page_size,
        )

    def conversation_summary_map(
        self,
        identity: UserIdentity,
        peers: list[str],
    ) -> dict[str, dict[str, Any]]:
        targets = list(
            dict.fromkeys(
                str(peer or "").strip()
                for peer in peers
                if str(peer or "").strip()
            )
        )[:500]
        if not targets:
            return {}
        with session_scope() as db:
            conversations = ConversationRepository(db).list_for_peers(
                identity.user_id,
                targets,
            )
            latest = MessageRepository(db).latest_for_conversations(
                identity.user_id,
                [conversation.id for conversation in conversations],
            )
            summaries: dict[str, dict[str, Any]] = {}
            for conversation in conversations:
                peer = str(conversation.peer_upstream_uid or "").strip()
                if not peer:
                    continue
                message = latest.get(conversation.id)
                metadata = dict(conversation.extra_data or {})
                occurred_at = (
                    message.occurred_at
                    if message is not None
                    else None
                )
                message_metadata = (
                    dict(message.extra_data or {})
                    if message is not None and isinstance(message.extra_data, dict)
                    else {}
                )
                preview = _conversation_message_preview(message) or str(
                    metadata.get("last_message") or ""
                )[:500]
                preview_authoritative = message is not None or metadata.get(
                    "preview_authoritative"
                ) is True
                summaries[peer] = {
                    "last_message": preview,
                    "content": preview,
                    "preview_timestamp": (
                        occurred_at.isoformat()
                        if occurred_at is not None
                        else str(metadata.get("preview_timestamp") or "")
                    ),
                    "preview_sequence": str(
                        message_metadata.get("message_sequence")
                        or metadata.get("preview_sequence")
                        or ""
                    ),
                    "preview_source": "archive",
                    "preview_authoritative": preview_authoritative,
                    "preview_timestamp_inferred": metadata.get(
                        "preview_timestamp_inferred"
                    ) is True,
                }
            return summaries

    def mark_conversations_read(
        self,
        identity: UserIdentity,
        peers: list[str],
    ) -> int:
        with session_scope() as db:
            return ConversationRepository(db).mark_peers_read(
                identity.user_id,
                peers,
                observed_at=utcnow(),
            )

    def enqueue_message_archive(
        self,
        *,
        identity: UserIdentity,
        payload: dict[str, Any],
        client_ip: str,
    ) -> bool:
        idempotency_key = str(payload.get("idempotency_key") or "")
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        job_id = f"archive-message-{identity.user_id}-{digest}"
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
        transport_ok = 200 <= int(status) < 300
        response_ok = transport_ok and response_data.get("ok") is not False
        if transport_ok and path == "/api/frontback":
            self.set_web_presence(
                identity.upstream_uid,
                active=str(request_data.get("frontorback") or "1") == "1",
            )
        elif transport_ok and path in {
            "/api/online",
            "/api/heartbeat/start",
            "/api/heartbeat/once",
        }:
            self.set_web_presence(identity.upstream_uid, active=True)
        elif transport_ok and path in {"/api/auth/logout", "/api/heartbeat/stop"}:
            self.set_web_presence(identity.upstream_uid, active=False)
        elif response_ok:
            if path in {
                "/api/auth/login",
                "/api/auth/sms-login",
            }:
                self.set_web_presence(identity.upstream_uid, active=True)
        if method.upper() == "POST" and path == "/api/im/read":
            raw_read_peers = response_data.get("read_peers")
            if isinstance(raw_read_peers, list):
                read_peers = [str(peer or "").strip() for peer in raw_read_peers]
            elif response_ok:
                raw_request_peers = request_data.get("peers")
                if isinstance(raw_request_peers, list):
                    read_peers = [str(peer or "").strip() for peer in raw_request_peers]
                else:
                    read_peers = [
                        str(
                            request_data.get("peer")
                            or request_data.get("uid")
                            or request_data.get("to")
                            or ""
                        ).strip()
                    ]
            else:
                read_peers = []
            if read_peers:
                self.mark_conversations_read(
                    identity,
                    [peer for peer in read_peers if peer],
                )
        if (
            method.upper() == "GET"
            and path == "/api/moments/posts"
            and response_ok
            and response_data.get("ok") is True
        ):
            self.remember_moment_video_grants(identity, response_data)
        if (
            response_ok
            and path in {"/api/im/messages", "/api/im/conversations"}
            and response_data
        ):
            digest = hashlib.sha256(
                json.dumps(response_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if self._claim_response_digest("history", identity, digest):
                try:
                    self.im_ingest_queue.enqueue(
                        "bbw_web.jobs.ingest_history_response",
                        str(identity.user_id),
                        str(identity.external_account_id),
                        path,
                        query,
                        response_data,
                        job_id=f"history-response-{identity.user_id}-{digest}",
                        job_timeout=300,
                        result_ttl=600,
                        failure_ttl=86400,
                    )
                except Exception:
                    self.redis.delete(self._response_digest_key("history", identity, digest))
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
            if self._claim_response_digest("social-snapshot", identity, snapshot_digest):
                try:
                    self.default_queue.enqueue(
                        "bbw_web.jobs.ingest_social_snapshot",
                        str(identity.user_id),
                        str(identity.external_account_id),
                        path,
                        query,
                        response_data,
                        job_id=f"social-snapshot-{identity.user_id}-{snapshot_digest}",
                        result_ttl=600,
                        failure_ttl=86400,
                    )
                except Exception:
                    self.redis.delete(
                        self._response_digest_key(
                            "social-snapshot", identity, snapshot_digest
                        )
                    )
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
            "/api/match/online-users",
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
            if self._claim_response_digest("product-event", identity, digest):
                try:
                    self.default_queue.enqueue(
                        "bbw_web.jobs.record_product_event",
                        str(identity.user_id),
                        event_payload,
                        job_id=f"product-event-{identity.user_id}-{digest}",
                        result_ttl=600,
                        failure_ttl=86400,
                    )
                except Exception:
                    self.redis.delete(
                        self._response_digest_key("product-event", identity, digest)
                    )
