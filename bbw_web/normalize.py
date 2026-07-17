"""Normalize banghua API payloads into stable DTOs for the Web UI.

Product pages should render DTOs only — never depend on raw nested JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# APK serves relative paths like /images/999999/... from this host.
MEDIA_BASE = "https://oss.banghua.xin"
# ``CommonUtil.getOssResourceUrl`` in the APK migrates these retired buckets
# to the canonical CDN before any image or video is loaded.  Some older posts
# still return the retired absolute origins, which now answer with 403/404.
APK_MEDIA_ORIGIN_RE = re.compile(
    r"^(?:https?:)?//(?:"
    r"oss\.banghua\.xin|"
    r"moyuanoss\.oss-cn-shanghai\.aliyuncs\.com|"
    r"appletattachment\.oss-cn-beijing\.aliyuncs\.com"
    r")(?=[/?#]|$)",
    re.I,
)
EMPTY_MEDIA_VALUES = {
    "0",
    "false",
    "nil",
    "none",
    "null",
    "undefined",
    "[]",
    "{}",
    "[object object]",
}


def resolve_media_url(value: Any) -> str:
    """Turn relative APK media paths into absolute OSS URLs."""
    raw = str(value or "").strip()
    if not raw or raw.lower() in EMPTY_MEDIA_VALUES:
        return ""
    if raw.lower().startswith("data:"):
        return raw if raw.lower().startswith("data:image/") else ""
    canonical = APK_MEDIA_ORIGIN_RE.sub(MEDIA_BASE, raw, count=1)
    if canonical != raw:
        return canonical
    if raw.startswith("//"):
        return "https:" + raw
    if re.match(r"^https?://", raw, re.I):
        return raw
    if raw.startswith("/"):
        return MEDIA_BASE + raw
    if re.match(r"^(images|attachment|upload|uploads)/", raw, re.I):
        return f"{MEDIA_BASE}/{raw}"
    if "://" not in raw and not raw.startswith("{"):
        return f"{MEDIA_BASE}/{raw.lstrip('./')}"
    return ""


# ---------------------------------------------------------------------------
# Error catalogue (product copy)
# ---------------------------------------------------------------------------

ERROR_MAP: List[Tuple[Any, ...]] = [
    # (matchers on code/message/raw, title, detail, action)
    (("700", "登录失效", "登陆失效", "登录过期"), "登录已失效", "请重新登录后再试", "relogin"),
    (("340", "未实名", "实名认证", "还未实名"), "需要实名认证", "该功能需先完成实名，请在官方客户端内刷脸认证", "realname"),
    (("430", "卡不足", "匹配卡", "次数不足"), "次数或道具不足", "匹配卡/免费次数不够，可做任务或乐园币买卡", "buy_card"),
    (("403", "不可修改", "不可提现"), "暂无权限", "服务端拒绝了操作（常见：未实名或业务限制）", "none"),
    (("余额不足", "乐园币不足", "money"), "余额不足", "乐园币或余额不够，请先充值（网页版仅能创建订单参数）", "wallet"),
    (("400", "参数", "失败"), "请求未成功", "参数不完整或业务失败", "none"),
    (("false", "no", "NULL"), "操作未成功", "服务端返回失败", "none"),
]


def explain_error(
    code: str = "",
    message: str = "",
    raw: str = "",
    extra: str = "",
) -> Dict[str, Any]:
    if str(code or "").strip().upper() == "EMPTY_RESPONSE":
        return {
            "title": "结果待确认",
            "detail": "服务端未返回明确的业务结果，请刷新相关页面确认最终状态。",
            "action": "none",
            "code": "EMPTY_RESPONSE",
            "message": str(message or "服务端返回空响应")[:200],
        }
    blob = f"{code} {message} {extra} {raw}".lower()
    for entry in ERROR_MAP:
        keys = entry[0]
        title, detail, action = entry[1], entry[2], entry[3]
        for k in keys:
            if str(k).lower() in blob:
                return {
                    "title": title,
                    "detail": detail,
                    "action": action,
                    "code": str(code or ""),
                    "message": str(message or extra or raw or title)[:200],
                }
    msg = (message or extra or raw or "未知错误").strip()
    return {
        "title": msg[:40] if msg else "操作失败",
        "detail": msg[:200] if msg else "请稍后重试",
        "action": "none",
        "code": str(code or ""),
        "message": msg[:200],
    }


def envelope(
    r: Any,
    *,
    items: Optional[List[Dict[str, Any]]] = None,
    extra: Optional[Dict[str, Any]] = None,
    include_raw: bool = False,
) -> Dict[str, Any]:
    """Standard API envelope for product pages."""
    ok = bool(getattr(r, "ok", False))
    code = str(getattr(r, "code", "") or "")
    message = str(getattr(r, "message", "") or "")
    extra_s = str(getattr(r, "extra", "") or "")
    raw = str(getattr(r, "raw", "") or "")
    err = None if ok else explain_error(code, message, raw, extra_s)
    # business fail with http 200 + empty body patterns
    if ok and message and any(x in message for x in ("不足", "未实名", "不可", "失败")):
        if code in ("400", "403", "340", "430", "700") or "未实名" in message:
            ok = False
            err = explain_error(code, message, raw, extra_s)
    out: Dict[str, Any] = {
        "ok": ok,
        "code": code,
        "message": message or (err["message"] if err else ""),
        "error": err,
        "items": items if items is not None else [],
        "count": len(items) if items is not None else 0,
    }
    if extra:
        out.update(extra)
    if include_raw:
        out["raw_preview"] = raw[:800]
        out["data"] = getattr(r, "data", None)
    return out


# ---------------------------------------------------------------------------
# Deep extract helpers
# ---------------------------------------------------------------------------

def _as_dict(x: Any) -> Optional[Dict[str, Any]]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x.strip()[:1] in "{[":
        try:
            p = json.loads(x)
            return p if isinstance(p, dict) else None
        except Exception:
            return None
    return None


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str) and x.strip()[:1] == "[":
        try:
            p = json.loads(x)
            return p if isinstance(p, list) else []
        except Exception:
            return []
    d = _as_dict(x)
    if not d:
        return []
    for k in (
        "json_obj",
        "json",
        "list",
        "data",
        "info",
        "users",
        "userlist",
        "userInfoList",
        "user_info_list",
        "followList",
        "follow_list",
        "fansList",
        "fans_list",
        "followUsers",
        "fansUsers",
        "friendList",
        "friendsList",
        "friend_list",
        "friends_list",
        "applyList",
        "apply_list",
        "conversations",
        "conversation_list",
        "conversationList",
        "messages",
        "message_list",
        "messageList",
        "items",
        "result",
        "rows",
        "records",
        "giftlist",
        "gift_list",
        "matchlist",
        "slides",
        "slide_list",
        "banners",
        "topics",
        "topic_list",
        "posts",
        "postlist",
        "post_list",
        "luntan",
        "luntan_list",
        "comments",
        "commentlist",
        "comment_list",
        "rooms",
        "roomlist",
        "room_list",
        "songs",
        "songlist",
        "song_list",
        "music",
        "musiclist",
        "music_list",
        "bottles",
        "bottlelist",
        "bottle_list",
        "stickers",
        "stickerlist",
        "sticker_list",
    ):
        v = d.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            inner = _as_list(v)
            if inner:
                return inner
        if isinstance(v, dict):
            # sometimes { "0": {...}, "1": {...} }
            vals = list(v.values())
            if vals and all(isinstance(i, dict) for i in vals):
                return vals
            nested = _as_list(v)
            if nested:
                return nested
    # dict of dicts
    vals = list(d.values())
    if vals and all(isinstance(i, dict) for i in vals[:5]):
        return [i for i in vals if isinstance(i, dict)]
    return []


def extract_list(data: Any) -> List[Any]:
    return _as_list(data)


def _first(d: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return default


def _num(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(str(v).strip()))
    except Exception:
        return default


def _bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "t", "已收藏", "是"):
        return True
    if s in ("0", "false", "no", "n", "off", "f", "未收藏", "否", ""):
        return False
    return default


def normalize_value(data: Any) -> Dict[str, Any]:
    """Preserve a scalar/object response as a stable generic value DTO.

    This is for endpoints such as referral/version/auth/token responses where
    forcing the payload through a list/user normalizer would lose the actual
    business value.
    """
    value = data
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "["):
            try:
                value = json.loads(s)
            except Exception:
                value = data

    if value is None:
        value_type = "null"
        text = ""
        empty = True
    elif isinstance(value, bool):
        value_type = "boolean"
        text = "true" if value else "false"
        empty = False
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        value_type = "number"
        text = str(value)
        empty = False
    elif isinstance(value, list):
        value_type = "array"
        text = json.dumps(value, ensure_ascii=False)
        empty = len(value) == 0
    elif isinstance(value, dict):
        value_type = "object"
        text = json.dumps(value, ensure_ascii=False)
        empty = len(value) == 0
    else:
        value_type = "string"
        text = str(value)
        empty = text == ""

    return {
        "value": value,
        "value_type": value_type,
        "text": text,
        "empty": empty,
    }


# Explicit alias for callers that prefer the longer name.
normalize_generic_value = normalize_value


# ---------------------------------------------------------------------------
# Entity normalizers
# ---------------------------------------------------------------------------

def normalize_user(item: Any) -> Optional[Dict[str, Any]]:
    if item is None:
        return None
    if isinstance(item, str):
        d = _as_dict(item)
        if not d:
            return {
                "id": "",
                "nickname": item[:32],
                "avatar": "",
                "subtitle": "",
                "raw": item,
            }
        item = d
    if not isinstance(item, dict):
        return None
    # unwrap one level
    for k in ("user", "userinfo", "userInfo", "userInfoList", "json_obj"):
        if isinstance(item.get(k), dict):
            item = {**item, **item[k]}
        elif isinstance(item.get(k), str):
            inner = _as_dict(item[k])
            if inner:
                item = {**item, **inner}

    uid = str(
        _first(
            item,
            ["uid", "userId", "user_id", "userid", "myid", "ID", "Uuid", "id"],
            "",
        )
    )
    nick = str(
        _first(
            item,
            ["nickname", "nick", "name", "username", "user_name", "userNickName"],
            uid or "用户",
        )
    )
    avatar = resolve_media_url(
        _first(
            item,
            [
                "portrait",
                "avatar",
                "head",
                "headimg",
                "head_img",
                "icon",
                "photo",
                "userPortrait",
            ],
            "",
        )
    )
    role = str(_first(item, ["user_role", "role", "identity"], ""))
    city = str(_first(item, ["city", "region", "real_region", "area", "address"], ""))
    sign = str(_first(item, ["signature", "sign", "desc", "description"], ""))
    dist = str(_first(item, ["distance", "dist", "juli", "location"], ""))
    visit_time = str(_first(item, ["visit_time", "visited_at", "time"], ""))
    letter = str(_first(item, ["letters", "letter", "initial", "first_letter"], ""))
    apply_id = str(
        _first(
            item,
            ["apply_id", "applyId", "friend_apply_id", "friendsapply_id"],
            "",
        )
    )
    relation_id = str(
        _first(item, ["relation_id", "relationId", "friend_relation_id", "subid"], "")
    )
    sub_parts = [p for p in (uid and f"UID {uid}", role, city, dist, sign[:24]) if p]
    return {
        "id": uid,
        "nickname": nick,
        "avatar": avatar,
        "subtitle": " · ".join(sub_parts) if sub_parts else "",
        "role": role,
        "city": city,
        "signature": sign,
        "distance": dist,
        "online": str(
            _first(
                item,
                ["online", "online_status", "onlineStatus", "user_status", "userStatus"],
                "",
            )
        ),
        "hide_online": str(
            _first(item, ["hide_online", "hideOnline", "is_hide_online"], "0")
        ),
        "visit_time": visit_time,
        "custom_time": str(_first(item, ["custom_time"], "")),
        "friend_remark": str(_first(item, ["friendsremark", "friend_remark", "remark"], "")),
        "friend_tag": str(_first(item, ["friendstag", "friend_tag"], "")),
        "letters": letter,
        "letter": letter,
        "apply_id": apply_id,
        "relation_id": relation_id,
        "leave_words": str(_first(item, ["yourleavewords", "leave_words", "leave_word"], "")),
        "is_friend": _bool(_first(item, ["isFriend", "is_friend"], False)),
        "is_friend_apply": _bool(_first(item, ["isFriendApply", "is_friend_apply"], False)),
        "is_follower": _bool(
            _first(
                item,
                ["isFollower", "is_follower", "isFollow", "is_follow", "followed"],
                False,
            )
        ),
        "is_fans": _bool(_first(item, ["isFans", "is_fans", "isFan", "is_fan"], False)),
        "vip": str(_first(item, ["vip", "vip_time"], "0")),
        "svip": str(_first(item, ["svip", "svip_time"], "0")),
        "sex": str(_first(item, ["sex", "gender", "xingbie"], "")),
        "property": str(_first(item, ["property"], "")),
        "age": str(_first(item, ["age"], "")),
    }


def normalize_users(data: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    raw_list = extract_list(data)
    # single user object
    if not raw_list and isinstance(data, dict):
        u = normalize_user(data)
        if u and (u["id"] or u["nickname"] != "用户"):
            return [u]
        # maybe nested user
        for k in ("user", "info", "data", "json_obj"):
            if k in data:
                u = normalize_user(data[k])
                if u and u.get("id"):
                    return [u]
    for it in raw_list:
        u = normalize_user(it)
        if u:
            out.append(u)
    return out


def normalize_social_user(item: Any, current_uid: str = "") -> Optional[Dict[str, Any]]:
    """Normalize follow/fans relationship rows into the other user's profile."""
    d = _as_dict(item) if isinstance(item, str) else item
    if not isinstance(d, dict):
        return None
    current = str(current_uid or "")
    peer_id = ""
    for key in (
        "yourid",
        "your_id",
        "you",
        "friendid",
        "friendId",
        "fansid",
        "fanid",
        "followid",
        "follow_uid",
        "fans_uid",
        "target_id",
        "target_uid",
        "fromUserId",
        "from_user_id",
        "toUserId",
        "to_user_id",
    ):
        value = str(d.get(key) or "").strip()
        if value and value != current:
            peer_id = value
            break

    candidates: List[Dict[str, Any]] = []
    for key in (
        "userInfoList",
        "user_info",
        "user",
        "friend",
        "followUser",
        "follow_user",
        "fansUser",
        "fans_user",
        "targetUser",
        "target_user",
        "fromUser",
        "from_user",
        "toUser",
        "to_user",
    ):
        candidate = normalize_user(d.get(key))
        if candidate and candidate.get("id"):
            candidates.append(candidate)

    generic = normalize_user(d)
    if generic and generic.get("id"):
        candidates.append(generic)
    if not peer_id:
        peer_id = str(
            next(
                (
                    candidate.get("id")
                    for candidate in candidates
                    if str(candidate.get("id") or "") != current
                ),
                "",
            )
        )
    if not peer_id:
        row_id = str(d.get("id") or "").strip()
        row_uid = str(d.get("uid") or "").strip()
        if row_id and row_id != current and row_uid == current:
            peer_id = row_id
    if not peer_id:
        return None

    profile = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("id") or "") == peer_id
        ),
        {},
    )
    nickname = str(
        _first(
            d,
            [
                "yournickname",
                "yourNickname",
                "friendnickname",
                "friendNickname",
                "fansnickname",
                "fan_nickname",
                "follownickname",
                "follow_nickname",
                "target_nickname",
                "fromUserNickName",
                "from_nickname",
                "toUserNickName",
                "to_nickname",
            ],
            profile.get("nickname") or "",
        )
    ).strip()
    avatar = resolve_media_url(
        _first(
            d,
            [
                "yourportrait",
                "yourPortrait",
                "friendportrait",
                "friendPortrait",
                "fansportrait",
                "fan_avatar",
                "followportrait",
                "follow_avatar",
                "target_avatar",
                "fromUserAvatar",
                "from_portrait",
                "toUserAvatar",
                "to_portrait",
            ],
            profile.get("avatar") or "",
        )
    )
    base = dict(profile or {})
    base.update(
        {
            "id": peer_id,
            "nickname": nickname or f"用户 {peer_id}",
            "avatar": avatar,
            "_needs_profile": not bool(nickname),
            "subtitle": " · ".join(
                part
                for part in (
                    f"uid {peer_id}",
                    str(_first(d, ["region", "city", "area"], profile.get("city") or "")),
                    str(
                        _first(
                            d,
                            ["signature", "sign", "description"],
                            profile.get("signature") or "",
                        )
                    )[:24],
                )
                if part
            ),
        }
    )
    return base


