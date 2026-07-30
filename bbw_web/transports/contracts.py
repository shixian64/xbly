"""Provider-neutral transport contracts for external messaging systems."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MessageTransportResult(Protocol):
    """Minimum response shape consumed by message reconciliation jobs."""

    ok: bool
    error_code: int
    error_info: str
    data: Any


@runtime_checkable
class MessageRecallLookupTransport(Protocol):
    """Minimal roaming-history edge used to recover a missing recall key."""

    def roaming_messages(
        self,
        from_account: str,
        to_account: str,
        *,
        min_time: int = 0,
        max_time: int = 0,
        max_count: int = 100,
        last_msg_key: str = "",
    ) -> MessageTransportResult: ...


@runtime_checkable
class MessageHistoryTransport(MessageRecallLookupTransport, Protocol):
    """Server-side history operations needed by durable chat sync."""

    def recent_contacts(
        self,
        user_id: str,
        *,
        timestamp: int = 0,
        start_index: int = 0,
        top_timestamp: int = 0,
        top_start_index: int = 0,
        assist_flags: int = 7,
    ) -> MessageTransportResult: ...

    def c2c_unread_counts(
        self,
        to_account: str,
        peer_accounts: list[str],
    ) -> MessageTransportResult: ...


@runtime_checkable
class MessageSendTransport(Protocol):
    """Provider-neutral text send edge used by compatibility mirrors."""

    def send_text(
        self,
        from_account: str,
        to_account: str,
        text: str,
        *,
        cloud_custom_data: Any = None,
        sync_other_machine: int = 1,
        idempotency_key: str | None = None,
    ) -> MessageTransportResult: ...


@runtime_checkable
class MessageReadTransport(Protocol):
    """Server-side conversation and explicit receipt read operations."""

    def mark_c2c_read(
        self,
        report_account: str,
        peer_account: str,
    ) -> MessageTransportResult: ...

    def sync_c2c_message_read_receipts(
        self,
        operator_account: str,
        peer_account: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        max_messages: int = 300,
        batch_size: int = 30,
        retention_days: int = 180,
    ) -> MessageTransportResult: ...


@runtime_checkable
class MessageMirrorTransport(MessageSendTransport, Protocol):
    """Compatibility edge for text, hosted media elements and recalls."""

    def send_elements(
        self,
        from_account: str,
        to_account: str,
        elements: list[dict[str, Any]],
        *,
        cloud_custom_data: Any = None,
        sync_other_machine: int = 1,
        offline_push_info: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> MessageTransportResult: ...

    def revoke_c2c(
        self,
        from_account: str,
        to_account: str,
        msg_key: str,
    ) -> MessageTransportResult: ...


@runtime_checkable
class MessageTransport(
    MessageHistoryTransport,
    MessageMirrorTransport,
    MessageReadTransport,
    Protocol,
):
    """Complete TIM-compatible transport currently used by background jobs."""
