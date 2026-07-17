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

from .bundle import NativeBundle
from .face import FaceAdapter, FaceSession
from .im import ImAdapter, RongCredentials, TimCredentials
from .roomkit import RoomKitAdapter, RoomKitCredentials

__all__ = [
    "NativeBundle",
    "ImAdapter",
    "TimCredentials",
    "RongCredentials",
    "FaceAdapter",
    "FaceSession",
    "RoomKitAdapter",
    "RoomKitCredentials",
]
