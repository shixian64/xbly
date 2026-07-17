#!/usr/bin/env python3
"""Product Web BFF — APK business surface with an opt-in research lab.

  python -m bbw_web --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web.store import SessionStore  # noqa: E402
from bbw_web import normalize as N  # noqa: E402
from bbw_web import flash_photo as F  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "bbw_sid"
STORE: Optional[SessionStore] = None


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Reject a second local server instead of sharing the same Windows port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


LAB_ENABLED = False
INVITE_LOGIN_ENABLED = False
CORS_ALLOW_ORIGINS: Set[str] = set()
MAX_JSON_BODY_BYTES = 256 * 1024
COOKIE_SECURE = False
PROFILE_CACHE_TTL_SEC = 15 * 60.0
PROFILE_CACHE_ERROR_TTL_SEC = 30.0
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS: Dict[str, List[float]] = {}
MUTATION_LOCK = threading.Lock()
RECENT_MUTATIONS: Dict[str, float] = {}
FINANCIAL_PATHS = {
    "/api/pay/coin",
    "/api/pay/vip",
    "/api/pay/card",
    "/api/wallet/exchange-vip",
    "/api/wallet/send-gift",
    "/api/wallet/withdraw",
}


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _rate_allowed(key: str, limit: int, window_sec: float) -> Tuple[bool, int]:
    """Small in-memory limiter for login and SMS abuse protection."""
    now = time.monotonic()
    cutoff = now - window_sec
    with RATE_LIMIT_LOCK:
        if len(RATE_LIMIT_BUCKETS) > 2048:
            stale = [
                bucket_key
                for bucket_key, stamps in RATE_LIMIT_BUCKETS.items()
                if not stamps or stamps[-1] <= cutoff
            ]
            for bucket_key in stale:
                RATE_LIMIT_BUCKETS.pop(bucket_key, None)
        recent = [stamp for stamp in RATE_LIMIT_BUCKETS.get(key, []) if stamp > cutoff]
        if len(recent) >= limit:
            retry = max(1, int(window_sec - (now - recent[0])))
            RATE_LIMIT_BUCKETS[key] = recent
            return False, retry
        recent.append(now)
        RATE_LIMIT_BUCKETS[key] = recent
        return True, 0


def _mutation_allowed(sid: str, path: str, data: Dict[str, Any], window_sec: float = 8.0) -> bool:
    """Reject an identical financial mutation repeated in a short window."""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{sid}\n{path}\n{canonical}".encode("utf-8")).hexdigest()
    now = time.monotonic()
    with MUTATION_LOCK:
        if len(RECENT_MUTATIONS) > 1024:
            cutoff = now - window_sec
            for key in [key for key, stamp in RECENT_MUTATIONS.items() if stamp <= cutoff]:
                RECENT_MUTATIONS.pop(key, None)
        previous = RECENT_MUTATIONS.get(digest)
        if previous is not None and now - previous < window_sec:
            return False
        RECENT_MUTATIONS[digest] = now
        return True


def _json_bytes(obj: Any, status: int = 200) -> Tuple[int, bytes, str]:
    return (
        status,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
        "application/json; charset=utf-8",
    )


def R(
    r: Any,
    *,
    lab: bool = False,
    include_value: bool = False,
    empty_ok: bool = False,
) -> Dict[str, Any]:
    """Protocol result. Product pages prefer RU/RG/RT/envelope helpers."""
    d = N.envelope(r, items=[], include_raw=lab)
    d["status"] = getattr(r, "status", 0)
    d["kind"] = str(getattr(r, "kind", "") or "")
    d["extra"] = str(getattr(r, "extra", "") or "")
    if empty_ok and d["kind"] == "empty" and 200 <= d["status"] < 300:
        d.update(ok=True, code="", message="请求已提交", error=None)
    if include_value:
        d.update(N.normalize_value(getattr(r, "data", None)))
    if lab:
        d["data"] = getattr(r, "data", None)
        d["raw_preview"] = (getattr(r, "raw", None) or "")[:1200]
    return d


def L(data: Any) -> Any:
    return N.extract_list(data)


def RL(r: Any) -> Dict[str, Any]:
    """List endpoint: normalized user cards when possible."""
    users = N.normalize_users(getattr(r, "data", None))
    items = users if users else []
    # fallback generic list of dicts as items with nickname guess
    if not items:
        for it in N.extract_list(getattr(r, "data", None)):
            u = N.normalize_user(it)
            if u:
                items.append(u)
    d = N.envelope(r, items=items)
    d["list"] = items  # back-compat
    d["status"] = getattr(r, "status", 0)
    return d


def _enrich_social_profiles(app: Any, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for item in items[:20]:
        needs_profile = bool(item.pop("_needs_profile", False))
        uid = str(item.get("id") or "")
        if not needs_profile or not uid:
            continue
        try:
            result = app.profile.get_user(uid)
            profiles = N.normalize_users(result.data)
            profile = next(
                (value for value in profiles if str(value.get("id") or "") == uid),
                profiles[0] if profiles else None,
            )
            if not profile:
                continue
            item["nickname"] = profile.get("nickname") or item.get("nickname")
            item["avatar"] = profile.get("avatar") or item.get("avatar")
            item["city"] = profile.get("city") or item.get("city")
            item["signature"] = profile.get("signature") or item.get("signature")
            item["subtitle"] = profile.get("subtitle") or item.get("subtitle")
        except Exception:
            continue
    for item in items[20:]:
        item.pop("_needs_profile", None)
    return items


def RS(r: Any, current_uid: str = "", app: Any = None) -> Dict[str, Any]:
    """Follow/fans endpoint normalized as the peer, never the logged-in user."""
    items = N.normalize_social_users(getattr(r, "data", None), current_uid)
    if app is not None:
        items = _enrich_social_profiles(app, items)
    else:
        for item in items:
            item.pop("_needs_profile", None)
    d = N.envelope(r, items=items)
    d["list"] = items
    d["status"] = getattr(r, "status", 0)
    return d


def _accepted_friend_ids(app: Any, current_uid: str) -> Set[str]:
    try:
        result = app.social.friends()
        if not getattr(result, "ok", False):
            return set()
        return {
            str(item.get("id") or "")
            for item in N.normalize_friends(result.data, current_uid)
            if item.get("id")
        }
    except Exception:
        return set()


def _friend_applications(app: Any, current_uid: str, result: Any = None) -> Tuple[Any, List[Dict[str, Any]]]:
    if result is None:
        result = app.social.friend_apply_list("1")
    items = N.normalize_friend_applications(result.data, current_uid)
    accepted = _accepted_friend_ids(app, current_uid)
    if accepted:
        items = [item for item in items if str(item.get("id") or "") not in accepted]
    return result, items


def _presence_uids(values: Any, limit: int = 100) -> List[str]:
    """Parse a bounded, de-duplicated TIM account list from query values."""
    raw_values = values if isinstance(values, (list, tuple)) else [values]
    out: List[str] = []
    seen: Set[str] = set()
    for raw in raw_values:
        for value in str(raw or "").replace("\n", ",").split(","):
            uid = value.strip()
            if not uid or uid in seen or len(uid) > 64:
                continue
            if any(ord(char) < 33 for char in uid):
                continue
            seen.add(uid)
            out.append(uid)
            if len(out) >= limit:
                return out
    return out


def _presence_status(value: Any) -> Tuple[str, str, Optional[bool]]:
    raw = str(value or "").strip()
    lowered = raw.lower().replace("_", "").replace("-", "")
    if lowered in {"pushonline", "background", "away", "后台在线"}:
        return "away", "后台在线", True
    if lowered in {
        "offline",
        "unlogin",
        "unlogged",
        "logout",
        "0",
        "false",
        "离线",
        "下线",
        "不在线",
    } or "offline" in lowered:
        return "offline", "离线", False
    if lowered in {"online", "1", "true", "active", "在线"} or (
        "online" in lowered and "offline" not in lowered
    ):
        return "online", "在线", True
    return "unknown", "状态未知", None


def _normalize_presence_result(result: Any, requested: List[str]) -> Dict[str, Any]:
    """Expose only stable account/status fields from Tencent query_online_status."""
    data = getattr(result, "data", None)
    rows: List[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in (
            "QueryResult",
            "queryResult",
            "UserStatusList",
            "userStatusList",
            "StatusList",
            "statusList",
            "items",
        ):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break

    by_uid: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = str(
            row.get("To_Account")
            or row.get("to_account")
            or row.get("UserID")
            or row.get("userID")
            or row.get("uid")
            or ""
        ).strip()
        if not uid:
            continue
        raw_status = row.get("Status")
        if raw_status is None:
            raw_status = row.get("status") or row.get("state")
        status, label, is_online = _presence_status(raw_status)
        by_uid[uid] = {
            "uid": uid,
            "status": status,
            "label": label,
            "is_online": is_online,
        }

    items = [
        by_uid.get(
            uid,
            {"uid": uid, "status": "unknown", "label": "状态未知", "is_online": None},
        )
        for uid in requested
    ]
    ok = bool(getattr(result, "ok", False))
    return {
        "ok": ok,
        "items": items,
        "list": items,
        "count": len(items),
        "message": "在线状态已更新" if ok else "在线状态暂时不可用",
    }


def RG(r: Any) -> Dict[str, Any]:
    gifts = N.normalize_gifts(getattr(r, "data", None))
    d = N.envelope(r, items=gifts)
    d["list"] = gifts
    return d


def RT(r: Any) -> Dict[str, Any]:
    tasks = N.normalize_tasks(getattr(r, "data", None))
    d = N.envelope(r, items=tasks)
    d["list"] = tasks
    return d


def _load_tasks(app: Any) -> Tuple[List[Dict[str, Any]], Any, Any]:
    """Read the authoritative task list with the APK's red-dot API as fallback."""
    create = app.call("createHotActivityList", uid=app.session.uid)
    have = app.call("haveHotActivityList", uid=app.session.uid)
    items = N.normalize_tasks(getattr(create, "data", None))
    if not items:
        items = N.normalize_tasks(getattr(have, "data", None))
    return items, create, have


