"""生产环境配置。

本模块刻意不在 import 阶段读取或校验密钥，避免 Alembic、运维脚本等
不需要解密能力的进程因为未挂载 Secret 而无法启动。真正需要密钥时调用
``Settings.load_credential_keyring``。
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote_plus


MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE


class ConfigurationError(RuntimeError):
    """配置缺失或格式错误。"""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return parsed


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _read_secret_source(value: str | None, file_path: str | None, *, name: str) -> bytes:
    if value and file_path:
        raise ConfigurationError(f"configure only one of {name} and {name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_bytes()
        except OSError as exc:
            raise ConfigurationError(f"cannot read {name}_FILE: {file_path}") from exc
    if value is not None:
        return value.encode("utf-8")
    raise ConfigurationError(f"missing {name} or {name}_FILE")


def decode_32_byte_secret(raw: bytes, *, name: str) -> bytes:
    """接受 32 字节原始密钥、Base64/Base64URL 或 64 位十六进制。"""

    if len(raw) == 32:
        return raw
    stripped = raw.strip()
    if len(stripped) == 32:
        return stripped
    try:
        text = stripped.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"{name} must contain 32 raw bytes or an encoded key") from exc

    if len(text) == 64:
        try:
            decoded_hex = bytes.fromhex(text)
        except ValueError:
            decoded_hex = b""
        if len(decoded_hex) == 32:
            return decoded_hex

    padded = text + "=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded)
        except (ValueError, binascii.Error):
            continue
        if len(decoded) == 32:
            return decoded
    raise ConfigurationError(f"{name} must decode to exactly 32 bytes")


def _load_json_mapping(value: str | None, file_path: str | None, *, name: str) -> Mapping[str, str]:
    if value and file_path:
        raise ConfigurationError(f"configure only one of {name} and {name}_FILE")
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"cannot read {name}_FILE: {file_path}") from exc
    if not value:
        return {}
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ConfigurationError(f"{name} contains duplicate key version {key!r}")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must be a JSON object") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(secret, str) for key, secret in parsed.items()
    ):
        raise ConfigurationError(f"{name} must map key versions to encoded keys")
    return parsed


def _database_url_from_env() -> str:
    explicit = os.getenv("BBW_DATABASE_URL")
    if explicit:
        return explicit
    password = os.getenv("POSTGRES_PASSWORD")
    password_file = os.getenv("POSTGRES_PASSWORD_FILE")
    if password and password_file:
        raise ConfigurationError("configure only one of POSTGRES_PASSWORD and POSTGRES_PASSWORD_FILE")
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"cannot read POSTGRES_PASSWORD_FILE: {password_file}") from exc
    password = password if password is not None else "bbw"
    user = os.getenv("POSTGRES_USER", "bbw")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = _env_int("POSTGRES_PORT", 5432, minimum=1)
    database = os.getenv("POSTGRES_DB", "bbw")
    return (
        f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}@"
        f"{host}:{port}/{quote_plus(database)}"
    )


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    database_url: str
    redis_url: str
    credential_master_key: str | None
    credential_master_key_file: str | None
    credential_keys_json: str | None
    credential_keys_file: str | None
    credential_key_version: int
    phone_hmac_key: str | None
    phone_hmac_key_file: str | None
    session_hmac_key: str | None
    session_hmac_key_file: str | None
    sql_echo: bool
    db_pool_size: int
    db_max_overflow: int
    web_session_idle_seconds: int
    web_session_absolute_seconds: int
    admin_session_idle_seconds: int
    admin_session_absolute_seconds: int
    admin_unlock_seconds: int
    audit_retention_days: int
    chat_retention_days: int
    raw_response_retention_days: int
    user_media_quota_bytes: int
    system_media_quota_bytes: int
    redis_prefix: str
    user_cookie_name: str
    admin_cookie_name: str
    cookie_secure: bool
    trust_proxy_headers: bool
    max_json_body_bytes: int
    max_request_body_bytes: int
    r2_endpoint: str | None
    r2_access_key_file: str | None
    r2_secret_key_file: str | None
    r2_bucket: str | None
    r2_region: str
    rq_queues: tuple[str, ...]
    turnstile_site_key: str | None
    turnstile_secret_key_file: str | None
    invite_required: bool
    active_sync_seconds: int
    inactive_sync_seconds: int
    media_strip_metadata: bool
    media_max_image_bytes: int
    media_max_audio_bytes: int
    media_max_video_bytes: int
    media_max_attachment_bytes: int
    media_allowed_hosts: tuple[str, ...]
    r2_presign_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("BBW_ENV", "development"),
            database_url=_database_url_from_env(),
            redis_url=os.getenv("BBW_REDIS_URL", "redis://redis:6379/0"),
            credential_master_key=os.getenv("BBW_CREDENTIAL_MASTER_KEY"),
            credential_master_key_file=os.getenv("BBW_CREDENTIAL_MASTER_KEY_FILE"),
            credential_keys_json=os.getenv("BBW_CREDENTIAL_KEYS_JSON"),
            credential_keys_file=os.getenv("BBW_CREDENTIAL_KEYS_FILE"),
            credential_key_version=_env_int("BBW_CREDENTIAL_KEY_VERSION", 1, minimum=1),
            phone_hmac_key=os.getenv("BBW_PHONE_HMAC_KEY"),
            phone_hmac_key_file=os.getenv("BBW_PHONE_HMAC_KEY_FILE"),
            session_hmac_key=os.getenv("BBW_SESSION_HMAC_KEY"),
            session_hmac_key_file=os.getenv("BBW_SESSION_HMAC_KEY_FILE"),
            sql_echo=_env_bool("BBW_SQL_ECHO", False),
            db_pool_size=_env_int("BBW_DB_POOL_SIZE", 5, minimum=1),
            db_max_overflow=_env_int("BBW_DB_MAX_OVERFLOW", 2, minimum=0),
            web_session_idle_seconds=_env_int("BBW_WEB_SESSION_IDLE_SECONDS", 7 * 86400, minimum=60),
            web_session_absolute_seconds=_env_int(
                "BBW_WEB_SESSION_ABSOLUTE_SECONDS", 30 * 86400, minimum=60
            ),
            admin_session_idle_seconds=_env_int(
                "BBW_ADMIN_SESSION_IDLE_SECONDS", 30 * 60, minimum=60
            ),
            admin_session_absolute_seconds=_env_int(
                "BBW_ADMIN_SESSION_ABSOLUTE_SECONDS", 8 * 3600, minimum=60
            ),
            admin_unlock_seconds=_env_int("BBW_ADMIN_UNLOCK_SECONDS", 15 * 60, minimum=60),
            audit_retention_days=_env_int("BBW_AUDIT_RETENTION_DAYS", 180, minimum=1),
            chat_retention_days=_env_int("BBW_CHAT_RETENTION_DAYS", 180, minimum=1),
            raw_response_retention_days=_env_int(
                "BBW_RAW_RESPONSE_RETENTION_DAYS", 7, minimum=1
            ),
            user_media_quota_bytes=_env_int(
                "BBW_USER_MEDIA_QUOTA_BYTES", 100 * MEBIBYTE, minimum=1
            ),
            system_media_quota_bytes=_env_int(
                "BBW_SYSTEM_MEDIA_QUOTA_BYTES", 8 * GIBIBYTE, minimum=1
            ),
            redis_prefix=os.getenv("BBW_REDIS_PREFIX", "bbw").strip(": ") or "bbw",
            user_cookie_name=os.getenv("BBW_USER_COOKIE_NAME", "__Host-bbw_sid"),
            admin_cookie_name=os.getenv("BBW_ADMIN_COOKIE_NAME", "__Host-bbw_admin_sid"),
            cookie_secure=_env_bool("BBW_COOKIE_SECURE", True),
            trust_proxy_headers=_env_bool("BBW_TRUST_PROXY_HEADERS", True),
            max_json_body_bytes=_env_int("BBW_MAX_JSON_BODY_BYTES", 256 * 1024, minimum=1024),
            max_request_body_bytes=_env_int(
                "BBW_MAX_REQUEST_BODY_BYTES", 32 * MEBIBYTE, minimum=1024
            ),
            r2_endpoint=os.getenv("BBW_R2_ENDPOINT"),
            r2_access_key_file=os.getenv("BBW_R2_ACCESS_KEY_FILE"),
            r2_secret_key_file=os.getenv("BBW_R2_SECRET_KEY_FILE"),
            r2_bucket=os.getenv("BBW_R2_BUCKET"),
            r2_region=os.getenv("BBW_R2_REGION", "auto"),
            rq_queues=tuple(
                item.strip()
                for item in os.getenv("BBW_RQ_QUEUES", "critical,default,media,sync").split(",")
                if item.strip()
            ),
            turnstile_site_key=os.getenv("BBW_TURNSTILE_SITE_KEY"),
            turnstile_secret_key_file=os.getenv("BBW_TURNSTILE_SECRET_KEY_FILE"),
            invite_required=_env_bool("BBW_INVITE_REQUIRED", True),
            active_sync_seconds=_env_int("BBW_ACTIVE_SYNC_SECONDS", 300, minimum=60),
            inactive_sync_seconds=_env_int("BBW_INACTIVE_SYNC_SECONDS", 3600, minimum=300),
            media_strip_metadata=_env_bool("BBW_MEDIA_STRIP_METADATA", True),
            media_max_image_bytes=_env_int(
                "BBW_MEDIA_MAX_IMAGE_BYTES", 10 * MEBIBYTE, minimum=1
            ),
            media_max_audio_bytes=_env_int(
                "BBW_MEDIA_MAX_AUDIO_BYTES", 10 * MEBIBYTE, minimum=1
            ),
            media_max_video_bytes=_env_int(
                "BBW_MEDIA_MAX_VIDEO_BYTES", 50 * MEBIBYTE, minimum=1
            ),
            media_max_attachment_bytes=_env_int(
                "BBW_MEDIA_MAX_ATTACHMENT_BYTES", 20 * MEBIBYTE, minimum=1
            ),
            media_allowed_hosts=_env_csv(
                "BBW_MEDIA_ALLOWED_HOSTS",
                "oss.banghua.xin,*.myqcloud.com,*.qcloud.com",
            ),
            r2_presign_ttl_seconds=_env_int(
                "BBW_R2_PRESIGN_TTL_SECONDS", 300, minimum=30
            ),
        )

    # 下列别名采用应用层约定的名称，同时保留上方更明确的内部名称。
    @property
    def master_key_file(self) -> str | None:
        return self.credential_master_key_file

    @property
    def user_idle_ttl_seconds(self) -> int:
        return self.web_session_idle_seconds

    @property
    def user_absolute_ttl_seconds(self) -> int:
        return self.web_session_absolute_seconds

    @property
    def admin_idle_ttl_seconds(self) -> int:
        return self.admin_session_idle_seconds

    @property
    def admin_absolute_ttl_seconds(self) -> int:
        return self.admin_session_absolute_seconds

    @property
    def credential_unlock_seconds(self) -> int:
        return self.admin_unlock_seconds

    @property
    def message_retention_days(self) -> int:
        return self.chat_retention_days

    @property
    def per_user_media_quota_bytes(self) -> int:
        return self.user_media_quota_bytes

    @property
    def global_media_quota_bytes(self) -> int:
        return self.system_media_quota_bytes

    @property
    def access_key_file(self) -> str | None:
        return self.r2_access_key_file

    @property
    def secret_key_file(self) -> str | None:
        return self.r2_secret_key_file

    def load_credential_keyring(self) -> dict[int, bytes]:
        encoded = _load_json_mapping(
            self.credential_keys_json,
            self.credential_keys_file,
            name="BBW_CREDENTIAL_KEYS_JSON",
        )
        keyring: dict[int, bytes] = {}
        for raw_version, secret in encoded.items():
            try:
                version = int(raw_version)
            except ValueError as exc:
                raise ConfigurationError("credential key versions must be integers") from exc
            if version < 1:
                raise ConfigurationError("credential key versions must be positive")
            if raw_version != str(version):
                raise ConfigurationError(
                    "credential key versions must use canonical positive integer strings"
                )
            decoded_key = decode_32_byte_secret(
                secret.encode("ascii"), name=f"credential key version {version}"
            )
            if version in keyring:
                raise ConfigurationError(f"duplicate credential key version {version}")
            keyring[version] = decoded_key

        if self.credential_master_key or self.credential_master_key_file:
            raw = _read_secret_source(
                self.credential_master_key,
                self.credential_master_key_file,
                name="BBW_CREDENTIAL_MASTER_KEY",
            )
            active_key = decode_32_byte_secret(
                raw, name="BBW_CREDENTIAL_MASTER_KEY"
            )
            existing_active_key = keyring.get(self.credential_key_version)
            if existing_active_key is not None and not hmac.compare_digest(
                existing_active_key, active_key
            ):
                raise ConfigurationError(
                    "active credential master key conflicts with the same keyring version"
                )
            keyring[self.credential_key_version] = active_key
        if self.credential_key_version not in keyring:
            raise ConfigurationError(
                f"active credential key version {self.credential_key_version} is not configured"
            )
        return keyring

    def load_phone_hmac_key(self) -> bytes:
        raw = _read_secret_source(
            self.phone_hmac_key,
            self.phone_hmac_key_file,
            name="BBW_PHONE_HMAC_KEY",
        )
        return decode_32_byte_secret(raw, name="BBW_PHONE_HMAC_KEY")

    def load_session_hmac_key(self) -> bytes:
        raw = _read_secret_source(
            self.session_hmac_key,
            self.session_hmac_key_file,
            name="BBW_SESSION_HMAC_KEY",
        )
        return decode_32_byte_secret(raw, name="BBW_SESSION_HMAC_KEY")

    def validate(self) -> None:
        if not self.database_url.startswith(("postgresql://", "postgresql+")):
            raise ConfigurationError("BBW_DATABASE_URL must use PostgreSQL")
        if self.environment.strip().lower() in {"prod", "production"}:
            if self.database_url == "postgresql+psycopg://bbw:bbw@postgres:5432/bbw":
                raise ConfigurationError(
                    "production must provide BBW_DATABASE_URL or POSTGRES_PASSWORD_FILE via the container entrypoint"
                )
            direct_secret_names = [
                name
                for name, value in (
                    ("BBW_CREDENTIAL_MASTER_KEY", self.credential_master_key),
                    ("BBW_CREDENTIAL_KEYS_JSON", self.credential_keys_json),
                    ("BBW_PHONE_HMAC_KEY", self.phone_hmac_key),
                    ("BBW_SESSION_HMAC_KEY", self.session_hmac_key),
                )
                if value is not None
            ]
            if direct_secret_names:
                raise ConfigurationError(
                    "production secrets must use *_FILE sources, not direct environment values: "
                    + ", ".join(direct_secret_names)
                )
            if not self.cookie_secure:
                raise ConfigurationError("production cookies must be Secure")
            if not self.user_cookie_name.startswith(
                "__Host-"
            ) or not self.admin_cookie_name.startswith("__Host-"):
                raise ConfigurationError("production cookies must use the __Host- prefix")
            if self.web_session_idle_seconds > 7 * 86400:
                raise ConfigurationError("production web-session idle lifetime must not exceed 7 days")
            if self.web_session_absolute_seconds > 30 * 86400:
                raise ConfigurationError(
                    "production web-session absolute lifetime must not exceed 30 days"
                )
            if self.admin_session_idle_seconds > 30 * 60:
                raise ConfigurationError(
                    "production administrator idle lifetime must not exceed 30 minutes"
                )
            if self.admin_session_absolute_seconds > 8 * 3600:
                raise ConfigurationError(
                    "production administrator absolute lifetime must not exceed 8 hours"
                )
            if self.chat_retention_days > 180:
                raise ConfigurationError("production chat retention must not exceed 180 days")
            if self.raw_response_retention_days > 7:
                raise ConfigurationError(
                    "production raw-response retention must not exceed 7 days"
                )
            if self.audit_retention_days > 180:
                raise ConfigurationError("production audit retention must not exceed 180 days")
        if self.web_session_idle_seconds > self.web_session_absolute_seconds:
            raise ConfigurationError("web session idle lifetime exceeds absolute lifetime")
        if self.admin_session_idle_seconds > self.admin_session_absolute_seconds:
            raise ConfigurationError("admin session idle lifetime exceeds absolute lifetime")
        if self.admin_unlock_seconds > 15 * 60:
            raise ConfigurationError("administrator credential unlock must not exceed 15 minutes")
        if self.user_media_quota_bytes > self.system_media_quota_bytes:
            raise ConfigurationError("per-user media quota exceeds system quota")
        largest_file = max(
            self.media_max_image_bytes,
            self.media_max_audio_bytes,
            self.media_max_video_bytes,
            self.media_max_attachment_bytes,
        )
        if largest_file > self.user_media_quota_bytes:
            raise ConfigurationError("a per-file media limit exceeds the per-user quota")
        if self.active_sync_seconds > self.inactive_sync_seconds:
            raise ConfigurationError("active sync interval exceeds inactive sync interval")
        if self.r2_presign_ttl_seconds > 900:
            raise ConfigurationError("R2 presign lifetime must not exceed 900 seconds")
        if not self.media_allowed_hosts:
            raise ConfigurationError("at least one media archive host must be allowlisted")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
        _settings.validate()
    return _settings


def reset_settings_cache() -> None:
    """仅供测试或显式重载环境变量。"""

    global _settings
    _settings = None
