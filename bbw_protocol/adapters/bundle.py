"""Facade: all native adapters on one BeibeiwuApp instance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from ..app import BeibeiwuApp
from .face import FaceAdapter
from .im import ImAdapter
from .tim_rest import TimRestClient
from .jd_chat import JdChatClient

if TYPE_CHECKING:
    from .roomkit import RoomKitAdapter


class NativeBundle:
    """Attach IM / face / RoomKit adapters to a protocol app."""

    def __init__(self, app: BeibeiwuApp):
        self.app = app
        self.im = ImAdapter(app)
        self.face = FaceAdapter(app)
        self._roomkit: RoomKitAdapter | None = None
        self.tim_rest = TimRestClient()
        # v162 APK chat API (Bearer user token), independent from Tencent TIM.
        self.jd_chat = JdChatClient(str(getattr(app.session, "token", "") or ""), session=app.session)

    @property
    def roomkit(self) -> RoomKitAdapter:
        """Load the independent voice-room adapter only when it is used."""

        adapter = self._roomkit
        if adapter is None:
            from .roomkit import RoomKitAdapter

            adapter = RoomKitAdapter(self.app)
            self._roomkit = adapter
        return adapter

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
                "jd_chat": "test.banghua.xin/api/im/messages/send",
            },
            "face": {
                "http": "InitFaceVerify0 / DescribeFaceVerify0 / SaveRPVerifyInfo",
                "live": "Aliyun ZIM SDK (metaInfo)",
                "forge": "not viable (see analysis/08)",
            },
            "roomkit": self.roomkit.public_status(),
        }

    def web_bootstrap(self, prefer_tim: str = "local") -> Dict[str, Any]:
        """JSON blob for Web shell first paint (creds only, no secrets)."""
        return {
            "whoami": self.app.whoami(),
            "im": self.im.bootstrap(prefer_tim=prefer_tim),
            "face_pipeline": self.face.status_hint()["pipeline"],
        }
