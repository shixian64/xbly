"""Social graph, posts, comments, friends."""

from __future__ import annotations

from typing import Any, Optional

from ..client import ApiResult, ProtocolClient


class SocialAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    # ---- follow ----
    def follow(self, you: str, quietly: str = "1") -> ApiResult:
        return self.c.call(
            "follow",
            me=self.c.session.uid,
            you=you,
            quietly_follow=quietly,
        )

    def unfollow(self, you: str) -> ApiResult:
        return self.c.call("unfollow", me=self.c.session.uid, you=you)

    def follow_quietly(self, you: str) -> ApiResult:
        return self.c.call("followQuietly", me=self.c.session.uid, you=you)

    def follow_list(self, id_: Optional[str] = None) -> ApiResult:
        return self.c.call("getFollowList", id=id_ or self.c.session.uid)

    def follow_users(self, id_: Optional[str] = None, page: str = "1") -> ApiResult:
        return self.c.call(
            "getFollowUser", id=id_ or self.c.session.uid, pageIndex=page
        )

    def fans_users(self, id_: Optional[str] = None, page: str = "1") -> ApiResult:
        return self.c.call(
            "getFansUser", id=id_ or self.c.session.uid, pageIndex=page
        )

    # ---- friends ----
    def friend_apply_list(self, page: str = "1") -> ApiResult:
        return self.c.call(
            "Friendsapply0", uid=self.c.session.uid, pageIndex=page
        )

    def agree_friend(self, id_: str) -> ApiResult:
        return self.c.call("agreefriend0", id=id_)

    def delete_friend(self, **params: Any) -> ApiResult:
        return self.c.call("Deletefriend0", params)

    # ---- blacklist ----
    def my_blacklist(self) -> ApiResult:
        return self.c.call("getMyBlackList")

    def blacklist_me(self) -> ApiResult:
        return self.c.call("getBlackListMe")

    def add_blacklist(self, **params: Any) -> ApiResult:
        return self.c.call("addblacklist", params)

    def delete_blacklist(self, **params: Any) -> ApiResult:
        return self.c.call("deleteblacklist", params)

    # ---- like / post related common ----
    def luntan_like(self, postid: str) -> ApiResult:
        return self.c.call("luntanlike", postid=postid)

    def guangchang_like(self, id_: str) -> ApiResult:
        return self.c.call("guangchanglike", **{"id": id_} if False else {})  # placeholder

    def report(
        self, type_: str, itemid: str, reason: str, myid: Optional[str] = None
    ) -> ApiResult:
        return self.c.request(
            self.c.url("ReportViolation", i="888"),
            {
                "type": type_,
                "myid": myid or self.c.session.uid,
                "itemid": itemid,
                "reason": reason,
            },
        )

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)
