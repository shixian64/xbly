"""Prevent autonomous reply backfill and require an activation watermark.

Revision ID: 20260727_0019
Revises: 20260727_0018
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0019"
down_revision: Union[str, Sequence[str], None] = "20260727_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_agent_autonomy_settings",
        sa.Column(
            "auto_reply_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Existing enabled rows start from the deployment watermark instead of
    # treating every archived incoming head as newly received work.
    op.execute(
        "UPDATE ai_agent_autonomy_settings "
        "SET auto_reply_started_at = now() "
        "WHERE auto_reply_enabled = true"
    )
    op.create_check_constraint(
        op.f(
            "ck_ai_agent_autonomy_settings_"
            "ai_agent_autonomy_reply_requires_watermark"
        ),
        "ai_agent_autonomy_settings",
        "NOT auto_reply_enabled OR auto_reply_started_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f(
            "ck_ai_agent_autonomy_settings_"
            "ai_agent_autonomy_reply_requires_watermark"
        ),
        "ai_agent_autonomy_settings",
        type_="check",
    )
    op.drop_column("ai_agent_autonomy_settings", "auto_reply_started_at")
