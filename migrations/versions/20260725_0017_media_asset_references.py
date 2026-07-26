"""Add owner-bound profile and social-post media references.

Revision ID: 20260725_0017
Revises: 20260725_0016
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0017"
down_revision: Union[str, Sequence[str], None] = "20260725_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_social_posts_id_author"),
        "social_posts",
        ["id", "author_user_id"],
    )
    op.create_table(
        "media_asset_references",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=24), nullable=False),
        sa.Column("profile_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("social_post_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("slot", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(resource_type = 'profile' AND profile_user_id IS NOT NULL "
            "AND social_post_id IS NULL AND profile_user_id = owner_user_id "
            "AND slot = 'avatar') OR "
            "(resource_type = 'social_post' AND profile_user_id IS NULL "
            "AND social_post_id IS NOT NULL AND "
            "(slot IN ('video', 'cover') OR "
            "slot ~ '^pictures\\[(0|[1-9][0-9]*)\\]$'))",
            name=op.f(
                "ck_media_asset_references_"
                "media_asset_reference_target_slot_valid"
            ),
        ),
        sa.CheckConstraint(
            "position('://' in slot) = 0",
            name=op.f(
                "ck_media_asset_references_media_asset_reference_slot_not_url"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "owner_user_id"],
            ["media_assets.id", "media_assets.owner_user_id"],
            name=op.f("fk_media_asset_references_asset_owner"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_user_id"],
            ["users.id"],
            name=op.f("fk_media_asset_references_profile_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["social_post_id", "owner_user_id"],
            ["social_posts.id", "social_posts.author_user_id"],
            name=op.f("fk_media_asset_references_post_owner"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_asset_references")),
        sa.UniqueConstraint(
            "asset_id",
            name=op.f("uq_media_asset_references_asset"),
        ),
        sa.UniqueConstraint(
            "profile_user_id",
            "slot",
            name=op.f("uq_media_asset_references_profile_slot"),
        ),
        sa.UniqueConstraint(
            "social_post_id",
            "slot",
            name=op.f("uq_media_asset_references_social_post_slot"),
        ),
    )
    op.create_index(
        "ix_media_asset_references_owner_resource",
        "media_asset_references",
        ["owner_user_id", "resource_type"],
        unique=False,
    )
    op.create_index(
        "ix_media_asset_references_social_post",
        "media_asset_references",
        ["social_post_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("media_asset_references")
    op.drop_constraint(
        op.f("uq_social_posts_id_author"),
        "social_posts",
        type_="unique",
    )
