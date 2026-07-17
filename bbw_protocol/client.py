"""HTTP protocol engine."""

from __future__ import annotations

import json
import mimetypes
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from urllib import parse

import httpx

from . import sign
from .session import Session

APPLET = "https://applet.banghua.xin/app/index.php"
REDIS = "https://redis.banghua.xin/app/index.php"
SMS_URL = "https://applet.banghua.xin/sms_beibeiwu.php"
TXIM_SIGN = "https://applet.banghua.xin/tximsign.php"
RONG_REGISTER = (
    "https://applet.banghua.xin/otherinterface/rongyun/"
    "RongCloudNew/example/User/userregister.php"
)
AGORA_RTC = (
    "https://applet.banghua.xin/otherinterface/agora/sample/"
    "RtcTokenBuilderSampleXiaobei.php"
)
AGORA_RTM = (
    "https://applet.banghua.xin/otherinterface/agora/sample/"
    "RtmTokenBuilderSampleXiaobei.php"
)
FACE_INIT = "https://applet.banghua.xin/otherinterface/aliyun/InitFaceVerify0.php"
FACE_DESC = "https://applet.banghua.xin/otherinterface/aliyun/DescribeFaceVerify0.php"
ALIPAY_ORDER = (
    "https://applet.banghua.xin/otherinterface/alipay-sdk-PHP/alipaybeiyuan2.php"
)


@dataclass
class ApiResult:
    ok: bool
    status: int
    raw: str
    data: Any = None
    code: str = ""
    message: str = ""
    extra: str = ""
    kind: str = "unknown"  # json_info / json_other / text / empty / error
    headers: Dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _parse_result(status: int, raw: str, headers: Dict[str, str]) -> ApiResult:
    text = raw if raw is not None else ""
    if status < 0:
        return ApiResult(False, status, text, kind="error", message=text, headers=headers)
    if not text.strip():
        # An empty HTTP 200 only proves that the transport completed.  Several
        # business APIs use an empty body for a rejected/no-op operation, so it
        # must not be promoted to a successful business result globally.
        return ApiResult(
            False,
            status,
            text,
            data=None,
            code="EMPTY_RESPONSE",
            message="empty response; business outcome is unknown",
            kind="empty",
            headers=headers,
        )

    # try JSON
    try:
        data = json.loads(text)
    except Exception:
        # plain text business result
        stripped = text.strip()
        lowered = stripped.lower()
        failure_code = ""
        if lowered in ("false", "no", "null", "none"):
            ok = False
            failure_code = "FALSE_RESPONSE" if lowered in ("false", "no") else "NULL_RESPONSE"
        elif lowered in ("true", "ok", "success", "t"):
            ok = True
        elif any(
            k in stripped
            for k in (
                "成功",
                "设置成功",
                "密码修改成功",
                "提交成功",
                "已关注",
            )
        ):
            ok = True
        elif any(
            k in stripped
            for k in (
                "不足",
                "失败",
                "错误",
                "失效",
                "不可",
                "登录失效",
                "未实名",
            )
        ):
            ok = False
        else:
            # long opaque payloads (word lists, tokens) treat as transport OK
            ok = 200 <= status < 300
        ok = ok and 200 <= status < 300
        return ApiResult(
            ok=ok,
            status=status,
            raw=text,
            data=text,
            code=failure_code,
            message=text[:200],
            kind="text",
            headers=headers,
        )

    transport_ok = 200 <= status < 300

    # JSON booleans/null need explicit handling.  json.loads("false") and
    # json.loads("null") succeed, so the plain-text failure branch above never
    # sees them.  Numeric values and lists, on the other hand, are legitimate
    # payloads used by endpoints such as getReferral and must be preserved.
    if data is None:
        return ApiResult(
            False,
            status,
            text,
            data=None,
            code="NULL_RESPONSE",
            message="null response; business outcome is unknown",
            kind="json_other",
            headers=headers,
        )
    if isinstance(data, bool):
        return ApiResult(
            ok=transport_ok and data,
            status=status,
            raw=text,
            data=data,
            code="" if data else "FALSE_RESPONSE",
            message="" if data else "service returned false",
            kind="json_other",
            headers=headers,
        )
    if isinstance(data, (list, int, float, str)):
        return ApiResult(
            transport_ok,
            status,
            text,
            data=data,
            message="" if transport_ok else f"HTTP {status}",
            kind="json_other",
            headers=headers,
        )

    if not isinstance(data, dict):
        return ApiResult(
            transport_ok,
            status,
            text,
            data=data,
            message="" if transport_ok else f"HTTP {status}",
            kind="json_other",
            headers=headers,
        )

    code = str(data.get("code", data.get("error", "")))
    message = str(data.get("message", data.get("info", "")))
    extra = str(data.get("extra", ""))
    # nested json string
    nested = data.get("json")
    if isinstance(nested, str) and nested.strip().startswith(("{", "[")):
        try:
            data = {**data, "json_obj": json.loads(nested)}
        except Exception:
            pass

    ok = transport_ok and (
        code in ("200", "0", "")
        or data.get("error") == "0"
        or data.get("error") == 0
    )
    if code in ("400", "403", "700", "300"):
        ok = False
    # some success only have message T
    if code == "200" and transport_ok:
        ok = True

    return ApiResult(
        ok=ok,
        status=status,
        raw=text,
        data=data,
        code=code,
        message=message,
        extra=extra,
        kind="json_info",
        headers=headers,
    )


