#!/usr/bin/env python3
"""Probe guest/login-tier API capability matrix for beibeiwu CTF."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

PHONE = os.getenv("BBW_TEST_PHONE", "").strip()
PASSWORD = os.getenv("BBW_TEST_PASSWORD", "")
UID_HINT = os.getenv("BBW_TEST_UID", "").strip()
APPLET = "https://applet.banghua.xin/app/index.php"
OUT = r"D:\project\AI\bbw\analysis\guest_capability_results.json"


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
        "SIGN-TOKEN": md5_hex((uid or "0") + "socialchat" + (uid or "0")),
        "User-Agent": "okhttp/4.9.3 beibeiwu/154",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def post(
    url: str, body: Optional[Dict[str, str]], uid: str, token: str
) -> Tuple[int, str]:
    data = urllib.parse.urlencode(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers(uid, token), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, f"EXC:{e}"


def action_url(action: str, i: str = "999999", m: str = "socialchat") -> str:
    if action.startswith("http"):
        return action
    return f"{APPLET}?i={i}&c=entry&a=webapp&do={action}&m={m}"


def classify(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "empty_body"
    if s.startswith("EXC:"):
        return "network_error"
    try:
        j = json.loads(s)
        code = str(j.get("code", j.get("error", "")))
        msg = str(j.get("message", j.get("info", "")))
        if code in ("200", "0") or j.get("error") == "0":
            return f"ok:{msg[:40]}"
        if code == "403":
            return f"deny_403:{msg[:60]}"
        if code == "400":
            return f"deny_400:{msg[:60]}"
        if code == "700":
            return f"logout_700:{msg[:60]}"
        if code == "300":
            return f"relogin_300:{msg[:60]}"
        return f"code_{code}:{msg[:60]}"
    except Exception:
        if "不足" in s or "未" in s or "请" in s or "失败" in s or "错误" in s:
            return f"text_deny:{s[:60]}"
        if "成功" in s:
            return f"text_ok:{s[:60]}"
        return f"text:{s[:60]}"


@dataclass
class Probe:
    tier: str
    name: str
    action: str
    body: Dict[str, str]
    status: int = 0
    cls: str = ""
    raw_head: str = ""


def login() -> Tuple[str, str, dict]:
    st, raw = post(
        action_url("signin0"),
        {
            "userAccount": PHONE,
            "userPassword": PASSWORD,
            "uniquelogintoken": sha1_hex("xiaobei" + UID_HINT),
            "phonebrand": "Android",
            "pushregid": "guest_probe",
            "version_code": "154",
        },
        "0",
        "0",
    )
    j = json.loads(raw)
    info = json.loads(j["json"])
    return info["id"], info["token"], info


def run_suite(tier: str, uid: str, token: str, sample_other_uid: str = "1") -> List[Probe]:
    # Representative features across product areas
    cases = [
        ("版本/配置", "getVersion1", {}),
        ("广告开关", "isShowAD1", {}),
        ("礼物列表", "getGiftList", {}),
        ("轮播", "getSlide", {"slidesort": "1"}),
        ("推荐页", "Tuijiannew", {"type": "getSlide"}),
        ("用户资料(自己)", "getUserAttributes", {"userId": uid or "0", "latitude": "0", "longitude": "0"}),
        ("用户资料(他人)", "getUserAttributes0", {"userId": sample_other_uid, "latitude": "0", "longitude": "0"}),
        ("自己资料Me", "getUserAttributesMe1", {"userId": uid or "0", "phonebrand": "Android", "pushregid": "p", "version_code": "154"}),
        ("关注列表", "getFollowList", {"id": uid or "0"}),
        ("粉丝列表", "getFansUser", {"id": uid or "0", "pageindex": "1"}),
        ("发帖相关-filter", "getFilterWords", {}),
        ("敏感词", "getIllegalWord", {}),
        ("聊天审查配置", "getChatCensorship", {}),
        ("在线心跳", "UpdateOnline0", {"id": uid or "0", "sign": md5_hex((uid or "0") + "socialchat" + (uid or "0")), "version_code": "154"}),
        ("改昵称resetNew", "resetNew", {"sign": md5_hex((uid or "0") + "socialchat" + (uid or "0")), "type": "昵称设置", "userId": uid or "0", "value": "Vom"}),
        ("改名次数", "resetNum", {"uid": uid or "0", "type": "昵称"}),
        ("SVIP试用", "SvipTry", {"id": uid or "0"}),
        ("余额兑VIP", "moneyExchangeVip", {"uid": uid or "0", "vip_id": "5", "coupon_id": "0", "token": sha1_hex("xiaobei" + (uid or "0"))}),
        ("提现", "withdraw", {"authid": uid or "0", "alilogonid": "a@b.com", "aliname": "t", "transamount": "1"}),
        ("送礼", "sendGift1", {"userId": uid or "0", "giftId": "1", "num": "1", "receiverId": sample_other_uid}),
        ("创建房间", "createRoom0", {"myID": uid or "0", "audioroomtype": "处CP"}),
        ("房间权限", "getRoomAuth", {"uid": uid or "0"}),
        ("匹配移除", "https://redis.banghua.xin/app/index.php?i=888&c=entry&a=webapp&do=removeXiaobeiMatch&m=rediscache", {"id": uid or "0", "type": "1"}),
        ("testField", "testField", {}),
        ("话题列表", "getTopic", {"topic": "test"}),
        ("推荐码", "getReferral", {}),
        ("关注", "follow", {"me": uid or "0", "you": sample_other_uid, "quietly_follow": "1"}),
        ("拉黑列表", "getMyBlackList", {}),
        ("礼仪分", "getEtiquetteScoreAndDescription", {}),
    ]
    out: List[Probe] = []
    for name, action, body in cases:
        # resetNew in app is multipart; form may still show server auth decision
        url = action_url(action) if not action.startswith("http") else action
        # special: redis url already full
        if action.startswith("http"):
            url = action
        st, raw = post(url, body, uid, token)
        p = Probe(
            tier=tier,
            name=name,
            action=action if len(action) < 80 else action.split("do=")[-1][:80],
            body=body,
            status=st,
            cls=classify(raw),
            raw_head=raw[:300].replace("\n", " "),
        )
        out.append(p)
        print(f"[{tier:8s}] {name:16s} -> {p.cls}")
    return out


def main() -> None:
    if not PHONE or not PASSWORD or not UID_HINT:
        raise SystemExit(
            "set BBW_TEST_PHONE, BBW_TEST_PASSWORD and BBW_TEST_UID before running this live probe"
        )
    results: Dict[str, Any] = {"tiers": {}, "account": {}}

    # Tier A: not logged in
    print("\n==== TIER anon (uid=0 token=0) ====")
    anon = run_suite("anon", "0", "0")
    results["tiers"]["anon"] = [asdict(x) for x in anon]

    # Tier B: logged-in guest profile account
    print("\n==== TIER guest_login (signin0) ====")
    uid, token, info = login()
    results["account"] = {
        "uid": uid,
        "nickname": info.get("nickname"),
        "user_role": info.get("user_role"),
        "rp_verify_time": info.get("rp_verify_time"),
        "phone": info.get("phone"),
        "vip": info.get("vip"),
        "svip": info.get("svip"),
        "money": info.get("money"),
        "name_card": info.get("name_card"),
        "match_card": info.get("match_card"),
        "advanced_user": info.get("advanced_user"),
        "token_prefix": (token or "")[:20],
    }
    print("account", results["account"])
    guest = run_suite("guest_login", uid, token)
    results["tiers"]["guest_login"] = [asdict(x) for x in guest]

    # summary matrix
    print("\n==== MATRIX ====")
    names = [p.name for p in anon]
    for name in names:
        a = next(x for x in anon if x.name == name)
        g = next(x for x in guest if x.name == name)
        print(f"{name:16s} | anon={a.cls:28s} | guest={g.cls}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("saved", OUT)


if __name__ == "__main__":
    main()
