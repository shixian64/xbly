"""Web 本地权威消息服务。"""

from importlib import import_module
from typing import Any

from .contracts import (
    CanonicalMessageStore,
    InvalidLocalMessage,
    LocalDeliveryResult,
    LocalIdentityUnavailable,
    LocalMessageBlocked,
    LocalMessageForbidden,
    LocalMessageIdempotencyConflict,
    LocalMessageNotFound,
    LocalMessageRevocationDenied,
    LocalMessageRevocationExpired,
    LocalMessageSourceStale,
    LocalMessageView,
    LocalPrincipal,
    LocalRevokeResult,
    PeerNotMigrated,
    TimMirrorIntent,
    TimRevokeIntent,
)
from .service import LocalMessagingService

__all__ = [
    "CanonicalMessageStore",
    "InvalidLocalMessage",
    "LocalDeliveryResult",
    "LocalIdentityUnavailable",
    "LocalMessageBlocked",
    "LocalMessageForbidden",
    "LocalMessageIdempotencyConflict",
    "LocalMessageNotFound",
    "LocalMessageRevocationDenied",
    "LocalMessageRevocationExpired",
    "LocalMessageSourceStale",
    "LocalMessageView",
    "LocalMessagingService",
    "LocalPrincipal",
    "LocalRevokeResult",
    "PeerNotMigrated",
    "SqlAlchemyCanonicalMessageStore",
    "TimMirrorIntent",
    "TimRevokeIntent",
]


def __getattr__(name: str) -> Any:
    if name != "SqlAlchemyCanonicalMessageStore":
        raise AttributeError(name)
    module = import_module(".repository", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
