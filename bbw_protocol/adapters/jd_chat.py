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
        token = str(getattr(self.session, "token", "") or self.token).strip()
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
        try:
            sender = int(str(sender_id))
            receiver = int(str(receiver_id))
        except (TypeError, ValueError):
            return JdChatResult(False, error_info="聊天用户 ID 必须是数字")
        text = str(content or "").strip()
        if not text:
            return JdChatResult(False, error_info="消息内容不能为空")
        return self._request(
            "POST", "/api/im/messages/send",
            json_body={"senderId": sender, "receiverId": receiver, "content": text},
        )

    def history(self, *, peer_id: Optional[str] = None, limit: int = 50) -> JdChatResult:
        params = {"userId": peer_id, "limit": limit} if peer_id else {"limit": limit}
        return self._request("GET", "/api/messages/history", params=params)


__all__ = ["JD_CHAT_BASE", "JdChatClient", "JdChatResult"]
