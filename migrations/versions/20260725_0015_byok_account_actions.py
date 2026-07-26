"""Add administrator-gated BYOK account action execution persistence.

Revision ID: 20260725_0015
Revises: 20260725_0014
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0015"
down_revision: Union[str, Sequence[str], None] = "20260725_0014"
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


def _replace_agent_run_type_constraint(*, include_reply_send: bool) -> None:
    op.drop_constraint(
        op.f("ck_ai_agent_runs_ai_agent_run_type_valid"),
        "ai_agent_runs",
        type_="check",
    )
    allowed = (
        "('connection_test', 'style_analysis', 'reply_draft', 'reply_send')"
        if include_reply_send
        else "('connection_test', 'style_analysis', 'reply_draft')"
    )
    op.create_check_constraint(
        op.f("ck_ai_agent_runs_ai_agent_run_type_valid"),
        "ai_agent_runs",
        f"run_type IN {allowed}",
    )


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "byok_account_actions_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_model_runner_system_settings",
        sa.Column(
            "account_actions_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    _replace_agent_run_type_constraint(include_reply_send=True)

    op.create_table(
        "ai_agent_execution_settings",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "user_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "auto_send_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allowed_actions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "NOT auto_send_enabled OR user_enabled",
            name=op.f(
                "ck_ai_agent_execution_settings_"
                "ai_agent_execution_setting_auto_send_requires_enabled"
            ),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_actions) = 'array'",
            name=op.f(
                "ck_ai_agent_execution_settings_"
                "ai_agent_execution_setting_allowed_actions_array"
            ),
        ),
        sa.CheckConstraint(
            "allowed_actions <@ "
            "'[\"send_private_message\", \"publish_text_post\", "
            "\"follow_user\", \"unfollow_user\"]'::jsonb",
            name=op.f(
                "ck_ai_agent_execution_settings_"
                "ai_agent_execution_setting_allowed_actions_supported"
            ),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f(
                "ck_ai_agent_execution_settings_"
                "ai_agent_execution_setting_version_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f(
                "fk_ai_agent_execution_settings_owner_user_id_users"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_ai_agent_execution_settings")
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            name=op.f("uq_ai_agent_execution_settings_owner"),
        ),
    )

    op.create_table(
        "ai_agent_action_executions",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "external_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="queued", nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("execution_setting_version", sa.Integer(), nullable=False),
        sa.Column(
            "target_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parameter_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("approval_source", sa.String(length=32), nullable=False),
        sa.Column("trigger_source", sa.String(length=32), nullable=False),
        sa.Column("stable_error_code", sa.String(length=64), nullable=True),
        sa.Column("external_result_id", sa.String(length=256), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "action_type IN ('send_private_message', 'publish_text_post', "
            "'follow_user', 'unfollow_user')",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_type_valid"
            ),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'cancelled', 'manual_review')",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_status_valid"
            ),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 160",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_idempotency_length"
            ),
        ),
        sa.CheckConstraint(
            "execution_setting_version >= 1",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_setting_version_positive"
            ),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_snapshot) = 'object'",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_target_object"
            ),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameter_snapshot) = 'object'",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_parameter_object"
            ),
        ),
        sa.CheckConstraint(
            "approval_source IN ('user_explicit', 'user_allowlist')",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_approval_source_valid"
            ),
        ),
        sa.CheckConstraint(
            "trigger_source IN ('user', 'model', 'schedule', 'system')",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_trigger_source_valid"
            ),
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND started_at IS NULL "
            "AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'manual_review') "
            "AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL "
            "AND cancelled_at IS NOT NULL)",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_timestamps_consistent"
            ),
        ),
        sa.CheckConstraint(
            "(status IN ('failed', 'cancelled', 'manual_review') "
            "AND stable_error_code IS NOT NULL) OR "
            "(status IN ('queued', 'running', 'succeeded') "
            "AND stable_error_code IS NULL)",
            name=op.f(
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_error_consistent"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f(
                "fk_ai_agent_action_executions_owner_user_id_users"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["external_account_id", "owner_user_id"],
            ["external_accounts.id", "external_accounts.user_id"],
            name="fk_ai_agent_action_executions_account_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_ai_agent_action_executions")
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "action_type",
            "idempotency_key",
            name="uq_ai_agent_action_executions_owner_action_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_agent_action_executions_owner_created",
        "ai_agent_action_executions",
        ["owner_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_agent_action_executions_owner_status_queued",
        "ai_agent_action_executions",
        ["owner_user_id", "status", "queued_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_agent_action_executions_queue",
        "ai_agent_action_executions",
        ["status", "queued_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("ai_agent_action_executions")
    op.drop_table("ai_agent_execution_settings")
    _replace_agent_run_type_constraint(include_reply_send=False)
    op.drop_column(
        "ai_model_runner_system_settings", "account_actions_enabled"
    )
    op.drop_column("users", "byok_account_actions_enabled")
