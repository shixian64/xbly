"""Native capability adapters (IM / face / RoomKit).

These sit *beside* the HTTP protocol core:
  protocol  → credentials / face session
  adapters  → shape them for official SDKs (Web / App)
  BFF/Web   → never put SECRETKEY in the browser

Usage:
    from bbw_protocol import BeibeiwuApp
    from bbw_protocol.adapters import NativeBundle

    app = BeibeiwuApp.load()
    native = NativeBundle(app)
    print(native.im.tim_login_payload())
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "NativeBundle",
    "ImAdapter",
    "TimCredentials",
    "RongCredentials",
    "FaceAdapter",
    "FaceSession",
    "RoomKitAdapter",
    "RoomKitCredentials",
    "JdChatClient",
    "JdChatResult",
    "extract_collection_rows",
]

_EXPORT_MODULES = {
    "NativeBundle": ".bundle",
    "FaceAdapter": ".face",
    "FaceSession": ".face",
    "ImAdapter": ".im",
    "RongCredentials": ".im",
    "TimCredentials": ".im",
    "RoomKitAdapter": ".roomkit",
    "RoomKitCredentials": ".roomkit",
    "JdChatClient": ".jd_chat",
    "JdChatResult": ".jd_chat",
    "extract_collection_rows": ".jd_chat",
}


def __getattr__(name: str) -> Any:
    """Load optional capability modules only when their exports are requested."""

    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
