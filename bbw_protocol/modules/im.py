"""IM helpers (HTTP side). Full realtime still needs SDK."""

from __future__ import annotations

from typing import Any, Optional

from .. import sign
from ..client import RONG_REGISTER, TXIM_SIGN, ApiResult, ProtocolClient


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
            userId=user_id or self.c.session.uid,
            userNickName=nickname or self.c.session.nickname or "user",
            userPortrait=portrait or self.c.session.portrait or "",
        )

    def flash_photo_get(self, **params: Any) -> ApiResult:
        return self.c.call("GetflashphotoTencent", params)

    def flash_photo_send(self, **params: Any) -> ApiResult:
        return self.c.call("SendTencentFlashPhoto", params)

    def stickers(self, **params: Any) -> ApiResult:
        return self.c.call("GetAllStickersWithFavorite", params)

    def sticker_search(self, key: str) -> ApiResult:
        return self.c.call("getStickersByKeyWord", key_word=key)

    def history_message_insert(self, **params: Any) -> ApiResult:
        return self.c.call("insertTencentHistoryMessage", params)

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
