"""Asynchronous compatibility cache for upstream Dynamic-feed videos.

The browser never receives an "open original" fallback from this module.  A
validated upstream object is downloaded by an RQ worker, converted to a small
H.264/AAC MP4, and written to the existing private R2 bucket under a
deterministic cache key.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from redis import Redis

from bbw_prod.config import get_settings
from bbw_web.media_archive import MediaArchiveError, PreparedMedia, download_and_prepare
from bbw_web.normalize import resolve_media_url
from bbw_web.r2 import R2Storage


COMPAT_PROFILE = "h264-main-1280-v1"
CACHE_INDEX_VERSION = "atomic-v1"
ASSET_ID_RE = re.compile(r"^[0-9a-f]{64}$")
# Older APK feeds stored MP4 video objects below ``audios/99999``. Keep both
# known layouts explicit instead of allowing arbitrary public CDN paths.
SOURCE_PATH_RE = re.compile(
    r"^(?:"
    r"/video/\d{6}/\d{10,20}\.(?:mp4|mov)|"
    r"/audios/99999/\d{4}/(?:0[1-9]|1[0-2])/[A-Z0-9_-]{8,128}\.mp4"
    r")$",
    re.I,
)
ALLOWED_SOURCE_HOSTS = ("oss.banghua.xin",)


# Keep the three Redis index mutations in one server-side operation.  A normal
# pipeline/MULTI is not enough when the previous hash value was read before the
# transaction: two concurrent readers can otherwise both apply their delta to
# ``total-bytes`` and permanently over-count the cache.
_REMEMBER_CACHE_LUA = r"""
local asset_id = ARGV[1]
local size = math.max(0, tonumber(ARGV[2]) or 0)
local touched_at = tonumber(ARGV[3]) or 0
local previous_size = math.max(
    0,
    tonumber(redis.call('HGET', KEYS[2], asset_id)) or 0
)
local total = tonumber(redis.call('GET', KEYS[3]))
if total == nil then
    total = 0
    for _, value in ipairs(redis.call('HVALS', KEYS[2])) do
        total = total + math.max(0, tonumber(value) or 0)
    end
end
redis.call('ZADD', KEYS[1], touched_at, asset_id)
redis.call('HSET', KEYS[2], asset_id, size)
total = math.max(0, total + size - previous_size)
redis.call('SET', KEYS[3], total)
return total
"""


_FORGET_CACHE_LUA = r"""
local asset_id = ARGV[1]
local size = math.max(
    0,
    tonumber(redis.call('HGET', KEYS[2], asset_id)) or 0
)
local total = tonumber(redis.call('GET', KEYS[3]))
redis.call('ZREM', KEYS[1], asset_id)
local removed = redis.call('HDEL', KEYS[2], asset_id)
if total == nil then
    total = 0
    for _, value in ipairs(redis.call('HVALS', KEYS[2])) do
        total = total + math.max(0, tonumber(value) or 0)
    end
elseif removed == 1 then
    total = math.max(0, total - size)
else
    total = math.max(0, total)
end
redis.call('SET', KEYS[3], total)
return total
"""


# Periodic selection also repairs totals written by the old non-atomic
# implementation.  Redis serializes this script with remember/forget scripts,
# so the repair cannot overwrite a concurrent delta with a stale HVALS sum.
_CACHE_USAGE_LUA = r"""
local total = tonumber(redis.call('GET', KEYS[3]))
local version = redis.call('GET', KEYS[4])
if total == nil or version ~= ARGV[1] then
    total = 0
    for _, value in ipairs(redis.call('HVALS', KEYS[2])) do
        total = total + math.max(0, tonumber(value) or 0)
    end
    redis.call('SET', KEYS[3], total)
    redis.call('SET', KEYS[4], ARGV[1])
end
return {total, redis.call('ZCARD', KEYS[1])}
"""


class MomentVideoError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VideoProbe:
    codec: str
    pixel_format: str
    width: int
    height: int
    duration: float
    fps: float


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _canonical_url_path(value: str) -> str:
    """Normalize equivalent RFC 3986 path spellings without merging ``//`` keys."""
    raw = str(value or "")
    if not raw.startswith("/"):
        raise MomentVideoError("invalid video source path")
    unreserved = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    normalized: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "%":
            normalized.append(char)
            index += 1
            continue
        if index + 2 >= len(raw):
            raise MomentVideoError("invalid video source path encoding")
        token = raw[index + 1 : index + 3]
        try:
            byte = int(token, 16)
        except ValueError as exc:
            raise MomentVideoError("invalid video source path encoding") from exc
        if byte in unreserved:
            normalized.append(chr(byte))
        else:
            normalized.append(f"%{byte:02X}")
        index += 3

    path = "".join(normalized)
    segments = path.split("/")
    output: list[str] = []
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if len(output) > 1:
                output.pop()
            continue
        output.append(segment)
    if segments[-1:] and segments[-1] in {".", ".."}:
        output.append("")
    canonical = "/".join(output)
    # Encode literal Unicode/control characters once while retaining canonical
    # uppercase escapes for reserved bytes such as %2F.
    return quote(canonical, safe="/%:@!$&'()*+,;=-._~")


