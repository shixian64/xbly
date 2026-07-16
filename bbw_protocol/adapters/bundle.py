"""Facade: all native adapters on one BeibeiwuApp instance."""

from __future__ import annotations

from typing import Any, Dict

from ..app import BeibeiwuApp
from .face import FaceAdapter
from .im import ImAdapter
from .pay import PayAdapter
from .roomkit import RoomKitAdapter
from .tim_rest import TimRestClient


class NativeBundle:
    """Attach IM / face / pay / RoomKit adapters to a protocol app."""

    def __init__(self, app: BeibeiwuApp):
        self.app = app
        self.im = ImAdapter(app)
        self.face = FaceAdapter(app)
        self.pay = PayAdapter(app)
        self.roomkit = RoomKitAdapter(app)
        self.tim_rest = TimRestClient()

    def status(self) -> Dict[str, Any]:
        """What can be done now with current session + adapters."""
        s = self.app.session
        return {
            "logged_in": bool(getattr(s, "logged_in", False) or s.uid),
            "uid": s.uid,
            "nickname": getattr(s, "nickname", ""),
            "im": {
                "tim_local": "ready if uid set",
                "tim_server": "tximsign.php",
                "rong": "userregister.php + APP_KEY",
                "realtime": "needs TIM/Rong Web or App SDK",
                "rest_fallback": "openim/sendmsg via BFF when Web SDK login hangs",
            },
            "face": {
                "http": "InitFaceVerify0 / DescribeFaceVerify0 / SaveRPVerifyInfo",
                "live": "Aliyun ZIM SDK (metaInfo)",
                "forge": "not viable (see analysis/08)",
            },
            "roomkit": self.roomkit.public_status(),
            "pay": self.pay.capabilities(),
        }

    def web_bootstrap(self, prefer_tim: str = "local") -> Dict[str, Any]:
        """JSON blob for Web shell first paint (creds only, no secrets)."""
        return {
            "whoami": self.app.whoami(),
            "im": self.im.bootstrap(prefer_tim=prefer_tim),
            "pay_meta": self.pay.capabilities(),
            "face_pipeline": self.face.status_hint()["pipeline"],
        }
