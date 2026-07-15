#!/usr/bin/env python3
"""Enumerate all API actions from decompiled sources."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

ROOTS = [
    Path(r"D:\project\AI\bbw\jadx_out\sources\xin\banghua\beiyuan0"),
    Path(r"D:\project\AI\bbw\jadx_out\sources\cn\leyuan"),
]

OUT = Path(r"D:\project\AI\bbw\analysis\api_catalog.json")


def main() -> None:
    dos: set[str] = set()
    shorts: set[str] = set()
    full_urls: set[str] = set()
    method_map: dict[str, list[str]] = defaultdict(list)

    # startHttp(map, "action")
    re_start = re.compile(r'startHttp\([^,]+,\s*"([^"]+)"')
    re_do = re.compile(r"do=([A-Za-z0-9_]+)")
    re_url = re.compile(
        r'https://(?:applet|redis)\.banghua\.xin[^"\s\\]+'
    )
    # public static void foo(
    re_public = re.compile(
        r"public static void (\w+)\([^)]*\)\s*\{[^}]{0,200}?do=([A-Za-z0-9_]+)",
        re.S,
    )

    for root in ROOTS:
        for p in root.rglob("*.java"):
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in re_start.finditer(t):
                s = m.group(1)
                if s.startswith("http"):
                    full_urls.add(s)
                else:
                    shorts.add(s)
            for m in re_do.finditer(t):
                dos.add(m.group(1))
            for m in re_url.finditer(t):
                full_urls.add(m.group(0).rstrip("\\"))
            for m in re_public.finditer(t):
                method_map[m.group(2)].append(m.group(1))

    # categorize
    cats = defaultdict(list)
    rules = [
        ("auth", r"login|sign|sms|password|register|token|unique|verify|cert|RP|auth|logout|onekey"),
        ("profile", r"user|nick|reset|portrait|album|tag|mbti|attribute|personal|info|setting|privacy|referral"),
        ("social", r"follow|fan|friend|black|like|comment|post|luntan|dongtai|share|report|message|conversation"),
        ("match", r"match|bottle|draft|tantan|card|cp|dating|playing"),
        ("room", r"room|agora|song|ktv|gift|barrage|channel|audio|anchor"),
        ("economy", r"pay|coin|vip|svip|money|withdraw|alipay|wechat|order|buy|exchange|wallet|coupon"),
        ("content", r"slide|topic|filter|illegal|censor|version|ad|giftlist|tuijian|film|novel"),
        ("im", r"tencent|rong|im|flash|sticker"),
    ]
    all_actions = sorted(set(list(dos) + list(shorts)))
    for a in all_actions:
        placed = False
        for cat, pat in rules:
            if re.search(pat, a, re.I):
                cats[cat].append(a)
                placed = True
                break
        if not placed:
            cats["other"].append(a)

    catalog = {
        "counts": {
            "do": len(dos),
            "short_startHttp": len(shorts),
            "full_urls": len(full_urls),
            "all_actions": len(all_actions),
        },
        "categories": {k: sorted(v) for k, v in cats.items()},
        "dos": sorted(dos),
        "shorts": sorted(shorts),
        "full_urls": sorted(full_urls),
        "method_hints": {k: sorted(set(v)) for k, v in sorted(method_map.items())},
    }
    OUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(catalog["counts"], ensure_ascii=False, indent=2))
    for k, v in catalog["categories"].items():
        print(f"{k}: {len(v)}")


if __name__ == "__main__":
    main()
