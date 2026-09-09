"""Production ASGI entry point for the Web BFF.

The existing product routes still live in :mod:`bbw_web.bff_server`.  This
module adapts the tested ``BaseHTTPRequestHandler`` implementation to FastAPI
without opening a second listener, then adds the production-only persistence,
administration and archive APIs around it.

Run with::

    uvicorn bbw_web.api:app --host 0.0.0.0 --port 8000 --workers 1

Only one worker is used on the 2 GiB deployment profile. Authenticated browser
sessions are kept in Redis/PostgreSQL; the short two-stage login window also
holds an upstream runtime in that worker, so multi-worker deployments must add
sticky routing or externalize that pending runtime before scaling out.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from bbw_web import bff_server as legacy
from bbw_web.store import SessionStore, close_web_runtime


STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGGER = logging.getLogger(__name__)
SLOW_HTTP_REQUEST_MS = 1000.0
LOGIN_START_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/sms-login",
        "/api/auth/sms-send",
        "/api/admin/user-login",
    }
)
APK_DISCOVERY_ROUTE_PATHS = frozenset(
    {
        "/api/match/status",
        "/api/match/online",
        "/api/match/local",
        "/api/match/online-users",
        "/api/match/nearby-users",
    }
)
APK_SOCIAL_ROUTE_PATHS = frozenset(
    {
        "/api/profile/me",
        "/api/profile/user",
        "/api/profile/users",
        "/api/profile/nick",
        "/api/profile/reset",
        "/api/profile/privacy",
        "/api/social/follows",
        "/api/social/fans",
        "/api/social/friends",
        "/api/social/friend-apply",
        "/api/social/blacklist",
        "/api/social/blacklist-me",
        "/api/social/visitors",
        "/api/social/follow",
        "/api/social/unfollow",
        "/api/social/add-friend",
        "/api/social/agree-friend",
        "/api/social/delete-friend",
        "/api/social/blacklist-add",
        "/api/social/blacklist-del",
        "/api/social/visit",
    }
)
LOCAL_PASSWORD_AUTH_RETRY_AFTER_SECONDS = 1
_LOCAL_PASSWORD_AUTH_GATE_INIT_LOCK = threading.Lock()

HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://challenges.cloudflare.com; "
    "style-src 'self'; img-src 'self' data: blob: https:; "
    "media-src 'self' data: blob: https:; font-src 'self' data:; "
    "connect-src 'self' https: wss:; "
    "frame-src 'self' https://challenges.cloudflare.com; "
    "worker-src 'self' blob:; child-src 'self' blob:; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'; upgrade-insecure-requests"
)


class _LocalPasswordAuthGate:
    """Process-local, non-queueing bound for memory-hard password checks."""

    def __init__(self, capacity: int) -> None:
        normalized = int(capacity)
        if normalized < 1 or normalized > 8:
            raise ValueError("local password auth concurrency must be between 1 and 8")
        self.capacity = normalized
        self._slots = threading.BoundedSemaphore(normalized)

    @contextmanager
    def claim(self) -> Iterator[bool]:
        acquired = self._slots.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._slots.release()


def _local_password_auth_gate(request: Request) -> _LocalPasswordAuthGate:
    """Return this worker's Argon2 admission gate for local password checks.

    bbw_prod 侧另有独立的机会式凭据登记闸门（容量为本闸门配置的一半），
    同进程 Argon2 峰值并发为两闸门容量之和。
    """

    state = request.app.state
    gate = getattr(state, "local_password_auth_gate", None)
    if gate is not None:
        return gate
    with _LOCAL_PASSWORD_AUTH_GATE_INIT_LOCK:
        gate = getattr(state, "local_password_auth_gate", None)
        if gate is None:
            capacity = int(
                getattr(state.settings, "local_password_auth_concurrency", 2)
            )
            gate = _LocalPasswordAuthGate(capacity)
            state.local_password_auth_gate = gate
    return gate


def _use_native_discovery_route(
    path: str,
    method: str,
    native_paths: Iterable[str],
) -> bool:
    """Product discovery and matching are always dispatched to the APK BFF."""

    del path, method, native_paths
    return False


def _native_profile_avatar_reset(
    path: str,
    request_data: Mapping[str, Any],
) -> bool:
    if path != "/api/profile/reset":
        return False
    if request_data.get("avatar_asset_id") in (None, ""):
        return False
    field = str(
        request_data.get("field")
        or request_data.get("type")
        or request_data.get("type_")
        or ""
    ).strip()
    return field in {"", "avatar", "头像", "头像设置"}


def _use_native_social_route(
    path: str,
    method: str,
    native_paths: Iterable[str],
    request_data: Mapping[str, Any],
) -> bool:
    """Product profile and relationship routes never use local authority."""

    del path, method, native_paths, request_data
    return False


class RequestBodyLimitMiddleware:
    """Bound declared and chunked bodies before FastAPI/Pydantic parse them."""

    def __init__(self, application: Any) -> None:
        self.application = application
        self._slots = asyncio.Semaphore(8)

    @staticmethod
    async def _reject(send: Any, status_code: int, message: str) -> None:
        body = json.dumps(
            {"ok": False, "error": message}, ensure_ascii=False
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") not in {
            "POST",
            "PUT",
            "PATCH",
        }:
            await self.application(scope, receive, send)
            return

        content_type = next(
            (
                value.decode("latin-1").lower()
                for key, value in scope.get("headers", [])
                if key.decode("latin-1").lower() == "content-type"
            ),
            "",
        )
        if "application/json" in content_type:
            async with self._slots:
                replay = await self._buffer_limited(scope, receive, send)
            if replay is not None:
                # Small JSON commands must not keep a global slot occupied
                # while a legacy provider times out. Multipart/large bodies
                # remain bounded for the full request below.
                await self.application(scope, replay, send)
            return

        async with self._slots:
            replay = await self._buffer_limited(scope, receive, send)
            if replay is not None:
                await self.application(scope, replay, send)

    async def _buffer_limited(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> Any | None:

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        scope_state = getattr(scope.get("app"), "state", None)
        settings = getattr(scope_state, "settings", None)
        max_request = int(getattr(settings, "max_request_body_bytes", 32 * 1024 * 1024))
        max_json = int(getattr(settings, "max_json_body_bytes", 256 * 1024))
        content_type = headers.get("content-type", "").lower()
        limit = max_json if "application/json" in content_type else max_request
        if scope.get("path") in {"/admin", "/admin/"}:
            limit = min(limit, max_json)
        if scope.get("path") == "/api/im/flash/send":
            limit = min(
                limit,
                int(getattr(settings, "media_max_image_bytes", 20 * 1024 * 1024))
                + 1024 * 1024,
            )
        raw_length = headers.get("content-length", "")
        if raw_length:
            try:
                declared = int(raw_length)
            except ValueError:
                await self._reject(send, 400, "invalid content length")
                return
            if declared < 0 or declared > limit:
                await self._reject(send, 413, "request body too large")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = bytes(message.get("body") or b"")
            total += len(chunk)
            if total > limit:
                await self._reject(send, 413, "request body too large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal body, delivered
            if not delivered:
                delivered = True
                payload = body
                body = b""
                return {
                    "type": "http.request",
                    "body": payload,
                    "more_body": False,
                }
            return {"type": "http.request", "body": b"", "more_body": False}

        return replay


class CapturingHandler(legacy.Handler):
    """Run the legacy handler against in-memory request/response streams."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        headers: Any,
        body: bytes,
        client_ip: str,
        match_pool_online_list_enabled: Optional[bool] = None,
        nearby_custom_city_enabled: Optional[bool] = None,
        message_peer_authorizer: Optional[Callable[[str], bool]] = None,
        message_policy_allowed_peers: Iterable[str] = (),
        message_policy_match_peers: Iterable[str] = (),
        message_policy_blocked_peers: Iterable[str] = (),
        local_password_change_enabled: bool = False,
        message_block_snapshot_loader: Optional[
            Callable[[], Optional[dict[str, list[str]]]]
        ] = None,
        message_block_snapshot_guard: Optional[Callable[[str], Any]] = None,
        message_block_snapshot_recorder: Optional[
            Callable[[str, Iterable[str]], None]
        ] = None,
        match_history_loader: Optional[Callable[[int], dict[str, Any]]] = None,
        match_history_recorder: Optional[Callable[[str, dict[str, Any]], None]] = None,
        conversation_summary_loader: Optional[
            Callable[[list[str]], dict[str, dict[str, Any]]]
        ] = None,
        local_text_sender: Optional[
            Callable[[str, str, str, dict[str, str]], Mapping[str, Any]]
        ] = None,
        local_text_revoker: Optional[
            Callable[[str, str], Mapping[str, Any]]
        ] = None,
        local_read_marker: Optional[Callable[[str], Optional[int]]] = None,
        request_id: str = "",
        budget_config: Optional[Mapping[str, Any]] = None,
        dependency_breakers: Any = None,
        profile_lookup_coordinator: Any = None,
        presence_coordinator: Any = None,
        read_sync_enqueuer: Optional[Callable[..., Mapping[str, Any]]] = None,
    ) -> None:
        # BaseHTTPRequestHandler.__init__ immediately starts reading a socket;
        # intentionally do not call it here.
        self.command = method.upper()
        self.path = path
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = (client_ip, 0)
        self._request_match_pool_online_list_enabled = match_pool_online_list_enabled
        self._request_nearby_custom_city_enabled = nearby_custom_city_enabled
        self._request_message_peer_authorizer = message_peer_authorizer
        self._request_message_policy_allowed_peers = tuple(message_policy_allowed_peers)
        self._request_message_policy_match_peers = tuple(message_policy_match_peers)
        self._request_message_policy_blocked_peers = tuple(message_policy_blocked_peers)
        self._request_local_password_change_enabled = bool(
            local_password_change_enabled
        )
        self._request_message_peer_authorizer_canonical = callable(
            message_peer_authorizer
        )
        self._request_message_block_snapshot_loader = message_block_snapshot_loader
        self._request_message_block_snapshot_guard = message_block_snapshot_guard
        self._request_message_block_snapshot_recorder = message_block_snapshot_recorder
        self._request_match_history_loader = match_history_loader
        self._request_match_history_recorder = match_history_recorder
        self._request_conversation_summary_loader = conversation_summary_loader
        self._request_local_text_sender = local_text_sender
        self._request_local_text_revoker = local_text_revoker
        self._request_local_read_marker = local_read_marker
        self._request_id = str(request_id or "")[:64]
        self._request_budget_config = dict(budget_config or {})
        self._request_dependency_breakers = dependency_breakers
        self._request_profile_lookup_coordinator = profile_lookup_coordinator
        self._request_presence_coordinator = presence_coordinator
        self._request_read_sync_enqueuer = read_sync_enqueuer
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self._held_user_lock = None
        self._held_peer_lock = None
        self.response_status = 500
        self.response_headers: list[tuple[str, str]] = []

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        self.response_status = int(code)

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers.append((str(keyword), str(value)))

    def end_headers(self) -> None:
        return

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        return

    def finish_capture(self) -> tuple[int, list[tuple[str, str]], bytes]:
        peer_lock = self._held_peer_lock
        self._held_peer_lock = None
        if peer_lock is not None:
            peer_lock.release()
        lock = self._held_user_lock
        self._held_user_lock = None
        if lock is not None:
            lock.release()
        return self.response_status, self.response_headers, self.wfile.getvalue()


