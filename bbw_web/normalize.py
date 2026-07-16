"""Normalize banghua API payloads into stable DTOs for the Web UI.

Product pages should render DTOs only — never depend on raw nested JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# APK serves relative paths like /images/999999/... from this host.
MEDIA_BASE = "https://oss.banghua.xin"


def resolve_media_url(value: Any) -> str:
    """Turn relative APK media paths into absolute OSS URLs."""
    raw = str(value or "").strip()
    if not raw or raw in {"null", "undefined", "None"}:
        return ""
    if raw.startswith("data:image/"):
        return raw
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
    (("340", "未实名", "实名认证", "还未实名"), "需要实名认证", "该功能需先完成实名，请在官方 App 内刷脸认证", "realname"),
    (("430", "卡不足", "匹配卡", "次数不足"), "次数或道具不足", "匹配卡/免费次数不够，可做任务或乐园币买卡", "buy_card"),
    (("403", "不可修改", "不可提现"), "暂无权限", "服务端拒绝了操作（常见：未实名或业务限制）", "none"),
    (("余额不足", "乐园币不足", "money"), "余额不足", "乐园币或余额不够，请先充值（Web 仅能创建订单参数）", "wallet"),
    (("400", "参数", "失败"), "请求未成功", "参数不完整或业务失败", "none"),
    (("false", "no", "NULL"), "操作未成功", "服务端返回失败", "none"),
]


def explain_error(
    code: str = "",
    message: str = "",
    raw: str = "",
    extra: str = "",
) -> Dict[str, Any]:
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
    sub_parts = [p for p in (uid and f"uid {uid}", role, city, dist, sign[:24]) if p]
    return {
        "id": uid,
        "nickname": nick,
        "avatar": avatar,
        "subtitle": " · ".join(sub_parts) if sub_parts else "",
        "role": role,
        "city": city,
        "signature": sign,
        "distance": dist,
        "online": str(_first(item, ["online", "online_status"], "")),
        "hide_online": str(_first(item, ["hide_online"], "0")),
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
        "nickname": str((peer_user or {}).get("nickname") or peer_id or "用户"),
        "avatar": str((peer_user or {}).get("avatar") or ""),
        "content": content,
        "last_message": content,
        "timestamp": timestamp,
        "from_user_id": str(_first(d, ["fromUserId", "from_user_id", "from"], "")),
        "to_user_id": str(_first(d, ["toUserId", "to_user_id", "to"], "")),
        "object_name": str(_first(d, ["objectName", "object_name"], "")),
        "channel_type": str(_first(d, ["channelType", "channel_type"], "")),
        "msg_uid": str(_first(d, ["msgUID", "msg_uid", "message_uid"], "")),
        "unread_count": _num(_first(d, ["unreadCount", "unread_count", "unread"], 0)),
        "user": peer_user,
    }


def normalize_conversations(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_conversation)


def normalize_message(item: Any) -> Optional[Dict[str, Any]]:
    """Normalize one Message_detail/TIM-style C2C message."""
    d = _entity_dict(item, ("message", "info"))
    if not d:
        return None
    payload = _first(d, ["payload", "msgContent", "msg_content"], None)
    payload_dict = payload if isinstance(payload, dict) else _as_dict(payload)
    text = str(
        _first(
            d,
            ["content", "text", "message", "msg", "body"],
            _first(payload_dict or {}, ["text", "Text", "content", "data"], ""),
        )
    )
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
            ],
            "",
        )
    )
    timestamp = str(
        _first(d, ["msgTimestamp", "msg_timestamp", "timestamp", "sent_time", "time", "msg_time"], "")
    )
    message_id = str(_first(d, ["msgUID", "msg_uid", "message_uid", "id", "msg_id"], ""))
    if not any((text, from_id, to_id, timestamp, message_id)):
        return None
    return {
        "id": message_id,
        "text": text,
        "content": text,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "timestamp": timestamp,
        "object_name": str(_first(d, ["objectName", "object_name", "msg_type", "type"], "")),
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


def normalize_room(item: Any) -> Optional[Dict[str, Any]]:
    d = _entity_dict(item, ("room", "roominfo", "roomInfo", "info"))
    if not d:
        return None
    rid = str(_first(d, ["id", "roomId", "room_id", "roomid", "channel_id"], ""))
    name = str(_first(d, ["roomName", "room_name", "roomname", "name", "title"], ""))
    cover = resolve_media_url(_first(d, ["cover", "image", "img", "pic", "roomCover", "portrait"], ""))
    channel = str(_first(d, ["channel", "channelName", "channel_name", "channel_id"], ""))
    owner_id = str(_first(d, ["owner_id", "ownerId", "uid", "userId", "anchor_id", "myID"], ""))
    owner_name = str(_first(d, ["owner_name", "ownerName", "nickname", "anchor_name"], ""))
    if not any((rid, name, cover, channel, owner_id)):
        return None
    return {
        "id": rid,
        "name": name or rid or "房间",
        "title": name or rid or "房间",
        "cover": cover,
        "channel": channel,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "room_type": str(_first(d, ["audioroomtype", "room_type", "roomType", "type"], "")),
        "online_count": _num(_first(d, ["online_count", "online", "member_count", "people", "num"], 0)),
        "status": str(_first(d, ["status", "state"], "")),
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


def normalize_bottle(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str) and not _as_dict(item):
        content = item.strip()
        return {"id": "", "content": content, "user_id": "", "nickname": "", "avatar": "", "created_at": "", "reply_count": 0} if content else None
    d = _entity_dict(item, ("bottle", "draftBottle", "info"))
    if not d:
        return None
    bid = str(_first(d, ["id", "bottleId", "bottle_id", "draftBottleId", "draft_id"], ""))
    content = str(_first(d, ["content", "message", "text", "body", "leave_word"], ""))
    user = normalize_user(_first(d, ["user", "userinfo", "userInfo"], None))
    user_id = str(_first(d, ["user_id", "userId", "uid", "myid", "owner_id"], ""))
    nickname = str(_first(d, ["nickname", "nick", "user_name", "name"], ""))
    avatar = resolve_media_url(_first(d, ["avatar", "portrait", "head", "headimg"], ""))
    if user:
        user_id = user_id or str(user.get("id") or "")
        nickname = nickname or str(user.get("nickname") or "")
        avatar = avatar or resolve_media_url(user.get("avatar") or "")
    if not any((bid, content, user_id, nickname, avatar)):
        return None
    return {
        "id": bid,
        "content": content,
        "user_id": user_id,
        "nickname": nickname,
        "avatar": avatar,
        "created_at": str(_first(d, ["created_at", "create_time", "createtime", "time"], "")),
        "reply_count": _num(_first(d, ["reply_count", "comment_count", "leave_word_count", "num"], 0)),
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
            "thumbnail": "",
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
        "thumbnail": thumb,
        "favorite": _bool(_first(d, ["favorite", "is_favorite", "isFavorite", "collected"], False)),
    }


def normalize_stickers(data: Any) -> List[Dict[str, Any]]:
    return _normalize_many(data, normalize_sticker)


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
    blocked = is_claimed or any(x in av_l for x in negative_states)
    explicitly_ready = any(x in av_l for x in positive_states)
    if blocked:
        can_receive = False
    elif explicitly_ready:
        can_receive = True
    else:
        can_receive = total > 0 and progress >= total
    return {
        "id": tid,
        "title": title,
        "progress": progress,
        "total": total,
        "progress_text": f"{progress}/{total}" if total else str(progress or "—"),
        "status_text": available or ("可领取" if can_receive else "进行中"),
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
