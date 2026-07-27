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

    def do_POST(self) -> None:
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
    upstream_auth_mode: str = "provider-only",
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


class ProfileApkProviderDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store = bff_server.STORE
        bff_server.STORE = _Store()
        _LegacyProfileHandler.calls = 0

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    def test_profile_read_uses_apk_provider_without_native_lookup(self) -> None:
        request, raw_body = _request()
        with (
            patch.object(
                native_social_api,
                "dispatch_social_native",
                side_effect=AssertionError("profile read entered native dispatcher"),
            ),
            patch.object(web_api, "CapturingHandler", _LegacyProfileHandler),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_payload(response)["user"]["nickname"], "上游用户")
        self.assertEqual(_LegacyProfileHandler.calls, 1)

    def test_profile_read_rejects_retired_local_auth_mode(self) -> None:
        request, raw_body = _request(upstream_auth_mode="local-only")
        with (
            patch.object(
                native_social_api,
                "dispatch_social_native",
                side_effect=AssertionError("profile read entered native dispatcher"),
            ),
            patch.object(
                web_api,
                "CapturingHandler",
                side_effect=AssertionError("retired mode reached provider dispatch"),
            ),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(_payload(response)["code"], "UPSTREAM_AUTH_MODE_UNSUPPORTED")

    def test_profile_visit_write_uses_apk_provider(self) -> None:
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
                side_effect=AssertionError("profile visit entered native dispatcher"),
            ),
            patch.object(web_api, "CapturingHandler", _LegacyProfileHandler),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_LegacyProfileHandler.calls, 1)


if __name__ == "__main__":
    unittest.main()
