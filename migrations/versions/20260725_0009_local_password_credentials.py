"""Add independent Web user credentials.

Revision ID: 20260725_0009
Revises: 20260724_0008
Create Date: 2026-07-25

The migration only creates the target table. Existing encrypted upstream
passwords are not decrypted by Alembic; the separately controlled backfill
command defaults to dry-run and requires an explicit apply flag.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260725_0009"
down_revision: Union[str, Sequence[str], None] = "20260724_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("credential_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enrollment_source", sa.String(length=48), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "credential_version >= 1",
            name=op.f("ck_user_credentials_user_credential_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_credentials_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_credentials"),
        sa.UniqueConstraint("user_id", name="uq_user_credentials_user"),
    )
    op.create_index(
        "ix_user_credentials_enabled",
        "user_credentials",
        ["disabled_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_credentials_enabled", table_name="user_credentials")
    op.drop_table("user_credentials")
