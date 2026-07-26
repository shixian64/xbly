"""Production persistence bridge between the legacy BFF and PostgreSQL/Redis."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue as queue_module
import re
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from redis import Redis
from rq import Queue
from rq.exceptions import InvalidJobOperation
from sqlalchemy import and_, func, literal, or_, select, text, union_all
from sqlalchemy.orm import aliased

from bbw_prod.config import Settings
from bbw_prod.crypto import CredentialCipher, normalize_phone
from bbw_prod.db import session_scope
from bbw_prod.models import (
    Conversation,
    ExternalAccount,
    Relationship,
    SyncCursor,
    utcnow,
)
from bbw_prod.repositories import (
    ConversationRepository,
    ExternalAccountRepository,
    RelationshipRepository,
    SyncCursorRepository,
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
    UserCredentialService,
    UserSessionService,
)
from bbw_web.match_history import load_match_history, record_match_history_response
from bbw_web.dependency_health import (
    DependencyErrorKind,
    DependencyFailure,
    DependencyStatusRegistry,
)
from bbw_web.providers import (
    ProviderApiResult,
    ProviderApplication,
    ProviderSessionState,
    RuntimeProvider,
)
from bbw_web.private_message_policy import private_message_permission_query
from bbw_web.store import WebUser
from bbw_web.turnstile import TurnstileVerifier


LOGGER = logging.getLogger(__name__)


def _default_local_runtime_provider(
    account_provider_id: str = "beibeiwu",
) -> RuntimeProvider:
    from bbw_web.providers import WebNativeProvider

    return WebNativeProvider(account_provider_id=account_provider_id)


def _default_runtime_provider(settings: Settings | None = None) -> RuntimeProvider:
    """Resolve a provider without importing the protocol core in local-only mode."""

    if str(getattr(settings, "upstream_auth_mode", "") or "").lower() == "local-only":
        return _default_local_runtime_provider()
    from bbw_web.providers import LegacyBanghuaProvider

    return LegacyBanghuaProvider()


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    match_pool_online_list_enabled: bool = False
    nearby_custom_city_enabled: bool = False
    auth_source: str = "provider"


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


@dataclass(frozen=True, slots=True)
class LocalPasswordLoginCompletion:
    """A durable Web session issued after conservative local fallback."""

    raw_sid: str
    identity: UserIdentity
    web_user: WebUser


@dataclass(frozen=True, slots=True)
class RawResponseArchiveItem:
    owner_user_id: uuid.UUID
    endpoint: str
    request_meta: dict[str, Any]
    raw_response: str
    code: str
    message: str
    kind: str
    http_status: int


@dataclass(frozen=True, slots=True)
class ProductEventQueueItem:
    owner_user_id: uuid.UUID
    payload: dict[str, Any]
    digest: str
    digest_key: str


@dataclass(frozen=True, slots=True)
class HistoryResponseQueueItem:
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    path: str
    query: dict[str, Any]
    response_data: Any
    digest: str
    digest_key: str


HISTORY_CONVERSATION_REFRESH_SECONDS = 120
HISTORY_CONVERSATION_VOLATILE_KEYS = frozenset(
    {"unread_observed_at", "summary_observed_at"}
)


def _stable_history_response_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_history_response_value(item)
            for key, item in value.items()
            if str(key) not in HISTORY_CONVERSATION_VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_history_response_value(item) for item in value]
    return value


def _history_response_digest(
    path: str,
    query: Mapping[str, Any] | None,
    response_data: Any,
    *,
    observed_at: float | None = None,
) -> str:
    route = "/" + str(path or "").strip("/")
    digest_response = response_data
    payload: dict[str, Any] = {
        "path": route,
        "query": dict(query or {}),
    }
    if route == "/api/im/conversations":
        digest_response = _stable_history_response_value(response_data)
        timestamp = time.time() if observed_at is None else float(observed_at)
        payload["observation_bucket"] = int(
            timestamp // HISTORY_CONVERSATION_REFRESH_SECONDS
        )
    payload["response"] = digest_response
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


MESSAGE_POLICY_PROVIDER = "web-policy"
MESSAGE_POLICY_MATCH_KIND = "match"
MESSAGE_POLICY_CONVERSATION_KIND = "message_peer"
MESSAGE_PEER_SNAPSHOT_SCHEMA = 1
MESSAGE_PEER_SNAPSHOT_SOURCE = "beibeiwu"
MESSAGE_PEER_SNAPSHOT_STREAM = "message-peer-snapshot"
MESSAGE_PEER_SNAPSHOT_PATH = "/api/im/conversations"
MESSAGE_PEER_SNAPSHOT_MAX_PEERS = 5000
SOCIAL_RELATIONSHIP_PROVIDER = "beibeiwu"
SOCIAL_CANONICAL_PROVIDER = "web-local"
SOCIAL_MESSAGE_POLICY_PROVIDERS = (
    SOCIAL_RELATIONSHIP_PROVIDER,
    SOCIAL_CANONICAL_PROVIDER,
)
SOCIAL_FRIEND_KIND = "friend"
SOCIAL_BLACKLIST_KIND = "blacklist"
SOCIAL_BLACKLISTED_BY_KIND = "blacklisted_by"
SOCIAL_MESSAGE_BLOCK_KINDS = (
    SOCIAL_BLACKLIST_KIND,
    SOCIAL_BLACKLISTED_BY_KIND,
)
MESSAGE_BLOCK_SNAPSHOT_SOURCE = SOCIAL_RELATIONSHIP_PROVIDER
MESSAGE_BLOCK_SNAPSHOT_SCHEMA = 1
MESSAGE_BLOCK_SNAPSHOT_MAX_PEERS = 5000
MESSAGE_BLOCK_SNAPSHOT_PATHS = {
    SOCIAL_BLACKLIST_KIND: "/api/social/blacklist",
    SOCIAL_BLACKLISTED_BY_KIND: "/api/social/blacklist-me",
}
MESSAGE_BLOCK_SNAPSHOT_STREAMS = {
    kind: f"message-block-snapshot:{kind}" for kind in SOCIAL_MESSAGE_BLOCK_KINDS
}
MESSAGE_BLOCK_TRUSTED_SOURCES = {
    SOCIAL_BLACKLIST_KIND: frozenset(
        {
            "/api/social/blacklist",
            "/api/social/blacklist-add",
            "/api/social/blacklist-del",
        }
    ),
    SOCIAL_BLACKLISTED_BY_KIND: frozenset({"/api/social/blacklist-me"}),
}


def _message_peer_snapshot_digest(peers: Iterable[str]) -> str:
    normalized = sorted(
        set(
            str(peer or "").strip()
            for peer in peers
            if str(peer or "").strip()
        )
    )
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _response_has_item_list(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return isinstance(payload, list)
    for key in ("items", "list", "users"):
        if isinstance(payload.get(key), list):
            return True
    nested = payload.get("data")
    return nested is not payload and _response_has_item_list(nested)


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


_REQUEST_SECRET_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", key.casefold())
    for key in {
        "password",
        "userpassword",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "x-api-key",
        "client_secret",
        "clientsecret",
        "confirmation_token",
        "confirmationtoken",
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
)
_BYOK_SECRET_KEY_FRAGMENTS = ("apikey", "clientsecret", "confirmationtoken")


def _is_sensitive_request_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return normalized in _REQUEST_SECRET_KEYS or any(
        fragment in normalized for fragment in _BYOK_SECRET_KEY_FRAGMENTS
    )


def _redact_request(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_request_key(key)
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
    "apikey",
    "xapikey",
    "clientsecret",
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
                or any(
                    fragment in normalized
                    for fragment in _BYOK_SECRET_KEY_FRAGMENTS
                )
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
    FLASH_REVEAL_TTL_SECONDS = 5 * 60
    FLASH_REVEALED_TTL_SECONDS = 10 * 60
    RAW_RESPONSE_QUEUE_MAX = 64
    RAW_RESPONSE_BATCH_SIZE = 16
    RAW_RESPONSE_BATCH_WAIT_SECONDS = 0.1
    RAW_RESPONSE_SHUTDOWN_WAIT_SECONDS = 5.0
    PRODUCT_EVENT_QUEUE_MAX = 1024
    PRODUCT_EVENT_BATCH_SIZE = 32
    PRODUCT_EVENT_BATCH_WAIT_SECONDS = 0.1
    PRODUCT_EVENT_SHUTDOWN_WAIT_SECONDS = 5.0
    HISTORY_RESPONSE_QUEUE_MAX = 64
    HISTORY_RESPONSE_BATCH_SIZE = 8
    HISTORY_RESPONSE_BATCH_WAIT_SECONDS = 0.2
    HISTORY_RESPONSE_SHUTDOWN_WAIT_SECONDS = 5.0
    FLASH_REVEAL_READ_SCRIPT = """
local acknowledged = redis.call('EXISTS', KEYS[1])
if acknowledged == 1 then
  return false
end
return redis.call('GET', KEYS[2])
"""
    FLASH_REVEAL_REMEMBER_SCRIPT = """
local acknowledged = redis.call('EXISTS', KEYS[1])
if acknowledged == 1 then
  return 0
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
return 1
"""
    FLASH_REVEAL_ACK_SCRIPT = """
local acknowledged = redis.call('EXISTS', KEYS[1])
if acknowledged == 1 then
  return 1
end
local pending = redis.call('EXISTS', KEYS[2])
if pending == 0 then
  return 0
