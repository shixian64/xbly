"""High-level facade: use the app like the APK over HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import ApiResult, ProtocolClient, commerce_action_disabled
from .modules.auth import AuthAPI
from .modules.content import ContentAPI
from .modules.economy import EconomyAPI
from .modules.im import ImAPI
from .modules.match import MatchAPI
from .modules.misc import MiscAPI
from .modules.profile import ProfileAPI
from .modules.room import RoomAPI
from .modules.social import SocialAPI
from .session import Session

CATALOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "api_catalog.json"


class BeibeiwuApp:
    """Protocol-level stand-in for the Android client.

    Example:
        app = BeibeiwuApp.load()
        app.auth.login_password("191...", "pass")
        app.social.follow("123")
        app.profile.reset_nickname("Vom")
        app.save()

    Native sidecars (IM / face / RoomKit adapters):
        app.native.im.tim_login_payload()
    """

    def __init__(self, session: Optional[Session] = None):
        self.session = session or Session()
        self.client = ProtocolClient(self.session)
        self.auth = AuthAPI(self.client)
        self.content = ContentAPI(self.client)
        self.social = SocialAPI(self.client)
        self.profile = ProfileAPI(self.client)
        self.economy = EconomyAPI(self.client)
        self.room = RoomAPI(self.client)
        self.match = MatchAPI(self.client)
        self.im = ImAPI(self.client)
        self.misc = MiscAPI(self.client)
        self._native = None  # lazy NativeBundle
        self._catalog: Optional[Dict[str, Any]] = None

    @property
    def native(self):
        """IM / face / RoomKit adapters (lazy import)."""
        if self._native is None:
            from .adapters import NativeBundle

            self._native = NativeBundle(self)
        return self._native

    # ---- session ----
    @classmethod
    def load(cls, path: Optional[str] = None) -> "BeibeiwuApp":
        return cls(Session.load(path))

    def save(self, path: Optional[str] = None) -> Path:
        return self.session.save(path)

    def whoami(self) -> Dict[str, Any]:
        return self.session.summary()

    def set_device(
        self,
        *,
        seed: Optional[str] = None,
        phonebrand: Optional[str] = None,
        pushregid: Optional[str] = None,
        **extra: str,
    ) -> Dict[str, str]:
        """Attach APK-like device fields to this session (protocol fidelity)."""
        from .device import build_device_profile

        prof = build_device_profile(
            seed=seed or self.session.phone or self.session.uid or None,
            phonebrand=phonebrand,
            pushregid=pushregid,
            **{k: v for k, v in extra.items() if k in (
                "device_id", "version_code", "package_name"
            )},
        )
        self.session.apply_device(prof)
        return self.session.device_dict()

    def start_heartbeat(
        self, interval_sec: float = 55.0, jitter_sec: float = 8.0
    ):
        """Start background UpdateOnline0. Caller must keep the returned object."""
        from .heartbeat import Heartbeat

        hb = Heartbeat(self, interval_sec=interval_sec, jitter_sec=jitter_sec)
        hb.start()
        return hb

    # ---- generic escape hatch (commerce actions are blocked by ProtocolClient) ----
    def call(self, action: str, **params: Any) -> ApiResult:
        return self.client.call(action, params)

    def call_redis(self, action: str, **params: Any) -> ApiResult:
        return self.client.call_redis(action, params)

    def call_url(self, url: str, **params: Any) -> ApiResult:
        return self.client.call_url(url, params)

    def call_i888(self, action: str, m: str = "socialchat", **params: Any) -> ApiResult:
        return self.client.call_i888(action, params, m=m)

    # ---- catalog ----
    def catalog(self) -> Dict[str, Any]:
        if self._catalog is None:
            if CATALOG_PATH.exists():
                self._catalog = json.loads(
                    CATALOG_PATH.read_text(encoding="utf-8")
                )
            else:
                self._catalog = {"categories": {}, "shorts": [], "dos": []}
        return self._catalog

    def list_actions(self, category: Optional[str] = None) -> List[str]:
        cat = self.catalog()
        if category:
            return [
                action
                for action in cat.get("categories", {}).get(category, [])
                if not commerce_action_disabled(action)
            ]
        actions = set(cat.get("shorts", [])) | set(cat.get("dos", []))
        return sorted(action for action in actions if not commerce_action_disabled(action))

    def bootstrap(self, *, include_im: bool = True) -> Dict[str, ApiResult]:
        """Pull common public + personal data after login (like app cold start)."""
        out: Dict[str, ApiResult] = {}
        out["ad"] = self.content.is_show_ad()
        out["recommend"] = self.content.recommend()
        out["censor"] = self.content.chat_censorship()
        out["online"] = self.misc.update_online(first=True)
        if self.session.logged_in:
            out["me"] = self.profile.get_me()
            out["etiquette"] = self.profile.etiquette()
            if include_im:
                out["txim"] = self.im.tencent_sign()
        return out
