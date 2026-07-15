"""Coins / VIP / gifts / withdraw / pay."""

from __future__ import annotations

from typing import Any, Optional

from .. import sign
from ..client import AGORA_RTC, AGORA_RTM, ALIPAY_ORDER, ApiResult, ProtocolClient


class EconomyAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def gift_list(self) -> ApiResult:
        return self.c.call("getGiftList")

    def my_gifts(self) -> ApiResult:
        return self.c.call("getMyGiftLists")

    def send_gift1(self, **params: Any) -> ApiResult:
        return self.c.call("sendGift1", params)

    def send_gift2(self, **params: Any) -> ApiResult:
        return self.c.call("sendGift2", params)

    def svip_try(self, id_: Optional[str] = None) -> ApiResult:
        return self.c.call("SvipTry", id=id_ or self.c.session.uid)

    def send_vip(self, uid2: str, vip_id: int = 5) -> ApiResult:
        return self.c.call(
            "sendVip",
            uid1=self.c.session.uid,
            uid2=uid2,
            vip_id=str(vip_id),
            token=sign.unique_login_token(self.c.session.uid),
        )

    def money_exchange_vip(self, vip_id: int = 5, coupon_id: str = "0") -> ApiResult:
        return self.c.call(
            "moneyExchangeVip",
            uid=self.c.session.uid,
            vip_id=str(vip_id),
            coupon_id=coupon_id,
            token=sign.unique_login_token(self.c.session.uid),
        )

    def refund_svip_vip(self, **params: Any) -> ApiResult:
        return self.c.call("RefundSvipAndVip", params)

    def buy_coin_wechat(self, coin_id: str) -> ApiResult:
        return self.c.call(
            "buyCoinWechatXBXX",
            userid=self.c.session.uid,
            coinId=coin_id,
            platform="android",
            PackageName=sign.PACKAGE_NAME,
        )

    def buy_coin_alipay(self, coin_id: str) -> ApiResult:
        return self.c.call(
            "buyCoinAlipayXBXX",
            userid=self.c.session.uid,
            coinId=coin_id,
            platform="android",
            PackageName=sign.PACKAGE_NAME,
        )

    def withdraw(
        self, alilogonid: str, aliname: str, amount: str, authid: Optional[str] = None
    ) -> ApiResult:
        return self.c.call(
            "withdraw",
            authid=authid or self.c.session.uid,
            alilogonid=alilogonid,
            aliname=aliname,
            transamount=amount,
        )

    def pay_chat_call(self, target_id: str) -> ApiResult:
        return self.c.call(
            "payChatCall",
            uid=self.c.session.uid,
            target_id=target_id,
        )

    def vip_level_order(self, **params: Any) -> ApiResult:
        return self.c.call("vipLevelOrder", params)

    def alipay_order_php(self, **params: Any) -> ApiResult:
        return self.c.call_url(ALIPAY_ORDER, params)

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
