"""SQLAlchemy repository for canonical Web-local social content.

The caller owns the transaction.  No method calls Banghua or any other
network service; compatibility work is represented by service-layer intents.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bbw_prod.models import (
    LegacySocialBinding,
    SocialComment,
    SocialPost,
    SocialPostTopic,
    SocialPostViewEvent,
    SocialReaction,
    SocialReport,
    SocialTopic,
    User,
    UserDiscoveryProfile,
)

from .contracts import (
    LOCAL_SOCIAL_PROVIDER,
    LOCAL_SOCIAL_SCHEMA,
    LegacyCommentInput,
    LegacyPostInput,
    SocialCommentView,
    SocialContentNotFound,
    SocialIdempotencyConflict,
    SocialPostView,
    SocialPrincipal,
    SocialReactionState,
    SocialReportState,
    SocialTopicView,
)


def _topic_view(row: SocialTopic) -> SocialTopicView:
    return SocialTopicView(
        id=row.id,
        public_id=row.public_id,
        name=row.name,
        post_count=max(0, int(row.post_count or 0)),
    )


def _post_category(metadata: Mapping[str, Any]) -> str:
    raw = str(
        metadata.get("category")
        or metadata.get("plate")
        or metadata.get("platename")
        or ""
    ).strip()
    if not raw:
        legacy = metadata.get("legacy_metadata")
        if isinstance(legacy, Mapping):
            return _post_category(legacy)
    return "recruitment" if raw in {"recruitment", "招募令"} else "dynamic"


def _legacy_count(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, 2**31 - 1))


class SqlAlchemyCanonicalSocialStore:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _topics_for_post(self, post_id: uuid.UUID) -> tuple[SocialTopicView, ...]:
        rows = list(
            self.db.scalars(
                select(SocialTopic)
                .join(SocialPostTopic, SocialPostTopic.topic_id == SocialTopic.id)
                .where(SocialPostTopic.post_id == post_id)
                .order_by(SocialPostTopic.position, SocialTopic.name, SocialTopic.id)
            )
        )
        return tuple(_topic_view(row) for row in rows)

    def _post_view(self, row: SocialPost) -> SocialPostView:
        metadata = dict(row.extra_data or {})
        return SocialPostView(
            id=row.id,
            public_id=row.public_id,
            author_user_id=row.author_user_id,
            author_upstream_uid=row.author_upstream_uid,
            author_display_name=row.author_display_name,
            source=row.source,
            title=str(row.title or ""),
            body=str(row.body or ""),
            media=dict(row.media or {}),
            visibility=row.visibility,
            comment_policy=row.comment_policy,
            hide_comments=bool(row.hide_comments),
            status=row.status,
            is_pinned=bool(row.is_pinned),
            like_count=max(0, int(row.like_count or 0)),
            comment_count=max(0, int(row.comment_count or 0)),
            view_count=max(0, int(row.view_count or 0)),
            report_count=max(0, int(row.report_count or 0)),
            source_created_at=row.source_created_at,
            published_at=row.published_at,
            topics=self._topics_for_post(row.id),
            author_snapshot=dict(row.author_snapshot or {}),
            category=_post_category(metadata),
            metadata=metadata,
        )

    def _comment_view(self, row: SocialComment) -> SocialCommentView:
        post_public_id = self.db.scalar(
            select(SocialPost.public_id).where(SocialPost.id == row.post_id)
        )
        parent_public_id = ""
        if row.parent_comment_id is not None:
            parent_public_id = str(
                self.db.scalar(
                    select(SocialComment.public_id).where(
                        SocialComment.id == row.parent_comment_id
                    )
                )
                or ""
            )
        if not post_public_id:
            raise SocialContentNotFound("评论所属动态不存在")
        return SocialCommentView(
            id=row.id,
            public_id=row.public_id,
            post_id=row.post_id,
            post_public_id=str(post_public_id),
            parent_comment_id=row.parent_comment_id,
            parent_public_id=parent_public_id,
            author_user_id=row.author_user_id,
            author_upstream_uid=row.author_upstream_uid,
            author_display_name=row.author_display_name,
            source=row.source,
            body=row.body,
            status=row.status,
            like_count=max(0, int(row.like_count or 0)),
            reply_count=max(0, int(row.reply_count or 0)),
            source_created_at=row.source_created_at,
        )

    def _ensure_topic(
        self,
        *,
        name: str,
        normalized_name: str,
        description: str,
        created_by_user_id: uuid.UUID | None,
        occurred_at: datetime,
    ) -> tuple[SocialTopic, bool]:
        statement = (
            insert(SocialTopic)
            .values(
                name=name,
                normalized_name=normalized_name,
                description=description or None,
                created_by_user_id=created_by_user_id,
                status="active",
                post_count=0,
                extra_data={
                    "authority": LOCAL_SOCIAL_PROVIDER,
                    "schema": LOCAL_SOCIAL_SCHEMA,
                },
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="uq_social_topics_normalized_name")
            .returning(SocialTopic)
        )
        row = self.db.scalars(statement).first()
        if row is not None:
            return row, True
        existing = self.db.scalar(
            select(SocialTopic).where(SocialTopic.normalized_name == normalized_name)
        )
        if existing is None:
            raise RuntimeError("topic conflict occurred without an existing row")
        return existing, False

    def create_topic(
        self,
        *,
        name: str,
        normalized_name: str,
        description: str,
        created_by_user_id: uuid.UUID,
        occurred_at: datetime,
    ) -> tuple[SocialTopicView, bool]:
        row, created = self._ensure_topic(
            name=name,
            normalized_name=normalized_name,
            description=description,
            created_by_user_id=created_by_user_id,
            occurred_at=occurred_at,
        )
        return _topic_view(row), created

    def list_topics(self, *, query: str, limit: int) -> list[SocialTopicView]:
        statement = select(SocialTopic).where(SocialTopic.status == "active")
        if query:
            statement = statement.where(SocialTopic.normalized_name.contains(query))
        rows = list(
            self.db.scalars(
                statement.order_by(
                    SocialTopic.post_count.desc(), SocialTopic.name, SocialTopic.id
                ).limit(min(max(1, int(limit)), 100))
            )
        )
        return [_topic_view(row) for row in rows]

    def _attach_topics(
        self,
        *,
        post: SocialPost,
        names: tuple[str, ...],
        created_by_user_id: uuid.UUID | None,
        occurred_at: datetime,
    ) -> None:
        for position, name in enumerate(names):
            topic, _created = self._ensure_topic(
                name=name,
                normalized_name=name.casefold(),
                description="",
                created_by_user_id=created_by_user_id,
                occurred_at=occurred_at,
            )
            association = self.db.scalars(
                insert(SocialPostTopic)
                .values(
                    post_id=post.id,
                    topic_id=topic.id,
                    position=position,
                    created_at=occurred_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_social_post_topics_post_topic"
                )
                .returning(SocialPostTopic)
            ).first()
            if association is not None:
                topic.post_count = max(0, int(topic.post_count or 0)) + 1

    def create_post(
        self,
        *,
        principal: SocialPrincipal,
        client_request_id: str,
        payload_digest: str,
        title: str,
        body: str,
        media: dict,
        visibility: str,
        comment_policy: str,
        hide_comments: bool,
        category: str,
        topics: tuple[str, ...],
        occurred_at: datetime,
    ) -> tuple[SocialPostView, bool]:
        user = self.db.scalar(select(User).where(User.id == principal.user_id))
        profile = dict(user.profile or {}) if user is not None else {}
        author_snapshot = {
            key: profile[key]
            for key in (
                "avatar",
                "portrait",
                "city",
                "city_code",
                "city_name",
                "region",
                "region_code",
                "area",
                "gender",
                "property",
                "age",
            )
            if key in profile
        }
        discovery = self.db.scalar(
            select(UserDiscoveryProfile).where(
                UserDiscoveryProfile.user_id == principal.user_id
            )
        )
        if discovery is not None:
            if discovery.city_code:
                author_snapshot["city_code"] = discovery.city_code
            if discovery.city_name:
                author_snapshot["city_name"] = discovery.city_name
        statement = (
            insert(SocialPost)
            .values(
                client_request_id=client_request_id,
                payload_digest=payload_digest,
                author_user_id=principal.user_id,
                author_upstream_uid=principal.upstream_uid,
                author_display_name=principal.display_name,
                author_snapshot=author_snapshot,
                source=LOCAL_SOCIAL_PROVIDER,
                title=title or None,
                body=body or None,
                media=media,
                visibility=visibility,
                comment_policy=comment_policy,
                hide_comments=hide_comments,
                status="published",
                source_created_at=occurred_at,
                published_at=occurred_at,
                extra_data={
                    "authority": LOCAL_SOCIAL_PROVIDER,
                    "category": category,
                    "plate": "招募令" if category == "recruitment" else "动态",
                    "schema": LOCAL_SOCIAL_SCHEMA,
                },
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_social_posts_author_client_request"
            )
            .returning(SocialPost)
        )
        post = self.db.scalars(statement).first()
        created = post is not None
        if post is None:
            post = self.db.scalar(
                select(SocialPost).where(
                    SocialPost.author_user_id == principal.user_id,
                    SocialPost.client_request_id == client_request_id,
                )
            )
            if post is None or post.payload_digest != payload_digest:
                raise SocialIdempotencyConflict(
                    "client_request_id 已用于另一条动态"
                )
        else:
            self._attach_topics(
                post=post,
                names=topics,
                created_by_user_id=principal.user_id,
                occurred_at=occurred_at,
            )
        self.db.flush()
        return self._post_view(post), created

    def import_legacy_post(
        self,
        *,
        requested_by: SocialPrincipal,
        item: LegacyPostInput,
        import_scope: str,
        payload_digest: str,
        imported_at: datetime,
    ) -> tuple[SocialPostView, bool]:
        binding = self.db.scalar(
            select(LegacySocialBinding).where(
                LegacySocialBinding.provider == item.provider,
                LegacySocialBinding.entity_type == "post",
                LegacySocialBinding.upstream_id == item.upstream_id,
            )
        )
        if binding is not None:
            existing = self.db.scalar(
                select(SocialPost).where(SocialPost.id == binding.local_entity_id)
            )
            if existing is None:
                raise SocialContentNotFound("历史动态绑定已失效")
            if (
                existing.author_user_id != item.author_user_id
                or existing.author_upstream_uid != item.author_upstream_uid
            ):
                # digest 收敛只接受同作者的合法漂移；作者身份变化说明上游
                # 动态 ID 被复用给了别人的内容，必须失败关闭。
                raise SocialIdempotencyConflict("上游动态 ID 已绑定到其他作者")
            if binding.payload_digest != payload_digest:
                # provider+upstream_id 才是 legacy 导入的幂等身份；digest 漂移
                # （上游内容微调或旧算法存量值）按已导入收敛并登记最新 digest，
                # 不得让重跑把整账号迁移失败关闭。
                binding.payload_digest = payload_digest
                extra = dict(binding.extra_data or {})
                extra["digest_refreshed_at"] = imported_at.isoformat()
                binding.extra_data = extra
                binding.updated_at = imported_at
                self.db.flush()
            return self._post_view(existing), False

        legacy_metadata = dict(item.metadata or {})
        category = _post_category(legacy_metadata)
        is_pinned = bool(legacy_metadata.get("legacy_pinned"))
        post = SocialPost(
            client_request_id=None,
            payload_digest=payload_digest,
            author_user_id=item.author_user_id,
            author_upstream_uid=item.author_upstream_uid,
            author_display_name=item.author_display_name,
            author_snapshot=dict(item.author_snapshot or {}),
            source="legacy-import",
            title=item.title or None,
            body=item.body or None,
            media=dict(item.media or {}),
            visibility=item.visibility,
            comment_policy=item.comment_policy,
            hide_comments=item.hide_comments,
            status="published",
            is_pinned=is_pinned,
            pinned_at=item.source_created_at if is_pinned else None,
            like_count=_legacy_count(legacy_metadata.get("legacy_like_count")),
            source_created_at=item.source_created_at,
            published_at=item.source_created_at,
            extra_data={
                "authority": LOCAL_SOCIAL_PROVIDER,
                "category": category,
                "legacy_metadata": legacy_metadata,
                "plate": "招募令" if category == "recruitment" else "动态",
                "schema": LOCAL_SOCIAL_SCHEMA,
                "source_provider": item.provider,
            },
            created_at=imported_at,
            updated_at=imported_at,
        )
        self.db.add(post)
        self.db.flush()
        self._attach_topics(
            post=post,
            names=item.topics,
            created_by_user_id=None,
            occurred_at=imported_at,
        )
        self.db.add(
            LegacySocialBinding(
                provider=item.provider,
                entity_type="post",
                upstream_id=item.upstream_id,
                local_entity_id=post.id,
                local_public_id=post.public_id,
                imported_by_user_id=requested_by.user_id,
                import_scope=import_scope,
                source_created_at=item.source_created_at,
                payload_digest=payload_digest,
                extra_data={
                    "schema": LOCAL_SOCIAL_SCHEMA,
                    "visibility_verified": import_scope == "visible-history",
                },
                created_at=imported_at,
                updated_at=imported_at,
            )
        )
        self.db.flush()
        return self._post_view(post), True

    def get_post(
        self, public_id: str, *, for_update: bool = False
    ) -> SocialPostView | None:
        if public_id.startswith("pst_"):
            statement = select(SocialPost).where(SocialPost.public_id == public_id)
        else:
            statement = (
                select(SocialPost)
                .join(
                    LegacySocialBinding,
                    LegacySocialBinding.local_entity_id == SocialPost.id,
                )
                .where(
                    LegacySocialBinding.provider == "beibeiwu",
                    LegacySocialBinding.entity_type == "post",
                    LegacySocialBinding.upstream_id == public_id,
                )
            )
        if for_update:
            # 服务层采用「无锁预读定位 → 按序加锁」模式；预读已把实体装进
            # identity map，锁定重读必须强制以数据库行覆盖本地属性，否则
            # 锁内校验（status/计数）用的是拿锁前的过期快照。
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        row = self.db.scalar(statement)
        return self._post_view(row) if row is not None else None

    def _post_row(self, post_id: uuid.UUID, *, for_update: bool = True) -> SocialPost:
        statement = select(SocialPost).where(SocialPost.id == post_id)
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        row = self.db.scalar(statement)
        if row is None:
            raise SocialContentNotFound("动态不存在")
        return row

    def set_post_status(
        self, post_id: uuid.UUID, *, status: str, occurred_at: datetime
    ) -> SocialPostView:
        row = self._post_row(post_id)
        previous = row.status
        if previous != status:
            row.status = status
            row.deleted_at = occurred_at if status == "deleted" else None
            row.version = int(row.version or 1) + 1
            if previous == "published" and status != "published":
                topics = list(
                    self.db.scalars(
                        select(SocialTopic)
                        .join(
                            SocialPostTopic,
                            SocialPostTopic.topic_id == SocialTopic.id,
                        )
                        .where(SocialPostTopic.post_id == row.id)
                        .with_for_update()
                    )
                )
                for topic in topics:
                    topic.post_count = max(0, int(topic.post_count or 0) - 1)
            elif previous != "published" and status == "published":
                topics = list(
                    self.db.scalars(
                        select(SocialTopic)
                        .join(
                            SocialPostTopic,
                            SocialPostTopic.topic_id == SocialTopic.id,
                        )
                        .where(SocialPostTopic.post_id == row.id)
                        .with_for_update()
                    )
                )
                for topic in topics:
                    topic.post_count = max(0, int(topic.post_count or 0)) + 1
        self.db.flush()
        return self._post_view(row)

    def set_post_visibility(
        self, post_id: uuid.UUID, *, visibility: str
    ) -> SocialPostView:
        row = self._post_row(post_id)
        if row.visibility != visibility:
            row.visibility = visibility
            row.version = int(row.version or 1) + 1
        self.db.flush()
        return self._post_view(row)

    def set_post_pin(
        self, post_id: uuid.UUID, *, pinned: bool, occurred_at: datetime
    ) -> SocialPostView:
        row = self._post_row(post_id)
        if bool(row.is_pinned) != pinned:
            row.is_pinned = pinned
            row.pinned_at = occurred_at if pinned else None
            row.version = int(row.version or 1) + 1
        self.db.flush()
        return self._post_view(row)

    def set_comment_policy(
        self,
        post_id: uuid.UUID,
        *,
        comment_policy: str,
        hide_comments: bool | None,
    ) -> SocialPostView:
        row = self._post_row(post_id)
        changed = row.comment_policy != comment_policy
        row.comment_policy = comment_policy
        if hide_comments is not None and bool(row.hide_comments) != bool(hide_comments):
            row.hide_comments = bool(hide_comments)
            changed = True
        if changed:
            row.version = int(row.version or 1) + 1
        self.db.flush()
        return self._post_view(row)

    def create_comment(
        self,
        *,
        principal: SocialPrincipal,
        post: SocialPostView,
        parent: SocialCommentView | None,
        client_request_id: str,
        payload_digest: str,
        body: str,
        occurred_at: datetime,
    ) -> tuple[SocialCommentView, bool]:
        statement = (
            insert(SocialComment)
            .values(
                client_request_id=client_request_id,
                payload_digest=payload_digest,
                post_id=post.id,
                parent_comment_id=parent.id if parent else None,
                author_user_id=principal.user_id,
                author_upstream_uid=principal.upstream_uid,
                author_display_name=principal.display_name,
                author_snapshot={},
                source=LOCAL_SOCIAL_PROVIDER,
                body=body,
                status="active",
                source_created_at=occurred_at,
                extra_data={
                    "authority": LOCAL_SOCIAL_PROVIDER,
                    "schema": LOCAL_SOCIAL_SCHEMA,
                },
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_social_comments_author_client_request"
            )
            .returning(SocialComment)
        )
        comment = self.db.scalars(statement).first()
        created = comment is not None
        if comment is None:
            comment = self.db.scalar(
                select(SocialComment).where(
                    SocialComment.author_user_id == principal.user_id,
                    SocialComment.client_request_id == client_request_id,
                )
            )
            if comment is None or comment.payload_digest != payload_digest:
                raise SocialIdempotencyConflict(
                    "client_request_id 已用于另一条评论"
                )
        else:
            post_row = self._post_row(post.id)
            post_row.comment_count = max(0, int(post_row.comment_count or 0)) + 1
            if parent is not None:
                parent_row = self.db.scalar(
                    select(SocialComment)
                    .where(SocialComment.id == parent.id)
                    .with_for_update()
                )
                if parent_row is None or parent_row.post_id != post.id:
                    raise SocialContentNotFound("回复目标不存在")
                parent_row.reply_count = max(0, int(parent_row.reply_count or 0)) + 1
        self.db.flush()
        return self._comment_view(comment), created

    def import_legacy_comment(
        self,
        *,
        requested_by: SocialPrincipal,
        post: SocialPostView,
        parent: SocialCommentView | None,
        item: LegacyCommentInput,
        import_scope: str,
        payload_digest: str,
        imported_at: datetime,
    ) -> tuple[SocialCommentView, bool]:
        binding = self.db.scalar(
            select(LegacySocialBinding).where(
                LegacySocialBinding.provider == item.provider,
                LegacySocialBinding.entity_type == "comment",
                LegacySocialBinding.upstream_id == item.upstream_id,
            )
        )
        if binding is not None:
            existing = self.db.scalar(
                select(SocialComment).where(
                    SocialComment.id == binding.local_entity_id
                )
            )
            if existing is None:
                raise SocialContentNotFound("历史评论绑定已失效")
            if existing.post_id != post.id or existing.parent_comment_id != (
                parent.id if parent is not None else None
            ):
                # digest 收敛只接受同归属的合法漂移；所属动态或父评论变化
                # 说明上游评论 ID 被复用，必须失败关闭。
                raise SocialIdempotencyConflict("上游评论 ID 已绑定到其他动态或父评论")
            if binding.payload_digest != payload_digest:
                # 同 import_legacy_post：digest 漂移按已导入收敛，
                # 幂等身份以 provider+upstream_id 为准。
                binding.payload_digest = payload_digest
                extra = dict(binding.extra_data or {})
                extra["digest_refreshed_at"] = imported_at.isoformat()
                binding.extra_data = extra
                binding.updated_at = imported_at
                self.db.flush()
            return self._comment_view(existing), False

        comment = SocialComment(
            client_request_id=None,
            payload_digest=payload_digest,
            post_id=post.id,
            parent_comment_id=parent.id if parent is not None else None,
            author_user_id=item.author_user_id,
            author_upstream_uid=item.author_upstream_uid,
            author_display_name=item.author_display_name,
            author_snapshot=dict(item.author_snapshot or {}),
            source="legacy-import",
            body=item.body,
            status=item.status,
            like_count=item.like_count,
            reply_count=item.reply_count,
            source_created_at=item.source_created_at,
            deleted_at=None,
            extra_data={
                "authority": LOCAL_SOCIAL_PROVIDER,
                "legacy_metadata": dict(item.metadata or {}),
                "legacy_parent_upstream_id": item.parent_upstream_id,
                "legacy_post_upstream_id": item.post_upstream_id,
                "schema": LOCAL_SOCIAL_SCHEMA,
                "source_provider": item.provider,
            },
            created_at=imported_at,
            updated_at=imported_at,
        )
        self.db.add(comment)
        self.db.flush()
        post_row = self._post_row(post.id)
        post_row.comment_count = max(0, int(post_row.comment_count or 0)) + 1
        self.db.add(
            LegacySocialBinding(
                provider=item.provider,
                entity_type="comment",
                upstream_id=item.upstream_id,
                local_entity_id=comment.id,
                local_public_id=comment.public_id,
                imported_by_user_id=requested_by.user_id,
                import_scope=import_scope,
                source_created_at=item.source_created_at,
                payload_digest=payload_digest,
                extra_data={
                    "schema": LOCAL_SOCIAL_SCHEMA,
                    "legacy_parent_upstream_id": item.parent_upstream_id,
                    "legacy_post_upstream_id": item.post_upstream_id,
                },
                created_at=imported_at,
                updated_at=imported_at,
            )
        )
        self.db.flush()
        return self._comment_view(comment), True

    def get_comment(
        self, public_id: str, *, for_update: bool = False
    ) -> SocialCommentView | None:
        if public_id.startswith("cmt_"):
            statement = select(SocialComment).where(
                SocialComment.public_id == public_id
            )
        else:
            statement = (
                select(SocialComment)
                .join(
                    LegacySocialBinding,
                    LegacySocialBinding.local_entity_id == SocialComment.id,
                )
                .where(
                    LegacySocialBinding.provider == "beibeiwu",
                    LegacySocialBinding.entity_type == "comment",
                    LegacySocialBinding.upstream_id == public_id,
                )
            )
        if for_update:
            # 同 get_post：锁定重读强制刷新 identity map 中的过期实体。
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        row = self.db.scalar(statement)
        return self._comment_view(row) if row is not None else None

    def set_comment_status(
        self, comment_id: uuid.UUID, *, status: str, occurred_at: datetime
    ) -> SocialCommentView:
        row = self.db.scalar(
            select(SocialComment)
            .where(SocialComment.id == comment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise SocialContentNotFound("评论不存在")
        previous = row.status
        if previous != status:
            was_visible = previous == "active"
            is_visible = status == "active"
            row.status = status
            row.deleted_at = occurred_at if status == "deleted" else None
            delta = int(is_visible) - int(was_visible)
            if delta:
                post = self._post_row(row.post_id)
                post.comment_count = max(0, int(post.comment_count or 0) + delta)
                if row.parent_comment_id is not None:
                    parent = self.db.scalar(
                        select(SocialComment)
                        .where(SocialComment.id == row.parent_comment_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if parent is not None:
                        parent.reply_count = max(
                            0, int(parent.reply_count or 0) + delta
                        )
        self.db.flush()
        return self._comment_view(row)

    def set_like(
        self,
        *,
        actor_user_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        active: bool,
        occurred_at: datetime,
    ) -> SocialReactionState:
        if target_type == "post":
            target = self._post_row(target_id)
            target_column = SocialReaction.post_id
            public_id = target.public_id
        else:
            target = self.db.scalar(
                select(SocialComment)
                .where(SocialComment.id == target_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if target is None:
                raise SocialContentNotFound("评论不存在")
            target_column = SocialReaction.comment_id
            public_id = target.public_id
        reaction = self.db.scalar(
            select(SocialReaction)
            .where(
                SocialReaction.actor_user_id == actor_user_id,
                target_column == target_id,
                SocialReaction.reaction_type == "like",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reaction is None and not active:
            return SocialReactionState(
                target_type=target_type,
                target_public_id=public_id,
                active=False,
                count=max(0, int(target.like_count or 0)),
                changed=False,
            )
        changed = reaction is None or bool(reaction.active) != active
        if reaction is None:
            reaction = SocialReaction(
                actor_user_id=actor_user_id,
                post_id=target_id if target_type == "post" else None,
                comment_id=target_id if target_type == "comment" else None,
                reaction_type="like",
                active=active,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            self.db.add(reaction)
        elif changed:
            reaction.active = active
            reaction.updated_at = occurred_at
        if changed:
            delta = 1 if active else -1
            target.like_count = max(0, int(target.like_count or 0) + delta)
        self.db.flush()
        return SocialReactionState(
            target_type=target_type,
            target_public_id=public_id,
            active=active,
            count=max(0, int(target.like_count or 0)),
            changed=changed,
        )

    def reaction_active(
        self,
        *,
        actor_user_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
    ) -> bool:
        target_column = (
            SocialReaction.post_id
            if target_type == "post"
            else SocialReaction.comment_id
        )
        return bool(
            self.db.scalar(
                select(SocialReaction.active).where(
                    SocialReaction.actor_user_id == actor_user_id,
                    target_column == target_id,
                    SocialReaction.reaction_type == "like",
                )
            )
            or False
        )

    def create_report(
        self,
        *,
        reporter_user_id: uuid.UUID,
        idempotency_key: str,
        target_type: str,
        target_id: uuid.UUID,
        target_public_id: str,
        reason_code: str,
        reason_text: str,
        occurred_at: datetime,
    ) -> SocialReportState:
        existing = self.db.scalar(
            select(SocialReport).where(
                SocialReport.reporter_user_id == reporter_user_id,
                SocialReport.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            expected_id = existing.post_id if target_type == "post" else existing.comment_id
            if (
                expected_id != target_id
                or existing.reason_code != reason_code
                or str(existing.reason_text or "") != reason_text
            ):
                raise SocialIdempotencyConflict(
                    "举报幂等键已用于另一项内容"
                )
            return SocialReportState(
                public_id=existing.public_id,
                target_type=target_type,
                target_public_id=target_public_id,
                status=existing.status,
                created=False,
            )
        report = SocialReport(
            reporter_user_id=reporter_user_id,
            post_id=target_id if target_type == "post" else None,
            comment_id=target_id if target_type == "comment" else None,
            idempotency_key=idempotency_key,
            reason_code=reason_code,
            reason_text=reason_text or None,
            status="pending",
            extra_data={
                "schema": LOCAL_SOCIAL_SCHEMA,
                "target_public_id": target_public_id,
            },
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        self.db.add(report)
        if target_type == "post":
            post = self._post_row(target_id)
        else:
            comment = self.db.scalar(
                select(SocialComment).where(SocialComment.id == target_id)
            )
            if comment is None:
                raise SocialContentNotFound("评论不存在")
            post = self._post_row(comment.post_id)
        post.report_count = max(0, int(post.report_count or 0)) + 1
        self.db.flush()
        return SocialReportState(
            public_id=report.public_id,
            target_type=target_type,
            target_public_id=target_public_id,
            status=report.status,
            created=True,
        )

    def list_feed_candidates(
        self,
        *,
        not_before: datetime,
        before: datetime | None,
        limit: int,
    ) -> list[SocialPostView]:
        statement = select(SocialPost).where(
            SocialPost.status == "published",
            SocialPost.published_at >= not_before,
        )
        if before is not None:
            statement = statement.where(SocialPost.published_at < before)
        rows = list(
            self.db.scalars(
                statement.order_by(
                    SocialPost.published_at.desc(), SocialPost.id.desc()
                ).limit(min(max(1, int(limit)), 1_000))
            )
        )
        return [self._post_view(row) for row in rows]

    def list_posts_by_author(
        self,
        *,
        author_upstream_uid: str,
        before: datetime | None,
        limit: int,
        offset: int = 0,
    ) -> list[SocialPostView]:
        statement = select(SocialPost).where(
            SocialPost.author_upstream_uid == author_upstream_uid,
            SocialPost.status != "deleted",
        )
        if before is not None:
            statement = statement.where(SocialPost.published_at < before)
        rows = list(
            self.db.scalars(
                statement.order_by(
                    SocialPost.is_pinned.desc(),
                    SocialPost.published_at.desc(),
                    SocialPost.id.desc(),
                )
                .offset(max(0, int(offset)))
                .limit(min(max(1, int(limit)), 1_000))
            )
        )
        return [self._post_view(row) for row in rows]

    def list_comments(
        self,
        *,
        post_id: uuid.UUID,
        before: datetime | None,
        limit: int,
        offset: int,
    ) -> list[SocialCommentView]:
        statement = select(SocialComment).where(
            SocialComment.post_id == post_id,
            SocialComment.status == "active",
        )
        if before is not None:
            statement = statement.where(SocialComment.source_created_at < before)
        rows = list(
            self.db.scalars(
                statement.order_by(
                    SocialComment.source_created_at.desc(), SocialComment.id.desc()
                )
                .offset(max(0, int(offset)))
                .limit(min(max(1, int(limit)), 200))
            )
        )
        return [self._comment_view(row) for row in rows]

    def record_post_view(
        self,
        *,
        post_id: uuid.UUID,
        viewer_user_id: uuid.UUID,
        occurred_at: datetime,
    ) -> tuple[SocialPostView, bool]:
        viewed_on = occurred_at.date()
        statement = (
            insert(SocialPostViewEvent)
            .values(
                post_id=post_id,
                viewer_user_id=viewer_user_id,
                viewed_on=viewed_on,
                view_count=1,
                first_viewed_at=occurred_at,
                last_viewed_at=occurred_at,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="uq_social_post_views_daily")
            .returning(SocialPostViewEvent)
        )
        event = self.db.scalars(statement).first()
        counted = event is not None
        post = self._post_row(post_id)
        if counted:
            post.view_count = max(0, int(post.view_count or 0)) + 1
        else:
            existing = self.db.scalar(
                select(SocialPostViewEvent)
                .where(
                    SocialPostViewEvent.post_id == post_id,
                    SocialPostViewEvent.viewer_user_id == viewer_user_id,
                    SocialPostViewEvent.viewed_on == viewed_on,
                )
                .with_for_update()
            )
            if existing is None:
                raise RuntimeError(
                    "post view conflict occurred without an existing row"
                )
            if occurred_at > existing.last_viewed_at:
                existing.last_viewed_at = occurred_at
                existing.updated_at = occurred_at
        self.db.flush()
        return self._post_view(post), counted
