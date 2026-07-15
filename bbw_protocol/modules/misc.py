"""Misc: online heartbeat, moderation, verify helpers, generic."""

from __future__ import annotations

from typing import Any, Optional

from .. import sign
from ..client import FACE_DESC, FACE_INIT, ApiResult, ProtocolClient


class MiscAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def update_online(self, first: bool = False) -> ApiResult:
        s = self.c.session
        body = {
            "id": s.uid,
            "sign": sign.sign_token(s.uid),
            "version_code": getattr(s, "version_code", None) or sign.VERSION_CODE,
        }
        if first:
            body["first"] = "first"
        return self.c.call("UpdateOnline0", body)

    def front_or_back(self, frontorback: str = "1") -> ApiResult:
        s = self.c.session
        return self.c.call_url(
            "https://redis.banghua.xin/app/index.php?i=999999&c=entry&a=webapp&do=xiaobeifrontorback&m=rediscache",
            myid=s.uid,
            frontorback=frontorback,
            phonebrand=getattr(s, "phonebrand", None) or "Android",
            pushregid=getattr(s, "pushregid", None) or "bbw_protocol",
        )

    def check_age(self, cert_no: str) -> ApiResult:
        return self.c.call("checkAge", certNo=cert_no)

    def apply_manual_verify(
        self, cert_name: str, cert_no: str, result: str
    ) -> ApiResult:
        return self.c.call(
            "applyManualVerify",
            cert_name=cert_name,
            cert_no=cert_no,
            result=result,
        )

    def face_init(self, meta_info: str, cert_no: str, cert_name: str) -> ApiResult:
        return self.c.call_url(
            FACE_INIT,
            metaInfo=meta_info,
            certNo=cert_no,
            certName=cert_name,
        )

    def face_describe(
        self, certify_id: str, cert_name: str, cert_no: str, id_: Optional[str] = None
    ) -> ApiResult:
        return self.c.call_url(
            FACE_DESC,
            id=id_ or self.c.session.uid,
            cert_name=cert_name,
            cert_no=cert_no,
            certifyId=certify_id,
        )

    def save_rp_verify(
        self, cert_name: str, cert_no: str, result: str, id_: Optional[str] = None
    ) -> ApiResult:
        return self.c.request(
            self.c.url("SaveRPVerifyInfo", i="888"),
            {
                "id": id_ or self.c.session.uid,
                "cert_name": cert_name,
                "cert_no": cert_no,
                "result": result,
            },
        )

    def verify_cert_no(self, cert_no: str) -> ApiResult:
        return self.c.call("verify_certNo", cert_no=cert_no)

    def id2_meta_verify(self, **params: Any) -> ApiResult:
        """v154+ Id2MetaVerifyRequest (二要素/元信息类；缺参常见 401)."""
        return self.c.call("Id2MetaVerifyRequest", params)

    def verify_phone(self, **params: Any) -> ApiResult:
        return self.c.call("verifyPhone", params)

    def upload_error(self, **params: Any) -> ApiResult:
        return self.c.call("UploadError", params)

    def add_moderation(self, **params: Any) -> ApiResult:
        return self.c.call("AddModeration", params)

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
