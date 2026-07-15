#!/usr/bin/env python3
"""Full App BFF — APK feature surface over HTTP for browser clients.

  python -m bbw_web --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web.store import SessionStore  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "bbw_sid"
STORE: Optional[SessionStore] = None


def _json_bytes(obj: Any, status: int = 200) -> Tuple[int, bytes, str]:
    return (
        status,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
        "application/json; charset=utf-8",
    )


def R(r: Any) -> Dict[str, Any]:
    return {
        "ok": bool(getattr(r, "ok", False)),
        "status": getattr(r, "status", 0),
        "code": str(getattr(r, "code", "") or ""),
        "message": str(getattr(r, "message", "") or ""),
        "extra": str(getattr(r, "extra", "") or ""),
        "kind": str(getattr(r, "kind", "") or ""),
        "data": getattr(r, "data", None),
        "raw_preview": (getattr(r, "raw", None) or "")[:1200],
    }


def L(data: Any) -> Any:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return data
    for k in (
        "json_obj",
        "json",
        "list",
        "data",
        "info",
        "users",
        "items",
        "result",
        "rows",
        "records",
    ):
        v = data.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v.strip()[:1] in "[{":
            try:
                p = json.loads(v)
                if isinstance(p, list):
                    return p
                if isinstance(p, dict):
                    inner = L(p)
                    if inner != p:
                        return inner
            except Exception:
                pass
        if isinstance(v, dict):
            inner = L(v)
            if isinstance(inner, list):
                return inner
    return data


def RL(r: Any) -> Dict[str, Any]:
    d = R(r)
    d["list"] = L(r.data)
    return d


class Handler(BaseHTTPRequestHandler):
    server_version = "bbw-app/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        o = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", o)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-BBW-SID")

    def _send(
        self,
        status: int,
        body: bytes,
        ct: str,
        *,
        set_cookie: Optional[str] = None,
        clear_cookie: bool = False,
    ) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={set_cookie}; Path=/; HttpOnly; SameSite=Lax",
            )
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
        self.end_headers()
        self.wfile.write(body)

    def ok(self, obj: Any, status: int = 200, **kw: Any) -> None:
        st, body, ct = _json_bytes(obj, status)
        self._send(st, body, ct, **kw)

    def body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def sid(self, qs: Optional[Dict[str, Any]] = None) -> Optional[str]:
        h = self.headers.get("X-BBW-SID") or self.headers.get("x-bbw-sid")
        if h:
            return h.strip()
        if qs and qs.get("sid"):
            return qs["sid"][0]
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
            return STORE.require(sid)
        except KeyError:
            self.ok({"ok": False, "error": "请先登录"}, 401)
            return None

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        assert STORE is not None
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        q = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731

        if path in ("/", "/index.html"):
            return self.static("index.html")
        if path.startswith("/static/"):
            return self.static(Path(path).name)

        if path == "/api/health":
            return self.ok(
                {
                    "ok": True,
                    "service": "bbw-app",
                    "version": 2,
                    **STORE.stats(),
                    "features": FEATURES,
                }
            )

        if path == "/api/features":
            return self.ok({"ok": True, "features": FEATURES})

        if path == "/api/sessions":
            return self.ok({"ok": True, "sessions": STORE.list_public()})

        sid = self.sid(qs)

        if path == "/api/me":
            u = STORE.get(sid)
            if not u:
                return self.ok({"ok": False, "logged_in": False}, 401)
            return self.ok({"ok": True, **u.public(), "features": FEATURES})

        u = self.user(sid)
        if not u:
            return
        app = u.app

        # ---- dashboard ----
        if path in ("/api/app/home", "/api/home"):
            gifts = app.content.gift_list()
            rec = app.content.recommend()
            slide = app.content.slide()
            ver = app.content.version()
            return self.ok(
                {
                    "ok": True,
                    "user": app.whoami(),
                    "gifts": RL(gifts),
                    "recommend": RL(rec),
                    "slide": RL(slide),
                    "version": R(ver),
                    "heartbeat": u.heartbeat.status() if u.heartbeat else {"running": False},
                }
            )

        if path == "/api/app/bootstrap":
            batch = {
                k: {"ok": v.ok, "code": v.code, "message": v.message}
                for k, v in app.bootstrap().items()
            }
            try:
                tim = u.native.im.tim_login_payload(prefer=q("prefer", "local"))
            except Exception as e:
                tim = {"error": str(e)}
            u.persist()
            return self.ok({"ok": True, "user": app.whoami(), "batch": batch, "tim": tim})

        # ---- content / square ----
        if path == "/api/gifts":
            return self.ok(RL(app.content.gift_list()))
        if path == "/api/recommend":
            return self.ok(RL(app.content.recommend()))
        if path == "/api/slide":
            return self.ok(RL(app.content.slide()))
        if path == "/api/topics":
            return self.ok(RL(app.content.topic(q("q", ""))))
        if path == "/api/ads":
            return self.ok(R(app.content.is_show_ad()))
        if path == "/api/censor":
            return self.ok(R(app.content.chat_censorship()))
        if path == "/api/referral":
            return self.ok(R(app.content.referral()))
        if path == "/api/version":
            return self.ok(R(app.content.version()))

        # ---- profile ----
        if path == "/api/profile/me":
            r = app.profile.get_me()
            u.persist()
            return self.ok({**R(r), "user": app.whoami()})
        if path == "/api/profile/user":
            return self.ok(RL(app.profile.get_user(q("uid") or app.session.uid)))
        if path == "/api/profile/reset-num":
            return self.ok(R(app.profile.reset_num(q("type", "昵称"))))
        if path == "/api/profile/etiquette":
            return self.ok(R(app.profile.etiquette()))

        # ---- social ----
        if path == "/api/social/follows":
            return self.ok(RL(app.social.follow_users(q("uid") or None, page=q("page", "1"))))
        if path == "/api/social/fans":
            return self.ok(RL(app.social.fans_users(q("uid") or None, page=q("page", "1"))))
        if path == "/api/social/follow-list":
            return self.ok(RL(app.social.follow_list()))
        if path == "/api/social/friend-apply":
            return self.ok(RL(app.social.friend_apply_list(q("page", "1"))))
        if path == "/api/social/blacklist":
            return self.ok(RL(app.social.my_blacklist()))
        if path == "/api/social/blacklist-me":
            return self.ok(RL(app.social.blacklist_me()))

        # ---- match ----
        if path == "/api/match/status":
            cards = app.call("getMyCard")
            nums = app.call("getMatchNum")
            return self.ok(
                {
                    "ok": True,
                    "user": app.whoami(),
                    "cards": R(cards),
                    "cards_data": cards.data,
                    "nums": R(nums),
                    "nums_data": nums.data,
                }
            )
        if path == "/api/match/online-users":
            return self.ok(RL(app.match.online_users()))
        if path == "/api/match/bottles":
            return self.ok(RL(app.match.my_bottles()))

        # ---- tasks ----
        if path == "/api/tasks":
            create = app.call("createHotActivityList")
            have = app.call("haveHotActivityList")
            return self.ok(
                {
                    "ok": True,
                    "create": RL(create),
                    "have": RL(have),
                    "create_list": L(create.data),
                    "have_list": L(have.data),
                }
            )

        # ---- room ----
        if path == "/api/room/top":
            return self.ok(RL(app.room.top()))
        if path == "/api/room/auth":
            return self.ok(R(app.room.auth()))
        if path == "/api/room/tips":
            return self.ok(R(app.room.tips(q("type", "1"))))
        if path == "/api/room/songs":
            return self.ok(RL(app.room.song_list(q("room_id", ""))))
        if path == "/api/room/user":
            return self.ok(R(app.room.get_user_room_info(q("uid") or None)))

        # ---- wallet ----
        if path == "/api/wallet":
            me = app.profile.get_me()
            u.persist()
            myg = app.economy.my_gifts()
            glist = app.economy.gift_list()
            return self.ok(
                {
                    "ok": True,
                    "user": app.whoami(),
                    "me": R(me),
                    "my_gifts": RL(myg),
                    "gift_shop": RL(glist),
                    "pay": u.native.pay.capabilities(),
                }
            )

        # ---- im ----
        if path == "/api/im/tim":
            try:
                return self.ok(
                    {"ok": True, **u.native.im.tim_login_payload(prefer=q("prefer", "local"))}
                )
            except Exception as e:
                return self.ok({"ok": False, "error": str(e)}, 400)
        if path == "/api/im/rong":
            c = u.native.im.rong_register()
            return self.ok({"ok": c.ok, **c.to_dict()})
        if path == "/api/im/bootstrap":
            return self.ok(u.native.im.bootstrap(prefer_tim=q("prefer", "local")))
        if path == "/api/im/stickers":
            return self.ok(RL(app.im.stickers()))

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
            cat = q("cat") or None
            acts = app.list_actions(cat)
            return self.ok({"ok": True, "count": len(acts), "actions": acts[:500]})

        self.ok({"ok": False, "error": "not found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        assert STORE is not None
        path = urlparse(self.path).path
        data = self.body()
        qs = parse_qs(urlparse(self.path).query)
        sid = self.sid(qs)

        # ---- auth ----
        if path == "/api/auth/login":
            phone = str(data.get("phone") or "").strip()
            password = str(data.get("password") or "")
            mode = str(data.get("mode") or "password")
            if not phone:
                return self.ok({"ok": False, "error": "请输入手机号"}, 400)
            try:
                if mode == "onekey":
                    user = STORE.login_onekey(sid, phone, label=str(data.get("label") or ""))
                else:
                    if not password:
                        return self.ok({"ok": False, "error": "请输入密码"}, 400)
                    user = STORE.login_password(
                        sid, phone, password, label=str(data.get("label") or "")
                    )
            except Exception as e:
                return self.ok({"ok": False, "error": str(e)}, 400)
            try:
                user.app.bootstrap()
            except Exception:
                pass
            user.persist()
            return self.ok({"ok": True, **user.public()}, set_cookie=user.web_sid)

        if path == "/api/auth/logout":
            if sid:
                STORE.drop(sid)
            return self.ok({"ok": True}, clear_cookie=True)

        if path == "/api/auth/sms-send":
            from bbw_protocol import BeibeiwuApp

            phone = str(data.get("phone") or "").strip()
            if not phone:
                return self.ok({"ok": False, "error": "phone required"}, 400)
            return self.ok(R(BeibeiwuApp().auth.send_sms(phone)))

        if path == "/api/auth/sms-login":
            from bbw_protocol.device import build_device_profile

            phone = str(data.get("phone") or "").strip()
            code = str(data.get("code") or "").strip()
            if not phone or not code:
                return self.ok({"ok": False, "error": "需要手机号和验证码"}, 400)
            try:
                user = STORE.get(sid) or STORE.create(label=phone)
                user.app.session.apply_device(build_device_profile(seed=phone))
                r = user.app.auth.sms_login(phone, code)
                if not user.app.session.logged_in:
                    return self.ok(
                        {**R(r), "ok": False, "error": r.message or "登录失败"}, 400
                    )
                user.persist()
                if STORE.auto_heartbeat:
                    user.start_heartbeat(STORE.heartbeat_interval)
                STORE.put(user)
                return self.ok({"ok": True, **user.public()}, set_cookie=user.web_sid)
            except Exception as e:
                return self.ok({"ok": False, "error": str(e)}, 400)

        u = self.user(sid)
        if not u:
            return
        app = u.app

        try:
            # heartbeat
            if path == "/api/heartbeat/start":
                return self.ok(
                    {
                        "ok": True,
                        **u.start_heartbeat(
                            float(data.get("interval_sec") or STORE.heartbeat_interval)
                        ),
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
                action = str(data.get("action") or "")
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                if not action:
                    return self.ok({"ok": False, "error": "action required"}, 400)
                return self.ok(R(app.call(action, **params)))

            if path == "/api/call-redis":
                action = str(data.get("action") or "")
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                return self.ok(R(app.call_redis(action, **params)))

            # social writes
            if path == "/api/social/follow":
                return self.ok(R(app.social.follow(str(data.get("uid") or ""))))
            if path == "/api/social/unfollow":
                return self.ok(R(app.social.unfollow(str(data.get("uid") or ""))))
            if path == "/api/social/agree-friend":
                return self.ok(R(app.social.agree_friend(str(data.get("id") or ""))))
            if path == "/api/social/delete-friend":
                return self.ok(R(app.social.delete_friend(**data)))
            if path == "/api/social/blacklist-add":
                return self.ok(R(app.social.add_blacklist(**data)))
            if path == "/api/social/blacklist-del":
                return self.ok(R(app.social.delete_blacklist(**data)))
            if path == "/api/social/like-post":
                return self.ok(R(app.social.luntan_like(str(data.get("postid") or ""))))
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
                return self.ok({**R(r), "user": app.whoami()})
            if path == "/api/profile/reset":
                r = app.profile.reset_personal(
                    str(data.get("value") or ""),
                    type_=str(data.get("type") or "昵称设置"),
                )
                u.persist()
                return self.ok(R(r))
            if path == "/api/profile/privacy":
                return self.ok(R(app.profile.set_privacy(**_params(data))))

            # match
            if path == "/api/match/online":
                return self.ok(RL(app.match.online_one(**_params(data))))
            if path == "/api/match/local":
                return self.ok(RL(app.match.local_one(**_params(data))))
            if path == "/api/match/remove":
                return self.ok(R(app.match.remove(str(data.get("type") or "1"))))
            if path == "/api/match/bottle-throw":
                return self.ok(R(app.match.throw_bottle(**_params(data))))
            if path == "/api/match/bottle-pick":
                return self.ok(RL(app.match.pick_bottle(**_params(data))))
            if path == "/api/match/bottle-delete":
                return self.ok(R(app.match.delete_bottle(**_params(data))))
            if path == "/api/match/dating-publish":
                return self.ok(R(app.match.publish_dating(**_params(data))))
            if path == "/api/match/dating-apply":
                return self.ok(R(app.match.apply_dating(**_params(data))))
            if path == "/api/match/dating-cancel":
                return self.ok(R(app.match.cancel_dating(**_params(data))))

            # tasks
            if path == "/api/tasks/receive":
                tid = str(data.get("id") or data.get("task_id") or "")
                return self.ok(R(app.call("receiveHotActivityList", id=tid)))

            # room
            if path == "/api/room/create":
                return self.ok(R(app.room.create(str(data.get("type") or "处CP"))))
            if path == "/api/room/set":
                return self.ok(R(app.room.set(**_params(data))))
            if path == "/api/room/finish":
                return self.ok(R(app.room.finish(**_params(data))))
            if path == "/api/room/song-add":
                return self.ok(
                    R(
                        app.room.add_song(
                            str(data.get("room_id") or ""),
                            str(data.get("music_id") or ""),
                        )
                    )
                )
            if path == "/api/room/ktv-search":
                return self.ok(
                    RL(
                        app.room.ktv_search(
                            str(data.get("key_word") or data.get("q") or ""),
                            page=str(data.get("page") or "1"),
                        )
                    )
                )
            if path == "/api/room/rtc-token":
                return self.ok(
                    R(app.room.rtc_token(str(data.get("channel") or "")))
                )

            # wallet / economy
            if path == "/api/pay/coin":
                ch = (data.get("channel") or "wechat").lower()
                cid = str(data.get("coin_id") or "1")
                res = (
                    u.native.pay.prepare_coin_alipay(cid)
                    if ch == "alipay"
                    else u.native.pay.prepare_coin_wechat(cid)
                )
                return self.ok(res.to_dict())
            if path == "/api/pay/vip":
                ch = (data.get("channel") or "wechat").lower()
                lv = str(data.get("level") or "vip")
                res = (
                    u.native.pay.prepare_vip_alipay(lv)
                    if ch == "alipay"
                    else u.native.pay.prepare_vip_wechat(lv)
                )
                return self.ok(res.to_dict())
            if path == "/api/pay/card":
                return self.ok(
                    u.native.pay.buy_match_card(str(data.get("card_id") or "1")).to_dict()
                )
            if path == "/api/wallet/svip-try":
                return self.ok(R(app.economy.svip_try()))
            if path == "/api/wallet/exchange-vip":
                return self.ok(
                    R(app.economy.money_exchange_vip(int(data.get("vip_id") or 5)))
                )
            if path == "/api/wallet/send-gift":
                return self.ok(R(app.economy.send_gift1(**_params(data))))
            if path == "/api/wallet/withdraw":
                return self.ok(
                    R(
                        app.economy.withdraw(
                            str(data.get("alipay") or data.get("alilogonid") or ""),
                            str(data.get("name") or data.get("aliname") or ""),
                            str(data.get("amount") or data.get("transamount") or ""),
                        )
                    )
                )

            # content
            if path == "/api/topics/create":
                return self.ok(R(app.content.create_topic(str(data.get("topic") or ""))))
            if path == "/api/referral/set":
                return self.ok(R(app.content.set_referral(str(data.get("referral") or ""))))

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
                return self.ok(
                    u.native.face.manual(
                        str(data.get("cert_name") or ""),
                        str(data.get("cert_no") or ""),
                        str(data.get("result") or "pending"),
                    )
                )

            if path == "/api/online":
                return self.ok(R(app.misc.update_online(first=bool(data.get("first")))))
            if path == "/api/frontback":
                return self.ok(
                    R(app.misc.front_or_back(str(data.get("frontorback") or "1")))
                )

        except Exception as e:
            return self.ok({"ok": False, "error": str(e)}, 400)

        self.ok({"ok": False, "error": "not found", "path": path}, 404)

    def static(self, name: str) -> None:
        path = STATIC_DIR / Path(name).name
        if not path.is_file():
            path = STATIC_DIR / "index.html"
        if not path.is_file():
            self._send(404, b"missing", "text/plain")
            return
        data = path.read_bytes()
        ct = "text/html; charset=utf-8"
        if path.suffix == ".js":
            ct = "application/javascript; charset=utf-8"
        elif path.suffix == ".css":
            ct = "text/css; charset=utf-8"
        self._send(200, data, ct)


def _params(data: Dict[str, Any]) -> Dict[str, Any]:
    skip = {"action", "mode"}
    return {k: v for k, v in data.items() if k not in skip and v is not None}


FEATURES: List[Dict[str, str]] = [
    {"id": "home", "name": "首页", "desc": "推荐/幻灯/礼物/在线"},
    {"id": "square", "name": "广场", "desc": "话题/点赞/发现"},
    {"id": "match", "name": "匹配", "desc": "在线同城/漂流瓶/约会/买卡"},
    {"id": "social", "name": "社交", "desc": "关注粉丝好友黑名单举报"},
    {"id": "room", "name": "房间", "desc": "语音房榜/建房/点歌"},
    {"id": "msg", "name": "消息", "desc": "TIM 凭证与收发"},
    {"id": "wallet", "name": "钱包", "desc": "礼物VIP充值提现"},
    {"id": "tasks", "name": "任务", "desc": "热门任务领取"},
    {"id": "me", "name": "我的", "desc": "资料改名实名设置"},
    {"id": "lab", "name": "协议台", "desc": "任意 do= 调用"},
]


def main(argv=None) -> int:
    global STORE
    ap = argparse.ArgumentParser(description="bbw full web app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-heartbeat", action="store_true")
    ap.add_argument("--ttl-days", type=float, default=7.0)
    args = ap.parse_args(argv)
    STORE = SessionStore(
        ttl_sec=args.ttl_days * 86400,
        auto_heartbeat=not args.no_heartbeat,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"小贝 Web App  http://{args.host}:{args.port}/", flush=True)
    print("  功能: 首页/广场/匹配/社交/房间/消息/钱包/任务/我的", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        for s in list(STORE.list_public()):
            STORE.drop(s["web_sid"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