def canonical_source_identity(value: Any) -> str:
    """Canonicalize an APK video URL without performing network I/O."""
    raw = resolve_media_url(value)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise MomentVideoError("invalid video source URL") from exc
    if parsed.hostname != "oss.banghua.xin" or port not in {None, 443} or not parsed.path:
        raise MomentVideoError("video source host is not allowed")
    # This CDN serves public objects without query authentication. Drop both
    # query and fragment, and collapse equivalent origin spellings, so callers
    # cannot bypass deterministic job/R2 de-duplication with arbitrary suffixes.
    canonical_path = _canonical_url_path(parsed.path)
    if not SOURCE_PATH_RE.fullmatch(canonical_path):
        raise MomentVideoError("video source path is not allowed")
    return urlunsplit(("https", "oss.banghua.xin", canonical_path, "", ""))


def asset_id_for_url(source_url: str) -> str:
    return hashlib.sha256(f"{COMPAT_PROFILE}\n{source_url}".encode("utf-8")).hexdigest()


def validate_asset_id(value: Any) -> str:
    asset_id = str(value or "").strip().lower()
    if not ASSET_ID_RE.fullmatch(asset_id):
        raise MomentVideoError("invalid compatibility asset id")
    return asset_id


def object_key_for_asset(asset_id: str) -> str:
    asset_id = validate_asset_id(asset_id)
    return f"compat/moments/{COMPAT_PROFILE}/{asset_id[:2]}/{asset_id}.mp4"


def _cache_index_keys(settings: Any) -> tuple[str, str, str]:
    prefix = f"{settings.redis_prefix}:moment-video-cache:{COMPAT_PROFILE}"
    return f"{prefix}:age", f"{prefix}:size", f"{prefix}:total-bytes"