def _client_ip(request: Request) -> str:
    """Return the proxy-verified client address used for limits and audits."""
    state = getattr(request.app, "state", None)
    settings = getattr(state, "settings", None)
    trust_proxy = bool(getattr(settings, "trust_proxy_headers", True))
    if trust_proxy:
        cf_ip = str(request.headers.get("CF-Connecting-IP") or "").strip()
        if cf_ip:
            return cf_ip[:64]
        real_ip = str(request.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip[:64]
        forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:64]
    return str(request.client.host if request.client else "unknown")[:64]


def _request_path(request: Request) -> str:
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def _cookie_value(headers: Iterable[tuple[str, str]], cookie_name: str) -> Optional[str]:
    prefix = f"{cookie_name}="
    for name, value in headers:
        if name.lower() != "set-cookie" or not value.startswith(prefix):
            continue
        return value[len(prefix) :].split(";", 1)[0]
    return None


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _query_mapping(request: Request) -> dict[str, Any]:
    """Preserve repeated query keys for native compatibility dispatchers."""

    result: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        existing = result.get(key)
        if existing is None:
            result[key] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            result[key] = [existing, value]
    return result


def _im_epoch_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if abs(numeric) >= 10**12:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _im_item_time(item: Mapping[str, Any]) -> float:
    parsed = _im_epoch_datetime(item.get("timestamp") or item.get("time"))
    return parsed.timestamp() if parsed is not None else 0.0


def _filter_im_conversations_since(
    payload: Mapping[str, Any],
    since: datetime | None,
) -> dict[str, Any]:
    result = dict(payload)
    if since is None:
        return result
    boundary = since.timestamp()
    items = [
        dict(item)
        for item in payload.get("items", [])
        if isinstance(item, Mapping) and _im_item_time(item) >= boundary
    ]
    result.update(
        items=items,
        list=items,
        count=len(items),
        snapshot_complete=False,
    )
    return result


def _local_im_read_mode(
    *,
    identity: Any,
    upstream_auth_mode: str,
    sid: str | None,
) -> bool:
    """Main IM reads are always served by TIM/APK, never by local archives."""

    del identity, upstream_auth_mode, sid
    return False


def _main_im_archive_payload(request: Request, path: str) -> dict[str, Any]:
    """Project the shared PostgreSQL archive query onto the legacy IM paths."""

    from bbw_web import archive_api

    if path == "/api/im/conversations":
        payload = archive_api.archived_conversations(
            request,
            limit=100,
            since=_im_epoch_datetime(request.query_params.get("since")),
        )
        payload.setdefault("entity", "conversation")
        payload.setdefault("status", 200)
        return payload

    peer = str(
        request.query_params.get("peer")
        or request.query_params.get("uid")
        or request.query_params.get("yourid")
        or ""
    ).strip()
    if not peer:
        raise HTTPException(status_code=400, detail="缺少聊天对象 UID")
    summary_only = request.query_params.get("summary", "0") == "1"
    around = _im_epoch_datetime(request.query_params.get("at")) if summary_only else None
    if summary_only and around is None:
        raise HTTPException(status_code=400, detail="缺少会话消息时间")
    before = (
        _im_epoch_datetime(request.query_params.get("before"))
        if not summary_only
        else None
    )
    payload = archive_api.archived_messages(
        request,
        peer=peer,
        limit=50 if summary_only else 200,
        before=before,
        around=around,
    )
    items = [
        dict(item)
        for item in payload.get("items", [])
        if isinstance(item, Mapping)
    ]
    if summary_only:
        items = sorted(items, key=_im_item_time)[-1:]
        payload.update(summary=True, context=True)
    payload.update(
        items=items,
        list=items,
        count=len(items),
        entity="message",
        status=200,
    )
    return payload