def _attach_task_snapshot(
    payload: Dict[str, Any],
    task_id: str,
    refreshed_items: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    items = list(refreshed_items or [])
    task = next((item for item in items if str(item.get("id") or "") == str(task_id)), None)
    payload.update(
        items=items,
        list=items,
        count=len(items),
        task=task,
        verification="task_list_refreshed",
    )
    return task


def task_receive_envelope(
    result: Any,
    task_id: str,
    refreshed_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve an empty receive response from the refreshed server-side task state."""
    payload = R(result)
    status = int(getattr(result, "status", 0) or 0)
    kind = str(getattr(result, "kind", "") or "")
    if kind != "empty" or not 200 <= status < 300:
        return payload

    task = _attach_task_snapshot(payload, task_id, refreshed_items)
    payload["outcome"] = "unknown"
    if task and task.get("is_claimed"):
        payload.update(
            ok=True,
            code="",
            message="领取成功，任务状态已更新",
            error=None,
            outcome="claimed",
        )
        return payload

    if task and task.get("can_receive"):
        detail = "服务端返回空响应，刷新后任务仍显示可领取，暂不能确认奖励已经发放。请稍后更新进度再确认，避免连续点击。"
    elif task:
        detail = "服务端返回空响应，刷新后的任务也未显示“已领取”，暂不能确认奖励是否发放。请稍后更新进度再确认。"
    else:
        detail = "服务端返回空响应，刷新后未找到该任务，暂不能确认奖励是否发放。请稍后更新进度再确认。"
    payload.update(
        ok=False,
        code="TASK_CLAIM_UNCONFIRMED",
        message="领取结果待确认",
        error={
            "title": "领取结果待确认",
            "detail": detail,
            "action": "none",
            "code": "TASK_CLAIM_UNCONFIRMED",
            "message": "服务端返回空响应",
        },
    )
    return payload


def _enrich_session_profile(web_user: Any, result: Any = None) -> Optional[Dict[str, Any]]:
    """Fill placeholder login names/avatars from the authoritative profile API."""
    try:
        if result is None:
            result = web_user.app.profile.get_me()
        items = N.normalize_users(result.data)
        current_uid = str(web_user.app.session.uid or "")
        profile = next(
            (item for item in items if str(item.get("id") or "") == current_uid),
            items[0] if items else None,
        )
        if not profile:
            return None
        nickname = str(profile.get("nickname") or "").strip()
        if nickname and nickname not in {current_uid, "用户", "游客"}:
            web_user.app.session.nickname = nickname
        if profile.get("avatar"):
            web_user.app.session.portrait = str(profile["avatar"])
        web_user.persist()
        return profile
    except Exception:
        return None


def _clean_profile_region(value: Any) -> str:
    region = str(value or "").strip()
    if region.lower() in {"", "0", "none", "null", "undefined"}:
        return ""
    if region in {"不限", "未知", "未设置", "暂未设置", "—", "-"}:
        return ""
    return region


def _profile_region(source: Any, current_uid: str = "") -> str:
    """Read the account's saved region without using browser geolocation.

    APK v154's 同城 tab uses ``UserInfoList.getRegion()``. Prefer the exact
    raw profile field, then fall back to the normalized current-user profile.
    """
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except Exception:
            return ""
    if isinstance(source, dict):
        for key in ("region", "real_region", "realRegion", "user_region", "userRegion"):
            region = _clean_profile_region(source.get(key))
            if region:
                return region
        for key in ("user", "userinfo", "userInfo", "userInfoList", "profile", "data", "json_obj"):
            nested = source.get(key)
            if isinstance(nested, (dict, list, str)):
                region = _profile_region(nested, current_uid)
                if region:
                    return region
        province = _clean_profile_region(source.get("province") or source.get("real_province"))
        city = _clean_profile_region(source.get("city") or source.get("area"))
        if province and city:
            return city if province in city or "-" in city else f"{province}-{city}"
        if city or province:
            return city or province
        return _clean_profile_region(source.get("address"))
    if isinstance(source, list):
        current = str(current_uid or "")
        ordered = sorted(
            source,
            key=lambda item: 0
            if isinstance(item, dict)
            and current
            and str(item.get("id") or item.get("uid") or item.get("userId") or "") == current
            else 1,
        )
        for item in ordered:
            region = _profile_region(item, current)
            if region:
                return region
    normalized = N.normalize_user(source)
    return _clean_profile_region((normalized or {}).get("city"))


def _nearby_location_missing() -> Dict[str, Any]:
    title = "无法显示附近动态"
    detail = "当前用户资料中没有地区信息。请先在官方客户端完善所在地后再查看附近动态。"
    return {
        "ok": False,
        "code": "PROFILE_LOCATION_MISSING",
        "message": title,
        "error": {
            "title": title,
            "detail": detail,
            "action": "none",
            "code": "PROFILE_LOCATION_MISSING",
            "message": title,
        },
        "items": [],
        "list": [],
        "count": 0,
        "location_required": True,
    }


ENTITY_NORMALIZERS = {
    "slide": N.normalize_slides,
    "topic": N.normalize_topics,
    "post": N.normalize_posts,
    "comment": N.normalize_comments,
    "room": N.normalize_rooms,
    "song": N.normalize_songs,
    "bottle": N.normalize_bottles,
    "sticker": N.normalize_stickers,
    "conversation": N.normalize_conversations,
    "message": N.normalize_messages,
}


def RE(r: Any, entity: str) -> Dict[str, Any]:
    """Normalize one product entity type without coercing it into a user card."""
    normalizer = ENTITY_NORMALIZERS[entity]
    items = normalizer(getattr(r, "data", None))
    d = N.envelope(r, items=items)
    d["list"] = items
    d["entity"] = entity
    d["status"] = getattr(r, "status", 0)
    return d


def _cached_profile(
    app: Any,
    uid: str,
    cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]],
    *,
    fetch_on_miss: bool = True,
) -> Optional[Dict[str, Any]]:
    """Read one display profile through a short per-session cache."""
    target = str(uid or "").strip()
    if not target:
        return None
    now = time.monotonic()
    cached = cache.get(target)
    if cached:
        cached_at, profile = cached
        ttl = PROFILE_CACHE_TTL_SEC if profile else PROFILE_CACHE_ERROR_TTL_SEC
        if now - cached_at < ttl:
            return dict(profile) if profile else None
    if not fetch_on_miss:
        return None
    profile: Optional[Dict[str, Any]] = None
    try:
        result = app.profile.get_user(target)
        if getattr(result, "ok", False):
            profiles = N.normalize_users(getattr(result, "data", None))
            profile = next(
                (item for item in profiles if str(item.get("id") or "") == target),
                None,
            )
            if profile is None:
                # Some payloads omit an id for a single requested profile. That
                # shape is safe to use, but an explicit different id must never
                # be attached to this conversation as the peer's avatar.
                idless = [
                    item
                    for item in profiles
                    if not str(item.get("id") or "").strip()
                ]
                profile = (
                    idless[0]
                    if len(profiles) == 1 and len(idless) == 1
                    else None
                )
    except Exception:
        profile = None
    cache[target] = (now, dict(profile) if profile else None)
    if len(cache) > 200:
        oldest = min(cache, key=lambda key: cache[key][0])
        if oldest != target:
            cache.pop(oldest, None)
    return dict(profile) if profile else None


def conversation_envelope(
    app: Any,
    result: Any,
    profile_cache: Optional[
        Dict[str, tuple[float, Optional[Dict[str, Any]]]]
    ] = None,
) -> Dict[str, Any]:
    """Attach cached peer display data without delaying the conversation summary."""
    payload = RE(result, "conversation")
    cache = profile_cache if profile_cache is not None else {}
    for item in payload["items"]:
        peer = str(item.get("peer_id") or item.get("conversation_user") or "").strip()
        if not peer or item.get("avatar"):
            continue
        profile = _cached_profile(app, peer, cache, fetch_on_miss=False)
        if not profile:
            continue
        avatar = str(profile.get("avatar") or profile.get("portrait") or "")
        if avatar:
            item["avatar"] = avatar
        current_name = str(item.get("nickname") or "").strip()
        if not current_name or current_name in {"用户", peer, f"用户 {peer}"}:
            item["nickname"] = str(
                profile.get("nickname") or profile.get("name") or current_name or peer
            )
        existing_user = item.get("user") if isinstance(item.get("user"), dict) else {}
        item["user"] = {**profile, **existing_user}
    payload["list"] = payload["items"]
    return payload


def _is_false_response(r: Any, *accepted: str) -> bool:
    """Match explicit false/no business responses without weakening global parsing."""
    status = int(getattr(r, "status", 0) or 0)
    if not 200 <= status < 300:
        return False
    raw = str(getattr(r, "raw", "") or "").strip().lower()
    data = getattr(r, "data", None)
    values = {str(value).strip().lower() for value in accepted if str(value).strip()}
    return (data is False and "false" in values) or raw in values


def room_top_envelope(r: Any) -> Dict[str, Any]:
    """Mirror the APK: getRoomTop=false means no recommendation, not a page error."""
    payload = RE(r, "room")
    if _is_false_response(r, "false"):
        payload.update(
            ok=True,
            code="",
            message="当前没有推荐房间",
            error=None,
            items=[],
            list=[],
            count=0,
            availability="empty",
        )
    return payload


def room_create_envelope(r: Any) -> Dict[str, Any]:
    """Explain the legacy createRoom0 no/false response in room-specific terms."""
    payload = R(r, include_value=True)
    if _is_false_response(r, "false", "no"):
        message = "当前账号暂时无法创建语音房"
        payload.update(
            ok=False,
            code="ROOM_CREATE_UNAVAILABLE",
            message=message,
            availability="unavailable",
            outcome="rejected",
            upstream_code=str(getattr(r, "code", "") or ""),
            error={
                "title": "暂时无法创建语音房",
                "detail": "服务端未开放本次建房请求，可能受账号权限、房间资格或业务开关限制；服务端没有返回更具体原因。",
                "action": "none",
                "code": "ROOM_CREATE_UNAVAILABLE",
                "message": message,
            },
        )
    return payload


def roomkit_list_envelope(
    r: Any,
    session_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Expose RoomKit room metadata without leaking its Authorization or IM token."""
    payload = RE(r, "room")
    payload["source"] = "roomkit"
    payload["roomkit_session"] = dict(session_status or {})
    if payload.get("ok"):
        payload["availability"] = "active" if payload["items"] else "empty"
        payload["message"] = (
            f"已读取 {len(payload['items'])} 个客户端语音房"
            if payload["items"]
            else "客户端房间服务当前没有返回语音房"
        )
        return payload

    upstream_code = str(payload.get("code") or "")
    if upstream_code == "ROOMKIT_LOGIN_REQUIRED":
        title = "请先登录"
        detail = "登录乐园账号后才能建立独立的客户端房间服务会话。"
    elif upstream_code == "ROOMKIT_AUTH_MISSING":
        title = "客户端房间授权不可用"
        detail = "房间服务没有签发独立授权信息，暂时无法读取原生语音房列表。"
    else:
        title = "客户端房间服务暂时不可用"
        detail = "当前未能连接或登录客户端使用的独立房间服务，旧版房间榜单仍可继续使用。"
    payload.update(
        ok=False,
        code="ROOMKIT_UNAVAILABLE",
        message=title,
        availability="unavailable",
        upstream_code=upstream_code,
        error={
            "title": title,
            "detail": detail,
            "action": "none",
            "code": "ROOMKIT_UNAVAILABLE",
            "message": title,
        },
    )
    return payload


def RM(r: Any, entity: str, current_uid: str = "") -> Dict[str, Any]:
    """Dynamic-feed envelope with ownership flags and false-as-empty semantics."""
    d = RE(r, entity)
    uid = str(current_uid or "")
    for item in d["items"]:
        item["is_self"] = bool(uid and str(item.get("author_id") or "") == uid)
    if getattr(r, "data", None) is False or str(getattr(r, "raw", "")).strip().lower() == "false":
        d.update(ok=True, code="", message="", error=None, items=[], list=[], count=0)
    return d


class Handler(BaseHTTPRequestHandler):
    server_version = "bbw-app/3.0"

    def handle_one_request(self) -> None:
        """Release a per-user request lock after every HTTP request."""
        self._held_user_lock = None
        try:
            super().handle_one_request()
        finally:
            lock = self._held_user_lock
            self._held_user_lock = None
            if lock is not None:
                lock.release()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _allow_sensitive_action(
        self,
        scope: str,
        identity: str,
        *,
        limit: int,
        window_sec: float,
    ) -> bool:
        peer = str(self.client_address[0] if self.client_address else "unknown")
        normalized = str(identity or "-").strip().lower()[:80]
        allowed, retry = _rate_allowed(
            f"{scope}:{peer}:{normalized}", limit, window_sec
        )
        if allowed:
            return True
        self.ok(
            {
                "ok": False,
                "error": "请求过于频繁，请稍后再试",
                "retry_after": retry,
            },
            429,
        )
        return False

    def _cors(self) -> None:
        """Emit CORS headers only for an explicitly allowlisted Origin."""
        origin = _normalize_origin(self.headers.get("Origin") or "")
        if not origin or origin not in CORS_ALLOW_ORIGINS:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")

    def _origin_allowed(self) -> bool:
        """Accept same-origin requests and an optional exact CORS allowlist."""
        raw_origin = self.headers.get("Origin") or ""
        origin = _normalize_origin(raw_origin)
        if raw_origin:
            if not origin:
                return False
            if origin in CORS_ALLOW_ORIGINS:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            parsed = urlparse(origin)
            return bool(host and parsed.scheme in ("http", "https") and parsed.netloc.lower() == host)
        # Modern browsers expose cross-site navigations even when an intermediary
        # strips Origin.  Non-browser clients normally omit Sec-Fetch-Site entirely.
        return (self.headers.get("Sec-Fetch-Site") or "").lower() != "cross-site"

    def _check_api_origin(self) -> bool:
        if self._origin_allowed():
            return True
        self.ok({"ok": False, "error": "cross-origin request rejected"}, 403)
        return False

    def _send(
        self,
        status: int,
        body: bytes,
        ct: str,
        *,
        set_cookie: Optional[str] = None,
        clear_cookie: bool = False,
        cache_control: Optional[str] = None,
    ) -> None:
        try:
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy",
                "camera=(self), microphone=(self), geolocation=(self), payment=(), usb=()",
            )
            req_path = urlparse(self.path).path
            if cache_control is None:
                cache_control = "no-store" if req_path.startswith("/api/") else "no-cache"
            self.send_header("Cache-Control", cache_control)
            if req_path.startswith("/api/"):
                self.send_header("Pragma", "no-cache")
            if ct.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self'; img-src 'self' data: blob: https:; "
                    "media-src 'self' data: blob: https:; "
                    "connect-src 'self' https: wss:; "
                    # tim-js-sdk@2.27.6 creates its WebSocket and timer workers
                    # from blob: URLs. Blocking them leaves login() pending even
                    # when a page-level WebSocket probe reports success.
                    "worker-src 'self' blob:; child-src 'self' blob:; "
                    "object-src 'none'; base-uri 'self'; "
                    "frame-ancestors 'none'; form-action 'self'",
                )
            if set_cookie:
                secure = "; Secure" if COOKIE_SECURE else ""
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={set_cookie}; Path=/; HttpOnly; SameSite=Strict{secure}",
                )
            if clear_cookie:
                secure = "; Secure" if COOKIE_SECURE else ""
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}",
                )
            self.end_headers()
            # HEAD must not include a body (RFC 9110).
            if getattr(self, "command", "GET") == "HEAD":
                return
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Browser navigated away / aborted the request mid-response.
            return

    def ok(self, obj: Any, status: int = 200, **kw: Any) -> None:
        st, body, ct = _json_bytes(obj, status)
        self._send(st, body, ct, **kw)

    def body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw_length)
        except (TypeError, ValueError) as e:
            raise RequestBodyError(400, "invalid Content-Length") from e
        if n < 0:
            raise RequestBodyError(400, "invalid Content-Length")
        if n > MAX_JSON_BODY_BYTES:
            raise RequestBodyError(413, f"JSON body exceeds {MAX_JSON_BODY_BYTES} bytes")
        if n <= 0:
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise RequestBodyError(415, "Content-Type must be application/json")
        try:
            raw = self.rfile.read(n)
            if len(raw) != n:
                raise RequestBodyError(400, "incomplete request body")
            d = json.loads(raw.decode("utf-8"))
        except RequestBodyError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RequestBodyError(400, "invalid JSON body") from e
        if not isinstance(d, dict):
            raise RequestBodyError(400, "JSON body must be an object")
        return d

    def flash_multipart_body(self) -> Tuple[Dict[str, str], F.UploadPart]:
        """Read the one bounded multipart route used by flash-photo upload."""
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise RequestBodyError(400, "invalid Content-Length") from exc
        if length <= 0:
            raise RequestBodyError(400, "请选择闪图图片")
        if length > F.MAX_FLASH_MULTIPART_BYTES:
            raise RequestBodyError(413, "闪图文件过大")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestBodyError(400, "incomplete request body")
        try:
            return F.parse_multipart(self.headers.get("Content-Type") or "", raw)
        except F.FlashPhotoError as exc:
            raise RequestBodyError(exc.status, exc.message) from exc

    def sid(self) -> Optional[str]:
        """Read the browser credential from the HttpOnly cookie only."""
        raw = self.headers.get("Cookie") or ""
        if raw:
            c = SimpleCookie()
            try:
                c.load(raw)
                if COOKIE_NAME in c:
                    return c[COOKIE_NAME].value
            except Exception:
                pass
        return None

    def user(self, sid: Optional[str]):
        assert STORE is not None
        try:
            user = STORE.require(sid)
            if self._held_user_lock is None:
                user.lock.acquire()
                self._held_user_lock = user.lock
            return user
        except KeyError:
            self.ok({"ok": False, "error": "请先登录"}, 401)
            return None

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            return self.ok({"ok": False, "error": "cross-origin request rejected"}, 403)
        self._send(204, b"", "text/plain; charset=utf-8", cache_control="no-store")

    def do_HEAD(self) -> None:  # noqa: N802
        """Browsers / probes sometimes HEAD static assets; BaseHTTPRequestHandler defaults to 501."""
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        assert STORE is not None
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        q = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731

        if path in ("/", "/index.html"):
            return self.static("index.html")
        if path.startswith("/static/"):
            return self.static(path[len("/static/") :])

        if path.startswith("/api/") and not self._check_api_origin():
            return

        if path == "/api/health":
            return self.ok(
                {
                    "ok": True,
                    "service": "bbw-app",
                    "version": 3,
                    **STORE.stats(),
                    "features": _features(),
                    "capabilities": {
                        "roomkit_list": True,
                        "invite_login": INVITE_LOGIN_ENABLED,
                    },
                    "lab_enabled": LAB_ENABLED,
                }
            )

        if path == "/api/features":
            return self.ok(
                {
                    "ok": True,
                    "features": _features(),
                    "capabilities": {
                        "roomkit_list": True,
                        "invite_login": INVITE_LOGIN_ENABLED,
                    },
                    "lab_enabled": LAB_ENABLED,
                    "auto_heartbeat": STORE.auto_heartbeat,
                }
            )

        if path == "/api/actions" and not LAB_ENABLED:
            return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)

        if path == "/api/sessions":
            if not LAB_ENABLED:
                return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)
            current = self.user(self.sid())
            if not current:
                return
            return self.ok({"ok": True, "sessions": STORE.list_public()})

        sid = self.sid()

        if path == "/api/me":
            u = STORE.get(sid)
            if not u:
                return self.ok({"ok": False, "logged_in": False}, 401)
            with u.lock:
                if str(u.app.session.nickname or "").strip() in {"", "用户", "游客"}:
                    _enrich_session_profile(u)
                pub = u.public()
                user_dto = N.session_user_dto(u.app.whoami())
            return self.ok(
                {
                    "ok": True,
                    **pub,
                    "user": user_dto,
                    "features": _features(),
                    "capabilities": {
                        "roomkit_list": True,
                        "invite_login": INVITE_LOGIN_ENABLED,
                    },
                    "lab_enabled": LAB_ENABLED,
                    "auto_heartbeat": STORE.auto_heartbeat,
                }
            )

        u = self.user(sid)
        if not u:
            return
        app = u.app

        # ---- dashboard ----
        if path in ("/api/app/home", "/api/home"):
            gifts = app.content.gift_list()
            rec = app.content.recommend()
            who = N.session_user_dto(app.whoami())
            return self.ok(
                {
                    "ok": True,
                    "user": who,
                    "gifts": RG(gifts),
                    # Tuijiannew(type=getSlide) returns recommendation banners,
                    # not user profiles. Keep the entity explicit so banner ids
                    # are never rendered as usernames/UIDs by the Web client.
                    "recommend": RE(rec, "slide"),
                    "heartbeat": u.heartbeat.status() if u.heartbeat else {"running": False},
                }
            )

        if path == "/api/app/bootstrap":
            batch = {
                k: {"ok": v.ok, "code": v.code, "message": v.message}
                for k, v in app.bootstrap().items()
            }
            try:
                tim = {
                    "ok": True,
                    **u.native.im.tim_login_payload(
                        prefer=_tim_preference(q("prefer", "local")),
                        allow_local_fallback=True,
                    ),
                }
            except Exception as e:
                tim = {"ok": False, "error": _safe_error(e, "消息登录凭证获取失败")}
            u.persist()
            return self.ok(
                {
                    "ok": True,
                    "user": N.session_user_dto(app.whoami()),
                    "batch": batch,
                    "tim": tim,
                }
            )

        # ---- content / square ----
        if path == "/api/gifts":
            return self.ok(RG(app.content.gift_list()))
        if path == "/api/recommend":
            return self.ok(RE(app.content.recommend(q("type", "getSlide")), "slide"))
        if path == "/api/slide":
            return self.ok(RE(app.content.slide(q("slidesort", q("sort", "1"))), "slide"))
        if path == "/api/topics":
            return self.ok(RE(app.content.topic(q("q", "")), "topic"))
        if path == "/api/moments/posts":
            tab = q("tab", "推荐")
            cursor = q("cursor", q("page", "1"))
            current_uid = str(app.session.uid or "")
            target_uid = str(q("uid", "") or "").strip()
            if target_uid or tab == "我的":
                page = str(q("page", cursor) or "1").strip()
                if not page.isdigit() or not 1 <= int(page) <= 100000:
                    return self.ok({"ok": False, "error": "动态页码无效"}, 400)
                if target_uid:
                    if target_uid == "0" or not target_uid.isdigit() or len(target_uid) > 32:
                        return self.ok({"ok": False, "error": "用户 UID 无效"}, 400)
                    result = app.social.profile_posts(target_uid, page=page)
                else:
                    target_uid = current_uid
                    result = app.social.user_posts(current_uid, page=page)
                payload = RM(result, "post", current_uid)
                payload.update(
                    feed_type="user",
                    target_uid=target_uid,
                    page=page,
                    next_page=str(int(page) + 1) if payload.get("items") else "",
                )
                return self.ok(payload)
            else:
                allowed_tabs = {"推荐", "附近", "最新", "招募令", "关注"}
                if tab not in allowed_tabs:
                    return self.ok({"ok": False, "error": "不支持的动态分类"}, 400)
                region = "不限"
                if tab == "附近":
                    raw_user = getattr(app.session, "raw_user", {}) or {}
                    region = _profile_region(raw_user, current_uid)
                    if not region:
                        profile = _enrich_session_profile(u)
                        raw_user = getattr(app.session, "raw_user", {}) or {}
                        region = _profile_region(raw_user, current_uid) or _profile_region(profile, current_uid)
                    if not region:
                        return self.ok(_nearby_location_missing())
                result = app.social.posts(
                    tab,
                    cursor,
                    filter_gender=q("gender", "不限"),
                    filter_search=q("search", ""),
                    filter_property=q("property", "不限"),
                    filter_region=region,
                )
            payload = RM(result, "post", current_uid)
            if tab == "附近":
                payload["location_region"] = region
            return self.ok(payload)
        if path == "/api/moments/comments":
            postid = q("postid", q("post_id", ""))
            author_id = q("author_id", q("authid", ""))
            if not postid or not author_id:
                return self.ok({"ok": False, "error": "缺少动态或作者编号"}, 400)
            result = app.social.main_comments(
                postid,
                author_id,
                hide_comment=q("hide_comment", "0"),
                page=q("page", "1"),
            )
            return self.ok(RM(result, "comment", str(app.session.uid or "")))
        if path == "/api/ads":
            return self.ok(R(app.content.is_show_ad(), include_value=True))
        if path == "/api/censor":
            return self.ok(R(app.content.chat_censorship(), include_value=True))
        if path == "/api/referral":
            return self.ok(R(app.content.referral(), include_value=True))
        if path == "/api/version":
            return self.ok(R(app.content.version(), include_value=True))

        # ---- profile ----
        if path == "/api/profile/me":
            r = app.profile.get_me()
            items = N.normalize_users(r.data)
            current_uid = str(app.session.uid or "")
            profile_user = next(
                (item for item in items if str(item.get("id") or "") == current_uid),
                items[0] if items else None,
            )
            if profile_user and profile_user.get("avatar"):
                # Some login responses omit the portrait while the profile API has it.
                # Cache it in the session so every later /api/* user DTO stays complete.
                app.session.portrait = str(profile_user["avatar"])
            u.persist()
            who = N.session_user_dto(app.whoami(), profile_user)
            return self.ok({**R(r), "user": who, "items": items, "list": items})
        if path == "/api/profile/user":
            return self.ok(
                RL(
                    app.profile.get_user(
                        q("uid") or app.session.uid,
                        lat=q("lat", q("latitude", "0")),
                        lng=q("lng", q("longitude", "0")),
                    )
                )
            )
        if path == "/api/profile/reset-num":
            return self.ok(R(app.profile.reset_num(q("type", "昵称")), include_value=True))
        if path == "/api/profile/etiquette":
            return self.ok(R(app.profile.etiquette(), include_value=True))

        # ---- social ----
        if path == "/api/social/follows":
            # getFollowUser contains display profiles; getFollowList mostly
            # contains relationship ids and therefore renders numeric names.
            primary = app.social.follow_users(q("uid") or None, page=q("page", "1"))
            payload = RS(primary, str(app.session.uid or ""), app)
            if payload.get("items"):
                return self.ok(payload)
            return self.ok(
                RS(
                    app.social.follow_list(q("uid") or None),
                    str(app.session.uid or ""),
                    app,
                )
            )
        if path == "/api/social/fans":
            return self.ok(
                RS(
                    app.social.fans_users(q("uid") or None, page=q("page", "1")),
                    str(app.session.uid or ""),
                    app,
                )
            )
        if path == "/api/social/follow-list":
            return self.ok(RL(app.social.follow_list(q("uid") or q("id") or None)))
        if path == "/api/social/friend-apply":
            result = app.social.friend_apply_list(q("page", "1"))
            result, items = _friend_applications(app, str(app.session.uid or ""), result)
            payload = N.envelope(result, items=items)
            payload["list"] = items
            payload["status"] = result.status
            return self.ok(payload)
        if path == "/api/social/friends":
            result = app.social.friends()
            items = N.normalize_friends(result.data, str(app.session.uid or ""))
            payload = N.envelope(result, items=items)
            payload["list"] = items
            payload["status"] = result.status
            return self.ok(payload)
        if path == "/api/social/visitors":
            visit_type = q("type", "seen_me")
            page = q("page", q("pageindex", "0"))
            if visit_type == "seen_me":
                result = app.social.viewed_me(page)
            elif visit_type == "seen_by_me":
                result = app.social.i_viewed(page)
            else:
                return self.ok(
                    {"ok": False, "error": "type 仅支持 seen_me 或 seen_by_me"},
                    400,
                )
            return self.ok(RL(result))
        if path == "/api/social/blacklist":
            return self.ok(RL(app.social.my_blacklist()))
        if path == "/api/social/blacklist-me":
            return self.ok(RL(app.social.blacklist_me()))

        # ---- match ----
        if path == "/api/match/status":
            cards = app.call("getMyCard", uid=app.session.uid)
            nums = app.call("getMatchNum", uid=app.session.uid)
            who = N.session_user_dto(app.whoami())
            status = N.normalize_match_status(cards.data, nums.data, who)
            raw_user = getattr(app.session, "raw_user", {}) or {}
            if not isinstance(raw_user, dict):
                raw_user = {}
            filters = N.normalize_match_filters(raw_user)
            filters["properties"] = _saved_match_properties(
                raw_user, filters["property"]
            )
            return self.ok(
                {
                    "ok": True,
                    "user": who,
                    "status": status,
                    "display": status["display"],
                    "filters": filters,
                    # keep raw only for lab debugging if needed
                    "cards_ok": cards.ok,
                    "nums_ok": nums.ok,
                }
            )
        if path == "/api/match/online-users":
            profile = N.normalize_user(
                getattr(app.session, "raw_user", {}) or {}
            ) or {}
            return self.ok(
                RL(
                    app.match.online_users(
                        id=app.session.uid,
                        gender=profile.get("sex") or q("gender"),
                        property=profile.get("property") or q("property"),
                        pageIndex=q("page", q("pageIndex", "1")),
                    )
                )
            )
        if path == "/api/match/bottles":
            return self.ok(RE(app.match.my_bottles(uid=app.session.uid), "bottle"))

        # ---- tasks ----
        if path == "/api/tasks":
            items, create, have = _load_tasks(app)
            return self.ok(
                {
                    "ok": True,
                    "items": items,
                    "count": len(items),
                    "create": RT(create),
                    "have": RT(have),
                }
            )

        # ---- room ----
        if path == "/api/room/top":
            return self.ok(room_top_envelope(app.room.top()))
        if path == "/api/room/auth":
            return self.ok(R(app.room.auth(app.session.uid), include_value=True))
        if path == "/api/room/tips":
            return self.ok(R(app.room.tips(q("type", "1")), include_value=True))
        if path == "/api/room/songs":
            return self.ok(RE(app.room.song_list(q("room_id", "")), "song"))
        if path == "/api/room/user":
            return self.ok(R(app.room.get_user_room_info(q("uid") or None), include_value=True))

        # ---- wallet ----
        if path == "/api/wallet":
            me = app.profile.get_me()
            u.persist()
            myg = app.economy.my_gifts()
            glist = app.economy.gift_list()
            return self.ok(
                {
                    "ok": True,
                    "user": N.session_user_dto(app.whoami()),
                    "me": R(me),
                    "my_gifts": RG(myg),
                    "gift_shop": RG(glist),
                    "pay": u.native.pay.capabilities(),
                    "pay_notice": "下单成功只表示拿到支付参数，不等于资金到账；请在官方收银台完成支付。",
                }
            )

        # ---- im ----
        if path == "/api/im/tim":
            # Prefer server UserSig (tximsign.php puts sig in message=).
            # Fall back to a BFF-local mint using the server-side configured secret.
            try:
                prefer = _tim_preference(q("prefer", "server"))
                payload = u.native.im.tim_login_payload(
                    prefer=prefer,
                    allow_local_fallback=True,
                )
                if not payload.get("userSig") or not payload.get("userID"):
                    return self.ok(
                        {
                            "ok": False,
                            "error": {
                                "title": "消息登录凭证不完整",
                                "detail": "缺少用户标识或登录签名，请重新登录后再试",
                            },
                        },
                        400,
                    )
                # Never echo full userSig length into logs; client gets it once.
                return self.ok(
                    {
                        "ok": True,
                        **payload,
                        "sig_len": len(str(payload.get("userSig") or "")),
                    }
                )
            except Exception as e:
                return self.ok(
                    {"ok": False, "error": _safe_error(e, "消息登录凭证获取失败")},
                    400,
                )
        if path == "/api/im/rong":
            c = u.native.im.rong_register()
            return self.ok({"ok": c.ok, **c.to_dict()})
        if path == "/api/im/rest/health":
            # Proves APK-derived secret works against Tencent REST (not browser WSS).
            sample = str(app.session.uid or "1")
            return self.ok(u.native.tim_rest.health(sample_uid=sample))
        if path == "/api/im/rest/online":
            uid = q("uid") or app.session.uid or ""
            r = u.native.tim_rest.query_online([uid] if uid else ["1"])
            return self.ok(r.to_dict(), 200 if r.ok else 400)
        if path == "/api/im/presence":
            requested = _presence_uids((qs.get("uids") or []) + (qs.get("uid") or []))
            if not requested:
                return self.ok({"ok": False, "error": "缺少用户 UID"}, 400)
            try:
                result = u.native.tim_rest.query_online(requested)
                return self.ok(_normalize_presence_result(result, requested))
            except Exception:
                items = [
                    {
                        "uid": uid,
                        "status": "unknown",
                        "label": "状态未知",
                        "is_online": None,
                    }
                    for uid in requested
                ]
                return self.ok(
                    {
                        "ok": False,
                        "items": items,
                        "list": items,
                        "count": len(items),
                        "message": "在线状态暂时不可用",
                    }
                )
        if path == "/api/im/bootstrap":
            try:
                return self.ok(
                    u.native.im.bootstrap(
                        prefer_tim=_tim_preference(q("prefer", "local")),
                        allow_local_fallback=True,
                    )
                )
            except Exception as e:
                return self.ok(
                    {"ok": False, "error": _safe_error(e, "IM 凭证获取失败")},
                    400,
                )
        if path == "/api/im/stickers":
            return self.ok(RE(app.im.stickers(), "sticker"))
        if path == "/api/im/conversations":
            return self.ok(
                conversation_envelope(
                    app,
                    app.im.history_conversations(q("page", "1")),
                    u.profile_cache,
                )
            )
        if path == "/api/im/messages":
            peer = q("peer") or q("uid") or q("yourid")
            if not peer:
                return self.ok({"ok": False, "error": "缺少聊天对象 UID"}, 400)
            return self.ok(RE(app.im.history_messages(peer), "message"))

        if path == "/api/heartbeat":
            return self.ok(u.heartbeat.status() if u.heartbeat else {"running": False})
        if path == "/api/face/status":
            return self.ok(u.native.face.status_hint())
        if path == "/api/pay/capabilities":
            return self.ok(u.native.pay.capabilities())
        if path == "/api/misc/online":
            return self.ok(R(app.misc.update_online()))

        # generic catalog peek
        if path == "/api/actions":
            if not LAB_ENABLED:
                return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)
            cat = q("cat") or None
            acts = app.list_actions(cat)
            return self.ok({"ok": True, "count": len(acts), "actions": acts[:500]})

        self.ok({"ok": False, "error": "not found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        assert STORE is not None
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._check_api_origin():
            return
        if path in {"/api/call", "/api/call-redis", "/api/face/manual"} and not LAB_ENABLED:
            return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)
        flash_upload: Optional[F.UploadPart] = None
        try:
            if path == "/api/im/flash/send":
                data, flash_upload = self.flash_multipart_body()
            else:
                data = self.body()
        except RequestBodyError as e:
            return self.ok({"ok": False, "error": e.message}, e.status)
        sid = self.sid()

        # ---- auth ----
        if path == "/api/auth/login":
            phone = str(data.get("phone") or "").strip()
            password = str(data.get("password") or "")
            mode = str(data.get("mode") or "password")
            if not phone:
                return self.ok({"ok": False, "error": "请输入手机号"}, 400)
            if not self._allow_sensitive_action(
                "login", phone, limit=8, window_sec=300.0
            ):
                return
            try:
                if mode == "onekey":
                    if not LAB_ENABLED:
                        return self.ok(
                            {"ok": False, "error": "弱一键登录在产品模式下已禁用"},
                            403,
                        )
                    user = STORE.login_onekey(sid, phone, label=str(data.get("label") or ""))
                else:
                    if not password:
                        return self.ok({"ok": False, "error": "请输入密码"}, 400)
                    user = STORE.login_password(
                        sid, phone, password, label=str(data.get("label") or "")
                    )
            except Exception as e:
                return self.ok({"ok": False, "error": _safe_error(e, "登录失败")}, 400)
            profile_result = None
            try:
                bootstrap_result = user.app.bootstrap()
                profile_result = bootstrap_result.get("me")
            except Exception:
                pass
            _enrich_session_profile(user, profile_result)
            return self.ok({"ok": True, **user.public()}, set_cookie=user.web_sid)

        if path == "/api/auth/logout":
            if sid:
                user = STORE.get(sid)
                if user:
                    persisted_uid = user.app.session.uid
                    remote_ok = False
                    try:
                        with user.lock:
                            remote = user.app.auth.logout()
                            remote_ok = bool(getattr(remote, "ok", False))
                    except Exception:
                        remote_ok = False
                    finally:
                        STORE.drop(
                            sid,
                            delete_persisted=True,
                            persisted_uid=persisted_uid,
                        )
                    return self.ok(
                        {"ok": True, "remote_logout_ok": remote_ok}, clear_cookie=True
                    )
            return self.ok({"ok": True, "remote_logout_ok": False}, clear_cookie=True)

        if path == "/api/auth/sms-send":
            from bbw_protocol import BeibeiwuApp

            phone = str(data.get("phone") or "").strip()
            if not phone:
                return self.ok({"ok": False, "error": "请输入手机号"}, 400)
            if not self._allow_sensitive_action(
                "sms-send", phone, limit=3, window_sec=300.0
            ):
                return
            return self.ok(R(BeibeiwuApp().auth.send_sms(phone), empty_ok=True))

        if path == "/api/auth/sms-login":
            from bbw_protocol.device import build_device_profile

            phone = str(data.get("phone") or "").strip()
            code = str(data.get("code") or "").strip()
            if not phone or not code:
                return self.ok({"ok": False, "error": "需要手机号和验证码"}, 400)
            if not self._allow_sensitive_action(
                "sms-login", phone, limit=8, window_sec=300.0
            ):
                return
            try:
                user = STORE.get(sid)
                created = user is None
                if user is None:
                    user = STORE.create(label=phone)
                with user.lock:
                    user.app.session.apply_device(build_device_profile(seed=phone))
                    r = user.app.auth.sms_login(phone, code)
                    if not r.ok or not user.app.session.logged_in:
                        if created:
                            STORE.drop(user.web_sid)
                        return self.ok(
                            {**R(r), "ok": False, "error": r.message or "登录失败"},
                            400,
                        )
                    user.app.session.password = ""
                    _enrich_session_profile(user)
                    STORE.rotate_sid(user)
                    user.persist()
                    if STORE.auto_heartbeat:
                        user.start_heartbeat(STORE.heartbeat_interval)
                return self.ok({"ok": True, **user.public()}, set_cookie=user.web_sid)
            except Exception as e:
                return self.ok({"ok": False, "error": _safe_error(e, "短信登录失败")}, 400)

        u = self.user(sid)
        if not u:
            return
        app = u.app

        # Financial mutations are protected only after the cookie has resolved to an
        # authenticated WebUser.  The key includes the exact SID, route and complete
        # parsed JSON object, so another account, route or payload remains independent.
        if path in FINANCIAL_PATHS and not _mutation_allowed(str(sid), path, data):
            duplicate_message = "检测到短时间内的重复提交，请先确认上一次结果"
            return self.ok(
                {
                    "ok": False,
                    "code": "DUPLICATE_REQUEST",
                    "message": duplicate_message,
                    "error": duplicate_message,
                },
                409,
            )

        try:
            # heartbeat
            if path == "/api/heartbeat/start":
                interval = float(data.get("interval_sec") or STORE.heartbeat_interval)
                interval = max(15.0, min(300.0, interval))
                return self.ok(
                    {
                        "ok": True,
                        **u.start_heartbeat(interval),
                    }
                )
            if path == "/api/heartbeat/stop":
                u.stop_heartbeat()
                return self.ok({"ok": True, "running": False})
            if path == "/api/heartbeat/once":
                if not u.heartbeat:
                    from bbw_protocol.heartbeat import Heartbeat

                    u.heartbeat = Heartbeat(app)
                return self.ok(u.heartbeat.once())

            if path == "/api/call":
                if not LAB_ENABLED:
                    return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)
                action = str(data.get("action") or "")
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                if not action:
                    return self.ok({"ok": False, "error": "action required"}, 400)
                return self.ok(R(app.call(action, **params), lab=True))

            if path == "/api/call-redis":
                if not LAB_ENABLED:
                    return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)
                action = str(data.get("action") or "")
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                return self.ok(R(app.call_redis(action, **params), lab=True))

            # social writes
            if path == "/api/social/follow":
                return self.ok(
                    R(
                        app.social.follow(
                            str(data.get("uid") or data.get("you") or ""),
                            quietly=str(data.get("quietly_follow") or data.get("quietly") or "1"),
                        )
                    )
                )
            if path == "/api/social/unfollow":
                return self.ok(R(app.social.unfollow(str(data.get("uid") or data.get("you") or ""))))
            if path == "/api/social/agree-friend":
                current_uid = str(app.session.uid or "")
                applicant_uid = str(data.get("uid") or "").strip()
                apply_id = str(data.get("apply_id") or data.get("id") or "").strip()
                candidates = list(dict.fromkeys(value for value in (applicant_uid, apply_id) if value))
                if not candidates:
                    return self.ok({"ok": False, "error": "缺少好友申请编号"}, 400)
                result = None
                verified = False
                pending = True
                for candidate in candidates:
                    result = app.social.agree_friend(candidate)
                    accepted = _accepted_friend_ids(app, current_uid)
                    if applicant_uid and applicant_uid in accepted:
                        verified = True
                        pending = False
                        break
                    _, remaining = _friend_applications(app, current_uid)
                    pending = any(
                        str(item.get("id") or "") == applicant_uid
                        or str(item.get("apply_id") or "") == apply_id
                        for item in remaining
                    )
                    if not pending:
                        verified = True
                        break
                payload = R(result)
                payload["verified"] = verified
                if verified:
                    payload.update(ok=True, message="已同意好友申请", error=None)
                elif getattr(result, "ok", False):
                    payload.update(
                        ok=False,
                        message="好友申请状态未更新，请刷新后重试",
                        error={"title": "好友申请状态未更新", "detail": "服务端仍返回这条申请"},
                    )
                return self.ok(payload)
            if path == "/api/social/delete-friend":
                return self.ok(R(app.social.delete_friend(**_params(data))))
            if path == "/api/social/visit":
                target_uid = str(data.get("uid") or data.get("yourid") or "").strip()
                if not target_uid:
                    return self.ok({"ok": False, "error": "缺少对方 UID"}, 400)
                return self.ok(R(app.social.record_profile_view(target_uid)))

            # TIM REST fallback (when browser TIM.login hangs)
            if path == "/api/im/rest/send":
                message_type = str(
                    data.get("message_type") or data.get("type") or "text"
                ).strip().lower()
                if message_type not in {"", "text", "timtextelem"}:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "MEDIA_REQUIRES_TIM_SDK",
                            "message": "富媒体需要实时消息组件在线，文本备用通道不会上传本地文件",
                            "error": "富媒体需要实时消息组件在线，文本备用通道不会上传本地文件",
                        },
                        409,
                    )
                to_uid = str(data.get("to") or data.get("peer") or data.get("uid") or "").strip()
                text = str(data.get("text") or data.get("message") or "").strip()
                if not to_uid or not text:
                    return self.ok({"ok": False, "error": "需要对方 UID 与消息内容"}, 400)
                from_uid = str(app.session.uid or "").strip()
                if not from_uid:
                    return self.ok({"ok": False, "error": "当前会话无 uid"}, 400)
                r = u.native.tim_rest.send_text(from_uid, to_uid, text)
                # Best-effort: also mirror into banghua history if action exists.
                hist = None
                try:
                    hist = R(
                        app.im.history_message_insert(
                            from_id=from_uid,
                            to_id=to_uid,
                            content=text,
                            type="text",
                        )
                    )
                except Exception:
                    hist = None
                out = r.to_dict()
                out["from"] = from_uid
                out["to"] = to_uid
                if isinstance(r.data, dict):
                    msg_key = str(
                        r.data.get("MsgKey")
                        or r.data.get("msg_key")
                        or r.data.get("MsgUID")
                        or r.data.get("msg_uid")
                        or ""
                    ).strip()
                    if msg_key:
                        out["msg_key"] = msg_key
                        out["message_id"] = msg_key
                if hist is not None:
                    out["history_mirror"] = hist
                if r.ok:
                    out["message"] = "已通过文本备用通道发送"
                return self.ok(out, 200 if r.ok else 400)

            if path == "/api/im/rest/revoke":
                to_uid = str(data.get("to") or data.get("peer") or data.get("uid") or "").strip()
                msg_key = str(
                    data.get("msg_key")
                    or data.get("MsgKey")
                    or data.get("message_id")
                    or ""
                ).strip()
                from_uid = str(app.session.uid or "").strip()
                if not from_uid:
                    return self.ok({"ok": False, "error": "当前会话无 uid"}, 400)
                if not to_uid or not msg_key:
                    return self.ok({"ok": False, "error": "缺少对方 UID 或消息标识"}, 400)
                if len(to_uid) > 128 or len(msg_key) > 512:
                    return self.ok({"ok": False, "error": "消息标识不合法"}, 400)
                result = u.native.tim_rest.revoke_c2c(from_uid, to_uid, msg_key)
                out = result.to_dict()
                out.update({"from": from_uid, "to": to_uid, "msg_key": msg_key})
                if result.ok or int(result.error_code or 0) == 20023:
                    out["ok"] = True
                    out["already_revoked"] = not result.ok
                    out["message"] = "消息已撤回"
                    return self.ok(out)
                if int(result.error_code or 0) == 20022:
                    out["error"] = {
                        "title": "消息无法撤回",
                        "detail": "消息不存在，或已经不在当前应用的漫游存储期内",
                    }
                return self.ok(out, 400)

            if path == "/api/im/flash/send":
                if flash_upload is None:
                    return self.ok({"ok": False, "error": "请选择闪图图片"}, 400)
                if not self._allow_sensitive_action(
                    "flash-send",
                    str(app.session.uid or sid or "-"),
                    limit=12,
                    window_sec=60.0,
                ):
                    return
                uploaded: Optional[Dict[str, Any]] = None
                try:
                    target_id = F.validate_peer(
                        data.get("peer")
                        or data.get("targetId")
                        or data.get("target_id")
                        or data.get("to")
                    )
                    uploaded = F.upload_image(app.im, flash_upload.data)
                    try:
                        result = app.im.flash_photo_send(
                            target_id=target_id,
                            photo_url=str(uploaded["path"]),
                        )
                    except Exception as exc:
                        F.delete_image(app.im, str(uploaded["path"]))
                        raise F.FlashPhotoError("闪图发送失败", status=502) from exc
                    payload = R(result, empty_ok=True)
                    if not payload.get("ok"):
                        F.delete_image(app.im, str(uploaded["path"]))
                    payload["target_id"] = target_id
                    unique_id = F.result_value(result, "uniqueid", "uniqueId", "unique_id")
                    if unique_id:
                        payload["uniqueid"] = unique_id
                    if payload.get("ok"):
                        payload.update(
                            {
                                "path": uploaded["path"],
                                "url": uploaded["url"],
                                "photo_url": uploaded["url"],
                                "content_type": uploaded["content_type"],
                                "size": uploaded["size"],
                            }
                        )
                        payload["message"] = payload.get("message") or "闪图已发送"
                    return self.ok(payload, 200 if payload.get("ok") else 502)
                except F.FlashPhotoError as exc:
                    return self.ok({"ok": False, "error": exc.message}, exc.status)

            if path == "/api/im/flash/get":
                if not self._allow_sensitive_action(
                    "flash-get",
                    str(app.session.uid or sid or "-"),
                    limit=60,
                    window_sec=60.0,
                ):
                    return
                try:
                    unique_id = F.validate_unique_id(
                        data.get("uniqueid") or data.get("uniqueId") or data.get("unique_id")
                    )
                    result = app.im.flash_photo_get(uniqueid=unique_id)
                    payload = R(result)
                    photo_status = F.result_value(
                        result,
                        "photostatus",
                        "photoStatus",
                        "photo_status",
                    )[:100]
                    if photo_status and photo_status != "0":
                        expired_message = "闪图已查看、已失效或不存在"
                        payload.update(
                            {
                                "ok": False,
                                "message": expired_message,
                                "error": expired_message,
                                "uniqueid": unique_id,
                                "photo_status": photo_status,
                            }
                        )
                        return self.ok(payload, 410)
                    photo = F.result_photo(result)
                    payload.update(
                        {
                            "uniqueid": unique_id,
                            "path": photo["path"],
                            "url": photo["url"],
                            "photo_url": photo["url"],
                        }
                    )
                    if photo_status:
                        payload["photo_status"] = photo_status
                    if payload.get("ok") and photo["url"]:
                        payload["message"] = "闪图已获取"
                        payload["error"] = None
                        return self.ok(payload)
                    if getattr(result, "status", -1) < 0:
                        payload.update(ok=False, error="闪图服务暂时不可用")
                        return self.ok(payload, 502)
                    payload.update(ok=False, error="闪图已查看、已失效或不存在")
                    return self.ok(payload, 410)
                except F.FlashPhotoError as exc:
                    return self.ok({"ok": False, "error": exc.message}, exc.status)

            if path == "/api/social/blacklist-add":
                return self.ok(R(app.social.add_blacklist(**_params(data))))
            if path == "/api/social/blacklist-del":
                return self.ok(R(app.social.delete_blacklist(**_params(data))))
            if path == "/api/social/like-post":
                return self.ok(
                    R(
                        app.social.luntan_like(
                            str(data.get("postid") or data.get("post_id") or data.get("id") or "")
                        )
                    )
                )
            if path == "/api/moments/comment":
                text = str(data.get("text") or data.get("comment_text") or "").strip()
                postid = str(data.get("postid") or data.get("post_id") or "").strip()
                post_owner = str(data.get("post_owner") or data.get("author_id") or "").strip()
                if not text or not postid or not post_owner:
                    return self.ok({"ok": False, "error": "评论内容、动态编号和作者编号不能为空"}, 400)
                if len(text) > 500:
                    return self.ok({"ok": False, "error": "评论不能超过 500 字"}, 400)
                return self.ok(
                    R(
                        app.social.send_comment(
                            text,
                            postid,
                            post_owner,
                            main_id=str(data.get("main_id") or "0"),
                            main_owner=str(data.get("main_owner") or "0"),
                            sub_id=str(data.get("sub_id") or "0"),
                            sub_comment=str(data.get("sub_comment") or "0"),
                            author_reply=str(data.get("author_reply") or "0"),
                        ),
                        empty_ok=True,
                    )
                )
            if path == "/api/moments/comment-like":
                comment_id = str(data.get("comment_id") or data.get("id") or "").strip()
                if not comment_id:
                    return self.ok({"ok": False, "error": "缺少评论编号"}, 400)
                return self.ok(
                    R(
                        app.social.comment_like(
                            comment_id,
                            str(data.get("liked") or data.get("ifauthlike") or "0"),
                        ),
                        empty_ok=True,
                    )
                )
            if path == "/api/moments/comment-delete":
                comment_id = str(data.get("comment_id") or data.get("id") or "").strip()
                if not comment_id:
                    return self.ok({"ok": False, "error": "缺少评论编号"}, 400)
                return self.ok(R(app.social.delete_comment(comment_id), empty_ok=True))
            if path == "/api/moments/comment-forbid":
                comment_id = str(data.get("comment_id") or data.get("id") or "").strip()
                if not comment_id:
                    return self.ok({"ok": False, "error": "缺少评论编号"}, 400)
                return self.ok(R(app.social.forbid_comment(comment_id), empty_ok=True))
            if path == "/api/moments/post-delete":
                postid = str(data.get("postid") or data.get("id") or "").strip()
                if not postid:
                    return self.ok({"ok": False, "error": "缺少动态编号"}, 400)
                return self.ok(R(app.social.delete_post(postid), empty_ok=True))
            if path == "/api/moments/post-visibility":
                postid = str(data.get("postid") or data.get("id") or "").strip()
                scope = str(data.get("scope") or data.get("visibility_scope") or "").strip()
                if not postid or scope not in {"公开", "仅好友可见", "好友及粉丝可见", "仅自己可见"}:
                    return self.ok({"ok": False, "error": "动态编号或可见范围无效"}, 400)
                return self.ok(R(app.social.change_post_visibility(postid, scope), empty_ok=True))
            if path == "/api/moments/post-pin":
                postid = str(data.get("postid") or data.get("id") or "").strip()
                if not postid:
                    return self.ok({"ok": False, "error": "缺少动态编号"}, 400)
                return self.ok(R(app.social.toggle_profile_pin(postid), empty_ok=True))
            if path == "/api/moments/publish":
                text = str(data.get("text") or data.get("posttext") or "").strip()
                if not text:
                    return self.ok({"ok": False, "error": "动态内容不能为空"}, 400)
                if len(text) > 2000:
                    return self.ok({"ok": False, "error": "动态内容不能超过 2000 字"}, 400)
                scope = str(data.get("visibility_scope") or "公开").strip()
                if scope not in {"公开", "仅好友可见", "好友及粉丝可见", "仅自己可见"}:
                    return self.ok({"ok": False, "error": "可见范围无效"}, 400)
                plate = str(data.get("plate") or "动态").strip()
                if plate not in {"动态", "招募令"}:
                    return self.ok({"ok": False, "error": "动态分类无效"}, 400)
                topic = str(data.get("topic") or "").strip()
                topics = json.dumps([{"topic": topic}], ensure_ascii=False) if topic else ""
                return self.ok(
                    R(
                        app.social.publish_post(
                            text,
                            title=str(data.get("title") or "").strip(),
                            plate=plate,
                            visibility_scope=scope,
                            comment_forbid="1" if _as_bool(data.get("comment_forbid")) else "0",
                            hide_comment="1" if _as_bool(data.get("hide_comment")) else "0",
                            topics=topics,
                        ),
                        empty_ok=True,
                    )
                )
            if path == "/api/social/report":
                return self.ok(
                    R(
                        app.social.report(
                            str(data.get("type") or "user"),
                            str(data.get("itemid") or ""),
                            str(data.get("reason") or "web report"),
                        )
                    )
                )

            # profile
            if path == "/api/profile/nick":
                r = app.profile.reset_nickname(str(data.get("name") or data.get("nickname") or ""))
                u.persist()
                return self.ok({**R(r), "user": N.session_user_dto(app.whoami())})
            if path == "/api/profile/reset":
                r = app.profile.reset_personal(
                    str(data.get("value") or ""),
                    type_=str(data.get("type") or data.get("type_") or "昵称设置"),
                    as_form=_as_bool(data.get("as_form")),
                )
                u.persist()
                return self.ok(R(r))
            if path == "/api/profile/privacy":
                return self.ok(R(app.profile.set_privacy(**_params(data))))

            # match
            if path in {"/api/match/online", "/api/match/local"}:
                raw_user = getattr(app.session, "raw_user", {}) or {}
                if not isinstance(raw_user, dict):
                    raw_user = {}
                    app.session.raw_user = raw_user
                current_filters = N.normalize_match_filters(raw_user)
                gender = str(data.get("gender") or current_filters["gender"]).strip()
                if gender not in N.MATCH_GENDERS:
                    return self.ok({"ok": False, "error": "匹配性别无效"}, 400)

                saved_properties = _saved_match_properties(
                    raw_user, current_filters["property"]
                )
                requested_properties = data.get("properties")
                if requested_properties is None:
                    requested_properties = data.get("property") or saved_properties
                property_values = _match_property_values(requested_properties)
                if not property_values or any(
                    value not in N.MATCH_PROPERTIES for value in property_values
                ):
                    return self.ok({"ok": False, "error": "匹配属性无效"}, 400)

                selected = set(property_values)
                properties = [
                    value for value in N.MATCH_PROPERTY_ORDER if value in selected
                ]

                try:
                    cursor = int(raw_user.get("_web_match_properties_cursor") or 0)
                except (TypeError, ValueError):
                    cursor = 0
                if properties != saved_properties:
                    cursor = 0
                active_property = properties[cursor % len(properties)]
                raw_user["_web_match_properties_cursor"] = (cursor + 1) % len(
                    properties
                )

                # The APK persists each radio selection through resetMatch(value),
                # then starts matching with the account's own gender/property. Web
                # multi-select therefore rotates one selected native value per click,
                # in 双 -> Z -> B order, without issuing extra match requests.
                if gender != current_filters["gender"]:
                    app.match.set_filter(gender)
                if active_property != current_filters["property"]:
                    app.match.set_filter(active_property)

                raw_user["match_gender"] = gender
                raw_user["match_property"] = active_property
                raw_user["_web_match_properties"] = properties
                profile = N.normalize_user(raw_user) or {}
                params = {
                    "id": app.session.uid,
                    "gender": str(profile.get("sex") or ""),
                    "property": str(profile.get("property") or ""),
                }
                result = (
                    app.match.online_one(**params)
                    if path == "/api/match/online"
                    else app.match.local_one(**params)
                )
                payload = N.normalize_match_result(result)
                payload["filters"] = {
                    "gender": gender,
                    "property": active_property,
                    "properties": properties,
                }
                payload["active_property"] = active_property
                if len(properties) > 1:
                    payload["filter_strategy"] = "round_robin"
                return self.ok(payload)
            if path == "/api/match/remove":
                return self.ok(
                    R(
                        app.match.remove(
                            str(data.get("type") or "1"),
                            id_=app.session.uid,
                        )
                    )
                )
            if path == "/api/match/bottle-throw":
                text = str(
                    data.get("leave_word")
                    or data.get("content")
                    or data.get("message")
                    or data.get("text")
                    or ""
                ).strip()
                if not text:
                    return self.ok({"ok": False, "error": "请输入漂流瓶内容"}, 400)
                if len(text) > 160:
                    return self.ok({"ok": False, "error": "漂流瓶内容不能超过 160 个字符"}, 400)
                # APK creates a new bottle by adding its first leave word.  The
                # ThrowADriftBottle action is reserved for returning a picked
                # bottle to the pool and is not the compose endpoint.
                return self.ok(R(app.match.bottle_leave_word(leave_word=text)))
            if path == "/api/match/bottle-pick":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(RE(app.match.pick_bottle(**params), "bottle"))
            if path == "/api/match/bottle-delete":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(R(app.match.delete_bottle(**params)))
            if path == "/api/match/dating-publish":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(R(app.match.publish_dating(**params)))
            if path == "/api/match/dating-apply":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(R(app.match.apply_dating(**params)))
            if path == "/api/match/dating-cancel":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(R(app.match.cancel_dating(**params)))

            # tasks
            if path == "/api/tasks/receive":
                tid = str(data.get("id") or data.get("task_id") or "")
                if not tid:
                    return self.ok({"ok": False, "error": "缺少任务编号"}, 400)
                r = app.call("receiveHotActivityList", id=tid)
                if str(getattr(r, "kind", "") or "") == "empty" and 200 <= int(
                    getattr(r, "status", 0) or 0
                ) < 300:
                    items, _, _ = _load_tasks(app)
                    return self.ok(task_receive_envelope(r, tid, items))
                payload = R(r)
                if payload.get("ok"):
                    items, create, have = _load_tasks(app)
                    if items or getattr(create, "ok", False) or getattr(have, "ok", False):
                        _attach_task_snapshot(payload, tid, items)
                return self.ok(payload)

            # room
            if path == "/api/room/native-list":
                if not self._allow_sensitive_action(
                    "roomkit-list",
                    str(app.session.uid or sid or "-"),
                    limit=8,
                    window_sec=60.0,
                ):
                    return
                try:
                    page = max(1, int(data.get("page") or 1))
                    size = max(1, min(50, int(data.get("size") or 10)))
                except (TypeError, ValueError):
                    return self.ok({"ok": False, "error": "房间列表分页参数无效"}, 400)
                result = u.native.roomkit.rooms(
                    page=page,
                    size=size,
                    force_login=_as_bool(data.get("force_login")),
                )
                payload = roomkit_list_envelope(
                    result,
                    u.native.roomkit.public_status(),
                )
                return self.ok(payload, 200 if payload.get("ok") else 502)
            if path == "/api/room/create":
                return self.ok(
                    room_create_envelope(
                        app.room.create(
                            str(data.get("type") or data.get("audioroomtype") or "处CP"),
                            my_id=app.session.uid,
                        )
                    )
                )
            if path == "/api/room/set":
                return self.ok(R(app.room.set(**_params(data))))
            if path == "/api/room/finish":
                return self.ok(R(app.room.finish(**_params(data))))
            if path == "/api/room/song-add":
                return self.ok(
                    R(
                        app.room.add_song(
                            str(data.get("room_id") or data.get("roomId") or ""),
                            str(data.get("music_id") or data.get("musicId") or ""),
                        )
                    )
                )
            if path == "/api/room/ktv-search":
                return self.ok(
                    RE(
                        app.room.ktv_search(
                            str(data.get("key_word") or data.get("keyword") or data.get("q") or ""),
                            page=str(data.get("page") or data.get("pageIndex") or "1"),
                        ),
                        "song",
                    )
                )
            if path == "/api/room/rtc-token":
                return self.ok(
                    R(
                        app.room.rtc_token(
                            str(data.get("channel") or data.get("channelName") or ""),
                            uid=app.session.uid,
                        ),
                        include_value=True,
                    )
                )

            # wallet / economy
            if path == "/api/pay/coin":
                ch = (data.get("channel") or "wechat").lower()
                if ch not in {"wechat", "alipay"}:
                    return self.ok({"ok": False, "error": "不支持的支付渠道"}, 400)
                cid = str(data.get("coin_id") or "1")
                res = (
                    u.native.pay.prepare_coin_alipay(cid)
                    if ch == "alipay"
                    else u.native.pay.prepare_coin_wechat(cid)
                )
                d = res.to_dict()
                d["product_notice"] = (
                    "仅创建支付订单参数，不等于充值到账。请在微信/支付宝官方收银台完成支付后刷新余额。"
                )
                if d.get("ok"):
                    d["message"] = d.get("message") or "订单参数已生成（未支付）"
                else:
                    d["error"] = N.explain_error(
                        str(d.get("code") or ""),
                        str(d.get("message") or ""),
                        str(d.get("raw") or "")[:200],
                    )
                return self.ok(d)
            if path == "/api/pay/vip":
                ch = (data.get("channel") or "wechat").lower()
                lv = str(data.get("level") or "vip")
                if ch not in {"wechat", "alipay"}:
                    return self.ok({"ok": False, "error": "不支持的支付渠道"}, 400)
                if lv not in {"vip", "svip"}:
                    return self.ok({"ok": False, "error": "不支持的会员等级"}, 400)
                vipid = str(data.get("vipid") or data.get("vip_id") or "").strip()
                if not vipid:
                    return self.ok({"ok": False, "error": "请选择有效的会员商品编号"}, 400)
                extra = _params(data)
                for key in ("channel", "level", "vipid", "vip_id"):
                    extra.pop(key, None)
                res = (
                    u.native.pay.prepare_vip_alipay(lv, vipid=vipid, **extra)
                    if ch == "alipay"
                    else u.native.pay.prepare_vip_wechat(lv, vipid=vipid, **extra)
                )
                d = res.to_dict()
                d["product_notice"] = "仅创建会员订单参数，不等于开通成功。"
                return self.ok(d)
            if path == "/api/pay/card":
                res = u.native.pay.buy_match_card(str(data.get("card_id") or "1"))
                d = res.to_dict()
                if not d.get("ok"):
                    d["error"] = N.explain_error(
                        str(d.get("code") or ""),
                        str(d.get("message") or ""),
                        "",
                    )
                return self.ok(d)
            if path == "/api/wallet/svip-try":
                return self.ok(R(app.economy.svip_try(app.session.uid)))
            if path == "/api/wallet/exchange-vip":
                try:
                    vip_id = int(data.get("vip_id") or data.get("vipid") or 5)
                except (TypeError, ValueError):
                    return self.ok({"ok": False, "error": "会员商品编号无效"}, 400)
                if vip_id <= 0:
                    return self.ok({"ok": False, "error": "会员商品编号无效"}, 400)
                return self.ok(
                    R(
                        app.economy.money_exchange_vip(
                            vip_id,
                            coupon_id=str(data.get("coupon_id") or data.get("couponid") or "0"),
                        )
                    )
                )
            if path == "/api/wallet/send-gift":
                return self.ok(R(app.economy.send_gift1(**_params(data))))
            if path == "/api/wallet/withdraw":
                alipay = str(data.get("alipay") or data.get("alilogonid") or "").strip()
                name = str(data.get("name") or data.get("aliname") or "").strip()
                amount = str(data.get("amount") or data.get("transamount") or "").strip()
                try:
                    amount_number = float(amount)
                except (TypeError, ValueError):
                    amount_number = 0.0
                if not alipay or not name:
                    return self.ok({"ok": False, "error": "支付宝账号和真实姓名不能为空"}, 400)
                if not math.isfinite(amount_number) or amount_number <= 0:
                    return self.ok({"ok": False, "error": "请输入有效的提现金额"}, 400)
                return self.ok(
                    R(
                        app.economy.withdraw(
                            alipay,
                            name,
                            amount,
                            authid=app.session.uid,
                        )
                    )
                )

            # content
            if path == "/api/topics/create":
                return self.ok(R(app.content.create_topic(str(data.get("topic") or ""))))
            if path == "/api/referral/set":
                return self.ok(
                    R(
                        app.content.set_referral(
                            str(data.get("referral") or ""),
                            id_=app.session.uid,
                        )
                    )
                )

            # face
            if path == "/api/face/init":
                return self.ok(
                    u.native.face.start(
                        str(data.get("cert_name") or ""),
                        str(data.get("cert_no") or ""),
                        str(data.get("meta_info") or ""),
                    ).to_dict()
                )
            if path == "/api/face/describe":
                return self.ok(
                    u.native.face.describe(
                        certify_id=data.get("certify_id"),
                        cert_name=data.get("cert_name"),
                        cert_no=data.get("cert_no"),
                    ).to_dict()
                )
            if path == "/api/face/manual":
                if not LAB_ENABLED:
                    return self.ok({"ok": False, "error": "lab endpoint disabled"}, 404)
                return self.ok(
                    u.native.face.manual(
                        str(data.get("cert_name") or ""),
                        str(data.get("cert_no") or ""),
                        str(data.get("result") or "pending"),
                    )
                )

            if path == "/api/online":
                return self.ok(R(app.misc.update_online(first=_as_bool(data.get("first")))))
            if path == "/api/frontback":
                return self.ok(
                    R(app.misc.front_or_back(str(data.get("frontorback") or "1")))
                )

        except Exception as e:
            return self.ok({"ok": False, "error": _safe_error(e, "请求处理失败")}, 400)

        self.ok({"ok": False, "error": "not found", "path": path}, 404)

    def static(self, name: str) -> None:
        """Serve files under static/, including vendor/ subpaths (path-traversal safe)."""
        rel = Path(str(name or "").replace("\\", "/").lstrip("/"))
        if not rel.parts or ".." in rel.parts:
            self._send(404, b"missing", "text/plain")
            return
        path = (STATIC_DIR / rel).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(404, b"missing", "text/plain")
            return
        if not path.is_file():
            self._send(404, b"missing", "text/plain")
            return
        data = path.read_bytes()
        ct = "text/html; charset=utf-8"
        if path.suffix == ".js":
            ct = "application/javascript; charset=utf-8"
        elif path.suffix == ".css":
            ct = "text/css; charset=utf-8"
        elif path.suffix == ".txt":
            ct = "text/plain; charset=utf-8"
        elif path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            ct = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }[path.suffix]
        # Vendor SDK is large and immutable by version pin.  First-party HTML/JS
        # must be revalidated on every load: an older cached IM connector can keep
        # throwing before login even after the server-side file has been fixed.
        if "vendor" in rel.parts:
            cache = "public, max-age=86400"
        elif path.suffix in {".html", ".js"}:
            cache = "no-store"
        else:
            cache = "public, max-age=300"
        self._send(200, data, ct, cache_control=cache)


