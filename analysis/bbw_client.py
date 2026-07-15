#!/usr/bin/env python3
"""
beibeiwu / 小贝乐园 APK client signing helpers (static reverse only).

Reimplements client-side auth headers found in:
  - cn.leyuan.base_library.utils.MD5Tool
  - cn.leyuan.base_library.utils.CommonUtil
  - cn.leyuan.base_library.utils.SHA1Util
  - cn.leyuan.base_library.utils.OkHttpInstance
  - xin.banghua.beiyuan0.TencentIM.signature.GenerateTestUserSig
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import random
import time
import zlib
from typing import Any, Dict, Optional
from urllib.parse import urlencode


APPLET_BASE = "https://applet.banghua.xin/app/index.php"
REDIS_BASE = "https://redis.banghua.xin/app/index.php"
SMS_URL = "https://applet.banghua.xin/sms_beibeiwu.php"
TXIM_SIGN_URL = "https://applet.banghua.xin/tximsign.php"
RONG_REGISTER_URL = (
    "https://applet.banghua.xin/otherinterface/rongyun/"
    "RongCloudNew/example/User/userregister.php"
)
AGORA_RTC_URL = (
    "https://applet.banghua.xin/otherinterface/agora/sample/"
    "RtcTokenBuilderSampleXiaobei.php"
)
AGORA_RTM_URL = (
    "https://applet.banghua.xin/otherinterface/agora/sample/"
    "RtmTokenBuilderSampleXiaobei.php"
)

# From BuildConfig / GenerateTestUserSig
RONG_APP_KEY = "m7ua80gbmo0km"
BUSINESS_TOKEN = "lymM6dNKREIknE5VJGskfU"
BASE_SERVER = "https://redis.banghua.xin:8080/"
TXIM_SDKAPPID = 1600039823
TXIM_SECRETKEY = (
    "c064eea5978cf60af28dcbbe9dd7c35e110734fcf1a5662ead3e87e2eb8a554e"
)
WX_APP_ID_BEIYUAN0 = "wxf057dbbb960d9c39"
WX_APP_ID_BEIYUAN = "wxb8adb92718082e0b"
PACKAGE_NAME = "xin.banghua.beiyuan0"
VERSION_CODE = 148


def md5_hex(s: str) -> str:
    """Match MD5Tool.MD5: char-array -> byte cast, then lowercase hex."""
    # Java code does (byte)charArray[i], which truncates to 8-bit.
    raw = bytes(ord(c) & 0xFF for c in s)
    return hashlib.md5(raw).hexdigest()


def sha1_hex_default(s: str) -> str:
    """CommonUtil.sha1: MessageDigest digest of default charset bytes."""
    # Android default is usually UTF-8.
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sha1_iso8859_1(s: str) -> str:
    """SHA1Util.SHA1: update with iso-8859-1 bytes."""
    return hashlib.sha1(s.encode("iso-8859-1")).hexdigest()


def get_sign_token(uid: str) -> str:
    """SIGN-TOKEN = MD5(uid + 'socialchat' + uid)"""
    return md5_hex(f"{uid}socialchat{uid}")


def get_expire_token(ts: Optional[int] = None) -> str:
    """EXPIRE-TOKEN = sha1('xiaobei' + ts) + ts"""
    if ts is None:
        ts = int(time.time())
    ts_s = str(ts)
    return sha1_hex_default("xiaobei" + ts_s) + ts_s


def get_unique_login_token(uid: str) -> str:
    """uniquelogintoken = sha1('xiaobei' + uid)"""
    return sha1_hex_default("xiaobei" + uid)


def get_author_signature(uid: str, nonce: Optional[str] = None, ts_ms: Optional[str] = None) -> Dict[str, str]:
    """
    AUTHOR-UID / AUTHOR-NONCE / AUTHOR-TIMESTAMP / AUTHOR-SIGNATURE
    signature = SHA1( SHA1(uid) + nonce + timestamp )
    """
    if nonce is None:
        nonce = str(random.randint(0, 9999))
    if ts_ms is None:
        ts_ms = str(int(time.time() * 1000))
    sig = sha1_iso8859_1(sha1_iso8859_1(uid) + nonce + ts_ms)
    return {
        "AUTHOR-UID": uid,
        "AUTHOR-NONCE": nonce,
        "AUTHOR-TIMESTAMP": ts_ms,
        "AUTHOR-SIGNATURE": sig or "",
    }


def build_auth_headers(
    uid: str = "0",
    author_token: str = "0",
    with_author_sig: bool = False,
) -> Dict[str, str]:
    headers = {
        "AUTHOR-TOKEN": author_token or "0",
        "EXPIRE-TOKEN": get_expire_token(),
        "SIGN-TOKEN": get_sign_token(uid or "0"),
        "User-Agent": f"beibeiwu/{VERSION_CODE} ({PACKAGE_NAME})",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if with_author_sig:
        headers.update(get_author_signature(uid or "0"))
    return headers


def get_url(action_or_url: str, i: str = "999999", m: str = "socialchat") -> str:
    """Mirror OkHttpInstance.getUrl()."""
    if action_or_url.startswith("http"):
        return action_or_url
    return f"{APPLET_BASE}?i={i}&c=entry&a=webapp&do={action_or_url}&m={m}"


def redis_url(action: str, i: str = "888", m: str = "rediscache") -> str:
    return f"{REDIS_BASE}?i={i}&c=entry&a=webapp&do={action}&m={m}"


# --- Tencent IM UserSig (client-side SECRETKEY hardcode) ---

def _base64_url_encode(data: bytes) -> str:
    s = base64.b64encode(data).decode("ascii")
    return s.replace("+", "*").replace("/", "-").replace("=", "_")


def gen_tls_user_sig(
    identifier: str,
    sdkappid: int = TXIM_SDKAPPID,
    secret_key: str = TXIM_SECRETKEY,
    expire: int = 604800,
) -> str:
    """GenerateTestUserSig.GenTLSSignature"""
    now = int(time.time())
    content = (
        f"TLS.identifier:{identifier}\n"
        f"TLS.sdkappid:{sdkappid}\n"
        f"TLS.time:{now}\n"
        f"TLS.expire:{expire}\n"
    )
    sig = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    payload = {
        "TLS.ver": "2.0",
        "TLS.identifier": identifier,
        "TLS.sdkappid": sdkappid,
        "TLS.expire": expire,
        "TLS.time": now,
        "TLS.sig": sig,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = zlib.compress(raw)
    # Java Deflater default is zlib wrapper; zlib.compress matches.
    return _base64_url_encode(compressed)


# --- High-value endpoints (from static analysis) ---

ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "signin0": {
        "url": get_url("signin0"),
        "desc": "账号密码登录",
        "body": ["userAccount", "userPassword", "uniquelogintoken", "phonebrand", "pushregid", "version_code"],
    },
    "SigninOneKeyLogin1": {
        "url": get_url("SigninOneKeyLogin1"),
        "desc": "短信验证后一键登录",
        "body": ["userAccount", "uniquelogintoken"],
    },
    "smsVerify0": {
        "url": get_url("smsVerify0"),
        "desc": "短信验证码校验",
        "body": ["phone", "code"],
    },
    "sms_send": {
        "url": SMS_URL,
        "desc": "发送短信验证码",
        "body": ["phoneNumber"],
    },
    "uniquelogin": {
        "url": get_url("uniquelogin"),
        "desc": "唯一登录 token 对比",
        "body": ["myid", "token"],
    },
    "testField": {
        "url": get_url("testField"),
        "desc": "疑似测试接口",
        "body": [],
    },
    "SvipTry": {
        "url": get_url("SvipTry"),
        "desc": "SVIP 试用",
        "body": ["id"],
    },
    "buyCoinWechatXBXX": {
        "url": get_url("buyCoinWechatXBXX"),
        "desc": "微信充值金币下单",
        "body": ["userid", "coinId", "platform", "PackageName"],
    },
    "buyCoinAlipayXBXX": {
        "url": get_url("buyCoinAlipayXBXX"),
        "desc": "支付宝充值金币下单",
        "body": ["userid", "coinId", "platform", "PackageName"],
    },
    "withdraw": {
        "url": get_url("withdraw"),
        "desc": "提现",
        "body": ["authid", "alilogonid", "aliname", "transamount"],
    },
    "getUserAttributes": {
        "url": get_url("getUserAttributes"),
        "desc": "获取用户资料",
        "body": ["userId", "latitude", "longitude"],
    },
    "getGiftList": {
        "url": get_url("getGiftList"),
        "desc": "礼物列表",
        "body": [],
    },
    "tximsign": {
        "url": TXIM_SIGN_URL,
        "desc": "服务端发腾讯 IM UserSig",
        "body": ["uid"],
    },
    "rong_register": {
        "url": RONG_REGISTER_URL,
        "desc": "融云用户注册拿 token",
        "body": ["userId", "userNickName", "userPortrait"],
    },
    "agora_rtc": {
        "url": AGORA_RTC_URL,
        "desc": "声网 RTC token",
        "body": ["uid", "channelName"],
    },
    "agora_rtm": {
        "url": AGORA_RTM_URL,
        "desc": "声网 RTM token",
        "body": ["uid"],
    },
}


def print_request_template(
    action: str,
    uid: str = "0",
    author_token: str = "0",
    extra_body: Optional[Dict[str, str]] = None,
) -> None:
    ep = ENDPOINTS.get(action)
    if not ep:
        url = get_url(action)
        desc = action
        body_keys = list((extra_body or {}).keys())
    else:
        url = ep["url"]
        desc = ep["desc"]
        body_keys = ep["body"]

    headers = build_auth_headers(uid=uid, author_token=author_token)
    body: Dict[str, str] = {}
    for k in body_keys:
        if extra_body and k in extra_body:
            body[k] = extra_body[k]
        elif k == "version_code":
            body[k] = str(VERSION_CODE)
        elif k == "platform":
            body[k] = "android"
        elif k == "PackageName":
            body[k] = PACKAGE_NAME
        elif k == "uniquelogintoken":
            body[k] = get_unique_login_token(uid)
        elif k in ("userid", "userId", "id", "authid", "myid", "uid"):
            body[k] = uid
        else:
            body[k] = f"<{k}>"
    if extra_body:
        body.update(extra_body)

    print(f"## {action} — {desc}")
    print(f"POST {url}")
    print("Headers:")
    for k, v in headers.items():
        print(f"  {k}: {v}")
    print("Body:")
    print(f"  {urlencode(body)}")
    print()
    # curl
    h_args = " ".join([f'-H "{k}: {v}"' for k, v in headers.items()])
    print("curl:")
    print(f'curl -X POST "{url}" {h_args} -d "{urlencode(body)}"')
    print("-" * 60)


def demo() -> None:
    uid = "12345"
    print("=== Client-side tokens for uid=", uid, "===\n")
    print("SIGN-TOKEN     =", get_sign_token(uid))
    print("EXPIRE-TOKEN   =", get_expire_token())
    print("UNIQUE-LOGIN   =", get_unique_login_token(uid))
    print("AUTHOR-SIG     =", get_author_signature(uid))
    print("TXIM UserSig   =", gen_tls_user_sig(uid)[:80] + "...")
    print("\n=== Constants ===")
    print("RONG_APP_KEY   =", RONG_APP_KEY)
    print("TXIM_SDKAPPID  =", TXIM_SDKAPPID)
    print("TXIM_SECRETKEY =", TXIM_SECRETKEY)
    print("WX_APP_ID      =", WX_APP_ID_BEIYUAN0)
    print("PACKAGE        =", PACKAGE_NAME)
    print("\n=== Sample requests (templates only, no network) ===\n")
    for key in (
        "signin0",
        "sms_send",
        "smsVerify0",
        "SigninOneKeyLogin1",
        "testField",
        "SvipTry",
        "getUserAttributes",
        "buyCoinWechatXBXX",
        "withdraw",
        "tximsign",
        "agora_rtc",
    ):
        print_request_template(key, uid=uid, author_token="<from_login>")


def main() -> None:
    parser = argparse.ArgumentParser(description="beibeiwu client crypto helpers")
    parser.add_argument("cmd", nargs="?", default="demo", choices=["demo", "sign", "usersig", "url", "list"])
    parser.add_argument("--uid", default="0")
    parser.add_argument("--token", default="0", help="AUTHOR-TOKEN")
    parser.add_argument("--action", default="testField")
    parser.add_argument("--body", default="", help="k=v&k2=v2")
    args = parser.parse_args()

    if args.cmd == "demo":
        demo()
        return
    if args.cmd == "list":
        for k, v in ENDPOINTS.items():
            print(f"{k:24s} {v['desc']:24s} {v['url']}")
        return
    if args.cmd == "sign":
        print(json.dumps(build_auth_headers(args.uid, args.token), indent=2, ensure_ascii=False))
        return
    if args.cmd == "usersig":
        print(gen_tls_user_sig(args.uid))
        return
    if args.cmd == "url":
        extra = {}
        if args.body:
            for pair in args.body.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    extra[k] = v
        print_request_template(args.action, uid=args.uid, author_token=args.token, extra_body=extra)
        return


if __name__ == "__main__":
    main()
