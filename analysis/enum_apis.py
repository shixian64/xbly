import re
from pathlib import Path
root = Path(r'D:\project\AI\bbw\jadx_out\sources')
dos = set()
short = set()
urls = set()
for p in root.rglob('*.java'):
    try:
        t = p.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        continue
    for m in re.finditer(r'do=([A-Za-z0-9_]+)', t):
        dos.add(m.group(1))
    for m in re.finditer(r'startHttp\([^,]+,\s*"([^"]+)"', t):
        s = m.group(1)
        if s.startswith('http'):
            urls.add(s)
        else:
            short.add(s)
print('=== do= count', len(dos))
interesting = [x for x in sorted(dos) if re.search(r'(login|sign|sms|token|admin|vip|coin|pay|withdraw|test|debug|auth|password|register|verify|unique|gift|room)', x, re.I)]
print('interesting do=', len(interesting))
for x in interesting:
    print(x)
print('\n=== short startHttp', len(short))
for x in sorted(short):
    print(x)
print('\n=== abs urls', len(urls))
for x in sorted(urls):
    if any(k in x.lower() for k in ['login','sign','sms','token','pay','agora','rong','txim','alipay','wechat','test','admin']):
        print(x)
