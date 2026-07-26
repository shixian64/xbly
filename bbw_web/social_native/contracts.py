"""与 Banghua 无关的 Web 本地资料和关系契约。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence, runtime_checkable


SOCIAL_NATIVE_PROVIDER = "web-local"
LEGACY_ACCOUNT_PROVIDER = "beibeiwu"
SOCIAL_NATIVE_SCHEMA = 1

RELATION_FOLLOW = "follow"
RELATION_FRIEND = "friend"
RELATION_FRIEND_REQUEST = "friend_request"
RELATION_BLOCK = "blacklist"

REQUEST_PENDING = "pending"
REQUEST_ACCEPTED = "accepted"
REQUEST_REJECTED = "rejected"
REQUEST_CANCELLED = "cancelled"
REQUEST_BLOCKED = "blocked"

FRIEND_REQUEST_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({REQUEST_PENDING}),
    REQUEST_PENDING: frozenset(
        {
            REQUEST_ACCEPTED,
            REQUEST_REJECTED,
            REQUEST_CANCELLED,
            REQUEST_BLOCKED,
        }
    ),
    REQUEST_ACCEPTED: frozenset({REQUEST_PENDING}),
    REQUEST_REJECTED: frozenset({REQUEST_PENDING}),
    REQUEST_CANCELLED: frozenset({REQUEST_PENDING}),
    REQUEST_BLOCKED: frozenset({REQUEST_PENDING}),
}


@dataclass(frozen=True, slots=True)
class SocialPrincipal:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class SocialAccount:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    display_name: str
    profile: Mapping[str, object] = field(default_factory=dict)
    updated_at: datetime | None = None
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class RelationshipFlags:
    following: bool = False
    followed_by: bool = False
    friend: bool = False
    blocked: bool = False
    blocked_by: bool = False
    outgoing_friend_request: str | None = None
    incoming_friend_request: str | None = None


@dataclass(frozen=True, slots=True)
class SocialProfileView:
    user_id: uuid.UUID
    upstream_uid: str
    nickname: str
    avatar: str
    signature: str
    city: str
    gender: str
    relationship: RelationshipFlags
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProfilePatch:
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProfileMutationResult:
    profile: SocialAccount
    changed: bool
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class SocialMutationResult:
    action: str
    actor_upstream_uid: str
    target_upstream_uid: str
    state: str
    changed: bool
    occurred_at: datetime
    idempotent_replay: bool = False
    related_changes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FriendRequestView:
    id: uuid.UUID
    requester_upstream_uid: str
    target_upstream_uid: str
    message: str
    state: str
    requested_at: datetime
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FriendRequestMutationResult:
    request: FriendRequestView
    changed: bool
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class BlockView:
    profile: SocialProfileView
    direction: str
    started_at: datetime


@runtime_checkable
class CanonicalSocialStore(Protocol):
    """事务由调用方持有；每个 mutation 方法必须在当前事务内原子完成。"""

    def resolve_principal(self, principal: SocialPrincipal) -> SocialAccount | None: ...

    def resolve_active_target(
        self, upstream_uid: str, *, provider: str
    ) -> SocialAccount | None: ...

    def resolve_active_targets(
        self, upstream_uids: Sequence[str], *, provider: str
    ) -> Mapping[str, SocialAccount]: ...

    def relationship_flags(
        self, viewer: SocialAccount, subject: SocialAccount
    ) -> RelationshipFlags: ...

    def update_profile(
        self,
        *,
        actor: SocialAccount,
        patch: ProfilePatch,
        operation_id: str,
        occurred_at: datetime,
    ) -> ProfileMutationResult: ...

    def set_following(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        active: bool,
        operation_id: str,
        occurred_at: datetime,
    ) -> SocialMutationResult: ...

    def create_friend_request(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        message: str,
        operation_id: str,
        occurred_at: datetime,
    ) -> FriendRequestMutationResult: ...

    def resolve_friend_request(
        self,
        *,
        actor: SocialAccount,
        requester: SocialAccount,
        resolution: str,
        operation_id: str,
        occurred_at: datetime,
    ) -> FriendRequestMutationResult: ...

    def cancel_friend_request(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        operation_id: str,
        occurred_at: datetime,
    ) -> FriendRequestMutationResult: ...

    def delete_friend(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        operation_id: str,
        occurred_at: datetime,
    ) -> SocialMutationResult: ...

    def set_blocked(
        self,
        *,
        actor: SocialAccount,
        target: SocialAccount,
        active: bool,
        operation_id: str,
        occurred_at: datetime,
    ) -> SocialMutationResult: ...

    def list_relation_accounts(
        self,
        *,
        owner: SocialAccount,
        relation: str,
        limit: int,
        offset: int,
    ) -> list[SocialAccount]: ...

    def list_friend_requests(
        self,
        *,
        owner: SocialAccount,
        direction: str,
        state: str,
        limit: int,
        offset: int,
    ) -> list[FriendRequestView]: ...

    def list_blocks(
        self,
        *,
        owner: SocialAccount,
        direction: str,
        limit: int,
        offset: int,
    ) -> list[tuple[SocialAccount, datetime]]: ...
