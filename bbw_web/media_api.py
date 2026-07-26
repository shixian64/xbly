"""Authenticated access to archived and Web-native private R2 media."""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from bbw_prod.db import session_scope
from bbw_prod.models import MediaObject, Message, SocialPost, User, utcnow
from bbw_web import bff_server as legacy
from bbw_web.legacy_media_reference import matching_local_media_reference
from bbw_web.media_native.references import (
    BoundMediaAssetReference,
    PROFILE_RESOURCE,
    SOCIAL_POST_RESOURCE,
    SqlAlchemyMediaAssetReferenceRepository,
)
from bbw_web.moments_native import SocialAuthorRef, SocialPrincipal
from bbw_web.moments_native.policy import SqlAlchemySocialPermissionPolicy


router = APIRouter(prefix="/api/media", tags=["media"])


def _sid(request: Request) -> str:
    return str(request.cookies.get(legacy.COOKIE_NAME) or "")


def _client_ip(request: Request) -> str:
    return str(
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )[:64]


def _legacy_media_metadata(media: MediaObject) -> Mapping[str, Any] | None:
    metadata = media.extra_data if isinstance(media.extra_data, Mapping) else {}
    legacy_media = metadata.get("legacy_media")
    if not isinstance(legacy_media, Mapping) or legacy_media.get("schema") != 1:
        return None
    return legacy_media


