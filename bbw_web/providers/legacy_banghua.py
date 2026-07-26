"""Legacy Banghua implementation of the Web runtime provider boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from bbw_protocol.adapters import NativeBundle
from bbw_protocol.app import BeibeiwuApp
from bbw_protocol.session import Session

from .contracts import (
    ProviderApplication,
    ProviderNativeBundle,
    ProviderRuntime,
    ProviderSession,
    ProviderSessionState,
)


LEGACY_BANGHUA_PROVIDER_ID = "beibeiwu"

SessionFactory = Callable[[], ProviderSession]
SessionLoader = Callable[[str | None], ProviderSession]
ApplicationFactory = Callable[[Any], ProviderApplication]
NativeBundleFactory = Callable[[Any], ProviderNativeBundle]
SessionStateFactory = Callable[[ProviderSessionState], ProviderSession]


def _session_from_state(state: ProviderSessionState) -> ProviderSession:
    session = Session(
        uid=str(state.uid or "0"),
        token=str(state.token or "0"),
        phone=str(state.phone or ""),
        password="",
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
            for key, value in dict(state.device_data or {}).items()
            if value is not None
        }
    )
    return session


class LegacyBanghuaProvider:
    """Compose the existing protocol app and native adapters without changing them.

    Factories are injectable so the boundary can be tested without network
    access and later integrated into session restoration one call site at a
    time.  The defaults preserve the exact objects constructed by the current
    Web runtime.
    """

    provider_id = LEGACY_BANGHUA_PROVIDER_ID

    def __init__(
        self,
        *,
        session_factory: SessionFactory = Session,
        session_loader: SessionLoader = Session.load,
        session_state_factory: SessionStateFactory = _session_from_state,
        application_factory: ApplicationFactory = BeibeiwuApp,
        native_bundle_factory: NativeBundleFactory = NativeBundle,
    ) -> None:
        self._session_factory = session_factory
        self._session_loader = session_loader
        self._session_state_factory = session_state_factory
        self._application_factory = application_factory
        self._native_bundle_factory = native_bundle_factory

    def create_runtime(
        self,
        session: ProviderSession | None = None,
    ) -> ProviderRuntime:
        resolved_session = session if session is not None else self._session_factory()
        app = self._application_factory(resolved_session)
        native = self._native_bundle_factory(app)
        return ProviderRuntime(
            provider_id=self.provider_id,
            app=app,
            native=native,
        )

    def create_runtime_from_state(
        self,
        state: ProviderSessionState,
    ) -> ProviderRuntime:
        return self.create_runtime(self._session_state_factory(state))

    def load_runtime(self, path: str | Path | None = None) -> ProviderRuntime:
        session = self._session_loader(None if path is None else str(path))
        return self.create_runtime(session)
