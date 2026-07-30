"""Network-free runtime used by locally authenticated Web sessions.

The runtime intentionally implements only the provider-neutral surface needed
to keep an authenticated browser session alive.  APK compatibility domains
remain available through :mod:`legacy_banghua` for provider-backed sessions;
calling one of those domains from this runtime fails immediately and never
opens a network connection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ProviderRuntime, ProviderSession, ProviderSessionState, ProviderUnavailable


WEB_NATIVE_PROVIDER_ID = "web-native"
UPSTREAM_DISABLED_CODE = "UPSTREAM_DISABLED"


@dataclass(slots=True)
class WebNativeResult:
    ok: bool = False
    status: int = 503
    raw: str = ""
    data: Any = field(default_factory=dict)
    code: str = UPSTREAM_DISABLED_CODE
    message: str = "该能力依赖的外部服务当前不可用"
    extra: str = ""
    kind: str = "error"
    headers: dict[str, str] = field(default_factory=dict)
    error_code: int = 503
    error_info: str = "upstream disabled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "error_code": self.error_code,
            "error_info": self.error_info,
            "data": self.data,
        }


class _WebNativeClient:
    """Hook-compatible client that never performs transport work."""

    def __init__(self) -> None:
        self.response_hook: Any = None
        self.reauth_callback: Any = None
        self.timeout = 0

    def close(self) -> None:
        return None

    def request(self, *_args: Any, **_kwargs: Any) -> WebNativeResult:
        return WebNativeResult()

    call = request
    call_redis = request
    call_url = request


class _UnavailableApi:
    """Return a stable provider-style failure for every legacy domain call."""

    def __getattr__(self, _name: str) -> Any:
        def unavailable(*_args: Any, **_kwargs: Any) -> WebNativeResult:
            return WebNativeResult()

        return unavailable


@dataclass
class WebNativeSession:
    uid: str = "0"
    token: str = ""
    phone: str = ""
    password: str = ""
    nickname: str = ""
    user_role: str = ""
    rp_verify_time: str = "0"
    vip: str = "0"
    svip: str = "0"
    money: str = "0"
    portrait: str = ""
    user_sign: str = ""
    login_id: str = ""
    phonebrand: str = "Web"
    pushregid: str = ""
    device_id: str = ""
    version_code: str = "web"
    package_name: str = "bbw.web"
    user_agent: str = "BBW-Web"
    raw_user: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    @property
    def logged_in(self) -> bool:
        uid = str(self.uid or "").strip()
        return bool(uid and uid.lower() not in {"0", "none", "null"})

    @property
    def is_realname(self) -> bool:
        return bool(self.rp_verify_time and self.rp_verify_time != "0")

    def apply_device(self, profile: dict[str, str]) -> None:
        for key in (
            "phonebrand",
            "pushregid",
            "device_id",
            "version_code",
            "package_name",
            "user_agent",
        ):
            value = profile.get(key)
            if value:
                setattr(self, key, str(value))

    def device_dict(self) -> dict[str, str]:
        return {
            "phonebrand": self.phonebrand,
            "pushregid": self.pushregid,
            "device_id": self.device_id,
            "version_code": self.version_code,
            "package_name": self.package_name,
            "user_agent": self.user_agent,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "uid": self.uid,
            "nickname": self.nickname,
            "portrait": self.portrait,
            "user_role": self.user_role,
            "rp_verify_time": self.rp_verify_time,
            "is_realname": self.is_realname,
            "vip": self.vip,
            "svip": self.svip,
            "money": self.money,
            "phone": self.phone,
            "token_prefix": "",
            "device": {
                "phonebrand": self.phonebrand,
                "pushregid": "",
                "device_id": self.device_id,
                "version_code": self.version_code,
            },
        }


class _LocalHeartbeat:
    running = False

    def once(
        self,
        first: bool | None = None,
        *,
        timeout: float | None = None,
        deadline: Any = None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "running": False,
            "local": True,
            "skipped": True,
            "first": first,
            "timeout": timeout,
        }

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {"running": False, "local": True, "skipped": True}


class WebNativeApplication:
    def __init__(self, session: WebNativeSession) -> None:
        self.session = session
        self.client = _WebNativeClient()
        self.auth = _UnavailableApi()
        self.content = _UnavailableApi()
        self.social = _UnavailableApi()
        self.profile = _UnavailableApi()
        self.economy = _UnavailableApi()
        self.room = _UnavailableApi()
        self.match = _UnavailableApi()
        self.im = _UnavailableApi()
        self.misc = _UnavailableApi()

    def whoami(self) -> dict[str, Any]:
        return self.session.summary()

    def set_device(
        self,
        *,
        seed: str | None = None,
        phonebrand: str | None = None,
        pushregid: str | None = None,
        **extra: str,
    ) -> dict[str, str]:
        stable_seed = str(seed or self.session.phone or self.session.uid or "web")
        digest = hashlib.sha256(stable_seed.encode("utf-8")).hexdigest()
        profile = {
            "phonebrand": str(phonebrand or self.session.phonebrand or "Web"),
            "pushregid": str(pushregid or self.session.pushregid or ""),
            "device_id": str(extra.get("device_id") or self.session.device_id or digest[:16]),
            "version_code": str(extra.get("version_code") or self.session.version_code or "web"),
            "package_name": str(extra.get("package_name") or self.session.package_name or "bbw.web"),
            "user_agent": str(extra.get("user_agent") or self.session.user_agent or "BBW-Web"),
        }
        self.session.apply_device(profile)
        return self.session.device_dict()

    def create_heartbeat(
        self, interval_sec: float = 55.0, jitter_sec: float = 8.0
    ) -> _LocalHeartbeat:
        del interval_sec, jitter_sec
        return _LocalHeartbeat()

    def start_heartbeat(
        self, interval_sec: float = 55.0, jitter_sec: float = 8.0
    ) -> _LocalHeartbeat:
        return self.create_heartbeat(interval_sec, jitter_sec)

    def bootstrap(self, *, include_im: bool = True) -> dict[str, WebNativeResult]:
        keys = ["ad", "recommend", "censor", "online", "me", "etiquette"]
        if include_im:
            keys.append("txim")
        return {key: WebNativeResult() for key in keys}

    def call(self, _action: str, **_params: Any) -> WebNativeResult:
        return WebNativeResult()

    call_redis = call

    def call_url(self, _url: str, **_params: Any) -> WebNativeResult:
        return WebNativeResult()

    def list_actions(self, category: str | None = None) -> list[str]:
        del category
        return []


class _WebNativeTimRest:
    def health(self, **_kwargs: Any) -> dict[str, Any]:
        return WebNativeResult().to_dict()

    def query_online(self, *_args: Any, **_kwargs: Any) -> WebNativeResult:
        return WebNativeResult()

    send_text = query_online
    recent_contacts = query_online
    roaming_messages = query_online
    c2c_unread_counts = query_online
    mark_c2c_read = query_online
    sync_c2c_message_read_receipts = query_online
    revoke_c2c = query_online


class _WebNativeIm:
    def tim_login_payload(self, **_kwargs: Any) -> dict[str, Any]:
        raise ProviderUnavailable(
            "TIM credentials are disabled for a Web-local session",
            upstream_status=503,
            upstream_code=UPSTREAM_DISABLED_CODE,
        )

    def rong_register(self) -> WebNativeResult:
        return WebNativeResult()


class _WebNativeFace:
    def status_hint(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": UPSTREAM_DISABLED_CODE,
            "message": "刷脸能力依赖的外部服务当前不可用",
        }

    def start(self, *_args: Any, **_kwargs: Any) -> WebNativeResult:
        return WebNativeResult()

    describe = start
    manual = start


class _WebNativeRoomKit:
    def rooms(self, *_args: Any, **_kwargs: Any) -> WebNativeResult:
        return WebNativeResult(code="ROOMKIT_UNAVAILABLE")

    def public_status(self) -> dict[str, Any]:
        return {"authenticated": False, "local": True}


class WebNativeBundle:
    def __init__(self, app: WebNativeApplication) -> None:
        self.app = app
        self.im = _WebNativeIm()
        self.face = _WebNativeFace()
        self.roomkit = _WebNativeRoomKit()
        self.tim_rest = _WebNativeTimRest()

    def status(self) -> dict[str, Any]:
        return {"provider": WEB_NATIVE_PROVIDER_ID, "local": True}

    def web_bootstrap(self, prefer_tim: str = "local") -> dict[str, Any]:
        del prefer_tim
        return {
            "ok": True,
            "provider": WEB_NATIVE_PROVIDER_ID,
            "local": True,
            "tim": WebNativeResult().to_dict(),
        }


def _session_from_state(state: ProviderSessionState) -> WebNativeSession:
    data = dict(state.device_data or {})
    session = WebNativeSession(
        uid=str(state.uid or "0"),
        token="",
        phone=str(state.phone or ""),
        nickname=str(state.nickname or ""),
        user_role=str(state.user_role or ""),
        rp_verify_time=str(state.rp_verify_time or "0"),
        vip=str(state.vip or "0"),
        svip=str(state.svip or "0"),
        money=str(state.money or "0"),
        portrait=str(state.portrait or ""),
        user_sign=str(state.user_sign or ""),
        login_id=str(state.login_id or ""),
        raw_user=dict(state.raw_user or {}),
    )
    session.apply_device(
        {
            str(key): str(value)
            for key, value in data.items()
            if value is not None
        }
    )
    return session


class WebNativeProvider:
    """Create network-free runtimes while retaining an account provider id."""

    def __init__(self, *, account_provider_id: str = "beibeiwu") -> None:
        provider_id = str(account_provider_id or "").strip()
        if not provider_id:
            raise ValueError("account_provider_id is required")
        self.provider_id = provider_id

    def create_runtime(
        self, session: ProviderSession | None = None
    ) -> ProviderRuntime:
        if isinstance(session, WebNativeSession):
            resolved = session
        elif session is None:
            resolved = WebNativeSession()
        else:
            resolved = WebNativeSession(
                uid=str(getattr(session, "uid", "0") or "0"),
                phone=str(getattr(session, "phone", "") or ""),
                nickname=str(getattr(session, "nickname", "") or ""),
                portrait=str(getattr(session, "portrait", "") or ""),
                raw_user=dict(getattr(session, "raw_user", {}) or {}),
            )
            try:
                resolved.apply_device(dict(session.device_dict()))
            except Exception:
                pass
        app = WebNativeApplication(resolved)
        return ProviderRuntime(
            provider_id=WEB_NATIVE_PROVIDER_ID,
            app=app,
            native=WebNativeBundle(app),
        )

    def create_runtime_from_state(
        self, state: ProviderSessionState
    ) -> ProviderRuntime:
        return self.create_runtime(_session_from_state(state))

    def load_runtime(self, path: str | Path | None = None) -> ProviderRuntime:
        if path is None:
            return self.create_runtime()
        source = Path(path)
        if not source.exists():
            return self.create_runtime()
        payload = json.loads(source.read_text(encoding="utf-8"))
        known = {
            key: value
            for key, value in dict(payload or {}).items()
            if key in WebNativeSession.__dataclass_fields__ and key != "raw_user"
        }
        session = WebNativeSession(**known)
        session.raw_user = dict(payload.get("raw_user") or {})
        session.path = str(source)
        return self.create_runtime(session)


__all__ = [
    "UPSTREAM_DISABLED_CODE",
    "WEB_NATIVE_PROVIDER_ID",
    "WebNativeApplication",
    "WebNativeBundle",
    "WebNativeProvider",
    "WebNativeResult",
    "WebNativeSession",
]
