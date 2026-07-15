"""Client-side signing algorithms mirrored from the APK."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
import zlib
from typing import Dict, Optional

TXIM_SDKAPPID = 1600039823
TXIM_SECRETKEY = (
    "c064eea5978cf60af28dcbbe9dd7c35e110734fcf1a5662ead3e87e2eb8a554e"
)
SALT_SIGN = "socialchat"
SALT_EXPIRE = "xiaobei"
PACKAGE_NAME = "xin.banghua.beiyuan0"
# xbly.apk client build (About_app&version=154); was 148 on beibeiwu.apk
VERSION_CODE = "154"


def md5_hex(s: str) -> str:
    raw = bytes(ord(c) & 0xFF for c in s)
    return hashlib.md5(raw).hexdigest()


def sha1_utf8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sha1_iso(s: str) -> str:
    return hashlib.sha1(s.encode("iso-8859-1")).hexdigest()


def sign_token(uid: str = "0") -> str:
    uid = uid or "0"
    return md5_hex(f"{uid}{SALT_SIGN}{uid}")


def expire_token(ts: Optional[int] = None) -> str:
    if ts is None:
        ts = int(time.time())
    t = str(ts)
    return sha1_utf8(SALT_EXPIRE + t) + t


def unique_login_token(uid: str = "0") -> str:
    return sha1_utf8(SALT_EXPIRE + (uid or "0"))


def author_signature(
    uid: str, nonce: Optional[str] = None, ts_ms: Optional[str] = None
) -> Dict[str, str]:
    if nonce is None:
        nonce = str(random.randint(0, 9999))
    if ts_ms is None:
        ts_ms = str(int(time.time() * 1000))
    uid = uid or "0"
    sig = sha1_iso(sha1_iso(uid) + nonce + ts_ms)
    return {
        "AUTHOR-UID": uid,
        "AUTHOR-NONCE": nonce,
        "AUTHOR-TIMESTAMP": ts_ms,
        "AUTHOR-SIGNATURE": sig or "",
    }


def auth_headers(
    uid: str = "0",
    author_token: str = "0",
    with_author_sig: bool = False,
) -> Dict[str, str]:
    h = {
        "AUTHOR-TOKEN": author_token or "0",
        "EXPIRE-TOKEN": expire_token(),
        "SIGN-TOKEN": sign_token(uid or "0"),
        "User-Agent": f"okhttp/4.9.3 beibeiwu/{VERSION_CODE}",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if with_author_sig:
        h.update(author_signature(uid or "0"))
    return h


def _b64url_tx(data: bytes) -> str:
    s = base64.b64encode(data).decode("ascii")
    return s.replace("+", "*").replace("/", "-").replace("=", "_")


def gen_user_sig(
    identifier: str,
    sdkappid: int = TXIM_SDKAPPID,
    secret_key: str = TXIM_SECRETKEY,
    expire: int = 604800,
) -> str:
    now = int(time.time())
    content = (
        f"TLS.identifier:{identifier}\n"
        f"TLS.sdkappid:{sdkappid}\n"
        f"TLS.time:{now}\n"
        f"TLS.expire:{expire}\n"
    )
    sig = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            content.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    payload = {
        "TLS.ver": "2.0",
        "TLS.identifier": identifier,
        "TLS.sdkappid": sdkappid,
        "TLS.expire": expire,
        "TLS.time": now,
        "TLS.sig": sig,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return _b64url_tx(zlib.compress(raw))
