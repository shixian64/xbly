#!/usr/bin/env python3
"""Multi-user App BFF — use banghua like the APK from a browser.

  python -m bbw_web --port 8765
  open http://127.0.0.1:8765/

Auth: Cookie bbw_sid | Header X-BBW-SID
Each browser session → isolated BeibeiwuApp (+ optional heartbeat).
"""

from __future__ import annotations

import argparse
import json
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web.store import SessionStore  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "bbw_sid"
STORE: Optional[SessionStore] = None


def _json_bytes(obj: Any, status: int = 200) -> Tuple[int, bytes, str]:
    body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def _result_dict(r: Any) -> Dict[str, Any]:
    return {
        "ok": bool(getattr(r, "ok", False)),
        "status": getattr(r, "status", 0),
        "code": str(getattr(r, "code", "") or ""),
        "message": str(getattr(r, "message", "") or ""),
        "extra": str(getattr(r, "extra", "") or ""),
        "kind": str(getattr(r, "kind", "") or ""),
        "data": getattr(r, "data", None),
        "raw_preview": (getattr(r, "raw", None) or "")[:800],
    }


def _pick_list(data: Any) -> Any:
    """Best-effort extract list payloads from nested banghua JSON."""
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
        "user",
        "items",
        "result",
        "rows",
    ):
        v = data.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v.strip().startswith(("[", "{")):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    inner = _pick_list(parsed)
                    if inner:
                        return inner
            except Exception:
                pass
        if isinstance(v, dict):
            inner = _pick_list(v)
            if inner:
                return inner
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "bbw-app-bff/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-BBW-SID")

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        set_cookie: Optional[str] = None,
        clear_cookie: bool = False,
    ) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
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

    def _read_json(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _sid(self, qs: Optional[Dict[str, Any]] = None) -> Optional[str]:
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

    def _ok(self, obj: Any, status: int = 200, **kw: Any) -> None:
        st, body, ct = _json_bytes(obj, status)
        self._send(st, body, ct, **kw)

    def _need_user(self, sid: Optional[str]):
        assert STORE is not None
        try:
            return STORE.require(sid)
        except KeyError:
            self._ok({"ok": False, "error": "login required"}, 401)
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

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(Path(path).name)
            return

        if path == "/api/health":
            self._ok({"ok": True, "service": "bbw-app", "module": "bbw_web", **STORE.stats()})
            return

        if path == "/api/sessions":
            self._ok({"ok": True, "sessions": STORE.list_public()})
            return

        sid = self._sid(qs)

        if path == "/api/me":
            u = STORE.get(sid)
            if not u:
                self._ok({"ok": False, "logged_in": False, "error": "no web session"}, 401)
                return
            self._ok({"ok": True, **u.public(), "native": u.native.status()})
            return

        user = self._need_user(sid)
        if not user:
            return
        app = user.app

        # ---- App home / cold start ----
        if path == "/api/app/home":
            out: Dict[str, Any] = {"ok": True, "user": app.whoami()}
            try:
                gifts = app.content.gift_list()
                rec = app.content.recommend()
                slide = app.content.slide()
                out["gifts"] = _result_dict(gifts)
                out["recommend"] = _result_dict(rec)
                out["recommend_list"] = _pick_list(rec.data)
                out["slide"] = _result_dict(slide)
                out["slide_list"] = _pick_list(slide.data)
                out["heartbeat"] = (
                    user.heartbeat.status() if user.heartbeat else {"running": False}
                )
            except Exception as e:
                out["error"] = str(e)
            self._ok(out)
            return

        if path == "/api/app/bootstrap":
            prefer = (qs.get("prefer") or ["local"])[0]
            batch = app.bootstrap()
            summary = {
                k: {"ok": v.ok, "code": v.code, "message": v.message}
                for k, v in batch.items()
            }
            try:
                tim = user.native.im.tim_login_payload(prefer=prefer)
            except Exception as e:
                tim = {"error": str(e)}
            user.persist()
            self._ok(
                {
                    "ok": True,
                    "user": app.whoami(),
                    "batch": summary,
                    "tim": tim,
                    "heartbeat": user.heartbeat.status() if user.heartbeat else {},
                }
            )
            return

        # ---- content ----
        if path == "/api/gifts":
            r = app.content.gift_list()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/recommend":
            r = app.content.recommend()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/slide":
            r = app.content.slide()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/topics":
            r = app.content.topic(str((qs.get("q") or [""])[0]))
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return

        # ---- profile ----
        if path == "/api/profile/me":
            r = app.profile.get_me()
            user.persist()
            self._ok({**_result_dict(r), "user": app.whoami()})
            return
        if path == "/api/profile/user":
            uid = (qs.get("uid") or [app.session.uid])[0]
            r = app.profile.get_user(uid)
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/profile/reset-num":
            r = app.profile.reset_num()
            self._ok(_result_dict(r))
            return

        # ---- social ----
        if path == "/api/social/follows":
            r = app.social.follow_users((qs.get("uid") or [None])[0])
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/social/fans":
            r = app.social.fans_users((qs.get("uid") or [None])[0])
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/social/follow-list":
            r = app.social.follow_list()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/social/friend-apply":
            r = app.social.friend_apply_list()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/social/blacklist":
            r = app.social.my_blacklist()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return

        # ---- match ----
        if path == "/api/match/status":
            cards = app.call("getMyCard")
            nums = app.call("getMatchNum")
            self._ok(
                {
                    "ok": True,
                    "cards": _result_dict(cards),
                    "cards_data": cards.data,
                    "nums": _result_dict(nums),
                    "nums_data": nums.data,
                    "user": app.whoami(),
                }
            )
            return
        if path == "/api/match/online-users":
            r = app.match.online_users()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/match/bottles":
            r = app.match.my_bottles()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return

        # ---- tasks ----
        if path == "/api/tasks":
            create = app.call("createHotActivityList")
            have = app.call("haveHotActivityList")
            self._ok(
                {
                    "ok": True,
                    "create": _result_dict(create),
                    "create_list": _pick_list(create.data),
                    "have": _result_dict(have),
                    "have_list": _pick_list(have.data),
                }
            )
            return

        # ---- room ----
        if path == "/api/room/top":
            r = app.room.top()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return
        if path == "/api/room/auth":
            r = app.room.auth()
            self._ok(_result_dict(r))
            return

        # ---- economy ----
        if path == "/api/wallet":
            me = app.profile.get_me()
            user.persist()
            gifts = app.economy.my_gifts()
            self._ok(
                {
                    "ok": True,
                    "user": app.whoami(),
                    "me": _result_dict(me),
                    "my_gifts": _result_dict(gifts),
                    "my_gifts_list": _pick_list(gifts.data),
                    "pay": user.native.pay.capabilities(),
                }
            )
            return

        # ---- IM ----
        if path == "/api/im/tim":
            prefer = (qs.get("prefer") or ["local"])[0]
            try:
                self._ok({"ok": True, **user.native.im.tim_login_payload(prefer=prefer)})
            except Exception as e:
                self._ok({"ok": False, "error": str(e)}, 400)
            return
        if path == "/api/im/rong":
            cred = user.native.im.rong_register()
            self._ok({"ok": cred.ok, **cred.to_dict()})
            return
        if path == "/api/im/bootstrap":
            prefer = (qs.get("prefer") or ["local"])[0]
            self._ok(user.native.im.bootstrap(prefer_tim=prefer))
            return
        if path == "/api/im/stickers":
            r = app.im.stickers()
            self._ok({**_result_dict(r), "list": _pick_list(r.data)})
            return

        if path == "/api/heartbeat":
            self._ok(user.heartbeat.status() if user.heartbeat else {"running": False})
            return
        if path == "/api/pay/capabilities":
            self._ok(user.native.pay.capabilities())
            return
        if path == "/api/face/status":
            self._ok(user.native.face.status_hint())
            return
        if path == "/api/bootstrap":
            prefer = (qs.get("prefer") or ["local"])[0]
            try:
                data = user.native.web_bootstrap(prefer_tim=prefer)
                self._ok({"ok": True, "web_sid": user.web_sid, **data})
            except Exception as e:
                self._ok({"ok": False, "error": str(e)}, 400)
            return

        self._ok({"ok": False, "error": "not found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        assert STORE is not None
        path = urlparse(self.path).path
        data = self._read_json()
        qs = parse_qs(urlparse(self.path).query)
        sid = self._sid(qs)

        # ---- auth ----
        if path == "/api/auth/login":
            phone = str(data.get("phone") or data.get("userAccount") or "").strip()
            password = str(data.get("password") or data.get("userPassword") or "")
            mode = str(data.get("mode") or "password")
            label = str(data.get("label") or "")
            if not phone:
                self._ok({"ok": False, "error": "请输入手机号"}, 400)
                return
            try:
                if mode == "onekey":
                    user = STORE.login_onekey(sid, phone, label=label)
                else:
                    if not password:
                        self._ok({"ok": False, "error": "请输入密码"}, 400)
                        return
                    user = STORE.login_password(sid, phone, password, label=label)
            except Exception as e:
                self._ok({"ok": False, "error": str(e)}, 400)
                return
            # cold-start batch after login
            try:
                user.app.bootstrap()
            except Exception:
                pass
            user.persist()
            self._ok({"ok": True, **user.public()}, set_cookie=user.web_sid)
            return

        if path == "/api/auth/logout":
            if sid:
                STORE.drop(sid)
            self._ok({"ok": True}, clear_cookie=True)
            return

        if path == "/api/auth/sms-send":
            phone = str(data.get("phone") or "").strip()
            if not phone:
                self._ok({"ok": False, "error": "phone required"}, 400)
                return
            # temporary app without login
            from bbw_protocol import BeibeiwuApp

            tmp = BeibeiwuApp()
            r = tmp.auth.send_sms(phone)
            self._ok(_result_dict(r))
            return

        if path == "/api/auth/sms-login":
            phone = str(data.get("phone") or "").strip()
            code = str(data.get("code") or "").strip()
            if not phone or not code:
                self._ok({"ok": False, "error": "phone and code required"}, 400)
                return
            try:
                user = STORE.get(sid) or STORE.create(label=phone)
                user.app.session.apply_device(
                    __import__(
                        "bbw_protocol.device", fromlist=["build_device_profile"]
                    ).build_device_profile(seed=phone)
                )
                r = user.app.auth.sms_login(phone, code)
                if not user.app.session.logged_in:
                    self._ok({**_result_dict(r), "ok": False, "error": r.message or "登录失败"}, 400)
                    return
                user.persist()
                if STORE.auto_heartbeat:
                    user.start_heartbeat(STORE.heartbeat_interval)
                STORE.put(user)
                self._ok({"ok": True, **user.public()}, set_cookie=user.web_sid)
            except Exception as e:
                self._ok({"ok": False, "error": str(e)}, 400)
            return

        user = self._need_user(sid)
        if not user:
            return
        app = user.app

        try:
            if path == "/api/heartbeat/start":
                self._ok({"ok": True, **user.start_heartbeat(
                    float(data.get("interval_sec") or STORE.heartbeat_interval)
                )})
                return
            if path == "/api/heartbeat/stop":
                user.stop_heartbeat()
                self._ok({"ok": True, "running": False})
                return
            if path == "/api/heartbeat/once":
                if not user.heartbeat:
                    from bbw_protocol.heartbeat import Heartbeat

                    user.heartbeat = Heartbeat(app)
                self._ok(user.heartbeat.once())
                return

            if path == "/api/call":
                action = str(data.get("action") or "")
                params = data.get("params") or {}
                if not action:
                    self._ok({"ok": False, "error": "action required"}, 400)
                    return
                if not isinstance(params, dict):
                    params = {}
                r = app.call(action, **params)
                self._ok(_result_dict(r))
                return

            # social
            if path == "/api/social/follow":
                self._ok(_result_dict(app.social.follow(str(data.get("uid") or ""))))
                return
            if path == "/api/social/unfollow":
                self._ok(_result_dict(app.social.unfollow(str(data.get("uid") or ""))))
                return
            if path == "/api/social/agree-friend":
                self._ok(_result_dict(app.social.agree_friend(str(data.get("id") or ""))))
                return

            # profile
            if path == "/api/profile/nick":
                name = str(data.get("name") or data.get("nickname") or "")
                r = app.profile.reset_nickname(name)
                user.persist()
                self._ok({**_result_dict(r), "user": app.whoami()})
                return
            if path == "/api/profile/reset":
                r = app.profile.reset_personal(
                    str(data.get("value") or ""),
                    type_=str(data.get("type") or "昵称设置"),
                )
                user.persist()
                self._ok(_result_dict(r))
                return

            # match
            if path == "/api/match/online":
                r = app.match.online_one(**{
                    k: v for k, v in data.items() if k not in ()
                })
                self._ok({**_result_dict(r), "list": _pick_list(r.data)})
                return
            if path == "/api/match/local":
                r = app.match.local_one(**data)
                self._ok({**_result_dict(r), "list": _pick_list(r.data)})
                return
            if path == "/api/match/remove":
                self._ok(_result_dict(app.match.remove(str(data.get("type") or "1"))))
                return
            if path == "/api/match/bottle-throw":
                self._ok(_result_dict(app.match.throw_bottle(**data)))
                return
            if path == "/api/match/bottle-pick":
                r = app.match.pick_bottle(**data)
                self._ok({**_result_dict(r), "list": _pick_list(r.data)})
                return

            # tasks
            if path == "/api/tasks/receive":
                tid = str(data.get("id") or data.get("task_id") or "")
                r = app.call("receiveHotActivityList", id=tid)
                self._ok(_result_dict(r))
                return

            # room
            if path == "/api/room/create":
                r = app.room.create(str(data.get("type") or "处CP"))
                self._ok(_result_dict(r))
                return

            # wallet / pay
            if path == "/api/pay/coin":
                channel = (data.get("channel") or "wechat").lower()
                coin_id = str(data.get("coin_id") or "1")
                res = (
                    user.native.pay.prepare_coin_alipay(coin_id)
                    if channel == "alipay"
                    else user.native.pay.prepare_coin_wechat(coin_id)
                )
                self._ok(res.to_dict())
                return
            if path == "/api/pay/vip":
                channel = (data.get("channel") or "wechat").lower()
                level = str(data.get("level") or "vip")
                res = (
                    user.native.pay.prepare_vip_alipay(level)
                    if channel == "alipay"
                    else user.native.pay.prepare_vip_wechat(level)
                )
                self._ok(res.to_dict())
                return
            if path == "/api/pay/card":
                self._ok(user.native.pay.buy_match_card(str(data.get("card_id") or "1")).to_dict())
                return
            if path == "/api/wallet/svip-try":
                self._ok(_result_dict(app.economy.svip_try()))
                return
            if path == "/api/wallet/exchange-vip":
                self._ok(
                    _result_dict(
                        app.economy.money_exchange_vip(int(data.get("vip_id") or 5))
                    )
                )
                return

            # face
            if path == "/api/face/init":
                sess = user.native.face.start(
                    str(data.get("cert_name") or ""),
                    str(data.get("cert_no") or ""),
                    str(data.get("meta_info") or ""),
                )
                self._ok(sess.to_dict())
                return
            if path == "/api/face/describe":
                sess = user.native.face.describe(
                    certify_id=data.get("certify_id"),
                    cert_name=data.get("cert_name"),
                    cert_no=data.get("cert_no"),
                )
                self._ok(sess.to_dict())
                return

            # online presence
            if path == "/api/online":
                self._ok(_result_dict(app.misc.update_online(first=bool(data.get("first")))))
                return

        except Exception as e:
            self._ok({"ok": False, "error": str(e)}, 400)
            return

        self._ok({"ok": False, "error": "not found", "path": path}, 404)

    def _serve_static(self, name: str) -> None:
        safe = Path(name).name
        path = STATIC_DIR / safe
        if not path.is_file():
            path = STATIC_DIR / "index.html"
        if not path.is_file():
            self._send(404, b"missing static", "text/plain")
            return
        data = path.read_bytes()
        ct = "text/html; charset=utf-8"
        if path.suffix == ".js":
            ct = "application/javascript; charset=utf-8"
        elif path.suffix == ".css":
            ct = "text/css; charset=utf-8"
        self._send(200, data, ct)


def main(argv=None) -> int:
    global STORE
    ap = argparse.ArgumentParser(description="bbw App Web — use like the APK")
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
    print("  登录后使用：首页 / 匹配 / 社交 / 消息 / 我的", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        for s in list(STORE.list_public()):
            STORE.drop(s["web_sid"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
