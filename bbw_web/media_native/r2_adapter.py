"""Cloudflare R2 adapter for Web-local private messaging media.

The adapter deliberately keeps staging and durable objects in separate
namespaces, downloads the staged object for bounded content inspection, and
returns short-lived grants only.  No presigned URL is persisted by this
module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from bbw_web.media_archive import MediaArchiveError, _sniff, _validate_passive_content
from bbw_web.r2 import R2Storage

from .contracts import (
    MEDIA_KIND_AUDIO,
    MEDIA_KIND_FILE,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    STORAGE_ZONE_PRIVATE,
    STORAGE_ZONE_STAGING,
    FinalizedMediaObject,
    MediaAsset,
    MediaContractViolation,
    MediaInspectionRejected,
    MediaStorageAdapter,
    PresignedMediaRead,
    StagedMediaInspection,
    StagingUploadTarget,
    StorageObjectRef,
    UploadIntent,
)


LOGGER = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 40_000_000

_CHUNK_BYTES = 1024 * 1024
_MAX_PRESIGN_SECONDS = 900
_IMAGE_FORMAT_TYPES = {
    "AVIF": "image/avif",
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MediaContractViolation(f"{field} 必须是带时区时间")
    return value.astimezone(UTC)


def _safe_segment(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if (
        not text
        or len(text) > 64
        or text.startswith(('.', '-'))
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in text)
    ):
        raise MediaContractViolation(f"{field} 不合法")
    return text


def _suffix_for_content_type(content_type: str) -> str:
    return {
        "application/pdf": "pdf",
        "application/zip": "zip",
        "audio/aac": "aac",
        "audio/flac": "flac",
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/webm": "webm",
        "audio/x-m4a": "m4a",
        "image/avif": "avif",
        "image/gif": "gif",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "text/plain": "txt",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/webm": "webm",
    }.get(content_type, "bin")


class R2PrivateMediaAdapter(MediaStorageAdapter):
    """R2-backed staging, inspection, promotion and private-read grants."""

    def __init__(
        self,
        settings: Any,
        *,
        storage: R2Storage | None = None,
        deployment: str | None = None,
    ) -> None:
        self.storage = storage or R2Storage(settings)
        self.client = self.storage.client
        self.bucket = self.storage.bucket
        self.deployment = _safe_segment(
            deployment or getattr(settings, "environment", "development"),
            "deployment",
        )
        self.presign_ttl_seconds = max(
            30,
            min(
                _MAX_PRESIGN_SECONDS,
                int(getattr(settings, "r2_presign_ttl_seconds", 300) or 300),
            ),
        )
        self.staging_namespace = f"{self.deployment}/web-media-staging"
        self.private_namespace = f"{self.deployment}/web-media-private"

    def _object_ref(
        self,
        *,
        zone: str,
        key: str,
        owner_user_id: uuid.UUID,
    ) -> StorageObjectRef:
        namespace = (
            self.staging_namespace
            if zone == STORAGE_ZONE_STAGING
            else self.private_namespace
        )
        return StorageObjectRef(
            deployment=self.deployment,
            zone=zone,
            bucket=self.bucket,
            namespace=namespace,
            key=key,
            owner_user_id=owner_user_id,
        )

    def _require_ref(self, ref: StorageObjectRef, *, zone: str) -> None:
        expected_namespace = (
            self.staging_namespace
            if zone == STORAGE_ZONE_STAGING
            else self.private_namespace
        )
        if (
            ref.deployment != self.deployment
            or ref.zone != zone
            or ref.bucket != self.bucket
            or ref.namespace != expected_namespace
            or not ref.key.startswith(f"{expected_namespace}/")
        ):
            raise MediaContractViolation("R2 对象引用越过 deployment 或存储区边界")

    def prepare_staging_upload(
        self,
        *,
        deployment: str,
        intent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        kind: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        expires_at: datetime,
    ) -> StagingUploadTarget:
        if _safe_segment(deployment, "deployment") != self.deployment:
            raise MediaContractViolation("上传意图不属于当前 deployment")
        expires = _aware(expires_at, "expires_at")
        now = datetime.now(UTC)
        ttl = max(1, min(_MAX_PRESIGN_SECONDS, int((expires - now).total_seconds())))
        key = (
            f"{self.staging_namespace}/{owner_user_id}/{intent_id}/"
            f"source.{_suffix_for_content_type(content_type)}"
        )
        metadata = {
            "deployment": self.deployment,
            "intent-id": str(intent_id),
            "kind": str(kind),
            "owner-user-id": str(owner_user_id),
            "sha256": str(sha256),
            "size-bytes": str(int(size_bytes)),
        }
        url = str(
            self.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": content_type,
                    "CacheControl": "private, no-store",
                    "Metadata": metadata,
                },
                ExpiresIn=ttl,
            )
        )
        if not url:
            raise MediaContractViolation("R2 未生成上传授权")
        required_headers = (
            ("cache-control", "private, no-store"),
            ("content-type", content_type),
            *((f"x-amz-meta-{key_name}", value) for key_name, value in metadata.items()),
        )
        return StagingUploadTarget(
            intent_id=intent_id,
            object_ref=self._object_ref(
                zone=STORAGE_ZONE_STAGING,
                key=key,
                owner_user_id=owner_user_id,
            ),
            upload_url=url,
            expires_at=min(expires, now + timedelta(seconds=ttl)),
            required_headers=tuple(required_headers),
        )

    def _download_staging(self, intent: UploadIntent) -> Path:
        self._require_ref(intent.staging_object, zone=STORAGE_ZONE_STAGING)
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=intent.staging_object.key,
        )
        declared = int(response.get("ContentLength") or 0)
        if declared <= 0 or declared > intent.expected_size_bytes:
            response.get("Body").close()
            raise MediaInspectionRejected("staging 对象大小与上传意图不一致")
        fd, raw_name = tempfile.mkstemp(prefix="bbw-web-media-", suffix=".part")
        os.close(fd)
        target = Path(raw_name)
        size = 0
        try:
            body = response["Body"]
            with target.open("wb") as handle:
                while True:
                    chunk = body.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > intent.expected_size_bytes:
                        raise MediaInspectionRejected("staging 对象超过声明大小")
                    handle.write(chunk)
            body.close()
            if size != intent.expected_size_bytes:
                raise MediaInspectionRejected("staging 对象大小与上传意图不一致")
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _digest(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _inspect_image(path: Path) -> tuple[str, int, int]:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = str(image.format or "").upper()
                if image_format not in _IMAGE_FORMAT_TYPES:
                    raise MediaInspectionRejected("图片格式不受支持")
                image.load()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
            raise MediaInspectionRejected("图片无法安全解码") from exc
        if width <= 0 or height <= 0:
            raise MediaInspectionRejected("图片尺寸不合法")
        return _IMAGE_FORMAT_TYPES[image_format], int(width), int(height)

    @staticmethod
    def _probe_av(path: Path, expected_kind: str, declared_type: str) -> tuple[str, float, int | None, int | None]:
        binary = shutil.which("ffprobe")
        if not binary:
            raise MediaInspectionRejected("服务器缺少音视频检测组件")
        command = [
            binary,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,duration:format=duration,format_name",
            "-of",
            "json",
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaInspectionRejected("音视频检测失败") from exc
        if result.returncode != 0:
            raise MediaInspectionRejected("音视频容器无法解码")
        try:
            payload = json.loads(result.stdout or "{}")
            streams = list(payload.get("streams") or [])
            format_data = dict(payload.get("format") or {})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaInspectionRejected("音视频检测结果无效") from exc
        video_streams = [item for item in streams if item.get("codec_type") == "video"]
        audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
        if expected_kind == MEDIA_KIND_AUDIO and (not audio_streams or video_streams):
            raise MediaInspectionRejected("真实对象不是纯音频")
        if expected_kind == MEDIA_KIND_VIDEO and not video_streams:
            raise MediaInspectionRejected("真实对象不是视频")
        duration_values: list[float] = []
        for raw in [format_data.get("duration"), *(item.get("duration") for item in streams)]:
            try:
                value = float(raw or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                duration_values.append(value)
        if not duration_values:
            raise MediaInspectionRejected("音视频时长不可用")
        width: int | None = None
        height: int | None = None
        if video_streams:
            width = int(video_streams[0].get("width") or 0)
            height = int(video_streams[0].get("height") or 0)
            if width <= 0 or height <= 0:
                raise MediaInspectionRejected("视频尺寸不可用")
        # ffprobe confirms the real container/streams; retain the caller's
        # accepted MIME spelling for ambiguous MP4/M4A and Ogg containers.
        return declared_type, max(duration_values), width, height

    def inspect_staging(self, intent: UploadIntent) -> StagedMediaInspection:
        path = self._download_staging(intent)
        try:
            size, digest = self._digest(path)
            if size != intent.expected_size_bytes or digest != intent.expected_sha256:
                raise MediaInspectionRejected("staging 对象摘要与上传意图不一致")
            duration: float | None = None
            width: int | None = None
            height: int | None = None
            if intent.kind == MEDIA_KIND_IMAGE:
                detected_type, width, height = self._inspect_image(path)
            elif intent.kind in {MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO}:
                detected_type, duration, width, height = self._probe_av(
                    path,
                    intent.kind,
                    intent.declared_content_type,
                )
            elif intent.kind == MEDIA_KIND_FILE:
                try:
                    detected_type, _extension = _sniff(path)
                    _validate_passive_content(path, detected_type)
                except MediaArchiveError as exc:
                    raise MediaInspectionRejected("文件内容未通过安全检测") from exc
            else:
                raise MediaInspectionRejected("媒体类别不受支持")
            return StagedMediaInspection(
                object_ref=intent.staging_object,
                verified=True,
                detected_kind=intent.kind,
                detected_content_type=detected_type,
                size_bytes=size,
                sha256=digest,
                duration_seconds=duration,
                width=width,
                height=height,
            )
        finally:
            path.unlink(missing_ok=True)

    def promote_verified(
        self,
        intent: UploadIntent,
        inspection: StagedMediaInspection,
    ) -> FinalizedMediaObject:
        self._require_ref(intent.staging_object, zone=STORAGE_ZONE_STAGING)
        if inspection.object_ref != intent.staging_object or not inspection.verified:
            raise MediaContractViolation("只能提升已验证的当前 staging 对象")
        suffix = _suffix_for_content_type(inspection.detected_content_type)
        key = (
            f"{self.private_namespace}/{intent.owner_user_id}/{intent.id}/"
            f"{inspection.sha256}.{suffix}"
        )
        metadata = {
            "deployment": self.deployment,
            "intent-id": str(intent.id),
            "kind": intent.kind,
            "owner-user-id": str(intent.owner_user_id),
            "sha256": inspection.sha256,
            "size-bytes": str(inspection.size_bytes),
        }
        existing = self.storage.head_object(key)
        if existing is None:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=key,
                CopySource={"Bucket": self.bucket, "Key": intent.staging_object.key},
                CacheControl="private, no-store",
                ContentType=inspection.detected_content_type,
                Metadata=metadata,
                MetadataDirective="REPLACE",
            )
            existing = self.storage.head_object(key)
        if (
            existing is None
            or int(existing.get("size") or -1) != inspection.size_bytes
            or str((existing.get("metadata") or {}).get("sha256") or "")
            != inspection.sha256
        ):
            raise MediaContractViolation("R2 private 对象校验失败")
        try:
            self.storage.delete(intent.staging_object.key)
        except Exception:
            LOGGER.warning(
                "staging object cleanup failed after successful promotion",
                exc_info=True,
            )
        return FinalizedMediaObject(
            object_ref=self._object_ref(
                zone=STORAGE_ZONE_PRIVATE,
                key=key,
                owner_user_id=intent.owner_user_id,
            ),
            content_type=inspection.detected_content_type,
            size_bytes=inspection.size_bytes,
            sha256=inspection.sha256,
            duration_seconds=inspection.duration_seconds,
            width=inspection.width,
            height=inspection.height,
        )

    def _private_intent_prefix(self, intent: UploadIntent) -> str:
        if intent.deployment != self.deployment:
            raise MediaContractViolation("上传意图不属于当前 deployment")
        return (
            f"{self.private_namespace}/{intent.owner_user_id}/{intent.id}/"
        )

    def delete_upload_artifacts(self, intent: UploadIntent) -> int:
        """Delete expired staging and any deterministic promotion orphan.

        ``promote_verified`` writes below an intent-specific private prefix
        before the database transaction records the asset.  If that transaction
        fails, the pending intent remains the durable discovery record and this
        prefix makes cleanup deterministic without listing another user's data.
        Every delete is idempotent so a database failure after R2 cleanup can be
        retried safely.
        """

        self._require_ref(intent.staging_object, zone=STORAGE_ZONE_STAGING)
        prefix = self._private_intent_prefix(intent)
        deleted = 0
        self.storage.delete(intent.staging_object.key)
        deleted += 1
        continuation = ""
        while True:
            values: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation:
                values["ContinuationToken"] = continuation
            response = self.client.list_objects_v2(**values)
            for item in response.get("Contents") or ():
                key = str(item.get("Key") or "")
                if not key.startswith(prefix):
                    raise MediaContractViolation(
                        "R2 private 清理结果越过 intent 隔离前缀"
                    )
                self.storage.delete(key)
                deleted += 1
            if not response.get("IsTruncated"):
                break
            continuation = str(response.get("NextContinuationToken") or "")
            if not continuation:
                raise MediaContractViolation("R2 private 清理分页游标缺失")
        return deleted

    def delete_private_asset(self, asset: MediaAsset) -> None:
        self._require_ref(asset.object_ref, zone=STORAGE_ZONE_PRIVATE)
        if asset.deployment != self.deployment:
            raise MediaContractViolation("媒体 asset 不属于当前 deployment")
        self.storage.delete(asset.object_ref.key)

    def issue_private_read(
        self,
        asset: MediaAsset,
        *,
        expires_at: datetime,
    ) -> PresignedMediaRead:
        self._require_ref(asset.object_ref, zone=STORAGE_ZONE_PRIVATE)
        expires = _aware(expires_at, "expires_at")
        ttl = max(
            1,
            min(
                self.presign_ttl_seconds,
                _MAX_PRESIGN_SECONDS,
                int((expires - datetime.now(UTC)).total_seconds()),
            ),
        )
        disposition = "attachment" if asset.kind == MEDIA_KIND_FILE else "inline"
        url = str(
            self.client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": asset.object_ref.key,
                    "ResponseCacheControl": "private, no-store",
                    "ResponseContentDisposition": disposition,
                    "ResponseContentType": asset.content_type,
                },
                ExpiresIn=ttl,
            )
        )
        if not url:
            raise MediaContractViolation("R2 未生成私有读取授权")
        return PresignedMediaRead(
            object_ref=asset.object_ref,
            url=url,
            expires_at=expires,
        )


__all__ = ["R2PrivateMediaAdapter"]