def _resource_id(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _reference_matches(
    container: Any,
    *,
    media_id: uuid.UUID,
    slot: str,
    source_value: Any,
    source_hash: str,
) -> bool:
    reference = matching_local_media_reference(
        container,
        slot=slot,
        source_value=source_value,
    )
    return bool(
        reference is not None
        and reference.media_id == media_id
        and reference.source_hash == source_hash
    )


def _social_post_source(media: Mapping[str, Any], slot: str) -> Any:
    if slot in {"video", "cover"}:
        return media.get(slot)
    if not slot.startswith("pictures[") or not slot.endswith("]"):
        return None
    raw_index = slot[len("pictures[") : -1]
    if not raw_index.isascii() or not raw_index.isdigit():
        return None
    pictures = media.get("pictures") or media.get("images") or []
    if not isinstance(pictures, list):
        pictures = [pictures]
    index = int(raw_index)
    return pictures[index] if 0 <= index < len(pictures) else None


def _message_source(message_data: Mapping[str, Any], slot: str) -> Any:
    report = message_data.get("media_report")
    if not isinstance(report, Mapping) or not slot.startswith("media_report."):
        return None
    field = slot.split(".", 1)[1]
    return report.get(field) if field in {"url", "thumbnail"} else None


def _message_revoked(message: Message) -> bool:
    metadata = (
        message.extra_data if isinstance(message.extra_data, Mapping) else {}
    )
    revoked = metadata.get("revoked")
    return bool(
        str(message.status or "").strip().lower() == "revoked"
        or revoked is True
        or str(revoked or "").strip().lower() in {"1", "true"}
    )


def _current_legacy_media_allowed(
    db: Any,
    *,
    media: MediaObject,
    identity: Any,
) -> bool:
    legacy_media = _legacy_media_metadata(media)
    if legacy_media is None:
        return False
    resource_type = str(legacy_media.get("resource_type") or "")
    access_scope = str(legacy_media.get("access_scope") or "")
    slot = str(legacy_media.get("slot") or "")
    source_hash = str(legacy_media.get("source_hash") or "").lower()
    resource_id = _resource_id(legacy_media.get("resource_id"))
    if (
        resource_id is None
        or not slot
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        return False

    if resource_type == "profile" and access_scope == "profile":
        user = db.scalar(
            select(User).where(
                User.id == resource_id,
                User.id == media.owner_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
        )
        profile = dict(user.profile or {}) if user is not None else {}
        source = profile.get("avatar") or profile.get("portrait")
        return _reference_matches(
            profile,
            media_id=media.id,
            slot=slot,
            source_value=source,
            source_hash=source_hash,
        )

    if resource_type == "social_post" and access_scope == "social_post":
        post = db.scalar(
            select(SocialPost).where(
                SocialPost.id == resource_id,
                SocialPost.author_user_id == media.owner_user_id,
                SocialPost.status == "published",
            )
        )
        if post is None:
            return False
        post_media = dict(post.media or {})
        if not _reference_matches(
            post_media,
            media_id=media.id,
            slot=slot,
            source_value=_social_post_source(post_media, slot),
            source_hash=source_hash,
        ):
            return False
        try:
            viewer = SocialPrincipal(
                user_id=(
                    identity.user_id
                    if isinstance(identity.user_id, uuid.UUID)
                    else uuid.UUID(str(identity.user_id))
                ),
                upstream_uid=str(identity.upstream_uid or "").strip(),
            )
        except (AttributeError, TypeError, ValueError):
            return False
        return SqlAlchemySocialPermissionPolicy(db).can_view(
            viewer=viewer,
            author=SocialAuthorRef(
                user_id=post.author_user_id,
                upstream_uid=str(post.author_upstream_uid or ""),
            ),
            visibility=str(post.visibility or "private"),
        )

    if resource_type == "message" and access_scope == "message":
        # Historical private-message media is copied into each account's
        # archive.  It remains owner-only; a peer must use their own canonical
        # archive row.  The sidecar must still be the current canonical
        # reference so a replaced/revoked attachment cannot be reopened by a
        # previously retained URL.
        if identity.user_id != media.owner_user_id or media.message_id != resource_id:
            return False
        message = db.scalar(
            select(Message).where(
                Message.id == resource_id,
                Message.owner_user_id == media.owner_user_id,
            )
        )
        if message is None or _message_revoked(message):
            return False
        message_data = (
            dict(message.extra_data or {})
            if isinstance(message.extra_data, Mapping)
            else {}
        )
        return _reference_matches(
            message_data,
            media_id=media.id,
            slot=slot,
            source_value=_message_source(message_data, slot),
            source_hash=source_hash,
        )
    return False


def _load_media(
    db: Any,
    *,
    media_id: uuid.UUID,
    identity: Any,
    allow_shared: bool,
) -> MediaObject:
    media = db.scalar(select(MediaObject).where(MediaObject.id == media_id))
    if (
        media is None
        or media.status != "available"
        or media.deleted_at is not None
        or media.retention_expires_at <= utcnow()
    ):
        raise HTTPException(status_code=404, detail="媒体不存在或已过期")
    legacy_binding = _legacy_media_metadata(media)
    legacy_reference_checked = allow_shared and legacy_binding is not None
    if legacy_reference_checked:
        # ``/content`` is a projection of a current canonical sidecar, not a
        # permanent bearer URL.  Apply this check to the owner as well as to a
        # shared viewer so avatar replacement, post deletion and message
        # revocation invalidate old paths uniformly.
        if not _current_legacy_media_allowed(
            db, media=media, identity=identity
        ):
            raise HTTPException(status_code=404, detail="媒体不存在或已过期")
    elif media.owner_user_id != identity.user_id:
        raise HTTPException(status_code=404, detail="媒体不存在或已过期")
    if media.message_id is not None and not (
        legacy_reference_checked
        and str(legacy_binding.get("resource_type") or "") == "message"
    ):
        message = db.scalar(
            select(Message).where(
                Message.id == media.message_id,
                Message.owner_user_id == media.owner_user_id,
            )
        )
        if message is None or _message_revoked(message):
            raise HTTPException(status_code=404, detail="媒体不存在或已过期")
    return media


def _signed_url(
    request: Request, *, bucket: str, object_key: str
) -> tuple[str, int]:
    try:
        storage = request.app.state.persistence.get_r2_storage()
        if bucket != storage.bucket:
            raise RuntimeError("media bucket does not match configured R2 bucket")
        ttl = max(
            30,
            min(
                300,
                int(request.app.state.persistence.settings.r2_presign_ttl_seconds),
            ),
        )
        return storage.presigned_get(object_key, expires_seconds=ttl), ttl
    except Exception as exc:
        raise HTTPException(status_code=503, detail="媒体存储暂时不可用") from exc


def _native_reference_viewer(identity: Any) -> SocialPrincipal | None:
    try:
        user_id = (
            identity.user_id
            if isinstance(identity.user_id, uuid.UUID)
            else uuid.UUID(str(identity.user_id))
        )
        upstream_uid = str(identity.upstream_uid or "").strip()
    except (AttributeError, TypeError, ValueError):
        return None
    if not upstream_uid or len(upstream_uid) > 128:
        return None
    return SocialPrincipal(user_id=user_id, upstream_uid=upstream_uid)


def _load_native_media_reference(
    db: Any,
    *,
    asset_id: uuid.UUID,
    identity: Any,
    deployment: str | None = None,
    private_bucket: str | None = None,
) -> BoundMediaAssetReference:
    reference = SqlAlchemyMediaAssetReferenceRepository(
        db,
        deployment=deployment,
        private_bucket=private_bucket,
    ).get_current_reference(asset_id)
    if reference is None:
        raise HTTPException(status_code=404, detail="媒体不存在或引用已失效")
    if reference.resource_type == PROFILE_RESOURCE:
        profile = db.scalar(
            select(User).where(
                User.id == reference.resource_id,
                User.id == reference.owner_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
        )
        profile_values = (
            dict(getattr(profile, "profile", None) or {})
            if profile is not None
            else {}
        )
        current_avatar = str(
            profile_values.get("avatar") or profile_values.get("portrait") or ""
        ).strip()
        if profile is None or current_avatar != reference.content_path:
            raise HTTPException(status_code=404, detail="媒体不存在或引用已失效")
        return reference
    if reference.resource_type != SOCIAL_POST_RESOURCE:
        raise HTTPException(status_code=404, detail="媒体不存在或引用已失效")
    post = db.scalar(
        select(SocialPost).where(
            SocialPost.id == reference.resource_id,
            SocialPost.author_user_id == reference.owner_user_id,
            SocialPost.status == "published",
        )
    )
    viewer = _native_reference_viewer(identity)
    post_media = dict(getattr(post, "media", None) or {}) if post is not None else {}
    if (
        post is None
        or viewer is None
        or str(_social_post_source(post_media, reference.slot) or "").strip()
        != reference.content_path
    ):
        raise HTTPException(status_code=404, detail="媒体不存在或引用已失效")
    if not SqlAlchemySocialPermissionPolicy(db).can_view(
        viewer=viewer,
        author=SocialAuthorRef(
            user_id=post.author_user_id,
            upstream_uid=str(post.author_upstream_uid or ""),
        ),
        visibility=str(post.visibility or "private"),
    ):
        raise HTTPException(status_code=404, detail="媒体不存在或引用已失效")
    return reference


def _native_media_storage_scope(request: Request) -> tuple[str, str]:
    try:
        settings = request.app.state.settings
        storage = request.app.state.persistence.get_r2_storage()
        deployment = str(
            getattr(settings, "environment", "development") or "development"
        ).strip().lower()
        private_bucket = str(getattr(storage, "bucket", "") or "").strip()
        if not deployment or not private_bucket:
            raise RuntimeError("native media storage scope is incomplete")
        return deployment, private_bucket
    except Exception as exc:
        raise HTTPException(status_code=503, detail="媒体存储暂时不可用") from exc


@router.get("/native/{asset_id}/content", response_class=RedirectResponse)
def native_media_content(asset_id: uuid.UUID, request: Request) -> RedirectResponse:
    """Authorize one current owner-bound profile/post reference, then redirect."""

    persistence = request.app.state.persistence
    identity = persistence.require_identity(_sid(request))
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not persistence.rate_limit(
        f"media-native-content:{identity.user_id}", limit=1200, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="媒体访问请求过于频繁")
    deployment, private_bucket = _native_media_storage_scope(request)
    with session_scope() as db:
        reference = _load_native_media_reference(
            db,
            asset_id=asset_id,
            identity=identity,
            deployment=deployment,
            private_bucket=private_bucket,
        )
        object_key = reference.private_object_key
        bucket = reference.private_bucket
    url, _ttl = _signed_url(request, bucket=bucket, object_key=object_key)
    return RedirectResponse(
        url,
        status_code=307,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )


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
        media = _load_media(
            db,
            media_id=media_id,
            identity=identity,
            allow_shared=False,
        )
        object_key = media.r2_object_key
        bucket = media.r2_bucket
        content_type = media.content_type
        size_bytes = int(media.size_bytes)
        kind = media.kind

    url, ttl = _signed_url(request, bucket=bucket, object_key=object_key)

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


@router.get("/{media_id}/content", response_class=RedirectResponse)
def media_content(media_id: uuid.UUID, request: Request) -> RedirectResponse:
    """Authorize a current canonical reference, then redirect to private R2."""

    persistence = request.app.state.persistence
    identity = persistence.require_identity(_sid(request))
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not persistence.rate_limit(
        f"media-content:{identity.user_id}", limit=1200, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="媒体访问请求过于频繁")
    with session_scope() as db:
        media = _load_media(
            db,
            media_id=media_id,
            identity=identity,
            allow_shared=True,
        )
        object_key = media.r2_object_key
        bucket = media.r2_bucket
    url, _ttl = _signed_url(request, bucket=bucket, object_key=object_key)
    return RedirectResponse(
        url,
        status_code=307,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )
