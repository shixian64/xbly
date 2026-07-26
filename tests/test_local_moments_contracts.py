from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import unittest

from bbw_prod.models import (
    LegacySocialBinding,
    MatchPreference,
    MatchQueueEntry,
    MatchResult,
    SocialComment,
    SocialPost,
    SocialPostTopic,
    SocialPostViewEvent,
    SocialReaction,
    SocialReport,
    SocialTopic,
    UserDiscoveryProfile,
)
from bbw_web.moments_native import (
    InvalidSocialContent,
    LegacyCommentInput,
    LegacyHistoryOutsideWindow,
    LegacyPostInput,
    LocalMomentsService,
    SocialAuthorRef,
    SocialCommentView,
    SocialCommentsDisabled,
    SocialContentForbidden,
    SocialIdempotencyConflict,
    SocialPostView,
    SocialPrincipal,
    SocialReactionState,
    SocialReportState,
    SqlAlchemySocialPermissionPolicy,
    SocialTopicView,
    explain_feed_score,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _Policy:
    def __init__(self) -> None:
        self.following: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self.blocked: set[frozenset[uuid.UUID]] = set()

    def _blocked(self, viewer: SocialPrincipal, author: SocialAuthorRef) -> bool:
        return author.user_id is not None and frozenset(
            (viewer.user_id, author.user_id)
        ) in self.blocked

    def is_following(
        self, *, viewer: SocialPrincipal, author: SocialAuthorRef
    ) -> bool:
        return author.user_id is not None and (
            viewer.user_id,
            author.user_id,
        ) in self.following

    def can_view(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
        visibility: str,
    ) -> bool:
        if self._blocked(viewer, author):
            return False
        if author.user_id == viewer.user_id:
            return True
        if visibility == "public":
            return True
        if visibility == "followers":
            return self.is_following(viewer=viewer, author=author)
        return False

    def can_comment(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
        comment_policy: str,
    ) -> bool:
        if self._blocked(viewer, author) or comment_policy == "disabled":
            return False
        return comment_policy == "open" or self.is_following(
            viewer=viewer, author=author
        )


class _MemoryStore:
    def __init__(self) -> None:
        self.posts: dict[str, SocialPostView] = {}
        self.comments: dict[str, SocialCommentView] = {}
        self.post_requests: dict[tuple[uuid.UUID, str], tuple[str, str]] = {}
        self.comment_requests: dict[tuple[uuid.UUID, str], tuple[str, str]] = {}
        self.topics_by_name: dict[str, SocialTopicView] = {}
        self.likes: dict[tuple[uuid.UUID, str, uuid.UUID], bool] = {}
        self.reports: dict[tuple[uuid.UUID, str], tuple[tuple[object, ...], SocialReportState]] = {}
        self.legacy: dict[tuple[str, str], tuple[str, str]] = {}
        self.legacy_comments: dict[tuple[str, str], tuple[str, str]] = {}
        self.daily_views: set[tuple[uuid.UUID, uuid.UUID, object]] = set()

    @staticmethod
    def _public(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def _topic_views(self, names: tuple[str, ...], *, delta: int = 0):
        views = []
        for name in names:
            key = name.casefold()
            topic = self.topics_by_name.get(key)
            if topic is None:
                topic = SocialTopicView(uuid.uuid4(), self._public("tpc"), name, 0)
            if delta:
                topic = replace(topic, post_count=max(0, topic.post_count + delta))
            self.topics_by_name[key] = topic
            views.append(topic)
        return tuple(views)

    def create_topic(self, **values):
        key = values["normalized_name"]
        existing = self.topics_by_name.get(key)
        if existing is not None:
            return existing, False
        topic = SocialTopicView(
            uuid.uuid4(), self._public("tpc"), values["name"], 0
        )
        self.topics_by_name[key] = topic
        return topic, True

    def list_topics(self, *, query: str, limit: int):
        return sorted(
            (
                item
                for key, item in self.topics_by_name.items()
                if not query or query in key
            ),
            key=lambda item: (-item.post_count, item.name),
        )[:limit]

    def create_post(self, **values):
        principal = values["principal"]
        key = (principal.user_id, values["client_request_id"])
        existing = self.post_requests.get(key)
        if existing is not None:
            digest, public_id = existing
            if digest != values["payload_digest"]:
                raise SocialIdempotencyConflict("different post payload")
            return self.posts[public_id], False
        post = SocialPostView(
            id=uuid.uuid4(),
            public_id=self._public("pst"),
            author_user_id=principal.user_id,
            author_upstream_uid=principal.upstream_uid,
            author_display_name=principal.display_name,
            source="web-local",
            title=values["title"],
            body=values["body"],
            media=values["media"],
            visibility=values["visibility"],
            comment_policy=values["comment_policy"],
            hide_comments=values["hide_comments"],
            status="published",
            is_pinned=False,
            like_count=0,
            comment_count=0,
            view_count=0,
            report_count=0,
            source_created_at=values["occurred_at"],
            published_at=values["occurred_at"],
            topics=self._topic_views(values["topics"], delta=1),
            category=values["category"],
        )
        self.posts[post.public_id] = post
        self.post_requests[key] = (values["payload_digest"], post.public_id)
        return post, True

    def import_legacy_post(self, **values):
        item = values["item"]
        key = (item.provider, item.upstream_id)
        existing = self.legacy.get(key)
        if existing is not None:
            digest, public_id = existing
            if digest != values["payload_digest"]:
                raise SocialIdempotencyConflict("different legacy payload")
            return self.posts[public_id], False
        post = SocialPostView(
            id=uuid.uuid4(),
            public_id=self._public("pst"),
            author_user_id=item.author_user_id,
            author_upstream_uid=item.author_upstream_uid,
            author_display_name=item.author_display_name,
            source="legacy-import",
            title=item.title,
            body=item.body,
            media=item.media,
            visibility=item.visibility,
            comment_policy=item.comment_policy,
            hide_comments=item.hide_comments,
            status="published",
            is_pinned=False,
            like_count=0,
            comment_count=0,
            view_count=0,
            report_count=0,
            source_created_at=item.source_created_at,
            published_at=item.source_created_at,
            topics=self._topic_views(item.topics, delta=1),
            category=(
                "recruitment"
                if str((item.metadata or {}).get("plate") or "") == "招募令"
                else "dynamic"
            ),
        )
        self.posts[post.public_id] = post
        self.legacy[key] = (values["payload_digest"], post.public_id)
        return post, True

    def get_post(self, public_id: str, *, for_update: bool = False):
        del for_update
        direct = self.posts.get(public_id)
        if direct is not None:
            return direct
        binding = next(
            (
                value
                for (provider, upstream_id), value in self.legacy.items()
                if provider == "beibeiwu" and upstream_id == public_id
            ),
            None,
        )
        return self.posts.get(binding[1]) if binding else None

    def _set_post(self, post_id: uuid.UUID, **changes):
        for public_id, post in self.posts.items():
            if post.id == post_id:
                updated = replace(post, **changes)
                self.posts[public_id] = updated
                return updated
        raise AssertionError("post not found")

    def set_post_status(self, post_id, *, status, occurred_at):
        del occurred_at
        return self._set_post(post_id, status=status)

    def set_post_visibility(self, post_id, *, visibility):
        return self._set_post(post_id, visibility=visibility)

    def set_post_pin(self, post_id, *, pinned, occurred_at):
        del occurred_at
        return self._set_post(post_id, is_pinned=pinned)

    def set_comment_policy(self, post_id, *, comment_policy, hide_comments):
        post = next(item for item in self.posts.values() if item.id == post_id)
        return self._set_post(
            post_id,
            comment_policy=comment_policy,
            hide_comments=post.hide_comments if hide_comments is None else hide_comments,
        )

    def create_comment(self, **values):
        principal = values["principal"]
        key = (principal.user_id, values["client_request_id"])
        existing = self.comment_requests.get(key)
        if existing is not None:
            digest, public_id = existing
            if digest != values["payload_digest"]:
                raise SocialIdempotencyConflict("different comment payload")
            return self.comments[public_id], False
        post = values["post"]
        parent = values["parent"]
        comment = SocialCommentView(
            id=uuid.uuid4(),
            public_id=self._public("cmt"),
            post_id=post.id,
            post_public_id=post.public_id,
            parent_comment_id=parent.id if parent else None,
            parent_public_id=parent.public_id if parent else "",
            author_user_id=principal.user_id,
            author_upstream_uid=principal.upstream_uid,
            author_display_name=principal.display_name,
            source="web-local",
            body=values["body"],
            status="active",
            like_count=0,
            reply_count=0,
            source_created_at=values["occurred_at"],
        )
        self.comments[comment.public_id] = comment
        self.comment_requests[key] = (values["payload_digest"], comment.public_id)
        self._set_post(post.id, comment_count=post.comment_count + 1)
        if parent:
            self.comments[parent.public_id] = replace(
                parent, reply_count=parent.reply_count + 1
            )
        return comment, True

    def import_legacy_comment(self, **values):
        item = values["item"]
        key = (item.provider, item.upstream_id)
        existing = self.legacy_comments.get(key)
        if existing is not None:
            digest, public_id = existing
            if digest != values["payload_digest"]:
                raise SocialIdempotencyConflict("different legacy comment payload")
            return self.comments[public_id], False
        post = values["post"]
        parent = values["parent"]
        comment = SocialCommentView(
            id=uuid.uuid4(),
            public_id=self._public("cmt"),
            post_id=post.id,
            post_public_id=post.public_id,
            parent_comment_id=parent.id if parent else None,
            parent_public_id=parent.public_id if parent else "",
            author_user_id=item.author_user_id,
            author_upstream_uid=item.author_upstream_uid,
            author_display_name=item.author_display_name,
            source="legacy-import",
            body=item.body,
            status=item.status,
            like_count=item.like_count,
            reply_count=item.reply_count,
            source_created_at=item.source_created_at,
        )
        self.comments[comment.public_id] = comment
        self.legacy_comments[key] = (values["payload_digest"], comment.public_id)
        self._set_post(post.id, comment_count=post.comment_count + 1)
        return comment, True

    def get_comment(self, public_id: str, *, for_update: bool = False):
        del for_update
        direct = self.comments.get(public_id)
        if direct is not None:
            return direct
        binding = self.legacy_comments.get(("beibeiwu", public_id))
        return self.comments.get(binding[1]) if binding else None

    def set_comment_status(self, comment_id, *, status, occurred_at):
        del occurred_at
        for public_id, comment in self.comments.items():
            if comment.id == comment_id:
                delta = int(status == "active") - int(comment.status == "active")
                updated = replace(comment, status=status)
                self.comments[public_id] = updated
                post = self.posts[comment.post_public_id]
                self._set_post(
                    post.id, comment_count=max(0, post.comment_count + delta)
                )
                return updated
        raise AssertionError("comment not found")

    def set_like(self, **values):
        key = (values["actor_user_id"], values["target_type"], values["target_id"])
        previous = self.likes.get(key, False)
        active = values["active"]
        changed = previous != active
        if changed:
            self.likes[key] = active
        if values["target_type"] == "post":
            target = next(
                post for post in self.posts.values() if post.id == values["target_id"]
            )
            if changed:
                target = self._set_post(
                    target.id,
                    like_count=max(0, target.like_count + (1 if active else -1)),
                )
            count = target.like_count
            public_id = target.public_id
        else:
            target = next(
                comment
                for comment in self.comments.values()
                if comment.id == values["target_id"]
            )
            if changed:
                target = replace(
                    target,
                    like_count=max(0, target.like_count + (1 if active else -1)),
                )
                self.comments[target.public_id] = target
            count = target.like_count
            public_id = target.public_id
        return SocialReactionState(
            values["target_type"], public_id, active, count, changed
        )

    def reaction_active(self, *, actor_user_id, target_type, target_id):
        return self.likes.get((actor_user_id, target_type, target_id), False)

    def create_report(self, **values):
        key = (values["reporter_user_id"], values["idempotency_key"])
        payload = (
            values["target_type"],
            values["target_id"],
            values["reason_code"],
            values["reason_text"],
        )
        existing = self.reports.get(key)
        if existing:
            if existing[0] != payload:
                raise SocialIdempotencyConflict("different report payload")
            return replace(existing[1], created=False)
        report = SocialReportState(
            self._public("rpt"),
            values["target_type"],
            values["target_public_id"],
            "pending",
            True,
        )
        self.reports[key] = (payload, report)
        if values["target_type"] == "post":
            post = next(
                post for post in self.posts.values() if post.id == values["target_id"]
            )
        else:
            comment = next(
                item
                for item in self.comments.values()
                if item.id == values["target_id"]
            )
            post = self.posts[comment.post_public_id]
        self._set_post(post.id, report_count=post.report_count + 1)
        return report

    def list_feed_candidates(self, *, not_before, before, limit):
        rows = [
            post
            for post in self.posts.values()
            if post.status == "published"
            and post.published_at >= not_before
            and (before is None or post.published_at < before)
        ]
        return sorted(rows, key=lambda item: item.published_at, reverse=True)[:limit]

    def list_posts_by_author(
        self, *, author_upstream_uid, before, limit, offset=0
    ):
        rows = [
            post
            for post in self.posts.values()
            if post.author_upstream_uid == author_upstream_uid
            and post.status != "deleted"
            and (before is None or post.published_at < before)
        ]
        return sorted(
            rows,
            key=lambda item: (item.is_pinned, item.published_at),
            reverse=True,
        )[offset : offset + limit]

    def list_comments(self, *, post_id, before, limit, offset):
        rows = [
            comment
            for comment in self.comments.values()
            if comment.post_id == post_id
            and comment.status == "active"
            and (before is None or comment.source_created_at < before)
        ]
        ordered = sorted(rows, key=lambda item: item.source_created_at, reverse=True)
        return ordered[offset : offset + limit]

    def record_post_view(self, *, post_id, viewer_user_id, occurred_at):
        key = (post_id, viewer_user_id, occurred_at.date())
        counted = key not in self.daily_views
        self.daily_views.add(key)
        post = next(item for item in self.posts.values() if item.id == post_id)
        if counted:
            post = self._set_post(post_id, view_count=post.view_count + 1)
        return post, counted


class LocalMomentsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _MemoryStore()
        self.policy = _Policy()
        self.service = LocalMomentsService(
            self.store, self.policy, clock=lambda: NOW
        )
        self.author = SocialPrincipal(uuid.uuid4(), "42", "作者")
        self.reader = SocialPrincipal(uuid.uuid4(), "9", "读者")
        self.stranger = SocialPrincipal(uuid.uuid4(), "7", "陌生人")

    def publish(self, **changes):
        values = {
            "principal": self.author,
            "client_request_id": "post-one",
            "body": "本地动态",
            "topics": ("音乐",),
        }
        values.update(changes)
        return self.service.publish(**values)

    def test_publish_is_idempotent_and_only_returns_mirror_intent(self) -> None:
        first = self.publish()
        repeated = self.publish()
        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(first.post.id, repeated.post.id)
        self.assertEqual(first.mirror.operation_type, "social.post.publish")
        self.assertEqual(first.mirror.aggregate_public_id, first.post.public_id)
        self.assertEqual(first.mirror, repeated.mirror)
        with self.assertRaises(SocialIdempotencyConflict):
            self.publish(body="篡改后的内容")

    def test_ownership_visibility_pin_and_comment_policy_are_local_authoritative(self) -> None:
        published = self.publish(visibility="followers")
        with self.assertRaises(SocialContentForbidden):
            self.service.set_visibility(
                principal=self.reader,
                post_public_id=published.post.public_id,
                visibility="public",
            )
        pinned = self.service.set_pinned(
            principal=self.author,
            post_public_id=published.post.public_id,
            pinned=True,
        )
        self.assertTrue(pinned.post.is_pinned)
        configured = self.service.configure_comments(
            principal=self.author,
            post_public_id=published.post.public_id,
            comment_policy="disabled",
            hide_comments=True,
        )
        self.assertEqual(configured.post.comment_policy, "disabled")
        self.assertTrue(configured.post.hide_comments)

    def test_comments_moderation_and_likes_are_idempotent(self) -> None:
        post = self.publish().post
        comment = self.service.comment(
            principal=self.reader,
            post_public_id=post.public_id,
            client_request_id="comment-one",
            body="评论",
        )
        repeated = self.service.comment(
            principal=self.reader,
            post_public_id=post.public_id,
            client_request_id="comment-one",
            body="评论",
        )
        self.assertTrue(comment.created)
        self.assertFalse(repeated.created)

        first_like = self.service.set_like(
            principal=self.author,
            target_type="comment",
            target_public_id=comment.comment.public_id,
            active=True,
        )
        repeated_like = self.service.set_like(
            principal=self.author,
            target_type="comment",
            target_public_id=comment.comment.public_id,
            active=True,
        )
        self.assertTrue(first_like.reaction.changed)
        self.assertFalse(repeated_like.reaction.changed)
        self.assertIsNone(repeated_like.mirror)

        hidden = self.service.set_comment_hidden(
            principal=self.author,
            comment_public_id=comment.comment.public_id,
            hidden=True,
        )
        self.assertEqual(hidden.comment.status, "hidden")
        with self.assertRaises(SocialContentForbidden):
            self.service.delete_comment(
                principal=self.stranger,
                comment_public_id=comment.comment.public_id,
            )
        deleted = self.service.delete_comment(
            principal=self.reader,
            comment_public_id=comment.comment.public_id,
        )
        self.assertEqual(deleted.comment.status, "deleted")

    def test_disabled_and_followers_only_comments_are_enforced_by_injected_policy(self) -> None:
        post = self.publish(comment_policy="followers").post
        with self.assertRaises(SocialCommentsDisabled):
            self.service.comment(
                principal=self.reader,
                post_public_id=post.public_id,
                client_request_id="not-following",
                body="不能评论",
            )
        self.policy.following.add((self.reader.user_id, self.author.user_id))
        accepted = self.service.comment(
            principal=self.reader,
            post_public_id=post.public_id,
            client_request_id="following",
            body="可以评论",
        )
        self.assertTrue(accepted.created)

    def test_topics_reports_and_feed_score_are_explainable(self) -> None:
        created_topic = self.service.create_topic(
            principal=self.author, name="旅行", description="城市记录"
        )
        self.assertTrue(created_topic.created)
        self.assertEqual(self.service.topics(query="旅")[0].name, "旅行")
        post = self.publish(topics=("旅行",)).post
        report = self.service.report(
            principal=self.reader,
            target_type="post",
            target_public_id=post.public_id,
            idempotency_key="report-one",
            reason_code="spam",
        )
        repeated = self.service.report(
            principal=self.reader,
            target_type="post",
            target_public_id=post.public_id,
            idempotency_key="report-one",
            reason_code="spam",
        )
        self.assertTrue(report.report.created)
        self.assertFalse(repeated.report.created)
        self.assertIsNone(repeated.mirror)

        followed = replace(
            post,
            published_at=NOW - timedelta(days=30),
            like_count=10,
            comment_count=4,
            view_count=100,
        )
        score = explain_feed_score(post=followed, now=NOW, following=True)
        self.assertEqual(score.following, 1.0)
        self.assertEqual(len(score.explanation), 3)
        self.assertAlmostEqual(
            score.total,
            round(score.recency * 0.65 + 0.25 + score.activity * 0.10, 6),
        )

    def test_feed_filters_visibility_and_uses_follow_bonus(self) -> None:
        follower_post = self.publish(
            client_request_id="followers-post", visibility="followers"
        ).post
        self.policy.following.add((self.reader.user_id, self.author.user_id))
        feed = self.service.feed(principal=self.reader)
        self.assertEqual(feed[0].post.public_id, follower_post.public_id)
        self.assertEqual(feed[0].score.following, 1.0)
        self.assertEqual(self.service.feed(principal=self.stranger), [])

    def test_feed_filters_search_recruitment_following_and_city_before_paging(self) -> None:
        regular = self.publish(
            client_request_id="regular-post",
            body="普通城市记录",
        ).post
        recruitment = self.publish(
            client_request_id="recruitment-post",
            body="周末徒步同行招募",
            category="招募令",
        ).post
        self.store.posts[regular.public_id] = replace(
            regular, author_snapshot={"city_name": "北京"}
        )
        self.store.posts[recruitment.public_id] = replace(
            recruitment, author_snapshot={"city_name": "上海"}
        )
        self.policy.following.add((self.reader.user_id, self.author.user_id))

        feed = self.service.feed(
            principal=self.reader,
            query="徒步",
            following_only=True,
            region=("上海",),
            category="recruitment",
            order="latest",
        )
        self.assertEqual([item.post.public_id for item in feed], [recruitment.public_id])
        self.assertEqual(feed[0].post.category, "recruitment")
        self.assertEqual(
            self.service.feed(principal=self.reader, region=("深圳",)),
            [],
        )

    def test_author_posts_comments_and_daily_view_are_safe_local_reads(self) -> None:
        post = self.publish().post
        comment = self.service.comment(
            principal=self.reader,
            post_public_id=post.public_id,
            client_request_id="read-comment",
            body="可读取评论",
        ).comment
        author_posts = self.service.posts_by_author(
            principal=self.reader,
            author_upstream_uid=self.author.upstream_uid,
        )
        self.assertEqual(author_posts[0].public_id, post.public_id)
        comments = self.service.comments(
            principal=self.reader, post_public_id=post.public_id
        )
        self.assertEqual(comments[0].public_id, comment.public_id)

        first = self.service.record_view(
            principal=self.reader, post_public_id=post.public_id
        )
        repeated = self.service.record_view(
            principal=self.reader, post_public_id=post.public_id
        )
        self.assertTrue(first.counted)
        self.assertFalse(repeated.counted)
        self.assertEqual(first.post.view_count, 1)
        self.assertEqual(repeated.post.view_count, 1)
        self.assertIsNotNone(first.mirror)
        self.assertIsNone(repeated.mirror)

    def test_legacy_visible_history_has_hard_180_day_boundary_but_owner_history_does_not(self) -> None:
        old_other = LegacyPostInput(
            provider="beibeiwu",
            upstream_id="old-other",
            author_user_id=self.author.user_id,
            author_upstream_uid=self.author.upstream_uid,
            author_display_name=self.author.display_name,
            title="",
            body="旧动态",
            media={},
            visibility="public",
            comment_policy="open",
            hide_comments=False,
            source_created_at=NOW - timedelta(days=181),
        )
        with self.assertRaises(LegacyHistoryOutsideWindow):
            self.service.import_legacy_post(
                requested_by=self.reader,
                item=old_other,
                visibility_verified=True,
            )

        boundary = replace(
            old_other,
            upstream_id="boundary",
            source_created_at=NOW - timedelta(days=180),
        )
        accepted = self.service.import_legacy_post(
            requested_by=self.reader,
            item=boundary,
            visibility_verified=True,
        )
        self.assertTrue(accepted.created)
        self.assertIsNone(accepted.mirror)

        own_old = replace(
            old_other,
            upstream_id="own-old",
            author_user_id=self.reader.user_id,
            author_upstream_uid=self.reader.upstream_uid,
            source_created_at=NOW - timedelta(days=500),
        )
        self.assertTrue(
            self.service.import_legacy_post(
                requested_by=self.reader,
                item=own_old,
                visibility_verified=False,
            ).created
        )
        changed_by_legacy_id = self.service.set_visibility(
            principal=self.reader,
            post_public_id="own-old",
            visibility="private",
        )
        self.assertEqual(changed_by_legacy_id.post.visibility, "private")

    def test_legacy_comments_are_idempotent_parent_bound_and_limited_to_180_days(self) -> None:
        post = self.service.import_legacy_post(
            requested_by=self.reader,
            item=LegacyPostInput(
                provider="beibeiwu",
                upstream_id="legacy-post-comments",
                author_user_id=self.reader.user_id,
                author_upstream_uid=self.reader.upstream_uid,
                author_display_name=self.reader.display_name,
                title="",
                body="用于迁移评论的动态",
                media={},
                visibility="public",
                comment_policy="open",
                hide_comments=False,
                source_created_at=NOW - timedelta(days=2),
            ),
            visibility_verified=False,
        ).post
        root_input = LegacyCommentInput(
            provider="beibeiwu",
            upstream_id="legacy-comment-root",
            post_upstream_id="legacy-post-comments",
            parent_upstream_id="",
            author_user_id=self.author.user_id,
            author_upstream_uid=self.author.upstream_uid,
            author_display_name=self.author.display_name,
            body="历史主评论",
            status="active",
            like_count=4,
            reply_count=1,
            source_created_at=NOW - timedelta(days=1),
        )
        root = self.service.import_legacy_comment(
            requested_by=self.reader,
            post=post,
            item=root_input,
        )
        repeated = self.service.import_legacy_comment(
            requested_by=self.reader,
            post=post,
            item=root_input,
        )
        self.assertTrue(root.created)
        self.assertFalse(repeated.created)
        self.assertEqual(root.comment.like_count, 4)
        self.assertIsNone(root.mirror)

        reply = self.service.import_legacy_comment(
            requested_by=self.reader,
            post=post,
            parent=root.comment,
            item=LegacyCommentInput(
                provider="beibeiwu",
                upstream_id="legacy-comment-reply",
                post_upstream_id="legacy-post-comments",
                parent_upstream_id="legacy-comment-root",
                author_user_id=self.reader.user_id,
                author_upstream_uid=self.reader.upstream_uid,
                author_display_name=self.reader.display_name,
                body="历史回复",
                status="active",
                like_count=0,
                reply_count=0,
                source_created_at=NOW - timedelta(hours=2),
            ),
        )
        self.assertEqual(reply.comment.parent_public_id, root.comment.public_id)

        with self.assertRaises(LegacyHistoryOutsideWindow):
            self.service.import_legacy_comment(
                requested_by=self.reader,
                post=post,
                item=replace(
                    root_input,
                    upstream_id="legacy-comment-old",
                    source_created_at=NOW - timedelta(days=181),
                ),
            )
        with self.assertRaisesRegex(Exception, "父评论"):
            self.service.import_legacy_comment(
                requested_by=self.reader,
                post=post,
                item=replace(
                    root_input,
                    upstream_id="legacy-comment-orphan",
                    parent_upstream_id="missing-parent",
                ),
            )

    def test_invalid_content_is_rejected_before_store(self) -> None:
        with self.assertRaises(InvalidSocialContent):
            self.publish(body="", media={})
        with self.assertRaises(InvalidSocialContent):
            self.publish(visibility="unknown")
        with self.assertRaises(InvalidSocialContent):
            self.publish(topics=tuple(f"topic-{index}" for index in range(6)))


class LocalSocialSchemaTests(unittest.TestCase):
    def test_sqlalchemy_permission_policy_is_web_local_and_deny_wins(self) -> None:
        class ScalarDB:
            def __init__(self, values):
                self.values = iter(values)
                self.statements = []

            def scalar(self, statement):
                self.statements.append(statement)
                return next(self.values)

        viewer = SocialPrincipal(uuid.uuid4(), "viewer")
        author = SocialAuthorRef(uuid.uuid4(), "author")

        allowed_db = ScalarDB((None, uuid.uuid4()))
        allowed = SqlAlchemySocialPermissionPolicy(allowed_db)
        self.assertTrue(
            allowed.can_view(
                viewer=viewer, author=author, visibility="followers"
            )
        )
        self.assertTrue(
            allowed.can_comment(
                viewer=viewer, author=author, comment_policy="followers"
            )
        )
        sql = "\n".join(str(statement) for statement in allowed_db.statements)
        self.assertIn("relationships.provider", sql)
        self.assertIn("relationships.kind", sql)

        blocked_db = ScalarDB((uuid.uuid4(),))
        blocked = SqlAlchemySocialPermissionPolicy(blocked_db)
        self.assertFalse(
            blocked.can_view(viewer=viewer, author=author, visibility="public")
        )
        self.assertFalse(
            blocked.can_comment(
                viewer=viewer, author=author, comment_policy="open"
            )
        )

    def test_canonical_content_tables_and_constraints_exist(self) -> None:
        models = (
            SocialPost,
            SocialComment,
            SocialReaction,
            SocialTopic,
            SocialPostTopic,
            SocialReport,
            SocialPostViewEvent,
            LegacySocialBinding,
        )
        self.assertEqual(
            {model.__tablename__ for model in models},
            {
                "social_posts",
                "social_comments",
                "social_reactions",
                "social_topics",
                "social_post_topics",
                "social_reports",
                "social_post_views",
                "legacy_social_bindings",
            },
        )
        post_constraints = {item.name for item in SocialPost.__table__.constraints}
        self.assertIn("uq_social_posts_author_client_request", post_constraints)
        legacy_constraints = {
            item.name for item in LegacySocialBinding.__table__.constraints
        }
        self.assertIn(
            "ck_legacy_social_bindings_legacy_social_binding_visible_history_180_days",
            legacy_constraints,
        )

    def test_discovery_and_match_schema_is_city_only_and_bilateral(self) -> None:
        self.assertEqual(UserDiscoveryProfile.__tablename__, "user_discovery_profiles")
        self.assertEqual(MatchPreference.__tablename__, "match_preferences")
        self.assertEqual(MatchQueueEntry.__tablename__, "match_queue_entries")
        self.assertEqual(MatchResult.__tablename__, "match_results")
        discovery_columns = set(UserDiscoveryProfile.__table__.columns.keys())
        self.assertIn("city_code", discovery_columns)
        self.assertIn("city_name", discovery_columns)
        self.assertFalse(
            discovery_columns
            & {"latitude", "longitude", "lat", "lng", "geohash", "coordinates"}
        )
        result_indexes = {item.name for item in MatchResult.__table__.indexes}
        self.assertIn("ix_match_results_low_matched", result_indexes)
        self.assertIn("ix_match_results_high_matched", result_indexes)
        result_constraints = {item.name for item in MatchResult.__table__.constraints}
        self.assertIn("uq_match_results_match_key", result_constraints)
        self.assertIn("fk_match_results_low_queue_user", result_constraints)
        self.assertIn("fk_match_results_high_queue_user", result_constraints)

    def test_migration_0013_follows_0012_and_contains_no_coordinate_columns(self) -> None:
        migration = (
            ROOT
            / "migrations"
            / "versions"
            / "20260725_0013_local_social_content.py"
        ).read_text(encoding="utf-8")
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "20260725_0012"', migration)
        for table in (
            "social_posts",
            "social_comments",
            "social_reactions",
            "social_topics",
            "social_post_topics",
            "social_reports",
            "social_post_views",
            "legacy_social_bindings",
            "user_discovery_profiles",
            "match_preferences",
            "match_queue_entries",
            "match_results",
        ):
            self.assertIn(f'"{table}"', migration)
        lowered = migration.lower()
        for forbidden in ("latitude", "longitude", "geohash", "coordinates"):
            self.assertNotIn(forbidden, lowered)

    def test_native_service_has_no_legacy_or_network_dependency(self) -> None:
        package = ROOT / "bbw_web" / "moments_native"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(package.glob("*.py"))
        )
        self.assertNotIn("import httpx", source)
        self.assertNotIn("import requests", source)
        self.assertNotIn("bbw_protocol", source)
        self.assertNotIn("legacy_banghua", source)
        self.assertNotIn("import bbw_web.persistence", source)
        self.assertNotIn("from bbw_web import persistence", source)


if __name__ == "__main__":
    unittest.main()
