"""Durable RQ jobs for chat reconciliation, media archival and retention.

The browser and the protocol client are both untrusted ingestion sources.  Every
job therefore derives ownership from the PostgreSQL account binding passed by
the authenticated Web layer and never accepts an owner identifier from a
message payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx
from botocore.exceptions import ClientError
from redis import Redis
from rq import Queue
from rq.exceptions import InvalidJobOperation
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import aliased

from bbw_agent.repositories import (
    AgentActionExecutionRepository,
    AgentRunRepository,
)
from bbw_agent.autonomous import (
    AUTONOMY_REPLY_CLOCK_SKEW_SECONDS,
    AUTONOMY_REPLY_MAX_AGE_SECONDS,
    autonomy_reply_message_is_eligible,
    autonomy_reply_message_is_fresh,
)
from bbw_prod.compatibility import (
    CompatibilityMode,
    compatibility_dispatch_enabled,
    compatibility_mode,
)
from bbw_prod.config import Settings, get_settings
from bbw_prod.crypto import CredentialCipher
from bbw_prod.db import session_scope
from bbw_prod.models import (
    ActivityEvent,
    AiAgentAutonomyTask,
    Conversation,
    ExternalAccount,
    MediaObject,
    Message,
    MessageDelivery,
    OperationOutbox,
    Relationship,
    SyncCursor,
    User,
    WebSession,
    utcnow,
)
from bbw_prod.repositories import (
    ActivityEventRepository,
    ConversationRepository,
    ExternalAccountRepository,
    MediaObjectRepository,
    MessageRepository,
    OperationOutboxRepository,
    RelationshipRepository,
    SyncCursorRepository,
    UserRepository,
)
from bbw_prod.services import (
    MediaQuotaService,
    NotFoundError,
    QuotaExceeded,
    RetentionService,
)
from bbw_web.media_archive import MediaArchiveError, PreparedMedia, download_and_prepare
from bbw_web.match_history import MATCH_HISTORY_PROVIDER, MATCH_HISTORY_RETENTION_DAYS
from bbw_web.message_quote import (
    encode_message_quote,
    extract_local_message_identity,
    extract_message_quote,
    normalize_message_quote,
)
from bbw_web.messaging.contracts import TIM_MIRROR_CHANNEL
from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore
from bbw_web.normalize import normalize_conversations, normalize_messages
from bbw_web.r2 import R2Storage
from bbw_web.transports import (
    MessageHistoryTransport,
    MessageMirrorTransport,
    MessageRecallLookupTransport,
    MessageSendTransport,
)


CHAT_PROVIDER = "tim"
SYNC_SOURCE = "beibeiwu"
SYNC_STREAM = "chat-history"
MEDIA_OPERATION = "media.archive"
MEDIA_MAX_ATTEMPTS = 8
MEDIA_CONFIGURATION_ALERT_INTERVAL = MEDIA_MAX_ATTEMPTS
MEDIA_CONFIGURATION_PROBE_SECONDS = 300
MEDIA_CONFIGURATION_READY_SECONDS = 900
MEDIA_CONFIGURATION_ERROR_MARKERS = (
    "accessdenied",
    "access denied",
    "authorizationheadermalformed",
    "invalidaccesskeyid",
    "invalid access key",
    "invalidtoken",
    "invalid token",
    "signaturedoesnotmatch",
    "signature does not match",
    "nosuchbucket",
    "no such bucket",
    "bucket does not exist",
    "r2 endpoint, bucket and credentials are required",
    "invalid endpoint",
    "/run/secrets/r2_",
    "/run/secrets/r2-",
)
TIM_MIRROR_LOCK_SECONDS = 120
TIM_MIRROR_JOB_TIMEOUT_SECONDS = 60
TIM_MIRROR_BACKOFF_MAX_SECONDS = 3600
TIM_MEDIA_READ_TTL_SECONDS = 900
TIM_RECALL_LOOKUP_MAX_PAGES = 3
WEB_NATIVE_MEDIA_CLEANUP_BATCH = 200
WEB_NATIVE_UPLOAD_CLEANUP_GRACE_SECONDS = 60 * 60
WEB_NATIVE_MEDIA_UNSENT_GRACE_SECONDS = 24 * 60 * 60
AUTONOMY_CONTROL_INTERVAL_SECONDS = 10
LOGGER = logging.getLogger(__name__)
DEFAULT_MEDIA_HOSTS = (
    "oss.banghua.xin",
    "*.myqcloud.com",
    "*.qcloud.com",
)
CONVERSATION_PREVIEW_METADATA_KEYS = frozenset(
    {
        "last_message",
        "preview_timestamp",
        "preview_sequence",
        "preview_authoritative",
        "preview_timestamp_inferred",
        "last_source",
    }
)
MAX_METADATA_STRING = 20_000
MAX_METADATA_ITEMS = 100
_TIM_SDK_MESSAGE_ID = re.compile(r"^\d{12,}-(\d{9,13})-(\d{1,20})$")
_TIM_HISTORY_MESSAGE_ID = re.compile(r"^\d{1,20}_(\d{1,20})_(\d{9,13})$")


def _default_message_history_transport() -> MessageHistoryTransport:
    """Resolve the legacy TIM adapter only when a sync job needs it."""

    from bbw_web.transports import create_legacy_tim_rest_transport

    return create_legacy_tim_rest_transport()


def _default_message_send_transport() -> MessageMirrorTransport:
    """Resolve the legacy TIM adapter only when a mirror job executes."""

    from bbw_web.transports import create_legacy_tim_rest_transport

    return create_legacy_tim_rest_transport()


def _safe_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(("https://", "http://")):
        return raw[:MAX_METADATA_STRING]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[INVALID_URL]"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return "[INVALID_URL]"
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))[:MAX_METADATA_STRING]


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid {field}") from exc


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _purge_expired_match_history(
    db: Any,
    *,
    at: datetime,
    limit: int = 5000,
) -> int:
    cutoff = at - timedelta(days=MATCH_HISTORY_RETENTION_DAYS)
    ids = list(
        db.scalars(
            select(ActivityEvent.id)
            .where(
                ActivityEvent.provider == MATCH_HISTORY_PROVIDER,
                ActivityEvent.occurred_at <= cutoff,
            )
            .order_by(ActivityEvent.occurred_at, ActivityEvent.id)
            .limit(max(1, min(int(limit), 5000)))
        )
    )
    if not ids:
        return 0
    result = db.execute(delete(ActivityEvent).where(ActivityEvent.id.in_(ids)))
    return int(result.rowcount or 0)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "revoked"}


def _as_int(value: Any, default: int = 0, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Bound JSON stored in metadata/outbox rows without changing useful fields."""

    if depth >= 6:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value.isoformat() if isinstance(value, datetime) else value)
    if isinstance(value, str):
        return _safe_url(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_truncated"] = True
                break
            result[str(key)[:128]] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        result = [_json_safe(item, depth=depth + 1) for item in value[:MAX_METADATA_ITEMS]]
        if len(value) > MAX_METADATA_ITEMS:
            result.append("[TRUNCATED]")
        return result
    return str(value)[:MAX_METADATA_STRING]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_time(value: Any, *, fallback: datetime | None = None) -> datetime:
    now = fallback or utcnow()
    if isinstance(value, datetime):
        parsed = _as_utc(value)
    else:
        raw = str(value or "").strip()
        if not raw:
            return now
        try:
            number = float(raw)
        except (ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                parsed = _as_utc(parsed)
            except ValueError:
                return now
        else:
            absolute = abs(number)
            if absolute >= 10**15:
                number /= 1_000_000
            elif absolute >= 10**12:
                number /= 1_000
            try:
                parsed = datetime.fromtimestamp(number, UTC)
            except (ValueError, OSError, OverflowError):
                return now
    # Reject corrupt timestamps without discarding legitimate old history.
    if parsed < datetime(2000, 1, 1, tzinfo=UTC) or parsed > now + timedelta(days=2):
        return now
    return parsed


def _optional_time(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    return _parse_time(value)


def _stable_identifier(value: Any, *, prefix: str = "") -> str:
    raw = str(value or "").strip()
    if raw and len(raw) <= 256:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}{digest}"[:256]


def _stable_json_digest(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_conversation_id(peer_uid: str, reported: Any = "") -> str:
    peer = _bounded(peer_uid, 128)
    if peer:
        return f"C2C{peer}"[:256]
    return _stable_identifier(reported, prefix="conversation:")


def _normal_message_type(value: Any) -> str:
    raw = _bounded(value, 80).lower()
    aliases = {
        "timtextelem": "text",
        "timimageelem": "image",
        "timsoundelem": "audio",
        "voice": "audio",
        "sound": "audio",
        "timvideofileelem": "video",
        "timfileelem": "file",
        "attachment": "file",
        "flash_photo": "flash",
    }
    return aliases.get(raw, raw or "unknown")[:40]


def _media_quota_kind(message_type: str, mime: str = "") -> str:
    kind = _normal_message_type(message_type)
    if kind in {"image", "flash"}:
        return "image"
    if kind == "audio":
        return "audio"
    if kind == "video":
        return "video"
    if kind == "file":
        return "attachment"
    mime_value = str(mime or "").lower()
    if mime_value.startswith("image/"):
        return "image"
    if mime_value.startswith("audio/"):
        return "audio"
    if mime_value.startswith("video/"):
        return "video"
    return "attachment"


def _load_owner_binding(
    db: Any,
    owner_user_id: uuid.UUID,
    external_account_id: uuid.UUID | None,
    *,
    require_active: bool = True,
) -> tuple[User, ExternalAccount]:
    binding = ExternalAccountRepository(db).get_user_binding(
        owner_user_id,
        external_account_id=external_account_id,
    )
    if binding is None:
        raise NotFoundError("user/account binding was not found")
    user, account = binding
    if require_active and user.status != "active":
        raise NotFoundError("active user was not found")
    return user, account


def _merge_dict(current: Any, update: Mapping[str, Any]) -> dict[str, Any]:
    base = dict(current) if isinstance(current, Mapping) else {}
    base.update({key: _json_safe(value) for key, value in update.items()})
    return base


def _conversation_name_is_placeholder(name: Any, peer: str) -> bool:
    value = str(name or "").strip()
    target = str(peer or "").strip()
    return not value or value in {"用户", "游客", target, f"用户 {target}"}


def _conversation_candidate(
    *,
    owner_user_id: uuid.UUID,
    peer_uid: str,
    reported_id: Any = "",
    title: Any = "",
    unread_count: int | None = None,
    unread_observed_at: datetime | None = None,
    last_message_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    peer = _bounded(peer_uid, 128)
    display_title = _bounded(title, 200)
    if _conversation_name_is_placeholder(display_title, peer):
        display_title = ""
    return {
        "owner_user_id": owner_user_id,
        "provider": CHAT_PROVIDER,
        "upstream_conversation_id": _canonical_conversation_id(peer, reported_id),
        "peer_upstream_uid": peer or None,
        "kind": "direct",
        "title": display_title or None,
        "unread_count": max(0, int(unread_count)) if unread_count is not None else 0,
        "unread_observed_at": _as_utc(unread_observed_at) if unread_observed_at else None,
        "last_message_at": _as_utc(last_message_at) if last_message_at else None,
        "extra_data": _merge_dict({}, metadata or {}),
    }


def _merge_conversation_candidates(
    current: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(current)
    merged["peer_upstream_uid"] = incoming.get("peer_upstream_uid") or current.get(
        "peer_upstream_uid"
    )
    merged["kind"] = incoming.get("kind") or current.get("kind") or "direct"
    merged["title"] = incoming.get("title") or current.get("title")

    current_unread_at = current.get("unread_observed_at")
    incoming_unread_at = incoming.get("unread_observed_at")
    if incoming_unread_at is not None and (
        current_unread_at is None or incoming_unread_at >= current_unread_at
    ):
        merged["unread_count"] = incoming.get("unread_count", 0)
        merged["unread_observed_at"] = incoming_unread_at

    current_last = current.get("last_message_at")
    incoming_last = incoming.get("last_message_at")
    last_message_is_newer = current_last is None or (
        incoming_last is not None and incoming_last >= current_last
    )
    metadata = dict(incoming.get("extra_data") or {})
    if not last_message_is_newer:
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in CONVERSATION_PREVIEW_METADATA_KEYS
        }
    merged["last_message_at"] = incoming_last if last_message_is_newer else current_last
    merged["extra_data"] = _merge_dict(current.get("extra_data"), metadata)
    return merged


def _conversation_candidate_changes(
    existing: Conversation | None, candidate: Mapping[str, Any]
) -> bool:
    if existing is None:
        return True
    if existing.peer_upstream_uid != candidate.get("peer_upstream_uid"):
        return True
    if existing.kind != candidate.get("kind"):
        return True
    incoming_title = candidate.get("title")
    if incoming_title is not None and existing.title != incoming_title:
        return True

    incoming_unread_at = candidate.get("unread_observed_at")
    existing_unread_at = (
        _as_utc(existing.unread_observed_at) if existing.unread_observed_at else None
    )
    if incoming_unread_at is not None and (
        existing_unread_at is None or incoming_unread_at >= existing_unread_at
    ):
        if int(existing.unread_count or 0) != int(candidate.get("unread_count") or 0):
            return True
        if existing_unread_at != incoming_unread_at:
            return True

    existing_last = _as_utc(existing.last_message_at) if existing.last_message_at else None
    incoming_last = candidate.get("last_message_at")
    last_message_is_newer = existing_last is None or (
        incoming_last is not None and incoming_last >= existing_last
    )
    if last_message_is_newer and existing_last != incoming_last:
        return True
    metadata = dict(candidate.get("extra_data") or {})
    if not last_message_is_newer:
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in CONVERSATION_PREVIEW_METADATA_KEYS
        }
    return _merge_dict(existing.extra_data, metadata) != dict(existing.extra_data or {})


def _upsert_conversations(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, Conversation]:
    deduplicated: dict[str, dict[str, Any]] = {}
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        upstream_id = str(candidate.get("upstream_conversation_id") or "").strip()
        if not upstream_id:
            continue
        previous = deduplicated.get(upstream_id)
        deduplicated[upstream_id] = (
            _merge_conversation_candidates(previous, candidate) if previous else candidate
        )
    if not deduplicated:
        return {}

    repository = ConversationRepository(db)
    resolved: dict[str, Conversation] = {}
    # Keep lock acquisition order stable when live ingestion and reconciliation
    # touch the same owner's conversations concurrently.
    upstream_ids = sorted(deduplicated)
    for start in range(0, len(upstream_ids), 250):
        chunk_ids = upstream_ids[start : start + 250]
        existing = repository.list_by_upstream_ids(owner_user_id, CHAT_PROVIDER, chunk_ids)
        changed = [
            deduplicated[upstream_id]
            for upstream_id in chunk_ids
            if _conversation_candidate_changes(existing.get(upstream_id), deduplicated[upstream_id])
        ]
        written = repository.upsert_many(changed)
        resolved.update(existing)
        resolved.update({row.upstream_conversation_id: row for row in written})
    return resolved


def _upsert_conversation(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    peer_uid: str,
    reported_id: Any = "",
    title: Any = "",
    unread_count: int | None = None,
    unread_observed_at: datetime | None = None,
    last_message_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
):
    candidate = _conversation_candidate(
        owner_user_id=owner_user_id,
        peer_uid=peer_uid,
        reported_id=reported_id,
        title=title,
        unread_count=unread_count,
        unread_observed_at=unread_observed_at,
        last_message_at=last_message_at,
        metadata=metadata,
    )
    rows = _upsert_conversations(db, owner_user_id=owner_user_id, candidates=[candidate])
    return rows[candidate["upstream_conversation_id"]]


def _media_spec_from_report(report: Mapping[str, Any], message_type: str) -> dict[str, Any] | None:
    raw = report.get("media")
    if not isinstance(raw, Mapping):
        return None
    source_url = _bounded(raw.get("url") or raw.get("thumbnail"), 4096)
    if not source_url.startswith("https://"):
        return None
    mime = _bounded(raw.get("mime"), 160)
    return {
        "source_url": source_url,
        "kind": _media_quota_kind(message_type, mime),
        "reported_kind": _normal_message_type(message_type),
        "original_name": _bounded(raw.get("name"), 255),
        "reported": _json_safe(raw),
    }


def _media_source_context(owner_user_id: uuid.UUID, message_id: uuid.UUID, digest: str) -> str:
    return f"media-outbox:{owner_user_id}:{message_id}:{digest}:source-url"


def _message_status(report: Mapping[str, Any], direction: str) -> str:
    if _as_bool(report.get("revoked")):
        return "revoked"
    raw = _bounded(report.get("delivery") or report.get("status"), 32).lower()
    if raw:
        return raw
    return "sent" if direction == "outgoing" else "received"


def _prefer_message_status(current: str, incoming: str) -> str:
    if current == "revoked" or incoming == "revoked":
        return "revoked"
    rank = {
        "unknown": 0,
        "pending": 1,
        "sending": 1,
        "failed": 1,
        "sent": 2,
        "received": 2,
        "delivered": 3,
        "read": 4,
    }
    return incoming if rank.get(incoming, 1) >= rank.get(current, 1) else current


def _tim_message_random(report: Mapping[str, Any]) -> str:
    for key in ("message_random", "msg_random", "MsgRandom"):
        candidate = str(report.get(key) or "").strip()
        if candidate.isdigit() and len(candidate) <= 20:
            return candidate
    for key in ("upstream_message_id", "message_key", "upstream_message_key"):
        candidate = str(report.get(key) or "").strip()
        sdk_match = _TIM_SDK_MESSAGE_ID.fullmatch(candidate)
        if sdk_match:
            return sdk_match.group(2)
        history_match = _TIM_HISTORY_MESSAGE_ID.fullmatch(candidate)
        if history_match:
            return history_match.group(1)
    return ""


def _tim_message_sequence(report: Mapping[str, Any]) -> str:
    for key in (
        "message_sequence",
        "sequence",
        "MsgSeq",
        "msgSeq",
        "msg_seq",
        "seq",
    ):
        candidate = str(report.get(key) or "").strip()
        if candidate:
            return candidate[:80]
    if not _as_bool(report.get("revoked")):
        return ""
    for key in ("upstream_message_id", "message_key", "upstream_message_key"):
        candidate = str(report.get(key) or "").strip()
        if candidate.isdigit() and len(candidate) <= 20:
            return candidate
    return ""


def _message_identifier(
    report: Mapping[str, Any],
    peer_uid: str,
    *,
    sender_uid: str = "",
    recipient_uid: str = "",
) -> str:
    message_random = _tim_message_random(report)
    sender = str(sender_uid or "").strip()
    recipient = str(recipient_uid or "").strip()
    if message_random and sender and recipient:
        identity = "\x1f".join(("tim-c2c", sender, recipient, message_random))
        return f"tim-c2c:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
    for key in (
        "upstream_message_id",
        "message_key",
        "upstream_message_key",
        "client_message_key",
        "flash_id",
        "idempotency_key",
    ):
        candidate = str(report.get(key) or "").strip()
        if candidate:
            return _stable_identifier(candidate, prefix="message:")
    fingerprint = {
        "peer_uid": peer_uid,
        "sent_at": report.get("sent_at"),
        "direction": report.get("direction"),
        "message_type": report.get("message_type"),
        "object_name": report.get("object_name"),
        "text": report.get("text"),
        "media": report.get("media"),
    }
    return f"derived:{_stable_json_digest(fingerprint)}"


def _message_key_quality(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.startswith("web-message:"):
        return 1
    return 2


def _merge_message_metadata(current: Any, update: Mapping[str, Any]) -> dict[str, Any]:
    previous = dict(current) if isinstance(current, Mapping) else {}
    incoming = {key: _json_safe(value) for key, value in update.items()}
    merged = dict(previous)
    for key, value in incoming.items():
        if value not in (None, "", [], {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value

    source_rank = {"browser": 0, "tim_sdk": 1, "rest": 2, "history": 3}
    previous_source = str(previous.get("source") or "")
    incoming_source = str(incoming.get("source") or "")
    merged["source"] = max(
        (previous_source, incoming_source),
        key=lambda value: source_rank.get(value, 0),
    )

    previous_key = previous.get("message_key")
    incoming_key = incoming.get("message_key")
    merged["message_key"] = max(
        (previous_key, incoming_key), key=_message_key_quality
    ) or ""
    for key in (
        "canonical_message_id",
        "client_message_id",
        "message_sequence",
        "client_message_key",
        "read_at",
        "object_name",
    ):
        merged[key] = incoming.get(key) or previous.get(key) or ""

    merged["revoked"] = bool(previous.get("revoked") or incoming.get("revoked"))
    read_states = (previous.get("is_peer_read"), incoming.get("is_peer_read"))
    if True in read_states:
        merged["is_peer_read"] = True
    elif False in read_states:
        merged["is_peer_read"] = False
    else:
        merged["is_peer_read"] = None

    previous_media = previous.get("media_report")
    incoming_media = incoming.get("media_report")
    if isinstance(incoming_media, Mapping) and any(incoming_media.values()):
        merged["media_report"] = dict(incoming_media)
    elif isinstance(previous_media, Mapping):
        merged["media_report"] = dict(previous_media)

    aliases: list[str] = []
    for value in (
        *(previous.get("raw_upstream_message_ids") or []),
        previous.get("raw_upstream_message_id"),
        *(incoming.get("raw_upstream_message_ids") or []),
        incoming.get("raw_upstream_message_id"),
    ):
        raw = str(value or "").strip()
        if raw and raw not in aliases:
            aliases.append(raw[:512])
    merged["raw_upstream_message_ids"] = aliases[:20]
    merged["raw_upstream_message_id"] = aliases[-1] if aliases else ""
    merged["message_random"] = (
        incoming.get("message_random") or previous.get("message_random") or ""
    )
    return merged


def _find_message_by_sequence(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    sequence: str,
    direction: str,
    sender_uid: str | None,
    recipient_uid: str | None,
) -> Message | None:
    normalized = str(sequence or "").strip()
    if not normalized:
        return None
    conditions = [
        Message.owner_user_id == owner_user_id,
        Message.conversation_id == conversation_id,
        Message.provider == CHAT_PROVIDER,
        or_(
            Message.extra_data["message_sequence"].astext == normalized,
            Message.upstream_message_id == normalized,
        ),
    ]
    if direction != "unknown":
        conditions.append(Message.direction.in_((direction, "unknown")))
    if sender_uid:
        conditions.append(
            or_(
                Message.sender_upstream_uid == sender_uid,
                Message.sender_upstream_uid.is_(None),
            )
        )
    if recipient_uid:
        conditions.append(
            or_(
                Message.recipient_upstream_uid == recipient_uid,
                Message.recipient_upstream_uid.is_(None),
            )
        )
    rows = list(db.scalars(select(Message).where(*conditions).limit(20)))
    if not rows:
        return None

    def quality(row: Message) -> tuple[int, int, int, int]:
        metadata = dict(row.extra_data or {}) if isinstance(row.extra_data, Mapping) else {}
        return (
            1 if str(metadata.get("message_random") or "").strip() else 0,
            1 if str(row.body or "") else 0,
            1 if str(row.upstream_message_id or "").startswith("tim-c2c:") else 0,
            len(str(metadata.get("raw_upstream_message_ids") or "")),
        )

    return max(rows, key=quality)


def _ingest_message(
    db: Any,
    *,
    settings: Settings,
    user: User,
    account: ExternalAccount,
    report: Mapping[str, Any],
    conversation: Conversation | None = None,
    occurred_at: datetime | None = None,
    enqueue_media_archive: bool = True,
) -> tuple[Message, bool, uuid.UUID | None]:
    peer_uid = _bounded(report.get("peer_uid"), 128)
    if not peer_uid:
        raise ValueError("peer_uid is required")
    direction = _bounded(report.get("direction"), 16).lower()
    if direction not in {"incoming", "outgoing", "unknown"}:
        direction = "unknown"
    if occurred_at is None:
        occurred_at = _parse_time(report.get("sent_at") or report.get("observed_at"))
    message_type = _normal_message_type(report.get("message_type") or report.get("type"))
    if conversation is None:
        conversation = _upsert_conversation(
            db,
            owner_user_id=user.id,
            peer_uid=peer_uid,
            reported_id=report.get("conversation_id"),
            last_message_at=occurred_at,
            metadata={
                "reported_conversation_id": _bounded(report.get("conversation_id"), 256),
                "last_source": _bounded(report.get("source"), 64) or "browser",
            },
        )
    upstream_uid = _bounded(account.upstream_uid, 128)
    if direction == "outgoing":
        sender_uid, recipient_uid = upstream_uid or None, peer_uid
    elif direction == "incoming":
        sender_uid, recipient_uid = peer_uid, upstream_uid or None
    else:
        sender_uid = _bounded(report.get("sender_upstream_uid"), 128) or None
        recipient_uid = _bounded(report.get("recipient_upstream_uid"), 128) or None
    raw_upstream_message_id = _bounded(report.get("upstream_message_id"), 512)
    message_random = _tim_message_random(report)
    message_sequence = _tim_message_sequence(report)
    metadata = {
        "schema_version": _as_int(report.get("schema_version"), 1, minimum=1, maximum=10),
        "source": _bounded(report.get("source"), 64) or "browser",
        "object_name": _bounded(report.get("object_name"), 128),
        "message_key": _bounded(
            report.get("message_key") or report.get("upstream_message_key"), 512
        ),
        "message_sequence": message_sequence,
        "message_random": message_random,
        "client_message_key": _bounded(report.get("client_message_key"), 512),
        "canonical_message_id": _bounded(report.get("canonical_message_id"), 128),
        "client_message_id": _bounded(report.get("client_message_id"), 160),
        "idempotency_key": _bounded(report.get("idempotency_key"), 256),
        "observed_at": _bounded(report.get("observed_at"), 80),
        "revoked": _as_bool(report.get("revoked")),
        "is_peer_read": report.get("is_peer_read") if isinstance(report.get("is_peer_read"), bool) else None,
        "read_at": _bounded(report.get("read_at"), 80),
        "flash_id": _bounded(report.get("flash_id"), 512),
        "media_report": _json_safe(report.get("media")),
        "quote": normalize_message_quote(report.get("quote")),
        "raw_upstream_message_id": raw_upstream_message_id,
        "raw_upstream_message_ids": [raw_upstream_message_id]
        if raw_upstream_message_id
        else [],
    }
    retention_days = max(1, int(user.chat_retention_days or settings.message_retention_days))
    upstream_message_id = _message_identifier(
        report,
        peer_uid,
        sender_uid=sender_uid or "",
        recipient_uid=recipient_uid or "",
    )
    repository = MessageRepository(db)
    row = _find_message_by_sequence(
        db,
        owner_user_id=user.id,
        conversation_id=conversation.id,
        sequence=message_sequence,
        direction=direction,
        sender_uid=sender_uid,
        recipient_uid=recipient_uid,
    )
    created = False
    if row is not None and message_random and row.upstream_message_id != upstream_message_id:
        canonical = repository.get_by_upstream(user.id, CHAT_PROVIDER, upstream_message_id)
        if canonical is not None:
            row = canonical
        else:
            row.upstream_message_id = upstream_message_id
    if row is None:
        row, created = repository.insert_idempotent(
            owner_user_id=user.id,
            conversation_id=conversation.id,
            provider=CHAT_PROVIDER,
            upstream_message_id=upstream_message_id,
            direction=direction,
            sender_upstream_uid=sender_uid,
            recipient_upstream_uid=recipient_uid,
            message_type=message_type,
            body=str(report.get("text") or "")[:100_000] or None,
            status=_message_status(report, direction),
            occurred_at=occurred_at,
            retention_expires_at=utcnow() + timedelta(days=retention_days),
            extra_data=metadata,
        )
    if not created:
        # Idempotency must not discard later delivery/read/revoke information.
        incoming_status = _message_status(report, direction)
        row.status = _prefer_message_status(str(row.status or "unknown"), incoming_status)
        if not row.body and report.get("text"):
            row.body = str(report.get("text"))[:100_000]
        previous_metadata = dict(row.extra_data or {})
        merged_metadata = _merge_message_metadata(previous_metadata, metadata)
        previous_read = previous_metadata.get("is_peer_read")
        incoming_read = metadata.get("is_peer_read")
        if previous_read is True:
            merged_metadata["is_peer_read"] = True
        elif isinstance(incoming_read, bool):
            merged_metadata["is_peer_read"] = incoming_read
        elif "is_peer_read" in previous_metadata:
            merged_metadata["is_peer_read"] = previous_read
        if not metadata.get("read_at") and previous_metadata.get("read_at"):
            merged_metadata["read_at"] = previous_metadata["read_at"]
        row.extra_data = merged_metadata
        if occurred_at < row.occurred_at:
            row.occurred_at = occurred_at
        if row.direction == "unknown" and direction != "unknown":
            row.direction = direction
        if not row.sender_upstream_uid and sender_uid:
            row.sender_upstream_uid = sender_uid
        if not row.recipient_upstream_uid and recipient_uid:
            row.recipient_upstream_uid = recipient_uid
        db.flush()

    outbox_id: uuid.UUID | None = None
    media_spec = _media_spec_from_report(report, message_type)
    if media_spec and enqueue_media_archive:
        digest = hashlib.sha256(
            f"{row.id}\n{media_spec['source_url']}".encode("utf-8", errors="replace")
        ).hexdigest()
        encrypted_source_url = CredentialCipher.from_settings(settings).encrypt_text(
            media_spec["source_url"],
            purpose="media-source.url",
            context=_media_source_context(user.id, row.id, digest),
        )
        operation, _ = OperationOutboxRepository(db).enqueue(
            owner_user_id=user.id,
            operation_type=MEDIA_OPERATION,
            aggregate_type="message",
            aggregate_id=str(row.id),
            idempotency_key=digest,
            payload={
                "message_id": str(row.id),
                "external_account_id": str(account.id),
                "source_url_encrypted": encrypted_source_url,
                "source_url_hash": digest,
                "source_url_safe": _safe_url(media_spec["source_url"]),
                "kind": media_spec["kind"],
                "reported_kind": media_spec["reported_kind"],
                "original_name": media_spec["original_name"],
                "reported": media_spec["reported"],
            },
            status="pending",
            max_attempts=MEDIA_MAX_ATTEMPTS,
        )
        outbox_id = operation.id
    return row, created, outbox_id


def _duplicate_queue_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        isinstance(exc, InvalidJobOperation)
        or "already exists" in message
        or "already been enqueued" in message
        or "job exists" in message
    )


def _worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class _ClaimedTimMirror:
    delivery_id: uuid.UUID
    payload: dict[str, Any]
    attempt: int
    max_attempts: int


def _tim_mirror_due(row: MessageDelivery, *, now: datetime) -> bool:
    status = str(row.status or "")
    locked_until = _as_utc(row.locked_until) if row.locked_until else None
    if status in {"pending", "retry"}:
        return _as_utc(row.available_at) <= now and (
            locked_until is None or locked_until <= now
        )
    if status == "processing":
        return locked_until is None or locked_until <= now
    return False


def _claim_tim_message_delivery(delivery_id: uuid.UUID) -> _ClaimedTimMirror | None:
    """Claim one TIM outbox row; concurrent workers skip the locked row."""

    now = utcnow()
    with session_scope() as db:
        row = db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.id == delivery_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.required.is_(False),
            )
            .with_for_update(skip_locked=True)
        )
        if row is None or not _tim_mirror_due(row, now=now):
            return None
        if row.attempt_count >= row.max_attempts:
            row.status = "failed"
            row.locked_by = None
            row.locked_until = None
            row.last_error = row.last_error or "TIM mirror retry budget exhausted"
            return None
        row.status = "processing"
        row.attempt_count += 1
        row.locked_by = _worker_identity()
        row.locked_until = now + timedelta(seconds=TIM_MIRROR_LOCK_SECONDS)
        row.last_error = None
        db.flush()
        return _ClaimedTimMirror(
            delivery_id=row.id,
            payload=dict(row.payload or {}),
            attempt=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
        )


def _tim_mirror_cloud_custom_data(payload: Mapping[str, Any]) -> str:
    canonical_message_id = _bounded(payload.get("canonical_message_id"), 128)
    client_message_id = _bounded(payload.get("client_message_id"), 160)
    if not canonical_message_id or not client_message_id:
        raise ValueError("TIM mirror payload has no canonical message identity")
    quote = normalize_message_quote(payload.get("quote"))
    cloud: dict[str, Any] = {}
    encoded_quote = encode_message_quote(quote)
    if encoded_quote:
        decoded = json.loads(encoded_quote)
        if isinstance(decoded, Mapping):
            cloud.update(decoded)
    local_identity: dict[str, Any] = {
        "canonical_message_id": canonical_message_id,
        "client_message_id": client_message_id,
        "message_id": canonical_message_id,
        "quote": quote,
        "version": 1,
    }
    for key, limit in (
        ("attachment_id", 128),
        ("asset_id", 128),
        ("message_type", 32),
        ("operation", 16),
    ):
        value = _bounded(payload.get(key), limit)
        if value:
            local_identity[key] = value
    cloud["bbw_message"] = local_identity
    return json.dumps(cloud, ensure_ascii=False, separators=(",", ":"))


def _tim_mirror_send_values(
    payload: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    from_uid = _bounded(payload.get("from"), 128)
    to_uid = _bounded(payload.get("to"), 128)
    text = str(payload.get("text") or "")
    if (
        not from_uid
        or not to_uid
        or from_uid == to_uid
        or not text.strip()
        or len(text) > 2000
    ):
        raise ValueError("TIM mirror payload is not sendable")
    return from_uid, to_uid, text, _tim_mirror_cloud_custom_data(payload)


def _tim_mirror_parties(payload: Mapping[str, Any]) -> tuple[str, str]:
    from_uid = _bounded(payload.get("from"), 128)
    to_uid = _bounded(payload.get("to"), 128)
    if not from_uid or not to_uid or from_uid == to_uid:
        raise ValueError("TIM mirror payload has invalid participants")
    return from_uid, to_uid


def _tim_media_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    report = payload.get("media_report")
    if not isinstance(report, Mapping):
        raise ValueError("TIM media mirror payload has no media report")
    result = dict(report)
    attachment_id = _bounded(
        payload.get("attachment_id") or result.get("attachment_id"), 128
    )
    asset_id = _bounded(payload.get("asset_id") or result.get("asset_id"), 128)
    if not attachment_id or not asset_id:
        raise ValueError("TIM media mirror payload has no attachment identity")
    result["attachment_id"] = attachment_id
    result["asset_id"] = asset_id
    return result


def _tim_media_asset_and_url(
    payload: Mapping[str, Any],
) -> tuple[Any, str]:
    """Resolve one private local asset and issue a bounded compatibility URL."""

    from bbw_web.media_native import (
        ATTACHMENT_STATUS_SENT,
        SqlAlchemyMediaNativeRepository,
    )
    from bbw_web.media_native.r2_adapter import R2PrivateMediaAdapter

    report = _tim_media_report(payload)
    attachment_id = _uuid(report["attachment_id"], field="attachment_id")
    asset_id = _uuid(report["asset_id"], field="asset_id")
    with session_scope() as db:
        repository = SqlAlchemyMediaNativeRepository(db)
        attachment = repository.get_media_attachment(attachment_id)
        asset = repository.get_media_asset(asset_id)
        if (
            attachment is None
            or attachment.status != ATTACHMENT_STATUS_SENT
            or attachment.message_id
            != _uuid(payload.get("canonical_message_id"), field="canonical_message_id")
            or attachment.payload.asset_id != asset_id
            or asset is None
            or asset.owner_user_id != attachment.sender_user_id
        ):
            raise ValueError("TIM media mirror asset is unavailable or revoked")

    settings = get_settings()
    adapter = R2PrivateMediaAdapter(
        settings,
        deployment=str(getattr(settings, "environment", "development")),
    )
    read = adapter.issue_private_read(
        asset,
        expires_at=utcnow() + timedelta(seconds=TIM_MEDIA_READ_TTL_SECONDS),
    )
    url = str(read.url or "").strip()
    if not url.startswith("https://"):
        raise ValueError("TIM media compatibility URL is unavailable")
    return asset, url


def _tim_media_element(
    payload: Mapping[str, Any],
    *,
    asset: Any,
    url: str,
) -> dict[str, Any]:
    report = _tim_media_report(payload)
    kind = _bounded(payload.get("message_type"), 32).lower()
    if kind not in {"image", "audio", "video", "file"}:
        raise ValueError("TIM media mirror kind is unsupported")
    media_uuid = _bounded(report.get("asset_id"), 128)
    size = max(0, int(report.get("size") or getattr(asset, "size_bytes", 0) or 0))
    filename = _bounded(
        report.get("name") or getattr(asset, "filename", "") or "文件",
        255,
    )
    if kind == "image":
        content_type = _bounded(
            report.get("mime") or getattr(asset, "content_type", ""), 160
        ).lower()
        image_format = {
            "image/gif": 2,
            "image/png": 3,
            "image/bmp": 4,
        }.get(content_type, 1)
        return {
            "MsgType": "TIMImageElem",
            "MsgContent": {
                "UUID": media_uuid,
                "ImageFormat": image_format,
                "ImageInfoArray": [
                    {
                        "Type": 0,
                        "Size": size,
                        "Width": max(0, int(report.get("width") or 0)),
                        "Height": max(0, int(report.get("height") or 0)),
                        "URL": url,
                    }
                ],
            },
        }
    if kind == "audio":
        duration = max(1, int(math.ceil(float(report.get("duration") or 0))))
        return {
            "MsgType": "TIMSoundElem",
            "MsgContent": {
                "Url": url,
                "UUID": media_uuid,
                "Size": size,
                "Second": duration,
                "Download_Flag": 2,
            },
        }
    # TIM REST cannot upload a local video thumbnail.  Mirror video as a file
    # so APK users still receive an attachment without inventing a public image.
    return {
        "MsgType": "TIMFileElem",
        "MsgContent": {
            "Url": url,
            "UUID": media_uuid,
            "FileSize": size,
            "FileName": filename,
            "Download_Flag": 2,
        },
    }


def _tim_mirror_delivery_is_processing(delivery_id: uuid.UUID) -> bool:
    with session_scope() as db:
        status = db.scalar(
            select(MessageDelivery.status).where(
                MessageDelivery.id == delivery_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
            )
        )
    return str(status or "") == "processing"


def _tim_media_send_result(
    delivery_id: uuid.UUID,
    payload: Mapping[str, Any],
    sender: MessageMirrorTransport,
) -> Any:
    from_uid, to_uid = _tim_mirror_parties(payload)
    message_type = _bounded(payload.get("message_type"), 32).lower()
    if not _tim_mirror_delivery_is_processing(delivery_id):
        raise RuntimeError("TIM media send was cancelled before upstream dispatch")
    if message_type == "flash":
        return sender.send_text(
            from_uid,
            to_uid,
            "收到一张闪照，请在 Web 端查看",
            cloud_custom_data=_tim_mirror_cloud_custom_data(payload),
            sync_other_machine=1,
            idempotency_key=_bounded(payload.get("canonical_message_id"), 128),
        )
    asset, url = _tim_media_asset_and_url(payload)
    # Resolving a private URL can overlap a local revoke transaction.  Recheck
    # immediately before the external call so a cancelled delivery is not sent.
    if not _tim_mirror_delivery_is_processing(delivery_id):
        raise RuntimeError("TIM media send was cancelled before upstream dispatch")
    element = _tim_media_element(payload, asset=asset, url=url)
    return sender.send_elements(
        from_uid,
        to_uid,
        [element],
        cloud_custom_data=_tim_mirror_cloud_custom_data(payload),
        sync_other_machine=1,
        idempotency_key=_bounded(payload.get("canonical_message_id"), 128),
    )


def _tim_send_delivery_target_key(payload: Mapping[str, Any]) -> str:
    _from_uid, to_uid = _tim_mirror_parties(payload)
    message_type = _bounded(payload.get("message_type"), 32).lower()
    return to_uid if message_type == "text" else f"media-send:{to_uid}"


def _tim_send_upstream_key(payload: Mapping[str, Any]) -> tuple[str, str]:
    canonical_message_id = _uuid(
        payload.get("canonical_message_id"), field="canonical_message_id"
    )
    target_key = _tim_send_delivery_target_key(payload)
    with session_scope() as db:
        row = db.scalar(
            select(MessageDelivery).where(
                MessageDelivery.message_id == canonical_message_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.target_key == target_key,
            )
        )
        if row is None:
            return "missing", ""
        return str(row.status or ""), _bounded(
            dict(row.payload or {}).get("upstream_message_id"), 256
        )


def _tim_media_send_upstream_key(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Backward-compatible name for text/media revoke compensation lookup."""

    return _tim_send_upstream_key(payload)


def _tim_mirror_dedup_identity(payload: Mapping[str, Any]) -> tuple[int, int]:
    canonical_message_id = _bounded(payload.get("canonical_message_id"), 128)
    if not canonical_message_id:
        raise ValueError("TIM mirror payload has no canonical message identity")
    digest = hashlib.sha256(canonical_message_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") or 1, int.from_bytes(
        digest[4:8], "big"
    ) or 1


def _tim_recall_history_key(
    payload: Mapping[str, Any],
    transport: MessageRecallLookupTransport,
) -> str:
    """Recover a missing MsgKey from bounded roaming history.

    TIM send retries reuse deterministic MsgSeq/MsgRandom values derived from
    the canonical message ID.  Matching both that pair and CloudCustomData
    keeps compensation idempotent even when the original send response omitted
    MsgKey or the local worker was cancelled before acknowledging it.
    """

    from_uid, to_uid = _tim_mirror_parties(payload)
    canonical_message_id = _bounded(payload.get("canonical_message_id"), 128)
    expected_sequence, expected_random = _tim_mirror_dedup_identity(payload)
    last_msg_key = ""
    max_time = int(utcnow().timestamp()) + 60
    seen_cursors: set[tuple[str, int]] = set()
    for _page in range(TIM_RECALL_LOOKUP_MAX_PAGES):
        result = transport.roaming_messages(
            from_uid,
            to_uid,
            min_time=0,
            max_time=max_time,
            max_count=100,
            last_msg_key=last_msg_key,
        )
        if not bool(getattr(result, "ok", False)):
            code = int(getattr(result, "error_code", 0) or 0)
            info = _bounded(getattr(result, "error_info", ""), 500)
            raise RuntimeError(
                f"TIM recall history lookup failed ({code}): {info or 'unknown error'}"
            )
        data = getattr(result, "data", None)
        data = data if isinstance(data, Mapping) else {}
        rows = data.get("MsgList")
        rows = rows if isinstance(rows, list) else []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            cloud_custom_data = (
                raw.get("CloudCustomData")
                or raw.get("cloudCustomData")
                or raw.get("cloud_custom_data")
                or ""
            )
            local_identity = extract_local_message_identity(cloud_custom_data)
            identity_matches = (
                _bounded(local_identity.get("canonical_message_id"), 128)
                == canonical_message_id
            )
            sequence_matches = (
                _as_int(raw.get("MsgSeq"), 0, maximum=0xFFFFFFFF)
                == expected_sequence
                and _as_int(raw.get("MsgRandom"), 0, maximum=0xFFFFFFFF)
                == expected_random
            )
            if not identity_matches and not sequence_matches:
                continue
            for key in ("MsgKey", "MsgUID", "msg_key", "msg_uid", "message_id"):
                upstream_message_id = _bounded(raw.get(key), 256)
                if upstream_message_id:
                    return upstream_message_id

        next_key = _bounded(data.get("LastMsgKey"), 256)
        next_time = _as_int(data.get("LastMsgTime"), 0)
        complete = str(data.get("Complete") or "0").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        cursor = (next_key, next_time)
        if complete or not rows or not next_key or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        last_msg_key = next_key
        if next_time > 0:
            max_time = min(max_time, next_time)
    return ""


def _remember_tim_send_upstream_key(
    payload: Mapping[str, Any], upstream_message_id: str
) -> None:
    key = _bounded(upstream_message_id, 256)
    if not key:
        return
    canonical_message_id = _uuid(
        payload.get("canonical_message_id"), field="canonical_message_id"
    )
    target_key = _tim_send_delivery_target_key(payload)
    with session_scope() as db:
        row = db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.message_id == canonical_message_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.target_key == target_key,
            )
            .with_for_update()
        )
        if row is None:
            return
        row.payload = {
            **dict(row.payload or {}),
            "upstream_message_id": key,
            "upstream_message_id_recovered": True,
        }


def _remember_tim_media_send_upstream_key(
    payload: Mapping[str, Any], upstream_message_id: str
) -> None:
    """Backward-compatible name for text/media compensation persistence."""

    _remember_tim_send_upstream_key(payload, upstream_message_id)


def _tim_media_revoke_result(
    payload: Mapping[str, Any],
    sender: MessageMirrorTransport,
    *,
    delivery_id: uuid.UUID | None = None,
) -> Any:
    if delivery_id is not None and not _tim_mirror_delivery_is_processing(
        delivery_id
    ):
        raise RuntimeError("TIM revoke was cancelled before upstream dispatch")
    from_uid, to_uid = _tim_mirror_parties(payload)
    send_status, upstream_message_id = _tim_media_send_upstream_key(payload)
    if not upstream_message_id and isinstance(sender, MessageRecallLookupTransport):
        if delivery_id is not None and not _tim_mirror_delivery_is_processing(
            delivery_id
        ):
            raise RuntimeError(
                "TIM revoke was cancelled before upstream history lookup"
            )
        upstream_message_id = _tim_recall_history_key(payload, sender)
        if upstream_message_id:
            _remember_tim_media_send_upstream_key(payload, upstream_message_id)
    if not upstream_message_id:
        raise RuntimeError(
            f"TIM send result is not ready for revoke ({send_status or 'unknown'})"
        )
    if delivery_id is not None and not _tim_mirror_delivery_is_processing(
        delivery_id
    ):
        raise RuntimeError("TIM revoke was cancelled before upstream dispatch")
    result = sender.revoke_c2c(from_uid, to_uid, upstream_message_id)
    if not bool(getattr(result, "ok", False)) and int(
        getattr(result, "error_code", 0) or 0
    ) not in {20022, 20023}:
        return result
    return result


def _remember_cancelled_tim_upstream_id(
    delivery_id: uuid.UUID,
    upstream_message_id: str,
) -> None:
    if not upstream_message_id:
        return
    with session_scope() as db:
        row = db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.id == delivery_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
            )
            .with_for_update()
        )
        if row is None or str(row.status or "") != "cancelled":
            return
        row.payload = {
            **dict(row.payload or {}),
            "upstream_message_id": upstream_message_id[:256],
            "upstream_send_completed_after_cancel": True,
        }


def _tim_mirror_upstream_id(result: Any) -> str:
    data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        return ""
    for key in ("MsgKey", "MsgUID", "msg_key", "msg_uid", "message_id"):
        value = _bounded(data.get(key), 256)
        if value:
            return value
    return ""


def _tim_mirror_backoff_seconds(attempt: int) -> int:
    exponent = min(max(0, int(attempt) - 1), 7)
    return min(TIM_MIRROR_BACKOFF_MAX_SECONDS, 30 * (2**exponent))


def _mark_tim_mirror_delivered(
    delivery_id: uuid.UUID,
    *,
    upstream_message_id: str,
) -> str:
    with session_scope() as db:
        row = SqlAlchemyCanonicalMessageStore(db).mark_tim_delivered(
            delivery_id,
            delivered_at=utcnow(),
            upstream_message_id=upstream_message_id,
        )
        return str(row.status) if row is not None else "missing"


def _mark_tim_mirror_failed(
    delivery_id: uuid.UUID,
    *,
    attempt: int,
    error: str,
) -> tuple[str, int]:
    delay = _tim_mirror_backoff_seconds(attempt)
    with session_scope() as db:
        row = SqlAlchemyCanonicalMessageStore(db).mark_tim_failed(
            delivery_id,
            error=error,
            retry_at=utcnow() + timedelta(seconds=delay),
        )
        return (str(row.status) if row is not None else "missing", delay)


def mirror_tim_message_delivery(
    delivery_id: str,
    *,
    transport: MessageSendTransport | MessageMirrorTransport | None = None,
) -> dict[str, Any]:
    """Mirror one canonical local message to TIM without changing local success."""

    operation_id = _uuid(delivery_id, field="delivery_id")
    mode = compatibility_mode()
    if mode is not CompatibilityMode.ENABLED:
        return {
            "ok": True,
            "ignored": True,
            "delivery_id": str(operation_id),
            "reason": "legacy compatibility dispatch is disabled",
            "compatibility_mode": mode.value,
        }
    claimed = _claim_tim_message_delivery(operation_id)
    if claimed is None:
        return {"ok": True, "ignored": True, "delivery_id": str(operation_id)}
    payload = claimed.payload
    operation = _bounded(payload.get("operation"), 16).lower() or "send"
    message_type = _bounded(payload.get("message_type"), 32).lower() or "text"
    try:
        enforce_state_recheck = transport is None
        sender = transport or _default_message_send_transport()
        if operation == "revoke":
            if not isinstance(sender, MessageMirrorTransport):
                raise RuntimeError("TIM mirror transport cannot revoke messages")
            result = _tim_media_revoke_result(
                payload,
                sender,
                delivery_id=operation_id if enforce_state_recheck else None,
            )
        elif message_type == "text":
            from_uid, to_uid, text, cloud_custom_data = _tim_mirror_send_values(
                payload
            )
            if enforce_state_recheck and not _tim_mirror_delivery_is_processing(
                operation_id
            ):
                raise RuntimeError("TIM text send was cancelled before upstream dispatch")
            result = sender.send_text(
                from_uid,
                to_uid,
                text,
                cloud_custom_data=cloud_custom_data,
                sync_other_machine=1,
                idempotency_key=_bounded(
                    payload.get("canonical_message_id"), 128
                ),
            )
        else:
            if not isinstance(sender, MessageMirrorTransport):
                raise RuntimeError("TIM mirror transport cannot send media")
            result = _tim_media_send_result(operation_id, payload, sender)
        accepted_missing_revoke = operation == "revoke" and int(
            getattr(result, "error_code", 0) or 0
        ) in {20022, 20023}
        if not bool(getattr(result, "ok", False)) and not accepted_missing_revoke:
            code = int(getattr(result, "error_code", 0) or 0)
            info = _bounded(getattr(result, "error_info", ""), 500)
            raise RuntimeError(f"TIM mirror rejected ({code}): {info or 'unknown error'}")
    except Exception as exc:
        status, delay = _mark_tim_mirror_failed(
            operation_id,
            attempt=claimed.attempt,
            error=str(exc)[:2000],
        )
        return {
            "ok": False,
            "delivery_id": str(operation_id),
            "local_delivery_unchanged": True,
            "retry": status == "retry",
            "retry_after": delay if status == "retry" else 0,
            "status": status,
        }

    upstream_message_id = _tim_mirror_upstream_id(result)
    status = _mark_tim_mirror_delivered(
        operation_id,
        upstream_message_id=upstream_message_id,
    )
    if status == "cancelled" and operation == "send":
        _remember_cancelled_tim_upstream_id(operation_id, upstream_message_id)
    return {
        "ok": status == "delivered",
        "canonical_message_id": _bounded(payload.get("canonical_message_id"), 128),
        "client_message_id": _bounded(payload.get("client_message_id"), 160),
        "delivery_id": str(operation_id),
        "local_delivery_unchanged": True,
        "status": status,
        "operation": operation,
        "message_type": message_type,
        "upstream_message_id": upstream_message_id,
    }


def _due_tim_mirror_deliveries(*, limit: int, now: datetime) -> list[tuple[uuid.UUID, int]]:
    due_state = or_(
        and_(
            MessageDelivery.status.in_(("pending", "retry")),
            MessageDelivery.available_at <= now,
            or_(
                MessageDelivery.locked_until.is_(None),
                MessageDelivery.locked_until <= now,
            ),
        ),
        and_(
            MessageDelivery.status == "processing",
            or_(
                MessageDelivery.locked_until.is_(None),
                MessageDelivery.locked_until <= now,
            ),
        ),
    )
    with session_scope() as db:
        exhausted = list(
            db.scalars(
                select(MessageDelivery)
                .where(
                    MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                    MessageDelivery.required.is_(False),
                    MessageDelivery.attempt_count >= MessageDelivery.max_attempts,
                    due_state,
                )
                .with_for_update(skip_locked=True)
                .limit(min(max(1, int(limit)), 100))
            )
        )
        for row in exhausted:
            row.status = "failed"
            row.locked_by = None
            row.locked_until = None
            row.last_error = row.last_error or "TIM mirror retry budget exhausted"
        rows = list(
            db.scalars(
                select(MessageDelivery)
                .where(
                    MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                    MessageDelivery.required.is_(False),
                    MessageDelivery.attempt_count < MessageDelivery.max_attempts,
                    due_state,
                )
                .order_by(MessageDelivery.available_at, MessageDelivery.created_at)
                .with_for_update(skip_locked=True)
                .limit(min(max(1, int(limit)), 100))
            )
        )
        return [(row.id, int(row.attempt_count) + 1) for row in rows]


def dispatch_due_tim_message_deliveries(
    *,
    connection: Redis | None = None,
    limit: int = 20,
) -> dict[str, int | bool]:
    """Enqueue due TIM mirrors; the worker performs the authoritative claim."""

    if not compatibility_dispatch_enabled():
        return {"ok": True, "due": 0, "dispatched": 0, "queue_errors": 0}
    candidates = _due_tim_mirror_deliveries(limit=limit, now=utcnow())
    redis_connection = connection
    if redis_connection is None:
        redis_connection = Redis.from_url(get_settings().redis_url)
    queue = Queue("sync", connection=redis_connection)
    dispatched = 0
    queue_errors = 0
    for delivery_id, next_attempt in candidates:
        try:
            queue.enqueue(
                "bbw_web.jobs.mirror_tim_message_delivery",
                str(delivery_id),
                job_id=f"mirror-tim-message-{delivery_id}-{next_attempt}",
                job_timeout=TIM_MIRROR_JOB_TIMEOUT_SECONDS,
                result_ttl=600,
                failure_ttl=7 * 86400,
            )
            dispatched += 1
        except Exception as exc:
            if _duplicate_queue_error(exc):
                dispatched += 1
            else:
                queue_errors += 1
    return {
        "ok": queue_errors == 0,
        "due": len(candidates),
        "dispatched": dispatched,
        "queue_errors": queue_errors,
    }


def dispatch_due_compatibility_operations(
    *,
    limit: int | None = None,
) -> dict[str, int | bool]:
    """Run one bounded PostgreSQL-authoritative Banghua compatibility batch.

    ``compatibility_outbox.dispatch_due`` owns row leases and converts every
    provider failure into ``retry``/``failed`` state.  This RQ entry point
    therefore completes normally when Banghua is unavailable and never turns
    an already committed Web-local mutation into a failed product request.
    """

    from bbw_web.compatibility_outbox import dispatch_due

    batch_size = (
        max(1, min(int(limit), 100))
        if limit is not None
        else max(
            1,
            min(
                100,
                _as_int(
                    os.getenv("BBW_COMPATIBILITY_OUTBOX_BATCH_SIZE"),
                    20,
                    minimum=1,
                    maximum=100,
                ),
            ),
        )
    )
    # One batch can contain several 30-second legacy calls.  Keep its row
    # leases aligned with the RQ timeout so a second worker cannot reclaim the
    # tail of the batch while this job is still processing it.
    summary = dispatch_due(limit=batch_size, lease_seconds=900)
    return {"ok": True, **summary}


def _enqueue_compatibility_outbox_dispatch(
    *,
    connection: Redis,
) -> dict[str, int | bool]:
    """Enqueue at most one compatibility consumer without creating backlog."""

    if not compatibility_dispatch_enabled():
        return {"ok": True, "dispatched": 0, "queue_errors": 0}
    queue = Queue("sync", connection=connection)
    try:
        queue.enqueue(
            "bbw_web.jobs.dispatch_due_compatibility_operations",
            job_id="dispatch-compatibility-outbox",
            job_timeout=900,
            result_ttl=30,
            failure_ttl=90,
        )
        dispatched = 1
        queue_errors = 0
    except Exception as exc:
        if _duplicate_queue_error(exc):
            dispatched = 1
            queue_errors = 0
        else:
            dispatched = 0
            queue_errors = 1
    return {
        "ok": queue_errors == 0,
        "dispatched": dispatched,
        "queue_errors": queue_errors,
    }


def _claim_media_outboxes(
    *, outbox_ids: Iterable[uuid.UUID] | None = None, limit: int = 20
) -> list[tuple[uuid.UUID, int]]:
    now = utcnow()
    selected = list(outbox_ids or [])
    with session_scope() as db:
        # watchdog：worker 崩溃会留下 processing 且预算耗尽的行，认领条件
        # attempt_count < max_attempts 永远选不中它们，也没有其他路径转
        # failed。这里把「锁已过期 + 预算耗尽」的死行转为 failed，使其可被
        # 观测与人工处理（readiness 仍计入 unfinished，不放行切流）。
        dead_rows = list(
            db.scalars(
                select(OperationOutbox)
                .where(
                    OperationOutbox.operation_type == MEDIA_OPERATION,
                    OperationOutbox.status == "processing",
                    OperationOutbox.attempt_count >= OperationOutbox.max_attempts,
                    OperationOutbox.locked_until.is_not(None),
                    OperationOutbox.locked_until <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for row in dead_rows:
            row.status = "failed"
            row.locked_by = None
            row.locked_until = None
            # 说明文字不得命中 MEDIA_CONFIGURATION_ERROR_MARKERS，避免被
            # 配置错误恢复通道误复活。
            row.last_error = row.last_error or "media archive retry budget exhausted"
        due_state = or_(
            and_(
                OperationOutbox.status.in_(("pending", "retry")),
                OperationOutbox.available_at <= now,
            ),
            and_(
                OperationOutbox.status == "processing",
                OperationOutbox.locked_until.is_not(None),
                OperationOutbox.locked_until <= now,
            ),
        )
        stmt = (
            select(OperationOutbox)
            .where(
                OperationOutbox.operation_type == MEDIA_OPERATION,
                OperationOutbox.attempt_count < OperationOutbox.max_attempts,
                due_state,
            )
            .order_by(OperationOutbox.available_at, OperationOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(max(1, min(int(limit), 100)))
        )
        if selected:
            stmt = stmt.where(OperationOutbox.id.in_(selected))
        rows = list(db.scalars(stmt))
        claimed: list[tuple[uuid.UUID, int]] = []
        for row in rows:
            row.status = "processing"
            row.attempt_count += 1
            row.locked_by = _worker_identity()
            row.locked_until = now + timedelta(minutes=10)
            row.last_error = None
            claimed.append((row.id, row.attempt_count))
        db.flush()
        return claimed


def _reset_outbox_dispatch(outbox_id: uuid.UUID, error: Exception) -> None:
    with session_scope() as db:
        row = db.scalar(select(OperationOutbox).where(OperationOutbox.id == outbox_id).with_for_update())
        if row is None or row.status == "completed":
            return
        row.status = "retry" if row.attempt_count < row.max_attempts else "failed"
        row.available_at = utcnow() + timedelta(seconds=min(3600, 30 * (2 ** min(row.attempt_count, 7))))
        row.locked_by = None
        row.locked_until = None
        row.last_error = str(error)[:2000]


def _dispatch_media_outboxes(
    outbox_ids: Iterable[uuid.UUID] | None = None, *, limit: int = 20
) -> int:
    # media.archive 需要访问遗留上游；paused/retired 下不得外呼，
    # 行保持 pending 且不消耗重试预算（retirement 也不会取消它们）。
    if compatibility_mode() is not CompatibilityMode.ENABLED:
        return 0
    claimed = _claim_media_outboxes(outbox_ids=outbox_ids, limit=limit)
    if not claimed:
        return 0
    settings = get_settings()
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("media", connection=connection)
    dispatched = 0
    for outbox_id, attempt in claimed:
        try:
            queue.enqueue(
                "bbw_web.jobs.archive_media_job",
                str(outbox_id),
                job_id=f"archive-media-{outbox_id}-{attempt}",
                job_timeout=360,
                result_ttl=600,
                failure_ttl=7 * 86400,
            )
            dispatched += 1
        except Exception as exc:
            if _duplicate_queue_error(exc):
                dispatched += 1
            else:
                _reset_outbox_dispatch(outbox_id, exc)
    return dispatched


def _failed_media_configuration_filter():
    return or_(
        *(
            OperationOutbox.last_error.ilike(f"%{marker}%")
            for marker in MEDIA_CONFIGURATION_ERROR_MARKERS
        )
    )


def _has_failed_media_configuration_outboxes() -> bool:
    with session_scope() as db:
        return (
            db.scalar(
                select(OperationOutbox.id)
                .where(
                    OperationOutbox.operation_type == MEDIA_OPERATION,
                    OperationOutbox.status == "failed",
                    OperationOutbox.last_error.is_not(None),
                    _failed_media_configuration_filter(),
                )
                .limit(1)
            )
            is not None
        )


def _recover_failed_media_configuration_outboxes(*, limit: int = 20) -> int:
    """Move historical R2 configuration failures back into durable retry."""

    now = utcnow()
    with session_scope() as db:
        rows = list(
            db.scalars(
                select(OperationOutbox)
                .where(
                    OperationOutbox.operation_type == MEDIA_OPERATION,
                    OperationOutbox.status == "failed",
                    OperationOutbox.last_error.is_not(None),
                    _failed_media_configuration_filter(),
                )
                .order_by(OperationOutbox.updated_at, OperationOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(max(1, min(int(limit), 100)))
            )
        )
        for row in rows:
            row.status = "retry"
            row.max_attempts = max(row.max_attempts + 1, row.attempt_count + 1)
            row.available_at = now
            row.locked_by = None
            row.locked_until = None
            row.completed_at = None
        db.flush()
        return len(rows)


def _maybe_recover_failed_media_configuration_outboxes(
    settings: Settings,
    connection: Redis,
    *,
    limit: int = 20,
) -> int:
    """Probe R2 capabilities and revive old failures only after they work."""

    if not _has_failed_media_configuration_outboxes():
        return 0
    prefix = str(settings.redis_prefix or "bbw").strip(": ") or "bbw"
    ready_key = f"{prefix}:media:r2-write-delete-ready"
    probe_key = f"{prefix}:media:r2-write-delete-probe"
    ready = bool(connection.get(ready_key))
    if not ready:
        acquired = connection.set(
            probe_key,
            "1",
            nx=True,
            ex=MEDIA_CONFIGURATION_PROBE_SECONDS,
        )
        if not acquired:
            return 0
        try:
            R2Storage(settings).verify_write_delete()
        except Exception as exc:
            LOGGER.warning(
                "R2 write/delete capability unavailable; historical media retries remain paused: %s",
                type(exc).__name__,
            )
            return 0
        connection.set(
            ready_key,
            "1",
            ex=MEDIA_CONFIGURATION_READY_SECONDS,
        )
    recovered = _recover_failed_media_configuration_outboxes(limit=limit)
    if recovered:
        LOGGER.warning(
            "Recovered %d historical media outboxes after R2 write/delete verification",
            recovered,
        )
    return recovered


def archive_message_job(
    owner_user_id: str,
    external_account_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one authenticated browser report and durably schedule its media."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    account_id = _uuid(external_account_id, field="external_account_id")
    if not isinstance(payload, Mapping):
        raise ValueError("message payload must be an object")
    settings = get_settings()
    with session_scope() as db:
        user, account = _load_owner_binding(db, owner_id, account_id)
        message, created, outbox_id = _ingest_message(
            db, settings=settings, user=user, account=account, report=payload
        )
        message_id = message.id
    media_dispatched = _dispatch_media_outboxes([outbox_id]) if outbox_id else 0
    return {
        "ok": True,
        "message_id": str(message_id),
        "created": created,
        "media_dispatched": media_dispatched,
    }


def archive_message_batch_job(
    owner_user_id: str,
    external_account_id: str,
    payloads: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist a bounded browser batch in one transaction and dispatch media once."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    account_id = _uuid(external_account_id, field="external_account_id")
    if (
        not isinstance(payloads, Sequence)
        or isinstance(payloads, (bytes, bytearray, str))
        or not payloads
        or len(payloads) > 20
    ):
        raise ValueError("message payload batch must contain between 1 and 20 objects")
    reports: list[dict[str, Any]] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            raise ValueError("each message payload must be an object")
        reports.append(dict(payload))

    settings = get_settings()
    message_ids: list[uuid.UUID] = []
    created_count = 0
    outbox_ids: list[uuid.UUID] = []
    with session_scope() as db:
        user, account = _load_owner_binding(db, owner_id, account_id)
        prepared: list[tuple[dict[str, Any], datetime, str]] = []
        conversation_candidates: list[dict[str, Any]] = []
        for report in reports:
            peer_uid = _bounded(report.get("peer_uid"), 128)
            if not peer_uid:
                raise ValueError("peer_uid is required")
            occurred_at = _parse_time(
                report.get("sent_at") or report.get("observed_at")
            )
            candidate = _conversation_candidate(
                owner_user_id=user.id,
                peer_uid=peer_uid,
                reported_id=report.get("conversation_id"),
                last_message_at=occurred_at,
                metadata={
                    "reported_conversation_id": _bounded(
                        report.get("conversation_id"), 256
                    ),
                    "last_source": _bounded(report.get("source"), 64)
                    or "browser",
                },
            )
            conversation_candidates.append(candidate)
            prepared.append(
                (report, occurred_at, candidate["upstream_conversation_id"])
            )

        conversations = _upsert_conversations(
            db,
            owner_user_id=user.id,
            candidates=conversation_candidates,
        )
        for report, occurred_at, conversation_id in prepared:
            message, created, outbox_id = _ingest_message(
                db,
                settings=settings,
                user=user,
                account=account,
                report=report,
                conversation=conversations[conversation_id],
                occurred_at=occurred_at,
            )
            message_ids.append(message.id)
            created_count += int(created)
            if outbox_id is not None and outbox_id not in outbox_ids:
                outbox_ids.append(outbox_id)

    media_dispatched = (
        _dispatch_media_outboxes(outbox_ids, limit=len(outbox_ids))
        if outbox_ids
        else 0
    )
    return {
        "ok": True,
        "count": len(message_ids),
        "created": created_count,
        "updated": len(message_ids) - created_count,
        "message_ids": [str(message_id) for message_id in message_ids],
        "media_dispatched": media_dispatched,
    }


def _envelope_items(payload: Any, normalizer: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("items", "list"):
            values = payload.get(key)
            if isinstance(values, list) and all(isinstance(item, Mapping) for item in values):
                return [dict(item) for item in values]
    return list(normalizer(payload))


def _media_from_normalized_message(item: Mapping[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    payload = item.get("payload")
    if isinstance(payload, Mapping):
        candidates.append((_normal_message_type(item.get("message_type") or item.get("type")), payload))
    elements = item.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, Mapping) or not isinstance(element.get("payload"), Mapping):
                continue
            candidates.append(
                (_normal_message_type(element.get("type") or element.get("object_name")), element["payload"])
            )
    for kind, media in candidates:
        if kind not in {"image", "audio", "video", "file", "flash"}:
            continue
        if kind == "video":
            url = media.get("video_url") or media.get("url")
            thumbnail = media.get("thumbnail_url") or media.get("thumb_url")
        elif kind == "image":
            url = media.get("original_url") or media.get("url") or media.get("large_url")
            thumbnail = media.get("thumbnail_url") or media.get("thumbnail")
        else:
            url = media.get("url")
            thumbnail = media.get("thumbnail_url") or media.get("thumbnail")
        if not str(url or thumbnail or "").startswith("https://"):
            continue
        return {
            "url": _bounded(url, 4096),
            "thumbnail": _bounded(thumbnail, 4096),
            "name": _bounded(media.get("file_name") or media.get("name"), 255),
            "size": _as_int(media.get("size"), 0, maximum=100 * 1024 * 1024),
            "mime": _bounded(media.get("mime") or media.get("content_type"), 160),
            "duration": _as_int(media.get("duration") or media.get("second"), 0, maximum=86400),
            "width": _as_int(media.get("width"), 0, maximum=65535),
            "height": _as_int(media.get("height"), 0, maximum=65535),
            "_kind": kind,
        }
    return None


def _history_message_report(
    item: Mapping[str, Any], *, account_uid: str, requested_peer: str
) -> dict[str, Any] | None:
    from_uid = _bounded(item.get("from_user_id") or item.get("from"), 128)
    to_uid = _bounded(item.get("to_user_id") or item.get("to"), 128)
    peer = _bounded(requested_peer, 128)
    if not peer:
        peer = to_uid if from_uid == account_uid else from_uid
    if not peer:
        return None
    if from_uid and from_uid == account_uid:
        direction = "outgoing"
    elif to_uid and to_uid == account_uid:
        direction = "incoming"
    else:
        direction = "unknown"
    media = _media_from_normalized_message(item)
    message_type = _normal_message_type(
        (media or {}).get("_kind") or item.get("message_type") or item.get("type")
    )
    if media:
        media.pop("_kind", None)
    cloud_custom_data = (
        item.get("cloud_custom_data")
        or item.get("cloudCustomData")
        or item.get("CloudCustomData")
    )
    local_identity = extract_local_message_identity(cloud_custom_data)
    identity = (
        item.get("id")
        or item.get("msg_key")
        or item.get("MsgKey")
        or f"history:{_stable_json_digest(item)}"
    )
    return {
        "schema_version": 1,
        "idempotency_key": f"history:{_stable_json_digest({'peer': peer, 'id': identity})}",
        "source": "history",
        "direction": direction,
        "upstream_message_id": str(item.get("id") or ""),
        "message_key": str(item.get("msg_key") or item.get("MsgKey") or ""),
        "canonical_message_id": local_identity.get("canonical_message_id", ""),
        "client_message_id": local_identity.get("client_message_id", ""),
        "client_message_key": local_identity.get("client_message_id", ""),
        "message_sequence": str(
            item.get("sequence") or item.get("msg_sequence") or item.get("MsgSeq") or ""
        ),
        "message_random": str(
            item.get("message_random") or item.get("msg_random") or item.get("MsgRandom") or ""
        ),
        "peer_uid": peer,
        "conversation_id": _canonical_conversation_id(peer),
        "message_type": message_type,
        "object_name": str(item.get("object_name") or item.get("objectName") or ""),
        "text": str(item.get("text") or item.get("content") or "")[:100_000],
        "sent_at": item.get("timestamp") or item.get("time"),
        "delivery": (
            "read"
            if item.get("is_peer_read") is True
            else str(item.get("send_status") or item.get("status") or "")
        ),
        "revoked": _as_bool(item.get("is_revoked") or item.get("isRevoked")),
        "is_peer_read": item.get("is_peer_read") if isinstance(item.get("is_peer_read"), bool) else None,
        "read_at": str(item.get("read_time") or item.get("readTime") or ""),
        "flash_id": str(item.get("flash_unique_id") or ""),
        "media": media,
        "quote": extract_message_quote(cloud_custom_data),
        "sender_upstream_uid": from_uid,
        "recipient_upstream_uid": to_uid,
    }


def _ingest_history_response_in_session(
    db: Any,
    *,
    settings: Settings,
    user: User,
    account: ExternalAccount,
    owner_id: uuid.UUID,
    path: str,
    query: Mapping[str, Any] | None,
    response_data: Any,
    outbox_ids: list[uuid.UUID],
) -> dict[str, Any]:
    route = str(path or "")
    if isinstance(response_data, Mapping) and response_data.get("ok") is False:
        return {"ok": True, "ignored": True, "reason": "upstream response was not successful"}
    created = 0
    existing = 0
    conversations = 0
    if route == "/api/im/conversations":
        items = _envelope_items(response_data, normalize_conversations)
        conversation_candidates: list[dict[str, Any]] = []
        for item in items[:500]:
            peer = _bounded(item.get("peer_id") or item.get("conversation_user"), 128)
            if not peer:
                continue
            unread_authoritative = item.get("unread_authoritative") is not False
            preview = _bounded(
                item.get("last_message") or item.get("content"),
                500,
            )
            conversation_metadata = {
                "reported_conversation_id": _bounded(item.get("id"), 256),
                "object_name": _bounded(item.get("object_name"), 128),
                "avatar": _bounded(item.get("avatar"), 4096),
                "user": _json_safe(item.get("user")),
                "last_source": "history",
            }
            if preview:
                conversation_metadata.update(
                    last_message=preview,
                    preview_timestamp=_bounded(
                        item.get("preview_timestamp"),
                        80,
                    ),
                    preview_sequence=_bounded(item.get("preview_sequence"), 80),
                    preview_authoritative=item.get("preview_authoritative") is True,
                    preview_timestamp_inferred=item.get("preview_timestamp_inferred") is True,
                )
            conversation_candidates.append(
                _conversation_candidate(
                    owner_user_id=owner_id,
                    peer_uid=peer,
                    reported_id=item.get("id"),
                    title=item.get("nickname"),
                    unread_count=(
                        _as_int(item.get("unread_count"), 0)
                        if unread_authoritative
                        else None
                    ),
                    unread_observed_at=(
                        _optional_time(
                            item.get("unread_observed_at")
                            or item.get("summary_observed_at")
                            or item.get("observed_at")
                        )
                        or utcnow()
                        if unread_authoritative
                        else None
                    ),
                    last_message_at=_optional_time(item.get("timestamp")),
                    metadata=conversation_metadata,
                )
            )
            conversations += 1
        _upsert_conversations(
            db,
            owner_user_id=owner_id,
            candidates=conversation_candidates,
        )
    elif route == "/api/im/messages":
        requested_peer = ""
        if isinstance(query, Mapping):
            requested_peer = _bounded(
                query.get("peer") or query.get("uid") or query.get("yourid"), 128
            )
        items = _envelope_items(response_data, normalize_messages)
        reports: list[tuple[dict[str, Any], datetime]] = []
        conversation_candidates = []
        for item in items[:5000]:
            report = _history_message_report(
                item,
                account_uid=_bounded(account.upstream_uid, 128),
                requested_peer=requested_peer,
            )
            if report is None:
                continue
            occurred_at = _parse_time(
                report.get("sent_at") or report.get("observed_at")
            )
            reports.append((report, occurred_at))
            peer_uid = _bounded(report.get("peer_uid"), 128)
            conversation_candidates.append(
                _conversation_candidate(
                    owner_user_id=owner_id,
                    peer_uid=peer_uid,
                    reported_id=report.get("conversation_id"),
                    last_message_at=occurred_at,
                    metadata={
                        "reported_conversation_id": _bounded(
                            report.get("conversation_id"), 256
                        ),
                        "last_source": _bounded(report.get("source"), 64) or "browser",
                    },
                )
            )
        conversation_rows = _upsert_conversations(
            db,
            owner_user_id=owner_id,
            candidates=conversation_candidates,
        )
        for report, occurred_at in reports:
            peer_uid = _bounded(report.get("peer_uid"), 128)
            upstream_id = _canonical_conversation_id(peer_uid, report.get("conversation_id"))
            conversation = conversation_rows.get(upstream_id)
            if conversation is None:
                raise RuntimeError("conversation batch did not return the requested row")
            _message, was_created, outbox_id = _ingest_message(
                db,
                settings=settings,
                user=user,
                account=account,
                report=report,
                conversation=conversation,
                occurred_at=occurred_at,
            )
            if was_created:
                created += 1
            else:
                existing += 1
            if outbox_id and outbox_id not in outbox_ids:
                outbox_ids.append(outbox_id)
    else:
        return {"ok": True, "ignored": True, "reason": "unsupported history route"}
    return {
        "ok": True,
        "conversations": conversations,
        "messages_created": created,
        "messages_existing": existing,
    }


def ingest_history_response(
    owner_user_id: str,
    external_account_id: str,
    path: str,
    query: Mapping[str, Any] | None,
    response_data: Any,
) -> dict[str, Any]:
    """Ingest one normalized BFF history response."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    account_id = _uuid(external_account_id, field="external_account_id")
    settings = get_settings()
    outbox_ids: list[uuid.UUID] = []
    with session_scope() as db:
        user, account = _load_owner_binding(db, owner_id, account_id)
        result = _ingest_history_response_in_session(
            db,
            settings=settings,
            user=user,
            account=account,
            owner_id=owner_id,
            path=path,
            query=query,
            response_data=response_data,
            outbox_ids=outbox_ids,
        )
    result["media_dispatched"] = (
        _dispatch_media_outboxes(outbox_ids, limit=min(100, len(outbox_ids)))
        if outbox_ids
        else 0
    )
    return result


def ingest_history_responses_batch(
    owner_user_id: str,
    external_account_id: str,
    responses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Ingest a bounded history-response batch in one transaction."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    account_id = _uuid(external_account_id, field="external_account_id")
    if (
        not isinstance(responses, Sequence)
        or isinstance(responses, (bytes, bytearray, str))
        or not responses
        or len(responses) > 16
    ):
        raise ValueError("history response batch must contain between 1 and 16 objects")
    envelopes: list[dict[str, Any]] = []
    for response in responses:
        if not isinstance(response, Mapping):
            raise ValueError("each history response must be an object")
        query = response.get("query")
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("history response query must be an object")
        envelopes.append(
            {
                "path": str(response.get("path") or ""),
                "query": dict(query or {}),
                "response_data": response.get("response_data"),
            }
        )

    settings = get_settings()
    outbox_ids: list[uuid.UUID] = []
    results: list[dict[str, Any]] = []
    with session_scope() as db:
        user, account = _load_owner_binding(db, owner_id, account_id)
        for envelope in envelopes:
            results.append(
                _ingest_history_response_in_session(
                    db,
                    settings=settings,
                    user=user,
                    account=account,
                    owner_id=owner_id,
                    path=envelope["path"],
                    query=envelope["query"],
                    response_data=envelope["response_data"],
                    outbox_ids=outbox_ids,
                )
            )
    media_dispatched = (
        _dispatch_media_outboxes(outbox_ids, limit=min(100, len(outbox_ids)))
        if outbox_ids
        else 0
    )
    return {
        "ok": True,
        "count": len(results),
        "ignored": sum(1 for result in results if result.get("ignored") is True),
        "conversations": sum(int(result.get("conversations") or 0) for result in results),
        "messages_created": sum(int(result.get("messages_created") or 0) for result in results),
        "messages_existing": sum(int(result.get("messages_existing") or 0) for result in results),
        "media_dispatched": media_dispatched,
    }


def _social_items(response_data: Any) -> list[dict[str, Any]]:
    if isinstance(response_data, list):
        return [dict(item) for item in response_data if isinstance(item, Mapping)]
    if not isinstance(response_data, Mapping):
        return []
    for key in ("items", "list", "users"):
        value = response_data.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    nested = response_data.get("data")
    if nested is not response_data:
        return _social_items(nested)
    return []


def _social_uid(item: Mapping[str, Any]) -> str:
    for key in ("uid", "user_id", "userid", "id", "yourid", "from_user_id"):
        value = _bounded(item.get(key), 128)
        if value:
            return value
    nested = item.get("user")
    return _social_uid(nested) if isinstance(nested, Mapping) else ""


def ingest_social_snapshot(
    owner_user_id: str,
    external_account_id: str,
    path: str,
    query: Mapping[str, Any] | None,
    response_data: Any,
) -> dict[str, Any]:
    """Persist relationship/visit snapshots returned by authenticated social APIs."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    account_id = _uuid(external_account_id, field="external_account_id")
    route = "/" + str(path or "").strip("/")
    relation_kinds = {
        "/api/social/follows": "follow",
        "/api/social/follow-list": "follow",
        "/api/social/fans": "follower",
        "/api/social/friends": "friend",
        "/api/social/friend-apply": "friend_request",
        "/api/social/blacklist": "blacklist",
        "/api/social/blacklist-me": "blacklisted_by",
    }
    visit_type = _bounded((query or {}).get("type"), 32) or "seen_me"
    if route == "/api/social/visitors":
        relation_kind = "profile_view" if visit_type == "seen_by_me" else "visitor"
    else:
        relation_kind = relation_kinds.get(route)
    if relation_kind is None:
        return {"ok": True, "ignored": True, "reason": "unsupported social route"}

    items = _social_items(response_data)[:1000]
    now = utcnow()
    relationships = 0
    events = 0
    with session_scope() as db:
        _user, account = _load_owner_binding(db, owner_id, account_id)
        relation_repo = RelationshipRepository(db)
        event_repo = ActivityEventRepository(db)
        for item in items:
            subject_uid = _social_uid(item)
            if not subject_uid or subject_uid == str(account.upstream_uid or ""):
                continue
            existing = db.scalar(
                select(Relationship).where(
                    Relationship.owner_user_id == owner_id,
                    Relationship.provider == SYNC_SOURCE,
                    Relationship.subject_upstream_uid == subject_uid,
                    Relationship.kind == relation_kind,
                )
            )
            raw_observed_at = (
                item.get("timestamp")
                or item.get("time")
                or item.get("visit_time")
                or item.get("created_at")
            )
            observed_at = _optional_time(raw_observed_at) or now
            relation_repo.upsert(
                owner_user_id=owner_id,
                provider=SYNC_SOURCE,
                subject_upstream_uid=subject_uid,
                kind=relation_kind,
                status="active",
                started_at=existing.started_at if existing else observed_at,
                ended_at=None,
                extra_data=_merge_dict(
                    existing.extra_data if existing else {},
                    {
                        "last_seen_at": observed_at.isoformat(),
                        "source_path": route,
                        "profile": _json_safe(item),
                    },
                ),
            )
            relationships += 1

            if route == "/api/social/friends":
                request_relation = db.scalar(
                    select(Relationship).where(
                        Relationship.owner_user_id == owner_id,
                        Relationship.provider == SYNC_SOURCE,
                        Relationship.subject_upstream_uid == subject_uid,
                        Relationship.kind == "friend_request",
                    )
                )
                if request_relation is not None and request_relation.status == "active":
                    relation_repo.upsert(
                        owner_user_id=owner_id,
                        provider=SYNC_SOURCE,
                        subject_upstream_uid=subject_uid,
                        kind="friend_request",
                        status="inactive",
                        started_at=request_relation.started_at,
                        ended_at=now,
                        extra_data=_merge_dict(
                            request_relation.extra_data,
                            {
                                "resolved_as": "accepted",
                                "resolved_at": now.isoformat(),
                                "source_path": route,
                            },
                        ),
                    )

            if route in {"/api/social/visitors", "/api/social/friend-apply"}:
                identity = _stable_json_digest(
                    {
                        "path": route,
                        "type": visit_type,
                        "uid": subject_uid,
                        "at": str(raw_observed_at or "unknown"),
                        "item": item,
                    }
                )
                outgoing = route == "/api/social/visitors" and visit_type == "seen_by_me"
                event_repo.insert_idempotent(
                    owner_user_id=owner_id,
                    provider=SYNC_SOURCE,
                    upstream_event_id=f"snapshot:{identity}",
                    event_type=(
                        "social.visit.sent"
                        if outgoing
                        else (
                            "social.friend_request.received"
                            if route == "/api/social/friend-apply"
                            else "social.visit.received"
                        )
                    ),
                    actor_upstream_uid=(
                        str(account.upstream_uid or "") if outgoing else subject_uid
                    ),
                    subject_upstream_uid=(
                        subject_uid if outgoing else str(account.upstream_uid or "")
                    ),
                    occurred_at=observed_at,
                    details={"source_path": route, "profile": _json_safe(item)},
                )
                events += 1
    return {
        "ok": True,
        "items": len(items),
        "relationships": relationships,
        "events": events,
    }


def _subject_uid(request_payload: Any) -> str:
    if not isinstance(request_payload, Mapping):
        return ""
    for key in (
        "uid",
        "you",
        "yourid",
        "your_id",
        "target_id",
        "targetId",
        "user_id",
        "userid",
        "friend_id",
    ):
        value = _bounded(request_payload.get(key), 128)
        if value:
            return value
    params = request_payload.get("params")
    return _subject_uid(params) if isinstance(params, Mapping) else ""


def _successful_product_event(event_payload: Mapping[str, Any]) -> bool:
    status = _as_int(event_payload.get("status"), 0, minimum=0, maximum=999)
    response = event_payload.get("response")
    if not 200 <= status < 300:
        return False
    return not (isinstance(response, Mapping) and response.get("ok") is False)


def _product_event_read_peers(event_payload: Mapping[str, Any]) -> list[str]:
    response = event_payload.get("response")
    request = event_payload.get("request")
    raw_peers = response.get("read_peers") if isinstance(response, Mapping) else None
    if not isinstance(raw_peers, list) and isinstance(request, Mapping):
        raw_peers = request.get("peers")
        if not isinstance(raw_peers, list):
            raw_peers = [
                request.get("peer")
                or request.get("uid")
                or request.get("to")
                or ""
            ]
    if not isinstance(raw_peers, list):
        return []
    return list(
        dict.fromkeys(
            peer
            for peer in (_bounded(value, 128) for value in raw_peers[:500])
            if peer
        )
    )


PRODUCT_RELATIONSHIP_MAP = {
    "/api/social/follow": ("follow", "active"),
    "/api/social/unfollow": ("follow", "inactive"),
    "/api/social/add-friend": ("friend_request", "active"),
    "/api/social/agree-friend": ("friend", "active"),
    "/api/social/delete-friend": ("friend", "inactive"),
    "/api/social/visit": ("profile_view", "active"),
    "/api/social/blacklist-add": ("blacklist", "active"),
    "/api/social/blacklist-del": ("blacklist", "inactive"),
}


def _record_product_event_in_session(
    db: Any,
    *,
    owner_id: uuid.UUID,
    account: ExternalAccount,
    event_payload: Mapping[str, Any],
    event_repo: ActivityEventRepository,
    relationship_repo: RelationshipRepository,
    relationship_cache: dict[tuple[str, str], Relationship | None],
) -> dict[str, Any]:
    path = "/" + str(event_payload.get("path") or "").strip("/")
    event_type = ("api." + path.strip("/").replace("/", "."))[:64]
    request_payload = event_payload.get("request")
    subject_uid = _subject_uid(request_payload) or _subject_uid(event_payload.get("query"))
    now = utcnow()
    explicit_event_id = _bounded(event_payload.get("idempotency_key"), 256) or None
    event = event_repo.insert_idempotent(
        owner_user_id=owner_id,
        provider=SYNC_SOURCE,
        upstream_event_id=explicit_event_id,
        event_type=event_type or "api.unknown",
        actor_upstream_uid=_bounded(account.upstream_uid, 128) or None,
        subject_upstream_uid=subject_uid or None,
        occurred_at=now,
        details=_json_safe(event_payload),
    )
    conversations_marked_read = 0
    if path == "/api/im/read" and _successful_product_event(event_payload):
        read_peers = _product_event_read_peers(event_payload)
        if read_peers:
            conversations_marked_read = ConversationRepository(db).mark_peers_read(
                owner_id,
                read_peers,
                observed_at=now,
            )

    def current_relationship(kind: str) -> Relationship | None:
        cache_key = (subject_uid, kind)
        if cache_key not in relationship_cache:
            relationship_cache[cache_key] = db.scalar(
                select(Relationship).where(
                    Relationship.owner_user_id == owner_id,
                    Relationship.provider == SYNC_SOURCE,
                    Relationship.subject_upstream_uid == subject_uid,
                    Relationship.kind == kind,
                )
            )
        return relationship_cache[cache_key]

    relationship_id: uuid.UUID | None = None
    relation = PRODUCT_RELATIONSHIP_MAP.get(path)
    if relation and subject_uid and _successful_product_event(event_payload):
        kind, relation_status = relation
        existing = current_relationship(kind)
        relationship = relationship_repo.upsert(
            owner_user_id=owner_id,
            provider=SYNC_SOURCE,
            subject_upstream_uid=subject_uid,
            kind=kind,
            status=relation_status,
            started_at=existing.started_at if existing else now,
            ended_at=now if relation_status == "inactive" else None,
            extra_data=_merge_dict(
                existing.extra_data if existing else {},
                {"last_event_type": event_type, "last_event_at": now.isoformat()},
            ),
        )
        relationship_cache[(subject_uid, kind)] = relationship
        relationship_id = relationship.id
        if path == "/api/social/agree-friend":
            request_relation = current_relationship("friend_request")
            if request_relation is not None:
                request_relation = relationship_repo.upsert(
                    owner_user_id=owner_id,
                    provider=SYNC_SOURCE,
                    subject_upstream_uid=subject_uid,
                    kind="friend_request",
                    status="inactive",
                    started_at=request_relation.started_at,
                    ended_at=now,
                    extra_data=_merge_dict(
                        request_relation.extra_data,
                        {
                            "resolved_as": "accepted",
                            "resolved_at": now.isoformat(),
                            "last_event_type": event_type,
                        },
                    ),
                )
                relationship_cache[(subject_uid, "friend_request")] = request_relation
    return {
        "ok": True,
        "event_id": str(event.id),
        "relationship_id": str(relationship_id) if relationship_id else None,
        "conversations_marked_read": conversations_marked_read,
    }


def record_product_event(owner_user_id: str, event_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Record one meaningful product action."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    if not isinstance(event_payload, Mapping):
        raise ValueError("event payload must be an object")
    with session_scope() as db:
        _user, account = _load_owner_binding(db, owner_id, None)
        return _record_product_event_in_session(
            db,
            owner_id=owner_id,
            account=account,
            event_payload=event_payload,
            event_repo=ActivityEventRepository(db),
            relationship_repo=RelationshipRepository(db),
            relationship_cache={},
        )


def record_product_events_batch(
    owner_user_id: str,
    event_payloads: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Record a bounded product-event batch with one binding load and transaction."""

    owner_id = _uuid(owner_user_id, field="owner_user_id")
    if (
        not isinstance(event_payloads, Sequence)
        or isinstance(event_payloads, (bytes, bytearray, str))
        or not event_payloads
        or len(event_payloads) > 64
    ):
        raise ValueError("product event batch must contain between 1 and 64 objects")
    payloads: list[dict[str, Any]] = []
    for payload in event_payloads:
        if not isinstance(payload, Mapping):
            raise ValueError("each product event payload must be an object")
        payloads.append(dict(payload))

    results: list[dict[str, Any]] = []
    with session_scope() as db:
        _user, account = _load_owner_binding(db, owner_id, None)
        event_repo = ActivityEventRepository(db)
        relationship_repo = RelationshipRepository(db)
        relationship_cache: dict[tuple[str, str], Relationship | None] = {}
        for payload in payloads:
            results.append(
                _record_product_event_in_session(
                    db,
                    owner_id=owner_id,
                    account=account,
                    event_payload=payload,
                    event_repo=event_repo,
                    relationship_repo=relationship_repo,
                    relationship_cache=relationship_cache,
                )
            )
    return {
        "ok": True,
        "count": len(results),
        "event_ids": [result["event_id"] for result in results],
        "relationship_updates": sum(
            1 for result in results if result["relationship_id"] is not None
        ),
        "conversations_marked_read": sum(
            int(result.get("conversations_marked_read") or 0) for result in results
        ),
    }


def _setting_seconds(settings: Settings, attribute: str, env_name: str, default: int) -> int:
    value = getattr(settings, attribute, None)
    if value is None:
        value = os.getenv(env_name, str(default))
    return max(60, _as_int(value, default, minimum=60, maximum=86400))


def _active_owner_ids(db: Any, *, now: datetime, active_seconds: int) -> set[uuid.UUID]:
    cutoff = now - timedelta(seconds=max(600, active_seconds * 2))
    stmt = select(WebSession.user_id).where(
        WebSession.revoked_at.is_(None),
        WebSession.idle_expires_at > now,
        WebSession.absolute_expires_at > now,
        WebSession.last_seen_at >= cutoff,
    )
    return set(db.scalars(stmt))


def _cursor_values(
    row: SyncCursor | None,
    *,
    owner_user_id: uuid.UUID,
    next_sync_at: datetime | None,
    attempted_at: datetime | None = None,
    succeeded_at: datetime | None = None,
    watermark_at: datetime | None = None,
    cursor: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "owner_user_id": owner_user_id,
        "source": SYNC_SOURCE,
        "stream": SYNC_STREAM,
        "cursor": cursor if cursor is not None else (row.cursor if row else None),
        "watermark_at": watermark_at if watermark_at is not None else (row.watermark_at if row else None),
        "next_sync_at": next_sync_at,
        "last_attempted_at": attempted_at if attempted_at is not None else (row.last_attempted_at if row else None),
        "last_succeeded_at": succeeded_at if succeeded_at is not None else (row.last_succeeded_at if row else None),
        "last_error": error,
        "version": int(row.version if row else 1),
    }


@dataclass(frozen=True, slots=True)
class _AutonomyReplyCandidate:
    message_id: uuid.UUID
    peer_upstream_uid: str
    message_identity: str


def _unanswered_autonomy_reply_candidates(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    not_before: datetime,
    now: datetime,
    limit: int,
) -> list[_AutonomyReplyCandidate]:
    """Return fresh, meaningful unanswered heads not already bound to a task.

    Excluding existing task rows in SQL lets the database scan past old failed,
    cancelled or otherwise terminal work instead of allowing those rows to
    consume the scheduler's bounded candidate window forever.
    """

    later = aliased(Message)
    later_exists = exists(
        select(later.id).where(
            later.owner_user_id == Message.owner_user_id,
            later.conversation_id == Message.conversation_id,
            or_(
                later.occurred_at > Message.occurred_at,
                and_(
                    later.occurred_at == Message.occurred_at,
                    later.id > Message.id,
                ),
            ),
        )
    )
    existing_task = exists(
        select(AiAgentAutonomyTask.id).where(
            AiAgentAutonomyTask.owner_user_id == owner_user_id,
            AiAgentAutonomyTask.source_message_id == Message.id,
        )
    )
    blocked_target = exists(
        select(Relationship.id).where(
            Relationship.owner_user_id == owner_user_id,
            Relationship.subject_upstream_uid
            == Conversation.peer_upstream_uid,
            Relationship.kind.in_(("blacklist", "blacklisted_by")),
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
        )
    )
    metadata_origin = func.coalesce(
        Message.extra_data.op("->>")("origin"), ""
    )
    client_key = func.coalesce(
        Message.extra_data.op("->>")("client_message_key"),
        Message.extra_data.op("->>")("client_message_id"),
        "",
    )
    metadata_revoked = func.lower(
        func.coalesce(Message.extra_data.op("->>")("revoked"), "false")
    )
    maximum = min(max(1, int(limit)), 50)
    rows = list(
        db.execute(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.owner_user_id == owner_user_id,
                Conversation.owner_user_id == owner_user_id,
                Message.provider.in_(("web-local", CHAT_PROVIDER)),
                Conversation.provider.in_(("web-local", CHAT_PROVIDER)),
                Message.provider == Conversation.provider,
                func.lower(Conversation.kind) == "direct",
                Message.direction == "incoming",
                func.lower(Message.message_type).in_(("text", "timtextelem")),
                func.length(func.btrim(func.coalesce(Message.body, ""))) > 0,
                func.lower(Message.status) != "revoked",
                metadata_revoked.notin_(("true", "1")),
                func.lower(metadata_origin) != "agent",
                ~client_key.like("agent:%"),
                ~later_exists,
                ~existing_task,
                ~blocked_target,
                Message.occurred_at >= not_before,
                Message.occurred_at
                <= now + timedelta(seconds=AUTONOMY_REPLY_CLOCK_SKEW_SECONDS),
                func.length(
                    func.btrim(func.coalesce(Conversation.peer_upstream_uid, ""))
                )
                > 0,
                or_(
                    Message.sender_upstream_uid.is_(None),
                    func.length(
                        func.btrim(func.coalesce(Message.sender_upstream_uid, ""))
                    )
                    == 0,
                    Message.sender_upstream_uid == Conversation.peer_upstream_uid,
                ),
            )
            .order_by(Message.occurred_at.desc(), Message.id.desc())
            .limit(min(maximum * 4, 200))
        )
    )
    candidates: list[_AutonomyReplyCandidate] = []
    for message, conversation in rows:
        metadata = message.extra_data if isinstance(message.extra_data, Mapping) else {}
        peer = str(conversation.peer_upstream_uid or "").strip()
        if not autonomy_reply_message_is_fresh(
            message.occurred_at,
            now=now,
            started_at=not_before,
        ):
            continue
        if not autonomy_reply_message_is_eligible(
            message.body,
            sender_upstream_uid=message.sender_upstream_uid,
            peer_upstream_uid=peer,
            conversation_title=conversation.title,
        ):
            continue
        identity = str(metadata.get("canonical_message_id") or "").strip()
        if not identity:
            identity = f"{message.provider}:{message.upstream_message_id}"
        candidates.append(
            _AutonomyReplyCandidate(
                message_id=message.id,
                peer_upstream_uid=peer,
                message_identity=identity,
            )
        )
        if len(candidates) >= maximum:
            break
    return candidates


def _agent_candidate_truthy(snapshot: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        value = snapshot.get(key)
        if value is True or str(value or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "active",
            "pending",
            "accepted",
        }:
            return True
    return False


def _agent_candidate_next_action(
    *,
    policy: Any,
    candidate: Any,
    tasks: Any,
) -> tuple[Any, str] | None:
    from bbw_agent.autonomous import (
        FOLLOW_USER,
        REQUEST_FRIEND,
        SEND_PRIVATE_MESSAGE,
        AutonomyTaskType,
    )

    snapshot = (
        dict(candidate.profile_snapshot or {})
        if isinstance(candidate.profile_snapshot, Mapping)
        else {}
    )
    target = str(candidate.target_upstream_uid or "").strip()
    last_action = str(candidate.last_action_type or "").strip()
    following = _agent_candidate_truthy(
        snapshot,
        "is_following",
        "is_follower",
    ) or tasks.relationship_is_active(
        owner_user_id=candidate.owner_user_id,
        target_upstream_uid=target,
    )
    friendship_started = _agent_candidate_truthy(snapshot, "is_friend")
    friend_request_pending = _agent_candidate_truthy(
        snapshot,
        "is_friend_apply",
        "has_incoming_friend_apply",
        "friend_apply_status",
        "incoming_friend_apply_status",
    )
    if (
        policy.follow_discovered_enabled
        and FOLLOW_USER in policy.selected_actions
        and not following
        and last_action not in {FOLLOW_USER, REQUEST_FRIEND, SEND_PRIVATE_MESSAGE}
    ):
        return AutonomyTaskType.FOLLOW_DISCOVERED, FOLLOW_USER
    if (
        policy.friend_request_enabled
        and REQUEST_FRIEND in policy.selected_actions
        and not friendship_started
        and not friend_request_pending
        and last_action not in {REQUEST_FRIEND, SEND_PRIVATE_MESSAGE}
    ):
        return AutonomyTaskType.REQUEST_FRIEND, REQUEST_FRIEND
    if (
        policy.proactive_message_enabled
        and SEND_PRIVATE_MESSAGE in policy.selected_actions
        and last_action != SEND_PRIVATE_MESSAGE
        and tasks.get_conversation_head(
            owner_user_id=candidate.owner_user_id,
            peer_upstream_uid=target,
        )
        is None
    ):
        return AutonomyTaskType.PROACTIVE_MESSAGE, SEND_PRIVATE_MESSAGE
    return None


def _schedule_autonomy_owner(
    owner_user_id: uuid.UUID,
    *,
    now: datetime,
    task_limit: int,
    failure_backoff_seconds: int,
) -> dict[str, int | bool]:
    """Create bounded, database-idempotent tasks for one authorized owner."""

    from bbw_agent.autonomous import (
        BROWSE_ONLINE_USERS,
        FOLLOW_USER,
        PUBLISH_TEXT_POST,
        REQUEST_FRIEND,
        REQUEST_TEXT_MATCH,
        SEND_PRIVATE_MESSAGE,
        UNFOLLOW_USER,
        AutonomyTaskType,
        deterministic_task_key,
        policy_budget_day,
        quiet_window_end,
    )
    from bbw_agent.repositories import (
        AgentDiscoveryCandidateRepository,
        AgentAutonomySettingRepository,
        AgentAutonomyTaskRepository,
    )
    from bbw_agent.runtime import policy_from_rows

    created = 0
    reused = 0
    with session_scope() as db:
        settings_repository = AgentAutonomySettingRepository(db)
        snapshot = settings_repository.get_policy_snapshot(
            owner_user_id,
            for_update=True,
        )
        if snapshot is None:
            return {"eligible": False, "created": 0, "reused": 0}
        setting = snapshot["autonomy_setting"]
        if (
            not setting.user_enabled
            or setting.halted_at is not None
            or (
                setting.next_run_at is not None
                and setting.next_run_at > now
            )
        ):
            return {"eligible": False, "created": 0, "reused": 0}
        account = snapshot.get("external_account")
        try:
            policy = (
                policy_from_rows(
                    owner_user_id=owner_user_id,
                    external_account_id=account.id,
                    user=snapshot.get("user"),
                    system=snapshot.get("system_setting"),
                    autonomy_setting=setting,
                    execution_setting=snapshot.get("execution_setting"),
                    runner_setting=snapshot.get("agent_setting"),
                    connection=snapshot.get("connection"),
                )
                if account is not None
                else None
            )
        except (TypeError, ValueError):
            policy = None
            setting.halted_at = now
            setting.halted_reason = "autonomy_policy_invalid"
            setting.next_run_at = None
        if policy is None or not policy.active:
            if setting.halted_at is None:
                setting.next_run_at = now + timedelta(
                    seconds=failure_backoff_seconds
                )
            setting.last_run_at = now
            return {"eligible": False, "created": 0, "reused": 0}

        if (
            policy.relationship_actions_enabled
            and FOLLOW_USER in policy.selected_actions
            and UNFOLLOW_USER in policy.selected_actions
        ):
            setting.halted_at = now
            setting.halted_reason = "autonomy_relationship_action_conflict"
            setting.next_run_at = None
            setting.last_run_at = now
            setting.updated_at = now
            return {"eligible": False, "created": 0, "reused": 0}

        tasks = AgentAutonomyTaskRepository(db)
        if tasks.has_open_task(owner_user_id=owner_user_id):
            setting.last_run_at = now
            setting.next_run_at = now + timedelta(
                seconds=AUTONOMY_CONTROL_INTERVAL_SECONDS
            )
            setting.updated_at = now
            return {"eligible": True, "created": 0, "reused": 0}
        # One owner may create at most one task per control scan.  The task is
        # dispatched in the same scan when its action interval is due, instead
        # of building a database backlog faster than the worker can consume it.
        remaining = min(1, max(1, int(task_limit)))
        if (
            policy.auto_reply_enabled
            and remaining > 0
            and setting.auto_reply_started_at is not None
            and not tasks.has_open_task_type(
                owner_user_id=owner_user_id,
                task_type=AutonomyTaskType.REPLY_TO_MESSAGE.value,
            )
        ):
            reply_not_before = max(
                setting.auto_reply_started_at,
                now - timedelta(seconds=AUTONOMY_REPLY_MAX_AGE_SECONDS),
            )
            candidates = _unanswered_autonomy_reply_candidates(
                db,
                owner_user_id=owner_user_id,
                not_before=reply_not_before,
                now=now,
                limit=1,
            )
            for candidate in candidates:
                key = deterministic_task_key(
                    owner_user_id=owner_user_id,
                    task_type=AutonomyTaskType.REPLY_TO_MESSAGE,
                    source_identity=candidate.message_identity,
                )
                _, was_created = tasks.enqueue(
                    owner_user_id=owner_user_id,
                    external_account_id=account.id,
                    task_type=AutonomyTaskType.REPLY_TO_MESSAGE.value,
                    action_type=SEND_PRIVATE_MESSAGE,
                    idempotency_key=key,
                    policy_version=policy.version,
                    execution_setting_version=policy.execution_setting_version,
                    runner_setting_version=policy.runner_setting_version,
                    model_connection_id=policy.model_connection_id,
                    runner_configuration_fingerprint=(
                        policy.runner_configuration_fingerprint
                    ),
                    source_message_id=candidate.message_id,
                    source_message_identity=candidate.message_identity,
                    target_upstream_uid=candidate.peer_upstream_uid,
                    generation_instruction=str(setting.operation_brief or ""),
                    not_before=now,
                )
                if was_created:
                    created += 1
                    remaining -= 1
                else:
                    reused += 1
                if remaining <= 0:
                    break

        discovery_interval_seconds = max(
            10,
            int(setting.discovery_interval_seconds),
        )
        browse_due = bool(
            policy.discovery_enabled
            and remaining > 0
            and not tasks.has_open_task_type(
                owner_user_id=owner_user_id,
                task_type=AutonomyTaskType.BROWSE_ONLINE.value,
            )
            and (
                setting.last_discovery_at is None
                or setting.last_discovery_at
                + timedelta(seconds=discovery_interval_seconds)
                <= now
            )
        )
        if browse_due:
            slot = int(now.timestamp()) // discovery_interval_seconds
            key = deterministic_task_key(
                owner_user_id=owner_user_id,
                task_type=AutonomyTaskType.BROWSE_ONLINE,
                source_identity=f"browse:{policy.version}:{slot}",
            )
            _, was_created = tasks.enqueue(
                owner_user_id=owner_user_id,
                external_account_id=account.id,
                task_type=AutonomyTaskType.BROWSE_ONLINE.value,
                action_type=BROWSE_ONLINE_USERS,
                idempotency_key=key,
                policy_version=policy.version,
                execution_setting_version=policy.execution_setting_version,
                runner_setting_version=policy.runner_setting_version,
                model_connection_id=policy.model_connection_id,
                runner_configuration_fingerprint=(
                    policy.runner_configuration_fingerprint
                ),
                schedule_slot=str(slot),
                not_before=now,
            )
            created += int(was_created)
            reused += int(not was_created)
            remaining -= 1

        match_due = bool(
            policy.text_match_enabled
            and remaining > 0
            and not tasks.has_open_task_type(
                owner_user_id=owner_user_id,
                task_type=AutonomyTaskType.REQUEST_MATCH.value,
            )
            and (
                setting.last_match_at is None
                or setting.last_match_at
                + timedelta(seconds=discovery_interval_seconds)
                <= now
            )
        )
        if match_due:
            slot = int(now.timestamp()) // discovery_interval_seconds
            key = deterministic_task_key(
                owner_user_id=owner_user_id,
                task_type=AutonomyTaskType.REQUEST_MATCH,
                source_identity=f"match:{policy.version}:{slot}",
            )
            _, was_created = tasks.enqueue(
                owner_user_id=owner_user_id,
                external_account_id=account.id,
                task_type=AutonomyTaskType.REQUEST_MATCH.value,
                action_type=REQUEST_TEXT_MATCH,
                idempotency_key=key,
                policy_version=policy.version,
                execution_setting_version=policy.execution_setting_version,
                runner_setting_version=policy.runner_setting_version,
                model_connection_id=policy.model_connection_id,
                runner_configuration_fingerprint=(
                    policy.runner_configuration_fingerprint
                ),
                schedule_slot=str(slot),
                not_before=now,
            )
            created += int(was_created)
            reused += int(not was_created)
            remaining -= 1

        post_due = bool(
            policy.scheduled_posts_enabled
            and remaining > 0
            and (
                setting.last_post_at is None
                or setting.last_post_at
                + timedelta(minutes=int(setting.post_interval_minutes))
                <= now
            )
        )
        if post_due:
            scheduled_for = quiet_window_end(policy, now=now) or now
            interval_seconds = int(setting.post_interval_minutes) * 60
            slot = int(scheduled_for.timestamp()) // interval_seconds
            source_identity = f"post:{policy.version}:{slot}"
            key = deterministic_task_key(
                owner_user_id=owner_user_id,
                task_type=AutonomyTaskType.SCHEDULED_POST,
                source_identity=source_identity,
            )
            _, was_created = tasks.enqueue(
                owner_user_id=owner_user_id,
                external_account_id=account.id,
                task_type=AutonomyTaskType.SCHEDULED_POST.value,
                action_type=PUBLISH_TEXT_POST,
                idempotency_key=key,
                policy_version=policy.version,
                execution_setting_version=policy.execution_setting_version,
                runner_setting_version=policy.runner_setting_version,
                model_connection_id=policy.model_connection_id,
                runner_configuration_fingerprint=(
                    policy.runner_configuration_fingerprint
                ),
                schedule_slot=str(slot),
                generation_instruction=str(setting.operation_brief or ""),
                scheduled_for=scheduled_for,
                not_before=scheduled_for,
            )
            created += int(was_created)
            reused += int(not was_created)
            remaining -= 1

        if policy.relationship_actions_enabled and remaining > 0:
            budget_day = policy_budget_day(policy, now=now).isoformat()
            for target in sorted(policy.target_allowlist):
                active = tasks.relationship_is_active(
                    owner_user_id=owner_user_id,
                    target_upstream_uid=target,
                )
                action = ""
                task_type = None
                if FOLLOW_USER in policy.selected_actions and not active:
                    action = FOLLOW_USER
                    task_type = AutonomyTaskType.FOLLOW_TARGET
                elif (
                    UNFOLLOW_USER in policy.selected_actions
                    and FOLLOW_USER not in policy.selected_actions
                    and active
                ):
                    action = UNFOLLOW_USER
                    task_type = AutonomyTaskType.UNFOLLOW_TARGET
                if task_type is None:
                    continue
                source_identity = f"relationship:{action}:{target}:{budget_day}"
                key = deterministic_task_key(
                    owner_user_id=owner_user_id,
                    task_type=task_type,
                    source_identity=source_identity,
                )
                _, was_created = tasks.enqueue(
                    owner_user_id=owner_user_id,
                    external_account_id=account.id,
                    task_type=task_type.value,
                    action_type=action,
                    idempotency_key=key,
                    policy_version=policy.version,
                    execution_setting_version=policy.execution_setting_version,
                    runner_setting_version=policy.runner_setting_version,
                    model_connection_id=policy.model_connection_id,
                    runner_configuration_fingerprint=(
                        policy.runner_configuration_fingerprint
                    ),
                    schedule_slot=budget_day,
                    target_upstream_uid=target,
                    not_before=now,
                )
                created += int(was_created)
                reused += int(not was_created)
                remaining -= 1
                if remaining <= 0:
                    break

        if (
            remaining > 0
            and (
                policy.follow_discovered_enabled
                or policy.friend_request_enabled
                or policy.proactive_message_enabled
            )
        ):
            candidates = AgentDiscoveryCandidateRepository(db).list_recent_eligible(
                owner_user_id,
                seen_after=now - timedelta(days=7),
                interaction_before=now - timedelta(minutes=15),
                limit=50,
            )
            budget_day = policy_budget_day(policy, now=now).isoformat()
            for candidate in candidates:
                target = str(candidate.target_upstream_uid or "").strip()
                if (
                    not target
                    or tasks.has_open_task_for_target(
                        owner_user_id=owner_user_id,
                        target_upstream_uid=target,
                    )
                    or tasks.target_is_blocked(
                        owner_user_id=owner_user_id,
                        target_upstream_uid=target,
                    )
                ):
                    continue
                planned = _agent_candidate_next_action(
                    policy=policy,
                    candidate=candidate,
                    tasks=tasks,
                )
                if planned is None:
                    continue
                task_type, action = planned
                key = deterministic_task_key(
                    owner_user_id=owner_user_id,
                    task_type=task_type,
                    source_identity=(
                        f"candidate:{task_type.value}:{target}:{budget_day}"
                    ),
                )
                _, was_created = tasks.enqueue(
                    owner_user_id=owner_user_id,
                    external_account_id=account.id,
                    task_type=task_type.value,
                    action_type=action,
                    idempotency_key=key,
                    policy_version=policy.version,
                    execution_setting_version=policy.execution_setting_version,
                    runner_setting_version=policy.runner_setting_version,
                    model_connection_id=policy.model_connection_id,
                    runner_configuration_fingerprint=(
                        policy.runner_configuration_fingerprint
                    ),
                    schedule_slot=budget_day,
                    target_upstream_uid=target,
                    generation_instruction=(
                        str(setting.operation_brief or "")
                        if task_type
                        in {
                            AutonomyTaskType.PROACTIVE_MESSAGE,
                            AutonomyTaskType.REQUEST_FRIEND,
                        }
                        else ""
                    ),
                    not_before=now,
                )
                created += int(was_created)
                reused += int(not was_created)
                remaining -= 1
                if remaining <= 0:
                    break

        setting.last_run_at = now
        setting.next_run_at = now + timedelta(
            seconds=AUTONOMY_CONTROL_INTERVAL_SECONDS
        )
        setting.updated_at = now
    return {"eligible": True, "created": created, "reused": reused}


def schedule_due_agent_runs(
    *,
    connection: Redis | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Scan due policies, create safe tasks and enqueue only opaque task IDs."""

    from bbw_agent.repositories import (
        AgentAutonomySettingRepository,
        AgentAutonomyTaskRepository,
    )

    settings = get_settings()
    if not bool(getattr(settings, "ai_agent_background_enabled", False)):
        return {"ok": True, "enabled": False, "scheduled": 0}
    batch_size = min(
        max(
            1,
            int(
                limit
                if limit is not None
                else getattr(settings, "ai_agent_dispatch_batch_size", 10)
            ),
        ),
        50,
    )
    failure_backoff = min(
        max(
            60,
            int(getattr(settings, "ai_agent_failure_backoff_seconds", 300)),
        ),
        3600,
    )
    now = utcnow()
    with session_scope() as db:
        stale_unknown = AgentAutonomyTaskRepository(
            db
        ).mark_expired_dispatching_unknown(
            before=now - timedelta(seconds=600),
            stable_error_code="worker_lost",
        )
        due_owner_ids = [
            row.owner_user_id
            for row in AgentAutonomySettingRepository(db).due(
                at=now,
                limit=batch_size,
                for_update=False,
            )
        ]

    created = 0
    reused = 0
    eligible = 0
    for owner_user_id in due_owner_ids:
        if created >= batch_size:
            break
        summary = _schedule_autonomy_owner(
            owner_user_id,
            now=now,
            task_limit=batch_size - created,
            failure_backoff_seconds=failure_backoff,
        )
        created += int(summary["created"])
        reused += int(summary["reused"])
        eligible += int(bool(summary["eligible"]))

    with session_scope() as db:
        task_ids = AgentAutonomyTaskRepository(db).due_task_ids(
            now=now,
            limit=batch_size,
        )
    redis_connection = connection or Redis.from_url(settings.redis_url)
    queue = Queue("agent", connection=redis_connection)
    dispatched = 0
    queue_errors = 0
    for task_id in task_ids:
        try:
            queue.enqueue(
                "bbw_web.jobs.run_unattended_agent",
                str(task_id),
                job_id=f"autonomy-task-v1-{task_id}",
                job_timeout=300,
                result_ttl=0,
                failure_ttl=300,
            )
            dispatched += 1
        except Exception as exc:
            if _duplicate_queue_error(exc):
                dispatched += 1
            else:
                queue_errors += 1
    return {
        "ok": queue_errors == 0,
        "enabled": True,
        "owners_due": len(due_owner_ids),
        "owners_eligible": eligible,
        "tasks_created": created,
        "tasks_reused": reused,
        "tasks_due": len(task_ids),
        "scheduled": dispatched,
        "queue_errors": queue_errors,
        "stale_dispatches_halted": stale_unknown,
    }


def run_unattended_agent(task_id: str) -> dict[str, Any]:
    """Execute one authorized task with a short-lived provider session."""

    from bbw_agent.autonomous import AgentAutonomyOrchestrator
    from bbw_agent.runtime import (
        ByokAgentAutonomyModelRunner,
        FixedLayerAgentAutonomyDispatcher,
        SqlAgentAutonomyStore,
        SqlAlchemyAgentAutonomyDispatchGate,
        bound_sql_agent_autonomy_repository_factory,
    )
    from bbw_web.persistence import RuntimePersistence

    settings = get_settings()
    if not bool(getattr(settings, "ai_agent_background_enabled", False)):
        return {"ok": True, "enabled": False, "status": "disabled"}
    parsed_task_id = _uuid(task_id, field="task_id")
    persistence = RuntimePersistence(settings)
    try:
        orchestrator = AgentAutonomyOrchestrator(
            store=SqlAgentAutonomyStore(
                bound_sql_agent_autonomy_repository_factory(parsed_task_id)
            ),
            model_runner=ByokAgentAutonomyModelRunner(
                settings=settings,
                cipher=persistence.cipher,
            ),
            dispatcher=FixedLayerAgentAutonomyDispatcher(
                persistence=persistence,
                dispatch_gate=SqlAlchemyAgentAutonomyDispatchGate(),
            ),
            lease_seconds=300,
        )
        result = orchestrator.run_once(worker_id=_worker_identity())
        return {
            "ok": True,
            "enabled": True,
            "status": result.status,
            "code": result.code,
            "task_id": str(result.task_id) if result.task_id else None,
        }
    finally:
        persistence.close()


def schedule_due_syncs() -> dict[str, Any]:
    """Lease due accounts, enqueue reconciliation, and dispatch durable media work."""

    settings = get_settings()
    mode = compatibility_mode(settings)
    legacy_dispatch_enabled = mode is CompatibilityMode.ENABLED
    active_seconds = _setting_seconds(settings, "active_sync_seconds", "BBW_ACTIVE_SYNC_SECONDS", 300)
    inactive_seconds = _setting_seconds(
        settings, "inactive_sync_seconds", "BBW_INACTIVE_SYNC_SECONDS", 3600
    )
    batch_size = max(1, min(100, _as_int(os.getenv("BBW_SYNC_BATCH_SIZE"), 20, minimum=1, maximum=100)))
    now = utcnow()
    candidates: list[tuple[uuid.UUID, uuid.UUID, int]] = []
    with session_scope() as db:
        active_ids = _active_owner_ids(db, now=now, active_seconds=active_seconds)
        account_rows = (
            list(
                db.execute(
                    select(ExternalAccount, User)
                    .join(User, User.id == ExternalAccount.user_id)
                    .where(
                        ExternalAccount.sync_enabled.is_(True),
                        User.status == "active",
                    )
                    .order_by(ExternalAccount.last_sync_at.asc().nullsfirst())
                    .limit(500)
                )
            )
            if legacy_dispatch_enabled
            else []
        )
        cursor_rows = {
            row.owner_user_id: row
            for row in db.scalars(
                select(SyncCursor).where(
                    SyncCursor.source == SYNC_SOURCE, SyncCursor.stream == SYNC_STREAM
                )
            )
        }
        for account, _user in account_rows:
            interval = active_seconds if account.user_id in active_ids else inactive_seconds
            cursor = cursor_rows.get(account.user_id)
            stored_due_at = (
                _as_utc(cursor.next_sync_at)
                if cursor and cursor.next_sync_at
                else None
            )
            last_sync_at = (
                _as_utc(cursor.last_succeeded_at)
                if cursor and cursor.last_succeeded_at
                else _as_utc(account.last_sync_at)
                if account.last_sync_at is not None
                else None
            )
            interval_due_at = (
                last_sync_at + timedelta(seconds=interval)
                if last_sync_at is not None
                else None
            )
            due_candidates = [
                value for value in (stored_due_at, interval_due_at) if value is not None
            ]
            due_at = min(due_candidates) if due_candidates else None
            if due_at is not None and _as_utc(due_at) > now:
                continue
            candidates.append((account.user_id, account.id, interval))
            if len(candidates) >= batch_size:
                break

    connection = Redis.from_url(settings.redis_url)
    queue = Queue("sync", connection=connection)
    scheduled: list[tuple[uuid.UUID, int]] = []
    queue_errors = 0
    for owner_id, account_id, interval in candidates:
        slot = int(now.timestamp() // max(60, interval))
        try:
            queue.enqueue(
                "bbw_web.jobs.sync_account_history",
                str(owner_id),
                str(account_id),
                job_id=f"sync-history-{account_id}-{slot}",
                job_timeout=900,
                result_ttl=600,
                failure_ttl=7 * 86400,
            )
            scheduled.append((owner_id, interval))
        except Exception as exc:
            if _duplicate_queue_error(exc):
                scheduled.append((owner_id, interval))
            else:
                queue_errors += 1

    if scheduled:
        with session_scope() as db:
            repo = SyncCursorRepository(db)
            for owner_id, interval in scheduled:
                row = repo.get(owner_id, SYNC_SOURCE, SYNC_STREAM, for_update=True)
                repo.upsert(
                    **_cursor_values(
                        row,
                        owner_user_id=owner_id,
                        next_sync_at=now + timedelta(seconds=interval),
                        error=row.last_error if row else None,
                    )
                )
    media_recovered = _maybe_recover_failed_media_configuration_outboxes(
        settings,
        connection,
        limit=20,
    )
    media_dispatched = _dispatch_media_outboxes(limit=20)
    tim_mirror_batch_size = max(
        1,
        min(
            100,
            _as_int(
                os.getenv("BBW_TIM_MIRROR_BATCH_SIZE"),
                20,
                minimum=1,
                maximum=100,
            ),
        ),
    )
    tim_mirrors = dispatch_due_tim_message_deliveries(
        connection=connection,
        limit=tim_mirror_batch_size,
    )
    compatibility = _enqueue_compatibility_outbox_dispatch(connection=connection)
    return {
        "ok": True,
        "compatibility_mode": mode.value,
        "scheduled": len(scheduled),
        "queue_errors": queue_errors,
        "media_recovered": media_recovered,
        "media_dispatched": media_dispatched,
        "tim_mirror_due": tim_mirrors["due"],
        "tim_mirror_dispatched": tim_mirrors["dispatched"],
        "tim_mirror_queue_errors": tim_mirrors["queue_errors"],
        "compatibility_dispatched": compatibility["dispatched"],
        "compatibility_queue_errors": compatibility["queue_errors"],
    }


def _save_sync_result(
    *,
    owner_user_id: uuid.UUID,
    external_account_id: uuid.UUID,
    active_seconds: int,
    inactive_seconds: int,
    succeeded: bool,
    summary: Mapping[str, Any] | None = None,
    watermark_at: datetime | None = None,
    error: str | None = None,
) -> None:
    now = utcnow()
    with session_scope() as db:
        account = ExternalAccountRepository(db).get_for_user(owner_user_id, for_update=True)
        if account is None or account.id != external_account_id:
            return
        active = owner_user_id in _active_owner_ids(db, now=now, active_seconds=active_seconds)
        interval = active_seconds if active else inactive_seconds
        if not succeeded:
            interval = min(interval, 300)
        repo = SyncCursorRepository(db)
        row = repo.get(owner_user_id, SYNC_SOURCE, SYNC_STREAM, for_update=True)
        repo.upsert(
            **_cursor_values(
                row,
                owner_user_id=owner_user_id,
                next_sync_at=now + timedelta(seconds=max(60, interval)),
                attempted_at=now,
                succeeded_at=now if succeeded else None,
                watermark_at=watermark_at,
                cursor=(
                    json.dumps(_json_safe(summary), ensure_ascii=False, sort_keys=True)
                    if summary is not None
                    else None
                ),
                error=_bounded(error, 2000) or None,
            )
        )
        if succeeded:
            account.last_sync_at = now


def _tim_c2c_unread_counts(
    client: MessageHistoryTransport,
    account_uid: str,
    peers: Sequence[str],
) -> tuple[dict[str, int], int]:
    normalized = list(
        dict.fromkeys(
            _bounded(peer, 128)
            for peer in peers
            if _bounded(peer, 128) and _bounded(peer, 128) != account_uid
        )
    )[:500]
    counts: dict[str, int] = {}
    request_count = 0
    for offset in range(0, len(normalized), 100):
        result = client.c2c_unread_counts(
            account_uid,
            normalized[offset : offset + 100],
        )
        request_count += 1
        if not result.ok:
            continue
        data = result.data if isinstance(result.data, Mapping) else {}
        rows = data.get("C2CUnreadMsgNumList")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            peer = _bounded(row.get("Peer_Account"), 128)
            if peer:
                counts[peer] = _as_int(row.get("C2CUnreadMsgNum"), 0, minimum=0)
    return counts, request_count


def _tim_apply_outgoing_read_state(
    items: list[dict[str, Any]],
    *,
    account_uid: str,
    peer_uid: str,
    unread_count: int | None,
) -> None:
    if unread_count is None:
        return
    outgoing = [
        item
        for item in items
        if _bounded(item.get("from") or item.get("from_user_id"), 128) == account_uid
        and _bounded(item.get("to") or item.get("to_user_id"), 128) == peer_uid
    ]
    read_count = max(0, len(outgoing) - max(0, int(unread_count)))
    for index, item in enumerate(outgoing):
        is_read = index < read_count
        item["is_peer_read"] = is_read
        item["read_state"] = "read" if is_read else "unread"


def _tim_recent_conversations(
    client: MessageHistoryTransport,
    account_uid: str,
    *,
    max_pages: int,
    max_conversations: int = 500,
) -> list[dict[str, Any]]:
    timestamp = 0
    start_index = 0
    top_timestamp = 0
    top_start_index = 0
    conversations: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_cursor: tuple[int, int, int, int] | None = None
    observed_at = utcnow()
    for _page in range(max(1, min(int(max_pages), 10))):
        result = client.recent_contacts(
            account_uid,
            timestamp=timestamp,
            start_index=start_index,
            top_timestamp=top_timestamp,
            top_start_index=top_start_index,
        )
        if not result.ok:
            raise RuntimeError(
                f"TIM recent contacts failed: {result.error_code or result.error_info}"
            )
        data = result.data if isinstance(result.data, Mapping) else {}
        items = data.get("SessionItem")
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            session_type = str(item.get("Type") or "").strip().lower()
            if session_type not in {"1", "c2c"}:
                continue
            peer = _bounded(item.get("To_Account"), 128)
            if not peer or peer == account_uid or peer in seen:
                continue
            seen.add(peer)
            conversations.append(
                {
                    "id": _canonical_conversation_id(peer),
                    "peer_id": peer,
                    "conversation_user": peer,
                    "timestamp": item.get("MsgTime"),
                    "activity_sequence": item.get("MsgSeq"),
                    "unread_count": _as_int(
                        item.get("UnreadMsgCount") or item.get("UnreadMsgNum"), 0
                    ),
                    "unread_observed_at": observed_at,
                    "unread_authoritative": True,
                    "source": "tim_rest",
                }
            )
            if len(conversations) >= max(1, min(int(max_conversations), 500)):
                break
        if len(conversations) >= max(1, min(int(max_conversations), 500)):
            break
        if _as_int(data.get("CompleteFlag"), 0) == 1:
            break
        cursor = (
            _as_int(data.get("TimeStamp"), timestamp, minimum=0),
            _as_int(data.get("StartIndex"), start_index, minimum=0),
            _as_int(data.get("TopTimeStamp"), top_timestamp, minimum=0),
            _as_int(data.get("TopStartIndex"), top_start_index, minimum=0),
        )
        if cursor == previous_cursor or not items:
            break
        previous_cursor = cursor
        timestamp, start_index, top_timestamp, top_start_index = cursor
    conversations = sorted(
        conversations,
        key=lambda item: _as_int(
            item.get("timestamp"),
            0,
            minimum=0,
            maximum=10**18,
        ),
        reverse=True,
    )
    unread_counts, _requests = _tim_c2c_unread_counts(
        client,
        account_uid,
        [str(item.get("peer_id") or "") for item in conversations],
    )
    for item in conversations:
        peer = str(item.get("peer_id") or "")
        if peer in unread_counts:
            item["unread_count"] = unread_counts[peer]
    return conversations


def _durable_message_conversations(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    account_uid: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    rows: list[tuple[Any, Any]] = []
    rows.extend(
        db.execute(
            select(Conversation.peer_upstream_uid, Conversation.last_message_at)
            .where(
                Conversation.owner_user_id == owner_user_id,
                Conversation.kind == "direct",
                Conversation.peer_upstream_uid.is_not(None),
            )
            .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.updated_at.desc())
            .limit(max(1, min(int(limit), 500)))
        )
    )
    rows.extend(
        db.execute(
            select(Relationship.subject_upstream_uid, Relationship.updated_at)
            .where(
                Relationship.owner_user_id == owner_user_id,
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
                or_(
                    and_(
                        Relationship.provider == SYNC_SOURCE,
                        Relationship.kind == "friend",
                    ),
                    and_(
                        Relationship.provider == "web-policy",
                        Relationship.kind.in_(("match", "message_peer")),
                    ),
                ),
            )
            .order_by(Relationship.updated_at.desc())
            .limit(max(1, min(int(limit), 500)))
        )
    )
    conversations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for peer_value, observed_at in rows:
        peer = _bounded(peer_value, 128)
        if not peer or peer == account_uid or peer in seen:
            continue
        seen.add(peer)
        conversations.append(
            {
                "id": _canonical_conversation_id(peer),
                "peer_id": peer,
                "conversation_user": peer,
                "timestamp": observed_at,
                "unread_count": 0,
                "unread_authoritative": False,
                "source": "durable",
            }
        )
        if len(conversations) >= max(1, min(int(limit), 500)):
            break
    return conversations


def _tim_roaming_history(
    client: MessageHistoryTransport,
    *,
    account_uid: str,
    peer_uid: str,
    min_time: int,
    max_time: int,
    max_messages: int,
) -> tuple[list[dict[str, Any]], int]:
    raw_items: list[dict[str, Any]] = []
    request_count = 0
    page_limit = max(1, (max(1, int(max_messages)) + 99) // 100)
    for sender, recipient in ((peer_uid, account_uid), (account_uid, peer_uid)):
        last_msg_key = ""
        for _page in range(page_limit):
            result = client.roaming_messages(
                sender,
                recipient,
                min_time=min_time,
                max_time=max_time,
                max_count=min(100, max(1, int(max_messages))),
                last_msg_key=last_msg_key,
            )
            request_count += 1
            if not result.ok:
                raise RuntimeError(
                    f"TIM roaming history failed: {result.error_code or result.error_info}"
                )
            data = result.data if isinstance(result.data, Mapping) else {}
            items = data.get("MsgList")
            if not isinstance(items, list):
                items = []
            raw_items.extend(dict(item) for item in items if isinstance(item, Mapping))
            next_key = _bounded(data.get("LastMsgKey"), 256)
            if _as_int(data.get("Complete"), 0) == 1 or not items or not next_key:
                break
            if next_key == last_msg_key:
                break
            last_msg_key = next_key

    normalized = normalize_messages(raw_items)
    by_identity: dict[str, dict[str, Any]] = {}
    for item in normalized:
        identity = _bounded(item.get("msg_key") or item.get("id"), 512)
        if not identity:
            identity = _stable_json_digest(item)
        by_identity[identity] = item
    ordered = sorted(
        by_identity.values(),
        key=lambda item: _parse_time(item.get("timestamp") or item.get("time")),
    )
    ordered = ordered[-max(1, int(max_messages)) :]
    unread_counts, unread_requests = _tim_c2c_unread_counts(
        client,
        peer_uid,
        [account_uid],
    )
    request_count += unread_requests
    _tim_apply_outgoing_read_state(
        ordered,
        account_uid=account_uid,
        peer_uid=peer_uid,
        unread_count=unread_counts.get(account_uid),
    )
    return ordered, request_count


def sync_account_history(
    owner_user_id: str,
    external_account_id: str,
    *,
    transport_factory: Callable[[], MessageHistoryTransport] | None = None,
) -> dict[str, Any]:
    """Reconcile C2C conversations/messages through server-owned TIM REST APIs."""

    mode = compatibility_mode()
    if mode is not CompatibilityMode.ENABLED:
        return {
            "ok": True,
            "ignored": True,
            "reason": "legacy compatibility dispatch is disabled",
            "compatibility_mode": mode.value,
        }
    owner_id = _uuid(owner_user_id, field="owner_user_id")
    account_id = _uuid(external_account_id, field="external_account_id")
    settings = get_settings()
    active_seconds = _setting_seconds(settings, "active_sync_seconds", "BBW_ACTIVE_SYNC_SECONDS", 300)
    inactive_seconds = _setting_seconds(
        settings, "inactive_sync_seconds", "BBW_INACTIVE_SYNC_SECONDS", 3600
    )
    max_pages = max(
        1,
        min(10, _as_int(os.getenv("BBW_SYNC_MAX_PAGES"), 5, minimum=1, maximum=10)),
    )
    max_conversations = max(
        1,
        min(
            100,
            _as_int(
                os.getenv("BBW_SYNC_MAX_CONVERSATIONS"),
                20,
                minimum=1,
                maximum=100,
            ),
        ),
    )
    max_messages = max(
        1,
        min(
            2000,
            _as_int(
                os.getenv("BBW_SYNC_MAX_MESSAGES_PER_PEER"),
                500,
                minimum=1,
                maximum=2000,
            ),
        ),
    )
    now = utcnow()
    with session_scope() as db:
        _user, account = _load_owner_binding(db, owner_id, account_id)
        if not account.sync_enabled:
            return {"ok": True, "ignored": True, "reason": "account synchronization is disabled"}
        account_uid = _bounded(account.upstream_uid, 128)
        if not account_uid:
            raise RuntimeError("upstream account UID is missing")
        cursor_row = SyncCursorRepository(db).get(owner_id, SYNC_SOURCE, SYNC_STREAM)
        try:
            previous_cursor = json.loads(cursor_row.cursor) if cursor_row and cursor_row.cursor else {}
        except (TypeError, json.JSONDecodeError):
            previous_cursor = {}
        peer_offset = _as_int(
            previous_cursor.get("next_peer_offset") if isinstance(previous_cursor, Mapping) else 0,
            0,
            minimum=0,
            maximum=1_000_000,
        )
        stored_peer_watermarks = (
            previous_cursor.get("peer_watermarks")
            if isinstance(previous_cursor, Mapping)
            and isinstance(previous_cursor.get("peer_watermarks"), Mapping)
            else {}
        )
        peer_watermarks = {
            _bounded(peer, 128): _as_int(value, 0, minimum=0)
            for peer, value in dict(stored_peer_watermarks).items()
            if _bounded(peer, 128)
        }
        previous_watermark = (
            _as_utc(cursor_row.watermark_at)
            if cursor_row and cursor_row.watermark_at
            else None
        )
        owner_is_active = owner_id in _active_owner_ids(
            db, now=now, active_seconds=active_seconds
        )
        durable_conversations = _durable_message_conversations(
            db,
            owner_user_id=owner_id,
            account_uid=account_uid,
        )

    watermark: datetime | None = previous_watermark
    client = (transport_factory or _default_message_history_transport)()
    try:
        contact_fallback = False
        try:
            recent_conversations = _tim_recent_conversations(
                client,
                account_uid,
                max_pages=max_pages,
            )
        except Exception:
            recent_conversations = []
            contact_fallback = True

        conversations: list[dict[str, Any]] = []
        seen_peers: set[str] = set()
        for item in recent_conversations + durable_conversations:
            peer = _bounded(item.get("peer_id") or item.get("conversation_user"), 128)
            if not peer or peer == account_uid or peer in seen_peers:
                continue
            seen_peers.add(peer)
            conversations.append(item)
            if len(conversations) >= 500:
                break
        if not conversations and contact_fallback:
            raise RuntimeError("TIM recent contacts failed and no durable peers are available")

        if conversations:
            start = peer_offset % len(conversations)
            rotated = conversations[start:] + conversations[:start]
            if previous_watermark is None:
                changed = list(conversations)
            else:
                changed_cutoff = previous_watermark - timedelta(minutes=5)
                changed = []
                for item in conversations:
                    stamp = _optional_time(item.get("timestamp") or item.get("time"))
                    if _as_int(item.get("unread_count"), 0) > 0 or (
                        stamp is not None and stamp >= changed_cutoff
                    ):
                        changed.append(item)
            selected_conversations = changed[:max_conversations]
            selected_peers = {
                _bounded(item.get("peer_id") or item.get("conversation_user"), 128)
                for item in selected_conversations
            }
            fallback_target = min(
                max(0, max_conversations - len(selected_conversations)),
                5 if owner_is_active else max_conversations,
            )
            fallback_added = 0
            for item in rotated:
                if len(selected_conversations) >= max_conversations or fallback_added >= fallback_target:
                    break
                peer = _bounded(item.get("peer_id") or item.get("conversation_user"), 128)
                if not peer or peer in selected_peers:
                    continue
                selected_conversations.append(item)
                selected_peers.add(peer)
                fallback_added += 1
            advance = fallback_added or min(len(selected_conversations), max_conversations) or 1
            next_peer_offset = (start + advance) % len(conversations)
        else:
            selected_conversations = []
            next_peer_offset = 0

        conversation_result = ingest_history_response(
            str(owner_id),
            str(account_id),
            "/api/im/conversations",
            {"page": "tim-rest"},
            {"ok": True, "items": conversations},
        )
        messages_created = 0
        messages_existing = 0
        roaming_requests = 0
        peer_errors: list[str] = []
        now_epoch = int(now.timestamp())
        retention_floor = now_epoch - max(1, int(settings.message_retention_days)) * 86400
        for index, conversation in enumerate(selected_conversations, 1):
            peer = _bounded(
                conversation.get("peer_id") or conversation.get("conversation_user"), 128
            )
            if not peer:
                continue
            previous_peer_time = _as_int(peer_watermarks.get(peer), 0, minimum=0)
            min_time = max(retention_floor, previous_peer_time - 300) if previous_peer_time else retention_floor
            try:
                items, request_count = _tim_roaming_history(
                    client,
                    account_uid=account_uid,
                    peer_uid=peer,
                    min_time=min_time,
                    max_time=now_epoch + 60,
                    max_messages=max_messages,
                )
                roaming_requests += request_count
            except Exception as exc:
                peer_errors.append(f"peer-{index}:{str(exc)[:300]}")
                continue

            peer_watermark = previous_peer_time
            for item in items:
                stamp = _optional_time(item.get("timestamp") or item.get("time"))
                if stamp:
                    peer_watermark = max(peer_watermark, int(stamp.timestamp()))
                    if watermark is None or stamp > watermark:
                        watermark = stamp
            peer_watermarks[peer] = max(peer_watermark, now_epoch if not items else 0)
            result_summary = ingest_history_response(
                str(owner_id),
                str(account_id),
                "/api/im/messages",
                {"peer": peer},
                {"ok": True, "items": items},
            )
            messages_created += int(result_summary.get("messages_created") or 0)
            messages_existing += int(result_summary.get("messages_existing") or 0)

        if selected_conversations and len(peer_errors) >= len(selected_conversations):
            raise RuntimeError("TIM roaming reconciliation failed for every selected conversation")

        peer_watermarks = dict(
            sorted(peer_watermarks.items(), key=lambda item: item[1], reverse=True)[:500]
        )
        summary = {
            "conversations": int(conversation_result.get("conversations") or 0),
            "messages_created": messages_created,
            "messages_existing": messages_existing,
            "peer_errors": len(peer_errors),
            "next_peer_offset": next_peer_offset,
            "roaming_requests": roaming_requests,
            "contact_fallback": contact_fallback,
        }
        cursor_state = {**summary, "peer_watermarks": peer_watermarks}
        _save_sync_result(
            owner_user_id=owner_id,
            external_account_id=account_id,
            active_seconds=active_seconds,
            inactive_seconds=inactive_seconds,
            succeeded=True,
            summary=cursor_state,
            watermark_at=watermark,
            error="; ".join(peer_errors[:10]) or None,
        )
        return {"ok": True, **summary}
    except Exception as exc:
        _save_sync_result(
            owner_user_id=owner_id,
            external_account_id=account_id,
            active_seconds=active_seconds,
            inactive_seconds=inactive_seconds,
            succeeded=False,
            watermark_at=watermark,
            error=str(exc),
        )
        raise


def _media_hosts(settings: Settings) -> tuple[str, ...]:
    configured = getattr(settings, "media_allowed_hosts", None)
    if isinstance(configured, str):
        values = tuple(item.strip() for item in configured.split(",") if item.strip())
    elif configured:
        values = tuple(str(item).strip() for item in configured if str(item).strip())
    else:
        raw = os.getenv("BBW_MEDIA_ALLOWED_HOSTS", "")
        values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or DEFAULT_MEDIA_HOSTS


def _outbox_payload(outbox_id: uuid.UUID) -> tuple[uuid.UUID, dict[str, Any], int] | None:
    with session_scope() as db:
        row = db.scalar(select(OperationOutbox).where(OperationOutbox.id == outbox_id).with_for_update())
        if row is None or row.operation_type != MEDIA_OPERATION:
            return None
        if row.status in {"completed", "failed"}:
            return None
        if row.status in {"pending", "retry"}:
            row.status = "processing"
            row.attempt_count += 1
            row.locked_by = _worker_identity()
            row.locked_until = utcnow() + timedelta(minutes=10)
        return row.owner_user_id, dict(row.payload or {}), int(row.attempt_count)


def _finish_outbox(outbox_id: uuid.UUID) -> None:
    with session_scope() as db:
        row = db.scalar(select(OperationOutbox).where(OperationOutbox.id == outbox_id).with_for_update())
        if row is None:
            return
        row.status = "completed"
        row.completed_at = utcnow()
        row.locked_by = None
        row.locked_until = None
        row.last_error = None


def _fail_outbox(outbox_id: uuid.UUID, exc: Exception, *, permanent: bool) -> None:
    with session_scope() as db:
        row = db.scalar(select(OperationOutbox).where(OperationOutbox.id == outbox_id).with_for_update())
        if row is None or row.status == "completed":
            return
        exhausted = row.attempt_count >= row.max_attempts
        row.status = "failed" if permanent or exhausted else "retry"
        row.available_at = utcnow() + timedelta(
            seconds=min(3600, 30 * (2 ** min(row.attempt_count, 7)))
        )
        row.locked_by = None
        row.locked_until = None
        row.last_error = str(exc)[:2000]


def _defer_outbox_configuration_error(outbox_id: uuid.UUID, exc: Exception) -> bool:
    """Keep a configuration-blocked media outbox recoverable and request periodic alerts."""

    with session_scope() as db:
        row = db.scalar(
            select(OperationOutbox).where(OperationOutbox.id == outbox_id).with_for_update()
        )
        if row is None:
            return True
        if row.status == "completed":
            return False
        # Configuration failures do not consume the ordinary transient-error
        # retry budget.  Extending the ceiling also keeps attempt numbers
        # monotonic, so every later RQ job id remains unique while credentials
        # are repaired outside the worker.
        row.max_attempts = max(row.max_attempts + 1, row.attempt_count + 1)
        row.status = "retry"
        row.available_at = utcnow() + timedelta(
            seconds=min(3600, 30 * (2 ** min(row.attempt_count, 7)))
        )
        row.locked_by = None
        row.locked_until = None
        row.last_error = str(exc)[:2000]
        return (
            row.attempt_count > 0
            and row.attempt_count % MEDIA_CONFIGURATION_ALERT_INTERVAL == 0
        )


def _release_disabled_media_outbox(outbox_id: uuid.UUID, mode: CompatibilityMode) -> None:
    """兼容模式非 enabled 时，把已被派发 claim 的行退回 retry 并补回预算。"""

    with session_scope() as db:
        row = db.scalar(
            select(OperationOutbox).where(OperationOutbox.id == outbox_id).with_for_update()
        )
        if row is None or row.status in {"completed", "failed"}:
            return
        # 派发阶段 _claim_media_outboxes 已把行置为 processing 并消耗一次
        # attempt；模式切换导致的放弃不应占用瞬时错误的重试预算。这里抬高
        # max_attempts（而非回退 attempt_count）保持 attempt 序号单调，
        # 后续 RQ job id 仍然唯一。
        row.max_attempts = max(int(row.max_attempts or 0) + 1, int(row.attempt_count or 0) + 1)
        row.status = "retry"
        row.available_at = utcnow()
        row.locked_by = None
        row.locked_until = None
        # 说明文字不得命中 MEDIA_CONFIGURATION_ERROR_MARKERS，
        # 避免被配置错误恢复流程误判为 R2 凭据问题。
        row.last_error = f"兼容模式为 {mode.value}，归档任务退回等待重新派发"[:2000]


def _permanent_media_error(exc: Exception) -> bool:
    if isinstance(exc, NotFoundError):
        return True
    if isinstance(exc, QuotaExceeded):
        return False
    if isinstance(exc, MediaArchiveError):
        message = str(exc).lower()
        return not any(
            marker in message
            for marker in ("cannot be resolved", "media is empty", "too many media redirects")
        )
    if isinstance(exc, ValueError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(exc.response.status_code)
        return 400 <= status < 500 and status not in {408, 425, 429}
    return False


def _media_storage_configuration_error(
    exc: Exception,
    *,
    during_r2_initialization: bool = False,
) -> bool:
    if during_r2_initialization and isinstance(exc, (RuntimeError, OSError, ValueError)):
        # R2Storage validates required settings and reads credential files
        # or validates endpoint values before boto3 can produce a ClientError.
        # Restrict this classification to that initialization boundary so
        # unrelated worker failures keep their normal retry behavior.
        return True
    if not isinstance(exc, ClientError):
        return False
    response = exc.response if isinstance(exc.response, Mapping) else {}
    error = response.get("Error") if isinstance(response.get("Error"), Mapping) else {}
    metadata = (
        response.get("ResponseMetadata")
        if isinstance(response.get("ResponseMetadata"), Mapping)
        else {}
    )
    code = str(error.get("Code") or "").strip()
    try:
        status = int(metadata.get("HTTPStatusCode") or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    return status in {401, 403} or code in {
        "AccessDenied",
        "AuthorizationHeaderMalformed",
        "InvalidAccessKeyId",
        "NoSuchBucket",
        "InvalidToken",
        "SignatureDoesNotMatch",
    }


def archive_media_job(outbox_id: str) -> dict[str, Any]:
    """Download, validate and copy one media object to a private R2 bucket."""

    operation_id = _uuid(outbox_id, field="outbox_id")
    mode = compatibility_mode()
    if mode is not CompatibilityMode.ENABLED:
        # 已入队但模式已切换的任务直接放弃执行。行在派发时已被
        # _claim_media_outboxes 置为 processing 并消耗一次 attempt，
        # 必须退回 retry 并补回预算，否则反复暂停/恢复会耗尽
        # MEDIA_MAX_ATTEMPTS，行永久卡在 processing 无人再派发。
        _release_disabled_media_outbox(operation_id, mode)
        return {
            "ok": True,
            "ignored": True,
            "outbox_id": str(operation_id),
            "reason": "legacy compatibility dispatch is disabled",
            "compatibility_mode": mode.value,
        }
    loaded = _outbox_payload(operation_id)
    if loaded is None:
        return {"ok": True, "ignored": True}
    owner_id, payload, _attempt = loaded
    message_id = _uuid(payload.get("message_id"), field="message_id")
    settings = get_settings()
    source_digest = _bounded(payload.get("source_url_hash"), 64)
    encrypted_source = payload.get("source_url_encrypted")
    if (
        len(source_digest) != 64
        or any(char not in "0123456789abcdef" for char in source_digest.lower())
        or not isinstance(encrypted_source, Mapping)
    ):
        exc = ValueError("encrypted media source URL is missing")
        _fail_outbox(operation_id, exc, permanent=True)
        return {"ok": False, "permanent": True, "error": str(exc)}
    try:
        source_url = CredentialCipher.from_settings(settings).decrypt_text(
            encrypted_source,
            purpose="media-source.url",
            context=_media_source_context(owner_id, message_id, source_digest),
        )
    except Exception:
        exc = ValueError("encrypted media source URL cannot be decrypted")
        _fail_outbox(operation_id, exc, permanent=True)
        return {"ok": False, "permanent": True, "error": str(exc)}
    source_url_safe = _safe_url(source_url)
    kind = _bounded(payload.get("kind"), 32)
    if not source_url.startswith("https://"):
        exc = ValueError("media source URL must use HTTPS")
        _fail_outbox(operation_id, exc, permanent=True)
        return {"ok": False, "permanent": True, "error": str(exc)}

    storage: R2Storage | None = None
    prepared: PreparedMedia | None = None
    reserved_media_id: uuid.UUID | None = None
    object_key = ""
    initializing_r2_storage = False
    try:
        with session_scope() as db:
            message = MessageRepository(db).get(owner_id, message_id)
            if message is None:
                raise NotFoundError("message was not found")
            existing = db.scalar(
                select(MediaObject).where(
                    MediaObject.owner_user_id == owner_id,
                    MediaObject.message_id == message_id,
                    MediaObject.source_url == source_url_safe,
                    MediaObject.deleted_at.is_(None),
                    MediaObject.status == "available",
                )
            )
            if existing is not None:
                existing_id = existing.id
            else:
                existing_id = None
        if existing_id:
            _finish_outbox(operation_id)
            return {"ok": True, "media_id": str(existing_id), "existing": True}

        initializing_r2_storage = True
        storage = R2Storage(settings)
        initializing_r2_storage = False
        with session_scope() as db:
            stale = list(
                db.scalars(
                    select(MediaObject).where(
                        MediaObject.owner_user_id == owner_id,
                        MediaObject.message_id == message_id,
                        MediaObject.source_url == source_url_safe,
                        MediaObject.deleted_at.is_(None),
                        MediaObject.status.in_(("pending", "uploading", "delete_failed")),
                    )
                )
            )
            stale_objects = [
                (item.id, item.r2_bucket, item.r2_object_key) for item in stale
            ]
        for stale_id, stale_bucket, stale_key in stale_objects:
            if stale_bucket != storage.bucket:
                raise RuntimeError("stale media object belongs to a different R2 bucket")
            storage.delete(stale_key)
            with session_scope() as db:
                MediaQuotaService(db, settings).release(
                    owner_user_id=owner_id,
                    media_id=stale_id,
                    final_status="failed",
                )

        prepared = download_and_prepare(
            url=source_url,
            kind=kind,
            allowed_hosts=_media_hosts(settings),
            original_name=_bounded(payload.get("original_name"), 255),
            settings=settings,
        )
        object_key = storage.object_key(str(owner_id), prepared.extension)
        with session_scope() as db:
            reservation = MediaQuotaService(db, settings).reserve(
                owner_user_id=owner_id,
                kind=kind,
                size_bytes=prepared.size,
                r2_bucket=storage.bucket,
                r2_object_key=object_key,
                content_type=prepared.content_type,
                sha256=prepared.sha256,
                message_id=message_id,
                original_filename=prepared.original_name,
                source_url=source_url_safe,
                metadata={
                    "outbox_id": str(operation_id),
                    "reported_kind": _bounded(payload.get("reported_kind"), 40),
                    "reported": _json_safe(payload.get("reported")),
                },
            )
            reservation.media.status = "uploading"
            reserved_media_id = reservation.media.id

        with prepared.path.open("rb") as handle:
            stored = storage.upload_stream(
                key=object_key,
                stream=handle,
                size=prepared.size,
                content_type=prepared.content_type,
                sha256=prepared.sha256,
                metadata={"archive": "v1"},
            )
        with session_scope() as db:
            media = MediaQuotaService(db, settings).mark_available(owner_id, reserved_media_id)
            media.extra_data = _merge_dict(
                media.extra_data,
                {"etag": stored.etag, "archived_at": utcnow().isoformat()},
            )
        _finish_outbox(operation_id)
        return {"ok": True, "media_id": str(reserved_media_id), "size": prepared.size}
    except Exception as exc:
        safe_error_text = re.sub(
            r"https?://[^\s'\"<>]+",
            "[MEDIA_URL]",
            str(exc).replace(source_url, "[MEDIA_URL]"),
            flags=re.IGNORECASE,
        )[:2000]
        safe_exc = RuntimeError(safe_error_text or type(exc).__name__)
        if reserved_media_id is not None:
            safe_to_release = False
            if storage is not None and object_key:
                try:
                    storage.delete(object_key)
                    safe_to_release = True
                except Exception:
                    safe_to_release = False
            if safe_to_release:
                try:
                    with session_scope() as db:
                        MediaQuotaService(db, settings).release(
                            owner_user_id=owner_id,
                            media_id=reserved_media_id,
                            final_status="failed",
                        )
                except Exception:
                    pass
            else:
                try:
                    with session_scope() as db:
                        media = MediaObjectRepository(db).get(owner_id, reserved_media_id)
                        if media is not None:
                            media.status = "delete_failed"
                            media.extra_data = _merge_dict(
                                media.extra_data, {"last_error": safe_error_text[:1000]}
                            )
                except Exception:
                    pass
        configuration_error = _media_storage_configuration_error(
            exc,
            during_r2_initialization=initializing_r2_storage,
        )
        permanent = _permanent_media_error(exc)
        if configuration_error:
            # Invalid R2 credentials cannot be repaired by rerunning the same
            # RQ job immediately.  Preserve a durable retry for recovery after
            # configuration is fixed, and periodically fail the RQ invocation
            # so exhausted configuration retries remain visible to operators.
            should_alert = _defer_outbox_configuration_error(operation_id, safe_exc)
            LOGGER.warning(
                "Media archival deferred for an R2 configuration error: "
                "outbox_id=%s attempt=%d error=%s",
                operation_id,
                _attempt,
                safe_error_text[:500],
            )
            if should_alert:
                raise RuntimeError(
                    "media archival configuration failure; durable retry remains scheduled"
                ) from None
            return {
                "ok": False,
                "retry": True,
                "configuration_error": True,
                "error": safe_error_text[:500],
            }
        _fail_outbox(operation_id, safe_exc, permanent=permanent)
        if permanent:
            return {"ok": False, "permanent": True, "error": safe_error_text[:500]}
        raise RuntimeError(f"media archival transient failure: {type(exc).__name__}") from None
    finally:
        if prepared is not None:
            prepared.cleanup()


def _cleanup_web_native_media(
    settings: Settings,
    storage: R2Storage,
    *,
    at: datetime,
    limit: int = WEB_NATIVE_MEDIA_CLEANUP_BATCH,
) -> dict[str, int]:
    """Clean Web-native upload artifacts and release committed asset quota."""

    from bbw_web.media_native.r2_adapter import R2PrivateMediaAdapter
    from bbw_web.media_native.repository import SqlAlchemyMediaNativeRepository

    bounded_limit = max(1, min(int(limit), 1000))
    grace_seconds = max(
        60 * 60,
        min(
            30 * 24 * 60 * 60,
            int(
                getattr(
                    settings,
                    "media_native_unsent_grace_seconds",
                    WEB_NATIVE_MEDIA_UNSENT_GRACE_SECONDS,
                )
                or WEB_NATIVE_MEDIA_UNSENT_GRACE_SECONDS
            ),
        ),
    )
    unsent_grace = timedelta(seconds=grace_seconds)
    upload_cleanup_grace = timedelta(
        seconds=max(
            5 * 60,
            min(
                24 * 60 * 60,
                int(
                    getattr(
                        settings,
                        "media_native_upload_cleanup_grace_seconds",
                        WEB_NATIVE_UPLOAD_CLEANUP_GRACE_SECONDS,
                    )
                    or WEB_NATIVE_UPLOAD_CLEANUP_GRACE_SECONDS
                ),
            ),
        )
    )
    adapter = R2PrivateMediaAdapter(settings, storage=storage)
    result = {
        "upload_intents_deleted": 0,
        "upload_intent_errors": 0,
        "assets_deleted": 0,
        "asset_errors": 0,
    }

    with session_scope() as db:
        intents = SqlAlchemyMediaNativeRepository(db).claim_expired_upload_intents(
            at=at,
            cleanup_grace=upload_cleanup_grace,
            limit=bounded_limit,
        )
    for intent in intents:
        try:
            adapter.delete_upload_artifacts(intent)
            with session_scope() as db:
                deleted = SqlAlchemyMediaNativeRepository(
                    db
                ).delete_cancelled_upload_intent(intent.id)
            if deleted:
                result["upload_intents_deleted"] += 1
        except Exception:
            result["upload_intent_errors"] += 1
            LOGGER.warning(
                "Web-native upload artifact cleanup failed: intent_id=%s",
                intent.id,
                exc_info=True,
            )

    with session_scope() as db:
        candidates = SqlAlchemyMediaNativeRepository(db).asset_cleanup_candidates(
            at=at,
            unsent_grace=unsent_grace,
            limit=bounded_limit,
        )
    for asset_id, owner_user_id in candidates:
        try:
            with session_scope() as db:
                deleted = SqlAlchemyMediaNativeRepository(
                    db
                ).delete_asset_and_release_quota(
                    asset_id=asset_id,
                    owner_user_id=owner_user_id,
                    at=at,
                    unsent_grace=unsent_grace,
                    delete_object=adapter.delete_private_asset,
                )
            if deleted:
                result["assets_deleted"] += 1
        except Exception:
            result["asset_errors"] += 1
            LOGGER.warning(
                "Web-native asset cleanup failed: asset_id=%s",
                asset_id,
                exc_info=True,
            )
    return result


def cleanup_expired_data() -> dict[str, Any]:
    """Delete expired private R2 objects first, then release quota and purge rows."""

    settings = get_settings()
    now = utcnow()
    media_deleted = 0
    media_errors = 0
    compat_media_deleted = 0
    compat_media_errors = 0
    storage: R2Storage | None
    try:
        storage = R2Storage(settings)
    except Exception:
        storage = None

    if storage is not None:
        with session_scope() as db:
            rows = MediaObjectRepository(db).expired_batch(at=now, limit=200)
            pending = [
                (row.owner_user_id, row.id, row.r2_bucket, row.r2_object_key, row.status)
                for row in rows
            ]
            for row in rows:
                row.status = "deleting"
        for owner_id, media_id, bucket, object_key, previous_status in pending:
            try:
                if bucket != storage.bucket:
                    raise RuntimeError("media object belongs to a different R2 bucket")
                storage.delete(object_key)
                with session_scope() as db:
                    MediaQuotaService(db, settings).release(
                        owner_user_id=owner_id, media_id=media_id, final_status="deleted"
                    )
                media_deleted += 1
            except Exception as exc:
                media_errors += 1
                with session_scope() as db:
                    media = MediaObjectRepository(db).get(owner_id, media_id)
                    if media is not None and media.deleted_at is None:
                        media.status = previous_status
                        media.extra_data = _merge_dict(
                            media.extra_data,
                            {"delete_error": str(exc)[:1000], "delete_attempted_at": now.isoformat()},
                        )
        try:
            from bbw_web.moment_video import (
                COMPAT_PROFILE,
                _cache_index_keys,
                cached_assets_for_cleanup,
                clear_empty_cache_index,
                compatibility_profiles_for_cleanup,
                forget_cached_asset,
                object_key_for_asset,
            )

            connection = Redis.from_url(settings.redis_url)
            try:
                for compat_profile in compatibility_profiles_for_cleanup():
                    compat_assets = cached_assets_for_cleanup(
                        connection,
                        settings,
                        limit=50,
                        profile=compat_profile,
                    )
                    age_key, _size_key, _total_key = _cache_index_keys(
                        settings,
                        profile=compat_profile,
                    )
                    for asset_id in compat_assets:
                        try:
                            last_access = connection.zscore(age_key, asset_id)
                            if last_access is not None and time.time() - float(last_access) < 60:
                                continue
                            storage.delete(
                                object_key_for_asset(
                                    asset_id,
                                    profile=compat_profile,
                                )
                            )
                            forget_cached_asset(
                                connection,
                                settings,
                                asset_id,
                                profile=compat_profile,
                            )
                            compat_media_deleted += 1
                        except Exception:
                            compat_media_errors += 1
                    if compat_profile != COMPAT_PROFILE:
                        clear_empty_cache_index(
                            connection,
                            settings,
                            profile=compat_profile,
                        )
            finally:
                connection.close()
        except Exception:
            compat_media_errors += 1

    with session_scope() as db:
        stale_model_runs_failed = AgentRunRepository(db).fail_stale_running(
            at=now
        )
        stale_action_executions_review = (
            AgentActionExecutionRepository(
                db
            ).mark_stale_running_for_manual_review(at=now)
        )
        retention = RetentionService(db, settings)
        raw_deleted = retention.purge_expired_raw_responses(at=now)
        audit_deleted = retention.purge_expired_audit_logs(at=now)
        web_sessions_deleted, admin_sessions_deleted = retention.purge_stale_sessions(at=now)
        messages_deleted = retention.purge_expired_messages(at=now, limit=5000)
        canonical_messages_deleted = retention.purge_expired_canonical_messages(
            at=now,
            limit=5000,
        )
        match_history_deleted = _purge_expired_match_history(db, at=now, limit=5000)
        media_result = db.execute(
            delete(MediaObject).where(
                MediaObject.deleted_at.is_not(None),
                MediaObject.retention_expires_at <= now,
                MediaObject.message_id.is_(None),
            )
        )
        media_rows_purged = int(media_result.rowcount or 0)
        outbox_cutoff = now - timedelta(days=max(180, int(settings.message_retention_days)))
        result = db.execute(
            delete(OperationOutbox).where(
                OperationOutbox.status.in_(("completed", "failed", "cancelled")),
                OperationOutbox.updated_at <= outbox_cutoff,
                # media.archive 门禁只认 completed 为终态（文档 §4.5 不得
                # 直接取消）；failed/cancelled 的归档行必须保留，否则
                # retention 会让 migration_readiness 的 media_archive
                # 门禁在归档从未完成时静默转绿。
                or_(
                    OperationOutbox.status == "completed",
                    and_(
                        OperationOutbox.operation_type != MEDIA_OPERATION,
                        OperationOutbox.operation_type.not_like(
                            "compatibility.media.archive%"
                        ),
                    ),
                ),
            )
        )
        outboxes_deleted = int(result.rowcount or 0)
    native_media_cleanup = {
        "upload_intents_deleted": 0,
        "upload_intent_errors": 0,
        "assets_deleted": 0,
        "asset_errors": 0,
    }
    if storage is not None:
        try:
            native_media_cleanup = _cleanup_web_native_media(
                settings,
                storage,
                at=now,
            )
        except Exception:
            native_media_cleanup["asset_errors"] += 1
            LOGGER.warning("Web-native media cleanup batch failed", exc_info=True)
    return {
        "ok": True,
        "media_deleted": media_deleted,
        "media_errors": media_errors,
        "compat_media_deleted": compat_media_deleted,
        "compat_media_errors": compat_media_errors,
        "media_rows_purged": media_rows_purged,
        "raw_responses_deleted": raw_deleted,
        "audit_logs_deleted": audit_deleted,
        "web_sessions_deleted": web_sessions_deleted,
        "admin_sessions_deleted": admin_sessions_deleted,
        "messages_deleted": messages_deleted,
        "canonical_messages_deleted": canonical_messages_deleted,
        "match_history_deleted": match_history_deleted,
        "stale_model_runs_failed": stale_model_runs_failed,
        "stale_action_executions_review": stale_action_executions_review,
        "outboxes_deleted": outboxes_deleted,
        "native_upload_intents_deleted": native_media_cleanup[
            "upload_intents_deleted"
        ],
        "native_upload_intent_errors": native_media_cleanup[
            "upload_intent_errors"
        ],
        "native_assets_deleted": native_media_cleanup["assets_deleted"],
        "native_asset_errors": native_media_cleanup["asset_errors"],
        "r2_available": storage is not None,
    }