def normalize_social_users(data: Any, current_uid: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in extract_list(data):
        user = normalize_social_user(item, current_uid)
        uid = str((user or {}).get("id") or "")
        if user and uid and uid not in seen:
            seen.add(uid)
            out.append(user)
    return out


def normalize_friend_application(item: Any, current_uid: str = "") -> Optional[Dict[str, Any]]:
    """Normalize Friendsapply0 without mistaking the recipient for the applicant."""
    d = _as_dict(item) if isinstance(item, str) else item
    if not isinstance(d, dict):
        return None
    current = str(current_uid or "")
    apply_id = str(
        _first(
            d,
            ["apply_id", "applyId", "friend_apply_id", "friendsapply_id", "subid", "id"],
            "",
        )
    )
    applicant_id = ""
    for key in (
        "friendid",
        "friendId",
        "friendsid",
        "yourid",
        "your_id",
        "youid",
        "fromUserId",
        "from_user_id",
        "fromid",
        "from_id",
        "applicant_id",
        "apply_uid",
        "sender_id",
        "userId",
        "userid",
        "uid",
    ):
        value = str(d.get(key) or "").strip()
        if value and value != current:
            applicant_id = value
            break

    nested = _first(
        d,
        ["friend", "applicant", "fromUser", "from_user", "applyUser", "userInfoList", "user"],
        None,
    )
    nested_user = normalize_user(nested)
    nested_id = str((nested_user or {}).get("id") or "")
    if not applicant_id and nested_id and nested_id != current:
        applicant_id = nested_id

    generic = normalize_user(d) or {}
    generic_id = str(generic.get("id") or "")
    if not applicant_id and generic_id and generic_id != current and generic_id != apply_id:
        applicant_id = generic_id

    nickname = str(
        _first(
            d,
            [
                "friendnickname",
                "friendNickname",
                "yournickname",
                "fromnickname",
                "from_nickname",
                "applicant_nickname",
                "sender_name",
            ],
            "",
        )
    )
    avatar = resolve_media_url(
        _first(
            d,
            [
                "friendportrait",
                "friendPortrait",
                "yourportrait",
                "fromportrait",
                "from_portrait",
                "applicant_avatar",
                "sender_avatar",
            ],
            "",
        )
    )
    if nested_user and nested_id == applicant_id:
        nickname = nickname or str(nested_user.get("nickname") or "")
        avatar = avatar or str(nested_user.get("avatar") or "")
    if generic_id == applicant_id:
        nickname = nickname or str(generic.get("nickname") or "")
        avatar = avatar or str(generic.get("avatar") or "")

    if not applicant_id:
        return None
    profile_online = ""
    if nested_id == applicant_id:
        profile_online = str((nested_user or {}).get("online") or "")
    elif generic_id == applicant_id:
        profile_online = str((generic or {}).get("online") or "")
    peer_online = str(
        _first(
            d,
            [
                "friendonline",
                "friend_online",
                "friendOnline",
                "youronline",
                "your_online",
                "yourOnline",
            ],
            profile_online,
        )
    )
    peer_hide_online = str(
        _first(
            d,
            ["friend_hide_online", "friendHideOnline", "your_hide_online"],
            (nested_user or {}).get("hide_online")
            if nested_id == applicant_id
            else (generic or {}).get("hide_online")
            if generic_id == applicant_id
            else "0",
        )
    )
    return {
        **generic,
        "id": applicant_id,
        "nickname": nickname or f"用户 {applicant_id}",
        "avatar": avatar,
        "online": peer_online,
        "hide_online": peer_hide_online,
        "subtitle": " · ".join(
            part
            for part in (
                f"uid {applicant_id}",
                str(_first(d, ["region", "city", "area"], "")),
                str(_first(d, ["signature", "sign", "description"], ""))[:24],
            )
            if part
        ),
        "apply_id": apply_id or applicant_id,
    }


def normalize_friend_applications(data: Any, current_uid: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in extract_list(data):
        normalized = normalize_friend_application(item, current_uid)
        if normalized:
            out.append(normalized)
    return out


def normalize_friends(data: Any, current_uid: str = "") -> List[Dict[str, Any]]:
    """Normalize getAddFriend relationship rows into the other user's profile."""
    out: List[Dict[str, Any]] = []
    current = str(current_uid or "")
    for item in extract_list(data):
        normalized = normalize_friend_application(item, current)
        if normalized:
            normalized["relation_id"] = str(
                _first(
                    item if isinstance(item, dict) else {},
                    ["relation_id", "relationId", "subid", "id"],
                    normalized.get("relation_id") or "",
                )
            )
            normalized["apply_id"] = ""
            out.append(normalized)
            continue
        user = normalize_user(item)
        if user and user.get("id") and str(user["id"]) != current:
            out.append(user)
    return out


def normalize_conversation(item: Any) -> Optional[Dict[str, Any]]:
    """Normalize the APK ``ReceiveMessageList`` conversation summary model."""
    d = _entity_dict(item, ("conversation", "info"))
    if not d:
        return None

    nested_user = _first(d, ["userInfoList", "user_info", "user", "userinfo"], None)
    user = normalize_user(nested_user)
    conversation_user = str(
        _first(d, ["conversation_user", "conversationUser", "peer_id", "target_id"], "")
    )
    # conversation_user is the target chosen by the APK. Some responses put the
    # logged-in user's profile in userInfoList, so preferring that nested id makes
    # a bogus self-conversation appear in the Web list.
    peer_id = str(conversation_user or (user or {}).get("id") or "")
    peer_user = user if not user or not peer_id or str(user.get("id") or "") == peer_id else None
    peer_nickname = str(
        _first(
            d,
            [
                "conversation_nickname",
                "conversationNickname",
                "peer_name",
                "peerName",
                "peer_nickname",
                "peerNickname",
                "friendnickname",
                "friendNickname",
                "yournickname",
                "yourNickname",
            ],
            (peer_user or {}).get("nickname") or "",
        )
    )
    peer_avatar = resolve_media_url(
        _first(
            d,
            [
                "conversation_portrait",
                "conversationPortrait",
                "conversation_user_portrait",
                "conversationUserPortrait",
                "peer_avatar",
                "peerAvatar",
                "peer_portrait",
                "peerPortrait",
                "friendportrait",
                "friendPortrait",
                "yourportrait",
                "yourPortrait",
            ],
            (peer_user or {}).get("avatar") or "",
        )
    )
    record_id = str(_first(d, ["id", "conversation_id", "conversationId"], ""))
    content = str(_first(d, ["content", "last_message", "message", "text"], ""))
    timestamp = str(
        _first(d, ["msgTimestamp", "msg_timestamp", "timestamp", "sent_time", "time"], "")
    )
    if not any((record_id, peer_id, content, timestamp, user)):
        return None
    return {
        "id": record_id or peer_id,
        "conversation_user": conversation_user,
        "peer_id": peer_id,
        "nickname": peer_nickname or peer_id or "用户",
        "avatar": peer_avatar,
        "content": content,
        "last_message": content,
        "timestamp": timestamp,
        "from_user_id": str(_first(d, ["fromUserId", "from_user_id", "from"], "")),
        "to_user_id": str(_first(d, ["toUserId", "to_user_id", "to"], "")),
        "object_name": str(_first(d, ["objectName", "object_name"], "")),
        "channel_type": str(_first(d, ["channelType", "channel_type"], "")),
        "msg_uid": str(_first(d, ["msgUID", "msg_uid", "message_uid"], "")),
        "unread_count": _num(_first(d, ["unreadCount", "unread_count", "unread"], 0)),
        "online": str(
            _first(
                d,
                ["online", "online_status", "peer_online", "conversation_user_online"],
                (peer_user or {}).get("online") or "",
            )
        ),
        "hide_online": str(
            _first(
                d,
                ["hide_online", "peer_hide_online"],
                (peer_user or {}).get("hide_online") or "0",
            )
        ),
        "user": peer_user,
    }


def normalize_conversations(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_conversation)


_TIM_TYPE_BY_NUMBER = {
    "1": "TIMTextElem",
    "2": "TIMCustomElem",
    "3": "TIMImageElem",
    "4": "TIMSoundElem",
    "5": "TIMVideoFileElem",
    "6": "TIMFileElem",
    "7": "TIMLocationElem",
    "8": "TIMFaceElem",
    "9": "TIMGroupTipElem",
    "10": "TIMRelayElem",
}

_MESSAGE_KIND_BY_OBJECT = {
    "TIMTextElem": "text",
    "TIMCustomElem": "custom",
    "TIMImageElem": "image",
    "TIMSoundElem": "audio",
    "TIMVideoFileElem": "video",
    "TIMFileElem": "file",
    "TIMLocationElem": "location",
    "TIMFaceElem": "face",
    "TIMGroupTipElem": "group_tip",
    "TIMRelayElem": "relay",
}


def _message_object_name(value: Any) -> str:
    raw = str(value or "").strip()
    return _TIM_TYPE_BY_NUMBER.get(raw, raw)


def _message_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    parsed = _as_dict(value)
    return dict(parsed) if parsed else {}


def _infer_message_object_name(payload: Dict[str, Any]) -> str:
    keys = {str(key).lower() for key in payload}
    if keys & {"text", "content"}:
        return "TIMTextElem"
    if keys & {"imageinfoarray", "image_info_array", "imageformat"}:
        return "TIMImageElem"
    if keys & {"videourl", "videouuid", "thumburl", "videosecond"}:
        return "TIMVideoFileElem"
    if keys & {"filename", "filesize"}:
        return "TIMFileElem"
    if "index" in keys and "data" in keys:
        return "TIMFaceElem"
    if keys & {"second", "soundurl"} and keys & {"uuid", "url", "soundurl"}:
        return "TIMSoundElem"
    return ""


def _message_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _message_data_text(value: Any) -> str:
    """Convert TIM face/custom byte data to the UTF-8 text used by the APK."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, list) and all(isinstance(part, int) for part in value):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            pass
    return str(value or "")


def _normalize_image_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    infos = _message_list(
        _first(payload, ["imageInfoArray", "ImageInfoArray", "image_info_array", "images"], [])
    )
    normalized: List[Dict[str, Any]] = []
    for raw in infos:
        info = raw if isinstance(raw, dict) else _as_dict(raw)
        if not info:
            continue
        url = resolve_media_url(_first(info, ["url", "URL", "imageUrl", "image_url"], ""))
        normalized.append(
            {
                "type": _num(_first(info, ["type", "Type"], 0)),
                "url": url,
                "uuid": str(_first(info, ["UUID", "uuid"], "")),
                "size": _num(_first(info, ["size", "Size"], 0)),
                "width": _num(_first(info, ["width", "Width"], 0)),
                "height": _num(_first(info, ["height", "Height"], 0)),
            }
        )
    direct_url = resolve_media_url(
        _first(payload, ["url", "URL", "imageUrl", "image_url", "originalUrl"], "")
    )
    if direct_url and not any(info.get("url") == direct_url for info in normalized):
        normalized.append(
            {
                "type": _num(_first(payload, ["type", "Type"], 0)),
                "url": direct_url,
                "uuid": str(_first(payload, ["UUID", "uuid"], "")),
                "size": _num(_first(payload, ["size", "Size"], 0)),
                "width": _num(_first(payload, ["width", "Width"], 0)),
                "height": _num(_first(payload, ["height", "Height"], 0)),
            }
        )
    original = next((info for info in normalized if info["type"] == 0 and info["url"]), None)
    large = next((info for info in normalized if info["type"] == 1 and info["url"]), None)
    thumbnail = next((info for info in normalized if info["type"] == 2 and info["url"]), None)
    fallback = next((info for info in normalized if info["url"]), {})
    # TIM image variants are not returned in a stable array order.  Keep the
    # full-size URL as the canonical target used by preview/download actions,
    # and expose the thumbnail separately for the chat bubble.
    primary = original or large or thumbnail or fallback
    preview = thumbnail or large or original or fallback
    large_variant = large or original or thumbnail or fallback
    out.update(
        {
            "images": normalized,
            "url": str(primary.get("url") or ""),
            "thumbnail": str(preview.get("url") or ""),
            "thumbnail_url": str(preview.get("url") or ""),
            "large_url": str(large_variant.get("url") or ""),
            "original_url": str(primary.get("url") or ""),
            "width": _num(primary.get("width")),
            "height": _num(primary.get("height")),
            "size": _num(primary.get("size")),
            "uuid": str(primary.get("uuid") or _first(payload, ["UUID", "uuid"], "")),
            "format": _num(_first(payload, ["imageFormat", "ImageFormat", "format"], 0)),
        }
    )
    return out


def _normalize_timed_media_payload(payload: Dict[str, Any], kind: str) -> Dict[str, Any]:
    out = dict(payload)
    if kind == "audio":
        url = resolve_media_url(_first(payload, ["url", "Url", "URL", "soundUrl"], ""))
        out.update(
            {
                "url": url,
                "uuid": str(_first(payload, ["UUID", "uuid"], "")),
                "duration": _num(_first(payload, ["second", "Second", "duration"], 0)),
                "second": _num(_first(payload, ["second", "Second", "duration"], 0)),
                "size": _num(_first(payload, ["size", "Size"], 0)),
            }
        )
        return out
    video_url = resolve_media_url(
        _first(payload, ["videoUrl", "VideoUrl", "url", "URL"], "")
    )
    thumb_url = resolve_media_url(
        _first(payload, ["thumbUrl", "ThumbUrl", "snapshotUrl", "snapshot_url"], "")
    )
    out.update(
        {
            "url": video_url,
            "video_url": video_url,
            "thumbnail_url": thumb_url,
            "thumb_url": thumb_url,
            "uuid": str(_first(payload, ["videoUUID", "VideoUUID", "UUID", "uuid"], "")),
            "thumb_uuid": str(_first(payload, ["thumbUUID", "ThumbUUID"], "")),
            "duration": _num(_first(payload, ["second", "Second", "videoSecond", "VideoSecond", "duration"], 0)),
            "second": _num(_first(payload, ["second", "Second", "videoSecond", "VideoSecond", "duration"], 0)),
            "size": _num(_first(payload, ["videoSize", "VideoSize", "size", "Size"], 0)),
            "format": str(_first(payload, ["format", "videoFormat", "VideoFormat"], "")),
            "width": _num(_first(payload, ["thumbWidth", "ThumbWidth", "width", "Width"], 0)),
            "height": _num(_first(payload, ["thumbHeight", "ThumbHeight", "height", "Height"], 0)),
        }
    )
    return out


def _normalize_element_payload(object_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    kind = _MESSAGE_KIND_BY_OBJECT.get(object_name, "unknown")
    if kind == "text":
        out = dict(payload)
        out["text"] = str(_first(payload, ["text", "Text", "content"], ""))
        return out
    if kind == "image":
        return _normalize_image_payload(payload)
    if kind in {"audio", "video"}:
        return _normalize_timed_media_payload(payload, kind)
    if kind == "file":
        out = dict(payload)
        out.update(
            {
                "url": resolve_media_url(_first(payload, ["url", "Url", "URL"], "")),
                "uuid": str(_first(payload, ["UUID", "uuid"], "")),
                "name": str(_first(payload, ["fileName", "FileName", "name"], "")),
                "file_name": str(_first(payload, ["fileName", "FileName", "name"], "")),
                "size": _num(_first(payload, ["fileSize", "FileSize", "size", "Size"], 0)),
            }
        )
        return out
    if kind == "face":
        out = dict(payload)
        data = _message_data_text(_first(payload, ["data", "Data"], ""))
        out.update(
            {
                "index": _num(_first(payload, ["index", "Index"], 0)),
                "data": data,
                "url": resolve_media_url(data),
            }
        )
        return out
    if kind == "custom":
        out = dict(payload)
        out.update(
            {
                "data": _message_data_text(_first(payload, ["data", "Data"], "")),
                "description": str(_first(payload, ["description", "Desc"], "")),
                "extension": str(_first(payload, ["extension", "Ext"], "")),
            }
        )
        return out
    return dict(payload)


def _normalize_message_elements(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
    raw_body = _first(d, ["MsgBody", "msgBody", "msg_body", "elements"], None)
    for raw in _message_list(raw_body):
        if not isinstance(raw, dict):
            continue
        object_name = _message_object_name(
            _first(raw, ["MsgType", "msgType", "type", "objectName", "object_name"], "")
        )
        payload = _message_payload(
            _first(raw, ["MsgContent", "msgContent", "payload", "content"], {})
        )
        elements.append(
            {
                "object_name": object_name,
                "type": _MESSAGE_KIND_BY_OBJECT.get(object_name, "unknown"),
                "payload": _normalize_element_payload(object_name, payload),
            }
        )
    if elements:
        return elements
    object_name = _message_object_name(
        _first(d, ["objectName", "object_name", "msg_type", "type", "elemType", "elem_type"], "")
    )
    payload = _message_payload(_first(d, ["payload", "msgContent", "msg_content"], {}))
    object_name = object_name or _infer_message_object_name(payload)
    if object_name or payload:
        elements.append(
            {
                "object_name": object_name,
                "type": _MESSAGE_KIND_BY_OBJECT.get(object_name, "unknown"),
                "payload": _normalize_element_payload(object_name, payload),
            }
        )
    return elements


def _flash_unique_id(text: str, object_name: str, cloud_data: Any) -> str:
    is_flash_copy = "闪图" in text or "点击查看5秒闪图" in text
    if not is_flash_copy and object_name != "flash_photo":
        return ""
    parsed = cloud_data if isinstance(cloud_data, dict) else _as_dict(cloud_data)
    if parsed:
        return str(_first(parsed, ["uniqueid", "uniqueId", "unique_id", "id"], ""))
    return _message_data_text(cloud_data).strip()


def normalize_message(item: Any) -> Optional[Dict[str, Any]]:
    """Normalize one Message_detail/TIM-style C2C message."""
    d = _entity_dict(item, ("message", "info"))
    if not d:
        return None
    elements = _normalize_message_elements(d)
    primary = elements[0] if elements else {"object_name": "", "type": "unknown", "payload": {}}
    object_name = str(primary.get("object_name") or "")
    payload_dict = primary.get("payload") if isinstance(primary.get("payload"), dict) else {}
    element_text = "".join(
        str((element.get("payload") or {}).get("text") or "")
        for element in elements
        if element.get("type") == "text" and isinstance(element.get("payload"), dict)
    )
    text = str(_first(d, ["content", "text", "message", "msg", "body"], element_text))
    if not object_name and text:
        object_name = "TIMTextElem"
        payload_dict = {**payload_dict, "text": text}
        primary = {"object_name": object_name, "type": "text", "payload": payload_dict}
        elements = [primary]
    from_id = str(
        _first(
            d,
            [
                "fromUserId",
                "from_user_id",
                "from_id",
                "sendUserId",
                "senderId",
                "from",
                "sender",
                "from_account",
                "From_Account",
            ],
            "",
        )
    )
    to_id = str(
        _first(
            d,
            [
                "toUserId",
                "to_user_id",
                "to_id",
                "receiveUserId",
                "receiverId",
                "to",
                "receiver",
                "to_account",
                "To_Account",
            ],
            "",
        )
    )
    timestamp = str(
        _first(
            d,
            [
                "msgTimestamp",
                "MsgTimeStamp",
                "msg_timestamp",
                "timestamp",
                "sent_time",
                "time",
                "msg_time",
            ],
            "",
        )
    )
    message_id = str(
        _first(d, ["msgUID", "msg_uid", "message_uid", "MsgKey", "ID", "id", "msg_id"], "")
    )
    msg_key = str(
        _first(d, ["MsgKey", "msg_key", "messageKey", "message_key", "msgUID", "msg_uid"], "")
    )
    revoked = _bool(
        _first(
            d,
            ["isRevoked", "is_revoked", "revoked", "isWithdrawn", "is_withdrawn", "is_revoke"],
            False,
        ),
        False,
    )
    peer_read_raw = _first(
        d,
        [
            "isPeerRead",
            "is_peer_read",
            "peerRead",
            "peer_read",
            "readByPeer",
            "read_by_peer",
        ],
        None,
    )
    read_state_raw = str(
        _first(d, ["readStatus", "read_status", "peer_read_status"], "")
    ).strip()
    if peer_read_raw is None and read_state_raw:
        lowered = read_state_raw.lower()
        if lowered in {"read", "1", "true", "yes", "已读"}:
            peer_read: Optional[bool] = True
        elif lowered in {"unread", "0", "false", "no", "未读"}:
            peer_read = False
        else:
            peer_read = None
    elif peer_read_raw is None:
        peer_read = None
    else:
        peer_read = _bool(peer_read_raw, False)
    receipt_info = _as_dict(
        _first(d, ["readReceiptInfo", "read_receipt_info", "receiptInfo", "receipt_info"], {})
    ) or {}
    read_time = str(
        _first(
            d,
            [
                "readTime",
                "read_time",
                "lastReadTime",
                "last_read_time",
                "readAt",
                "read_at",
                "peerReadTime",
                "peer_read_time",
            ],
            _first(
                receipt_info,
                [
                    "readTime",
                    "read_time",
                    "lastReadTime",
                    "last_read_time",
                    "readAt",
                    "read_at",
                ],
                "",
            ),
        )
    )
    cloud_data = _first(
        d,
        ["cloudCustomData", "CloudCustomData", "cloud_custom_data", "cloudData"],
        "",
    )
    flash_unique_id = _flash_unique_id(text, object_name, cloud_data)
    kind = "flash" if flash_unique_id else str(primary.get("type") or "unknown")
    send_status = str(_first(d, ["status", "send_status", "message_status"], ""))
    progress = _first(d, ["progress", "uploadProgress", "upload_progress"], None)
    if not any((text, from_id, to_id, timestamp, message_id, object_name, payload_dict, elements)):
        return None
    return {
        "id": message_id,
        "msg_key": msg_key,
        "MsgKey": msg_key,
        "is_revoked": revoked,
        "isRevoked": revoked,
        "text": text,
        "content": text,
        "type": kind,
        "message_type": kind,
        "payload": payload_dict,
        "elements": elements,
        "cloud_custom_data": cloud_data,
        "cloudCustomData": cloud_data,
        "flash_unique_id": flash_unique_id,
        "is_flash": bool(flash_unique_id),
        "from_user_id": from_id,
        "from": from_id,
        "to_user_id": to_id,
        "to": to_id,
        "timestamp": timestamp,
        "time": timestamp,
        "object_name": object_name,
        "objectName": object_name,
        "flow": str(_first(d, ["flow", "message_flow"], "")),
        "is_peer_read": peer_read,
        "read_state": "read" if peer_read is True else "unread" if peer_read is False else "",
        "read_time": read_time,
        "readTime": read_time,
        "send_status": send_status,
        "status": send_status,
        "progress": progress,
    }


def normalize_messages(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_message)


def _entity_dict(item: Any, nested_keys: Tuple[str, ...] = ()) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        parsed = _as_dict(item)
        if not parsed:
            return None
        item = parsed
    if not isinstance(item, dict):
        return None
    out = dict(item)
    for key in nested_keys:
        nested = out.get(key)
        if isinstance(nested, str):
            nested = _as_dict(nested)
        if isinstance(nested, dict):
            out = {**out, **nested}
    return out


def _normalize_many(data: Any, one: Any) -> List[Dict[str, Any]]:
    raw = extract_list(data)
    if not raw and isinstance(data, (dict, str)):
        item = one(data)
        return [item] if item else []
    out: List[Dict[str, Any]] = []
    for item in raw:
        dto = one(item)
        if dto:
            out.append(dto)
    return out


def normalize_slide(item: Any) -> Optional[Dict[str, Any]]:
    d = _entity_dict(item, ("slide", "banner", "info"))
    if not d:
        return None
    sid = str(_first(d, ["id", "slideid", "slide_id", "banner_id"], ""))
    title = str(
        _first(d, ["title", "name", "slidename", "slide_name", "description", "desc"], "")
    )
    image = resolve_media_url(
        _first(
            d,
            [
                "image",
                "img",
                "pic",
                "picture",
                "cover",
                "thumb",
                "thumbnail",
                "slideimage",
                "slide_img",
                "slidepic",
                "slidepicture",
                "slide_picture",
            ],
            "",
        )
    )
    link = str(
        _first(
            d,
            ["link", "href", "target_url", "jump_url", "web_url", "redirect", "slideurl", "url"],
            "",
        )
    )
    if not any((sid, title, image, link)):
        return None
    return {
        "id": sid,
        "title": title or sid or "轮播",
        "image": image,
        "url": link,
        "sort": str(_first(d, ["sort", "slidesort", "order", "weight"], "")),
    }


def normalize_slides(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_slide)


def normalize_topic(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str) and not _as_dict(item):
        name = item.strip()
        return {"id": "", "name": name, "title": name, "description": "", "image": "", "post_count": 0} if name else None
    d = _entity_dict(item, ("topic", "info"))
    if not d:
        return None
    tid = str(_first(d, ["id", "topicid", "topic_id", "topicId"], ""))
    name = str(_first(d, ["topic", "topic_name", "topicName", "name", "title"], ""))
    desc = str(_first(d, ["description", "desc", "content", "summary"], ""))
    image = resolve_media_url(_first(d, ["image", "img", "pic", "cover", "thumb"], ""))
    if not any((tid, name, desc, image)):
        return None
    return {
        "id": tid,
        "name": name or tid or "话题",
        "title": name or tid or "话题",
        "description": desc,
        "image": image,
        "post_count": _num(_first(d, ["post_count", "postnum", "count", "num"], 0)),
    }


def normalize_topics(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_topic)


def _media_list(value: Any) -> List[str]:
    if isinstance(value, list):
        values = value
    else:
        values = str(value or "").split(",")
    out: List[str] = []
    for item in values:
        url = resolve_media_url(item)
        if url and not url.endswith("/0") and url not in out:
            out.append(url)
    return out


def _topic_names(value: Any) -> List[str]:
    parsed: Any = value
    if isinstance(value, str) and value.strip()[:1] in "[{":
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
    values = parsed if isinstance(parsed, list) else [parsed]
    out: List[str] = []
    for item in values:
        if isinstance(item, dict):
            name = str(_first(item, ["topic", "name", "title"], "")).strip()
        else:
            name = str(item or "").strip()
        if name and name not in {"0", "[]", "{}"} and name not in out:
            out.append(name)
    return out


def normalize_post(item: Any) -> Optional[Dict[str, Any]]:
    """Normalize the APK LuntanList payload used by the Dynamic feed."""
    d = _entity_dict(item, ("post", "luntan", "info"))
    if not d:
        return None
    post_id = str(_first(d, ["id", "postid", "post_id", "postId"], ""))
    author = normalize_user(_first(d, ["userInfo", "userinfo", "user", "author"], None))
    author_id = str(_first(d, ["authid", "author_id", "uid", "myid"], ""))
    nickname = str(_first(d, ["authnickname", "nickname", "author_name", "mynickname"], ""))
    avatar = resolve_media_url(
        _first(d, ["authportrait", "portrait", "avatar", "myportrait"], "")
    )
    if author:
        author_id = author_id or str(author.get("id") or "")
        nickname = nickname or str(author.get("nickname") or "")
        avatar = avatar or str(author.get("avatar") or "")
    content = str(_first(d, ["posttext", "content", "context", "text", "body"], ""))
    pictures = _media_list(_first(d, ["postpicture", "pictures", "picture", "images"], ""))
    video = resolve_media_url(_first(d, ["postvideo", "video", "video_url"], ""))
    if video.endswith("/0"):
        video = ""
    cover = resolve_media_url(_first(d, ["cover", "video_cover", "thumb"], ""))
    if not any((post_id, author_id, nickname, content, pictures, video)):
        return None
    return {
        "id": post_id,
        "author_id": author_id,
        "nickname": nickname or (f"用户 {author_id}" if author_id else "用户"),
        "avatar": avatar,
        "title": str(_first(d, ["posttitle", "title"], "")),
        "content": content.replace("\\n", "\n"),
        "pictures": pictures,
        "video": video,
        "cover": cover,
        "plate": str(_first(d, ["platename", "plate", "category"], "")),
        "posttip": str(_first(d, ["posttip", "tip"], "")),
        "topics": _topic_names(_first(d, ["topic", "topicLists", "topics"], "")),
        "like_count": _num(_first(d, ["like", "like_count", "likes"], 0)),
        "comment_count": _num(_first(d, ["comment_sum", "comment_count", "comments"], 0)),
        "time": str(_first(d, ["time", "created_at", "create_time"], "")),
        "age": str(_first(d, ["age", "authage"], "")),
        "gender": str(_first(d, ["gender", "authgender"], "")),
        "region": str(_first(d, ["region", "authregion"], "")),
        "property": str(_first(d, ["property", "authproperty"], "")),
        "comment_forbid": _bool(_first(d, ["comment_forbid"], False)),
        "hide_comment": _bool(_first(d, ["hide_comment"], False)),
        "visibility_scope": str(_first(d, ["visibility_scope", "visible_scope"], "")),
        "is_pinned": _bool(_first(d, ["u_top", "is_top", "pinned"], False)),
        "is_liked": _bool(_first(d, ["ifauthlike", "is_liked", "liked"], False)),
    }


def normalize_posts(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_post)


def normalize_comment(item: Any) -> Optional[Dict[str, Any]]:
    d = _entity_dict(item, ("comment", "info"))
    if not d:
        return None
    comment_id = str(_first(d, ["id", "comment_id", "commentID"], ""))
    author_id = str(_first(d, ["authid", "author_id", "uid", "myID"], ""))
    content = str(_first(d, ["comment_text", "content", "text", "body"], ""))
    nickname = str(_first(d, ["nickname", "authnickname", "name"], ""))
    avatar = resolve_media_url(_first(d, ["portrait", "authportrait", "avatar"], ""))
    if not any((comment_id, author_id, content, nickname)):
        return None
    return {
        "id": comment_id,
        "post_id": str(_first(d, ["postid", "postID", "post_id"], "")),
        "author_id": author_id,
        "nickname": nickname or (f"用户 {author_id}" if author_id else "用户"),
        "avatar": avatar,
        "content": content.replace("\\n", "\n"),
        "time": str(_first(d, ["time", "created_at", "create_time"], "")),
        "like_count": _num(_first(d, ["like", "like_count", "likes"], 0)),
        "is_liked": _bool(_first(d, ["ifauthlike", "is_liked", "liked"], False)),
        "is_forbidden": _bool(_first(d, ["forbid", "is_forbidden"], False)),
        "main_id": str(_first(d, ["mainID", "main_id"], "")),
        "sub_id": str(_first(d, ["subID", "sub_id"], "")),
        "reply_to_name": str(_first(d, ["sub_nickname", "reply_to_name"], "")),
        "reply_count": _num(_first(d, ["subcomment_num", "reply_count"], 0)),
    }


def normalize_comments(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_comment)


def normalize_room(item: Any) -> Optional[Dict[str, Any]]:
    d = _entity_dict(item, ("room", "roominfo", "roomInfo", "info"))
    if not d:
        return None
    rid = str(_first(d, ["id", "roomId", "room_id", "roomid", "channel_id"], ""))
    name = str(_first(d, ["roomName", "room_name", "roomname", "name", "title"], ""))
    theme_picture_url = resolve_media_url(_first(d, ["themePictureUrl", "theme_picture_url"], ""))
    background_url = resolve_media_url(_first(d, ["backgroundUrl", "background_url"], ""))
    legacy_cover = resolve_media_url(
        _first(d, ["cover", "image", "img", "pic", "roomCover", "portrait"], "")
    )
    cover = theme_picture_url or legacy_cover or background_url
    channel = str(_first(d, ["channel", "channelName", "channel_name", "channel_id"], ""))
    create_user = _as_dict(_first(d, ["createUser", "creator", "owner"], None)) or {}
    owner_id = str(
        _first(d, ["owner_id", "ownerId", "uid", "userId", "anchor_id", "myID"], "")
        or _first(create_user, ["userId", "user_id", "uid", "id"], "")
    )
    owner_name = str(
        _first(d, ["owner_name", "ownerName", "nickname", "anchor_name"], "")
        or _first(create_user, ["userName", "user_name", "nickname", "name"], "")
    )
    owner_avatar = resolve_media_url(
        _first(d, ["owner_avatar", "ownerAvatar"], "")
        or _first(create_user, ["portrait", "avatar", "headimg"], "")
    )
    if not any((rid, name, cover, channel, owner_id)):
        return None
    return {
        "id": rid,
        "name": name or rid or "房间",
        "title": name or rid or "房间",
        "cover": cover,
        "theme_picture_url": theme_picture_url,
        "background_url": background_url,
        "channel": channel,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "owner_avatar": owner_avatar,
        "room_type": str(_first(d, ["audioroomtype", "room_type", "roomType", "type"], "")),
        "online_count": _num(
            _first(d, ["online_count", "online", "member_count", "userTotal", "people", "num"], 0)
        ),
        "status": str(_first(d, ["status", "state"], "")),
        "is_private": _bool(_first(d, ["isPrivate", "is_private", "private"], False)),
        "is_stopped": _bool(_first(d, ["stop", "isStopped", "is_stopped"], False)),
    }


def normalize_rooms(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_room)


def normalize_song(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str) and not _as_dict(item):
        name = item.strip()
        return {"id": "", "name": name, "title": name, "singer": "", "cover": "", "url": "", "duration": ""} if name else None
    d = _entity_dict(item, ("song", "music", "info"))
    if not d:
        return None
    sid = str(_first(d, ["id", "songId", "song_id", "musicId", "music_id"], ""))
    name = str(_first(d, ["songName", "song_name", "musicName", "music_name", "name", "title"], ""))
    singer = str(_first(d, ["singer", "artist", "author", "singerName", "singer_name"], ""))
    cover = str(_first(d, ["cover", "image", "img", "pic", "album_pic", "albumPic"], ""))
    url = str(_first(d, ["url", "play_url", "playUrl", "music_url", "song_url", "audio"], ""))
    if not any((sid, name, singer, cover, url)):
        return None
    return {
        "id": sid,
        "name": name or sid or "歌曲",
        "title": name or sid or "歌曲",
        "singer": singer,
        "cover": cover,
        "url": url,
        "duration": str(_first(d, ["duration", "time", "length"], "")),
    }


def normalize_songs(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_song)


def _normalize_bottle_words(value: Any) -> List[Dict[str, str]]:
    """Normalize the APK ``leave_words_json`` conversation payload."""
    if isinstance(value, dict):
        raw_words: List[Any] = [value]
    else:
        raw_words = _as_list(value)

    words: List[Dict[str, str]] = []
    for raw in raw_words:
        if isinstance(raw, str):
            parsed = _as_dict(raw)
            if parsed:
                raw = parsed
            elif raw.strip():
                words.append({"user_id": "", "content": raw.strip(), "time": ""})
                continue
        if not isinstance(raw, dict):
            continue
        content = str(
            _first(raw, ["leave_word", "leaveWord", "content", "message", "text"], "")
        ).replace("\\n", "\n").strip()
        user_id = str(_first(raw, ["uid", "user_id", "userId", "author_id"], ""))
        time = str(_first(raw, ["time", "created_at", "create_time"], ""))
        if content or user_id or time:
            words.append({"user_id": user_id, "content": content, "time": time})
    return words


def normalize_bottle(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str) and not _as_dict(item):
        content = item.strip()
        return {
            "id": "",
            "content": content,
            "user_id": "",
            "picker_id": "",
            "nickname": "",
            "avatar": "",
            "created_at": "",
            "picked_at": "",
            "picked_times": 0,
            "state": "",
            "leave_words": [{"user_id": "", "content": content, "time": ""}],
            "reply_count": 0,
        } if content else None
    d = _entity_dict(item, ("bottle", "draftBottle", "info"))
    if not d:
        return None
    bid = str(_first(d, ["id", "bottleId", "bottle_id", "draftBottleId", "draft_id"], ""))
    leave_words = _normalize_bottle_words(
        _first(
            d,
            ["leave_words_json", "leaveWordsJson", "leave_words", "leaveWords"],
            [],
        )
    )
    content = str(
        _first(d, ["content", "message", "text", "body", "leave_word"], "")
    ).replace("\\n", "\n").strip()
    if not content and leave_words:
        content = str(leave_words[0].get("content") or "")
    user = normalize_user(_first(d, ["user", "userinfo", "userInfo"], None))
    user_id = str(
        _first(d, ["user_id", "userId", "uid", "uid1", "myid", "owner_id"], "")
    )
    picker_id = str(_first(d, ["picker_id", "pickerId", "uid2"], ""))
    nickname = str(_first(d, ["nickname", "nick", "user_name", "name"], ""))
    avatar = resolve_media_url(_first(d, ["avatar", "portrait", "head", "headimg"], ""))
    if user:
        user_id = user_id or str(user.get("id") or "")
        nickname = nickname or str(user.get("nickname") or "")
        avatar = avatar or resolve_media_url(user.get("avatar") or "")
    if not any((bid, content, user_id, picker_id, nickname, avatar, leave_words)):
        return None
    raw_reply_count = _first(
        d,
        ["reply_count", "comment_count", "leave_word_count", "num"],
        None,
    )
    reply_count = (
        _num(raw_reply_count)
        if raw_reply_count is not None
        else max(len(leave_words) - 1, 0)
    )
    return {
        "id": bid,
        "content": content,
        "user_id": user_id,
        "picker_id": picker_id,
        "nickname": nickname,
        "avatar": avatar,
        "created_at": str(_first(d, ["created_at", "create_time", "createtime", "time"], "")),
        "picked_at": str(_first(d, ["picked_at", "pick_time", "pickTime"], "")),
        "picked_times": _num(_first(d, ["picked_times", "pickedTimes"], 0)),
        "state": str(_first(d, ["state", "status"], "")),
        "leave_words": leave_words,
        "reply_count": reply_count,
    }


def normalize_bottles(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_bottle)


def normalize_sticker(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str) and not _as_dict(item):
        value = item.strip()
        if not value:
            return None
        is_url = value.startswith(("http://", "https://", "data:"))
        return {
            "id": "",
            "name": "" if is_url else value,
            "image": value if is_url else "",
            "url": value if is_url else "",
            "data": value if is_url else "",
            "thumbnail": "",
            "group_id": "",
            "group_name": "",
            "group_icon": "",
            "index": 0,
            "width": 0,
            "height": 0,
            "favorite": False,
        }
    d = _entity_dict(item, ("sticker", "emoji", "info"))
    if not d:
        return None
    sid = str(_first(d, ["id", "stickerId", "sticker_id", "emoji_id"], ""))
    name = str(_first(d, ["name", "title", "stickerName", "sticker_name"], ""))
    image = resolve_media_url(_first(d, ["image", "img", "pic", "sticker_url", "stickerUrl", "url"], ""))
    thumb = resolve_media_url(_first(d, ["thumbnail", "thumb", "preview", "small_url"], ""))
    if not any((sid, name, image, thumb)):
        return None
    return {
        "id": sid,
        "name": name or sid or "表情",
        "image": image,
        "url": image,
        "data": str(_first(d, ["data", "faceKey", "face_key"], image)),
        "thumbnail": thumb,
        "group_id": str(_first(d, ["group_id", "groupId", "packageId", "package_id"], "")),
        "group_name": str(_first(d, ["group_name", "groupName", "packageName"], "")),
        "group_icon": resolve_media_url(_first(d, ["group_icon", "groupIcon", "packageIcon"], "")),
        "index": _num(_first(d, ["index", "group_id", "groupId", "packageId"], 0)),
        "width": _num(_first(d, ["width", "w"], 0)),
        "height": _num(_first(d, ["height", "h"], 0)),
        "favorite": _bool(_first(d, ["favorite", "is_favorite", "isFavorite", "collected"], False)),
    }


def normalize_stickers(data: Any) -> List[Dict[str, Any]]:
    """Flatten the v154 FaceGroup response into createFaceMessage-ready rows.

    ``GetAllStickersWithFavorite`` returns packages rather than individual
    stickers.  TUIKit sends the package id as ``TIMFaceElem.index`` and the
    image URL bytes as ``TIMFaceElem.data``; keeping those fields avoids a
    lossy package-to-generic-image conversion in the Web client.
    """
    out: List[Dict[str, Any]] = []
    for item in extract_list(data):
        d = item if isinstance(item, dict) else _as_dict(item)
        urls = _message_list(
            _first(d or {}, ["urls_array", "urlsArray", "urls", "stickerUrls"], [])
        )
        if not d or not urls:
            normalized = normalize_sticker(item)
            if normalized:
                out.append(normalized)
            continue
        group_id = str(_first(d, ["id", "group_id", "groupId", "packageId"], ""))
        group_name = str(_first(d, ["name", "group_name", "groupName", "packageName"], "表情包"))
        group_icon = resolve_media_url(_first(d, ["icon", "group_icon", "groupIcon"], ""))
        sizes = _message_list(_first(d, ["wh_array", "whArray", "sizes"], []))
        for position, raw_url in enumerate(urls):
            url_item = raw_url if isinstance(raw_url, dict) else {"url": raw_url}
            face_url = resolve_media_url(
                _first(url_item, ["url", "image", "src", "faceUrl", "face_url"], "")
            )
            if not face_url:
                continue
            size_item = sizes[position] if position < len(sizes) else {}
            size = size_item if isinstance(size_item, dict) else (_as_dict(size_item) or {})
            face_key = _message_data_text(
                _first(url_item, ["faceKey", "face_key", "data", "url"], face_url)
            )
            out.append(
                {
                    "id": f"{group_id}:{position}" if group_id else str(position),
                    "name": str(
                        _first(url_item, ["name", "title"], f"{group_name} {position + 1}")
                    ),
                    "image": face_url,
                    "url": face_url,
                    "data": face_key or face_url,
                    "thumbnail": face_url,
                    "group_id": group_id,
                    "group_name": group_name,
                    "group_icon": group_icon,
                    "index": _num(group_id),
                    "width": _num(_first(size, ["width", "w"], 0)),
                    "height": _num(_first(size, ["height", "h"], 0)),
                    "favorite": _bool(
                        _first(d, ["favorite", "is_favorite", "isFavorite", "collected"], True),
                        True,
                    ),
                }
            )
    return out


def normalize_gift(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    return {
        "id": str(_first(item, ["id", "giftid", "gift_id", "giftId"], "")),
        "name": str(
            _first(item, ["giftname", "gift_name", "name", "title", "giftName"], "礼物")
        ),
        "price": str(_first(item, ["price", "money", "coin", "cost", "gold"], "")),
        "icon": resolve_media_url(_first(item, ["icon", "image", "img", "picture", "url", "pic"], "")),
    }


def normalize_gifts(data: Any) -> List[Dict[str, Any]]:
    out = []
    for it in extract_list(data):
        g = normalize_gift(it)
        if g:
            out.append(g)
    return out


def normalize_task(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    tid = str(_first(item, ["id", "task_id", "taskId", "activity_id"], ""))
    title = str(
        _first(item, ["title", "name", "activity_name", "task_name", "content"], "任务")
    )
    progress = _num(_first(item, ["progress", "now", "current", "finish"], 0))
    total = _num(_first(item, ["num", "total", "target", "need", "max"], 0))
    available = str(_first(item, ["available", "status", "state", "receive"], ""))
    av_l = available.strip().lower()
    claimed_states = (
        "已领取",
        "已领",
        "完成领取",
        "领取成功",
        "claimed",
        "received",
    )
    negative_states = (
        "不可领取",
        "不能领取",
        "无法领取",
        "未完成",
        "disabled",
    )
    positive_states = (
        "可领取",
        "待领取",
        "未领取",
        "领取奖励",
        "available",
        "ready",
    )
    is_claimed = any(x in av_l for x in claimed_states)
    explicitly_blocked = any(x in av_l for x in negative_states)
    explicitly_ready = not explicitly_blocked and any(x in av_l for x in positive_states)
    completed = total > 0 and progress >= total
    if is_claimed:
        can_receive = False
    elif completed or explicitly_ready:
        can_receive = True
    else:
        can_receive = False
    if is_claimed:
        status_text = "已领取"
    elif can_receive:
        # APK 的领取按钮以任务进度达到 num 为本地门禁。服务端偶尔仍返回
        # “不可领取/未完成”的旧状态，不能让它覆盖已经完成的进度。
        status_text = "可领取"
    else:
        status_text = available or ("未完成" if explicitly_blocked else "进行中")
    return {
        "id": tid,
        "title": title,
        "progress": progress,
        "total": total,
        "progress_text": f"{progress}/{total}" if total else str(progress or "—"),
        "status_text": status_text,
        "is_claimed": is_claimed,
        "can_receive": can_receive,
        "reward": str(_first(item, ["reward", "prize", "gift", "card"], "")),
    }


def normalize_tasks(data: Any) -> List[Dict[str, Any]]:
    out = []
    for it in extract_list(data):
        t = normalize_task(it)
        if t:
            out.append(t)
    return out


def _dig_num(data: Any, keys: List[str]) -> Optional[int]:
    if data is None:
        return None
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return int(data)
    d = data if isinstance(data, dict) else _as_dict(data)
    if not d:
        # search in string
        if isinstance(data, str):
            for k in keys:
                m = re.search(rf'"{k}"\s*:\s*"?(\d+)"?', data)
                if m:
                    return int(m.group(1))
        return None
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return _num(d[k], 0)
    for v in d.values():
        if isinstance(v, dict):
            n = _dig_num(v, keys)
            if n is not None:
                return n
        if isinstance(v, str) and v.strip()[:1] == "{":
            n = _dig_num(v, keys)
            if n is not None:
                return n
    return None


MATCH_GENDERS = {"不限", "男", "女"}
MATCH_PROPERTY_ORDER = ("双", "Z", "B")
MATCH_PROPERTIES = set(MATCH_PROPERTY_ORDER)


def normalize_match_filters(data: Any) -> Dict[str, str]:
    """Read the two preferences exposed by the APK match filter dialog."""
    source = _as_dict(data) if isinstance(data, str) else data
    if not isinstance(source, dict):
        source = {}

    merged = dict(source)
    for key in ("user", "userinfo", "userInfo", "userInfoList", "json_obj", "data"):
        nested = source.get(key)
        if isinstance(nested, str):
            nested = _as_dict(nested)
        if isinstance(nested, dict):
            merged.update(nested)

    gender = str(
        _first(
            merged,
            ["match_gender", "matchGender", "matchgender"],
            "不限",
        )
    ).strip()
    property_ = str(
        _first(
            merged,
            ["match_property", "matchProperty", "matchproperty"],
            "双",
        )
    ).strip()
    return {
        "gender": gender if gender in MATCH_GENDERS else "不限",
        "property": property_ if property_ in MATCH_PROPERTIES else "双",
    }


def normalize_match_status(cards_data: Any, nums_data: Any, user: Optional[Dict] = None) -> Dict[str, Any]:
    online = _dig_num(
        nums_data,
        [
            "online",
            "online_free",
            "free_online",
            "online_num",
            "onlinefree",
            "Online",
            "onlinematch",
            "freeOnline",
        ],
    )
    local = _dig_num(
        nums_data,
        [
            "local",
            "local_free",
            "free_local",
            "local_num",
            "localfree",
            "Local",
            "localmatch",
            "freeLocal",
        ],
    )
    voice = _dig_num(nums_data, ["voice", "voice_free", "free_voice", "yuyin"])
    video = _dig_num(nums_data, ["video", "video_free", "free_video"])
    match_card = _dig_num(
        cards_data,
        [
            "match_card",
            "matchcard",
            "card",
            "num",
            "count",
            "number",
            "card_num",
            "matchCard",
        ],
    )
    # fallbacks: first small ints in dict
    if online is None and isinstance(nums_data, dict):
        for k, v in nums_data.items():
            if "online" in str(k).lower() or "在线" in str(k):
                online = _num(v, 0)
                break
    if local is None and isinstance(nums_data, dict):
        for k, v in nums_data.items():
            if "local" in str(k).lower() or "同城" in str(k):
                local = _num(v, 0)
                break
    if match_card is None and isinstance(cards_data, dict):
        for k, v in cards_data.items():
            if "card" in str(k).lower() or "match" in str(k).lower():
                match_card = _num(v, 0)
                break

    money = "0"
    if user:
        money = str(user.get("money") or "0")

    return {
        "online_free": online if online is not None else 0,
        "local_free": local if local is not None else 0,
        "voice_free": voice if voice is not None else 0,
        "video_free": video if video is not None else 0,
        "match_card": match_card if match_card is not None else 0,
        "money": money,
        "display": {
            "online": str(online if online is not None else "—"),
            "local": str(local if local is not None else "—"),
            "card": str(match_card if match_card is not None else "—"),
            "money": money,
        },
        "parsed": {
            "online_ok": online is not None,
            "local_ok": local is not None,
            "card_ok": match_card is not None,
        },
    }


def normalize_match_result(r: Any) -> Dict[str, Any]:
    """Turn match API result into product envelope with user cards."""
    base = envelope(r, include_raw=False)
    users = normalize_users(getattr(r, "data", None))
    # sometimes matched user is top-level fields
    if not users and isinstance(getattr(r, "data", None), dict):
        u = normalize_user(r.data)
        if u and u.get("id"):
            users = [u]
    base["items"] = users
    base["count"] = len(users)
    if base["ok"] and not users:
        # ok but empty — still success with empty state
        base["empty"] = True
        base["empty_title"] = "暂时没有可匹配的人"
        base["empty_detail"] = "稍后再试，或检查匹配次数/匹配卡"
    elif not base["ok"] and base.get("error"):
        base["empty"] = True
        base["empty_title"] = base["error"]["title"]
        base["empty_detail"] = base["error"]["detail"]
        base["empty_action"] = base["error"]["action"]
    else:
        base["empty"] = False
    return base


def session_user_dto(
    who: Dict[str, Any], profile: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build the logged-in user DTO, optionally enriched by a profile response."""
    profile_user = normalize_user(profile) if profile else None
    phone = str(who.get("phone") or "")
    if len(phone) >= 7:
        phone = f"{phone[:3]}****{phone[-4:]}"
    avatar = resolve_media_url(
        who.get("avatar")
        or who.get("portrait")
        or (profile_user or {}).get("avatar")
        or ""
    )
    return {
        "id": str(who.get("uid") or ""),
        "uid": str(who.get("uid") or ""),
        "nickname": str(who.get("nickname") or (profile_user or {}).get("nickname") or "游客"),
        "avatar": avatar,
        "portrait": avatar,
        "phone": phone,
        "is_realname": bool(who.get("is_realname")),
        "money": str(who.get("money") or "0"),
        "vip": str(who.get("vip") or "0"),
        "svip": str(who.get("svip") or "0"),
        "user_role": str(who.get("user_role") or ""),
        "rp_verify_time": str(who.get("rp_verify_time") or "0"),
        "logged_in": bool(who.get("logged_in")),
    }
