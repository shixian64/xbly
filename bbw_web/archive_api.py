"""Authenticated browser-to-server archive ingestion APIs."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_, select

from bbw_prod.db import session_scope
from bbw_prod.models import Conversation, ExternalAccount, MediaFlashClaim, Message, User
from bbw_prod.repositories import ConversationRepository, MessageRepository
from bbw_web import bff_server as legacy
from bbw_web.legacy_media_reference import (
    projected_message_media,
    projected_profile_avatar,
)


router = APIRouter(prefix="/api/archive", tags=["archive"])
MESSAGE_SEARCH_KINDS = ("all", "media", "file", "link")
MESSAGE_SEARCH_TIMEZONE = ZoneInfo("Asia/Shanghai")
ARCHIVE_MESSAGE_CURSOR_VERSION = 1


class MediaReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(default="", max_length=4096)
    thumbnail: str = Field(default="", max_length=4096)
    name: str = Field(default="", max_length=512)
    size: int = Field(default=0, ge=0, le=100 * 1024 * 1024)
    mime: str = Field(default="", max_length=160)
    duration: float = Field(default=0, ge=0, le=86400)
    width: int = Field(default=0, ge=0, le=65535)
    height: int = Field(default=0, ge=0, le=65535)

    @field_validator("url", "thumbnail")
    @classmethod
    def https_only(cls, value: str) -> str:
        value = str(value or "").strip()
        if value and not value.startswith("https://"):
            return ""
        return value


class QuoteReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: str = Field(default="", max_length=512)
    message_random: str = Field(default="", max_length=80)
    message_sequence: str = Field(default="", max_length=80)
    sender_uid: str = Field(default="", max_length=128)
    sender_name: str = Field(default="", max_length=120)
    text: str = Field(default="", max_length=500)
    kind: str = Field(default="text", max_length=40)
    sent_at: str = Field(default="", max_length=80)


class MessageReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    idempotency_key: str = Field(min_length=8, max_length=256)
    schema_version: int = Field(default=1, ge=1, le=10)
    source: str = Field(default="browser", min_length=1, max_length=64)
    direction: Literal["incoming", "outgoing", "unknown"] = "unknown"
    upstream_message_id: str = Field(default="", max_length=512)
    message_key: str = Field(
        default="",
        max_length=512,
        validation_alias=AliasChoices(
            "message_key", "upstream_message_key", "client_message_key"
        ),
    )
    client_message_key: str = Field(default="", max_length=512)
    message_random: str = Field(default="", max_length=80)
    peer_uid: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(default="", max_length=256)
    message_type: str = Field(default="text", max_length=80)
    object_name: str = Field(default="", max_length=128)
    text: str = Field(default="", max_length=100_000)
    sent_at: str | float = ""
    observed_at: str = Field(default="", max_length=80)
    delivery: str = Field(default="", max_length=80)
    revoked: bool = False
    media: Optional[MediaReport] = None
    quote: Optional[QuoteReport] = None
    flash_id: str = Field(default="", max_length=512)

    @field_validator("peer_uid", "upstream_message_id", "message_key", "conversation_id")
    @classmethod
    def no_control_chars(cls, value: str) -> str:
        value = str(value or "").strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("control characters are not allowed")
        return value


class MessageReportBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[MessageReport] = Field(min_length=1, max_length=20)


def _sid(request: Request) -> str:
    return str(request.cookies.get(legacy.COOKIE_NAME) or "")


def _message_preview(message: Any) -> str:
    if message is None:
        return ""
    metadata = (
        dict(message.extra_data)
        if isinstance(getattr(message, "extra_data", None), dict)
        else {}
    )
    if str(getattr(message, "status", "") or "").strip().lower() == "revoked" or str(
        metadata.get("revoked") or ""
    ).strip().lower() in {"1", "true"}:
        return "消息已撤回"
    body = str(message.body or "").strip()
    if body:
        return body[:500]
    return {
        "image": "图片",
        "audio": "语音",
        "video": "视频",
        "file": "文件",
        "location": "位置",
        "face": "表情",
        "custom": "消息",
    }.get(str(message.message_type or "").lower(), "消息")


def _metadata_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if abs(numeric) >= 10**12:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def _archive_cursor_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _encode_archive_message_cursor(message: Any) -> str:
    payload = {
        "v": ARCHIVE_MESSAGE_CURSOR_VERSION,
        "occurred_at": message.occurred_at.isoformat(),
        "created_at": getattr(message, "created_at", message.occurred_at).isoformat(),
        "id": str(message.id),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_archive_message_cursor(value: Any) -> tuple[datetime, datetime, uuid.UUID]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 1024:
        raise ValueError("invalid archive message cursor")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        payload = json.loads(decoded.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or int(payload.get("v") or 0) != ARCHIVE_MESSAGE_CURSOR_VERSION
        ):
            raise ValueError("unsupported archive message cursor")
        return (
            _archive_cursor_datetime(payload.get("occurred_at")),
            _archive_cursor_datetime(payload.get("created_at")),
            uuid.UUID(str(payload.get("id") or "")),
        )
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid archive message cursor") from error


def _archive_message_cursor_key(message: Any) -> tuple[datetime, datetime, str]:
    return (
        message.occurred_at,
        getattr(message, "created_at", message.occurred_at),
        str(message.id),
    )


def _public_profile_value(data: Any, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value and value.lower() not in {"none", "null", "undefined"}:
            return value
    return ""


def _conversation_name_is_placeholder(name: Any, peer: str) -> bool:
    value = str(name or "").strip()
    return not value or value in {"用户", "游客", peer, f"用户 {peer}"}


def _local_public_profile_map(db: Any, peers: list[str]) -> dict[str, dict[str, str]]:
    targets = list(dict.fromkeys(str(peer or "").strip() for peer in peers if str(peer or "").strip()))
    if not targets:
        return {}
    rows = db.execute(
        select(
            ExternalAccount.upstream_uid,
            User.display_name,
            User.profile,
            ExternalAccount.device_data,
        )
        .join(User, User.id == ExternalAccount.user_id)
        .where(
            ExternalAccount.provider == "beibeiwu",
            ExternalAccount.upstream_uid.in_(targets),
        )
    ).all()
    profiles: dict[str, dict[str, str]] = {}
    for upstream_uid, display_name, profile_data, device_data in rows:
        peer = str(upstream_uid or "").strip()
        if not peer:
            continue
        nickname = next(
            (
                value
                for value in (
                    str(display_name or "").strip(),
                    *(
                        _public_profile_value(profile_data, key)
                        for key in ("nickname", "nick", "name", "username")
                    ),
                )
                if not _conversation_name_is_placeholder(value, peer)
            ),
            "",
        )
        avatar = projected_profile_avatar(profile_data) or _public_profile_value(
            device_data, "portrait", "avatar"
        ) or _public_profile_value(
            profile_data,
            "portrait",
            "avatar",
            "head",
            "headimg",
            "head_img",
        )
        public_profile: dict[str, str] = {"id": peer}
        if nickname:
            public_profile["nickname"] = nickname
        if avatar:
            public_profile["avatar"] = avatar
            public_profile["portrait"] = avatar
        if len(public_profile) > 1:
            profiles[peer] = public_profile
    return profiles


def _conversation_search_fields(
    conversation: Any,
    local_profile: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata = (
        dict(conversation.extra_data)
        if isinstance(conversation.extra_data, dict)
        else {}
    )
    user = metadata.get("user") if isinstance(metadata.get("user"), dict) else {}
    profile = local_profile if isinstance(local_profile, dict) else {}
    peer = str(conversation.peer_upstream_uid or "").strip()
    nickname = next(
        (
            value
            for value in (
                str(profile.get("nickname") or profile.get("name") or "").strip(),
                str(conversation.title or "").strip(),
                str(user.get("nickname") or user.get("name") or "").strip(),
            )
            if not _conversation_name_is_placeholder(value, peer)
        ),
        f"用户 {peer}" if peer else "聊天",
    )
    avatar = str(
        profile.get("avatar")
        or profile.get("portrait")
        or metadata.get("avatar")
        or user.get("avatar")
        or user.get("portrait")
        or ""
    ).strip()
    return {
        "peer_id": peer,
        "conversation_user": peer,
        "conversation_name": nickname,
        "nickname": nickname,
        "conversation_avatar": avatar,
        "avatar": avatar,
    }


def _message_search_day_window(value: str) -> tuple[datetime | None, datetime | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    try:
        selected = date.fromisoformat(raw)
        local_start = datetime.combine(
            selected,
            time.min,
            tzinfo=MESSAGE_SEARCH_TIMEZONE,
        )
        local_end = local_start + timedelta(days=1)
        return local_start.astimezone(UTC), local_end.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise HTTPException(status_code=400, detail="搜索日期格式不正确") from error


def _archived_message_item(message: Any) -> dict[str, Any]:
    metadata = dict(message.extra_data) if isinstance(message.extra_data, dict) else {}
    media = projected_message_media(metadata)
    if (
        str(message.message_type or "").strip().lower() == "flash"
        and str(message.direction or "").strip().lower() == "incoming"
    ):
        # Keep the archived media metadata server-side, but never replay a
        # received flash photo's source addresses to the recipient.
        media.pop("url", None)
        media.pop("thumbnail", None)
    message_id = str(message.upstream_message_id or message.id)
    message_key = str(
        metadata.get("message_key")
        or metadata.get("client_message_key")
        or ""
    )
    revoked = bool(
        metadata.get("revoked") or str(getattr(message, "status", "") or "") == "revoked"
    )
    message_sequence = str(metadata.get("message_sequence") or "")
    if not message_sequence and revoked and message_id.isdigit() and len(message_id) <= 20:
        message_sequence = message_id
    message_random = str(metadata.get("message_random") or "")
    canonical_message_id = str(metadata.get("canonical_message_id") or "")
    client_message_id = str(
        metadata.get("client_message_id")
        or metadata.get("client_message_key")
        or ""
    )
    quote = metadata.get("quote") if isinstance(metadata.get("quote"), dict) else None
    canonical_revoked = bool(
        revoked
        and str(getattr(message, "provider", "") or "").strip().lower()
        == "web-local"
    )
    recalled_text = ""
    if canonical_revoked and str(message.direction or "").strip().lower() == "outgoing":
        recalled_text = str(metadata.get("recalled_text") or "")
    body = "" if canonical_revoked else str(message.body or "")
    return {
        "id": message_id,
        "message_id": message_id,
        "message_key": message_key,
        "msg_key": message_key,
        "sequence": message_sequence,
        "msg_sequence": message_sequence,
        "message_random": message_random,
        "msg_random": message_random,
        "MsgRandom": message_random,
        "canonical_message_id": canonical_message_id,
        "client_message_id": client_message_id,
        "quote": quote,
        "text": body,
        "body": body,
        "recalled_text": recalled_text,
        "kind": str(message.message_type or "text"),
        "message_type": str(message.message_type or "text"),
        "object_name": str(metadata.get("object_name") or ""),
        "from": str(message.sender_upstream_uid or ""),
        "to": str(message.recipient_upstream_uid or ""),
        "flow": "out" if str(message.direction or "") == "outgoing" else "in",
        "status": str(message.status or ""),
        "time": message.occurred_at.isoformat(),
        "timestamp": message.occurred_at.isoformat(),
        "source": "archive",
        "provider": str(getattr(message, "provider", "") or ""),
        "revoked": revoked,
        "is_peer_read": metadata.get("is_peer_read"),
        "read_at": str(metadata.get("read_at") or ""),
        "flash_id": str(metadata.get("flash_id") or ""),
        "media": media,
    }


def _annotate_native_flash_claims(
    db: Any,
    claimant_user_id: uuid.UUID,
    items: list[dict[str, Any]],
) -> None:
    """Mark already-consumed native flash messages without exposing their URL."""

    flash_ids: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for item in items:
        if (
            str(item.get("message_type") or item.get("kind") or "").lower()
            != "flash"
            or str(item.get("flow") or "").lower() != "in"
            or str(item.get("provider") or "").lower() != "web-local"
        ):
            continue
        try:
            attachment_id = uuid.UUID(str(item.get("flash_id") or ""))
        except (TypeError, ValueError):
            continue
        item["native_flash_claimed"] = False
        flash_ids.setdefault(attachment_id, []).append(item)
    if not flash_ids:
        return
    claimed_ids = set(
        db.scalars(
            select(MediaFlashClaim.attachment_id).where(
                MediaFlashClaim.claimant_user_id == claimant_user_id,
                MediaFlashClaim.attachment_id.in_(tuple(flash_ids)),
            )
        )
    )
    for attachment_id in claimed_ids:
        for item in flash_ids.get(attachment_id, ()):
            item["native_flash_claimed"] = True


def _archived_items_refer_to_same_message(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    if left.get("flow") and right.get("flow") and left["flow"] != right["flow"]:
        return False
    if left.get("from") and right.get("from") and left["from"] != right["from"]:
        return False
    if left.get("to") and right.get("to") and left["to"] != right["to"]:
        return False
    pairs = (
        (
            left.get("canonical_message_id"),
            right.get("canonical_message_id"),
        ),
        (left.get("message_random"), right.get("message_random")),
        (left.get("message_key"), right.get("message_key")),
        (left.get("id"), right.get("id")),
        (left.get("sequence"), right.get("sequence")),
        (left.get("id"), right.get("sequence")),
        (left.get("sequence"), right.get("id")),
    )
    return any(
        str(first or "").strip() and str(first or "").strip() == str(second or "").strip()
        for first, second in pairs
    )


def _merge_archived_message_items(
    previous: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    def quality(item: dict[str, Any]) -> tuple[int, int, int, int]:
        return (
            1 if str(item.get("message_random") or "").strip() else 0,
            1 if str(item.get("text") or "").strip() else 0,
            1 if str(item.get("message_key") or "").strip() else 0,
            1 if not str(item.get("id") or "").isdigit() else 0,
        )

    primary, secondary = (
        (incoming, previous) if quality(incoming) > quality(previous) else (previous, incoming)
    )
    merged = {**secondary, **primary}
    for key in (
        "message_key",
        "msg_key",
        "sequence",
        "msg_sequence",
        "message_random",
        "msg_random",
        "MsgRandom",
        "canonical_message_id",
        "client_message_id",
        "text",
        "body",
        "recalled_text",
    ):
        merged[key] = primary.get(key) or secondary.get(key) or ""
    cursor_candidates = [
        item
        for item in (previous, incoming)
        if isinstance(item.get("_archive_cursor_key"), tuple)
        and str(item.get("_archive_cursor") or "")
    ]
    if cursor_candidates:
        oldest_cursor_item = min(
            cursor_candidates,
            key=lambda item: item["_archive_cursor_key"],
        )
        merged["_archive_cursor_key"] = oldest_cursor_item["_archive_cursor_key"]
        merged["_archive_cursor"] = oldest_cursor_item["_archive_cursor"]
    read_sources = [
        item for item in (incoming, previous) if item.get("is_peer_read") is True
    ]
    if read_sources:
        # Read receipts are monotonic.  A later TIM compatibility projection
        # can be richer in message identity while still carrying an older
        # ``false`` snapshot; it must never roll a canonical Web receipt back.
        merged["is_peer_read"] = True
        merged["read_at"] = next(
            (
                str(item.get("read_at") or "")
                for item in read_sources
                if str(item.get("read_at") or "").strip()
            ),
            "",
        )
    elif any(item.get("is_peer_read") is False for item in (previous, incoming)):
        merged["is_peer_read"] = False
        merged["read_at"] = ""
    else:
        merged["is_peer_read"] = None
        merged["read_at"] = ""
    canonical_sources = [
        item
        for item in (previous, incoming)
        if str(item.get("provider") or "").strip().lower() == "web-local"
    ]
    revoke_sources = canonical_sources or [previous, incoming]
    merged["revoked"] = any(
        bool(item.get("revoked"))
        or str(item.get("status") or "").strip().lower() == "revoked"
        for item in revoke_sources
    )
    if merged["revoked"]:
        merged["status"] = "revoked"
        canonical_revoked_sources = [
            item
            for item in canonical_sources
            if bool(item.get("revoked"))
            or str(item.get("status") or "").strip().lower() == "revoked"
        ]
        if canonical_revoked_sources:
            # The owner-scoped canonical projection is authoritative for
            # privacy after revoke.  A stale TIM mirror may still contain the
            # original text, but archive merging must never replay it.
            merged["text"] = ""
            merged["body"] = ""
            merged["quote"] = None
            merged["recalled_text"] = next(
                (
                    str(item.get("recalled_text") or "")
                    for item in canonical_revoked_sources
                    if str(item.get("flow") or "").strip().lower() == "out"
                ),
                "",
            )
    elif canonical_sources:
        # The canonical Web projection owns message state.  A stale TIM mirror
        # may report the same message as revoked, but it cannot revoke an
        # otherwise active canonical message in the merged archive response.
        merged["status"] = next(
            (
                str(item.get("status") or "")
                for item in canonical_sources
                if str(item.get("status") or "").strip().lower() != "revoked"
            ),
            "",
        )
    return merged


def _deduplicate_archived_message_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        index = next(
            (
                position
                for position, previous in enumerate(merged)
                if _archived_items_refer_to_same_message(previous, item)
            ),
            -1,
        )
        if index < 0:
            merged.append(item)
        else:
            merged[index] = _merge_archived_message_items(merged[index], item)
    return merged


@router.get("/conversations")
def archived_conversations(request: Request, limit: int = 100) -> dict[str, Any]:
    persistence = request.app.state.persistence
    identity = persistence.require_identity(_sid(request))
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not persistence.rate_limit(
        f"archive-conversations:{identity.user_id}", limit=120, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="会话缓存读取过于频繁")

    bounded_limit = min(max(1, int(limit)), 200)
    with session_scope() as db:
        conversations = ConversationRepository(db).list_for_owner(
            identity.user_id, limit=min(500, bounded_limit * 3)
        )
        latest = MessageRepository(db).latest_for_conversations(
            identity.user_id, [conversation.id for conversation in conversations]
        )
        local_profiles = _local_public_profile_map(
            db,
            [str(conversation.peer_upstream_uid or "") for conversation in conversations],
        )
        items: list[dict[str, Any]] = []
        for conversation in conversations:
            conversation_provider = str(
                getattr(conversation, "provider", "") or ""
            )
            metadata = (
                dict(conversation.extra_data)
                if isinstance(conversation.extra_data, dict)
                else {}
            )
            user = metadata.get("user") if isinstance(metadata.get("user"), dict) else {}
            message = latest.get(conversation.id)
            activity_at = conversation.last_message_at or (
                message.occurred_at if message is not None else None
            )
            preview = _message_preview(message) or str(metadata.get("last_message") or "")[:500]
            preview_at = (
                message.occurred_at
                if message is not None
                else _metadata_time(metadata.get("preview_timestamp"))
            )
            message_metadata = (
                dict(message.extra_data or {})
                if message is not None and isinstance(message.extra_data, dict)
                else {}
            )
            local_preview_revoked = bool(
                conversation_provider == "web-local"
                and message is not None
                and (
                    str(message.status or "").strip().lower() == "revoked"
                    or str(message_metadata.get("revoked") or "").strip().lower()
                    in {"1", "true"}
                )
            )
            avatar = str(
                local_profiles.get(str(conversation.peer_upstream_uid or "").strip(), {}).get("avatar")
                or metadata.get("avatar")
                or user.get("avatar")
                or user.get("portrait")
                or ""
            )
            peer = str(conversation.peer_upstream_uid or "").strip()
            if not peer:
                continue
            local_profile = local_profiles.get(peer, {})
            local_profile_name = str(
                local_profile.get("nickname") or local_profile.get("name") or ""
            ).strip()
            local_profile_avatar = str(
                local_profile.get("avatar") or local_profile.get("portrait") or ""
            ).strip()
            local_profile_name_resolved = not _conversation_name_is_placeholder(
                local_profile_name,
                peer,
            )
            nickname = next(
                (
                    value
                    for value in (
                        str(local_profile.get("nickname") or "").strip(),
                        str(conversation.title or "").strip(),
                        str(user.get("nickname") or user.get("name") or "").strip(),
                    )
                    if not _conversation_name_is_placeholder(value, peer)
                ),
                peer,
            )
            public_user = {**user, **local_profile, "nickname": nickname}
            if avatar:
                public_user["avatar"] = avatar
            items.append(
                {
                    "id": str(conversation.id),
                    "conversation_id": str(conversation.upstream_conversation_id or f"C2C{peer}"),
                    "conversation_type": "C2C",
                    "source": "archive",
                    "provider": conversation_provider,
                    "peer_id": peer,
                    "conversation_user": peer,
                    "nickname": nickname,
                    "avatar": avatar,
                    "user": public_user,
                    "profile_resolved": bool(
                        local_profile_name_resolved and local_profile_avatar
                    ),
                    "last_message": preview,
                    "content": preview,
                    "timestamp": activity_at.isoformat() if activity_at is not None else "",
                    "preview_timestamp": preview_at.isoformat() if preview_at is not None else "",
                    "preview_sequence": str(
                        message_metadata.get("message_sequence")
                        or metadata.get("preview_sequence")
                        or ""
                    ),
                    "preview_source": "archive",
                    "preview_authoritative": message is not None
                    or metadata.get("preview_authoritative") is True,
                    "preview_timestamp_inferred": metadata.get(
                        "preview_timestamp_inferred"
                    ) is True,
                    "unread_count": max(0, int(conversation.unread_count or 0)),
                    "unread_observed_at": (
                        conversation.unread_observed_at.isoformat()
                        if conversation.unread_observed_at is not None
                        else ""
                    ),
                    "unread_authoritative": conversation_provider == "web-local",
                    "_activity_sort": (
                        activity_at.timestamp() if activity_at is not None else 0.0
                    ),
                    "_local_authority": conversation_provider == "web-local",
                    "_local_preview_revoked": local_preview_revoked,
                }
            )
        merged_by_peer: dict[str, dict[str, Any]] = {}
        for candidate in items:
            peer = str(candidate.get("peer_id") or "")
            previous = merged_by_peer.get(peer)
            if previous is None:
                merged_by_peer[peer] = candidate
                continue
            newest, older = (
                (candidate, previous)
                if float(candidate.get("_activity_sort") or 0)
                >= float(previous.get("_activity_sort") or 0)
                else (previous, candidate)
            )
            local_authority = (
                candidate
                if candidate.get("_local_authority") is True
                else previous
                if previous.get("_local_authority") is True
                else None
            )
            merged = {**older, **newest}
            if local_authority is not None:
                for key in (
                    "unread_count",
                    "unread_observed_at",
                    "unread_authoritative",
                ):
                    merged[key] = local_authority.get(key)
                # ``provider`` identifies the authority used by the browser
                # for unread reconciliation.  A newer TIM compatibility row
                # may still provide the preview, but it must not hide the
                # canonical local unread counter.
                merged["provider"] = "web-local"
                merged["unread_authoritative"] = True
                merged["_local_authority"] = True
                if (
                    local_authority.get("_local_preview_revoked") is True
                    and float(local_authority.get("_activity_sort") or 0)
                    >= float(merged.get("_activity_sort") or 0)
                ):
                    for key in (
                        "last_message",
                        "content",
                        "preview_timestamp",
                        "preview_sequence",
                        "preview_source",
                        "preview_authoritative",
                        "preview_timestamp_inferred",
                    ):
                        merged[key] = local_authority.get(key)
                    merged["_local_preview_revoked"] = True
            merged_by_peer[peer] = merged
        items = sorted(
            merged_by_peer.values(),
            key=lambda item: float(item.get("_activity_sort") or 0),
            reverse=True,
        )[:bounded_limit]
        for item in items:
            item.pop("_activity_sort", None)
            item.pop("_local_authority", None)
            item.pop("_local_preview_revoked", None)
    return {"ok": True, "items": items, "list": items, "count": len(items)}


@router.get("/messages")
def archived_messages(
    request: Request,
    peer: str,
    limit: int = 200,
    before: datetime | None = None,
    around: datetime | None = None,
    cursor: str = "",
) -> dict[str, Any]:
    persistence = request.app.state.persistence
    identity = persistence.require_identity(_sid(request))
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    target = str(peer or "").strip()
    if (
        not target
        or len(target) > 128
        or any(ord(char) < 33 for char in target)
        or target == str(identity.upstream_uid or "").strip()
    ):
        raise HTTPException(status_code=400, detail="聊天对象 UID 不合法")
    if not persistence.rate_limit(
        f"archive-messages:{identity.user_id}", limit=120, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="聊天缓存读取过于频繁")

    bounded_limit = min(max(1, int(limit)), 200)
    cursor_value: tuple[datetime, datetime, uuid.UUID] | None = None
    if cursor and around is not None:
        raise HTTPException(status_code=400, detail="定位消息时不能同时使用分页游标")
    if cursor:
        try:
            cursor_value = _decode_archive_message_cursor(cursor)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="聊天记录分页游标不合法") from error
    with session_scope() as db:
        if not callable(getattr(db, "scalars", None)):
            conversation = ConversationRepository(db).get_by_peer(
                identity.user_id,
                target,
            )
            if conversation is None:
                return {
                    "ok": True,
                    "items": [],
                    "list": [],
                    "count": 0,
                    "has_more": False,
                    "next_before": "",
                    "next_cursor": "",
                    "context": around is not None,
                }
            repository = MessageRepository(db)
            if around is not None and callable(
                getattr(repository, "list_around_conversation", None)
            ):
                rows = repository.list_around_conversation(
                    identity.user_id,
                    conversation.id,
                    around=around,
                    limit=bounded_limit,
                )
            else:
                rows = repository.list_for_conversation(
                    identity.user_id,
                    conversation.id,
                    before=before,
                    limit=bounded_limit,
                )
            ordered_rows = sorted(
                rows,
                key=lambda message: (
                    message.occurred_at,
                    getattr(message, "created_at", message.occurred_at),
                ),
            )
            items = _deduplicate_archived_message_items(
                [_archived_message_item(message) for message in ordered_rows]
            )[-bounded_limit:]
            return {
                "ok": True,
                "items": items,
                "list": items,
                "count": len(items),
                "has_more": len(rows) > bounded_limit,
                "next_before": "",
                "next_cursor": "",
                "context": around is not None,
            }
        conversation_ids = list(
            db.scalars(
                select(Conversation.id).where(
                    Conversation.owner_user_id == identity.user_id,
                    Conversation.peer_upstream_uid == target,
                    Conversation.kind == "direct",
                )
            )
        )
        if not conversation_ids:
            return {
                "ok": True,
                "items": [],
                "list": [],
                "count": 0,
                "has_more": False,
                "next_before": "",
                "next_cursor": "",
                "context": around is not None,
            }
        base_conditions = (
            Message.owner_user_id == identity.user_id,
            Message.conversation_id.in_(conversation_ids),
        )
        fetch_limit = min(600, bounded_limit * 3)
        if around is not None:
            before_limit = fetch_limit // 2 + fetch_limit % 2
            after_limit = fetch_limit // 2
            before_rows = list(
                db.scalars(
                    select(Message)
                    .where(*base_conditions, Message.occurred_at <= around)
                    .order_by(Message.occurred_at.desc(), Message.created_at.desc())
                    .limit(before_limit)
                )
            )
            after_rows = (
                list(
                    db.scalars(
                        select(Message)
                        .where(*base_conditions, Message.occurred_at > around)
                        .order_by(
                            Message.occurred_at.asc(), Message.created_at.asc()
                        )
                        .limit(after_limit)
                    )
                )
                if after_limit
                else []
            )
            ordered_rows = sorted(
                [*before_rows, *after_rows],
                key=lambda message: (message.occurred_at, message.created_at),
            )
            cursor_rows = before_rows
        else:
            statement = select(Message).where(*base_conditions)
            if cursor_value is not None:
                cursor_occurred_at, cursor_created_at, cursor_id = cursor_value
                statement = statement.where(
                    or_(
                        Message.occurred_at < cursor_occurred_at,
                        and_(
                            Message.occurred_at == cursor_occurred_at,
                            Message.created_at < cursor_created_at,
                        ),
                        and_(
                            Message.occurred_at == cursor_occurred_at,
                            Message.created_at == cursor_created_at,
                            Message.id < cursor_id,
                        ),
                    )
                )
            elif before is not None:
                statement = statement.where(Message.occurred_at < before)
            cursor_rows = list(
                db.scalars(
                    statement.order_by(
                        Message.occurred_at.desc(),
                        Message.created_at.desc(),
                        Message.id.desc(),
                    ).limit(fetch_limit)
                )
            )
            ordered_rows = list(reversed(cursor_rows))
        raw_items: list[dict[str, Any]] = []
        for message in ordered_rows:
            item = _archived_message_item(message)
            item["_archive_cursor"] = _encode_archive_message_cursor(message)
            item["_archive_cursor_key"] = _archive_message_cursor_key(message)
            raw_items.append(item)
        items = _deduplicate_archived_message_items(raw_items)
        page_was_trimmed = len(items) > bounded_limit
        if page_was_trimmed:
            items = items[-bounded_limit:]
        _annotate_native_flash_claims(db, identity.user_id, items)
    # ``cursor_rows`` intentionally over-fetches because one canonical message
    # can have both ``web-local`` and TIM compatibility projections.  When the
    # deduplicated page is trimmed, advancing to the oldest *raw* row would
    # skip the deduplicated messages that were fetched but not returned.  The
    # next page must therefore continue before the oldest item the caller
    # actually received.
    has_more = (
        around is None
        and bool(items)
        and (page_was_trimmed or len(cursor_rows) == fetch_limit)
    )
    next_before = str(items[0].get("timestamp") or "") if has_more else ""
    next_cursor = str(items[0].get("_archive_cursor") or "") if has_more else ""
    for item in items:
        item.pop("_archive_cursor", None)
        item.pop("_archive_cursor_key", None)
    return {
        "ok": True,
        "items": items,
        "list": items,
        "count": len(items),
        "has_more": has_more,
        "next_before": next_before,
        "next_cursor": next_cursor,
        "context": around is not None,
    }


@router.get("/search")
def archived_message_search(
    request: Request,
    q: str = Query(default="", max_length=120),
    peer: str = Query(default="", max_length=128),
    kind: Literal["all", "media", "file", "link"] = "all",
    search_date: str = Query(default="", alias="date", max_length=10),
    limit: int = Query(default=200, ge=1, le=200),
) -> dict[str, Any]:
    persistence = request.app.state.persistence
    identity = persistence.require_identity(_sid(request))
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not persistence.rate_limit(
        f"archive-message-search:{identity.user_id}", limit=120, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="聊天记录搜索过于频繁")

    normalized_query = str(q or "").strip()
    target = str(peer or "").strip()
    if target and (
        len(target) > 128
        or any(ord(char) < 33 for char in target)
        or target == str(identity.upstream_uid or "").strip()
    ):
        raise HTTPException(status_code=400, detail="聊天对象 UID 不合法")
    if kind not in MESSAGE_SEARCH_KINDS:
        raise HTTPException(status_code=400, detail="消息类型筛选不合法")
    occurred_from, occurred_to = _message_search_day_window(search_date)
    if not normalized_query and kind == "all" and occurred_from is None:
        return {
            "ok": True,
            "items": [],
            "list": [],
            "groups": [],
            "count": 0,
            "total": 0,
            "has_more": False,
        }

    with session_scope() as db:
        conversation_repository = ConversationRepository(db)
        selected_conversation_ids: list[Any] | None = None
        if target:
            selected_conversation_ids = list(
                db.scalars(
                    select(Conversation.id).where(
                        Conversation.owner_user_id == identity.user_id,
                        Conversation.peer_upstream_uid == target,
                        Conversation.kind == "direct",
                    )
                )
            )
            if not selected_conversation_ids:
                return {
                    "ok": True,
                    "items": [],
                    "list": [],
                    "groups": [],
                    "count": 0,
                    "total": 0,
                    "has_more": False,
                }
        search_limit = min(200, max(limit, limit * 3))
        rows, _conversation_counts, has_more = MessageRepository(db).search_for_owner(
            identity.user_id,
            conversation_ids=selected_conversation_ids,
            query=normalized_query,
            kind=kind,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=search_limit,
        )
        conversation_ids = list(dict.fromkeys(row.conversation_id for row in rows))
        conversations = conversation_repository.list_by_ids(
            identity.user_id,
            conversation_ids,
        )
        conversation_map = {item.id: item for item in conversations}
        local_profiles = _local_public_profile_map(
            db,
            [str(item.peer_upstream_uid or "") for item in conversations],
        )

        candidates: list[dict[str, Any]] = []
        for message in rows:
            current_conversation = conversation_map.get(message.conversation_id)
            if current_conversation is None:
                continue
            peer_uid = str(current_conversation.peer_upstream_uid or "").strip()
            if not peer_uid:
                continue
            conversation_fields = _conversation_search_fields(
                current_conversation,
                local_profiles.get(peer_uid),
            )
            candidates.append(
                {
                    **_archived_message_item(message),
                    **conversation_fields,
                    "conversation_id": str(
                        current_conversation.upstream_conversation_id
                        or f"C2C{peer_uid}"
                    ),
                }
            )

        deduplicated = _deduplicate_archived_message_items(candidates)
        _annotate_native_flash_claims(db, identity.user_id, deduplicated)
        if len(deduplicated) > limit:
            has_more = True
        items = deduplicated[:limit]
        peer_counts: dict[str, int] = {}
        for item in items:
            peer_uid = str(item.get("peer_id") or "").strip()
            if peer_uid:
                peer_counts[peer_uid] = peer_counts.get(peer_uid, 0) + 1
        groups: dict[str, dict[str, Any]] = {}
        for item in items:
            peer_uid = str(item.get("peer_id") or "").strip()
            if not peer_uid:
                continue
            item["match_count"] = peer_counts[peer_uid]
            if peer_uid not in groups:
                groups[peer_uid] = {
                    "peer_id": peer_uid,
                    "conversation_user": item.get("conversation_user") or peer_uid,
                    "conversation_name": item.get("conversation_name") or peer_uid,
                    "nickname": item.get("nickname") or peer_uid,
                    "conversation_avatar": item.get("conversation_avatar") or "",
                    "avatar": item.get("avatar") or "",
                    "conversation_id": item["conversation_id"],
                    "count": peer_counts[peer_uid],
                    "latest_at": item["timestamp"],
                    "preview": str(item.get("text") or item.get("body") or "")[:120],
                }

    group_items = list(groups.values())
    return {
        "ok": True,
        "items": items,
        "list": items,
        "groups": group_items,
        "count": len(items),
        "total": len(items),
        "has_more": has_more,
    }


@router.post("/messages", status_code=status.HTTP_202_ACCEPTED)
def archive_message(report: MessageReport, request: Request) -> dict[str, Any]:
    persistence = request.app.state.persistence
    sid = _sid(request)
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")

    client_ip = str(
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )[:64]
    if not persistence.rate_limit(
        f"archive-message:{identity.user_id}", limit=60, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="消息归档请求过于频繁")

    payload = report.model_dump(mode="json")
    accepted = persistence.enqueue_message_archive(
        identity=identity,
        payload=payload,
        client_ip=client_ip,
    )
    return {"ok": True, "accepted": bool(accepted)}


@router.post("/messages/batch", status_code=status.HTTP_202_ACCEPTED)
def archive_message_batch(
    batch: MessageReportBatch, request: Request
) -> dict[str, Any]:
    persistence = request.app.state.persistence
    sid = _sid(request)
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")

    client_ip = str(
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )[:64]
    if not persistence.rate_limit(
        f"archive-message:{identity.user_id}", limit=60, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="消息归档请求过于频繁")

    payloads = [item.model_dump(mode="json") for item in batch.items]
    accepted = persistence.enqueue_message_archive_batch(
        identity=identity,
        payloads=payloads,
        client_ip=client_ip,
    )
    return {
        "ok": True,
        "accepted": bool(accepted),
        "count": len(payloads),
    }
