"""Tencent Cloud IM REST (server-side) fallback when browser TIM Web SDK cannot login.

Uses the APK-derived SDKAppID + SECRETKEY only inside the BFF/protocol process.
Docs: https://cloud.tencent.com/document/product/269/2282
"""

from __future__ import annotations

import json
import random
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .. import sign

REST_BASE = "https://console.tim.qq.com/v4"
DEFAULT_ADMIN = "administrator"


@dataclass
class RestResult:
    ok: bool
    action: str
    error_code: int = 0
    error_info: str = ""
    data: Any = None
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "error_code": self.error_code,
            "error_info": self.error_info,
            "data": self.data,
        }


class TimRestClient:
    """Minimal REST client for C2C text send + account probes."""

    def __init__(
        self,
        sdk_app_id: int = sign.TXIM_SDKAPPID,
        secret_key: str = sign.TXIM_SECRETKEY,
        admin_id: str = DEFAULT_ADMIN,
        timeout: int = 15,
    ):
        self.sdk_app_id = int(sdk_app_id)
        self.secret_key = secret_key
        self.admin_id = admin_id
        self.timeout = timeout
        self._ctx = ssl.create_default_context()

    def _usersig(self, identifier: Optional[str] = None, expire: int = 86400) -> str:
        return sign.gen_user_sig(
            identifier or self.admin_id,
            sdkappid=self.sdk_app_id,
            secret_key=self.secret_key,
            expire=expire,
        )

    def call(self, command: str, body: Dict[str, Any], *, admin: Optional[str] = None) -> RestResult:
        identifier = admin or self.admin_id
        usersig = self._usersig(identifier)
        rnd = random.randint(0, 0xFFFFFFFF)
        url = (
            f"{REST_BASE}/{command}"
            f"?sdkappid={self.sdk_app_id}"
            f"&identifier={urllib.parse.quote(str(identifier))}"
            f"&usersig={urllib.parse.quote(usersig)}"
            f"&random={rnd}"
            f"&contenttype=json"
        )
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            return RestResult(
                ok=False,
                action=command,
                error_code=int(data.get("ErrorCode") or e.code or -1),
                error_info=str(data.get("ErrorInfo") or e.reason or "HTTP error"),
                data=data,
                raw=raw,
            )
        except Exception as e:
            return RestResult(ok=False, action=command, error_code=-1, error_info=str(e)[:300])

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return RestResult(
                ok=False,
                action=command,
                error_code=-1,
                error_info="non-json response",
                raw=raw[:500],
            )

        code = int(data.get("ErrorCode") or 0)
        ok = str(data.get("ActionStatus") or "").upper() == "OK" or code == 0
        return RestResult(
            ok=ok,
            action=command,
            error_code=code,
            error_info=str(data.get("ErrorInfo") or ""),
            data=data,
            raw=raw,
        )

    def account_check(self, user_ids: List[str]) -> RestResult:
        items = [{"UserID": str(u)} for u in user_ids if str(u).strip()]
        return self.call("im_open_login_svc/account_check", {"CheckItem": items})

    def query_online(self, user_ids: List[str]) -> RestResult:
        return self.call(
            "openim/query_online_status",
            {"To_Account": [str(u) for u in user_ids if str(u).strip()]},
        )

    def send_text(
        self,
        from_account: str,
        to_account: str,
        text: str,
        *,
        sync_other_machine: int = 1,
    ) -> RestResult:
        """Send a C2C text message as from_account (admin API, appears from that user)."""
        text = str(text or "").strip()
        if not text:
            return RestResult(ok=False, action="openim/sendmsg", error_info="empty text")
        if not from_account or not to_account:
            return RestResult(ok=False, action="openim/sendmsg", error_info="missing account")
        body = {
            "SyncOtherMachine": int(sync_other_machine),
            "From_Account": str(from_account),
            "To_Account": str(to_account),
            "MsgRandom": random.randint(0, 0xFFFFFFFF),
            "MsgBody": [
                {
                    "MsgType": "TIMTextElem",
                    "MsgContent": {"Text": text[:2000]},
                }
            ],
        }
        return self.call("openim/sendmsg", body)

    def health(self, sample_uid: str = "1") -> Dict[str, Any]:
        """Quick connectivity check: admin UserSig + account_check."""
        r = self.account_check([sample_uid])
        return {
            "ok": r.ok or r.error_code in (0,),
            "sdk_app_id": self.sdk_app_id,
            "admin": self.admin_id,
            "error_code": r.error_code,
            "error_info": r.error_info,
            "note": "REST works with APK secret; use when browser TIM.login hangs.",
        }


