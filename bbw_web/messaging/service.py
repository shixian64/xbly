"""Web 本地权威消息业务服务。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Callable

from bbw_web.message_quote import normalize_message_quote

from .contracts import (
    CanonicalMessageStore,
    InvalidLocalMessage,
    LocalAccount,
    LocalDeliveryResult,
    LocalIdentityUnavailable,
    LocalMessageBlocked,
    LocalMessageForbidden,
    LocalMessageNotFound,
    LocalMessageRevocationDenied,
    LocalMessageView,
    LocalPrincipal,
    LocalRevokeResult,
    PeerNotMigrated,
)


MAX_TEXT_LENGTH = 2_000
MAX_CLIENT_MESSAGE_ID_LENGTH = 160
MAX_SOURCE_MESSAGE_IDENTITY_LENGTH = 512


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_upstream_uid(value: object) -> str:
    uid = str(value or "").strip()
    if (
        not uid
        or uid.lower() in {"0", "none", "null"}
        or len(uid) > 128
        or any(ord(char) < 33 for char in uid)
    ):
        return ""
    return uid


def normalize_client_message_id(value: object) -> str:
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > MAX_CLIENT_MESSAGE_ID_LENGTH
        or any(ord(char) < 32 for char in identifier)
    ):
        return ""
    return identifier


class LocalMessagingService:
    """本地写入先成功；TIM mirror 只以 outbox intent 返回。"""

    def __init__(
        self,
        store: CanonicalMessageStore,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.store = store
        self.clock = clock

    def _principal_account(self, principal: LocalPrincipal) -> LocalAccount:
        account = self.store.resolve_principal(principal)
        if account is None:
            raise LocalIdentityUnavailable("当前 Web 账号不可用于本地消息")
        return account

    def send_text(
        self,
        *,
        principal: LocalPrincipal,
        peer_upstream_uid: object,
        client_message_id: object,
        text: object,
        quote: object = None,
        expected_source_message_identity: object = "",
    ) -> LocalDeliveryResult:
        peer_uid = normalize_upstream_uid(peer_upstream_uid)
        message_key = normalize_client_message_id(client_message_id)
        body = str(text or "")
        source_identity = str(expected_source_message_identity or "").strip()
        if not peer_uid or peer_uid == normalize_upstream_uid(principal.upstream_uid):
            raise InvalidLocalMessage("聊天对象 UID 不合法")
        if not message_key:
            raise InvalidLocalMessage("client_message_id 不合法")
        if not body.strip() or len(body) > MAX_TEXT_LENGTH:
            raise InvalidLocalMessage("文本消息为空或超过长度限制")
        if source_identity and (
            len(source_identity) > MAX_SOURCE_MESSAGE_IDENTITY_LENGTH
            or any(ord(char) < 32 for char in source_identity)
        ):
            raise InvalidLocalMessage("预期来源消息标识不合法")

        sender = self._principal_account(principal)
        recipient = self.store.resolve_active_peer(
            peer_uid,
            provider=principal.account_provider,
        )
        if recipient is None:
            raise PeerNotMigrated("对方尚未迁移到 Web，本地通道无法投递")
        if sender.user_id == recipient.user_id:
            raise InvalidLocalMessage("不能向自己发送本地消息")
        if self.store.is_blocked_between(sender, recipient):
            raise LocalMessageBlocked("任一方黑名单关系禁止发送消息")
        if not self.store.can_send_private_message(sender, recipient):
            raise LocalMessageForbidden("当前账号无权向该用户发送私聊消息")

        return self.store.store_text(
            sender=sender,
            recipient=recipient,
            client_message_id=message_key,
            text=body,
            quote=normalize_message_quote(quote),
            occurred_at=self.clock(),
            expected_source_message_identity=source_identity,
        )

    def list_direct_messages(
        self,
        *,
        principal: LocalPrincipal,
        peer_upstream_uid: object,
        before_message_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[LocalMessageView]:
        peer_uid = normalize_upstream_uid(peer_upstream_uid)
        if not peer_uid or peer_uid == normalize_upstream_uid(principal.upstream_uid):
            raise InvalidLocalMessage("聊天对象 UID 不合法")
        owner = self._principal_account(principal)
        return self.store.list_direct_messages(
            owner=owner,
            peer_upstream_uid=peer_uid,
            before_message_id=before_message_id,
            limit=min(max(1, int(limit)), 200),
        )

    def revoke_text(
        self,
        *,
        principal: LocalPrincipal,
        peer_upstream_uid: object,
        canonical_message_id: object,
    ) -> LocalRevokeResult:
        peer_uid = normalize_upstream_uid(peer_upstream_uid)
        if not peer_uid or peer_uid == normalize_upstream_uid(principal.upstream_uid):
            raise InvalidLocalMessage("聊天对象 UID 不合法")
        try:
            message_id = uuid.UUID(str(canonical_message_id or "").strip())
        except (TypeError, ValueError) as error:
            raise LocalMessageNotFound("不是 Web 本地 canonical 消息") from error

        actor = self._principal_account(principal)
        result = self.store.revoke_text(
            actor=actor,
            peer_upstream_uid=peer_uid,
            canonical_message_id=message_id,
            revoked_at=self.clock(),
        )
        if result is None:
            raise LocalMessageNotFound("Web 本地 canonical 消息不存在")
        if result.message.sender_user_id != actor.user_id:
            raise LocalMessageRevocationDenied("只有发送者可以撤回消息")
        return result

    def mark_direct_read(
        self,
        *,
        principal: LocalPrincipal,
        peer_upstream_uid: object,
    ) -> int:
        peer_uid = normalize_upstream_uid(peer_upstream_uid)
        if not peer_uid or peer_uid == normalize_upstream_uid(principal.upstream_uid):
            raise InvalidLocalMessage("聊天对象 UID 不合法")
        owner = self._principal_account(principal)
        peer = self.store.resolve_active_peer(
            peer_uid,
            provider=principal.account_provider,
        )
        if peer is None:
            raise PeerNotMigrated("对方尚未迁移到 Web")
        return self.store.mark_direct_read(owner=owner, peer=peer, occurred_at=self.clock())
