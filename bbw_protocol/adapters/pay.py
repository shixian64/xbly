"""Payment adapter — create business orders; cashier is WeChat/Alipay official SDK."""

from __future__ import annotations

import json
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
        out = dict(data)
        # Payment backends commonly wrap the actual SDK fields one level down.
        for key in ("data", "result", "json_obj", "pay", "order"):
            nested = out.get(key)
            if isinstance(nested, str) and nested.strip()[:1] == "{":
                try:
                    nested = json.loads(nested)
                except Exception:
                    nested = None
            if isinstance(nested, dict):
                out = {**out, **nested}
        if isinstance(out.get("json"), str) and out["json"].strip()[:1] == "{":
            try:
                nested = json.loads(out["json"])
                if isinstance(nested, dict):
                    out = {**out, **nested}
            except Exception:
                pass
        return out
    if isinstance(data, str):
        return {"text": data}
    if data is None:
        return {}
    return {"value": data}


def _wechat_order_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "appId": params.get("appid") or params.get("appId") or WECHAT_APP_ID,
        "partnerId": params.get("partnerid") or params.get("partnerId") or params.get("mch_id"),
        "prepayId": params.get("prepayid") or params.get("prepayId") or params.get("prepay_id"),
        "package": params.get("package") or params.get("packageValue") or "Sign=WXPay",
        "nonceStr": params.get("noncestr") or params.get("nonceStr") or params.get("nonce_str"),
        "timeStamp": params.get("timestamp") or params.get("timeStamp") or params.get("time_stamp"),
        "sign": params.get("sign") or params.get("paySign") or params.get("paysign"),
        "raw_fields": params,
    }


def _alipay_order_string(params: Dict[str, Any], raw: Any = None) -> str:
    value = (
        params.get("orderString")
        or params.get("orderInfo")
        or params.get("order_string")
        or params.get("body")
        or params.get("text")
        or ""
    )
    if not value and isinstance(raw, str):
        candidate = raw.strip()
        if candidate and candidate[:1] not in "{[":
            value = candidate
    return str(value or "")


def _validated_result(
    *,
    protocol_ok: bool,
    protocol_code: str,
    protocol_message: str,
    valid_payload: bool,
    missing_message: str,
) -> tuple[bool, str, str]:
    if not protocol_ok:
        return False, protocol_code, protocol_message
    if valid_payload:
        return True, protocol_code, protocol_message
    return False, "INVALID_ORDER_PAYLOAD", missing_message


