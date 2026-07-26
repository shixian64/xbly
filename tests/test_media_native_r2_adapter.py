from __future__ import annotations

import hashlib
import importlib.util
import io
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bbw_web.media_native import (
    MEDIA_KIND_FILE,
    MEDIA_KIND_IMAGE,
    STORAGE_ZONE_PRIVATE,
    STORAGE_ZONE_STAGING,
    MediaInspectionRejected,
    UploadIntent,
)


HAS_MEDIA_DEPENDENCIES = bool(
    importlib.util.find_spec("PIL") and importlib.util.find_spec("boto3")
)
if HAS_MEDIA_DEPENDENCIES:
    from PIL import Image

    from bbw_web.media_native.r2_adapter import R2PrivateMediaAdapter
else:  # pragma: no cover - exercised by dependency-minimal contract jobs.
    Image = None
    R2PrivateMediaAdapter = None


NOW = datetime.now(UTC)


class _Body(io.BytesIO):
    pass


class _FakeClient:
    def __init__(self, objects: dict[str, dict[str, object]]) -> None:
        self.objects = objects
        self.presigns: list[tuple[str, dict[str, object], int]] = []

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presigns.append((operation, dict(Params), int(ExpiresIn)))
        return f"https://private.invalid/{operation}/{Params['Key']}"

    def get_object(self, *, Bucket, Key):
        item = self.objects[Key]
        data = bytes(item["data"])
        return {"ContentLength": len(data), "Body": _Body(data)}

    def copy_object(self, *, Bucket, Key, CopySource, **values):
        source = self.objects[CopySource["Key"]]
        self.objects[Key] = {
            "data": bytes(source["data"]),
            "content_type": values["ContentType"],
            "metadata": dict(values["Metadata"]),
        }

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {
            "Contents": [{"Key": key} for key in keys[:MaxKeys]],
            "IsTruncated": False,
        }


class _FakeStorage:
    bucket = "private-test"

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.client = _FakeClient(self.objects)
        self.deleted: list[str] = []

    def head_object(self, key: str):
        item = self.objects.get(key)
        if item is None:
            return None
        return {
            "size": len(bytes(item["data"])),
            "content_type": item.get("content_type", "application/octet-stream"),
            "metadata": dict(item.get("metadata") or {}),
        }

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


def _png() -> bytes:
    output = io.BytesIO()
    assert Image is not None
    Image.new("RGB", (3, 2), color=(10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


@unittest.skipUnless(HAS_MEDIA_DEPENDENCIES, "Pillow/boto3 未安装")
class R2PrivateMediaAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = _FakeStorage()
        settings = SimpleNamespace(
            environment="test-blue",
            r2_presign_ttl_seconds=300,
        )
        assert R2PrivateMediaAdapter is not None
        self.adapter = R2PrivateMediaAdapter(settings, storage=self.storage)
        self.owner_id = uuid.uuid4()

    def _intent(self, data: bytes, *, kind: str, content_type: str) -> UploadIntent:
        digest = hashlib.sha256(data).hexdigest()
        grant = self.adapter.prepare_staging_upload(
            deployment="test-blue",
            intent_id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            kind=kind,
            content_type=content_type,
            size_bytes=len(data),
            sha256=digest,
            expires_at=NOW + timedelta(minutes=10),
        )
        self.storage.objects[grant.object_ref.key] = {
            "data": data,
            "content_type": content_type,
            "metadata": {"sha256": digest},
        }
        return UploadIntent(
            id=grant.intent_id,
            owner_user_id=self.owner_id,
            deployment="test-blue",
            kind=kind,
            filename="source.png" if kind == MEDIA_KIND_IMAGE else "note.txt",
            declared_content_type=content_type,
            expected_size_bytes=len(data),
            expected_sha256=digest,
            staging_object=grant.object_ref,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )

    def test_presigned_upload_is_bound_to_staging_namespace_and_headers(self) -> None:
        data = _png()
        intent = self._intent(
            data,
            kind=MEDIA_KIND_IMAGE,
            content_type="image/png",
        )

        self.assertEqual(intent.staging_object.zone, STORAGE_ZONE_STAGING)
        self.assertIn("/web-media-staging/", intent.staging_object.key)
        operation, params, ttl = self.storage.client.presigns[0]
        self.assertEqual(operation, "put_object")
        self.assertEqual(params["ContentType"], "image/png")
        self.assertEqual(params["Metadata"]["owner-user-id"], str(self.owner_id))
        self.assertLessEqual(ttl, 900)

    def test_real_image_is_decoded_promoted_and_read_through_short_grant(self) -> None:
        data = _png()
        intent = self._intent(
            data,
            kind=MEDIA_KIND_IMAGE,
            content_type="image/png",
        )
        inspected = self.adapter.inspect_staging(intent)

        self.assertTrue(inspected.verified)
        self.assertEqual(inspected.detected_content_type, "image/png")
        self.assertEqual((inspected.width, inspected.height), (3, 2))

        finalized = self.adapter.promote_verified(intent, inspected)
        self.assertEqual(finalized.object_ref.zone, STORAGE_ZONE_PRIVATE)
        self.assertIn("/web-media-private/", finalized.object_ref.key)
        self.assertNotEqual(
            finalized.object_ref.namespace,
            intent.staging_object.namespace,
        )
        self.assertIn(intent.staging_object.key, self.storage.deleted)

        from bbw_web.media_native import MediaAsset

        asset = MediaAsset(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            deployment="test-blue",
            kind=MEDIA_KIND_IMAGE,
            filename="source.png",
            content_type="image/png",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            object_ref=finalized.object_ref,
            created_at=NOW,
            width=3,
            height=2,
        )
        read = self.adapter.issue_private_read(
            asset,
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        self.assertIn("get_object", read.url)
        self.assertEqual(read.object_ref, finalized.object_ref)
        self.assertFalse(hasattr(asset, "url"))

    def test_active_file_content_is_rejected_even_when_declared_plain_text(self) -> None:
        intent = self._intent(
            b"<!doctype html><script>alert(1)</script>",
            kind=MEDIA_KIND_FILE,
            content_type="text/plain",
        )
        with self.assertRaises(MediaInspectionRejected):
            self.adapter.inspect_staging(intent)

    def test_expired_intent_cleanup_removes_staging_and_private_orphan_prefix(self) -> None:
        data = _png()
        intent = self._intent(
            data,
            kind=MEDIA_KIND_IMAGE,
            content_type="image/png",
        )
        private_prefix = (
            f"{self.adapter.private_namespace}/{self.owner_id}/{intent.id}/"
        )
        orphan_keys = {
            f"{private_prefix}first.png",
            f"{private_prefix}retry.png",
        }
        for key in orphan_keys:
            self.storage.objects[key] = {"data": data, "metadata": {}}
        unrelated = f"{self.adapter.private_namespace}/{self.owner_id}/{uuid.uuid4()}/keep.png"
        self.storage.objects[unrelated] = {"data": data, "metadata": {}}

        deleted = self.adapter.delete_upload_artifacts(intent)

        self.assertEqual(deleted, 3)
        self.assertNotIn(intent.staging_object.key, self.storage.objects)
        self.assertTrue(orphan_keys.isdisjoint(self.storage.objects))
        self.assertIn(unrelated, self.storage.objects)
        self.assertEqual(self.adapter.delete_upload_artifacts(intent), 1)


if __name__ == "__main__":
    unittest.main()
