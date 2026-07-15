"""Login / register / password / SMS."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .. import sign
from ..client import APPLET, SMS_URL, ApiResult, ProtocolClient


class AuthAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def _device_fields(
        self,
        phonebrand: Optional[str] = None,
        pushregid: Optional[str] = None,
        version_code: Optional[str] = None,
    ) -> Dict[str, str]:
        s = self.c.session
        return {
            "phonebrand": phonebrand or getattr(s, "phonebrand", None) or "Android",
            "pushregid": pushregid or getattr(s, "pushregid", None) or "bbw_protocol",
            "version_code": version_code
            or getattr(s, "version_code", None)
            or sign.VERSION_CODE,
        }

    def login_password(
        self,
        phone: str,
        password: str,
        *,
        phonebrand: Optional[str] = None,
        pushregid: Optional[str] = None,
        version_code: Optional[str] = None,
    ) -> ApiResult:
        dev = self._device_fields(phonebrand, pushregid, version_code)
        body = {
            "userAccount": phone,
            "userPassword": password,
            "uniquelogintoken": sign.unique_login_token(self.c.session.uid or "0"),
            **dev,
        }
        # login often without prior token
        r = self.c.request(self.c.url("signin0"), body, uid="0", token="0")
        self._apply_login_result(r, phone=phone, password=password)
        return r

    def login_onekey(
        self,
        phone: str,
        *,
        version_code: Optional[str] = None,
        phonebrand: Optional[str] = None,
        pushregid: Optional[str] = None,
    ) -> ApiResult:
        """SigninOneKeyLogin1 — known weak path (may not need SMS)."""
        dev = self._device_fields(phonebrand, pushregid, version_code)
        body = {
            "userAccount": phone,
            "uniquelogintoken": sign.unique_login_token("0"),
            **dev,
        }
        r = self.c.request(self.c.url("SigninOneKeyLogin1"), body, uid="0", token="0")
        self._apply_login_result(r, phone=phone)
        return r

    def login_onekey_legacy(self, phone: str) -> ApiResult:
        body = {
            "userAccount": phone,
            "uniquelogintoken": sign.unique_login_token("0"),
        }
        return self.c.request(self.c.url("SigninOneKeyLogin"), body, uid="0", token="0")

    def send_sms(self, phone: str) -> ApiResult:
        return self.c.request(SMS_URL, {"phoneNumber": phone}, uid="0", token="0")

    def sms_verify(self, phone: str, code: str) -> ApiResult:
        return self.c.call("smsVerify0", phone=phone, code=code)

    def sms_login(self, phone: str, code: str) -> ApiResult:
        v = self.sms_verify(phone, code)
        if not v.ok and v.code not in ("200",):
            return v
        return self.login_onekey(phone)

    def find_password(self, phone: str, new_password: str) -> ApiResult:
        body = {
            "sign": sign.sign_token(phone),
            "userPhone": phone,
            "userPassword": new_password,
        }
        r = self.c.call("findpassword", body)
        if r.ok or "成功" in (r.raw or ""):
            self.c.session.password = new_password
            self.c.session.phone = phone
        return r

    def signup(self, **fields: Any) -> ApiResult:
        return self.c.call("signup", fields)

    def logout(self) -> ApiResult:
        r = self.c.call("logout")
        self.c.session.uid = "0"
        self.c.session.token = "0"
        return r

    def unique_login_check(self, myid: Optional[str] = None, token: Optional[str] = None) -> ApiResult:
        uid = myid or self.c.session.uid
        tok = token or sign.unique_login_token(uid)
        return self.c.call("uniquelogin", myid=uid, token=tok)

    def refresh_me(self) -> ApiResult:
        """Re-login with stored password if present, else getUserAttributesMe1."""
        if self.c.session.phone and self.c.session.password:
            return self.login_password(self.c.session.phone, self.c.session.password)
        dev = self._device_fields()
        return self.c.call(
            "getUserAttributesMe1",
            userId=self.c.session.uid,
            **dev,
        )

    def _apply_login_result(
        self, r: ApiResult, phone: str = "", password: str = ""
    ) -> None:
        if not r.data or not isinstance(r.data, dict):
            return
        user = r.data.get("json_obj")
        if user is None and isinstance(r.data.get("json"), str):
            try:
                user = json.loads(r.data["json"])
            except Exception:
                user = None
        if not isinstance(user, dict):
            # maybe top-level user fields
            if "id" in r.data and "token" in r.data:
                user = r.data
            else:
                return
        self.c.session.update_from_user(user)
        if phone:
            self.c.session.phone = phone
        if password:
            self.c.session.password = password
        # uniquelogintoken for later
        if self.c.session.uid and self.c.session.uid != "0":
            # store computed unique token into session raw
            self.c.session.raw_user["uniquelogintoken_local"] = sign.unique_login_token(
                self.c.session.uid
            )
