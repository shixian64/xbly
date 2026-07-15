import re
from pathlib import Path

files = [
    Path(r"D:\project\AI\bbw\jadx_out\sources\cn\leyuan\base_library\utils\OkHttpInstance.java"),
    Path(r"D:\project\AI\bbw\jadx_out\sources\xin\banghua\beiyuan0"),
]
short = set()
dos = set()
for base in files:
    paths = [base] if base.is_file() else list(base.rglob("*.java"))
    for p in paths:
        t = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'startHttp\([^,]+,\s*"([^"]+)"', t):
            short.add(m.group(1))
        for m in re.finditer(r"do=([A-Za-z0-9_]+)", t):
            dos.add(m.group(1))

print("=== short startHttp (non-http) ===")
for x in sorted(s for s in short if not s.startswith("http")):
    print(x)
print("\n=== short startHttp (http special) ===")
for x in sorted(s for s in short if s.startswith("http")):
    print(x)
print("\n=== high value do= ===")
keys = re.compile(
    r"login|sign|sms|token|vip|coin|pay|withdraw|test|admin|auth|password|"
    r"register|verify|unique|gift|room|black|follow|chat|money|exchange|sendVip|order",
    re.I,
)
for x in sorted(dos):
    if keys.search(x):
        print(x)
print("counts", len(short), len(dos))
