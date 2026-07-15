"""Native capability adapters (IM / face / pay).

These sit *beside* the HTTP protocol core:
  protocol  → credentials / order params / face session
  adapters  → shape them for official SDKs (Web / App)
  BFF/Web   → never put SECRETKEY in the browser

Usage:
    from bbw_protocol import BeibeiwuApp
    from bbw_protocol.adapters import NativeBundle

    app = BeibeiwuApp.load()
    native = NativeBundle(app)
    print(native.im.tim_login_payload())
    print(native.pay.prepare_coin_wechat("1"))
"""

from .bundle import NativeBundle
from .face import FaceAdapter, FaceSession
from .im import ImAdapter, RongCredentials, TimCredentials
from .pay import PayAdapter, PayPrepareResult

__all__ = [
    "NativeBundle",
    "ImAdapter",
    "TimCredentials",
    "RongCredentials",
    "FaceAdapter",
    "FaceSession",
    "PayAdapter",
    "PayPrepareResult",
]
