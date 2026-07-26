"""字段级信封加密与确定性查找辅助函数。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings


class EncryptionError(ValueError):
    """密文损坏、上下文错误或密钥不可用。"""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str, *, field: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise EncryptionError(f"invalid base64 in {field}") from exc


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    v: int
    alg: str
    key_version: int
    purpose: str
    context_sha256: str
    wrapped_key_nonce: str
    wrapped_key: str
    data_nonce: str
    ciphertext: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EncryptedPayload":
        try:
            return cls(
                v=int(value["v"]),
                alg=str(value["alg"]),
                key_version=int(value["key_version"]),
                purpose=str(value["purpose"]),
                context_sha256=str(value["context_sha256"]),
                wrapped_key_nonce=str(value["wrapped_key_nonce"]),
                wrapped_key=str(value["wrapped_key"]),
                data_nonce=str(value["data_nonce"]),
                ciphertext=str(value["ciphertext"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EncryptionError("invalid encrypted payload") from exc


class CredentialCipher:
    """AES-256-GCM 信封加密。

    每次加密生成独立 256 位数据密钥（DEK），再由当前版本主密钥（KEK）
    加密 DEK。``purpose`` 与调用方提供的记录 ``context`` 都进入 AAD，防止
    把某一账号的密码密文复制到另一账号或字段后仍能解密。
    """

    FORMAT_VERSION = 1
    ALGORITHM = "AES-256-GCM+ENVELOPE"

    def __init__(self, keyring: Mapping[int, bytes], current_version: int):
        normalized: dict[int, bytes] = {}
        for version, key in keyring.items():
            if int(version) < 1 or len(key) != 32:
                raise EncryptionError("every KEK must be 32 bytes and have a positive version")
            normalized[int(version)] = bytes(key)
        if current_version not in normalized:
            raise EncryptionError(f"active key version {current_version} is unavailable")
        self._keyring = normalized
        self.current_version = current_version

    @classmethod
    def from_settings(cls, settings: Settings) -> "CredentialCipher":
        return cls(settings.load_credential_keyring(), settings.credential_key_version)

    @staticmethod
    def _context_digest(context: str | bytes) -> str:
        raw = context.encode("utf-8") if isinstance(context, str) else context
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _aad(cls, *, purpose: str, context_digest: str, key_version: int, layer: str) -> bytes:
        return (
            f"bbw|envelope-v{cls.FORMAT_VERSION}|{layer}|{key_version}|{purpose}|{context_digest}"
        ).encode("utf-8")

    def encrypt_bytes(
        self,
        plaintext: bytes,
        *,
        purpose: str,
        context: str | bytes,
        key_version: int | None = None,
    ) -> dict[str, Any]:
        if not purpose or len(purpose) > 128:
            raise EncryptionError("purpose must contain 1 to 128 characters")
        version = key_version or self.current_version
        try:
            kek = self._keyring[version]
        except KeyError as exc:
            raise EncryptionError(f"key version {version} is unavailable") from exc

        context_digest = self._context_digest(context)
        dek = os.urandom(32)
        data_nonce = os.urandom(12)
        wrap_nonce = os.urandom(12)
        data_aad = self._aad(
            purpose=purpose, context_digest=context_digest, key_version=version, layer="data"
        )
        wrap_aad = self._aad(
            purpose=purpose, context_digest=context_digest, key_version=version, layer="dek"
        )
        ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, data_aad)
        wrapped_key = AESGCM(kek).encrypt(wrap_nonce, dek, wrap_aad)
        return EncryptedPayload(
            v=self.FORMAT_VERSION,
            alg=self.ALGORITHM,
            key_version=version,
            purpose=purpose,
            context_sha256=context_digest,
            wrapped_key_nonce=_b64encode(wrap_nonce),
            wrapped_key=_b64encode(wrapped_key),
            data_nonce=_b64encode(data_nonce),
            ciphertext=_b64encode(ciphertext),
        ).to_dict()

    def decrypt_bytes(
        self,
        payload: Mapping[str, Any],
        *,
        purpose: str,
        context: str | bytes,
    ) -> bytes:
        item = EncryptedPayload.from_mapping(payload)
        if item.v != self.FORMAT_VERSION or item.alg != self.ALGORITHM:
            raise EncryptionError("unsupported encrypted payload format")
        if not hmac.compare_digest(item.purpose, purpose):
            raise EncryptionError("encrypted payload purpose mismatch")
        expected_context = self._context_digest(context)
        if not hmac.compare_digest(item.context_sha256, expected_context):
            raise EncryptionError("encrypted payload context mismatch")
        try:
            kek = self._keyring[item.key_version]
        except KeyError as exc:
            raise EncryptionError(f"key version {item.key_version} is unavailable") from exc

        wrap_aad = self._aad(
            purpose=purpose,
            context_digest=expected_context,
            key_version=item.key_version,
            layer="dek",
        )
        data_aad = self._aad(
            purpose=purpose,
            context_digest=expected_context,
            key_version=item.key_version,
            layer="data",
        )
        try:
            dek = AESGCM(kek).decrypt(
                _b64decode(item.wrapped_key_nonce, field="wrapped_key_nonce"),
                _b64decode(item.wrapped_key, field="wrapped_key"),
                wrap_aad,
            )
            if len(dek) != 32:
                raise EncryptionError("decrypted DEK has an invalid length")
            return AESGCM(dek).decrypt(
                _b64decode(item.data_nonce, field="data_nonce"),
                _b64decode(item.ciphertext, field="ciphertext"),
                data_aad,
            )
        except InvalidTag as exc:
            raise EncryptionError("encrypted payload authentication failed") from exc

    def encrypt_text(self, value: str, *, purpose: str, context: str | bytes) -> dict[str, Any]:
        return self.encrypt_bytes(value.encode("utf-8"), purpose=purpose, context=context)

    def decrypt_text(self, payload: Mapping[str, Any], *, purpose: str, context: str | bytes) -> str:
        try:
            return self.decrypt_bytes(payload, purpose=purpose, context=context).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EncryptionError("decrypted value is not UTF-8 text") from exc

    def encrypt_json(self, value: Any, *, purpose: str, context: str | bytes) -> dict[str, Any]:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.encrypt_bytes(encoded, purpose=purpose, context=context)

    def decrypt_json(self, payload: Mapping[str, Any], *, purpose: str, context: str | bytes) -> Any:
        try:
            return json.loads(self.decrypt_bytes(payload, purpose=purpose, context=context))
        except json.JSONDecodeError as exc:
            raise EncryptionError("decrypted value is not valid JSON") from exc

    def reencrypt(
        self, payload: Mapping[str, Any], *, purpose: str, context: str | bytes
    ) -> dict[str, Any]:
        plaintext = self.decrypt_bytes(payload, purpose=purpose, context=context)
        return self.encrypt_bytes(plaintext, purpose=purpose, context=context)


_PHONE_SEPARATOR_RE = re.compile(r"[\s\-().]")


def normalize_phone(phone: str) -> str:
    normalized = unicodedata.normalize("NFKC", phone).strip()
    normalized = _PHONE_SEPARATOR_RE.sub("", normalized)
    if normalized.startswith("00"):
        normalized = "+" + normalized[2:]
    if normalized.startswith("+"):
        digits = normalized[1:]
    else:
        digits = normalized
    if not digits.isdigit() or not 6 <= len(digits) <= 20:
        raise ValueError("invalid phone number")
    return ("+" if normalized.startswith("+") else "") + digits


def phone_lookup_hmac(phone: str, key: bytes) -> str:
    if len(key) < 32:
        raise ValueError("phone lookup HMAC key must contain at least 32 bytes")
    normalized = normalize_phone(phone).encode("utf-8")
    return hmac.new(key, b"bbw:phone:v1:" + normalized, hashlib.sha256).hexdigest()


SENSITIVE_RAW_KEYS = frozenset(
    {
        "password",
        "userpassword",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "confirmation_token",
        "confirmationtoken",
        "usersig",
        "phone",
        "phonenumber",
        "mobile",
        "useraccount",
        "loginaccount",
        "id_card",
        "idcard",
        "certno",
        "certname",
        "identity_number",
        "identitynumber",
    }
)

# Upstream payloads are inconsistent about separators and casing (for example
# ``access_token``, ``accessToken`` and ``Access-Token``).  Compare only the
# normalized form; comparing a normalized key against the un-normalized set
# would leave common camelCase credentials in the encrypted diagnostic copy.
SENSITIVE_RAW_KEYS_NORMALIZED = frozenset(
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in SENSITIVE_RAW_KEYS
) | frozenset(
    {
        "apikey",
        "authortoken",
        "businesstoken",
        "clientsecret",
        "confirmationtoken",
        "cookie",
        "expiretoken",
        "loginsecret",
        "secret",
        "secretkey",
        "sessionid",
        "sessionkey",
        "sessiontoken",
        "setcookie",
        "signtoken",
        "ticket",
    }
)
SENSITIVE_RAW_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "businesstoken",
    "cookie",
    "confirmationtoken",
    "objectkey",
    "passwd",
    "password",
    "phonehmac",
    "privatekey",
    "secret",
    "sessiontoken",
    "sidhash",
    "token",
    "usersig",
)
_RAW_URL_KEY_FRAGMENTS = ("url", "uri", "link", "href")
_BEARER_VALUE_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_VALUE_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_RAW_MAX_NODES = 20_000
_RAW_MAX_CHARS = 2_000_000


def redact_raw_payload(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
    key_hint: str = "",
) -> Any:
    """在加密原始响应前移除不应进入排障副本的凭据/实名号码。"""

    if budget is None:
        budget = [_RAW_MAX_NODES, _RAW_MAX_CHARS]
    if depth >= 20 or budget[0] <= 0 or budget[1] <= 0:
        return "[TRUNCATED]"
    budget[0] -= 1
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:1000]:
            raw_key_text = str(key)
            key_text = raw_key_text[:160]
            normalized_key = re.sub(r"[^a-z0-9]", "", raw_key_text.casefold())
            if normalized_key in SENSITIVE_RAW_KEYS_NORMALIZED or any(
                fragment in normalized_key for fragment in SENSITIVE_RAW_KEY_FRAGMENTS
            ):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_raw_payload(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    key_hint=normalized_key,
                )
        return result
    if isinstance(value, list):
        return [
            redact_raw_payload(item, depth=depth + 1, budget=budget, key_hint=key_hint)
            for item in value[:1000]
        ]
    if isinstance(value, tuple):
        return [
            redact_raw_payload(item, depth=depth + 1, budget=budget, key_hint=key_hint)
            for item in value[:1000]
        ]
    if isinstance(value, str):
        text = value
        if any(
            fragment in key_hint for fragment in _RAW_URL_KEY_FRAGMENTS
        ) or text.lstrip().lower().startswith(("https://", "http://")):
            text = text.split("?", 1)[0].split("#", 1)[0]
        text = _BEARER_VALUE_RE.sub("Bearer [REDACTED]", text)
        text = _JWT_VALUE_RE.sub("[REDACTED_JWT]", text)
        stripped = text.strip()
        if depth < 19 and len(stripped) <= 256_000 and stripped[:1] in {"{", "["}:
            try:
                nested = json.loads(stripped)
            except (TypeError, ValueError):
                nested = None
            if isinstance(nested, (dict, list)):
                sanitized_nested = redact_raw_payload(
                    nested,
                    depth=depth + 1,
                    budget=budget,
                    key_hint=key_hint,
                )
                text = json.dumps(
                    sanitized_nested,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        available = max(0, min(200_000, budget[1]))
        result = text[:available]
        budget[1] -= len(result)
        return result if len(text) <= available else result + "[TRUNCATED]"
    return value
