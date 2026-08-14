from __future__ import annotations

import json
import sys
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.requests import Request  # noqa: E402

from bbw_web import api as web_api  # noqa: E402
from bbw_web import archive_api, bff_server  # noqa: E402


class _Persistence:
    def __init__(self, *, auth_source: str = "provider") -> None:
        self.identity = SimpleNamespace(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
            auth_source=auth_source,
        )
        self.auth_source = auth_source
        self.policy_responses: list[dict[str, object]] = []
        self.product_responses: list[dict[str, object]] = []

    def revoke_non_provider_session(self, sid: str) -> bool:
        return sid == "test-session" and self.auth_source != "provider"

    def require_identity(self, sid: str) -> object | None:
        return self.identity if sid == "test-session" else None

    def rate_limit(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def can_message_peer(self, _identity: object, _peer: object) -> bool:
        return True

    def remember_message_policy_response(self, **kwargs: object) -> None:
        self.policy_responses.append(dict(kwargs))

    def capture_product_response(self, **kwargs: object) -> None:
        self.product_responses.append(dict(kwargs))


class _WebUser:
    authentication_source = "provider"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.conversation_message_peers: set[str] = set()
        self.match_message_peers: set[str] = set()


class _Store:
    def __init__(self) -> None:
        self.web_user = _WebUser()
        self.dropped: list[str] = []

    def get(self, sid: str | None) -> _WebUser | None:
        return self.web_user if sid == "test-session" else None

    def drop(self, sid: str) -> None:
        self.dropped.append(sid)


def _request(
    path: str,
    *,
    query: str = "",
    auth_source: str = "provider",
    upstream_auth_mode: str = "provider-only",
) -> tuple[Request, _Persistence]:
    persistence = _Persistence(auth_source=auth_source)
    application = SimpleNamespace(
        state=SimpleNamespace(
            persistence=persistence,
            settings=SimpleNamespace(
                max_request_body_bytes=1024 * 1024,
                trust_proxy_headers=False,
                cookie_secure=False,
                upstream_auth_mode=upstream_auth_mode,
            ),
        )
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", f"{bff_server.COOKIE_NAME}=test-session".encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": application,
        }
    )
    return request, persistence


def _payload(response: object) -> dict[str, object]:
    return json.loads(bytes(response.body).decode("utf-8"))


class _CapturedHandler:
    response_status = 200
    response_payload: dict[str, object] = {"ok": True, "items": [], "list": []}
    calls = 0
    kwargs: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        self.__class__.calls += 1
        self.__class__.kwargs = dict(kwargs)

    def do_GET(self) -> None:
        return None

    def finish_capture(self) -> tuple[int, list[tuple[str, str]], bytes]:
        body = json.dumps(self.response_payload, ensure_ascii=False).encode("utf-8")
        return (
            self.response_status,
            [("Content-Type", "application/json; charset=utf-8")],
            body,
        )


class CanonicalImReadDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store = bff_server.STORE
        self.store = _Store()
        bff_server.STORE = self.store
        _CapturedHandler.calls = 0
        _CapturedHandler.kwargs = {}

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    def test_main_im_reads_return_provider_results_without_archive_merge(self) -> None:
        cases = (
            (
                "/api/im/conversations",
                "page=1",
                {"ok": True, "items": [{"peer_id": "9", "source": "tim"}], "list": []},
            ),
            (
                "/api/im/messages",
                "peer=9",
                {"ok": True, "items": [{"id": "tim-1", "text": "上游消息"}], "list": []},
            ),
        )
        for path, query, provider_payload in cases:
            with self.subTest(path=path):
                request, _persistence = _request(path, query=query)

                class SuccessfulHandler(_CapturedHandler):
                    response_payload = provider_payload

                with patch.object(web_api, "CapturingHandler", SuccessfulHandler), patch.object(
                    archive_api,
                    "archived_conversations",
                    side_effect=AssertionError("main IM read queried archive"),
                ), patch.object(
                    archive_api,
                    "archived_messages",
                    side_effect=AssertionError("main IM read queried archive"),
                ):
                    response = web_api._legacy_dispatch_sync(request, b"")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(_payload(response), provider_payload)
                self.assertIsNone(SuccessfulHandler.kwargs["conversation_summary_loader"])

    def test_provider_failure_is_not_replaced_with_local_200(self) -> None:
        request, _persistence = _request("/api/im/conversations", query="page=1")

        class FailedHandler(_CapturedHandler):
            response_status = 502
            response_payload = {
                "ok": False,
                "code": "UPSTREAM_CONVERSATIONS_UNAVAILABLE",
                "items": [],
                "list": [],
            }

        with patch.object(web_api, "CapturingHandler", FailedHandler), patch.object(
            archive_api,
            "archived_conversations",
            side_effect=AssertionError("provider failure queried archive"),
        ):
            response = web_api._legacy_dispatch_sync(request, b"")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            _payload(response)["code"], "UPSTREAM_CONVERSATIONS_UNAVAILABLE"
        )

    def test_conversation_range_filter_preserves_captured_response_headers(self) -> None:
        request, _persistence = _request(
            "/api/im/conversations",
            query="page=1&since=100",
        )

        class SuccessfulHandler(_CapturedHandler):
            response_payload = {
                "ok": True,
                "items": [
                    {"peer_id": "9", "timestamp": 100},
                    {"peer_id": "10", "timestamp": 99},
                ],
                "list": [],
            }

            def finish_capture(self) -> tuple[int, list[tuple[str, str]], bytes]:
                body = json.dumps(self.response_payload).encode("utf-8")
                return (
                    200,
                    [
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body))),
                        ("Set-Cookie", f"{bff_server.COOKIE_NAME}=renewed; Path=/"),
                        ("X-Captured-Policy", "preserved"),
                    ],
                    body,
                )

        with patch.object(web_api, "CapturingHandler", SuccessfulHandler):
            response = web_api._legacy_dispatch_sync(request, b"")

        self.assertEqual(
            [item["peer_id"] for item in _payload(response)["items"]],
            ["9"],
        )
        self.assertEqual(_persistence.product_responses[0]["sid"], "renewed")
        raw_headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in response.raw_headers
        ]
        self.assertIn(
            ("Set-Cookie", f"{bff_server.COOKIE_NAME}=renewed; Path=/"),
            raw_headers,
        )
        self.assertIn(("X-Captured-Policy", "preserved"), raw_headers)
        content_lengths = [
            value for name, value in raw_headers if name.lower() == "content-length"
        ]
        self.assertEqual(content_lengths, [str(len(bytes(response.body)))])

    def test_provider_exception_is_not_hidden_by_archive_history(self) -> None:
        request, _persistence = _request("/api/im/messages", query="peer=9")

        class CrashedHandler(_CapturedHandler):
            def do_GET(self) -> None:
                raise RuntimeError("provider transport died")

        with patch.object(web_api, "CapturingHandler", CrashedHandler), patch.object(
            archive_api,
            "archived_messages",
            side_effect=AssertionError("provider exception queried archive"),
        ), patch.object(web_api.LOGGER, "exception"):
            response = web_api._legacy_dispatch_sync(request, b"")

        self.assertEqual(response.status_code, 500)
        self.assertFalse(_payload(response)["ok"])

    def test_historical_web_local_session_requires_provider_relogin(self) -> None:
        request, _persistence = _request(
            "/api/im/messages",
            query="peer=9",
            auth_source="web-local",
        )

        with patch.object(
            web_api,
            "CapturingHandler",
            side_effect=AssertionError("revoked local session reached provider"),
        ), patch.object(
            archive_api,
            "archived_messages",
            side_effect=AssertionError("revoked local session reached archive"),
        ):
            response = web_api._legacy_dispatch_sync(request, b"")

        payload = _payload(response)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(payload["code"], "PROVIDER_RELOGIN_REQUIRED")
        self.assertEqual(self.store.dropped, ["test-session"])

    def test_retired_local_only_configuration_is_rejected(self) -> None:
        request, _persistence = _request(
            "/api/im/conversations",
            upstream_auth_mode="local-only",
        )

        with patch.object(
            web_api,
            "CapturingHandler",
            side_effect=AssertionError("retired mode reached provider"),
        ):
            response = web_api._legacy_dispatch_sync(request, b"")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(_payload(response)["code"], "UPSTREAM_AUTH_MODE_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
