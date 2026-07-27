"""Store autonomous discovery cadence in seconds with a ten-second default.

Revision ID: 20260727_0022
Revises: 20260727_0021
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0022"
down_revision: Union[str, Sequence[str], None] = "20260727_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "ai_agent_autonomy_settings"
_SECONDS_CONSTRAINT = (
    "ck_ai_agent_autonomy_settings_"
    "ai_agent_autonomy_discovery_seconds_valid"
)
_SYNC_FUNCTION = "sync_ai_agent_discovery_interval_seconds"
_SYNC_TRIGGER = "trg_ai_agent_discovery_interval_seconds"


def upgrade() -> None:
    # Keep the old minute column for rolling-deployment compatibility.  New
    # code reads the seconds column; the trigger mirrors writes made by an old
    # process until the compatibility column can be retired in a later release.
    op.add_column(
        _TABLE,
        sa.Column(
            "discovery_interval_seconds",
            sa.Integer(),
            server_default=sa.text("10"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE ai_agent_autonomy_settings "
        "SET discovery_interval_seconds = CASE "
        "WHEN discovery_interval_minutes = 30 THEN 10 "
        "ELSE discovery_interval_minutes * 60 END"
    )
    op.create_check_constraint(
        _SECONDS_CONSTRAINT,
        _TABLE,
        "discovery_interval_seconds BETWEEN 10 AND 86400",
    )
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {_SYNC_FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.discovery_interval_seconds = 10
                       AND NEW.discovery_interval_minutes <> 30 THEN
                        NEW.discovery_interval_seconds :=
                            NEW.discovery_interval_minutes * 60;
                    END IF;
                ELSIF NEW.discovery_interval_minutes IS DISTINCT FROM
                          OLD.discovery_interval_minutes
                      AND NEW.discovery_interval_seconds IS NOT DISTINCT FROM
                          OLD.discovery_interval_seconds THEN
                    NEW.discovery_interval_seconds :=
                        NEW.discovery_interval_minutes * 60;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_SYNC_TRIGGER}
            BEFORE INSERT OR UPDATE OF
                discovery_interval_minutes,
                discovery_interval_seconds
            ON {_TABLE}
            FOR EACH ROW
            EXECUTE FUNCTION {_SYNC_FUNCTION}()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS {_SYNC_TRIGGER} ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_SYNC_FUNCTION}()"))
    op.drop_constraint(_SECONDS_CONSTRAINT, _TABLE, type_="check")
    op.drop_column(_TABLE, "discovery_interval_seconds")
