from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from bbw_protocol.client import ApiResult
from bbw_protocol.modules.im import ImAPI
from bbw_web import bff_server
from bbw_web import flash_photo as flash


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
            with self.assertRaisesRegex(flash.FlashPhotoError, "10MB"):
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


class FlashBffRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def _run(self, path, data, app, upload=None):
        web_user = SimpleNamespace(app=app)

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
            response = self._run("/api/im/flash/send", {"peer": "9"}, app, upload)

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
