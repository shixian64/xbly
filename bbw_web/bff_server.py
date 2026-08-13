#!/usr/bin/env python3
"""Product Web BFF — APK business surface with an opt-in research lab.

  python -m bbw_web --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web import normalize as N  # noqa: E402
from bbw_web import flash_photo as F  # noqa: E402
from bbw_web.dependency_health import (  # noqa: E402
    DependencyCircuitOpen,
    DependencyDeadlineExceeded,
    RequestDeadline,
)
from bbw_web.message_quote import encode_message_quote, normalize_message_quote  # noqa: E402
from bbw_web.providers import (  # noqa: E402
    ProviderAuthenticationRejected,
    ProviderUnavailable,
    ProviderUpstreamInterrupted,
)
from bbw_web.store import SessionStore  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "bbw_sid"
STORE: Optional[SessionStore] = None
PRESENCE_BACKEND: Any = None
LOGGER = logging.getLogger(__name__)

PRESENCE_LOCK_FREE_PATHS = frozenset(
    {
        "/api/frontback",
        "/api/heartbeat/start",
        "/api/heartbeat/stop",
        "/api/heartbeat/once",
    }
)
PEER_SCOPED_MUTATION_PATHS = frozenset({"/api/im/read"})


class _InteractiveDependencyFailure(RuntimeError):
    pass


def _request_lock_mode(method: str, path: str) -> str:
    normalized_method = str(method or "GET").upper()
    normalized_path = str(path or "").split("?", 1)[0]
    if normalized_path in PRESENCE_LOCK_FREE_PATHS:
        return "none"
    if normalized_path in PEER_SCOPED_MUTATION_PATHS:
        return "peer"
    if normalized_method in {"GET", "HEAD"}:
        return "read"
    # Unknown mutations stay account-exclusive during the gradual lock split.
    return "write"


def _budget_kwargs(timeout: Optional[float], deadline: Any) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    if timeout is not None:
        output["timeout"] = timeout
    if deadline is not None:
        output["deadline"] = deadline
    return output


def _dependency_retryable(result: Any) -> bool:
    if isinstance(result, Mapping):
        if bool(result.get("retryable")):
            return True
        try:
            mapped_status = int(result.get("status") or 0)
        except (TypeError, ValueError, OverflowError):
            mapped_status = 0
        return mapped_status in {-1, 408, 425, 429, 504} or mapped_status >= 500
    if bool(getattr(result, "retryable", False)):
        return True
    try:
        status = int(getattr(result, "status", 0) or getattr(result, "status_code", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    try:
        error_code = int(getattr(result, "error_code", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        error_code = 0
    return status == -1 or status in {408, 425, 429, 504} or status >= 500 or error_code == -1


def _dependency_call(
    method: Any,
    *args: Any,
    provider: str,
    domain: str,
    breakers: Any = None,
    timeout: Optional[float] = None,
    deadline: Any = None,
    request_id: str = "",
    **kwargs: Any,
) -> Any:
    if breakers is not None:
        breakers.before_call(provider, domain)
    started_at = time.monotonic()
    outcome = "exception"
    try:
        result = method(
            *args,
            **kwargs,
            **_budget_kwargs(timeout, deadline),
        )
        ok = bool(
            result.get("ok", False)
            if isinstance(result, Mapping)
            else getattr(result, "ok", False)
        )
        retryable = _dependency_retryable(result)
        outcome = "ok" if ok else "retryable_failure" if retryable else "rejected"
        if breakers is not None:
            if ok or not retryable:
                breakers.record_success(provider, domain)
            else:
                breakers.record_failure(provider, domain, retryable=True)
        return result
    except DependencyCircuitOpen:
        outcome = "short_circuit"
        raise
    except DependencyDeadlineExceeded:
        outcome = "deadline_exceeded"
        if breakers is not None:
            breakers.record_failure(provider, domain, retryable=True)
        raise
    except Exception:
        if breakers is not None:
            breakers.record_failure(provider, domain, retryable=True)
        raise
    finally:
        LOGGER.info(
            json.dumps(
                {
                    "event": "interactive_dependency_call",
                    "request_id": str(request_id or "")[:64],
                    "provider": provider,
                    "domain": domain,
                    "outcome": outcome,
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


def _flash_reveal_backend(method: str, *args: Any, default: Any = None) -> Any:
    backend = PRESENCE_BACKEND
    callback = getattr(backend, method, None) if backend is not None else None
    if not callable(callback):
        return default
    try:
        return callback(*args)
    except Exception:
        # Flash viewing must remain available when the short-lived Redis cache
        # is unavailable; the upstream one-time status remains authoritative.
        return default


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Reject a second local server instead of sharing the same Windows port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


LAB_ENABLED = False
INVITE_LOGIN_ENABLED = False
MOMENT_VIDEO_COMPAT_ENABLED = False
CORS_ALLOW_ORIGINS: Set[str] = set()
MAX_JSON_BODY_BYTES = 256 * 1024
COOKIE_SECURE = False
PROFILE_CACHE_TTL_SEC = 15 * 60.0
PROFILE_CACHE_ERROR_TTL_SEC = 30.0
PROFILE_CACHE_REFRESH_MIN_AGE_SEC = 60.0
MESSAGE_BLOCK_SNAPSHOT_TTL_SEC = 60.0
MESSAGE_BLOCK_SNAPSHOT_RETRY_SEC = 10.0
SYSTEM_CUSTOMER_SERVICE_UID = "1"
FRIEND_APPLICATION_PAGE_SIZE = 15
FRIEND_APPLICATION_SCAN_PAGES = 5
MOMENT_ID_CURSOR_TABS = frozenset({"推荐", "招募令", "关注"})
MOMENT_PAGE_CURSOR_TABS = frozenset({"附近", "最新"})
MOMENT_FEED_TABS = MOMENT_ID_CURSOR_TABS | MOMENT_PAGE_CURSOR_TABS
MOMENT_VIEW_TASK_TITLE_MARKERS = ("观看动态", "浏览动态")
MOMENT_VIEW_TASK_POLL_DELAYS_SEC = (0.0, 0.25, 0.75)
MOMENT_VIEW_TASK_DEADLINE_SEC = 55.0
MOMENT_VIEW_TASK_MAX_REPEAT_EVENTS = 200
MOMENT_VIEW_TASK_BATCH_SIZE = 10
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS: Dict[str, List[float]] = {}
MUTATION_LOCK = threading.Lock()
RECENT_MUTATIONS: Dict[str, float] = {}
FINANCIAL_PATHS = {
    "/api/wallet/withdraw",
}
VOICE_MATCH_ACTIVE_TTL_SEC = 10 * 60.0
VOICE_RONG_CREDENTIAL_TTL_SEC = 6 * 60 * 60.0
VOICE_WEB_SDK = {
    "imlib": "5.9.5",
    "rtc": "5.7.2",
    "call": "5.2.10",
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


def _voice_match_state(user: Any) -> Dict[str, Any]:
    value = getattr(user, "voice_match_state", None)
    if not isinstance(value, dict):
        value = {}
        setattr(user, "voice_match_state", value)
    state = str(value.get("state") or "idle")
    started_at = float(value.get("started_at") or 0.0)
    active = state in {"waiting", "matched", "calling"}
    stale = bool(active and started_at and time.time() - started_at >= VOICE_MATCH_ACTIVE_TTL_SEC)
    return {
        "state": "stale" if stale else state,
        "active": active and not stale,
        "stale": stale,
        "target": value.get("target") if isinstance(value.get("target"), dict) else None,
        "started_at": started_at,
        "updated_at": float(value.get("updated_at") or started_at or 0.0),
    }


def _set_voice_match_state(
    user: Any,
    state: str,
    *,
    target: Optional[Dict[str, Any]] = None,
    preserve_started: bool = False,
) -> Dict[str, Any]:
    previous = getattr(user, "voice_match_state", None)
    previous = previous if isinstance(previous, dict) else {}
    now = time.time()
    started_at = float(previous.get("started_at") or 0.0) if preserve_started else now
    if state == "idle":
        started_at = 0.0
    value = {
        "state": state,
        "target": dict(target) if isinstance(target, dict) else None,
        "started_at": started_at,
        "updated_at": now,
    }
    setattr(user, "voice_match_state", value)
    return _voice_match_state(user)


def _voice_rong_credentials(user: Any) -> Optional[Dict[str, str]]:
    value = getattr(user, "voice_rong_credentials", None)
    created_at = float(getattr(user, "voice_rong_credentials_at", 0.0) or 0.0)
    session = getattr(getattr(user, "app", None), "session", None)
    current_uid = str(getattr(session, "uid", "") or "").strip()
    if (
        not isinstance(value, dict)
        or not value.get("token")
        or str(value.get("user_id") or "") != current_uid
        or not created_at
        or time.time() - created_at >= VOICE_RONG_CREDENTIAL_TTL_SEC
    ):
        return None
    return {
        "app_key": str(value.get("app_key") or ""),
        "user_id": current_uid,
        "token": str(value.get("token") or ""),
        "nickname": str(value.get("nickname") or ""),
        "portrait": str(value.get("portrait") or ""),
    }


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


def _fetch_social_profile(
    app: Any,
    uid: str,
    *,
    timeout: Optional[float] = None,
    deadline: Any = None,
    breakers: Any = None,
    request_id: str = "",
) -> Optional[Dict[str, Any]]:
    try:
        result = _dependency_call(
            app.profile.get_user,
            uid,
            provider="beibeiwu",
            domain="profile",
            breakers=breakers,
            timeout=timeout,
            deadline=deadline,
            request_id=request_id,
        )
        if (
            not getattr(result, "ok", False)
            and _dependency_retryable(result)
            and (breakers is not None or timeout is not None or deadline is not None)
        ):
            raise _InteractiveDependencyFailure(
                "profile provider temporarily unavailable"
            )
        profiles = N.normalize_users(result.data)
        return next(
            (value for value in profiles if str(value.get("id") or "") == uid),
            profiles[0] if profiles else None,
        )
    except (
        DependencyCircuitOpen,
        DependencyDeadlineExceeded,
        _InteractiveDependencyFailure,
    ):
        raise
    except Exception:
        return None


def _enrich_social_profiles(
    app: Any,
    items: List[Dict[str, Any]],
    profile_cache: Optional[
        Dict[str, tuple[float, Optional[Dict[str, Any]]]]
    ] = None,
) -> List[Dict[str, Any]]:
    cache = profile_cache if profile_cache is not None else {}
    pending: List[tuple[Dict[str, Any], str]] = []
    resolved: Dict[str, Optional[Dict[str, Any]]] = {}
    for item in items[:20]:
        needs_profile = bool(item.pop("_needs_profile", False))
        uid = str(item.get("id") or "")
        if not needs_profile or not uid:
            continue
        profile = _cached_profile(app, uid, cache, fetch_on_miss=False)
        if profile:
            resolved[uid] = profile
        else:
            pending.append((item, uid))

    missing_uids = list(dict.fromkeys(uid for _item, uid in pending))
    if missing_uids:
        workers = min(4, len(missing_uids))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bbw-profile") as pool:
            profiles = list(pool.map(lambda uid: _fetch_social_profile(app, uid), missing_uids))
        cached_at = time.monotonic()
        for uid, profile in zip(missing_uids, profiles):
            resolved[uid] = profile
            cache[uid] = (cached_at, dict(profile) if profile else None)
        while len(cache) > 200:
            oldest = min(cache, key=lambda key: cache[key][0])
            cache.pop(oldest, None)

    for item, uid in pending:
        profile = resolved.get(uid)
        if not profile:
            continue
        item["nickname"] = profile.get("nickname") or item.get("nickname")
        item["avatar"] = profile.get("avatar") or item.get("avatar")
        item["city"] = profile.get("city") or item.get("city")
        item["signature"] = profile.get("signature") or item.get("signature")
        item["subtitle"] = profile.get("subtitle") or item.get("subtitle")
    for item in items[20:]:
        item.pop("_needs_profile", None)
    return items


def RS(
    r: Any,
    current_uid: str = "",
    app: Any = None,
    profile_cache: Optional[
        Dict[str, tuple[float, Optional[Dict[str, Any]]]]
    ] = None,
) -> Dict[str, Any]:
    """Follow/fans endpoint normalized as the peer, never the logged-in user."""
    items = N.normalize_social_users(getattr(r, "data", None), current_uid)
    if app is not None:
        items = _enrich_social_profiles(app, items, profile_cache)
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


def _friend_applications(
    app: Any,
    current_uid: str,
    result: Any = None,
    *,
    start_page: int = 1,
    scan_pages: int = FRIEND_APPLICATION_SCAN_PAGES,
) -> Tuple[Any, List[Dict[str, Any]], Dict[str, Any]]:
    """Return a bounded window of friend-application history across upstream pages."""
    first_page = max(1, int(start_page))
    page_limit = max(1, min(int(scan_pages), FRIEND_APPLICATION_SCAN_PAGES))
    primary = result or app.social.friend_apply_list(str(first_page))
    page_results: List[Tuple[int, Any]] = [(first_page, primary)]
    first_raw = N.extract_list(getattr(primary, "data", None))
    if len(first_raw) >= FRIEND_APPLICATION_PAGE_SIZE and page_limit > 1:
        pages = list(range(first_page + 1, first_page + page_limit))
        with ThreadPoolExecutor(
            max_workers=min(4, len(pages)), thread_name_prefix="bbw-friend-apply"
        ) as pool:
            results = list(pool.map(lambda page: app.social.friend_apply_list(str(page)), pages))
        page_results.extend(zip(pages, results))

    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    status_unknown = False
    next_page = ""
    scanned_pages = 0
    for page, page_result in page_results:
        if not getattr(page_result, "ok", False) and not _is_false_response(
            page_result, "false"
        ):
            next_page = str(page)
            break
        raw_items = N.extract_list(getattr(page_result, "data", None))
        scanned_pages += 1
        for item in N.normalize_friend_applications(
            getattr(page_result, "data", None), current_uid
        ):
            key = str(item.get("apply_id") or item.get("id") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            if not item.get("request_status_known"):
                status_unknown = True
            normalized.append(item)
        if len(raw_items) < FRIEND_APPLICATION_PAGE_SIZE:
            next_page = ""
            break
        next_page = str(page + 1)

    if status_unknown and normalized:
        accepted = _accepted_friend_ids(app, current_uid)
        if accepted:
            for item in normalized:
                if str(item.get("id") or "") not in accepted:
                    continue
                item.update(
                    request_status="accepted",
                    request_status_known=True,
                    is_pending=False,
                    is_accepted=True,
                    status="accepted",
                    status_label="已成为好友",
                    can_accept=False,
                    is_friend=True,
                )
    metadata = {
        "page": str(first_page),
        "next_page": next_page,
        "has_more": bool(next_page),
        "scanned_pages": scanned_pages,
        "count": len(normalized),
        **_friend_application_counts(normalized),
    }
    return primary, normalized, metadata


def _friend_application_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    incoming = [item for item in items if item.get("direction") == "incoming"]
    outgoing = [item for item in items if item.get("direction") == "outgoing"]
    pending = [item for item in items if item.get("status") == "pending"]
    pending_incoming = [item for item in incoming if item.get("status") == "pending"]
    accepted = [item for item in items if item.get("status") == "accepted"]
    return {
        "incoming_count": len(incoming),
        "outgoing_count": len(outgoing),
        "pending_count": len(pending),
        "pending_incoming_count": len(pending_incoming),
        "accepted_count": len(accepted),
    }


def _friend_application_summary(metadata: Dict[str, Any]) -> Dict[str, Any]:
    summary = dict(metadata)
    summary["total_count"] = int(metadata.get("count") or 0)
    summary["count"] = int(metadata.get("pending_incoming_count") or 0)
    return summary


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
    unknown_count = sum(1 for item in items if item.get("status") == "unknown")
    return {
        "ok": ok,
        "partial": unknown_count > 0,
        "unknown_count": unknown_count,
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
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bbw-task") as pool:
        create_future = pool.submit(
            app.call, "createHotActivityList", uid=app.session.uid
        )
        have_future = pool.submit(
            app.call, "haveHotActivityList", uid=app.session.uid
        )
        create = create_future.result()
        have = have_future.result()
    items = N.normalize_tasks(getattr(create, "data", None))
    if not items:
        items = N.normalize_tasks(getattr(have, "data", None))
    return items, create, have


def _moment_view_tasks(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks = []
    for item in items:
        title = "".join(str(item.get("title") or "").split())
        if any(marker in title for marker in MOMENT_VIEW_TASK_TITLE_MARKERS):
            tasks.append(item)
    return tasks


def _moment_view_task_remaining(task: Dict[str, Any]) -> Optional[int]:
    if task.get("is_claimed") or task.get("can_receive"):
        return 0
    try:
        progress = max(0.0, float(task.get("progress") or 0))
        total = max(0.0, float(task.get("total") or 0))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0, int(math.ceil(total - progress)))


def _moment_view_task_snapshot(
    tasks: List[Dict[str, Any]],
) -> Optional[Dict[str, int]]:
    snapshot: Dict[str, int] = {}
    for task in tasks:
        task_id = str(task.get("id") or "").strip()
        remaining = _moment_view_task_remaining(task)
        if not task_id or remaining is None or task_id in snapshot:
            return None
        snapshot[task_id] = remaining
    return snapshot


def _moment_view_task_max_remaining(snapshot: Dict[str, int]) -> int:
    return max(snapshot.values(), default=0)


def _moment_view_progress_change(
    before: Optional[Dict[str, int]],
    after: Optional[Dict[str, int]],
    expected_events: int,
) -> str:
    if before is None or after is None:
        return "unknown"
    active_ids = [task_id for task_id, remaining in before.items() if remaining > 0]
    if not active_ids:
        return "complete"
    deltas = []
    for task_id in active_ids:
        if task_id not in after:
            return "unknown"
        delta = before[task_id] - after[task_id]
        if delta < 0 or delta > expected_events:
            return "unknown"
        deltas.append(delta)
    return "advanced" if any(delta > 0 for delta in deltas) else "unchanged"


def _moment_view_event_submitted(result: Any) -> bool:
    if getattr(result, "ok", False):
        return True
    status = int(getattr(result, "status", 0) or 0)
    return str(getattr(result, "kind", "") or "") == "empty" and 200 <= status < 300


def _record_moment_view_events(
    app: Any,
    post_id: str,
    count: int,
    *,
    deadline: float,
) -> Tuple[int, int]:
    total = max(0, int(count))
    if total <= 0:
        return 0, 0
    attempted = 0
    accepted = 0
    for _index in range(total):
        if time.monotonic() >= deadline:
            break
        attempted += 1
        try:
            submitted = _moment_view_event_submitted(
                app.social.record_post_view(post_id)
            )
        except Exception:
            submitted = False
        if not submitted:
            break
        accepted += 1
    return attempted, accepted


def _moment_view_task_state(
    app: Any,
) -> Tuple[bool, List[Dict[str, Any]], Optional[Dict[str, int]]]:
    items, create, have = _load_tasks(app)
    available = bool(
        items
        or getattr(create, "ok", False)
        or getattr(have, "ok", False)
    )
    tasks = _moment_view_tasks(items)
    return available, tasks, _moment_view_task_snapshot(tasks)


def _poll_moment_view_task_progress(
    app: Any,
    before: Dict[str, int],
    expected_events: int,
    deadline: float,
) -> Tuple[
    bool,
    List[Dict[str, Any]],
    Optional[Dict[str, int]],
    str,
]:
    before_remaining = _moment_view_task_max_remaining(before)
    last_available = False
    last_tasks: List[Dict[str, Any]] = []
    last_snapshot: Optional[Dict[str, int]] = None
    last_change = "unknown"
    for delay in MOMENT_VIEW_TASK_POLL_DELAYS_SEC:
        if delay:
            if time.monotonic() + delay >= deadline:
                break
            time.sleep(delay)
        if time.monotonic() >= deadline:
            break
        try:
            available, tasks, snapshot = _moment_view_task_state(app)
        except Exception:
            continue
        if not available:
            continue
        candidate_change = _moment_view_progress_change(
            before,
            snapshot,
            expected_events,
        )
        if last_change not in {"advanced", "complete"} or candidate_change in {
            "advanced",
            "complete",
        }:
            last_available = True
            last_tasks = tasks
            last_snapshot = snapshot
            last_change = candidate_change
        progressed = (
            before_remaining - _moment_view_task_max_remaining(snapshot)
            if snapshot is not None
            else 0
        )
        if candidate_change == "complete" or (
            candidate_change == "advanced" and progressed >= expected_events
        ):
            break
    return last_available, last_tasks, last_snapshot, last_change


def _assist_moment_view_task(
    app: Any,
    post_id: str,
    *,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Best-effort completion of active view tasks using the APK's real PV event.

    The APK allows the same post to be reported again after it re-enters the
    visible area.  Before sending the remaining events, verify against the
    authoritative task list that repeated reports actually advance progress.
    """

    if deadline is None:
        deadline = time.monotonic() + MOMENT_VIEW_TASK_DEADLINE_SEC
    deadline_expired = time.monotonic() >= deadline
    if deadline_expired:
        state_available, tasks, snapshot = False, [], None
    else:
        try:
            state_available, tasks, snapshot = _moment_view_task_state(app)
        except Exception:
            state_available, tasks, snapshot = False, [], None
    payload: Dict[str, Any] = {
        "checked": state_available,
        "task_found": bool(tasks),
        "completed": False,
        "retryable": False,
        "state": (
            "deadline_reached"
            if deadline_expired
            else ("not_found" if state_available else "unavailable")
        ),
        "remaining_before": None,
        "remaining_after": None,
        "requested_repeat_views": 0,
        "successful_repeat_views": 0,
        "progress_delta": 0,
        "capped": False,
        "tasks": tasks,
    }
    if not state_available:
        payload["retryable"] = True
        return payload
    if not tasks:
        return payload

    if snapshot is None:
        payload.update(state="target_unknown", retryable=True)
        return payload
    remaining_before = _moment_view_task_max_remaining(snapshot)
    payload["remaining_before"] = remaining_before
    if remaining_before <= 0:
        payload.update(completed=True, state="already_complete", remaining_after=0)
        return payload

    repeat_budget = min(remaining_before, MOMENT_VIEW_TASK_MAX_REPEAT_EVENTS)
    payload["capped"] = remaining_before > repeat_budget
    current_snapshot = snapshot
    current_remaining = remaining_before
    remaining_budget = repeat_budget
    first_batch = True
    while current_remaining > 0 and remaining_budget > 0:
        if time.monotonic() >= deadline:
            payload.update(state="deadline_reached", retryable=True)
            break
        chunk_count = min(
            current_remaining,
            remaining_budget,
            1 if first_batch else MOMENT_VIEW_TASK_BATCH_SIZE,
        )
        attempted, successful = _record_moment_view_events(
            app,
            post_id,
            chunk_count,
            deadline=deadline,
        )
        payload["requested_repeat_views"] += attempted
        payload["successful_repeat_views"] += successful
        remaining_budget -= attempted
        if successful <= 0:
            payload.update(
                state=(
                    "deadline_reached"
                    if time.monotonic() >= deadline
                    else ("probe_failed" if first_batch else "partial")
                ),
                retryable=True,
            )
            break

        available, next_tasks, next_snapshot, change = (
            _poll_moment_view_task_progress(
                app,
                current_snapshot,
                successful,
                deadline,
            )
        )
        payload["tasks"] = next_tasks
        if not available or next_snapshot is None:
            payload.update(
                checked=False,
                state=(
                    "deadline_reached"
                    if time.monotonic() >= deadline
                    else "verification_unavailable"
                ),
                retryable=True,
            )
            break
        next_remaining = _moment_view_task_max_remaining(next_snapshot)
        payload["remaining_after"] = next_remaining
        payload["progress_delta"] = max(
            0,
            remaining_before - next_remaining,
        )
        progressed = current_remaining - next_remaining
        if change not in {"advanced", "complete"} or progressed <= 0:
            payload.update(state="verification_pending", retryable=True)
            break

        current_snapshot = next_snapshot
        current_remaining = next_remaining
        first_batch = False
        if current_remaining > 0 and progressed < successful:
            payload.update(state="verification_pending", retryable=True)
            break
        if successful < attempted or attempted < chunk_count:
            payload.update(state="partial", retryable=True)
            break

    if current_remaining <= 0:
        payload.update(
            completed=True,
            retryable=False,
            state="completed",
            remaining_after=0,
        )
    elif payload["state"] not in {
        "deadline_reached",
        "probe_failed",
        "partial",
        "verification_pending",
        "verification_unavailable",
    }:
        payload.update(
            state="capped" if payload["capped"] else "partial",
            retryable=True,
            remaining_after=current_remaining,
        )
    elif payload["remaining_after"] is None:
        payload["remaining_after"] = current_remaining
    return payload


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


