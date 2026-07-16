"""Match / bottle / dating / cards."""

from __future__ import annotations

from typing import Any, Optional

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
