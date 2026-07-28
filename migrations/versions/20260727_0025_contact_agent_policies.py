"""Add explicit per-contact chat Agent authorization.

Revision ID: 20260727_0025
Revises: 20260727_0024
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260727_0025"
down_revision: Union[str, Sequence[str], None] = "20260727_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_agent_contact_policies",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("peer_upstream_uid", sa.String(length=128), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=24),
            server_default="suggest_only",
            nullable=False,
        ),
        sa.Column("stage_override", sa.String(length=24), nullable=True),
        sa.Column(
            "paused",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "minimum_reply_delay_seconds",
            sa.SmallInteger(),
            server_default=sa.text("30"),
            nullable=False,
        ),
        sa.Column(
            "maximum_reply_age_seconds",
            sa.Integer(),
            server_default=sa.text("7200"),
            nullable=False,
        ),
        sa.Column(
            "allow_address_terms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('suggest_only', 'auto_low_risk', 'manual_only')",
            name=op.f(
                "ck_ai_agent_contact_policies_"
                "ai_agent_contact_policy_mode_valid"
            ),
        ),
        sa.CheckConstraint(
            "stage_override IS NULL OR stage_override IN "
            "('new', 'engaged', 'established', 'close', 'manual_only', 'inactive')",
            name=op.f(
                "ck_ai_agent_contact_policies_"
                "ai_agent_contact_policy_stage_override_valid"
            ),
        ),
        sa.CheckConstraint(
            "minimum_reply_delay_seconds BETWEEN 10 AND 300",
            name=op.f(
                "ck_ai_agent_contact_policies_"
                "ai_agent_contact_policy_minimum_delay_valid"
            ),
        ),
        sa.CheckConstraint(
            "maximum_reply_age_seconds BETWEEN 60 AND 7200 "
            "AND maximum_reply_age_seconds >= minimum_reply_delay_seconds",
            name=op.f(
                "ck_ai_agent_contact_policies_"
                "ai_agent_contact_policy_maximum_age_valid"
            ),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allow_address_terms) = 'array' "
            "AND jsonb_array_length(allow_address_terms) <= 20",
            name=op.f(
                "ck_ai_agent_contact_policies_"
                "ai_agent_contact_policy_address_terms_valid"
            ),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f(
                "ck_ai_agent_contact_policies_"
                "ai_agent_contact_policy_version_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f(
                "fk_ai_agent_contact_policies_owner_user_id_users"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_ai_agent_contact_policies"),
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "peer_upstream_uid",
            name="uq_ai_agent_contact_policies_owner_peer",
        ),
    )
    op.create_index(
        "ix_ai_agent_contact_policies_owner_updated",
        "ai_agent_contact_policies",
        ["owner_user_id", "updated_at"],
        unique=False,
    )

    op.add_column(
        "ai_agent_autonomy_tasks",
        sa.Column("contact_policy_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ai_agent_autonomy_tasks",
        sa.Column("relationship_stage", sa.String(length=24), nullable=True),
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_"
            "ai_agent_autonomy_task_contact_policy_version_positive"
        ),
        "ai_agent_autonomy_tasks",
        "contact_policy_version IS NULL OR contact_policy_version >= 1",
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_"
            "ai_agent_autonomy_task_relationship_stage_valid"
        ),
        "ai_agent_autonomy_tasks",
        "relationship_stage IS NULL OR relationship_stage IN "
        "('new', 'engaged', 'established', 'close', 'manual_only', 'inactive')",
    )
    op.create_index(
        "ix_ai_agent_autonomy_tasks_contact_policy",
        "ai_agent_autonomy_tasks",
        ["owner_user_id", "target_upstream_uid", "task_type", "status"],
        unique=False,
    )

    # Work created before contact-level authorization existed must never be
    # inherited by the new runtime. Dispatching rows remain untouched because
    # their external outcome may already be in flight; the new final gate will
    # reject any row without a matching contact policy version.
    op.execute(
        "UPDATE ai_agent_autonomy_tasks "
        "SET status = 'stale', stable_error_code = 'contact_policy_required', "
        "completed_at = now(), lease_owner = NULL, lease_token = NULL, "
        "lease_until = NULL, updated_at = now() "
        "WHERE task_type = 'reply_to_message' "
        "AND status IN ('queued', 'deferred', 'leased', 'generating')"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_agent_autonomy_tasks_contact_policy",
        table_name="ai_agent_autonomy_tasks",
    )
    op.drop_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_"
            "ai_agent_autonomy_task_relationship_stage_valid"
        ),
        "ai_agent_autonomy_tasks",
        type_="check",
    )
    op.drop_constraint(
        op.f(
            "ck_ai_agent_autonomy_tasks_"
            "ai_agent_autonomy_task_contact_policy_version_positive"
        ),
        "ai_agent_autonomy_tasks",
        type_="check",
    )
    op.drop_column("ai_agent_autonomy_tasks", "relationship_stage")
    op.drop_column("ai_agent_autonomy_tasks", "contact_policy_version")
    op.drop_index(
        "ix_ai_agent_contact_policies_owner_updated",
        table_name="ai_agent_contact_policies",
    )
    op.drop_table("ai_agent_contact_policies")
