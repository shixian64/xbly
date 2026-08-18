"""Revoke historical per-user phone-only login grants.

The permission column from revision 20260818_0028 is intentionally retained
for rolling-deployment and downgrade compatibility.  Runtime code no longer
maps or reads it, while this migration prevents an older process from
re-enabling grants that existed before the administrator-only entry replaced
the public browser flow.

Revision ID: 20260818_0029
Revises: 20260818_0028
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260818_0029"
down_revision: Union[str, Sequence[str], None] = "20260818_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE users
        SET phone_only_login_enabled = false
        WHERE phone_only_login_enabled IS TRUE
        """
    )


def downgrade() -> None:
    # Revoked authorization decisions must not be reconstructed on downgrade.
    pass
