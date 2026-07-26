from __future__ import annotations

import io
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from botocore.exceptions import ClientError

from bbw_protocol.client import ApiResult
from bbw_protocol.modules.im import ImAPI
from bbw_web import bff_server
from bbw_web import flash_photo as flash
from bbw_web import jobs
from bbw_web.persistence import RuntimePersistence


def multipart_body(boundary: str, image: bytes) -> bytes:
    return (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="peer"\r\n\r\n'
        "9\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="photo.png"\r\n'
        "Content-Type: text/html\r\n\r\n"
    ).encode() + image + f"\r\n--{boundary}--\r\n".encode()


class FlashMultipartTests(unittest.TestCase):
    def test_multipart_is_bounded_and_image_type_comes_from_magic(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 12
        body = multipart_body("demo-boundary", png)

        fields, upload = flash.parse_multipart(
            "multipart/form-data; boundary=demo-boundary",
            body,
        )
        info = flash.inspect_image(upload.data)

        self.assertEqual(fields, {"peer": "9"})
        self.assertEqual(upload.filename, "photo.png")
        self.assertEqual(upload.content_type, "text/html")
        self.assertEqual(info.content_type, "image/png")

    def test_executable_or_oversize_gif_is_rejected(self) -> None:
        with self.assertRaisesRegex(flash.FlashPhotoError, "JPEG"):
            flash.inspect_image(b"<svg onload=alert(1)>")
        with patch.object(flash, "MAX_FLASH_GIF_BYTES", 8):
            with self.assertRaisesRegex(flash.FlashPhotoError, "20MB"):
                flash.inspect_image(b"GIF89a123")


class FlashOssTests(unittest.TestCase):
    def test_oss_signature_stays_server_side_and_put_is_integrity_signed(self) -> None:
        canonical_values = []
        requests = []
        auth = "OSS LTAItestAccessKey:YWJjZGVmZ2hpamtsbW5vcA=="

        class FakeIm:
            def aliyun_signature(self, canonical):
                canonical_values.append(canonical)
                return ApiResult(True, 200, auth, data=auth, kind="text")

        class FakeResponse:
            status = 200
            headers = {"ETag": '"abc"'}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                return b""

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        jpeg = b"\xff\xd8\xff\xe0demo"
        with patch.object(flash.time, "time", return_value=1_700_000_000.0), patch.object(
            flash.time, "strftime", return_value="202607"
        ), patch.object(flash.secrets, "randbelow", return_value=7):
            uploaded = flash.upload_image(FakeIm(), jpeg, opener=opener)

        self.assertEqual(uploaded["path"], "images/202607/1700000000007.jpg")
        self.assertEqual(uploaded["url"], "https://oss.banghua.xin/images/202607/1700000000007.jpg")
        self.assertNotIn("authorization", uploaded)
        self.assertIn("PUT\n", canonical_values[0])
        self.assertIn("/newecs/images/202607/1700000000007.jpg", canonical_values[0])
        header_map = {key.lower(): value for key, value in requests[0][0].header_items()}
        self.assertEqual(header_map["authorization"], auth)
        self.assertTrue(header_map["content-md5"])
        self.assertNotIn(auth, requests[0][0].full_url)

    def test_get_result_accepts_active_and_legacy_shapes_only_on_known_oss(self) -> None:
        active = ApiResult(
            True,
            200,
            "",
            data={"code": "200", "message": "images/202607/a.jpg"},
        )
        legacy = ApiResult(
            True,
            200,
            "",
            data={
                "json_obj": {
                    "photourl": "https://newecs.oss-cn-shanghai.aliyuncs.com/images/202607/b.gif"
                }
            },
        )
        untrusted = ApiResult(
            True,
            200,
            "",
            data={"photourl": "https://example.invalid/steal.jpg"},
        )

        self.assertEqual(flash.result_photo(active)["url"], "https://oss.banghua.xin/images/202607/a.jpg")
        self.assertEqual(flash.result_photo(legacy)["path"], "images/202607/b.gif")
        self.assertEqual(flash.result_photo(untrusted)["url"], "")


class FlashProtocolTests(unittest.TestCase):
    def test_protocol_uses_exact_flash_fields_and_i99999_get_signer(self) -> None:
        calls = []

        class FakeClient:
            def call(self, action, params=None, **kwargs):
                calls.append(("call", action, dict(params or {}), kwargs))
                return ApiResult(True, 200, "true", data=True)

            def request(self, url, body=None, *, method="POST", **_kwargs):
                calls.append(("request", url, body, method))
                return ApiResult(True, 200, "OSS x:y", data="OSS x:y")

        api = ImAPI(FakeClient())
        api.flash_photo_send(target_id="9", photo_url="images/202607/a.jpg")
        api.flash_photo_get(uniqueid="unique-1")
        api.aliyun_signature("PUT\ncanonical/value")

        self.assertEqual(
            calls[0][2],
            {"targetId": "9", "photourl": "images/202607/a.jpg"},
        )
        self.assertEqual(calls[1][2], {"uniqueid": "unique-1"})
        signer_url = calls[2][1]
        query = parse_qs(urlparse(signer_url).query)
        self.assertEqual(query["i"], ["99999"])
        self.assertEqual(query["content"], ["PUT\ncanonical/value"])
        self.assertEqual(calls[2][3], "GET")


class MediaArchiveFailureTests(unittest.TestCase):
    def test_r2_access_denied_is_treated_as_configuration_failure(self) -> None:
        error = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Access Denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "PutObject",
        )

        self.assertTrue(jobs._media_storage_configuration_error(error))
        self.assertFalse(jobs._permanent_media_error(error))

    def test_missing_r2_bucket_is_treated_as_configuration_failure(self) -> None:
        error = ClientError(
            {
                "Error": {"Code": "NoSuchBucket", "Message": "Bucket does not exist"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "PutObject",
        )

        self.assertTrue(jobs._media_storage_configuration_error(error))
        self.assertFalse(jobs._permanent_media_error(error))

    def test_r2_initialization_configuration_failures_are_suppressed(self) -> None:
        missing_settings = RuntimeError(
            "R2 endpoint, bucket and credentials are required"
        )
        invalid_endpoint = ValueError("Invalid endpoint: not-a-url")
        invalid_encoding = UnicodeDecodeError(
            "utf-8",
            b"\xff",
            0,
            1,
            "invalid start byte",
        )
        unreadable_credentials = PermissionError(
            13,
            "Permission denied",
            "/run/secrets/r2-access-key",
        )

        self.assertTrue(
            jobs._media_storage_configuration_error(
                missing_settings,
                during_r2_initialization=True,
            )
        )
        self.assertTrue(
            jobs._media_storage_configuration_error(
                unreadable_credentials,
                during_r2_initialization=True,
            )
        )
        self.assertTrue(
            jobs._media_storage_configuration_error(
                invalid_endpoint,
                during_r2_initialization=True,
            )
        )
        self.assertTrue(
            jobs._media_storage_configuration_error(
                invalid_encoding,
                during_r2_initialization=True,
            )
        )
        self.assertFalse(
            jobs._media_storage_configuration_error(
                OSError("temporary media file write failed"),
                during_r2_initialization=False,
            )
        )

    def test_configuration_failures_remain_retryable_and_alert_every_eight_attempts(self) -> None:
        row = SimpleNamespace(
            status="processing",
            attempt_count=0,
            max_attempts=jobs.MEDIA_MAX_ATTEMPTS,
            available_at=None,
            locked_by="worker",
            locked_until=jobs.utcnow(),
            last_error=None,
        )
        db = SimpleNamespace(scalar=lambda _statement: row)
        outbox_id = uuid.uuid4()
        alerts = []

        with patch.object(jobs, "session_scope") as session_scope:
            session_scope.return_value.__enter__.return_value = db
            for attempt in range(1, jobs.MEDIA_MAX_ATTEMPTS + 1):
                row.status = "processing"
                row.attempt_count = attempt
                alerts.append(
                    jobs._defer_outbox_configuration_error(
                        outbox_id,
                        RuntimeError("R2 configuration is invalid"),
                    )
                )

        self.assertEqual(
            alerts,
            [False] * (jobs.MEDIA_MAX_ATTEMPTS - 1) + [True],
        )
        self.assertEqual(row.status, "retry")
        self.assertEqual(row.attempt_count, jobs.MEDIA_MAX_ATTEMPTS)
        self.assertEqual(row.max_attempts, jobs.MEDIA_MAX_ATTEMPTS * 2)
        self.assertIsNone(row.locked_by)
        self.assertIsNone(row.locked_until)
        self.assertIn("configuration is invalid", row.last_error)

    def test_historical_configuration_failures_recover_only_after_write_delete_probe(self) -> None:
        settings = SimpleNamespace(redis_prefix="bbw")

        class FakeRedis:
            def __init__(self):
                self.values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, **_kwargs):
                self.values[key] = value
                return True

        connection = FakeRedis()
        storage = SimpleNamespace(verify_write_delete=lambda: True)
        with (
            patch.object(jobs, "_has_failed_media_configuration_outboxes", return_value=True),
            patch.object(jobs, "R2Storage", return_value=storage),
            patch.object(
                jobs,
                "_recover_failed_media_configuration_outboxes",
                return_value=20,
            ) as recover,
        ):
            recovered = jobs._maybe_recover_failed_media_configuration_outboxes(
                settings,
                connection,
            )

        self.assertEqual(recovered, 20)
        recover.assert_called_once_with(limit=20)
        self.assertEqual(connection.values["bbw:media:r2-write-delete-ready"], "1")

    def test_failed_write_delete_probe_keeps_historical_failures_paused(self) -> None:
        settings = SimpleNamespace(redis_prefix="bbw")

        class FakeRedis:
            def get(self, _key):
                return None

            def set(self, _key, _value, **_kwargs):
                return True

        storage = SimpleNamespace(
            verify_write_delete=lambda: (_ for _ in ()).throw(
                RuntimeError("write denied")
            )
        )
        with (
            patch.object(jobs, "_has_failed_media_configuration_outboxes", return_value=True),
            patch.object(jobs, "R2Storage", return_value=storage),
            patch.object(
                jobs,
                "_recover_failed_media_configuration_outboxes",
            ) as recover,
        ):
            recovered = jobs._maybe_recover_failed_media_configuration_outboxes(
                settings,
                FakeRedis(),
            )

        self.assertEqual(recovered, 0)
        recover.assert_not_called()


class FlashRevealPersistenceTests(unittest.TestCase):
    def test_cached_reveal_checks_acknowledged_and_reads_pending_atomically(self) -> None:
        calls = []

        class FakeRedis:
            def eval(self, script, key_count, *values):
                calls.append((script, key_count, values))
                return b"images/202607/atomic.png"

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        runtime.redis = FakeRedis()
        runtime._flash_revealed_key = lambda _uid, _identifier: "acknowledged"
        runtime._flash_reveal_key = lambda _uid, _identifier: "pending"

        result = runtime.cached_flash_reveal("42", "flash-1")

        self.assertEqual(result, "images/202607/atomic.png")
        self.assertEqual(calls[0][1:], (2, ("acknowledged", "pending")))
        self.assertIn("redis.call('EXISTS', KEYS[1])", calls[0][0])
        self.assertIn("redis.call('GET', KEYS[2])", calls[0][0])

    def test_ack_retry_is_successful_after_the_first_response_is_lost(self) -> None:
        runtime = RuntimePersistence.__new__(RuntimePersistence)
        runtime.redis = SimpleNamespace(eval=lambda *_args: 1)
        runtime._flash_reveal_key = lambda _uid, _identifier: "pending"
        runtime._flash_revealed_key = lambda _uid, _identifier: "acknowledged"

        self.assertTrue(runtime.acknowledge_flash_reveal("42", "flash-1"))


class FlashBffRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        self.old_presence_backend = bff_server.PRESENCE_BACKEND
        bff_server.STORE = SimpleNamespace()
        bff_server.PRESENCE_BACKEND = None

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store
        bff_server.PRESENCE_BACKEND = self.old_presence_backend

    def _run(self, path, data, app, upload=None, permission=None, match_peers=None):
        values = {
            "app": app,
            "blocked_message_peers": set(),
            "blocked_by_message_peers": set(),
            "blocked_message_peers_snapshot_at": time.monotonic(),
            "blocked_by_message_peers_snapshot_at": time.monotonic(),
        }
        if permission is not None:
            values["match_pool_online_list_enabled"] = bool(permission)
        if match_peers is not None:
            values["match_message_peers"] = set(match_peers)
        web_user = SimpleNamespace(**values)

        class Harness:
            def __init__(self):
                self.path = path
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return data

            def flash_multipart_body(self):
                return data, upload

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def _allow_sensitive_action(self, *_args, **_kwargs):
                return True

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return harness.response

    def test_send_uploads_then_calls_business_api_with_relative_path(self) -> None:
        calls = []
        result = ApiResult(
            True,
            200,
            '{"code":"200"}',
            data={"code": "200", "uniqueid": "flash-1"},
            code="200",
        )
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            im=SimpleNamespace(
                flash_photo_send=lambda **kwargs: calls.append(kwargs) or result,
            ),
        )
        upload = flash.UploadPart("x.jpg", "image/jpeg", b"\xff\xd8\xff\xe0x")
        uploaded = {
            "path": "images/202607/a.jpg",
            "url": "https://oss.banghua.xin/images/202607/a.jpg",
            "content_type": "image/jpeg",
            "size": 5,
        }
        with patch.object(flash, "upload_image", return_value=uploaded):
            response = self._run(
                "/api/im/flash/send",
                {"peer": "9"},
                app,
                upload,
                permission=False,
                match_peers={"9"},
            )

        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["url"], uploaded["url"])
        self.assertEqual(
            calls,
            [{"target_id": "9", "photo_url": "images/202607/a.jpg"}],
        )

    def test_get_returns_only_normalized_url(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"code":"200"}',
            data={"code": "200", "message": "images/202607/a.png"},
            code="200",
        )
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            im=SimpleNamespace(flash_photo_get=lambda **_kwargs: result),
        )

        response = self._run("/api/im/flash/get", {"uniqueid": "flash-1"}, app)

        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["path"], "images/202607/a.png")
        self.assertEqual(response[1]["url"], "https://oss.banghua.xin/images/202607/a.png")
        self.assertNotIn("data", response[1])

    def test_get_is_cached_until_browser_acknowledges_display(self) -> None:
        calls = []

        class FlashRevealBackend:
            def __init__(self):
                self.pending = {}
                self.acknowledged = set()

            def cached_flash_reveal(self, uid, unique_id):
                return self.pending.get((uid, unique_id), "")

            def remember_flash_reveal(self, uid, unique_id, path):
                self.pending[(uid, unique_id)] = path
                return True

            def flash_reveal_acknowledged(self, uid, unique_id):
                return (uid, unique_id) in self.acknowledged

            def acknowledge_flash_reveal(self, uid, unique_id):
                key = (uid, unique_id)
                if key not in self.pending:
                    return False
                self.pending.pop(key, None)
                self.acknowledged.add(key)
                return True

        result = ApiResult(
            True,
            200,
            '{"code":"200"}',
            data={"code": "200", "message": "images/202607/cached.png"},
            code="200",
        )
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            im=SimpleNamespace(
                flash_photo_get=lambda **kwargs: calls.append(kwargs) or result,
            ),
        )
        bff_server.PRESENCE_BACKEND = FlashRevealBackend()

        first = self._run("/api/im/flash/get", {"uniqueid": "flash-1"}, app)
        second = self._run("/api/im/flash/get", {"uniqueid": "flash-1"}, app)
        acknowledged = self._run("/api/im/flash/ack", {"uniqueid": "flash-1"}, app)
        after_ack = self._run("/api/im/flash/get", {"uniqueid": "flash-1"}, app)

        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertTrue(second[1]["cached"])
        self.assertEqual(calls, [{"uniqueid": "flash-1"}])
        self.assertTrue(acknowledged[1]["acknowledged"])
        self.assertEqual(after_ack[0], 410)
        self.assertEqual(after_ack[1]["photo_status"], "acknowledged")

    def test_get_does_not_return_photo_when_ack_wins_cache_race(self) -> None:
        class FlashRevealBackend:
            def __init__(self):
                self.acknowledged = False

            def cached_flash_reveal(self, _uid, _unique_id):
                return ""

            def remember_flash_reveal(self, _uid, _unique_id, _path):
                self.acknowledged = True
                return False

            def flash_reveal_acknowledged(self, _uid, _unique_id):
                return self.acknowledged

        result = ApiResult(
            True,
            200,
            '{"code":"200"}',
            data={"code": "200", "message": "images/202607/raced.png"},
            code="200",
        )
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            im=SimpleNamespace(flash_photo_get=lambda **_kwargs: result),
        )
        bff_server.PRESENCE_BACKEND = FlashRevealBackend()

        response = self._run("/api/im/flash/get", {"uniqueid": "flash-race"}, app)

        self.assertEqual(response[0], 410)
        self.assertFalse(response[1]["ok"])
        self.assertEqual(response[1]["photo_status"], "acknowledged")
        self.assertNotIn("path", response[1])
        self.assertNotIn("url", response[1])
        self.assertNotIn("photo_url", response[1])

    def test_pending_flash_acknowledgements_sync_across_tabs(self) -> None:
        app_js = (
            Path(__file__).resolve().parents[1] / "bbw_web" / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn('window.addEventListener("storage"', app_js)
        self.assertIn("syncFlashAckAccount({ forceReload: true })", app_js)
        self.assertIn("event.key?.startsWith(FLASH_ACK_STORAGE_PREFIX)", app_js)
        self.assertNotIn("if (!storedIds.has(id)) S.flashAckPending.delete(id)", app_js)

    def test_flash_send_is_denied_before_upload_without_permission_or_match(self) -> None:
        app = SimpleNamespace(session=SimpleNamespace(uid="42"), im=SimpleNamespace())
        upload = flash.UploadPart("x.jpg", "image/jpeg", b"\xff\xd8\xff\xe0x")

        with patch.object(flash, "upload_image") as upload_image:
            response = self._run(
                "/api/im/flash/send",
                {"peer": "9"},
                app,
                upload,
                permission=False,
                match_peers=set(),
            )

        self.assertEqual(response[0], 403)
        self.assertEqual(response[1]["code"], "PRIVATE_MESSAGE_PERMISSION_REQUIRED")
        upload_image.assert_not_called()

    def test_rest_fallback_rejects_non_text_without_sending(self) -> None:
        app = SimpleNamespace(session=SimpleNamespace(uid="42"))
        response = self._run(
            "/api/im/rest/send",
            {"peer": "9", "message_type": "image", "text": "not-a-file"},
            app,
        )
        self.assertEqual(response[0], 409)
        self.assertEqual(response[1]["code"], "MEDIA_REQUIRES_TIM_SDK")


if __name__ == "__main__":
    unittest.main()
