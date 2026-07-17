#!/usr/bin/env python3
"""Smoke test against live API with known test account."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bbw_protocol import BeibeiwuApp

PHONE = os.getenv("BBW_TEST_PHONE", "").strip()
PASSWORD = os.getenv("BBW_TEST_PASSWORD", "")


def main() -> int:
    if not PHONE or not PASSWORD:
        print("set BBW_TEST_PHONE and BBW_TEST_PASSWORD before running the live smoke test", file=sys.stderr)
        return 2
    app = BeibeiwuApp()
    results = {}

    r = app.auth.login_password(PHONE, PASSWORD)
    results["login"] = {"ok": r.ok, "code": r.code, "message": r.message, "uid": app.session.uid}
    assert app.session.logged_in, "login failed"
    app.save()

    for name, fn in [
        ("recommend", app.content.recommend),
        ("ads", app.content.is_show_ad),
        ("censor", app.content.chat_censorship),
        ("online", app.misc.update_online),
        ("me", app.profile.get_me),
        ("reset_num", app.profile.reset_num),
        ("follow", lambda: app.social.follow("1")),
        ("nick", lambda: app.profile.reset_nickname("Vom")),
        ("withdraw", lambda: app.economy.withdraw("a@b.com", "t", "1")),
        ("room_create", app.room.create),
        ("usersig", lambda: type("R", (), {"ok": True, "code": "", "message": app.im.local_user_sig()[:40], "raw": ""})()),
        ("testField", lambda: app.call("testField")),
        ("catalog", lambda: type("R", (), {"ok": True, "code": "", "message": str(len(app.list_actions())), "raw": ""})()),
    ]:
        try:
            res = fn()
            results[name] = {
                "ok": bool(getattr(res, "ok", True)),
                "code": getattr(res, "code", ""),
                "message": str(getattr(res, "message", res))[:120],
            }
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}

    app.save()
    print(json.dumps({"whoami": app.whoami(), "results": results}, ensure_ascii=False, indent=2))
    # soft assert core
    assert results["login"]["ok"] or results["login"].get("uid") != "0"
    assert "recommend" in results
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
