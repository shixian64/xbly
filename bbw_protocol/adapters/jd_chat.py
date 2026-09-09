"""Banghua JD Chat HTTP adapter used by the Web BFF.

The v162 Android client authenticates this API with the account ``user_token``
as a Bearer token.  This is deliberately kept server-side; browser code never
receives the token.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import asyncio
import json
import urllib.parse
import hashlib
import threading
import uuid
import time


JD_CHAT_BASE = "https://test.banghua.xin"


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

    def _request(self, method: str, path: str, *, json_body: Any = None, params: Any = None) -> JdChatResult:
        token = self._access_token()
        if not token:
            return JdChatResult(False, error_info="当前会话缺少聊天 token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._http.request(
                method, self.base_url + "/" + path.lstrip("/"),
                headers=headers, json=json_body, params=params, timeout=self.timeout,
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

    def send_text(self, sender_id: str, receiver_id: str, content: str) -> JdChatResult:
        """Send via the APK-compatible WebSocket, with legacy HTTP fallback."""
        result = self.send_text_ws(sender_id, receiver_id, content)
        if result.ok:
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

    def send_text_ws(self, sender_id: str, receiver_id: str, content: str, *, device_id: str = "") -> JdChatResult:
        """Send through the same WebSocket path used by APK v162."""
        token = self._access_token()
        try:
            sender, receiver = int(str(sender_id)), int(str(receiver_id))
        except (TypeError, ValueError):
            return JdChatResult(False, error_info="聊天用户 ID 必须是数字")
        if not token or not str(content or "").strip():
            return JdChatResult(False, error_info="聊天凭证或消息内容为空")
        message = {
            "messageId": str(uuid.uuid4()),
            "fromUserId": sender, "toUserId": receiver,
            "content": str(content).strip(), "type": "TEXT",
            "timestamp": int(time.time() * 1000), "status": "SENT",
        }
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
        async def run() -> Any:
            import websockets
            headers = {"Authorization": f"Bearer {token}"}
            try:
                ws_cm = websockets.connect(url, additional_headers=headers, ping_interval=30, close_timeout=2)
            except TypeError:
                ws_cm = websockets.connect(url, extra_headers=headers, ping_interval=30, close_timeout=2)
            async with ws_cm as ws:
                await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
                try:
                    for _ in range(3):
                        raw = await asyncio.wait_for(ws.recv(), timeout=3)
                        item = json.loads(raw) if isinstance(raw, str) else raw
                        if isinstance(item, dict) and str(item.get("type") or "").upper() in {"ACK", "ERROR", "NACK"}:
                            return item
                    return {"_timeout": True}
                except asyncio.TimeoutError:
                    return {"_timeout": True}

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
                    raise TimeoutError("WebSocket send timed out")
                ack = box[0]
            if isinstance(ack, dict) and ack.get("_timeout"):
                return JdChatResult(False, 504, {"data": message, "ack": None}, "服务器未返回消息确认")
            if isinstance(ack, dict):
                ack_type = str(ack.get("type") or "").upper()
                ack_status = str(ack.get("status") or "").upper()
                if ack_type in {"ERROR", "NACK"} or ack_status in {"ERROR", "FAILED", "FAIL", "REJECTED"}:
                    return JdChatResult(False, 502, {"data": message, "ack": ack}, str(ack.get("content") or ack.get("message") or "消息服务器拒绝发送"))
                ack_id = str(ack.get("content") or ack.get("messageId") or ack.get("ackMessageId") or "")
                if ack_type != "ACK" or ack_id != message["messageId"]:
                    return JdChatResult(False, 502, {"data": message, "ack": ack}, "服务器未确认本条消息")
            return JdChatResult(True, 200, {"data": message, "ack": ack})
        except Exception as exc:
            return JdChatResult(False, error_info=str(exc))

    def history(self, *, peer_id: str, limit: int = 50, before: Optional[int] = None, before_seq: Optional[int] = None) -> JdChatResult:
        """Fetch a C2C history page (partnerId is required by v162 API)."""
        try:
            params = {"partnerId": int(str(peer_id)), "limit": int(limit)}
            if before is not None:
                params["before"] = int(before)
            if before_seq is not None:
                params["beforeSeq"] = int(before_seq)
        except (TypeError, ValueError):
            return JdChatResult(False, error_info="聊天用户 ID 必须是数字")
        return self._request("GET", "/api/messages/history", params=params)

    def _access_token(self) -> str:
        session = self.session
        raw = getattr(session, "raw_user", {}) if session is not None else {}
        if isinstance(raw, dict):
            for key in ("accessToken", "access_token", "user_token", "userToken"):
                value = str(raw.get(key) or "").strip()
                if value:
                    return value
        return str(getattr(session, "token", "") or self.token).strip()


__all__ = ["JD_CHAT_BASE", "JdChatClient", "JdChatResult"]


