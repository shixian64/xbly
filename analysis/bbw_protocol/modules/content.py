"""Public / content reads."""

from __future__ import annotations

from typing import Any, Optional

from ..client import ApiResult, ProtocolClient


class ContentAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def gift_list(self) -> ApiResult:
        return self.c.call("getGiftList")

    def slide(self, slidesort: str = "1") -> ApiResult:
        return self.c.call("getSlide", slidesort=slidesort)

    def recommend(self, type_: str = "getSlide") -> ApiResult:
        return self.c.call("Tuijiannew", type=type_)

    def is_show_ad(self) -> ApiResult:
        return self.c.call("isShowAD1")

    def is_show_ad2(self) -> ApiResult:
        return self.c.call("isShowAD2")

    def version(self) -> ApiResult:
        return self.c.call("getVersion1")

    def filter_words(self) -> ApiResult:
        return self.c.call("getFilterWords")

    def filter_words_group(self) -> ApiResult:
        return self.c.call("getFilterWordsGroup")

    def illegal_words(self) -> ApiResult:
        return self.c.call("getIllegalWord")

    def illegal_words_group(self) -> ApiResult:
        return self.c.call("getIllegalWordGroup")

    def chat_censorship(self) -> ApiResult:
        return self.c.call("getChatCensorship")

    def referral(self) -> ApiResult:
        return self.c.call("getReferral")

    def set_referral(self, referral: str, id_: Optional[str] = None) -> ApiResult:
        return self.c.call(
            "setReferral",
            referral=referral,
            id=id_ or self.c.session.uid,
        )

    def topic(self, topic: str = "") -> ApiResult:
        return self.c.call("getTopic", topic=topic)

    def create_topic(self, topic: str) -> ApiResult:
        return self.c.call("createTopic", topic=topic)

    def test_field(self) -> ApiResult:
        return self.c.call("testField")

    def about(self) -> ApiResult:
        return self.c.call_url(
            f"https://applet.banghua.xin/app/index.php?i=888&c=entry&do=About_app&m=socialchat&version=148"
        )

    def hot_task(self) -> ApiResult:
        return self.c.call_url(
            "https://applet.banghua.xin/app/index.php?i=888&c=entry&do=Hot_task&m=socialchat"
        )

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
