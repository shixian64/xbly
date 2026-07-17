"""IM realtime adapter — credentials only; connect with official TIM/Rong SDK."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .. import sign
from ..app import BeibeiwuApp

# From APK BuildConfig
RONG_APP_KEY = "m7ua80gbmo0km"


@dataclass
class TimCredentials:
    """Payload for Tencent Cloud Chat (TIM) Web/App SDK login."""

    sdk_app_id: int
    user_id: str
    user_sig: str
    source: str  # "local" | "server"
    expire_hint: str = "local sig typically 7d; server may differ"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def web_login_options(self) -> Dict[str, Any]:
        """Shape expected by @tencentcloud/chat SDK create / login."""
        return {
            "SDKAppID": self.sdk_app_id,
            "userID": self.user_id,
            "userSig": self.user_sig,
        }


@dataclass
class RongCredentials:
    """Payload for RongCloud IM connect."""

    app_key: str
    user_id: str
    token: str
    nickname: str
    portrait: str
    raw: Any = None
    ok: bool = False
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # raw may be huge/non-serializable
        if d.get("raw") is not None and not isinstance(d["raw"], (str, int, float, bool, list, dict, type(None))):
            d["raw"] = str(d["raw"])
        return d


class ImAdapter:
    """Build TIM / Rong credentials from session + protocol HTTP helpers."""

    def __init__(self, app: BeibeiwuApp):
        self.app = app

    @staticmethod
    def _looks_like_user_sig(value: str) -> bool:
        """Heuristic: Tencent UserSig is zlib+base64url (often starts with eJ / eJwt)."""
        s = str(value or "").strip()
        if len(s) < 40:
            return False
        if s[0] in "{[<":
            return False
        # reject plain error messages
        if any(x in s.lower() for x in ("fail", "error", "失败", "无效", "登录")):
            return False
        return True

    def tim_local(self, uid: Optional[str] = None, expire: int = 604800) -> TimCredentials:
        """Generate UserSig with the server-side configured secret (never ship it to the browser)."""
        user_id = uid or self.app.session.uid
        if not user_id:
            raise ValueError("uid empty; login first")
        return TimCredentials(
            sdk_app_id=sign.TXIM_SDKAPPID,
            user_id=str(user_id),
            user_sig=sign.gen_user_sig(str(user_id), expire=expire),
            source="local",
        )

    def tim_server(
        self,
        uid: Optional[str] = None,
        *,
        allow_local_fallback: bool = True,
    ) -> TimCredentials:
        """Fetch a server-issued UserSig, optionally falling back to local signing.

        Product Web routes disable the fallback so the APK-derived signing secret is
        never used implicitly.  CLI/lab callers may still opt into the historical
        fallback when they are explicitly operating in the research environment.
        """
        user_id = uid or self.app.session.uid
        if not user_id:
            raise ValueError("uid empty; login first")
        r = self.app.im.tencent_sign(user_id)
        sig = ""
        if isinstance(r.data, dict):
            # tximsign.php returns: {"code":"200","message":"<UserSig>"}
            # (UserSig lives in message, NOT a nested userSign field.)
            sig = (
                r.data.get("userSign")
                or r.data.get("usersig")
                or r.data.get("UserSig")
                or r.data.get("sig")
                or r.data.get("user_sig")
                or ""
            )
            msg = r.data.get("message")
            if not sig and isinstance(msg, str) and self._looks_like_user_sig(msg):
                sig = msg
            if not sig and isinstance(r.data.get("data"), dict):
                d = r.data["data"]
                sig = (
                    d.get("userSign")
                    or d.get("usersig")
                    or d.get("UserSig")
                    or d.get("sig")
                    or ""
                )
            if not sig and isinstance(r.data.get("data"), str) and self._looks_like_user_sig(
                r.data["data"]
            ):
                sig = r.data["data"]
        if not sig and r.raw and len(r.raw) > 20 and r.raw.strip()[0] not in "{[<":
            # plain text sig
            sig = r.raw.strip()
        if not sig and isinstance(r.message, str) and self._looks_like_user_sig(r.message):
            sig = r.message
        if not sig:
            # session may already hold login-time userSign
            sig = getattr(self.app.session, "user_sign", "") or ""
        if sig:
            return TimCredentials(
                sdk_app_id=sign.TXIM_SDKAPPID,
                user_id=str(user_id),
                user_sig=str(sig),
                source="server",
            )
        if allow_local_fallback:
            return self.tim_local(user_id)
        raise RuntimeError("server did not return a usable TIM UserSig")

    def tim_login_payload(
        self,
        prefer: str = "local",
        uid: Optional[str] = None,
        *,
        allow_local_fallback: bool = True,
    ) -> Dict[str, Any]:
        """Ready for frontend: {SDKAppID, userID, userSig, source}."""
        if prefer == "server":
            cred = self.tim_server(uid, allow_local_fallback=allow_local_fallback)
        else:
            cred = self.tim_local(uid)
        out = cred.web_login_options()
        out["source"] = cred.source
        out["hint"] = (
            "Use TIM.create({SDKAppID}).login({userID, userSig}). "
            "Do not expose TXIM_SECRETKEY to the browser."
        )
        return out

    def rong_register(
        self,
        user_id: Optional[str] = None,
        nickname: Optional[str] = None,
        portrait: Optional[str] = None,
    ) -> RongCredentials:
        r = self.app.im.rong_register(
            user_id=user_id,
            nickname=nickname,
            portrait=portrait,
        )
        token = ""
        uid = user_id or self.app.session.uid or ""
        nick = nickname or self.app.session.nickname or "user"
        port = portrait or getattr(self.app.session, "portrait", "") or ""
        data = r.data
        if isinstance(data, dict):
            token = data.get("token") or data.get("Token") or ""
            nested = data.get("data")
            if not token and isinstance(nested, dict):
                token = nested.get("token") or nested.get("Token") or ""
            elif not token and isinstance(nested, str):
                token = nested
            uid = str(data.get("userID") or data.get("userId") or data.get("user_id") or uid)
            if isinstance(nested, dict):
                uid = str(
                    nested.get("userID")
                    or nested.get("userId")
                    or nested.get("user_id")
                    or uid
                )
        if not token and r.raw:
            # sometimes plain token
            t = r.raw.strip()
            if t and t[0] not in "{[":
                token = t
        token_s = str(token or "")
        # Observed stub from backend in CTF env: {"token":"123"} — not a real Rong token
        stub = token_s in ("", "123", "null", "None")
        msg = r.message or r.code or ""
        if stub and token_s:
            msg = (msg + " " if msg else "") + "token looks like stub; check rong userregister params/env"
        return RongCredentials(
            app_key=RONG_APP_KEY,
            user_id=str(uid),
            token=token_s,
            nickname=str(nick),
            portrait=str(port),
            raw=data if data is not None else r.raw,
            ok=bool(token_s) and not stub,
            message=msg.strip(),
        )

    def bootstrap(
        self,
        prefer_tim: str = "local",
        *,
        allow_local_fallback: bool = True,
    ) -> Dict[str, Any]:
        """One-shot IM credentials for Web shell cold start."""
        tim = self.tim_login_payload(
            prefer=prefer_tim,
            allow_local_fallback=allow_local_fallback,
        )
        rong = self.rong_register()
        return {
            "tim": tim,
            "rong": rong.to_dict(),
            "note": "Realtime send/recv requires TIM Web SDK or Rong Web SDK after connect.",
        }
