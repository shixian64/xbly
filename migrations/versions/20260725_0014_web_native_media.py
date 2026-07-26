"""Add Web-native private media uploads, attachments and flash claims.

Revision ID: 20260725_0014
Revises: 20260725_0013
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0014"
down_revision: Union[str, Sequence[str], None] = "20260725_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_primary_key() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "media_upload_intents",
        _uuid_primary_key(),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("declared_content_type", sa.String(length=160), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("staging_bucket", sa.String(length=128), nullable=False),
        sa.Column("staging_namespace", sa.String(length=160), nullable=False),
        sa.Column("staging_object_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "kind IN ('image', 'audio', 'video', 'file')",
            name=op.f("ck_media_upload_intents_media_upload_intent_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'expired', 'cancelled')",
            name=op.f("ck_media_upload_intents_media_upload_intent_status_valid"),
        ),
        sa.CheckConstraint(
            "expected_size_bytes > 0",
            name=op.f("ck_media_upload_intents_media_upload_intent_size_positive"),
        ),
        sa.CheckConstraint(
            "(kind IN ('image', 'audio') AND expected_size_bytes <= 20971520) OR "
            "(kind = 'video' AND expected_size_bytes <= 104857600) OR "
            "(kind = 'file' AND expected_size_bytes <= 41943040)",
            name=op.f("ck_media_upload_intents_media_upload_intent_kind_size_limit"),
        ),
        sa.CheckConstraint(
            "length(expected_sha256) = 64",
            name=op.f("ck_media_upload_intents_media_upload_intent_sha256_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_media_upload_intents_media_upload_intent_expiry_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_asset_id IS NOT NULL) OR "
            "(status <> 'completed' AND completed_asset_id IS NULL)",
            name=op.f("ck_media_upload_intents_media_upload_intent_completion_consistent"),
        ),
        sa.CheckConstraint(
            "position('://' in staging_object_key) = 0",
            name=op.f("ck_media_upload_intents_media_upload_intent_key_not_url"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_media_upload_intents_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_upload_intents")),
        sa.UniqueConstraint(
            "id",
            "owner_user_id",
            name=op.f("uq_media_upload_intents_id_owner"),
        ),
    )
    op.create_index(
        "ix_media_upload_intents_owner_status",
        "media_upload_intents",
        ["owner_user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_upload_intents_expiry",
        "media_upload_intents",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "media_assets",
        _uuid_primary_key(),
        sa.Column("upload_intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=160), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("private_bucket", sa.String(length=128), nullable=False),
        sa.Column("private_namespace", sa.String(length=160), nullable=False),
        sa.Column("private_object_key", sa.String(length=1024), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="available", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "kind IN ('image', 'audio', 'video', 'file')",
            name=op.f("ck_media_assets_media_asset_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('available', 'deleted')",
            name=op.f("ck_media_assets_media_asset_status_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name=op.f("ck_media_assets_media_asset_size_positive"),
        ),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name=op.f("ck_media_assets_media_asset_sha256_length"),
        ),
        sa.CheckConstraint(
            "(kind IN ('image', 'audio') AND size_bytes <= 20971520) OR "
            "(kind = 'video' AND size_bytes <= 104857600) OR "
            "(kind = 'file' AND size_bytes <= 41943040)",
            name=op.f("ck_media_assets_media_asset_kind_size_limit"),
        ),
        sa.CheckConstraint(
            "kind <> 'audio' OR (duration_seconds IS NOT NULL "
            "AND duration_seconds > 0 AND duration_seconds <= 60)",
            name=op.f("ck_media_assets_media_asset_audio_duration_valid"),
        ),
        sa.CheckConstraint(
            "kind <> 'video' OR (duration_seconds IS NOT NULL AND duration_seconds > 0)",
            name=op.f("ck_media_assets_media_asset_video_duration_valid"),
        ),
        sa.CheckConstraint(
            "kind <> 'image' OR (width IS NOT NULL AND width > 0 "
            "AND height IS NOT NULL AND height > 0)",
            name=op.f("ck_media_assets_media_asset_image_dimensions_valid"),
        ),
        sa.CheckConstraint(
            "position('://' in private_object_key) = 0",
            name=op.f("ck_media_assets_media_asset_key_not_url"),
        ),
        sa.ForeignKeyConstraint(
            ["upload_intent_id", "owner_user_id"],
            ["media_upload_intents.id", "media_upload_intents.owner_user_id"],
            name=op.f("fk_media_assets_upload_owner"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
        sa.UniqueConstraint(
            "id", "owner_user_id", name=op.f("uq_media_assets_id_owner")
        ),
        sa.UniqueConstraint(
            "upload_intent_id", name=op.f("uq_media_assets_upload_intent")
        ),
        sa.UniqueConstraint(
            "deployment",
            "private_bucket",
            "private_namespace",
            "private_object_key",
            name=op.f("uq_media_assets_private_object"),
        ),
    )
    op.create_index(
        "ix_media_assets_owner_status",
        "media_assets",
        ["owner_user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_assets_expiry",
        "media_assets",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_media_upload_intents_completed_asset",
        "media_upload_intents",
        "media_assets",
        ["completed_asset_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "media_attachments",
        _uuid_primary_key(),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_message_id", sa.String(length=160), nullable=False),
        sa.Column("sender_upstream_uid", sa.String(length=128), nullable=False),
        sa.Column("recipient_upstream_uid", sa.String(length=128), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="sent", nullable=False),
        sa.Column("flash", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flash_display_seconds", sa.SmallInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('sent', 'revoked')",
            name=op.f("ck_media_attachments_media_attachment_status_valid"),
        ),
        sa.CheckConstraint(
            "sender_user_id <> recipient_user_id",
            name=op.f("ck_media_attachments_media_attachment_distinct_users"),
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status = 'sent' AND revoked_at IS NULL)",
            name=op.f("ck_media_attachments_media_attachment_revoke_consistent"),
        ),
        sa.CheckConstraint(
            "(flash AND flash_display_seconds = 5) OR "
            "(NOT flash AND flash_display_seconds IS NULL)",
            name=op.f("ck_media_attachments_media_attachment_flash_display_valid"),
        ),
        sa.CheckConstraint(
            "NOT (payload ? 'url') AND NOT (payload ? 'upload_url') "
            "AND NOT (payload ? 'presigned_url')",
            name=op.f("ck_media_attachments_media_attachment_payload_url_free"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "thread_id"],
            ["chat_messages.id", "chat_messages.thread_id"],
            name=op.f("fk_media_attachments_message_thread"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "sender_user_id"],
            ["media_assets.id", "media_assets.owner_user_id"],
            name=op.f("fk_media_attachments_asset_sender"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name=op.f("fk_media_attachments_recipient_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_attachments")),
        sa.UniqueConstraint(
            "message_id", name=op.f("uq_media_attachments_message")
        ),
        sa.UniqueConstraint(
            "id",
            "recipient_user_id",
            name=op.f("uq_media_attachments_id_recipient"),
        ),
    )
    op.create_index(
        "ix_media_attachments_thread_sent",
        "media_attachments",
        ["thread_id", "sent_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_media_attachments_recipient",
        "media_attachments",
        ["recipient_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_media_attachments_asset",
        "media_attachments",
        ["asset_id"],
        unique=False,
    )

    op.create_table(
        "media_flash_claims",
        _uuid_primary_key(),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claimant_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("display_until", sa.DateTime(timezone=True), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "display_until = claimed_at + interval '5 seconds'",
            name=op.f("ck_media_flash_claims_media_flash_claim_display_five_seconds"),
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id", "claimant_user_id"],
            ["media_attachments.id", "media_attachments.recipient_user_id"],
            name=op.f("fk_media_flash_claims_attachment_recipient"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_flash_claims")),
        sa.UniqueConstraint(
            "attachment_id", name=op.f("uq_media_flash_claims_attachment")
        ),
    )
    op.create_index(
        "ix_media_flash_claims_claimant",
        "media_flash_claims",
        ["claimant_user_id", "claimed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("media_flash_claims")
    op.drop_table("media_attachments")
    op.drop_constraint(
        "fk_media_upload_intents_completed_asset",
        "media_upload_intents",
        type_="foreignkey",
    )
    op.drop_table("media_assets")
    op.drop_table("media_upload_intents")
