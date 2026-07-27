from __future__ import annotations

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from bbw_web import api as web_api
from bbw_web import bff_server
from bbw_web import native_social_api


class _Persistence:
    def __init__(self) -> None:
        self.identity = SimpleNamespace(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
            account_provider="beibeiwu",
            auth_source="provider",
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
        )
        self.captured: list[dict[str, object]] = []

    def require_identity(self, sid: str) -> object | None:
        return self.identity if sid == "test-session" else None

    def rate_limit(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def capture_product_response(self, **kwargs: object) -> None:
        self.captured.append(dict(kwargs))


class _Store:
    def __init__(self) -> None:
        self.web_user = object()

    def get(self, sid: str | None) -> object | None:
        return self.web_user if sid == "test-session" else None

    def drop(self, _sid: str) -> None:
        return None


class _LegacyProfileHandler:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        type(self).calls += 1

    def do_GET(self) -> None:
        return None

    def finish_capture(self) -> tuple[int, list[tuple[str, str]], bytes]:
        user = {"id": "9", "uid": "9", "nickname": "上游用户"}
        body = json.dumps(
            {"ok": True, "user": user, "items": [user], "list": [user]},
            ensure_ascii=False,
        ).encode("utf-8")
        return (
            200,
            [("Content-Type", "application/json; charset=utf-8")],
            body,
        )


def _request(
    *,
    method: str = "GET",
    path: str = "/api/profile/user",
    query: str = "uid=9",
    upstream_auth_mode: str = "provider-first",
    body: dict[str, object] | None = None,
) -> tuple[Request, bytes]:
    raw_body = json.dumps(body or {}).encode("utf-8") if method == "POST" else b""
    headers = [
        (b"host", b"testserver"),
        (b"cookie", f"{bff_server.COOKIE_NAME}=test-session".encode("ascii")),
    ]
    if method == "POST":
        headers.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw_body)).encode("ascii")),
            ]
        )
    application = SimpleNamespace(
        state=SimpleNamespace(
            persistence=_Persistence(),
            settings=SimpleNamespace(
                max_request_body_bytes=1024 * 1024,
                trust_proxy_headers=False,
                cookie_secure=False,
                upstream_auth_mode=upstream_auth_mode,
            ),
        )
    )
    return (
        Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": query.encode("ascii"),
                "headers": headers,
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": application,
            }
        ),
        raw_body,
    )


def _payload(response: object) -> dict[str, object]:
    return json.loads(bytes(response.body).decode("utf-8"))


class ProfileLegacyFallbackDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store = bff_server.STORE
        bff_server.STORE = _Store()
        _LegacyProfileHandler.calls = 0

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    @staticmethod
    def _unavailable(*, fallback: bool) -> native_social_api.NativeSocialResponse:
        return native_social_api.NativeSocialResponse(
            status=404,
            payload={
                "ok": False,
                "code": "SOCIAL_TARGET_UNAVAILABLE",
                "error": "目标账号不存在、未迁移或已停用",
                "source": "web-local",
            },
            legacy_read_fallback_allowed=fallback,
        )

    def test_provider_first_unmigrated_profile_uses_legacy_read(self) -> None:
        request, raw_body = _request()
        with (
            patch.object(
                native_social_api,
                "dispatch_social_native",
                return_value=self._unavailable(fallback=True),
            ),
            patch.object(web_api, "CapturingHandler", _LegacyProfileHandler),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_payload(response)["user"]["nickname"], "上游用户")
        self.assertEqual(_LegacyProfileHandler.calls, 1)

    def test_local_only_never_uses_legacy_profile_read(self) -> None:
        request, raw_body = _request(upstream_auth_mode="local-only")
        with (
            patch.object(
                native_social_api,
                "dispatch_social_native",
                return_value=self._unavailable(fallback=True),
            ),
            patch.object(
                web_api,
                "CapturingHandler",
                side_effect=AssertionError("local-only contacted provider"),
            ),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(_payload(response)["code"], "SOCIAL_TARGET_UNAVAILABLE")

    def test_inactive_or_migrated_target_never_uses_legacy_profile_read(self) -> None:
        cases = (
            self._unavailable(fallback=False),
            native_social_api.NativeSocialResponse(
                status=200,
                payload={
                    "ok": True,
                    "user": {"uid": "9", "nickname": "本地用户"},
                    "source": "web-local",
                },
            ),
        )
        for native_response in cases:
            with self.subTest(status=native_response.status):
                request, raw_body = _request()
                with (
                    patch.object(
                        native_social_api,
                        "dispatch_social_native",
                        return_value=native_response,
                    ),
                    patch.object(
                        web_api,
                        "CapturingHandler",
                        side_effect=AssertionError("provider must not be contacted"),
                    ),
                ):
                    response = web_api._legacy_dispatch_sync(request, raw_body)

                self.assertEqual(response.status_code, native_response.status)

    def test_write_path_ignores_legacy_read_fallback_marker(self) -> None:
        request, raw_body = _request(
            method="POST",
            path="/api/social/visit",
            query="",
            body={"uid": "9", "operation_id": "visit-one"},
        )
        with (
            patch.object(
                native_social_api,
                "dispatch_social_native",
                return_value=self._unavailable(fallback=True),
            ),
            patch.object(
                web_api,
                "CapturingHandler",
                side_effect=AssertionError("write path contacted provider"),
            ),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(_payload(response)["code"], "SOCIAL_TARGET_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
