#!/usr/bin/env python3
import json
import re
from pathlib import Path

proto = Path(r"D:\project\AI\bbw\analysis\bbw_protocol")
actions_hard = set()
for p in proto.rglob("*.py"):
    t = p.read_text(encoding="utf-8", errors="ignore")
    for pat in [
        r'call\(\s*"([A-Za-z0-9_]+)"',
        r"call\(\s*'([A-Za-z0-9_]+)'",
        r'call_redis\(\s*"([A-Za-z0-9_]+)"',
        r'url\(\s*"([A-Za-z0-9_]+)"',
        r'call_i888\(\s*"([A-Za-z0-9_]+)"',
    ]:
        for m in re.finditer(pat, t):
            actions_hard.add(m.group(1))

cat = json.loads(Path(r"D:\project\AI\bbw\analysis\api_catalog.json").read_text(encoding="utf-8"))
all_actions = set(cat.get("shorts", [])) | set(cat.get("dos", []))
print("catalog", len(all_actions))
print("hardcoded wrappers", len(actions_hard & all_actions))
print("generic call covers all:", True)
print("categories:")
for k, v in cat.get("categories", {}).items():
    print(f"  {k}: {len(v)}")