DISCOVERY_AGE_RANGES: Dict[str, Tuple[Optional[int], Optional[int]]] = {
    "不限": (None, None),
    "": (None, None),
    "18-24": (18, 24),
    "25-34": (25, 34),
    "35-44": (35, 44),
    "45+": (45, None),
}
NEARBY_RADIUS_KM = 100.0


def _discovery_gender(value: Any) -> str:
    gender = str(value or "不限").strip()
    return gender if gender in N.MATCH_GENDERS else ""


def _discovery_property(value: Any) -> str:
    property_ = str(value or "不限").strip()
    return property_ if property_ == "不限" or property_ in N.MATCH_PROPERTIES else ""


def _discovery_age_range(value: Any) -> Optional[Tuple[Optional[int], Optional[int]]]:
    return DISCOVERY_AGE_RANGES.get(str(value or "不限").strip())


def _numeric_age(value: Any) -> Optional[int]:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if not digits:
        return None
    age = int(digits)
    return age if 1 <= age <= 120 else None


def _normalized_gender(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    if raw in {"男", "male", "m", "1"}:
        return "男"
    if raw in {"女", "female", "f", "2"}:
        return "女"
    return str(value or "").strip()


def _filter_discovery_users(
    items: List[Dict[str, Any]],
    *,
    current_uid: str,
    gender: str,
    property_: str,
    age_range: Tuple[Optional[int], Optional[int]],
) -> List[Dict[str, Any]]:
    minimum_age, maximum_age = age_range
    filtered: List[Dict[str, Any]] = []
    for item in items:
        if str(item.get("id") or "") == current_uid:
            continue
        if gender != "不限" and _normalized_gender(item.get("sex")) != gender:
            continue
        if property_ != "不限" and str(item.get("property") or "").strip() != property_:
            continue
        if minimum_age is not None or maximum_age is not None:
            age = _numeric_age(item.get("age"))
            if age is None:
                continue
            if minimum_age is not None and age < minimum_age:
                continue
            if maximum_age is not None and age > maximum_age:
                continue
        filtered.append(item)
    return filtered


def _city_key(value: Any) -> str:
    region = _clean_profile_region(value).casefold()
    for separator in ("/", "|", ",", "，", "-"):
        if separator in region:
            region = region.split(separator)[-1]
    region = "".join(region.split())
    for suffix in ("特别行政区", "自治州", "地区", "盟", "市"):
        if region.endswith(suffix) and len(region) > len(suffix):
            region = region[: -len(suffix)]
            break
    return region


def _same_city(left: Any, right: Any) -> bool:
    left_key = _city_key(left)
    right_key = _city_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or left_key in right_key or right_key in left_key


def _custom_city(value: Any) -> str:
    city = str(value or "").strip()
    if not city:
        return ""
    if len(city) > 40 or any(ord(char) < 32 for char in city):
        return ""
    return city


def _coordinate(value: Any, minimum: float, maximum: float) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _profile_coordinates(source: Any) -> Optional[Tuple[float, float]]:
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except Exception:
            return None
    if isinstance(source, dict):
        latitude = _coordinate(
            source.get("latitude") or source.get("lat") or source.get("poi_lat"),
            -90.0,
            90.0,
        )
        longitude = _coordinate(
            source.get("longitude")
            or source.get("lng")
            or source.get("lon")
            or source.get("poi_lng"),
            -180.0,
            180.0,
        )
        if latitude is not None and longitude is not None:
            return latitude, longitude
        for key in ("user", "userinfo", "userInfo", "userInfoList", "profile", "data", "json_obj"):
            nested = source.get(key)
            if isinstance(nested, (dict, list, str)):
                coordinates = _profile_coordinates(nested)
                if coordinates is not None:
                    return coordinates
    elif isinstance(source, list):
        for item in source:
            coordinates = _profile_coordinates(item)
            if coordinates is not None:
                return coordinates
    return None


def _raw_user_coordinates(data: Any) -> Dict[str, Tuple[float, float]]:
    coordinates: Dict[str, Tuple[float, float]] = {}
    for row in N.extract_list(data):
        user = N.normalize_user(row) or {}
        uid = str(user.get("id") or "")
        point = _profile_coordinates(row)
        if uid and point is not None:
            coordinates[uid] = point
    return coordinates


def _distance_km(left: Tuple[float, float], right: Tuple[float, float]) -> float:
    lat1, lon1 = (math.radians(value) for value in left)
    lat2, lon2 = (math.radians(value) for value in right)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _with_discovery_distance(item: Dict[str, Any], distance_km: float) -> Dict[str, Any]:
    updated = dict(item)
    distance = f"{distance_km:.1f} 公里" if distance_km < 10 else f"{distance_km:.0f} 公里"
    updated["distance"] = distance
    parts = [
        f"UID {updated.get('id')}" if updated.get("id") else "",
        str(updated.get("role") or ""),
        str(updated.get("city") or ""),
        distance,
        str(updated.get("signature") or "")[:24],
    ]
    updated["subtitle"] = " · ".join(part for part in parts if part)
    return updated


def _current_profile_region(web_user: Any) -> str:
    app = web_user.app
    current_uid = str(app.session.uid or "")
    raw_user = getattr(app.session, "raw_user", {}) or {}
    region = _profile_region(raw_user, current_uid)
    if region:
        return region
    profile = _enrich_session_profile(web_user)
    raw_user = getattr(app.session, "raw_user", {}) or {}
    return _profile_region(raw_user, current_uid) or _profile_region(profile, current_uid)


def _nearby_people_location_missing(capabilities: Dict[str, bool]) -> Dict[str, Any]:
    return {
        "ok": False,
        "code": "NEARBY_LOCATION_REQUIRED",
        "message": "需要获取位置信息",
        "error": {
            "title": "需要获取位置信息",
            "detail": "资料中没有配置城市，请允许浏览器获取当前位置后继续。",
            "action": "request_location",
            "code": "NEARBY_LOCATION_REQUIRED",
            "message": "需要获取位置信息",
        },
        "items": [],
        "list": [],
        "count": 0,
        "location_required": True,
        "capabilities": capabilities,
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


def _is_html_protocol_result(result: Any) -> bool:
    headers = getattr(result, "headers", None)
    content_type = ""
    if isinstance(headers, dict):
        content_type = str(
            headers.get("content-type") or headers.get("Content-Type") or ""
        ).lower()
    prefix = str(getattr(result, "raw", "") or "").lstrip()[:256].lower()
    return "text/html" in content_type or prefix.startswith(("<!doctype html", "<html"))


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
    observed_at = time.time()
    for item in payload["items"]:
        item["unread_observed_at"] = observed_at
        item["unread_authoritative"] = True
        if item.get("last_message") and not item.get("preview_timestamp"):
            item["preview_source"] = item.get("source") or "history"
            item["preview_authoritative"] = False
            item["preview_timestamp_inferred"] = True
    cache = profile_cache if profile_cache is not None else {}
    _attach_cached_conversation_profiles(app, payload["items"], cache)
    payload["list"] = payload["items"]
    return payload


def _attach_cached_conversation_profiles(
    app: Any,
    items: List[Dict[str, Any]],
    cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]],
) -> None:
    for item in items:
        peer = str(item.get("peer_id") or item.get("conversation_user") or "").strip()
        if not peer:
            continue
        profile = _cached_profile(app, peer, cache, fetch_on_miss=False)
        if not profile:
            continue
        avatar = str(profile.get("avatar") or profile.get("portrait") or "")
        if avatar:
            item["avatar"] = avatar
        current_name = str(item.get("nickname") or "").strip()
        profile_name = str(profile.get("nickname") or profile.get("name") or "").strip()
        profile_name_resolved = bool(
            profile_name
            and profile_name not in {"用户", "游客", peer, f"用户 {peer}"}
        )
        if profile_name_resolved:
            item["nickname"] = profile_name
        elif not current_name or current_name in {"用户", "游客", peer, f"用户 {peer}"}:
            item["nickname"] = peer
        existing_user = item.get("user") if isinstance(item.get("user"), dict) else {}
        item["user"] = {**existing_user, **profile}
        item["profile_resolved"] = bool(avatar and profile_name_resolved)


def _conversation_summary_time(value: Any) -> float:
    numeric = _tim_epoch_sort_value(value)
    if numeric:
        return numeric
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _conversation_sequence_value(value: Any) -> int:
    raw = str(value or "").strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return 0


