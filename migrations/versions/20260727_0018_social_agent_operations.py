"""Expand the unattended runner into a fixed-operation social agent.

Revision ID: 20260727_0018
Revises: 20260725_0017
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260727_0018"
down_revision: Union[str, Sequence[str], None] = "20260725_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACCOUNT_ACTIONS = (
    "'send_private_message', 'publish_text_post', 'follow_user', "
    "'unfollow_user', 'browse_online_users', 'request_text_match', "
    "'request_friend'"
)


def _drop_check(table: str, name: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")


def _create_action_constraints(*, expanded: bool) -> None:
    actions = (
        ACCOUNT_ACTIONS
        if expanded
        else "'send_private_message', 'publish_text_post', 'follow_user', 'unfollow_user'"
    )
    allowed_json = (
        "'[\"send_private_message\", \"publish_text_post\", \"follow_user\", "
        "\"unfollow_user\", \"browse_online_users\", \"request_text_match\", "
        "\"request_friend\"]'::jsonb"
        if expanded
        else "'[\"send_private_message\", \"publish_text_post\", "
        "\"follow_user\", \"unfollow_user\"]'::jsonb"
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_execution_settings_"
            "ai_agent_execution_setting_allowed_actions_supported"
        ),
        "ai_agent_execution_settings",
        f"allowed_actions <@ {allowed_json}",
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_action_executions_"
            "ai_agent_action_execution_type_valid"
        ),
        "ai_agent_action_executions",
        f"action_type IN ({actions})",
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_settings_"
            "ai_agent_autonomy_allowed_actions_supported"
        ),
        "ai_agent_autonomy_settings",
        f"allowed_actions <@ {allowed_json}",
    )


def _replace_run_type_constraint(*, expanded: bool) -> None:
    _drop_check("ai_agent_runs", "ai_agent_run_type_valid")
    values = (
        "'connection_test', 'style_analysis', 'reply_draft', 'reply_send', "
        "'autonomous_reply', 'autonomous_post', 'autonomous_plan', "
        "'autonomous_outreach'"
        if expanded
        else "'connection_test', 'style_analysis', 'reply_draft', 'reply_send', "
        "'autonomous_reply', 'autonomous_post', 'autonomous_plan'"
    )
    op.create_check_constraint(
        op.f("ck_ai_agent_runs_ai_agent_run_type_valid"),
        "ai_agent_runs",
        f"run_type IN ({values})",
    )


def _replace_task_constraints(*, expanded: bool) -> None:
    for name in (
        "ai_agent_autonomy_task_type_valid",
        "ai_agent_autonomy_task_action_valid",
        "ai_agent_autonomy_task_action_matches_type",
    ):
        _drop_check("ai_agent_autonomy_tasks", name)
    if expanded:
        task_types = (
            "'reply_to_message', 'scheduled_post', 'follow_target', "
            "'unfollow_target', 'browse_online', 'request_match', "
            "'proactive_message', 'follow_discovered', 'request_friend'"
        )
        mapping = (
            "(task_type = 'reply_to_message' AND action_type = 'send_private_message') OR "
            "(task_type = 'scheduled_post' AND action_type = 'publish_text_post') OR "
            "(task_type = 'follow_target' AND action_type = 'follow_user') OR "
            "(task_type = 'unfollow_target' AND action_type = 'unfollow_user') OR "
            "(task_type = 'browse_online' AND action_type = 'browse_online_users') OR "
            "(task_type = 'request_match' AND action_type = 'request_text_match') OR "
            "(task_type = 'proactive_message' AND action_type = 'send_private_message') OR "
            "(task_type = 'follow_discovered' AND action_type = 'follow_user') OR "
            "(task_type = 'request_friend' AND action_type = 'request_friend')"
        )
        actions = ACCOUNT_ACTIONS
    else:
        task_types = (
            "'reply_to_message', 'scheduled_post', 'follow_target', 'unfollow_target'"
        )
        actions = "'send_private_message', 'publish_text_post', 'follow_user', 'unfollow_user'"
        mapping = (
            "(task_type = 'reply_to_message' AND action_type = 'send_private_message') OR "
            "(task_type = 'scheduled_post' AND action_type = 'publish_text_post') OR "
            "(task_type = 'follow_target' AND action_type = 'follow_user') OR "
            "(task_type = 'unfollow_target' AND action_type = 'unfollow_user')"
        )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_ai_agent_autonomy_task_type_valid"
        ),
        "ai_agent_autonomy_tasks",
        f"task_type IN ({task_types})",
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_ai_agent_autonomy_task_action_valid"
        ),
        "ai_agent_autonomy_tasks",
        f"action_type IN ({actions})",
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_"
            "ai_agent_autonomy_task_action_matches_type"
        ),
        "ai_agent_autonomy_tasks",
        mapping,
    )


def _replace_usage_constraints(*, expanded: bool) -> None:
    for name in (
        "ai_agent_autonomy_daily_usage_nonnegative",
        "ai_agent_autonomy_daily_usage_total_consistent",
    ):
        _drop_check("ai_agent_autonomy_daily_usage", name)
    extra_nonnegative = (
        "AND browse_actions >= 0 AND match_actions >= 0 "
        "AND outreach_actions >= 0 "
        if expanded
        else ""
    )
    total = (
        "reply_actions + outreach_actions + post_actions + relationship_actions + "
        "browse_actions + match_actions"
        if expanded
        else "reply_actions + post_actions + relationship_actions"
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_daily_usage_"
            "ai_agent_autonomy_daily_usage_nonnegative"
        ),
        "ai_agent_autonomy_daily_usage",
        "total_actions >= 0 AND reply_actions >= 0 AND post_actions >= 0 "
        "AND relationship_actions >= 0 AND failed_actions >= 0 "
        f"AND outcome_unknown_actions >= 0 {extra_nonnegative}",
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_daily_usage_"
            "ai_agent_autonomy_daily_usage_total_consistent"
        ),
        "ai_agent_autonomy_daily_usage",
        f"total_actions = {total}",
    )


def _replace_autonomy_feature_constraints(*, expanded: bool) -> None:
    _drop_check(
        "ai_agent_autonomy_settings", "ai_agent_autonomy_enabled_has_capability"
    )
    if expanded:
        op.create_check_constraint(
            op.f(
                "ck_ai_agent_autonomy_settings_"
                "ai_agent_autonomy_discovery_requires_action"
            ),
            "ai_agent_autonomy_settings",
            "NOT discovery_enabled OR "
            "(user_enabled AND allowed_actions ? 'browse_online_users')",
        )
        op.create_check_constraint(
            op.f(
                "ck_ai_agent_autonomy_settings_"
                "ai_agent_autonomy_match_requires_action"
            ),
            "ai_agent_autonomy_settings",
            "NOT text_match_enabled OR "
            "(user_enabled AND allowed_actions ? 'request_text_match')",
        )
        op.create_check_constraint(
            op.f(
                "ck_ai_agent_autonomy_settings_"
                "ai_agent_autonomy_outreach_requires_action"
            ),
            "ai_agent_autonomy_settings",
            "NOT proactive_message_enabled OR "
            "(user_enabled AND allowed_actions ? 'send_private_message')",
        )
        op.create_check_constraint(
            op.f(
                "ck_ai_agent_autonomy_settings_"
                "ai_agent_autonomy_discovered_follow_requires_action"
            ),
            "ai_agent_autonomy_settings",
            "NOT follow_discovered_enabled OR "
            "(user_enabled AND allowed_actions ? 'follow_user')",
        )
        op.create_check_constraint(
            op.f(
                "ck_ai_agent_autonomy_settings_"
                "ai_agent_autonomy_friend_request_requires_action"
            ),
            "ai_agent_autonomy_settings",
            "NOT friend_request_enabled OR "
            "(user_enabled AND allowed_actions ? 'request_friend')",
        )
        enabled = (
            "NOT user_enabled OR auto_reply_enabled OR scheduled_post_enabled OR "
            "managed_relationships_enabled OR discovery_enabled OR "
            "text_match_enabled OR proactive_message_enabled OR "
            "follow_discovered_enabled OR friend_request_enabled"
        )
    else:
        enabled = (
            "NOT user_enabled OR auto_reply_enabled OR scheduled_post_enabled "
            "OR managed_relationships_enabled"
        )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_settings_"
            "ai_agent_autonomy_enabled_has_capability"
        ),
        "ai_agent_autonomy_settings",
        enabled,
    )


def upgrade() -> None:
    _drop_check(
        "ai_agent_execution_settings",
        "ai_agent_execution_setting_allowed_actions_supported",
    )
    _drop_check(
        "ai_agent_action_executions", "ai_agent_action_execution_type_valid"
    )
    _drop_check(
        "ai_agent_autonomy_settings", "ai_agent_autonomy_allowed_actions_supported"
    )

    for name in (
        "discovery_enabled",
        "text_match_enabled",
        "proactive_message_enabled",
        "follow_discovered_enabled",
        "friend_request_enabled",
    ):
        op.add_column(
            "ai_agent_autonomy_settings",
            sa.Column(
                name,
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )
    op.add_column(
        "ai_agent_autonomy_settings",
        sa.Column(
            "discovery_interval_minutes",
            sa.Integer(),
            server_default="30",
            nullable=False,
        ),
    )
    for name in ("last_discovery_at", "last_match_at", "last_outreach_at"):
        op.add_column(
            "ai_agent_autonomy_settings",
            sa.Column(name, sa.DateTime(timezone=True), nullable=True),
        )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_settings_"
            "ai_agent_autonomy_discovery_interval_valid"
        ),
        "ai_agent_autonomy_settings",
        "discovery_interval_minutes BETWEEN 5 AND 1440",
    )

    for name in ("browse_actions", "match_actions", "outreach_actions"):
        op.add_column(
            "ai_agent_autonomy_daily_usage",
            sa.Column(name, sa.Integer(), server_default="0", nullable=False),
        )

    op.create_table(
        "ai_agent_discovery_candidates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_upstream_uid", sa.String(length=128), nullable=False),
        sa.Column(
            "source", sa.String(length=32), server_default="online", nullable=False
        ),
        sa.Column("display_name", sa.String(length=160), nullable=True),
        sa.Column(
            "profile_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_action_type", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "source IN ('online', 'match')",
            name="ai_agent_discovery_candidate_source_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(profile_snapshot) = 'object'",
            name="ai_agent_discovery_candidate_profile_object",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "target_upstream_uid",
            name="uq_ai_agent_discovery_candidates_owner_target",
        ),
    )
    op.create_index(
        "ix_ai_agent_discovery_candidates_owner_seen",
        "ai_agent_discovery_candidates",
        ["owner_user_id", "last_seen_at"],
    )
    op.create_index(
        "ix_ai_agent_discovery_candidates_owner_interaction",
        "ai_agent_discovery_candidates",
        ["owner_user_id", "last_interaction_at"],
    )

    _create_action_constraints(expanded=True)
    _replace_run_type_constraint(expanded=True)
    _replace_task_constraints(expanded=True)
    _replace_usage_constraints(expanded=True)
    _replace_autonomy_feature_constraints(expanded=True)


def downgrade() -> None:
    op.execute(
        "DELETE FROM ai_agent_autonomy_tasks WHERE task_type IN "
        "('browse_online', 'request_match', 'proactive_message', "
        "'follow_discovered', 'request_friend')"
    )
    op.execute(
        "DELETE FROM ai_agent_action_executions WHERE action_type IN "
        "('browse_online_users', 'request_text_match', 'request_friend')"
    )
    op.execute(
        "DELETE FROM ai_agent_runs WHERE run_type = 'autonomous_outreach'"
    )
    op.execute(
        "UPDATE ai_agent_execution_settings SET allowed_actions = "
        "allowed_actions - 'browse_online_users' - 'request_text_match' - 'request_friend'"
    )
    op.execute(
        "UPDATE ai_agent_autonomy_settings SET allowed_actions = "
        "allowed_actions - 'browse_online_users' - 'request_text_match' - 'request_friend', "
        "user_enabled = false, auto_reply_enabled = false, "
        "scheduled_post_enabled = false, managed_relationships_enabled = false"
    )

    _drop_check(
        "ai_agent_execution_settings",
        "ai_agent_execution_setting_allowed_actions_supported",
    )
    _drop_check(
        "ai_agent_action_executions", "ai_agent_action_execution_type_valid"
    )
    _drop_check(
        "ai_agent_autonomy_settings", "ai_agent_autonomy_allowed_actions_supported"
    )
    for name in (
        "ai_agent_autonomy_discovery_requires_action",
        "ai_agent_autonomy_match_requires_action",
        "ai_agent_autonomy_outreach_requires_action",
        "ai_agent_autonomy_discovered_follow_requires_action",
        "ai_agent_autonomy_friend_request_requires_action",
    ):
        _drop_check("ai_agent_autonomy_settings", name)
    _replace_autonomy_feature_constraints(expanded=False)
    op.execute(
        "UPDATE ai_agent_autonomy_daily_usage SET browse_actions = 0, "
        "match_actions = 0, outreach_actions = 0, total_actions = "
        "reply_actions + post_actions + relationship_actions"
    )
    _replace_usage_constraints(expanded=False)
    _replace_task_constraints(expanded=False)
    _replace_run_type_constraint(expanded=False)
    _create_action_constraints(expanded=False)

    op.drop_index(
        "ix_ai_agent_discovery_candidates_owner_interaction",
        table_name="ai_agent_discovery_candidates",
    )
    op.drop_index(
        "ix_ai_agent_discovery_candidates_owner_seen",
        table_name="ai_agent_discovery_candidates",
    )
    op.drop_table("ai_agent_discovery_candidates")

    for name in ("outreach_actions", "match_actions", "browse_actions"):
        op.drop_column("ai_agent_autonomy_daily_usage", name)
    _drop_check(
        "ai_agent_autonomy_settings", "ai_agent_autonomy_discovery_interval_valid"
    )
    for name in (
        "last_outreach_at",
        "last_match_at",
        "last_discovery_at",
        "discovery_interval_minutes",
        "friend_request_enabled",
        "follow_discovered_enabled",
        "proactive_message_enabled",
        "text_match_enabled",
        "discovery_enabled",
    ):
        op.drop_column("ai_agent_autonomy_settings", name)
