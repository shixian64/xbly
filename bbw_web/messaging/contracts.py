"""与 Banghua/TIM 无关的 Web 本地消息契约。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


LOCAL_MESSAGE_PROVIDER = "web-local"
LOCAL_MESSAGE_SCHEMA = 1
LEGACY_ACCOUNT_PROVIDER = "beibeiwu"
TIM_MIRROR_CHANNEL = "tim"
LOCAL_DELIVERY_CHANNEL = "local"


class LocalMessagingError(RuntimeError):
    code = "local_messaging_error"


class InvalidLocalMessage(LocalMessagingError):
    code = "invalid_local_message"


class LocalMessageSourceStale(InvalidLocalMessage):
    code = "local_message_source_stale"


class LocalIdentityUnavailable(LocalMessagingError):
    code = "local_identity_unavailable"


class PeerNotMigrated(LocalMessagingError):
    code = "peer_not_migrated"


class LocalMessageBlocked(LocalMessagingError):
    code = "local_message_blocked"


class LocalMessageForbidden(LocalMessagingError):
    code = "local_message_forbidden"


class LocalMessageIdempotencyConflict(LocalMessagingError):
    code = "local_message_idempotency_conflict"


class LocalMessageNotFound(LocalMessagingError):
    code = "local_message_not_found"


class LocalMessageRevocationDenied(LocalMessageForbidden):
    code = "local_message_revocation_denied"


class LocalMessageRevocationExpired(LocalMessagingError):
    code = "local_message_revocation_expired"


@dataclass(frozen=True, slots=True)
class LocalPrincipal:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class LocalAccount:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    display_name: str
    retention_days: int


@dataclass(frozen=True, slots=True)
class LocalMessageView:
    id: uuid.UUID
    thread_id: uuid.UUID
    client_message_id: str
    sender_user_id: uuid.UUID
    sender_upstream_uid: str
    recipient_upstream_uid: str
    message_type: str
    body: str
    quote: dict[str, str]
    occurred_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class TimMirrorIntent:
    delivery_id: uuid.UUID
    message_id: uuid.UUID
    from_upstream_uid: str
    to_upstream_uid: str
    client_message_id: str
    text: str
    quote: dict[str, str]
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class TimRevokeIntent:
    delivery_id: uuid.UUID
    message_id: uuid.UUID
    from_upstream_uid: str
    to_upstream_uid: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class LocalDeliveryResult:
    message: LocalMessageView
    sender_projection_id: uuid.UUID
    recipient_projection_id: uuid.UUID
    recipient_user_id: uuid.UUID
    created: bool
    tim_mirror: TimMirrorIntent


@dataclass(frozen=True, slots=True)
class LocalRevokeResult:
    message: LocalMessageView
    recalled_text: str
    revoked_at: datetime
    created: bool
    tim_mirror: TimRevokeIntent | None


@runtime_checkable
class CanonicalMessageStore(Protocol):
    def resolve_principal(self, principal: LocalPrincipal) -> LocalAccount | None: ...

    def resolve_active_peer(
        self, upstream_uid: str, *, provider: str
    ) -> LocalAccount | None: ...

    def is_blocked_between(self, left: LocalAccount, right: LocalAccount) -> bool: ...

    def can_send_private_message(
        self, sender: LocalAccount, recipient: LocalAccount
    ) -> bool: ...

    def store_text(
        self,
        *,
        sender: LocalAccount,
        recipient: LocalAccount,
        client_message_id: str,
        text: str,
        quote: dict[str, str],
        occurred_at: datetime,
        expected_source_message_identity: str = "",
    ) -> LocalDeliveryResult: ...

    def revoke_text(
        self,
        *,
        actor: LocalAccount,
        peer_upstream_uid: str,
        canonical_message_id: uuid.UUID,
        revoked_at: datetime,
    ) -> LocalRevokeResult | None: ...

    def list_direct_messages(
        self,
        *,
        owner: LocalAccount,
        peer_upstream_uid: str,
        before_message_id: uuid.UUID | None,
        limit: int,
    ) -> list[LocalMessageView]: ...

    def mark_direct_read(
        self,
        *,
        owner: LocalAccount,
        peer: LocalAccount,
        occurred_at: datetime,
    ) -> int: ...