class ProtocolClient:
    def __init__(self, session: Optional[Session] = None, timeout: int = 30):
        self.session = session or Session()
        self.timeout = timeout
        self.last: Optional[ApiResult] = None
        self._http = httpx.Client(
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=45.0,
            ),
            timeout=httpx.Timeout(float(timeout)),
        )
        # Production BFF hooks.  The protocol package remains usable without
        # them by CLI/tests; the Web runtime attaches per-account callbacks.
        self.response_hook: Optional[
            Callable[[Dict[str, Any], ApiResult], None]
        ] = None
        self.reauth_callback: Optional[Callable[[], bool]] = None
        self._reauthing = False
        self._reauth_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # ---- URL helpers ----
    def url(
        self,
        action: str,
        i: str = "999999",
        m: str = "socialchat",
        a: str = "webapp",
    ) -> str:
        if action.startswith("http"):
            return action
        return f"{APPLET}?i={i}&c=entry&a={a}&do={action}&m={m}"

    def redis_url(self, action: str, i: str = "888", m: str = "rediscache") -> str:
        return f"{REDIS}?i={i}&c=entry&a=webapp&do={action}&m={m}"

    # ---- core request ----
    def request(
        self,
        url: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        method: str = "POST",
        multipart: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Union[str, Path]]] = None,
        with_author_sig: bool = False,
        uid: Optional[str] = None,
        token: Optional[str] = None,
        _allow_reauth: bool = True,
    ) -> ApiResult:
        uid = uid if uid is not None else self.session.uid
        token = token if token is not None else self.session.token
        hdrs = sign.auth_headers(uid, token, with_author_sig=with_author_sig)
        # prefer session UA (APK-like); fall back to sign default
        ua = getattr(self.session, "user_agent", None)
        if ua:
            hdrs["User-Agent"] = ua

        data: Optional[bytes]
        if files or multipart is not None:
            fields = dict(multipart or body or {})
            data, ctype = self._encode_multipart(fields, files or {})
            hdrs["Content-Type"] = ctype
        else:
            payload = {
                k: "" if v is None else str(v) for k, v in (body or {}).items()
            }
            data = parse.urlencode(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            response = self._http.request(
                method,
                url,
                content=data if method != "GET" else None,
                headers=hdrs,
                timeout=float(self.timeout),
            )
            raw = response.content.decode("utf-8", errors="replace")
            rh = {k: v for k, v in response.headers.items()}
            result = _parse_result(response.status_code, raw, rh)
        except Exception as e:
            result = ApiResult(False, -1, f"EXC:{e}", kind="error", message=str(e))

        self.last = result
        request_meta = {
            "url": url,
            "method": method,
            # Hooks need field names for diagnostics, never local file bytes.
            "field_names": sorted(str(k) for k in (multipart or body or {}).keys()),
            "file_fields": sorted(str(k) for k in (files or {}).keys()),
            "status": result.status,
        }
        if self.response_hook is not None:
            try:
                self.response_hook(request_meta, result)
            except Exception:
                # Archival/observability must not change protocol outcomes.
                pass

        if (
            _allow_reauth
            and not self._reauthing
            and self.reauth_callback is not None
            and self._looks_auth_expired(result)
        ):
            original_token = str(token or "")
            with self._reauth_lock:
                if original_token and str(self.session.token or "") != original_token:
                    refreshed = True
                else:
                    try:
                        self._reauthing = True
                        refreshed = bool(self.reauth_callback())
                    except Exception:
                        refreshed = False
                    finally:
                        self._reauthing = False
            if refreshed:
                return self.request(
                    url,
                    body,
                    method=method,
                    multipart=multipart,
                    files=files,
                    with_author_sig=with_author_sig,
                    uid=None,
                    token=None,
                    _allow_reauth=False,
                )
        return result

    @staticmethod
    def _looks_auth_expired(result: ApiResult) -> bool:
        """Conservatively recognize an expired login without retrying business 403s."""
        if int(result.status or 0) == 401:
            return True
        text = " ".join(
            str(value or "")
            for value in (result.code, result.message, result.extra, result.raw[:300])
        ).lower()
        return any(
            marker in text
            for marker in (
                "登录失效",
                "登录已失效",
                "请先登录",
                "尚未登录",
                "token失效",
                "token expired",
                "invalid token",
                "not logged in",
            )
        )

    def _encode_multipart(
        self, fields: Dict[str, Any], files: Dict[str, Union[str, Path]]
    ) -> tuple[bytes, str]:
        boundary = f"----BBW{uuid.uuid4().hex}"
        lines: List[bytes] = []
        for k, v in fields.items():
            lines.append(f"--{boundary}\r\n".encode())
            lines.append(
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            )
            lines.append(str(v).encode("utf-8"))
            lines.append(b"\r\n")
        for name, path in files.items():
            p = Path(path)
            content = p.read_bytes()
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            lines.append(f"--{boundary}\r\n".encode())
            lines.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{p.name}"\r\n'
                ).encode()
            )
            lines.append(f"Content-Type: {mime}\r\n\r\n".encode())
            lines.append(content)
            lines.append(b"\r\n")
        lines.append(f"--{boundary}--\r\n".encode())
        return b"".join(lines), f"multipart/form-data; boundary={boundary}"

    # ---- high level call styles ----
    def call(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> ApiResult:
        """Call a short action via socialchat module (startHttp style)."""
        body = dict(params or {})
        body.update(kwargs)
        return self.request(self.url(action), body)

    def call_i888(self, action: str, params: Optional[Dict[str, Any]] = None, m: str = "socialchat", **kwargs: Any) -> ApiResult:
        body = dict(params or {})
        body.update(kwargs)
        return self.request(self.url(action, i="888", m=m), body)

    def call_redis(self, action: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> ApiResult:
        body = dict(params or {})
        body.update(kwargs)
        return self.request(self.redis_url(action), body)

    def call_url(self, url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> ApiResult:
        body = dict(params or {})
        body.update(kwargs)
        return self.request(url, body)

    def call_multipart(
        self,
        action: str,
        fields: Dict[str, Any],
        files: Optional[Dict[str, Union[str, Path]]] = None,
        **url_kw: Any,
    ) -> ApiResult:
        return self.request(
            self.url(action, **url_kw) if not action.startswith("http") else action,
            multipart=fields,
            files=files,
        )