def _attach_cached_conversation_summaries(
    items: List[Dict[str, Any]],
    summaries: Mapping[str, Mapping[str, Any]],
) -> None:
    for item in items:
        peer = str(item.get("peer_id") or item.get("conversation_user") or "").strip()
        cached = summaries.get(peer) if peer else None
        if isinstance(cached, Mapping):
            cached_preview = str(cached.get("last_message") or cached.get("content") or "")
            current_preview = str(item.get("last_message") or item.get("content") or "")
            cached_time = _conversation_summary_time(cached.get("preview_timestamp"))
            current_time = _conversation_summary_time(item.get("preview_timestamp"))
            cached_sequence = _conversation_sequence_value(cached.get("preview_sequence"))
            current_sequence = _conversation_sequence_value(item.get("preview_sequence"))
            cached_is_newer = (cached_time, cached_sequence) >= (
                current_time,
                current_sequence,
            )
            if cached_preview and (not current_preview or cached_is_newer):
                item["last_message"] = cached_preview
                item["content"] = cached_preview
                item["preview_timestamp"] = cached.get("preview_timestamp") or ""
                item["preview_sequence"] = cached.get("preview_sequence") or ""
                item["preview_source"] = cached.get("preview_source") or "archive"
                item["preview_authoritative"] = cached.get("preview_authoritative") is True
                item["preview_timestamp_inferred"] = (
                    cached.get("preview_timestamp_inferred") is True
                )
        activity_time = _conversation_summary_time(item.get("timestamp"))
        preview_time = _conversation_summary_time(item.get("preview_timestamp"))
        activity_sequence = _conversation_sequence_value(
            item.get("activity_sequence") or item.get("MsgSeq")
        )
        preview_sequence = _conversation_sequence_value(item.get("preview_sequence"))
        item["preview_stale"] = bool(
            activity_time
            and (
                not preview_time
                or activity_time > preview_time
                or (
                    activity_time == preview_time
                    and (
                        (activity_sequence and activity_sequence > preview_sequence)
                        or item.get("preview_authoritative") is not True
                        or item.get("preview_timestamp_inferred") is True
                    )
                )
            )
        )


def _tim_epoch_sort_value(value: Any) -> float:
    raw = str(value or "").strip()
    try:
        parsed = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    absolute = abs(parsed)
    if absolute >= 10**15:
        parsed /= 1_000_000
    elif absolute >= 10**12:
        parsed /= 1_000
    return parsed


def _tim_c2c_unread_counts(
    client: Any,
    account_uid: str,
    peers: List[str],
    *,
    deadline: Any = None,
    call_timeout: Optional[float] = None,
    breakers: Any = None,
    request_id: str = "",
) -> Dict[str, int]:
    method = getattr(client, "c2c_unread_counts", None)
    normalized = list(
        dict.fromkeys(
            str(peer or "").strip()
            for peer in peers
            if str(peer or "").strip()
            and str(peer or "").strip() != account_uid
        )
    )[:500]
    if not callable(method) or not account_uid or not normalized:
        return {}
    counts: Dict[str, int] = {}
    for offset in range(0, len(normalized), 100):
        result = _dependency_call(
            method,
            account_uid,
            normalized[offset : offset + 100],
            provider="tim",
            domain="im-read",
            breakers=breakers,
            timeout=call_timeout,
            deadline=deadline,
            request_id=request_id,
        )
        if not getattr(result, "ok", False):
            continue
        data = getattr(result, "data", None)
        data = data if isinstance(data, Mapping) else {}
        rows = data.get("C2CUnreadMsgNumList")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            peer = str(row.get("Peer_Account") or "").strip()
            if peer:
                try:
                    counts[peer] = max(0, int(row.get("C2CUnreadMsgNum") or 0))
                except (TypeError, ValueError, OverflowError):
                    counts[peer] = 0
    return counts


def _tim_apply_outgoing_read_state(
    items: List[Dict[str, Any]],
    *,
    account_uid: str,
    peer_uid: str,
    unread_count: Optional[int],
) -> None:
    if unread_count is None:
        return
    outgoing = [
        item
        for item in items
        if str(item.get("from") or item.get("from_user_id") or "") == account_uid
        and str(item.get("to") or item.get("to_user_id") or "") == peer_uid
    ]
    read_count = max(0, len(outgoing) - max(0, int(unread_count)))
    for index, item in enumerate(outgoing):
        is_read = index < read_count
        item["is_peer_read"] = is_read
        item["read_state"] = "read" if is_read else "unread"


def _tim_recent_conversation_envelope(
    app: Any,
    client: Any,
    account_uid: str,
    profile_cache: Optional[
        Dict[str, tuple[float, Optional[Dict[str, Any]]]]
    ] = None,
    *,
    max_pages: int = 5,
    deadline: Any = None,
    call_timeout: Optional[float] = None,
    breakers: Any = None,
    request_id: str = "",
) -> Dict[str, Any]:
    timestamp = 0
    start_index = 0
    top_timestamp = 0
    top_start_index = 0
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    previous_cursor: Optional[Tuple[int, int, int, int]] = None
    observed_at = time.time()
    snapshot_complete = False
    for _page in range(max(1, min(int(max_pages), 10))):
        result = _dependency_call(
            client.recent_contacts,
            account_uid,
            timestamp=timestamp,
            start_index=start_index,
            top_timestamp=top_timestamp,
            top_start_index=top_start_index,
            provider="tim",
            domain="im-read",
            breakers=breakers,
            timeout=call_timeout,
            deadline=deadline,
            request_id=request_id,
        )
        if not getattr(result, "ok", False):
            raise RuntimeError("TIM recent contacts unavailable")
        data = getattr(result, "data", None)
        data = data if isinstance(data, Mapping) else {}
        session_items = data.get("SessionItem")
        session_items = session_items if isinstance(session_items, list) else []
        for raw in session_items:
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("Type") or "").strip().lower() not in {"1", "c2c"}:
                continue
            peer = str(raw.get("To_Account") or "").strip()
            if (
                not peer
                or peer == account_uid
                or peer in seen
                or len(peer) > 128
                or any(ord(char) < 33 for char in peer)
            ):
                continue
            seen.add(peer)
            rows.append(
                {
                    "id": f"C2C{peer}",
                    "peer_id": peer,
                    "conversation_user": peer,
                    "timestamp": raw.get("MsgTime"),
                    "activity_sequence": raw.get("MsgSeq"),
                    "unread_count": raw.get("UnreadMsgNum") or 0,
                    "unread_observed_at": observed_at,
                    "unread_authoritative": True,
                    "source": "tim_rest",
                }
            )
        if int(data.get("CompleteFlag") or 0) == 1:
            snapshot_complete = True
            break
        cursor = (
            max(0, int(data.get("TimeStamp") or timestamp)),
            max(0, int(data.get("StartIndex") or start_index)),
            max(0, int(data.get("TopTimeStamp") or top_timestamp)),
            max(0, int(data.get("TopStartIndex") or top_start_index)),
        )
        if not session_items or cursor == previous_cursor:
            break
        previous_cursor = cursor
        timestamp, start_index, top_timestamp, top_start_index = cursor

    rows.sort(
        key=lambda item: _tim_epoch_sort_value(item.get("timestamp")),
        reverse=True,
    )
    items = N.normalize_conversations(rows)
    unread_counts = _tim_c2c_unread_counts(
        client,
        account_uid,
        [str(item.get("peer_id") or "") for item in items],
        deadline=deadline,
        call_timeout=call_timeout,
        breakers=breakers,
        request_id=request_id,
    )
    for item in items:
        item["source"] = "tim_rest"
        peer = str(item.get("peer_id") or "")
        if peer in unread_counts:
            item["unread_count"] = unread_counts[peer]
        item["unread_observed_at"] = observed_at
        item["unread_authoritative"] = True
    cache = profile_cache if profile_cache is not None else {}
    _attach_cached_conversation_profiles(app, items, cache)
    return {
        "ok": True,
        "items": items,
        "list": items,
        "count": len(items),
        "entity": "conversation",
        "status": 200,
        "source": "tim_rest",
        # Only a server-side cursor that explicitly reached CompleteFlag=1 can
        # prove the account's existing-conversation snapshot is complete.  The
        # persistence layer uses this marker for the retirement readiness gate;
        # browser archives and truncated TIM pages never set it.
        "snapshot_complete": snapshot_complete,
    }


def _tim_message_sort_key(item: Mapping[str, Any]) -> Tuple[float, int, str]:
    return (
        _tim_epoch_sort_value(item.get("timestamp") or item.get("time")),
        _conversation_sequence_value(
            item.get("sequence") or item.get("msg_sequence") or item.get("MsgSeq")
        ),
        str(item.get("msg_key") or item.get("id") or ""),
    )


def _tim_roaming_message_envelope(
    client: Any,
    account_uid: str,
    peer_uid: str,
    *,
    max_messages: int = 200,
    retention_days: int = 180,
    around_time: Optional[int] = None,
    before_time: Optional[int] = None,
    include_read_state: bool = True,
    deadline: Any = None,
    call_timeout: Optional[float] = None,
    breakers: Any = None,
    request_id: str = "",
) -> Dict[str, Any]:
    now_epoch = int(time.time())
    if around_time is not None and int(around_time) > 0:
        center = int(around_time)
        min_time = max(0, center - 5)
        max_time = center + 5
    else:
        min_time = now_epoch - max(1, int(retention_days)) * 86400
        max_time = (
            min(now_epoch + 60, max(0, int(before_time)))
            if before_time is not None and int(before_time) > 0
            else now_epoch + 60
        )
    if max_time < min_time:
        return {
            "ok": True,
            "items": [],
            "list": [],
            "count": 0,
            "entity": "message",
            "status": 200,
            "source": "tim_rest",
            "has_more": False,
            "next_before": "",
        }
    page_limit = max(1, (max(1, int(max_messages)) + 99) // 100)
    raw_items: List[Dict[str, Any]] = []
    for sender, recipient in ((peer_uid, account_uid), (account_uid, peer_uid)):
        last_msg_key = ""
        for _page in range(page_limit):
            result = _dependency_call(
                client.roaming_messages,
                sender,
                recipient,
                min_time=min_time,
                max_time=max_time,
                max_count=min(100, max(1, int(max_messages))),
                last_msg_key=last_msg_key,
                provider="tim",
                domain="im-read",
                breakers=breakers,
                timeout=call_timeout,
                deadline=deadline,
                request_id=request_id,
            )
            if not getattr(result, "ok", False):
                raise RuntimeError("TIM roaming messages unavailable")
            data = getattr(result, "data", None)
            data = data if isinstance(data, Mapping) else {}
            page_items = data.get("MsgList")
            page_items = page_items if isinstance(page_items, list) else []
            raw_items.extend(dict(item) for item in page_items if isinstance(item, Mapping))
            next_key = str(data.get("LastMsgKey") or "").strip()[:256]
            if int(data.get("Complete") or 0) == 1 or not page_items or not next_key:
                break
            if next_key == last_msg_key:
                break
            last_msg_key = next_key

    by_identity: Dict[str, Dict[str, Any]] = {}
    for item in N.normalize_messages(raw_items):
        identity = str(item.get("msg_key") or item.get("id") or "").strip()
        if not identity:
            identity = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        by_identity[identity] = item
    items = sorted(by_identity.values(), key=_tim_message_sort_key)[
        -max(1, int(max_messages)):
    ]
    if include_read_state:
        unread_counts = _tim_c2c_unread_counts(
            client,
            peer_uid,
            [account_uid],
            deadline=deadline,
            call_timeout=call_timeout,
            breakers=breakers,
            request_id=request_id,
        )
        _tim_apply_outgoing_read_state(
            items,
            account_uid=account_uid,
            peer_uid=peer_uid,
            unread_count=unread_counts.get(account_uid),
        )
    oldest_time = (
        int(
            _tim_epoch_sort_value(
                items[0].get("timestamp") or items[0].get("time")
            )
        )
        if items
        else 0
    )
    return {
        "ok": True,
        "items": items,
        "list": items,
        "count": len(items),
        "entity": "message",
        "status": 200,
        "source": "tim_rest",
        "has_more": len(items) >= max(1, int(max_messages)),
        "next_before": str(oldest_time) if oldest_time > 0 else "",
    }


def _batch_cached_profiles(
    app: Any,
    uids: List[str],
    cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]],
    *,
    coordinator: Any = None,
    account_key: str = "",
    budget_seconds: float = 0.0,
    call_timeout: Optional[float] = None,
    max_sync: int = 12,
    force_refresh: bool = False,
    breakers: Any = None,
    request_id: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    targets = list(
        dict.fromkeys(
            str(uid or "").strip() for uid in uids if str(uid or "").strip()
        )
    )
    resolved: Dict[str, Optional[Dict[str, Any]]] = {}
    missing: List[str] = []
    now = time.monotonic()
    for uid in targets:
        cached = cache.get(uid)
        if cached:
            cached_at, profile = cached
            ttl = PROFILE_CACHE_TTL_SEC if profile else PROFILE_CACHE_ERROR_TTL_SEC
            age = max(0.0, now - cached_at)
            refresh_allowed = bool(
                force_refresh and age >= PROFILE_CACHE_REFRESH_MIN_AGE_SEC
            )
            if age < ttl and not refresh_allowed:
                resolved[uid] = dict(profile) if profile else None
                continue
        missing.append(uid)
    pending: List[str] = []
    saturated: List[str] = []
    if missing and coordinator is not None and budget_seconds > 0:
        batch = coordinator.fetch_many(
            account_key=account_key or str(getattr(app.session, "uid", "") or ""),
            uids=missing,
            fetcher=lambda uid: _fetch_social_profile(
                app,
                uid,
                timeout=call_timeout,
                breakers=breakers,
                request_id=request_id,
            ),
            budget_seconds=budget_seconds,
            max_sync=max_sync,
        )
        cached_at = time.monotonic()
        for uid, profile in batch.completed.items():
            resolved[uid] = profile
            cache[uid] = (cached_at, dict(profile) if profile else None)
        pending = list(batch.pending)
        saturated = list(batch.saturated)
    elif missing:
        admitted = missing[: max(0, int(max_sync))]
        pending = list(missing[len(admitted) :])
        if admitted:
            with ThreadPoolExecutor(
                max_workers=min(4, len(admitted)),
                thread_name_prefix="bbw-profile-batch",
            ) as pool:
                profiles = list(
                    pool.map(lambda uid: _fetch_social_profile(app, uid), admitted)
                )
            cached_at = time.monotonic()
            for uid, profile in zip(admitted, profiles):
                resolved[uid] = profile
                cache[uid] = (cached_at, dict(profile) if profile else None)
        while len(cache) > 200:
            oldest = min(cache, key=lambda key: cache[key][0])
            cache.pop(oldest, None)
    if coordinator is not None:
        while len(cache) > 200:
            oldest = min(cache, key=lambda key: cache[key][0])
            cache.pop(oldest, None)
    if details is not None:
        details.update(
            pending_uids=pending,
            saturated_uids=saturated,
            requested_count=len(targets),
            resolved_count=sum(1 for uid in targets if resolved.get(uid)),
        )
    return [dict(resolved[uid]) for uid in targets if resolved.get(uid)]


def _is_false_response(r: Any, *accepted: str) -> bool:
    """Match explicit false/no business responses without weakening global parsing."""
    status = int(getattr(r, "status", 0) or 0)
    if not 200 <= status < 300:
        return False
    raw = str(getattr(r, "raw", "") or "").strip().lower()
    data = getattr(r, "data", None)
    values = {str(value).strip().lower() for value in accepted if str(value).strip()}
    return (data is False and "false" in values) or raw in values