def _merge_main_im_conversations(
    upstream: Mapping[str, Any], local: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep local unread/message state authoritative while retaining old history."""

    merged_by_peer: dict[str, dict[str, Any]] = {}
    for source in (upstream, local):
        for raw in source.get("items", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            peer = str(item.get("peer_id") or item.get("conversation_user") or "").strip()
            if not peer:
                continue
            previous = merged_by_peer.get(peer)
            merged_by_peer[peer] = {**previous, **item} if previous is not None else item
    items = sorted(
        merged_by_peer.values(),
        key=_im_item_time,
        reverse=True,
    )
    payload = {**dict(upstream), "ok": True}
    payload.update(items=items, list=items, count=len(items), entity="conversation")
    return payload


def _merge_main_im_messages(
    upstream: Mapping[str, Any],
    local: Mapping[str, Any],
    *,
    summary_only: bool,
) -> dict[str, Any]:
    """Merge provider history with canonical text/media projections by identity."""

    from bbw_web import archive_api

    items = [
        dict(item)
        for item in upstream.get("items", [])
        if isinstance(item, Mapping)
    ]
    for raw_local in local.get("items", []):
        if not isinstance(raw_local, Mapping):
            continue
        local_item = dict(raw_local)
        match_index = next(
            (
                index
                for index, previous in enumerate(items)
                if archive_api._archived_items_refer_to_same_message(
                    previous, local_item
                )
            ),
            -1,
        )
        if match_index < 0:
            items.append(local_item)
            continue
        merged = archive_api._merge_archived_message_items(
            items[match_index], local_item
        )
        if str(local_item.get("provider") or "").strip().lower() == "web-local":
            # Canonical native content and its R2 projection must not be replaced
            # by a stale TIM compatibility copy of the same logical message.
            for key in (
                "provider",
                "source",
                "kind",
                "message_type",
                "object_name",
                "media",
                "quote",
                "flash_id",
                "canonical_message_id",
                "client_message_id",
            ):
                if key in local_item:
                    merged[key] = local_item[key]
            if str(local_item.get("text") or ""):
                merged["text"] = local_item["text"]
                merged["body"] = local_item.get("body") or local_item["text"]
        items[match_index] = merged
    items.sort(key=_im_item_time)
    if summary_only:
        items = items[-1:]
    elif len(items) > 200:
        items = items[-200:]
    payload = {**dict(upstream), "ok": True}
    payload.update(
        items=items,
        list=items,
        count=len(items),
        entity="message",
        summary=summary_only,
        has_more=bool(local.get("has_more") or upstream.get("has_more")),
        next_before=str(
            local.get("next_before") or upstream.get("next_before") or ""
        ),
        next_cursor=str(local.get("next_cursor") or ""),
    )
    return payload


def _merge_main_im_payload(
    path: str,
    upstream: Mapping[str, Any],
    local: Mapping[str, Any],
    *,
    summary_only: bool = False,
) -> dict[str, Any]:
    if path == "/api/im/conversations":
        return _merge_main_im_conversations(upstream, local)
    return _merge_main_im_messages(
        upstream,
        local,
        summary_only=summary_only,
    )


def _captured_json(payload: Mapping[str, Any]) -> tuple[list[tuple[str, str]], bytes]:
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    return (
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ],
        body,
    )


def _replace_captured_json(
    headers: Iterable[tuple[str, str]],
    payload: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], bytes]:
    """Replace a captured JSON body without dropping cookies or policy headers."""

    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    replaced = [
        (name, value)
        for name, value in headers
        if name.lower() not in {"content-type", "content-length"}
    ]
    replaced.insert(0, ("Content-Type", "application/json; charset=utf-8"))
    replaced.append(("Content-Length", str(len(body))))
    if not any(name.lower() == "cache-control" for name, _value in replaced):
        replaced.append(("Cache-Control", "no-store"))
    return replaced, body


def _response_json(headers: Iterable[tuple[str, str]], body: bytes) -> dict[str, Any]:
    content_type = next(
        (value for name, value in headers if name.lower() == "content-type"),
        "",
    )
    if "json" not in content_type.lower():
        return {}
    return _json_object(body)


def _pending_cookie_name(user_cookie_name: str) -> str:
    if user_cookie_name.startswith("__Host-"):
        return f"__Host-{user_cookie_name[len('__Host-'):]}-pending-login"
    return f"{user_cookie_name}_pending_login"


def _valid_pending_sid(raw_sid: str | None) -> bool:
    value = str(raw_sid or "")
    return 20 <= len(value) <= 128 and all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value
    )


def _auth_json_request_error(request: Request) -> JSONResponse | None:
    content_type = str(request.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("application/json"):
        return JSONResponse(
            {"ok": False, "error": "请求格式必须为 JSON"}, status_code=415
        )
    raw_origin = str(request.headers.get("Origin") or "")
    if raw_origin:
        origin = legacy._normalize_origin(raw_origin)
        host = str(request.headers.get("Host") or "").strip().lower()
        if not origin:
            return JSONResponse(
                {"ok": False, "error": "cross-origin request rejected"},
                status_code=403,
            )
        parsed_origin = urlparse(origin)
        if not host or parsed_origin.netloc.lower() != host:
            return JSONResponse(
                {"ok": False, "error": "cross-origin request rejected"},
                status_code=403,
            )
    elif str(request.headers.get("Sec-Fetch-Site") or "").lower() in {
        "cross-site",
        "same-site",
    }:
        return JSONResponse(
            {"ok": False, "error": "cross-origin request rejected"},
            status_code=403,
        )
    return None


def _set_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    secure: bool,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def _clear_cookie(response: Response, *, name: str, secure: bool) -> None:
    response.delete_cookie(
        key=name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def _logout_web_user(web_user: Any) -> None:
    try:
        with web_user.lock:
            web_user.app.auth.logout()
    except Exception:
        pass


def _logout_expired_pending_user(web_user: Any) -> None:
    try:
        with web_user.lock:
            if web_user.pending_until is None:
                return
            web_user.app.auth.logout()
    except Exception:
        pass
    finally:
        close_web_runtime(web_user.app, web_user.native)


def _discard_pending_runtime(raw_sid: str) -> None:
    if not raw_sid or legacy.STORE is None:
        return
    web_user = legacy.STORE.get(raw_sid)
    if web_user is not None:
        _logout_web_user(web_user)
        web_user.clear_pending()
    legacy.STORE.drop(raw_sid)


def _cancel_pending_runtime(persistence: Any, raw_sid: str) -> bool:
    """Cancel only a still-pending SID, never a concurrently promoted session."""
    if not _valid_pending_sid(raw_sid):
        return False
    web_user = legacy.STORE.get(raw_sid) if legacy.STORE is not None else None
    if web_user is None:
        return bool(persistence.cancel_pending_login(raw_sid))
    with web_user.lock:
        if persistence.require_identity(raw_sid) is not None:
            return False
        deleted = bool(persistence.cancel_pending_login(raw_sid))
        if not deleted and web_user.pending_until is None:
            return False
        try:
            web_user.app.auth.logout()
        except Exception:
            pass
        web_user.clear_pending()
        if legacy.STORE is not None:
            legacy.STORE.drop(raw_sid)
        return True


def _record_local_login_failure(
    persistence: Any, *, phone: str, client_ip: str
) -> None:
    """Best-effort security accounting must not replace the auth response."""

    try:
        persistence.record_login_failure(phone=phone, client_ip=client_ip)
    except Exception:
        LOGGER.exception("local login failure accounting failed")


def _local_password_login_response(
    request: Request,
    *,
    persistence: Any,
    request_json: Mapping[str, Any],
    sid: str | None,
    pending_sid: str | None,
    cookie_name: str,
    pending_cookie_name: str,
    client_ip: str,
    dependency_mode: str,
    upstream_may_recover: bool,
) -> JSONResponse:
    """Authenticate one migrated account without constructing an upstream login.

    Callers own the fallback decision.  Provider-first mode reaches this helper
    only after the adapter emitted ``UPSTREAM_AUTH_UNAVAILABLE``; local-only
    mode is an explicit operator decision that Banghua authentication must not
    be contacted at all.
    """

    from bbw_prod.services import (
        AuthenticationFailed,
        LocalAuthenticationUnavailable,
        PermissionDenied,
    )

    phone = str(request_json.get("phone") or "").strip()
    gate = _local_password_auth_gate(request)
    with gate.claim() as admitted:
        if not admitted:
            retry_after = LOCAL_PASSWORD_AUTH_RETRY_AFTER_SECONDS
            return JSONResponse(
                {
                    "ok": False,
                    "code": "LOCAL_AUTH_BUSY",
                    "error": "Web 本地认证繁忙，请稍后重试",
                    "retryable": True,
                    "retry_after": retry_after,
                },
                status_code=503,
                headers={"Retry-After": str(retry_after)},
            )
        try:
            local_login = persistence.complete_local_password_login(
                phone=phone,
                password=str(request_json.get("password") or ""),
                old_sid=sid,
                client_ip=client_ip,
                user_agent=str(request.headers.get("User-Agent") or "")[:512],
            )
        except (
            AuthenticationFailed,
            LocalAuthenticationUnavailable,
            PermissionDenied,
        ):
            # Unknown, wrong-password, suspended, disabled and not-yet-migrated
            # accounts deliberately share one public response.  The service
            # still keeps the internal reason for audit/operations, but an
            # anonymous caller cannot enumerate migration or account state.
            _record_local_login_failure(
                persistence, phone=phone, client_ip=client_ip
            )
            return JSONResponse(
                {
                    "ok": False,
                    "code": "LOCAL_AUTH_REJECTED",
                    "error": (
                        "账号或密码验证失败；若尚未完成 Web 密码迁移，请在原认证服务恢复后使用密码登录"
                        if upstream_may_recover
                        else "账号或密码验证失败；当前模式仅支持已完成 Web 密码迁移的账号"
                    ),
                    "retryable": False,
                },
                status_code=401,
            )
        except Exception:
            LOGGER.exception("local password login failed")
            return JSONResponse(
                {
                    "ok": False,
                    "code": "LOCAL_AUTH_UNAVAILABLE",
                    "error": "Web 本地认证暂时不可用，请稍后重试",
                    "retryable": True,
                },
                status_code=503,
            )

    if legacy.STORE is None:
        persistence.revoke_session(
            local_login.raw_sid, reason="runtime_store_unavailable"
        )
        return JSONResponse(
            {
                "ok": False,
                "code": "LOCAL_AUTH_UNAVAILABLE",
                "error": "Web 本地认证暂时不可用，请稍后重试",
                "retryable": True,
            },
            status_code=503,
        )
    if sid and sid != local_login.raw_sid:
        legacy.STORE.drop(sid)
    legacy.STORE.put(local_login.web_user)
    response = JSONResponse(
        {
            "ok": True,
            **local_login.web_user.public(),
            "auth_source": "web-local",
            "dependency_mode": dependency_mode,
        },
        status_code=200,
    )
    secure_cookie = bool(request.app.state.settings.cookie_secure)
    _set_cookie(
        response,
        name=cookie_name,
        value=local_login.raw_sid,
        secure=secure_cookie,
    )
    if pending_sid:
        _clear_cookie(
            response,
            name=pending_cookie_name,
            secure=secure_cookie,
        )
    return response


async def _legacy_dispatch(request: Request) -> Response:
    """Restore durable state, execute one product route, then persist effects."""
    max_body = int(
        getattr(request.app.state.settings, "max_request_body_bytes", 32 * 1024 * 1024)
    )
    try:
        declared_length = int(request.headers.get("Content-Length") or 0)
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid content length"}, status_code=400)
    if declared_length < 0 or declared_length > max_body:
        return JSONResponse({"ok": False, "error": "request body too large"}, status_code=413)
    try:
        raw_body = await request.body()
    except ClientDisconnect:
        return Response(status_code=499)
    return await run_in_threadpool(_legacy_dispatch_sync, request, raw_body)


def _legacy_dispatch_sync(request: Request, raw_body: bytes) -> Response:
    """Run blocking protocol, database and Redis work outside the event loop."""
    max_body = int(getattr(request.app.state.settings, "max_request_body_bytes", 32 * 1024 * 1024))
    if len(raw_body) > max_body:
        return JSONResponse({"ok": False, "error": "request body too large"}, status_code=413)

    # The legacy JSON/multipart readers require an exact Content-Length.
    headers = Headers(
        {
            **dict(request.headers),
            "content-length": str(len(raw_body)),
        }
    )
    client_ip = _client_ip(request)
    cookie_name = legacy.COOKIE_NAME
    sid = request.cookies.get(cookie_name)
    persistence = request.app.state.persistence
    pending_cookie_name = _pending_cookie_name(cookie_name)
    pending_sid = request.cookies.get(pending_cookie_name)

    path = request.url.path
    request_json = _json_object(raw_body)

    if request.method == "POST" and path in {"/api/auth/login", "/api/auth/sms-login"}:
        request_error = _auth_json_request_error(request)
        if request_error is not None:
            return request_error
        if _valid_pending_sid(pending_sid):
            cancelled = _cancel_pending_runtime(persistence, pending_sid)
            if not cancelled and persistence.require_identity(pending_sid) is not None:
                promoted_user = (
                    legacy.STORE.get(pending_sid) if legacy.STORE is not None else None
                )
                if promoted_user is not None:
                    return _pending_login_response(
                        request,
                        {"ok": True, **promoted_user.public()},
                        status_code=200,
                        clear_pending=True,
                        set_session_sid=pending_sid,
                    )

    revoke_non_provider_session = getattr(
        persistence, "revoke_non_provider_session", None
    )
    legacy_local_session = bool(
        sid
        and callable(revoke_non_provider_session)
        and revoke_non_provider_session(str(sid))
    )
    if legacy_local_session:
        if legacy.STORE is not None:
            legacy.STORE.drop(str(sid))
        if path not in LOGIN_START_PATHS:
            response = JSONResponse(
                {
                    "ok": False,
                    "code": "PROVIDER_RELOGIN_REQUIRED",
                    "error": "登录方式已恢复为原账号服务，请重新登录",
                    "retryable": False,
                },
                status_code=401,
            )
            _clear_cookie(
                response,
                name=cookie_name,
                secure=bool(request.app.state.settings.cookie_secure),
            )
            _clear_cookie(
                response,
                name=pending_cookie_name,
                secure=bool(request.app.state.settings.cookie_secure),
            )
            return response
        sid = None

    # Never trust an authenticated object that only remains in process memory.
    # PostgreSQL/Redis are the authority for idle/absolute expiry and revocation.
    identity = persistence.require_identity(str(sid or "")) if sid else None
    if sid and identity is None and legacy.STORE is not None:
        legacy.STORE.drop(sid)

    if path.startswith("/api/") and path != "/api/health":
        limiter_identity = (
            f"user:{identity.user_id}" if identity is not None else f"ip:{client_ip}"
        )
        if request.method == "GET" and path == "/api/me":
            # Session restoration must not share the ordinary product API
            # bucket. A busy authenticated page can otherwise consume all 60
            # tokens just before a reload and make the browser look logged out.
            if not persistence.rate_limit(
                f"session-restore:{limiter_identity}", limit=180, window_seconds=60
            ):
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "SESSION_RESTORE_RATE_LIMIT",
                        "error": "登录状态恢复请求过于频繁，请稍后重试",
                        "retryable": True,
                        "retry_after": 5,
                    },
                    status_code=429,
                    headers={"Retry-After": "5"},
                )
        elif not persistence.rate_limit(
            f"api:{limiter_identity}", limit=60, window_seconds=60
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "code": "API_RATE_LIMIT",
                    "error": "请求过于频繁，请稍后重试",
                    "retryable": True,
                    "retry_after": 60,
                },
                status_code=429,
                headers={"Retry-After": "60"},
            )

        content_type = str(request.headers.get("Content-Type") or "").lower()
        is_upload = content_type.startswith("multipart/form-data") or "upload" in path
        if is_upload and not persistence.rate_limit(
            f"upload:{limiter_identity}", limit=10, window_seconds=60
        ):
            return JSONResponse(
                {"ok": False, "error": "上传请求过于频繁，请稍后重试"},
                status_code=429,
                headers={"Retry-After": "60"},
            )

    if sid and identity is not None and legacy.STORE is not None and legacy.STORE.get(sid) is None:
        restored = persistence.restore_web_user(sid)
        if restored is not None:
            legacy.STORE.put(restored)

    conversation_message_peers_before: set[str] | None = None
    if (
        identity is not None
        and request.method == "GET"
        and path == "/api/im/conversations"
        and sid
        and legacy.STORE is not None
    ):
        web_user = legacy.STORE.get(sid)
        if web_user is not None:
            with web_user.lock:
                conversation_message_peers_before = set(
                    web_user.conversation_message_peers
                )

    configured_auth_mode = str(
        getattr(request.app.state.settings, "upstream_auth_mode", "provider-only")
        or "provider-only"
    ).strip().lower()
    if configured_auth_mode not in {"provider-only", "provider-first"}:
        return JSONResponse(
            {
                "ok": False,
                "code": "UPSTREAM_AUTH_MODE_UNSUPPORTED",
                "error": "当前配置禁用了原账号服务，Web 产品拒绝进入本地认证模式",
                "retryable": False,
            },
            status_code=503,
        )
    upstream_auth_mode = "provider-only"

    if request.method == "POST" and path == "/api/auth/sms-send":
        phone = str(request_json.get("phone") or "").strip()
        try:
            account_context = persistence.precheck_login_credentials(
                phone=phone,
                client_ip=client_ip,
                turnstile_token=str(request_json.get("turnstile_token") or ""),
            )
            normalized_phone = account_context.normalized_phone
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "手机号格式无效"}, status_code=400
            )
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except RuntimeError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=429,
                headers={"Retry-After": "900"},
            )
        sms_limits = (
            persistence.rate_limit(
                f"sms-send-phone:{normalized_phone}", limit=3, window_seconds=10 * 60
            ),
            persistence.rate_limit(
                f"sms-send-ip:{client_ip}", limit=3, window_seconds=10 * 60
            ),
            persistence.rate_limit(
                f"sms-send-combined:{normalized_phone}:{client_ip}",
                limit=3,
                window_seconds=10 * 60,
            ),
        )
        if not all(sms_limits):
            return JSONResponse(
                {"ok": False, "error": "短信发送过于频繁，请稍后重试"},
                status_code=429,
                headers={"Retry-After": "600"},
            )

    if request.method == "POST" and path == "/api/auth/login":
        login_mode = str(request_json.get("mode") or "password").strip().lower()
        if login_mode != "password":
            return JSONResponse(
                {"ok": False, "error": "不支持的登录方式"}, status_code=400
            )

    login_context: Any = None
    if request.method == "POST" and path in {"/api/auth/login", "/api/auth/sms-login"}:
        try:
            login_context = persistence.precheck_login_credentials(
                phone=str(request_json.get("phone") or "").strip(),
                client_ip=client_ip,
                turnstile_token=str(request_json.get("turnstile_token") or ""),
            )
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "手机号格式无效"}, status_code=400
            )
        except RuntimeError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=429,
                headers={"Retry-After": "900"},
            )
    if (
        identity is not None
        and request.method == "POST"
        and path == "/api/auth/password"
    ):
        return JSONResponse(
            {
                "ok": False,
                "code": "LOCAL_PASSWORD_DISABLED",
                "error": "登录密码由原账号服务管理，Web 不再保存或修改本地密码",
                "retryable": False,
            },
            status_code=409,
        )

    if request.method == "POST" and _native_profile_avatar_reset(path, request_json):
        return JSONResponse(
            {
                "ok": False,
                "code": "APK_AVATAR_UPLOAD_REQUIRED",
                "error": "Web 私有媒体头像已停用，请使用原账号服务支持的头像方式",
                "retryable": False,
            },
            status_code=409,
        )

    if request.method == "POST" and path == "/api/moments/publish":
        media_asset_ids = request_json.get("media_asset_ids")
        if isinstance(media_asset_ids, (list, tuple, set)) and any(
            str(value or "").strip() for value in media_asset_ids
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "code": "APK_TEXT_MOMENT_ONLY",
                    "error": "当前原 APK 发布接口仅支持文字动态，未发送所选本地媒体",
                    "retryable": False,
                },
                status_code=409,
            )

    # Profile, relationships, discovery, matching and moments all fall through
    # to the APK-compatible BFF below. PostgreSQL remains a cache/archive and
    # permission mirror only.

    message_policy_allowed_peers: tuple[str, ...] = ()
    message_policy_match_peers: tuple[str, ...] = ()
    message_policy_blocked_peers: tuple[str, ...] = ()
    if identity is not None and path == "/api/im/message-policy":
        try:
            message_policy_snapshot = persistence.message_policy_snapshot(identity)
            message_policy_allowed_peers = tuple(
                message_policy_snapshot["allowed_peers"]
            )
            message_policy_match_peers = tuple(message_policy_snapshot["match_peers"])
            message_policy_blocked_peers = tuple(
                message_policy_snapshot["blocked_peers"]
            )
        except Exception:
            LOGGER.exception("message policy snapshot lookup failed")

    match_history_loader: Optional[Callable[[int], dict[str, Any]]] = None
    if identity is not None and path == "/api/match/history":
        match_history_loader = (
            lambda page, request_identity=identity: persistence.match_history(
                request_identity,
                page=page,
            )
        )

    conversation_summary_loader: Optional[
        Callable[[list[str]], dict[str, dict[str, Any]]]
    ] = None
    if identity is not None and path == "/api/im/conversations":
        conversation_summary_loader = (
            lambda peers, request_identity=identity: persistence.conversation_summary_map(
                request_identity,
                peers,
            )
        )

    match_history_recorder: Optional[Callable[[str, dict[str, Any]], None]] = None
    if (
        identity is not None
        and request.method == "POST"
        and path
        in {
            "/api/match/online",
            "/api/match/local",
            "/api/match/voice/start",
        }
    ):
        match_history_request_id = uuid.uuid4().hex

        def persist_match_history(
            match_path: str,
            response_payload: dict[str, Any],
            *,
            request_identity: Any = identity,
            request_id: str = match_history_request_id,
        ) -> None:
            try:
                persistence.remember_match_history_response(
                    identity=request_identity,
                    method="POST",
                    path=match_path,
                    response_data=response_payload,
                    status=200,
                    request_id=request_id,
                )
            except Exception:
                LOGGER.exception("match history persistence failed")
                raise

        match_history_recorder = persist_match_history

    message_block_snapshot_loader: Optional[
        Callable[[], Optional[dict[str, list[str]]]]
    ] = None
    message_block_snapshot_guard: Optional[Callable[[str], Any]] = None
    message_block_snapshot_recorder: Optional[
        Callable[[str, Iterable[str]], None]
    ] = None
    if identity is not None:
        message_block_snapshot_loader = (
            lambda request_identity=identity: persistence.trusted_message_block_snapshot(
                request_identity
            )
        )

        @contextmanager
        def serialize_message_block_snapshot(
            snapshot_path: str,
            *,
            request_identity: Any = identity,
        ):
            kind = {
                "/api/social/blacklist": "blacklist",
                "/api/social/blacklist-me": "blacklisted_by",
            }.get(snapshot_path)
            if kind is None:
                raise ValueError("unsupported message block snapshot")
            lock = persistence.redis.lock(
                (
                    f"{persistence.settings.redis_prefix}:message-block-snapshot:"
                    f"{request_identity.user_id}:{kind}"
                ),
                timeout=60,
                blocking_timeout=2,
                thread_local=False,
            )
            if not lock.acquire(blocking=True):
                raise TimeoutError("message block snapshot synchronization is busy")
            try:
                yield
            finally:
                try:
                    lock.release()
                except Exception:
                    LOGGER.exception("message block snapshot lock release failed")

        def persist_message_block_snapshot(
            snapshot_path: str,
            peers: Iterable[str],
            *,
            request_identity: Any = identity,
        ) -> None:
            kind = {
                "/api/social/blacklist": "blacklist",
                "/api/social/blacklist-me": "blacklisted_by",
            }.get(snapshot_path)
            if kind is None:
                raise ValueError("unsupported message block snapshot")
            persistence.replace_social_message_relationships(
                identity=request_identity,
                peers=set(peers),
                kind=kind,
                deactivate_missing=True,
                source_path=snapshot_path,
            )

        message_block_snapshot_guard = serialize_message_block_snapshot
        message_block_snapshot_recorder = persist_message_block_snapshot

    local_text_sender: Optional[
        Callable[[str, str, str, dict[str, str]], Mapping[str, Any]]
    ] = None
    if (
        identity is not None
        and request.method == "POST"
        and path == "/api/im/rest/send"
    ):
        from bbw_web.messaging import (
            InvalidLocalMessage,
            LocalIdentityUnavailable,
            LocalMessageBlocked,
            LocalMessageForbidden,
            LocalMessageIdempotencyConflict,
            PeerNotMigrated,
        )

        def send_local_text(
            peer: str,
            text_value: str,
            client_message_id: str,
            quote: dict[str, str],
            *,
            request_identity: Any = identity,
        ) -> Mapping[str, Any]:
            try:
                result = persistence.send_local_text_message(
                    identity=request_identity,
                    peer=peer,
                    client_message_id=client_message_id,
                    text_value=text_value,
                    quote=quote,
                )
            except PeerNotMigrated:
                return {"handled": False, "reason": "peer_not_migrated"}
            except LocalIdentityUnavailable:
                return {"handled": False, "reason": "sender_not_migrated"}
            except InvalidLocalMessage as exc:
                return {
                    "handled": True,
                    "status": 400,
                    "payload": {
                        "ok": False,
                        "code": exc.code.upper(),
                        "error": str(exc),
                        "retryable": False,
                    },
                }
            except (LocalMessageBlocked, LocalMessageForbidden) as exc:
                return {
                    "handled": True,
                    "status": 403,
                    "payload": {
                        "ok": False,
                        "code": exc.code.upper(),
                        "error": str(exc),
                        "retryable": False,
                    },
                }
            except LocalMessageIdempotencyConflict as exc:
                return {
                    "handled": True,
                    "status": 409,
                    "payload": {
                        "ok": False,
                        "code": exc.code.upper(),
                        "error": str(exc),
                        "retryable": False,
                    },
                }

            message_id = str(result.message.id)
            tim_mirror_status = str(
                getattr(result.tim_mirror, "status", "pending") or "pending"
            )
            return {
                "handled": True,
                "status": 200,
                "payload": {
                    "ok": True,
                    "action": "web-local/send",
                    "source": "web-local",
                    "message": "消息已由 Web 本地服务投递",
                    "message_id": message_id,
                    "canonical_message_id": message_id,
                    "msg_key": message_id,
                    "client_message_id": result.message.client_message_id,
                    "from": result.message.sender_upstream_uid,
                    "to": result.message.recipient_upstream_uid,
                    "timestamp": result.message.occurred_at.isoformat(),
                    "delivery": "delivered",
                    "created": bool(result.created),
                    "local_delivery": "delivered",
                    "tim_mirror_status": tim_mirror_status,
                    "compatibility_sync": tim_mirror_status,
                },
            }

        local_text_sender = send_local_text

    local_read_marker: Optional[Callable[[str], Optional[int]]] = None
    local_text_revoker: Optional[
        Callable[[str, str], Mapping[str, Any]]
    ] = None
    if (
        identity is not None
        and request.method == "POST"
        and path == "/api/im/rest/revoke"
    ):
        from bbw_web.messaging import (
            InvalidLocalMessage,
            LocalIdentityUnavailable,
            LocalMessageIdempotencyConflict,
            LocalMessageNotFound,
            LocalMessageRevocationDenied,
            LocalMessageRevocationExpired,
        )

        def revoke_local_text(
            peer: str,
            canonical_message_id: str,
            *,
            request_identity: Any = identity,
        ) -> Mapping[str, Any]:
            try:
                result = persistence.revoke_local_text_message(
                    identity=request_identity,
                    peer=peer,
                    canonical_message_id=canonical_message_id,
                )
            except LocalMessageNotFound:
                return {"handled": False, "reason": "local_message_not_found"}
            except LocalIdentityUnavailable:
                return {"handled": False, "reason": "sender_not_migrated"}
            except InvalidLocalMessage as exc:
                return {
                    "handled": True,
                    "status": 400,
                    "payload": {
                        "ok": False,
                        "code": exc.code.upper(),
                        "error": str(exc),
                        "retryable": False,
                    },
                }
            except LocalMessageRevocationDenied as exc:
                return {
                    "handled": True,
                    "status": 403,
                    "payload": {
                        "ok": False,
                        "code": exc.code.upper(),
                        "error": str(exc),
                        "retryable": False,
                    },
                }
            except (LocalMessageRevocationExpired, LocalMessageIdempotencyConflict) as exc:
                return {
                    "handled": True,
                    "status": 409,
                    "payload": {
                        "ok": False,
                        "code": exc.code.upper(),
                        "error": str(exc),
                        "retryable": False,
                    },
                }

            tim_mirror_status = (
                str(result.tim_mirror.status or "pending")
                if result.tim_mirror is not None
                else "cancelled"
            )
            message_id = str(result.message.id)
            return {
                "handled": True,
                "status": 200,
                "payload": {
                    "ok": True,
                    "action": "web-local/revoke",
                    "source": "web-local",
                    "provider": "web-local",
                    "message": "消息已撤回",
                    "message_id": message_id,
                    "canonical_message_id": message_id,
                    "msg_key": message_id,
                    "from": result.message.sender_upstream_uid,
                    "to": result.message.recipient_upstream_uid,
                    "revoked": True,
                    "revoked_at": result.revoked_at.isoformat(),
                    "recalled_text": result.recalled_text,
                    "created": bool(result.created),
                    "already_revoked": not bool(result.created),
                    "local_delivery": "revoked",
                    "tim_mirror_status": tim_mirror_status,
                    "compatibility_sync": tim_mirror_status,
                },
            }

        local_text_revoker = revoke_local_text

    if identity is not None and request.method == "POST" and path == "/api/im/read":
        from bbw_web.messaging import LocalIdentityUnavailable, PeerNotMigrated

        def mark_local_read(
            peer: str,
            *,
            request_identity: Any = identity,
        ) -> Optional[int]:
            try:
                return persistence.mark_local_conversation_read(
                    identity=request_identity,
                    peer=peer,
                )
            except (LocalIdentityUnavailable, PeerNotMigrated):
                return None

        local_read_marker = mark_local_read

    main_im_read = bool(
        identity is not None
        and request.method == "GET"
        and path in {"/api/im/conversations", "/api/im/messages"}
    )
    served_local_im_read = bool(
        main_im_read
        and _local_im_read_mode(
            identity=identity,
            upstream_auth_mode=upstream_auth_mode,
            sid=sid,
        )
    )
    if served_local_im_read:
        if path == "/api/im/messages":
            peer = str(
                request.query_params.get("peer")
                or request.query_params.get("uid")
                or request.query_params.get("yourid")
                or ""
            ).strip()
            if peer and peer != legacy.SYSTEM_CUSTOMER_SERVICE_UID:
                try:
                    allowed = persistence.can_message_peer(identity, peer)
                except Exception:
                    LOGGER.exception("local IM read authorization failed")
                    allowed = False
                if not allowed:
                    return JSONResponse(
                        {
                            "ok": False,
                            "code": "PRIVATE_MESSAGE_PERMISSION_REQUIRED",
                            "error": "当前账号与该用户没有可用的私聊关系",
                        },
                        status_code=403,
                    )
        try:
            local_payload = _main_im_archive_payload(request, path)
        except HTTPException as exc:
            return JSONResponse(
                {"ok": False, "error": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        except Exception:
            LOGGER.exception("canonical IM read failed")
            return JSONResponse(
                {
                    "ok": False,
                    "code": "LOCAL_IM_READ_UNAVAILABLE",
                    "error": "本地聊天记录暂时不可用，请稍后重试",
                },
                status_code=503,
            )
        local_payload.update(
            dependency_mode="web-local",
            upstream_contacted=False,
        )
        status = 200
        response_headers, response_body = _captured_json(local_payload)
    else:
        handler = CapturingHandler(
            method=request.method,
            path=_request_path(request),
            headers=headers,
            body=raw_body,
            client_ip=client_ip,
            match_pool_online_list_enabled=(
                identity.match_pool_online_list_enabled if identity is not None else None
            ),
            nearby_custom_city_enabled=(
                identity.nearby_custom_city_enabled if identity is not None else None
            ),
            message_peer_authorizer=(
                (
                    lambda peer, request_identity=identity: persistence.can_message_peer(
                        request_identity, peer
                    )
                )
                if identity is not None
                else None
            ),
            message_policy_allowed_peers=message_policy_allowed_peers,
            message_policy_match_peers=message_policy_match_peers,
            message_policy_blocked_peers=message_policy_blocked_peers,
            local_password_change_enabled=False,
            message_block_snapshot_loader=message_block_snapshot_loader,
            message_block_snapshot_guard=message_block_snapshot_guard,
            message_block_snapshot_recorder=message_block_snapshot_recorder,
            match_history_loader=match_history_loader,
            match_history_recorder=match_history_recorder,
            conversation_summary_loader=None,
            local_text_sender=None,
            local_text_revoker=None,
            local_read_marker=None,
            request_id=str(getattr(request.state, "request_id", "") or ""),
            budget_config={
                "im_budget_seconds": float(
                    getattr(
                        request.app.state.settings,
                        "im_interactive_budget_seconds",
                        0,
                    )
                    or 0
                ),
                "tim_timeout_seconds": float(
                    getattr(
                        request.app.state.settings,
                        "tim_interactive_timeout_seconds",
                        0,
                    )
                    or 0
                ),
                "provider_timeout_seconds": float(
                    getattr(
                        request.app.state.settings,
                        "provider_interactive_timeout_seconds",
                        0,
                    )
                    or 0
                ),
                "profile_budget_seconds": float(
                    getattr(
                        request.app.state.settings,
                        "profile_interactive_budget_seconds",
                        0,
                    )
                    or 0
                ),
                "profile_timeout_seconds": float(
                    getattr(
                        request.app.state.settings,
                        "profile_interactive_timeout_seconds",
                        0,
                    )
                    or 0
                ),
                "profile_sync_limit": int(
                    getattr(
                        request.app.state.settings,
                        "profile_sync_fetch_limit",
                        12,
                    )
                    or 12
                ),
                "presence_timeout_seconds": float(
                    getattr(
                        request.app.state.settings,
                        "presence_interactive_timeout_seconds",
                        0,
                    )
                    or 0
                ),
            },
            dependency_breakers=getattr(
                persistence,
                "dependency_breakers",
                None,
            ),
            profile_lookup_coordinator=getattr(
                persistence,
                "profile_lookup_coordinator",
                None,
            ),
            presence_coordinator=getattr(
                persistence,
                "presence_coordinator",
                None,
            ),
            read_sync_enqueuer=(
                getattr(persistence, "enqueue_tim_read_sync", None)
                if identity is not None
                else None
            ),
        )
        try:
            if request.method == "GET":
                handler.do_GET()
            elif request.method == "POST":
                handler.do_POST()
            elif request.method == "OPTIONS":
                handler.do_OPTIONS()
            elif request.method == "HEAD":
                handler.do_HEAD()
            else:
                return JSONResponse(
                    {"ok": False, "error": "method not allowed"}, status_code=405
                )
        except Exception:
            LOGGER.exception("legacy route dispatch failed")
            handler.finish_capture()
            return JSONResponse(
                {"ok": False, "error": "服务暂时不可用，请稍后重试"},
                status_code=500,
            )
        else:
            status, response_headers, response_body = handler.finish_capture()

    response_data = _response_json(response_headers, response_body)
    if (
        request.method == "GET"
        and path == "/api/im/conversations"
        and status < 400
        and response_data.get("ok") is not False
    ):
        activity_since = _im_epoch_datetime(request.query_params.get("since"))
        if activity_since is not None:
            response_data = _filter_im_conversations_since(
                response_data,
                activity_since,
            )
            response_headers, response_body = _replace_captured_json(
                response_headers,
                response_data,
            )
    new_sid = _cookie_value(response_headers, cookie_name)

    message_policy_paths = {
        "/api/social/friends",
        "/api/social/agree-friend",
        "/api/social/delete-friend",
        "/api/social/blacklist-add",
        "/api/social/blacklist-del",
        "/api/match/online",
        "/api/match/local",
        "/api/match/voice/start",
        "/api/im/conversations",
        "/api/im/rest/send",
        "/api/im/flash/send",
    }
    if identity is not None and path in message_policy_paths:
        try:
            persistence.remember_message_policy_response(
                identity=identity,
                method=request.method,
                path=path,
                request_data=request_json,
                response_data=response_data,
                status=status,
            )
        except Exception:
            LOGGER.exception("message policy response persistence failed")
            if (
                path
                in {
                    "/api/social/friends",
                    "/api/social/agree-friend",
                    "/api/social/delete-friend",
                    "/api/social/blacklist-add",
                    "/api/social/blacklist-del",
                }
                and status < 400
                and response_data.get("ok") is True
            ):
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "SOCIAL_DM_POLICY_PERSISTENCE_FAILED",
                        "error": "关系状态已返回，但私聊权限同步失败，请刷新后重试",
                    },
                    status_code=503,
                )
            if (
                path == "/api/im/conversations"
                and status < 400
                and response_data.get("ok") is True
            ):
                web_user = (
                    legacy.STORE.get(sid)
                    if sid and legacy.STORE is not None
                    else None
                )
                if web_user is not None:
                    response_peers = legacy._message_peer_ids(response_data)
                    if conversation_message_peers_before is not None:
                        response_peers.difference_update(
                            conversation_message_peers_before
                        )
                    with web_user.lock:
                        web_user.conversation_message_peers.difference_update(
                            response_peers
                        )
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "CONVERSATION_DM_GRANT_PERSISTENCE_FAILED",
                        "error": "会话已读取，但私聊权限保存失败，请稍后重试",
                    },
                    status_code=503,
                )
            if (
                path in {"/api/match/online", "/api/match/local", "/api/match/voice/start"}
                and status < 400
                and response_data.get("ok") is True
            ):
                web_user = (
                    legacy.STORE.get(sid)
                    if sid and legacy.STORE is not None
                    else None
                )
                if web_user is not None:
                    with web_user.lock:
                        web_user.match_message_peers.difference_update(
                            legacy._message_peer_ids(response_data)
                        )
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "MATCH_DM_GRANT_PERSISTENCE_FAILED",
                        "error": "匹配结果暂时无法建立受控私信权限，请稍后重试",
                    },
                    status_code=503,
                )

    is_login_path = path in {"/api/auth/login", "/api/auth/sms-login"}
    if is_login_path and not response_data.get("ok"):
        persistence.record_login_failure(
            phone=str(request_json.get("phone") or "").strip(),
            client_ip=client_ip,
        )

    if is_login_path and response_data.get("ok") and new_sid:
        try:
            web_user = legacy.STORE.get(new_sid) if legacy.STORE is not None else None
            if web_user is None:
                raise RuntimeError("authenticated session was not registered")
            if login_context is not None and login_context.requires_invite:
                expires_in = persistence.begin_pending_login(
                    raw_sid=new_sid,
                    phone=str(request_json.get("phone") or "").strip(),
                    password=str(request_json.get("password") or ""),
                    mode=(
                        "sms"
                        if path == "/api/auth/sms-login"
                        else str(request_json.get("mode") or "password")
                    ),
                    upstream_uid=str(web_user.app.session.uid or ""),
                    client_ip=client_ip,
                    user_agent=str(request.headers.get("User-Agent") or "")[:512],
                )
                web_user.mark_pending(expires_in)
                response = JSONResponse(
                    {
                        "ok": True,
                        "authenticated": False,
                        "requires_invite": True,
                        "next": "invite",
                        "message": "账号验证成功，请输入邀请码",
                        "expires_in": expires_in,
                    },
                    status_code=202,
                )
                _set_cookie(
                    response,
                    name=pending_cookie_name,
                    value=new_sid,
                    secure=bool(request.app.state.settings.cookie_secure),
                    max_age=expires_in,
                )
                return response
            identity = persistence.complete_login(
                web_user=web_user,
                phone=str(request_json.get("phone") or "").strip(),
                invite_code=None,
                password=str(request_json.get("password") or ""),
                password_verified=(
                    path == "/api/auth/login"
                    and str(request_json.get("mode") or "password").strip().lower()
                    == "password"
                ),
                login_context=login_context,
                old_sid=sid,
                client_ip=client_ip,
                user_agent=str(request.headers.get("User-Agent") or "")[:512],
            )
        except Exception:
            LOGGER.exception("authenticated session persistence failed")
            persistence.cancel_pending_login(new_sid)
            _discard_pending_runtime(new_sid)
            persistence.revoke_session(new_sid)
            return JSONResponse(
                {"ok": False, "error": "登录状态保存失败，请稍后重试"},
                status_code=503,
            )
    elif path == "/api/auth/logout":
        if sid:
            persistence.revoke_session(sid)

    try:
        persistence.capture_product_response(
            sid=new_sid or sid,
            identity=identity,
            method=request.method,
            path=path,
            query=dict(request.query_params),
            request_data=request_json,
            response_data=response_data,
            status=status,
            client_ip=client_ip,
        )
    except Exception:
        LOGGER.exception("product event capture failed")

    response = Response(content=response_body, status_code=status)
    response.raw_headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in response_headers
    ]
    if is_login_path and response_data.get("ok") and pending_sid:
        _clear_cookie(
            response,
            name=pending_cookie_name,
            secure=bool(request.app.state.settings.cookie_secure),
        )
    return response


def _load_runtime() -> tuple[Any, Any]:
    # Imports stay lazy so repository-only tests that do not install production
    # dependencies can continue importing the legacy BFF.
    from bbw_prod.config import get_settings
    from bbw_web.persistence import RuntimePersistence

    settings = get_settings()
    return settings, RuntimePersistence(settings)


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.draining = False
    application.state.accepting_logins = True
    application.state.login_starts_in_flight = 0
    application.state.deploy_lock = threading.Lock()
    application.state.started_at = time.time()
    settings, persistence = _load_runtime()
    application.state.settings = settings
    application.state.persistence = persistence
    application.state.local_password_auth_gate = _LocalPasswordAuthGate(
        settings.local_password_auth_concurrency
    )
    application.state.deployment_control_token = (
        settings.load_deployment_control_token()
    )

    legacy.LAB_ENABLED = False
    legacy.INVITE_LOGIN_ENABLED = True
    legacy.MOMENT_VIDEO_COMPAT_ENABLED = True
    legacy.CORS_ALLOW_ORIGINS = set()
    legacy.COOKIE_SECURE = bool(settings.cookie_secure)
    legacy.COOKIE_NAME = str(settings.user_cookie_name)
    legacy.MAX_JSON_BODY_BYTES = int(settings.max_json_body_bytes)
    legacy.PRESENCE_BACKEND = persistence
    legacy.STORE = SessionStore(
        ttl_sec=float(settings.user_idle_ttl_seconds),
        auto_heartbeat=False,
        upstream_auth_timeout_sec=float(settings.upstream_auth_timeout_seconds),
        persist_sessions=False,
        allow_weak_onekey=False,
        pending_expire_callback=_logout_expired_pending_user,
        runtime_provider=persistence.runtime_provider,
    )
    try:
        persistence.startup()
        # The production deployment intentionally supports exactly one
        # super-administrator.  Bootstrap it only after the database and Redis
        # runtime are ready, while keeping failures inside this cleanup scope.
        from bbw_web.admin_api import bootstrap_initial_admin

        bootstrap_initial_admin(settings, persistence)
        yield
    finally:
        with application.state.deploy_lock:
            application.state.accepting_logins = False
            application.state.draining = True
        legacy.INVITE_LOGIN_ENABLED = False
        legacy.MOMENT_VIDEO_COMPAT_ENABLED = False
        legacy.PRESENCE_BACKEND = None
        for coordinator_name in (
            "presence_coordinator",
            "profile_lookup_coordinator",
        ):
            coordinator = getattr(persistence, coordinator_name, None)
            close = getattr(coordinator, "close", None)
            if callable(close):
                close()
        if legacy.STORE is not None:
            legacy.STORE.close()
        persistence.close()


app = FastAPI(
    title="BBW Web",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(RequestBodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def sanitized_validation_error(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    # FastAPI's default payload includes the rejected input value.  Password,
    # TOTP and token fields must never be reflected merely because validation
    # failed.
    details: list[dict[str, Any]] = []
    for error in exc.errors()[:50]:
        details.append(
            {
                "loc": [str(item)[:80] for item in error.get("loc", ())[:8]],
                "msg": str(error.get("msg") or "invalid value")[:240],
                "type": str(error.get("type") or "validation_error")[:120],
            }
        )
    return JSONResponse(
        {"ok": False, "detail": details},
        status_code=422,
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    started_at = time.monotonic()
    deploy_lock = getattr(request.app.state, "deploy_lock", None)
    login_start_admitted = False
    reject_login_start = False
    if request.method == "POST" and request.url.path in LOGIN_START_PATHS:
        if deploy_lock is None:
            reject_login_start = True
        else:
            with deploy_lock:
                if not bool(
                    getattr(request.app.state, "accepting_logins", False)
                ):
                    reject_login_start = True
                else:
                    request.app.state.login_starts_in_flight = max(
                        0,
                        int(
                            getattr(
                                request.app.state,
                                "login_starts_in_flight",
                                0,
                            )
                            or 0
                        ),
                    ) + 1
                    login_start_admitted = True
    response: Response
    try:
        if reject_login_start:
            response = JSONResponse(
                {
                    "ok": False,
                    "code": "SERVICE_DRAINING",
                    "error": "服务正在切换，请稍后重试登录",
                    "retryable": True,
                    "retry_after": 3,
                },
                status_code=503,
                headers={"Retry-After": "3"},
            )
        else:
            response = await call_next(request)
    except RuntimeError as exc:
        if str(exc) != "No response returned.":
            LOGGER.exception(
                json.dumps(
                    {
                        "event": "http_request_failed",
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path[:256],
                        "error_type": type(exc).__name__,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            raise
        # Starlette raises this exact error when the browser disconnects while
        # a blocking legacy route is still finishing in the thread pool.  The
        # route has no client left to receive a response, so record the common
        # reverse-proxy status instead of emitting an application traceback.
        response = Response(status_code=499)
    except Exception as exc:
        LOGGER.exception(
            json.dumps(
                {
                    "event": "http_request_failed",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path[:256],
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise
    finally:
        if login_start_admitted and deploy_lock is not None:
            with deploy_lock:
                request.app.state.login_starts_in_flight = max(
                    0,
                    int(
                        getattr(
                            request.app.state,
                            "login_starts_in_flight",
                            0,
                        )
                        or 0
                    )
                    - 1,
                )
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(self), microphone=(self), geolocation=(self), payment=(), usb=()",
    )
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        response.headers["Content-Security-Policy"] = HTML_CSP
    if request.url.path.startswith(("/api/", "/admin", "/internal/")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if request.url.path.startswith("/admin"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    route = request.scope.get("route")
    route_path = str(getattr(route, "path", "") or "")
    if not route_path or route_path == "/{path:path}":
        route_path = request.url.path[:256]
    status_code = int(response.status_code)
    duration_ms = round((time.monotonic() - started_at) * 1000, 2)
    response.headers["Server-Timing"] = f"app;dur={duration_ms:.2f}"
    if request.url.path not in {"/livez", "/readyz", "/healthz"} or status_code >= 400:
        LOGGER.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_path[:256],
                    "status": status_code,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if (
        duration_ms >= SLOW_HTTP_REQUEST_MS
        and request.url.path.startswith(("/api/", "/admin"))
    ):
        LOGGER.warning(
            json.dumps(
                {
                    "event": "slow_http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_path[:256],
                    "status": status_code,
                    "duration_ms": duration_ms,
                    "threshold_ms": SLOW_HTTP_REQUEST_MS,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return response


@app.get("/livez", include_in_schema=False)
def livez() -> dict[str, bool]:
    return {"ok": True}


def _readiness(request: Request, response: Response) -> dict[str, bool]:
    if bool(getattr(request.app.state, "draining", False)):
        response.status_code = 503
        return {"ok": False}
    try:
        state = request.app.state.persistence.health()
    except Exception:
        state = {"ok": False}
    if not state.get("ok"):
        response.status_code = 503
    # Keep dependency names and topology out of the public response.
    return {"ok": bool(state.get("ok"))}


@app.get("/readyz", include_in_schema=False)
def readyz(request: Request, response: Response) -> dict[str, bool]:
    return _readiness(request, response)


@app.get("/healthz", include_in_schema=False)
def healthz(request: Request, response: Response) -> dict[str, bool]:
    # Backward-compatible readiness alias for existing external monitors.
    return _readiness(request, response)


def _require_deployment_control(request: Request) -> None:
    expected = bytes(
        getattr(request.app.state, "deployment_control_token", b"") or b""
    )
    authorization = str(request.headers.get("Authorization") or "")
    scheme, separator, credential = authorization.partition(" ")
    supplied = (
        credential.strip().encode("utf-8")
        if separator and scheme.lower() == "bearer"
        else b""
    )
    if (
        not expected
        or len(supplied) > 512
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(status_code=404, detail="not found")


@app.post("/internal/drain", include_in_schema=False)
def begin_drain(request: Request, response: Response) -> dict[str, Any]:
    _require_deployment_control(request)
    with request.app.state.deploy_lock:
        request.app.state.accepting_logins = False
        login_starts_in_flight = max(
            0,
            int(getattr(request.app.state, "login_starts_in_flight", 0) or 0),
        )
        store_state = legacy.STORE.stats() if legacy.STORE is not None else {}
        pending_logins = max(0, int(store_state.get("pending_logins") or 0))
        if login_starts_in_flight == 0 and pending_logins == 0:
            request.app.state.draining = True
        draining = bool(getattr(request.app.state, "draining", False))
    if login_starts_in_flight > 0 or pending_logins > 0:
        retry_after = 3 if login_starts_in_flight > 0 else 30
        response.status_code = 409
        response.headers["Retry-After"] = str(retry_after)
        return {
            "ok": False,
            "draining": draining,
            "accepting_logins": False,
            "login_starts_in_flight": login_starts_in_flight,
            "pending_logins": pending_logins,
            "retry_after": retry_after,
        }
    LOGGER.info(
        json.dumps(
            {
                "event": "application_draining",
                "request_id": str(getattr(request.state, "request_id", ""))[:64],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return {
        "ok": True,
        "draining": True,
        "accepting_logins": False,
        "login_starts_in_flight": 0,
        "pending_logins": 0,
    }


@app.post("/internal/resume", include_in_schema=False)
def resume_after_drain(request: Request) -> dict[str, bool]:
    _require_deployment_control(request)
    with request.app.state.deploy_lock:
        request.app.state.draining = False
        request.app.state.accepting_logins = True
    LOGGER.info(
        json.dumps(
            {
                "event": "application_resumed",
                "request_id": str(getattr(request.state, "request_id", ""))[:64],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return {"ok": True, "draining": False, "accepting_logins": True}


@app.get("/api/health", include_in_schema=False)
def public_health() -> dict[str, Any]:
    # Do not expose active session counts, persistence mode or Lab state.
    return {"ok": True, "service": "bbw-web", "version": 1}


class ConversationPreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_all_conversations: StrictBool


def _conversation_preferences_identity(request: Request) -> Any:
    persistence = request.app.state.persistence
    sid = str(request.cookies.get(legacy.COOKIE_NAME) or "")
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not persistence.rate_limit(
        f"conversation-preferences:user:{identity.user_id}",
        limit=120,
        window_seconds=60,
    ):
        raise HTTPException(
            status_code=429,
            detail="聊天列表设置操作过于频繁，请稍后重试",
            headers={"Retry-After": "60"},
        )
    return identity


@app.get("/api/preferences/conversations", include_in_schema=False)
def conversation_preferences(request: Request) -> dict[str, Any]:
    persistence = request.app.state.persistence
    identity = _conversation_preferences_identity(request)
    return {
        "ok": True,
        "show_all_conversations": persistence.get_show_all_conversations(identity),
    }


@app.put("/api/preferences/conversations", include_in_schema=False)
def update_conversation_preferences(
    request: Request,
    body: ConversationPreferencesBody,
) -> JSONResponse:
    request_error = _auth_json_request_error(request)
    if request_error is not None:
        return request_error
    persistence = request.app.state.persistence
    identity = _conversation_preferences_identity(request)
    enabled = persistence.set_show_all_conversations(
        identity,
        enabled=body.show_all_conversations,
    )
    return JSONResponse({"ok": True, "show_all_conversations": enabled})


@app.get("/api/auth/security", include_in_schema=False)
def auth_security(request: Request, phone: str = "") -> dict[str, Any]:
    client_ip = _client_ip(request)
    if not request.app.state.persistence.rate_limit(
        f"auth-security:{client_ip}", limit=60, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="请求过于频繁")
    return {
        "ok": True,
        **request.app.state.persistence.login_security_state(
            phone=str(phone or "").strip()[:32],
            client_ip=client_ip,
        ),
    }


def _pending_login_response(
    request: Request,
    payload: dict[str, Any],
    *,
    status_code: int,
    clear_pending: bool = False,
    set_session_sid: str | None = None,
) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    secure = bool(request.app.state.settings.cookie_secure)
    if set_session_sid:
        _set_cookie(
            response,
            name=legacy.COOKIE_NAME,
            value=set_session_sid,
            secure=secure,
        )
    if clear_pending:
        _clear_cookie(
            response,
            name=_pending_cookie_name(legacy.COOKIE_NAME),
            secure=secure,
        )
    return response


def _finish_pending_invite_sync(
    request: Request, invite_code: str
) -> JSONResponse:
    from bbw_web.persistence import (
        PendingLoginConflict,
        PendingLoginExpired,
        PendingLoginRejected,
    )

    persistence = request.app.state.persistence
    client_ip = _client_ip(request)
    user_agent = str(request.headers.get("User-Agent") or "")[:512]
    pending_cookie = _pending_cookie_name(legacy.COOKIE_NAME)
    pending_sid = str(request.cookies.get(pending_cookie) or "")
    if not _valid_pending_sid(pending_sid):
        return _pending_login_response(
            request,
            {"ok": False, "error": "登录验证已失效，请重新登录"},
            status_code=401,
            clear_pending=True,
        )
    if not persistence.rate_limit(
        f"invite-verify:{pending_sid}:{client_ip}", limit=8, window_seconds=5 * 60
    ):
        return _pending_login_response(
            request,
            {"ok": False, "error": "邀请码验证过于频繁，请稍后重试"},
            status_code=429,
        )
    web_user = legacy.STORE.get(pending_sid) if legacy.STORE is not None else None
    if web_user is None:
        try:
            persistence.peek_pending_login(
                pending_sid, client_ip=client_ip, user_agent=user_agent
            )
        except PendingLoginExpired:
            return _pending_login_response(
                request,
                {"ok": False, "error": "登录验证已失效，请重新登录"},
                status_code=401,
                clear_pending=True,
            )
        return _pending_login_response(
            request,
            {
                "ok": False,
                "retryable": True,
                "error": "登录验证正在同步，请稍后重试",
            },
            status_code=503,
        )
    if not web_user.app.session.logged_in:
        _cancel_pending_runtime(persistence, pending_sid)
        return _pending_login_response(
            request,
            {"ok": False, "error": "登录验证已失效，请重新登录"},
            status_code=401,
            clear_pending=True,
        )
    try:
        with web_user.lock:
            persistence.finish_pending_login(
                raw_sid=pending_sid,
                web_user=web_user,
                invite_code=invite_code,
                old_sid=request.cookies.get(legacy.COOKIE_NAME),
                client_ip=client_ip,
                user_agent=user_agent,
            )
    except PendingLoginExpired as exc:
        # A simultaneous duplicate completion may observe the one-time marker
        # after the first request has already promoted this same SID.
        if persistence.require_identity(pending_sid) is not None:
            return _pending_login_response(
                request,
                {"ok": True, **web_user.public()},
                status_code=200,
                clear_pending=True,
                set_session_sid=pending_sid,
            )
        _cancel_pending_runtime(persistence, pending_sid)
        return _pending_login_response(
            request,
            {"ok": False, "error": str(exc)},
            status_code=401,
            clear_pending=True,
        )
    except PendingLoginRejected as exc:
        _cancel_pending_runtime(persistence, pending_sid)
        return _pending_login_response(
            request,
            {"ok": False, "pending_cleared": True, "error": str(exc)},
            status_code=403,
            clear_pending=True,
        )
    except PendingLoginConflict as exc:
        _cancel_pending_runtime(persistence, pending_sid)
        return _pending_login_response(
            request,
            {"ok": False, "pending_cleared": True, "error": str(exc)},
            status_code=409,
            clear_pending=True,
        )
    except PermissionError as exc:
        try:
            persistence.peek_pending_login(
                pending_sid, client_ip=client_ip, user_agent=user_agent
            )
        except PendingLoginExpired:
            _cancel_pending_runtime(persistence, pending_sid)
            return _pending_login_response(
                request,
                {"ok": False, "error": "邀请码状态已变化，请重新登录"},
                status_code=409,
                clear_pending=True,
            )
        return _pending_login_response(
            request,
            {"ok": False, "error": str(exc)},
            status_code=403,
        )
    except Exception:
        LOGGER.exception("pending invitation completion failed")
        if persistence.require_identity(pending_sid) is not None:
            return _pending_login_response(
                request,
                {"ok": True, **web_user.public()},
                status_code=200,
                clear_pending=True,
                set_session_sid=pending_sid,
            )
        _cancel_pending_runtime(persistence, pending_sid)
        return _pending_login_response(
            request,
            {
                "ok": False,
                "pending_cleared": True,
                "error": "登录状态保存失败，请重新登录",
            },
            status_code=503,
            clear_pending=True,
        )
    return _pending_login_response(
        request,
        {"ok": True, **web_user.public()},
        status_code=200,
        clear_pending=True,
        set_session_sid=pending_sid,
    )


@app.post("/api/auth/invite", include_in_schema=False)
async def complete_pending_invite(request: Request) -> JSONResponse:
    request_error = _auth_json_request_error(request)
    if request_error is not None:
        return request_error
    data = _json_object(await request.body())
    invite_code = str(data.get("invite_code") or "").strip()
    if not invite_code:
        return _pending_login_response(
            request,
            {"ok": False, "error": "请输入有效邀请码"},
            status_code=400,
        )
    return await run_in_threadpool(_finish_pending_invite_sync, request, invite_code)


@app.post("/api/auth/invite/cancel", include_in_schema=False)
async def cancel_pending_invite(request: Request) -> JSONResponse:
    request_error = _auth_json_request_error(request)
    if request_error is not None:
        return request_error
    pending_sid = str(
        request.cookies.get(_pending_cookie_name(legacy.COOKIE_NAME)) or ""
    )
    if _valid_pending_sid(pending_sid):
        await run_in_threadpool(
            _cancel_pending_runtime,
            request.app.state.persistence,
            pending_sid,
        )
    return _pending_login_response(
        request,
        {"ok": True},
        status_code=200,
        clear_pending=True,
    )


def _static_asset_path(relative_path: str) -> Path:
    candidate = (STATIC_DIR / str(relative_path or "")).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="static asset not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="static asset not found")
    return candidate


def _static_asset_cache_control(request: Request, path: Path) -> str:
    try:
        relative = path.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return "no-cache"
    if request.query_params.get("v") or "vendor" in relative.parts:
        return "public, max-age=31536000, immutable"
    if path.suffix.lower() in {".html", ".js"}:
        return "no-cache"
    return "public, max-age=300"


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def product_page() -> FileResponse:
    return FileResponse(
        _static_asset_path("index.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.api_route(
    "/static/{asset_path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
def static_asset(request: Request, asset_path: str) -> FileResponse:
    path = _static_asset_path(asset_path)
    return FileResponse(
        path,
        headers={"Cache-Control": _static_asset_cache_control(request, path)},
    )


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    path = STATIC_DIR / "admin.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="admin UI is not installed")
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/admin/", include_in_schema=False)
def admin_page_slash() -> FileResponse:
    # Serve the same no-store document instead of relying on an implicit
    # redirect, so reverse proxies cannot accidentally cache a privileged
    # entry-point response.
    return admin_page()


@app.post("/admin", include_in_schema=False)
@app.post("/admin/", include_in_schema=False)
def admin_requires_javascript() -> JSONResponse:
    # Every management form is handled by admin.js.  If that script is blocked
    # or fails to load, reject the fallback submission without parsing or
    # reflecting password/TOTP fields in a validation response.
    return JSONResponse(
        {"ok": False, "error": "管理端需要启用 JavaScript"},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


# Admin and archive routes are registered in separate modules to keep this
# adapter reviewable and to avoid mixing privileged APIs into the product BFF.
from bbw_web.admin_api import router as admin_router  # noqa: E402
from bbw_agent.api import router as agent_router  # noqa: E402
from bbw_web.archive_api import router as archive_router  # noqa: E402
from bbw_web.media_api import router as media_router  # noqa: E402
from bbw_web.native_moments_api import moderation_router  # noqa: E402
from bbw_web.native_media_api import router as native_media_router  # noqa: E402
from bbw_web.moment_media_api import (  # noqa: E402
    MomentVideoServiceError,
    router as moment_media_router,
)


@app.exception_handler(MomentVideoServiceError)
async def moment_video_service_error(
    request: Request,
    exc: MomentVideoServiceError,
) -> JSONResponse:
    request_id = str(getattr(request.state, "request_id", ""))[:64]
    payload: dict[str, Any] = {
        "ok": False,
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
    }
    if exc.retry_after > 0:
        payload["retry_after"] = exc.retry_after
    if request_id:
        payload["request_id"] = request_id
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after > 0:
        headers["Retry-After"] = str(exc.retry_after)
    LOGGER.warning(
        json.dumps(
            {
                "event": "moment_video_service_error",
                "request_id": request_id,
                "code": exc.code,
                "status": exc.status_code,
                "retryable": exc.retryable,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return JSONResponse(payload, status_code=exc.status_code, headers=headers)

app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(archive_router)
app.include_router(media_router)
app.include_router(moderation_router)
app.include_router(native_media_router)
app.include_router(moment_media_router)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def legacy_routes(request: Request, path: str) -> Response:
    return await _legacy_dispatch(request)
