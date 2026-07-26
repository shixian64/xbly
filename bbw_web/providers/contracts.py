"""Provider-neutral runtime contracts for Web product integrations.

The Web application currently runs on the APK-compatible Banghua protocol
stack.  These structural types keep that concrete dependency at an adapter
edge so future local providers can expose the same runtime shape without
importing :mod:`bbw_protocol`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class ProviderAuthenticationRejected(RuntimeError):
    """The provider was reachable but did not authenticate the credentials.

    This signal must never be treated as permission to fall back to a local
    credential verifier.  It also covers conservative business failures such
    as HTTP 401, 403 and 429 where accepting an older local credential could
    bypass an upstream rejection or account restriction.
    """

    retryable = False

    def __init__(
        self,
        message: str = "provider authentication rejected",
        *,
        upstream_status: int = 0,
        upstream_code: str = "",
    ) -> None:
        super().__init__(message)
        self.upstream_status = int(upstream_status)
        self.upstream_code = str(upstream_code or "")


class ProviderUnavailable(RuntimeError):
    """The provider returned an explicit transport or server outage result."""

    retryable = True

    def __init__(
        self,
        message: str = "provider unavailable",
        *,
        upstream_status: int = 0,
        upstream_code: str = "",
    ) -> None:
        super().__init__(message)
        self.upstream_status = int(upstream_status)
        self.upstream_code = str(upstream_code or "")


class ProviderUpstreamInterrupted(RuntimeError):
    """上游连接在请求中途被中断（典型如陈旧 keep-alive 连接被对端先行关闭）。

    这是可重试的瞬态故障信号，但它证明上游是「可达」的——只是本次连接被
    打断——所以绝不能触发本地密码回退（回退闸门只认
    ``UPSTREAM_AUTH_UNAVAILABLE``，见 docs/17 的安全取舍）。它也刻意不
    继承 :class:`ProviderUnavailable`，避免任何按类型判断的回退路径误收。
    """

    retryable = True

    def __init__(
        self,
        message: str = "provider upstream interrupted",
        *,
        upstream_status: int = 0,
        upstream_code: str = "",
    ) -> None:
        super().__init__(message)
        self.upstream_status = int(upstream_status)
        self.upstream_code = str(upstream_code or "")


@runtime_checkable
class ProviderSession(Protocol):
    """Minimum session surface shared by provider-backed runtimes."""

    uid: str
    token: str
    phone: str
    password: str
    nickname: str
    user_role: str
    rp_verify_time: str
    vip: str
    svip: str
    money: str
    portrait: str
    user_sign: str
    login_id: str
    raw_user: dict[str, Any]
    path: str

    @property
    def logged_in(self) -> bool: ...

    def apply_device(self, profile: dict[str, str]) -> None: ...

    def device_dict(self) -> dict[str, str]: ...

    def summary(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProviderSessionState:
    """Provider-neutral state used to rebuild one authenticated runtime.

    Production persistence owns this DTO.  Only a concrete provider adapter is
    allowed to translate it into its protocol-specific session object.
    """

    uid: str
    token: str = field(repr=False)
    phone: str = field(default="", repr=False)
    nickname: str = ""
    user_role: str = ""
    rp_verify_time: str = "0"
    vip: str = "0"
    svip: str = "0"
    money: str = "0"
    portrait: str = ""
    user_sign: str = ""
    login_id: str = ""
    raw_user: dict[str, Any] = field(default_factory=dict, repr=False)
    device_data: dict[str, Any] = field(default_factory=dict, repr=False)


@runtime_checkable
class ProviderHeartbeat(Protocol):
    """Lifecycle surface used by Web sessions for provider presence updates."""

    @property
    def running(self) -> bool: ...

    def once(self, first: bool | None = None) -> dict[str, Any]: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


@runtime_checkable
class ProviderApiResult(Protocol):
    """Sanitized response surface consumed by persistence hooks."""

    ok: bool
    status: int
    raw: str
    code: str
    message: str
    kind: str


@runtime_checkable
class ProviderApplication(Protocol):
    """Application facade consumed by the existing Web BFF."""

    session: ProviderSession
    client: Any
    auth: Any
    content: Any
    social: Any
    profile: Any
    economy: Any
    room: Any
    match: Any
    im: Any
    misc: Any

    def whoami(self) -> dict[str, Any]: ...

    def set_device(
        self,
        *,
        seed: str | None = None,
        phonebrand: str | None = None,
        pushregid: str | None = None,
        **extra: str,
    ) -> dict[str, str]: ...

    def create_heartbeat(
        self,
        interval_sec: float = 55.0,
        jitter_sec: float = 8.0,
    ) -> ProviderHeartbeat: ...

    def start_heartbeat(
        self,
        interval_sec: float = 55.0,
        jitter_sec: float = 8.0,
    ) -> ProviderHeartbeat: ...

    def bootstrap(self, *, include_im: bool = True) -> dict[str, Any]: ...

    def call(self, action: str, **params: Any) -> Any: ...

    def call_redis(self, action: str, **params: Any) -> Any: ...

    def call_url(self, url: str, **params: Any) -> Any: ...

    def list_actions(self, category: str | None = None) -> list[str]: ...


@runtime_checkable
class ProviderNativeBundle(Protocol):
    """Native-side capability facade paired with one provider application."""

    app: ProviderApplication
    im: Any
    face: Any
    roomkit: Any
    tim_rest: Any

    def status(self) -> dict[str, Any]: ...

    def web_bootstrap(self, prefer_tim: str = "local") -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """One application/native pair created by the same provider."""

    provider_id: str
    app: ProviderApplication
    native: ProviderNativeBundle


@runtime_checkable
class RuntimeProvider(Protocol):
    """Factory boundary used to create or restore a provider runtime."""

    provider_id: str

    def create_runtime(
        self,
        session: ProviderSession | None = None,
    ) -> ProviderRuntime: ...

    def create_runtime_from_state(
        self,
        state: ProviderSessionState,
    ) -> ProviderRuntime: ...

    def load_runtime(self, path: str | Path | None = None) -> ProviderRuntime: ...
