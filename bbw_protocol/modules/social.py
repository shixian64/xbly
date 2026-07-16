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

    def friends(self, type_: str = "好友") -> ApiResult:
        """Return the current user's accepted friend/contact list.

        APK v154 uses ``getAddFriend`` for the address-book style friend tab.  The
        older ``friendsnewnew`` action still exists in the APK, but it is routed
        through a different ``i=99999`` tenant and is no longer the primary UI
        path.
        """
        return self.c.call(
            "getAddFriend",
            uid=self.c.session.uid,
            type=type_,
        )

    def legacy_friends(self) -> ApiResult:
        """Call the legacy v154 friend-list endpoint (``i=99999``)."""
        return self.c.request(
            self.c.url("friendsnewnew", i="99999"),
            {"myid": self.c.session.uid},
        )

    def agree_friend(self, id_: str) -> ApiResult:
        return self.c.call("agreefriend0", id=id_)

    def delete_friend(self, **params: Any) -> ApiResult:
        return self.c.call("Deletefriend0", params)

    # ---- profile visits ----
    def visits(self, type_: str, page: str = "0") -> ApiResult:
        """Read either "谁看过我" or "我看过谁" from the shared APK action."""
        if type_ not in ("谁看过我", "我看过谁"):
            raise ValueError("type_ must be 谁看过我 or 我看过谁")
        return self.c.call(
            "ISawAndSawMe",
            pageindex=page,
            type=type_,
        )

    def viewed_me(self, page: str = "0") -> ApiResult:
        return self.visits("谁看过我", page)

    def i_viewed(self, page: str = "0") -> ApiResult:
        return self.visits("我看过谁", page)

    def record_profile_view(self, yourid: str) -> ApiResult:
        """Record that the current user opened another user's profile.

        The active xbly v154 path posts ``myid=current`` and
        ``yourid=profile target``.  Older APK-derived implementations also sent
        visitor display fields, so retain those optional fields for compatibility
        without reversing the v154 ids.
        """
        params = {
            "myid": self.c.session.uid,
            "yourid": yourid,
        }
        nickname = getattr(self.c.session, "nickname", "") or ""
        portrait = getattr(self.c.session, "portrait", "") or ""
        if nickname:
            params["yournickname"] = nickname
        if portrait:
            params["yourportrait"] = portrait
        return self.c.call("addsawme", params)

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
        return self.c.call("guangchanglike", id=id_)

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
