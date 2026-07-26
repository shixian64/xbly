"""Web-native direct-message media HTTP API.

The browser uploads directly to a private R2 staging key, while PostgreSQL
remains authoritative for the upload intent, asset, attachment and message.
No request handler contacts TIM; compatibility work is represented by the
``message_deliveries`` outbox written in the same local transaction.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, HTTPException, Request, Response

from bbw_prod.db import session_scope
from bbw_web.media_native import (
    FlashAlreadyClaimed,
    FlashClaimDenied,
    FlashClaimRequired,
    InvalidMediaRequest,
    MediaAccessDenied,
    MediaAssetUnavailable,
    MediaBlocked,
    MediaContractViolation,
    MediaIdempotencyConflict,
    MediaIdentityUnavailable,
    MediaInspectionRejected,
    MediaIntegrityMismatch,
    MediaNativeError,
    MediaNativeService,
    MediaPeerUnavailable,
    MediaPrincipal,
    MediaQuotaExceeded,
    MediaRevocationDenied,
    MediaRevocationExpired,
    MediaThreadForbidden,
    MediaTooLarge,
    MediaTypeRejected,
    SqlAlchemyMediaConversationPolicy,
    SqlAlchemyMediaNativeRepository,
    UploadAlreadyCompleted,
    UploadIntentExpired,
    UploadIntentUnavailable,
)
from bbw_web.media_native.r2_adapter import R2PrivateMediaAdapter


router = APIRouter(prefix="/api/im/media", tags=["media-native"])
DEFAULT_COOKIE_NAME = "bbw_sid"


def _cookie_name(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "user_cookie_name", "") or DEFAULT_COOKIE_NAME)


def _identity(request: Request) -> Any:
    persistence = request.app.state.persistence
    sid = str(request.cookies.get(_cookie_name(request)) or "")
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return identity


def _principal(identity: Any) -> MediaPrincipal:
    try:
        user_id = (
            identity.user_id
            if isinstance(identity.user_id, uuid.UUID)
            else uuid.UUID(str(identity.user_id))
        )
        external_account_id = (
            identity.external_account_id
            if isinstance(identity.external_account_id, uuid.UUID)
            else uuid.UUID(str(identity.external_account_id))
        )
        upstream_uid = str(identity.upstream_uid or "").strip()
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="登录身份绑定不完整") from exc
    if (
        not upstream_uid
        or len(upstream_uid) > 128
        or any(ord(character) < 33 for character in upstream_uid)
    ):
        raise HTTPException(status_code=401, detail="登录身份绑定不完整")
    return MediaPrincipal(
        user_id=user_id,
        external_account_id=external_account_id,
        upstream_uid=upstream_uid,
    )


def _effective_origin(request: Request) -> tuple[str, str, int] | None:
    scheme = str(
        request.headers.get("X-Forwarded-Proto") or request.url.scheme
    ).split(",", 1)[0].strip().lower()
    host = str(request.headers.get("Host") or request.url.netloc).strip()
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        port = parsed.port or (
            443 if scheme == "https" else 80 if scheme == "http" else 0
        )
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not parsed.hostname or not port:
        return None
    return scheme, parsed.hostname.lower(), port


def _require_same_origin(request: Request) -> None:
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")
    raw_origin = str(request.headers.get("Origin") or "").strip()
    if not raw_origin:
        return
    try:
        parsed = urlsplit(raw_origin)
        port = parsed.port or (
            443
            if parsed.scheme.lower() == "https"
            else 80
            if parsed.scheme.lower() == "http"
            else 0
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="请求来源无效") from exc
    origin = (parsed.scheme.lower(), str(parsed.hostname or "").lower(), port)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or origin != _effective_origin(request)
    ):
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")


def _rate_limit(
    request: Request,
    identity: Any,
    action: str,
    *,
    limit: int,
) -> None:
    persistence = request.app.state.persistence
    if not persistence.rate_limit(
        f"media-native:{action}:{identity.user_id}",
        limit=limit,
        window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="媒体操作过于频繁")


def _require_message_permission(request: Request, identity: Any, peer: Any) -> None:
    persistence = request.app.state.persistence
    try:
        allowed = bool(persistence.can_message_peer(identity, peer))
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "media_message_policy_unavailable",
                "message": "私聊授权暂时不可用",
                "retryable": True,
            },
        ) from exc
    if not allowed:
        raise HTTPException(status_code=403, detail="当前账号无权向该用户发送私聊媒体")


@contextmanager
def _service_context(
    request: Request,
) -> Iterator[tuple[Any, MediaNativeService]]:
    settings = request.app.state.settings
    persistence = request.app.state.persistence
    with session_scope() as db:
        repository = SqlAlchemyMediaNativeRepository(
            db,
            user_quota_bytes=int(
                getattr(settings, "per_user_media_quota_bytes", 0) or 0
            )
            or None,
            system_quota_bytes=int(
                getattr(settings, "global_media_quota_bytes", 0) or 0
            )
            or None,
            compatibility_mode=str(
                getattr(settings, "compatibility_mode", "enabled") or "enabled"
            ),
        )
        policy = SqlAlchemyMediaConversationPolicy(db)
        try:
            storage = R2PrivateMediaAdapter(
                settings,
                storage=persistence.get_r2_storage(),
                deployment=str(getattr(settings, "environment", "development")),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "media_storage_unavailable",
                    "message": "媒体存储暂时不可用",
                    "retryable": True,
                },
            ) from exc
        yield db, MediaNativeService(
            repository,
            storage,
            policy,
            deployment=str(getattr(settings, "environment", "development")),
        )


def _error(exc: MediaNativeError) -> HTTPException:
    if isinstance(
        exc,
        (InvalidMediaRequest, MediaTypeRejected, MediaTooLarge, MediaQuotaExceeded),
    ):
        status = 400
    elif isinstance(exc, (MediaIdentityUnavailable,)):
        status = 401
    elif isinstance(
        exc,
        (
            MediaBlocked,
            MediaThreadForbidden,
            MediaAccessDenied,
            MediaRevocationDenied,
            FlashClaimDenied,
        ),
    ):
        status = 403
    elif isinstance(
        exc,
        (
            MediaPeerUnavailable,
            MediaAssetUnavailable,
            UploadIntentUnavailable,
        ),
    ):
        status = 404
    elif isinstance(
        exc,
        (
            UploadAlreadyCompleted,
            MediaIdempotencyConflict,
            MediaRevocationExpired,
            FlashClaimRequired,
            FlashAlreadyClaimed,
        ),
    ):
        status = 409
    elif isinstance(exc, UploadIntentExpired):
        status = 410
    elif isinstance(
        exc,
        (
            MediaInspectionRejected,
            MediaIntegrityMismatch,
            MediaContractViolation,
        ),
    ):
        status = 422
    else:
        status = 400
    return HTTPException(
        status_code=status,
        detail={
            "code": exc.code,
            "message": str(exc),
            "retryable": status >= 500,
        },
    )


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _media_payload(payload: Any, *, attachment_id: uuid.UUID | None = None) -> dict[str, Any]:
    result = {
        "asset_id": str(payload.asset_id),
        "kind": payload.kind,
        "name": payload.filename,
        "filename": payload.filename,
        "mime": payload.content_type,
        "content_type": payload.content_type,
        "size": int(payload.size_bytes),
        "size_bytes": int(payload.size_bytes),
        "sha256": payload.sha256,
        "duration": payload.duration_seconds,
        "width": payload.width,
        "height": payload.height,
        "flash": bool(payload.flash),
    }
    if attachment_id is not None:
        result["attachment_id"] = str(attachment_id)
    return result


@router.post("/uploads")
def create_upload_intent(
    request: Request,
    response: Response,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    _require_same_origin(request)
    identity = _identity(request)
    _rate_limit(request, identity, "upload-create", limit=30)
    try:
        with _service_context(request) as (_db, service):
            grant = service.create_upload_intent(
                principal=_principal(identity),
                kind=body.get("kind"),
                filename=body.get("filename") or body.get("name"),
                content_type=body.get("content_type") or body.get("mime"),
                size_bytes=body.get("size_bytes") or body.get("size"),
                sha256=body.get("sha256"),
            )
    except MediaNativeError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {
        "ok": True,
        "source": "web-local",
        "intent_id": str(grant.intent.id),
        "kind": grant.intent.kind,
        "method": grant.target.method,
        "upload_url": grant.target.upload_url,
        "headers": dict(grant.target.required_headers),
        "expires_at": _iso(grant.target.expires_at),
    }


@router.post("/uploads/{intent_id}/complete")
def complete_upload_intent(
    intent_id: uuid.UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _require_same_origin(request)
    identity = _identity(request)
    _rate_limit(request, identity, "upload-complete", limit=30)
    try:
        with _service_context(request) as (_db, service):
            asset = service.complete_upload(
                principal=_principal(identity),
                intent_id=intent_id,
            )
    except MediaNativeError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return {
        "ok": True,
        "source": "web-local",
        "asset_id": str(asset.id),
        "kind": asset.kind,
        "filename": asset.filename,
        "content_type": asset.content_type,
        "size_bytes": int(asset.size_bytes),
        "sha256": asset.sha256,
        "duration": asset.duration_seconds,
        "width": asset.width,
        "height": asset.height,
        "created_at": _iso(asset.created_at),
    }


@router.post("/messages")
def send_media_message(
    request: Request,
    response: Response,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    _require_same_origin(request)
    identity = _identity(request)
    _rate_limit(request, identity, "send", limit=60)
    _require_message_permission(
        request,
        identity,
        body.get("to") or body.get("peer"),
    )
    try:
        with _service_context(request) as (_db, service):
            result = service.send_attachment(
                principal=_principal(identity),
                peer_upstream_uid=body.get("to") or body.get("peer"),
                client_message_id=(
                    body.get("client_message_id") or body.get("clientMessageId")
                ),
                asset_id=body.get("asset_id"),
                flash=body.get("flash", False),
            )
    except MediaNativeError as exc:
        raise _error(exc) from exc
    attachment = result.attachment
    mirror_status = str(
        getattr(result.tim_mirror, "status", "pending") or "pending"
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "ok": True,
        "source": "web-local",
        "provider": "web-local",
        "canonical_message_id": str(attachment.message_id),
        "message_id": str(attachment.message_id),
        "attachment_id": str(attachment.id),
        "client_message_id": attachment.client_message_id,
        "kind": "flash" if attachment.payload.flash else attachment.payload.kind,
        "message_type": (
            "flash" if attachment.payload.flash else attachment.payload.kind
        ),
        "from": attachment.sender_upstream_uid,
        "to": attachment.recipient_upstream_uid,
        "occurred_at": _iso(attachment.sent_at),
        "media": _media_payload(attachment.payload, attachment_id=attachment.id),
        "flash_id": str(attachment.id) if attachment.payload.flash else "",
        "created": bool(result.created),
        "compatibility_sync": {
            "channel": "tim",
            "status": mirror_status,
            "delivery_id": str(result.tim_mirror.delivery_id),
            "required": False,
        },
        "tim_mirror_status": mirror_status,
    }


@router.get("/attachments/{attachment_id}/access")
def media_attachment_access(
    attachment_id: uuid.UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    identity = _identity(request)
    _rate_limit(request, identity, "access", limit=120)
    try:
        with _service_context(request) as (_db, service):
            grant = service.request_access(
                principal=_principal(identity),
                attachment_id=attachment_id,
            )
    except MediaNativeError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {
        "ok": True,
        "attachment_id": str(grant.attachment_id),
        "asset_id": str(grant.asset_id),
        "url": grant.url,
        "expires_at": _iso(grant.expires_at),
        "content_type": grant.content_type,
        "size_bytes": int(grant.size_bytes),
    }


@router.post("/attachments/{attachment_id}/revoke")
def revoke_media_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _require_same_origin(request)
    identity = _identity(request)
    _rate_limit(request, identity, "revoke", limit=30)
    try:
        with _service_context(request) as (_db, service):
            result = service.revoke_attachment(
                principal=_principal(identity),
                attachment_id=attachment_id,
            )
    except MediaNativeError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    mirror = result.tim_mirror
    mirror_status = (
        str(getattr(mirror, "status", "pending") or "pending")
        if mirror is not None
        else "cancelled"
    )
    return {
        "ok": True,
        "source": "web-local",
        "canonical_message_id": str(result.attachment.message_id),
        "message_id": str(result.attachment.message_id),
        "attachment_id": str(result.attachment.id),
        "revoked": True,
        "revoked_at": _iso(result.attachment.revoked_at),
        "created": bool(result.created),
        "compatibility_sync": (
            {
                "channel": "tim",
                "status": mirror_status,
                "delivery_id": str(mirror.delivery_id),
                "required": False,
            }
            if mirror is not None
            else {
                "channel": "tim",
                "status": "cancelled",
                "required": False,
            }
        ),
        "tim_mirror_status": mirror_status,
    }


@router.post("/attachments/{attachment_id}/claim")
def claim_flash_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _require_same_origin(request)
    identity = _identity(request)
    _rate_limit(request, identity, "flash-claim", limit=20)
    try:
        with _service_context(request) as (_db, service):
            result = service.claim_flash(
                principal=_principal(identity),
                attachment_id=attachment_id,
            )
    except MediaNativeError as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {
        "ok": True,
        "source": "web-local",
        "attachment_id": str(result.access.attachment_id),
        "asset_id": str(result.access.asset_id),
        "url": result.access.url,
        "content_type": result.access.content_type,
        "size_bytes": int(result.access.size_bytes),
        "claimed_at": _iso(result.claim.claimed_at),
        "display_until": _iso(result.claim.display_until),
        "expires_at": _iso(result.access.expires_at),
        "display_seconds": 5,
    }


__all__ = ["router"]
