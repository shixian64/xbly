"""城市级发现与 Web-native 文本匹配领域契约。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


LEGACY_ACCOUNT_PROVIDER = "beibeiwu"
DISCOVERY_PROVIDER = "web-local"
TEXT_MATCH_KIND = "text"

PROFILE_GENDERS = frozenset({"male", "female", "other", "unspecified"})
FILTER_GENDERS = frozenset({"any", "male", "female", "other"})
MATCH_CITY_SCOPES = frozenset({"same-city", "anywhere"})

QUEUE_STATUS_WAITING = "waiting"
QUEUE_STATUS_MATCHED = "matched"
MATCH_STATUS_ACTIVE = "active"


class DiscoveryNativeError(RuntimeError):
    code = "discovery_native_error"


class DiscoveryContractViolation(DiscoveryNativeError):
    code = "discovery_contract_violation"


class InvalidDiscoveryRequest(DiscoveryNativeError):
    code = "invalid_discovery_request"


class DiscoveryIdentityUnavailable(DiscoveryNativeError):
    code = "discovery_identity_unavailable"


class DiscoveryProfileUnavailable(DiscoveryNativeError):
    code = "discovery_profile_unavailable"


class DiscoveryLocationRequired(DiscoveryNativeError):
    code = "discovery_location_required"


class MatchPreferenceUnavailable(DiscoveryNativeError):
    code = "match_preference_unavailable"


class MatchPreferenceConflict(DiscoveryNativeError):
    code = "match_preference_conflict"


class MatchRequestConflict(DiscoveryNativeError):
    code = "match_request_conflict"


class MatchRateLimited(DiscoveryNativeError):
    code = "match_rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass(frozen=True, slots=True)
class DiscoveryPrincipal:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class DiscoveryAccount:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    display_name: str
    active: bool = True
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class DiscoveryProfile:
    user_id: uuid.UUID
    city_code: str | None
    city_name: str | None
    gender: str
    profile_property: str | None
    age: int | None
    discoverable: bool
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    account: DiscoveryAccount
    profile: DiscoveryProfile


@dataclass(frozen=True, slots=True)
class DiscoveryFilters:
    gender: str
    profile_property: str | None
    min_age: int
    max_age: int
    city_code: str | None = None
    city_name: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryCard:
    user_id: uuid.UUID
    upstream_uid: str
    display_name: str
    city_code: str
    city_name: str
    gender: str
    profile_property: str | None
    age: int | None
    online: bool


@dataclass(frozen=True, slots=True)
class DiscoveryList:
    items: tuple[DiscoveryCard, ...]
    filters: DiscoveryFilters
    scanned_count: int


@dataclass(frozen=True, slots=True)
class MatchPreference:
    user_id: uuid.UUID
    city_scope: str
    gender_preference: str
    property_preference: str | None
    min_age: int
    max_age: int
    enabled: bool
    version: int


@dataclass(frozen=True, slots=True)
class MatchQueueEntry:
    id: uuid.UUID
    public_id: str
    user_id: uuid.UUID
    request_id: str
    queue_kind: str
    status: str
    city_code: str | None
    preference_version: int
    enqueued_at: datetime
    expires_at: datetime
    matched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    account: DiscoveryAccount
    profile: DiscoveryProfile
    preference: MatchPreference
    queue_entry: MatchQueueEntry


@dataclass(frozen=True, slots=True)
class MatchFrequencyDecision:
    allowed: bool
    retry_after_seconds: int = 0


@dataclass(frozen=True, slots=True)
class PeerMessageGrant:
    owner_user_id: uuid.UUID
    peer_user_id: uuid.UUID
    peer_upstream_uid: str


@dataclass(frozen=True, slots=True)
class DirectMessageAuthorizationIntent:
    id: uuid.UUID
    match_result_id: uuid.UUID
    grants: tuple[PeerMessageGrant, PeerMessageGrant]
    reason: str
    granted_at: datetime


@dataclass(frozen=True, slots=True)
class LocalMatchResult:
    id: uuid.UUID
    public_id: str
    match_key: str
    user_low_id: uuid.UUID
    user_high_id: uuid.UUID
    user_low_queue_entry_id: uuid.UUID
    user_high_queue_entry_id: uuid.UUID
    initiated_by_user_id: uuid.UUID
    status: str
    matched_at: datetime


@dataclass(frozen=True, slots=True)
class TextMatchCommitPlan:
    match_key: str
    user_low_id: uuid.UUID
    user_high_id: uuid.UUID
    user_low_queue_entry_id: uuid.UUID
    user_high_queue_entry_id: uuid.UUID
    initiated_by_user_id: uuid.UUID
    authorization_intent_id: uuid.UUID
    matched_at: datetime


@dataclass(frozen=True, slots=True)
class TextMatchOutcome:
    status: str
    request_id: str
    queue_entry: MatchQueueEntry
    created: bool
    match_result: LocalMatchResult | None = None
    peer: DiscoveryCard | None = None
    message_authorization: DirectMessageAuthorizationIntent | None = None


@runtime_checkable
class DiscoveryProfileStore(Protocol):
    def resolve_principal(
        self, principal: DiscoveryPrincipal
    ) -> DiscoveryAccount | None: ...

    def get_discovery_profile(self, user_id: uuid.UUID) -> DiscoveryProfile | None: ...

    def save_discovery_profile(
        self, profile: DiscoveryProfile
    ) -> DiscoveryProfile: ...

    def touch_discovery_last_seen(
        self, *, user_id: uuid.UUID, seen_at: datetime
    ) -> DiscoveryProfile: ...

    def list_discovery_candidates(self, *, limit: int) -> list[DiscoveryCandidate]: ...


@runtime_checkable
class DiscoveryRelationshipStore(Protocol):
    def is_blocked_between(
        self, left_user_id: uuid.UUID, right_user_id: uuid.UUID
    ) -> bool: ...


@runtime_checkable
class TextMatchStore(Protocol):
    def get_match_preference(self, user_id: uuid.UUID) -> MatchPreference | None: ...

    def save_match_preference(
        self,
        preference: MatchPreference,
        *,
        expected_version: int | None,
    ) -> MatchPreference: ...

    def get_text_match_outcome(
        self, *, user_id: uuid.UUID, request_id: str
    ) -> TextMatchOutcome | None: ...

    def consume_match_frequency(
        self,
        *,
        user_id: uuid.UUID,
        request_id: str,
        occurred_at: datetime,
        limit: int,
        window_seconds: int,
    ) -> MatchFrequencyDecision:
        """本地原子限频；同一 user/request_id 重试不得重复计数。"""
        ...

    def enqueue_text_match(
        self,
        *,
        account: DiscoveryAccount,
        profile: DiscoveryProfile,
        preference: MatchPreference,
        request_id: str,
        enqueued_at: datetime,
        expires_at: datetime,
    ) -> MatchQueueEntry:
        """按 user/request_id 幂等入队，不同参数必须拒绝。"""
        ...

    def list_waiting_text_candidates(
        self,
        *,
        exclude_user_id: uuid.UUID,
        at: datetime,
        limit: int,
    ) -> list[MatchCandidate]: ...

    def commit_text_match(
        self,
        *,
        requester: MatchCandidate,
        candidate: MatchCandidate,
        plan: TextMatchCommitPlan,
    ) -> TextMatchOutcome:
        """在一个事务内消费双方队列、写规范化 MatchResult 和双向私聊授权。"""
        ...


@runtime_checkable
class DiscoveryNativeStore(
    DiscoveryProfileStore,
    DiscoveryRelationshipStore,
    TextMatchStore,
    Protocol,
):
    pass
