from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import inspect
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bbw_web import native_media_api as api
from bbw_web.media_native import MediaQuotaExceeded, MediaTooLarge


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _Persistence:
    def __init__(self, identity: SimpleNamespace | None) -> None:
        self.identity = identity
        self.allowed = True
        self.message_allowed = True
        self.rate_calls: list[tuple[str, int, int]] = []
        self.message_policy_calls: list[tuple[object, object]] = []

    def require_identity(self, sid: str):
        if sid != "valid-session":
            return None
        return self.identity

    def rate_limit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        self.rate_calls.append((key, limit, window_seconds))
        return self.allowed

    def can_message_peer(self, identity: object, peer: object) -> bool:
        self.message_policy_calls.append((identity, peer))
        return self.message_allowed


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.intent_id = uuid.uuid4()
        self.asset_id = uuid.uuid4()
        self.attachment_id = uuid.uuid4()
        self.message_id = uuid.uuid4()
        self.delivery_id = uuid.uuid4()
        self.payload = SimpleNamespace(
            asset_id=self.asset_id,
            kind="image",
            filename="photo.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            sha256="ab" * 32,
            duration_seconds=None,
            width=800,
            height=600,
            flash=False,
        )
        self.attachment = SimpleNamespace(
            id=self.attachment_id,
            message_id=self.message_id,
            client_message_id="browser-message-1",
            sender_upstream_uid="42",
            recipient_upstream_uid="9",
            sent_at=NOW,
            revoked_at=NOW,
            payload=self.payload,
        )

    def create_upload_intent(self, **values):
        self.calls.append(("create_upload_intent", values))
        return SimpleNamespace(
            intent=SimpleNamespace(id=self.intent_id, kind="image"),
            target=SimpleNamespace(
                method="PUT",
                upload_url="https://upload.invalid/private?signature=redacted",
                required_headers=(("content-type", "image/jpeg"),),
                expires_at=NOW + timedelta(minutes=15),
            ),
        )

    def complete_upload(self, **values):
        self.calls.append(("complete_upload", values))
        return SimpleNamespace(
            id=self.asset_id,
            kind="image",
            filename="photo.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            sha256="ab" * 32,
            duration_seconds=None,
            width=800,
            height=600,
            created_at=NOW,
        )

    def send_attachment(self, **values):
        self.calls.append(("send_attachment", values))
        return SimpleNamespace(
            attachment=self.attachment,
            created=True,
            tim_mirror=SimpleNamespace(delivery_id=self.delivery_id),
        )

    def request_access(self, **values):
        self.calls.append(("request_access", values))
        return SimpleNamespace(
            attachment_id=self.attachment_id,
            asset_id=self.asset_id,
            url="https://read.invalid/private?signature=redacted",
            expires_at=NOW + timedelta(minutes=5),
            content_type="image/jpeg",
            size_bytes=1024,
        )

    def revoke_attachment(self, **values):
        self.calls.append(("revoke_attachment", values))
        return SimpleNamespace(
            attachment=self.attachment,
            created=True,
            tim_mirror=SimpleNamespace(delivery_id=self.delivery_id),
        )

    def claim_flash(self, **values):
        self.calls.append(("claim_flash", values))
        access = self.request_access(**values)
        return SimpleNamespace(
            access=access,
            claim=SimpleNamespace(
                claimed_at=NOW,
                display_until=NOW + timedelta(seconds=5),
            ),
        )


