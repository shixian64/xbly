"""Remove credential-shaped keys from persisted user profiles.

Revision ID: 20260727_0023
Revises: 20260727_0022
Create Date: 2026-07-27

The migration deliberately keeps latitude and longitude because existing
nearby-product behavior still consumes them.  Model-context builders must use
a narrower allowlist and never read precise location from ``users.profile``.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0023"
down_revision: Union[str, Sequence[str], None] = "20260727_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NORMALIZED_SECRET_KEYS = (
    "password",
    "passwd",
    "pwd",
    "userpassword",
    "apikey",
    "xapikey",
    "clientsecret",
    "token",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "phone",
    "phonenumber",
    "mobile",
    "useraccount",
    "loginaccount",
    "usersig",
    "secret",
    "secretkey",
    "cookie",
    "sessionid",
    "certno",
    "certname",
    "idcard",
    "identitynumber",
    "clientip",
    "ipaddress",
    "deviceid",
    "androidid",
    "imei",
    "imsi",
    "oaid",
    "idfa",
    "uniquelogintoken",
    "uniquelogintokenlocal",
    "pushid",
    "pushregid",
    "registrationid",
)


def upgrade() -> None:
    normalized_keys = ", ".join(
        f"'{key}'" for key in _NORMALIZED_SECRET_KEYS
    )
    op.execute(
        sa.text(
            f"""
            UPDATE users
            SET profile = CASE
                WHEN jsonb_typeof(profile) = 'object' THEN (
                    SELECT COALESCE(jsonb_object_agg(entry.key, entry.value), '{{}}'::jsonb)
                    FROM jsonb_each(
                        CASE
                            WHEN jsonb_typeof(profile) = 'object' THEN profile
                            ELSE '{}'::jsonb
                        END
                    ) AS entry(key, value)
                    WHERE regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          NOT IN ({normalized_keys})
                      AND regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          NOT LIKE '%apikey%'
                      AND regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          NOT LIKE '%clientsecret%'
                      AND regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          NOT LIKE '%confirmationtoken%'
                )
                ELSE '{{}}'::jsonb
            END
            WHERE jsonb_typeof(profile) <> 'object'
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_each(
                        CASE
                            WHEN jsonb_typeof(profile) = 'object' THEN profile
                            ELSE '{}'::jsonb
                        END
                    ) AS entry(key, value)
                    WHERE regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          IN ({normalized_keys})
                       OR regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          LIKE '%apikey%'
                       OR regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          LIKE '%clientsecret%'
                       OR regexp_replace(lower(entry.key), '[^a-z0-9]', '', 'g')
                          LIKE '%confirmationtoken%'
               )
            """
        )
    )


def downgrade() -> None:
    # Removed credential material and redaction placeholders cannot be safely
    # reconstructed.  Authentication data remains in its dedicated storage.
    pass
