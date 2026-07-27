"""SQLAlchemy repository for Web-local canonical profiles and relationships.

The caller owns the transaction.  Mutation methods lock both user rows in UUID
order, claim an ``ActivityEvent`` idempotency key, and apply every related row
change before the caller can commit.  Banghua is never contacted here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Mapping, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bbw_prod.models import ActivityEvent, ExternalAccount, Relationship, User

from .contracts import (
    FRIEND_REQUEST_TRANSITIONS,
    LEGACY_ACCOUNT_PROVIDER,
    RELATION_BLOCK,
    RELATION_FOLLOW,
    RELATION_FRIEND,
    RELATION_FRIEND_REQUEST,
    REQUEST_ACCEPTED,
    REQUEST_BLOCKED,
    REQUEST_CANCELLED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    SOCIAL_NATIVE_PROVIDER,
    SOCIAL_NATIVE_SCHEMA,
    FriendRequestMutationResult,
    FriendRequestView,
    ProfileMutationResult,
    ProfilePatch,
    RelationshipFlags,
    SocialAccount,
    SocialMutationResult,
    SocialPrincipal,
)
from .errors import (
    AlreadyFriends,
    FriendRequestStateConflict,
    FriendRequestUnavailable,
    SocialBlocked,
    SocialIdempotencyConflict,
    SocialIdentityUnavailable,
    SocialTargetUnavailable,
)


BLOCK_CLEANUP_KINDS = (RELATION_FRIEND, RELATION_FOLLOW)
BLOCK_CLEANUP_DIRECTIONS = ("actor-target", "target-actor")
LEGACY_RELATION_FOLLOWER = "follower"
LEGACY_RELATION_BLOCKED_BY = "blacklisted_by"
SOCIAL_RELATIONSHIP_PROVIDERS = (
    SOCIAL_NATIVE_PROVIDER,
    LEGACY_ACCOUNT_PROVIDER,
)

# ``beibeiwu`` rows were written by two server-side ingestion paths before the
# Web-local authority existed.  Only rows carrying one of these authenticated
# response/action markers may be used as migration evidence.  Arbitrary legacy
# rows are deliberately ignored.
TRUSTED_LEGACY_SOURCE_PATHS: Mapping[str, frozenset[str]] = {
    RELATION_FOLLOW: frozenset({"/api/social/follows", "/api/social/follow-list"}),
    LEGACY_RELATION_FOLLOWER: frozenset({"/api/social/fans"}),
    RELATION_FRIEND: frozenset({"/api/social/friends"}),
    RELATION_FRIEND_REQUEST: frozenset(
        {"/api/social/friend-apply", "/api/social/friends"}
    ),
    RELATION_BLOCK: frozenset({"/api/social/blacklist"}),
    LEGACY_RELATION_BLOCKED_BY: frozenset({"/api/social/blacklist-me"}),
}
TRUSTED_LEGACY_ACTION_EVENTS: Mapping[str, frozenset[str]] = {
    RELATION_FOLLOW: frozenset(
        {
            "api.api.social.follow",
            "api.api.social.unfollow",
            "api.social.follow",
            "api.social.unfollow",
        }
    ),
    RELATION_FRIEND_REQUEST: frozenset(
        {
            "api.api.social.add-friend",
            "api.api.social.agree-friend",
            "api.social.add-friend",
            "api.social.agree-friend",
        }
    ),
    RELATION_FRIEND: frozenset(
        {
            "api.api.social.agree-friend",
            "api.api.social.delete-friend",
            "api.social.agree-friend",
            "api.social.delete-friend",
        }
    ),
    RELATION_BLOCK: frozenset(
        {
            "api.api.social.blacklist-add",
            "api.api.social.blacklist-del",
            "api.social.blacklist-add",
            "api.social.blacklist-del",
        }
    ),
}
REQUEST_STATES = frozenset(
    {
        REQUEST_PENDING,
        REQUEST_ACCEPTED,
        REQUEST_REJECTED,
        REQUEST_CANCELLED,
        REQUEST_BLOCKED,
    }
)


def is_trusted_legacy_relationship(row: Relationship) -> bool:
    """Return whether a legacy row is authenticated migration evidence."""

    if str(row.provider or "") != LEGACY_ACCOUNT_PROVIDER:
        return False
    kind = str(row.kind or "")
    metadata = dict(row.extra_data or {})
    if metadata.get("server_owned") is True:
        return kind in TRUSTED_LEGACY_SOURCE_PATHS
    source_path = str(metadata.get("source_path") or "").strip()
    if source_path in TRUSTED_LEGACY_SOURCE_PATHS.get(kind, frozenset()):
        if kind == RELATION_FRIEND_REQUEST and source_path == "/api/social/friends":
            return str(metadata.get("resolved_as") or "") == REQUEST_ACCEPTED
        return True
    action = str(metadata.get("last_event_type") or "").strip()
    return action in TRUSTED_LEGACY_ACTION_EVENTS.get(kind, frozenset())


def normalized_relationship_state(row: Relationship) -> str:
    """Map legacy active/inactive request states to the Web-local state machine."""

    status = str(row.status or "")
    if str(row.kind or "") != RELATION_FRIEND_REQUEST:
        return status
    if str(row.provider or "") == SOCIAL_NATIVE_PROVIDER:
        return status
    if status == "active" and row.ended_at is None:
        return REQUEST_PENDING
    resolved_as = str(dict(row.extra_data or {}).get("resolved_as") or "")
    return resolved_as if resolved_as in REQUEST_STATES else REQUEST_CANCELLED


def _row_is_active(row: Relationship | None) -> bool:
    return bool(
        row is not None
        and str(row.status or "") == "active"
        and row.ended_at is None
    )


def _row_timestamp(row: Relationship) -> datetime:
    for value in (
        getattr(row, "updated_at", None),
        row.ended_at,
        row.started_at,
        getattr(row, "created_at", None),
    ):
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


def _latest_relationship(rows: Sequence[Relationship]) -> Relationship | None:
    return max(rows, key=lambda row: (_row_timestamp(row), str(row.id))) if rows else None


def active_account_query(*, upstream_uid: str, provider: str):
    """可独立编译和审计的 active 账号查询。"""

    return (
        select(User, ExternalAccount)
        .join(ExternalAccount, ExternalAccount.user_id == User.id)
        .where(
            User.status == "active",
            User.disabled_at.is_(None),
            ExternalAccount.provider == provider,
            ExternalAccount.upstream_uid == upstream_uid,
        )
    )


def block_between_query(left: SocialAccount, right: SocialAccount):
    """单向 blacklist canonical 行同时支持 blocked/blocked_by 派生。"""

    return (
        select(Relationship.id)
        .where(
            Relationship.provider == SOCIAL_NATIVE_PROVIDER,
            Relationship.kind == RELATION_BLOCK,
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
            or_(
                and_(
                    Relationship.owner_user_id == left.user_id,
                    Relationship.subject_upstream_uid == right.upstream_uid,
                ),
                and_(
                    Relationship.owner_user_id == right.user_id,
                    Relationship.subject_upstream_uid == left.upstream_uid,
                ),
            ),
        )
        .limit(1)
    )


def _payload_digest(action: str, target_uid: str, payload: Mapping[str, object]) -> str:
    value = {
        "action": action,
        "payload": dict(payload),
        "schema": SOCIAL_NATIVE_SCHEMA,
        "target_uid": target_uid,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SqlAlchemyCanonicalSocialStore:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _account(
        user: User,
        account: ExternalAccount,
        *,
        include_private_display: bool = False,
    ) -> SocialAccount:
        account_data = dict(account.device_data or {})
        return SocialAccount(
            user_id=user.id,
            external_account_id=account.id,
            upstream_uid=str(account.upstream_uid or ""),
            display_name=str(user.display_name or ""),
            profile=dict(user.profile or {}),
            updated_at=user.updated_at,
            account_provider=account.provider,
            account_display_data=(
                {
                    key: account_data[key]
                    for key in (
                        "is_realname",
                        "money",
                        "portrait",
                        "rp_verify_time",
                        "svip",
                        "user_sign",
                        "user_role",
                        "vip",
                    )
                    if key in account_data
                }
                if include_private_display
                else {}
            ),
        )

    def resolve_principal(self, principal: SocialPrincipal) -> SocialAccount | None:
        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id == principal.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.id == principal.external_account_id,
                ExternalAccount.provider == principal.account_provider,
                ExternalAccount.upstream_uid == principal.upstream_uid,
            )
        ).one_or_none()
        return (
            self._account(row[0], row[1], include_private_display=True)
            if row is not None
            else None
        )

    def resolve_active_target(
        self, upstream_uid: str, *, provider: str
    ) -> SocialAccount | None:
        row = self.db.execute(
            active_account_query(upstream_uid=upstream_uid, provider=provider)
        ).one_or_none()
        return self._account(row[0], row[1]) if row is not None else None

    def resolve_active_targets(
        self, upstream_uids: Sequence[str], *, provider: str
    ) -> Mapping[str, SocialAccount]:
        uids = list(dict.fromkeys(upstream_uids))
        if not uids:
            return {}
        rows = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == provider,
                ExternalAccount.upstream_uid.in_(uids),
            )
        ).all()
        return {
            str(account.upstream_uid): self._account(user, account)
            for user, account in rows
            if account.upstream_uid
        }

    def _relationship_rows(
        self,
        viewer: SocialAccount,
        subject: SocialAccount,
        *,
        lock: bool = False,
    ) -> list[Relationship]:
        statement = select(Relationship).where(
            Relationship.provider.in_(SOCIAL_RELATIONSHIP_PROVIDERS),
            or_(
                and_(
                    Relationship.owner_user_id == viewer.user_id,
                    Relationship.subject_upstream_uid == subject.upstream_uid,
                ),
                and_(
                    Relationship.owner_user_id == subject.user_id,
                    Relationship.subject_upstream_uid == viewer.upstream_uid,
                ),
            ),
        )
        if lock:
            statement = statement.with_for_update()
        return list(self.db.scalars(statement))

    @staticmethod
    def _local_relation_from_rows(
        rows: Sequence[Relationship],
        *,
        owner_id: uuid.UUID,
        subject_uid: str,
        kind: str,
    ) -> Relationship | None:
        return _latest_relationship(
            [
                row
                for row in rows
                if row.owner_user_id == owner_id
                and str(row.provider or "") == SOCIAL_NATIVE_PROVIDER
                and str(row.subject_upstream_uid or "") == subject_uid
                and str(row.kind or "") == kind
            ]
        )

    @staticmethod
    def _trusted_legacy_rows(
        rows: Sequence[Relationship],
        *,
        owner_id: uuid.UUID,
        subject_uid: str,
        kind: str,
    ) -> list[Relationship]:
        return [
            row
            for row in rows
            if row.owner_user_id == owner_id
            and str(row.subject_upstream_uid or "") == subject_uid
            and str(row.kind or "") == kind
            and is_trusted_legacy_relationship(row)
        ]

    @classmethod
    def _effective_edge_row_from_rows(
        cls,
        rows: Sequence[Relationship],
        *,
        owner: SocialAccount,
        subject: SocialAccount,
        kind: str,
    ) -> Relationship | None:
        """Resolve one canonical directed edge with local rows taking precedence."""

        local = cls._local_relation_from_rows(
            rows,
            owner_id=owner.user_id,
            subject_uid=subject.upstream_uid,
            kind=kind,
        )
        if local is not None:
            return local

        direct = cls._trusted_legacy_rows(
            rows,
            owner_id=owner.user_id,
            subject_uid=subject.upstream_uid,
            kind=kind,
        )
        if direct:
            # The account owner's own snapshot/action is stronger than a peer's
            # reverse projection, including when it is an inactive legacy row.
            return _latest_relationship(direct)

        reverse_kind = {
            RELATION_FOLLOW: LEGACY_RELATION_FOLLOWER,
            RELATION_FRIEND: RELATION_FRIEND,
            RELATION_BLOCK: LEGACY_RELATION_BLOCKED_BY,
        }.get(kind)
        if reverse_kind is None:
            return None
        return _latest_relationship(
            cls._trusted_legacy_rows(
                rows,
                owner_id=subject.user_id,
                subject_uid=owner.upstream_uid,
                kind=reverse_kind,
            )
        )

    @staticmethod
    def _legacy_request_role(row: Relationship) -> str:
        metadata = dict(row.extra_data or {})
        action = str(metadata.get("last_event_type") or "").strip()
        if action in {
            "api.api.social.add-friend",
            "api.social.add-friend",
        }:
            return "direct"
        if action in {
            "api.api.social.agree-friend",
            "api.social.agree-friend",
        }:
            return "incoming-mirror"
        source_path = str(metadata.get("source_path") or "").strip()
        if source_path in {"/api/social/friend-apply", "/api/social/friends"}:
            return "incoming-mirror"
        return ""

    @classmethod
    def _effective_request_row_from_rows(
        cls,
        rows: Sequence[Relationship],
        *,
        requester: SocialAccount,
        target: SocialAccount,
    ) -> Relationship | None:
        local = cls._local_relation_from_rows(
            rows,
            owner_id=requester.user_id,
            subject_uid=target.upstream_uid,
            kind=RELATION_FRIEND_REQUEST,
        )
        if local is not None:
            return local

        direct = [
            row
            for row in cls._trusted_legacy_rows(
                rows,
                owner_id=requester.user_id,
                subject_uid=target.upstream_uid,
                kind=RELATION_FRIEND_REQUEST,
            )
            if cls._legacy_request_role(row) == "direct"
        ]
        if direct:
            return _latest_relationship(direct)
        mirrors = [
            row
            for row in cls._trusted_legacy_rows(
                rows,
                owner_id=target.user_id,
                subject_uid=requester.upstream_uid,
                kind=RELATION_FRIEND_REQUEST,
            )
            if cls._legacy_request_role(row) == "incoming-mirror"
        ]
        return _latest_relationship(mirrors)

    def relationship_flags(
        self, viewer: SocialAccount, subject: SocialAccount
    ) -> RelationshipFlags:
        rows = self._relationship_rows(viewer, subject)
        viewer_follow = self._effective_edge_row_from_rows(
            rows, owner=viewer, subject=subject, kind=RELATION_FOLLOW
        )
        subject_follow = self._effective_edge_row_from_rows(
            rows, owner=subject, subject=viewer, kind=RELATION_FOLLOW
        )
        viewer_friend = self._effective_edge_row_from_rows(
            rows, owner=viewer, subject=subject, kind=RELATION_FRIEND
        )
        subject_friend = self._effective_edge_row_from_rows(
            rows, owner=subject, subject=viewer, kind=RELATION_FRIEND
        )
        viewer_block = self._effective_edge_row_from_rows(
            rows, owner=viewer, subject=subject, kind=RELATION_BLOCK
        )
        subject_block = self._effective_edge_row_from_rows(
            rows, owner=subject, subject=viewer, kind=RELATION_BLOCK
        )
        outgoing_request = self._effective_request_row_from_rows(
            rows, requester=viewer, target=subject
        )
        incoming_request = self._effective_request_row_from_rows(
            rows, requester=subject, target=viewer
        )

        return RelationshipFlags(
            following=_row_is_active(viewer_follow),
            followed_by=_row_is_active(subject_follow),
            friend=_row_is_active(viewer_friend) and _row_is_active(subject_friend),
            blocked=_row_is_active(viewer_block),
            blocked_by=_row_is_active(subject_block),
            outgoing_friend_request=(
                normalized_relationship_state(outgoing_request)
                if outgoing_request is not None
                else None
            ),
            incoming_friend_request=(
                normalized_relationship_state(incoming_request)
                if incoming_request is not None
                else None
            ),
        )

    def _lock_active_actor(self, actor: SocialAccount) -> User:
        user = self.db.scalar(
            select(User)
            .where(
                User.id == actor.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
            .with_for_update()
        )
        if user is None:
            raise SocialIdentityUnavailable("当前账号已停用")
        return user

    def _lock_active_pair(
        self, actor: SocialAccount, target: SocialAccount
    ) -> dict[uuid.UUID, User]:
        ids = sorted((actor.user_id, target.user_id), key=str)
        users = list(
            self.db.scalars(
                select(User)
                .where(
                    User.id.in_(ids),
                    User.status == "active",
                    User.disabled_at.is_(None),
                )
                .order_by(User.id)
                .with_for_update()
            )
        )
        by_id = {user.id: user for user in users}
        if actor.user_id not in by_id:
            raise SocialIdentityUnavailable("当前账号已停用")
        if target.user_id not in by_id:
            raise SocialTargetUnavailable("目标账号已停用")
        return by_id

    def _claim_operation(
        self,
        *,
        actor: SocialAccount,
        target_uid: str,
        action: str,
        operation_id: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
    ) -> tuple[ActivityEvent, bool]:
        digest = _payload_digest(action, target_uid, payload)
        event_key = f"social:{operation_id}"
        details = {
            "action": action,
            "authority": SOCIAL_NATIVE_PROVIDER,
            "idempotency_digest": digest,
            "payload": dict(payload),
            "schema": SOCIAL_NATIVE_SCHEMA,
        }
        statement = (
            insert(ActivityEvent)
            .values(
                owner_user_id=actor.user_id,
                provider=SOCIAL_NATIVE_PROVIDER,
                upstream_event_id=event_key,
                event_type=f"social.{action}",
                actor_upstream_uid=actor.upstream_uid,
                subject_upstream_uid=target_uid or actor.upstream_uid,
                occurred_at=occurred_at,
                details=details,
            )
            .on_conflict_do_nothing(constraint="uq_activity_events_owner_source")
            .returning(ActivityEvent)
        )
        created = self.db.scalars(statement).first()
        if created is not None:
            return created, True
        existing = self.db.scalar(
            select(ActivityEvent).where(
                ActivityEvent.owner_user_id == actor.user_id,
                ActivityEvent.provider == SOCIAL_NATIVE_PROVIDER,
                ActivityEvent.upstream_event_id == event_key,
            )
        )
        existing_details = dict(existing.details or {}) if existing is not None else {}
        if (
            existing is None
            or existing.event_type != f"social.{action}"
            or existing_details.get("idempotency_digest") != digest
        ):
            raise SocialIdempotencyConflict("operation_id 已被其他写操作或参数占用")
        return existing, False

    @staticmethod
    def _finish_event(event: ActivityEvent, result: Mapping[str, object]) -> None:
        details = dict(event.details or {})
        details["result"] = dict(result)
        event.details = details

    @staticmethod
    def _replay_values(event: ActivityEvent) -> dict[str, object]:
        return dict(dict(event.details or {}).get("result") or {})

    @staticmethod
    def _mark_web_local_profile_fields(
        profile: Mapping[str, object],
        fields: Sequence[str],
        occurred_at: datetime,
    ) -> tuple[dict[str, object], bool]:
        marked = dict(profile or {})
        existing = marked.get("_web_local_fields")
        current_fields = {
            str(value)
            for value in (existing if isinstance(existing, (list, tuple, set)) else ())
            if str(value)
        }
        current_fields.update(str(field) for field in fields if str(field))
        normalized_fields = sorted(current_fields)
        timestamp = occurred_at.isoformat()
        existing_timestamp = str(marked.get("_web_local_updated_at") or "").strip()
        if existing_timestamp:
            try:
                parsed_existing = datetime.fromisoformat(
                    existing_timestamp.replace("Z", "+00:00")
                )
                if parsed_existing.tzinfo is None:
                    parsed_existing = parsed_existing.replace(tzinfo=UTC)
                parsed_occurred = (
                    occurred_at
                    if occurred_at.tzinfo is not None
                    else occurred_at.replace(tzinfo=UTC)
                )
                if parsed_existing > parsed_occurred:
                    timestamp = existing_timestamp
            except ValueError:
                pass
        changed = (
            marked.get("_web_local_fields") != normalized_fields
            or marked.get("_web_local_updated_at") != timestamp
        )
        marked["_web_local_fields"] = normalized_fields
        marked["_web_local_updated_at"] = timestamp
        return marked, changed

    def _relation(
        self,
        *,
        owner_id: uuid.UUID,
        subject_uid: str,
        kind: str,
        lock: bool = True,
    ) -> Relationship | None:
        statement = select(Relationship).where(
            Relationship.owner_user_id == owner_id,
            Relationship.provider == SOCIAL_NATIVE_PROVIDER,
            Relationship.subject_upstream_uid == subject_uid,
            Relationship.kind == kind,
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def _effective_edge_row(
        self,
        *,
        owner: SocialAccount,
        subject: SocialAccount,
        kind: str,
        lock: bool = True,
    ) -> Relationship | None:
        rows = self._relationship_rows(owner, subject, lock=lock)
        return self._effective_edge_row_from_rows(
            rows, owner=owner, subject=subject, kind=kind
        )

    @staticmethod
    def _localized_metadata(
        source: Relationship | None,
        *,
        subject: SocialAccount,
        reason: str,
    ) -> dict[str, object]:
        metadata = dict(source.extra_data or {}) if source is not None else {}
        if source is not None and str(source.provider or "") != SOCIAL_NATIVE_PROVIDER:
            metadata.update(
                {
                    "localized_from_id": str(source.id),
                    "localized_from_provider": str(source.provider or ""),
                }
            )
        metadata.update(
            {
                "authority": SOCIAL_NATIVE_PROVIDER,
                "schema": SOCIAL_NATIVE_SCHEMA,
                "subject_account_provider": subject.account_provider,
                "last_reason": reason,
            }
        )
        return metadata

    def _set_active_relation(
        self,
        *,
        owner: SocialAccount,
        subject: SocialAccount,
        kind: str,
        active: bool,
        occurred_at: datetime,
        reason: str,
    ) -> bool:
        row = self._relation(
            owner_id=owner.user_id,
            subject_uid=subject.upstream_uid,
            kind=kind,
        )
        effective = row
        if effective is None:
            effective = self._effective_edge_row(
                owner=owner,
                subject=subject,
                kind=kind,
            )
        currently_active = _row_is_active(effective)
        changed = currently_active != active
        if row is not None and not changed:
            return False
        metadata = self._localized_metadata(
            effective, subject=subject, reason=reason
        )
        if row is None:
            row = Relationship(
                id=uuid.uuid4(),
                owner_user_id=owner.user_id,
                provider=SOCIAL_NATIVE_PROVIDER,
                subject_upstream_uid=subject.upstream_uid,
                kind=kind,
                status="active" if active else "inactive",
                started_at=(
                    effective.started_at
                    if effective is not None and (not active or currently_active)
                    else occurred_at
                ),
                ended_at=None if active else occurred_at,
                extra_data=metadata,
            )
            self.db.add(row)
        else:
            row.status = "active" if active else "inactive"
            if active:
                row.started_at = occurred_at
                row.ended_at = None
            else:
                row.ended_at = occurred_at
            row.extra_data = metadata
        return changed

    def _ensure_not_blocked(self, actor: SocialAccount, target: SocialAccount) -> None:
        rows = self._relationship_rows(actor, target, lock=True)
        actor_block = self._effective_edge_row_from_rows(
            rows, owner=actor, subject=target, kind=RELATION_BLOCK
        )
        target_block = self._effective_edge_row_from_rows(
            rows, owner=target, subject=actor, kind=RELATION_BLOCK
        )
        if _row_is_active(actor_block) or _row_is_active(target_block):
            raise SocialBlocked("任一方黑名单关系禁止该操作")

    def _friendship_active(self, left: SocialAccount, right: SocialAccount) -> bool:
        rows = self._relationship_rows(left, right, lock=True)
        left_row = self._effective_edge_row_from_rows(
            rows, owner=left, subject=right, kind=RELATION_FRIEND
        )
        right_row = self._effective_edge_row_from_rows(
            rows, owner=right, subject=left, kind=RELATION_FRIEND
        )
        return _row_is_active(left_row) and _row_is_active(right_row)

    def _request_relation(
        self,
        *,
        requester: SocialAccount,
        target: SocialAccount,
        localize: bool,
        occurred_at: datetime,
        reason: str,
    ) -> Relationship | None:
        rows = self._relationship_rows(requester, target, lock=True)
        row = self._effective_request_row_from_rows(
            rows, requester=requester, target=target
        )
        if (
            row is None
            or str(row.provider or "") == SOCIAL_NATIVE_PROVIDER
            or not localize
        ):
            return row
        state = normalized_relationship_state(row)
        localized = Relationship(
            id=uuid.uuid4(),
            owner_user_id=requester.user_id,
            provider=SOCIAL_NATIVE_PROVIDER,
            subject_upstream_uid=target.upstream_uid,
            kind=RELATION_FRIEND_REQUEST,
            status=state,
            started_at=row.started_at,
            ended_at=(
                None
                if state == REQUEST_PENDING
                else row.ended_at or occurred_at
            ),
            extra_data=self._localized_metadata(
                row, subject=target, reason=reason
            ),
        )
        self.db.add(localized)
        return localized

    @staticmethod
    def _request_view(
        row: Relationship, requester: SocialAccount, target: SocialAccount
    ) -> FriendRequestView:
        metadata = dict(row.extra_data or {})
        return FriendRequestView(
            id=row.id,
            requester_upstream_uid=requester.upstream_uid,
            target_upstream_uid=target.upstream_uid,
            message=str(metadata.get("message") or ""),
            state=normalized_relationship_state(row),
            requested_at=row.started_at,
            resolved_at=row.ended_at,
        )

    def update_profile(
        self,
        *,
        actor: SocialAccount,
        patch: ProfilePatch,
        operation_id: str,
        occurred_at: datetime,
    ) -> ProfileMutationResult:
        user = self._lock_active_actor(actor)
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=actor.upstream_uid,
            action="profile.update",
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload=dict(patch.values),
        )
        if not claimed:
            marked_profile, marker_changed = self._mark_web_local_profile_fields(
                dict(user.profile or {}), patch.values.keys(), event.occurred_at
            )
            if marker_changed:
                user.profile = marked_profile
            current = replace(
                actor,
                display_name=str(user.display_name or ""),
                profile=marked_profile,
                updated_at=user.updated_at,
            )
            return ProfileMutationResult(
                profile=current,
                changed=bool(self._replay_values(event).get("changed")),
                idempotent_replay=True,
            )

        profile = dict(user.profile or {})
        changed = False
        for key, value in patch.values.items():
            if key == "nickname":
                if str(user.display_name or "") != value:
                    user.display_name = value
                    changed = True
                if profile.get("nickname") != value:
                    profile["nickname"] = value
                    changed = True
                continue
            if profile.get(key) != value:
                profile[key] = value
                changed = True
        profile, marker_changed = self._mark_web_local_profile_fields(
            profile, patch.values.keys(), occurred_at
        )
        if changed or marker_changed:
            user.profile = profile
            user.updated_at = occurred_at
        result_account = replace(
            actor,
            display_name=str(user.display_name or ""),
            profile=dict(user.profile or profile),
            updated_at=user.updated_at,
        )
        self._finish_event(event, {"changed": changed})
        return ProfileMutationResult(profile=result_account, changed=changed)

    def set_following(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        active: bool,
        operation_id: str,
        occurred_at: datetime,
    ) -> SocialMutationResult:
        self._lock_active_pair(actor, target)
        action = "follow" if active else "unfollow"
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=target.upstream_uid,
            action=action,
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload={"active": active},
        )
        if not claimed:
            replay = self._replay_values(event)
            return SocialMutationResult(
                action=action,
                actor_upstream_uid=actor.upstream_uid,
                target_upstream_uid=target.upstream_uid,
                state="active" if active else "inactive",
                changed=bool(replay.get("changed")),
                occurred_at=event.occurred_at,
                idempotent_replay=True,
            )
        if active:
            self._ensure_not_blocked(actor, target)
        changed = self._set_active_relation(
            owner=actor,
            subject=target,
            kind=RELATION_FOLLOW,
            active=active,
            occurred_at=occurred_at,
            reason=action,
        )
        self._finish_event(event, {"changed": changed})
        return SocialMutationResult(
            action=action,
            actor_upstream_uid=actor.upstream_uid,
            target_upstream_uid=target.upstream_uid,
            state="active" if active else "inactive",
            changed=changed,
            occurred_at=occurred_at,
        )

    def create_friend_request(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        message: str,
        operation_id: str,
        occurred_at: datetime,
    ) -> FriendRequestMutationResult:
        self._lock_active_pair(actor, target)
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=target.upstream_uid,
            action="friend.request",
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload={"message": message},
        )
        row = self._request_relation(
            requester=actor,
            target=target,
            localize=claimed,
            occurred_at=occurred_at,
            reason="friend-request-created",
        )
        if not claimed:
            if row is None:
                raise FriendRequestUnavailable("幂等好友申请不存在")
            return FriendRequestMutationResult(
                request=self._request_view(row, actor, target),
                changed=bool(self._replay_values(event).get("changed")),
                idempotent_replay=True,
            )
        self._ensure_not_blocked(actor, target)
        if self._friendship_active(actor, target):
            raise AlreadyFriends("双方已经是好友")

        previous = normalized_relationship_state(row) if row is not None else None
        if previous == REQUEST_PENDING:
            changed = False
        else:
            if REQUEST_PENDING not in FRIEND_REQUEST_TRANSITIONS.get(previous, frozenset()):
                raise FriendRequestStateConflict("好友申请不能回到待处理状态")
            metadata = dict(row.extra_data or {}) if row is not None else {}
            metadata.update(
                {
                    "authority": SOCIAL_NATIVE_PROVIDER,
                    "message": message,
                    "schema": SOCIAL_NATIVE_SCHEMA,
                    "subject_account_provider": target.account_provider,
                }
            )
            if row is None:
                row = Relationship(
                    id=uuid.uuid4(),
                    owner_user_id=actor.user_id,
                    provider=SOCIAL_NATIVE_PROVIDER,
                    subject_upstream_uid=target.upstream_uid,
                    kind=RELATION_FRIEND_REQUEST,
                    status=REQUEST_PENDING,
                    started_at=occurred_at,
                    ended_at=None,
                    extra_data=metadata,
                )
                self.db.add(row)
            else:
                row.status = REQUEST_PENDING
                row.started_at = occurred_at
                row.ended_at = None
                row.extra_data = metadata
            changed = True
        assert row is not None
        self._finish_event(event, {"changed": changed, "request_id": str(row.id)})
        return FriendRequestMutationResult(
            request=self._request_view(row, actor, target), changed=changed
        )

    def resolve_friend_request(
        self,
        *,
        actor: SocialAccount,
        requester: SocialAccount,
        resolution: str,
        operation_id: str,
        occurred_at: datetime,
    ) -> FriendRequestMutationResult:
        self._lock_active_pair(actor, requester)
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=requester.upstream_uid,
            action=f"friend.{resolution}",
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload={"resolution": resolution},
        )
        row = self._request_relation(
            requester=requester,
            target=actor,
            localize=claimed,
            occurred_at=occurred_at,
            reason=f"friend-request-{resolution}",
        )
        if row is None:
            raise FriendRequestUnavailable("待处理好友申请不存在")
        if not claimed:
            return FriendRequestMutationResult(
                request=self._request_view(row, requester, actor),
                changed=bool(self._replay_values(event).get("changed")),
                idempotent_replay=True,
            )
        if resolution == REQUEST_ACCEPTED:
            self._ensure_not_blocked(actor, requester)

        previous = normalized_relationship_state(row)
        if previous == resolution:
            changed = False
            if resolution == REQUEST_ACCEPTED and not self._friendship_active(actor, requester):
                raise FriendRequestStateConflict("旧申请已接受但好友关系后来已删除")
        elif previous != REQUEST_PENDING or resolution not in FRIEND_REQUEST_TRANSITIONS[REQUEST_PENDING]:
            raise FriendRequestStateConflict(
                f"好友申请不能从 {previous} 变更为 {resolution}"
            )
        else:
            row.status = resolution
            row.ended_at = occurred_at
            metadata = dict(row.extra_data or {})
            metadata.update({"resolved_as": resolution, "resolved_by": actor.upstream_uid})
            row.extra_data = metadata
            changed = True

        related_changes: list[str] = []
        if resolution == REQUEST_ACCEPTED and changed:
            if self._set_active_relation(
                owner=actor,
                subject=requester,
                kind=RELATION_FRIEND,
                active=True,
                occurred_at=occurred_at,
                reason="friend-request-accepted",
            ):
                related_changes.append("friend:actor-requester")
            if self._set_active_relation(
                owner=requester,
                subject=actor,
                kind=RELATION_FRIEND,
                active=True,
                occurred_at=occurred_at,
                reason="friend-request-accepted",
            ):
                related_changes.append("friend:requester-actor")
            reverse = self._request_relation(
                requester=actor,
                target=requester,
                localize=True,
                occurred_at=occurred_at,
                reason="friend-request-reverse-accepted",
            )
            if (
                reverse is not None
                and normalized_relationship_state(reverse) == REQUEST_PENDING
            ):
                reverse.status = REQUEST_ACCEPTED
                reverse.ended_at = occurred_at
                reverse_metadata = dict(reverse.extra_data or {})
                reverse_metadata.update(
                    {"resolved_as": REQUEST_ACCEPTED, "resolved_by": actor.upstream_uid}
                )
                reverse.extra_data = reverse_metadata
                related_changes.append("friend-request:reverse-accepted")

        self._finish_event(
            event,
            {
                "changed": changed,
                "related_changes": related_changes,
                "request_id": str(row.id),
            },
        )
        return FriendRequestMutationResult(
            request=self._request_view(row, requester, actor), changed=changed
        )

    def cancel_friend_request(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        operation_id: str,
        occurred_at: datetime,
    ) -> FriendRequestMutationResult:
        self._lock_active_pair(actor, target)
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=target.upstream_uid,
            action="friend.cancel",
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload={},
        )
        row = self._request_relation(
            requester=actor,
            target=target,
            localize=claimed,
            occurred_at=occurred_at,
            reason="friend-request-cancelled",
        )
        if row is None:
            raise FriendRequestUnavailable("好友申请不存在")
        if not claimed:
            return FriendRequestMutationResult(
                request=self._request_view(row, actor, target),
                changed=bool(self._replay_values(event).get("changed")),
                idempotent_replay=True,
            )
        current_state = normalized_relationship_state(row)
        if current_state == REQUEST_CANCELLED:
            changed = False
        elif current_state != REQUEST_PENDING:
            raise FriendRequestStateConflict("只有待处理申请可以撤销")
        else:
            row.status = REQUEST_CANCELLED
            row.ended_at = occurred_at
            metadata = dict(row.extra_data or {})
            metadata["resolved_as"] = REQUEST_CANCELLED
            row.extra_data = metadata
            changed = True
        self._finish_event(event, {"changed": changed, "request_id": str(row.id)})
        return FriendRequestMutationResult(
            request=self._request_view(row, actor, target), changed=changed
        )

    def delete_friend(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        operation_id: str,
        occurred_at: datetime,
    ) -> SocialMutationResult:
        self._lock_active_pair(actor, target)
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=target.upstream_uid,
            action="friend.delete",
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload={},
        )
        if not claimed:
            replay = self._replay_values(event)
            return SocialMutationResult(
                action="friend.delete",
                actor_upstream_uid=actor.upstream_uid,
                target_upstream_uid=target.upstream_uid,
                state="inactive",
                changed=bool(replay.get("changed")),
                occurred_at=event.occurred_at,
                idempotent_replay=True,
                related_changes=tuple(replay.get("related_changes") or ()),
            )
        related: list[str] = []
        for owner, subject, label in (
            (actor, target, "friend:actor-target"),
            (target, actor, "friend:target-actor"),
        ):
            if self._set_active_relation(
                owner=owner,
                subject=subject,
                kind=RELATION_FRIEND,
                active=False,
                occurred_at=occurred_at,
                reason="friend-deleted",
            ):
                related.append(label)
        changed = bool(related)
        self._finish_event(event, {"changed": changed, "related_changes": related})
        return SocialMutationResult(
            action="friend.delete",
            actor_upstream_uid=actor.upstream_uid,
            target_upstream_uid=target.upstream_uid,
            state="inactive",
            changed=changed,
            occurred_at=occurred_at,
            related_changes=tuple(related),
        )

    def _block_pending_request(
        self,
        *,
        requester: SocialAccount,
        target: SocialAccount,
        occurred_at: datetime,
    ) -> bool:
        row = self._request_relation(
            requester=requester,
            target=target,
            localize=True,
            occurred_at=occurred_at,
            reason="friend-request-blocked",
        )
        if row is None or normalized_relationship_state(row) != REQUEST_PENDING:
            return False
        row.status = REQUEST_BLOCKED
        row.ended_at = occurred_at
        metadata = dict(row.extra_data or {})
        metadata["resolved_as"] = REQUEST_BLOCKED
        row.extra_data = metadata
        return True

    def set_blocked(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        active: bool,
        operation_id: str,
        occurred_at: datetime,
    ) -> SocialMutationResult:
        """拉黑原子清理双向好友、关注和 pending 申请；解除不恢复。"""

        self._lock_active_pair(actor, target)
        action = "block" if active else "unblock"
        event, claimed = self._claim_operation(
            actor=actor,
            target_uid=target.upstream_uid,
            action=action,
            operation_id=operation_id,
            occurred_at=occurred_at,
            payload={"active": active},
        )
        if not claimed:
            replay = self._replay_values(event)
            return SocialMutationResult(
                action=action,
                actor_upstream_uid=actor.upstream_uid,
                target_upstream_uid=target.upstream_uid,
                state="active" if active else "inactive",
                changed=bool(replay.get("changed")),
                occurred_at=event.occurred_at,
                idempotent_replay=True,
                related_changes=tuple(replay.get("related_changes") or ()),
            )

        related: list[str] = []
        block_changed = self._set_active_relation(
            owner=actor,
            subject=target,
            kind=RELATION_BLOCK,
            active=active,
            occurred_at=occurred_at,
            reason=action,
        )
        if active:
            for kind in BLOCK_CLEANUP_KINDS:
                for owner, subject, direction in (
                    (actor, target, BLOCK_CLEANUP_DIRECTIONS[0]),
                    (target, actor, BLOCK_CLEANUP_DIRECTIONS[1]),
                ):
                    if self._set_active_relation(
                        owner=owner,
                        subject=subject,
                        kind=kind,
                        active=False,
                        occurred_at=occurred_at,
                        reason="blocked",
                    ):
                        related.append(f"{kind}:{direction}")
            for requester, request_target, direction in (
                (actor, target, BLOCK_CLEANUP_DIRECTIONS[0]),
                (target, actor, BLOCK_CLEANUP_DIRECTIONS[1]),
            ):
                if self._block_pending_request(
                    requester=requester,
                    target=request_target,
                    occurred_at=occurred_at,
                ):
                    related.append(f"friend_request:{direction}")

        changed = block_changed or bool(related)
        self._finish_event(event, {"changed": changed, "related_changes": related})
        return SocialMutationResult(
            action=action,
            actor_upstream_uid=actor.upstream_uid,
            target_upstream_uid=target.upstream_uid,
            state="active" if active else "inactive",
            changed=changed,
            occurred_at=occurred_at,
            related_changes=tuple(related),
        )

    def _owner_related_rows(
        self, owner: SocialAccount, *, kinds: Sequence[str]
    ) -> list[Relationship]:
        return list(
            self.db.scalars(
                select(Relationship).where(
                    Relationship.provider.in_(SOCIAL_RELATIONSHIP_PROVIDERS),
                    Relationship.kind.in_(tuple(kinds)),
                    or_(
                        Relationship.owner_user_id == owner.user_id,
                        Relationship.subject_upstream_uid == owner.upstream_uid,
                    ),
                )
            )
        )

    def _load_candidate_accounts(
        self,
        owner: SocialAccount,
        *,
        upstream_uids: set[str],
        user_ids: set[uuid.UUID],
    ) -> list[SocialAccount]:
        upstream_uids.discard(owner.upstream_uid)
        user_ids.discard(owner.user_id)
        conditions = []
        if upstream_uids:
            conditions.append(ExternalAccount.upstream_uid.in_(sorted(upstream_uids)))
        if user_ids:
            conditions.append(User.id.in_(sorted(user_ids, key=str)))
        if not conditions:
            return []
        rows = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == owner.account_provider,
                or_(*conditions),
            )
        ).all()
        by_user_id: dict[uuid.UUID, SocialAccount] = {}
        for user, account in rows:
            by_user_id[user.id] = self._account(user, account)
        return list(by_user_id.values())

    @staticmethod
    def _usable_candidate_row(row: Relationship) -> bool:
        return (
            str(row.provider or "") == SOCIAL_NATIVE_PROVIDER
            or is_trusted_legacy_relationship(row)
        )

    def list_relation_accounts(
        self,
        *,
        owner: SocialAccount,
        relation: str,
        limit: int,
        offset: int,
    ) -> list[SocialAccount]:
        if relation not in {"following", "followers", "friends"}:
            raise ValueError("unsupported relation")
        kinds = (
            (RELATION_FRIEND,)
            if relation == "friends"
            else (RELATION_FOLLOW, LEGACY_RELATION_FOLLOWER)
        )
        source_rows = [
            row
            for row in self._owner_related_rows(owner, kinds=kinds)
            if self._usable_candidate_row(row)
        ]
        candidate_uids: set[str] = set()
        candidate_user_ids: set[uuid.UUID] = set()
        candidate_times_by_uid: dict[str, datetime] = {}
        candidate_times_by_user_id: dict[uuid.UUID, datetime] = {}

        def remember_uid(row: Relationship) -> None:
            uid = str(row.subject_upstream_uid or "")
            if not uid:
                return
            candidate_uids.add(uid)
            candidate_times_by_uid[uid] = max(
                candidate_times_by_uid.get(uid, datetime.min.replace(tzinfo=UTC)),
                _row_timestamp(row),
            )

        def remember_owner(row: Relationship) -> None:
            candidate_user_ids.add(row.owner_user_id)
            candidate_times_by_user_id[row.owner_user_id] = max(
                candidate_times_by_user_id.get(
                    row.owner_user_id, datetime.min.replace(tzinfo=UTC)
                ),
                _row_timestamp(row),
            )

        for row in source_rows:
            kind = str(row.kind or "")
            provider = str(row.provider or "")
            if relation == "following":
                if row.owner_user_id == owner.user_id and kind == RELATION_FOLLOW:
                    remember_uid(row)
                elif (
                    provider == LEGACY_ACCOUNT_PROVIDER
                    and row.subject_upstream_uid == owner.upstream_uid
                    and kind == LEGACY_RELATION_FOLLOWER
                ):
                    remember_owner(row)
            elif relation == "followers":
                if row.subject_upstream_uid == owner.upstream_uid and kind == RELATION_FOLLOW:
                    remember_owner(row)
                elif (
                    provider == LEGACY_ACCOUNT_PROVIDER
                    and row.owner_user_id == owner.user_id
                    and kind == LEGACY_RELATION_FOLLOWER
                ):
                    remember_uid(row)
            elif kind == RELATION_FRIEND:
                if row.owner_user_id == owner.user_id:
                    remember_uid(row)
                elif row.subject_upstream_uid == owner.upstream_uid:
                    remember_owner(row)

        candidates = self._load_candidate_accounts(
            owner,
            upstream_uids=candidate_uids,
            user_ids=candidate_user_ids,
        )
        filtered: list[SocialAccount] = []
        candidate_times: dict[uuid.UUID, datetime] = {}
        for account in candidates:
            flags = self.relationship_flags(owner, account)
            active = {
                "following": flags.following,
                "followers": flags.followed_by,
                "friends": flags.friend,
            }[relation]
            if not active:
                continue
            filtered.append(account)
            candidate_times[account.user_id] = max(
                candidate_times_by_uid.get(
                    account.upstream_uid, datetime.min.replace(tzinfo=UTC)
                ),
                candidate_times_by_user_id.get(
                    account.user_id, datetime.min.replace(tzinfo=UTC)
                ),
            )
        filtered.sort(key=lambda account: str(account.user_id))
        filtered.sort(
            key=lambda account: candidate_times[account.user_id], reverse=True
        )
        return filtered[offset : offset + limit]

    def list_friend_requests(
        self,
        *,
        owner: SocialAccount,
        direction: str,
        state: str,
        limit: int,
        offset: int,
    ) -> list[FriendRequestView]:
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("unsupported friend request direction")
        source_rows = [
            row
            for row in self._owner_related_rows(
                owner, kinds=(RELATION_FRIEND_REQUEST,)
            )
            if self._usable_candidate_row(row)
        ]
        candidate_uids = {
            str(row.subject_upstream_uid or "")
            for row in source_rows
            if row.owner_user_id == owner.user_id
        }
        candidate_user_ids = {
            row.owner_user_id
            for row in source_rows
            if row.subject_upstream_uid == owner.upstream_uid
        }
        candidates = self._load_candidate_accounts(
            owner,
            upstream_uids=candidate_uids,
            user_ids=candidate_user_ids,
        )
        result: list[FriendRequestView] = []
        for peer in candidates:
            requester, target = (
                (owner, peer) if direction == "outgoing" else (peer, owner)
            )
            rows = self._relationship_rows(owner, peer)
            relationship = self._effective_request_row_from_rows(
                rows, requester=requester, target=target
            )
            if (
                relationship is None
                or normalized_relationship_state(relationship) != state
            ):
                continue
            result.append(self._request_view(relationship, requester, target))
        result.sort(key=lambda item: (item.requested_at, str(item.id)), reverse=True)
        return result[offset : offset + limit]

    def list_blocks(
        self,
        *,
        owner: SocialAccount,
        direction: str,
        limit: int,
        offset: int,
    ) -> list[tuple[SocialAccount, datetime]]:
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("unsupported block direction")
        source_rows = [
            row
            for row in self._owner_related_rows(
                owner, kinds=(RELATION_BLOCK, LEGACY_RELATION_BLOCKED_BY)
            )
            if self._usable_candidate_row(row)
        ]
        candidate_uids: set[str] = set()
        candidate_user_ids: set[uuid.UUID] = set()
        for row in source_rows:
            kind = str(row.kind or "")
            provider = str(row.provider or "")
            if direction == "outgoing":
                if row.owner_user_id == owner.user_id and kind == RELATION_BLOCK:
                    candidate_uids.add(str(row.subject_upstream_uid or ""))
                elif (
                    provider == LEGACY_ACCOUNT_PROVIDER
                    and row.subject_upstream_uid == owner.upstream_uid
                    and kind == LEGACY_RELATION_BLOCKED_BY
                ):
                    candidate_user_ids.add(row.owner_user_id)
            else:
                if row.subject_upstream_uid == owner.upstream_uid and kind == RELATION_BLOCK:
                    candidate_user_ids.add(row.owner_user_id)
                elif (
                    provider == LEGACY_ACCOUNT_PROVIDER
                    and row.owner_user_id == owner.user_id
                    and kind == LEGACY_RELATION_BLOCKED_BY
                ):
                    candidate_uids.add(str(row.subject_upstream_uid or ""))
        candidates = self._load_candidate_accounts(
            owner,
            upstream_uids=candidate_uids,
            user_ids=candidate_user_ids,
        )
        result: list[tuple[SocialAccount, datetime]] = []
        for peer in candidates:
            blocker, blocked = (
                (owner, peer) if direction == "outgoing" else (peer, owner)
            )
            rows = self._relationship_rows(owner, peer)
            relationship = self._effective_edge_row_from_rows(
                rows, owner=blocker, subject=blocked, kind=RELATION_BLOCK
            )
            if not _row_is_active(relationship):
                continue
            assert relationship is not None
            result.append((peer, relationship.started_at))
        result.sort(key=lambda item: str(item[0].user_id))
        result.sort(key=lambda item: item[1], reverse=True)
        return result[offset : offset + limit]
