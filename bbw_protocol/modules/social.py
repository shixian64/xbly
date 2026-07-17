"""Social graph, posts, comments, friends."""

from __future__ import annotations

from typing import Any, Optional

from ..client import ApiResult, ProtocolClient


DYNAMIC_TAB_PLATENAMES = {
    "推荐": "精华",
    "附近": "同城",
    "最新": "首页",
    "招募令": "招募令",
    "关注": "关注",
}


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
    def posts(
        self,
        tab: str = "推荐",
        cursor: str = "1",
        *,
        filter_gender: str = "不限",
        filter_search: str = "",
        filter_property: str = "不限",
        filter_region: str = "不限",
    ) -> ApiResult:
        """Read the v154 Dynamic feed used by the active APK navigation graph.

        ``Main4Branch.LuntanFragment`` sends ``精华`` for 推荐, ``同城`` for
        附近 and ``首页`` for 最新 to ``luntannewnewnew``. Pagination starts
        at ``1`` and subsequent requests use the last post id as ``pageindex``.
        """
        platename = DYNAMIC_TAB_PLATENAMES.get(str(tab or "推荐"), str(tab or "推荐"))
        return self.c.request(
            # dongtai_graph starts at LuntanFragment. Luntan0 belongs to the
            # retained Main4Activity/LuntanNewFragment flow and is not this UI.
            self.c.url("luntannewnewnew", i="999999"),
            {
                "type": "getPostlist",
                "myid": self.c.session.uid,
                "platename": platename,
                "pageindex": cursor,
                "filter_gender": filter_gender,
                "filter_search": filter_search,
                "filter_property": filter_property,
                "filter_region": filter_region,
            },
        )

    def user_posts(self, authid: Optional[str] = None, page: str = "1") -> ApiResult:
        """Read posts from the v154 ``我的动态`` flow.

        This retained self-management entry uses tenant ``99999``. ``page`` is
        a one-based decimal page number (``1``, ``2``, ...).
        """
        return self.c.request(
            self.c.url("someonesluntannew", i="99999"),
            {
                "myid": self.c.session.uid,
                "authid": authid or self.c.session.uid,
                "pageindex": str(page or "1"),
            },
        )

    def profile_posts(self, authid: str, page: str = "1") -> ApiResult:
        """Read another user's posts from the active v154 profile page."""
        return self.c.request(
            self.c.url("someonesluntannew", i="999999"),
            {
                "myid": self.c.session.uid,
                "authid": authid,
                "pageindex": str(page or "1"),
            },
        )

    def luntan_like(self, postid: str) -> ApiResult:
        return self.c.call("luntanlike", postid=postid)

    def main_comments(
        self,
        postid: str,
        authid: str,
        *,
        hide_comment: str = "0",
        page: str = "1",
    ) -> ApiResult:
        return self.c.call(
            "getMainComment",
            myid=self.c.session.uid,
            authid=authid,
            hide_comment=hide_comment,
            postID=postid,
            pageIndex=page,
        )

    def send_comment(
        self,
        text: str,
        postid: str,
        post_owner: str,
        *,
        main_id: str = "0",
        main_owner: str = "0",
        sub_id: str = "0",
        sub_comment: str = "0",
        author_reply: str = "0",
    ) -> ApiResult:
        return self.c.call(
            "sendComment",
            myID=self.c.session.uid,
            comment_text=text,
            postID=postid,
            postid_user=post_owner,
            mainID=main_id,
            mainID_user=main_owner,
            subID=sub_id,
            subID_comment=sub_comment,
            ifauthreply=author_reply,
        )

    def comment_like(self, comment_id: str, liked: str = "0") -> ApiResult:
        return self.c.call(
            "sendCommentLike",
            myID=self.c.session.uid,
            commentID=comment_id,
            ifauthlike=liked,
        )

    def delete_comment(self, comment_id: str) -> ApiResult:
        return self.c.call("deleteComment", comment_id=comment_id)

    def forbid_comment(self, comment_id: str) -> ApiResult:
        return self.c.call("forbidComment", comment_id=comment_id)

    def delete_post(self, postid: str) -> ApiResult:
        return self.c.call("deletepost", postid=postid)

    def change_post_visibility(self, postid: str, scope: str) -> ApiResult:
        return self.c.call(
            "changeVisibilityScope",
            userid=self.c.session.uid,
            postid=postid,
            visibility_scope=scope,
        )

    def toggle_profile_pin(self, postid: str) -> ApiResult:
        return self.c.call("SetPostUTop", id=postid)

    def publish_post(
        self,
        text: str,
        *,
        title: str = "",
        plate: str = "动态",
        visibility_scope: str = "公开",
        comment_forbid: str = "0",
        hide_comment: str = "0",
        topics: str = "",
    ) -> ApiResult:
        params = {
            "authid": self.c.session.uid,
            "visibility_scope": visibility_scope,
            "hide_comment": hide_comment,
            "allow_download": "1",
            "allow_anonymity": "1",
            "posttitle": title,
            "posttext": text,
            "platename": plate,
            "comment_forbid": comment_forbid,
            "post_bg": "",
        }
        if topics:
            params["topicLists"] = topics
        return self.c.call("fabutiezi1", params)

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
