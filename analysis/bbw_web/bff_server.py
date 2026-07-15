#!/usr/bin/env python3
"""Minimal BFF: session + IM credentials + pay prepare + face HTTP.

Stdlib only. Secrets (TXIM_SECRETKEY) stay on this process — browser only gets UserSig.

  cd analysis
  python -m bbw_web.bff_server --port 8765

  GET  /api/health
  GET  /api/me
  GET  /api/im/tim?prefer=local
  GET  /api/im/bootstrap
  POST /api/pay/coin  {"channel":"wechat"|"alipay","coin_id":"1"}
  POST /api/face/init {"cert_name":"...","cert_no":"...","meta_info":"..."}
  POST /api/face/describe {"certify_id":"...","cert_name":"...","cert_no":"..."}
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_protocol.app import BeibeiwuApp  # noqa: E402
from bbw_protocol.adapters import NativeBundle  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _json_bytes(obj: Any, status: int = 200) -> Tuple[int, bytes, str]:
    body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


class BffState:
    def __init__(self, session_path: Optional[str] = None):
        self.session_path = session_path
        self.app = BeibeiwuApp.load(session_path)
        self.native = NativeBundle(self.app)

    def reload(self) -> None:
        self.app = BeibeiwuApp.load(self.session_path)
        self.native = NativeBundle(self.app)


STATE: Optional[BffState] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "bbw-bff/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        assert STATE is not None
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return

        if path == "/api/health":
            st, body, ct = _json_bytes({"ok": True, "service": "bbw-bff"})
            self._send(st, body, ct)
            return

        if path == "/api/me":
            STATE.reload()
            st, body, ct = _json_bytes(STATE.native.status())
            self._send(st, body, ct)
            return

        if path == "/api/bootstrap":
            STATE.reload()
            prefer = (qs.get("prefer") or ["local"])[0]
            st, body, ct = _json_bytes(STATE.native.web_bootstrap(prefer_tim=prefer))
            self._send(st, body, ct)
            return

        if path == "/api/im/tim":
            STATE.reload()
            prefer = (qs.get("prefer") or ["local"])[0]
            try:
                payload = STATE.native.im.tim_login_payload(prefer=prefer)
                st, body, ct = _json_bytes({"ok": True, **payload})
            except Exception as e:
                st, body, ct = _json_bytes({"ok": False, "error": str(e)}, 400)
            self._send(st, body, ct)
            return

        if path == "/api/im/rong":
            STATE.reload()
            cred = STATE.native.im.rong_register()
            st, body, ct = _json_bytes({"ok": cred.ok, **cred.to_dict()})
            self._send(st, body, ct)
            return

        if path == "/api/im/bootstrap":
            STATE.reload()
            prefer = (qs.get("prefer") or ["local"])[0]
            st, body, ct = _json_bytes(STATE.native.im.bootstrap(prefer_tim=prefer))
            self._send(st, body, ct)
            return

        if path == "/api/pay/capabilities":
            st, body, ct = _json_bytes(STATE.native.pay.capabilities())
            self._send(st, body, ct)
            return

        if path == "/api/face/status":
            STATE.reload()
            st, body, ct = _json_bytes(STATE.native.face.status_hint())
            self._send(st, body, ct)
            return

        st, body, ct = _json_bytes({"ok": False, "error": "not found", "path": path}, 404)
        self._send(st, body, ct)

    def do_POST(self) -> None:  # noqa: N802
        assert STATE is not None
        path = urlparse(self.path).path
        data = self._read_json()
        STATE.reload()

        try:
            if path == "/api/pay/coin":
                channel = (data.get("channel") or "wechat").lower()
                coin_id = str(data.get("coin_id") or data.get("coinId") or "1")
                if channel == "alipay":
                    res = STATE.native.pay.prepare_coin_alipay(coin_id)
                else:
                    res = STATE.native.pay.prepare_coin_wechat(coin_id)
                st, body, ct = _json_bytes(res.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/pay/vip":
                channel = (data.get("channel") or "wechat").lower()
                level = str(data.get("level") or "vip")
                extra = {k: v for k, v in data.items() if k not in ("channel", "level")}
                if channel == "alipay":
                    res = STATE.native.pay.prepare_vip_alipay(level, **extra)
                else:
                    res = STATE.native.pay.prepare_vip_wechat(level, **extra)
                st, body, ct = _json_bytes(res.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/pay/card":
                card_id = str(data.get("card_id") or "1")
                res = STATE.native.pay.buy_match_card(card_id)
                st, body, ct = _json_bytes(res.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/face/init":
                sess = STATE.native.face.start(
                    str(data.get("cert_name") or ""),
                    str(data.get("cert_no") or ""),
                    str(data.get("meta_info") or ""),
                )
                st, body, ct = _json_bytes(sess.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/face/describe":
                sess = STATE.native.face.describe(
                    certify_id=data.get("certify_id"),
                    cert_name=data.get("cert_name"),
                    cert_no=data.get("cert_no"),
                )
                st, body, ct = _json_bytes(sess.to_dict())
                self._send(st, body, ct)
                return

            if path == "/api/face/manual":
                out = STATE.native.face.manual(
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
        # prevent path traversal
        safe = Path(name).name if "/" not in name and "\\" not in name else Path(name).name
        if name.count("/") == 0:
            path = STATIC_DIR / safe
        else:
            # allow only files directly under static/
            path = (STATIC_DIR / Path(name).name).resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())):
                self._send(403, b"forbidden", "text/plain")
                return
        if not path.is_file():
            # try index
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
        elif path.suffix == ".json":
            ct = "application/json; charset=utf-8"
        self._send(200, data, ct)


def main(argv=None) -> int:
    global STATE
    ap = argparse.ArgumentParser(description="bbw native-capability BFF")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--session", default=None, help="path to session.json")
    args = ap.parse_args(argv)

    STATE = BffState(args.session)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"bbw BFF http://{args.host}:{args.port}/  "
        f"(session uid={STATE.app.session.uid or 'none'})",
        flush=True,
    )
    print("  /api/im/tim  /api/bootstrap  /api/pay/coin  static TIM demo", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
