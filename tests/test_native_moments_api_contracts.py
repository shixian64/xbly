from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bbw_web import native_moments_api as api
from bbw_web.moments_native import (
    FeedItem,
    FeedScore,
    SocialCommentView,
    SocialMirrorIntent,
    SocialPostMutation,
    SocialPostView,
    SocialPrincipal,
    SocialTopicMutation,
    SocialTopicView,
    SocialViewMutation,
)
from bbw_web.media_native.references import (
    BoundMediaAssetReference,
    ValidatedMediaAsset,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _post(principal: SocialPrincipal) -> SocialPostView:
    topic = SocialTopicView(uuid.uuid4(), f"tpc_{uuid.uuid4().hex}", "音乐", 1)
    return SocialPostView(
        id=uuid.uuid4(),
        public_id=f"pst_{uuid.uuid4().hex}",
        author_user_id=principal.user_id,
        author_upstream_uid=principal.upstream_uid,
        author_display_name=principal.display_name,
        source="web-local",
        title="",
        body="本地动态",
        media={},
        visibility="public",
        comment_policy="open",
        hide_comments=False,
        status="published",
        is_pinned=False,
        like_count=0,
        comment_count=0,
        view_count=0,
        report_count=0,
        source_created_at=NOW,
        published_at=NOW,
        topics=(topic,),
    )


class _Persistence:
    def __init__(self, identity) -> None:
        self.identity = identity
        self.rate_calls = []

    def require_identity(self, sid: str):
        return self.identity if sid == "valid-session" else None

    def rate_limit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        self.rate_calls.append((key, limit, window_seconds))
        return True

    def get_r2_storage(self):
        return SimpleNamespace(bucket="private-media")


class _DB:
    def __init__(self) -> None:
        self.liked_ids: set[uuid.UUID] = set()

    def scalars(self, _statement):
        return iter(self.liked_ids)


class _Service:
    def __init__(self, principal: SocialPrincipal) -> None:
        self.principal = principal
        self.post = _post(principal)
        self.comment = SocialCommentView(
            id=uuid.uuid4(),
            public_id=f"cmt_{uuid.uuid4().hex}",
            post_id=self.post.id,
            post_public_id=self.post.public_id,
            parent_comment_id=None,
            parent_public_id="",
            author_user_id=principal.user_id,
            author_upstream_uid=principal.upstream_uid,
            author_display_name=principal.display_name,
            source="web-local",
            body="本地评论",
            status="active",
            like_count=1,
            reply_count=0,
            source_created_at=NOW,
        )
        self.publish_calls = []
        self.delete_calls = []
        self.feed_calls = []
        self.topic_calls = []
        self.published_request_ids: set[str] = set()

    def topics(self, *, query, limit):
        del query, limit
        return list(self.post.topics)

    def feed(self, **values):
        self.feed_calls.append(values)
        return [
            FeedItem(
                self.post,
                FeedScore(0.75, 1.0, 0.0, 1.0, ("recency", "following", "activity")),
            )
        ]

    def posts_by_author(self, **values):
        del values
        return [self.post]

    def comments(self, **values):
        del values
        return [self.comment]

    def create_topic(self, **values):
        self.topic_calls.append(values)
        topic = self.post.topics[0]
        return SocialTopicMutation(
            topic=topic,
            created=True,
            mirror=SocialMirrorIntent(
                "social.topic.create",
                "topic",
                topic.public_id,
                "topic-key",
                {"topic_public_id": topic.public_id},
            ),
        )

    def record_view(self, **values):
        del values
        return SocialViewMutation(post=self.post, counted=True, mirror=None)

    def publish(self, **values):
        self.publish_calls.append(values)
        request_id = str(values.get("client_request_id") or "")
        created = request_id not in self.published_request_ids
        self.published_request_ids.add(request_id)
        self.post = replace(
            self.post,
            body=str(values.get("body") or "") or None,
            media=dict(values.get("media") or {}),
        )
        return SocialPostMutation(
            post=self.post,
            created=created,
            mirror=SocialMirrorIntent(
                "social.post.publish",
                "post",
                self.post.public_id,
                "publish-key",
                {"post_public_id": self.post.public_id},
            ),
        )

    def delete_post(self, **values):
        self.delete_calls.append(values)
        self.post = replace(self.post, status="deleted")
        return SocialPostMutation(
            post=self.post,
            created=False,
            mirror=SocialMirrorIntent(
                "social.post.delete",
                "post",
                self.post.public_id,
                f"delete:{self.post.public_id}",
                {"post_public_id": self.post.public_id},
            ),
        )


class NativeMomentsApiContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.user_id = uuid.uuid4()
        self.identity = SimpleNamespace(user_id=self.user_id, upstream_uid="42")
        self.principal = SocialPrincipal(self.user_id, "42", "作者")
        self.service = _Service(self.principal)
        self.db = _DB()
        self.persistence = _Persistence(self.identity)
        self.app = FastAPI()
        self.app.state.persistence = self.persistence
        self.app.state.settings = SimpleNamespace(
            user_cookie_name="bbw_sid",
            environment="production",
            r2_bucket="private-media",
        )
        self.app.include_router(api.router)

    @contextmanager
    def service_context(self, _identity):
        yield self.db, self.principal, self.service

    def test_router_covers_all_compatibility_paths(self) -> None:
        registered = {
            (method, route.path)
            for route in api.router.routes
            for method in getattr(route, "methods", set())
        }
        expected = {
            ("GET", "/api/moments/posts"),
            ("GET", "/api/moments/comments"),
            ("GET", "/api/topics"),
            ("POST", "/api/topics/create"),
            ("POST", "/api/moments/view"),
            ("POST", "/api/moments/publish"),
            ("POST", "/api/moments/comment"),
            ("POST", "/api/social/like-post"),
            ("POST", "/api/moments/comment-like"),
            ("POST", "/api/moments/comment-delete"),
            ("POST", "/api/moments/comment-forbid"),
            ("POST", "/api/moments/post-delete"),
            ("POST", "/api/moments/post-visibility"),
            ("POST", "/api/moments/post-pin"),
            ("POST", "/api/social/report"),
        }
        self.assertTrue(expected <= registered)

    def test_authenticated_feed_uses_local_shape_and_explainable_score(self) -> None:
        self.db.liked_ids.add(self.service.post.id)
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            response = client.get(
                "/api/moments/posts?tab=%E6%8B%9B%E5%8B%9F%E4%BB%A4&search=%E5%BE%92%E6%AD%A5",
                cookies={"bbw_sid": "valid-session"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "web-local")
        self.assertEqual(payload["items"][0]["id"], self.service.post.public_id)
        self.assertTrue(payload["items"][0]["is_self"])
        self.assertTrue(payload["items"][0]["is_liked"])
        self.assertEqual(len(payload["items"][0]["feed_score_explanation"]), 3)
        self.assertEqual(self.service.feed_calls[0]["query"], "徒步")
        self.assertEqual(self.service.feed_calls[0]["category"], "recruitment")

    def test_publish_is_canonical_first_and_enqueues_only_mirror_intent(self) -> None:
        captured = []

        def enqueue(_db, *, principal, intent, not_mappable_reason=""):
            self.assertEqual(not_mappable_reason, "")
            captured.append((principal, intent))
            return True

        with patch.object(api, "_service_context", self.service_context), patch.object(
            api, "_enqueue_mirror", enqueue
        ), TestClient(self.app) as client:
            response = client.post(
                "/api/moments/publish",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver", "Idempotency-Key": "client-one"},
                json={
                    "text": "本地动态",
                    "visibility_scope": "公开",
                    "topic": "音乐",
                    "plate": "招募令",
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["created"])
        self.assertEqual(payload["compatibility_sync"], "pending")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1].operation_type, "social.post.publish")
        self.assertEqual(
            self.service.publish_calls[0]["client_request_id"], "client-one"
        )
        self.assertEqual(self.service.publish_calls[0]["category"], "recruitment")

    def test_publish_rejects_external_or_unbound_media_metadata(self) -> None:
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            response = client.post(
                "/api/moments/publish",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver"},
                json={
                    "text": "不能把外站地址伪装成本地媒体",
                    "pictures": ["https://oss.banghua.xin/legacy.jpg"],
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("仅接受 media_asset_ids", response.json()["detail"]["message"])
        self.assertEqual(self.service.publish_calls, [])

    def test_publish_two_native_images_is_ordered_idempotent_and_not_mirrored(self) -> None:
        asset_ids = (uuid.uuid4(), uuid.uuid4())
        repository_calls = []

        class References:
            def __init__(self, _db, *, deployment=None, private_bucket=None):
                repository_calls.append(("scope", deployment, private_bucket))

            def preview_social_post_asset_ids(self, *, owner_user_id, asset_ids):
                normalized = tuple(uuid.UUID(str(value)) for value in asset_ids)
                repository_calls.append(("preview", owner_user_id, normalized))
                return {
                    f"pictures[{index}]": ValidatedMediaAsset(
                        asset_id=value,
                        owner_user_id=owner_user_id,
                        kind="image",
                        content_type="image/jpeg",
                        size_bytes=1024,
                    )
                    for index, value in enumerate(normalized)
                }

            def bind_social_post_assets(
                self, *, owner_user_id, social_post_id, assets_by_slot
            ):
                repository_calls.append(
                    ("bind", owner_user_id, social_post_id, dict(assets_by_slot))
                )
                return {
                    slot: BoundMediaAssetReference(
                        reference_id=uuid.uuid4(),
                        asset_id=asset_id,
                        owner_user_id=owner_user_id,
                        resource_type="social_post",
                        resource_id=social_post_id,
                        slot=slot,
                        kind="image",
                        content_type="image/jpeg",
                        size_bytes=1024,
                        private_bucket="private-media",
                        private_object_key=(
                            f"production/web-media-private/{owner_user_id}/"
                            f"{asset_id}/source.jpg"
                        ),
                    )
                    for slot, asset_id in assets_by_slot.items()
                }

        mirror_reasons = []

        def enqueue(_db, *, principal, intent, not_mappable_reason=""):
            del principal, intent
            mirror_reasons.append(not_mappable_reason)
            return "cancelled"

        request_body = {
            "text": "两张站内图片",
            "media_asset_ids": [str(value) for value in asset_ids],
        }
        with patch.object(api, "_service_context", self.service_context), patch.object(
            api, "SqlAlchemyMediaAssetReferenceRepository", References
        ), patch.object(api, "_enqueue_mirror", enqueue), TestClient(self.app) as client:
            first = client.post(
                "/api/moments/publish",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver", "Idempotency-Key": "media-one"},
                json=request_body,
            )
            replay = client.post(
                "/api/moments/publish",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver", "Idempotency-Key": "media-one"},
                json=request_body,
            )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertFalse(replay.json()["created"])
        expected_paths = [
            f"/api/media/native/{asset_id}/content" for asset_id in asset_ids
        ]
        self.assertEqual(first.json()["post"]["pictures"], expected_paths)
        self.assertEqual(self.service.publish_calls[0]["media"], {"pictures": expected_paths})
        self.assertEqual(mirror_reasons, ["post_media_not_mappable"] * 2)
        self.assertEqual(first.json()["compatibility_sync"], "cancelled")
        self.assertEqual(
            repository_calls[0], ("scope", "production", "private-media")
        )
        self.assertEqual(
            len([call for call in repository_calls if call[0] == "bind"]),
            2,
        )

    def test_publish_single_native_video_uses_video_slot(self) -> None:
        asset_id = uuid.uuid4()

        class References:
            def __init__(self, _db, **_scope):
                pass

            def preview_social_post_asset_ids(self, *, owner_user_id, asset_ids):
                self.asset_id = uuid.UUID(str(tuple(asset_ids)[0]))
                return {
                    "video": ValidatedMediaAsset(
                        asset_id=self.asset_id,
                        owner_user_id=owner_user_id,
                        kind="video",
                        content_type="video/mp4",
                        size_bytes=2048,
                    )
                }

            def bind_social_post_assets(
                self, *, owner_user_id, social_post_id, assets_by_slot
            ):
                return {
                    "video": BoundMediaAssetReference(
                        reference_id=uuid.uuid4(),
                        asset_id=assets_by_slot["video"],
                        owner_user_id=owner_user_id,
                        resource_type="social_post",
                        resource_id=social_post_id,
                        slot="video",
                        kind="video",
                        content_type="video/mp4",
                        size_bytes=2048,
                        private_bucket="private-media",
                        private_object_key=(
                            f"production/web-media-private/{owner_user_id}/"
                            f"{assets_by_slot['video']}/source.mp4"
                        ),
                    )
                }

        with patch.object(api, "_service_context", self.service_context), patch.object(
            api, "SqlAlchemyMediaAssetReferenceRepository", References
        ), patch.object(api, "_enqueue_mirror", return_value="cancelled"), TestClient(
            self.app
        ) as client:
            response = client.post(
                "/api/moments/publish",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver", "Idempotency-Key": "video-one"},
                json={"media_asset_ids": [str(asset_id)]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.service.publish_calls[-1]["media"],
            {"video": f"/api/media/native/{asset_id}/content"},
        )

    def test_user_feed_comments_and_topic_creation_match_frontend_contract(self) -> None:
        self.db.liked_ids.update((self.service.post.id, self.service.comment.id))
        captured = []

        def enqueue(_db, *, principal, intent, not_mappable_reason=""):
            self.assertEqual(not_mappable_reason, "")
            captured.append((principal, intent))
            return True

        with patch.object(api, "_service_context", self.service_context), patch.object(
            api, "_enqueue_mirror", enqueue
        ), TestClient(self.app) as client:
            user_feed = client.get(
                "/api/moments/posts?tab=%E6%88%91%E7%9A%84&page=1&limit=1",
                cookies={"bbw_sid": "valid-session"},
            )
            comments = client.get(
                f"/api/moments/comments?postid={self.service.post.public_id}&page=1&limit=1",
                cookies={"bbw_sid": "valid-session"},
            )
            topic = client.post(
                "/api/topics/create",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver"},
                json={"topic": "音乐"},
            )

        self.assertEqual(user_feed.status_code, 200)
        self.assertEqual(user_feed.json()["next_page"], 2)
        self.assertTrue(user_feed.json()["items"][0]["is_liked"])
        self.assertEqual(comments.status_code, 200)
        self.assertTrue(comments.json()["items"][0]["is_self"])
        self.assertTrue(comments.json()["items"][0]["is_liked"])
        self.assertEqual(topic.status_code, 200)
        self.assertTrue(topic.json()["created"])
        self.assertEqual(topic.json()["topic"]["name"], "音乐")
        self.assertEqual(captured[0][1].operation_type, "social.topic.create")

    def test_nearby_tab_requires_and_applies_city_level_location(self) -> None:
        with patch.object(api, "_service_context", self.service_context), patch.object(
            api, "_viewer_location", return_value=(("上海",), "上海")
        ), TestClient(self.app) as client:
            response = client.get(
                "/api/moments/posts?tab=%E9%99%84%E8%BF%91",
                cookies={"bbw_sid": "valid-session"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["location_region"], "上海")
        self.assertEqual(self.service.feed_calls[0]["region"], ("上海",))
        self.assertEqual(self.service.feed_calls[0]["order"], "latest")

        self.service.feed_calls.clear()
        with patch.object(api, "_service_context", self.service_context), patch.object(
            api, "_viewer_location", return_value=((), "")
        ), TestClient(self.app) as client:
            missing = client.get(
                "/api/moments/posts?tab=%E9%99%84%E8%BF%91",
                cookies={"bbw_sid": "valid-session"},
            )
        self.assertEqual(missing.status_code, 200)
        self.assertFalse(missing.json()["ok"])
        self.assertTrue(missing.json()["location_required"])
        self.assertEqual(self.service.feed_calls, [])

    def test_local_view_response_stops_legacy_task_retry_loop(self) -> None:
        with patch.object(api, "_service_context", self.service_context), TestClient(
            self.app
        ) as client:
            response = client.post(
                "/api/moments/view",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "http://testserver"},
                json={"post_id": self.service.post.public_id},
            )
        self.assertEqual(response.status_code, 200)
        task_assist = response.json()["task_assist"]
        self.assertTrue(task_assist["checked"])
        self.assertFalse(task_assist["retryable"])

    def test_unauthenticated_and_cross_site_writes_are_rejected(self) -> None:
        with TestClient(self.app) as client:
            unauthorized = client.get("/api/topics")
            cross_site = client.post(
                "/api/moments/publish",
                cookies={"bbw_sid": "valid-session"},
                headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
                json={"text": "不应写入"},
            )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(self.service.publish_calls, [])

    def test_adapter_has_no_external_client_or_protocol_import(self) -> None:
        source = (ROOT / "bbw_web" / "native_moments_api.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "import httpx",
            "import requests",
            "bbw_protocol",
            "legacy_banghua",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("OperationOutboxRepository", source)

    def test_frontend_accepts_canonical_local_post_ids_for_view_tracking(self) -> None:
        source = (ROOT / "bbw_web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('/^pst_[0-9a-f]{32}$/.test(postId)', source)


if __name__ == "__main__":
    unittest.main()
