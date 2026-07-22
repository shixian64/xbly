"""Add the TIM message sequence lookup index.

Revision ID: 20260722_0006
Revises: 20260720_0005
Create Date: 2026-07-22
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260722_0006"
down_revision: Union[str, Sequence[str], None] = "20260720_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_messages_owner_conversation_sequence"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A cancelled concurrent build can leave an invalid index with the same
        # name. Remove it first so retrying the migration always repairs it.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY {INDEX_NAME}
            ON messages (
                owner_user_id,
                conversation_id,
                provider,
                (metadata ->> 'message_sequence')
            )
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
