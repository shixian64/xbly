"""Add a user-controlled chat suggestion visibility switch.

Revision ID: 20260728_0026
Revises: 20260727_0025
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0026"
down_revision: Union[str, Sequence[str], None] = "20260727_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_agent_settings",
        sa.Column(
            "chat_suggestions_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_agent_settings", "chat_suggestions_enabled")
