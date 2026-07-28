"""Version and diversify global language-style samples.

Revision ID: 20260727_0024
Revises: 20260727_0023
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0024"
down_revision: Union[str, Sequence[str], None] = "20260727_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in (
        "source_peer_count",
        "sampling_policy_version",
        "sanitizer_version",
    ):
        op.add_column(
            "ai_style_profiles",
            sa.Column(
                column,
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )
    op.create_check_constraint(
        "ck_ai_style_profiles_ai_style_profile_source_peer_count_valid",
        "ai_style_profiles",
        "source_peer_count >= 0 AND source_peer_count <= source_message_count",
    )
    op.create_check_constraint(
        "ck_ai_style_profiles_ai_style_profile_policy_versions_nonnegative",
        "ai_style_profiles",
        "sampling_policy_version >= 0 AND sanitizer_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ai_style_profiles_ai_style_profile_policy_versions_nonnegative",
        "ai_style_profiles",
        type_="check",
    )
    op.drop_constraint(
        "ck_ai_style_profiles_ai_style_profile_source_peer_count_valid",
        "ai_style_profiles",
        type_="check",
    )
    for column in (
        "sanitizer_version",
        "sampling_policy_version",
        "source_peer_count",
    ):
        op.drop_column("ai_style_profiles", column)
