"""Runtime provider boundaries for the Web product."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .contracts import (
    ProviderApiResult,
    ProviderApplication,
    ProviderAuthenticationRejected,
    ProviderHeartbeat,
    ProviderNativeBundle,
    ProviderRuntime,
    ProviderSession,
    ProviderSessionState,
    ProviderUnavailable,
    RuntimeProvider,
)

__all__ = [
    "ProviderApiResult",
    "ProviderApplication",
    "ProviderAuthenticationRejected",
    "ProviderHeartbeat",
    "ProviderNativeBundle",
    "ProviderRuntime",
    "ProviderSession",
    "ProviderSessionState",
    "ProviderUnavailable",
    "RuntimeProvider",
    "LEGACY_BANGHUA_PROVIDER_ID",
    "LegacyBanghuaProvider",
    "WEB_NATIVE_PROVIDER_ID",
    "WebNativeProvider",
    "WebNativeSession",
]


def __getattr__(name: str) -> Any:
    modules = {
        "LEGACY_BANGHUA_PROVIDER_ID": ".legacy_banghua",
        "LegacyBanghuaProvider": ".legacy_banghua",
        "WEB_NATIVE_PROVIDER_ID": ".web_native",
        "WebNativeProvider": ".web_native",
        "WebNativeSession": ".web_native",
    }
    module_name = modules.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
