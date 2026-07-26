"""SQLAlchemy access policy backed by Web-local canonical relationships."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from bbw_prod.models import ExternalAccount, Relationship, User

from .contracts import (
    LEGACY_SOCIAL_PROVIDER,
    LOCAL_SOCIAL_PROVIDER,
    SocialAuthorRef,
    SocialPrincipal,
)


ACTIVE_AUDIENCE_KINDS = ("follow", "friend")


class SqlAlchemySocialPermissionPolicy:
    """Deny-wins permissions for public/followers/private posts and comments."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._author_ids: dict[str, uuid.UUID | None] = {}
        self._blocked_pairs: dict[tuple[uuid.UUID, str], bool] = {}
        self._following_pairs: dict[tuple[uuid.UUID, str], bool] = {}

    def _author_user_id(self, author: SocialAuthorRef) -> uuid.UUID | None:
        if author.user_id is not None:
            return author.user_id
        if author.upstream_uid in self._author_ids:
            return self._author_ids[author.upstream_uid]
        resolved = self.db.scalar(
            select(ExternalAccount.user_id)
            .join(User, User.id == ExternalAccount.user_id)
            .where(
                ExternalAccount.provider == LEGACY_SOCIAL_PROVIDER,
                ExternalAccount.upstream_uid == author.upstream_uid,
                User.status == "active",
                User.disabled_at.is_(None),
            )
            .limit(1)
        )
        self._author_ids[author.upstream_uid] = resolved
        return resolved

    def _blocked(
        self, *, viewer: SocialPrincipal, author: SocialAuthorRef
    ) -> bool:
        cache_key = (viewer.user_id, author.upstream_uid)
        if cache_key in self._blocked_pairs:
            return self._blocked_pairs[cache_key]
        author_user_id = self._author_user_id(author)
        if author_user_id is None:
            # We can still honor a viewer-owned block against an imported UID;
            # reverse blocks require a locally resolvable author account.
            conditions = (
                and_(
                    Relationship.owner_user_id == viewer.user_id,
                    Relationship.subject_upstream_uid == author.upstream_uid,
                ),
            )
        else:
            conditions = (
                and_(
                    Relationship.owner_user_id == viewer.user_id,
                    Relationship.subject_upstream_uid == author.upstream_uid,
                ),
                and_(
                    Relationship.owner_user_id == author_user_id,
                    Relationship.subject_upstream_uid == viewer.upstream_uid,
                ),
            )
        blocked = (
            self.db.scalar(
                select(Relationship.id)
                .where(
                    Relationship.provider == LOCAL_SOCIAL_PROVIDER,
                    Relationship.kind == "blacklist",
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                    or_(*conditions),
                )
                .limit(1)
            )
            is not None
        )
        self._blocked_pairs[cache_key] = blocked
        return blocked

    def is_following(
        self, *, viewer: SocialPrincipal, author: SocialAuthorRef
    ) -> bool:
        cache_key = (viewer.user_id, author.upstream_uid)
        if cache_key in self._following_pairs:
            return self._following_pairs[cache_key]
        if author.user_id == viewer.user_id or self._blocked(
            viewer=viewer, author=author
        ):
            self._following_pairs[cache_key] = False
            return False
        following = (
            self.db.scalar(
                select(Relationship.id)
                .where(
                    Relationship.owner_user_id == viewer.user_id,
                    Relationship.provider == LOCAL_SOCIAL_PROVIDER,
                    Relationship.subject_upstream_uid == author.upstream_uid,
                    Relationship.kind.in_(ACTIVE_AUDIENCE_KINDS),
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                )
                .limit(1)
            )
            is not None
        )
        self._following_pairs[cache_key] = following
        return following

    def can_view(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
        visibility: str,
    ) -> bool:
        if author.user_id == viewer.user_id:
            return True
        if visibility == "private" or self._blocked(viewer=viewer, author=author):
            return False
        if visibility == "public":
            return True
        return visibility == "followers" and self.is_following(
            viewer=viewer, author=author
        )

    def can_comment(
        self,
        *,
        viewer: SocialPrincipal,
        author: SocialAuthorRef,
        comment_policy: str,
    ) -> bool:
        if comment_policy == "disabled":
            return False
        if author.user_id == viewer.user_id:
            return True
        if self._blocked(viewer=viewer, author=author):
            return False
        if comment_policy == "open":
            return True
        return comment_policy == "followers" and self.is_following(
            viewer=viewer, author=author
        )
