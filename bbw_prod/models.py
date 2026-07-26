"""生产数据模型。

所有用户业务表都显式携带 ``owner_user_id``，repository 也必须把 owner 条件
作为查询的一部分，避免仅凭上游 UID 或对象 ID 造成跨用户读取。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .config import GIBIBYTE, MEBIBYTE
from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def expires_in_days(days: int) -> datetime:
    return utcnow() + timedelta(days=days)


JSON_EMPTY_OBJECT = text("'{}'::jsonb")
JSON_EMPTY_ARRAY = text("'[]'::jsonb")


class SerializableMixin:
    """为管理端和 Pydantic ``from_attributes`` 提供安全的基础序列化。"""

    __sensitive_fields__: ClassVar[frozenset[str]] = frozenset()

    def to_dict(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attribute in self.__mapper__.column_attrs:
            key = attribute.key
            if not include_sensitive and key in self.__sensitive_fields__:
                continue
            value = getattr(self, key)
            if isinstance(value, uuid.UUID):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            result[key] = value
        return result


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        server_default=func.now(),
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("media_quota_bytes > 0", name="user_media_quota_positive"),
        CheckConstraint("media_used_bytes >= 0", name="user_media_used_nonnegative"),
        CheckConstraint("media_used_bytes <= media_quota_bytes", name="user_media_within_quota"),
        CheckConstraint("chat_retention_days >= 1", name="user_chat_retention_positive"),
        Index("ix_users_status_created", "status", "created_at"),
    )

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    display_name: Mapped[str | None] = mapped_column(String(160))
    profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    invite_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invite_codes.id", ondelete="SET NULL"), index=True
    )
    media_quota_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=100 * MEBIBYTE, server_default=str(100 * MEBIBYTE)
    )
    media_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    chat_retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=180, server_default="180"
    )
    match_pool_online_list_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    nearby_custom_city_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    byok_model_runner_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    byok_account_actions_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    byok_autonomous_agent_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InviteCode(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "invite_codes"
    __table_args__ = (
        CheckConstraint("max_uses >= 1", name="invite_max_uses_positive"),
        CheckConstraint("use_count >= 0", name="invite_use_count_nonnegative"),
        CheckConstraint("use_count <= max_uses", name="invite_use_count_within_max"),
        Index("ix_invite_codes_available", "disabled_at", "expires_at"),
    )
    __sensitive_fields__ = frozenset({"code_hash"})

    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(120))
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )


class ExternalAccount(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "external_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_external_accounts_user"),
        UniqueConstraint("provider", "upstream_uid", name="uq_external_accounts_provider_uid"),
        UniqueConstraint("provider", "phone_hmac", name="uq_external_accounts_provider_phone"),
        UniqueConstraint("id", "user_id", name="uq_external_accounts_id_user"),
    )
    __sensitive_fields__ = frozenset(
        {
            "phone_hmac",
            "phone_encrypted",
            "login_account_encrypted",
            "password_encrypted",
            "token_encrypted",
        }
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        String(40), nullable=False, default="beibeiwu", server_default="beibeiwu"
    )
    upstream_uid: Mapped[str | None] = mapped_column(String(128))
    phone_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_encrypted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    login_account_encrypted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    password_encrypted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_encrypted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserCredential(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Web 本地认证材料，与上游账号绑定和可逆凭据严格分离。"""

    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_credentials_user"),
        CheckConstraint(
            "credential_version >= 1",
            name="user_credential_version_positive",
        ),
        Index("ix_user_credentials_enabled", "disabled_at"),
    )
    __sensitive_fields__ = frozenset({"password_hash"})

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    credential_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    enrollment_source: Mapped[str] = mapped_column(String(48), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebSession(UUIDPrimaryKeyMixin, SerializableMixin, Base):
    __tablename__ = "web_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("external_account_id", "user_id"),
            ("external_accounts.id", "external_accounts.user_id"),
            ondelete="CASCADE",
            name="fk_web_sessions_account_owner",
        ),
        CheckConstraint("idle_expires_at <= absolute_expires_at", name="web_session_idle_before_absolute"),
        CheckConstraint(
            "auth_source IN ('provider', 'web-local')",
            name="web_session_auth_source_valid",
        ),
        Index("ix_web_sessions_user_active", "user_id", "revoked_at", "absolute_expires_at"),
    )
    __sensitive_fields__ = frozenset({"sid_hash", "ip_hash"})

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    external_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sid_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    auth_source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="provider", server_default="provider"
    )
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(160))


class AdminUser(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (Index("ix_admin_users_active", "is_active"),)
    __sensitive_fields__ = frozenset({"password_hash", "totp_secret_encrypted"})

    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    totp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    totp_secret_encrypted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_totp_counter: Mapped[int | None] = mapped_column(BigInteger)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class AdminSession(UUIDPrimaryKeyMixin, SerializableMixin, Base):
    __tablename__ = "admin_sessions"
    __table_args__ = (
        CheckConstraint("idle_expires_at <= absolute_expires_at", name="admin_session_idle_before_absolute"),
        Index("ix_admin_sessions_admin_active", "admin_user_id", "revoked_at", "absolute_expires_at"),
    )
    __sensitive_fields__ = frozenset({"sid_hash", "ip_hash"})

    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sid_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sensitive_unlocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(160))


