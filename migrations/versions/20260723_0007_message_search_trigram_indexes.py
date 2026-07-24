"""Add trigram indexes for archived message search.

Revision ID: 20260723_0007
Revises: 20260722_0006
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260723_0007"
down_revision: Union[str, Sequence[str], None] = "20260722_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_DEFINITIONS = {
    "ix_messages_body_trgm": (
        "message_search_index_text(body) gin_trgm_ops"
    ),
    "ix_messages_media_name_trgm": (
        "message_search_media_name(metadata) gin_trgm_ops"
    ),
    "ix_messages_quote_text_trgm": (
        "message_search_quote_text(metadata) gin_trgm_ops"
    ),
}
ACTIVE_MESSAGE_PREDICATE = (
    "status <> 'revoked' AND "
    "coalesce(metadata ->> 'revoked', 'false') NOT IN ('true', '1')"
)


def _create_search_functions() -> None:
    # Prefix every normalized UTF-8 byte with an alphanumeric marker. Even one
    # ASCII character then produces a three-character key (for example x61),
    # so pg_trgm can narrow short Chinese and Latin searches through GIN.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION message_search_index_text(value text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $function$
            SELECT regexp_replace(
                encode(convert_to(lower(coalesce(value, '')), 'UTF8'), 'hex'),
                '([0-9a-f]{2})',
                E'x\\1',
                'g'
            )
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION message_search_media_name(value jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $function$
            SELECT message_search_index_text(
                value -> 'media_report' ->> 'name'
            )
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION message_search_quote_text(value jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $function$
            SELECT message_search_index_text(
                value -> 'quote' ->> 'text'
            )
        $function$
        """
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        _create_search_functions()
        for index_name, expression in INDEX_DEFINITIONS.items():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
            op.execute(
                f"""
                CREATE INDEX CONCURRENTLY {index_name}
                ON messages USING gin ({expression})
                WHERE {ACTIVE_MESSAGE_PREDICATE}
                """
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name in INDEX_DEFINITIONS:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
        op.execute("DROP FUNCTION IF EXISTS message_search_media_name(jsonb)")
        op.execute("DROP FUNCTION IF EXISTS message_search_quote_text(jsonb)")
        op.execute("DROP FUNCTION IF EXISTS message_search_index_text(text)")
