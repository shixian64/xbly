"""Add administrator-gated unattended social account agent persistence.

Revision ID: 20260725_0016
Revises: 20260725_0015
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0016"
down_revision: Union[str, Sequence[str], None] = "20260725_0015"
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


def _replace_agent_run_type_constraint(*, include_autonomy: bool) -> None:
    op.drop_constraint(
        op.f("ck_ai_agent_runs_ai_agent_run_type_valid"),
        "ai_agent_runs",
        type_="check",
    )
    allowed = (
        "('connection_test', 'style_analysis', 'reply_draft', 'reply_send', "
        "'autonomous_reply', 'autonomous_post', 'autonomous_plan')"
        if include_autonomy
        else "('connection_test', 'style_analysis', 'reply_draft', 'reply_send')"
    )
    op.create_check_constraint(
        op.f("ck_ai_agent_runs_ai_agent_run_type_valid"),
        "ai_agent_runs",
        f"run_type IN {allowed}",
    )


def _replace_action_execution_unknown_constraints(
    *, include_manual_review: bool
) -> None:
    status_name = op.f(
        "ck_ai_agent_action_executions_"
        "ai_agent_action_execution_status_valid"
    )
    timestamps_name = op.f(
        "ck_ai_agent_action_executions_"
        "ai_agent_action_execution_timestamps_consistent"
    )
    error_name = op.f(
        "ck_ai_agent_action_executions_"
        "ai_agent_action_execution_error_consistent"
    )
    for name in (status_name, timestamps_name, error_name):
        op.drop_constraint(
            name,
            "ai_agent_action_executions",
            type_="check",
        )
    terminal_statuses = (
        "'succeeded', 'failed', 'manual_review'"
        if include_manual_review
        else "'succeeded', 'failed'"
    )
    error_statuses = (
        "'failed', 'cancelled', 'manual_review'"
        if include_manual_review
        else "'failed', 'cancelled'"
    )
    allowed_statuses = (
        "'queued', 'running', 'succeeded', 'failed', 'cancelled', "
        "'manual_review'"
        if include_manual_review
        else "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
    )
    op.create_check_constraint(
        status_name,
        "ai_agent_action_executions",
        f"status IN ({allowed_statuses})",
    )
    op.create_check_constraint(
        timestamps_name,
        "ai_agent_action_executions",
        "(status = 'queued' AND started_at IS NULL "
        "AND completed_at IS NULL AND cancelled_at IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL "
        "AND completed_at IS NULL AND cancelled_at IS NULL) OR "
        f"(status IN ({terminal_statuses}) AND started_at IS NOT NULL "
        "AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
        "(status = 'cancelled' AND completed_at IS NOT NULL "
        "AND cancelled_at IS NOT NULL)",
    )
    op.create_check_constraint(
        error_name,
        "ai_agent_action_executions",
        f"(status IN ({error_statuses}) AND stable_error_code IS NOT NULL) OR "
        "(status IN ('queued', 'running', 'succeeded') "
        "AND stable_error_code IS NULL)",
    )


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "byok_autonomous_agent_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_model_runner_system_settings",
        sa.Column(
            "autonomous_agent_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    _replace_agent_run_type_constraint(include_autonomy=True)
    _replace_action_execution_unknown_constraints(include_manual_review=True)

    op.create_table(
        "ai_agent_autonomy_settings",
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
            "auto_reply_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "scheduled_post_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "managed_relationships_enabled",
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
        sa.Column(
            "operation_brief", sa.Text(), server_default="", nullable=False
        ),
        sa.Column(
            "managed_target_uids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(length=64), server_default="UTC", nullable=False),
        sa.Column(
            "active_start_minute", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "active_end_minute", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "minimum_action_interval_seconds",
            sa.Integer(),
            server_default="300",
            nullable=False,
        ),
        sa.Column(
            "daily_total_limit", sa.SmallInteger(), server_default="20", nullable=False
        ),
        sa.Column(
            "daily_reply_limit", sa.SmallInteger(), server_default="10", nullable=False
        ),
        sa.Column(
            "daily_post_limit", sa.SmallInteger(), server_default="1", nullable=False
        ),
        sa.Column(
            "daily_relationship_limit",
            sa.SmallInteger(),
            server_default="5",
            nullable=False,
        ),
        sa.Column(
            "post_interval_minutes", sa.Integer(), server_default="1440", nullable=False
        ),
        sa.Column(
            "consecutive_failure_limit",
            sa.SmallInteger(),
            server_default="3",
            nullable=False,
        ),
        sa.Column(
            "consecutive_failures", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_post_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halted_reason", sa.String(length=160), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_actions) = 'array'",
            name="ai_agent_autonomy_allowed_actions_array",
        ),
        sa.CheckConstraint(
            "allowed_actions <@ '[\"send_private_message\", "
            "\"publish_text_post\", \"follow_user\", \"unfollow_user\"]'::jsonb",
            name="ai_agent_autonomy_allowed_actions_supported",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(managed_target_uids) = 'array'",
            name="ai_agent_autonomy_targets_array",
        ),
        sa.CheckConstraint(
            "length(operation_brief) <= 4000",
            name="ai_agent_autonomy_brief_length",
        ),
        sa.CheckConstraint(
            "length(btrim(timezone)) BETWEEN 1 AND 64",
            name="ai_agent_autonomy_timezone_length",
        ),
        sa.CheckConstraint(
            "active_start_minute BETWEEN 0 AND 1439 "
            "AND active_end_minute BETWEEN 0 AND 1439",
            name="ai_agent_autonomy_active_minutes_valid",
        ),
        sa.CheckConstraint(
            "minimum_action_interval_seconds BETWEEN 60 AND 86400",
            name="ai_agent_autonomy_minimum_interval_valid",
        ),
        sa.CheckConstraint(
            "daily_total_limit BETWEEN 1 AND 200 "
            "AND daily_reply_limit BETWEEN 0 AND 200 "
            "AND daily_post_limit BETWEEN 0 AND 20 "
            "AND daily_relationship_limit BETWEEN 0 AND 100",
            name="ai_agent_autonomy_daily_limits_valid",
        ),
        sa.CheckConstraint(
            "post_interval_minutes BETWEEN 60 AND 10080",
            name="ai_agent_autonomy_post_interval_valid",
        ),
        sa.CheckConstraint(
            "consecutive_failure_limit BETWEEN 1 AND 20 "
            "AND consecutive_failures >= 0",
            name="ai_agent_autonomy_failure_limits_valid",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ai_agent_autonomy_version_positive"
        ),
        sa.CheckConstraint(
            "NOT auto_reply_enabled OR "
            "(user_enabled AND allowed_actions ? 'send_private_message' "
            "AND daily_reply_limit >= 1)",
            name="ai_agent_autonomy_reply_requires_action",
        ),
        sa.CheckConstraint(
            "NOT scheduled_post_enabled OR "
            "(user_enabled AND allowed_actions ? 'publish_text_post' "
            "AND daily_post_limit >= 1)",
            name="ai_agent_autonomy_post_requires_action",
        ),
        sa.CheckConstraint(
            "NOT managed_relationships_enabled OR "
            "(user_enabled AND "
            "(allowed_actions ? 'follow_user' OR allowed_actions ? 'unfollow_user') "
            "AND jsonb_array_length(managed_target_uids) > 0 "
            "AND daily_relationship_limit >= 1)",
            name="ai_agent_autonomy_relationship_requires_targets",
        ),
        sa.CheckConstraint(
            "NOT user_enabled OR auto_reply_enabled OR scheduled_post_enabled "
            "OR managed_relationships_enabled",
            name="ai_agent_autonomy_enabled_has_capability",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", name="uq_ai_agent_autonomy_settings_owner"
        ),
    )
    op.create_index(
        "ix_ai_agent_autonomy_settings_due",
        "ai_agent_autonomy_settings",
        ["user_enabled", "halted_at", "next_run_at"],
    )

    op.create_table(
        "ai_agent_autonomy_tasks",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "external_account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("action_idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("execution_setting_version", sa.Integer(), nullable=False),
        sa.Column("runner_setting_version", sa.Integer(), nullable=False),
        sa.Column(
            "model_connection_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "runner_configuration_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_message_identity", sa.String(length=256), nullable=True),
        sa.Column("schedule_slot", sa.String(length=96), nullable=True),
        sa.Column("target_upstream_uid", sa.String(length=128), nullable=True),
        sa.Column("generation_instruction", sa.Text(), server_default="", nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stable_error_code", sa.String(length=64), nullable=True),
        sa.Column("result_id", sa.String(length=256), nullable=True),
        sa.Column("budget_day", sa.Date(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=160), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome_unknown",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "task_type IN ('reply_to_message', 'scheduled_post', "
            "'follow_target', 'unfollow_target')",
            name="ai_agent_autonomy_task_type_valid",
        ),
        sa.CheckConstraint(
            "action_type IN ('send_private_message', 'publish_text_post', "
            "'follow_user', 'unfollow_user')",
            name="ai_agent_autonomy_task_action_valid",
        ),
        sa.CheckConstraint(
            "(task_type = 'reply_to_message' AND action_type = 'send_private_message') OR "
            "(task_type = 'scheduled_post' AND action_type = 'publish_text_post') OR "
            "(task_type = 'follow_target' AND action_type = 'follow_user') OR "
            "(task_type = 'unfollow_target' AND action_type = 'unfollow_user')",
            name="ai_agent_autonomy_task_action_matches_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'deferred', 'leased', 'generating', "
            "'dispatching', 'succeeded', 'failed', 'cancelled', 'stale', "
            "'manual_review')",
            name="ai_agent_autonomy_task_status_valid",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 160",
            name="ai_agent_autonomy_task_idempotency_length",
        ),
        sa.CheckConstraint(
            "policy_version >= 1 AND execution_setting_version >= 1 "
            "AND runner_setting_version >= 1",
            name="ai_agent_autonomy_task_versions_positive",
        ),
        sa.CheckConstraint(
            "length(runner_configuration_fingerprint) = 64",
            name="ai_agent_autonomy_task_runner_fingerprint_valid",
        ),
        sa.CheckConstraint(
            "outcome_unknown = (status = 'manual_review')",
            name="ai_agent_autonomy_task_unknown_consistent",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ai_agent_autonomy_task_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "dispatch_started_at IS NULL OR started_at IS NOT NULL",
            name="ai_agent_autonomy_task_dispatch_after_start",
        ),
        sa.CheckConstraint(
            "dispatch_started_at IS NULL OR budget_day IS NOT NULL",
            name="ai_agent_autonomy_task_dispatch_has_budget_day",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR status IN "
            "('succeeded', 'failed', 'cancelled', 'stale', 'manual_review')",
            name="ai_agent_autonomy_task_completion_terminal",
        ),
        sa.ForeignKeyConstraint(
            ["external_account_id", "owner_user_id"],
            ["external_accounts.id", "external_accounts.user_id"],
            ondelete="CASCADE",
            name="fk_ai_agent_autonomy_tasks_account_owner",
        ),
        sa.ForeignKeyConstraint(
            ["model_connection_id", "owner_user_id"],
            ["ai_model_connections.id", "ai_model_connections.owner_user_id"],
            ondelete="CASCADE",
            name="fk_ai_agent_autonomy_tasks_connection_owner",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["model_run_id"], ["ai_agent_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["action_execution_id"],
            ["ai_agent_action_executions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_ai_agent_autonomy_tasks_owner_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_agent_autonomy_tasks_due",
        "ai_agent_autonomy_tasks",
        ["status", "not_before", "lease_until"],
    )
    op.create_index(
        "ix_ai_agent_autonomy_tasks_owner_created",
        "ai_agent_autonomy_tasks",
        ["owner_user_id", "created_at"],
    )
    op.create_index(
        "ix_ai_agent_autonomy_tasks_source_message",
        "ai_agent_autonomy_tasks",
        ["owner_user_id", "source_message_id"],
    )

    op.create_table(
        "ai_agent_autonomy_daily_usage",
        _uuid_primary_key(),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("total_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reply_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("post_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "relationship_actions", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("failed_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "outcome_unknown_actions", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_action_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "total_actions >= 0 AND reply_actions >= 0 AND post_actions >= 0 "
            "AND relationship_actions >= 0 AND failed_actions >= 0 "
            "AND outcome_unknown_actions >= 0",
            name="ai_agent_autonomy_daily_usage_nonnegative",
        ),
        sa.CheckConstraint(
            "total_actions = reply_actions + post_actions + relationship_actions",
            name="ai_agent_autonomy_daily_usage_total_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "usage_date",
            name="uq_ai_agent_autonomy_daily_usage_owner_date",
        ),
    )
    op.create_index(
        "ix_ai_agent_autonomy_daily_usage_owner_date",
        "ai_agent_autonomy_daily_usage",
        ["owner_user_id", "usage_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_agent_autonomy_daily_usage_owner_date",
        table_name="ai_agent_autonomy_daily_usage",
    )
    op.drop_table("ai_agent_autonomy_daily_usage")
    op.drop_index(
        "ix_ai_agent_autonomy_tasks_source_message",
        table_name="ai_agent_autonomy_tasks",
    )
    op.drop_index(
        "ix_ai_agent_autonomy_tasks_owner_created",
        table_name="ai_agent_autonomy_tasks",
    )
    op.drop_index(
        "ix_ai_agent_autonomy_tasks_due",
        table_name="ai_agent_autonomy_tasks",
    )
    op.drop_table("ai_agent_autonomy_tasks")
    op.drop_index(
        "ix_ai_agent_autonomy_settings_due",
        table_name="ai_agent_autonomy_settings",
    )
    op.drop_table("ai_agent_autonomy_settings")
    op.execute(
        "UPDATE ai_agent_action_executions "
        "SET status = 'failed' WHERE status = 'manual_review'"
    )
    _replace_action_execution_unknown_constraints(include_manual_review=False)
    _replace_agent_run_type_constraint(include_autonomy=False)
    op.drop_column(
        "ai_model_runner_system_settings", "autonomous_agent_enabled"
    )
    op.drop_column("users", "byok_autonomous_agent_enabled")
