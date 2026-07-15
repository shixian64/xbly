#!/usr/bin/env python3
"""Scan decompiled sources for guest/login/rp/vip gate patterns."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOTS = [
    Path(r"D:\project\AI\bbw\jadx_out\sources\xin\banghua\beiyuan0"),
    Path(r"D:\project\AI\bbw\jadx_out\sources\cn\leyuan\base_library"),
]

MSG_KEYS = (
    "登录",
    "实名",
    "绑定",
    "手机",
    "VIP",
    "会员",
    "SVIP",
    "游客",
    "认证",
    "余额",
    "金币",
    "完善",
    "资料",
    "卡不足",
    "相册",
    "等级",
)

COND_PATTERNS = [
    (r'getRp_verify_time\(\)\.equals\("0"\)', "rp_verify_time==0"),
    (r"TextUtils\.isEmpty\([^\n]*getPhone\(\)\)", "phone empty"),
    (r'getId\(\)\.equals\("0"\)', "uid==0"),
    (r"isSignIn\(", "isSignIn()"),
    (r"isVip\(|isSVip\(", "vip/svip check"),
    (r"getName_card\(\)|改名卡", "name_card"),
    (r"getMatch_card\(\)|匹配卡", "match_card"),
    (r"getMoney\(\)|余额不足", "money"),
]


def main() -> None:
    msgs: dict[str, set[str]] = defaultdict(set)
    cond_hits: dict[str, set[str]] = defaultdict(set)
    string_lits = re.compile(r'"([^"\\]{4,60})"')

    for root in ROOTS:
        for p in root.rglob("*.java"):
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            name = p.name
            for lit in string_lits.findall(t):
                if any(k in lit for k in MSG_KEYS):
                    if any(
                        x in lit
                        for x in (
                            "请",
                            "还",
                            "未",
                            "先",
                            "不足",
                            "游客",
                            "登录",
                            "实名",
                            "绑定",
                        )
                    ):
                        msgs[lit].add(name)
            for pat, label in COND_PATTERNS:
                if re.search(pat, t):
                    cond_hits[label].add(name)

    print("=== CONDITION HITS (file counts) ===")
    for label, files in sorted(cond_hits.items(), key=lambda x: -len(x[1])):
        print(f"{label:20s} {len(files):3d}  e.g. {sorted(files)[:5]}")

    print("\n=== GATE MESSAGES ===")
    for s, files in sorted(msgs.items(), key=lambda x: (-len(x[1]), x[0]))[:150]:
        print(f"[{len(files):2d}] {s}")
        print(f"     {', '.join(sorted(files)[:6])}")


if __name__ == "__main__":
    main()