def empty_list_envelope(
    result: Any,
    payload: Dict[str, Any],
    message: str = "列表为空",
) -> Dict[str, Any]:
    """Treat an explicit HTTP-200 false as an empty collection for list APIs."""
    if _is_false_response(result, "false"):
        payload.update(
            ok=True,
            code="",
            message=message,
            error=None,
            items=[],
            list=[],
            count=0,
            availability="empty",
        )
    return payload


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
        self._held_peer_lock = None
        try:
            super().handle_one_request()
        finally:
            peer_lock = self._held_peer_lock
            self._held_peer_lock = None
            if peer_lock is not None:
                peer_lock.release()
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
        extra_headers: Optional[Mapping[str, Any]] = None,
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
            for header_name, header_value in dict(extra_headers or {}).items():
                name = str(header_name or "").strip()
                value = str(header_value or "").strip()
                if name and value and "\r" not in name + value and "\n" not in name + value:
                    self.send_header(name, value)
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
                method = str(getattr(self, "command", "GET") or "GET").upper()
                path = urlparse(str(getattr(self, "path", "") or "")).path
                mode = _request_lock_mode(method, path)
                started_at = time.monotonic()
                if mode == "read":
                    self._held_user_lock = user.request_gate.acquire_read()
                elif mode == "write":
                    self._held_user_lock = user.request_gate.acquire_write()
                wait_ms = (time.monotonic() - started_at) * 1000
                if mode in {"read", "write"}:
                    LOGGER.info(
                        json.dumps(
                            {
                                "event": "request_gate_acquired",
                                "request_id": str(
                                    getattr(self, "_request_id", "") or ""
                                )[:64],
                                "path": path[:256],
                                "mode": mode,
                                "wait_ms": round(wait_ms, 2),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
            return user
        except KeyError:
            self.ok({"ok": False, "error": "请先登录"}, 401)
            return None

    def acquire_peer_request_gate(self, user: Any, peers: List[str]) -> None:
        if getattr(self, "_held_peer_lock", None) is not None:
            return
        gate = getattr(user, "peer_request_gate", None)
        acquire = getattr(gate, "acquire", None)
        if not callable(acquire):
            return
        started_at = time.monotonic()
        self._held_peer_lock = acquire(peers)
        LOGGER.info(
            json.dumps(
                {
                    "event": "peer_gate_acquired",
                    "request_id": str(getattr(self, "_request_id", "") or "")[:64],
                    "path": urlparse(str(getattr(self, "path", "") or "")).path[:256],
                    "peer_count": len(peers),
                    "wait_ms": round((time.monotonic() - started_at) * 1000, 2),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def web_user_capabilities(self, user: Any) -> Dict[str, bool]:
        return _web_user_capabilities(
            user,
            match_pool_online_list_enabled=getattr(
                self,
                "_request_match_pool_online_list_enabled",
                None,
            ),
            nearby_custom_city_enabled=getattr(
                self,
                "_request_nearby_custom_city_enabled",
                None,
            ),
            local_password_change_enabled=bool(
                getattr(
                    self,
                    "_request_local_password_change_enabled",
                    False,
                )
            ),
        )

    def _store_message_block_snapshot(
        self,
        user: Any,
        *,
        path: str,
        result: Any,
        peer_attribute: str,
        timestamp_attribute: str,
    ) -> Tuple[bool, Dict[str, Any]]:
        payload = empty_list_envelope(result, RL(result), "黑名单为空")
        if payload.get("ok") is not True:
            return False, payload
        session = getattr(getattr(user, "app", None), "session", None)
        current_uid = str(getattr(session, "uid", "") or "")
        peers = {
            peer
            for peer in _message_peer_ids(payload)
            if peer != current_uid
        }
        # Enforce a newly observed block in this process even when the durable
        # snapshot write fails.  Mark that direction incomplete so subsequent
        # policy reads cannot disagree with the durable authorizer.
        setattr(user, peer_attribute, peers)
        recorder = getattr(self, "_request_message_block_snapshot_recorder", None)
        if callable(recorder):
            try:
                recorder(path, peers)
            except Exception:
                setattr(user, timestamp_attribute, 0.0)
                return False, {
                    "ok": False,
                    "code": "MESSAGE_BLOCK_POLICY_PERSISTENCE_FAILED",
                    "error": "黑名单已读取，但私聊阻断状态保存失败，请稍后重试",
                }
        setattr(user, timestamp_attribute, time.monotonic())
        return True, payload

    def _load_message_block_snapshot(
        self,
        user: Any,
        *,
        path: str,
        fetcher: Any,
        peer_attribute: str,
        timestamp_attribute: str,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Read and persist one block direction under the production guard."""

        guard_factory = getattr(
            self,
            "_request_message_block_snapshot_guard",
            None,
        )
        try:
            if callable(guard_factory):
                with guard_factory(path):
                    return Handler._store_message_block_snapshot(
                        self,
                        user,
                        path=path,
                        result=fetcher(),
                        peer_attribute=peer_attribute,
                        timestamp_attribute=timestamp_attribute,
                    )
            return Handler._store_message_block_snapshot(
                self,
                user,
                path=path,
                result=fetcher(),
                peer_attribute=peer_attribute,
                timestamp_attribute=timestamp_attribute,
            )
        except Exception:
            return False, {
                "ok": False,
                "code": "MESSAGE_BLOCK_POLICY_SYNC_UNAVAILABLE",
                "error": "黑名单状态同步繁忙或暂时不可用，请稍后重试",
            }

    def _restore_durable_message_block_snapshots(self, user: Any) -> bool:
        """Seed both in-memory block directions from one trusted DB snapshot."""

        loader = getattr(self, "_request_message_block_snapshot_loader", None)
        if not callable(loader):
            return False
        try:
            snapshot = loader()
        except Exception:
            return False
        if not isinstance(snapshot, Mapping):
            return False

        session = getattr(getattr(user, "app", None), "session", None)
        current_uid = str(getattr(session, "uid", "") or "").strip()

        def trusted_peers(key: str) -> Optional[Set[str]]:
            if key not in snapshot:
                return None
            values = snapshot.get(key)
            if not isinstance(values, (list, tuple, set, frozenset)):
                return None
            peers: Set[str] = set()
            for value in values:
                peer = str(value or "").strip()
                if (
                    not peer
                    or peer.lower() in {"0", "none", "null"}
                    or len(peer) > 128
                    or any(ord(char) < 33 for char in peer)
                    or peer == current_uid
                ):
                    return None
                peers.add(peer)
            return peers

        own_peers = trusted_peers("blacklist")
        incoming_peers = trusted_peers("blacklisted_by")
        if own_peers is None or incoming_peers is None:
            return False
        restored_at = time.monotonic()
        setattr(user, "blocked_message_peers", own_peers)
        setattr(user, "blocked_by_message_peers", incoming_peers)
        setattr(user, "blocked_message_peers_snapshot_at", restored_at)
        setattr(user, "blocked_by_message_peers_snapshot_at", restored_at)
        setattr(user, "message_blocks_retry_at", 0.0)
        return True

    def ensure_message_blocks_loaded(self, user: Any) -> bool:
        """Load both blacklist directions before authorizing any private message."""

        now = time.monotonic()
        own_snapshot_at = float(
            getattr(user, "blocked_message_peers_snapshot_at", 0.0) or 0.0
        )
        incoming_snapshot_at = float(
            getattr(user, "blocked_by_message_peers_snapshot_at", 0.0) or 0.0
        )
        if own_snapshot_at <= 0 or incoming_snapshot_at <= 0:
            if Handler._restore_durable_message_block_snapshots(self, user):
                now = time.monotonic()
                own_snapshot_at = float(
                    getattr(user, "blocked_message_peers_snapshot_at", 0.0) or 0.0
                )
                incoming_snapshot_at = float(
                    getattr(user, "blocked_by_message_peers_snapshot_at", 0.0) or 0.0
                )
        if str(getattr(user, "authentication_source", "") or "") == "local":
            # A local-password session exists specifically because the provider
            # is unavailable.  Reuse the last complete, account-bound snapshot
            # without introducing a synchronous network timeout.  Missing
            # snapshots still fail closed.
            return own_snapshot_at > 0 and incoming_snapshot_at > 0
        own_fresh = own_snapshot_at > 0 and now - own_snapshot_at < MESSAGE_BLOCK_SNAPSHOT_TTL_SEC
        incoming_fresh = (
            incoming_snapshot_at > 0
            and now - incoming_snapshot_at < MESSAGE_BLOCK_SNAPSHOT_TTL_SEC
        )
        if own_fresh and incoming_fresh:
            return True
        if now < float(getattr(user, "message_blocks_retry_at", 0.0) or 0.0):
            return own_snapshot_at > 0 and incoming_snapshot_at > 0

        app = getattr(user, "app", None)
        social = getattr(app, "social", None)
        own_ok = own_snapshot_at > 0
        incoming_ok = incoming_snapshot_at > 0
        refresh_failed = False
        if not own_fresh:
            loaded, _payload = Handler._load_message_block_snapshot(
                self,
                user,
                path="/api/social/blacklist",
                fetcher=social.my_blacklist,
                peer_attribute="blocked_message_peers",
                timestamp_attribute="blocked_message_peers_snapshot_at",
            )
            refresh_failed = refresh_failed or not loaded
            own_ok = loaded or own_snapshot_at > 0
        if not incoming_fresh:
            loaded, _payload = Handler._load_message_block_snapshot(
                self,
                user,
                path="/api/social/blacklist-me",
                fetcher=social.blacklist_me,
                peer_attribute="blocked_by_message_peers",
                timestamp_attribute="blocked_by_message_peers_snapshot_at",
            )
            refresh_failed = refresh_failed or not loaded
            incoming_ok = loaded or incoming_snapshot_at > 0
        if refresh_failed:
            setattr(
                user,
                "message_blocks_retry_at",
                time.monotonic() + MESSAGE_BLOCK_SNAPSHOT_RETRY_SEC,
            )
        else:
            setattr(user, "message_blocks_retry_at", 0.0)
        return own_ok and incoming_ok

    def message_block_snapshots_fresh(self, user: Any) -> bool:
        now = time.monotonic()
        return all(
            timestamp > 0 and now - timestamp < MESSAGE_BLOCK_SNAPSHOT_TTL_SEC
            for timestamp in (
                float(getattr(user, "blocked_message_peers_snapshot_at", 0.0) or 0.0),
                float(
                    getattr(user, "blocked_by_message_peers_snapshot_at", 0.0)
                    or 0.0
                ),
            )
        )

    def can_message_peer(self, user: Any, peer: Any) -> bool:
        target = str(peer or "").strip()
        session = getattr(getattr(user, "app", None), "session", None)
        current_uid = str(getattr(session, "uid", "") or "").strip()
        if (
            not target
            or target.lower() in {"0", "none", "null"}
            or len(target) > 128
            or target == current_uid
            or target == SYSTEM_CUSTOMER_SERVICE_UID
        ):
            return False
        if not Handler.ensure_message_blocks_loaded(self, user):
            return False
        authorizer = getattr(self, "_request_message_peer_authorizer", None)
        if callable(authorizer) and bool(
            getattr(self, "_request_message_peer_authorizer_canonical", False)
        ):
            # PostgreSQL is the live authority after migration. The in-memory
            # sets only prove both directions were loaded; they must not keep a
            # later Web-local unblock denied for the lifetime of this session.
            # A zero timestamp means a newly observed block was not persisted,
            # in which case the safe behavior remains fail-closed.
            if any(
                float(getattr(user, attribute, 0.0) or 0.0) <= 0
                for attribute in (
                    "blocked_message_peers_snapshot_at",
                    "blocked_by_message_peers_snapshot_at",
                )
            ):
                return False
            try:
                return bool(authorizer(target))
            except Exception:
                return False
        if target in (
            set(getattr(user, "blocked_message_peers", set()) or set())
            | set(getattr(user, "blocked_by_message_peers", set()) or set())
        ):
            return False
        if callable(authorizer):
            try:
                return bool(authorizer(target))
            except Exception:
                return False
        if Handler.web_user_capabilities(self, user).get("proactive_private_message"):
            return True
        for attribute in (
            "friend_message_peers",
            "match_message_peers",
            "conversation_message_peers",
        ):
            if target in set(getattr(user, attribute, set()) or set()):
                return True
        return False

    def can_view_message_peer(self, user: Any, peer: Any) -> bool:
        target = str(peer or "").strip()
        session = getattr(getattr(user, "app", None), "session", None)
        current_uid = str(getattr(session, "uid", "") or "").strip()
        if target == SYSTEM_CUSTOMER_SERVICE_UID:
            return bool(current_uid and target != current_uid)
        return Handler.can_message_peer(self, user, target)

    def deny_private_message(self, capabilities: Dict[str, bool]) -> None:
        self.ok(
            {
                "ok": False,
                "code": "PRIVATE_MESSAGE_PERMISSION_REQUIRED",
                "error": "该私信入口仅向管理员授权的用户开放；好友、匹配成功的用户和已有会话不受影响",
                "capabilities": capabilities,
            },
            403,
        )

    def broad_im_credentials_disabled_payload(
        self, capabilities: Dict[str, bool]
    ) -> Dict[str, Any]:
        return {
            "ok": False,
            "code": "IM_DIRECT_CREDENTIALS_DISABLED",
            "error": "网页版私信统一使用服务端受控消息通道",
            "capabilities": capabilities,
        }

    def deny_broad_im_credentials(self, capabilities: Dict[str, bool]) -> None:
        self.ok(
            Handler.broad_im_credentials_disabled_payload(self, capabilities),
            403,
        )

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
                        "moment_video_compat": MOMENT_VIDEO_COMPAT_ENABLED,
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
                        "moment_video_compat": MOMENT_VIDEO_COMPAT_ENABLED,
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
                pub = u.public()
                user_dto = N.session_user_dto(u.app.whoami())
                authentication_source = str(
                    getattr(u, "authentication_source", "web-session")
                    or "web-session"
                )
                local_authentication = authentication_source == "local"
            return self.ok(
                {
                    "ok": True,
                    **pub,
                    "user": user_dto,
                    "auth_source": (
                        "web-local" if local_authentication else authentication_source
                    ),
                    "dependency_mode": (
                        "degraded" if local_authentication else "provider"
                    ),
                    "features": _features(),
                    "capabilities": {
                        "roomkit_list": True,
                        "invite_login": INVITE_LOGIN_ENABLED,
                        "moment_video_compat": MOMENT_VIDEO_COMPAT_ENABLED,
                        **Handler.web_user_capabilities(self, u),
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
            who = N.session_user_dto(app.whoami())
            deferred = {
                "ok": True,
                "items": [],
                "list": [],
                "count": 0,
                "deferred": True,
            }
            return self.ok(
                {
                    "ok": True,
                    "user": who,
                    # The Web landing page fetches people and slides separately.
                    # Avoid two unused upstream calls on every route entry.
                    "gifts": deferred,
                    "recommend": {**deferred, "entity": "slide"},
                    "heartbeat": u.heartbeat.status() if u.heartbeat else {"running": False},
                    "capabilities": Handler.web_user_capabilities(self, u),
                }
            )

        if path == "/api/app/bootstrap":
            capabilities = Handler.web_user_capabilities(self, u)
            batch = {
                k: {"ok": v.ok, "code": v.code, "message": v.message}
                for k, v in app.bootstrap(include_im=False).items()
            }
            if capabilities.get("direct_im_credentials"):
                if not Handler.ensure_message_blocks_loaded(self, u):
                    tim = {
                        "ok": False,
                        "code": "MESSAGE_BLOCK_POLICY_UNAVAILABLE",
                        "error": "黑名单状态暂时无法确认，实时消息凭证已暂停下发",
                    }
                else:
                    try:
                        tim = {
                            "ok": True,
                            **u.native.im.tim_login_payload(
                                prefer="server",
                                allow_local_fallback=False,
                            ),
                        }
                    except Exception as exc:
                        tim = {
                            "ok": False,
                            "error": _safe_error(exc, "消息登录凭证获取失败"),
                        }
            else:
                tim = Handler.broad_im_credentials_disabled_payload(self, capabilities)
            u.persist()
            return self.ok(
                {
                    "ok": True,
                    "user": N.session_user_dto(app.whoami()),
                    "batch": batch,
                    "tim": tim,
                    "capabilities": capabilities,
                }
            )

        # ---- content / square ----
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
                if tab not in MOMENT_FEED_TABS:
                    return self.ok({"ok": False, "error": "不支持的动态分类"}, 400)
                if (
                    not cursor.isdigit()
                    or cursor == "0"
                    or len(cursor) > 32
                    or (tab in MOMENT_PAGE_CURSOR_TABS and int(cursor) > 100000)
                ):
                    return self.ok({"ok": False, "error": "动态游标无效"}, 400)
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
            payload["cursor"] = cursor
            feed_items = payload.get("items") if isinstance(payload.get("items"), list) else []
            if feed_items:
                if tab in MOMENT_PAGE_CURSOR_TABS:
                    payload["next_cursor"] = str(int(cursor) + 1)
                else:
                    payload["next_cursor"] = str(feed_items[-1].get("id") or "")
            else:
                payload["next_cursor"] = ""
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
        if path == "/api/profile/users":
            requested = _presence_uids((qs.get("uids") or []) + (qs.get("uid") or []), limit=100)
            if not requested:
                return self.ok({"ok": False, "error": "缺少用户 UID"}, 400)
            budget_config = dict(
                getattr(self, "_request_budget_config", None) or {}
            )
            lookup_details: Dict[str, Any] = {}
            items = _batch_cached_profiles(
                app,
                requested,
                u.profile_cache,
                coordinator=getattr(
                    self,
                    "_request_profile_lookup_coordinator",
                    None,
                ),
                account_key=str(app.session.uid or ""),
                budget_seconds=float(
                    budget_config.get("profile_budget_seconds") or 0
                ),
                call_timeout=(
                    float(budget_config.get("profile_timeout_seconds"))
                    if budget_config.get("profile_timeout_seconds")
                    else None
                ),
                max_sync=int(budget_config.get("profile_sync_limit") or 12),
                force_refresh=_as_bool(q("refresh", "0")),
                breakers=getattr(
                    self,
                    "_request_dependency_breakers",
                    None,
                ),
                request_id=str(getattr(self, "_request_id", "") or ""),
                details=lookup_details,
            )
            pending_uids = list(lookup_details.get("pending_uids") or [])
            return self.ok(
                {
                    "ok": True,
                    "partial": bool(pending_uids),
                    "items": items,
                    "list": items,
                    "count": len(items),
                    "requested_count": len(requested),
                    "refresh_requested": _as_bool(q("refresh", "0")),
                    "pending_uids": pending_uids,
                    "retry_after": 2 if pending_uids else 0,
                }
            )
        if path == "/api/profile/reset-num":
            return self.ok(R(app.profile.reset_num(q("type", "昵称")), include_value=True))
        if path == "/api/profile/etiquette":
            return self.ok(R(app.profile.etiquette(), include_value=True))

        # ---- social ----
        if path == "/api/social/follows":
            summary = q("summary", "0") == "1"
            # getFollowUser contains display profiles; getFollowList mostly
            # contains relationship ids and therefore renders numeric names.
            primary = app.social.follow_users(q("uid") or None, page=q("page", "1"))
            payload = RS(
                primary,
                str(app.session.uid or ""),
                None if summary else app,
                getattr(u, "profile_cache", None),
            )
            payload = empty_list_envelope(primary, payload, "关注列表为空")
            if payload.get("availability") == "empty":
                return self.ok(
                    {"ok": True, "count": 0} if summary else payload
                )
            if payload.get("items"):
                return self.ok(
                    {"ok": payload.get("ok", True), "count": payload.get("count", 0)}
                    if summary
                    else payload
                )
            fallback = app.social.follow_list(q("uid") or None)
            payload = RS(
                fallback,
                str(app.session.uid or ""),
                None if summary else app,
                getattr(u, "profile_cache", None),
            )
            payload = empty_list_envelope(fallback, payload, "关注列表为空")
            return self.ok(
                {"ok": payload.get("ok", True), "count": payload.get("count", 0)}
                if summary
                else payload
            )
        if path == "/api/social/fans":
            summary = q("summary", "0") == "1"
            result = app.social.fans_users(q("uid") or None, page=q("page", "1"))
            payload = RS(
                result,
                str(app.session.uid or ""),
                None if summary else app,
                getattr(u, "profile_cache", None),
            )
            payload = empty_list_envelope(result, payload, "粉丝列表为空")
            return self.ok(
                {"ok": payload.get("ok", True), "count": payload.get("count", 0)}
                if summary
                else payload
            )
        if path == "/api/social/follow-list":
            result = app.social.follow_list(q("uid") or q("id") or None)
            return self.ok(empty_list_envelope(result, RL(result), "关注列表为空"))
        if path == "/api/social/friend-apply":
            page = str(q("page", "1") or "1").strip()
            if not page.isdigit() or not 1 <= int(page) <= 100000:
                return self.ok({"ok": False, "error": "好友申请页码无效"}, 400)
            result = app.social.friend_apply_list(page)
            result, items, metadata = _friend_applications(
                app,
                str(app.session.uid or ""),
                result,
                start_page=int(page),
            )
            if _is_false_response(result, "false"):
                payload = empty_list_envelope(
                    result,
                    N.envelope(result, items=[]),
                    "好友申请列表为空",
                )
                payload["status"] = result.status
                payload.update(metadata)
                return self.ok(
                    {"ok": True, **_friend_application_summary(metadata)}
                    if q("summary", "0") == "1"
                    else payload
                )
            if q("summary", "0") == "1":
                return self.ok(
                    {"ok": bool(result.ok), **_friend_application_summary(metadata)}
                )
            payload = N.envelope(result, items=items)
            payload["list"] = items
            payload["status"] = result.status
            payload.update(metadata)
            return self.ok(payload)
        if path == "/api/social/friends":
            result = app.social.friends()
            items = N.normalize_friends(result.data, str(app.session.uid or ""))
            friend_message_peers = {
                peer
                for peer in _message_peer_ids({"items": items})
                if peer != str(app.session.uid or "")
            }
            setattr(u, "friend_message_peers", friend_message_peers)
            payload = N.envelope(result, items=items)
            payload["list"] = items
            payload["status"] = result.status
            payload = empty_list_envelope(result, payload, "通讯录为空")
            if q("summary", "0") == "1":
                return self.ok(
                    {"ok": bool(payload.get("ok")), "count": int(payload.get("count") or 0)}
                )
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
            payload = RL(result)
            payload = empty_list_envelope(result, payload, "访客列表为空")
            if q("summary", "0") == "1":
                return self.ok(
                    {"ok": payload.get("ok", True), "count": payload.get("count", 0)}
                )
            return self.ok(payload)
        if path == "/api/social/blacklist":
            _stored, payload = Handler._load_message_block_snapshot(
                self,
                u,
                path=path,
                fetcher=app.social.my_blacklist,
                peer_attribute="blocked_message_peers",
                timestamp_attribute="blocked_message_peers_snapshot_at",
            )
            status = (
                503
                if payload.get("code")
                in {
                    "MESSAGE_BLOCK_POLICY_PERSISTENCE_FAILED",
                    "MESSAGE_BLOCK_POLICY_SYNC_UNAVAILABLE",
                }
                else 200
            )
            return self.ok(payload, status)
        if path == "/api/social/blacklist-me":
            _stored, payload = Handler._load_message_block_snapshot(
                self,
                u,
                path=path,
                fetcher=app.social.blacklist_me,
                peer_attribute="blocked_by_message_peers",
                timestamp_attribute="blocked_by_message_peers_snapshot_at",
            )
            status = (
                503
                if payload.get("code")
                in {
                    "MESSAGE_BLOCK_POLICY_PERSISTENCE_FAILED",
                    "MESSAGE_BLOCK_POLICY_SYNC_UNAVAILABLE",
                }
                else 200
            )
            return self.ok(payload, status)

        # ---- match ----
        if path == "/api/match/history":
            raw_page = str(q("page", "1") or "1").strip()
            if not raw_page.isdigit() or not 1 <= int(raw_page) <= 100_000:
                return self.ok({"ok": False, "error": "匹配历史页码无效"}, 400)
            loader = getattr(self, "_request_match_history_loader", None)
            if not callable(loader):
                lock = getattr(u, "lock", None)
                if lock is None:
                    history = list(getattr(u, "match_history", []) or [])
                else:
                    with lock:
                        history = list(getattr(u, "match_history", []) or [])
                page = int(raw_page)
                page_size = 10
                start = (page - 1) * page_size
                items = history[start : start + page_size]
                has_more = start + len(items) < len(history)
                return self.ok(
                    {
                        "ok": True,
                        "items": items,
                        "list": items,
                        "count": len(history),
                        "page": page,
                        "next_page": page + 1 if has_more else None,
                        "has_more": has_more,
                    }
                )
            try:
                return self.ok(loader(int(raw_page)))
            except Exception:
                return self.ok(
                    {"ok": False, "error": "匹配历史暂时无法加载，请稍后重试"},
                    503,
                )
        if path == "/api/match/status":
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bbw-match") as pool:
                cards_future = pool.submit(app.call, "getMyCard", uid=app.session.uid)
                nums_future = pool.submit(app.call, "getMatchNum", uid=app.session.uid)
                cards = cards_future.result()
                nums = nums_future.result()
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
                    "capabilities": Handler.web_user_capabilities(self, u),
                    # keep raw only for lab debugging if needed
                    "cards_ok": cards.ok,
                    "nums_ok": nums.ok,
                }
            )
        if path == "/api/match/voice/status":
            return self.ok(
                {
                    "ok": True,
                    **_voice_match_state(u),
                    "credential_ready": _voice_rong_credentials(u) is not None,
                    "sdk": dict(VOICE_WEB_SDK),
                    "capabilities": Handler.web_user_capabilities(self, u),
                }
            )
        if path in {"/api/match/online-users", "/api/match/nearby-users"}:
            capabilities = Handler.web_user_capabilities(self, u)
            gender = _discovery_gender(q("gender", "不限"))
            property_ = _discovery_property(q("property", "不限"))
            age_value = str(q("age", "不限") or "不限").strip()
            age_range = _discovery_age_range(age_value)
            if not gender or not property_ or age_range is None:
                return self.ok({"ok": False, "error": "筛选条件无效"}, 400)

            custom_city_value = str(q("city", "") or "").strip()
            custom_city = _custom_city(custom_city_value)
            if custom_city_value and not custom_city:
                return self.ok({"ok": False, "error": "城市名称无效"}, 400)
            if custom_city and not capabilities.get("nearby_custom_city"):
                return self.ok(
                    {
                        "ok": False,
                        "code": "CUSTOM_CITY_PERMISSION_REQUIRED",
                        "error": "自定义城市筛选需要管理员授权",
                        "capabilities": capabilities,
                    },
                    403,
                )

            latitude_value = str(q("latitude", "") or "").strip()
            longitude_value = str(q("longitude", "") or "").strip()
            latitude = _coordinate(latitude_value, -90.0, 90.0) if latitude_value else None
            longitude = _coordinate(longitude_value, -180.0, 180.0) if longitude_value else None
            if bool(latitude_value) != bool(longitude_value) or (
                latitude_value and (latitude is None or longitude is None)
            ):
                return self.ok({"ok": False, "error": "位置信息无效"}, 400)

            result = app.match.online_users(
                id=app.session.uid,
                gender=gender,
                property=property_,
                pageIndex=q("page", q("pageIndex", "1")),
            )
            payload = RL(result)
            source_items = list(payload.get("items") or [])
            items = _filter_discovery_users(
                source_items,
                current_uid=str(app.session.uid or ""),
                gender=gender,
                property_=property_,
                age_range=age_range,
            )
            payload["source_count"] = len(source_items)
            payload["filters"] = {
                "gender": gender,
                "property": property_,
                "age": age_value,
            }

            if path == "/api/match/nearby-users":
                configured_city = _current_profile_region(u)
                target_city = custom_city or configured_city
                if target_city:
                    items = [item for item in items if _same_city(item.get("city"), target_city)]
                    payload["location"] = {
                        "mode": "custom_city" if custom_city else "profile_city",
                        "city": target_city,
                        "label": f"当前城市：{target_city}",
                    }
                elif latitude is not None and longitude is not None:
                    origin = (latitude, longitude)
                    coordinate_map = _raw_user_coordinates(getattr(result, "data", None))
                    nearby_items: List[Tuple[float, Dict[str, Any]]] = []
                    for item in items:
                        point = coordinate_map.get(str(item.get("id") or ""))
                        if point is None:
                            continue
                        distance = _distance_km(origin, point)
                        if distance <= NEARBY_RADIUS_KM:
                            nearby_items.append((distance, _with_discovery_distance(item, distance)))
                    nearby_items.sort(key=lambda value: value[0])
                    items = [item for _distance, item in nearby_items]
                    payload["location"] = {
                        "mode": "browser_location",
                        "city": "",
                        "label": f"当前位置附近 {int(NEARBY_RADIUS_KM)} 公里",
                        "radius_km": NEARBY_RADIUS_KM,
                    }
                else:
                    return self.ok(_nearby_people_location_missing(capabilities))

            payload["items"] = items[:50]
            payload["list"] = payload["items"]
            payload["count"] = len(payload["items"])
            payload["capabilities"] = capabilities
            return self.ok(payload)
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
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bbw-wallet") as pool:
                me_future = pool.submit(app.profile.get_me)
                myg_future = pool.submit(app.economy.my_gifts)
                me = me_future.result()
                myg = myg_future.result()
            u.persist()
            user = N.session_user_dto(app.whoami())
            return self.ok(
                {
                    "ok": True,
                    "user": user,
                    "me": R(me),
                    "my_gifts": RG(myg),
                    "membership": {
                        "vip": str(user.get("vip") or "0"),
                        "svip": str(user.get("svip") or "0"),
                        "source": "server",
                        "read_only": True,
                    },
                }
            )

        # ---- im ----
        if path == "/api/im/message-policy":
            if not Handler.ensure_message_blocks_loaded(self, u):
                return self.ok(
                    {
                        "ok": False,
                        "code": "MESSAGE_BLOCK_POLICY_UNAVAILABLE",
                        "error": "黑名单状态暂时无法确认，私聊功能已临时关闭",
                    },
                    503,
                )
            durable_blocked_peers = (
                ()
                if Handler.message_block_snapshots_fresh(self, u)
                else (
                    getattr(self, "_request_message_policy_blocked_peers", ())
                    or ()
                )
            )
            blocked_peers = {
                str(peer).strip()
                for peer in (
                    list(getattr(u, "blocked_message_peers", set()) or set())
                    + list(getattr(u, "blocked_by_message_peers", set()) or set())
                    + list(durable_blocked_peers)
                )
                if str(peer).strip()
                and str(peer).strip().lower() not in {"0", "none", "null"}
                and str(peer).strip() != str(app.session.uid or "")
            }
            match_peers = {
                str(peer).strip()
                for peer in (
                    list(getattr(u, "match_message_peers", set()) or set())
                    + list(
                        getattr(self, "_request_message_policy_match_peers", ())
                        or ()
                    )
                )
                if str(peer).strip()
                and str(peer).strip().lower() not in {"0", "none", "null"}
                and str(peer).strip() != str(app.session.uid or "")
            }
            allowed_peers = {
                str(peer).strip()
                for peer in (
                    list(getattr(u, "friend_message_peers", set()) or set())
                    + list(getattr(u, "match_message_peers", set()) or set())
                    + list(getattr(u, "conversation_message_peers", set()) or set())
                    + list(
                        getattr(self, "_request_message_policy_allowed_peers", ())
                        or ()
                    )
                )
                if str(peer).strip()
                and str(peer).strip().lower() not in {"0", "none", "null"}
                and str(peer).strip() != str(app.session.uid or "")
            }
            allowed_peers.update(match_peers)
            match_peers.difference_update(blocked_peers)
            allowed_peers.difference_update(blocked_peers)
            return self.ok(
                {
                    "ok": True,
                    "capabilities": Handler.web_user_capabilities(self, u),
                    "match_peers": sorted(match_peers)[:5000],
                    "allowed_peers": sorted(allowed_peers)[:5000],
                    "blocked_peers": sorted(blocked_peers)[:5000],
                }
            )
        if path == "/api/im/tim":
            capabilities = Handler.web_user_capabilities(self, u)
            if not capabilities.get("direct_im_credentials"):
                return Handler.deny_broad_im_credentials(self, capabilities)
            if not Handler.ensure_message_blocks_loaded(self, u):
                return self.ok(
                    {
                        "ok": False,
                        "code": "MESSAGE_BLOCK_POLICY_UNAVAILABLE",
                        "error": "黑名单状态暂时无法确认，实时消息凭证已暂停下发",
                    },
                    503,
                )
            try:
                payload = u.native.im.tim_login_payload(
                    prefer="server",
                    allow_local_fallback=False,
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
                return self.ok(
                    {
                        "ok": True,
                        **payload,
                        "sig_len": len(str(payload.get("userSig") or "")),
                    }
                )
            except Exception as exc:
                return self.ok(
                    {"ok": False, "error": _safe_error(exc, "消息登录凭证获取失败")},
                    400,
                )
        if path == "/api/im/rong":
            capabilities = Handler.web_user_capabilities(self, u)
            return Handler.deny_broad_im_credentials(self, capabilities)
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
            web_online: Set[str] = set()
            retry_after = 0
            if PRESENCE_BACKEND is not None:
                try:
                    web_online = set(PRESENCE_BACKEND.read_web_presence(requested))
                    retry_after = int(PRESENCE_BACKEND.presence_rest_retry_after())
                except Exception:
                    web_online = set()
                    retry_after = 0
            unresolved = [uid for uid in requested if uid not in web_online]
            normalized: Dict[str, Any] = {
                "ok": False,
                "items": [],
                "list": [],
                "count": 0,
                "message": "在线状态暂时不可用",
            }
            budget_config = dict(
                getattr(self, "_request_budget_config", None) or {}
            )
            presence_timeout = float(
                budget_config.get("presence_timeout_seconds") or 0
            )
            presence_deadline = (
                RequestDeadline(presence_timeout)
                if presence_timeout > 0
                else None
            )
            try:
                if unresolved and retry_after <= 0:
                    result = _dependency_call(
                        u.native.tim_rest.query_online,
                        unresolved,
                        provider="tim",
                        domain="presence",
                        breakers=getattr(
                            self,
                            "_request_dependency_breakers",
                            None,
                        ),
                        timeout=presence_timeout or None,
                        deadline=presence_deadline,
                        request_id=str(
                            getattr(self, "_request_id", "") or ""
                        ),
                    )
                    normalized = _normalize_presence_result(result, unresolved)
                    if not result.ok and PRESENCE_BACKEND is not None:
                        PRESENCE_BACKEND.mark_presence_rest_unavailable(result.error_code)
                        retry_after = int(PRESENCE_BACKEND.presence_rest_retry_after())
                elif not unresolved:
                    normalized = {
                        "ok": True,
                        "items": [],
                        "list": [],
                        "count": 0,
                        "message": "在线状态已更新",
                    }
            except DependencyCircuitOpen as exc:
                retry_after = max(retry_after, exc.retry_after)
                if PRESENCE_BACKEND is not None:
                    try:
                        PRESENCE_BACKEND.mark_presence_rest_unavailable(0)
                    except Exception:
                        pass
            except Exception:
                if PRESENCE_BACKEND is not None:
                    try:
                        PRESENCE_BACKEND.mark_presence_rest_unavailable(0)
                        retry_after = int(PRESENCE_BACKEND.presence_rest_retry_after())
                    except Exception:
                        pass
            by_uid = {
                str(item.get("uid") or ""): item
                for item in normalized.get("items", [])
                if isinstance(item, dict) and item.get("uid")
            }
            for uid in web_online:
                by_uid[uid] = {
                    "uid": uid,
                    "status": "online",
                    "label": "在线",
                    "is_online": True,
                    "source": "web",
                }
            items = [
                by_uid.get(
                    uid,
                    {
                        "uid": uid,
                        "status": "unknown",
                        "label": "状态未知",
                        "is_online": None,
                    },
                )
                for uid in requested
            ]
            available = bool(normalized.get("ok")) or bool(web_online)
            unknown_count = sum(1 for item in items if item.get("status") == "unknown")
            payload = {
                "ok": available,
                "partial": bool(normalized.get("partial"))
                or (not bool(normalized.get("ok")) and bool(unresolved)),
                "unknown_count": unknown_count,
                "items": items,
                "list": items,
                "count": len(items),
                "message": "在线状态已更新" if available else "在线状态暂时不可用",
                "retry_after": max(0, retry_after),
            }
            return self.ok(payload)
        if path == "/api/im/bootstrap":
            capabilities = Handler.web_user_capabilities(self, u)
            return Handler.deny_broad_im_credentials(self, capabilities)
        if path == "/api/im/stickers":
            return self.ok(RE(app.im.stickers(), "sticker"))
        if path == "/api/im/conversations":
            budget_config = dict(
                getattr(self, "_request_budget_config", None) or {}
            )
            im_budget = float(budget_config.get("im_budget_seconds") or 0)
            tim_timeout = float(
                budget_config.get("tim_timeout_seconds") or 0
            )
            provider_timeout = float(
                budget_config.get("provider_timeout_seconds") or 0
            )
            deadline = RequestDeadline(im_budget) if im_budget > 0 else None
            breakers = getattr(self, "_request_dependency_breakers", None)
            request_id = str(getattr(self, "_request_id", "") or "")
            try:
                payload = _tim_recent_conversation_envelope(
                    app,
                    u.native.tim_rest,
                    str(app.session.uid or ""),
                    u.profile_cache,
                    deadline=deadline,
                    call_timeout=tim_timeout or None,
                    breakers=breakers,
                    request_id=request_id,
                )
            except Exception:
                if deadline is not None and deadline.expired:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "IM_INTERACTIVE_BUDGET_EXHAUSTED",
                            "error": "聊天列表暂时不可用，请稍后重试",
                            "retryable": True,
                            "retry_after": 2,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        503,
                        extra_headers={"Retry-After": "2"},
                    )
                try:
                    result = _dependency_call(
                        app.im.history_conversations,
                        q("page", "1"),
                        provider="beibeiwu",
                        domain="im-read",
                        breakers=breakers,
                        timeout=provider_timeout or None,
                        deadline=deadline,
                        request_id=request_id,
                    )
                except DependencyCircuitOpen as exc:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "UPSTREAM_CONVERSATIONS_UNAVAILABLE",
                            "error": "聊天列表暂时不可用，请稍后重试",
                            "retryable": True,
                            "retry_after": exc.retry_after,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        503,
                        extra_headers={"Retry-After": str(exc.retry_after)},
                    )
                except Exception:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "UPSTREAM_CONVERSATIONS_UNAVAILABLE",
                            "error": "聊天列表暂时不可用，请稍后重试",
                            "retryable": True,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        502,
                    )
                if _is_html_protocol_result(result) or not getattr(result, "ok", False):
                    retry_after = (
                        int(breakers.retry_after("beibeiwu", "im-read"))
                        if breakers is not None
                        else 0
                    )
                    return self.ok(
                        {
                            "ok": False,
                            "code": "UPSTREAM_CONVERSATIONS_UNAVAILABLE",
                            "error": "聊天列表暂时不可用，请稍后重试",
                            "retryable": True,
                            "retry_after": retry_after,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        502,
                        extra_headers=(
                            {"Retry-After": str(retry_after)}
                            if retry_after > 0
                            else None
                        ),
                    )
                payload = conversation_envelope(app, result, u.profile_cache)
            summary_loader = getattr(self, "_request_conversation_summary_loader", None)
            if callable(summary_loader):
                try:
                    peers = [
                        str(item.get("peer_id") or item.get("conversation_user") or "").strip()
                        for item in payload.get("items", [])
                        if isinstance(item, Mapping)
                    ]
                    _attach_cached_conversation_summaries(
                        payload.get("items", []),
                        summary_loader(peers),
                    )
                    payload["list"] = payload.get("items", [])
                except Exception:
                    pass
            conversation_peers = getattr(u, "conversation_message_peers", None)
            if conversation_peers is None:
                conversation_peers = set()
                setattr(u, "conversation_message_peers", conversation_peers)
            conversation_peers.update(
                peer
                for peer in _message_peer_ids(payload)
                if peer != str(app.session.uid or "")
            )
            return self.ok(payload)
        if path == "/api/im/messages":
            peer = q("peer") or q("uid") or q("yourid")
            if not peer:
                return self.ok({"ok": False, "error": "缺少聊天对象 UID"}, 400)
            summary_only = q("summary", "0") == "1"
            raw_around_time = str(q("at", "") or "").strip()
            around_time = int(raw_around_time) if raw_around_time.isdigit() else None
            raw_before_time = str(q("before", "") or "").strip()
            before_time = int(raw_before_time) if raw_before_time.isdigit() else None
            if before_time is not None and before_time > 1_000_000_000_000:
                before_time //= 1000
            if summary_only and around_time is None:
                return self.ok({"ok": False, "error": "缺少会话消息时间"}, 400)
            capabilities = Handler.web_user_capabilities(self, u)
            if not Handler.can_view_message_peer(self, u, peer):
                return Handler.deny_private_message(self, capabilities)
            budget_config = dict(
                getattr(self, "_request_budget_config", None) or {}
            )
            im_budget = float(budget_config.get("im_budget_seconds") or 0)
            tim_timeout = float(
                budget_config.get("tim_timeout_seconds") or 0
            )
            provider_timeout = float(
                budget_config.get("provider_timeout_seconds") or 0
            )
            deadline = RequestDeadline(im_budget) if im_budget > 0 else None
            breakers = getattr(self, "_request_dependency_breakers", None)
            request_id = str(getattr(self, "_request_id", "") or "")
            try:
                payload = _tim_roaming_message_envelope(
                    u.native.tim_rest,
                    str(app.session.uid or ""),
                    str(peer),
                    max_messages=50 if summary_only else 200,
                    around_time=around_time if summary_only else None,
                    before_time=before_time if not summary_only else None,
                    include_read_state=not summary_only,
                    deadline=deadline,
                    call_timeout=tim_timeout or None,
                    breakers=breakers,
                    request_id=request_id,
                )
            except Exception:
                if deadline is not None and deadline.expired:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "IM_INTERACTIVE_BUDGET_EXHAUSTED",
                            "error": {
                                "title": "上游聊天记录暂时不可用",
                                "detail": "请求预算已用尽，请稍后重试",
                            },
                            "retryable": True,
                            "retry_after": 2,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        503,
                        extra_headers={"Retry-After": "2"},
                    )
                try:
                    result = _dependency_call(
                        app.im.history_messages,
                        peer,
                        provider="beibeiwu",
                        domain="im-read",
                        breakers=breakers,
                        timeout=provider_timeout or None,
                        deadline=deadline,
                        request_id=request_id,
                    )
                except DependencyCircuitOpen as exc:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "UPSTREAM_HISTORY_UNAVAILABLE",
                            "error": {
                                "title": "上游聊天记录暂时不可用",
                                "detail": "上游服务正在恢复，请稍后重试",
                            },
                            "retryable": True,
                            "retry_after": exc.retry_after,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        503,
                        extra_headers={"Retry-After": str(exc.retry_after)},
                    )
                except Exception:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "UPSTREAM_HISTORY_UNAVAILABLE",
                            "error": {
                                "title": "上游聊天记录暂时不可用",
                                "detail": "请稍后重试",
                            },
                            "retryable": True,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        502,
                    )
                if _is_html_protocol_result(result) or not getattr(result, "ok", False):
                    retry_after = (
                        int(breakers.retry_after("beibeiwu", "im-read"))
                        if breakers is not None
                        else 0
                    )
                    return self.ok(
                        {
                            "ok": False,
                            "code": "UPSTREAM_HISTORY_UNAVAILABLE",
                            "error": {
                                "title": "上游聊天记录暂时不可用",
                                "detail": "已改用服务器归档的聊天记录",
                            },
                            "retryable": True,
                            "retry_after": retry_after,
                            "items": [],
                            "list": [],
                            "count": 0,
                        },
                        502,
                        extra_headers=(
                            {"Retry-After": str(retry_after)}
                            if retry_after > 0
                            else None
                        ),
                    )
                payload = RE(result, "message")
            if summary_only:
                latest_items = sorted(
                    [item for item in payload.get("items", []) if isinstance(item, Mapping)],
                    key=_tim_message_sort_key,
                )[-1:]
                payload.update(
                    items=latest_items,
                    list=latest_items,
                    count=len(latest_items),
                    summary=True,
                )
            return self.ok(payload)

        if path == "/api/heartbeat":
            return self.ok(u.heartbeat.status() if u.heartbeat else {"running": False})
        if path == "/api/face/status":
            return self.ok(u.native.face.status_hint())
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
            except ProviderUnavailable as e:
                return self.ok(
                    {
                        "ok": False,
                        "code": "UPSTREAM_AUTH_UNAVAILABLE",
                        "error": _safe_error(
                            e,
                            "登录服务暂时不可用，请稍后重试",
                        ),
                        "retryable": True,
                    },
                    503,
                )
            except ProviderAuthenticationRejected as e:
                return self.ok(
                    {
                        "ok": False,
                        "code": "UPSTREAM_AUTH_REJECTED",
                        "error": _safe_error(e, "账号或密码验证失败"),
                        "retryable": False,
                    },
                    401,
                )
            except ProviderUpstreamInterrupted as e:
                # 上游连接中途被打断（如陈旧 keep-alive 被对端关闭）。
                # 给出可重试信号；code 刻意区别于 UPSTREAM_AUTH_UNAVAILABLE，
                # 确保 api.py 的本地密码回退闸门不被瞬态连接中断触发。
                return self.ok(
                    {
                        "ok": False,
                        "code": "UPSTREAM_AUTH_INTERRUPTED",
                        "error": _safe_error(
                            e,
                            "登录服务连接中断，请稍后重试",
                        ),
                        "retryable": True,
                    },
                    503,
                )
            except Exception as e:
                return self.ok({"ok": False, "error": _safe_error(e, "登录失败")}, 400)
            # Login response latency must depend only on authentication and
            # durable session persistence.  Home/profile/IM data is loaded by
            # the product routes after the browser enters the application.
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
            phone = str(data.get("phone") or "").strip()
            if not phone:
                return self.ok({"ok": False, "error": "请输入手机号"}, 400)
            if not self._allow_sensitive_action(
                "sms-send", phone, limit=3, window_sec=300.0
            ):
                return
            return self.ok(R(STORE.send_sms(phone), empty_ok=True))

        if path == "/api/auth/sms-login":
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
                    user.app.set_device(seed=phone)
                    r = user.app.auth.sms_login(phone, code)
                    if not r.ok or not user.app.session.logged_in:
                        if created:
                            STORE.drop(user.web_sid)
                        return self.ok(
                            {**R(r), "ok": False, "error": r.message or "登录失败"},
                            400,
                        )
                    user.app.session.password = ""
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
                coordinator = getattr(
                    self,
                    "_request_presence_coordinator",
                    None,
                )
                if coordinator is not None:
                    budget_config = dict(
                        getattr(self, "_request_budget_config", None) or {}
                    )
                    timeout = float(
                        budget_config.get("presence_timeout_seconds") or 0
                    )

                    def heartbeat_once() -> Any:
                        deadline = RequestDeadline(timeout) if timeout > 0 else None
                        return _dependency_call(
                            u.heartbeat_once,
                            provider="beibeiwu",
                            domain="presence",
                            breakers=getattr(
                                self,
                                "_request_dependency_breakers",
                                None,
                            ),
                            timeout=timeout or None,
                            deadline=deadline,
                            request_id=str(
                                getattr(self, "_request_id", "") or ""
                            ),
                        )

                    accepted = coordinator.submit(
                        str(app.session.uid or sid or ""),
                        "heartbeat",
                        heartbeat_once,
                    )
                    if not accepted:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "PRESENCE_QUEUE_UNAVAILABLE",
                                "error": "在线状态更新暂时繁忙",
                                "retryable": True,
                                "retry_after": 2,
                            },
                            503,
                            extra_headers={"Retry-After": "2"},
                        )
                    return self.ok(
                        {
                            "ok": True,
                            "accepted": True,
                            "queued": True,
                            "operation": "heartbeat",
                        },
                        202,
                    )
                return self.ok(u.heartbeat_once())

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
            if path == "/api/social/add-friend":
                current_uid = str(app.session.uid or "").strip()
                target_uid = str(data.get("uid") or data.get("target_uid") or "").strip()
                leave_word = str(
                    data.get("leave_word")
                    or data.get("yourwords")
                    or data.get("message")
                    or ""
                ).strip()
                if not target_uid or len(target_uid) > 128:
                    return self.ok({"ok": False, "error": "缺少有效的目标用户 UID"}, 400)
                if target_uid == current_uid:
                    return self.ok({"ok": False, "error": "不能申请添加自己为好友"}, 400)
                if len(leave_word) > 100:
                    return self.ok({"ok": False, "error": "好友申请留言不能超过 100 个字符"}, 400)
                if not self._allow_sensitive_action(
                    "friend-request",
                    current_uid or self.sid() or "-",
                    limit=10,
                    window_sec=60.0,
                ):
                    return
                return self.ok(
                    R(
                        app.social.add_friend(
                            target_uid,
                            leave_word or "你好，想和你成为好友",
                        ),
                        empty_ok=True,
                    )
                )
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
                    _, remaining, _metadata = _friend_applications(
                        app, current_uid
                    )
                    pending = any(
                        (
                            str(item.get("id") or "") == applicant_uid
                            or str(item.get("apply_id") or "") == apply_id
                        )
                        and item.get("direction") == "incoming"
                        and item.get("status") == "pending"
                        for item in remaining
                    )
                    if not pending:
                        verified = True
                        break
                payload = R(result)
                payload["verified"] = verified
                payload["application_status"] = "accepted" if verified else "pending"
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

            # Keep both the conversation unread counter and TUIKit's explicit
            # per-message read receipts in sync. The latter is required for the
            # sender's "已读/未读" indicator and is not covered by
            # openim/admin_set_msg_read.
            if path == "/api/im/read":
                raw_peers = data.get("peers")
                if isinstance(raw_peers, list):
                    peer_uids = list(
                        dict.fromkeys(
                            str(peer or "").strip()
                            for peer in raw_peers[:100]
                            if str(peer or "").strip()
                        )
                    )
                else:
                    peer_uid = str(
                        data.get("peer") or data.get("uid") or data.get("to") or ""
                    ).strip()
                    peer_uids = [peer_uid] if peer_uid else []
                account_uid = str(app.session.uid or "").strip()
                if not peer_uids or not account_uid:
                    return self.ok({"ok": False, "error": "缺少聊天对象 UID"}, 400)
                capabilities = Handler.web_user_capabilities(self, u)
                for peer_uid in peer_uids:
                    if not Handler.can_view_message_peer(self, u, peer_uid):
                        return Handler.deny_private_message(self, capabilities)
                Handler.acquire_peer_request_gate(self, u, peer_uids)
                supplied_receipts = data.get("receipt_messages")
                receipt_messages = (
                    [
                        dict(item)
                        for item in supplied_receipts[:300]
                        if isinstance(item, Mapping)
                    ]
                    if len(peer_uids) == 1 and isinstance(supplied_receipts, list)
                    else []
                )
                local_read_peers: List[str] = []
                local_read_counts: Dict[str, int] = {}
                local_only_mode = (
                    str(getattr(u, "authentication_source", "") or "")
                    == "local"
                )
                local_read_marker = getattr(
                    self, "_request_local_read_marker", None
                )
                if local_only_mode and not callable(local_read_marker):
                    return self.ok(
                        {
                            "ok": False,
                            "code": "LOCAL_READ_SERVICE_UNAVAILABLE",
                            "error": "Web 本地已读服务未就绪",
                            "retryable": True,
                        },
                        503,
                    )
                if callable(local_read_marker):
                    try:
                        for peer_uid in peer_uids:
                            local_count = local_read_marker(peer_uid)
                            if local_count is None:
                                continue
                            local_read_peers.append(peer_uid)
                            local_read_counts[peer_uid] = max(
                                0, int(local_count or 0)
                            )
                    except Exception:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "LOCAL_READ_SERVICE_UNAVAILABLE",
                                "error": "Web 本地已读状态暂时无法保存",
                                "retryable": True,
                            },
                            503,
                        )
                local_unavailable_peers = [
                    peer_uid
                    for peer_uid in peer_uids
                    if peer_uid not in local_read_peers
                ]
                if local_only_mode and local_unavailable_peers:
                    # A Web-local session must never fall through to the TIM
                    # compatibility client.  The peer may only have a legacy
                    # archive and therefore has no canonical unread state to
                    # mutate locally.
                    return self.ok(
                        {
                            "ok": False,
                            "code": "PEER_NOT_MIGRATED",
                            "error": "部分聊天对象尚未完成 Web 消息迁移",
                            "retryable": False,
                            "read_peers": local_read_peers,
                            "failed_peers": local_unavailable_peers,
                            "local_read_peers": local_read_peers,
                            "local_read_counts": local_read_counts,
                            "compatibility_sync_skipped_peers": peer_uids,
                        },
                        409,
                    )
                read_peers: List[str] = []
                conversation_read_peers: List[str] = []
                receipt_synced_peers: List[str] = []
                failed_peers: List[str] = []
                conversation_failed_peers: List[str] = []
                receipt_failed_peers: List[str] = []
                receipt_counts: Dict[str, int] = {}
                skipped_compatibility_peers = (
                    list(local_read_peers) if local_only_mode else []
                )
                remote_read_peers = [
                    peer_uid
                    for peer_uid in peer_uids
                    if peer_uid not in skipped_compatibility_peers
                ]

                read_sync_enqueuer = getattr(
                    self,
                    "_request_read_sync_enqueuer",
                    None,
                )
                if callable(read_sync_enqueuer) and remote_read_peers:
                    try:
                        queued = read_sync_enqueuer(
                            account_uid=account_uid,
                            peers=remote_read_peers,
                            receipt_messages=receipt_messages,
                        )
                    except Exception:
                        if set(remote_read_peers).issubset(set(local_read_peers)):
                            return self.ok(
                                {
                                    "ok": True,
                                    "read": True,
                                    "read_peers": local_read_peers,
                                    "count": len(local_read_peers),
                                    "local_read_peers": local_read_peers,
                                    "local_read_counts": local_read_counts,
                                    "compatibility_sync": "failed",
                                    "compatibility_sync_skipped_peers": skipped_compatibility_peers,
                                }
                            )
                        return self.ok(
                            {
                                "ok": False,
                                "code": "IM_READ_QUEUE_UNAVAILABLE",
                                "error": "消息已读同步队列暂时不可用",
                                "retryable": True,
                                "retry_after": 2,
                                "read_peers": local_read_peers,
                                "failed_peers": remote_read_peers,
                                "local_read_peers": local_read_peers,
                                "local_read_counts": local_read_counts,
                            },
                            503,
                            extra_headers={"Retry-After": "2"},
                        )
                    queued_peers = list(
                        dict.fromkeys(
                            str(peer or "").strip()
                            for peer in (
                                queued.get("accepted_peers", remote_read_peers)
                                if isinstance(queued, Mapping)
                                else remote_read_peers
                            )
                            if str(peer or "").strip()
                        )
                    )
                    accepted_peers = list(
                        dict.fromkeys(local_read_peers + queued_peers)
                    )
                    return self.ok(
                        {
                            "ok": True,
                            "read": True,
                            "accepted": True,
                            "queued": True,
                            "durable": bool(
                                queued.get("durable", True)
                                if isinstance(queued, Mapping)
                                else True
                            ),
                            "read_peers": local_read_peers,
                            "accepted_peers": accepted_peers,
                            "queued_peers": queued_peers,
                            "count": len(accepted_peers),
                            "local_read_peers": local_read_peers,
                            "local_read_counts": local_read_counts,
                            "compatibility_sync": "queued",
                            "compatibility_sync_skipped_peers": skipped_compatibility_peers,
                        },
                        202,
                    )

                def mark_peer_read(peer_uid: str) -> Tuple[str, Any, Any]:
                    conversation_result = u.native.tim_rest.mark_c2c_read(
                        account_uid,
                        peer_uid,
                    )
                    receipt_result = u.native.tim_rest.sync_c2c_message_read_receipts(
                        account_uid,
                        peer_uid,
                        messages=receipt_messages if len(peer_uids) == 1 and receipt_messages else None,
                    )
                    return peer_uid, conversation_result, receipt_result

                if len(remote_read_peers) == 1:
                    read_results = [mark_peer_read(remote_read_peers[0])]
                elif not remote_read_peers:
                    read_results = []
                else:
                    with ThreadPoolExecutor(
                        max_workers=min(5, len(remote_read_peers))
                    ) as executor:
                        read_results = list(
                            executor.map(mark_peer_read, remote_read_peers)
                        )
                read_peers.extend(skipped_compatibility_peers)
                for peer_uid, conversation_result, receipt_result in read_results:
                    conversation_ok = bool(getattr(conversation_result, "ok", False))
                    receipt_ok = bool(getattr(receipt_result, "ok", False))
                    if conversation_ok:
                        conversation_read_peers.append(peer_uid)
                    else:
                        conversation_failed_peers.append(peer_uid)
                    if receipt_ok:
                        receipt_synced_peers.append(peer_uid)
                    else:
                        receipt_failed_peers.append(peer_uid)
                    receipt_data = getattr(receipt_result, "data", None)
                    receipt_data = receipt_data if isinstance(receipt_data, Mapping) else {}
                    try:
                        receipt_counts[peer_uid] = max(
                            0,
                            int(receipt_data.get("receipt_count") or 0),
                        )
                    except (TypeError, ValueError, OverflowError):
                        receipt_counts[peer_uid] = 0
                    if (
                        conversation_ok and receipt_ok
                    ) or peer_uid in local_read_peers:
                        read_peers.append(peer_uid)
                    else:
                        failed_peers.append(peer_uid)
                if failed_peers:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "IM_READ_REPORT_FAILED",
                            "error": "消息已读状态暂时无法同步",
                            "read_peers": read_peers,
                            "failed_peers": failed_peers,
                            "conversation_read_peers": conversation_read_peers,
                            "conversation_failed_peers": conversation_failed_peers,
                            "receipt_synced_peers": receipt_synced_peers,
                            "receipt_failed_peers": receipt_failed_peers,
                            "local_read_peers": local_read_peers,
                            "local_read_counts": local_read_counts,
                            "compatibility_sync_skipped_peers": skipped_compatibility_peers,
                            "receipt_counts": receipt_counts,
                            "receipt_count": sum(receipt_counts.values()),
                        },
                        502,
                    )
                return self.ok(
                    {
                        "ok": True,
                        "read": True,
                        "read_peers": read_peers,
                        "count": len(read_peers),
                        "conversation_read_peers": conversation_read_peers,
                        "local_read_peers": local_read_peers,
                        "local_read_counts": local_read_counts,
                        "compatibility_sync_skipped_peers": skipped_compatibility_peers,
                        "receipt_synced_peers": receipt_synced_peers,
                        "receipt_counts": receipt_counts,
                        "receipt_count": sum(receipt_counts.values()),
                    }
                )

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
                capabilities = Handler.web_user_capabilities(self, u)
                if not Handler.can_message_peer(self, u, to_uid):
                    return Handler.deny_private_message(self, capabilities)
                quote = normalize_message_quote(data.get("quote"))
                client_message_id = str(
                    data.get("client_message_id")
                    or data.get("client_message_key")
                    or data.get("idempotency_key")
                    or ""
                ).strip()
                if not client_message_id:
                    entropy = (
                        f"{from_uid}\x00{to_uid}\x00{text}\x00{time.time_ns()}"
                    ).encode("utf-8")
                    client_message_id = (
                        "legacy-web-" + hashlib.sha256(entropy).hexdigest()[:48]
                    )

                local_fallback_reason = ""
                local_sender = getattr(self, "_request_local_text_sender", None)
                local_only_mode = (
                    str(getattr(u, "authentication_source", "") or "")
                    == "local"
                )
                if local_only_mode and not callable(local_sender):
                    return self.ok(
                        {
                            "ok": False,
                            "code": "LOCAL_MESSAGE_SERVICE_UNAVAILABLE",
                            "error": "Web 本地消息服务未就绪",
                            "retryable": True,
                        },
                        503,
                    )
                if callable(local_sender):
                    try:
                        local_outcome = local_sender(
                            to_uid,
                            text,
                            client_message_id,
                            quote,
                        )
                    except Exception:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "LOCAL_MESSAGE_SERVICE_UNAVAILABLE",
                                "error": "Web 本地消息服务暂时不可用，请稍后重试",
                                "retryable": True,
                            },
                            503,
                        )
                    if isinstance(local_outcome, Mapping):
                        if local_outcome.get("handled") is True:
                            local_payload = dict(local_outcome.get("payload") or {})
                            local_status = int(local_outcome.get("status") or 200)
                            if local_payload.get("ok") is True:
                                conversation_peers = getattr(
                                    u, "conversation_message_peers", None
                                )
                                if conversation_peers is None:
                                    conversation_peers = set()
                                    setattr(
                                        u,
                                        "conversation_message_peers",
                                        conversation_peers,
                                    )
                                conversation_peers.add(to_uid)
                            return self.ok(local_payload, local_status)
                        local_fallback_reason = str(
                            local_outcome.get("reason") or ""
                        ).strip()

                if local_only_mode:
                    if not local_fallback_reason:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "LOCAL_MESSAGE_SERVICE_UNAVAILABLE",
                                "error": "Web 本地消息服务未返回有效结果",
                                "retryable": True,
                            },
                            503,
                        )
                    peer_not_migrated = (
                        local_fallback_reason == "peer_not_migrated"
                    )
                    return self.ok(
                        {
                            "ok": False,
                            "code": (
                                "PEER_NOT_MIGRATED"
                                if peer_not_migrated
                                else "LOCAL_MESSAGE_IDENTITY_NOT_READY"
                            ),
                            "error": (
                                "对方尚未完成 Web 消息迁移，本地通道无法投递"
                                if peer_not_migrated
                                else "当前账号尚未完成 Web 消息迁移"
                            ),
                            "retryable": False,
                        },
                        409,
                    )

                quote_cloud_data = encode_message_quote(quote)
                send_options = (
                    {"cloud_custom_data": quote_cloud_data}
                    if quote_cloud_data
                    else {}
                )
                r = u.native.tim_rest.send_text(
                    from_uid,
                    to_uid,
                    text,
                    **send_options,
                )
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
                    conversation_peers = getattr(u, "conversation_message_peers", None)
                    if conversation_peers is None:
                        conversation_peers = set()
                        setattr(u, "conversation_message_peers", conversation_peers)
                    conversation_peers.add(to_uid)
                elif local_fallback_reason:
                    out["code"] = (
                        "PEER_NOT_MIGRATED"
                        if local_fallback_reason == "peer_not_migrated"
                        else "LOCAL_MESSAGE_IDENTITY_NOT_READY"
                    )
                    out["error"] = (
                        "对方尚未完成 Web 消息迁移，当前外部消息通道也无法投递"
                        if local_fallback_reason == "peer_not_migrated"
                        else "当前账号尚未完成 Web 消息迁移，且外部消息通道暂时不可用"
                    )
                    out["retryable"] = True
                return self.ok(
                    out,
                    200 if r.ok else (409 if local_fallback_reason else 400),
                )

            if path == "/api/im/rest/revoke":
                to_uid = str(data.get("to") or data.get("peer") or data.get("uid") or "").strip()
                canonical_message_id = str(
                    data.get("canonical_message_id") or ""
                ).strip()
                msg_key = str(
                    canonical_message_id
                    or data.get("msg_key")
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
                local_revoker = getattr(self, "_request_local_text_revoker", None)
                local_only_mode = (
                    str(getattr(u, "authentication_source", "") or "") == "local"
                )
                local_fallback_reason = ""
                if callable(local_revoker):
                    try:
                        local_outcome = local_revoker(to_uid, msg_key)
                    except Exception:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "LOCAL_MESSAGE_REVOKE_UNAVAILABLE",
                                "error": "Web 本地消息撤回服务暂时不可用，请稍后重试",
                                "retryable": True,
                            },
                            503,
                        )
                    if isinstance(local_outcome, Mapping):
                        if local_outcome.get("handled") is True:
                            return self.ok(
                                dict(local_outcome.get("payload") or {}),
                                int(local_outcome.get("status") or 200),
                            )
                        local_fallback_reason = str(
                            local_outcome.get("reason") or ""
                        ).strip()

                if canonical_message_id:
                    return self.ok(
                        {
                            "ok": False,
                            "code": (
                                "LOCAL_MESSAGE_IDENTITY_NOT_READY"
                                if local_fallback_reason == "sender_not_migrated"
                                else "LOCAL_MESSAGE_NOT_FOUND"
                            ),
                            "error": (
                                "当前账号尚未完成 Web 消息迁移"
                                if local_fallback_reason == "sender_not_migrated"
                                else "Web 本地消息不存在"
                            ),
                            "retryable": False,
                        },
                        409 if local_fallback_reason == "sender_not_migrated" else 404,
                    )
                if local_only_mode:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "LEGACY_MESSAGE_REVOKE_UNAVAILABLE",
                            "error": "当前消息没有 Web 本地记录，且外部消息通道已停用",
                            "retryable": False,
                        },
                        409,
                    )
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
                    capabilities = Handler.web_user_capabilities(self, u)
                    if not Handler.can_message_peer(self, u, target_id):
                        return Handler.deny_private_message(self, capabilities)
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
                        conversation_peers = getattr(u, "conversation_message_peers", None)
                        if conversation_peers is None:
                            conversation_peers = set()
                            setattr(u, "conversation_message_peers", conversation_peers)
                        conversation_peers.add(target_id)
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
                    account_uid = str(app.session.uid or "").strip()
                    if _flash_reveal_backend(
                        "flash_reveal_acknowledged",
                        account_uid,
                        unique_id,
                        default=False,
                    ):
                        expired_message = "闪图已查看、已失效或不存在"
                        return self.ok(
                            {
                                "ok": False,
                                "message": expired_message,
                                "error": expired_message,
                                "uniqueid": unique_id,
                                "photo_status": "acknowledged",
                            },
                            410,
                        )
                    cached_path = str(
                        _flash_reveal_backend(
                            "cached_flash_reveal",
                            account_uid,
                            unique_id,
                            default="",
                        )
                        or ""
                    )
                    cached_photo = F.photo_from_path(cached_path)
                    if cached_photo["url"]:
                        return self.ok(
                            {
                                "ok": True,
                                "uniqueid": unique_id,
                                "path": cached_photo["path"],
                                "url": cached_photo["url"],
                                "photo_url": cached_photo["url"],
                                "photo_status": "0",
                                "message": "闪图已获取",
                                "error": None,
                                "cached": True,
                            }
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
                        remembered = bool(
                            _flash_reveal_backend(
                                "remember_flash_reveal",
                                account_uid,
                                unique_id,
                                photo["path"],
                                default=False,
                            )
                        )
                        if not remembered and _flash_reveal_backend(
                            "flash_reveal_acknowledged",
                            account_uid,
                            unique_id,
                            default=False,
                        ):
                            expired_message = "闪图已查看、已失效或不存在"
                            return self.ok(
                                {
                                    "ok": False,
                                    "message": expired_message,
                                    "error": expired_message,
                                    "uniqueid": unique_id,
                                    "photo_status": "acknowledged",
                                },
                                410,
                            )
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

            if path == "/api/im/flash/ack":
                if not self._allow_sensitive_action(
                    "flash-ack",
                    str(app.session.uid or sid or "-"),
                    limit=120,
                    window_sec=60.0,
                ):
                    return
                try:
                    unique_id = F.validate_unique_id(
                        data.get("uniqueid") or data.get("uniqueId") or data.get("unique_id")
                    )
                    acknowledged = bool(
                        _flash_reveal_backend(
                            "acknowledge_flash_reveal",
                            str(app.session.uid or "").strip(),
                            unique_id,
                            default=False,
                        )
                    )
                    return self.ok(
                        {
                            "ok": True,
                            "uniqueid": unique_id,
                            "acknowledged": acknowledged,
                        }
                    )
                except F.FlashPhotoError as exc:
                    return self.ok({"ok": False, "error": exc.message}, exc.status)

            if path == "/api/social/blacklist-add":
                payload = R(app.social.add_blacklist(**_params(data)))
                if payload.get("ok"):
                    blocked = getattr(u, "blocked_message_peers", None)
                    if blocked is None:
                        blocked = set()
                        setattr(u, "blocked_message_peers", blocked)
                    blocked.update(_message_peer_ids({"items": [data]}))
                return self.ok(payload)
            if path == "/api/social/blacklist-del":
                payload = R(app.social.delete_blacklist(**_params(data)))
                if payload.get("ok"):
                    blocked = getattr(u, "blocked_message_peers", None)
                    if blocked is not None:
                        blocked.difference_update(_message_peer_ids({"items": [data]}))
                return self.ok(payload)
            if path == "/api/moments/view":
                postid = str(
                    data.get("postid") or data.get("post_id") or data.get("id") or ""
                ).strip()
                if (
                    not postid
                    or postid == "0"
                    or not postid.isdigit()
                    or len(postid) > 32
                ):
                    return self.ok({"ok": False, "error": "动态编号无效"}, 400)
                assist_task = str(data.get("assist_task") or "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                assist_deadline = (
                    time.monotonic() + MOMENT_VIEW_TASK_DEADLINE_SEC
                    if assist_task
                    else None
                )
                payload = R(app.social.record_post_view(postid), empty_ok=True)
                payload["post_id"] = postid
                if assist_task:
                    if payload.get("ok"):
                        try:
                            payload["task_assist"] = _assist_moment_view_task(
                                app,
                                postid,
                                deadline=assist_deadline,
                            )
                        except Exception:
                            payload["task_assist"] = {
                                "checked": False,
                                "task_found": False,
                                "completed": False,
                                "retryable": True,
                                "state": "unavailable",
                                "requested_repeat_views": 0,
                                "successful_repeat_views": 0,
                            }
                    else:
                        payload["task_assist"] = {
                            "checked": False,
                            "task_found": False,
                            "completed": False,
                            "retryable": True,
                            "state": "initial_view_failed",
                            "requested_repeat_views": 0,
                            "successful_repeat_views": 0,
                        }
                return self.ok(payload)
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
            if path == "/api/match/voice/bootstrap":
                credentials = _voice_rong_credentials(u)
                if credentials is None:
                    issued = u.native.im.rong_register()
                    token = str(getattr(issued, "token", "") or "").strip()
                    issued_uid = str(getattr(issued, "user_id", "") or app.session.uid or "").strip()
                    if not bool(getattr(issued, "ok", False)) or token in {
                        "",
                        "123",
                        "null",
                        "None",
                    }:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "VOICE_RONG_TOKEN_UNAVAILABLE",
                                "error": "语音服务身份暂时不可用，请稍后重试",
                                "sdk": dict(VOICE_WEB_SDK),
                            },
                            503,
                        )
                    if issued_uid != str(app.session.uid or ""):
                        return self.ok(
                            {
                                "ok": False,
                                "code": "VOICE_RONG_USER_MISMATCH",
                                "error": "语音服务身份与当前账号不一致，请重新登录",
                            },
                            409,
                        )
                    credentials = {
                        "app_key": str(getattr(issued, "app_key", "") or ""),
                        "user_id": issued_uid,
                        "token": token,
                        "nickname": str(getattr(issued, "nickname", "") or ""),
                        "portrait": str(getattr(issued, "portrait", "") or ""),
                    }
                    setattr(u, "voice_rong_credentials", dict(credentials))
                    setattr(u, "voice_rong_credentials_at", time.time())
                return self.ok(
                    {
                        "ok": True,
                        "credentials": {
                            "appKey": credentials["app_key"],
                            "userId": credentials["user_id"],
                            "token": credentials["token"],
                            "nickname": credentials["nickname"],
                            "portrait": credentials["portrait"],
                        },
                        "sdk": dict(VOICE_WEB_SDK),
                    }
                )
            if path == "/api/match/voice/start":
                if _voice_rong_credentials(u) is None:
                    return self.ok(
                        {
                            "ok": False,
                            "code": "VOICE_SERVICE_NOT_READY",
                            "error": "请先连接语音服务，再开始匹配",
                        },
                        409,
                    )
                with u.lock:
                    current = _voice_match_state(u)
                    if current["stale"]:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "VOICE_MATCH_STALE_REQUIRES_CANCEL",
                                "error": "上次语音匹配状态已超时，请先取消后再重新开始",
                                **current,
                            },
                            409,
                        )
                    if current["active"]:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "VOICE_MATCH_ALREADY_ACTIVE",
                                "error": "当前已有进行中的语音匹配",
                                **current,
                            },
                            409,
                        )
                    _set_voice_match_state(u, "calling")
                try:
                    result = app.match.start_voice(id_=app.session.uid)
                except Exception:
                    with u.lock:
                        _set_voice_match_state(u, "idle")
                    raise
                normalized = app.match.normalize_voice_result(result)
                outcome = str(normalized.get("outcome") or "error")
                if outcome == "insufficient":
                    with u.lock:
                        state = _set_voice_match_state(u, "idle")
                    return self.ok(
                        {
                            "ok": False,
                            "code": "VOICE_MATCH_QUOTA_REQUIRED",
                            "error": "语音匹配次数或匹配卡不足",
                            "outcome": outcome,
                            **state,
                        },
                        409,
                    )
                if outcome == "waiting":
                    with u.lock:
                        state = _set_voice_match_state(u, "waiting")
                    return self.ok(
                        {
                            "ok": True,
                            "outcome": outcome,
                            "message": "正在等待另一位用户加入",
                            **state,
                        }
                    )
                if outcome == "matched":
                    target_raw = normalized.get("target")
                    target = N.normalize_user(target_raw) or {}
                    target_id = str(
                        target.get("id")
                        or (target_raw or {}).get("id")
                        or (target_raw or {}).get("uid")
                        or (target_raw or {}).get("userId")
                        or ""
                    ).strip()
                    if not target_id:
                        with u.lock:
                            _set_voice_match_state(u, "idle")
                        return self.ok(
                            {
                                "ok": False,
                                "code": "VOICE_MATCH_TARGET_INVALID",
                                "error": "匹配结果缺少用户信息，请稍后重试",
                            },
                            502,
                        )
                    target["id"] = target_id
                    target.setdefault("uid", target_id)
                    with u.lock:
                        state = _set_voice_match_state(u, "matched", target=target)
                        match_peers = getattr(u, "match_message_peers", None)
                        if match_peers is None:
                            match_peers = set()
                            setattr(u, "match_message_peers", match_peers)
                        match_peers.add(target_id)
                    payload = {
                        "ok": True,
                        "outcome": outcome,
                        "message": "已匹配到用户，正在发起语音通话",
                        "target": target,
                        "items": [target],
                        "message_peers": [target_id],
                        "count": 1,
                        **state,
                    }
                    recorded_peers = _remember_web_match_history(u, path, payload)
                    if recorded_peers:
                        payload["history_saved"] = True
                        recorder = getattr(self, "_request_match_history_recorder", None)
                        if callable(recorder):
                            try:
                                recorder(path, payload)
                            except Exception:
                                payload["history_saved"] = False
                                payload["history_warning"] = (
                                    "匹配成功，但历史记录暂时没有保存，请保留当前匹配结果"
                                )
                    return self.ok(payload)
                with u.lock:
                    _set_voice_match_state(u, "idle")
                return self.ok(
                    {
                        "ok": False,
                        "code": "VOICE_MATCH_RESPONSE_INVALID",
                        "error": "语音匹配返回了无法识别的结果，请稍后重试",
                    },
                    502,
                )
            if path == "/api/match/voice/cancel":
                # A returned match is no longer in the Redis waiting queue.
                # Clearing that local target must therefore not depend on
                # removeXiaobeiMatch returning success (the APK does not call
                # remove after receiving a user either).  Keeping it blocked
                # on a false remove response would prevent all later matches.
                force_remote = _as_bool(data.get("force_remote"))
                with u.lock:
                    stored = getattr(u, "voice_match_state", None)
                    stored_state = (
                        str(stored.get("state") or "idle")
                        if isinstance(stored, dict)
                        else "idle"
                    )
                    if stored_state == "matched" or (
                        stored_state == "idle" and not force_remote
                    ):
                        state = _set_voice_match_state(u, "idle")
                        return self.ok(
                            {
                                "ok": True,
                                "code": "",
                                "message": (
                                    "已放弃当前匹配用户"
                                    if stored_state == "matched"
                                    else "当前没有等待中的语音匹配"
                                ),
                                "error": None,
                                "remote_ok": True,
                                "remote_required": False,
                                "forced_remote": False,
                                **state,
                            }
                        )
                result = app.match.cancel_voice(id_=app.session.uid)
                remote = R(result, empty_ok=True)
                with u.lock:
                    state = (
                        _set_voice_match_state(u, "idle")
                        if remote.get("ok")
                        else _voice_match_state(u)
                    )
                return self.ok(
                    {
                        "ok": bool(remote.get("ok")),
                        "code": str(remote.get("code") or ""),
                        "message": "已取消语音匹配" if remote.get("ok") else "本地等待已结束，但服务端取消状态未确认",
                        "error": None if remote.get("ok") else "服务端取消状态未确认，请留意后续来电",
                        "remote_ok": bool(remote.get("ok")),
                        "remote_required": True,
                        "forced_remote": force_remote,
                        **state,
                    },
                    200 if remote.get("ok") else 502,
                )
            if path == "/api/match/voice/finish":
                with u.lock:
                    state = _set_voice_match_state(u, "idle")
                return self.ok({"ok": True, "message": "语音通话状态已结束", **state})
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
                if payload.get("ok"):
                    match_peers = getattr(u, "match_message_peers", None)
                    if match_peers is None:
                        match_peers = set()
                        setattr(u, "match_message_peers", match_peers)
                    matched_peers = {
                        peer
                        for peer in _message_peer_ids(payload)
                        if peer != str(app.session.uid or "")
                    }
                    match_peers.update(matched_peers)
                    payload["message_peers"] = sorted(matched_peers)
                    recorded_peers = _remember_web_match_history(u, path, payload)
                    if recorded_peers:
                        payload["history_saved"] = True
                        recorder = getattr(self, "_request_match_history_recorder", None)
                        if callable(recorder):
                            try:
                                recorder(path, payload)
                            except Exception:
                                payload["history_saved"] = False
                                payload["history_warning"] = (
                                    "匹配成功，但历史记录暂时没有保存，请保留当前匹配结果"
                                )
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
                frontorback = str(data.get("frontorback") or "1")
                coordinator = getattr(
                    self,
                    "_request_presence_coordinator",
                    None,
                )
                if coordinator is not None:
                    budget_config = dict(
                        getattr(self, "_request_budget_config", None) or {}
                    )
                    timeout = float(
                        budget_config.get("presence_timeout_seconds") or 0
                    )

                    def update_frontback() -> Any:
                        deadline = RequestDeadline(timeout) if timeout > 0 else None
                        return _dependency_call(
                            app.misc.front_or_back,
                            frontorback,
                            provider="beibeiwu",
                            domain="presence",
                            breakers=getattr(
                                self,
                                "_request_dependency_breakers",
                                None,
                            ),
                            timeout=timeout or None,
                            deadline=deadline,
                            request_id=str(
                                getattr(self, "_request_id", "") or ""
                            ),
                        )

                    accepted = coordinator.submit(
                        str(app.session.uid or sid or ""),
                        "frontback",
                        update_frontback,
                    )
                    if not accepted:
                        return self.ok(
                            {
                                "ok": False,
                                "code": "PRESENCE_QUEUE_UNAVAILABLE",
                                "error": "在线状态更新暂时繁忙",
                                "retryable": True,
                                "retry_after": 2,
                            },
                            503,
                            extra_headers={"Retry-After": "2"},
                        )
                    return self.ok(
                        {
                            "ok": True,
                            "accepted": True,
                            "queued": True,
                            "operation": "frontback",
                            "active": frontorback == "1",
                        },
                        202,
                    )
                return self.ok(R(app.misc.front_or_back(frontorback)))

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
        # Query-versioned first-party assets and version-pinned vendor SDK files
        # are immutable.  Keep unversioned entry points revalidated so a direct
        # /static/app.js request cannot retain stale application code.
        has_version = bool(parse_qs(urlparse(self.path).query).get("v"))
        if has_version or "vendor" in rel.parts:
            cache = "public, max-age=31536000, immutable"
        elif path.suffix in {".html", ".js"}:
            cache = "no-cache"
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


def _message_peer_ids(payload: Any) -> Set[str]:
    if not isinstance(payload, Mapping):
        return set()
    rows = payload.get("items") or payload.get("list") or []
    if not isinstance(rows, list):
        return set()
    peers: Set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value: Any = ""
        for key in (
            "peer_id",
            "conversation_user",
            "user_id",
            "uid",
            "id",
            "yourid",
            "target_uid",
            "userId",
            "targetId",
        ):
            value = row.get(key)
            if value:
                break
        if not value:
            for key in ("user", "target", "profile"):
                nested = row.get(key)
                if not isinstance(nested, Mapping):
                    continue
                nested_peers = _message_peer_ids({"items": [nested]})
                if nested_peers:
                    peers.update(nested_peers)
                    break
            continue
        peer = str(value or "").strip()
        if (
            peer
            and peer.lower() not in {"0", "none", "null"}
            and len(peer) <= 128
            and not any(ord(char) < 33 for char in peer)
        ):
            peers.add(peer)
    return peers


def _remember_web_match_history(user: Any, path: str, payload: Mapping[str, Any]) -> List[str]:
    """Keep a bounded session history for the documented memory-only BFF."""

    modes = {
        "/api/match/online": "online",
        "/api/match/local": "local",
        "/api/match/voice/start": "voice",
    }
    mode = modes.get(path)
    if mode is None:
        return []
    current_uid = str(getattr(getattr(user.app, "session", None), "uid", "") or "").strip()
    matched_at = int(time.time() * 1000)
    profiles = N.normalize_users(payload.get("items") or payload.get("list") or [])
    entries: List[Dict[str, Any]] = []
    peers: List[str] = []
    for index, profile in enumerate(profiles):
        peer = str(profile.get("id") or "").strip()
        if not peer or peer == current_uid:
            continue
        peers.append(peer)
        entries.append(
            {
                **profile,
                "id": peer,
                "uid": peer,
                "history_id": f"memory-{time.time_ns()}-{index}",
                "match_mode": mode,
                "matched_at": matched_at,
                "source_path": path,
                "filters": dict(payload.get("filters") or {}),
            }
        )
    if not entries:
        return []

    def update_history() -> None:
        history = getattr(user, "match_history", None)
        if not isinstance(history, list):
            history = []
            setattr(user, "match_history", history)
        history[0:0] = entries
        del history[500:]

    lock = getattr(user, "lock", None)
    if lock is None:
        update_history()
    else:
        with lock:
            update_history()
    return peers


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


def _web_user_capabilities(
    user: Any,
    *,
    match_pool_online_list_enabled: Optional[bool] = None,
    nearby_custom_city_enabled: Optional[bool] = None,
    local_password_change_enabled: bool = False,
) -> Dict[str, bool]:
    enabled = (
        bool(getattr(user, "match_pool_online_list_enabled", False))
        if match_pool_online_list_enabled is None
        else bool(match_pool_online_list_enabled)
    )
    return {
        "match_pool_online_list": True,
        "voice_match": True,
        "proactive_private_message": enabled,
        # A TIM UserSig is account-wide and cannot be scoped to one peer. Only
        # accounts authorized for arbitrary proactive private messages receive
        # it; all other accounts use the per-peer checked BFF transport.
        "direct_im_credentials": enabled,
        "nearby_custom_city": (
            bool(getattr(user, "nearby_custom_city_enabled", False))
            if nearby_custom_city_enabled is None
            else bool(nearby_custom_city_enabled)
        ),
        "local_password_change": bool(local_password_change_enabled),
    }



def _features() -> List[Dict[str, str]]:
    return [dict(item) for item in FEATURES if LAB_ENABLED or item.get("id") != "lab"]


FEATURES: List[Dict[str, str]] = [
    {"id": "nearby", "name": "身边", "desc": "在线用户、资料与好友申请"},
    {"id": "msg", "name": "消息", "desc": "历史会话、未读数与受控实时聊天"},
    {"id": "match", "name": "匹配", "desc": "在线同城、语音匹配、漂流瓶与约会"},
    {"id": "moments", "name": "动态", "desc": "多分类动态流、点赞与评论"},
    {"id": "me", "name": "我的", "desc": "关系统计、资料、实名与设置"},
    {"id": "social", "name": "关系中心", "desc": "通讯录、申请、关注、粉丝、访客与黑名单"},
    {"id": "wallet", "name": "资产与权益", "desc": "余额、提现、礼物背包与服务端会员权益"},
    {"id": "tasks", "name": "任务与奖励", "desc": "任务列表与奖励领取"},
    {"id": "lab", "name": "协议台", "desc": "非商业 do= 调用"},
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
