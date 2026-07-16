#!/usr/bin/env python3
"""Product Web BFF — APK business surface with an opt-in research lab.

  python -m bbw_web --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "bbw_sid"
STORE: Optional[SessionStore] = None
LAB_ENABLED = False
CORS_ALLOW_ORIGINS: Set[str] = set()
MAX_JSON_BODY_BYTES = 256 * 1024
COOKIE_SECURE = False
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


ENTITY_NORMALIZERS = {
    "slide": N.normalize_slides,
    "topic": N.normalize_topics,
    "room": N.normalize_rooms,
    "song": N.normalize_songs,
    "bottle": N.normalize_bottles,
    "sticker": N.normalize_stickers,
    "conversation": N.normalize_conversations,
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
                    "style-src 'self'; img-src 'self' data: https:; "
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
                    "lab_enabled": LAB_ENABLED,
                }
            )

        if path == "/api/features":
            return self.ok(
                {
                    "ok": True,
                    "features": _features(),
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
            return self.ok(
                {
                    "ok": True,
                    **pub,
                    "user": user_dto,
                    "features": _features(),
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
                tim = {"ok": False, "error": _safe_error(e, "TIM 凭证获取失败")}
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
            return self.ok(RL(app.social.follow_users(q("uid") or None, page=q("page", "1"))))
        if path == "/api/social/fans":
            return self.ok(RL(app.social.fans_users(q("uid") or None, page=q("page", "1"))))
        if path == "/api/social/follow-list":
            return self.ok(RL(app.social.follow_list(q("uid") or q("id") or None)))
        if path == "/api/social/friend-apply":
            return self.ok(RL(app.social.friend_apply_list(q("page", "1"))))
        if path == "/api/social/friends":
            return self.ok(RL(app.social.friends()))
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
            return self.ok(
                {
                    "ok": True,
                    "user": who,
                    "status": status,
                    "display": status["display"],
                    # keep raw only for lab debugging if needed
                    "cards_ok": cards.ok,
                    "nums_ok": nums.ok,
                }
            )
        if path == "/api/match/online-users":
            return self.ok(
                RL(
                    app.match.online_users(
                        uid=app.session.uid,
                        gender=q("gender"),
                        property=q("property"),
                        pageIndex=q("page", q("pageIndex", "1")),
                    )
                )
            )
        if path == "/api/match/bottles":
            return self.ok(RE(app.match.my_bottles(uid=app.session.uid), "bottle"))

        # ---- tasks ----
        if path == "/api/tasks":
            create = app.call("createHotActivityList", uid=app.session.uid)
            have = app.call("haveHotActivityList", uid=app.session.uid)
            items = N.normalize_tasks(create.data) or N.normalize_tasks(have.data)
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
            return self.ok(RE(app.room.top(), "room"))
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
            # Fall back to BFF-local mint (APK SECRETKEY, never sent to browser).
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
                                "title": "TIM 凭证不完整",
                                "detail": "缺少 userID 或 userSig，请重新登录后再试",
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
                    {"ok": False, "error": _safe_error(e, "TIM 凭证获取失败")},
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
                RE(app.im.history_conversations(q("page", "1")), "conversation")
            )

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
        try:
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
            try:
                user.app.bootstrap()
            except Exception:
                pass
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
                return self.ok(
                    R(app.social.agree_friend(str(data.get("id") or data.get("apply_id") or "")))
                )
            if path == "/api/social/delete-friend":
                return self.ok(R(app.social.delete_friend(**_params(data))))
            if path == "/api/social/visit":
                target_uid = str(data.get("uid") or data.get("yourid") or "").strip()
                if not target_uid:
                    return self.ok({"ok": False, "error": "缺少对方 UID"}, 400)
                return self.ok(R(app.social.record_profile_view(target_uid)))

            # TIM REST fallback (when browser TIM.login hangs)
            if path == "/api/im/rest/send":
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
                if hist is not None:
                    out["history_mirror"] = hist
                if r.ok:
                    out["message"] = "已通过腾讯 IM REST 发送（非浏览器实时长连接）"
                return self.ok(out, 200 if r.ok else 400)

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
            if path == "/api/match/online":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(N.normalize_match_result(app.match.online_one(**params)))
            if path == "/api/match/local":
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(N.normalize_match_result(app.match.local_one(**params)))
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
                params = _params(data)
                params["uid"] = app.session.uid
                return self.ok(R(app.match.throw_bottle(**params)))
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
                    return self.ok({"ok": False, "error": "缺少任务 ID"}, 400)
                r = app.call("receiveHotActivityList", id=tid)
                return self.ok(R(r))

            # room
            if path == "/api/room/create":
                return self.ok(
                    R(
                        app.room.create(
                            str(data.get("type") or data.get("audioroomtype") or "处CP"),
                            my_id=app.session.uid,
                        ),
                        include_value=True,
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
                    return self.ok({"ok": False, "error": "请选择有效的会员商品 ID"}, 400)
                extra = _params(data)
                for key in ("channel", "level", "vipid", "vip_id"):
                    extra.pop(key, None)
                res = (
                    u.native.pay.prepare_vip_alipay(lv, vipid=vipid, **extra)
                    if ch == "alipay"
                    else u.native.pay.prepare_vip_wechat(lv, vipid=vipid, **extra)
                )
                d = res.to_dict()
                d["product_notice"] = "仅创建 VIP 订单参数，不等于开通成功。"
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
                    return self.ok({"ok": False, "error": "会员商品 ID 无效"}, 400)
                if vip_id <= 0:
                    return self.ok({"ok": False, "error": "会员商品 ID 无效"}, 400)
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
    {"id": "moments", "name": "动态", "desc": "推荐、轮播与话题"},
    {"id": "me", "name": "我的", "desc": "关系统计、资料、实名与设置"},
    {"id": "friends", "name": "好友列表", "desc": "好友通讯录与新朋友"},
    {"id": "visitors", "name": "访客足迹", "desc": "谁看过我与我看过谁"},
    {"id": "social", "name": "关注与粉丝", "desc": "关注、粉丝、申请与黑名单"},
    {"id": "room", "name": "语音房间", "desc": "房间榜、建房与点歌"},
    {"id": "wallet", "name": "钱包会员", "desc": "礼物、VIP、充值与提现"},
    {"id": "tasks", "name": "成长任务", "desc": "任务列表与奖励领取"},
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
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
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
