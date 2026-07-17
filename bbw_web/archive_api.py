"""Authenticated browser-to-server archive ingestion APIs."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

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
