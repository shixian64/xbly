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
    def __init__(self, *, auth_source: str = "provider", allowed: bool = True) -> None:
        self.identity = SimpleNamespace(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
            auth_source=auth_source,
        )
        self.allowed = allowed
        self.policy_responses: list[dict[str, object]] = []
        self.product_responses: list[dict[str, object]] = []

    def require_identity(self, sid: str) -> object | None:
        return self.identity if sid == "test-session" else None

    def rate_limit(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def can_message_peer(self, _identity: object, _peer: object) -> bool:
        return self.allowed

    def conversation_summary_map(
        self, _identity: object, _peers: list[str]
    ) -> dict[str, dict[str, object]]:
        return {}

    def remember_message_policy_response(self, **kwargs: object) -> None:
        self.policy_responses.append(dict(kwargs))

    def capture_product_response(self, **kwargs: object) -> None:
        self.product_responses.append(dict(kwargs))


class _WebUser:
    def __init__(self, authentication_source: str) -> None:
        self.authentication_source = authentication_source
        self.lock = threading.RLock()
        self.conversation_message_peers: set[str] = set()


class _Store:
    def __init__(self, web_user: _WebUser) -> None:
        self.web_user = web_user

    def get(self, sid: str | None) -> _WebUser | None:
        return self.web_user if sid == "test-session" else None

    def drop(self, _sid: str) -> None:
        return None


def _request(
    path: str,
    *,
    query: str = "",
    auth_source: str = "provider",
    upstream_auth_mode: str = "provider-first",
    allowed: bool = True,
) -> tuple[Request, _Persistence]:
    persistence = _Persistence(auth_source=auth_source, allowed=allowed)
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

    def __init__(self, **_kwargs: object) -> None:
        self.__class__.calls += 1

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

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    def test_local_only_conversations_never_construct_the_provider_handler(self) -> None:
        request, persistence = _request(
            "/api/im/conversations",
            query="page=1",
            upstream_auth_mode="local-only",
        )
        bff_server.STORE = _Store(_WebUser("provider"))
        local = {
            "ok": True,
            "items": [{"peer_id": "9", "provider": "web-local"}],
            "list": [{"peer_id": "9", "provider": "web-local"}],
            "count": 1,
        }

        with patch.object(
            web_api, "CapturingHandler", side_effect=AssertionError("provider contacted")
        ), patch.object(archive_api, "archived_conversations", return_value=local):
            response = web_api._legacy_dispatch_sync(request, b"")

        payload = _payload(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["items"][0]["peer_id"], "9")
        self.assertFalse(payload["upstream_contacted"])
        self.assertEqual(payload["dependency_mode"], "web-local")
        self.assertEqual(len(persistence.policy_responses), 1)

    def test_web_local_session_bypasses_provider_in_provider_first_mode(self) -> None:
        request, _persistence = _request(
            "/api/im/messages",
            query="peer=9&before=1784995200",
            auth_source="web-local",
        )
        bff_server.STORE = _Store(_WebUser("local"))
        observed: dict[str, object] = {}

        def archived_messages(_request: Request, **kwargs: object) -> dict[str, object]:
            observed.update(kwargs)
            return {
                "ok": True,
                "items": [{"id": "local-1", "timestamp": "2026-07-25T00:00:00+00:00"}],
                "list": [],
                "count": 1,
                "has_more": False,
            }

        with patch.object(
            web_api, "CapturingHandler", side_effect=AssertionError("provider contacted")
        ), patch.object(archive_api, "archived_messages", side_effect=archived_messages):
            response = web_api._legacy_dispatch_sync(request, b"")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_payload(response)["items"][0]["id"], "local-1")
        self.assertEqual(observed["peer"], "9")
        self.assertEqual(int(observed["before"].timestamp()), 1784995200)

    def test_provider_failure_returns_local_200_instead_of_502(self) -> None:
        request, _persistence = _request("/api/im/conversations", query="page=1")
        bff_server.STORE = _Store(_WebUser("provider"))

        class FailedHandler(_CapturedHandler):
            response_status = 502
            response_payload = {
                "ok": False,
                "code": "UPSTREAM_CONVERSATIONS_UNAVAILABLE",
                "items": [],
                "list": [],
            }

        local = {
            "ok": True,
            "items": [{"peer_id": "9", "provider": "web-local"}],
            "list": [],
            "count": 1,
        }
        with patch.object(web_api, "CapturingHandler", FailedHandler), patch.object(
            archive_api, "archived_conversations", return_value=local
        ):
            response = web_api._legacy_dispatch_sync(request, b"")

        payload = _payload(response)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["upstream_unavailable"])
        self.assertEqual(payload["items"][0]["peer_id"], "9")

    def test_provider_exception_still_returns_local_history(self) -> None:
        request, _persistence = _request("/api/im/messages", query="peer=9")
        bff_server.STORE = _Store(_WebUser("provider"))

        class CrashedHandler(_CapturedHandler):
            def do_GET(self) -> None:
                raise RuntimeError("provider transport died")

        local = {
            "ok": True,
            "items": [{"id": "local-after-crash", "timestamp": 1784995200}],
            "list": [],
            "count": 1,
            "has_more": False,
        }
        with patch.object(web_api, "CapturingHandler", CrashedHandler), patch.object(
            archive_api, "archived_messages", return_value=local
        ):
            response = web_api._legacy_dispatch_sync(request, b"")

        payload = _payload(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["items"][0]["id"], "local-after-crash")
        self.assertTrue(payload["upstream_unavailable"])

    def test_provider_history_merges_with_canonical_media_projection(self) -> None:
        request, _persistence = _request("/api/im/messages", query="peer=9")
        bff_server.STORE = _Store(_WebUser("provider"))

        class SuccessfulHandler(_CapturedHandler):
            response_payload = {
                "ok": True,
                "items": [
                    {
                        "id": "mirror-id",
                        "canonical_message_id": "canonical-1",
                        "from": "42",
                        "to": "9",
                        "flow": "out",
                        "text": "",
                        "kind": "image",
                        "media": {"url": "https://tim.invalid/old"},
                        "timestamp": 1784995200,
                    },
                    {
                        "id": "legacy-only",
                        "from": "9",
                        "to": "42",
                        "flow": "in",
                        "text": "旧历史",
                        "timestamp": 1784995100,
                    },
                ],
                "list": [],
                "count": 2,
            }

        local = {
            "ok": True,
            "items": [
                {
                    "id": "local-id",
                    "canonical_message_id": "canonical-1",
                    "provider": "web-local",
                    "source": "archive",
                    "from": "42",
                    "to": "9",
                    "flow": "out",
                    "text": "本地图片",
                    "body": "本地图片",
                    "kind": "image",
                    "message_type": "image",
                    "media": {"url": "https://r2.example/native-image"},
                    "timestamp": "2026-07-25T00:00:00+00:00",
                }
            ],
            "list": [],
            "count": 1,
            "has_more": False,
        }
        with patch.object(web_api, "CapturingHandler", SuccessfulHandler), patch.object(
            archive_api, "archived_messages", return_value=local
        ):
            response = web_api._legacy_dispatch_sync(request, b"")

        payload = _payload(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(payload["items"]), 2)
        canonical = next(
            item for item in payload["items"] if item.get("canonical_message_id") == "canonical-1"
        )
        self.assertEqual(canonical["provider"], "web-local")
        self.assertEqual(canonical["text"], "本地图片")
        self.assertEqual(canonical["media"]["url"], "https://r2.example/native-image")
        self.assertIn("旧历史", [item.get("text") for item in payload["items"]])

    def test_local_message_read_cannot_grant_itself_from_browser_query(self) -> None:
        request, _persistence = _request(
            "/api/im/messages",
            query="peer=untrusted-browser-peer",
            auth_source="web-local",
            allowed=False,
        )
        bff_server.STORE = _Store(_WebUser("local"))
        with patch.object(
            web_api, "CapturingHandler", side_effect=AssertionError("provider contacted")
        ), patch.object(archive_api, "archived_messages") as archive_read:
            response = web_api._legacy_dispatch_sync(request, b"")

        payload = _payload(response)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(payload["code"], "PRIVATE_MESSAGE_PERMISSION_REQUIRED")
        archive_read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
