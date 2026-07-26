"""Business rules for Web-local canonical social content."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from .contracts import (
    LOCAL_SOCIAL_SCHEMA,
    VISIBLE_HISTORY_DAYS,
    CanonicalSocialStore,
    FeedItem,
    FeedScore,
    InvalidSocialContent,
    LegacyCommentInput,
    LegacyHistoryOutsideWindow,
    LegacyPostInput,
    SocialAuthorRef,
    SocialCommentMutation,
    SocialCommentView,
    SocialCommentsDisabled,
    SocialContentForbidden,
    SocialContentNotFound,
    SocialIdempotencyConflict,
    SocialMirrorIntent,
    SocialPermissionPolicy,
    SocialPostMutation,
    SocialPostView,
    SocialPrincipal,
    SocialReactionMutation,
    SocialReportMutation,
    SocialTopicMutation,
    SocialViewMutation,
)


POST_VISIBILITIES = frozenset({"public", "followers", "private"})
COMMENT_POLICIES = frozenset({"open", "followers", "disabled"})
POST_CATEGORIES = frozenset({"dynamic", "recruitment"})
POST_CATEGORY_ALIASES = {
    "dynamic": "dynamic",
    "动态": "dynamic",
    "recruitment": "recruitment",
    "招募令": "recruitment",
}
FEED_ORDERS = frozenset({"ranked", "latest"})
MAX_CLIENT_REQUEST_ID_LENGTH = 160
MAX_POST_TITLE_LENGTH = 240
MAX_POST_BODY_LENGTH = 20_000
MAX_COMMENT_BODY_LENGTH = 4_000
MAX_MEDIA_METADATA_BYTES = 64 * 1024
MAX_TOPIC_COUNT = 5
MAX_TOPIC_NAME_LENGTH = 80
MAX_REPORT_REASON_CODE_LENGTH = 80
MAX_REPORT_REASON_TEXT_LENGTH = 1_000
MAX_FEED_SEARCH_LENGTH = 160
MAX_FEED_PAGE_SIZE = 100
MAX_FEED_CANDIDATES = 1_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidSocialContent("时间必须包含时区")
    return value.astimezone(UTC)


def _normalize_identifier(value: object, *, limit: int) -> str:
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > limit
        or any(ord(character) < 32 for character in identifier)
    ):
        return ""
    return identifier


def _normalize_public_id(value: object, prefix: str) -> str:
    identifier = _normalize_identifier(value, limit=40)
    if not identifier.startswith(f"{prefix}_"):
        return ""
    suffix = identifier[len(prefix) + 1 :]
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        return ""
    return identifier


def _normalize_content_id(value: object, prefix: str) -> str:
    raw = _normalize_identifier(value, limit=256)
    if not raw:
        return ""
    if raw.startswith(f"{prefix}_"):
        return _normalize_public_id(raw, prefix)
    return raw


def _normalize_topics(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        candidates = (values,)
    elif isinstance(values, (list, tuple)):
        candidates = tuple(values)
    else:
        raise InvalidSocialContent("话题格式不合法")
    topics: list[str] = []
    normalized: set[str] = set()
    for candidate in candidates:
        name = " ".join(str(candidate or "").strip().split())
        key = name.casefold()
        if not name or len(name) > MAX_TOPIC_NAME_LENGTH:
            raise InvalidSocialContent("话题名称为空或超过长度限制")
        if key in normalized:
            continue
        normalized.add(key)
        topics.append(name)
    if len(topics) > MAX_TOPIC_COUNT:
        raise InvalidSocialContent("一条动态的话题数量超过限制")
    return tuple(topics)


def _normalize_category(value: object, *, allow_empty: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw and allow_empty:
        return ""
    category = POST_CATEGORY_ALIASES.get(raw.casefold()) or POST_CATEGORY_ALIASES.get(raw)
    if category not in POST_CATEGORIES:
        raise InvalidSocialContent("动态分类不合法")
    return category


def _normalized_search(value: object) -> str:
    query = " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().casefold().split()
    )
    if len(query) > MAX_FEED_SEARCH_LENGTH:
        raise InvalidSocialContent("动态搜索词超过长度限制")
    return query


def _location_tokens(value: object) -> set[str]:
    if isinstance(value, Mapping):
        tokens: set[str] = set()
        for key in (
            "city",
            "city_code",
            "city_name",
            "region",
            "region_code",
            "area",
        ):
            if key in value:
                tokens.update(_location_tokens(value.get(key)))
        return tokens
    if isinstance(value, (list, tuple, set, frozenset)):
        tokens: set[str] = set()
        for item in value:
            tokens.update(_location_tokens(item))
        return tokens
    normalized = " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().casefold().split()
    )
    if normalized in {
        "",
        "0",
        "none",
        "null",
        "undefined",
        "不限",
        "未知",
        "未设置",
        "暂未设置",
        "-",
    }:
        return set()
    tokens = {normalized}
    if normalized.endswith("市") and len(normalized) > 2:
        tokens.add(normalized[:-1])
    parts = [normalized]
    for separator in ("/", "·", ",", "，", " "):
        parts = [piece for part in parts for piece in part.split(separator)]
    if "-" in normalized and not normalized.startswith("local-"):
        parts.extend(normalized.split("-"))
    tail = next((piece.strip() for piece in reversed(parts) if piece.strip()), "")
    if tail:
        tokens.add(tail)
        if tail.endswith("市") and len(tail) > 2:
            tokens.add(tail[:-1])
    return tokens


def _post_matches_search(post: SocialPostView, query: str) -> bool:
    if not query:
        return True
    searchable = "\n".join(
        (
            post.title,
            post.body,
            post.author_display_name,
            post.author_upstream_uid,
            *(topic.name for topic in post.topics),
        )
    )
    return query in unicodedata.normalize("NFKC", searchable).casefold()


def _normalize_media(value: object) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise InvalidSocialContent("媒体描述必须是对象")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        normalized = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidSocialContent("媒体描述无法安全序列化") from exc
    if len(encoded) > MAX_MEDIA_METADATA_BYTES:
        raise InvalidSocialContent("媒体描述超过大小限制")
    return normalized


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _post_payload_digest(
    *,
    principal: SocialPrincipal,
    title: str,
    body: str,
    media: dict[str, Any],
    visibility: str,
    comment_policy: str,
    hide_comments: bool,
    category: str,
    topics: tuple[str, ...],
) -> str:
    return _digest(
        {
            "author_user_id": str(principal.user_id),
            "body": body,
            "category": category,
            "comment_policy": comment_policy,
            "hide_comments": hide_comments,
            "media": media,
            "schema": LOCAL_SOCIAL_SCHEMA,
            "title": title,
            "topics": topics,
            "visibility": visibility,
        }
    )


def _comment_payload_digest(
    *, post: SocialPostView, parent: SocialCommentView | None, body: str
) -> str:
    return _digest(
        {
            "body": body,
            "parent_public_id": parent.public_id if parent else "",
            "post_public_id": post.public_id,
            "schema": LOCAL_SOCIAL_SCHEMA,
        }
    )


def _legacy_payload_digest(item: LegacyPostInput) -> str:
    # digest 只纳入稳定的身份与内容字段：点赞/评论计数、作者快照、metadata
    # 与相对时间（"N分钟前"）解析出的 source_created_at 在两次运行之间会漂移，
    # 纳入 digest 会让失败重跑与停服前最后一轮必然触发幂等冲突。
    return _digest(
        {
            "author_upstream_uid": item.author_upstream_uid,
            "author_user_id": str(item.author_user_id or ""),
            "body": item.body,
            "comment_policy": item.comment_policy,
            "hide_comments": item.hide_comments,
            "media": item.media,
            "provider": item.provider,
            "schema": LOCAL_SOCIAL_SCHEMA,
            "title": item.title,
            "topics": item.topics,
            "upstream_id": item.upstream_id,
            "visibility": item.visibility,
        }
    )


def _legacy_comment_payload_digest(item: LegacyCommentInput) -> str:
    # 同 _legacy_payload_digest：只保留稳定身份与内容，可变计数、状态、
    # 快照与相对时间不参与幂等判定。
    return _digest(
        {
            "author_upstream_uid": item.author_upstream_uid,
            "author_user_id": str(item.author_user_id or ""),
            "body": item.body,
            "parent_upstream_id": item.parent_upstream_id,
            "post_upstream_id": item.post_upstream_id,
            "provider": item.provider,
            "schema": LOCAL_SOCIAL_SCHEMA,
            "upstream_id": item.upstream_id,
        }
    )


def _mirror(
    operation_type: str,
    *,
    aggregate_type: str,
    aggregate_public_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> SocialMirrorIntent:
    return SocialMirrorIntent(
        operation_type=operation_type,
        aggregate_type=aggregate_type,
        aggregate_public_id=aggregate_public_id,
        idempotency_key=idempotency_key,
        payload={"schema": LOCAL_SOCIAL_SCHEMA, **payload},
    )


class LocalMomentsService:
    """Canonical local writes first; compatibility is returned as an intent only."""

    def __init__(
        self,
        store: CanonicalSocialStore,
        permissions: SocialPermissionPolicy,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.store = store
        self.permissions = permissions
        self.clock = clock

    @staticmethod
    def _owns(principal: SocialPrincipal, post: SocialPostView) -> bool:
        return post.author_user_id == principal.user_id

    def _require_post(self, public_id: object, *, for_update: bool = False) -> SocialPostView:
        normalized = _normalize_content_id(public_id, "pst")
        if not normalized:
            raise InvalidSocialContent("动态 ID 不合法")
        post = self.store.get_post(normalized, for_update=for_update)
        if post is None:
            raise SocialContentNotFound("动态不存在")
        return post

    def _require_comment(
        self, public_id: object, *, for_update: bool = False
    ) -> SocialCommentView:
        normalized = _normalize_content_id(public_id, "cmt")
        if not normalized:
            raise InvalidSocialContent("评论 ID 不合法")
        comment = self.store.get_comment(normalized, for_update=for_update)
        if comment is None:
            raise SocialContentNotFound("评论不存在")
        return comment

    def _require_visible(self, principal: SocialPrincipal, post: SocialPostView) -> None:
        if self._owns(principal, post):
            return
        if post.status != "published" or not self.permissions.can_view(
            viewer=principal,
            author=post.author,
            visibility=post.visibility,
        ):
            raise SocialContentForbidden("无权查看该动态")

    def create_topic(
        self,
        *,
        principal: SocialPrincipal,
        name: object,
        description: object = "",
    ) -> SocialTopicMutation:
        normalized_topics = _normalize_topics((name,))
        topic_name = normalized_topics[0]
        topic_description = str(description or "").strip()
        if len(topic_description) > 1_000:
            raise InvalidSocialContent("话题描述超过长度限制")
        topic, created = self.store.create_topic(
            name=topic_name,
            normalized_name=topic_name.casefold(),
            description=topic_description,
            created_by_user_id=principal.user_id,
            occurred_at=_ensure_aware(self.clock()),
        )
        return SocialTopicMutation(
            topic=topic,
            created=created,
            mirror=_mirror(
                "social.topic.create",
                aggregate_type="topic",
                aggregate_public_id=topic.public_id,
                idempotency_key=f"topic:{topic.public_id}",
                payload={
                    "topic_public_id": topic.public_id,
                    "name": topic.name,
                    "description": topic_description,
                },
            ),
        )

    def topics(self, *, query: object = "", limit: int = 50):
        normalized_query = " ".join(str(query or "").strip().split()).casefold()
        if len(normalized_query) > MAX_TOPIC_NAME_LENGTH:
            raise InvalidSocialContent("话题查询超过长度限制")
        return self.store.list_topics(
            query=normalized_query,
            limit=min(max(1, int(limit)), 100),
        )

    def publish(
        self,
        *,
        principal: SocialPrincipal,
        client_request_id: object,
        title: object = "",
        body: object = "",
        media: object = None,
        visibility: object = "public",
        comment_policy: object = "open",
        hide_comments: object = False,
        category: object = "dynamic",
        topics: object = None,
    ) -> SocialPostMutation:
        request_id = _normalize_identifier(
            client_request_id, limit=MAX_CLIENT_REQUEST_ID_LENGTH
        )
        normalized_title = str(title or "").strip()
        normalized_body = str(body or "").strip()
        normalized_media = _normalize_media(media)
        normalized_visibility = str(visibility or "").strip().lower()
        normalized_comment_policy = str(comment_policy or "").strip().lower()
        normalized_category = _normalize_category(category)
        normalized_topics = _normalize_topics(topics)
        if not request_id:
            raise InvalidSocialContent("client_request_id 不合法")
        if len(normalized_title) > MAX_POST_TITLE_LENGTH:
            raise InvalidSocialContent("动态标题超过长度限制")
        if len(normalized_body) > MAX_POST_BODY_LENGTH:
            raise InvalidSocialContent("动态正文超过长度限制")
        if not normalized_body and not normalized_media:
            raise InvalidSocialContent("动态正文和媒体不能同时为空")
        if normalized_visibility not in POST_VISIBILITIES:
            raise InvalidSocialContent("动态可见范围不合法")
        if normalized_comment_policy not in COMMENT_POLICIES:
            raise InvalidSocialContent("评论策略不合法")
        occurred_at = _ensure_aware(self.clock())
        payload_digest = _post_payload_digest(
            principal=principal,
            title=normalized_title,
            body=normalized_body,
            media=normalized_media,
            visibility=normalized_visibility,
            comment_policy=normalized_comment_policy,
            hide_comments=bool(hide_comments),
            category=normalized_category,
            topics=normalized_topics,
        )
        post, created = self.store.create_post(
            principal=principal,
            client_request_id=request_id,
            payload_digest=payload_digest,
            title=normalized_title,
            body=normalized_body,
            media=normalized_media,
            visibility=normalized_visibility,
            comment_policy=normalized_comment_policy,
            hide_comments=bool(hide_comments),
            category=normalized_category,
            topics=normalized_topics,
            occurred_at=occurred_at,
        )
        return SocialPostMutation(
            post=post,
            created=created,
            mirror=_mirror(
                "social.post.publish",
                aggregate_type="post",
                aggregate_public_id=post.public_id,
                idempotency_key=f"publish:{principal.user_id}:{request_id}",
                payload={
                    "post_public_id": post.public_id,
                    "plate": "招募令" if post.category == "recruitment" else "动态",
                    "title": post.title,
                    "body": post.body,
                    "media": post.media,
                    "visibility": post.visibility,
                    "comment_policy": post.comment_policy,
                    "hide_comments": post.hide_comments,
                    "topics": [topic.name for topic in post.topics],
                },
            ),
        )

    def import_legacy_post(
        self,
        *,
        requested_by: SocialPrincipal,
        item: LegacyPostInput,
        visibility_verified: bool,
    ) -> SocialPostMutation:
        provider = _normalize_identifier(item.provider, limit=40)
        upstream_id = _normalize_identifier(item.upstream_id, limit=256)
        author_uid = _normalize_identifier(item.author_upstream_uid, limit=128)
        source_created_at = _ensure_aware(item.source_created_at)
        now = _ensure_aware(self.clock())
        if not provider or not upstream_id or not author_uid:
            raise InvalidSocialContent("历史动态来源标识不合法")
        if source_created_at > now + timedelta(minutes=5):
            raise InvalidSocialContent("历史动态时间晚于当前时间")
        if item.visibility not in POST_VISIBILITIES:
            raise InvalidSocialContent("历史动态可见范围不合法")
        if item.comment_policy not in COMMENT_POLICIES:
            raise InvalidSocialContent("历史动态评论策略不合法")
        if len(item.title) > MAX_POST_TITLE_LENGTH or len(item.body) > MAX_POST_BODY_LENGTH:
            raise InvalidSocialContent("历史动态正文超过长度限制")
        normalized_media = _normalize_media(item.media)
        if not item.body.strip() and not normalized_media:
            raise InvalidSocialContent("历史动态正文和媒体不能同时为空")
        normalized_topics = _normalize_topics(item.topics)
        normalized_item = LegacyPostInput(
            provider=provider,
            upstream_id=upstream_id,
            author_user_id=item.author_user_id,
            author_upstream_uid=author_uid,
            author_display_name=str(item.author_display_name or "")[:160],
            title=str(item.title or "").strip(),
            body=str(item.body or "").strip(),
            media=normalized_media,
            visibility=item.visibility,
            comment_policy=item.comment_policy,
            hide_comments=bool(item.hide_comments),
            source_created_at=source_created_at,
            topics=normalized_topics,
            author_snapshot=dict(item.author_snapshot or {}),
            metadata=dict(item.metadata or {}),
        )
        is_owner = item.author_user_id == requested_by.user_id
        import_scope = "owner" if is_owner else "visible-history"
        if not is_owner:
            cutoff = now - timedelta(days=VISIBLE_HISTORY_DAYS)
            if source_created_at < cutoff:
                raise LegacyHistoryOutsideWindow("可见历史动态超过 180 天迁移边界")
            if not visibility_verified or not self.permissions.can_view(
                viewer=requested_by,
                author=normalized_item_author(normalized_item),
                visibility=normalized_item.visibility,
            ):
                raise SocialContentForbidden("历史动态未通过可见性验证")
        post, created = self.store.import_legacy_post(
            requested_by=requested_by,
            item=normalized_item,
            import_scope=import_scope,
            payload_digest=_legacy_payload_digest(normalized_item),
            imported_at=now,
        )
        return SocialPostMutation(post=post, created=created, mirror=None)

    def import_legacy_comment(
        self,
        *,
        requested_by: SocialPrincipal,
        post: SocialPostView,
        item: LegacyCommentInput,
        parent: SocialCommentView | None = None,
    ) -> SocialCommentMutation:
        provider = _normalize_identifier(item.provider, limit=40)
        upstream_id = _normalize_identifier(item.upstream_id, limit=256)
        post_upstream_id = _normalize_identifier(item.post_upstream_id, limit=256)
        parent_upstream_id = _normalize_identifier(
            item.parent_upstream_id, limit=256
        ) if str(item.parent_upstream_id or "").strip() else ""
        author_uid = _normalize_identifier(item.author_upstream_uid, limit=128)
        source_created_at = _ensure_aware(item.source_created_at)
        now = _ensure_aware(self.clock())
        body = str(item.body or "").strip()
        if not provider or not upstream_id or not post_upstream_id or not author_uid:
            raise InvalidSocialContent("历史评论来源标识不合法")
        if source_created_at > now + timedelta(minutes=5):
            raise InvalidSocialContent("历史评论时间晚于当前时间")
        if source_created_at < now - timedelta(days=VISIBLE_HISTORY_DAYS):
            raise LegacyHistoryOutsideWindow("历史评论超过 180 天迁移边界")
        if not body or len(body) > MAX_COMMENT_BODY_LENGTH:
            raise InvalidSocialContent("历史评论为空或超过长度限制")
        if item.status not in {"active", "hidden"}:
            raise InvalidSocialContent("历史评论状态不合法")
        for value in (item.like_count, item.reply_count):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 2**31 - 1
            ):
                raise InvalidSocialContent("历史评论计数不合法")
        if parent_upstream_id:
            if parent is None:
                raise SocialContentNotFound("历史评论的父评论尚未迁移")
            if parent.post_id != post.id:
                raise InvalidSocialContent("历史评论的父评论不属于目标动态")

        normalized_item = LegacyCommentInput(
            provider=provider,
            upstream_id=upstream_id,
            post_upstream_id=post_upstream_id,
            parent_upstream_id=parent_upstream_id,
            author_user_id=item.author_user_id,
            author_upstream_uid=author_uid,
            author_display_name=str(item.author_display_name or "")[:160],
            body=body,
            status=item.status,
            like_count=item.like_count,
            reply_count=item.reply_count,
            source_created_at=source_created_at,
            author_snapshot=dict(item.author_snapshot or {}),
            metadata=dict(item.metadata or {}),
        )
        comment, created = self.store.import_legacy_comment(
            requested_by=requested_by,
            post=post,
            parent=parent,
            item=normalized_item,
            import_scope=(
                "owner"
                if item.author_user_id == requested_by.user_id
                else "visible-history"
            ),
            payload_digest=_legacy_comment_payload_digest(normalized_item),
            imported_at=now,
        )
        return SocialCommentMutation(comment=comment, created=created, mirror=None)

    def delete_post(
        self, *, principal: SocialPrincipal, post_public_id: object
    ) -> SocialPostMutation:
        post = self._require_post(post_public_id, for_update=True)
        if not self._owns(principal, post):
            raise SocialContentForbidden("只能删除自己的动态")
        now = _ensure_aware(self.clock())
        updated = (
            post
            if post.status == "deleted"
            else self.store.set_post_status(post.id, status="deleted", occurred_at=now)
        )
        return SocialPostMutation(
            post=updated,
            created=False,
            mirror=_mirror(
                "social.post.delete",
                aggregate_type="post",
                aggregate_public_id=updated.public_id,
                idempotency_key=f"delete:{updated.public_id}",
                payload={"post_public_id": updated.public_id},
            ),
        )

    def set_visibility(
        self,
        *,
        principal: SocialPrincipal,
        post_public_id: object,
        visibility: object,
    ) -> SocialPostMutation:
        post = self._require_post(post_public_id, for_update=True)
        normalized = str(visibility or "").strip().lower()
        if normalized not in POST_VISIBILITIES:
            raise InvalidSocialContent("动态可见范围不合法")
        if not self._owns(principal, post) or post.status == "deleted":
            raise SocialContentForbidden("无权修改动态可见范围")
        updated = (
            post
            if post.visibility == normalized
            else self.store.set_post_visibility(post.id, visibility=normalized)
        )
        return SocialPostMutation(
            post=updated,
            created=False,
            mirror=_mirror(
                "social.post.visibility",
                aggregate_type="post",
                aggregate_public_id=updated.public_id,
                idempotency_key=f"visibility:{updated.public_id}:{normalized}",
                payload={"post_public_id": updated.public_id, "visibility": normalized},
            ),
        )

    def set_pinned(
        self,
        *,
        principal: SocialPrincipal,
        post_public_id: object,
        pinned: bool,
    ) -> SocialPostMutation:
        post = self._require_post(post_public_id, for_update=True)
        if not self._owns(principal, post) or post.status == "deleted":
            raise SocialContentForbidden("无权置顶该动态")
        now = _ensure_aware(self.clock())
        updated = (
            post
            if post.is_pinned == bool(pinned)
            else self.store.set_post_pin(post.id, pinned=bool(pinned), occurred_at=now)
        )
        return SocialPostMutation(
            post=updated,
            created=False,
            mirror=_mirror(
                "social.post.pin",
                aggregate_type="post",
                aggregate_public_id=updated.public_id,
                idempotency_key=f"pin:{updated.public_id}:{int(bool(pinned))}",
                payload={"post_public_id": updated.public_id, "pinned": bool(pinned)},
            ),
        )

    def toggle_pinned(
        self, *, principal: SocialPrincipal, post_public_id: object
    ) -> SocialPostMutation:
        post = self._require_post(post_public_id)
        return self.set_pinned(
            principal=principal,
            post_public_id=post.public_id,
            pinned=not post.is_pinned,
        )

    def configure_comments(
        self,
        *,
        principal: SocialPrincipal,
        post_public_id: object,
        comment_policy: object,
        hide_comments: bool | None = None,
    ) -> SocialPostMutation:
        post = self._require_post(post_public_id, for_update=True)
        normalized = str(comment_policy or "").strip().lower()
        if normalized not in COMMENT_POLICIES:
            raise InvalidSocialContent("评论策略不合法")
        if not self._owns(principal, post) or post.status == "deleted":
            raise SocialContentForbidden("无权修改评论策略")
        updated = self.store.set_comment_policy(
            post.id,
            comment_policy=normalized,
            hide_comments=hide_comments,
        )
        return SocialPostMutation(
            post=updated,
            created=False,
            mirror=_mirror(
                "social.post.comment-policy",
                aggregate_type="post",
                aggregate_public_id=updated.public_id,
                idempotency_key=(
                    f"comment-policy:{updated.public_id}:{normalized}:"
                    f"{int(updated.hide_comments)}"
                ),
                payload={
                    "post_public_id": updated.public_id,
                    "comment_policy": updated.comment_policy,
                    "hide_comments": updated.hide_comments,
                },
            ),
        )

    def comment(
        self,
        *,
        principal: SocialPrincipal,
        post_public_id: object,
        client_request_id: object,
        body: object,
        parent_comment_public_id: object = "",
    ) -> SocialCommentMutation:
        request_id = _normalize_identifier(
            client_request_id, limit=MAX_CLIENT_REQUEST_ID_LENGTH
        )
        normalized_body = str(body or "").strip()
        if not request_id:
            raise InvalidSocialContent("client_request_id 不合法")
        if not normalized_body or len(normalized_body) > MAX_COMMENT_BODY_LENGTH:
            raise InvalidSocialContent("评论为空或超过长度限制")
        post = self._require_post(post_public_id, for_update=True)
        self._require_visible(principal, post)
        if post.status != "published" or post.comment_policy == "disabled":
            raise SocialCommentsDisabled("该动态已关闭评论")
        if not self._owns(principal, post) and not self.permissions.can_comment(
            viewer=principal,
            author=post.author,
            comment_policy=post.comment_policy,
        ):
            raise SocialCommentsDisabled("当前关系不允许评论")
        parent: SocialCommentView | None = None
        if str(parent_comment_public_id or "").strip():
            parent = self._require_comment(parent_comment_public_id)
            if parent.post_id != post.id or parent.status != "active":
                raise InvalidSocialContent("回复目标不属于该动态或已不可用")
        now = _ensure_aware(self.clock())
        comment, created = self.store.create_comment(
            principal=principal,
            post=post,
            parent=parent,
            client_request_id=request_id,
            payload_digest=_comment_payload_digest(
                post=post, parent=parent, body=normalized_body
            ),
            body=normalized_body,
            occurred_at=now,
        )
        return SocialCommentMutation(
            comment=comment,
            created=created,
            mirror=_mirror(
                "social.comment.publish",
                aggregate_type="comment",
                aggregate_public_id=comment.public_id,
                idempotency_key=f"comment:{principal.user_id}:{request_id}",
                payload={
                    "post_public_id": post.public_id,
                    "comment_public_id": comment.public_id,
                    "parent_public_id": comment.parent_public_id,
                    "body": comment.body,
                },
            ),
        )

    def delete_comment(
        self, *, principal: SocialPrincipal, comment_public_id: object
    ) -> SocialCommentMutation:
        # 全部评论写路径的锁序固定为「先 Post 后 Comment」（与 comment()
        # 一致），否则与并发回复互为反序会造成数据库死锁。先无锁预读定位
        # 所属 Post，再按序加锁。
        preview = self._require_comment(comment_public_id)
        post = self._require_post(preview.post_public_id, for_update=True)
        comment = self._require_comment(comment_public_id, for_update=True)
        if comment.author_user_id != principal.user_id and not self._owns(principal, post):
            raise SocialContentForbidden("无权删除该评论")
        now = _ensure_aware(self.clock())
        updated = (
            comment
            if comment.status == "deleted"
            else self.store.set_comment_status(
                comment.id, status="deleted", occurred_at=now
            )
        )
        return SocialCommentMutation(
            comment=updated,
            created=False,
            mirror=_mirror(
                "social.comment.delete",
                aggregate_type="comment",
                aggregate_public_id=updated.public_id,
                idempotency_key=f"delete-comment:{updated.public_id}",
                payload={
                    "post_public_id": updated.post_public_id,
                    "comment_public_id": updated.public_id,
                },
            ),
        )

    def set_comment_hidden(
        self,
        *,
        principal: SocialPrincipal,
        comment_public_id: object,
        hidden: bool,
    ) -> SocialCommentMutation:
        # 锁序同 delete_comment：先 Post 后 Comment，避免与并发回复死锁。
        preview = self._require_comment(comment_public_id)
        post = self._require_post(preview.post_public_id, for_update=True)
        comment = self._require_comment(comment_public_id, for_update=True)
        if not self._owns(principal, post) or comment.status == "deleted":
            raise SocialContentForbidden("只有动态作者可以禁用评论")
        status = "hidden" if hidden else "active"
        now = _ensure_aware(self.clock())
        updated = (
            comment
            if comment.status == status
            else self.store.set_comment_status(comment.id, status=status, occurred_at=now)
        )
        return SocialCommentMutation(
            comment=updated,
            created=False,
            mirror=_mirror(
                "social.comment.moderate",
                aggregate_type="comment",
                aggregate_public_id=updated.public_id,
                idempotency_key=f"moderate-comment:{updated.public_id}:{status}",
                payload={
                    "post_public_id": updated.post_public_id,
                    "comment_public_id": updated.public_id,
                    "hidden": hidden,
                },
            ),
        )

    def set_like(
        self,
        *,
        principal: SocialPrincipal,
        target_type: object,
        target_public_id: object,
        active: bool | None,
    ) -> SocialReactionMutation:
        normalized_type = str(target_type or "").strip().lower()
        if normalized_type == "post":
            post = self._require_post(target_public_id, for_update=True)
            self._require_visible(principal, post)
            target_id = post.id
            public_id = post.public_id
        elif normalized_type == "comment":
            # 锁序同 delete_comment：先 Post 后 Comment，避免与并发回复死锁。
            preview = self._require_comment(target_public_id)
            post = self._require_post(preview.post_public_id, for_update=True)
            comment = self._require_comment(target_public_id, for_update=True)
            if comment.status != "active":
                raise SocialContentForbidden("评论已不可用")
            self._require_visible(principal, post)
            target_id = comment.id
            public_id = comment.public_id
        else:
            raise InvalidSocialContent("点赞目标类型不合法")
        desired = (
            not self.store.reaction_active(
                actor_user_id=principal.user_id,
                target_type=normalized_type,
                target_id=target_id,
            )
            if active is None
            else bool(active)
        )
        now = _ensure_aware(self.clock())
        state = self.store.set_like(
            actor_user_id=principal.user_id,
            target_type=normalized_type,
            target_id=target_id,
            active=desired,
            occurred_at=now,
        )
        return SocialReactionMutation(
            reaction=state,
            mirror=_mirror(
                "social.reaction.like",
                aggregate_type=normalized_type,
                aggregate_public_id=public_id,
                idempotency_key=(
                    f"like:{principal.user_id}:{normalized_type}:{public_id}:"
                    f"{int(desired)}"
                ),
                payload={
                    "target_type": normalized_type,
                    "target_public_id": public_id,
                    "active": desired,
                },
            )
            if state.changed
            else None,
        )

    def report(
        self,
        *,
        principal: SocialPrincipal,
        target_type: object,
        target_public_id: object,
        idempotency_key: object,
        reason_code: object,
        reason_text: object = "",
    ) -> SocialReportMutation:
        target = str(target_type or "").strip().lower()
        key = _normalize_identifier(idempotency_key, limit=MAX_CLIENT_REQUEST_ID_LENGTH)
        code = _normalize_identifier(reason_code, limit=MAX_REPORT_REASON_CODE_LENGTH)
        detail = str(reason_text or "").strip()
        if not key or not code or len(detail) > MAX_REPORT_REASON_TEXT_LENGTH:
            raise InvalidSocialContent("举报参数不合法")
        if target == "post":
            post = self._require_post(target_public_id)
            self._require_visible(principal, post)
            if self._owns(principal, post):
                raise SocialContentForbidden("不能举报自己的动态")
            target_id = post.id
            public_id = post.public_id
        elif target == "comment":
            comment = self._require_comment(target_public_id)
            if comment.status == "deleted":
                raise SocialContentNotFound("评论不存在")
            post = self._require_post(comment.post_public_id)
            self._require_visible(principal, post)
            if comment.author_user_id == principal.user_id:
                raise SocialContentForbidden("不能举报自己的评论")
            target_id = comment.id
            public_id = comment.public_id
        else:
            raise InvalidSocialContent("举报目标类型不合法")
        report = self.store.create_report(
            reporter_user_id=principal.user_id,
            idempotency_key=key,
            target_type=target,
            target_id=target_id,
            target_public_id=public_id,
            reason_code=code,
            reason_text=detail,
            occurred_at=_ensure_aware(self.clock()),
        )
        return SocialReportMutation(
            report=report,
            mirror=_mirror(
                "social.report.create",
                aggregate_type=target,
                aggregate_public_id=public_id,
                idempotency_key=f"report:{principal.user_id}:{key}",
                payload={
                    "report_public_id": report.public_id,
                    "target_type": target,
                    "target_public_id": public_id,
                    "reason_code": code,
                    "reason_text": detail,
                },
            )
            if report.created
            else None,
        )

    def posts_by_author(
        self,
        *,
        principal: SocialPrincipal,
        author_upstream_uid: object,
        before: datetime | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> list[SocialPostView]:
        author_uid = _normalize_identifier(author_upstream_uid, limit=128)
        if not author_uid:
            raise InvalidSocialContent("作者 UID 不合法")
        normalized_before = _ensure_aware(before) if before is not None else None
        try:
            page_size = min(max(1, int(limit)), MAX_FEED_PAGE_SIZE)
            page_offset = max(0, int(offset))
        except (TypeError, ValueError) as exc:
            raise InvalidSocialContent("动态分页参数不合法") from exc
        candidates = self.store.list_posts_by_author(
            author_upstream_uid=author_uid,
            before=normalized_before,
            limit=min(MAX_FEED_CANDIDATES, max(page_size * 5, page_size)),
            offset=page_offset,
        )
        visible: list[SocialPostView] = []
        for post in candidates:
            if post.status == "deleted":
                continue
            if self._owns(principal, post) or (
                post.status == "published"
                and self.permissions.can_view(
                    viewer=principal,
                    author=post.author,
                    visibility=post.visibility,
                )
            ):
                visible.append(post)
            if len(visible) >= page_size:
                break
        return visible

    def comments(
        self,
        *,
        principal: SocialPrincipal,
        post_public_id: object,
        before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SocialCommentView]:
        post = self._require_post(post_public_id)
        self._require_visible(principal, post)
        if post.status != "published" and not self._owns(principal, post):
            raise SocialContentForbidden("无权查看评论")
        if post.hide_comments and not self._owns(principal, post):
            return []
        normalized_before = _ensure_aware(before) if before is not None else None
        try:
            page_size = min(max(1, int(limit)), 200)
            page_offset = max(0, int(offset))
        except (TypeError, ValueError) as exc:
            raise InvalidSocialContent("评论分页参数不合法") from exc
        return self.store.list_comments(
            post_id=post.id,
            before=normalized_before,
            limit=page_size,
            offset=page_offset,
        )

    def record_view(
        self,
        *,
        principal: SocialPrincipal,
        post_public_id: object,
    ) -> SocialViewMutation:
        post = self._require_post(post_public_id, for_update=True)
        self._require_visible(principal, post)
        if post.status != "published":
            raise SocialContentForbidden("动态已不可浏览")
        occurred_at = _ensure_aware(self.clock())
        updated, counted = self.store.record_post_view(
            post_id=post.id,
            viewer_user_id=principal.user_id,
            occurred_at=occurred_at,
        )
        return SocialViewMutation(
            post=updated,
            counted=counted,
            mirror=_mirror(
                "social.post.view",
                aggregate_type="post",
                aggregate_public_id=post.public_id,
                idempotency_key=(
                    f"view:{post.public_id}:{principal.user_id}:"
                    f"{occurred_at.date().isoformat()}"
                ),
                payload={"post_public_id": post.public_id, "type": "pv"},
            )
            if counted
            else None,
        )

    def feed(
        self,
        *,
        principal: SocialPrincipal,
        before: datetime | None = None,
        limit: int = 30,
        query: object = "",
        following_only: bool = False,
        region: object = (),
        category: object = "",
        order: object = "ranked",
    ) -> list[FeedItem]:
        now = _ensure_aware(self.clock())
        normalized_before = _ensure_aware(before) if before is not None else None
        page_size = min(max(1, int(limit)), MAX_FEED_PAGE_SIZE)
        normalized_query = _normalized_search(query)
        normalized_category = _normalize_category(category, allow_empty=True)
        normalized_region = _location_tokens(region)
        normalized_order = str(order or "ranked").strip().lower()
        if normalized_order not in FEED_ORDERS:
            raise InvalidSocialContent("动态排序方式不合法")
        candidates = self.store.list_feed_candidates(
            not_before=now - timedelta(days=VISIBLE_HISTORY_DAYS),
            before=normalized_before,
            limit=min(MAX_FEED_CANDIDATES, max(page_size * 10, page_size)),
        )
        ranked: list[FeedItem] = []
        for post in candidates:
            if post.status != "published":
                continue
            own = self._owns(principal, post)
            if not own and not self.permissions.can_view(
                viewer=principal,
                author=post.author,
                visibility=post.visibility,
            ):
                continue
            if normalized_category and post.category != normalized_category:
                continue
            if normalized_region and not (
                normalized_region & _location_tokens(post.author_snapshot)
            ):
                continue
            if not _post_matches_search(post, normalized_query):
                continue
            following = False if own else self.permissions.is_following(
                viewer=principal, author=post.author
            )
            if following_only and not following:
                continue
            ranked.append(
                FeedItem(
                    post=post,
                    score=explain_feed_score(
                        post=post,
                        now=now,
                        following=following,
                    ),
                )
            )
        if normalized_order == "latest":
            ranked.sort(
                key=lambda item: (item.post.published_at, item.post.public_id),
                reverse=True,
            )
        else:
            ranked.sort(
                key=lambda item: (
                    item.score.total,
                    item.post.published_at,
                    item.post.public_id,
                ),
                reverse=True,
            )
        return ranked[:page_size]


def normalized_item_author(item: LegacyPostInput) -> SocialAuthorRef:
    return SocialAuthorRef(item.author_user_id, item.author_upstream_uid)


def explain_feed_score(
    *, post: SocialPostView, now: datetime, following: bool
) -> FeedScore:
    """Return a deterministic, inspectable time/follow/activity score."""

    current = _ensure_aware(now)
    published = _ensure_aware(post.published_at)
    age_days = max(0.0, (current - published).total_seconds() / 86_400)
    recency = max(0.0, 1.0 - age_days / VISIBLE_HISTORY_DAYS)
    following_score = 1.0 if following else 0.0
    activity_units = (
        max(0, post.like_count)
        + max(0, post.comment_count) * 2
        + max(0, post.view_count) * 0.05
    )
    activity = min(1.0, math.log1p(activity_units) / math.log1p(1_000))
    total = recency * 0.65 + following_score * 0.25 + activity * 0.10
    return FeedScore(
        total=round(total, 6),
        recency=round(recency, 6),
        following=following_score,
        activity=round(activity, 6),
        explanation=(
            f"recency=0.65*{recency:.6f}",
            f"following=0.25*{following_score:.0f}",
            f"activity=0.10*{activity:.6f}",
        ),
    )
