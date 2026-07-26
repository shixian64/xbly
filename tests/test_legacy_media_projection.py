from __future__ import annotations

from contextlib import contextmanager
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bbw_web import media_api
from bbw_web.archive_api import _archived_message_item
from bbw_web.legacy_media_reference import (
    legacy_source_hash,
    projected_message_media,
    projected_profile_avatar,
    projected_social_post_media,
)
from bbw_web.media_api import _load_media


class LegacyMediaProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.media_id = uuid.UUID("11111111-1111-4111-8111-111111111111")

    def _sidecar(self, slot: str, source: str) -> dict[str, object]:
        return {
            "schema": 1,
            "items": [
                {
                    "slot": slot,
                    "media_id": str(self.media_id),
                    "source_hash": legacy_source_hash(source),
                }
            ],
        }

    def test_profile_projection_requires_current_source_hash(self) -> None:
        source = "https://legacy.example/avatar.jpg"
        profile = {
            "avatar": source,
            "_local_media": self._sidecar("avatar", source),
        }
        expected = f"/api/media/{self.media_id}/content"
        self.assertEqual(projected_profile_avatar(profile), expected)

        profile["avatar"] = "https://legacy.example/new-avatar.jpg"
        self.assertEqual(projected_profile_avatar(profile), profile["avatar"])

    def test_post_projection_replaces_only_matching_slots(self) -> None:
        first = "https://legacy.example/first.jpg"
        second = "https://legacy.example/second.jpg"
        video = "https://legacy.example/video.mp4"
        media = {
            "pictures": [first, second],
            "video": video,
            "_local_media": {
                "schema": 1,
                "items": [
                    {
                        "slot": "pictures[0]",
                        "media_id": str(self.media_id),
                        "source_hash": legacy_source_hash(first),
                    },
                    {
                        "slot": "video",
                        "media_id": "22222222-2222-4222-8222-222222222222",
                        "source_hash": legacy_source_hash(video),
                    },
                ],
            },
        }
        projected = projected_social_post_media(media)
        self.assertEqual(
            projected["pictures"],
            [f"/api/media/{self.media_id}/content", second],
        )
        self.assertEqual(
            projected["video"],
            "/api/media/22222222-2222-4222-8222-222222222222/content",
        )

    def test_message_projection_and_archive_output_use_local_content_path(self) -> None:
        source = "https://legacy.example/file.bin"
        metadata = {
            "media_report": {"url": source, "name": "file.bin"},
            "_local_media": self._sidecar("media_report.url", source),
        }
        self.assertEqual(
            projected_message_media(metadata)["url"],
            f"/api/media/{self.media_id}/content",
        )
        message = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="legacy-message",
            extra_data=metadata,
            message_type="file",
            direction="incoming",
            body="",
            sender_upstream_uid="peer",
            recipient_upstream_uid="self",
            status="received",
            occurred_at=datetime(2026, 7, 25, tzinfo=UTC),
            provider="tim",
        )
        item = _archived_message_item(message)
        self.assertEqual(
            item["media"]["url"],
            f"/api/media/{self.media_id}/content",
        )

    def test_received_flash_still_hides_archived_addresses(self) -> None:
        source = "https://legacy.example/flash.jpg"
        message = SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="legacy-flash",
            extra_data={
                "media_report": {"url": source, "thumbnail": source},
                "_local_media": {
                    "schema": 1,
                    "items": [
                        {
                            "slot": field,
                            "media_id": str(self.media_id),
                            "source_hash": legacy_source_hash(source),
                        }
                        for field in (
                            "media_report.url",
                            "media_report.thumbnail",
                        )
                    ],
                },
            },
            message_type="flash",
            direction="incoming",
            body="",
            sender_upstream_uid="peer",
            recipient_upstream_uid="self",
            status="received",
            occurred_at=datetime(2026, 7, 25, tzinfo=UTC),
            provider="tim",
        )
        item = _archived_message_item(message)
        self.assertNotIn("url", item["media"])
        self.assertNotIn("thumbnail", item["media"])

    def test_frontend_keeps_local_private_media_paths_on_current_origin(self) -> None:
        source = Path("bbw_web/static/app.js").read_text(encoding="utf-8")
        self.assertIn("LOCAL_PRIVATE_MEDIA_PATH_RE", source)
        self.assertIn("if (LOCAL_PRIVATE_MEDIA_PATH_RE.test(raw)) return raw;", source)

    def _media_object(
        self,
        *,
        owner_user_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
        slot: str,
        source: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.media_id,
            owner_user_id=owner_user_id,
            status="available",
            deleted_at=None,
            retention_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            message_id=None,
            extra_data={
                "legacy_media": {
                    "schema": 1,
                    "source_hash": legacy_source_hash(source),
                    "slot": slot,
                    "resource_type": resource_type,
                    "resource_id": str(resource_id),
                    "access_scope": resource_type,
                }
            },
        )

    def test_shared_profile_media_requires_current_canonical_reference(self) -> None:
        owner_id = uuid.uuid4()
        viewer_id = uuid.uuid4()
        source = "https://legacy.example/avatar.jpg"
        media = self._media_object(
            owner_user_id=owner_id,
            resource_type="profile",
            resource_id=owner_id,
            slot="avatar",
            source=source,
        )
        user = SimpleNamespace(
            profile={
                "avatar": source,
                "_local_media": self._sidecar("avatar", source),
            }
        )

        class Database:
            def __init__(self, rows: list[object]) -> None:
                self.rows = iter(rows)

            def scalar(self, _statement: object) -> object:
                return next(self.rows)

        identity = SimpleNamespace(user_id=viewer_id, upstream_uid="viewer")
        self.assertIs(
            _load_media(
                Database([media, user]),
                media_id=self.media_id,
                identity=identity,
                allow_shared=True,
            ),
            media,
        )

        stale_user = SimpleNamespace(profile={"avatar": "https://legacy.example/new.jpg"})
        with self.assertRaises(HTTPException) as raised:
            _load_media(
                Database([media, stale_user]),
                media_id=self.media_id,
                identity=identity,
                allow_shared=True,
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_owner_content_projection_also_requires_current_reference(self) -> None:
        owner_id = uuid.uuid4()
        source = "https://legacy.example/avatar.jpg"
        media = self._media_object(
            owner_user_id=owner_id,
            resource_type="profile",
            resource_id=owner_id,
            slot="avatar",
            source=source,
        )
        stale_user = SimpleNamespace(
            profile={"avatar": "https://legacy.example/replaced.jpg"}
        )

        class Database:
            def __init__(self) -> None:
                self.rows = iter([media, stale_user])

            def scalar(self, _statement: object) -> object:
                return next(self.rows)

        identity = SimpleNamespace(user_id=owner_id, upstream_uid="owner")
        with self.assertRaises(HTTPException) as raised:
            _load_media(
                Database(),
                media_id=self.media_id,
                identity=identity,
                allow_shared=True,
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_message_metadata_revocation_blocks_private_media(self) -> None:
        owner_id = uuid.uuid4()
        message_id = uuid.uuid4()
        source = "https://legacy.example/file.bin"
        media = self._media_object(
            owner_user_id=owner_id,
            resource_type="message",
            resource_id=message_id,
            slot="media_report.url",
            source=source,
        )
        media.message_id = message_id
        message = SimpleNamespace(
            id=message_id,
            owner_user_id=owner_id,
            status="received",
            extra_data={
                "revoked": True,
                "media_report": {"url": source},
                "_local_media": self._sidecar("media_report.url", source),
            },
        )

        class Database:
            def __init__(self) -> None:
                self.rows = iter([media, message])

            def scalar(self, _statement: object) -> object:
                return next(self.rows)

        with self.assertRaises(HTTPException) as raised:
            _load_media(
                Database(),
                media_id=self.media_id,
                identity=SimpleNamespace(
                    user_id=owner_id,
                    upstream_uid="owner",
                ),
                allow_shared=True,
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_social_post_media_reuses_canonical_visibility_policy(self) -> None:
        owner_id = uuid.uuid4()
        viewer_id = uuid.uuid4()
        post_id = uuid.uuid4()
        source = "https://legacy.example/post.jpg"
        media = self._media_object(
            owner_user_id=owner_id,
            resource_type="social_post",
            resource_id=post_id,
            slot="pictures[0]",
            source=source,
        )
        post = SimpleNamespace(
            id=post_id,
            author_user_id=owner_id,
            author_upstream_uid="owner",
            status="published",
            visibility="followers",
            media={
                "pictures": [source],
                "_local_media": self._sidecar("pictures[0]", source),
            },
        )

        class Database:
            def __init__(self) -> None:
                self.rows = iter([media, post])

            def scalar(self, _statement: object) -> object:
                return next(self.rows)

        identity = SimpleNamespace(user_id=viewer_id, upstream_uid="viewer")
        with patch(
            "bbw_web.media_api.SqlAlchemySocialPermissionPolicy.can_view",
            return_value=True,
        ) as permission:
            self.assertIs(
                _load_media(
                    Database(),
                    media_id=self.media_id,
                    identity=identity,
                    allow_shared=True,
                ),
                media,
            )
        permission.assert_called_once()

        with patch(
            "bbw_web.media_api.SqlAlchemySocialPermissionPolicy.can_view",
            return_value=False,
        ), self.assertRaises(HTTPException) as raised:
            _load_media(
                Database(),
                media_id=self.media_id,
                identity=identity,
                allow_shared=True,
            )
        self.assertEqual(raised.exception.status_code, 404)


class _RouteStorage:
    def __init__(self) -> None:
        self.bucket = "private-bucket"
        self.calls: list[tuple[str, int]] = []
        self.error: Exception | None = None

    def presigned_get(self, key: str, *, expires_seconds: int) -> str:
        self.calls.append((key, expires_seconds))
        if self.error is not None:
            raise self.error
        return "https://r2.invalid/private?signature=secret"


class _RoutePersistence:
    def __init__(self, identity: SimpleNamespace, storage: _RouteStorage) -> None:
        self.identity = identity
        self.storage = storage
        self.allowed = True
        self.captured: list[dict[str, object]] = []
        self.settings = SimpleNamespace(r2_presign_ttl_seconds=300)

    def require_identity(self, sid: str):
        return self.identity if sid == "valid-session" else None

    def rate_limit(self, _key: str, *, limit: int, window_seconds: int) -> bool:
        return self.allowed

    def get_r2_storage(self) -> _RouteStorage:
        return self.storage

    def capture_product_response(self, **values: object) -> None:
        self.captured.append(dict(values))


class _RouteDatabase:
    def __init__(self, rows: list[object]) -> None:
        self.rows = iter(rows)

    def scalar(self, _statement: object) -> object:
        return next(self.rows)


class LegacyMediaHttpRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner_id = uuid.uuid4()
        self.media_id = uuid.uuid4()
        self.identity = SimpleNamespace(
            user_id=self.owner_id,
            upstream_uid="owner",
        )
        self.storage = _RouteStorage()
        self.persistence = _RoutePersistence(self.identity, self.storage)
        self.media = SimpleNamespace(
            id=self.media_id,
            owner_user_id=self.owner_id,
            status="available",
            deleted_at=None,
            retention_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            message_id=None,
            extra_data={},
            r2_bucket="private-bucket",
            r2_object_key="legacy-media/private-object.bin",
            content_type="application/octet-stream",
            size_bytes=128,
            kind="attachment",
        )
        self.database_rows: list[object] = [self.media]
        self.app = FastAPI()
        self.app.state.persistence = self.persistence
        self.app.include_router(media_api.router)

    @contextmanager
    def session_scope(self):
        yield _RouteDatabase(list(self.database_rows))

    def test_content_route_enforces_cookie_rate_limit_and_private_redirect(self) -> None:
        with patch.object(media_api, "session_scope", self.session_scope), TestClient(
            self.app
        ) as client:
            missing = client.get(f"/api/media/{self.media_id}/content")
            self.persistence.allowed = False
            limited = client.get(
                f"/api/media/{self.media_id}/content",
                cookies={"bbw_sid": "valid-session"},
            )
            self.persistence.allowed = True
            success = client.get(
                f"/api/media/{self.media_id}/content",
                cookies={"bbw_sid": "valid-session"},
                follow_redirects=False,
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(success.status_code, 307)
        self.assertEqual(
            success.headers["location"],
            "https://r2.invalid/private?signature=secret",
        )
        self.assertEqual(success.headers["cache-control"], "private, no-store")
        self.assertEqual(success.headers["referrer-policy"], "no-referrer")
        self.assertEqual(success.headers["x-robots-tag"], "noindex, nofollow, noarchive")
        self.assertEqual(
            self.storage.calls,
            [("legacy-media/private-object.bin", 300)],
        )

    def test_access_route_is_owner_only_and_does_not_audit_signed_url(self) -> None:
        with patch.object(media_api, "session_scope", self.session_scope), TestClient(
            self.app
        ) as client:
            success = client.get(
                f"/api/media/{self.media_id}/access",
                cookies={"bbw_sid": "valid-session"},
            )
            self.identity.user_id = uuid.uuid4()
            denied = client.get(
                f"/api/media/{self.media_id}/access",
                cookies={"bbw_sid": "valid-session"},
            )

        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json()["url"], "https://r2.invalid/private?signature=secret")
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(len(self.persistence.captured), 1)
        audited = repr(self.persistence.captured[0])
        self.assertNotIn("signature=secret", audited)
        self.assertNotIn("legacy-media/private-object.bin", audited)

    def test_r2_bucket_or_presign_failure_returns_sanitized_503(self) -> None:
        with patch.object(media_api, "session_scope", self.session_scope), TestClient(
            self.app
        ) as client:
            self.storage.bucket = "wrong-bucket"
            mismatch = client.get(
                f"/api/media/{self.media_id}/content",
                cookies={"bbw_sid": "valid-session"},
                follow_redirects=False,
            )
            self.storage.bucket = "private-bucket"
            self.storage.error = RuntimeError("private R2 details")
            unavailable = client.get(
                f"/api/media/{self.media_id}/content",
                cookies={"bbw_sid": "valid-session"},
                follow_redirects=False,
            )

        self.assertEqual(mismatch.status_code, 503)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(mismatch.json(), {"detail": "媒体存储暂时不可用"})
        self.assertEqual(unavailable.json(), {"detail": "媒体存储暂时不可用"})
        self.assertNotIn("private R2 details", unavailable.text)


if __name__ == "__main__":
    unittest.main()
