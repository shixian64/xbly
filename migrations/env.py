from __future__ import annotations

from logging.config import fileConfig
import re
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from bbw_prod.config import get_settings
from bbw_prod.db import Base
from bbw_prod import models  # noqa: F401 - register model metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


_PUBLIC_ID_DEFAULT_PREFIXES = {
    "match_queue_entries": "mqe",
    "match_results": "mch",
    "social_comments": "cmt",
    "social_posts": "pst",
    "social_reports": "rpt",
    "social_topics": "tpc",
}


def _public_id_default_matches(value: object, *, prefix: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or "")).lower()
    return all(
        marker in compact
        for marker in (
            f"'{prefix}_'",
            "replace(",
            "gen_random_uuid()",
            "'-'",
            "''",
        )
    )


def _compare_server_default(
    _context: Any,
    inspected_column: Any,
    metadata_column: Any,
    inspected_default: object,
    _metadata_default: object,
    rendered_metadata_default: object,
) -> bool | None:
    """Ignore only equivalent volatile public-id defaults.

    PostgreSQL normalizes casts and parentheses around these expressions, and
    Alembic's normal server-default comparison cannot evaluate two calls to
    ``gen_random_uuid()`` as equal because each call intentionally differs.
    Every other default still uses Alembic's normal drift detection.
    """

    table_name = str(getattr(getattr(inspected_column, "table", None), "name", ""))
    prefix = _PUBLIC_ID_DEFAULT_PREFIXES.get(table_name)
    if (
        prefix is not None
        and str(getattr(inspected_column, "name", "")) == "public_id"
        and str(getattr(metadata_column, "name", "")) == "public_id"
        and _public_id_default_matches(inspected_default, prefix=prefix)
        and _public_id_default_matches(rendered_metadata_default, prefix=prefix)
    ):
        return False
    return None


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=_compare_server_default,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=_compare_server_default,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
