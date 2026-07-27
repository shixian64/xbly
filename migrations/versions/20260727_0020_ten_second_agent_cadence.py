"""Allow ten-second autonomous action cadence.

Revision ID: 20260727_0020
Revises: 20260727_0019
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0020"
down_revision: Union[str, Sequence[str], None] = "20260727_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "ai_agent_autonomy_settings"
_CONSTRAINT = op.f(
    "ck_ai_agent_autonomy_settings_"
    "ai_agent_autonomy_minimum_interval_valid"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.alter_column(
        _TABLE,
        "minimum_action_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("10"),
    )
    # The previous default was not exposed in the product form.  Move rows
    # still carrying that implicit default to the requested ten-second cadence
    # while preserving any explicitly customized interval.
    op.execute(
        "UPDATE ai_agent_autonomy_settings "
        "SET minimum_action_interval_seconds = 10 "
        "WHERE minimum_action_interval_seconds = 300"
    )
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "minimum_action_interval_seconds BETWEEN 10 AND 86400",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE ai_agent_autonomy_settings "
        "SET minimum_action_interval_seconds = 60 "
        "WHERE minimum_action_interval_seconds < 60"
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.alter_column(
        _TABLE,
        "minimum_action_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("300"),
    )
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "minimum_action_interval_seconds BETWEEN 60 AND 86400",
    )
