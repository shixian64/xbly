"""SQLAlchemy repository for canonical Web-local messaging.

The caller owns the transaction.  One ``store_text`` call writes the canonical
message, both owner-scoped compatibility projections, the local delivery
receipt and the optional TIM outbox row before the transaction can commit.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from bbw_prod.compatibility import (
    TIM_MIRROR_CANCELLED_REASON,
    CompatibilityMode,
    compatibility_mode as resolve_compatibility_mode,
    normalize_compatibility_mode,
)
from bbw_prod.models import (
    ChatMember,
    ChatMessage,
    ChatThread,
    Conversation,
    ExternalAccount,
    Message,
    MessageDelivery,
    MessageReceipt,
    Relationship,
    User,
    UserCredential,
)
from bbw_web.private_message_policy import private_message_permission_query

from .contracts import (
    LEGACY_ACCOUNT_PROVIDER,
    LOCAL_DELIVERY_CHANNEL,
    LOCAL_MESSAGE_PROVIDER,
    LOCAL_MESSAGE_SCHEMA,
    TIM_MIRROR_CHANNEL,
    LocalAccount,
    LocalDeliveryResult,
    LocalMessageBlocked,
    LocalMessageForbidden,
    LocalMessageIdempotencyConflict,
    LocalMessageRevocationDenied,
    LocalMessageRevocationExpired,
    LocalMessageSourceStale,
    LocalMessageView,
    LocalPrincipal,
    LocalRevokeResult,
    TimMirrorIntent,
    TimRevokeIntent,
)


BLOCK_KINDS = ("blacklist", "blacklisted_by")
BLOCK_PROVIDERS = (LEGACY_ACCOUNT_PROVIDER, LOCAL_MESSAGE_PROVIDER)
REVOKED_TEXT_PLACEHOLDER = "消息已撤回"


def block_between_query(left: LocalAccount, right: LocalAccount):
    """Resolve symmetric blocks while honoring Web-local inactive tombstones."""

    left_override = aliased(Relationship, name="left_local_block_override")
    right_override = aliased(Relationship, name="right_local_block_override")
    left_has_local = (
        select(left_override.id)
        .where(
            left_override.owner_user_id == left.user_id,
            left_override.provider == LOCAL_MESSAGE_PROVIDER,
            left_override.subject_upstream_uid == right.upstream_uid,
            left_override.kind == "blacklist",
        )
        .exists()
    )
    right_has_local = (
        select(right_override.id)
        .where(
            right_override.owner_user_id == right.user_id,
            right_override.provider == LOCAL_MESSAGE_PROVIDER,
            right_override.subject_upstream_uid == left.upstream_uid,
            right_override.kind == "blacklist",
        )
        .exists()
    )
    left_to_right = and_(
        Relationship.owner_user_id == left.user_id,
        Relationship.subject_upstream_uid == right.upstream_uid,
    )
    right_to_left = and_(
        Relationship.owner_user_id == right.user_id,
        Relationship.subject_upstream_uid == left.upstream_uid,
    )
    return (
        select(Relationship.id)
        .where(
            Relationship.provider.in_(BLOCK_PROVIDERS),
            Relationship.kind.in_(BLOCK_KINDS),
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
            or_(
                and_(
                    Relationship.provider == LOCAL_MESSAGE_PROVIDER,
                    Relationship.kind == "blacklist",
                    or_(left_to_right, right_to_left),
                ),
                and_(
                    Relationship.provider == LEGACY_ACCOUNT_PROVIDER,
                    or_(
                        and_(
                            left_to_right,
                            or_(
                                and_(
                                    Relationship.kind == "blacklist",
                                    ~left_has_local,
                                ),
                                and_(
                                    Relationship.kind == "blacklisted_by",
                                    ~right_has_local,
                                ),
                            ),
                        ),
                        and_(
                            right_to_left,
                            or_(
                                and_(
                                    Relationship.kind == "blacklist",
                                    ~right_has_local,
                                ),
                                and_(
                                    Relationship.kind == "blacklisted_by",
                                    ~left_has_local,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        .limit(1)
    )


def direct_thread_key(left_user_id: uuid.UUID, right_user_id: uuid.UUID) -> str:
    left, right = sorted((str(left_user_id), str(right_user_id)))
    return f"direct:{left}:{right}"


def _payload_digest(
    *,
    direct_key: str,
    recipient_user_id: uuid.UUID,
    message_type: str,
    body: str,
    quote: dict[str, str],
) -> str:
    payload = {
        "body": body,
        "direct_key": direct_key,
        "message_type": message_type,
        "recipient_user_id": str(recipient_user_id),
        "quote": quote,
        "schema": LOCAL_MESSAGE_SCHEMA,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_view(row: ChatMessage) -> LocalMessageView:
    metadata = dict(row.extra_data or {})
    return LocalMessageView(
        id=row.id,
        thread_id=row.thread_id,
        client_message_id=row.client_message_id,
        sender_user_id=row.sender_user_id,
        sender_upstream_uid=str(metadata.get("sender_upstream_uid") or ""),
        recipient_upstream_uid=str(metadata.get("recipient_upstream_uid") or ""),
        message_type=row.message_type,
        body=str(row.body or ""),
        quote=dict(metadata.get("quote") or {}),
        occurred_at=row.occurred_at,
        status=row.status,
    )


def _projection_message_identity(row: Message) -> str:
    metadata = row.extra_data if isinstance(row.extra_data, dict) else {}
    canonical = str(metadata.get("canonical_message_id") or "").strip()
    return canonical or f"{row.provider}:{row.upstream_message_id}"


def _projection_message_revoked(row: Message) -> bool:
    metadata = row.extra_data if isinstance(row.extra_data, dict) else {}
    return bool(
        str(row.status or "").strip().lower() == "revoked"
        or str(metadata.get("revoked") or "").strip().lower() in {"1", "true"}
    )


class SqlAlchemyCanonicalMessageStore:
    def __init__(self, db: Session, *, compatibility_mode: str | None = None) -> None:
        self.db = db
        self.compatibility_mode = (
            normalize_compatibility_mode(compatibility_mode)
            if compatibility_mode is not None
            else resolve_compatibility_mode()
        )

    @staticmethod
    def _account(row: tuple[User, ExternalAccount]) -> LocalAccount:
        user, account = row
        return LocalAccount(
            user_id=user.id,
            external_account_id=account.id,
            upstream_uid=str(account.upstream_uid or ""),
            display_name=str(user.display_name or ""),
            retention_days=max(1, int(user.chat_retention_days or 180)),
        )

    def resolve_principal(self, principal: LocalPrincipal) -> LocalAccount | None:
        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                User.id == principal.user_id,
                User.status == "active",
                UserCredential.disabled_at.is_(None),
                ExternalAccount.id == principal.external_account_id,
                ExternalAccount.provider == principal.account_provider,
                ExternalAccount.upstream_uid == principal.upstream_uid,
            )
        ).one_or_none()
        return self._account((row[0], row[1])) if row is not None else None

    def resolve_active_peer(
        self, upstream_uid: str, *, provider: str = LEGACY_ACCOUNT_PROVIDER
    ) -> LocalAccount | None:
        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                User.status == "active",
                UserCredential.disabled_at.is_(None),
                ExternalAccount.provider == provider,
                ExternalAccount.upstream_uid == upstream_uid,
            )
        ).one_or_none()
        return self._account((row[0], row[1])) if row is not None else None

    def is_blocked_between(self, left: LocalAccount, right: LocalAccount) -> bool:
        blocked = self.db.scalar(block_between_query(left, right))
        return blocked is not None

    def can_send_private_message(
        self,
        sender: LocalAccount,
        recipient: LocalAccount,
    ) -> bool:
        proactive = self.db.scalar(
            select(User.match_pool_online_list_enabled).where(
                User.id == sender.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
        )
        return bool(
            self.db.scalar(
                private_message_permission_query(
                    sender_user_id=sender.user_id,
                    sender_upstream_uid=sender.upstream_uid,
                    recipient_user_id=recipient.user_id,
                    recipient_upstream_uid=recipient.upstream_uid,
                    proactive_private_message=bool(proactive),
                )
            )
        )

    def _ensure_thread(
        self, sender: LocalAccount, recipient: LocalAccount, direct_key: str
    ) -> ChatThread:
        statement = insert(ChatThread).values(
            kind="direct",
            direct_key=direct_key,
            created_by_user_id=sender.user_id,
            status="active",
            extra_data={"schema": LOCAL_MESSAGE_SCHEMA, "authority": LOCAL_MESSAGE_PROVIDER},
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_chat_threads_direct_key",
            set_={"direct_key": statement.excluded.direct_key},
        ).returning(ChatThread)
        thread = self.db.scalars(statement).one()
        locked = self.db.scalar(
            select(ChatThread)
            .where(
                ChatThread.id == thread.id,
                ChatThread.direct_key == direct_key,
                ChatThread.kind == "direct",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise RuntimeError("direct message thread lock was lost")
        return locked

    def _assert_expected_source_is_current(
        self,
        *,
        sender: LocalAccount,
        recipient: LocalAccount,
        expected_source_message_identity: str,
    ) -> None:
        expected = str(expected_source_message_identity or "").strip()
        if not expected:
            return
        latest = self.db.scalar(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.owner_user_id == sender.user_id,
                Message.provider == LOCAL_MESSAGE_PROVIDER,
                Conversation.owner_user_id == sender.user_id,
                Conversation.provider == LOCAL_MESSAGE_PROVIDER,
                Conversation.peer_upstream_uid == recipient.upstream_uid,
                Conversation.kind == "direct",
            )
            .order_by(Message.occurred_at.desc(), Message.id.desc())
            .limit(1)
        )
        outgoing_after = None
        if latest is not None:
            outgoing_after = self.db.scalar(
                select(Message.id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.owner_user_id == sender.user_id,
                    Message.provider == LOCAL_MESSAGE_PROVIDER,
                    Conversation.owner_user_id == sender.user_id,
                    Conversation.provider == LOCAL_MESSAGE_PROVIDER,
                    Conversation.peer_upstream_uid == recipient.upstream_uid,
                    Conversation.kind == "direct",
                    Message.direction == "outgoing",
                    or_(
                        Message.occurred_at > latest.occurred_at,
                        and_(
                            Message.occurred_at == latest.occurred_at,
                            Message.id > latest.id,
                        ),
                    ),
                )
                .limit(1)
            )
        if (
            latest is None
            or _projection_message_identity(latest) != expected
            or str(latest.direction or "").strip().lower() != "incoming"
            or str(latest.message_type or "").strip().lower()
            not in {"text", "timtextelem"}
            or not str(latest.body or "").strip()
            or _projection_message_revoked(latest)
            or outgoing_after is not None
        ):
            raise LocalMessageSourceStale(
                "待回复消息已变化或已被回复，本次未发送"
            )

    def _ensure_members(
        self, thread: ChatThread, accounts: tuple[LocalAccount, LocalAccount]
    ) -> dict[uuid.UUID, ChatMember]:
        members: dict[uuid.UUID, ChatMember] = {}
        for account in sorted(accounts, key=lambda item: str(item.user_id)):
            statement = insert(ChatMember).values(
                thread_id=thread.id,
                user_id=account.user_id,
                role="member",
                left_at=None,
                extra_data={"upstream_uid": account.upstream_uid},
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_chat_members_thread_user",
                set_={"left_at": None, "role": "member"},
            ).returning(ChatMember)
            member = self.db.scalars(statement).one()
            members[member.user_id] = member
        return members

    def _ensure_projection_conversations(
        self,
        thread: ChatThread,
        sender: LocalAccount,
        recipient: LocalAccount,
    ) -> dict[uuid.UUID, Conversation]:
        conversations: dict[uuid.UUID, Conversation] = {}
        source_id = f"{LOCAL_MESSAGE_PROVIDER}:{thread.id}"
        pairs = ((sender, recipient), (recipient, sender))
        for owner, peer in sorted(pairs, key=lambda item: str(item[0].user_id)):
            statement = insert(Conversation).values(
                owner_user_id=owner.user_id,
                provider=LOCAL_MESSAGE_PROVIDER,
                upstream_conversation_id=source_id,
                peer_upstream_uid=peer.upstream_uid,
                kind="direct",
                title=peer.display_name or None,
                unread_count=0,
                extra_data={
                    "authority": LOCAL_MESSAGE_PROVIDER,
                    "canonical_thread_id": str(thread.id),
                    "peer_user_id": str(peer.user_id),
                    "schema": LOCAL_MESSAGE_SCHEMA,
                },
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_conversations_owner_source",
                set_={
                    "peer_upstream_uid": statement.excluded.peer_upstream_uid,
                    "kind": "direct",
                    "title": statement.excluded.title,
                    "metadata": Conversation.extra_data.op("||")(
                        statement.excluded.metadata
                    ),
                },
            ).returning(Conversation)
            conversation = self.db.scalars(statement).one()
            conversations[conversation.owner_user_id] = conversation
        return conversations

    def _insert_projection(
        self,
        *,
        owner: LocalAccount,
        conversation: Conversation,
        canonical: ChatMessage,
        sender: LocalAccount,
        recipient: LocalAccount,
        direction: str,
    ) -> tuple[Message, bool]:
        metadata = {
            "authority": LOCAL_MESSAGE_PROVIDER,
            "canonical_message_id": str(canonical.id),
            "canonical_thread_id": str(canonical.thread_id),
            "client_message_key": canonical.client_message_id,
            "is_peer_read": False,
            "message_key": str(canonical.id),
            "object_name": "TIMTextElem",
            "schema": LOCAL_MESSAGE_SCHEMA,
            "source": LOCAL_MESSAGE_PROVIDER,
            "quote": dict(canonical.extra_data or {}).get("quote") or {},
        }
        statement = (
            insert(Message)
            .values(
                owner_user_id=owner.user_id,
                conversation_id=conversation.id,
                provider=LOCAL_MESSAGE_PROVIDER,
                upstream_message_id=str(canonical.id),
                direction=direction,
                sender_upstream_uid=sender.upstream_uid,
                recipient_upstream_uid=recipient.upstream_uid,
                message_type="text",
                body=canonical.body,
                status="sent" if direction == "outgoing" else "received",
                occurred_at=canonical.occurred_at,
                retention_expires_at=canonical.occurred_at
                + timedelta(days=owner.retention_days),
                extra_data=metadata,
            )
            .on_conflict_do_nothing(constraint="uq_messages_owner_source")
            .returning(Message)
        )
        created = self.db.scalars(statement).first()
        if created is not None:
            return created, True
        existing = self.db.scalar(
            select(Message).where(
                Message.owner_user_id == owner.user_id,
                Message.provider == LOCAL_MESSAGE_PROVIDER,
                Message.upstream_message_id == str(canonical.id),
            )
        )
        if (
            existing is None
            or existing.conversation_id != conversation.id
            or existing.body != canonical.body
            or existing.direction != direction
        ):
            raise LocalMessageIdempotencyConflict("本地消息投影与幂等键冲突")
        return existing, False

    def _ensure_delivery(
        self,
        *,
        canonical: ChatMessage,
        sender: LocalAccount,
        recipient: LocalAccount,
        channel: str,
        occurred_at: datetime,
        projection_ids: tuple[uuid.UUID, uuid.UUID],
    ) -> MessageDelivery:
        is_local = channel == LOCAL_DELIVERY_CHANNEL
        mirror_retired = (
            not is_local and self.compatibility_mode is CompatibilityMode.RETIRED
        )
        status="delivered" if is_local else "pending"
        if mirror_retired:
            status = "cancelled"
        target_key = str(recipient.user_id) if is_local else recipient.upstream_uid
        payload: dict[str, Any] = {
            "canonical_message_id": str(canonical.id),
            "client_message_id": canonical.client_message_id,
            "from": sender.upstream_uid,
            "message_type": "text",
            "schema": LOCAL_MESSAGE_SCHEMA,
            "text": str(canonical.body or ""),
            "to": recipient.upstream_uid,
            "quote": dict(canonical.extra_data or {}).get("quote") or {},
        }
        if is_local:
            payload["projection_message_ids"] = [str(item) for item in projection_ids]
        statement = (
            insert(MessageDelivery)
            .values(
                message_id=canonical.id,
                channel=channel,
                target_key=target_key,
                target_user_id=recipient.user_id if is_local else None,
                target_upstream_uid=recipient.upstream_uid,
                required=is_local,
                status=status,
                delivered_at=occurred_at if is_local else None,
                last_error=TIM_MIRROR_CANCELLED_REASON if mirror_retired else None,
                payload=payload,
            )
            .on_conflict_do_nothing(constraint="uq_message_deliveries_message_target")
            .returning(MessageDelivery)
        )
        created = self.db.scalars(statement).first()
        if created is not None:
            return created
        existing = self.db.scalar(
            select(MessageDelivery).where(
                MessageDelivery.message_id == canonical.id,
                MessageDelivery.channel == channel,
                MessageDelivery.target_key == target_key,
            )
        )
        if existing is None:
            raise RuntimeError("message delivery conflict occurred without an existing row")
        return existing

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
    ) -> LocalDeliveryResult:
        if self.is_blocked_between(sender, recipient):
            raise LocalMessageBlocked("任一方黑名单关系禁止发送消息")
        if not self.can_send_private_message(sender, recipient):
            raise LocalMessageForbidden("当前账号无权向该用户发送私聊消息")
        direct_key = direct_thread_key(sender.user_id, recipient.user_id)
        digest = _payload_digest(
            direct_key=direct_key,
            recipient_user_id=recipient.user_id,
            message_type="text",
            body=text,
            quote=quote,
        )
        thread = self._ensure_thread(sender, recipient, direct_key)
        self._assert_expected_source_is_current(
            sender=sender,
            recipient=recipient,
            expected_source_message_identity=expected_source_message_identity,
        )
        members = self._ensure_members(thread, (sender, recipient))
        conversations = self._ensure_projection_conversations(thread, sender, recipient)
        retention_days = max(sender.retention_days, recipient.retention_days)
        metadata = {
            "recipient_upstream_uid": recipient.upstream_uid,
            "recipient_user_id": str(recipient.user_id),
            "schema": LOCAL_MESSAGE_SCHEMA,
            "sender_upstream_uid": sender.upstream_uid,
            "quote": quote,
        }
        statement = (
            insert(ChatMessage)
            .values(
                thread_id=thread.id,
                sender_user_id=sender.user_id,
                client_message_id=client_message_id,
                payload_digest=digest,
                message_type="text",
                body=text,
                status="accepted",
                occurred_at=occurred_at,
                retention_expires_at=occurred_at + timedelta(days=retention_days),
                extra_data=metadata,
            )
            .on_conflict_do_nothing(constraint="uq_chat_messages_sender_client")
            .returning(ChatMessage)
        )
        canonical = self.db.scalars(statement).first()
        created = canonical is not None
        if canonical is None:
            canonical = self.db.scalar(
                select(ChatMessage).where(
                    ChatMessage.sender_user_id == sender.user_id,
                    ChatMessage.client_message_id == client_message_id,
                )
            )
            if (
                canonical is None
                or canonical.thread_id != thread.id
                or canonical.payload_digest != digest
                or canonical.message_type != "text"
                or canonical.body != text
            ):
                raise LocalMessageIdempotencyConflict(
                    "client_message_id 已用于另一条消息"
                )

        sender_projection, _sender_projection_created = self._insert_projection(
            owner=sender,
            conversation=conversations[sender.user_id],
            canonical=canonical,
            sender=sender,
            recipient=recipient,
            direction="outgoing",
        )
        recipient_projection, recipient_projection_created = self._insert_projection(
            owner=recipient,
            conversation=conversations[recipient.user_id],
            canonical=canonical,
            sender=sender,
            recipient=recipient,
            direction="incoming",
        )

        preview = {
            "last_message": text[:500],
            "last_message_id": str(canonical.id),
            "preview_authoritative": True,
            "preview_timestamp": canonical.occurred_at.isoformat(),
            "source": LOCAL_MESSAGE_PROVIDER,
        }
        if created:
            thread.last_message_at = canonical.occurred_at
            recipient_member = members[recipient.user_id]
            recipient_member.unread_count = int(recipient_member.unread_count or 0) + 1
        for owner_id, conversation in conversations.items():
            if (
                conversation.last_message_at is None
                or conversation.last_message_at <= canonical.occurred_at
            ):
                conversation.last_message_at = canonical.occurred_at
                conversation.extra_data = {**dict(conversation.extra_data or {}), **preview}
            if owner_id == recipient.user_id and recipient_projection_created:
                conversation.unread_count = int(conversation.unread_count or 0) + 1
                conversation.unread_observed_at = canonical.occurred_at

        receipt_statement = (
            insert(MessageReceipt)
            .values(
                message_id=canonical.id,
                user_id=recipient.user_id,
                receipt_type="delivered",
                occurred_at=canonical.occurred_at,
                extra_data={"channel": LOCAL_DELIVERY_CHANNEL},
            )
            .on_conflict_do_nothing(
                constraint="uq_message_receipts_message_user_type"
            )
        )
        self.db.execute(receipt_statement)
        projection_ids = (sender_projection.id, recipient_projection.id)
        self._ensure_delivery(
            canonical=canonical,
            sender=sender,
            recipient=recipient,
            channel=LOCAL_DELIVERY_CHANNEL,
            occurred_at=canonical.occurred_at,
            projection_ids=projection_ids,
        )
        tim_delivery = self._ensure_delivery(
            canonical=canonical,
            sender=sender,
            recipient=recipient,
            channel=TIM_MIRROR_CHANNEL,
            occurred_at=canonical.occurred_at,
            projection_ids=projection_ids,
        )
        self.db.flush()
        return LocalDeliveryResult(
            message=_as_view(canonical),
            sender_projection_id=sender_projection.id,
            recipient_projection_id=recipient_projection.id,
            recipient_user_id=recipient.user_id,
            created=created,
            tim_mirror=TimMirrorIntent(
                delivery_id=tim_delivery.id,
                message_id=canonical.id,
                from_upstream_uid=sender.upstream_uid,
                to_upstream_uid=recipient.upstream_uid,
                client_message_id=canonical.client_message_id,
                text=str(canonical.body or ""),
                quote=dict(canonical.extra_data or {}).get("quote") or {},
                status=str(tim_delivery.status or "pending"),
            ),
        )

    @staticmethod
    def _text_revoke_payload(
        canonical: ChatMessage,
        *,
        sender_upstream_uid: str,
        recipient_upstream_uid: str,
    ) -> dict[str, Any]:
        return {
            "operation": "revoke",
            "canonical_message_id": str(canonical.id),
            "client_message_id": canonical.client_message_id,
            "from": sender_upstream_uid,
            "to": recipient_upstream_uid,
            "message_type": "text",
            "schema": LOCAL_MESSAGE_SCHEMA,
        }

    def _existing_text_revoke_delivery(
        self,
        canonical: ChatMessage,
        *,
        sender_upstream_uid: str,
        recipient_upstream_uid: str,
    ) -> MessageDelivery | None:
        target_key = f"text-revoke:{recipient_upstream_uid}"
        delivery = self.db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.message_id == canonical.id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.target_key == target_key,
            )
            .with_for_update()
        )
        expected = self._text_revoke_payload(
            canonical,
            sender_upstream_uid=sender_upstream_uid,
            recipient_upstream_uid=recipient_upstream_uid,
        )
        if delivery is not None and dict(delivery.payload or {}) != expected:
            raise LocalMessageIdempotencyConflict("文字撤回 outbox 冲突")
        return delivery

    def _ensure_text_revoke_delivery(
        self,
        canonical: ChatMessage,
        *,
        sender_upstream_uid: str,
        recipient_upstream_uid: str,
    ) -> MessageDelivery:
        target_key = f"text-revoke:{recipient_upstream_uid}"
        mirror_retired = self.compatibility_mode is CompatibilityMode.RETIRED
        payload = self._text_revoke_payload(
            canonical,
            sender_upstream_uid=sender_upstream_uid,
            recipient_upstream_uid=recipient_upstream_uid,
        )
        statement = (
            insert(MessageDelivery)
            .values(
                message_id=canonical.id,
                channel=TIM_MIRROR_CHANNEL,
                target_key=target_key,
                target_upstream_uid=recipient_upstream_uid,
                required=False,
                status="cancelled" if mirror_retired else "pending",
                last_error=TIM_MIRROR_CANCELLED_REASON if mirror_retired else None,
                payload=payload,
            )
            .on_conflict_do_nothing(
                constraint="uq_message_deliveries_message_target"
            )
            .returning(MessageDelivery)
        )
        delivery = self.db.scalars(statement).first()
        if delivery is not None:
            return delivery
        existing = self._existing_text_revoke_delivery(
            canonical,
            sender_upstream_uid=sender_upstream_uid,
            recipient_upstream_uid=recipient_upstream_uid,
        )
        if existing is None:
            raise LocalMessageIdempotencyConflict("文字撤回 outbox 不存在")
        return existing

    def _reconcile_tim_text_send_on_revoke(
        self,
        canonical: ChatMessage,
        *,
        sender_upstream_uid: str,
        recipient_upstream_uid: str,
    ) -> MessageDelivery | None:
        """Cancel an unsent text mirror and compensate a possible TIM send."""

        send_delivery = self.db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.message_id == canonical.id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.target_key == recipient_upstream_uid,
            )
            .with_for_update()
        )
        if send_delivery is None:
            return self._ensure_text_revoke_delivery(
                canonical,
                sender_upstream_uid=sender_upstream_uid,
                recipient_upstream_uid=recipient_upstream_uid,
            )
        operation = str((send_delivery.payload or {}).get("operation") or "send")
        if operation != "send":
            raise LocalMessageIdempotencyConflict("文字发送 outbox 类型冲突")

        send_status = str(send_delivery.status or "")
        if send_status in {"pending", "retry", "failed"}:
            send_delivery.status = "cancelled"
            send_delivery.locked_by = None
            send_delivery.locked_until = None
            send_delivery.last_error = "cancelled by local text revoke"
            return None
        if send_status == "cancelled":
            existing = self._existing_text_revoke_delivery(
                canonical,
                sender_upstream_uid=sender_upstream_uid,
                recipient_upstream_uid=recipient_upstream_uid,
            )
            if existing is not None or str(send_delivery.last_error or "") in {
                TIM_MIRROR_CANCELLED_REASON,
                "cancelled by local text revoke",
            }:
                return existing
            return self._ensure_text_revoke_delivery(
                canonical,
                sender_upstream_uid=sender_upstream_uid,
                recipient_upstream_uid=recipient_upstream_uid,
            )
        if send_status == "processing":
            send_delivery.status = "cancelled"
            send_delivery.locked_by = None
            send_delivery.locked_until = None
            send_delivery.last_error = "cancelled while TIM text send was processing"
            return self._ensure_text_revoke_delivery(
                canonical,
                sender_upstream_uid=sender_upstream_uid,
                recipient_upstream_uid=recipient_upstream_uid,
            )
        if send_status == "delivered":
            return self._ensure_text_revoke_delivery(
                canonical,
                sender_upstream_uid=sender_upstream_uid,
                recipient_upstream_uid=recipient_upstream_uid,
            )
        return self._ensure_text_revoke_delivery(
            canonical,
            sender_upstream_uid=sender_upstream_uid,
            recipient_upstream_uid=recipient_upstream_uid,
        )

    def revoke_text(
        self,
        *,
        actor: LocalAccount,
        peer_upstream_uid: str,
        canonical_message_id: uuid.UUID,
        revoked_at: datetime,
    ) -> LocalRevokeResult | None:
        canonical = self.db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.id == canonical_message_id,
                ChatMessage.message_type == "text",
            )
            .with_for_update()
        )
        if canonical is None:
            return None
        metadata = dict(canonical.extra_data or {})
        recipient_upstream_uid = str(
            metadata.get("recipient_upstream_uid") or ""
        ).strip()
        sender_upstream_uid = str(metadata.get("sender_upstream_uid") or "").strip()
        if canonical.sender_user_id != actor.user_id:
            raise LocalMessageRevocationDenied("只有发送者可以撤回消息")
        if not recipient_upstream_uid or recipient_upstream_uid != peer_upstream_uid:
            raise LocalMessageRevocationDenied("消息与当前聊天对象不匹配")
        if sender_upstream_uid != actor.upstream_uid:
            raise LocalMessageRevocationDenied("消息发送身份不匹配")

        projections = list(
            self.db.scalars(
                select(Message)
                .where(
                    Message.provider == LOCAL_MESSAGE_PROVIDER,
                    Message.upstream_message_id == str(canonical.id),
                )
                .with_for_update()
            )
        )
        sender_projection = next(
            (
                row
                for row in projections
                if row.owner_user_id == actor.user_id and row.direction == "outgoing"
            ),
            None,
        )
        created = str(canonical.status or "") != "revoked"
        recalled_text = ""
        effective_revoked_at = revoked_at
        if sender_projection is not None:
            recalled_text = str(
                dict(sender_projection.extra_data or {}).get("recalled_text") or ""
            )
        if not created:
            try:
                effective_revoked_at = datetime.fromisoformat(
                    str(metadata.get("revoked_at") or "")
                )
            except ValueError:
                effective_revoked_at = revoked_at
        if created:
            if revoked_at > canonical.occurred_at + timedelta(minutes=2):
                raise LocalMessageRevocationExpired("文字消息撤回期限为 2 分钟")
            recalled_text = str(canonical.body or "")
            canonical.status = "revoked"
            canonical.body = REVOKED_TEXT_PLACEHOLDER
            metadata.pop("quote", None)
            metadata.update(
                {
                    "revoked": True,
                    "revoked_at": revoked_at.isoformat(),
                }
            )
            canonical.extra_data = metadata
            for projection in projections:
                projection_metadata = dict(projection.extra_data or {})
                projection_metadata.pop("quote", None)
                projection_metadata.update(
                    {
                        "revoked": True,
                        "revoked_at": revoked_at.isoformat(),
                    }
                )
                if (
                    projection.owner_user_id == actor.user_id
                    and projection.direction == "outgoing"
                ):
                    projection_metadata["recalled_text"] = recalled_text
                else:
                    projection_metadata.pop("recalled_text", None)
                projection.status = "revoked"
                projection.body = None
                projection.extra_data = projection_metadata

            conversations = list(
                self.db.scalars(
                    select(Conversation)
                    .where(
                        Conversation.provider == LOCAL_MESSAGE_PROVIDER,
                        Conversation.upstream_conversation_id
                        == f"{LOCAL_MESSAGE_PROVIDER}:{canonical.thread_id}",
                    )
                    .with_for_update()
                )
            )
            for conversation in conversations:
                conversation_metadata = dict(conversation.extra_data or {})
                preview_message_id = str(
                    conversation_metadata.get("last_message_id") or ""
                )
                if (
                    preview_message_id == str(canonical.id)
                    or (
                        not preview_message_id
                        and conversation.last_message_at == canonical.occurred_at
                    )
                ):
                    conversation_metadata["last_message"] = REVOKED_TEXT_PLACEHOLDER
                    conversation_metadata["last_message_id"] = str(canonical.id)
                    conversation.extra_data = conversation_metadata

        delivery = self._reconcile_tim_text_send_on_revoke(
            canonical,
            sender_upstream_uid=sender_upstream_uid,
            recipient_upstream_uid=recipient_upstream_uid,
        )
        self.db.flush()
        return LocalRevokeResult(
            message=_as_view(canonical),
            recalled_text=recalled_text,
            revoked_at=effective_revoked_at,
            created=created,
            tim_mirror=(
                TimRevokeIntent(
                    delivery_id=delivery.id,
                    message_id=canonical.id,
                    from_upstream_uid=sender_upstream_uid,
                    to_upstream_uid=recipient_upstream_uid,
                    status=str(delivery.status or "pending"),
                )
                if delivery is not None
                else None
            ),
        )

    def list_direct_messages(
        self,
        *,
        owner: LocalAccount,
        peer_upstream_uid: str,
        before_message_id: uuid.UUID | None,
        limit: int,
    ) -> list[LocalMessageView]:
        peer_account = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                ExternalAccount.provider == LEGACY_ACCOUNT_PROVIDER,
                ExternalAccount.upstream_uid == peer_upstream_uid,
            )
        ).one_or_none()
        if peer_account is None:
            return []
        peer_user_id = peer_account[0].id
        direct_key = direct_thread_key(owner.user_id, peer_user_id)
        thread = self.db.scalar(
            select(ChatThread)
            .join(ChatMember, ChatMember.thread_id == ChatThread.id)
            .where(
                ChatThread.direct_key == direct_key,
                ChatThread.status == "active",
                ChatMember.user_id == owner.user_id,
                ChatMember.left_at.is_(None),
            )
        )
        if thread is None:
            return []
        statement = select(ChatMessage).where(ChatMessage.thread_id == thread.id)
        if before_message_id is not None:
            cursor = self.db.scalar(
                select(ChatMessage).where(
                    ChatMessage.id == before_message_id,
                    ChatMessage.thread_id == thread.id,
                )
            )
            if cursor is None:
                return []
            statement = statement.where(
                or_(
                    ChatMessage.occurred_at < cursor.occurred_at,
                    and_(
                        ChatMessage.occurred_at == cursor.occurred_at,
                        ChatMessage.id < cursor.id,
                    ),
                )
            )
        rows = list(
            self.db.scalars(
                statement.order_by(
                    ChatMessage.occurred_at.desc(), ChatMessage.id.desc()
                ).limit(min(max(1, int(limit)), 200))
            )
        )
        return [_as_view(row) for row in reversed(rows)]

    def mark_direct_read(
        self,
        *,
        owner: LocalAccount,
        peer: LocalAccount,
        occurred_at: datetime,
    ) -> int:
        direct_key = direct_thread_key(owner.user_id, peer.user_id)
        thread = self.db.scalar(
            select(ChatThread).where(ChatThread.direct_key == direct_key)
        )
        if thread is None:
            return 0
        member = self.db.scalar(
            select(ChatMember)
            .where(
                ChatMember.thread_id == thread.id,
                ChatMember.user_id == owner.user_id,
                ChatMember.left_at.is_(None),
            )
            .with_for_update()
        )
        if member is None:
            return 0
        previous_unread = int(member.unread_count or 0)
        member.unread_count = 0
        member.last_read_at = occurred_at
        conversation = self.db.scalar(
            select(Conversation).where(
                Conversation.owner_user_id == owner.user_id,
                Conversation.provider == LOCAL_MESSAGE_PROVIDER,
                Conversation.upstream_conversation_id
                == f"{LOCAL_MESSAGE_PROVIDER}:{thread.id}",
            )
        )
        if conversation is not None:
            conversation.unread_count = 0
            conversation.unread_observed_at = occurred_at
        latest_incoming = self.db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.thread_id == thread.id,
                ChatMessage.sender_user_id == peer.user_id,
            )
            .order_by(ChatMessage.occurred_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
        if latest_incoming is not None:
            self.db.execute(
                insert(MessageReceipt)
                .values(
                    message_id=latest_incoming.id,
                    user_id=owner.user_id,
                    receipt_type="read",
                    occurred_at=occurred_at,
                    extra_data={"covers_thread_through": str(latest_incoming.id)},
                )
                .on_conflict_do_update(
                    constraint="uq_message_receipts_message_user_type",
                    set_={"occurred_at": occurred_at},
                )
            )
            sender_projections = list(
                self.db.scalars(
                    select(Message).where(
                        Message.owner_user_id == peer.user_id,
                        Message.provider == LOCAL_MESSAGE_PROVIDER,
                        Message.direction == "outgoing",
                        Message.sender_upstream_uid == peer.upstream_uid,
                        Message.recipient_upstream_uid == owner.upstream_uid,
                        Message.occurred_at <= latest_incoming.occurred_at,
                    )
                )
            )
            for projection in sender_projections:
                metadata = dict(projection.extra_data or {})
                metadata.update(
                    {
                        "is_peer_read": True,
                        "read_at": occurred_at.isoformat(),
                    }
                )
                projection.extra_data = metadata
        self.db.flush()
        return previous_unread

    def claim_tim_outbox(
        self,
        *,
        worker_id: str,
        lock_until: datetime,
        now: datetime,
        limit: int = 20,
    ) -> list[MessageDelivery]:
        rows = list(
            self.db.scalars(
                select(MessageDelivery)
                .where(
                    MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                    or_(
                        and_(
                            MessageDelivery.status.in_(("pending", "retry")),
                            MessageDelivery.available_at <= now,
                            or_(
                                MessageDelivery.locked_until.is_(None),
                                MessageDelivery.locked_until <= now,
                            ),
                        ),
                        and_(
                            MessageDelivery.status == "processing",
                            MessageDelivery.locked_until.is_not(None),
                            MessageDelivery.locked_until <= now,
                        ),
                    ),
                    MessageDelivery.attempt_count < MessageDelivery.max_attempts,
                )
                .order_by(MessageDelivery.available_at, MessageDelivery.created_at)
                .with_for_update(skip_locked=True)
                .limit(min(max(1, int(limit)), 100))
            )
        )
        for row in rows:
            row.status = "processing"
            row.locked_by = worker_id[:128]
            row.locked_until = lock_until
            row.attempt_count += 1
        self.db.flush()
        return rows

    def mark_tim_delivered(
        self,
        delivery_id: uuid.UUID,
        *,
        delivered_at: datetime,
        upstream_message_id: str = "",
    ) -> MessageDelivery | None:
        row = self.db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.id == delivery_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.status != "processing":
            return row
        row.status = "delivered"
        row.delivered_at = delivered_at
        row.locked_by = None
        row.locked_until = None
        row.last_error = None
        if upstream_message_id:
            row.payload = {
                **dict(row.payload or {}),
                "upstream_message_id": str(upstream_message_id)[:256],
            }
        self.db.flush()
        return row

    def mark_tim_failed(
        self,
        delivery_id: uuid.UUID,
        *,
        error: str,
        retry_at: datetime,
    ) -> MessageDelivery | None:
        """Record mirror failure without changing canonical/local delivery state."""

        row = self.db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.id == delivery_id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.status != "processing":
            return row
        row.status = (
            "failed" if row.attempt_count >= row.max_attempts else "retry"
        )
        row.available_at = retry_at
        row.locked_by = None
        row.locked_until = None
        row.last_error = str(error or "TIM mirror failed")[:2000]
        self.db.flush()
        return row
