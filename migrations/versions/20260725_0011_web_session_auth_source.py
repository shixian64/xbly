"""Persist whether a Web session was authenticated by provider or locally.

Revision ID: 20260725_0011
Revises: 20260725_0010
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260725_0011"
down_revision: Union[str, Sequence[str], None] = "20260725_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "web_sessions",
        sa.Column(
            "auth_source",
            sa.String(length=24),
            server_default="provider",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_web_sessions_web_session_auth_source_valid"),
        "web_sessions",
        "auth_source IN ('provider', 'web-local')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_web_sessions_web_session_auth_source_valid"),
        "web_sessions",
        type_="check",
    )
    op.drop_column("web_sessions", "auth_source")