class AuditLog(UUIDPrimaryKeyMixin, SerializableMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_admin_created", "admin_user_id", "created_at"),
        Index("ix_audit_logs_target_created", "target_user_id", "created_at"),
        Index("ix_audit_logs_expiry", "expires_at"),
    )
    __sensitive_fields__ = frozenset({"ip_hash"})

    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="RESTRICT"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(500))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: expires_in_days(180)
    )


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_conversations_id_owner"),
        UniqueConstraint(
            "owner_user_id", "provider", "upstream_conversation_id", name="uq_conversations_owner_source"
        ),
        Index("ix_conversations_owner_last_message", "owner_user_id", "last_message_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="tim", server_default="tim")
    upstream_conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    peer_upstream_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="direct", server_default="direct")
    title: Mapped[str | None] = mapped_column(String(200))
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unread_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class Message(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ("conversation_id", "owner_user_id"),
            ("conversations.id", "conversations.owner_user_id"),
            ondelete="CASCADE",
            name="fk_messages_conversation_owner",
        ),
        UniqueConstraint("id", "owner_user_id", name="uq_messages_id_owner"),
        UniqueConstraint(
            "owner_user_id", "provider", "upstream_message_id", name="uq_messages_owner_source"
        ),
        Index("ix_messages_owner_occurred", "owner_user_id", "occurred_at"),
        Index("ix_messages_conversation_occurred", "conversation_id", "occurred_at"),
        Index(
            "ix_messages_owner_conversation_sequence",
            "owner_user_id",
            "conversation_id",
            "provider",
            text("(metadata ->> 'message_sequence')"),
        ),
        Index(
            "ix_messages_body_trgm",
            text("message_search_index_text(body) gin_trgm_ops"),
            postgresql_using="gin",
            postgresql_where=text(
                "status <> 'revoked' AND "
                "coalesce(metadata ->> 'revoked', 'false') NOT IN ('true', '1')"
            ),
        ),
        Index(
            "ix_messages_media_name_trgm",
            text("message_search_media_name(metadata) gin_trgm_ops"),
            postgresql_using="gin",
            postgresql_where=text(
                "status <> 'revoked' AND "
                "coalesce(metadata ->> 'revoked', 'false') NOT IN ('true', '1')"
            ),
        ),
        Index(
            "ix_messages_quote_text_trgm",
            text("message_search_quote_text(metadata) gin_trgm_ops"),
            postgresql_using="gin",
            postgresql_where=text(
                "status <> 'revoked' AND "
                "coalesce(metadata ->> 'revoked', 'false') NOT IN ('true', '1')"
            ),
        ),
        Index("ix_messages_retention", "retention_expires_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="tim", server_default="tim")
    upstream_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    recipient_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    message_type: Mapped[str] = mapped_column(String(40), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received", server_default="received")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: expires_in_days(180)
    )
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class ChatThread(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Web 本地权威会话。

    ``Conversation`` 继续作为按用户查询的兼容投影；线程、成员和规范消息以
    这里的 canonical 记录为准。直接会话的 ``direct_key`` 由两个内部用户 ID
    排序生成，因此不依赖 Banghua/TIM 的存活状态。
    """

    __tablename__ = "chat_threads"
    __table_args__ = (
        UniqueConstraint("direct_key", name="uq_chat_threads_direct_key"),
        CheckConstraint("kind = 'direct'", name="chat_thread_direct_only"),
        Index("ix_chat_threads_last_message", "last_message_at"),
    )

    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="direct", server_default="direct"
    )
    direct_key: Mapped[str] = mapped_column(String(96), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class ChatMember(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "chat_members"
    __table_args__ = (
        UniqueConstraint("thread_id", "user_id", name="uq_chat_members_thread_user"),
        CheckConstraint("unread_count >= 0", name="chat_member_unread_nonnegative"),
        Index("ix_chat_members_user_active", "user_id", "left_at"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(24), nullable=False, default="member", server_default="member"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unread_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class ChatMessage(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "sender_user_id", "client_message_id", name="uq_chat_messages_sender_client"
        ),
        UniqueConstraint("id", "thread_id", name="uq_chat_messages_id_thread"),
        CheckConstraint(
            "message_type <> 'text' OR length(btrim(coalesce(body, ''))) > 0",
            name="chat_message_text_nonempty",
        ),
        Index("ix_chat_messages_thread_occurred", "thread_id", "occurred_at", "id"),
        Index("ix_chat_messages_retention", "retention_expires_at"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    sender_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    client_message_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str] = mapped_column(String(40), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="accepted", server_default="accepted"
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class MessageReceipt(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "message_receipts"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "user_id", "receipt_type", name="uq_message_receipts_message_user_type"
        ),
        Index("ix_message_receipts_user_occurred", "user_id", "occurred_at"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    receipt_type: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class MessageDelivery(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """本地投递记录，同时作为非阻塞 TIM mirror outbox。"""

    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "channel", "target_key", name="uq_message_deliveries_message_target"
        ),
        CheckConstraint("attempt_count >= 0", name="message_delivery_attempt_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="message_delivery_max_attempt_positive"),
        Index("ix_message_deliveries_claim", "channel", "status", "available_at", "locked_until"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    target_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    target_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8, server_default="8")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class MediaObject(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "media_objects"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="media_size_nonnegative"),
        ForeignKeyConstraint(
            ("message_id", "owner_user_id"),
            ("messages.id", "messages.owner_user_id"),
            ondelete="RESTRICT",
            name="fk_media_objects_message_owner",
        ),
        UniqueConstraint("r2_bucket", "r2_object_key", name="uq_media_objects_r2_object"),
        Index("ix_media_objects_owner_status", "owner_user_id", "status"),
        Index("ix_media_objects_retention", "retention_expires_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    r2_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    r2_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    counts_toward_quota: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: expires_in_days(180)
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class Relationship(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "provider", "subject_upstream_uid", "kind", name="uq_relationships_owner_subject"
        ),
        Index("ix_relationships_owner_kind_status", "owner_user_id", "kind", "status"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="beibeiwu", server_default="beibeiwu")
    subject_upstream_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class ActivityEvent(UUIDPrimaryKeyMixin, SerializableMixin, Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "provider", "upstream_event_id", name="uq_activity_events_owner_source"
        ),
        Index("ix_activity_events_owner_occurred", "owner_user_id", "occurred_at"),
        Index("ix_activity_events_owner_type", "owner_user_id", "event_type"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="beibeiwu", server_default="beibeiwu")
    upstream_event_id: Mapped[str | None] = mapped_column(String(256))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    subject_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class RawUpstreamResponse(UUIDPrimaryKeyMixin, SerializableMixin, Base):
    __tablename__ = "raw_upstream_responses"
    __table_args__ = (
        Index("ix_raw_upstream_owner_received", "owner_user_id", "received_at"),
        Index("ix_raw_upstream_expiry", "expires_at"),
    )
    __sensitive_fields__ = frozenset({"encrypted_payload"})

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    request_correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    encrypted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: expires_in_days(7)
    )


class AiModelRunnerSystemSetting(TimestampMixin, SerializableMixin, Base):
    """管理员控制的 BYOK 模型运行器单行总开关。"""

    __tablename__ = "ai_model_runner_system_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ai_model_runner_system_singleton"),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, default=1, server_default="1"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    account_actions_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    autonomous_agent_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )


class AiModelConnection(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """用户自带密钥的 OpenAI-compatible 模型连接。"""

    __tablename__ = "ai_model_connections"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_ai_model_connections_id_owner"),
        UniqueConstraint(
            "owner_user_id", "label", name="uq_ai_model_connections_owner_label"
        ),
        CheckConstraint(
            "provider = 'openai_compatible'",
            name="ai_model_connection_provider_supported",
        ),
        CheckConstraint(
            "last_test_status IN ('never', 'ok', 'failed')",
            name="ai_model_connection_test_status_valid",
        ),
        Index("ix_ai_model_connections_owner_updated", "owner_user_id", "updated_at"),
    )
    __sensitive_fields__ = frozenset({"api_key_encrypted"})

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(40), nullable=False, default="openai_compatible", server_default="openai_compatible"
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    api_key_encrypted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_test_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="never", server_default="never"
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class AiAgentSetting(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """用户自己的运行开关与草稿生成参数。"""

    __tablename__ = "ai_agent_settings"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_ai_agent_settings_owner"),
        ForeignKeyConstraint(
            ("active_connection_id", "owner_user_id"),
            ("ai_model_connections.id", "ai_model_connections.owner_user_id"),
            ondelete="CASCADE",
            name="fk_ai_agent_settings_connection_owner",
        ),
        CheckConstraint("mode = 'draft'", name="ai_agent_setting_mode_supported"),
        CheckConstraint(
            "temperature_milli BETWEEN 0 AND 2000",
            name="ai_agent_setting_temperature_valid",
        ),
        CheckConstraint(
            "max_output_tokens BETWEEN 64 AND 4096",
            name="ai_agent_setting_output_tokens_valid",
        ),
        CheckConstraint(
            "context_message_limit BETWEEN 1 AND 100",
            name="ai_agent_setting_context_limit_valid",
        ),
        CheckConstraint("version >= 1", name="ai_agent_setting_version_positive"),
    )
    __sensitive_fields__ = frozenset({"custom_instructions"})

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    active_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    temperature_milli: Mapped[int] = mapped_column(
        Integer, nullable=False, default=700, server_default="700"
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=512, server_default="512"
    )
    context_message_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=30, server_default="30"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class AiAgentExecutionSetting(
    UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base
):
    """用户自己的账号动作执行开关与显式动作白名单。"""

    __tablename__ = "ai_agent_execution_settings"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", name="uq_ai_agent_execution_settings_owner"
        ),
        CheckConstraint(
            "NOT auto_send_enabled OR user_enabled",
            name="ai_agent_execution_setting_auto_send_requires_enabled",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_actions) = 'array'",
            name="ai_agent_execution_setting_allowed_actions_array",
        ),
        CheckConstraint(
            "allowed_actions <@ "
            "'[\"send_private_message\", \"publish_text_post\", "
            "\"follow_user\", \"unfollow_user\"]'::jsonb",
            name="ai_agent_execution_setting_allowed_actions_supported",
        ),
        CheckConstraint(
            "version >= 1", name="ai_agent_execution_setting_version_positive"
        ),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    auto_send_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    allowed_actions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=JSON_EMPTY_ARRAY
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class AiAgentAutonomySetting(
    UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base
):
    """无人值守 Agent 的用户级策略、预算与紧急停机状态。"""

    __tablename__ = "ai_agent_autonomy_settings"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", name="uq_ai_agent_autonomy_settings_owner"
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_actions) = 'array'",
            name="ai_agent_autonomy_allowed_actions_array",
        ),
        CheckConstraint(
            "allowed_actions <@ "
            "'[\"send_private_message\", \"publish_text_post\", "
            "\"follow_user\", \"unfollow_user\"]'::jsonb",
            name="ai_agent_autonomy_allowed_actions_supported",
        ),
        CheckConstraint(
            "jsonb_typeof(managed_target_uids) = 'array'",
            name="ai_agent_autonomy_targets_array",
        ),
        CheckConstraint(
            "length(operation_brief) <= 4000",
            name="ai_agent_autonomy_brief_length",
        ),
        CheckConstraint(
            "length(btrim(timezone)) BETWEEN 1 AND 64",
            name="ai_agent_autonomy_timezone_length",
        ),
        CheckConstraint(
            "active_start_minute BETWEEN 0 AND 1439 "
            "AND active_end_minute BETWEEN 0 AND 1439",
            name="ai_agent_autonomy_active_minutes_valid",
        ),
        CheckConstraint(
            "minimum_action_interval_seconds BETWEEN 60 AND 86400",
            name="ai_agent_autonomy_minimum_interval_valid",
        ),
        CheckConstraint(
            "daily_total_limit BETWEEN 1 AND 200 "
            "AND daily_reply_limit BETWEEN 0 AND 200 "
            "AND daily_post_limit BETWEEN 0 AND 20 "
            "AND daily_relationship_limit BETWEEN 0 AND 100",
            name="ai_agent_autonomy_daily_limits_valid",
        ),
        CheckConstraint(
            "post_interval_minutes BETWEEN 60 AND 10080",
            name="ai_agent_autonomy_post_interval_valid",
        ),
        CheckConstraint(
            "consecutive_failure_limit BETWEEN 1 AND 20 "
            "AND consecutive_failures >= 0",
            name="ai_agent_autonomy_failure_limits_valid",
        ),
        CheckConstraint(
            "version >= 1", name="ai_agent_autonomy_version_positive"
        ),
        CheckConstraint(
            "NOT auto_reply_enabled OR "
            "(user_enabled AND allowed_actions ? 'send_private_message' "
            "AND daily_reply_limit >= 1)",
            name="ai_agent_autonomy_reply_requires_action",
        ),
        CheckConstraint(
            "NOT scheduled_post_enabled OR "
            "(user_enabled AND allowed_actions ? 'publish_text_post' "
            "AND daily_post_limit >= 1)",
            name="ai_agent_autonomy_post_requires_action",
        ),
        CheckConstraint(
            "NOT managed_relationships_enabled OR "
            "(user_enabled AND "
            "(allowed_actions ? 'follow_user' OR allowed_actions ? 'unfollow_user') "
            "AND jsonb_array_length(managed_target_uids) > 0 "
            "AND daily_relationship_limit >= 1)",
            name="ai_agent_autonomy_relationship_requires_targets",
        ),
        CheckConstraint(
            "NOT user_enabled OR auto_reply_enabled OR scheduled_post_enabled "
            "OR managed_relationships_enabled",
            name="ai_agent_autonomy_enabled_has_capability",
        ),
        Index(
            "ix_ai_agent_autonomy_settings_due",
            "user_enabled",
            "halted_at",
            "next_run_at",
        ),
    )
    __sensitive_fields__ = frozenset(
        {"operation_brief", "managed_target_uids"}
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    auto_reply_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    scheduled_post_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    managed_relationships_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    allowed_actions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=JSON_EMPTY_ARRAY
    )
    operation_brief: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    managed_target_uids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=JSON_EMPTY_ARRAY
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    active_start_minute: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    active_end_minute: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    minimum_action_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )
    daily_total_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=20, server_default="20"
    )
    daily_reply_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=10, server_default="10"
    )
    daily_post_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    daily_relationship_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=5, server_default="5"
    )
    post_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1440, server_default="1440"
    )
    consecutive_failure_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=3, server_default="3"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    halted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    halted_reason: Mapped[str | None] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class AiStyleProfile(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """从用户自己的历史发言提炼出的结构化语言风格。"""

    __tablename__ = "ai_style_profiles"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_ai_style_profiles_owner"),
        ForeignKeyConstraint(
            ("model_connection_id", "owner_user_id"),
            ("ai_model_connections.id", "ai_model_connections.owner_user_id"),
            ondelete="CASCADE",
            name="fk_ai_style_profiles_connection_owner",
        ),
        CheckConstraint(
            "source_message_count >= 0", name="ai_style_profile_source_count_nonnegative"
        ),
    )
    __sensitive_fields__ = frozenset({"summary", "traits"})

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    model_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    traits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    source_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class AiAgentRun(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """模型运行审计；不保存 prompt、密钥、Header 或原始模型响应。"""

    __tablename__ = "ai_agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("connection_id", "owner_user_id"),
            ("ai_model_connections.id", "ai_model_connections.owner_user_id"),
            ondelete="CASCADE",
            name="fk_ai_agent_runs_connection_owner",
        ),
        UniqueConstraint(
            "owner_user_id",
            "run_type",
            "idempotency_key",
            name="uq_ai_agent_runs_owner_type_idempotency",
        ),
        CheckConstraint(
            "run_type IN "
            "('connection_test', 'style_analysis', 'reply_draft', 'reply_send', "
            "'autonomous_reply', 'autonomous_post', 'autonomous_plan')",
            name="ai_agent_run_type_valid",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ai_agent_run_status_valid",
        ),
        CheckConstraint(
            "source_message_count >= 0", name="ai_agent_run_source_count_nonnegative"
        ),
        CheckConstraint(
            "prompt_char_count >= 0", name="ai_agent_run_prompt_chars_nonnegative"
        ),
        CheckConstraint(
            "output_char_count >= 0", name="ai_agent_run_output_chars_nonnegative"
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ai_agent_run_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ai_agent_run_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ai_agent_run_latency_nonnegative",
        ),
        Index("ix_ai_agent_runs_owner_created", "owner_user_id", "created_at"),
        Index("ix_ai_agent_runs_owner_status_created", "owner_user_id", "status", "created_at"),
    )
    __sensitive_fields__ = frozenset(
        {"idempotency_key", "model_snapshot", "output_text"}
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="running", server_default="running"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    model_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    peer_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    source_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    prompt_char_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_text: Mapped[str | None] = mapped_column(Text)
    output_char_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAgentActionExecution(
    UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base
):
    """账号动作执行审计；快照可能含正文或目标标识，默认禁止通用序列化。"""

    __tablename__ = "ai_agent_action_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("external_account_id", "owner_user_id"),
            ("external_accounts.id", "external_accounts.user_id"),
            ondelete="CASCADE",
            name="fk_ai_agent_action_executions_account_owner",
        ),
        UniqueConstraint(
            "owner_user_id",
            "action_type",
            "idempotency_key",
            name="uq_ai_agent_action_executions_owner_action_idempotency",
        ),
        CheckConstraint(
            "action_type IN "
            "('send_private_message', 'publish_text_post', "
            "'follow_user', 'unfollow_user')",
            name="ai_agent_action_execution_type_valid",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'cancelled', 'manual_review')",
            name="ai_agent_action_execution_status_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 160",
            name="ai_agent_action_execution_idempotency_length",
        ),
        CheckConstraint(
            "execution_setting_version >= 1",
            name="ai_agent_action_execution_setting_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(target_snapshot) = 'object'",
            name="ai_agent_action_execution_target_object",
        ),
        CheckConstraint(
            "jsonb_typeof(parameter_snapshot) = 'object'",
            name="ai_agent_action_execution_parameter_object",
        ),
        CheckConstraint(
            "approval_source IN ('user_explicit', 'user_allowlist')",
            name="ai_agent_action_execution_approval_source_valid",
        ),
        CheckConstraint(
            "trigger_source IN ('user', 'model', 'schedule', 'system')",
            name="ai_agent_action_execution_trigger_source_valid",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL "
            "AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'manual_review') "
            "AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL "
            "AND cancelled_at IS NOT NULL)",
            name="ai_agent_action_execution_timestamps_consistent",
        ),
        CheckConstraint(
            "(status IN ('failed', 'cancelled', 'manual_review') "
            "AND stable_error_code IS NOT NULL) OR "
            "(status IN ('queued', 'running', 'succeeded') "
            "AND stable_error_code IS NULL)",
            name="ai_agent_action_execution_error_consistent",
        ),
        Index(
            "ix_ai_agent_action_executions_owner_created",
            "owner_user_id",
            "created_at",
        ),
        Index(
            "ix_ai_agent_action_executions_owner_status_queued",
            "owner_user_id",
            "status",
            "queued_at",
        ),
        Index(
            "ix_ai_agent_action_executions_queue",
            "status",
            "queued_at",
        ),
    )
    __sensitive_fields__ = frozenset(
        {
            "idempotency_key",
            "target_snapshot",
            "parameter_snapshot",
            "external_result_id",
        }
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    external_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    execution_setting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    parameter_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    approval_source: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False)
    stable_error_code: Mapped[str | None] = mapped_column(String(64))
    external_result_id: Mapped[str | None] = mapped_column(String(256))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAgentAutonomyTask(
    UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base
):
    """一个不可自由扩展工具集的无人值守任务。"""

    __tablename__ = "ai_agent_autonomy_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ("external_account_id", "owner_user_id"),
            ("external_accounts.id", "external_accounts.user_id"),
            ondelete="CASCADE",
            name="fk_ai_agent_autonomy_tasks_account_owner",
        ),
        ForeignKeyConstraint(
            ("model_connection_id", "owner_user_id"),
            ("ai_model_connections.id", "ai_model_connections.owner_user_id"),
            ondelete="CASCADE",
            name="fk_ai_agent_autonomy_tasks_connection_owner",
        ),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_ai_agent_autonomy_tasks_owner_idempotency",
        ),
        CheckConstraint(
            "task_type IN ('reply_to_message', 'scheduled_post', "
            "'follow_target', 'unfollow_target')",
            name="ai_agent_autonomy_task_type_valid",
        ),
        CheckConstraint(
            "action_type IN ('send_private_message', 'publish_text_post', "
            "'follow_user', 'unfollow_user')",
            name="ai_agent_autonomy_task_action_valid",
        ),
        CheckConstraint(
            "(task_type = 'reply_to_message' AND action_type = 'send_private_message') OR "
            "(task_type = 'scheduled_post' AND action_type = 'publish_text_post') OR "
            "(task_type = 'follow_target' AND action_type = 'follow_user') OR "
            "(task_type = 'unfollow_target' AND action_type = 'unfollow_user')",
            name="ai_agent_autonomy_task_action_matches_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'deferred', 'leased', 'generating', "
            "'dispatching', 'succeeded', 'failed', 'cancelled', 'stale', "
            "'manual_review')",
            name="ai_agent_autonomy_task_status_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 160",
            name="ai_agent_autonomy_task_idempotency_length",
        ),
        CheckConstraint(
            "policy_version >= 1 AND execution_setting_version >= 1 "
            "AND runner_setting_version >= 1",
            name="ai_agent_autonomy_task_versions_positive",
        ),
        CheckConstraint(
            "length(runner_configuration_fingerprint) = 64",
            name="ai_agent_autonomy_task_runner_fingerprint_valid",
        ),
        CheckConstraint(
            "outcome_unknown = (status = 'manual_review')",
            name="ai_agent_autonomy_task_unknown_consistent",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ai_agent_autonomy_task_attempt_nonnegative",
        ),
        CheckConstraint(
            "dispatch_started_at IS NULL OR started_at IS NOT NULL",
            name="ai_agent_autonomy_task_dispatch_after_start",
        ),
        CheckConstraint(
            "dispatch_started_at IS NULL OR budget_day IS NOT NULL",
            name="ai_agent_autonomy_task_dispatch_has_budget_day",
        ),
        CheckConstraint(
            "completed_at IS NULL OR status IN "
            "('succeeded', 'failed', 'cancelled', 'stale', 'manual_review')",
            name="ai_agent_autonomy_task_completion_terminal",
        ),
        Index(
            "ix_ai_agent_autonomy_tasks_due",
            "status",
            "not_before",
            "lease_until",
        ),
        Index(
            "ix_ai_agent_autonomy_tasks_owner_created",
            "owner_user_id",
            "created_at",
        ),
        Index(
            "ix_ai_agent_autonomy_tasks_source_message",
            "owner_user_id",
            "source_message_id",
        ),
    )
    __sensitive_fields__ = frozenset(
        {
            "idempotency_key",
            "action_idempotency_key",
            "source_message_identity",
            "target_upstream_uid",
            "generation_instruction",
            "lease_token",
            "runner_configuration_fingerprint",
        }
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    action_idempotency_key: Mapped[str | None] = mapped_column(String(160))
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_setting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    runner_setting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    runner_configuration_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    source_message_identity: Mapped[str | None] = mapped_column(String(256))
    schedule_slot: Mapped[str | None] = mapped_column(String(96))
    target_upstream_uid: Mapped[str | None] = mapped_column(String(128))
    generation_instruction: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agent_runs.id", ondelete="SET NULL")
    )
    action_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_action_executions.id", ondelete="SET NULL"),
    )
    stable_error_code: Mapped[str | None] = mapped_column(String(64))
    result_id: Mapped[str | None] = mapped_column(String(256))
    budget_day: Mapped[date | None] = mapped_column(Date)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(160))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_unknown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class AiAgentAutonomyDailyUsage(
    UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base
):
    """按用户本地日期锁定的自治动作硬预算账本。"""

    __tablename__ = "ai_agent_autonomy_daily_usage"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "usage_date",
            name="uq_ai_agent_autonomy_daily_usage_owner_date",
        ),
        CheckConstraint(
            "total_actions >= 0 AND reply_actions >= 0 AND post_actions >= 0 "
            "AND relationship_actions >= 0 AND failed_actions >= 0 "
            "AND outcome_unknown_actions >= 0",
            name="ai_agent_autonomy_daily_usage_nonnegative",
        ),
        CheckConstraint(
            "total_actions = reply_actions + post_actions + relationship_actions",
            name="ai_agent_autonomy_daily_usage_total_consistent",
        ),
        Index(
            "ix_ai_agent_autonomy_daily_usage_owner_date",
            "owner_user_id",
            "usage_date",
        ),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_actions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reply_actions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    post_actions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    relationship_actions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_actions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    outcome_unknown_actions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncCursor(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "source", "stream", name="uq_sync_cursors_owner_stream"),
        Index("ix_sync_cursors_due", "next_sync_at", "last_succeeded_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    stream: Mapped[str] = mapped_column(String(96), nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class OperationOutbox(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "operation_outbox"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "operation_type", "idempotency_key", name="uq_outbox_idempotency"),
        CheckConstraint("attempt_count >= 0", name="outbox_attempt_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="outbox_max_attempt_positive"),
        Index("ix_outbox_claim", "status", "available_at", "locked_until"),
        Index("ix_outbox_owner_created", "owner_user_id", "created_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(96), nullable=False)
    aggregate_type: Mapped[str | None] = mapped_column(String(80))
    aggregate_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8, server_default="8")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class SystemStorageQuota(SerializableMixin, Base):
    """单行配额账本；媒体写入事务必须先 ``SELECT ... FOR UPDATE``。"""

    __tablename__ = "system_storage_quota"
    __table_args__ = (
        CheckConstraint("id = 1", name="system_storage_singleton"),
        CheckConstraint("quota_bytes > 0", name="system_storage_quota_positive"),
        CheckConstraint("used_bytes >= 0", name="system_storage_used_nonnegative"),
        CheckConstraint("used_bytes <= quota_bytes", name="system_storage_within_quota"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default="1")
    quota_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=8 * GIBIBYTE, server_default=str(8 * GIBIBYTE)
    )
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class SocialPost(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Web-local canonical dynamic post.

    ``public_id`` is the only identifier exposed by the Web API.  Legacy APK
    identifiers live in ``legacy_social_bindings`` so an upstream identifier
    can never become the canonical primary key.
    """

    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_social_posts_public_id"),
        UniqueConstraint(
            "id",
            "author_user_id",
            name="uq_social_posts_id_author",
        ),
        UniqueConstraint(
            "author_user_id",
            "client_request_id",
            name="uq_social_posts_author_client_request",
        ),
        CheckConstraint(
            "source IN ('web-local', 'legacy-import')",
            name="social_post_source_valid",
        ),
        CheckConstraint(
            "(source = 'web-local' AND author_user_id IS NOT NULL "
            "AND client_request_id IS NOT NULL) "
            "OR source = 'legacy-import'",
            name="social_post_local_author_required",
        ),
        CheckConstraint(
            "length(payload_digest) = 64", name="social_post_digest_length"
        ),
        CheckConstraint(
            "visibility IN ('public', 'followers', 'private')",
            name="social_post_visibility_valid",
        ),
        CheckConstraint(
            "comment_policy IN ('open', 'followers', 'disabled')",
            name="social_post_comment_policy_valid",
        ),
        CheckConstraint(
            "status IN ('published', 'hidden', 'deleted')",
            name="social_post_status_valid",
        ),
        CheckConstraint(
            "length(btrim(coalesce(body, ''))) > 0 OR media <> '{}'::jsonb",
            name="social_post_content_nonempty",
        ),
        CheckConstraint("like_count >= 0", name="social_post_like_count_nonnegative"),
        CheckConstraint(
            "comment_count >= 0", name="social_post_comment_count_nonnegative"
        ),
        CheckConstraint("view_count >= 0", name="social_post_view_count_nonnegative"),
        CheckConstraint(
            "report_count >= 0", name="social_post_report_count_nonnegative"
        ),
        CheckConstraint("version >= 1", name="social_post_version_positive"),
        Index(
            "ix_social_posts_feed",
            "status",
            "visibility",
            "published_at",
            "id",
        ),
        Index(
            "ix_social_posts_author_published",
            "author_user_id",
            "status",
            "is_pinned",
            "published_at",
        ),
        Index("ix_social_posts_source_created", "source", "source_created_at"),
    )

    public_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: f"pst_{uuid.uuid4().hex}",
        server_default=text("'pst_' || replace(gen_random_uuid()::text, '-', '')"),
    )
    client_request_id: Mapped[str | None] = mapped_column(String(160))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    author_upstream_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    author_display_name: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    author_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="web-local", server_default="web-local"
    )
    title: Mapped[str | None] = mapped_column(String(240))
    body: Mapped[str | None] = mapped_column(Text)
    media: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    visibility: Mapped[str] = mapped_column(
        String(24), nullable=False, default="public", server_default="public"
    )
    comment_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, default="open", server_default="open"
    )
    hide_comments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="published", server_default="published"
    )
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    like_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    comment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    view_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    report_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class SocialComment(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Canonical comment or one-level reply for a social post."""

    __tablename__ = "social_comments"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_social_comments_public_id"),
        UniqueConstraint(
            "author_user_id",
            "client_request_id",
            name="uq_social_comments_author_client_request",
        ),
        CheckConstraint(
            "source IN ('web-local', 'legacy-import')",
            name="social_comment_source_valid",
        ),
        CheckConstraint(
            "(source = 'web-local' AND author_user_id IS NOT NULL "
            "AND client_request_id IS NOT NULL) "
            "OR source = 'legacy-import'",
            name="social_comment_local_author_required",
        ),
        CheckConstraint(
            "length(payload_digest) = 64", name="social_comment_digest_length"
        ),
        CheckConstraint(
            "status IN ('active', 'hidden', 'deleted')",
            name="social_comment_status_valid",
        ),
        CheckConstraint(
            "length(btrim(body)) > 0", name="social_comment_body_nonempty"
        ),
        CheckConstraint(
            "like_count >= 0", name="social_comment_like_count_nonnegative"
        ),
        CheckConstraint(
            "reply_count >= 0", name="social_comment_reply_count_nonnegative"
        ),
        Index(
            "ix_social_comments_post_created",
            "post_id",
            "status",
            "source_created_at",
            "id",
        ),
        Index("ix_social_comments_parent_created", "parent_comment_id", "source_created_at"),
        Index("ix_social_comments_author_created", "author_user_id", "source_created_at"),
    )

    public_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: f"cmt_{uuid.uuid4().hex}",
        server_default=text("'cmt_' || replace(gen_random_uuid()::text, '-', '')"),
    )
    client_request_id: Mapped[str | None] = mapped_column(String(160))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_comment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("social_comments.id", ondelete="SET NULL")
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    author_upstream_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    author_display_name: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    author_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="web-local", server_default="web-local"
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    like_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reply_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class SocialReaction(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Idempotent like state; rows are retained when a user unlikes."""

    __tablename__ = "social_reactions"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id", "post_id", "reaction_type", name="uq_social_reactions_post_actor"
        ),
        UniqueConstraint(
            "actor_user_id",
            "comment_id",
            "reaction_type",
            name="uq_social_reactions_comment_actor",
        ),
        CheckConstraint(
            "(post_id IS NOT NULL AND comment_id IS NULL) OR "
            "(post_id IS NULL AND comment_id IS NOT NULL)",
            name="social_reaction_exactly_one_target",
        ),
        CheckConstraint(
            "reaction_type = 'like'", name="social_reaction_type_supported"
        ),
        Index("ix_social_reactions_post_active", "post_id", "active"),
        Index("ix_social_reactions_comment_active", "comment_id", "active"),
    )

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("social_posts.id", ondelete="CASCADE")
    )
    comment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("social_comments.id", ondelete="CASCADE")
    )
    reaction_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default="like", server_default="like"
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class SocialTopic(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "social_topics"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_social_topics_public_id"),
        UniqueConstraint("normalized_name", name="uq_social_topics_normalized_name"),
        CheckConstraint(
            "status IN ('active', 'hidden')", name="social_topic_status_valid"
        ),
        CheckConstraint(
            "length(btrim(name)) > 0", name="social_topic_name_nonempty"
        ),
        CheckConstraint(
            "post_count >= 0", name="social_topic_post_count_nonnegative"
        ),
        Index("ix_social_topics_status_posts", "status", "post_count", "name"),
    )

    public_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: f"tpc_{uuid.uuid4().hex}",
        server_default=text("'tpc_' || replace(gen_random_uuid()::text, '-', '')"),
    )
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    post_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class SocialPostTopic(UUIDPrimaryKeyMixin, SerializableMixin, Base):
    __tablename__ = "social_post_topics"
    __table_args__ = (
        UniqueConstraint("post_id", "topic_id", name="uq_social_post_topics_post_topic"),
        CheckConstraint("position >= 0", name="social_post_topic_position_nonnegative"),
        Index("ix_social_post_topics_topic_created", "topic_id", "created_at"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_topics.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class SocialReport(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "social_reports"
    __table_args__ = (
        UniqueConstraint(
            "reporter_user_id", "idempotency_key", name="uq_social_reports_reporter_key"
        ),
        UniqueConstraint("public_id", name="uq_social_reports_public_id"),
        CheckConstraint(
            "(post_id IS NOT NULL AND comment_id IS NULL) OR "
            "(post_id IS NULL AND comment_id IS NOT NULL)",
            name="social_report_exactly_one_target",
        ),
        CheckConstraint(
            "status IN ('pending', 'reviewed', 'dismissed', 'actioned')",
            name="social_report_status_valid",
        ),
        CheckConstraint(
            "length(btrim(reason_code)) > 0", name="social_report_reason_nonempty"
        ),
        Index("ix_social_reports_queue", "status", "created_at"),
        Index("ix_social_reports_post", "post_id", "created_at"),
        Index("ix_social_reports_comment", "comment_id", "created_at"),
    )

    public_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: f"rpt_{uuid.uuid4().hex}",
        server_default=text("'rpt_' || replace(gen_random_uuid()::text, '-', '')"),
    )
    reporter_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("social_posts.id", ondelete="CASCADE")
    )
    comment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("social_comments.id", ondelete="CASCADE")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_text: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    resolution: Mapped[str | None] = mapped_column(String(1000))
    reviewed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class SocialPostViewEvent(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """At most one canonical view increment per viewer, post and UTC day."""

    __tablename__ = "social_post_views"
    __table_args__ = (
        UniqueConstraint(
            "post_id", "viewer_user_id", "viewed_on", name="uq_social_post_views_daily"
        ),
        CheckConstraint("view_count >= 1", name="social_post_view_count_positive"),
        CheckConstraint(
            "first_viewed_at <= last_viewed_at",
            name="social_post_view_time_order_valid",
        ),
        Index("ix_social_post_views_post_last", "post_id", "last_viewed_at"),
        Index("ix_social_post_views_viewer_last", "viewer_user_id", "last_viewed_at"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("social_posts.id", ondelete="CASCADE"), nullable=False
    )
    viewer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    viewed_on: Mapped[date] = mapped_column(Date, nullable=False)
    view_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    first_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LegacySocialBinding(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Auditable mapping from an APK object ID to a local canonical object."""

    __tablename__ = "legacy_social_bindings"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "entity_type",
            "upstream_id",
            name="uq_legacy_social_bindings_source",
        ),
        UniqueConstraint(
            "provider",
            "entity_type",
            "local_entity_id",
            name="uq_legacy_social_bindings_local",
        ),
        CheckConstraint(
            "entity_type IN ('post', 'comment', 'topic')",
            name="legacy_social_binding_entity_valid",
        ),
        CheckConstraint(
            "import_scope IN ('owner', 'visible-history')",
            name="legacy_social_binding_scope_valid",
        ),
        CheckConstraint(
            "import_scope <> 'visible-history' OR "
            "source_created_at >= created_at - interval '180 days'",
            name="legacy_social_binding_visible_history_180_days",
        ),
        CheckConstraint(
            "length(payload_digest) = 64", name="legacy_social_binding_digest_length"
        ),
        Index(
            "ix_legacy_social_bindings_importer_created",
            "imported_by_user_id",
            "created_at",
        ),
        Index(
            "ix_legacy_social_bindings_source_created",
            "provider",
            "entity_type",
            "source_created_at",
        ),
    )

    provider: Mapped[str] = mapped_column(
        String(40), nullable=False, default="beibeiwu", server_default="beibeiwu"
    )
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    upstream_id: Mapped[str] = mapped_column(String(256), nullable=False)
    local_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    local_public_id: Mapped[str] = mapped_column(String(40), nullable=False)
    imported_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    import_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class UserDiscoveryProfile(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """City-level discovery projection; precise coordinates are never stored."""

    __tablename__ = "user_discovery_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_discovery_profiles_user"),
        CheckConstraint(
            "gender IN ('male', 'female', 'other', 'unspecified')",
            name="user_discovery_profile_gender_valid",
        ),
        CheckConstraint(
            "age IS NULL OR age BETWEEN 18 AND 120",
            name="user_discovery_profile_age_valid",
        ),
        CheckConstraint(
            "NOT discoverable OR city_code IS NOT NULL",
            name="user_discovery_profile_city_required",
        ),
        Index(
            "ix_user_discovery_profiles_city_active",
            "city_code",
            "discoverable",
            "last_active_at",
        ),
        Index(
            "ix_user_discovery_profiles_filters",
            "gender",
            "profile_property",
            "age",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    city_code: Mapped[str | None] = mapped_column(String(32))
    city_name: Mapped[str | None] = mapped_column(String(80))
    gender: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unspecified", server_default="unspecified"
    )
    profile_property: Mapped[str | None] = mapped_column(String(80))
    age: Mapped[int | None] = mapped_column(SmallInteger)
    discoverable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatchPreference(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "match_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_match_preferences_user"),
        CheckConstraint(
            "city_scope IN ('same-city', 'anywhere')",
            name="match_preference_city_scope_valid",
        ),
        CheckConstraint(
            "gender_preference IN ('any', 'male', 'female', 'other')",
            name="match_preference_gender_valid",
        ),
        CheckConstraint(
            "min_age BETWEEN 18 AND 120 AND max_age BETWEEN 18 AND 120 "
            "AND min_age <= max_age",
            name="match_preference_age_range_valid",
        ),
        CheckConstraint("version >= 1", name="match_preference_version_positive"),
        Index("ix_match_preferences_enabled", "enabled", "updated_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    city_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default="same-city", server_default="same-city"
    )
    gender_preference: Mapped[str] = mapped_column(
        String(16), nullable=False, default="any", server_default="any"
    )
    property_preference: Mapped[str | None] = mapped_column(String(80))
    min_age: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=18, server_default="18"
    )
    max_age: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=120, server_default="120"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class MatchQueueEntry(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    __tablename__ = "match_queue_entries"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_match_queue_entries_id_user"),
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_match_queue_entries_user_key"
        ),
        CheckConstraint(
            "queue_kind = 'text'", name="match_queue_entry_kind_supported"
        ),
        CheckConstraint(
            "status IN ('waiting', 'matched', 'cancelled', 'expired')",
            name="match_queue_entry_status_valid",
        ),
        CheckConstraint(
            "expires_at > enqueued_at", name="match_queue_entry_expiry_valid"
        ),
        CheckConstraint(
            "preference_version >= 1",
            name="match_queue_entry_preference_version_positive",
        ),
        Index(
            "uq_match_queue_entries_user_waiting",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'waiting'"),
        ),
        Index(
            "ix_match_queue_entries_claim",
            "queue_kind",
            "status",
            "city_code",
            "enqueued_at",
        ),
        Index("ix_match_queue_entries_expiry", "status", "expires_at"),
    )

    public_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        unique=True,
        default=lambda: f"mqe_{uuid.uuid4().hex}",
        server_default=text("'mqe_' || replace(gen_random_uuid()::text, '-', '')"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    queue_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="text", server_default="text"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting", server_default="waiting"
    )
    city_code: Mapped[str | None] = mapped_column(String(32))
    preference_version: Mapped[int] = mapped_column(Integer, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatchResult(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """Canonical ordered user pair for an idempotent text-match result."""

    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_match_results_public_id"),
        UniqueConstraint("match_key", name="uq_match_results_match_key"),
        UniqueConstraint(
            "user_low_queue_entry_id",
            "user_high_queue_entry_id",
            name="uq_match_results_queue_pair",
        ),
        ForeignKeyConstraint(
            ("user_low_queue_entry_id", "user_low_id"),
            ("match_queue_entries.id", "match_queue_entries.user_id"),
            ondelete="RESTRICT",
            name="fk_match_results_low_queue_user",
        ),
        ForeignKeyConstraint(
            ("user_high_queue_entry_id", "user_high_id"),
            ("match_queue_entries.id", "match_queue_entries.user_id"),
            ondelete="RESTRICT",
            name="fk_match_results_high_queue_user",
        ),
        CheckConstraint(
            "user_low_id < user_high_id", name="match_result_users_canonical_order"
        ),
        CheckConstraint(
            "user_low_queue_entry_id <> user_high_queue_entry_id",
            name="match_result_queue_entries_distinct",
        ),
        CheckConstraint(
            "initiated_by_user_id IN (user_low_id, user_high_id)",
            name="match_result_initiator_is_party",
        ),
        CheckConstraint(
            "status IN ('active', 'dismissed', 'expired')",
            name="match_result_status_valid",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > matched_at",
            name="match_result_expiry_valid",
        ),
        Index("ix_match_results_low_matched", "user_low_id", "matched_at"),
        Index("ix_match_results_high_matched", "user_high_id", "matched_at"),
        Index("ix_match_results_status_expiry", "status", "expires_at"),
    )

    public_id: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: f"mch_{uuid.uuid4().hex}",
        server_default=text("'mch_' || replace(gen_random_uuid()::text, '-', '')"),
    )
    match_key: Mapped[str] = mapped_column(String(160), nullable=False)
    user_low_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    user_high_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    user_low_queue_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    user_high_queue_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaUploadIntent(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """用户绑定且一次性消费的 Web-native staging 上传意图。"""

    __tablename__ = "media_upload_intents"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_media_upload_intents_id_owner"),
        ForeignKeyConstraint(
            ["completed_asset_id"],
            ["media_assets.id"],
            use_alter=True,
            name="fk_media_upload_intents_completed_asset",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('image', 'audio', 'video', 'file')",
            name="media_upload_intent_kind_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'expired', 'cancelled')",
            name="media_upload_intent_status_valid",
        ),
        CheckConstraint(
            "expected_size_bytes > 0",
            name="media_upload_intent_size_positive",
        ),
        CheckConstraint(
            "(kind IN ('image', 'audio') AND expected_size_bytes <= 20971520) OR "
            "(kind = 'video' AND expected_size_bytes <= 104857600) OR "
            "(kind = 'file' AND expected_size_bytes <= 41943040)",
            name="media_upload_intent_kind_size_limit",
        ),
        CheckConstraint(
            "length(expected_sha256) = 64",
            name="media_upload_intent_sha256_length",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="media_upload_intent_expiry_valid",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_asset_id IS NOT NULL) OR "
            "(status <> 'completed' AND completed_asset_id IS NULL)",
            name="media_upload_intent_completion_consistent",
        ),
        CheckConstraint(
            "position('://' in staging_object_key) = 0",
            name="media_upload_intent_key_not_url",
        ),
        Index(
            "ix_media_upload_intents_owner_status",
            "owner_user_id",
            "status",
            "created_at",
        ),
        Index("ix_media_upload_intents_expiry", "status", "expires_at"),
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    deployment: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_content_type: Mapped[str] = mapped_column(String(160), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    staging_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    staging_namespace: Mapped[str] = mapped_column(String(160), nullable=False)
    staging_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class MediaAsset(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """完成真实媒体检测后位于 private 区的稳定媒体对象。"""

    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_media_assets_id_owner"),
        UniqueConstraint("upload_intent_id", name="uq_media_assets_upload_intent"),
        UniqueConstraint(
            "deployment",
            "private_bucket",
            "private_namespace",
            "private_object_key",
            name="uq_media_assets_private_object",
        ),
        ForeignKeyConstraint(
            ["upload_intent_id", "owner_user_id"],
            ["media_upload_intents.id", "media_upload_intents.owner_user_id"],
            name="fk_media_assets_upload_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('image', 'audio', 'video', 'file')",
            name="media_asset_kind_valid",
        ),
        CheckConstraint(
            "status IN ('available', 'deleted')",
            name="media_asset_status_valid",
        ),
        CheckConstraint("size_bytes > 0", name="media_asset_size_positive"),
        CheckConstraint("length(sha256) = 64", name="media_asset_sha256_length"),
        CheckConstraint(
            "(kind IN ('image', 'audio') AND size_bytes <= 20971520) OR "
            "(kind = 'video' AND size_bytes <= 104857600) OR "
            "(kind = 'file' AND size_bytes <= 41943040)",
            name="media_asset_kind_size_limit",
        ),
        CheckConstraint(
            "kind <> 'audio' OR "
            "(duration_seconds IS NOT NULL AND duration_seconds > 0 "
            "AND duration_seconds <= 60)",
            name="media_asset_audio_duration_valid",
        ),
        CheckConstraint(
            "kind <> 'video' OR (duration_seconds IS NOT NULL AND duration_seconds > 0)",
            name="media_asset_video_duration_valid",
        ),
        CheckConstraint(
            "kind <> 'image' OR (width IS NOT NULL AND width > 0 "
            "AND height IS NOT NULL AND height > 0)",
            name="media_asset_image_dimensions_valid",
        ),
        CheckConstraint(
            "position('://' in private_object_key) = 0",
            name="media_asset_key_not_url",
        ),
        Index("ix_media_assets_owner_status", "owner_user_id", "status", "created_at"),
        Index("ix_media_assets_expiry", "status", "expires_at"),
    )

    upload_intent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deployment: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    private_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    private_namespace: Mapped[str] = mapped_column(String(160), nullable=False)
    private_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="available", server_default="available"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAssetReference(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """资料/动态对 Web-native 私有媒体的当前 owner-bound 引用。

    表内只保存 canonical UUID 和 slot，不保存任意 URL。一个 asset 最多只能有
    一个当前资料/动态引用，因此以 asset UUID 生成的站内路径可在替换或释放后
    立即失效，不会被另一个资源的引用意外继续授权。
    """

    __tablename__ = "media_asset_references"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            name="uq_media_asset_references_asset",
        ),
        UniqueConstraint(
            "profile_user_id",
            "slot",
            name="uq_media_asset_references_profile_slot",
        ),
        UniqueConstraint(
            "social_post_id",
            "slot",
            name="uq_media_asset_references_social_post_slot",
        ),
        ForeignKeyConstraint(
            ["asset_id", "owner_user_id"],
            ["media_assets.id", "media_assets.owner_user_id"],
            name="fk_media_asset_references_asset_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["social_post_id", "owner_user_id"],
            ["social_posts.id", "social_posts.author_user_id"],
            name="fk_media_asset_references_post_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(resource_type = 'profile' AND profile_user_id IS NOT NULL "
            "AND social_post_id IS NULL AND profile_user_id = owner_user_id "
            "AND slot = 'avatar') OR "
            "(resource_type = 'social_post' AND profile_user_id IS NULL "
            "AND social_post_id IS NOT NULL AND "
            "(slot IN ('video', 'cover') OR "
            "slot ~ '^pictures\\[(0|[1-9][0-9]*)\\]$'))",
            name="media_asset_reference_target_slot_valid",
        ),
        CheckConstraint(
            "position('://' in slot) = 0",
            name="media_asset_reference_slot_not_url",
        ),
        Index(
            "ix_media_asset_references_owner_resource",
            "owner_user_id",
            "resource_type",
        ),
        Index(
            "ix_media_asset_references_social_post",
            "social_post_id",
        ),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(24), nullable=False)
    profile_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    social_post_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    slot: Mapped[str] = mapped_column(String(80), nullable=False)


class MediaAttachment(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """canonical ChatMessage 的 URL-free 富媒体附件。"""

    __tablename__ = "media_attachments"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_media_attachments_message"),
        UniqueConstraint(
            "id", "recipient_user_id", name="uq_media_attachments_id_recipient"
        ),
        ForeignKeyConstraint(
            ["message_id", "thread_id"],
            ["chat_messages.id", "chat_messages.thread_id"],
            name="fk_media_attachments_message_thread",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["asset_id", "sender_user_id"],
            ["media_assets.id", "media_assets.owner_user_id"],
            name="fk_media_attachments_asset_sender",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('sent', 'revoked')",
            name="media_attachment_status_valid",
        ),
        CheckConstraint(
            "sender_user_id <> recipient_user_id",
            name="media_attachment_distinct_users",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status = 'sent' AND revoked_at IS NULL)",
            name="media_attachment_revoke_consistent",
        ),
        CheckConstraint(
            "(flash AND flash_display_seconds = 5) OR "
            "(NOT flash AND flash_display_seconds IS NULL)",
            name="media_attachment_flash_display_valid",
        ),
        CheckConstraint(
            "NOT (payload ? 'url') AND NOT (payload ? 'upload_url') "
            "AND NOT (payload ? 'presigned_url')",
            name="media_attachment_payload_url_free",
        ),
        Index("ix_media_attachments_thread_sent", "thread_id", "sent_at", "id"),
        Index("ix_media_attachments_recipient", "recipient_user_id", "status"),
        Index("ix_media_attachments_asset", "asset_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sender_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    client_message_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sender_upstream_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    recipient_upstream_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="sent", server_default="sent"
    )
    flash: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    flash_display_seconds: Mapped[int | None] = mapped_column(SmallInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaFlashClaim(UUIDPrimaryKeyMixin, TimestampMixin, SerializableMixin, Base):
    """闪照全局一次领取记录；访问 URL 不进入数据库。"""

    __tablename__ = "media_flash_claims"
    __table_args__ = (
        UniqueConstraint("attachment_id", name="uq_media_flash_claims_attachment"),
        ForeignKeyConstraint(
            ["attachment_id", "claimant_user_id"],
            ["media_attachments.id", "media_attachments.recipient_user_id"],
            name="fk_media_flash_claims_attachment_recipient",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "display_until = claimed_at + interval '5 seconds'",
            name="media_flash_claim_display_five_seconds",
        ),
        Index("ix_media_flash_claims_claimant", "claimant_user_id", "claimed_at"),
    )

    attachment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    claimant_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    display_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
