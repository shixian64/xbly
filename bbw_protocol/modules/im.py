"""IM helpers (HTTP side). Full realtime still needs SDK."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from .. import sign
from ..client import APPLET, RONG_REGISTER, TXIM_SIGN, ApiResult, ProtocolClient


class ImAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def tencent_sign(self, uid: Optional[str] = None) -> ApiResult:
        return self.c.call_url(TXIM_SIGN, uid=uid or self.c.session.uid)

    def local_user_sig(self, uid: Optional[str] = None) -> str:
        return sign.gen_user_sig(uid or self.c.session.uid)

    def rong_register(
        self,
        user_id: Optional[str] = None,
        nickname: Optional[str] = None,
        portrait: Optional[str] = None,
    ) -> ApiResult:
        return self.c.call_url(
            RONG_REGISTER,
            # The APK posts the case-sensitive PHP field ``userID``.  Using
            # ``userId`` falls through to the upstream demo endpoint's stub
            # response (observed as {"token":"123"}).
            userID=user_id or self.c.session.uid,
            userNickName=nickname or self.c.session.nickname or "user",
            userPortrait=portrait or self.c.session.portrait or "",
        )

    def flash_photo_get(self, uniqueid: str = "", **params: Any) -> ApiResult:
        value = str(uniqueid or params.pop("uniqueId", "") or params.get("uniqueid", "")).strip()
        if value:
            params["uniqueid"] = value
        return self.c.call("GetflashphotoTencent", params)

    def flash_photo_send(
        self,
        target_id: str = "",
        photo_url: str = "",
        **params: Any,
    ) -> ApiResult:
        target = str(
            target_id or params.pop("target_id", "") or params.get("targetId", "")
        ).strip()
        photo = str(
            photo_url or params.pop("photo_url", "") or params.get("photourl", "")
        ).strip()
        if target:
            params["targetId"] = target
        if photo:
            # APK sends the OSS object key, not the public absolute URL.
            params["photourl"] = photo
        return self.c.call("SendTencentFlashPhoto", params)

    def aliyun_signature(self, content: str) -> ApiResult:
        """Request the APK-compatible OSS signature for one canonical string."""
        canonical = str(content or "")
        if not canonical:
            raise ValueError("content is required")
        # CommonUtil's OSSCustomSignerCredentialProvider uses this exact tenant
        # and sends the canonical string in the GET query, not a form POST.
        url = (
            f"{APPLET}?i=99999&c=entry&a=webapp&do=getAliyunSignature"
            f"&m=socialchat&content={quote(canonical, safe='')}"
        )
        return self.c.request(url, method="GET")

    def stickers(self, **params: Any) -> ApiResult:
        return self.c.call("GetAllStickersWithFavorite", params)

    def sticker_search(self, key: str) -> ApiResult:
        return self.c.call("getStickersByKeyWord", key_word=key)

    def history_message_insert(self, **params: Any) -> ApiResult:
        return self.c.call("insertTencentHistoryMessage", params)

    def history_conversations(
        self,
        page: str = "1",
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> ApiResult:
        """Return the server-side conversation/history summary used by the APK."""
        call_options = {}
        if timeout is not None:
            call_options["timeout"] = timeout
        if deadline is not None:
            call_options["deadline"] = deadline
        return self.c.call(
            "getHistoryConversation",
            page=page,
            **call_options,
        )

    def history_messages(
        self,
        peer_id: str,
        *,
        timeout: Optional[float] = None,
        deadline: Any = None,
    ) -> ApiResult:
        """Return the APK message-detail timeline for one C2C peer."""
        peer = str(peer_id or "").strip()
        if not peer:
            raise ValueError("peer_id is required")
        # APK v154 uses the i=888 Message_detail URL with yourid in the query
        # string and without the usual a=webapp parameter.
        url = (
            f"{APPLET}?i=888&c=entry&do=Message_detail&m=socialchat"
            f"&yourid={quote(peer, safe='')}"
        )
        call_options = {}
        if timeout is not None:
            call_options["timeout"] = timeout
        if deadline is not None:
            call_options["deadline"] = deadline
        return self.c.request(url, method="GET", **call_options)

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