def _cache_limits(settings: Any) -> tuple[int, int, int]:
    retention_days = _env_int(
        "BBW_MOMENT_VIDEO_CACHE_RETENTION_DAYS", 30, minimum=1, maximum=365
    )
    max_objects = _env_int(
        "BBW_MOMENT_VIDEO_CACHE_MAX_OBJECTS", 2000, minimum=10, maximum=100000
    )
    system_quota = int(
        getattr(settings, "system_media_quota_bytes", 8 * 1024 * 1024 * 1024)
    )
    default_cache_max = max(
        100 * 1024 * 1024,
        min(2 * 1024 * 1024 * 1024, system_quota // 4),
    )
    max_bytes = _env_int(
        "BBW_MOMENT_VIDEO_CACHE_MAX_BYTES",
        default_cache_max,
        minimum=100 * 1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    return retention_days, max_objects, max_bytes


def remember_cached_asset(
    connection: Any, settings: Any, asset_id: str, size_bytes: int
) -> None:
    asset_id = validate_asset_id(asset_id)
    size = max(0, int(size_bytes))
    age_key, size_key, total_key = _cache_index_keys(settings)
    connection.eval(
        _REMEMBER_CACHE_LUA,
        3,
        age_key,
        size_key,
        total_key,
        asset_id,
        str(size),
        repr(time.time()),
    )


def _cache_usage(connection: Any, settings: Any) -> tuple[int, int]:
    age_key, size_key, total_key = _cache_index_keys(settings)
    usage = connection.eval(
        _CACHE_USAGE_LUA,
        4,
        age_key,
        size_key,
        total_key,
        f"{total_key}:version",
        CACHE_INDEX_VERSION,
    )
    values = list(usage or ())
    total_bytes = max(0, int((values + [0, 0])[0] or 0))
    object_count = max(0, int((values + [0, 0])[1] or 0))
    return total_bytes, object_count


def _decoded_asset_ids(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = (
            value.decode("ascii", errors="ignore")
            if isinstance(value, bytes)
            else str(value)
        )
        if ASSET_ID_RE.fullmatch(text):
            result.append(text)
    return result


def cached_assets_for_cleanup(
    connection: Any, settings: Any, *, limit: int = 50
) -> list[str]:
    age_key, size_key, total_key = _cache_index_keys(settings)
    batch_limit = max(1, min(500, int(limit)))
    retention_days, max_objects, max_bytes = _cache_limits(settings)

    cutoff = time.time() - retention_days * 86400
    selected = _decoded_asset_ids(
        connection.zrangebyscore(age_key, "-inf", cutoff, start=0, num=batch_limit)
    )
    selected_set = set(selected)
    total_bytes, object_count = _cache_usage(connection, settings)
    for asset_id in selected:
        total_bytes -= max(0, int(connection.hget(size_key, asset_id) or 0))
        object_count -= 1
    if len(selected) < batch_limit and (
        total_bytes > max_bytes or object_count > max_objects
    ):
        oldest = _decoded_asset_ids(
            connection.zrange(age_key, 0, batch_limit * 4 - 1)
        )
        for asset_id in oldest:
            if asset_id in selected_set:
                continue
            selected.append(asset_id)
            selected_set.add(asset_id)
            total_bytes -= max(0, int(connection.hget(size_key, asset_id) or 0))
            object_count -= 1
            if len(selected) >= batch_limit or (
                total_bytes <= max_bytes and object_count <= max_objects
            ):
                break
    return selected


def forget_cached_asset(connection: Any, settings: Any, asset_id: str) -> None:
    asset_id = validate_asset_id(asset_id)
    age_key, size_key, total_key = _cache_index_keys(settings)
    connection.eval(
        _FORGET_CACHE_LUA,
        3,
        age_key,
        size_key,
        total_key,
        asset_id,
    )


def trim_cached_assets_after_write(
    connection: Any,
    settings: Any,
    storage: Any,
    *,
    protected_asset_id: str,
    batch_limit: int = 50,
    max_batches: int = 4,
) -> tuple[int, int]:
    """Bound cache growth on the write path instead of waiting 15 minutes.

    The scheduled retention job remains the eventual retry path for R2
    failures.  One successful write adds one object while this bounded pass can
    remove up to 200 old objects with the defaults, so sustained writes cannot
    outrun quota cleanup under the normal single transcode-worker deployment.
    """

    protected = validate_asset_id(protected_asset_id)
    limit = max(1, min(100, int(batch_limit)))
    rounds = max(1, min(10, int(max_batches)))
    _retention_days, max_objects, max_bytes = _cache_limits(settings)
    age_key, _size_key, _total_key = _cache_index_keys(settings)
    deleted = 0
    errors = 0
    skipped = {protected}

    for _round in range(rounds):
        total_bytes, object_count = _cache_usage(connection, settings)
        if total_bytes <= max_bytes and object_count <= max_objects:
            break
        selected_at = time.time()
        oldest = _decoded_asset_ids(
            connection.zrange(age_key, 0, limit + len(skipped) - 1)
        )
        candidates = [asset_id for asset_id in oldest if asset_id not in skipped][
            :limit
        ]
        if not candidates:
            break
        made_progress = False
        for asset_id in candidates:
            try:
                # If a request touched this entry after selection began, leave
                # it for a later pass rather than invalidating active playback.
                last_access = connection.zscore(age_key, asset_id)
                if last_access is None or float(last_access) > selected_at:
                    skipped.add(asset_id)
                    continue
                storage.delete(object_key_for_asset(asset_id))
            except Exception:
                # Keep the index entry when R2 deletion fails so the periodic
                # cleanup job can retry the physical object.
                errors += 1
                skipped.add(asset_id)
                continue
            deleted += 1
            made_progress = True
            try:
                forget_cached_asset(connection, settings, asset_id)
            except Exception:
                # Physical quota was released, but retain an accounting
                # over-count until a later idempotent delete/forget retry.
                errors += 1
                skipped.add(asset_id)
        if not made_progress:
            break
    return deleted, errors


def job_id_for_asset(asset_id: str) -> str:
    return f"moment-video:{COMPAT_PROFILE}:{validate_asset_id(asset_id)}"


def verified_source_url(source_url: Any, asset_id: str) -> str:
    """Revalidate the public CDN URL carried by the private Redis job."""
    canonical = canonical_source_identity(source_url)
    if asset_id_for_url(canonical) != validate_asset_id(asset_id):
        raise MomentVideoError("video source identity mismatch")
    return canonical


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is not installed")
    return path


def cleanup_stale_scratch(*, older_than_seconds: int = 86400) -> None:
    """Remove only this pipeline's orphaned files from its configured scratch dir."""
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - max(3600, int(older_than_seconds))
    prefixes = ("bbw-archive-", "bbw-moment-h264-")
    try:
        entries = list(root.iterdir())[:1000]
    except OSError:
        return
    for path in entries:
        if not path.is_file() or not path.name.startswith(prefixes):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Decoder errors are not returned to users and can be attacker-
            # amplified by malformed inputs. Do not retain an unbounded stderr
            # buffer inside the 512 MiB transcode worker.
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MomentVideoError("video compatibility processing timed out") from exc


def probe_video(path: Path) -> VideoProbe:
    command = [
        _binary("ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,duration,avg_frame_rate,r_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = _run(command, timeout=60)
    if result.returncode != 0:
        raise MomentVideoError("video metadata cannot be decoded")
    try:
        payload = json.loads(result.stdout or "{}")
        stream = (payload.get("streams") or [])[0]
        format_data = payload.get("format") or {}
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        durations = []
        for raw_duration in (stream.get("duration"), format_data.get("duration")):
            try:
                candidate = float(raw_duration or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(candidate) and candidate > 0:
                durations.append(candidate)
        duration = max(durations, default=0.0)
        fps = 0.0
        for raw_rate in (stream.get("avg_frame_rate"), stream.get("r_frame_rate")):
            rate_text = str(raw_rate or "0/1")
            numerator_text, denominator_text = (rate_text.split("/", 1) + ["1"])[:2]
            try:
                denominator = float(denominator_text or 1)
                candidate_fps = float(numerator_text or 0) / denominator if denominator else 0.0
            except (TypeError, ValueError):
                continue
            if math.isfinite(candidate_fps) and candidate_fps > 0:
                fps = candidate_fps
                break
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MomentVideoError("video metadata is invalid") from exc
    if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
        raise MomentVideoError("video metadata is incomplete")
    max_dimension = _env_int(
        "BBW_MOMENT_VIDEO_MAX_SOURCE_DIMENSION", 4096, minimum=320, maximum=8192
    )
    max_duration = _env_int(
        "BBW_MOMENT_VIDEO_MAX_DURATION_SECONDS", 600, minimum=5, maximum=3600
    )
    if width > max_dimension or height > max_dimension:
        raise MomentVideoError("video dimensions exceed the compatibility limit")
    if duration > max_duration:
        raise MomentVideoError("video duration exceeds the compatibility limit")
    max_pixels = _env_int(
        "BBW_MOMENT_VIDEO_MAX_SOURCE_PIXELS",
        9_000_000,
        minimum=320 * 240,
        maximum=8192 * 8192,
    )
    if width * height > max_pixels:
        raise MomentVideoError("video pixel count exceeds the compatibility limit")
    max_fps = _env_int("BBW_MOMENT_VIDEO_MAX_SOURCE_FPS", 120, minimum=24, maximum=240)
    if not math.isfinite(fps) or fps <= 0 or fps > max_fps:
        raise MomentVideoError("video frame rate exceeds the compatibility limit")
    return VideoProbe(
        codec=str(stream.get("codec_name") or "").lower(),
        pixel_format=str(stream.get("pix_fmt") or "").lower(),
        width=width,
        height=height,
        duration=duration,
        fps=fps,
    )


def transcode_h264(source: Path, probe: VideoProbe) -> PreparedMedia:
    fd, output_name = tempfile.mkstemp(prefix="bbw-moment-h264-", suffix=".mp4")
    os.close(fd)
    output = Path(output_name)
    max_output = _env_int(
        "BBW_MOMENT_VIDEO_MAX_OUTPUT_BYTES",
        100 * 1024 * 1024,
        minimum=1024 * 1024,
        maximum=512 * 1024 * 1024,
    )
    timeout = min(
        _env_int("BBW_MOMENT_VIDEO_JOB_TIMEOUT_SECONDS", 1500, minimum=120, maximum=7200),
        max(180, int(probe.duration * 12) + 120),
    )
    total_budget_bps = int((max_output * 8 * 0.94) / max(1.0, probe.duration))
    video_maxrate = max(300_000, min(4_000_000, total_budget_bps - 160_000))
    if video_maxrate + 160_000 > total_budget_bps:
        raise MomentVideoError("compatible video cannot fit the output size limit")
    # Do not pass a remote URL to ffmpeg.  The SSRF-safe downloader has already
    # materialized a bounded local input before this command is reached.
    command = [
        _binary("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-vf",
        r"scale=min(1280\,iw):min(1280\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30,setsar=1,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        os.getenv("BBW_MOMENT_VIDEO_X264_PRESET", "veryfast"),
        "-crf",
        str(_env_int("BBW_MOMENT_VIDEO_X264_CRF", 23, minimum=18, maximum=30)),
        "-maxrate",
        str(video_maxrate),
        "-bufsize",
        str(video_maxrate * 2),
        "-profile:v",
        "main",
        "-level:v",
        "4.0",
        "-tag:v",
        "avc1",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(_env_int("BBW_MOMENT_VIDEO_THREADS", 1, minimum=1, maximum=2)),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-t",
        f"{probe.duration:.3f}",
        "-fs",
        str(max_output),
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "1024",
        str(output),
    ]
    try:
        result = _run(command, timeout=timeout)
        if result.returncode != 0 or not output.is_file():
            raise MomentVideoError("video compatibility transcoding failed")
        size = output.stat().st_size
        if size <= 0 or size > max_output:
            raise MomentVideoError("compatible video exceeds the output size limit")
        output_probe = probe_video(output)
        if output_probe.codec != "h264" or output_probe.pixel_format not in {
            "yuv420p",
            "yuvj420p",
        }:
            raise MomentVideoError("compatible video verification failed")
        duration_tolerance = max(2.0, probe.duration * 0.05)
        if output_probe.duration + duration_tolerance < probe.duration:
            raise MomentVideoError("compatible video was truncated before completion")
        digest = hashlib.sha256()
        with output.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return PreparedMedia(
            path=output,
            size=size,
            sha256=digest.hexdigest(),
            content_type="video/mp4",
            extension="mp4",
            original_name="",
        )
    except Exception:
        output.unlink(missing_ok=True)
        raise


def transcode_job(asset_id: str, source_url: str) -> dict[str, Any]:
    """Create one cached H.264 derivative for a browser-incompatible video."""
    asset_id = validate_asset_id(asset_id)
    settings = get_settings()
    source_url = verified_source_url(source_url, asset_id)
    cleanup_stale_scratch()
    storage = R2Storage(settings)
    object_key = object_key_for_asset(asset_id)
    existing = storage.head_object(object_key)
    if existing is not None:
        connection = Redis.from_url(settings.redis_url)
        try:
            remember_cached_asset(connection, settings, asset_id, int(existing.get("size") or 0))
            trim_cached_assets_after_write(
                connection,
                settings,
                storage,
                protected_asset_id=asset_id,
            )
        finally:
            connection.close()
        return {"ok": True, "asset_id": asset_id, "existing": True}

    source: PreparedMedia | None = None
    compatible: PreparedMedia | None = None
    try:
        source = download_and_prepare(
            url=source_url,
            kind="video",
            allowed_hosts=ALLOWED_SOURCE_HOSTS,
            settings=settings,
        )
        probe = probe_video(source.path)
        compatible = transcode_h264(source.path, probe)
        with compatible.path.open("rb") as handle:
            stored = storage.upload_stream(
                key=object_key,
                stream=handle,
                size=compatible.size,
                content_type=compatible.content_type,
                sha256=compatible.sha256,
                metadata={
                    "compat-profile": COMPAT_PROFILE,
                    "source-sha256": source.sha256,
                    "source-codec": probe.codec[:32],
                },
            )
        connection = Redis.from_url(settings.redis_url)
        try:
            remember_cached_asset(connection, settings, asset_id, compatible.size)
            trim_cached_assets_after_write(
                connection,
                settings,
                storage,
                protected_asset_id=asset_id,
            )
        finally:
            connection.close()
        return {
            "ok": True,
            "asset_id": asset_id,
            "size_bytes": compatible.size,
            "etag": stored.etag,
        }
    finally:
        if compatible is not None:
            compatible.cleanup()
        if source is not None:
            source.cleanup()


def transcode_moment_video_job(
    asset_id: str, source_url: str
) -> dict[str, Any]:
    """RQ entry point kept independent from the application protocol stack."""

    try:
        return transcode_job(asset_id, source_url)
    except MomentVideoError as exc:
        # Invalid, oversized or undecodable inputs will not improve on an
        # automatic retry. Finish deterministically; the API exposes only a
        # generic failure and still allows a tightly rate-limited manual retry.
        if "timed out" in str(exc).lower():
            raise
        return {
            "ok": False,
            "permanent": True,
            "error_type": type(exc).__name__,
        }
    except MediaArchiveError as exc:
        if "cannot be resolved" in str(exc).lower():
            raise
        return {
            "ok": False,
            "permanent": True,
            "error_type": type(exc).__name__,
        }
    except httpx.HTTPStatusError as exc:
        status = int(exc.response.status_code)
        # Request Timeout, Too Early and Too Many Requests are explicitly
        # transient; re-raise so the queue's delayed Retry policy handles them.
        if 400 <= status < 500 and status not in {408, 425, 429}:
            return {
                "ok": False,
                "permanent": True,
                "error_type": "UpstreamMediaUnavailable",
            }
        raise
