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
import io
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from bbw_web import bff_server as legacy
from bbw_web.store import SessionStore


STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGGER = logging.getLogger(__name__)

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

        async with self._slots:
            await self._handle_limited(scope, receive, send)

    async def _handle_limited(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:

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
                int(getattr(settings, "media_max_image_bytes", 10 * 1024 * 1024))
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
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.application(scope, replay, send)


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
        message_block_snapshot_guard: Optional[Callable[[str], Any]] = None,
        message_block_snapshot_recorder: Optional[
            Callable[[str, Iterable[str]], None]
        ] = None,
        match_history_loader: Optional[Callable[[int], dict[str, Any]]] = None,
        match_history_recorder: Optional[Callable[[str, dict[str, Any]], None]] = None,
        conversation_summary_loader: Optional[
            Callable[[list[str]], dict[str, dict[str, Any]]]
        ] = None,
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
        self._request_message_block_snapshot_guard = message_block_snapshot_guard
        self._request_message_block_snapshot_recorder = message_block_snapshot_recorder
        self._request_match_history_loader = match_history_loader
        self._request_match_history_recorder = match_history_recorder
        self._request_conversation_summary_loader = conversation_summary_loader
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self._held_user_lock = None
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
        try:
            web_user.app.client.close()
        except Exception:
            pass


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

    # Never trust an authenticated object that only remains in process memory.
    # PostgreSQL/Redis are the authority for idle/absolute expiry and revocation.
    identity = persistence.require_identity(str(sid or "")) if sid else None
    if sid and identity is None and legacy.STORE is not None:
        legacy.STORE.drop(sid)

    if path.startswith("/api/") and path != "/api/health":
        limiter_identity = (
            f"user:{identity.user_id}" if identity is not None else f"ip:{client_ip}"
        )
        if not persistence.rate_limit(
            f"api:{limiter_identity}", limit=60, window_seconds=60
        ):
            return JSONResponse(
                {"ok": False, "error": "请求过于频繁，请稍后重试"},
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

    message_policy_allowed_peers: tuple[str, ...] = ()
    message_policy_match_peers: tuple[str, ...] = ()
    message_policy_blocked_peers: tuple[str, ...] = ()
    if identity is not None and path == "/api/im/message-policy":
        try:
            message_policy_allowed_peers = tuple(
                persistence.message_policy_allowed_peers(identity)
            )
        except Exception:
            LOGGER.exception("message policy allowed peer lookup failed")
        try:
            message_policy_match_peers = tuple(
                persistence.message_policy_match_peers(identity)
            )
        except Exception:
            LOGGER.exception("message policy match peer lookup failed")
        try:
            message_policy_blocked_peers = tuple(
                persistence.message_policy_blocked_peers(identity)
            )
        except Exception:
            LOGGER.exception("message policy blocked peer lookup failed")

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

    message_block_snapshot_guard: Optional[Callable[[str], Any]] = None
    message_block_snapshot_recorder: Optional[
        Callable[[str, Iterable[str]], None]
    ] = None
    if identity is not None:
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
        message_block_snapshot_guard=message_block_snapshot_guard,
        message_block_snapshot_recorder=message_block_snapshot_recorder,
        match_history_loader=match_history_loader,
        match_history_recorder=match_history_recorder,
        conversation_summary_loader=conversation_summary_loader,
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
            return JSONResponse({"ok": False, "error": "method not allowed"}, status_code=405)
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
                    mode="sms" if path == "/api/auth/sms-login" else "password",
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
    settings, persistence = _load_runtime()
    application.state.settings = settings
    application.state.persistence = persistence

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
        persist_sessions=False,
        allow_weak_onekey=False,
        pending_expire_callback=_logout_expired_pending_user,
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
        legacy.INVITE_LOGIN_ENABLED = False
        legacy.MOMENT_VIDEO_COMPAT_ENABLED = False
        legacy.PRESENCE_BACKEND = None
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
    try:
        response = await call_next(request)
    except RuntimeError as exc:
        if str(exc) != "No response returned.":
            raise
        # Starlette raises this exact error when the browser disconnects while
        # a blocking legacy route is still finishing in the thread pool.  The
        # route has no client left to receive a response, so record the common
        # reverse-proxy status instead of emitting an application traceback.
        return Response(status_code=499)
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
    if request.url.path.startswith(("/api/", "/admin")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if request.url.path.startswith("/admin"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.get("/healthz", include_in_schema=False)
def healthz(request: Request, response: Response) -> dict[str, Any]:
    state = request.app.state.persistence.health()
    if not state.get("ok"):
        response.status_code = 503
    # Keep dependency names and topology out of the public response.
    return {"ok": bool(state.get("ok"))}


@app.get("/api/health", include_in_schema=False)
def public_health() -> dict[str, Any]:
    # Do not expose active session counts, persistence mode or Lab state.
    return {"ok": True, "service": "bbw-web", "version": 1}


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
from bbw_web.archive_api import router as archive_router  # noqa: E402
from bbw_web.media_api import router as media_router  # noqa: E402
from bbw_web.moment_media_api import router as moment_media_router  # noqa: E402

app.include_router(admin_router)
app.include_router(archive_router)
app.include_router(media_router)
app.include_router(moment_media_router)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def legacy_routes(request: Request, path: str) -> Response:
    return await _legacy_dispatch(request)
