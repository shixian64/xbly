"""Authenticated prepare/status/playback APIs for compatible moment videos."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from rq import Queue, Retry
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry

from bbw_web import bff_server as legacy
from bbw_web.moment_video import (
    COMPAT_PROFILE,
    MomentVideoError,
    asset_id_for_url,
    canonical_source_identity,
    job_id_for_asset,
    object_key_for_asset,
    remember_cached_asset,
    validate_asset_id,
)


router = APIRouter(prefix="/api/media/compat-video", tags=["media"])
PENDING_JOB_STATES = {"queued", "started", "deferred", "scheduled"}
DEFAULT_ORIGIN_PORTS = {"http": 80, "https": 443}


class PrepareMomentVideo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_url: str = Field(min_length=1, max_length=4096)
    post_id: str = Field(min_length=1, max_length=128)
    retry: bool = False


def _persistence(request: Request) -> Any:
    return request.app.state.persistence


def _identity(request: Request) -> Any:
    sid = str(request.cookies.get(legacy.COOKIE_NAME) or "")
    identity = _persistence(request).require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return identity


def _client_ip(request: Request) -> str:
    return str(
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )[:64]


def _normalized_origin(value: str) -> tuple[str, str, int] | None:
    """Return a strict scheme/host/effective-port origin tuple."""
    raw = str(value or "").strip()
    if not raw or raw == "null" or any(char.isspace() for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = str(parsed.hostname or "").lower()
    if (
        scheme not in DEFAULT_ORIGIN_PORTS
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return scheme, hostname, port if port is not None else DEFAULT_ORIGIN_PORTS[scheme]


def _request_origin(request: Request) -> tuple[str, str, int] | None:
    # Caddy terminates HTTPS and sends an exact X-Forwarded-Proto value. Uvicorn
    # also applies it to request.url, but reading it explicitly keeps this check
    # correct in focused ASGI tests and behind the documented reverse proxy.
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").split(
        ",", 1
    )[0].strip().lower()
    scheme = forwarded_proto or str(request.url.scheme or "").lower()
    if scheme not in DEFAULT_ORIGIN_PORTS:
        return None
    host = str(request.headers.get("Host") or request.url.netloc or "").strip()
    return _normalized_origin(f"{scheme}://{host}")


def _require_same_origin(request: Request) -> None:
    if str(request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")
    origin = str(request.headers.get("Origin") or "").strip()
    if not origin:
        return
    normalized = _normalized_origin(origin)
    if normalized is None:
        raise HTTPException(status_code=403, detail="请求来源无效")
    if normalized != _request_origin(request):
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")


def _queue(request: Request) -> Queue:
    return Queue("transcode", connection=_persistence(request).redis)


def _job_status(queue: Queue, asset_id: str) -> str:
    job = queue.fetch_job(job_id_for_asset(asset_id))
    if job is None:
        return "missing"
    raw = job.get_status(refresh=True)
    value = str(getattr(raw, "value", raw) or "").lower()
    if value in PENDING_JOB_STATES:
        return "processing"
    if value == "finished":
        # Permanent validation failures deliberately finish with a bounded
        # public-safe result so RQ will not retry them. Keep those distinct from
        # a successful derivative whose R2 object was later evicted.
        result = job.return_value(refresh=True)
        if isinstance(result, Mapping) and result.get("ok") is False:
            return "failed"
        return "finished"
    if value in {"failed", "stopped", "canceled"}:
        return "failed"
    return "processing"


def _registry_count(registry: Any) -> int:
    value = getattr(registry, "count", 0)
    try:
        return max(0, int(value() if callable(value) else value))
    except (TypeError, ValueError):
        return 0


def _queue_depth(queue: Queue) -> int:
    connection = queue.connection
    name = queue.name
    return len(queue) + sum(
        _registry_count(registry(name=name, connection=connection))
        for registry in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry)
    )


def _public_payload(asset_id: str, status: str, *, size_bytes: int = 0) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": status != "failed",
        "asset_id": asset_id,
        "status": status,
        "profile": COMPAT_PROFILE,
        "status_url": f"/api/media/compat-video/{asset_id}/status",
    }
    if status == "ready":
        payload["playback_url"] = f"/api/media/compat-video/{asset_id}/play"
        payload["content_type"] = "video/mp4"
        payload["size_bytes"] = max(0, int(size_bytes))
    elif status == "processing":
        payload["retry_after"] = 3
    else:
        payload["code"] = "VIDEO_COMPAT_FAILED"
        payload["message"] = "该视频暂时无法转换为兼容格式"
        payload["retryable"] = True
    return payload


def _ready_metadata(request: Request, asset_id: str) -> Mapping[str, Any] | None:
    try:
        persistence = _persistence(request)
        metadata = persistence.get_r2_storage().head_object(
            object_key_for_asset(asset_id)
        )
        if metadata is not None:
            remember_cached_asset(
                persistence.redis,
                persistence.settings,
                asset_id,
                int(metadata.get("size") or 0),
            )
        return metadata
    except Exception as exc:
        raise HTTPException(status_code=503, detail="兼容视频存储暂时不可用") from exc


@router.post("/prepare")
def prepare_moment_video(body: PrepareMomentVideo, request: Request) -> dict[str, Any]:
    """Queue one bounded conversion without ever echoing the original URL."""
    _require_same_origin(request)
    identity = _identity(request)
    persistence = _persistence(request)
    if not persistence.rate_limit(
        f"moment-video-prepare:{identity.user_id}", limit=6, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="兼容视频处理请求过于频繁")
    try:
        source_url = canonical_source_identity(body.source_url)
        asset_id = asset_id_for_url(source_url)
    except MomentVideoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not persistence.authorize_moment_video(
        identity, post_id=body.post_id, asset_id=asset_id
    ):
        raise HTTPException(status_code=403, detail="该动态视频当前不可访问")

    metadata = _ready_metadata(request, asset_id)
    if metadata is not None:
        return _public_payload(asset_id, "ready", size_bytes=int(metadata.get("size") or 0))

    queue = _queue(request)
    state = _job_status(queue, asset_id)
    if state == "finished":
        # The deterministic RQ job can outlive an R2 object removed by cache
        # retention. Remove the stale successful job so this ordinary prepare
        # request recreates the derivative without requiring a manual retry.
        finished_job = queue.fetch_job(job_id_for_asset(asset_id))
        if finished_job is not None:
            try:
                finished_job.delete()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="兼容视频暂时无法重新生成") from exc
        state = "missing"
    if state == "failed":
        if not body.retry:
            return _public_payload(asset_id, "failed")
        if not persistence.rate_limit(
            f"moment-video-retry:{identity.user_id}", limit=2, window_seconds=600
        ):
            raise HTTPException(status_code=429, detail="兼容视频重试过于频繁")
        failed_job = queue.fetch_job(job_id_for_asset(asset_id))
        if failed_job is not None:
            try:
                failed_job.delete()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="兼容视频暂时无法重试") from exc
        state = "missing"
    if state == "processing":
        return _public_payload(asset_id, "processing")
    if _queue_depth(queue) >= 50:
        raise HTTPException(status_code=503, detail="兼容视频处理队列繁忙，请稍后重试")

    try:
        queue.enqueue(
            "bbw_web.moment_video.transcode_moment_video_job",
            asset_id,
            source_url,
            job_id=job_id_for_asset(asset_id),
            job_timeout=1800,
            result_ttl=86400,
            failure_ttl=3600,
            retry=Retry(max=2, interval=[30, 120]),
            meta={
                "owner_user_id": str(identity.user_id),
                "client_ip_hash": hashlib.sha256(_client_ip(request).encode("utf-8")).hexdigest(),
            },
        )
    except Exception as exc:
        # A racing request may have enqueued the deterministic job first.
        if _job_status(queue, asset_id) == "missing":
            raise HTTPException(status_code=503, detail="兼容视频任务暂时无法创建") from exc
    return _public_payload(asset_id, "processing")


@router.get("/{asset_id}/status")
def moment_video_status(asset_id: str, request: Request) -> dict[str, Any]:
    identity = _identity(request)
    persistence = _persistence(request)
    try:
        asset_id = validate_asset_id(asset_id)
    except MomentVideoError as exc:
        raise HTTPException(status_code=404, detail="兼容视频不存在") from exc
    if not persistence.can_access_moment_video(identity, asset_id):
        raise HTTPException(status_code=403, detail="该动态视频当前不可访问")
    if not persistence.rate_limit(
        f"moment-video-status-total:{identity.user_id}",
        limit=240,
        window_seconds=60,
    ) or not persistence.rate_limit(
        f"moment-video-status:{identity.user_id}:{asset_id}",
        limit=60,
        window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="兼容视频状态查询过于频繁")
    state = _job_status(_queue(request), asset_id)
    # Avoid one R2 HEAD on every client poll while a long transcode is known to
    # be active. Once the job leaves a pending state, probe the deterministic
    # object so finished jobs and historical cache entries converge to ready.
    if state == "processing":
        return _public_payload(asset_id, "processing")
    metadata = _ready_metadata(request, asset_id)
    if metadata is not None:
        return _public_payload(asset_id, "ready", size_bytes=int(metadata.get("size") or 0))
    return _public_payload(asset_id, "failed")


@router.api_route("/{asset_id}/play", methods=["GET", "HEAD"])
def play_compatible_moment_video(asset_id: str, request: Request) -> Response:
    """Redirect only to the private H.264 derivative, never to the source."""
    identity = _identity(request)
    persistence = _persistence(request)
    if not persistence.rate_limit(
        f"moment-video-play:{identity.user_id}", limit=60, window_seconds=60
    ):
        raise HTTPException(status_code=429, detail="兼容视频播放请求过于频繁")
    try:
        asset_id = validate_asset_id(asset_id)
    except MomentVideoError as exc:
        raise HTTPException(status_code=404, detail="兼容视频不存在") from exc
    if not persistence.can_access_moment_video(identity, asset_id):
        raise HTTPException(status_code=403, detail="该动态视频当前不可访问")
    metadata = _ready_metadata(request, asset_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="兼容视频尚未就绪")
    if request.method == "HEAD":
        # A 307 preserves HEAD, while the R2 URL below is signed specifically
        # for GET. Answer metadata locally instead of redirecting HEAD with an
        # incompatible SigV4 method.
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "Content-Length": str(max(0, int(metadata.get("size") or 0))),
        }
        etag = str(metadata.get("etag") or "").strip()
        if etag:
            headers["ETag"] = f'"{etag}"'
        return Response(status_code=200, media_type="video/mp4", headers=headers)
    try:
        # Compatible videos may be up to ten minutes long. Keep the final R2
        # URL valid long enough for later Range/seek requests during playback.
        ttl = 900
        signed_url = persistence.get_r2_storage().presigned_get(
            object_key_for_asset(asset_id), expires_seconds=ttl
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="兼容视频存储暂时不可用") from exc
    return RedirectResponse(
        signed_url,
        status_code=307,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
