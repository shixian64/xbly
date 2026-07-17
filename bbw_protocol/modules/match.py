"""Match / bottle / dating / cards."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..client import ApiResult, ProtocolClient


class MatchAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def remove(self, type_: str = "1", id_: Optional[str] = None) -> ApiResult:
        return self.c.call_redis(
            "removeXiaobeiMatch",
            id=id_ or self.c.session.uid,
            type=type_,
        )

    def start_voice(self, id_: Optional[str] = None) -> ApiResult:
        """Start the APK-compatible one-to-one audio match.

        The native client posts the literal Chinese value ``语音`` to the
        Redis-backed ``xiaobeiMatchNew`` action.  A successful response is
        either ``wait`` or one matched user's JSON object.
        """
        return self.c.call_redis(
            "xiaobeiMatchNew",
            id=id_ or self.c.session.uid,
            type="语音",
        )

    def cancel_voice(self, id_: Optional[str] = None) -> ApiResult:
        """Best-effort removal from the native voice matching queue."""
        return self.remove("语音", id_=id_)

    @staticmethod
    def normalize_voice_result(result: ApiResult) -> Dict[str, Any]:
        """Classify the three APK response branches without leaking raw data."""
        value: Any = getattr(result, "data", None)
        if isinstance(value, str):
            text = value.strip()
            lowered = text.lower()
            if lowered == "wait":
                return {"outcome": "waiting", "target": None}
            if lowered in {"false", "no", "null", "none", ""}:
                return {"outcome": "insufficient", "target": None}
            if text[:1] in "[{":
                try:
                    value = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return {"outcome": "error", "target": None}

        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, dict)), None)
        if isinstance(value, dict):
            nested = value.get("data")
            if isinstance(nested, dict) and not any(
                key in value for key in ("id", "uid", "userId", "userID")
            ):
                value = nested
            target_id = str(
                value.get("id")
                or value.get("uid")
                or value.get("userId")
                or value.get("userID")
                or ""
            ).strip()
            if target_id:
                return {"outcome": "matched", "target": value}

        raw = str(getattr(result, "raw", "") or "").strip().lower()
        if raw == "wait":
            return {"outcome": "waiting", "target": None}
        if raw in {"false", "no", "null", "none", ""}:
            return {"outcome": "insufficient", "target": None}
        return {"outcome": "error", "target": None}

    def online_users(self, **params: Any) -> ApiResult:
        return self.c.call("getOnlineMatchUser", params)

    def set_filter(self, value: str) -> ApiResult:
        """Persist one APK-compatible match preference value.

        The native ``MatchDialog`` submits gender and property separately to
        ``resetMatch`` using the same ``value`` field.
        """
        return self.c.call("resetMatch", id=self.c.session.uid, value=value)

    def online_one(self, **params: Any) -> ApiResult:
        return self.c.call("getOnlineMatchUserOneNewNew", params)

    def local_one(self, **params: Any) -> ApiResult:
        return self.c.call("getLocalMatchUserOneNewNew", params)

    def throw_bottle(self, **params: Any) -> ApiResult:
        """Return an existing picked bottle to the pool."""
        return self.c.call("ThrowADriftBottle", params)

    def pick_bottle(self, **params: Any) -> ApiResult:
        return self.c.call("PickADraftBottle", params)

    def my_bottles(self, **params: Any) -> ApiResult:
        return self.c.call("GetMyDraftBottle", params)

    def delete_bottle(self, **params: Any) -> ApiResult:
        return self.c.call("DeleteADriftBottle", params)

    def bottle_leave_word(self, **params: Any) -> ApiResult:
        """Create a bottle's first word, or append a reply when ``id`` is supplied."""
        return self.c.call("AddDraftBottleLeaveWord", params)

    def publish_dating(self, **params: Any) -> ApiResult:
        return self.c.call("PublishDating", params)

    def apply_dating(self, **params: Any) -> ApiResult:
        return self.c.call("ApplyDating", params)

    def cancel_dating(self, **params: Any) -> ApiResult:
        return self.c.call("CancelDating", params)

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
