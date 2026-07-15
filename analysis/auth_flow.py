#!/usr/bin/env python3
"""Protocol login/register probe for beibeiwu (CTF)."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

PHONE = "19122614669"
PASSWORD = "YOUR_PASSWORD"  # set locally; do not commit real secrets
VERSION_CODE = "148"
PACKAGE = "xin.banghua.beiyuan0"

APPLET = "https://applet.banghua.xin/app/index.php"
SMS_URL = "https://applet.banghua.xin/sms_beibeiwu.php"


def md5_hex(s: str) -> str:
    raw = bytes(ord(c) & 0xFF for c in s)
    return hashlib.md5(raw).hexdigest()


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sign_token(uid: str = "0") -> str:
    return md5_hex(f"{uid}socialchat{uid}")


def expire_token(ts: Optional[int] = None) -> str:
    if ts is None:
        ts = int(time.time())
    t = str(ts)
    return sha1_hex("xiaobei" + t) + t


def unique_token(uid: str = "0") -> str:
    return sha1_hex("xiaobei" + uid)


def headers(uid: str = "0", author_token: str = "0") -> Dict[str, str]:
    return {
        "AUTHOR-TOKEN": author_token or "0",
        "EXPIRE-TOKEN": expire_token(),
        "SIGN-TOKEN": sign_token(uid or "0"),
        "User-Agent": f"okhttp/4.9.3 beibeiwu/{VERSION_CODE}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }


def action_url(action: str, i: str = "999999", m: str = "socialchat") -> str:
    return f"{APPLET}?i={i}&c=entry&a=webapp&do={action}&m={m}"


def post(
    url: str,
    body: Dict[str, str],
    uid: str = "0",
    author_token: str = "0",
    timeout: int = 25,
) -> Tuple[int, str, Dict[str, str]]:
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers(uid, author_token), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw, dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, raw, dict(e.headers.items()) if e.headers else {}
    except Exception as e:
        return -1, f"EXC: {type(e).__name__}: {e}", {}


def pretty(label: str, status: int, body: str) -> Any:
    print("=" * 72)
    print(f"[{label}] HTTP {status}")
    print(body[:4000] if body else "<empty>")
    print()
    try:
        return json.loads(body)
    except Exception:
        return None


def try_signin0() -> Optional[dict]:
    body = {
        "userAccount": PHONE,
        "userPassword": PASSWORD,
        "uniquelogintoken": unique_token("0"),
        "phonebrand": "Android",
        "pushregid": "protocol_probe",
        "version_code": VERSION_CODE,
    }
    status, raw, _ = post(action_url("signin0"), body)
    return pretty("signin0 password login", status, raw)


def try_send_sms() -> Optional[dict]:
    body = {"phoneNumber": PHONE}
    status, raw, _ = post(SMS_URL, body)
    return pretty("sms_beibeiwu.php send code", status, raw)


def try_sms_verify(code: str) -> Optional[dict]:
    body = {"phone": PHONE, "code": code}
    status, raw, _ = post(action_url("smsVerify0"), body)
    return pretty(f"smsVerify0 code={code}", status, raw)


def try_onekey_login() -> Optional[dict]:
    body = {
        "userAccount": PHONE,
        "uniquelogintoken": unique_token("0"),
        "version_code": VERSION_CODE,
        "phonebrand": "Android",
        "pushregid": "protocol_probe",
    }
    status, raw, _ = post(action_url("SigninOneKeyLogin1"), body)
    return pretty("SigninOneKeyLogin1", status, raw)


def try_find_password() -> Optional[dict]:
    # FindPasswordActivity posts sign + userPhone + userPassword
    body = {
        "sign": sign_token(PHONE),
        "userPhone": PHONE,
        "userPassword": PASSWORD,
    }
    status, raw, _ = post(action_url("findpassword"), body)
    return pretty("findpassword (set password)", status, raw)


def try_signup_variants() -> None:
    for action in ("signup", "signup0", "Signup", "register", "Register"):
        body = {
            "userAccount": PHONE,
            "userPassword": PASSWORD,
            "uniquelogintoken": unique_token("0"),
            "phonebrand": "Android",
            "pushregid": "protocol_probe",
            "version_code": VERSION_CODE,
            "code": "0000",
            "smscode": "0000",
        }
        status, raw, _ = post(action_url(action), body)
        pretty(f"probe {action}", status, raw)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"

    if cmd == "probe":
        print(f"phone={PHONE}")
        print(f"SIGN-TOKEN(uid=0)={sign_token('0')}")
        print(f"EXPIRE-TOKEN={expire_token()}")
        print()
        try_signin0()
        try_send_sms()
        try_signup_variants()
        return

    if cmd == "login":
        try_signin0()
        return

    if cmd == "sms":
        try_send_sms()
        return

    if cmd == "verify":
        code = sys.argv[2] if len(sys.argv) > 2 else ""
        if not code:
            print("usage: auth_flow.py verify <sms_code>")
            return
        try_sms_verify(code)
        try_onekey_login()
        try_find_password()  # after account exists, set password
        try_signin0()
        return

    if cmd == "setpass":
        try_find_password()
        try_signin0()
        return

    if cmd == "onekey":
        try_onekey_login()
        return

    print("commands: probe | login | sms | verify <code> | setpass | onekey")


if __name__ == "__main__":
    main()
