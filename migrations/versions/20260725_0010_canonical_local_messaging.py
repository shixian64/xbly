"""Add canonical Web-local messaging and non-blocking TIM outbox.

Revision ID: 20260725_0010
Revises: 20260725_0009
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0010"
down_revision: Union[str, Sequence[str], None] = "20260725_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_threads",
        sa.Column("kind", sa.String(length=32), server_default="direct", nullable=False),
        sa.Column("direct_key", sa.String(length=96), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind = 'direct'",
            name=op.f("ck_chat_threads_chat_thread_direct_only"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_chat_threads_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_threads"),
        sa.UniqueConstraint("direct_key", name="uq_chat_threads_direct_key"),
    )
    op.create_index("ix_chat_threads_last_message", "chat_threads", ["last_message_at"], unique=False)

    op.create_table(
        "chat_members",
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=24), server_default="member", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unread_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "unread_count >= 0",
            name=op.f("ck_chat_members_chat_member_unread_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["chat_threads.id"], name="fk_chat_members_thread_id_chat_threads", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_chat_members_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_members"),
        sa.UniqueConstraint("thread_id", "user_id", name="uq_chat_members_thread_user"),
    )
    op.create_index("ix_chat_members_user_active", "chat_members", ["user_id", "left_at"], unique=False)

    op.create_table(
        "chat_messages",
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("sender_user_id", sa.UUID(), nullable=False),
        sa.Column("client_message_id", sa.String(length=160), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("message_type", sa.String(length=40), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="accepted", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "message_type <> 'text' OR length(btrim(coalesce(body, ''))) > 0",
            name=op.f("ck_chat_messages_chat_message_text_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["chat_threads.id"], name="fk_chat_messages_thread_id_chat_threads", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"], ["users.id"], name="fk_chat_messages_sender_user_id_users", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
        sa.UniqueConstraint("sender_user_id", "client_message_id", name="uq_chat_messages_sender_client"),
        sa.UniqueConstraint("id", "thread_id", name="uq_chat_messages_id_thread"),
    )
    op.create_index(
        "ix_chat_messages_thread_occurred", "chat_messages", ["thread_id", "occurred_at", "id"], unique=False
    )
    op.create_index("ix_chat_messages_retention", "chat_messages", ["retention_expires_at"], unique=False)

    op.create_table(
        "message_receipts",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("receipt_type", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"], name="fk_message_receipts_message_id_chat_messages", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_message_receipts_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_receipts"),
        sa.UniqueConstraint(
            "message_id", "user_id", "receipt_type", name="uq_message_receipts_message_user_type"
        ),
    )
    op.create_index(
        "ix_message_receipts_user_occurred", "message_receipts", ["user_id", "occurred_at"], unique=False
    )

    op.create_table(
        "message_deliveries",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("target_key", sa.String(length=256), nullable=False),
        sa.Column("target_user_id", sa.UUID(), nullable=True),
        sa.Column("target_upstream_uid", sa.String(length=128), nullable=True),
        sa.Column("required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="8", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f(
                "ck_message_deliveries_message_delivery_attempt_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name=op.f(
                "ck_message_deliveries_message_delivery_max_attempt_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"], name="fk_message_deliveries_message_id_chat_messages", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], name="fk_message_deliveries_target_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_deliveries"),
        sa.UniqueConstraint("message_id", "channel", "target_key", name="uq_message_deliveries_message_target"),
    )
    op.create_index(
        "ix_message_deliveries_claim",
        "message_deliveries",
        ["channel", "status", "available_at", "locked_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("message_deliveries")
    op.drop_table("message_receipts")
    op.drop_table("chat_messages")
    op.drop_table("chat_members")
    op.drop_table("chat_threads")
