"""Use a less mechanical default cadence for autonomous social actions.

Revision ID: 20260727_0021
Revises: 20260727_0020
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0021"
down_revision: Union[str, Sequence[str], None] = "20260727_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "ai_agent_autonomy_settings"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        "minimum_action_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("30"),
    )
    op.execute(
        "UPDATE ai_agent_autonomy_settings "
        "SET minimum_action_interval_seconds = 30 "
        "WHERE minimum_action_interval_seconds < 30"
    )
def downgrade() -> None:
    op.alter_column(
        _TABLE,
        "minimum_action_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("10"),
    )
