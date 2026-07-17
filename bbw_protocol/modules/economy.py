"""Read-only gift inventory and withdrawal operations."""

from __future__ import annotations

from typing import Optional

from ..client import ApiResult, ProtocolClient


class EconomyAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def my_gifts(self) -> ApiResult:
        return self.c.call("getMyGiftLists")

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
