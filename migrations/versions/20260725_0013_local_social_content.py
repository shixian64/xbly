"""Add canonical Web-local social posts, comments and reactions.

Revision ID: 20260725_0013
Revises: 20260725_0012
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0013"
down_revision: Union[str, Sequence[str], None] = "20260725_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_primary_key() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def _json_object(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def _public_id(name: str, prefix: str) -> sa.Column:
    return sa.Column(
        name,
        sa.String(length=40),
        server_default=sa.text(
            f"'{prefix}_' || replace(gen_random_uuid()::text, '-', '')"
        ),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "social_topics",
        _uuid_primary_key(),
        _public_id("public_id", "tpc"),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("post_count", sa.Integer(), server_default="0", nullable=False),
        _json_object("metadata"),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('active', 'hidden')",
            name=op.f("ck_social_topics_social_topic_status_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0",
            name=op.f("ck_social_topics_social_topic_name_nonempty"),
        ),
        sa.CheckConstraint(
            "post_count >= 0",
            name=op.f("ck_social_topics_social_topic_post_count_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_social_topics_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_topics")),
        sa.UniqueConstraint(
            "public_id", name=op.f("uq_social_topics_public_id")
        ),
        sa.UniqueConstraint(
            "normalized_name", name=op.f("uq_social_topics_normalized_name")
        ),
    )
    op.create_index(
        "ix_social_topics_status_posts",
        "social_topics",
        ["status", "post_count", "name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_social_topics_created_by_user_id"),
        "social_topics",
        ["created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "social_posts",
        _uuid_primary_key(),
        _public_id("public_id", "pst"),
        sa.Column("client_request_id", sa.String(length=160), nullable=True),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_upstream_uid", sa.String(length=128), nullable=False),
        sa.Column(
            "author_display_name", sa.String(length=160), server_default="", nullable=False
        ),
        _json_object("author_snapshot"),
        sa.Column(
            "source", sa.String(length=24), server_default="web-local", nullable=False
        ),
        sa.Column("title", sa.String(length=240), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        _json_object("media"),
        sa.Column(
            "visibility", sa.String(length=24), server_default="public", nullable=False
        ),
        sa.Column(
            "comment_policy", sa.String(length=24), server_default="open", nullable=False
        ),
        sa.Column(
            "hide_comments", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=24), server_default="published", nullable=False
        ),
        sa.Column(
            "is_pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("like_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("comment_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("report_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "source_created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        _json_object("metadata"),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "source IN ('web-local', 'legacy-import')",
            name=op.f("ck_social_posts_social_post_source_valid"),
        ),
        sa.CheckConstraint(
            "(source = 'web-local' AND author_user_id IS NOT NULL "
            "AND client_request_id IS NOT NULL) "
            "OR source = 'legacy-import'",
            name=op.f("ck_social_posts_social_post_local_author_required"),
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name=op.f("ck_social_posts_social_post_digest_length"),
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'followers', 'private')",
            name=op.f("ck_social_posts_social_post_visibility_valid"),
        ),
        sa.CheckConstraint(
            "comment_policy IN ('open', 'followers', 'disabled')",
            name=op.f("ck_social_posts_social_post_comment_policy_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('published', 'hidden', 'deleted')",
            name=op.f("ck_social_posts_social_post_status_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(coalesce(body, ''))) > 0 OR media <> '{}'::jsonb",
            name=op.f("ck_social_posts_social_post_content_nonempty"),
        ),
        sa.CheckConstraint(
            "like_count >= 0",
            name=op.f("ck_social_posts_social_post_like_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "comment_count >= 0",
            name=op.f("ck_social_posts_social_post_comment_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "view_count >= 0",
            name=op.f("ck_social_posts_social_post_view_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "report_count >= 0",
            name=op.f("ck_social_posts_social_post_report_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_social_posts_social_post_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_social_posts_author_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_posts")),
        sa.UniqueConstraint(
            "public_id", name=op.f("uq_social_posts_public_id")
        ),
        sa.UniqueConstraint(
            "author_user_id",
            "client_request_id",
            name=op.f("uq_social_posts_author_client_request"),
        ),
    )
    op.create_index(
        op.f("ix_social_posts_author_user_id"),
        "social_posts",
        ["author_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_social_posts_feed",
        "social_posts",
        ["status", "visibility", "published_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_social_posts_author_published",
        "social_posts",
        ["author_user_id", "status", "is_pinned", "published_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_posts_source_created",
        "social_posts",
        ["source", "source_created_at"],
        unique=False,
    )

    op.create_table(
        "social_comments",
        _uuid_primary_key(),
        _public_id("public_id", "cmt"),
        sa.Column("client_request_id", sa.String(length=160), nullable=True),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_comment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_upstream_uid", sa.String(length=128), nullable=False),
        sa.Column(
            "author_display_name", sa.String(length=160), server_default="", nullable=False
        ),
        _json_object("author_snapshot"),
        sa.Column(
            "source", sa.String(length=24), server_default="web-local", nullable=False
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("like_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reply_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "source_created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _json_object("metadata"),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "source IN ('web-local', 'legacy-import')",
            name=op.f("ck_social_comments_social_comment_source_valid"),
        ),
        sa.CheckConstraint(
            "(source = 'web-local' AND author_user_id IS NOT NULL "
            "AND client_request_id IS NOT NULL) "
            "OR source = 'legacy-import'",
            name=op.f("ck_social_comments_social_comment_local_author_required"),
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name=op.f("ck_social_comments_social_comment_digest_length"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'hidden', 'deleted')",
            name=op.f("ck_social_comments_social_comment_status_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(body)) > 0",
            name=op.f("ck_social_comments_social_comment_body_nonempty"),
        ),
        sa.CheckConstraint(
            "like_count >= 0",
            name=op.f("ck_social_comments_social_comment_like_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "reply_count >= 0",
            name=op.f("ck_social_comments_social_comment_reply_count_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["social_posts.id"],
            name=op.f("fk_social_comments_post_id_social_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_comment_id"],
            ["social_comments.id"],
            name=op.f("fk_social_comments_parent_comment_id_social_comments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_social_comments_author_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_comments")),
        sa.UniqueConstraint(
            "public_id", name=op.f("uq_social_comments_public_id")
        ),
        sa.UniqueConstraint(
            "author_user_id",
            "client_request_id",
            name=op.f("uq_social_comments_author_client_request"),
        ),
    )
    op.create_index(
        op.f("ix_social_comments_author_user_id"),
        "social_comments",
        ["author_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_social_comments_post_created",
        "social_comments",
        ["post_id", "status", "source_created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_social_comments_parent_created",
        "social_comments",
        ["parent_comment_id", "source_created_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_comments_author_created",
        "social_comments",
        ["author_user_id", "source_created_at"],
        unique=False,
    )

    op.create_table(
        "social_post_topics",
        _uuid_primary_key(),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_social_post_topics_social_post_topic_position_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["social_posts.id"],
            name=op.f("fk_social_post_topics_post_id_social_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["social_topics.id"],
            name=op.f("fk_social_post_topics_topic_id_social_topics"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_post_topics")),
        sa.UniqueConstraint(
            "post_id",
            "topic_id",
            name=op.f("uq_social_post_topics_post_topic"),
        ),
    )
    op.create_index(
        "ix_social_post_topics_topic_created",
        "social_post_topics",
        ["topic_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "social_reactions",
        _uuid_primary_key(),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reaction_type", sa.String(length=24), server_default="like", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "(post_id IS NOT NULL AND comment_id IS NULL) OR "
            "(post_id IS NULL AND comment_id IS NOT NULL)",
            name=op.f("ck_social_reactions_social_reaction_exactly_one_target"),
        ),
        sa.CheckConstraint(
            "reaction_type = 'like'",
            name=op.f("ck_social_reactions_social_reaction_type_supported"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_social_reactions_actor_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["social_posts.id"],
            name=op.f("fk_social_reactions_post_id_social_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["social_comments.id"],
            name=op.f("fk_social_reactions_comment_id_social_comments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_reactions")),
        sa.UniqueConstraint(
            "actor_user_id",
            "post_id",
            "reaction_type",
            name=op.f("uq_social_reactions_post_actor"),
        ),
        sa.UniqueConstraint(
            "actor_user_id",
            "comment_id",
            "reaction_type",
            name=op.f("uq_social_reactions_comment_actor"),
        ),
    )
    op.create_index(
        "ix_social_reactions_post_active",
        "social_reactions",
        ["post_id", "active"],
        unique=False,
    )
    op.create_index(
        "ix_social_reactions_comment_active",
        "social_reactions",
        ["comment_id", "active"],
        unique=False,
    )

    op.create_table(
        "social_reports",
        _uuid_primary_key(),
        _public_id("public_id", "rpt"),
        sa.Column("reporter_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("reason_text", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("resolution", sa.String(length=1000), nullable=True),
        sa.Column("reviewed_by_admin_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        _json_object("metadata"),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "(post_id IS NOT NULL AND comment_id IS NULL) OR "
            "(post_id IS NULL AND comment_id IS NOT NULL)",
            name=op.f("ck_social_reports_social_report_exactly_one_target"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'reviewed', 'dismissed', 'actioned')",
            name=op.f("ck_social_reports_social_report_status_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason_code)) > 0",
            name=op.f("ck_social_reports_social_report_reason_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["reporter_user_id"],
            ["users.id"],
            name=op.f("fk_social_reports_reporter_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["social_posts.id"],
            name=op.f("fk_social_reports_post_id_social_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["social_comments.id"],
            name=op.f("fk_social_reports_comment_id_social_comments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_admin_id"],
            ["admin_users.id"],
            name=op.f("fk_social_reports_reviewed_by_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_reports")),
        sa.UniqueConstraint(
            "reporter_user_id",
            "idempotency_key",
            name=op.f("uq_social_reports_reporter_key"),
        ),
        sa.UniqueConstraint(
            "public_id", name=op.f("uq_social_reports_public_id")
        ),
    )
    op.create_index(
        "ix_social_reports_queue",
        "social_reports",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_reports_post",
        "social_reports",
        ["post_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_reports_comment",
        "social_reports",
        ["comment_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "social_post_views",
        _uuid_primary_key(),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewed_on", sa.Date(), nullable=False),
        sa.Column("view_count", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "view_count >= 1",
            name=op.f("ck_social_post_views_social_post_view_count_positive"),
        ),
        sa.CheckConstraint(
            "first_viewed_at <= last_viewed_at",
            name=op.f("ck_social_post_views_social_post_view_time_order_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["social_posts.id"],
            name=op.f("fk_social_post_views_post_id_social_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["viewer_user_id"],
            ["users.id"],
            name=op.f("fk_social_post_views_viewer_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_post_views")),
        sa.UniqueConstraint(
            "post_id",
            "viewer_user_id",
            "viewed_on",
            name=op.f("uq_social_post_views_daily"),
        ),
    )
    op.create_index(
        "ix_social_post_views_post_last",
        "social_post_views",
        ["post_id", "last_viewed_at"],
        unique=False,
    )
    op.create_index(
        "ix_social_post_views_viewer_last",
        "social_post_views",
        ["viewer_user_id", "last_viewed_at"],
        unique=False,
    )

    op.create_table(
        "legacy_social_bindings",
        _uuid_primary_key(),
        sa.Column("provider", sa.String(length=40), server_default="beibeiwu", nullable=False),
        sa.Column("entity_type", sa.String(length=24), nullable=False),
        sa.Column("upstream_id", sa.String(length=256), nullable=False),
        sa.Column("local_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_public_id", sa.String(length=40), nullable=False),
        sa.Column("imported_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_scope", sa.String(length=32), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        _json_object("metadata"),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "entity_type IN ('post', 'comment', 'topic')",
            name=op.f("ck_legacy_social_bindings_legacy_social_binding_entity_valid"),
        ),
        sa.CheckConstraint(
            "import_scope IN ('owner', 'visible-history')",
            name=op.f("ck_legacy_social_bindings_legacy_social_binding_scope_valid"),
        ),
        sa.CheckConstraint(
            "import_scope <> 'visible-history' OR "
            "source_created_at >= created_at - interval '180 days'",
            name=op.f(
                "ck_legacy_social_bindings_legacy_social_binding_visible_history_180_days"
            ),
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name=op.f("ck_legacy_social_bindings_legacy_social_binding_digest_length"),
        ),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"],
            ["users.id"],
            name=op.f("fk_legacy_social_bindings_imported_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legacy_social_bindings")),
        sa.UniqueConstraint(
            "provider",
            "entity_type",
            "upstream_id",
            name=op.f("uq_legacy_social_bindings_source"),
        ),
        sa.UniqueConstraint(
            "provider",
            "entity_type",
            "local_entity_id",
            name=op.f("uq_legacy_social_bindings_local"),
        ),
    )
    op.create_index(
        "ix_legacy_social_bindings_importer_created",
        "legacy_social_bindings",
        ["imported_by_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_legacy_social_bindings_source_created",
        "legacy_social_bindings",
        ["provider", "entity_type", "source_created_at"],
        unique=False,
    )

    op.create_table(
        "user_discovery_profiles",
        _uuid_primary_key(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("city_code", sa.String(length=32), nullable=True),
        sa.Column("city_name", sa.String(length=80), nullable=True),
        sa.Column(
            "gender", sa.String(length=16), server_default="unspecified", nullable=False
        ),
        sa.Column("profile_property", sa.String(length=80), nullable=True),
        sa.Column("age", sa.SmallInteger(), nullable=True),
        sa.Column(
            "discoverable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "gender IN ('male', 'female', 'other', 'unspecified')",
            name=op.f(
                "ck_user_discovery_profiles_user_discovery_profile_gender_valid"
            ),
        ),
        sa.CheckConstraint(
            "age IS NULL OR age BETWEEN 18 AND 120",
            name=op.f("ck_user_discovery_profiles_user_discovery_profile_age_valid"),
        ),
        sa.CheckConstraint(
            "NOT discoverable OR city_code IS NOT NULL",
            name=op.f(
                "ck_user_discovery_profiles_user_discovery_profile_city_required"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_discovery_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_discovery_profiles")),
        sa.UniqueConstraint(
            "user_id", name=op.f("uq_user_discovery_profiles_user")
        ),
    )
    op.create_index(
        "ix_user_discovery_profiles_city_active",
        "user_discovery_profiles",
        ["city_code", "discoverable", "last_active_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_discovery_profiles_filters",
        "user_discovery_profiles",
        ["gender", "profile_property", "age"],
        unique=False,
    )

    op.create_table(
        "match_preferences",
        _uuid_primary_key(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "city_scope", sa.String(length=24), server_default="same-city", nullable=False
        ),
        sa.Column(
            "gender_preference", sa.String(length=16), server_default="any", nullable=False
        ),
        sa.Column("property_preference", sa.String(length=80), nullable=True),
        sa.Column("min_age", sa.SmallInteger(), server_default="18", nullable=False),
        sa.Column("max_age", sa.SmallInteger(), server_default="120", nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "city_scope IN ('same-city', 'anywhere')",
            name=op.f("ck_match_preferences_match_preference_city_scope_valid"),
        ),
        sa.CheckConstraint(
            "gender_preference IN ('any', 'male', 'female', 'other')",
            name=op.f("ck_match_preferences_match_preference_gender_valid"),
        ),
        sa.CheckConstraint(
            "min_age BETWEEN 18 AND 120 AND max_age BETWEEN 18 AND 120 "
            "AND min_age <= max_age",
            name=op.f("ck_match_preferences_match_preference_age_range_valid"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_match_preferences_match_preference_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_match_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_preferences")),
        sa.UniqueConstraint("user_id", name=op.f("uq_match_preferences_user")),
    )
    op.create_index(
        "ix_match_preferences_enabled",
        "match_preferences",
        ["enabled", "updated_at"],
        unique=False,
    )

    op.create_table(
        "match_queue_entries",
        _uuid_primary_key(),
        _public_id("public_id", "mqe"),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("queue_kind", sa.String(length=24), server_default="text", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="waiting", nullable=False),
        sa.Column("city_code", sa.String(length=32), nullable=True),
        sa.Column("preference_version", sa.Integer(), nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "queue_kind = 'text'",
            name=op.f("ck_match_queue_entries_match_queue_entry_kind_supported"),
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'matched', 'cancelled', 'expired')",
            name=op.f("ck_match_queue_entries_match_queue_entry_status_valid"),
        ),
        sa.CheckConstraint(
            "expires_at > enqueued_at",
            name=op.f("ck_match_queue_entries_match_queue_entry_expiry_valid"),
        ),
        sa.CheckConstraint(
            "preference_version >= 1",
            name=op.f(
                "ck_match_queue_entries_match_queue_entry_preference_version_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_match_queue_entries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_queue_entries")),
        sa.UniqueConstraint(
            "public_id", name=op.f("uq_match_queue_entries_public_id")
        ),
        sa.UniqueConstraint(
            "id", "user_id", name=op.f("uq_match_queue_entries_id_user")
        ),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name=op.f("uq_match_queue_entries_user_key"),
        ),
    )
    op.create_index(
        "uq_match_queue_entries_user_waiting",
        "match_queue_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index(
        "ix_match_queue_entries_claim",
        "match_queue_entries",
        ["queue_kind", "status", "city_code", "enqueued_at"],
        unique=False,
    )
    op.create_index(
        "ix_match_queue_entries_expiry",
        "match_queue_entries",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "match_results",
        _uuid_primary_key(),
        _public_id("public_id", "mch"),
        sa.Column("match_key", sa.String(length=160), nullable=False),
        sa.Column("user_low_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_high_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_low_queue_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "user_high_queue_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("initiated_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "user_low_id < user_high_id",
            name=op.f("ck_match_results_match_result_users_canonical_order"),
        ),
        sa.CheckConstraint(
            "user_low_queue_entry_id <> user_high_queue_entry_id",
            name=op.f("ck_match_results_match_result_queue_entries_distinct"),
        ),
        sa.CheckConstraint(
            "initiated_by_user_id IN (user_low_id, user_high_id)",
            name=op.f("ck_match_results_match_result_initiator_is_party"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'dismissed', 'expired')",
            name=op.f("ck_match_results_match_result_status_valid"),
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > matched_at",
            name=op.f("ck_match_results_match_result_expiry_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_low_id"],
            ["users.id"],
            name=op.f("fk_match_results_user_low_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_high_id"],
            ["users.id"],
            name=op.f("fk_match_results_user_high_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initiated_by_user_id"],
            ["users.id"],
            name=op.f("fk_match_results_initiated_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_low_queue_entry_id", "user_low_id"],
            ["match_queue_entries.id", "match_queue_entries.user_id"],
            name=op.f("fk_match_results_low_queue_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_high_queue_entry_id", "user_high_id"],
            ["match_queue_entries.id", "match_queue_entries.user_id"],
            name=op.f("fk_match_results_high_queue_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_results")),
        sa.UniqueConstraint(
            "public_id", name=op.f("uq_match_results_public_id")
        ),
        sa.UniqueConstraint(
            "match_key", name=op.f("uq_match_results_match_key")
        ),
        sa.UniqueConstraint(
            "user_low_queue_entry_id",
            "user_high_queue_entry_id",
            name=op.f("uq_match_results_queue_pair"),
        ),
    )
    op.create_index(
        "ix_match_results_low_matched",
        "match_results",
        ["user_low_id", "matched_at"],
        unique=False,
    )
    op.create_index(
        "ix_match_results_high_matched",
        "match_results",
        ["user_high_id", "matched_at"],
        unique=False,
    )
    op.create_index(
        "ix_match_results_status_expiry",
        "match_results",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("match_results")
    op.drop_table("match_queue_entries")
    op.drop_table("match_preferences")
    op.drop_table("user_discovery_profiles")
    op.drop_table("legacy_social_bindings")
    op.drop_table("social_post_views")
    op.drop_table("social_reports")
    op.drop_table("social_reactions")
    op.drop_table("social_post_topics")
    op.drop_table("social_comments")
    op.drop_table("social_posts")
    op.drop_table("social_topics")
