"""Authenticated browser-to-server archive ingestion APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from bbw_prod.db import session_scope
from bbw_prod.models import ExternalAccount, User
from bbw_prod.repositories import ConversationRepository, MessageRepository
from bbw_web import bff_server as legacy


router = APIRouter(prefix="/api/archive", tags=["archive"])


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


def _sid(request: Request) -> str:
    return str(request.cookies.get(legacy.COOKIE_NAME) or "")


def _message_preview(message: Any) -> str:
    if message is None:
        return ""
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
        avatar = _public_profile_value(device_data, "portrait", "avatar") or _public_profile_value(
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


def _archived_message_item(message: Any) -> dict[str, Any]:
    metadata = dict(message.extra_data) if isinstance(message.extra_data, dict) else {}
    media = metadata.get("media_report")
    if not isinstance(media, dict):
        media = {}
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
    quote = metadata.get("quote") if isinstance(metadata.get("quote"), dict) else None
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
        "quote": quote,
        "text": str(message.body or ""),
        "body": str(message.body or ""),
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
        "revoked": revoked,
        "is_peer_read": metadata.get("is_peer_read"),
        "read_at": str(metadata.get("read_at") or ""),
        "flash_id": str(metadata.get("flash_id") or ""),
        "media": media,
    }


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
        "text",
        "body",
    ):
        merged[key] = primary.get(key) or secondary.get(key) or ""
    merged["revoked"] = bool(previous.get("revoked") or incoming.get("revoked"))
    if merged["revoked"]:
        merged["status"] = "revoked"
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

    with session_scope() as db:
        conversations = ConversationRepository(db).list_for_owner(
            identity.user_id, limit=min(max(1, int(limit)), 200)
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
                    "unread_authoritative": False,
                }
            )
    return {"ok": True, "items": items, "list": items, "count": len(items)}


@router.get("/messages")
def archived_messages(
    request: Request,
    peer: str,
    limit: int = 200,
    before: datetime | None = None,
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
    with session_scope() as db:
        conversation = ConversationRepository(db).get_by_peer(
            identity.user_id,
            target,
        )
        if conversation is None:
            return {"ok": True, "items": [], "list": [], "count": 0}
        rows = MessageRepository(db).list_for_conversation(
            identity.user_id,
            conversation.id,
            before=before,
            limit=bounded_limit,
        )
        items = _deduplicate_archived_message_items(
            [_archived_message_item(message) for message in reversed(rows)]
        )
    next_before = rows[-1].occurred_at.isoformat() if len(rows) == bounded_limit else ""
    return {
        "ok": True,
        "items": items,
        "list": items,
        "count": len(items),
        "has_more": bool(next_before),
        "next_before": next_before,
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
