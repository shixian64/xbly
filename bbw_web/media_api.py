"""Authenticated access to privately archived Cloudflare R2 media."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from bbw_prod.db import session_scope
from bbw_prod.models import utcnow
from bbw_prod.repositories import MediaObjectRepository
from bbw_web import bff_server as legacy


router = APIRouter(prefix="/api/media", tags=["media"])


def _sid(request: Request) -> str:
    return str(request.cookies.get(legacy.COOKIE_NAME) or "")


def _client_ip(request: Request) -> str:
    return str(
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )[:64]


@router.get("/{media_id}/access")
def media_access(media_id: uuid.UUID, request: Request, response: Response) -> dict[str, Any]:
    """Issue a short-lived URL only when the media belongs to the current user."""
    persistence = request.app.state.persistence
    sid = _sid(request)
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")

    if not persistence.rate_limit(
        f"media-access:{identity.user_id}", limit=60, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="媒体访问请求过于频繁")

    with session_scope() as db:
        media = MediaObjectRepository(db).get(identity.user_id, media_id)
        if (
            media is None
            or media.status != "available"
            or media.deleted_at is not None
            or media.retention_expires_at <= utcnow()
        ):
            raise HTTPException(status_code=404, detail="媒体不存在或已过期")
        object_key = media.r2_object_key
        bucket = media.r2_bucket
        content_type = media.content_type
        size_bytes = int(media.size_bytes)
        kind = media.kind

    try:
        storage = persistence.get_r2_storage()
        if bucket != storage.bucket:
            raise RuntimeError("archived media bucket does not match configured R2 bucket")
        ttl = max(30, min(300, int(persistence.settings.r2_presign_ttl_seconds)))
        url = storage.presigned_get(object_key, expires_seconds=ttl)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="媒体存储暂时不可用") from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"

    # Record the access without copying the signed URL or object key into logs.
    try:
        persistence.capture_product_response(
            sid=sid,
            identity=identity,
            method="GET",
            path="/api/media/access",
            query={"media_id": str(media_id)},
            request_data={},
            response_data={"ok": True, "kind": kind, "size_bytes": size_bytes},
            status=200,
            client_ip=_client_ip(request),
        )
    except Exception:
        # Access remains available; the signed URL/object key is never logged.
        pass
    return {
        "ok": True,
        "url": url,
        "expires_in": ttl,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "kind": kind,
    }
