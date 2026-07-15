"""Payment adapter — create business orders; cashier is WeChat/Alipay official SDK."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from ..app import BeibeiwuApp

# From APK BuildConfig / static analysis
WECHAT_APP_ID = "wxf057dbbb960d9c39"
PACKAGE_NAME = "xin.banghua.beiyuan0"


@dataclass
class PayPrepareResult:
    """Normalized order result for a cashier or debug UI."""

    channel: str  # wechat | alipay | park_coin | unknown
    product: str  # coin | vip | svip | card | other
    ok: bool
    order_params: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    code: str = ""
    message: str = ""
    cashier_hint: str = ""
    warnings: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _as_dict(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return {"text": data}
    if data is None:
        return {}
    return {"value": data}


class PayAdapter:
    """Map protocol buy/order APIs into pay payloads. Does NOT forge payment callbacks."""

    def __init__(self, app: BeibeiwuApp):
        self.app = app

    def prepare_coin_wechat(self, coin_id: str) -> PayPrepareResult:
        r = self.app.economy.buy_coin_wechat(coin_id)
        params = _as_dict(r.data)
        warnings = [
            "WeChat merchant is bound to official app package/signature.",
            "Your own Web domain usually cannot complete APP pay with this APP_ID without merchant reconfig.",
            "Use for protocol study / invoke official App; do not fake notify URL callbacks.",
        ]
        return PayPrepareResult(
            channel="wechat",
            product="coin",
            ok=r.ok,
            order_params={
                "appId": params.get("appid") or params.get("appId") or WECHAT_APP_ID,
                "partnerId": params.get("partnerid") or params.get("partnerId"),
                "prepayId": params.get("prepayid") or params.get("prepayId"),
                "package": params.get("package") or params.get("packageValue") or "Sign=WXPay",
                "nonceStr": params.get("noncestr") or params.get("nonceStr"),
                "timeStamp": params.get("timestamp") or params.get("timeStamp"),
                "sign": params.get("sign") or params.get("paySign"),
                "raw_fields": params,
            },
            raw=r.data if r.data is not None else r.raw,
            code=r.code,
            message=r.message,
            cashier_hint="Android: WXPay entry with order_params; iOS similar; H5 needs JSAPI openid binding.",
            warnings=warnings,
        )

    def prepare_coin_alipay(self, coin_id: str) -> PayPrepareResult:
        r = self.app.economy.buy_coin_alipay(coin_id)
        params = _as_dict(r.data)
        order_str = (
            params.get("orderString")
            or params.get("orderInfo")
            or params.get("body")
            or params.get("text")
            or (r.raw if isinstance(r.raw, str) and "alipay" in r.raw[:40].lower() else "")
        )
        return PayPrepareResult(
            channel="alipay",
            product="coin",
            ok=r.ok,
            order_params={
                "orderString": order_str,
                "raw_fields": params,
            },
            raw=r.data if r.data is not None else r.raw,
            code=r.code,
            message=r.message,
            cashier_hint="Pass orderString to Alipay SDK payV2 / JSAPI; then poll coin balance via protocol.",
            warnings=[
                "Completing pay requires real Alipay account + valid merchant order.",
                "Never forge server async notify.",
            ],
        )

    def prepare_vip_wechat(self, level: str = "vip", **extra: Any) -> PayPrepareResult:
        action = (
            "Payunifiedorder2svipXBXX"
            if level.lower() == "svip"
            else "Payunifiedorder2vipXBXX"
        )
        body = {
            "userid": self.app.session.uid,
            "platform": "android",
            "PackageName": PACKAGE_NAME,
            **extra,
        }
        r = self.app.call(action, **body)
        params = _as_dict(r.data)
        return PayPrepareResult(
            channel="wechat",
            product=level.lower(),
            ok=r.ok,
            order_params={"raw_fields": params, "appId": WECHAT_APP_ID},
            raw=r.data if r.data is not None else r.raw,
            code=r.code,
            message=r.message,
            cashier_hint=f"WeChat pay for {level}; same merchant constraints as coin.",
            warnings=["Official package/sign binding applies."],
        )

    def prepare_vip_alipay(self, level: str = "vip", **extra: Any) -> PayPrepareResult:
        action = (
            "Alipayaddorder2svipXBXX"
            if level.lower() == "svip"
            else "Alipayaddorder2vipXBXX"
        )
        body = {
            "userid": self.app.session.uid,
            "platform": "android",
            "PackageName": PACKAGE_NAME,
            **extra,
        }
        r = self.app.call(action, **body)
        params = _as_dict(r.data)
        return PayPrepareResult(
            channel="alipay",
            product=level.lower(),
            ok=r.ok,
            order_params={"raw_fields": params},
            raw=r.data if r.data is not None else r.raw,
            code=r.code,
            message=r.message,
            cashier_hint=f"Alipay order for {level}.",
            warnings=["Requires real Alipay checkout."],
        )

    def buy_match_card(self, card_id: str = "1") -> PayPrepareResult:
        """Spend 乐园币 for match card — no WeChat/Alipay cashier."""
        r = self.app.call(
            "buyCard",
            uid=self.app.session.uid,
            card_id=card_id,
        )
        return PayPrepareResult(
            channel="park_coin",
            product="card",
            ok=r.ok,
            order_params={"card_id": card_id, "raw_fields": _as_dict(r.data)},
            raw=r.data if r.data is not None else r.raw,
            code=r.code,
            message=r.message,
            cashier_hint="Pure business API; needs enough 乐园币. No external pay SDK.",
            warnings=[],
        )

    def capabilities(self) -> Dict[str, Any]:
        return {
            "wechat_app_id": WECHAT_APP_ID,
            "package_name": PACKAGE_NAME,
            "protocol_orders": [
                "buyCoinWechatXBXX",
                "buyCoinAlipayXBXX",
                "Payunifiedorder2vipXBXX",
                "Payunifiedorder2svipXBXX",
                "Alipayaddorder2vipXBXX",
                "Alipayaddorder2svipXBXX",
                "buyCard",
            ],
            "limits": [
                "Order creation = protocol; fund capture = official cashier + merchant.",
                "Web wrapper should not embed payment secrets; map order_params to SDK only.",
            ],
        }
