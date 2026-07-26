"""Bounded, SSRF-safe media download and privacy processing for R2 archival."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError


Image.MAX_IMAGE_PIXELS = 40_000_000

IMAGE_LIMIT = 20 * 1024 * 1024
AUDIO_LIMIT = 20 * 1024 * 1024
VIDEO_LIMIT = 100 * 1024 * 1024
FILE_LIMIT = 40 * 1024 * 1024


class MediaArchiveError(ValueError):
    pass


@dataclass
class PreparedMedia:
    path: Path
    size: int
    sha256: str
    content_type: str
    extension: str
    original_name: str = ""

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    hostname = str(host or "").strip(".").lower()
    for pattern in allowed_hosts:
        value = str(pattern or "").strip().lower()
        if not value:
            continue
        if value.startswith("*."):
            suffix = value[1:]
            if hostname.endswith(suffix) and hostname != suffix.lstrip("."):
                return True
        elif hostname == value:
            return True
    return False


def _validate_remote_url(url: str, allowed_hosts: Iterable[str]) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MediaArchiveError("media URL must be an authenticated-free HTTPS URL")
    if not _host_allowed(parsed.hostname, allowed_hosts):
        raise MediaArchiveError("media host is not allowlisted")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise MediaArchiveError("media host cannot be resolved") from exc
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if not ip.is_global:
            raise MediaArchiveError("media host resolved to a non-public address")
    return parsed.geturl()


def _limit_for_kind(kind: str, settings: Any = None) -> int:
    value = str(kind or "").strip().lower()
    if value in {"image", "flash", "timimageelem"}:
        return int(getattr(settings, "media_max_image_bytes", IMAGE_LIMIT))
    if value in {"audio", "voice", "sound", "timsoundelem"}:
        return int(getattr(settings, "media_max_audio_bytes", AUDIO_LIMIT))
    if value in {"video", "timvideofileelem"}:
        return int(getattr(settings, "media_max_video_bytes", VIDEO_LIMIT))
    return int(getattr(settings, "media_max_attachment_bytes", FILE_LIMIT))


def _sniff(path: Path) -> tuple[str, str]:
    with path.open("rb") as handle:
        head = handle.read(512)
    lowered = head.lstrip().lower()
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if head.startswith(b"BM"):
        return "image/bmp", "bmp"
    # AMR 语音（TIM 历史语音常见容器）的魔数 "#!AMR" 必须先于通用的
    # "#!" 活性内容判定识别，否则语音归档会被误判为可执行内容。
    if head.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "audio/amr", "amr"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        if brand == b"qt  ":
            return "video/quicktime", "mov"
        # M4A/M4B 是纯音频的 MP4 容器品牌，判成 video 会让语音历史
        # 在 _validate_kind 处失败关闭。
        if brand in {b"m4a ", b"m4b "}:
            return "audio/mp4", "m4a"
        return "video/mp4", "mp4"
    if head.startswith(b"ID3") or (len(head) > 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg", "mp3"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if head.startswith(b"OggS"):
        return "audio/ogg", "ogg"
    if head.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip", "zip"
    if head.startswith((b"MZ", b"\x7fELF")) or lowered.startswith(
        (b"<html", b"<!doctype html", b"<svg", b"<?xml", b"#!")
    ):
        raise MediaArchiveError("executable or active content is not accepted")
    # Plain text is allowed only when it does not resemble active content.
    if head and b"\x00" not in head:
        try:
            head.decode("utf-8")
            return "text/plain", "txt"
        except UnicodeDecodeError:
            pass
    return "application/octet-stream", "bin"


def _validate_kind(kind: str, content_type: str) -> None:
    value = str(kind or "").strip().lower()
    if value in {"image", "flash", "timimageelem"} and not content_type.startswith("image/"):
        raise MediaArchiveError("reported image is not an image")
    if value in {"audio", "voice", "sound", "timsoundelem"} and not content_type.startswith("audio/"):
        raise MediaArchiveError("reported audio is not audio")
    if value in {"video", "timvideofileelem"} and not content_type.startswith("video/"):
        raise MediaArchiveError("reported video is not video")


def _validate_passive_content(path: Path, content_type: str) -> None:
    if content_type == "application/pdf":
        blocked = (
            b"/javascript",
            b"/js",
            b"/launch",
            b"/openaction",
            b"/embeddedfile",
            b"/richmedia",
        )
        tail = b""
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(256 * 1024), b""):
                sample = (tail + chunk).lower()
                if any(marker in sample for marker in blocked):
                    raise MediaArchiveError("active PDF content is not accepted")
                tail = sample[-64:]
        return

    if content_type != "application/zip":
        return
    blocked_suffixes = {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".js",
        ".mjs",
        ".html",
        ".htm",
        ".svg",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".apk",
        ".jar",
        ".msi",
        ".scr",
        ".com",
        ".vbs",
    }
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise MediaArchiveError("archive contains too many entries")
            for entry in entries:
                normalized = entry.filename.replace("\\", "/").lower()
                parts = [part for part in normalized.split("/") if part not in {"", "."}]
                if normalized.startswith("/") or ".." in parts:
                    raise MediaArchiveError("archive paths are unsafe")
                suffix = Path(normalized).suffix
                if suffix in blocked_suffixes or normalized.endswith("vbaproject.bin"):
                    raise MediaArchiveError("archive contains active content")
                # Unix symlinks can redirect extraction outside an expected tree.
                if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                    raise MediaArchiveError("archive symlinks are not accepted")
                total_uncompressed += int(entry.file_size)
                if total_uncompressed > 100 * 1024 * 1024:
                    raise MediaArchiveError("archive expands beyond the safe limit")
                if entry.compress_size > 0 and entry.file_size / entry.compress_size > 100:
                    raise MediaArchiveError("archive compression ratio is unsafe")
    except zipfile.BadZipFile as exc:
        raise MediaArchiveError("archive cannot be safely decoded") from exc


def _strip_image_metadata(media: PreparedMedia) -> PreparedMedia:
    if media.content_type not in {"image/jpeg", "image/png", "image/webp", "image/bmp"}:
        return media
    target_fd, target_name = tempfile.mkstemp(prefix="bbw-clean-", suffix=f".{media.extension}")
    os.close(target_fd)
    target = Path(target_name)
    try:
        with Image.open(media.path) as image:
            if getattr(image, "is_animated", False):
                target.unlink(missing_ok=True)
                return media
            image.load()
            output = image.copy()
            save_format = {
                "jpg": "JPEG",
                "png": "PNG",
                "webp": "WEBP",
                "bmp": "BMP",
            }[media.extension]
            kwargs: dict[str, Any] = {"format": save_format}
            if save_format == "JPEG":
                if output.mode not in {"RGB", "L"}:
                    output = output.convert("RGB")
                kwargs.update(quality=92, optimize=True)
            elif save_format == "WEBP":
                kwargs.update(quality=92, method=4)
            output.save(target, **kwargs)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise MediaArchiveError("image cannot be safely decoded") from exc
    media.cleanup()
    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return PreparedMedia(
        path=target,
        size=size,
        sha256=digest.hexdigest(),
        content_type=media.content_type,
        extension=media.extension,
        original_name=media.original_name,
    )


def download_and_prepare(
    *,
    url: str,
    kind: str,
    allowed_hosts: Iterable[str],
    original_name: str = "",
    settings: Any = None,
    max_bytes: int | None = None,
) -> PreparedMedia:
    """Download one bounded remote object, identify it, and strip image metadata."""
    current = _validate_remote_url(url, allowed_hosts)
    limit = max(1, int(max_bytes)) if max_bytes is not None else _limit_for_kind(kind, settings)
    fd, filename = tempfile.mkstemp(prefix="bbw-archive-", suffix=".part")
    os.close(fd)
    target = Path(filename)
    digest = hashlib.sha256()
    size = 0
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _ in range(4):
                current = _validate_remote_url(current, allowed_hosts)
                with client.stream("GET", current, headers={"User-Agent": "bbw-media-archive/1"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise MediaArchiveError("media redirect has no location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    declared = int(response.headers.get("Content-Length") or 0)
                    if declared > limit:
                        raise MediaArchiveError("media exceeds the configured size limit")
                    with target.open("wb") as handle:
                        for chunk in response.iter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise MediaArchiveError("media exceeds the configured size limit")
                            digest.update(chunk)
                            handle.write(chunk)
                    break
            else:
                raise MediaArchiveError("too many media redirects")
        if size <= 0:
            raise MediaArchiveError("media is empty")
        content_type, extension = _sniff(target)
        _validate_kind(kind, content_type)
        _validate_passive_content(target, content_type)
        prepared = PreparedMedia(
            path=target,
            size=size,
            sha256=digest.hexdigest(),
            content_type=content_type,
            extension=extension,
            original_name=str(original_name or "")[:255],
        )
        if bool(getattr(settings, "media_strip_metadata", True)):
            prepared = _strip_image_metadata(prepared)
        if prepared.size > limit:
            prepared.cleanup()
            raise MediaArchiveError("processed media exceeds the configured size limit")
        return prepared
    except Exception:
        target.unlink(missing_ok=True)
        raise
