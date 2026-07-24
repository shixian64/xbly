"""生产数据模型。

所有用户业务表都显式携带 ``owner_user_id``，repository 也必须把 owner 条件
作为查询的一部分，避免仅凭上游 UID 或对象 ID 造成跨用户读取。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
        Index("ix_web_sessions_user_active", "user_id", "revoked_at", "absolute_expires_at"),
    )
    __sensitive_fields__ = frozenset({"sid_hash", "ip_hash"})

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    external_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sid_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
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
