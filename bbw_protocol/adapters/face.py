"""Face real-name adapter — HTTP orchestration; live face needs Aliyun ZIM SDK."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from ..app import BeibeiwuApp


@dataclass
class FaceSession:
    """State for one real-name attempt."""

    cert_name: str
    cert_no: str
    certify_id: str = ""
    meta_info: str = ""
    init_ok: bool = False
    init_message: str = ""
    init_raw: Any = None
    describe_ok: bool = False
    describe_message: str = ""
    describe_raw: Any = None
    save_ok: bool = False
    save_message: str = ""
    hint: str = (
        "metaInfo MUST come from Aliyun Face ZIM / real-person SDK. "
        "Fake metaInfo → Init fails; fake certifyId → Describe may say T but rp_verify_time stays 0."
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FaceAdapter:
    """Wire InitFaceVerify → (SDK live face) → DescribeFaceVerify → optional SaveRPVerifyInfo."""

    def __init__(self, app: BeibeiwuApp):
        self.app = app
        self._last: Optional[FaceSession] = None

    @property
    def last(self) -> Optional[FaceSession]:
        return self._last

    def start(
        self,
        cert_name: str,
        cert_no: str,
        meta_info: str,
    ) -> FaceSession:
        """Call InitFaceVerify0 with SDK-generated metaInfo + real ID fields."""
        sess = FaceSession(cert_name=cert_name, cert_no=cert_no, meta_info=meta_info)
        r = self.app.misc.face_init(meta_info, cert_no, cert_name)
        sess.init_ok = r.ok
        sess.init_message = r.message or r.code or r.raw[:200]
        sess.init_raw = r.data if r.data is not None else r.raw

        # extract certifyId from various shapes
        cid = ""
        data = r.data
        if isinstance(data, dict):
            cid = (
                data.get("certifyId")
                or data.get("CertifyId")
                or data.get("result")
                or ""
            )
            if not cid and isinstance(data.get("data"), dict):
                d = data["data"]
                cid = d.get("certifyId") or d.get("CertifyId") or d.get("result") or ""
            if not cid and isinstance(data.get("ResultObject"), dict):
                cid = data["ResultObject"].get("CertifyId") or ""
        sess.certify_id = str(cid or "")
        self._last = sess
        return sess

    def describe(
        self,
        certify_id: Optional[str] = None,
        cert_name: Optional[str] = None,
        cert_no: Optional[str] = None,
    ) -> FaceSession:
        """Query Aliyun result via backend. Prefer real certifyId from start()."""
        base = self._last or FaceSession(cert_name="", cert_no="")
        name = cert_name or base.cert_name
        no = cert_no or base.cert_no
        cid = certify_id or base.certify_id
        if not (name and no and cid):
            raise ValueError("cert_name, cert_no, certify_id required")
        sess = FaceSession(
            cert_name=name,
            cert_no=no,
            certify_id=cid,
            meta_info=base.meta_info,
            init_ok=base.init_ok,
            init_message=base.init_message,
            init_raw=base.init_raw,
        )
        r = self.app.misc.face_describe(cid, name, no)
        sess.describe_ok = r.ok
        sess.describe_message = r.message or r.code or ""
        sess.describe_raw = r.data if r.data is not None else r.raw
        # Note: message=T is NOT proof of server-side real-name (see analysis/08)
        if r.message == "T" or (isinstance(r.data, dict) and r.data.get("message") == "T"):
            sess.hint = (
                "Describe returned T — verify rp_verify_time after re-login; "
                "forged certifyId historically did NOT flip server real-name."
            )
        self._last = sess
        return sess

    def save(
        self,
        cert_name: Optional[str] = None,
        cert_no: Optional[str] = None,
        result: str = "T",
    ) -> FaceSession:
        """Client-side saveRP path (often cosmetic; server truth is Aliyun + backend)."""
        base = self._last or FaceSession(cert_name="", cert_no="")
        name = cert_name or base.cert_name
        no = cert_no or base.cert_no
        if not (name and no):
            raise ValueError("cert_name and cert_no required")
        r = self.app.misc.save_rp_verify(name, no, result)
        base.cert_name = name
        base.cert_no = no
        base.save_ok = r.ok
        base.save_message = r.message or r.code or r.raw[:200]
        self._last = base
        return base

    def manual(
        self, cert_name: str, cert_no: str, result: str = "pending"
    ) -> Dict[str, Any]:
        """Fallback: applyManualVerify (photo OSS path usually still needed in App)."""
        r = self.app.misc.apply_manual_verify(cert_name, cert_no, result)
        return {
            "ok": r.ok,
            "code": r.code,
            "message": r.message,
            "data": r.data,
            "hint": "Manual verify may require uploaded ID photos via OSS (getAliyunSignature).",
        }

    def status_hint(self) -> Dict[str, Any]:
        me = self.app.session.summary()
        rp = getattr(self.app.session, "rp_verify_time", None)
        return {
            "session": me,
            "rp_verify_time": rp,
            "pipeline": [
                "1. Aliyun ZIM SDK → metaInfo",
                "2. face.start(name, no, metaInfo) → certifyId",
                "3. ZIM verify with certifyId",
                "4. face.describe(certifyId)",
                "5. re-login / get_me → rp_verify_time != 0",
            ],
            "last": self._last.to_dict() if self._last else None,
        }