class PayAdapter:
    """Map protocol buy/order APIs into pay payloads. Does NOT forge payment callbacks."""

    def __init__(self, app: BeibeiwuApp):
        self.app = app

    def prepare_coin_wechat(self, coin_id: str) -> PayPrepareResult:
        r = self.app.economy.buy_coin_wechat(coin_id)
        params = _as_dict(r.data)
        order_params = _wechat_order_params(params)
        ok, code, message = _validated_result(
            protocol_ok=r.ok,
            protocol_code=r.code,
            protocol_message=r.message,
            valid_payload=bool(order_params["prepayId"] and order_params["sign"]),
            missing_message="WeChat order payload missing prepayId or sign",
        )
        warnings = [
            "WeChat merchant is bound to official app package/signature.",
            "Your own Web domain usually cannot complete APP pay with this APP_ID without merchant reconfig.",
            "Use for protocol study / invoke official App; do not fake notify URL callbacks.",
        ]
        if r.ok and not ok:
            warnings.append("Backend transport succeeded but required WeChat order fields are missing.")
        return PayPrepareResult(
            channel="wechat",
            product="coin",
            ok=ok,
            order_params=order_params,
            raw=r.data if r.data is not None else r.raw,
            code=code,
            message=message,
            cashier_hint="Android: WXPay entry with order_params; iOS similar; H5 needs JSAPI openid binding.",
            warnings=warnings,
        )

    def prepare_coin_alipay(self, coin_id: str) -> PayPrepareResult:
        r = self.app.economy.buy_coin_alipay(coin_id)
        params = _as_dict(r.data)
        order_str = _alipay_order_string(params, r.raw)
        ok, code, message = _validated_result(
            protocol_ok=r.ok,
            protocol_code=r.code,
            protocol_message=r.message,
            valid_payload=bool(order_str),
            missing_message="Alipay order payload missing orderString",
        )
        warnings = [
            "Completing pay requires real Alipay account + valid merchant order.",
            "Never forge server async notify.",
        ]
        if r.ok and not ok:
            warnings.append("Backend transport succeeded but orderString is missing.")
        return PayPrepareResult(
            channel="alipay",
            product="coin",
            ok=ok,
            order_params={
                "orderString": order_str,
                "raw_fields": params,
            },
            raw=r.data if r.data is not None else r.raw,
            code=code,
            message=message,
            cashier_hint="Pass orderString to Alipay SDK payV2 / JSAPI; then poll coin balance via protocol.",
            warnings=warnings,
        )

    def prepare_vip_wechat(
        self,
        level: str = "vip",
        vipid: Optional[str] = None,
        **extra: Any,
    ) -> PayPrepareResult:
        resolved_vipid = vipid or extra.pop("vip_id", None)
        if not resolved_vipid:
            return PayPrepareResult(
                channel="wechat",
                product=level.lower(),
                ok=False,
                code="MISSING_VIPID",
                message="vipid is required for VIP/SVIP order preparation",
                warnings=["Select a server-defined VIP product before creating an order."],
            )
        action = (
            "Payunifiedorder2svipXBXX"
            if level.lower() == "svip"
            else "Payunifiedorder2vipXBXX"
        )
        body = {
            "userid": self.app.session.uid,
            "vipid": str(resolved_vipid),
            "platform": "android",
            "PackageName": PACKAGE_NAME,
            **extra,
        }
        r = self.app.call(action, **body)
        params = _as_dict(r.data)
        order_params = _wechat_order_params(params)
        ok, code, message = _validated_result(
            protocol_ok=r.ok,
            protocol_code=r.code,
            protocol_message=r.message,
            valid_payload=bool(order_params["prepayId"] and order_params["sign"]),
            missing_message="WeChat VIP order payload missing prepayId or sign",
        )
        warnings = ["Official package/sign binding applies."]
        if r.ok and not ok:
            warnings.append("Backend transport succeeded but required WeChat order fields are missing.")
        return PayPrepareResult(
            channel="wechat",
            product=level.lower(),
            ok=ok,
            order_params=order_params,
            raw=r.data if r.data is not None else r.raw,
            code=code,
            message=message,
            cashier_hint=f"WeChat pay for {level}; same merchant constraints as coin.",
            warnings=warnings,
        )

    def prepare_vip_alipay(
        self,
        level: str = "vip",
        vipid: Optional[str] = None,
        **extra: Any,
    ) -> PayPrepareResult:
        resolved_vipid = vipid or extra.pop("vip_id", None)
        if not resolved_vipid:
            return PayPrepareResult(
                channel="alipay",
                product=level.lower(),
                ok=False,
                code="MISSING_VIPID",
                message="vipid is required for VIP/SVIP order preparation",
                warnings=["Select a server-defined VIP product before creating an order."],
            )
        action = (
            "Alipayaddorder2svipXBXX"
            if level.lower() == "svip"
            else "Alipayaddorder2vipXBXX"
        )
        body = {
            "userid": self.app.session.uid,
            "vipid": str(resolved_vipid),
            "platform": "android",
            "PackageName": PACKAGE_NAME,
            **extra,
        }
        r = self.app.call(action, **body)
        params = _as_dict(r.data)
        order_str = _alipay_order_string(params, r.raw)
        ok, code, message = _validated_result(
            protocol_ok=r.ok,
            protocol_code=r.code,
            protocol_message=r.message,
            valid_payload=bool(order_str),
            missing_message="Alipay VIP order payload missing orderString",
        )
        warnings = ["Requires real Alipay checkout."]
        if r.ok and not ok:
            warnings.append("Backend transport succeeded but orderString is missing.")
        return PayPrepareResult(
            channel="alipay",
            product=level.lower(),
            ok=ok,
            order_params={"orderString": order_str, "raw_fields": params},
            raw=r.data if r.data is not None else r.raw,
            code=code,
            message=message,
            cashier_hint=f"Alipay order for {level}.",
            warnings=warnings,
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
