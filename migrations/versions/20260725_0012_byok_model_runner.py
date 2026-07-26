"""Add the administrator-gated BYOK model runner foundation.

Revision ID: 20260725_0012
Revises: 20260725_0011
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0012"
down_revision: Union[str, Sequence[str], None] = "20260725_0011"
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
    op.add_column(
        "users",
        sa.Column(
            "byok_model_runner_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    op.create_table(
        "ai_model_runner_system_settings",
        sa.Column("id", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "updated_by_admin_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "id = 1",
            name=op.f(
                "ck_ai_model_runner_system_settings_ai_model_runner_system_singleton"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_admin_id"],
            ["admin_users.id"],
            name=op.f(
                "fk_ai_model_runner_system_settings_updated_by_admin_id_admin_users"
            ),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_ai_model_runner_system_settings")
        ),
    )
    op.create_index(
        op.f("ix_ai_model_runner_system_settings_updated_by_admin_id"),
        "ai_model_runner_system_settings",
        ["updated_by_admin_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "INSERT INTO ai_model_runner_system_settings "
            "(id, enabled) VALUES (1, false)"
        )
    )

    op.create_table(
        "ai_model_connections",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=40),
            server_default="openai_compatible",
            nullable=False,
        ),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column(
            "api_key_encrypted", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "last_test_status",
            sa.String(length=16),
            server_default="never",
            nullable=False,
        ),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "provider = 'openai_compatible'",
            name=op.f(
                "ck_ai_model_connections_ai_model_connection_provider_supported"
            ),
        ),
        sa.CheckConstraint(
            "last_test_status IN ('never', 'ok', 'failed')",
            name=op.f(
                "ck_ai_model_connections_ai_model_connection_test_status_valid"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_ai_model_connections_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_connections")),
        sa.UniqueConstraint(
            "id",
            "owner_user_id",
            name=op.f("uq_ai_model_connections_id_owner"),
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "label",
            name=op.f("uq_ai_model_connections_owner_label"),
        ),
    )
    op.create_index(
        "ix_ai_model_connections_owner_updated",
        "ai_model_connections",
        ["owner_user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "ai_agent_settings",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "active_connection_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "user_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "mode", sa.String(length=24), server_default="draft", nullable=False
        ),
        sa.Column("custom_instructions", sa.Text(), nullable=True),
        sa.Column(
            "temperature_milli", sa.Integer(), server_default="700", nullable=False
        ),
        sa.Column(
            "max_output_tokens", sa.Integer(), server_default="512", nullable=False
        ),
        sa.Column(
            "context_message_limit",
            sa.SmallInteger(),
            server_default="30",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "mode = 'draft'",
            name=op.f("ck_ai_agent_settings_ai_agent_setting_mode_supported"),
        ),
        sa.CheckConstraint(
            "temperature_milli BETWEEN 0 AND 2000",
            name=op.f("ck_ai_agent_settings_ai_agent_setting_temperature_valid"),
        ),
        sa.CheckConstraint(
            "max_output_tokens BETWEEN 64 AND 4096",
            name=op.f("ck_ai_agent_settings_ai_agent_setting_output_tokens_valid"),
        ),
        sa.CheckConstraint(
            "context_message_limit BETWEEN 1 AND 100",
            name=op.f("ck_ai_agent_settings_ai_agent_setting_context_limit_valid"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_ai_agent_settings_ai_agent_setting_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_ai_agent_settings_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["active_connection_id", "owner_user_id"],
            ["ai_model_connections.id", "ai_model_connections.owner_user_id"],
            name=op.f("fk_ai_agent_settings_connection_owner"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_settings")),
        sa.UniqueConstraint(
            "owner_user_id", name=op.f("uq_ai_agent_settings_owner")
        ),
    )

    op.create_table(
        "ai_style_profiles",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "model_connection_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "traits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_message_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "source_last_message_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "source_message_count >= 0",
            name=op.f(
                "ck_ai_style_profiles_ai_style_profile_source_count_nonnegative"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_ai_style_profiles_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_connection_id", "owner_user_id"],
            ["ai_model_connections.id", "ai_model_connections.owner_user_id"],
            name=op.f("fk_ai_style_profiles_connection_owner"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_style_profiles")),
        sa.UniqueConstraint(
            "owner_user_id", name=op.f("uq_ai_style_profiles_owner")
        ),
    )

    op.create_table(
        "ai_agent_runs",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="running", nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("model_snapshot", sa.String(length=160), nullable=False),
        sa.Column("peer_upstream_uid", sa.String(length=128), nullable=True),
        sa.Column(
            "source_message_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "prompt_char_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column(
            "output_char_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "run_type IN ('connection_test', 'style_analysis', 'reply_draft')",
            name=op.f("ck_ai_agent_runs_ai_agent_run_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_ai_agent_runs_ai_agent_run_status_valid"),
        ),
        sa.CheckConstraint(
            "source_message_count >= 0",
            name=op.f("ck_ai_agent_runs_ai_agent_run_source_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "prompt_char_count >= 0",
            name=op.f("ck_ai_agent_runs_ai_agent_run_prompt_chars_nonnegative"),
        ),
        sa.CheckConstraint(
            "output_char_count >= 0",
            name=op.f("ck_ai_agent_runs_ai_agent_run_output_chars_nonnegative"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_ai_agent_runs_ai_agent_run_input_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_ai_agent_runs_ai_agent_run_output_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_ai_agent_runs_ai_agent_run_latency_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_ai_agent_runs_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "owner_user_id"],
            ["ai_model_connections.id", "ai_model_connections.owner_user_id"],
            name=op.f("fk_ai_agent_runs_connection_owner"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_runs")),
        sa.UniqueConstraint(
            "owner_user_id",
            "run_type",
            "idempotency_key",
            name=op.f("uq_ai_agent_runs_owner_type_idempotency"),
        ),
    )
    op.create_index(
        "ix_ai_agent_runs_owner_created",
        "ai_agent_runs",
        ["owner_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_agent_runs_owner_status_created",
        "ai_agent_runs",
        ["owner_user_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("ai_agent_runs")
    op.drop_table("ai_style_profiles")
    op.drop_table("ai_agent_settings")
    op.drop_table("ai_model_connections")
    op.drop_table("ai_model_runner_system_settings")
    op.drop_column("users", "byok_model_runner_enabled")
