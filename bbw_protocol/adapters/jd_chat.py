"""Banghua JD Chat HTTP adapter used by the Web BFF.

The v162 Android client authenticates this API with the account ``user_token``
as a Bearer token.  This is deliberately kept server-side; browser code never
receives the token.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import ssl
import threading
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

import httpx


JD_CHAT_BASE = "https://test.banghua.xin"


# The Android Retrofit service returns a bare JSON list today.  Older test and
# staging deployments have wrapped the same list in one or more ``data`` /
# ``items`` / ``list`` objects, however.  Keep the expansion in the adapter so
# callers do not each grow subtly different response-shape heuristics.
_COLLECTION_KEYS = (
    "data",
    "items",
    "list",
    "records",
    "messages",
    "conversations",
    "content",
    "results",
)
_MESSAGE_KEYS = frozenset(
    {
        "messageId",
        "message_id",
        "fromUserId",
        "from_user_id",
        "toUserId",
        "to_user_id",
        "senderId",
        "sender_id",
        "receiverId",
        "receiver_id",
        "sequence",
        "timestamp",
        "unreadCount",
    }
)


def extract_collection_rows(payload: Any) -> list[dict[str, Any]]:
    """Return message/conversation rows from all known JD response shapes.

    ``ChatMessage`` itself is a mapping (it contains ``content``), so a mapping
    that already looks like one row must not be mistaken for an envelope.  The
    depth cap and ``seen`` set keep malformed recursive payloads harmless.
    Invalid scalar values are ignored rather than raising in a read path.
    """

    seen: set[int] = set()

    def walk(value: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 8 or value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if raw[:1] in {"[", "{"}:
                try:
                    return walk(json.loads(raw), depth + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return []
            return []
        if isinstance(value, (list, tuple)):
            rows: list[dict[str, Any]] = []
            for item in value:
                if isinstance(item, Mapping):
                    rows.append(dict(item))
                else:
                    rows.extend(walk(item, depth + 1))
            return rows
        if not isinstance(value, Mapping):
            return []
        marker = id(value)
        if marker in seen:
            return []
        seen.add(marker)
        mapping = dict(value)
        keys = {str(key) for key in mapping}
        if keys & _MESSAGE_KEYS:
            return [mapping]
        for key in _COLLECTION_KEYS:
            if key not in mapping:
                continue
            child = mapping.get(key)
            # An explicitly present empty list is an authoritative empty
            # collection; do not continue into unrelated envelope fields.
            if isinstance(child, (list, tuple)) and not child:
                return []
            rows = walk(child, depth + 1)
            if rows:
                return rows
            if child in (None, "", {}):
                return []
        return []

    return walk(payload)


@dataclass
class JdChatResult:
    ok: bool
    status_code: int = 0
    data: Any = None
    error_info: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status_code,
            "error": self.error_info or None,
            "data": self.data,
        }


class JdChatClient:
    """Small, token-aware client for the OpenAPI chat endpoints."""

    def __init__(self, token: str = "", *, session: Any = None, base_url: str = JD_CHAT_BASE, timeout: float = 8.0):
        self.token = str(token or "").strip()
        self.session = session
        self.base_url = str(base_url or JD_CHAT_BASE).rstrip("/")
        self.timeout = float(timeout)
        self._http = httpx.Client(follow_redirects=True, verify=ssl.create_default_context())

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Any = None,
        timeout: Optional[float] = None,
    ) -> JdChatResult:
        token = self._access_token()
        if not token:
            return JdChatResult(False, error_info="当前会话缺少聊天 token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            request_timeout = self.timeout
            if timeout is not None:
                try:
                    request_timeout = max(0.1, float(timeout))
                except (TypeError, ValueError, OverflowError):
                    request_timeout = self.timeout
            response = self._http.request(
                method, self.base_url + "/" + path.lstrip("/"),
                headers=headers, json=json_body, params=params, timeout=request_timeout,
            )
            raw = response.text
            try:
                payload = response.json()
            except Exception:
                payload = {"raw": raw}
            ok = 200 <= response.status_code < 300
            if isinstance(payload, dict):
                # APIs in different deployments use either success/ok or code.
                if payload.get("ok") is False or payload.get("success") is False:
                    ok = False
                code = payload.get("code")
                try:
                    if code is not None and int(code) >= 400:
                        ok = False
                except (TypeError, ValueError):
                    pass
                error = "" if ok else str(payload.get("message") or payload.get("error") or payload.get("errorInfo") or "")
            else:
                error = "" if ok else raw[:500]
            return JdChatResult(ok, response.status_code, payload, error, raw)
        except Exception as exc:
            return JdChatResult(False, error_info=str(exc))

    def send_text(
        self,
        sender_id: str,
        receiver_id: str,
        content: str,
        *,
        client_message_id: str = "",
    ) -> JdChatResult:
        """Send via the APK-compatible WebSocket, with legacy HTTP fallback.

        ``client_message_id`` is an idempotency/correlation hint from the Web
        client.  JD's wire model still owns the UUID ``messageId``; the hint is
        carried as ``clientMessageId`` when the WebSocket accepts unknown JSON
        members and is echoed by the BFF, allowing optimistic rows to converge
        without changing the APK protocol.
        """
        ws_sender = self.send_text_ws
        # Bind before invoking so an older injected adapter lacking the new
        # optional keyword is selected without a speculative first send.  A
        # TypeError raised *inside* a compatible implementation must not cause
        # a second invocation (and therefore cannot create a duplicate).
        try:
            try:
                signature = inspect.signature(ws_sender)
                signature.bind(
                    sender_id,
                    receiver_id,
                    content,
                    client_message_id=client_message_id,
                )
            except TypeError:
                result = ws_sender(sender_id, receiver_id, content)
            except (ValueError, AttributeError):
                # Some proxy callables do not expose a signature; the current
                # adapter accepts the keyword, so invoke it once and trust its
                # delivery markers if it reports a failure.
                result = ws_sender(
                    sender_id,
                    receiver_id,
                    content,
                    client_message_id=client_message_id,
                )
            else:
                result = ws_sender(
                    sender_id,
                    receiver_id,
                    content,
                    client_message_id=client_message_id,
                )
        except Exception as exc:
            # An implementation exception after invocation does not prove the
            # frame was rejected.  Report an indeterminate delivery so direct
            # callers and the BFF both suppress any second provider send.
            return JdChatResult(
                False,
                502,
                {
                    "delivery_uncertain": True,
                    "ws_sent": True,
                    "clientMessageId": str(client_message_id or ""),
                    "client_message_id": str(client_message_id or ""),
                },
                str(exc),
            )
        result_ok = bool(
            result.get("ok", False)
            if isinstance(result, Mapping)
            else getattr(result, "ok", False)
        )
        if result_ok:
            return result

        # A WebSocket write is not transactional from the caller's point of
        # view: the server may have accepted and persisted the frame even when
        # the ACK is lost (or the connection closes while ``send`` returns).
        # Retrying through HTTP in that state creates a second message.  The
        # WS adapter marks this condition explicitly; honour the marker before
        # considering the legacy endpoint.  Keep the helper local/defensive so
        # injected test doubles and older adapter results are handled too.
        result_data = (
            result.get("data")
            if isinstance(result, Mapping)
            else getattr(result, "data", None)
        )
        if isinstance(result_data, Mapping):
            uncertain = result_data.get("delivery_uncertain") is True
            sent = result_data.get("ws_sent") is True
            if not (uncertain or sent):
                nested = result_data.get("data")
                if isinstance(nested, Mapping):
                    uncertain = nested.get("delivery_uncertain") is True
                    sent = nested.get("ws_sent") is True
            if uncertain or sent:
                return result
        try:
            sender, receiver = int(str(sender_id)), int(str(receiver_id))
        except (TypeError, ValueError):
            return result
        text = str(content or "").strip()
        if not text:
            return result
        return self._request(
            "POST", "/api/im/messages/send",
            json_body={"senderId": sender, "receiverId": receiver, "content": text},
        )

    def send_text_ws(
        self,
        sender_id: str,
        receiver_id: str,
        content: str,
        *,
        device_id: str = "",
        client_message_id: str = "",
    ) -> JdChatResult:
        """Send through the same WebSocket path used by APK v162."""
        token = self._access_token()
        try:
            sender, receiver = int(str(sender_id)), int(str(receiver_id))
        except (TypeError, ValueError):
            return JdChatResult(False, error_info="聊天用户 ID 必须是数字")
        if not token or not str(content or "").strip():
            return JdChatResult(False, error_info="聊天凭证或消息内容为空")
        client_id = str(client_message_id or "").strip()[:512]
        message = {
            "messageId": str(uuid.uuid4()),
            "fromUserId": sender, "toUserId": receiver,
            "content": str(content).strip(), "type": "TEXT",
            "timestamp": int(time.time() * 1000), "status": "SENT",
        }
        if client_id:
            # The Android ChatMessage DTO has no client id field, but the JD
            # WebSocket endpoint is tolerant of additional JSON members.  It
            # is useful on deployments that persist metadata and harmless on
            # deployments that discard it.
            message["clientMessageId"] = client_id
        session_device = str(getattr(self.session, "device_id", "") or "").strip()
        if not session_device:
            seed = str(getattr(self.session, "uid", "") or sender_id or token)
            session_device = "web-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
            if self.session is not None:
                try:
                    self.session.device_id = session_device
                except Exception:
                    pass
        effective_device = str(device_id or session_device or "web").strip() or session_device
        ws_base = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        if "test.banghua.xin" in ws_base and "testchat.banghua.xin" not in ws_base:
            ws_base = ws_base.replace("test.banghua.xin", "testchat.banghua.xin")
        url = ws_base.rstrip("/") + "/ws/chat?token=" + urllib.parse.quote(token, safe="") + "&deviceId=" + urllib.parse.quote(effective_device, safe="")
        # Keep send state outside the coroutine: ``asyncio.run`` may execute
        # in a worker thread and an exception can escape before ``run``
        # returns its control mapping.  The caller still needs to know whether
        # a frame reached the socket so it never performs a duplicate HTTP
        # retry after an ambiguous post-write failure.
        delivery_state = {"send_started": False, "sent": False}
        async def run() -> Any:
            # ``send_started`` deliberately flips before awaiting the write.
            # If the coroutine raises at that boundary, delivery is still
            # ambiguous and a second attempt must not be made.
            sent = False
            send_started = False
            try:
                import websockets
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    ws_cm = websockets.connect(
                        url,
                        additional_headers=headers,
                        ping_interval=30,
                        close_timeout=2,
                    )
                except TypeError:
                    ws_cm = websockets.connect(
                        url,
                        extra_headers=headers,
                        ping_interval=30,
                        close_timeout=2,
                    )
                async with ws_cm as ws:
                    # Once the frame has been handed to the socket, a timeout
                    # or malformed ACK is an *ambiguous* delivery outcome.
                    # Returning that state lets send_text avoid issuing a
                    # second HTTP message (which would create duplicates).
                    send_started = True
                    delivery_state["send_started"] = True
                    await ws.send(
                        json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                    )
                    sent = True
                    delivery_state["sent"] = True
                    try:
                        for _ in range(3):
                            raw = await asyncio.wait_for(ws.recv(), timeout=3)
                            item = json.loads(raw) if isinstance(raw, str) else raw
                            if isinstance(item, dict):
                                kind = str(
                                    item.get("type")
                                    or item.get("messageType")
                                    or item.get("message_type")
                                    or ""
                                ).upper()
                                if kind in {"ACK", "ERROR", "NACK"}:
                                    return {"_ack": item, "_sent": sent}
                                nested = item.get("data")
                                if isinstance(nested, dict) and str(
                                    nested.get("type")
                                    or nested.get("messageType")
                                    or nested.get("message_type")
                                    or ""
                                ).upper() in {"ACK", "ERROR", "NACK"}:
                                    return {"_ack": nested, "_sent": sent}
                        return {"_timeout": True, "_sent": sent}
                    except asyncio.TimeoutError:
                        return {"_timeout": True, "_sent": sent}
            except Exception as exc:
                # Connection/open/send failures before a frame is accepted are
                # safe to retry through the legacy HTTP endpoint.  Preserve a
                # marker if the exception happened after send so callers never
                # perform a second delivery attempt blindly.
                return {
                    "_transport_error": str(exc),
                    "_sent": bool(sent or send_started),
                }

        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                ack = asyncio.run(run())
            else:
                box: list[Any] = []
                errors: list[BaseException] = []
                def worker() -> None:
                    try:
                        box.append(asyncio.run(run()))
                    except BaseException as exc:
                        errors.append(exc)
                thread = threading.Thread(target=worker, daemon=True)
                thread.start(); thread.join(self.timeout + 2)
                if errors:
                    raise errors[0]
                if not box:
                    sent = bool(
                        delivery_state.get("sent")
                        or delivery_state.get("send_started")
                    )
                    return JdChatResult(
                        False,
                        504,
                        {
                            "data": message,
                            "ack": None,
                            "delivery_uncertain": True,
                            "ws_sent": sent,
                        },
                        "WebSocket send timed out",
                    )
                ack = box[0]
            if isinstance(ack, Mapping) and ack.get("_timeout"):
                return JdChatResult(
                    False,
                    504,
                    {
                        "data": message,
                        "ack": None,
                        "delivery_uncertain": True,
                        "ws_sent": bool(ack.get("_sent", True)),
                    },
                    "服务器未返回消息确认",
                )
            if isinstance(ack, Mapping) and ack.get("_transport_error"):
                sent = bool(ack.get("_sent"))
                return JdChatResult(
                    False,
                    502 if sent else 0,
                    {
                        "data": message,
                        "ack": None,
                        "delivery_uncertain": sent,
                        "ws_sent": sent,
                    },
                    str(ack.get("_transport_error") or "WebSocket 连接失败"),
                )
            if not isinstance(ack, Mapping):
                # ``run`` should always return a control mapping.  Treat an
                # unexpected value as an ambiguous post-write outcome rather
                # than accidentally reporting success and/or retrying.
                return JdChatResult(
                    False,
                    502,
                    {
                        "data": message,
                        "ack": ack,
                        "delivery_uncertain": True,
                        "ws_sent": True,
                    },
                    "服务器未返回有效消息确认",
                )
            if isinstance(ack, Mapping):
                ack = dict(ack)
                if "_ack" in ack:
                    ack = ack.get("_ack")
                    if not isinstance(ack, Mapping):
                        return JdChatResult(
                            False,
                            502,
                            {
                                "data": message,
                                "ack": ack,
                                "delivery_uncertain": True,
                                "ws_sent": True,
                            },
                            "服务器未返回有效消息确认",
                        )
                    ack = dict(ack)
                ack_type = str(
                    ack.get("type")
                    or ack.get("messageType")
                    or ack.get("message_type")
                    or ""
                ).upper()
                ack_status = str(ack.get("status") or "").upper()
                if ack_type in {"ERROR", "NACK"} or ack_status in {"ERROR", "FAILED", "FAIL", "REJECTED"}:
                    return JdChatResult(
                        False,
                        502,
                        {
                            "data": message,
                            "ack": ack,
                            "delivery_uncertain": True,
                            "ws_sent": True,
                        },
                        str(
                            ack.get("content")
                            or ack.get("message")
                            or "消息服务器拒绝发送"
                        ),
                    )
                ack_id = str(
                    ack.get("content")
                    or ack.get("messageId")
                    or ack.get("ackMessageId")
                    or ack.get("ack_message_id")
                    or ack.get("originalMessageId")
                    or ack.get("original_message_id")
                    or ack.get("message_id")
                    or ""
                )
                if isinstance(ack.get("content"), Mapping):
                    nested_content = ack.get("content")
                    ack_id = str(
                        nested_content.get("messageId")
                        or nested_content.get("message_id")
                        or nested_content.get("originalMessageId")
                        or nested_content.get("original_message_id")
                        or ack_id
                        or ""
                    )
                if ack_type != "ACK" or ack_id != message["messageId"]:
                    return JdChatResult(
                        False,
                        502,
                        {
                            "data": message,
                            "ack": ack,
                            "delivery_uncertain": True,
                            "ws_sent": True,
                        },
                        "服务器未确认本条消息",
                    )
            return JdChatResult(
                True,
                200,
                {
                    "data": message,
                    "ack": ack,
                    "messageId": message["messageId"],
                    "message_id": message["messageId"],
                    "clientMessageId": client_id,
                    "client_message_id": client_id,
                    "delivery_uncertain": False,
                    "ws_sent": True,
                },
            )
        except Exception as exc:
            # Preserve the post-write uncertainty marker even for failures
            # outside ``run`` (event-loop setup, thread orchestration, or
            # malformed ACK processing).  Without this, ``send_text`` would
            # incorrectly fall back to HTTP and deliver the same text twice.
            sent = bool(
                delivery_state.get("sent") or delivery_state.get("send_started")
            )
            return JdChatResult(
                False,
                502 if sent else 0,
                {
                    "data": message,
                    "ack": None,
                    "delivery_uncertain": sent,
                    "ws_sent": sent,
                },
                str(exc),
            )

    def history(
        self,
        *,
        peer_id: str,
        limit: int = 50,
        before: Optional[int] = None,
        before_seq: Optional[int] = None,
        timeout: Optional[float] = None,
        deadline: Any = None,
        **_kwargs: Any,
    ) -> JdChatResult:
        """Fetch a C2C history page (partnerId is required by v162 API)."""
        del deadline, _kwargs
        try:
            params = {"partnerId": int(str(peer_id)), "limit": int(limit)}
            if before is not None:
                params["before"] = int(before)
            if before_seq is not None:
                params["beforeSeq"] = int(before_seq)
        except (TypeError, ValueError):
            return JdChatResult(False, error_info="聊天用户 ID 必须是数字")
        return self._request("GET", "/api/messages/history", params=params, timeout=timeout)

    def conversations(
        self,
        *,
        limit: Optional[int] = None,
        timeout: Optional[float] = None,
        deadline: Any = None,
        **_kwargs: Any,
    ) -> JdChatResult:
        """Fetch the APK v162 conversation summary list.

        The current Retrofit endpoint has no required query parameters and
        returns a bare ``List<ChatMessage>``.  ``limit`` is optional for local
        deployments that expose pagination; it is omitted by default to match
        the APK request exactly.
        """

        del deadline, _kwargs
        params = None
        if limit is not None:
            try:
                params = {"limit": max(1, int(limit))}
            except (TypeError, ValueError, OverflowError):
                return JdChatResult(False, error_info="会话数量参数无效")
        return self._request(
            "GET",
            "/api/messages/conversations",
            params=params,
            timeout=timeout,
        )

    def _access_token(self) -> str:
        session = self.session
        raw = getattr(session, "raw_user", {}) if session is not None else {}
        if isinstance(raw, dict):
            for key in ("accessToken", "access_token", "user_token", "userToken"):
                value = str(raw.get(key) or "").strip()
                if value:
                    return value
        return str(getattr(session, "token", "") or self.token).strip()


__all__ = [
    "JD_CHAT_BASE",
    "JdChatClient",
    "JdChatResult",
    "extract_collection_rows",
]


