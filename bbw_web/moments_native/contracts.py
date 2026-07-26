"""Contracts for the Web-local canonical social-content domain.

The domain deliberately has no dependency on ``bbw_web.persistence``, the
legacy BFF, Banghua, or an HTTP client.  Relationship and profile visibility
decisions are injected through ``SocialPermissionPolicy``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


LOCAL_SOCIAL_PROVIDER = "web-local"
LEGACY_SOCIAL_PROVIDER = "beibeiwu"
LOCAL_SOCIAL_SCHEMA = 1
VISIBLE_HISTORY_DAYS = 180


class SocialContentError(RuntimeError):
    code = "social_content_error"


class InvalidSocialContent(SocialContentError):
    code = "invalid_social_content"


class SocialContentNotFound(SocialContentError):
    code = "social_content_not_found"


class SocialContentForbidden(SocialContentError):
    code = "social_content_forbidden"


class SocialCommentsDisabled(SocialContentError):
    code = "social_comments_disabled"


class SocialIdempotencyConflict(SocialContentError):
    code = "social_idempotency_conflict"


class LegacyHistoryOutsideWindow(SocialContentError):
    code = "legacy_history_outside_window"


@dataclass(frozen=True, slots=True)
class SocialPrincipal:
    user_id: uuid.UUID
    upstream_uid: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class SocialAuthorRef:
    user_id: uuid.UUID | None
    upstream_uid: str


@dataclass(frozen=True, slots=True)
class SocialTopicView:
    id: uuid.UUID
    public_id: str
    name: str
    post_count: int


@dataclass(frozen=True, slots=True)
class SocialPostView:
    id: uuid.UUID
    public_id: str
    author_user_id: uuid.UUID | None
    author_upstream_uid: str
    author_display_name: str
    source: str
    title: str
    body: str
    media: dict[str, Any]
    visibility: str
    comment_policy: str
    hide_comments: bool
    status: str
    is_pinned: bool
    like_count: int
    comment_count: int
    view_count: int
    report_count: int
    source_created_at: datetime
    published_at: datetime
    topics: tuple[SocialTopicView, ...] = ()
    author_snapshot: dict[str, Any] = field(default_factory=dict)
    category: str = "dynamic"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def author(self) -> SocialAuthorRef:
        return SocialAuthorRef(self.author_user_id, self.author_upstream_uid)


@dataclass(frozen=True, slots=True)
class SocialCommentView:
    id: uuid.UUID
    public_id: str
    post_id: uuid.UUID
    post_public_id: str
    parent_comment_id: uuid.UUID | None
    parent_public_id: str
    author_user_id: uuid.UUID | None
    author_upstream_uid: str
    author_display_name: str
    source: str
    body: str
    status: str
    like_count: int
    reply_count: int
    source_created_at: datetime

    @property
    def author(self) -> SocialAuthorRef:
        return SocialAuthorRef(self.author_user_id, self.author_upstream_uid)


@dataclass(frozen=True, slots=True)
class SocialMirrorIntent:
    """A compatibility outbox intent; constructing it performs no network I/O."""

    operation_type: str
    aggregate_type: str
    aggregate_public_id: str
    idempotency_key: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SocialPostMutation:
    post: SocialPostView
    created: bool
    mirror: SocialMirrorIntent | None


@dataclass(frozen=True, slots=True)
class SocialCommentMutation:
    comment: SocialCommentView
    created: bool
    mirror: SocialMirrorIntent | None


@dataclass(frozen=True, slots=True)
class SocialTopicMutation:
    topic: SocialTopicView
    created: bool
    mirror: SocialMirrorIntent | None


@dataclass(frozen=True, slots=True)
class SocialReactionState:
    target_type: str
    target_public_id: str
    active: bool
    count: int
    changed: bool


@dataclass(frozen=True, slots=True)
class SocialReactionMutation:
    reaction: SocialReactionState
    mirror: SocialMirrorIntent | None


@dataclass(frozen=True, slots=True)
class SocialReportState:
    public_id: str
    target_type: str
    target_public_id: str
    status: str
    created: bool


@dataclass(frozen=True, slots=True)
class SocialReportMutation:
    report: SocialReportState
    mirror: SocialMirrorIntent | None


@dataclass(frozen=True, slots=True)
class SocialViewMutation:
    post: SocialPostView
    counted: bool
    mirror: SocialMirrorIntent | None


@dataclass(frozen=True, slots=True)
class FeedScore:
    total: float
    recency: float
    following: float
    activity: float
    explanation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeedItem:
    post: SocialPostView
    score: FeedScore


@dataclass(frozen=True, slots=True)
class LegacyPostInput:
    provider: str
    upstream_id: str
    author_user_id: uuid.UUID | None
    author_upstream_uid: str
    author_display_name: str
    title: str
    body: str
    media: dict[str, Any]
    visibility: str
    comment_policy: str
    hide_comments: bool
    source_created_at: datetime
    topics: tuple[str, ...] = ()
    author_snapshot: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LegacyCommentInput:
    provider: str
    upstream_id: str
    post_upstream_id: str
    parent_upstream_id: str
    author_user_id: uuid.UUID | None
    author_upstream_uid: str
    author_display_name: str
    body: str
    status: str
    like_count: int
    reply_count: int
    source_created_at: datetime
    author_snapshot: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


@runtime_checkable
class SocialPermissionPolicy(Protocol):
    """Profile/relationship-backed access decisions supplied by the caller."""

    def can_view(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
        visibility: str,
    ) -> bool: ...

    def can_comment(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
        comment_policy: str,
    ) -> bool: ...

    def is_following(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
    ) -> bool: ...


@runtime_checkable
class CanonicalSocialStore(Protocol):
    def create_topic(
        self,
        *,
        name: str,
        normalized_name: str,
        description: str,
        created_by_user_id: uuid.UUID,
        occurred_at: datetime,
    ) -> tuple[SocialTopicView, bool]: ...

    def list_topics(self, *, query: str, limit: int) -> list[SocialTopicView]: ...

    def create_post(
        self,
        *,
        principal: SocialPrincipal,
        client_request_id: str,
        payload_digest: str,
        title: str,
        body: str,
        media: dict[str, Any],
        visibility: str,
        comment_policy: str,
        hide_comments: bool,
        category: str,
        topics: tuple[str, ...],
        occurred_at: datetime,
    ) -> tuple[SocialPostView, bool]: ...

    def import_legacy_post(
        self,
        *,
        requested_by: SocialPrincipal,
        item: LegacyPostInput,
        import_scope: str,
        payload_digest: str,
        imported_at: datetime,
    ) -> tuple[SocialPostView, bool]: ...

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
    ) -> tuple[SocialCommentView, bool]: ...

    def get_post(self, public_id: str, *, for_update: bool = False) -> SocialPostView | None: ...

    def set_post_status(
        self, post_id: uuid.UUID, *, status: str, occurred_at: datetime
    ) -> SocialPostView: ...

    def set_post_visibility(
        self, post_id: uuid.UUID, *, visibility: str
    ) -> SocialPostView: ...

    def set_post_pin(
        self, post_id: uuid.UUID, *, pinned: bool, occurred_at: datetime
    ) -> SocialPostView: ...

    def set_comment_policy(
        self,
        post_id: uuid.UUID,
        *,
        comment_policy: str,
        hide_comments: bool | None,
    ) -> SocialPostView: ...

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
    ) -> tuple[SocialCommentView, bool]: ...

    def get_comment(
        self, public_id: str, *, for_update: bool = False
    ) -> SocialCommentView | None: ...

    def set_comment_status(
        self, comment_id: uuid.UUID, *, status: str, occurred_at: datetime
    ) -> SocialCommentView: ...

    def set_like(
        self,
        *,
        actor_user_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        active: bool,
        occurred_at: datetime,
    ) -> SocialReactionState: ...

    def reaction_active(
        self,
        *,
        actor_user_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
    ) -> bool: ...

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
    ) -> SocialReportState: ...

    def list_feed_candidates(
        self,
        *,
        not_before: datetime,
        before: datetime | None,
        limit: int,
    ) -> list[SocialPostView]: ...

    def list_posts_by_author(
        self,
        *,
        author_upstream_uid: str,
        before: datetime | None,
        limit: int,
        offset: int = 0,
    ) -> list[SocialPostView]: ...

    def list_comments(
        self,
        *,
        post_id: uuid.UUID,
        before: datetime | None,
        limit: int,
        offset: int,
    ) -> list[SocialCommentView]: ...

    def record_post_view(
        self,
        *,
        post_id: uuid.UUID,
        viewer_user_id: uuid.UUID,
        occurred_at: datetime,
    ) -> tuple[SocialPostView, bool]: ...
