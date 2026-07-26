"""External communication transport boundaries used by the Web product."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .contracts import (
    MessageHistoryTransport,
    MessageMirrorTransport,
    MessageRecallLookupTransport,
    MessageSendTransport,
    MessageTransport,
    MessageTransportResult,
)

__all__ = [
    "MessageHistoryTransport",
    "MessageMirrorTransport",
    "MessageRecallLookupTransport",
    "MessageSendTransport",
    "MessageTransport",
    "MessageTransportResult",
    "create_legacy_tim_rest_transport",
]


def __getattr__(name: str) -> Any:
    if name != "create_legacy_tim_rest_transport":
        raise AttributeError(name)
    module = import_module(".legacy_tim_rest", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
