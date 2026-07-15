"""Profile / user attributes / reset personal info."""

from __future__ import annotations

from typing import Any, Optional

from .. import sign
from ..client import ApiResult, ProtocolClient


class ProfileAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def get_user(self, user_id: Optional[str] = None, lat: str = "0", lng: str = "0") -> ApiResult:
        return self.c.call(
            "getUserAttributes",
            userId=user_id or self.c.session.uid,
            latitude=lat,
            longitude=lng,
        )

    def get_user0(self, user_id: Optional[str] = None, lat: str = "0", lng: str = "0") -> ApiResult:
        return self.c.call(
            "getUserAttributes0",
            userId=user_id or self.c.session.uid,
            latitude=lat,
            longitude=lng,
        )

    def get_me(
        self,
        phonebrand: str = "Android",
        pushregid: str = "bbw_protocol",
        version_code: str = sign.VERSION_CODE,
    ) -> ApiResult:
        r = self.c.call(
            "getUserAttributesMe1",
            userId=self.c.session.uid,
            phonebrand=phonebrand,
            pushregid=pushregid,
            version_code=version_code,
        )
        # try update session if dict user
        if isinstance(r.data, dict) and r.data.get("id"):
            self.c.session.update_from_user(r.data)
        return r

    def reset_num(self, type_: str = "昵称") -> ApiResult:
        return self.c.call("resetNum", uid=self.c.session.uid, type=type_)

    def reset_nickname(self, nickname: str) -> ApiResult:
        fields = {
            "sign": sign.sign_token(self.c.session.uid),
            "type": "昵称设置",
            "userId": self.c.session.uid,
            "value": nickname,
        }
        r = self.c.call_multipart("resetNew", fields)
        if r.ok or "成功" in (r.raw or ""):
            self.c.session.nickname = nickname
        return r

    def reset_personal(
        self, value: str, type_: str = "昵称设置", as_form: bool = False
    ) -> ApiResult:
        fields = {
            "sign": sign.sign_token(self.c.session.uid),
            "type": type_,
            "userId": self.c.session.uid,
            "value": value,
        }
        if as_form:
            return self.c.call("resetNew", fields)
        return self.c.call_multipart("resetNew", fields)

    def reset_name(self, value: str) -> ApiResult:
        return self.c.call("resetName", uid=self.c.session.uid, value=value)

    def update_location_ip(self) -> ApiResult:
        return self.c.call("UpdatelocationWithIP")

    def update_location_tencent(self, **params: Any) -> ApiResult:
        return self.c.call("UpdatelocationTencent", params)

    def set_allow_call(self, **params: Any) -> ApiResult:
        return self.c.call("SetAllowCall", params)

    def set_privacy(self, **params: Any) -> ApiResult:
        return self.c.call("setprivatesetting", params)

    def get_privacy(self) -> ApiResult:
        return self.c.call("getprivatesetting")

    def etiquette(self) -> ApiResult:
        return self.c.call("getEtiquetteScoreAndDescription")

    def same_identity_card(self) -> ApiResult:
        return self.c.call("GetSameIdentityCard")

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
