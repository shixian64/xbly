"""Device profile helpers for protocol fidelity (APK-like form fields).

Pure protocol utility — no web / multi-tenant logic.
"""

from __future__ import annotations

import hashlib
import random
import secrets
import time
from typing import Dict, Optional

from . import sign

BRAND_POOL = [
    "HUAWEI ELS-AN00",
    "HUAWEI NOH-AN00",
    "Xiaomi 2201123C",
    "Xiaomi 2211133C",
    "OPPO PGBM10",
    "vivo V2183A",
    "samsung SM-S9110",
    "OnePlus LE2120",
    "realme RMX3562",
    "Redmi 22041216C",
    "HONOR ANY-AN00",
    "Meizu 20",
]


def _stable_hex(seed: str, n: int = 32) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:n]


def build_device_profile(
    *,
    seed: Optional[str] = None,
    phonebrand: Optional[str] = None,
    pushregid: Optional[str] = None,
    device_id: Optional[str] = None,
    version_code: str = sign.VERSION_CODE,
    package_name: str = sign.PACKAGE_NAME,
) -> Dict[str, str]:
    """Build device form fields. Same seed → stable brand/ids."""
    s = seed or secrets.token_hex(8)
    if phonebrand is None:
        idx = int(_stable_hex(f"brand:{s}", 8), 16) % len(BRAND_POOL)
        phonebrand = BRAND_POOL[idx]
    if pushregid is None:
        pushregid = _stable_hex(f"push:{s}", 64) if seed else secrets.token_hex(32)
    if device_id is None:
        device_id = _stable_hex(f"dev:{s}", 16) if seed else secrets.token_hex(8)
    return {
        "phonebrand": phonebrand,
        "pushregid": pushregid,
        "device_id": device_id,
        "version_code": version_code,
        "package_name": package_name,
        "user_agent": f"okhttp/4.9.3 beibeiwu/{version_code}",
        "created_at": str(int(time.time())),
    }
