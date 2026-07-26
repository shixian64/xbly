"""密码、会话、邀请码与 TOTP 安全原语。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from argon2 import PasswordHasher as Argon2PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


class AuthenticationError(ValueError):
    pass


class PasswordHasher:
    """管理员密码 Argon2id；参数兼顾 2GB 测试服务器与离线破解成本。"""

    _dummy_hash: str | None = None
    _dummy_lock = threading.Lock()

    def __init__(self) -> None:
        self._hasher = Argon2PasswordHasher(
            time_cost=3,
            memory_cost=64 * 1024,
            parallelism=2,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    @staticmethod
    def validate_new_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("administrator password must contain at least 12 characters")
        if len(password.encode("utf-8")) > 1024:
            raise ValueError("administrator password is too long")

    def hash(self, password: str) -> str:
        self.validate_new_password(password)
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def verify_or_dummy(self, encoded_hash: str | None, password: str) -> bool:
        """Equalize unknown/disabled-account login work with a real verify."""

        if encoded_hash:
            return self.verify(encoded_hash, password)
        if self.__class__._dummy_hash is None:
            with self.__class__._dummy_lock:
                if self.__class__._dummy_hash is None:
                    self.__class__._dummy_hash = self._hasher.hash(
                        "timing-only-dummy-password-not-valid-for-login"
                    )
        dummy_hash = self.__class__._dummy_hash
        assert dummy_hash is not None
        self.verify(dummy_hash, password)
        return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except (InvalidHashError, VerificationError):
            return True


_USER_PASSWORD_HASH_MAX_ENCODED_LENGTH = 512
_USER_PASSWORD_TIME_COST_RANGE = range(1, 11)
_USER_PASSWORD_MEMORY_COST_RANGE = range(8 * 1024, 256 * 1024 + 1)
_USER_PASSWORD_PARALLELISM_RANGE = range(1, 9)
_USER_PASSWORD_SALT_LENGTH_RANGE = range(8, 65)
_USER_PASSWORD_HASH_LENGTH_RANGE = range(16, 65)


def is_safe_user_password_hash(encoded_hash: object) -> bool:
    """判断用户密码摘要能否在受控资源范围内安全验证。

    Argon2 会采用摘要自身携带的参数进行验证，因此不能把数据库中的参数
    不加限制地交给底层实现。此函数只做纯解析与边界检查，不执行哈希运算。
    """

    if (
        not isinstance(encoded_hash, str)
        or not encoded_hash
        or len(encoded_hash) > _USER_PASSWORD_HASH_MAX_ENCODED_LENGTH
        or not encoded_hash.isascii()
    ):
        return False
    try:
        parameters = extract_parameters(encoded_hash)
    except (InvalidHashError, TypeError, ValueError):
        return False
    return bool(
        parameters.type is Type.ID
        and parameters.version == 19
        and parameters.time_cost in _USER_PASSWORD_TIME_COST_RANGE
        and parameters.memory_cost in _USER_PASSWORD_MEMORY_COST_RANGE
        and parameters.parallelism in _USER_PASSWORD_PARALLELISM_RANGE
        and parameters.salt_len in _USER_PASSWORD_SALT_LENGTH_RANGE
        and parameters.hash_len in _USER_PASSWORD_HASH_LENGTH_RANGE
    )


_USER_PASSWORD_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=2$hgSFYbeBDzhtzVj32n3c+w$"
    "CF/Zm+UUweR/dpAqTcnhqiaeZrOI2lyMu7IlZwSWNv4"
)
if not is_safe_user_password_hash(_USER_PASSWORD_DUMMY_HASH):  # pragma: no cover
    raise RuntimeError("unsafe built-in user password dummy hash")


class UserPasswordHasher:
    """已由上游确认的用户密码所使用的本地 Argon2id 验证器。

    这里不能套用管理员密码的最小长度规则：迁移期间必须兼容上游已经接受的
    历史密码。密码原文仍然不做规范化，避免改变 APK 账号的认证语义。
    """

    def __init__(self) -> None:
        self._hasher = Argon2PasswordHasher(
            time_cost=3,
            memory_cost=64 * 1024,
            parallelism=2,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    @staticmethod
    def validate_verified_password(password: str) -> None:
        if not password:
            raise ValueError("user password must not be empty")

    def hash(self, password: str) -> str:
        self.validate_verified_password(password)
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        if not is_safe_user_password_hash(encoded_hash):
            return False
        try:
            return self._hasher.verify(encoded_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def verify_or_dummy(self, encoded_hash: str | None, password: str) -> bool:
        """未知账号和未迁移账号也执行一次 Argon2，降低枚举侧信道。"""

        if encoded_hash and is_safe_user_password_hash(encoded_hash):
            return self.verify(encoded_hash, password)
        self.verify(_USER_PASSWORD_DUMMY_HASH, password)
        return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        if not is_safe_user_password_hash(encoded_hash):
            return True
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except (InvalidHashError, VerificationError):
            return True


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not 3 <= len(normalized) <= 80:
        raise ValueError("username must contain 3 to 80 characters")
    return normalized


class SessionTokenManager:
    @staticmethod
    def generate() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_sid(raw_sid: str) -> str:
        if not raw_sid:
            raise ValueError("empty session identifier")
        return hashlib.sha256(raw_sid.encode("ascii")).hexdigest()

    @classmethod
    def redis_key(cls, prefix: str, namespace: str, raw_sid: str) -> str:
        return f"{prefix}:session:{namespace}:{cls.hash_sid(raw_sid)}"

    @classmethod
    def redis_key_from_hash(cls, prefix: str, namespace: str, sid_hash: str) -> str:
        if len(sid_hash) != 64:
            raise ValueError("invalid SID hash")
        return f"{prefix}:session:{namespace}:{sid_hash}"


class InviteCodeManager:
    @staticmethod
    def generate() -> str:
        # 原码只在创建响应中出现一次，数据库只保存摘要。
        return "bbw_" + secrets.token_urlsafe(24)

    @staticmethod
    def hash(code: str) -> str:
        normalized = code.strip()
        if len(normalized) < 16 or len(normalized) > 200:
            raise ValueError("invalid invite code")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def keyed_identifier_hash(value: str, key: bytes, *, purpose: str) -> str:
    if len(key) < 32:
        raise ValueError("identifier HMAC key must contain at least 32 bytes")
    normalized = unicodedata.normalize("NFKC", value).strip().encode("utf-8")
    return hmac.new(key, f"bbw:{purpose}:v1:".encode() + normalized, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class TOTPVerification:
    valid: bool
    counter: int | None = None


class TOTPManager:
    def __init__(self, *, digits: int = 6, period: int = 30):
        if digits not in {6, 8}:
            raise ValueError("TOTP digits must be 6 or 8")
        self.digits = digits
        self.period = period

    @staticmethod
    def generate_secret() -> str:
        return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_secret(secret: str) -> bytes:
        normalized = secret.strip().replace(" ", "").upper()
        try:
            decoded = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
        except Exception as exc:
            raise ValueError("invalid TOTP secret") from exc
        if len(decoded) < 16:
            raise ValueError("TOTP secret is too short")
        return decoded

    def at_counter(self, secret: str, counter: int) -> str:
        digest = hmac.new(
            self._decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1
        ).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return str(binary % (10**self.digits)).zfill(self.digits)

    def verify(
        self,
        secret: str,
        code: str,
        *,
        at_time: int | float | None = None,
        valid_window: int = 1,
        after_counter: int | None = None,
    ) -> TOTPVerification:
        if not code.isdigit() or len(code) != self.digits:
            return TOTPVerification(False)
        timestamp = time.time() if at_time is None else float(at_time)
        current = int(timestamp // self.period)
        for offset in range(-valid_window, valid_window + 1):
            counter = current + offset
            if counter < 0 or (after_counter is not None and counter <= after_counter):
                continue
            if hmac.compare_digest(self.at_counter(secret, counter), code):
                return TOTPVerification(True, counter)
        return TOTPVerification(False)

    def provisioning_uri(self, secret: str, *, account_name: str, issuer: str = "BBW Admin") -> str:
        label = quote(f"{issuer}:{account_name}")
        return (
            f"otpauth://totp/{label}?secret={quote(secret)}&issuer={quote(issuer)}"
            f"&period={self.period}&digits={self.digits}&algorithm=SHA1"
        )
