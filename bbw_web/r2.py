"""Private Cloudflare R2 object storage client."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Optional

import boto3
from botocore.config import Config


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str
    content_type: str
    etag: str = ""


def _read_secret(value: str = "", file_path: str = "") -> str:
    if file_path:
        with open(file_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return str(value or "").strip()


class R2Storage:
    """Small S3-compatible wrapper that never enables public bucket access."""

    def __init__(self, settings: Any):
        self.bucket = str(getattr(settings, "r2_bucket", "") or "").strip()
        endpoint = str(getattr(settings, "r2_endpoint", "") or "").strip()
        access_key = _read_secret(
            str(getattr(settings, "r2_access_key", "") or ""),
            str(getattr(settings, "r2_access_key_file", "") or ""),
        )
        secret_key = _read_secret(
            str(getattr(settings, "r2_secret_key", "") or ""),
            str(getattr(settings, "r2_secret_key_file", "") or ""),
        )
        if not all((self.bucket, endpoint, access_key, secret_key)):
            raise RuntimeError("R2 endpoint, bucket and credentials are required")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=str(getattr(settings, "r2_region", "auto") or "auto"),
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 4, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
                max_pool_connections=10,
            ),
        )

    @staticmethod
    def object_key(user_id: str, extension: str = "bin", *, prefix: str = "media") -> str:
        safe_ext = "".join(ch for ch in str(extension).lower() if ch.isalnum())[:12] or "bin"
        date = time.strftime("%Y/%m/%d", time.gmtime())
        nonce = secrets.token_hex(20)
        return str(PurePosixPath(prefix) / str(user_id) / date / f"{nonce}.{safe_ext}")

    def upload_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Optional[dict[str, str]] = None,
    ) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentLength=len(data),
            ContentType=content_type or "application/octet-stream",
            CacheControl="private, max-age=300",
            Metadata={
                "sha256": digest,
                **{str(k): str(v)[:1024] for k, v in (metadata or {}).items()},
            },
        )
        return StoredObject(
            key=key,
            size=len(data),
            sha256=digest,
            content_type=content_type or "application/octet-stream",
            etag=str(response.get("ETag") or "").strip('"'),
        )

    def upload_stream(
        self,
        *,
        key: str,
        stream: BinaryIO,
        size: int,
        content_type: str,
        sha256: str,
        metadata: Optional[dict[str, str]] = None,
    ) -> StoredObject:
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=stream,
            ContentLength=int(size),
            ContentType=content_type or "application/octet-stream",
            CacheControl="private, max-age=300",
            Metadata={
                "sha256": sha256,
                **{str(k): str(v)[:1024] for k, v in (metadata or {}).items()},
            },
        )
        return StoredObject(
            key=key,
            size=int(size),
            sha256=sha256,
            content_type=content_type or "application/octet-stream",
            etag=str(response.get("ETag") or "").strip('"'),
        )

    def presigned_get(self, key: str, *, expires_seconds: int = 300) -> str:
        return str(
            self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=max(30, min(900, int(expires_seconds))),
            )
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def health(self) -> bool:
        self.client.head_bucket(Bucket=self.bucket)
        return True
