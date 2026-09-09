"""RongCloud RoomKit HTTP adapter used by the APK's native voice-room module.

RoomKit has its own login and ``Authorization`` credential.  It is deliberately
kept separate from the Banghua ``AUTHOR-TOKEN`` session and must never be sent to
the browser by the BFF.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional
from urllib import error, parse, request

from ..app import BeibeiwuApp
from ..client import ApiResult
from .. import sign
from ..secrets import read_protocol_secret

ROOMKIT_BASE_URL = "https://redis.banghua.xin:8080/"
ROOMKIT_BUSINESS_TOKEN = read_protocol_secret(
    "BBW_ROOMKIT_BUSINESS_TOKEN",
    development_default="development-only-roomkit-business-token",
)
ROOMKIT_CHANNEL = "WalleChannelReader.getChannel(this)"
ROOMKIT_VOICE_TYPE = 1
ROOMKIT_SUCCESS_CODES = {"", "0", "10000"}


@dataclass
class RoomKitCredentials:
    """Per-user RoomKit state; authorization and IM token stay server-side."""

    banghua_uid: str
    authorization: str
    im_token: str = ""
    room_user_id: str = ""
    user_name: str = ""
    portrait: str = ""
    sex: str = "0"
    user_type: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "connected": bool(self.authorization),
            "banghua_uid": self.banghua_uid,
            "room_user_id": self.room_user_id,
            "user_name": self.user_name,
            "has_im_token": bool(self.im_token),
        }


class RoomKitAdapter:
    """Login to the APK RoomKit backend and read native voice-room metadata."""

    def __init__(
        self,
        app: BeibeiwuApp,
        *,
        timeout: float = 7.0,
        opener: Optional[Callable[..., Any]] = None,
    ):
        self.app = app
        self.timeout = float(timeout)
        self._opener = opener or request.urlopen
        self._credentials: Optional[RoomKitCredentials] = None

    def clear(self) -> None:
        self._credentials = None

    def public_status(self) -> Dict[str, Any]:
        if self._credentials is None:
            return {
                "connected": False,
                "banghua_uid": str(self.app.session.uid or ""),
                "room_user_id": "",
                "user_name": "",
                "has_im_token": False,
            }
        return self._credentials.public()

    @staticmethod
    def _profile(session: Any) -> Dict[str, Any]:
        raw = getattr(session, "raw_user", {}) or {}
        if not isinstance(raw, dict):
            return {}
        for key in ("userInfoList", "user_info", "user", "data"):
            nested = raw.get(key)
            if isinstance(nested, dict):
                return {**raw, **nested}
        return raw

    @staticmethod
    def _sex(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return "1" if normalized in {"1", "女", "female", "f", "woman"} else "0"

    @staticmethod
    def _device_id(session: Any, uid: str) -> str:
        current = str(getattr(session, "device_id", "") or "").strip()
        if current:
            return current
        seed = f"bbw-roomkit:{uid}:{getattr(session, 'pushregid', '')}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def _login_params(self) -> Dict[str, str]:
        session = self.app.session
        uid = str(session.uid or "").strip()
        profile = self._profile(session)
        nickname = str(
            getattr(session, "nickname", "")
            or profile.get("nickname")
            or profile.get("userName")
            or uid
        ).strip()
        portrait = str(
            getattr(session, "portrait", "")
            or profile.get("portrait")
            or profile.get("avatar")
            or ""
        ).strip()
        sex = self._sex(profile.get("sex") or profile.get("gender"))
        return {
            "userName": nickname or uid,
            "portrait": portrait,
            "mobile": uid,
            "sex": sex,
            "verifyCode": "111111",
            "deviceId": self._device_id(session, uid),
            "region": "86",
            "platform": "mobile",
            "platformType": "android",
            "channel": ROOMKIT_CHANNEL,
            "version": str(getattr(session, "version_code", "") or sign.VERSION_CODE),
        }

    @staticmethod
    def _parse_result(
        status: int,
        raw: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> ApiResult:
        response_headers = dict(headers or {})
        try:
            payload = json.loads(raw)
        except Exception:
            return ApiResult(
                False,
                status,
                raw,
                code="ROOMKIT_INVALID_RESPONSE",
                message="房间服务返回了无法识别的内容",
                kind="roomkit_error",
                headers=response_headers,
            )

        if not isinstance(payload, dict):
            return ApiResult(
                False,
                status,
                raw,
                data=payload,
                code="ROOMKIT_INVALID_RESPONSE",
                message="房间服务响应结构不正确",
                kind="roomkit_error",
                headers=response_headers,
            )

        code = str(payload.get("code", "") or "")
        message = str(payload.get("msg") or payload.get("message") or "")
        ok = 200 <= status < 300 and code in ROOMKIT_SUCCESS_CODES
        return ApiResult(
            ok,
            status,
            raw,
            data=payload.get("data"),
            code=code,
            message=message,
            kind="roomkit_json",
            headers=response_headers,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        authorization: str = "",
    ) -> ApiResult:
        url = parse.urljoin(ROOMKIT_BASE_URL, str(path or "").lstrip("/"))
        payload = {
            str(key): "" if value is None else str(value)
            for key, value in dict(params or {}).items()
        }
        body: Optional[bytes] = None
        if method.upper() == "GET":
            if payload:
                url = f"{url}?{parse.urlencode(payload)}"
        else:
            body = parse.urlencode(payload).encode("utf-8")

        headers = {
            "Accept": "application/json",
            "BusinessToken": ROOMKIT_BUSINESS_TOKEN,
            "User-Agent": str(
                getattr(self.app.session, "user_agent", "")
                or f"okhttp/4.9.3 beibeiwu/{sign.VERSION_CODE}"
            ),
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if authorization:
            headers["Authorization"] = authorization

        req = request.Request(
            url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with self._opener(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                response_headers = {
                    key: value for key, value in getattr(response, "headers", {}).items()
                }
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                return self._parse_result(status, raw, response_headers)
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            response_headers = {
                key: value for key, value in (exc.headers.items() if exc.headers else [])
            }
            return self._parse_result(int(exc.code), raw, response_headers)
        except Exception as exc:
            return ApiResult(
                False,
                -1,
                "",
                code="ROOMKIT_TRANSPORT_ERROR",
                message="房间服务连接失败",
                extra=type(exc).__name__,
                kind="roomkit_error",
            )

    def login(self, *, force: bool = False) -> ApiResult:
        session = self.app.session
        uid = str(session.uid or "").strip()
        if not uid or uid == "0" or not getattr(session, "logged_in", False):
            return ApiResult(
                False,
                401,
                "",
                code="ROOMKIT_LOGIN_REQUIRED",
                message="请先登录后再读取 APK 房间列表",
                kind="roomkit_error",
            )

        if (
            not force
            and self._credentials is not None
            and self._credentials.banghua_uid == uid
            and self._credentials.authorization
        ):
            return ApiResult(
                True,
                200,
                "",
                data=self._credentials.public(),
                code="10000",
                message="房间服务会话已就绪",
                kind="roomkit_cached",
            )

        if self._credentials is not None and self._credentials.banghua_uid != uid:
            self.clear()

        result = self._request("POST", "/user/login", self._login_params())
        if not result.ok:
            return result
        data = result.data if isinstance(result.data, dict) else {}
        authorization = str(data.get("authorization") or "").strip()
        if not authorization:
            return ApiResult(
                False,
                result.status,
                "",
                data={},
                code="ROOMKIT_AUTH_MISSING",
                message="房间服务未签发独立授权",
                kind="roomkit_error",
            )

        self._credentials = RoomKitCredentials(
            banghua_uid=uid,
            authorization=authorization,
            im_token=str(data.get("imToken") or data.get("im_token") or ""),
            room_user_id=str(data.get("userId") or data.get("user_id") or ""),
            user_name=str(data.get("userName") or data.get("user_name") or ""),
            portrait=str(data.get("portrait") or ""),
            sex=str(data.get("sex") or "0"),
            user_type=str(data.get("type") or ""),
        )
        return ApiResult(
            True,
            result.status,
            "",
            data=self._credentials.public(),
            code=result.code,
            message=result.message or "房间服务登录成功",
            kind="roomkit_login",
        )

    def rooms(
        self,
        *,
        page: int = 1,
        size: int = 10,
        force_login: bool = False,
    ) -> ApiResult:
        login_result = self.login(force=force_login)
        if not login_result.ok or self._credentials is None:
            return login_result

        page = max(1, int(page))
        size = max(1, min(50, int(size)))
        result = self._request(
            "GET",
            "/mic/room/list",
            {"page": page, "size": size, "type": ROOMKIT_VOICE_TYPE},
            authorization=self._credentials.authorization,
        )
        if result.status in {401, 403} and not force_login:
            self.clear()
            return self.rooms(page=page, size=size, force_login=True)
        return result
