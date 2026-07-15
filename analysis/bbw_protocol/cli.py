#!/usr/bin/env python3
"""CLI for beibeiwu protocol client — use the app without the APK UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

# allow running as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bbw_protocol.app import BeibeiwuApp  # noqa: E402
from bbw_protocol.client import ApiResult  # noqa: E402


def _print_result(r: ApiResult, raw: bool = False) -> int:
    if raw:
        print(r.raw)
        return 0 if r.ok else 1
    summary = {
        "ok": r.ok,
        "status": r.status,
        "kind": r.kind,
        "code": r.code,
        "message": r.message,
        "extra": r.extra,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if r.data is not None and r.kind != "empty":
        data = r.data
        # trim huge
        text = json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
        if len(text) > 4000:
            text = text[:4000] + "\n... [truncated]"
        print(text)
    return 0 if r.ok else 1


def _parse_kv(pairs) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for p in pairs or []:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k] = v
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bbw",
        description="贝贝屋/小贝乐园 协议客户端（CTF）",
    )
    p.add_argument("--session", default=None, help="session.json path")
    p.add_argument("--raw", action="store_true", help="print raw body only")
    sub = p.add_subparsers(dest="cmd", required=True)

    # session
    sub.add_parser("whoami", help="show session summary")
    sub.add_parser("save", help="save session")
    s = sub.add_parser("set", help="set session fields")
    s.add_argument("kv", nargs="+", help="uid=1 token=xx phone=..")

    # auth
    sp = sub.add_parser("login", help="password login")
    sp.add_argument("--phone", required=True)
    sp.add_argument("--password", required=True)
    sp = sub.add_parser("onekey", help="SigninOneKeyLogin1 (weak)")
    sp.add_argument("--phone", required=True)
    sp = sub.add_parser("sms-send", help="send SMS code")
    sp.add_argument("--phone", required=True)
    sp = sub.add_parser("sms-login", help="verify SMS then onekey login")
    sp.add_argument("--phone", required=True)
    sp.add_argument("--code", required=True)
    sp = sub.add_parser("setpass", help="findpassword")
    sp.add_argument("--phone", required=True)
    sp.add_argument("--password", required=True)
    sub.add_parser("logout")

    # content
    sub.add_parser("gifts")
    sub.add_parser("recommend")
    sub.add_parser("ads")
    sub.add_parser("censor")
    sub.add_parser("bootstrap", help="cold-start style batch")

    # social
    sp = sub.add_parser("follow")
    sp.add_argument("uid")
    sp = sub.add_parser("unfollow")
    sp.add_argument("uid")
    sp = sub.add_parser("followers")
    sp.add_argument("--uid", default=None)
    sp = sub.add_parser("fans")
    sp.add_argument("--uid", default=None)

    # profile
    sub.add_parser("me")
    sp = sub.add_parser("user")
    sp.add_argument("uid")
    sp = sub.add_parser("nick")
    sp.add_argument("name")
    sub.add_parser("reset-num")

    # economy
    sub.add_parser("svip-try")
    sp = sub.add_parser("exchange-vip")
    sp.add_argument("--vip-id", default="5")
    sp = sub.add_parser("withdraw")
    sp.add_argument("--alipay")
    sp.add_argument("--name")
    sp.add_argument("--amount")

    # room / match / im
    sp = sub.add_parser("room-create")
    sp.add_argument("--type", default="处CP")
    sub.add_parser("room-auth")
    sp = sub.add_parser("usersig")
    sp.add_argument("--uid", default=None)
    sub.add_parser("txim-sign")
    sub.add_parser("online")

    # generic
    sp = sub.add_parser("call", help="call any short action")
    sp.add_argument("action")
    sp.add_argument("kv", nargs="*", help="k=v")
    sp = sub.add_parser("call-redis")
    sp.add_argument("action")
    sp.add_argument("kv", nargs="*")
    sp = sub.add_parser("call-url")
    sp.add_argument("url")
    sp.add_argument("kv", nargs="*")
    sp = sub.add_parser("actions")
    sp.add_argument("--cat", default=None)

    # repl
    sub.add_parser("repl", help="interactive shell")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    app = BeibeiwuApp.load(args.session)
    raw = args.raw

    if args.cmd == "whoami":
        print(json.dumps(app.whoami(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "save":
        print(app.save(args.session))
        return 0
    if args.cmd == "set":
        for k, v in _parse_kv(args.kv).items():
            if hasattr(app.session, k):
                setattr(app.session, k, v)
        app.save(args.session)
        print(json.dumps(app.whoami(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "login":
        r = app.auth.login_password(args.phone, args.password)
        app.save(args.session)
        print(json.dumps(app.whoami(), ensure_ascii=False, indent=2))
        return _print_result(r, raw)
    if args.cmd == "onekey":
        r = app.auth.login_onekey(args.phone)
        app.save(args.session)
        print(json.dumps(app.whoami(), ensure_ascii=False, indent=2))
        return _print_result(r, raw)
    if args.cmd == "sms-send":
        return _print_result(app.auth.send_sms(args.phone), raw)
    if args.cmd == "sms-login":
        r = app.auth.sms_login(args.phone, args.code)
        app.save(args.session)
        return _print_result(r, raw)
    if args.cmd == "setpass":
        r = app.auth.find_password(args.phone, args.password)
        app.save(args.session)
        return _print_result(r, raw)
    if args.cmd == "logout":
        r = app.auth.logout()
        app.save(args.session)
        return _print_result(r, raw)

    if args.cmd == "gifts":
        return _print_result(app.content.gift_list(), raw)
    if args.cmd == "recommend":
        return _print_result(app.content.recommend(), raw)
    if args.cmd == "ads":
        return _print_result(app.content.is_show_ad(), raw)
    if args.cmd == "censor":
        return _print_result(app.content.chat_censorship(), raw)
    if args.cmd == "bootstrap":
        out = {k: {"ok": v.ok, "code": v.code, "message": v.message, "kind": v.kind} for k, v in app.bootstrap().items()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        app.save(args.session)
        return 0

    if args.cmd == "follow":
        return _print_result(app.social.follow(args.uid), raw)
    if args.cmd == "unfollow":
        return _print_result(app.social.unfollow(args.uid), raw)
    if args.cmd == "followers":
        return _print_result(app.social.follow_users(args.uid), raw)
    if args.cmd == "fans":
        return _print_result(app.social.fans_users(args.uid), raw)

    if args.cmd == "me":
        r = app.profile.get_me()
        app.save(args.session)
        return _print_result(r, raw)
    if args.cmd == "user":
        return _print_result(app.profile.get_user(args.uid), raw)
    if args.cmd == "nick":
        r = app.profile.reset_nickname(args.name)
        app.save(args.session)
        return _print_result(r, raw)
    if args.cmd == "reset-num":
        return _print_result(app.profile.reset_num(), raw)

    if args.cmd == "svip-try":
        return _print_result(app.economy.svip_try(), raw)
    if args.cmd == "exchange-vip":
        return _print_result(app.economy.money_exchange_vip(int(args.vip_id)), raw)
    if args.cmd == "withdraw":
        return _print_result(
            app.economy.withdraw(args.alipay, args.name, args.amount), raw
        )

    if args.cmd == "room-create":
        return _print_result(app.room.create(args.type), raw)
    if args.cmd == "room-auth":
        return _print_result(app.room.auth(), raw)
    if args.cmd == "usersig":
        print(app.im.local_user_sig(args.uid))
        return 0
    if args.cmd == "txim-sign":
        return _print_result(app.im.tencent_sign(), raw)
    if args.cmd == "online":
        return _print_result(app.misc.update_online(), raw)

    if args.cmd == "call":
        return _print_result(app.call(args.action, **_parse_kv(args.kv)), raw)
    if args.cmd == "call-redis":
        return _print_result(app.call_redis(args.action, **_parse_kv(args.kv)), raw)
    if args.cmd == "call-url":
        return _print_result(app.call_url(args.url, **_parse_kv(args.kv)), raw)
    if args.cmd == "actions":
        acts = app.list_actions(args.cat)
        print(f"count={len(acts)}")
        for a in acts:
            print(a)
        return 0

    if args.cmd == "repl":
        return run_repl(app)

    print("unknown command")
    return 2


def run_repl(app: BeibeiwuApp) -> int:
    print("Beibeiwu protocol REPL. cmds: whoami, login phone pass, call ACTION k=v, help, quit")
    while True:
        try:
            line = input("bbw> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            break
        if line == "help":
            print("whoami | save | login <phone> <pass> | onekey <phone>")
            print("call <action> [k=v ...] | redis <action> [k=v ...]")
            print("follow <uid> | nick <name> | me | gifts | bootstrap")
            print("actions [cat] | quit")
            continue
        parts = line.split()
        cmd = parts[0]
        try:
            if cmd == "whoami":
                print(json.dumps(app.whoami(), ensure_ascii=False, indent=2))
            elif cmd == "save":
                print(app.save())
            elif cmd == "login" and len(parts) >= 3:
                r = app.auth.login_password(parts[1], parts[2])
                app.save()
                _print_result(r)
                print(json.dumps(app.whoami(), ensure_ascii=False, indent=2))
            elif cmd == "onekey" and len(parts) >= 2:
                r = app.auth.login_onekey(parts[1])
                app.save()
                _print_result(r)
            elif cmd == "call" and len(parts) >= 2:
                kv = _parse_kv(parts[2:])
                _print_result(app.call(parts[1], **kv))
            elif cmd == "redis" and len(parts) >= 2:
                _print_result(app.call_redis(parts[1], **_parse_kv(parts[2:])))
            elif cmd == "follow" and len(parts) >= 2:
                _print_result(app.social.follow(parts[1]))
            elif cmd == "nick" and len(parts) >= 2:
                _print_result(app.profile.reset_nickname(parts[1]))
            elif cmd == "me":
                _print_result(app.profile.get_me())
            elif cmd == "gifts":
                _print_result(app.content.gift_list())
            elif cmd == "bootstrap":
                out = {k: v.ok for k, v in app.bootstrap().items()}
                print(out)
            elif cmd == "actions":
                cat = parts[1] if len(parts) > 1 else None
                acts = app.list_actions(cat)
                print(len(acts))
                print("\n".join(acts[:100]))
            else:
                print("unknown; type help")
        except Exception as e:
            print("ERR", e)
    app.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
