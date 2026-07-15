#!/usr/bin/env python3
"""Multi-user BFF for bbw_web — isolated from bbw_protocol core.

  cd analysis
  python -m bbw_web --port 8765

Browser auth:
  - Cookie ``bbw_sid`` or header ``X-BBW-SID`` / query ``sid=``
  - POST /api/auth/login  → sets cookie + returns web_sid

Protocol secrets (UserSig key) stay server-side; each web_sid maps to its own BeibeiwuApp.
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
        "ok": getattr(r, "ok", False),
        "status": getattr(r, "status", 0),
        "code": getattr(r, "code", ""),
        "message": getattr(r, "message", ""),
        "kind": getattr(r, "kind", ""),
        "data": getattr(r, "data", None),
        "raw_preview": (getattr(r, "raw", None) or "")[:500],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "bbw-web-bff/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-BBW-SID",
        )

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
            st, body, ct = _json_bytes(
                {"ok": True, "service": "bbw-web-bff", "module": "bbw_web", **STORE.stats()}
            )
            self._send(st, body, ct)
            return

        if path == "/api/sessions":
            # admin-style list of active web sessions (local use)
            st, body, ct = _json_bytes({"ok": True, "sessions": STORE.list_public()})
            self._send(st, body, ct)
            return

        sid = self._sid(qs)

        if path == "/api/me":
            u = STORE.get(sid)
            if not u:
                st, body, ct = _json_bytes(
                    {"ok": False, "logged_in": False, "error": "no web session"},
                    401,
                )
                self._send(st, body, ct)
                return
            st, body, ct = _json_bytes({"ok": True, **u.public(), "native": u.native.status()})
            self._send(st, body, ct)
            return

        # routes that need a web session
        try:
            user = STORE.require(sid)
        except KeyError:
            st, body, ct = _json_bytes({"ok": False, "error": "login required"}, 401)
            self._send(st, body, ct)
            return

        if path == "/api/bootstrap":
            prefer = (qs.get("prefer") or ["local"])[0]
            try:
                data = user.native.web_bootstrap(prefer_tim=prefer)
            except Exception as e:
                st, body, ct = _json_bytes({"ok": False, "error": str(e)}, 400)
                self._send(st, body, ct)
                return
            st, body, ct = _json_bytes({"ok": True, "web_sid": user.web_sid, **data})
            self._send(st, body, ct)
            return

        if path == "/api/im/tim":
            prefer = (qs.get("prefer") or ["local"])[0]
            try:
                payload = user.native.im.tim_login_payload(prefer=prefer)
                st, body, ct = _json_bytes({"ok": True, **payload})
            except Exception as e:
                st, body, ct = _json_bytes({"ok": False, "error": str(e)}, 400)
            self._send(st, body, ct)
            return

        if path == "/api/im/rong":
            cred = user.native.im.rong_register()
            st, body, ct = _json_bytes({"ok": cred.ok, **cred.to_dict()})
            self._send(st, body, ct)
            return

        if path == "/api/im/bootstrap":
            prefer = (qs.get("prefer") or ["local"])[0]
            st, body, ct = _json_bytes(user.native.im.bootstrap(prefer_tim=prefer))
            self._send(st, body, ct)
            return

        if path == "/api/pay/capabilities":
            st, body, ct = _json_bytes(user.native.pay.capabilities())
            self._send(st, body, ct)
            return

        if path == "/api/face/status":
            st, body, ct = _json_bytes(user.native.face.status_hint())
            self._send(st, body, ct)
            return

        if path == "/api/heartbeat":
            st, body, ct = _json_bytes(
                user.heartbeat.status() if user.heartbeat else {"running": False}
            )
            self._send(st, body, ct)
            return

        if path == "/api/gifts":
            st, body, ct = _json_bytes(_result_dict(user.app.content.gift_list()))
            self._send(st, body, ct)
            return

        if path == "/api/profile/me":
            st, body, ct = _json_bytes(_result_dict(user.app.profile.get_me()))
            user.persist()
            self._send(st, body, ct)
            return

        st, body, ct = _json_bytes({"ok": False, "error": "not found", "path": path}, 404)
        self._send(st, body, ct)

    def do_POST(self) -> None:  # noqa: N802
        assert STORE is not None
        path = urlparse(self.path).path
        data = self._read_json()
        qs = parse_qs(urlparse(self.path).query)
        sid = self._sid(qs)

        # ---- auth (no prior session required) ----
        if path == "/api/auth/login":
            phone = str(data.get("phone") or data.get("userAccount") or "").strip()
            password = str(data.get("password") or data.get("userPassword") or "")
            mode = str(data.get("mode") or "password")
            label = str(data.get("label") or "")
            if not phone:
                st, body, ct = _json_bytes({"ok": False, "error": "phone required"}, 400)
                self._send(st, body, ct)
                return
            try:
                if mode == "onekey":
                    user = STORE.login_onekey(sid, phone, label=label)
                else:
                    if not password:
                        st, body, ct = _json_bytes(
                            {"ok": False, "error": "password required"}, 400
                        )
                        self._send(st, body, ct)
                        return
                    user = STORE.login_password(sid, phone, password, label=label)
            except Exception as e:
                st, body, ct = _json_bytes({"ok": False, "error": str(e)}, 400)
                self._send(st, body, ct)
                return
            payload = {"ok": True, **user.public()}
            st, body, ct = _json_bytes(payload)
            self._send(st, body, ct, set_cookie=user.web_sid)
            return

        if path == "/api/auth/logout":
            if sid:
                STORE.drop(sid)
            st, body, ct = _json_bytes({"ok": True})
            self._send(st, body, ct, clear_cookie=True)
            return

        if path == "/api/auth/guest":
            # empty web session (not logged into banghua)
            user = STORE.create(label=str(data.get("label") or "guest"))
            st, body, ct = _json_bytes({"ok": True, **user.public()})
            self._send(st, body, ct, set_cookie=user.web_sid)
            return

        # ---- need web session ----
        try:
            user = STORE.require(sid)
        except KeyError:
            st, body, ct = _json_bytes({"ok": False, "error": "login required"}, 401)
            self._send(st, body, ct)
            return

        try:
            if path == "/api/heartbeat/start":
                interval = float(data.get("interval_sec") or STORE.heartbeat_interval)
                st, body, ct = _json_bytes(
                    {"ok": True, **user.start_heartbeat(interval)}
                )
                self._send(st, body, ct)
                return

            if path == "/api/heartbeat/stop":
                user.stop_heartbeat()
                st, body, ct = _json_bytes({"ok": True, "running": False})
                self._send(st, body, ct)
                return

            if path == "/api/heartbeat/once":
                if not user.heartbeat:
                    from bbw_protocol.heartbeat import Heartbeat

                    user.heartbeat = Heartbeat(user.app)
                st, body, ct = _json_bytes(user.heartbeat.once())
                self._send(st, body, ct)
                return

            if path == "/api/call":
                action = str(data.get("action") or "")
                params = data.get("params") or {}
                if not action:
                    st, body, ct = _json_bytes({"ok": False, "error": "action required"}, 400)
                    self._send(st, body, ct)
                    return
                if not isinstance(params, dict):
                    params = {}
                r = user.app.call(action, **params)
                st, body, ct = _json_bytes(_result_dict(r))
                self._send(st, body, ct)
                return

            if path == "/api/social/follow":
                uid = str(data.get("uid") or "")
                st, body, ct = _json_bytes(_result_dict(user.app.social.follow(uid)))
                self._send(st, body, ct)
                return

            if path == "/api/profile/nick":
                name = str(data.get("name") or data.get("nickname") or "")
                r = user.app.profile.reset_nickname(name)
                user.persist()
                st, body, ct = _json_bytes(_result_dict(r))
                self._send(st, body, ct)
                return

            if path == "/api/pay/coin":
                channel = (data.get("channel") or "wechat").lower()
                coin_id = str(data.get("coin_id") or data.get("coinId") or "1")
                if channel == "alipay":
                    res = user.native.pay.prepare_coin_alipay(coin_id)
                else:
                    res = user.native.pay.prepare_coin_wechat(coin_id)
                st, body, ct = _json_bytes(res.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/pay/vip":
                channel = (data.get("channel") or "wechat").lower()
                level = str(data.get("level") or "vip")
                extra = {
                    k: v
                    for k, v in data.items()
                    if k not in ("channel", "level")
                }
                if channel == "alipay":
                    res = user.native.pay.prepare_vip_alipay(level, **extra)
                else:
                    res = user.native.pay.prepare_vip_wechat(level, **extra)
                st, body, ct = _json_bytes(res.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/pay/card":
                res = user.native.pay.buy_match_card(str(data.get("card_id") or "1"))
                st, body, ct = _json_bytes(res.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/face/init":
                sess = user.native.face.start(
                    str(data.get("cert_name") or ""),
                    str(data.get("cert_no") or ""),
                    str(data.get("meta_info") or ""),
                )
                st, body, ct = _json_bytes(sess.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/face/describe":
                sess = user.native.face.describe(
                    certify_id=data.get("certify_id"),
                    cert_name=data.get("cert_name"),
                    cert_no=data.get("cert_no"),
                )
                st, body, ct = _json_bytes(sess.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/face/manual":
                out = user.native.face.manual(
                    str(data.get("cert_name") or ""),
                    str(data.get("cert_no") or ""),
                    str(data.get("result") or "pending"),
                )
                st, body, ct = _json_bytes(out)
                self._send(st, body, ct)
                return
        except Exception as e:
            st, body, ct = _json_bytes({"ok": False, "error": str(e)}, 400)
            self._send(st, body, ct)
            return

        st, body, ct = _json_bytes({"ok": False, "error": "not found"}, 404)
        self._send(st, body, ct)

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
    ap = argparse.ArgumentParser(description="bbw_web multi-user BFF")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-heartbeat", action="store_true", help="disable auto heartbeat on login")
    ap.add_argument("--ttl-days", type=float, default=7.0)
    args = ap.parse_args(argv)

    STORE = SessionStore(
        ttl_sec=args.ttl_days * 86400,
        auto_heartbeat=not args.no_heartbeat,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"bbw_web BFF http://{args.host}:{args.port}/  "
        f"(multi-user, core=bbw_protocol isolated)",
        flush=True,
    )
    print(
        "  POST /api/auth/login  GET /api/me  GET /api/sessions  Cookie: bbw_sid",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        for s in list(STORE.list_public()):
            STORE.drop(s["web_sid"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
