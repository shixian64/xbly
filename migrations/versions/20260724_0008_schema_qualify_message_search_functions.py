"""Schema-qualify helper calls used by message search indexes.

Revision ID: 20260724_0008
Revises: 20260723_0007
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260724_0008"
down_revision: Union[str, Sequence[str], None] = "20260723_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _replace_wrapper_functions(*, schema_qualified: bool) -> None:
    helper = (
        "public.message_search_index_text"
        if schema_qualified
        else "message_search_index_text"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.message_search_media_name(value jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $function$
            SELECT {helper}(
                value -> 'media_report' ->> 'name'
            )
        $function$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.message_search_quote_text(value jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $function$
            SELECT {helper}(
                value -> 'quote' ->> 'text'
            )
        $function$
        """
    )


def upgrade() -> None:
    # SQL-language function bodies written as string literals are resolved
    # using the caller's search_path. Autovacuum/ANALYZE does not guarantee
    # that ``public`` is present, so the nested helper must be qualified.
    _replace_wrapper_functions(schema_qualified=True)


def downgrade() -> None:
    _replace_wrapper_functions(schema_qualified=False)