end
redis.call('DEL', KEYS[2])
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
return 1
"""

    def __init__(
        self,
        settings: Settings,
        *,
        runtime_provider: RuntimeProvider | None = None,
        local_runtime_provider: RuntimeProvider | None = None,
    ):
        self.settings = settings
        self.runtime_provider = (
            runtime_provider
            if runtime_provider is not None
            else _default_runtime_provider(settings)
        )
        self.local_runtime_provider = (
            local_runtime_provider
            if local_runtime_provider is not None
            else _default_local_runtime_provider(
                str(getattr(self.runtime_provider, "provider_id", "") or "beibeiwu")
            )
        )
        self.dependency_status = DependencyStatusRegistry()
        self.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        self.cipher = CredentialCipher.from_settings(settings)
        self.phone_hmac_key = settings.load_phone_hmac_key()
        self.session_hmac_key = settings.load_session_hmac_key()
        self.turnstile = TurnstileVerifier(settings)
        self.default_queue = Queue("default", connection=self.redis)
        self.media_queue = Queue("media", connection=self.redis)
        self.im_ingest_queue = Queue("im-ingest", connection=self.redis)
        self._r2_storage: Any = None
        self._raw_response_queue: queue_module.Queue[RawResponseArchiveItem] = (
            queue_module.Queue(maxsize=self.RAW_RESPONSE_QUEUE_MAX)
        )
        self._raw_response_stop = threading.Event()
        self._raw_response_thread: threading.Thread | None = None
        self._raw_response_accepting = False
        self._raw_response_stored = 0
        self._raw_response_dropped = 0
        self._raw_response_failed = 0
        self._product_event_queue: queue_module.Queue[ProductEventQueueItem] = (
            queue_module.Queue(maxsize=self.PRODUCT_EVENT_QUEUE_MAX)
        )
        self._product_event_stop = threading.Event()
        self._product_event_thread: threading.Thread | None = None
        self._product_event_accepting = False
        self._product_event_enqueued = 0
        self._product_event_dropped = 0
        self._product_event_failed = 0
        self._history_response_queue: queue_module.Queue[HistoryResponseQueueItem] = (
            queue_module.Queue(maxsize=self.HISTORY_RESPONSE_QUEUE_MAX)
        )
        self._history_response_stop = threading.Event()
        self._history_response_thread: threading.Thread | None = None
        self._history_response_accepting = False
        self._history_response_enqueued = 0
        self._history_response_dropped = 0
        self._history_response_failed = 0

    def startup(self) -> None:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
        self.redis.ping()
        self._start_raw_response_worker()
        self._start_product_event_worker()
        self._start_history_response_worker()

    def close(self) -> None:
        self._stop_history_response_worker()
        self._stop_product_event_worker()
        self._stop_raw_response_worker()
        try:
            self.redis.close()
        except Exception:
            pass

    def _start_raw_response_worker(self) -> None:
        thread = self._raw_response_thread
        if thread is not None and thread.is_alive():
            self._raw_response_accepting = True
            return
        self._raw_response_stop.clear()
        self._raw_response_accepting = True
        self._raw_response_thread = threading.Thread(
            target=self._raw_response_worker,
            name="bbw-raw-response-archive",
            daemon=True,
        )
        self._raw_response_thread.start()

    def _stop_raw_response_worker(self) -> None:
        self._raw_response_accepting = False
        stop = getattr(self, "_raw_response_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "_raw_response_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.RAW_RESPONSE_SHUTDOWN_WAIT_SECONDS)
        pending = getattr(self, "_raw_response_queue", None)
        if thread is not None and thread.is_alive():
            LOGGER.warning(
                "raw response archive worker did not stop before shutdown; pending=%d",
                pending.qsize() if pending is not None else 0,
            )

    def _raw_response_worker(self) -> None:
        pending = self._raw_response_queue
        stop = self._raw_response_stop
        while not stop.is_set() or not pending.empty():
            try:
                first = pending.get(timeout=self.RAW_RESPONSE_BATCH_WAIT_SECONDS)
            except queue_module.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.RAW_RESPONSE_BATCH_WAIT_SECONDS
            while len(batch) < self.RAW_RESPONSE_BATCH_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(pending.get(timeout=remaining))
                except queue_module.Empty:
                    break
            try:
                self._store_raw_response_batch(batch)
                self._raw_response_stored += len(batch)
            except Exception:
                self._raw_response_failed += len(batch)
                LOGGER.exception(
                    "raw response archive batch failed; batch_size=%d",
                    len(batch),
                )
            finally:
                for _item in batch:
                    pending.task_done()

    def _store_raw_response_batch(
        self,
        batch: list[RawResponseArchiveItem],
    ) -> None:
        if not batch:
            return
        with session_scope() as db:
            service = RawResponseService(db, self.settings, self.cipher)
            for item in batch:
                service.store(
                    owner_user_id=item.owner_user_id,
                    endpoint=item.endpoint,
                    payload={
                        "request": _redact_request(item.request_meta),
                        "response": _safe_json(item.raw_response),
                        "code": item.code,
                        "message": item.message,
                        "kind": item.kind,
                    },
                    http_status=item.http_status,
                )

    def _start_product_event_worker(self) -> None:
        thread = self._product_event_thread
        if thread is not None and thread.is_alive():
            self._product_event_accepting = True
            return
        self._product_event_stop.clear()
        self._product_event_accepting = True
        self._product_event_thread = threading.Thread(
            target=self._product_event_worker,
            name="bbw-product-event-batch",
            daemon=True,
        )
        self._product_event_thread.start()

    def _stop_product_event_worker(self) -> None:
        self._product_event_accepting = False
        self._product_event_stop.set()
        thread = self._product_event_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.PRODUCT_EVENT_SHUTDOWN_WAIT_SECONDS)
        if thread is not None and thread.is_alive():
            LOGGER.warning(
                "product event worker did not stop before shutdown; pending=%d",
                self._product_event_queue.qsize(),
            )

    def _product_event_worker(self) -> None:
        pending = self._product_event_queue
        stop = self._product_event_stop
        while not stop.is_set() or not pending.empty():
            try:
                first = pending.get(timeout=self.PRODUCT_EVENT_BATCH_WAIT_SECONDS)
            except queue_module.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.PRODUCT_EVENT_BATCH_WAIT_SECONDS
            while len(batch) < self.PRODUCT_EVENT_BATCH_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(pending.get(timeout=remaining))
                except queue_module.Empty:
                    break
            grouped: dict[uuid.UUID, list[ProductEventQueueItem]] = {}
            for item in batch:
                grouped.setdefault(item.owner_user_id, []).append(item)
            for owner_user_id, items in grouped.items():
                try:
                    digest = hashlib.sha256(
                        "\n".join(sorted(item.digest for item in items)).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    self.default_queue.enqueue(
                        "bbw_web.jobs.record_product_events_batch",
                        str(owner_user_id),
                        [item.payload for item in items],
                        job_id=f"product-event-batch-{owner_user_id}-{digest}",
                        job_timeout=180,
                        result_ttl=600,
                        failure_ttl=86400,
                    )
                    self._product_event_enqueued += len(items)
                except InvalidJobOperation:
                    self._product_event_enqueued += len(items)
                except Exception as exc:
                    if "already exists" in str(exc).lower():
                        self._product_event_enqueued += len(items)
                        continue
                    self._product_event_failed += len(items)
                    pipe = self.redis.pipeline(transaction=False)
                    for item in items:
                        pipe.delete(item.digest_key)
                    try:
                        pipe.execute()
                    except Exception:
                        LOGGER.exception(
                            "failed to release product event dedupe claims"
                        )
                    LOGGER.exception(
                        "product event batch enqueue failed; batch_size=%d",
                        len(items),
                    )
            for _item in batch:
                pending.task_done()

    def _queue_product_event(
        self,
        *,
        identity: UserIdentity,
        payload: dict[str, Any],
        digest: str,
    ) -> bool:
        thread = self._product_event_thread
        digest_key = self._response_digest_key("product-event", identity, digest)
        if (
            not self._product_event_accepting
            or thread is None
            or not thread.is_alive()
        ):
            self.redis.delete(digest_key)
            self._product_event_dropped += 1
            return False
        try:
            self._product_event_queue.put_nowait(
                ProductEventQueueItem(
                    owner_user_id=identity.user_id,
                    payload=payload,
                    digest=digest,
                    digest_key=digest_key,
                )
            )
            return True
        except queue_module.Full:
            self.redis.delete(digest_key)
            self._product_event_dropped += 1
            if (
                self._product_event_dropped == 1
                or self._product_event_dropped % 100 == 0
            ):
                LOGGER.warning(
                    "product event queue full; dropped=%d queue_max=%d",
                    self._product_event_dropped,
                    self.PRODUCT_EVENT_QUEUE_MAX,
                )
            return False

    def _start_history_response_worker(self) -> None:
        thread = self._history_response_thread
        if thread is not None and thread.is_alive():
            self._history_response_accepting = True
            return
        self._history_response_stop.clear()
        self._history_response_accepting = True
        self._history_response_thread = threading.Thread(
            target=self._history_response_worker,
            name="bbw-history-response-batch",
            daemon=True,
        )
        self._history_response_thread.start()

    def _stop_history_response_worker(self) -> None:
        self._history_response_accepting = False
        self._history_response_stop.set()
        thread = self._history_response_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.HISTORY_RESPONSE_SHUTDOWN_WAIT_SECONDS)
        if thread is not None and thread.is_alive():
            LOGGER.warning(
                "history response worker did not stop before shutdown; pending=%d",
                self._history_response_queue.qsize(),
            )

    def _history_response_worker(self) -> None:
        pending = self._history_response_queue
        stop = self._history_response_stop
        while not stop.is_set() or not pending.empty():
            try:
                first = pending.get(timeout=self.HISTORY_RESPONSE_BATCH_WAIT_SECONDS)
            except queue_module.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.HISTORY_RESPONSE_BATCH_WAIT_SECONDS
            while len(batch) < self.HISTORY_RESPONSE_BATCH_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(pending.get(timeout=remaining))
                except queue_module.Empty:
                    break
            grouped: dict[
                tuple[uuid.UUID, uuid.UUID], list[HistoryResponseQueueItem]
            ] = {}
            for item in batch:
                grouped.setdefault(
                    (item.owner_user_id, item.external_account_id), []
                ).append(item)
            for (owner_user_id, external_account_id), items in grouped.items():
                try:
                    digest = hashlib.sha256(
                        "\n".join(sorted(item.digest for item in items)).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    self.im_ingest_queue.enqueue(
                        "bbw_web.jobs.ingest_history_responses_batch",
                        str(owner_user_id),
                        str(external_account_id),
                        [
                            {
                                "path": item.path,
                                "query": item.query,
                                "response_data": item.response_data,
                            }
                            for item in items
                        ],
                        job_id=(
                            f"history-response-batch-{owner_user_id}-{digest}"
                        ),
                        job_timeout=600,
                        result_ttl=600,
                        failure_ttl=86400,
                    )
                    self._history_response_enqueued += len(items)
                except InvalidJobOperation:
                    self._history_response_enqueued += len(items)
                except Exception as exc:
                    if "already exists" in str(exc).lower():
                        self._history_response_enqueued += len(items)
                        continue
                    self._history_response_failed += len(items)
                    pipe = self.redis.pipeline(transaction=False)
                    for item in items:
                        pipe.delete(item.digest_key)
                    try:
                        pipe.execute()
                    except Exception:
                        LOGGER.exception(
                            "failed to release history response dedupe claims"
                        )
                    LOGGER.exception(
                        "history response batch enqueue failed; batch_size=%d",
                        len(items),
                    )
            for _item in batch:
                pending.task_done()

    def _queue_history_response(
        self,
        *,
        identity: UserIdentity,
        path: str,
        query: Mapping[str, Any] | None,
        response_data: Any,
        digest: str,
    ) -> bool:
        thread = self._history_response_thread
        digest_key = self._response_digest_key("history", identity, digest)
        if (
            not self._history_response_accepting
            or thread is None
            or not thread.is_alive()
        ):
            self.redis.delete(digest_key)
            self._history_response_dropped += 1
            return False
        try:
            self._history_response_queue.put_nowait(
                HistoryResponseQueueItem(
                    owner_user_id=identity.user_id,
                    external_account_id=identity.external_account_id,
                    path=str(path or "")[:256],
                    query=dict(query or {}),
                    response_data=response_data,
                    digest=digest,
                    digest_key=digest_key,
                )
            )
            return True
        except queue_module.Full:
            self.redis.delete(digest_key)
            self._history_response_dropped += 1
            if (
                self._history_response_dropped == 1
                or self._history_response_dropped % 100 == 0
            ):
                LOGGER.warning(
                    "history response queue full; dropped=%d queue_max=%d",
                    self._history_response_dropped,
                    self.HISTORY_RESPONSE_QUEUE_MAX,
                )
            return False

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
            "dependencies": self.dependency_status.public_snapshot(),
            "raw_response_archive": {
                "worker_alive": bool(
                    self._raw_response_thread
                    and self._raw_response_thread.is_alive()
                ),
                "queue_depth": self._raw_response_queue.qsize(),
                "stored": self._raw_response_stored,
                "dropped": self._raw_response_dropped,
                "failed": self._raw_response_failed,
            },
            "product_event_queue": {
                "worker_alive": bool(
                    self._product_event_thread
                    and self._product_event_thread.is_alive()
                ),
                "queue_depth": self._product_event_queue.qsize(),
                "enqueued": self._product_event_enqueued,
                "dropped": self._product_event_dropped,
                "failed": self._product_event_failed,
            },
            "history_response_queue": {
                "worker_alive": bool(
                    self._history_response_thread
                    and self._history_response_thread.is_alive()
                ),
                "queue_depth": self._history_response_queue.qsize(),
                "enqueued": self._history_response_enqueued,
                "dropped": self._history_response_dropped,
                "failed": self._history_response_failed,
            },
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

    def _flash_reveal_digest(self, upstream_uid: str, unique_id: str) -> str:
        return keyed_identifier_hash(
            f"{str(upstream_uid or '').strip()}\n{str(unique_id or '').strip()}",
            self.session_hmac_key,
            purpose="flash-reveal",
        )

    def _flash_reveal_key(self, upstream_uid: str, unique_id: str) -> str:
        return (
            f"{self.settings.redis_prefix}:flash-reveal:pending:"
            f"{self._flash_reveal_digest(upstream_uid, unique_id)}"
        )

    def _flash_revealed_key(self, upstream_uid: str, unique_id: str) -> str:
        return (
            f"{self.settings.redis_prefix}:flash-reveal:acknowledged:"
            f"{self._flash_reveal_digest(upstream_uid, unique_id)}"
        )

    def cached_flash_reveal(self, upstream_uid: str, unique_id: str) -> str:
        if not str(upstream_uid or "").strip() or not str(unique_id or "").strip():
            return ""
        value = self.redis.eval(
            self.FLASH_REVEAL_READ_SCRIPT,
            2,
            self._flash_revealed_key(upstream_uid, unique_id),
            self._flash_reveal_key(upstream_uid, unique_id),
        )
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        return str(value or "")[:2048]

    def remember_flash_reveal(
        self,
        upstream_uid: str,
        unique_id: str,
        photo_path: str,
    ) -> bool:
        uid = str(upstream_uid or "").strip()
        identifier = str(unique_id or "").strip()
        path = str(photo_path or "").strip()[:2048]
        if not uid or not identifier or not path:
            return False
        return bool(
            self.redis.eval(
                self.FLASH_REVEAL_REMEMBER_SCRIPT,
                2,
                self._flash_revealed_key(uid, identifier),
                self._flash_reveal_key(uid, identifier),
                path.encode("utf-8"),
                self.FLASH_REVEAL_TTL_SECONDS,
            )
        )

    def flash_reveal_acknowledged(self, upstream_uid: str, unique_id: str) -> bool:
        uid = str(upstream_uid or "").strip()
        identifier = str(unique_id or "").strip()
        if not uid or not identifier:
            return False
        return bool(self.redis.exists(self._flash_revealed_key(uid, identifier)))

    def acknowledge_flash_reveal(self, upstream_uid: str, unique_id: str) -> bool:
        uid = str(upstream_uid or "").strip()
        identifier = str(unique_id or "").strip()
        if not uid or not identifier:
            return False
        pending_key = self._flash_reveal_key(uid, identifier)
        acknowledged_key = self._flash_revealed_key(uid, identifier)
        return bool(
            self.redis.eval(
                self.FLASH_REVEAL_ACK_SCRIPT,
                2,
                acknowledged_key,
                pending_key,
                self.FLASH_REVEALED_TTL_SECONDS,
            )
        )

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

    def _moment_video_source_grant_key(
        self,
        identity: UserIdentity,
        post_id: str,
        source_url: str,
    ) -> str:
        post_digest = hashlib.sha256(str(post_id).encode("utf-8")).hexdigest()
        source_digest = hashlib.sha256(str(source_url).encode("utf-8")).hexdigest()
        return (
            f"{self.settings.redis_prefix}:moment-video-source-grant:"
            f"{identity.user_id}:{post_digest}:{source_digest}"
        )

    def remember_moment_video_grants(
        self, identity: UserIdentity, response_data: dict[str, Any]
    ) -> int:
        """Remember only videos that appeared in this user's successful feed response."""
        from bbw_web.moment_video import (
            MomentVideoError,
            asset_id_for_url,
            canonical_source_identity,
            compatibility_profiles_for_cleanup,
        )

        raw_items = response_data.get("items") or response_data.get("list") or []
        if not isinstance(raw_items, list):
            return 0
        grants: list[tuple[str, str, tuple[str, ...]]] = []
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
            asset_ids = tuple(
                asset_id_for_url(canonical, profile=profile)
                for profile in compatibility_profiles_for_cleanup()
            )
            grants.append((post_id, canonical, asset_ids))
        if not grants:
            return 0
        pipe = self.redis.pipeline()
        for post_id, canonical, asset_ids in grants:
            pipe.set(
                self._moment_video_source_grant_key(identity, post_id, canonical),
                b"1",
                ex=self.MOMENT_VIDEO_GRANT_SECONDS,
            )
            for asset_id in asset_ids:
                pipe.set(
                    self._moment_video_feed_grant_key(identity, post_id, asset_id),
                    b"1",
                    ex=self.MOMENT_VIDEO_GRANT_SECONDS,
                )
        pipe.execute()
        return len(grants)

    def authorize_moment_video(
        self,
        identity: UserIdentity,
        *,
        post_id: str,
        asset_id: str,
        source_url: str = "",
    ) -> bool:
        from bbw_web.moment_video import (
            asset_id_for_url,
            compatibility_profiles_for_cleanup,
        )

        asset_ids = [str(asset_id)]
        grant_keys = [self._moment_video_feed_grant_key(identity, post_id, asset_id)]
        if source_url:
            grant_keys.append(
                self._moment_video_source_grant_key(identity, post_id, source_url)
            )
            asset_ids = [
                asset_id_for_url(source_url, profile=profile)
                for profile in compatibility_profiles_for_cleanup()
            ]
            grant_keys.extend(
                self._moment_video_feed_grant_key(identity, post_id, candidate)
                for candidate in asset_ids
            )
        if not any(self.redis.exists(key) for key in dict.fromkeys(grant_keys)):
            return False
        pipe = self.redis.pipeline()
        for candidate in dict.fromkeys(asset_ids):
            pipe.set(
                self._moment_video_access_key(identity, candidate),
                b"1",
                ex=self.MOMENT_VIDEO_GRANT_SECONDS,
            )
        pipe.execute()
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
        self._precheck_login_abuse(
            phone=phone,
            client_ip=client_ip,
            turnstile_token=turnstile_token,
        )
        return self.precheck_account(phone=phone)

    def precheck_local_password_credentials(
        self,
        *,
        phone: str,
        client_ip: str,
        turnstile_token: str = "",
    ) -> str:
        """Apply abuse controls without revealing local account state.

        Local-only authentication must reach the Argon2 verifier for unknown,
        suspended, disabled and not-yet-migrated accounts.  Querying the
        account state here would expose those cases before the dummy verify.
        """

        self._precheck_login_abuse(
            phone=phone,
            client_ip=client_ip,
            turnstile_token=turnstile_token,
        )
        return normalize_phone(phone)

    def _precheck_login_abuse(
        self,
        *,
        phone: str,
        client_ip: str,
        turnstile_token: str = "",
    ) -> None:
        if not phone:
            raise PermissionError("请输入手机号")
        failures = self._login_failure_count(phone, client_ip)
        if failures >= 5:
            raise RuntimeError("登录失败次数过多，请在 15 分钟后重试")
        if failures >= 2 and self.turnstile.enabled:
            if not self.turnstile.verify(turnstile_token, remote_ip=client_ip):
                raise PermissionError("需要完成人机验证后才能继续登录")

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
                password_verified=pending.mode == "password",
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
        password_verified: bool = False,
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
                password_verified=bool(password_verified),
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
        setattr(web_user, "authentication_source", "provider")
        self._attach_runtime(web_user, identity)
        provider = str(getattr(self.runtime_provider, "provider_id", "") or "")
        if provider:
            self.dependency_status.mark_available(provider, "auth")
        return identity

    def complete_local_password_login(
        self,
        *,
        phone: str,
        password: str,
        old_sid: str | None,
        client_ip: str,
        user_agent: str,
    ) -> LocalPasswordLoginCompletion:
        """Issue a normal Web session from a migrated local password verifier.

        The HTTP orchestration layer may call this method only after the
        provider returned the explicit ``UPSTREAM_AUTH_UNAVAILABLE`` signal.
        Keeping that decision outside the credential service prevents a stale
        local password from bypassing an upstream rejection, suspension or
        rate limit.
        """

        with session_scope() as db:
            account_service = LoginAccountService(
                db, self.settings, self.cipher, self.phone_hmac_key
            )
            authenticated = account_service.authenticate_local_password(
                phone=phone,
                password=password,
                provider=str(self.runtime_provider.provider_id),
            )
            issued = UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).issue(
                user_id=authenticated.user.id,
                external_account_id=authenticated.external_account.id,
                client_ip=client_ip,
                user_agent=user_agent,
                auth_source="web-local",
            )
            identity = UserIdentity(
                user_id=authenticated.user.id,
                external_account_id=authenticated.external_account.id,
                upstream_uid=str(authenticated.external_account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    authenticated.user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(
                    authenticated.user.nearby_custom_city_enabled
                ),
                auth_source="web-local",
            )

        try:
            web_user = self.restore_web_user(issued.sid)
            if web_user is None:
                raise RuntimeError("local login session could not be restored")
        except Exception:
            self.revoke_session(issued.sid, reason="local_login_restore_failed")
            raise

        if old_sid and old_sid != issued.sid:
            self.revoke_session(old_sid, reason="rotated")
        self._clear_login_failures(phone=phone, client_ip=client_ip)
        setattr(web_user, "authentication_source", "local")
        provider = str(getattr(self.runtime_provider, "provider_id", "") or "")
        if provider:
            self.dependency_status.mark_degraded(
                provider,
                "auth",
                failure=DependencyFailure(
                    DependencyErrorKind.UPSTREAM,
                    retryable=True,
                    status_code=503,
                ),
            )
        return LocalPasswordLoginCompletion(issued.sid, identity, web_user)

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
            binding = ExternalAccountRepository(db).get_user_binding(
                state.user_id,
                external_account_id=state.external_account_id,
            )
            if binding is None or binding[0].status != "active":
                sessions.revoke(sid, reason="account_unavailable")
                return None
            user, account = binding
            if str(account.provider or "") != str(self.runtime_provider.provider_id):
                sessions.revoke(sid, reason="provider_unavailable")
                return None
            stored_auth_source = str(
                getattr(state, "auth_source", "provider") or "provider"
            ).strip().lower()
            use_local_runtime = (
                stored_auth_source == "web-local"
                or str(
                    getattr(self.settings, "upstream_auth_mode", "provider-first")
                    or "provider-first"
                ).strip().lower()
                == "local-only"
            )
            if use_local_runtime:
                # Local sessions do not need an APK token or reversible login
                # field.  A missing, expired or undecryptable legacy token must
                # not prevent Web session recovery after provider retirement.
                login = ""
                token = ""
                runtime_provider = self.local_runtime_provider
            else:
                login, _password, token = self._decrypt_account(
                    account, include_password=False
                )
                runtime_provider = self.runtime_provider
            data = dict(account.device_data or {})
            runtime = runtime_provider.create_runtime_from_state(
                ProviderSessionState(
                    uid=str(account.upstream_uid or "0"),
                    token=token,
                    phone=login,
                    nickname=str(user.display_name or ""),
                    user_role=str(data.get("user_role") or ""),
                    rp_verify_time=str(data.get("rp_verify_time") or "0"),
                    vip=str(data.get("vip") or "0"),
                    svip=str(data.get("svip") or "0"),
                    money=str(data.get("money") or "0"),
                    portrait=str(data.get("portrait") or ""),
                    user_sign=str(data.get("user_sign") or ""),
                    login_id=str(data.get("login_id") or ""),
                    raw_user=dict(user.profile or {}),
                    device_data=data,
                )
            )
            web_user = WebUser(
                web_sid=sid,
                app=runtime.app,
                native=runtime.native,
                label=str(user.display_name or account.upstream_uid or ""),
                created_at=state.created_at.timestamp(),
                last_seen=state.last_seen_at.timestamp(),
                persist_sessions=False,
            )
            setattr(
                web_user,
                "authentication_source",
                "local" if use_local_runtime else "provider",
            )
            identity = UserIdentity(
                user_id=user.id,
                external_account_id=account.id,
                upstream_uid=str(account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(user.nearby_custom_city_enabled),
                auth_source=stored_auth_source,
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
        client = web_user.app.client
        if str(getattr(web_user, "authentication_source", "") or "") == "local":
            client.response_hook = None
            client.reauth_callback = None
            return
        client.response_hook = lambda meta, result: self.capture_upstream_response(
            identity=identity, request_meta=meta, result=result
        )
        client.reauth_callback = lambda: self._reauthenticate(
            identity=identity, app=web_user.app
        )

    def _reauthenticate(
        self,
        *,
        identity: UserIdentity,
        app: ProviderApplication,
    ) -> bool:
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
                password_verified=True,
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
            binding = ExternalAccountRepository(db).get_user_binding(
                state.user_id,
                external_account_id=state.external_account_id,
            )
            if binding is None or binding[0].status != "active":
                return None
            user, account = binding
            return UserIdentity(
                user_id=state.user_id,
                external_account_id=state.external_account_id,
                upstream_uid=str(account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(user.nearby_custom_city_enabled),
                auth_source=str(
                    getattr(state, "auth_source", "provider") or "provider"
                ),
            )

    def revoke_session(self, sid: str, *, reason: str = "logout") -> bool:
        if not sid:
            return False
        with session_scope() as db:
            return UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).revoke(sid, reason=reason)

    def change_local_password(
        self,
        *,
        identity: UserIdentity,
        current_password: str,
        new_password: str,
    ) -> int:
        """Change the Web-local password and revoke every active session.

        The current request is included in the revocation count.  Requiring a
        fresh login after a credential change prevents another browser session
        that already holds a valid cookie from silently surviving the change.
        """

        with session_scope() as db:
            binding = ExternalAccountRepository(db).get_user_binding(
                identity.user_id,
                external_account_id=identity.external_account_id,
                provider=str(self.runtime_provider.provider_id),
                for_update=True,
            )
            if (
                binding is None
                or binding[0].status != "active"
                or str(binding[1].upstream_uid or "") != str(identity.upstream_uid or "")
            ):
                raise PermissionDenied("账号当前不可用")
            UserCredentialService(db).change_local_password(
                user_id=identity.user_id,
                current_password=current_password,
                new_password=new_password,
            )
            return UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).revoke_all_for_user(identity.user_id, reason="password_changed")

    def require_identity(self, sid: str) -> Optional[UserIdentity]:
        if not sid:
            return None
        with session_scope() as db:
            state = UserSessionService(
                db, self.redis, self.settings, self.session_hmac_key
            ).touch(sid)
            if state is None:
                return None
            binding = ExternalAccountRepository(db).get_user_binding(
                state.user_id,
                external_account_id=state.external_account_id,
            )
            if binding is None or binding[0].status != "active":
                return None
            user, account = binding
            return UserIdentity(
                user_id=user.id,
                external_account_id=account.id,
                upstream_uid=str(account.upstream_uid or ""),
                match_pool_online_list_enabled=bool(
                    user.match_pool_online_list_enabled
                ),
                nearby_custom_city_enabled=bool(user.nearby_custom_city_enabled),
                auth_source=str(
                    getattr(state, "auth_source", "provider") or "provider"
                ),
            )

    def grant_message_peers(
        self,
        *,
        identity: UserIdentity,
        peers: list[str] | tuple[str, ...] | set[str],
        kind: str,
        evidence: Mapping[str, Any] | None = None,
        complete_snapshot: bool = False,
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
        if len(normalized) > MESSAGE_PEER_SNAPSHOT_MAX_PEERS:
            raise ValueError("message peer snapshot exceeds the supported limit")
        if complete_snapshot and kind != MESSAGE_POLICY_CONVERSATION_KIND:
            raise ValueError("only message-peer grants support complete snapshots")
        if not normalized and not complete_snapshot:
            return []
        safe_evidence = {
            str(key)[:80]: value
            for key, value in dict(evidence or {}).items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        if complete_snapshot and (
            safe_evidence.get("source_path") != MESSAGE_PEER_SNAPSHOT_PATH
            or safe_evidence.get("grant_reason") != "upstream_conversation"
        ):
            raise ValueError("complete message-peer snapshots require trusted evidence")
        now = utcnow()
        with session_scope() as db:
            existing_query = select(Relationship).where(
                Relationship.owner_user_id == identity.user_id,
                Relationship.provider == MESSAGE_POLICY_PROVIDER,
                Relationship.kind == kind,
            )
            if not complete_snapshot:
                existing_query = existing_query.where(
                    Relationship.subject_upstream_uid.in_(normalized)
                )
            existing_rows = list(
                db.scalars(
                    existing_query.limit(MESSAGE_PEER_SNAPSHOT_MAX_PEERS + 1)
                )
            )
            if len(existing_rows) > MESSAGE_PEER_SNAPSHOT_MAX_PEERS:
                raise ValueError("stored message peer grants exceed the supported limit")
            existing_by_peer = {
                _message_peer_uid(row.subject_upstream_uid): row
                for row in existing_rows
                if _message_peer_uid(row.subject_upstream_uid)
            }
            pending_rows: list[dict[str, Any]] = []
            normalized_set = set(normalized)
            for peer in normalized:
                existing = existing_by_peer.get(peer)
                metadata = dict(existing.extra_data or {}) if existing else {}
                metadata.update(safe_evidence)
                metadata["server_owned"] = True
                if complete_snapshot:
                    metadata.update(
                        {
                            "upstream_conversation_snapshot": True,
                            "upstream_conversation_source_path": (
                                MESSAGE_PEER_SNAPSHOT_PATH
                            ),
                        }
                    )
                if (
                    existing is not None
                    and existing.status == "active"
                    and existing.ended_at is None
                    and dict(existing.extra_data or {}) == metadata
                ):
                    continue
                pending_rows.append(
                    {
                        "owner_user_id": identity.user_id,
                        "provider": MESSAGE_POLICY_PROVIDER,
                        "subject_upstream_uid": peer,
                        "kind": kind,
                        "status": "active",
                        "started_at": existing.started_at if existing else now,
                        "ended_at": None,
                        "extra_data": metadata,
                    }
                )
            if complete_snapshot:
                # A later full trusted snapshot must remove stale membership
                # from the retirement proof without deleting another valid
                # grant reason such as an already-authorized local send.
                for peer, existing in existing_by_peer.items():
                    if peer in normalized_set:
                        continue
                    metadata = dict(existing.extra_data or {})
                    if metadata.get("upstream_conversation_snapshot") is not True:
                        continue
                    metadata["upstream_conversation_snapshot"] = False
                    pending_rows.append(
                        {
                            "owner_user_id": identity.user_id,
                            "provider": MESSAGE_POLICY_PROVIDER,
                            "subject_upstream_uid": peer,
                            "kind": kind,
                            "status": existing.status,
                            "started_at": existing.started_at,
                            "ended_at": existing.ended_at,
                            "extra_data": metadata,
                        }
                    )
            repo = RelationshipRepository(db)
            repo.upsert_many(pending_rows)
            if complete_snapshot:
                SyncCursorRepository(db).upsert(
                    owner_user_id=identity.user_id,
                    source=MESSAGE_PEER_SNAPSHOT_SOURCE,
                    stream=MESSAGE_PEER_SNAPSHOT_STREAM,
                    cursor=json.dumps(
                        {
                            "complete": True,
                            "external_account_id": str(identity.external_account_id),
                            "peer_count": len(normalized_set),
                            "peer_digest": _message_peer_snapshot_digest(
                                normalized_set
                            ),
                            "schema": MESSAGE_PEER_SNAPSHOT_SCHEMA,
                            "source_path": MESSAGE_PEER_SNAPSHOT_PATH,
                            "upstream_uid": _message_peer_uid(
                                identity.upstream_uid
                            ),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    watermark_at=now,
                    next_sync_at=None,
                    last_attempted_at=now,
                    last_succeeded_at=now,
                    last_error=None,
                    version=1,
                )
        return normalized

    def replace_social_message_relationships(
        self,
        *,
        identity: UserIdentity,
        peers: list[str] | tuple[str, ...] | set[str],
        kind: str,
        deactivate_missing: bool = True,
        source_path: str = "",
    ) -> list[str]:
        """Synchronize friend/block relationships used by the message guard.

        These snapshots come from authenticated upstream responses. Persisting
        them before the response reaches the browser avoids a race where a
        freshly rendered friend entry opens successfully but the following
        BFF send is denied because the background ingestion job has not run.
        """

        if kind not in {
            SOCIAL_FRIEND_KIND,
            SOCIAL_BLACKLIST_KIND,
            SOCIAL_BLACKLISTED_BY_KIND,
        }:
            raise ValueError("unsupported social message relationship kind")
        normalized = list(
            dict.fromkeys(
                peer
                for peer in (_message_peer_uid(value) for value in peers)
                if peer and peer != _message_peer_uid(identity.upstream_uid)
            )
        )
        now = utcnow()
        if not normalized and not deactivate_missing:
            return []
        with session_scope() as db:
            existing_query = select(Relationship).where(
                Relationship.owner_user_id == identity.user_id,
                Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                Relationship.kind == kind,
            )
            if not deactivate_missing:
                existing_query = existing_query.where(
                    Relationship.subject_upstream_uid.in_(normalized)
                )
            existing_rows = list(
                db.scalars(existing_query)
            )
            existing_by_peer = {
                _message_peer_uid(row.subject_upstream_uid): row
                for row in existing_rows
                if _message_peer_uid(row.subject_upstream_uid)
            }
            pending_rows: list[dict[str, Any]] = []
            for peer in normalized:
                existing = existing_by_peer.get(peer)
                metadata = dict(existing.extra_data or {}) if existing else {}
                metadata.update(
                    {
                        "server_owned": True,
                        "message_policy_source": source_path or "social_snapshot",
                    }
                )
                if (
                    existing is not None
                    and existing.status == "active"
                    and existing.ended_at is None
                    and dict(existing.extra_data or {}) == metadata
                ):
                    continue
                pending_rows.append(
                    {
                        "owner_user_id": identity.user_id,
                        "provider": SOCIAL_RELATIONSHIP_PROVIDER,
                        "subject_upstream_uid": peer,
                        "kind": kind,
                        "status": "active",
                        "started_at": existing.started_at if existing else now,
                        "ended_at": None,
                        "extra_data": metadata,
                    }
                )
            if deactivate_missing:
                normalized_set = set(normalized)
                for peer, existing in existing_by_peer.items():
                    if peer in normalized_set or existing.status != "active":
                        continue
                    metadata = dict(existing.extra_data or {})
                    metadata.update(
                        {
                            "server_owned": True,
                            "message_policy_source": source_path or "social_snapshot",
                        }
                    )
                    pending_rows.append(
                        {
                            "owner_user_id": identity.user_id,
                            "provider": SOCIAL_RELATIONSHIP_PROVIDER,
                            "subject_upstream_uid": peer,
                            "kind": kind,
                            "status": "inactive",
                            "started_at": existing.started_at,
                            "ended_at": now,
                            "extra_data": metadata,
                        }
                    )
            RelationshipRepository(db).upsert_many(pending_rows)
            expected_snapshot_path = MESSAGE_BLOCK_SNAPSHOT_PATHS.get(kind)
            if (
                deactivate_missing
                and expected_snapshot_path is not None
                and source_path == expected_snapshot_path
            ):
                # A relationship row cannot prove that a successfully fetched
                # blacklist was empty.  Record completion in the existing
                # durable cursor table in the same transaction as the rows so
                # session recovery never has to guess from row presence.
                SyncCursorRepository(db).upsert(
                    owner_user_id=identity.user_id,
                    source=MESSAGE_BLOCK_SNAPSHOT_SOURCE,
                    stream=MESSAGE_BLOCK_SNAPSHOT_STREAMS[kind],
                    cursor=json.dumps(
                        {
                            "complete": True,
                            "external_account_id": str(identity.external_account_id),
                            "kind": kind,
                            "schema": MESSAGE_BLOCK_SNAPSHOT_SCHEMA,
                            "source_path": source_path,
                            "upstream_uid": _message_peer_uid(identity.upstream_uid),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    watermark_at=now,
                    next_sync_at=None,
                    last_attempted_at=now,
                    last_succeeded_at=now,
                    last_error=None,
                    version=1,
                )
        return normalized

    def trusted_message_block_snapshot(
        self,
        identity: UserIdentity,
    ) -> Optional[dict[str, list[str]]]:
        """Restore both complete block directions from trusted durable state.

        A direction is usable only after a successful full snapshot wrote its
        dedicated ``SyncCursor`` marker.  Old relationship rows without those
        markers, partial pages, failed writes and browser-derived rows are not
        treated as proof of a complete blacklist.  ``None`` therefore means
        fail closed or refresh from the authenticated upstream; a dictionary
        with two empty lists is a valid, previously synchronized snapshot.
        """

        streams = set(MESSAGE_BLOCK_SNAPSHOT_STREAMS.values())
        own_peer = _message_peer_uid(identity.upstream_uid)
        if not own_peer:
            return None
        with session_scope() as db:
            cursor_rows = list(
                db.scalars(
                    select(SyncCursor).where(
                        SyncCursor.owner_user_id == identity.user_id,
                        SyncCursor.source == MESSAGE_BLOCK_SNAPSHOT_SOURCE,
                        SyncCursor.stream.in_(streams),
                    )
                )
            )
            cursors_by_stream = {str(row.stream or ""): row for row in cursor_rows}
            for kind in SOCIAL_MESSAGE_BLOCK_KINDS:
                stream = MESSAGE_BLOCK_SNAPSHOT_STREAMS[kind]
                cursor_row = cursors_by_stream.get(stream)
                if (
                    cursor_row is None
                    or cursor_row.last_succeeded_at is None
                    or cursor_row.watermark_at is None
                    or bool(str(cursor_row.last_error or "").strip())
                ):
                    return None
                try:
                    marker = json.loads(str(cursor_row.cursor or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
                if not isinstance(marker, Mapping) or marker.get("complete") is not True:
                    return None
                if (
                    marker.get("schema") != MESSAGE_BLOCK_SNAPSHOT_SCHEMA
                    or marker.get("external_account_id")
                    != str(identity.external_account_id)
                    or marker.get("kind") != kind
                    or marker.get("source_path") != MESSAGE_BLOCK_SNAPSHOT_PATHS[kind]
                    or marker.get("upstream_uid") != own_peer
                ):
                    return None

            relationship_rows = list(
                db.scalars(
                    select(Relationship)
                    .where(
                        Relationship.owner_user_id == identity.user_id,
                        Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                        Relationship.kind.in_(SOCIAL_MESSAGE_BLOCK_KINDS),
                        Relationship.status == "active",
                        Relationship.ended_at.is_(None),
                    )
                    .limit(MESSAGE_BLOCK_SNAPSHOT_MAX_PEERS + 1)
                )
            )
        if len(relationship_rows) > MESSAGE_BLOCK_SNAPSHOT_MAX_PEERS:
            return None

        snapshot = {
            SOCIAL_BLACKLIST_KIND: [],
            SOCIAL_BLACKLISTED_BY_KIND: [],
        }
        for row in relationship_rows:
            kind = str(row.kind or "")
            metadata = dict(row.extra_data or {})
            peer = _message_peer_uid(row.subject_upstream_uid)
            if (
                kind not in snapshot
                or not peer
                or peer == own_peer
                or metadata.get("server_owned") is not True
                or metadata.get("message_policy_source")
                not in MESSAGE_BLOCK_TRUSTED_SOURCES[kind]
            ):
                return None
            snapshot[kind].append(peer)
        snapshot[SOCIAL_BLACKLIST_KIND] = sorted(
            set(snapshot[SOCIAL_BLACKLIST_KIND])
        )
        snapshot[SOCIAL_BLACKLISTED_BY_KIND] = sorted(
            set(snapshot[SOCIAL_BLACKLISTED_BY_KIND])
        )
        return snapshot

    def set_social_message_relationship(
        self,
        *,
        identity: UserIdentity,
        peer: Any,
        kind: str,
        active: bool,
        source_path: str = "",
    ) -> list[str]:
        """Apply one successful friend/block mutation synchronously."""

        if kind not in {SOCIAL_FRIEND_KIND, SOCIAL_BLACKLIST_KIND}:
            raise ValueError("unsupported social message relationship kind")
        target = _message_peer_uid(peer)
        if not target or target == _message_peer_uid(identity.upstream_uid):
            return []
        now = utcnow()
        with session_scope() as db:
            existing = db.scalar(
                select(Relationship).where(
                    Relationship.owner_user_id == identity.user_id,
                    Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == target,
                    Relationship.kind == kind,
                )
            )
            if existing is None and not active:
                return [target]
            metadata = dict(existing.extra_data or {}) if existing else {}
            metadata.update(
                {
                    "server_owned": True,
                    "message_policy_source": source_path or "social_action",
                }
            )
            RelationshipRepository(db).upsert(
                owner_user_id=identity.user_id,
                provider=SOCIAL_RELATIONSHIP_PROVIDER,
                subject_upstream_uid=target,
                kind=kind,
                status="active" if active else "inactive",
                started_at=existing.started_at if existing else now,
                ended_at=None if active else now,
                extra_data=metadata,
            )
        return [target]

    def send_local_text_message(
        self,
        *,
        identity: UserIdentity,
        peer: Any,
        client_message_id: Any,
        text_value: Any,
        quote: Any = None,
        expected_source_message_identity: Any = "",
        db: Any | None = None,
    ) -> Any:
        """Write one Web-to-Web text message in the caller's transaction."""

        from bbw_web.message_quote import normalize_message_quote
        from bbw_web.messaging import LocalMessagingService, LocalPrincipal
        from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore

        owns_transaction = db is None
        with (session_scope() if owns_transaction else nullcontext(db)) as action_db:
            result = LocalMessagingService(
                SqlAlchemyCanonicalMessageStore(
                    action_db,
                    compatibility_mode=str(
                        getattr(self.settings, "compatibility_mode", "enabled")
                        or "enabled"
                    ),
                )
            ).send_text(
                principal=LocalPrincipal(
                    user_id=identity.user_id,
                    external_account_id=identity.external_account_id,
                    upstream_uid=identity.upstream_uid,
                    account_provider=str(self.runtime_provider.provider_id),
                ),
                peer_upstream_uid=peer,
                client_message_id=client_message_id,
                text=text_value,
                quote=normalize_message_quote(quote),
                expected_source_message_identity=expected_source_message_identity,
            )

        # A caller-owned transaction has not committed yet.  The durable mirror
        # scheduler will enqueue the pending delivery after commit, avoiding a
        # worker racing ahead of the authoritative message transaction.
        if not owns_transaction:
            return result

        # The required local delivery is committed. Queueing the optional
        # legacy mirror must never turn that success into a failed Web message;
        # the scheduler also redispatches due rows after enqueue loss.
        if (
            str(result.tim_mirror.status or "") == "pending"
            and str(getattr(self.settings, "compatibility_mode", "enabled"))
            == "enabled"
        ):
            try:
                self.default_queue.enqueue(
                    "bbw_web.jobs.mirror_tim_message_delivery",
                    str(result.tim_mirror.delivery_id),
                    job_id=f"mirror-tim-message-{result.tim_mirror.delivery_id}",
                    job_timeout=60,
                    result_ttl=300,
                    failure_ttl=86400,
                )
            except InvalidJobOperation:
                pass
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    LOGGER.warning(
                        "TIM mirror enqueue failed; delivery_id=%s",
                        result.tim_mirror.delivery_id,
                    )
        return result

    def mark_local_conversation_read(
        self,
        *,
        identity: UserIdentity,
        peer: Any,
    ) -> int:
        """Mark the canonical local thread read without contacting TIM."""

        from bbw_web.messaging import LocalMessagingService, LocalPrincipal
        from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore

        with session_scope() as db:
            return LocalMessagingService(
                SqlAlchemyCanonicalMessageStore(db)
            ).mark_direct_read(
                principal=LocalPrincipal(
                    user_id=identity.user_id,
                    external_account_id=identity.external_account_id,
                    upstream_uid=identity.upstream_uid,
                    account_provider=str(self.runtime_provider.provider_id),
                ),
                peer_upstream_uid=peer,
            )

    def revoke_local_text_message(
        self,
        *,
        identity: UserIdentity,
        peer: Any,
        canonical_message_id: Any,
        db: Any | None = None,
    ) -> Any:
        """Revoke one canonical Web text message without depending on TIM."""

        from bbw_web.messaging import LocalMessagingService, LocalPrincipal
        from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore

        owns_transaction = db is None
        with (session_scope() if owns_transaction else nullcontext(db)) as action_db:
            result = LocalMessagingService(
                SqlAlchemyCanonicalMessageStore(
                    action_db,
                    compatibility_mode=str(
                        getattr(self.settings, "compatibility_mode", "enabled")
                        or "enabled"
                    ),
                )
            ).revoke_text(
                principal=LocalPrincipal(
                    user_id=identity.user_id,
                    external_account_id=identity.external_account_id,
                    upstream_uid=identity.upstream_uid,
                    account_provider=str(self.runtime_provider.provider_id),
                ),
                peer_upstream_uid=peer,
                canonical_message_id=canonical_message_id,
            )

        if not owns_transaction or result.tim_mirror is None:
            return result
        if (
            str(result.tim_mirror.status or "") == "pending"
            and str(getattr(self.settings, "compatibility_mode", "enabled"))
            == "enabled"
        ):
            try:
                self.default_queue.enqueue(
                    "bbw_web.jobs.mirror_tim_message_delivery",
                    str(result.tim_mirror.delivery_id),
                    job_id=f"mirror-tim-message-{result.tim_mirror.delivery_id}",
                    job_timeout=60,
                    result_ttl=300,
                    failure_ttl=86400,
                )
            except InvalidJobOperation:
                pass
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    LOGGER.warning(
                        "TIM text revoke enqueue failed; delivery_id=%s",
                        result.tim_mirror.delivery_id,
                    )
        return result

    def can_message_peer(self, identity: UserIdentity, peer: Any) -> bool:
        """Authorize one private-message target from live durable state."""

        target = _message_peer_uid(peer)
        if not target or target == _message_peer_uid(identity.upstream_uid):
            return False
        with session_scope() as db:
            peer_user_id = (
                select(ExternalAccount.user_id)
                .where(
                    ExternalAccount.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                    ExternalAccount.upstream_uid == target,
                )
                .scalar_subquery()
            )
            return bool(
                db.scalar(
                    private_message_permission_query(
                        sender_user_id=identity.user_id,
                        sender_upstream_uid=_message_peer_uid(
                            identity.upstream_uid
                        ),
                        recipient_user_id=peer_user_id,
                        recipient_upstream_uid=target,
                        proactive_private_message=bool(
                            identity.match_pool_online_list_enabled
                        ),
                    )
                )
            )

    def message_policy_snapshot(
        self, identity: UserIdentity, *, limit: int = 2000
    ) -> dict[str, list[str]]:
        """Load all durable private-message policy lists in one DB round trip."""

        bounded_limit = max(1, min(int(limit), 5000))
        own_peer = _message_peer_uid(identity.upstream_uid)
        outgoing_block_override = aliased(
            Relationship, name="snapshot_outgoing_block_override"
        )
        incoming_block_override = aliased(
            Relationship, name="snapshot_incoming_block_override"
        )
        incoming_block_account = aliased(
            ExternalAccount, name="snapshot_incoming_block_account"
        )
        outgoing_has_local_block = (
            select(outgoing_block_override.id)
            .where(
                outgoing_block_override.owner_user_id == identity.user_id,
                outgoing_block_override.provider == SOCIAL_CANONICAL_PROVIDER,
                outgoing_block_override.subject_upstream_uid
                == Relationship.subject_upstream_uid,
                outgoing_block_override.kind == SOCIAL_BLACKLIST_KIND,
            )
            .exists()
        )
        incoming_has_local_block = (
            select(incoming_block_override.id)
            .join(
                incoming_block_account,
                incoming_block_account.user_id
                == incoming_block_override.owner_user_id,
            )
            .where(
                incoming_block_account.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                incoming_block_account.upstream_uid
                == Relationship.subject_upstream_uid,
                incoming_block_override.provider == SOCIAL_CANONICAL_PROVIDER,
                incoming_block_override.subject_upstream_uid == own_peer,
                incoming_block_override.kind == SOCIAL_BLACKLIST_KIND,
            )
            .exists()
        )
        owner_block_candidates = select(
            Relationship.subject_upstream_uid.label("peer"),
            Relationship.updated_at.label("observed_at"),
        ).where(
            Relationship.owner_user_id == identity.user_id,
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
            or_(
                and_(
                    Relationship.provider == SOCIAL_CANONICAL_PROVIDER,
                    Relationship.kind == SOCIAL_BLACKLIST_KIND,
                ),
                and_(
                    Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                    Relationship.kind == SOCIAL_BLACKLIST_KIND,
                    ~outgoing_has_local_block,
                ),
                and_(
                    Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                    Relationship.kind == SOCIAL_BLACKLISTED_BY_KIND,
                    ~incoming_has_local_block,
                ),
            ),
        )
        incoming_local_block_candidates = (
            select(
                ExternalAccount.upstream_uid.label("peer"),
                Relationship.updated_at.label("observed_at"),
            )
            .join(
                ExternalAccount,
                ExternalAccount.user_id == Relationship.owner_user_id,
            )
            .where(
                Relationship.provider == SOCIAL_CANONICAL_PROVIDER,
                Relationship.kind == SOCIAL_BLACKLIST_KIND,
                Relationship.subject_upstream_uid == own_peer,
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
                ExternalAccount.provider == SOCIAL_RELATIONSHIP_PROVIDER,
            )
        )
        blocked_candidates = union_all(
            owner_block_candidates,
            incoming_local_block_candidates,
        ).cte("message_policy_blocked_candidates")
        blocked_latest = (
            select(
                blocked_candidates.c.peer,
                func.max(blocked_candidates.c.observed_at).label("observed_at"),
            )
            .where(blocked_candidates.c.peer.is_not(None))
            .group_by(blocked_candidates.c.peer)
            .cte("message_policy_blocked_latest")
        )
        blocked_peer_ids = select(blocked_latest.c.peer)
        snapshot_friend_override = aliased(
            Relationship, name="snapshot_friend_override"
        )
        snapshot_peer_friend_tombstone = aliased(
            Relationship, name="snapshot_peer_friend_tombstone"
        )
        snapshot_peer_friend_account = aliased(
            ExternalAccount, name="snapshot_peer_friend_account"
        )
        snapshot_has_local_friend = (
            select(snapshot_friend_override.id)
            .where(
                snapshot_friend_override.owner_user_id == identity.user_id,
                snapshot_friend_override.provider == SOCIAL_CANONICAL_PROVIDER,
                snapshot_friend_override.subject_upstream_uid
                == Relationship.subject_upstream_uid,
                snapshot_friend_override.kind == SOCIAL_FRIEND_KIND,
            )
            .exists()
        )
        snapshot_peer_has_friend_tombstone = (
            select(snapshot_peer_friend_tombstone.id)
            .join(
                snapshot_peer_friend_account,
                snapshot_peer_friend_account.user_id
                == snapshot_peer_friend_tombstone.owner_user_id,
            )
            .where(
                snapshot_peer_friend_account.provider
                == SOCIAL_RELATIONSHIP_PROVIDER,
                snapshot_peer_friend_account.upstream_uid
                == Relationship.subject_upstream_uid,
                snapshot_peer_friend_tombstone.provider
                == SOCIAL_CANONICAL_PROVIDER,
                snapshot_peer_friend_tombstone.subject_upstream_uid == own_peer,
                snapshot_peer_friend_tombstone.kind == SOCIAL_FRIEND_KIND,
                or_(
                    snapshot_peer_friend_tombstone.status != "active",
                    snapshot_peer_friend_tombstone.ended_at.is_not(None),
                ),
            )
            .exists()
        )
        relationship_allowed = select(
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
                    Relationship.kind == SOCIAL_FRIEND_KIND,
                    or_(
                        and_(
                            Relationship.provider == SOCIAL_CANONICAL_PROVIDER,
                            ~snapshot_peer_has_friend_tombstone,
                        ),
                        and_(
                            Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                            ~snapshot_has_local_friend,
                            ~snapshot_peer_has_friend_tombstone,
                        ),
                    ),
                ),
            ),
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
        )
        reverse_friend_account = aliased(
            ExternalAccount, name="snapshot_reverse_friend_account"
        )
        reverse_own_friend_override = aliased(
            Relationship, name="snapshot_reverse_own_friend_override"
        )
        reverse_peer_friend_tombstone = aliased(
            Relationship, name="snapshot_reverse_peer_friend_tombstone"
        )
        reverse_has_own_override = (
            select(reverse_own_friend_override.id)
            .where(
                reverse_own_friend_override.owner_user_id == identity.user_id,
                reverse_own_friend_override.provider == SOCIAL_CANONICAL_PROVIDER,
                reverse_own_friend_override.subject_upstream_uid
                == reverse_friend_account.upstream_uid,
                reverse_own_friend_override.kind == SOCIAL_FRIEND_KIND,
            )
            .exists()
        )
        reverse_has_peer_tombstone = (
            select(reverse_peer_friend_tombstone.id)
            .where(
                reverse_peer_friend_tombstone.owner_user_id
                == Relationship.owner_user_id,
                reverse_peer_friend_tombstone.provider
                == SOCIAL_CANONICAL_PROVIDER,
                reverse_peer_friend_tombstone.subject_upstream_uid == own_peer,
                reverse_peer_friend_tombstone.kind == SOCIAL_FRIEND_KIND,
                or_(
                    reverse_peer_friend_tombstone.status != "active",
                    reverse_peer_friend_tombstone.ended_at.is_not(None),
                ),
            )
            .exists()
        )
        reverse_legacy_friend_allowed = (
            select(
                reverse_friend_account.upstream_uid.label("peer"),
                Relationship.updated_at.label("observed_at"),
            )
            .join(
                reverse_friend_account,
                reverse_friend_account.user_id == Relationship.owner_user_id,
            )
            .where(
                Relationship.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                Relationship.kind == SOCIAL_FRIEND_KIND,
                Relationship.subject_upstream_uid == own_peer,
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
                reverse_friend_account.provider == SOCIAL_RELATIONSHIP_PROVIDER,
                ~reverse_has_own_override,
                ~reverse_has_peer_tombstone,
            )
        )
        conversation_allowed = select(
            Conversation.peer_upstream_uid.label("peer"),
            Conversation.updated_at.label("observed_at"),
        ).where(
            Conversation.owner_user_id == identity.user_id,
            # TIM compatibility conversations may originate from untrusted
            # browser archive reports.  Trusted upstream synchronization is
            # represented by a server-owned web-policy/message_peer grant;
            # only canonical local conversations are authoritative directly.
            Conversation.provider == "web-local",
            Conversation.kind == "direct",
            Conversation.peer_upstream_uid.is_not(None),
        )
        allowed_candidates = union_all(
            relationship_allowed,
            reverse_legacy_friend_allowed,
            conversation_allowed,
        ).cte("message_policy_allowed_candidates")
        allowed_latest = (
            select(
                allowed_candidates.c.peer,
                func.max(allowed_candidates.c.observed_at).label("observed_at"),
            )
            .where(allowed_candidates.c.peer.is_not(None))
            .group_by(allowed_candidates.c.peer)
            .cte("message_policy_allowed_latest")
        )
        allowed_limited = (
            select(allowed_latest.c.peer, allowed_latest.c.observed_at)
            .where(
                ~allowed_latest.c.peer.in_(blocked_peer_ids),
                allowed_latest.c.peer != own_peer,
            )
            .order_by(allowed_latest.c.observed_at.desc())
            .limit(bounded_limit)
            .cte("message_policy_allowed_limited")
        )
        match_latest = (
            select(
                Relationship.subject_upstream_uid.label("peer"),
                func.max(Relationship.updated_at).label("observed_at"),
            )
            .where(
                Relationship.owner_user_id == identity.user_id,
                Relationship.provider == MESSAGE_POLICY_PROVIDER,
                Relationship.kind == MESSAGE_POLICY_MATCH_KIND,
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
            )
            .group_by(Relationship.subject_upstream_uid)
            .cte("message_policy_match_latest")
        )
        match_limited = (
            select(match_latest.c.peer, match_latest.c.observed_at)
            .where(
                ~match_latest.c.peer.in_(blocked_peer_ids),
                match_latest.c.peer != own_peer,
            )
            .order_by(match_latest.c.observed_at.desc())
            .limit(bounded_limit)
            .cte("message_policy_match_limited")
        )
        blocked_limited = (
            select(blocked_latest.c.peer, blocked_latest.c.observed_at)
            .where(blocked_latest.c.peer != own_peer)
            .order_by(blocked_latest.c.observed_at.desc())
            .limit(bounded_limit)
            .cte("message_policy_blocked_limited")
        )
        snapshot_rows = union_all(
            select(
                literal("allowed_peers").label("category"),
                allowed_limited.c.peer,
                allowed_limited.c.observed_at,
            ),
            select(
                literal("match_peers").label("category"),
                match_limited.c.peer,
                match_limited.c.observed_at,
            ),
            select(
                literal("blocked_peers").label("category"),
                blocked_limited.c.peer,
                blocked_limited.c.observed_at,
            ),
        ).subquery()
        statement = select(snapshot_rows.c.category, snapshot_rows.c.peer).order_by(
            snapshot_rows.c.category,
            snapshot_rows.c.observed_at.desc(),
        )
        snapshot = {
            "allowed_peers": [],
            "match_peers": [],
            "blocked_peers": [],
        }
        with session_scope() as db:
            rows = db.execute(statement)
            for category, value in rows:
                peer = _message_peer_uid(value)
                if peer and peer != own_peer and category in snapshot:
                    snapshot[category].append(peer)
        blocked_peers = set(snapshot["blocked_peers"])
        snapshot["match_peers"] = [
            peer for peer in snapshot["match_peers"] if peer not in blocked_peers
        ]
        snapshot["allowed_peers"] = [
            peer for peer in snapshot["allowed_peers"] if peer not in blocked_peers
        ]
        return snapshot

    def message_policy_allowed_peers(
        self, identity: UserIdentity, *, limit: int = 2000
    ) -> list[str]:
        return self.message_policy_snapshot(identity, limit=limit)["allowed_peers"]

    def message_policy_match_peers(
        self, identity: UserIdentity, *, limit: int = 2000
    ) -> list[str]:
        return self.message_policy_snapshot(identity, limit=limit)["match_peers"]

    def message_policy_blocked_peers(
        self, identity: UserIdentity, *, limit: int = 2000
    ) -> list[str]:
        return self.message_policy_snapshot(identity, limit=limit)["blocked_peers"]

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
        if method_upper == "GET" and path in {
            "/api/social/friends",
            "/api/social/blacklist",
            "/api/social/blacklist-me",
        }:
            if not _response_has_item_list(response_data):
                return []
            kind = {
                "/api/social/friends": SOCIAL_FRIEND_KIND,
                "/api/social/blacklist": SOCIAL_BLACKLIST_KIND,
                "/api/social/blacklist-me": SOCIAL_BLACKLISTED_BY_KIND,
            }[path]
            peers = [_item_peer_uid(item) for item in _response_items(response_data)]
            return self.replace_social_message_relationships(
                identity=identity,
                peers=peers,
                kind=kind,
                deactivate_missing=response_data.get("has_more") is not True,
                source_path=path,
            )
        social_actions = {
            "/api/social/agree-friend": (SOCIAL_FRIEND_KIND, True),
            "/api/social/delete-friend": (SOCIAL_FRIEND_KIND, False),
            "/api/social/blacklist-add": (SOCIAL_BLACKLIST_KIND, True),
            "/api/social/blacklist-del": (SOCIAL_BLACKLIST_KIND, False),
        }
        if method_upper == "POST" and path in social_actions:
            kind, active = social_actions[path]
            peer = _item_peer_uid(request_data) or _item_peer_uid(response_data)
            return self.set_social_message_relationship(
                identity=identity,
                peer=peer,
                kind=kind,
                active=active,
                source_path=path,
            )
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
            if not _response_has_item_list(response_data):
                return []
            peers = [_item_peer_uid(item) for item in _response_items(response_data)]
            return self.grant_message_peers(
                identity=identity,
                peers=peers,
                kind=MESSAGE_POLICY_CONVERSATION_KIND,
                evidence={
                    "source_path": path,
                    "grant_reason": "upstream_conversation",
                },
                complete_snapshot=response_data.get("snapshot_complete") is True,
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
            conversations_with_latest = ConversationRepository(
                db
            ).list_for_peers_with_latest(
                identity.user_id,
                targets,
            )
            summaries: dict[str, dict[str, Any]] = {}
            for conversation, message in conversations_with_latest:
                peer = str(conversation.peer_upstream_uid or "").strip()
                if not peer:
                    continue
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

    def enqueue_message_archive_batch(
        self,
        *,
        identity: UserIdentity,
        payloads: list[dict[str, Any]],
        client_ip: str,
    ) -> bool:
        if not payloads:
            return False
        idempotency_keys = sorted(
            str(payload.get("idempotency_key") or "") for payload in payloads
        )
        digest = hashlib.sha256(
            "\n".join(idempotency_keys).encode("utf-8")
        ).hexdigest()
        job_id = f"archive-message-batch-{identity.user_id}-{digest}"
        try:
            self.default_queue.enqueue(
                "bbw_web.jobs.archive_message_batch_job",
                str(identity.user_id),
                str(identity.external_account_id),
                payloads,
                job_id=job_id,
                job_timeout=180,
                result_ttl=600,
                failure_ttl=7 * 86400,
            )
            return True
        except InvalidJobOperation:
            return False
        except Exception as exc:
            if "already exists" in str(exc).lower():
                return False
            raise

    def capture_upstream_response(
        self,
        *,
        identity: UserIdentity,
        request_meta: dict[str, Any],
        result: ProviderApiResult,
    ) -> None:
        self._observe_provider_response(result)
        pending = getattr(self, "_raw_response_queue", None)
        thread = getattr(self, "_raw_response_thread", None)
        if (
            pending is None
            or not getattr(self, "_raw_response_accepting", False)
            or thread is None
            or not thread.is_alive()
        ):
            self._raw_response_dropped = getattr(self, "_raw_response_dropped", 0) + 1
            return
        item = RawResponseArchiveItem(
            owner_user_id=identity.user_id,
            endpoint=str(request_meta.get("url") or "")[:512],
            request_meta=dict(request_meta),
            raw_response=str(result.raw or "")[: 1024 * 1024],
            code=str(result.code or "")[:256],
            message=str(result.message or "")[:2000],
            kind=str(result.kind or "")[:64],
            http_status=int(result.status or 0),
        )
        try:
            pending.put_nowait(item)
        except queue_module.Full:
            self._raw_response_dropped += 1
            if self._raw_response_dropped == 1 or self._raw_response_dropped % 100 == 0:
                LOGGER.warning(
                    "raw response archive queue full; dropped=%d queue_max=%d",
                    self._raw_response_dropped,
                    self.RAW_RESPONSE_QUEUE_MAX,
                )

    def _observe_provider_response(self, result: ProviderApiResult) -> None:
        """Record coarse provider reachability without retaining response data."""

        registry = getattr(self, "dependency_status", None)
        provider = str(
            getattr(getattr(self, "runtime_provider", None), "provider_id", "") or ""
        )
        if not isinstance(registry, DependencyStatusRegistry) or not provider:
            return
        try:
            status = int(getattr(result, "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if status <= 0:
            registry.mark_unavailable(
                provider,
                "api",
                failure=DependencyFailure(
                    DependencyErrorKind.CONNECTION,
                    retryable=True,
                ),
            )
        elif status == 429:
            registry.mark_degraded(
                provider,
                "api",
                failure=DependencyFailure(
                    DependencyErrorKind.RATE_LIMITED,
                    retryable=True,
                    status_code=status,
                ),
            )
        elif status >= 500:
            registry.mark_unavailable(
                provider,
                "api",
                failure=DependencyFailure(
                    DependencyErrorKind.UPSTREAM,
                    retryable=True,
                    status_code=status,
                ),
            )
        else:
            # Business rejections still prove that the provider transport and
            # response contract are reachable; account-level authorization is
            # handled separately and must not poison global dependency health.
            registry.mark_available(provider, "api")

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
            digest = _history_response_digest(path, query, response_data)
            if self._claim_response_digest("history", identity, digest):
                self._queue_history_response(
                    identity=identity,
                    path=path,
                    query=query,
                    response_data=response_data,
                    digest=digest,
                )
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
                self._queue_product_event(
                    identity=identity,
                    payload=event_payload,
                    digest=digest,
                )
