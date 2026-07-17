"""Add per-user permission for the match-pool online list.

Revision ID: 20260717_0002
Revises: 20260716_0001
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0002"
down_revision: Union[str, Sequence[str], None] = "20260716_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "match_pool_online_list_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "match_pool_online_list_enabled")
