"""Durable owner-scoped matching history helpers.

This module intentionally avoids Redis/RQ imports so its persistence behavior
can be unit-tested without booting the complete production runtime.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Mapping

from sqlalchemy import func, select

from bbw_prod.db import session_scope
from bbw_prod.models import ActivityEvent, utcnow
from bbw_prod.repositories import ActivityEventRepository


MATCH_HISTORY_PROVIDER = "web-match"
MATCH_HISTORY_RETENTION_DAYS = 180
MATCH_HISTORY_EVENT_TYPES = {
    "/api/match/online": ("match.online", "online"),
    "/api/match/local": ("match.local", "local"),
    "/api/match/voice/start": ("match.voice", "voice"),
}
MATCH_HISTORY_PAGE_SIZE = 10

_PROFILE_FIELDS = frozenset(
    {
        "id",
        "uid",
        "user_id",
        "nickname",
        "name",
        "avatar",
        "portrait",
        "subtitle",
        "city",
        "region",
        "signature",
        "distance",
        "sex",
        "gender",
        "property",
        "age",
        "is_friend",
        "is_friend_apply",
    }
)


def _peer_uid(value: Any) -> str:
    peer = str(value or "").strip()
    if (
        not peer
        or peer.lower() in {"0", "none", "null"}
        or len(peer) > 128
        or any(ord(character) < 33 for character in peer)
    ):
        return ""
    return peer


def _response_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("items", "list", "users"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    nested = payload.get("data")
    return _response_items(nested) if nested is not payload else []


def _item_peer_uid(item: Mapping[str, Any]) -> str:
    for key in ("peer_id", "conversation_user", "user_id", "uid", "id", "yourid"):
        peer = _peer_uid(item.get(key))
        if peer:
            return peer
    nested = item.get("user")
    return _item_peer_uid(nested) if isinstance(nested, Mapping) else ""


def _profile_snapshot(item: Mapping[str, Any], peer: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in _PROFILE_FIELDS:
        if key not in item:
            continue
        value = item.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            snapshot[key] = value
        elif isinstance(value, str):
            snapshot[key] = value[:20_000]
    snapshot["id"] = peer
    snapshot["uid"] = peer
    if "user_id" in snapshot:
        snapshot["user_id"] = peer
    return snapshot


def record_match_history_response(
    *,
    owner_user_id: uuid.UUID,
    upstream_uid: str,
    method: str,
    path: str,
    response_data: Mapping[str, Any],
    status: int,
    request_id: str,
) -> list[str]:
    """Persist every valid peer in one successful server match response."""

    history_type = MATCH_HISTORY_EVENT_TYPES.get(path)
    if (
        str(method or "").upper() != "POST"
        or history_type is None
        or int(status) >= 400
        or response_data.get("ok") is not True
    ):
        return []
    event_type, mode = history_type
    current_uid = _peer_uid(upstream_uid)
    matched_items: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in _response_items(response_data):
        peer = _item_peer_uid(item)
        if not peer or peer == current_uid or peer in seen:
            continue
        seen.add(peer)
        matched_items.append((peer, item))
    if not matched_items:
        return []

    attempt_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(request_id or ""))[:96]
    if not attempt_id:
        attempt_id = uuid.uuid4().hex
    occurred_at = utcnow()
    filters = response_data.get("filters")
    safe_filters = (
        {
            str(key)[:80]: value
            for key, value in dict(filters or {}).items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
        if isinstance(filters, Mapping)
        else {}
    )
    with session_scope() as db:
        repo = ActivityEventRepository(db)
        for index, (peer, item) in enumerate(matched_items):
            repo.insert_idempotent(
                owner_user_id=owner_user_id,
                provider=MATCH_HISTORY_PROVIDER,
                upstream_event_id=f"{attempt_id}:{index}:{peer}"[:256],
                event_type=event_type,
                actor_upstream_uid=current_uid or None,
                subject_upstream_uid=peer,
                occurred_at=occurred_at,
                details={
                    "mode": mode,
                    "source_path": path,
                    "profile": _profile_snapshot(item, peer),
                    "filters": safe_filters,
                },
            )
    return [peer for peer, _item in matched_items]


def load_match_history(
    *,
    owner_user_id: uuid.UUID,
    page: int = 1,
    page_size: int = MATCH_HISTORY_PAGE_SIZE,
) -> dict[str, Any]:
    """Return one newest-first page restricted to the authenticated owner."""

    normalized_page = max(1, min(int(page), 100_000))
    normalized_size = max(1, min(int(page_size), 50))
    event_types = tuple(value[0] for value in MATCH_HISTORY_EVENT_TYPES.values())
    conditions = (
        ActivityEvent.owner_user_id == owner_user_id,
        ActivityEvent.provider == MATCH_HISTORY_PROVIDER,
        ActivityEvent.event_type.in_(event_types),
    )
    with session_scope() as db:
        total = int(
            db.scalar(select(func.count(ActivityEvent.id)).where(*conditions)) or 0
        )
        rows = list(
            db.scalars(
                select(ActivityEvent)
                .where(*conditions)
                .order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc())
                .offset((normalized_page - 1) * normalized_size)
                .limit(normalized_size)
            )
        )

    items: list[dict[str, Any]] = []
    for row in rows:
        details = dict(row.details or {})
        profile = details.get("profile")
        item = dict(profile) if isinstance(profile, Mapping) else {}
        peer = _peer_uid(row.subject_upstream_uid) or _item_peer_uid(item)
        if peer:
            item["id"] = peer
            item["uid"] = peer
            if "user_id" in item:
                item["user_id"] = peer
        item.update(
            history_id=str(row.id),
            match_mode=str(details.get("mode") or ""),
            matched_at=row.occurred_at.isoformat(),
            source_path=str(details.get("source_path") or ""),
            filters=(
                dict(details.get("filters") or {})
                if isinstance(details.get("filters"), Mapping)
                else {}
            ),
        )
        items.append(item)

    has_more = normalized_page * normalized_size < total
    return {
        "ok": True,
        "items": items,
        "list": items,
        "count": total,
        "page": normalized_page,
        "next_page": normalized_page + 1 if has_more else None,
        "has_more": has_more,
    }