class NativeMediaApiContracts(unittest.TestCase):
    def test_quota_exhaustion_uses_stable_client_error(self) -> None:
        error = api._error(MediaQuotaExceeded("用户媒体存储额度不足"))
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.detail["code"], "media_quota_exceeded")

    def setUp(self) -> None:
        self.identity = SimpleNamespace(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        self.persistence = _Persistence(self.identity)
        self.service = _Service()
        self.app = FastAPI()
        self.app.state.persistence = self.persistence
        self.app.state.settings = SimpleNamespace(user_cookie_name="bbw_sid")
        self.app.include_router(api.router)

    @contextmanager
    def service_context(self, _request):
        yield object(), self.service

    def test_router_exposes_exact_six_route_surface(self) -> None:
        registered = {
            (method, route.path)
            for route in api.router.routes
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(
            registered,
            {
                ("POST", "/api/im/media/uploads"),
                ("POST", "/api/im/media/uploads/{intent_id}/complete"),
                ("POST", "/api/im/media/messages"),
                ("GET", "/api/im/media/attachments/{attachment_id}/access"),
                ("POST", "/api/im/media/attachments/{attachment_id}/revoke"),
                ("POST", "/api/im/media/attachments/{attachment_id}/claim"),
            },
        )

    def test_http_module_has_no_tim_or_legacy_protocol_dependency(self) -> None:
        source = inspect.getsource(api)
        self.assertNotIn("bbw_protocol", source)
        self.assertNotIn("legacy_tim_rest", source)
        self.assertNotIn("create_legacy_tim_rest_transport", source)
        self.assertNotIn("TimRestClient", source)

    def test_all_request_paths_use_only_local_service(self) -> None:
        headers = {"Origin": "http://testserver"}
        cookies = {"bbw_sid": "valid-session"}
        attachment_id = str(self.service.attachment_id)
        intent_id = str(self.service.intent_id)
        requests = (
            (
                "post",
                "/api/im/media/uploads",
                {
                    "kind": "image",
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "size_bytes": 1024,
                    "sha256": "ab" * 32,
                },
            ),
            ("post", f"/api/im/media/uploads/{intent_id}/complete", None),
            (
                "post",
                "/api/im/media/messages",
                {
                    "to": "9",
                    "asset_id": str(self.service.asset_id),
                    "client_message_id": "browser-message-1",
                },
            ),
            ("get", f"/api/im/media/attachments/{attachment_id}/access", None),
            ("post", f"/api/im/media/attachments/{attachment_id}/revoke", None),
            ("post", f"/api/im/media/attachments/{attachment_id}/claim", None),
        )
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            responses = []
            for method, path, body in requests:
                kwargs = {"cookies": cookies}
                if method == "post":
                    kwargs["headers"] = headers
                if body is not None:
                    kwargs["json"] = body
                responses.append(getattr(client, method)(path, **kwargs))

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(
            [name for name, _values in self.service.calls],
            [
                "create_upload_intent",
                "complete_upload",
                "send_attachment",
                "request_access",
                "revoke_attachment",
                "claim_flash",
                "request_access",
            ],
        )
        send_payload = responses[2].json()
        self.assertEqual(send_payload["source"], "web-local")
        self.assertEqual(send_payload["provider"], "web-local")
        self.assertFalse(send_payload["compatibility_sync"]["required"])
        self.assertEqual(send_payload["compatibility_sync"]["status"], "pending")
        self.assertEqual(
            self.persistence.message_policy_calls,
            [(self.identity, "9")],
        )
        revoke_payload = responses[4].json()
        self.assertFalse(revoke_payload["compatibility_sync"]["required"])

    def test_media_send_requires_the_same_private_message_authorization(self) -> None:
        self.persistence.message_allowed = False
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            response = client.post(
                "/api/im/media/messages",
                headers={"Origin": "http://testserver"},
                cookies={"bbw_sid": "valid-session"},
                json={
                    "to": "9",
                    "asset_id": str(self.service.asset_id),
                    "client_message_id": "browser-message-1",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "当前账号无权向该用户发送私聊媒体"},
        )
        self.assertEqual(self.service.calls, [])

    def test_cross_site_write_is_rejected_before_auth_rate_limit_and_service(self) -> None:
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            response = client.post(
                "/api/im/media/uploads",
                headers={"Origin": "https://attacker.invalid"},
                cookies={"bbw_sid": "valid-session"},
                json={},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "跨站请求已拒绝")
        self.assertEqual(self.persistence.rate_calls, [])
        self.assertEqual(self.service.calls, [])

    def test_missing_session_and_rate_limit_have_stable_fastapi_errors(self) -> None:
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            missing = client.get(
                f"/api/im/media/attachments/{self.service.attachment_id}/access"
            )
            self.persistence.allowed = False
            limited = client.get(
                f"/api/im/media/attachments/{self.service.attachment_id}/access",
                cookies={"bbw_sid": "valid-session"},
            )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json(), {"detail": "请先登录"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json(), {"detail": "媒体操作过于频繁"})
        self.assertEqual(self.service.calls, [])

    def test_domain_and_path_validation_errors_keep_fastapi_detail_shape(self) -> None:
        def reject_upload(**_values):
            raise MediaTooLarge("图片最大 20 MiB")

        self.service.create_upload_intent = reject_upload
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            domain_error = client.post(
                "/api/im/media/uploads",
                headers={"Origin": "http://testserver"},
                cookies={"bbw_sid": "valid-session"},
                json={"kind": "image"},
            )
            path_error = client.post(
                "/api/im/media/uploads/not-a-uuid/complete",
                headers={"Origin": "http://testserver"},
                cookies={"bbw_sid": "valid-session"},
            )

        self.assertEqual(domain_error.status_code, 400)
        self.assertEqual(
            domain_error.json()["detail"],
            {
                "code": "media_too_large",
                "message": "图片最大 20 MiB",
                "retryable": False,
            },
        )
        self.assertEqual(path_error.status_code, 422)
        self.assertIsInstance(path_error.json()["detail"], list)


if __name__ == "__main__":
    unittest.main()
