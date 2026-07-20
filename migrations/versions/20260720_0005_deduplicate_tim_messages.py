"""Deduplicate TIM messages observed through SDK and roaming history.

Revision ID: 20260720_0005
Revises: 20260720_0004
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
from typing import Any, Mapping, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0005"
down_revision: Union[str, Sequence[str], None] = "20260720_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SDK_ID = re.compile(r"^\d{12,}-(\d{9,13})-(\d{1,20})$")
_HISTORY_ID = re.compile(r"^\d{1,20}_(\d{1,20})_(\d{9,13})$")
_SOURCE_RANK = {"browser": 0, "tim_sdk": 1, "rest": 2, "history": 3}
_STATUS_RANK = {
    "unknown": 0,
    "pending": 1,
    "sending": 1,
    "failed": 1,
    "sent": 2,
    "received": 2,
    "delivered": 3,
    "read": 4,
    "revoked": 5,
}


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _message_random(upstream_message_id: Any, metadata: Mapping[str, Any]) -> str:
    explicit = str(metadata.get("message_random") or "").strip()
    if explicit.isdigit() and len(explicit) <= 20:
        return explicit
    candidates = [
        upstream_message_id,
        metadata.get("raw_upstream_message_id"),
        metadata.get("message_key"),
    ]
    candidates.extend(metadata.get("raw_upstream_message_ids") or [])
    for value in candidates:
        raw = str(value or "").strip()
        sdk_match = _SDK_ID.fullmatch(raw)
        if sdk_match:
            return sdk_match.group(2)
        history_match = _HISTORY_ID.fullmatch(raw)
        if history_match:
            return history_match.group(1)
    return ""


def _canonical_id(sender: str, recipient: str, message_random: str) -> str:
    identity = "\x1f".join(("tim-c2c", sender, recipient, message_random))
    return f"tim-c2c:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _message_key_quality(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    return 1 if raw.startswith("web-message:") else 2


def _merge_metadata(rows: list[Mapping[str, Any]], message_random: str) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: _SOURCE_RANK.get(
            str(_metadata(row["extra_data"]).get("source") or ""), 0
        ),
    )
    merged: dict[str, Any] = {}
    aliases: list[str] = []
    idempotency_keys: list[str] = []
    read_states: list[Any] = []
    message_keys: list[Any] = []
    media_reports: list[Mapping[str, Any]] = []
    for row in ordered:
        metadata = _metadata(row["extra_data"])
        for key, value in metadata.items():
            if _nonempty(value):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        for value in (
            row["upstream_message_id"],
            metadata.get("raw_upstream_message_id"),
            *(metadata.get("raw_upstream_message_ids") or []),
        ):
            raw = str(value or "").strip()
            if raw and raw not in aliases:
                aliases.append(raw[:512])
        idempotency_key = str(metadata.get("idempotency_key") or "").strip()
        if idempotency_key and idempotency_key not in idempotency_keys:
            idempotency_keys.append(idempotency_key[:256])
        read_states.append(metadata.get("is_peer_read"))
        message_keys.append(metadata.get("message_key"))
        media = metadata.get("media_report")
        if isinstance(media, Mapping):
            media_reports.append(media)

    merged["source"] = max(
        (str(_metadata(row["extra_data"]).get("source") or "") for row in rows),
        key=lambda value: _SOURCE_RANK.get(value, 0),
    )
    merged["message_key"] = max(message_keys, key=_message_key_quality) or ""
    for key in ("message_sequence", "client_message_key", "read_at", "object_name"):
        merged[key] = next(
            (
                _metadata(row["extra_data"]).get(key)
                for row in reversed(ordered)
                if _metadata(row["extra_data"]).get(key)
            ),
            "",
        )
    merged["revoked"] = any(
        bool(_metadata(row["extra_data"]).get("revoked")) for row in rows
    )
    if True in read_states:
        merged["is_peer_read"] = True
    elif False in read_states:
        merged["is_peer_read"] = False
    else:
        merged["is_peer_read"] = None
    if media_reports:
        merged["media_report"] = max(
            media_reports,
            key=lambda media: sum(bool(value) for value in media.values()),
        )
    merged["message_random"] = message_random
    merged["raw_upstream_message_ids"] = aliases[:20]
    merged["raw_upstream_message_id"] = aliases[-1] if aliases else ""
    merged["idempotency_keys"] = idempotency_keys[:20]
    return merged


def _keeper_key(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
    metadata = _metadata(row["extra_data"])
    return (
        _SOURCE_RANK.get(str(metadata.get("source") or ""), 0),
        _message_key_quality(metadata.get("message_key")),
        1 if metadata.get("message_sequence") else 0,
        str(row["created_at"]),
    )


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                """
                SELECT id, owner_user_id, conversation_id, provider,
                       upstream_message_id, direction, sender_upstream_uid,
                       recipient_upstream_uid, message_type, body, status,
                       occurred_at, retention_expires_at, metadata AS extra_data,
                       created_at, updated_at
                FROM messages
                WHERE provider = 'tim'
                ORDER BY created_at, id
                """
            )
        ).mappings()
    )
    groups: dict[tuple[Any, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    random_by_group: dict[tuple[Any, str, str], str] = {}
    for row in rows:
        sender = str(row["sender_upstream_uid"] or "").strip()
        recipient = str(row["recipient_upstream_uid"] or "").strip()
        metadata = _metadata(row["extra_data"])
        message_random = _message_random(row["upstream_message_id"], metadata)
        if not sender or not recipient or not message_random:
            continue
        canonical_id = _canonical_id(sender, recipient, message_random)
        key = (row["owner_user_id"], str(row["provider"]), canonical_id)
        groups[key].append(row)
        random_by_group[key] = message_random

    for (owner_user_id, _provider, canonical_id), grouped_rows in groups.items():
        keeper = max(grouped_rows, key=_keeper_key)
        duplicates = [row for row in grouped_rows if row["id"] != keeper["id"]]
        merged_metadata = _merge_metadata(
            grouped_rows,
            random_by_group[(owner_user_id, _provider, canonical_id)],
        )
        for duplicate in duplicates:
            connection.execute(
                sa.text(
                    """
                    UPDATE media_objects
                    SET message_id = :keeper_id
                    WHERE owner_user_id = :owner_user_id
                      AND message_id = :duplicate_id
                    """
                ),
                {
                    "keeper_id": str(keeper["id"]),
                    "owner_user_id": str(owner_user_id),
                    "duplicate_id": str(duplicate["id"]),
                },
            )
            connection.execute(
                sa.text(
                    """
                    UPDATE operation_outbox
                    SET aggregate_id = CASE
                            WHEN aggregate_id = :duplicate_id THEN :keeper_id
                            ELSE aggregate_id
                        END,
                        payload = CASE
                            WHEN payload->>'message_id' = :duplicate_id
                            THEN jsonb_set(
                                payload,
                                '{message_id}',
                                to_jsonb(CAST(:keeper_id AS text)),
                                true
                            )
                            ELSE payload
                        END
                    WHERE owner_user_id = :owner_user_id
                      AND aggregate_type = 'message'
                      AND (
                          aggregate_id = :duplicate_id
                          OR payload->>'message_id' = :duplicate_id
                      )
                    """
                ),
                {
                    "keeper_id": str(keeper["id"]),
                    "owner_user_id": str(owner_user_id),
                    "duplicate_id": str(duplicate["id"]),
                },
            )
            connection.execute(
                sa.text("DELETE FROM messages WHERE id = :duplicate_id"),
                {"duplicate_id": str(duplicate["id"])},
            )

        preferred_body = next(
            (str(row["body"]) for row in sorted(grouped_rows, key=_keeper_key, reverse=True) if row["body"]),
            None,
        )
        preferred_status = max(
            (str(row["status"] or "unknown") for row in grouped_rows),
            key=lambda value: _STATUS_RANK.get(value, 1),
        )
        preferred_direction = next(
            (str(row["direction"]) for row in grouped_rows if row["direction"] != "unknown"),
            str(keeper["direction"]),
        )
        connection.execute(
            sa.text(
                """
                UPDATE messages
                SET upstream_message_id = :canonical_id,
                    direction = :direction,
                    body = :body,
                    status = :status,
                    retention_expires_at = :retention_expires_at,
                    metadata = CAST(:metadata AS jsonb),
                    created_at = :created_at,
                    updated_at = :updated_at
                WHERE id = :keeper_id
                """
            ),
            {
                "canonical_id": canonical_id,
                "direction": preferred_direction,
                "body": preferred_body,
                "status": preferred_status,
                "retention_expires_at": max(row["retention_expires_at"] for row in grouped_rows),
                "metadata": json.dumps(merged_metadata, ensure_ascii=False),
                "created_at": min(row["created_at"] for row in grouped_rows),
                "updated_at": max(row["updated_at"] for row in grouped_rows),
                "keeper_id": str(keeper["id"]),
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, metadata AS extra_data
            FROM messages
            WHERE provider = 'tim'
              AND upstream_message_id LIKE 'tim-c2c:%'
            """
        )
    ).mappings()
    for row in rows:
        metadata = _metadata(row["extra_data"])
        aliases = metadata.get("raw_upstream_message_ids") or []
        raw_id = str(aliases[0] if aliases else metadata.get("raw_upstream_message_id") or "").strip()
        if not raw_id:
            continue
        connection.execute(
            sa.text(
                """
                UPDATE messages
                SET upstream_message_id = :raw_id
                WHERE id = :message_id
                """
            ),
            {"raw_id": raw_id[:256], "message_id": str(row["id"])},
        )
