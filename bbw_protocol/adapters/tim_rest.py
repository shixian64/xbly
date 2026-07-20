"""Tencent Cloud IM REST (server-side) fallback when browser TIM Web SDK cannot login.

Uses the SDKAppID plus an externally configured secret only inside the BFF/protocol process.
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
SUPPORTED_ELEMENT_TYPES = {
    "TIMTextElem",
    "TIMCustomElem",
    "TIMImageElem",
    "TIMSoundElem",
    "TIMVideoFileElem",
    "TIMFileElem",
    "TIMLocationElem",
    "TIMFaceElem",
}


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
    """TIM REST fallback for account probes and already-hosted C2C elements.

    The REST API can reference media that has already been uploaded, but it
    cannot turn a browser ``File`` or a local path into a TIM media object.
    Product Web uploads therefore stay on the official TIM Web SDK; these
    helpers are intentionally named ``send_remote_*`` to avoid false success.
    """

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

    def recent_contacts(
        self,
        user_id: str,
        *,
        timestamp: int = 0,
        start_index: int = 0,
        top_timestamp: int = 0,
        top_start_index: int = 0,
        assist_flags: int = 7,
    ) -> RestResult:
        """Return the server-owned recent C2C conversation list for one user."""

        account = str(user_id or "").strip()
        if not account:
            return RestResult(
                ok=False,
                action="recentcontact/get_list",
                error_code=-2,
                error_info="missing user account",
            )
        return self.call(
            "recentcontact/get_list",
            {
                "From_Account": account,
                "TimeStamp": max(0, int(timestamp)),
                "StartIndex": max(0, int(start_index)),
                "TopTimeStamp": max(0, int(top_timestamp)),
                "TopStartIndex": max(0, int(top_start_index)),
                "AssistFlags": max(0, int(assist_flags)),
            },
        )

    def roaming_messages(
        self,
        from_account: str,
        to_account: str,
        *,
        min_time: int = 0,
        max_time: int = 0,
        max_count: int = 100,
        last_msg_key: str = "",
    ) -> RestResult:
        """Read one direction of C2C roaming history through the administrator API."""

        sender = str(from_account or "").strip()
        recipient = str(to_account or "").strip()
        if not sender or not recipient or sender == recipient:
            return RestResult(
                ok=False,
                action="openim/admin_getroammsg",
                error_code=-2,
                error_info="invalid from/to account",
            )
        return self.call(
            "openim/admin_getroammsg",
            {
                "From_Account": sender,
                "To_Account": recipient,
                "MaxCnt": max(1, min(int(max_count), 100)),
                "MinTime": max(0, int(min_time)),
                "MaxTime": max(0, int(max_time)),
                "LastMsgKey": str(last_msg_key or "")[:256],
            },
        )

    def c2c_unread_counts(
        self,
        to_account: str,
        peer_accounts: List[str],
    ) -> RestResult:
        """Return per-peer C2C unread counts for one account."""

        account = str(to_account or "").strip()
        peers = list(
            dict.fromkeys(
                str(peer or "").strip()
                for peer in peer_accounts
                if str(peer or "").strip()
                and str(peer or "").strip() != account
            )
        )[:100]
        if not account or not peers:
            return RestResult(
                ok=False,
                action="openim/get_c2c_unread_msg_num",
                error_code=-2,
                error_info="missing target account or peer accounts",
            )
        return self.call(
            "openim/get_c2c_unread_msg_num",
            {
                "To_Account": account,
                "Peer_Account": peers,
            },
        )

    def mark_c2c_read(self, report_account: str, peer_account: str) -> RestResult:
        """Mark one C2C conversation as read for ``report_account``."""

        account = str(report_account or "").strip()
        peer = str(peer_account or "").strip()
        if not account or not peer or account == peer:
            return RestResult(
                ok=False,
                action="openim/admin_set_msg_read",
                error_code=-2,
                error_info="invalid report/peer account",
            )
        return self.call(
            "openim/admin_set_msg_read",
            {
                "Report_Account": account,
                "Peer_Account": peer,
            },
        )

    def revoke_c2c(self, from_account: str, to_account: str, msg_key: str) -> RestResult:
        """Recall one C2C message previously sent by ``from_account``.

        Tencent identifies a REST-sent/history message with ``MsgKey``.  Unlike
        the client SDK's default two-minute window, this administrator API has
        no fixed recall deadline, but the message must still be inside the
        application's roaming-storage lifetime.  The BFF supplies
        ``from_account`` from the authenticated session so a browser cannot
        recall a message on behalf of another user.
        """
        sender = str(from_account or "").strip()
        receiver = str(to_account or "").strip()
        key = str(msg_key or "").strip()
        if not sender or not receiver or not key:
            return RestResult(
                ok=False,
                action="openim/admin_msgwithdraw",
                error_code=-2,
                error_info="missing from/to account or MsgKey",
            )
        return self.call(
            "openim/admin_msgwithdraw",
            {
                "From_Account": sender,
                "To_Account": receiver,
                "MsgKey": key,
            },
        )

    @staticmethod
    def _remote_url(value: Any) -> str:
        url = str(value or "").strip()
        return url if url.startswith(("https://", "http://")) else ""

    @staticmethod
    def _cloud_data(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _invalid(error: str, code: int = -2) -> RestResult:
        return RestResult(
            ok=False,
            action="openim/sendmsg",
            error_code=code,
            error_info=error,
        )

    def _element_error(self, msg_type: str, content: Dict[str, Any]) -> str:
        if msg_type == "TIMTextElem" and not str(content.get("Text") or "").strip():
            return "empty TIMTextElem text"
        if msg_type == "TIMFaceElem" and not str(content.get("Data") or "").strip():
            return "empty TIMFaceElem data"
        if msg_type == "TIMImageElem":
            infos = content.get("ImageInfoArray")
            has_url = isinstance(infos, list) and any(
                isinstance(info, dict) and self._remote_url(info.get("URL") or info.get("url"))
                for info in infos
            )
            if not str(content.get("UUID") or "").strip() or not has_url:
                return "TIMImageElem requires uploaded UUID and http(s) ImageInfoArray"
        if msg_type == "TIMSoundElem":
            if not self._remote_url(content.get("Url") or content.get("URL")):
                return "TIMSoundElem requires an uploaded http(s) URL"
            if not str(content.get("UUID") or "").strip():
                return "TIMSoundElem requires UUID"
        if msg_type == "TIMVideoFileElem":
            if not self._remote_url(content.get("VideoUrl")) or not self._remote_url(
                content.get("ThumbUrl")
            ):
                return "TIMVideoFileElem requires uploaded video and thumbnail URLs"
            if not str(content.get("VideoUUID") or "").strip() or not str(
                content.get("ThumbUUID") or ""
            ).strip():
                return "TIMVideoFileElem requires video and thumbnail UUIDs"
        if msg_type == "TIMFileElem":
            if not self._remote_url(content.get("Url") or content.get("URL")):
                return "TIMFileElem requires an uploaded http(s) URL"
            if not str(content.get("UUID") or "").strip() or not str(
                content.get("FileName") or ""
            ).strip():
                return "TIMFileElem requires UUID and file name"
        if msg_type == "TIMCustomElem" and not any(
            str(content.get(key) or "") for key in ("Data", "Desc", "Ext")
        ):
            return "empty TIMCustomElem"
        return ""

    def send_elements(
        self,
        from_account: str,
        to_account: str,
        elements: List[Dict[str, Any]],
        *,
        cloud_custom_data: Any = None,
        sync_other_machine: int = 1,
        offline_push_info: Optional[Dict[str, Any]] = None,
    ) -> RestResult:
        """Send documented TIM ``MsgBody`` elements that need no local upload."""
        if not str(from_account or "").strip() or not str(to_account or "").strip():
            return self._invalid("missing account")
        if not isinstance(elements, list) or not elements:
            return self._invalid("empty MsgBody")
        body_elements: List[Dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                return self._invalid("invalid MsgBody element")
            msg_type = str(element.get("MsgType") or "").strip()
            content = element.get("MsgContent")
            if msg_type not in SUPPORTED_ELEMENT_TYPES:
                return self._invalid(f"unsupported TIM element: {msg_type or 'empty'}")
            if not isinstance(content, dict):
                return self._invalid(f"{msg_type} MsgContent must be an object")
            element_error = self._element_error(msg_type, content)
            if element_error:
                return self._invalid(element_error)
            body_elements.append({"MsgType": msg_type, "MsgContent": dict(content)})

        body: Dict[str, Any] = {
            "SyncOtherMachine": int(sync_other_machine),
            "From_Account": str(from_account),
            "To_Account": str(to_account),
            "MsgRandom": random.randint(0, 0xFFFFFFFF),
            "MsgBody": body_elements,
        }
        cloud_data = self._cloud_data(cloud_custom_data)
        if cloud_data:
            body["CloudCustomData"] = cloud_data
        if isinstance(offline_push_info, dict) and offline_push_info:
            body["OfflinePushInfo"] = dict(offline_push_info)
        return self.call("openim/sendmsg", body)

    def send_text(
        self,
        from_account: str,
        to_account: str,
        text: str,
        *,
        cloud_custom_data: Any = None,
        sync_other_machine: int = 1,
    ) -> RestResult:
        """Send a C2C text message as from_account (admin API, appears from that user)."""
        text = str(text or "").strip()
        if not text:
            return RestResult(ok=False, action="openim/sendmsg", error_info="empty text")
        if not from_account or not to_account:
            return RestResult(ok=False, action="openim/sendmsg", error_info="missing account")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMTextElem",
                    "MsgContent": {"Text": text[:2000]},
                }
            ],
            cloud_custom_data=cloud_custom_data,
            sync_other_machine=sync_other_machine,
        )

    def send_face(
        self,
        from_account: str,
        to_account: str,
        index: int,
        data: str,
        *,
        sync_other_machine: int = 1,
    ) -> RestResult:
        face_data = str(data or "").strip()
        if not face_data:
            return self._invalid("empty TIMFaceElem data")
        return self.send_elements(
            from_account,
            to_account,
            [{"MsgType": "TIMFaceElem", "MsgContent": {"Index": int(index), "Data": face_data}}],
            sync_other_machine=sync_other_machine,
        )

    def send_remote_image(
        self,
        from_account: str,
        to_account: str,
        *,
        uuid: str,
        image_info: List[Dict[str, Any]],
        image_format: int = 1,
    ) -> RestResult:
        infos: List[Dict[str, Any]] = []
        for raw in image_info if isinstance(image_info, list) else []:
            if not isinstance(raw, dict):
                continue
            url = self._remote_url(raw.get("URL") or raw.get("url"))
            if not url:
                continue
            infos.append(
                {
                    "Type": int(raw.get("Type", raw.get("type", 0)) or 0),
                    "Size": int(raw.get("Size", raw.get("size", 0)) or 0),
                    "Width": int(raw.get("Width", raw.get("width", 0)) or 0),
                    "Height": int(raw.get("Height", raw.get("height", 0)) or 0),
                    "URL": url,
                }
            )
        if not str(uuid or "").strip() or not infos:
            return self._invalid("remote image requires UUID and an http(s) ImageInfoArray")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMImageElem",
                    "MsgContent": {
                        "UUID": str(uuid),
                        "ImageFormat": int(image_format),
                        "ImageInfoArray": infos,
                    },
                }
            ],
        )

    def send_remote_audio(
        self,
        from_account: str,
        to_account: str,
        *,
        url: str,
        uuid: str,
        duration: int,
        size: int = 0,
    ) -> RestResult:
        remote_url = self._remote_url(url)
        if not remote_url or not str(uuid or "").strip() or int(duration or 0) <= 0:
            return self._invalid("remote audio requires URL, UUID and positive duration")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMSoundElem",
                    "MsgContent": {
                        "Url": remote_url,
                        "UUID": str(uuid),
                        "Size": max(0, int(size or 0)),
                        "Second": int(duration),
                        "Download_Flag": 2,
                    },
                }
            ],
        )

    def send_remote_video(
        self,
        from_account: str,
        to_account: str,
        *,
        video_url: str,
        video_uuid: str,
        duration: int,
        thumb_url: str,
        thumb_uuid: str,
        video_size: int = 0,
        thumb_size: int = 0,
        thumb_width: int = 0,
        thumb_height: int = 0,
        video_format: str = "mp4",
    ) -> RestResult:
        remote_video = self._remote_url(video_url)
        remote_thumb = self._remote_url(thumb_url)
        if not all((remote_video, remote_thumb, str(video_uuid or "").strip(), str(thumb_uuid or "").strip())):
            return self._invalid("remote video requires video/thumb URLs and UUIDs")
        if int(duration or 0) <= 0:
            return self._invalid("remote video requires positive duration")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMVideoFileElem",
                    "MsgContent": {
                        "VideoUrl": remote_video,
                        "VideoUUID": str(video_uuid),
                        "VideoSize": max(0, int(video_size or 0)),
                        "VideoSecond": int(duration),
                        "VideoFormat": str(video_format or "mp4"),
                        "ThumbUrl": remote_thumb,
                        "ThumbUUID": str(thumb_uuid),
                        "ThumbSize": max(0, int(thumb_size or 0)),
                        "ThumbWidth": max(0, int(thumb_width or 0)),
                        "ThumbHeight": max(0, int(thumb_height or 0)),
                        "Download_Flag": 2,
                    },
                }
            ],
        )

    def send_remote_file(
        self,
        from_account: str,
        to_account: str,
        *,
        url: str,
        uuid: str,
        file_name: str,
        file_size: int = 0,
    ) -> RestResult:
        remote_url = self._remote_url(url)
        if not all((remote_url, str(uuid or "").strip(), str(file_name or "").strip())):
            return self._invalid("remote file requires URL, UUID and file name")
        return self.send_elements(
            from_account,
            to_account,
            [
                {
                    "MsgType": "TIMFileElem",
                    "MsgContent": {
                        "Url": remote_url,
                        "UUID": str(uuid),
                        "FileSize": max(0, int(file_size or 0)),
                        "FileName": str(file_name),
                        "Download_Flag": 2,
                    },
                }
            ],
        )

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
