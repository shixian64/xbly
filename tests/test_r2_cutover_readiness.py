from __future__ import annotations

import hashlib
import io
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace

from bbw_prod import migration_readiness
from bbw_web.r2 import R2Storage


class FakeR2Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.operations: list[tuple[str, str]] = []
        self.fail_write = False
        self.head_failures = 0
        self.fail_get = False
        self.corrupt_get = False
        self.fail_delete = False
        self.keep_after_delete = False

    def upload_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> object:
        self.operations.append(("put", key))
        if self.fail_write:
            raise RuntimeError("provider detail must not escape")
        self.objects[key] = data
        return SimpleNamespace(
            key=key,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
            metadata=metadata,
        )

    def head_object(self, key: str) -> dict[str, object] | None:
        self.operations.append(("head", key))
        if self.head_failures:
            self.head_failures -= 1
            raise RuntimeError("provider detail must not escape")
        data = self.objects.get(key)
        if data is None:
            return None
        return {
            "size": len(data),
            "content_type": "application/octet-stream",
            "metadata": {"sha256": hashlib.sha256(data).hexdigest()},
        }

    def get_bytes(self, key: str, *, max_bytes: int) -> bytes | None:
        self.operations.append(("get", key))
        if self.fail_get:
            raise RuntimeError("provider detail must not escape")
        data = self.objects.get(key)
        if data is None:
            return None
        if len(data) > max_bytes:
            raise RuntimeError("provider detail must not escape")
        return b"corrupt" if self.corrupt_get else data

    def delete(self, key: str) -> None:
        self.operations.append(("delete", key))
        if self.fail_delete:
            raise RuntimeError("provider detail must not escape")
        if not self.keep_after_delete:
            self.objects.pop(key, None)


class R2CutoverCapabilityTests(unittest.TestCase):
    def test_probe_uses_random_isolated_keys_and_proves_full_lifecycle(self) -> None:
        storage = FakeR2Storage()

        first = migration_readiness.probe_r2_capabilities(storage=storage)
        second = migration_readiness.probe_r2_capabilities(storage=storage)

        self.assertEqual(first, migration_readiness.R2_CAPABILITY_READY)
        self.assertEqual(second, migration_readiness.R2_CAPABILITY_READY)
        self.assertEqual(storage.objects, {})
        put_keys = [key for operation, key in storage.operations if operation == "put"]
        self.assertEqual(len(put_keys), 2)
        self.assertNotEqual(put_keys[0], put_keys[1])
        self.assertTrue(
            all(
                key.startswith(
                    f"{migration_readiness.R2_CAPABILITY_PROBE_PREFIX}/"
                )
                for key in put_keys
            )
        )
        for key in put_keys:
            operations = [
                operation
                for operation, operation_key in storage.operations
                if operation_key == key
            ]
            self.assertEqual(operations, ["put", "head", "get", "delete", "head"])

    def test_get_content_mismatch_fails_read_but_still_confirms_delete(self) -> None:
        storage = FakeR2Storage()
        storage.corrupt_get = True

        result = migration_readiness.probe_r2_capabilities(storage=storage)

        self.assertTrue(result.write_ready)
        self.assertFalse(result.read_ready)
        self.assertTrue(result.delete_ready)
        self.assertFalse(result.ready)
        self.assertEqual(
            result.error_code,
            migration_readiness.R2_CAPABILITY_ERROR_GET_VERIFICATION,
        )
        self.assertEqual(storage.objects, {})

    def test_delete_must_be_confirmed_absent(self) -> None:
        storage = FakeR2Storage()
        storage.keep_after_delete = True

        result = migration_readiness.probe_r2_capabilities(storage=storage)

        self.assertTrue(result.write_ready)
        self.assertTrue(result.read_ready)
        self.assertFalse(result.delete_ready)
        self.assertEqual(
            result.error_code,
            migration_readiness.R2_CAPABILITY_ERROR_DELETE_VERIFICATION,
        )
        probe_key = next(iter(storage.objects))
        serialized = json.dumps(asdict(result), ensure_ascii=False)
        self.assertNotIn(probe_key, serialized)
        self.assertNotIn("provider detail", serialized)

    def test_write_and_head_failures_are_stable_codes_and_do_not_raise(self) -> None:
        write_storage = FakeR2Storage()
        write_storage.fail_write = True
        write_result = migration_readiness.probe_r2_capabilities(
            storage=write_storage
        )
        self.assertEqual(
            write_result.error_code,
            migration_readiness.R2_CAPABILITY_ERROR_WRITE,
        )
        self.assertFalse(write_result.write_ready)
        self.assertFalse(write_result.read_ready)
        self.assertFalse(write_result.delete_ready)

        head_storage = FakeR2Storage()
        head_storage.head_failures = 1
        head_result = migration_readiness.probe_r2_capabilities(storage=head_storage)
        self.assertTrue(head_result.write_ready)
        self.assertFalse(head_result.read_ready)
        self.assertTrue(head_result.delete_ready)
        self.assertEqual(
            head_result.error_code,
            migration_readiness.R2_CAPABILITY_ERROR_HEAD,
        )
        self.assertEqual(head_storage.objects, {})


class R2StorageBoundedGetTests(unittest.TestCase):
    def test_get_bytes_is_bounded_and_closes_response_body(self) -> None:
        body = io.BytesIO(b"probe-body")

        class Client:
            def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
                self.request = (Bucket, Key)
                return {"Body": body, "ContentLength": len(b"probe-body")}

        storage = object.__new__(R2Storage)
        storage.bucket = "private-test"
        storage.client = Client()

        result = storage.get_bytes("isolated/object", max_bytes=64)

        self.assertEqual(result, b"probe-body")
        self.assertTrue(body.closed)
        self.assertEqual(
            storage.client.request,
            ("private-test", "isolated/object"),
        )

    def test_get_bytes_rejects_declared_oversize_and_still_closes_body(self) -> None:
        body = io.BytesIO(b"oversize")

        class Client:
            def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
                return {"Body": body, "ContentLength": 1024}

        storage = object.__new__(R2Storage)
        storage.bucket = "private-test"
        storage.client = Client()

        with self.assertRaisesRegex(RuntimeError, "bounded download limit"):
            storage.get_bytes("isolated/object", max_bytes=8)

        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