def _params(data: Dict[str, Any]) -> Dict[str, Any]:
    skip = {"action", "mode", "params"}
    out: Dict[str, Any] = {}
    nested = data.get("params")
    if isinstance(nested, dict):
        out.update({k: v for k, v in nested.items() if v is not None})
    out.update({k: v for k, v in data.items() if k not in skip and v is not None})
    return out


def _match_property_values(value: Any) -> List[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item or "").strip() for item in values]


def _saved_match_properties(raw_user: Dict[str, Any], fallback: str) -> List[str]:
    values = _match_property_values(raw_user.get("_web_match_properties"))
    selected = {value for value in values if value in N.MATCH_PROPERTIES}
    properties = [value for value in N.MATCH_PROPERTY_ORDER if value in selected]
    if properties:
        return properties
    return [fallback] if fallback in N.MATCH_PROPERTIES else ["双"]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _tim_preference(value: Any) -> str:
    """TIM UserSig source: server (tximsign.php) or local (BFF mint)."""
    requested = str(value or "server").strip().lower()
    if requested in {"local", "server"}:
        return requested
    return "server"


def _normalize_origin(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "null":
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _safe_error(exc: Exception, fallback: str) -> str:
    if LAB_ENABLED:
        return str(exc)[:300] or fallback
    return fallback


def _features() -> List[Dict[str, str]]:
    return [dict(item) for item in FEATURES if LAB_ENABLED or item.get("id") != "lab"]


FEATURES: List[Dict[str, str]] = [
    {"id": "nearby", "name": "身边", "desc": "在线用户、资料与聊天入口"},
    {"id": "msg", "name": "消息", "desc": "历史会话、未读数与受控实时聊天"},
    {"id": "match", "name": "匹配", "desc": "在线同城、漂流瓶与约会"},
    {"id": "moments", "name": "动态", "desc": "多分类动态流、点赞与评论"},
    {"id": "me", "name": "我的", "desc": "关系统计、资料、实名与设置"},
    {"id": "social", "name": "关系中心", "desc": "通讯录、申请、关注、粉丝、访客与黑名单"},
    {"id": "room", "name": "语音房", "desc": "旧版榜单、客户端房间列表与接口状态"},
    {"id": "wallet", "name": "钱包与会员", "desc": "礼物、会员、充值与提现"},
    {"id": "tasks", "name": "任务与奖励", "desc": "任务列表与奖励领取"},
    {"id": "lab", "name": "协议台", "desc": "任意 do= 调用"},
]


def main(argv=None) -> int:
    global COOKIE_SECURE, CORS_ALLOW_ORIGINS, LAB_ENABLED, MAX_JSON_BODY_BYTES, STORE
    ap = argparse.ArgumentParser(description="bbw product web app with optional lab mode")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--auto-heartbeat",
        action="store_true",
        help="run a server-side heartbeat thread per logged-in session; default follows browser visibility",
    )
    ap.add_argument("--no-heartbeat", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--ttl-days", type=float, default=7.0)
    ap.add_argument(
        "--enable-lab",
        action="store_true",
        help="enable protocol console, generic calls, session listing and manual face endpoint",
    )
    ap.add_argument(
        "--persist-sessions",
        action="store_true",
        help="persist reduced sessions (never password/raw_user); default is memory-only",
    )
    ap.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="exact cross-origin allowlist entry; repeat as needed (CORS is off by default)",
    )
    ap.add_argument("--max-json-kb", type=int, default=256)
    ap.add_argument(
        "--secure-cookie",
        action="store_true",
        help="add Secure to the SID cookie (use behind HTTPS)",
    )
    args = ap.parse_args(argv)
    LAB_ENABLED = bool(args.enable_lab)
    COOKIE_SECURE = bool(args.secure_cookie)
    MAX_JSON_BODY_BYTES = max(1, int(args.max_json_kb)) * 1024
    CORS_ALLOW_ORIGINS = {
        origin
        for raw in args.cors_origin
        for origin in [_normalize_origin(raw)]
        if origin
    }
    STORE = SessionStore(
        ttl_sec=args.ttl_days * 86400,
        auto_heartbeat=bool(args.auto_heartbeat and not args.no_heartbeat),
        persist_sessions=args.persist_sessions,
        allow_weak_onekey=args.enable_lab,
    )
    try:
        httpd = ExclusiveThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(
            f"无法启动 Web 服务：{args.host}:{args.port} 已被其他进程占用（{exc}）",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(f"小贝 Web App  http://{args.host}:{args.port}/", flush=True)
    print("  主导航: 身边/消息/匹配/动态/我的", flush=True)
    print(
        f"  模式: {'LAB' if LAB_ENABLED else 'PRODUCT'} · "
        f"session={'disk(sanitized)' if args.persist_sessions else 'memory-only'} · "
        f"cors={','.join(sorted(CORS_ALLOW_ORIGINS)) or 'off'}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        STORE.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
