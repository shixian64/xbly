"""同步 SQLAlchemy repositories。

业务对象查询默认要求 ``owner_user_id``。只有登录预检、管理员审计等明确的
系统边界允许不带 owner 查询。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import (
    ActivityEvent,
    AdminSession,
    AdminUser,
    AuditLog,
    Conversation,
    ExternalAccount,
    InviteCode,
    MediaObject,
    Message,
    OperationOutbox,
    RawUpstreamResponse,
    Relationship,
    SyncCursor,
    SystemStorageQuota,
    User,
    WebSession,
    utcnow,
)


ModelT = TypeVar("ModelT")


class Repository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, db: Session):
        self.db = db

    def add(self, item: ModelT, *, flush: bool = True) -> ModelT:
        self.db.add(item)
        if flush:
            self.db.flush()
        return item


class UserRepository(Repository[User]):
    model = User

    def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def list(self, *, offset: int = 0, limit: int = 100, status: str | None = None) -> list[User]:
        stmt = select(User).order_by(User.created_at.desc()).offset(offset).limit(min(limit, 500))
        if status:
            stmt = stmt.where(User.status == status)
        return list(self.db.scalars(stmt))


class ExternalAccountRepository(Repository[ExternalAccount]):
    model = ExternalAccount

    def get_for_user(
        self, user_id: uuid.UUID, *, provider: str = "beibeiwu", for_update: bool = False
    ) -> ExternalAccount | None:
        stmt = select(ExternalAccount).where(
            ExternalAccount.user_id == user_id, ExternalAccount.provider == provider
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_phone_hmac(
        self, phone_hmac: str, *, provider: str = "beibeiwu", for_update: bool = False
    ) -> ExternalAccount | None:
        stmt = select(ExternalAccount).where(
            ExternalAccount.provider == provider, ExternalAccount.phone_hmac == phone_hmac
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_upstream_uid(
        self, upstream_uid: str, *, provider: str = "beibeiwu", for_update: bool = False
    ) -> ExternalAccount | None:
        stmt = select(ExternalAccount).where(
            ExternalAccount.provider == provider, ExternalAccount.upstream_uid == upstream_uid
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)


class InviteCodeRepository(Repository[InviteCode]):
    model = InviteCode

    def get_by_hash(self, code_hash: str, *, for_update: bool = False) -> InviteCode | None:
        stmt = select(InviteCode).where(InviteCode.code_hash == code_hash)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def list(self, *, offset: int = 0, limit: int = 100) -> list[InviteCode]:
        stmt = (
            select(InviteCode)
            .order_by(InviteCode.created_at.desc())
            .offset(offset)
            .limit(min(limit, 500))
        )
        return list(self.db.scalars(stmt))


class WebSessionRepository(Repository[WebSession]):
    model = WebSession

    def get_by_hash(self, sid_hash: str, *, for_update: bool = False) -> WebSession | None:
        stmt = select(WebSession).where(WebSession.sid_hash == sid_hash)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def active_for_user(self, user_id: uuid.UUID, *, at: datetime | None = None) -> list[WebSession]:
        now = at or utcnow()
        stmt = select(WebSession).where(
            WebSession.user_id == user_id,
            WebSession.revoked_at.is_(None),
            WebSession.idle_expires_at > now,
            WebSession.absolute_expires_at > now,
        )
        return list(self.db.scalars(stmt))


class AdminUserRepository(Repository[AdminUser]):
    model = AdminUser

    def get(self, admin_id: uuid.UUID, *, for_update: bool = False) -> AdminUser | None:
        stmt = select(AdminUser).where(AdminUser.id == admin_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_username(self, username: str, *, for_update: bool = False) -> AdminUser | None:
        stmt = select(AdminUser).where(AdminUser.username == username)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)


class AdminSessionRepository(Repository[AdminSession]):
    model = AdminSession

    def get_by_hash(self, sid_hash: str, *, for_update: bool = False) -> AdminSession | None:
        stmt = select(AdminSession).where(AdminSession.sid_hash == sid_hash)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)


class AuditLogRepository(Repository[AuditLog]):
    model = AuditLog

    def list_for_admin(
        self, admin_id: uuid.UUID, *, offset: int = 0, limit: int = 100
    ) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.admin_user_id == admin_id)
            .order_by(AuditLog.created_at.desc())
            .offset(offset)
            .limit(min(limit, 500))
        )
        return list(self.db.scalars(stmt))

    def list_for_target_user(
        self, target_user_id: uuid.UUID, *, offset: int = 0, limit: int = 100
    ) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.target_user_id == target_user_id)
            .order_by(AuditLog.created_at.desc())
            .offset(offset)
            .limit(min(limit, 500))
        )
        return list(self.db.scalars(stmt))

    def purge_expired(self, *, at: datetime | None = None) -> int:
        result = self.db.execute(delete(AuditLog).where(AuditLog.expires_at <= (at or utcnow())))
        return int(result.rowcount or 0)


class ConversationRepository(Repository[Conversation]):
    model = Conversation

    @staticmethod
    def _upsert_statement(values: dict[str, Any] | list[dict[str, Any]]):
        stmt = insert(Conversation).values(values)
        excluded = stmt.excluded
        unread_is_newer = and_(
            excluded.unread_observed_at.is_not(None),
            or_(
                Conversation.unread_observed_at.is_(None),
                excluded.unread_observed_at >= Conversation.unread_observed_at,
            ),
        )
        last_message_is_newer = or_(
            Conversation.last_message_at.is_(None),
            and_(
                excluded.last_message_at.is_not(None),
                excluded.last_message_at >= Conversation.last_message_at,
            ),
        )
        metadata_without_preview = (
            excluded.metadata.op("-")("last_message")
            .op("-")("preview_timestamp")
            .op("-")("preview_sequence")
            .op("-")("preview_authoritative")
            .op("-")("preview_timestamp_inferred")
            .op("-")("last_source")
        )
        return stmt.on_conflict_do_update(
            constraint="uq_conversations_owner_source",
            set_={
                "peer_upstream_uid": excluded.peer_upstream_uid,
                "kind": excluded.kind,
                "title": func.coalesce(excluded.title, Conversation.title),
                "unread_count": case(
                    (unread_is_newer, excluded.unread_count),
                    else_=Conversation.unread_count,
                ),
                "unread_observed_at": case(
                    (unread_is_newer, excluded.unread_observed_at),
                    else_=Conversation.unread_observed_at,
                ),
                "last_message_at": case(
                    (last_message_is_newer, excluded.last_message_at),
                    else_=Conversation.last_message_at,
                ),
                "metadata": case(
                    (
                        last_message_is_newer,
                        Conversation.extra_data.op("||")(excluded.metadata),
                    ),
                    else_=Conversation.extra_data.op("||")(metadata_without_preview),
                ),
                "updated_at": func.now(),
            },
        ).returning(Conversation)

    def get_by_peer(
        self,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
        *,
        provider: str = "tim",
        kind: str = "direct",
    ) -> Conversation | None:
        return self.db.scalar(
            select(Conversation)
            .where(
                Conversation.owner_user_id == owner_user_id,
                Conversation.provider == provider,
                Conversation.peer_upstream_uid == peer_upstream_uid,
                Conversation.kind == kind,
            )
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.updated_at.desc(),
            )
            .limit(1)
        )

    def exists_for_peer(
        self,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
        *,
        provider: str = "tim",
        kind: str = "direct",
    ) -> bool:
        return (
            self.db.scalar(
                select(Conversation.id)
                .where(
                    Conversation.owner_user_id == owner_user_id,
                    Conversation.provider == provider,
                    Conversation.peer_upstream_uid == peer_upstream_uid,
                    Conversation.kind == kind,
                )
                .limit(1)
            )
            is not None
        )

    def list_for_owner(
        self, owner_user_id: uuid.UUID, *, offset: int = 0, limit: int = 100
    ) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.owner_user_id == owner_user_id)
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.updated_at.desc(),
            )
            .offset(max(0, int(offset)))
            .limit(min(max(1, int(limit)), 500))
        )
        return list(self.db.scalars(stmt))

    def list_for_peers(
        self,
        owner_user_id: uuid.UUID,
        peer_upstream_uids: list[str],
    ) -> list[Conversation]:
        peers = list(dict.fromkeys(str(peer or "").strip() for peer in peer_upstream_uids if str(peer or "").strip()))
        if not peers:
            return []
        stmt = (
            select(Conversation)
            .where(
                Conversation.owner_user_id == owner_user_id,
                Conversation.provider == "tim",
                Conversation.kind == "direct",
                Conversation.peer_upstream_uid.in_(peers[:500]),
            )
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.updated_at.desc(),
            )
        )
        return list(self.db.scalars(stmt))

    def get(self, owner_user_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation | None:
        return self.db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.owner_user_id == owner_user_id
            )
        )

    def get_by_upstream(
        self, owner_user_id: uuid.UUID, provider: str, upstream_conversation_id: str
    ) -> Conversation | None:
        return self.db.scalar(
            select(Conversation).where(
                Conversation.owner_user_id == owner_user_id,
                Conversation.provider == provider,
                Conversation.upstream_conversation_id == upstream_conversation_id,
            )
        )

    def list_by_upstream_ids(
        self,
        owner_user_id: uuid.UUID,
        provider: str,
        upstream_conversation_ids: list[str],
    ) -> dict[str, Conversation]:
        ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in upstream_conversation_ids
                if str(item or "").strip()
            )
        )
        if not ids:
            return {}
        rows = self.db.scalars(
            select(Conversation).where(
                Conversation.owner_user_id == owner_user_id,
                Conversation.provider == provider,
                Conversation.upstream_conversation_id.in_(ids[:500]),
            )
        )
        return {row.upstream_conversation_id: row for row in rows}

    def upsert(self, **values: Any) -> Conversation:
        return self.db.scalars(self._upsert_statement(values)).one()

    def upsert_many(self, values: list[dict[str, Any]]) -> list[Conversation]:
        if not values:
            return []
        return list(self.db.scalars(self._upsert_statement(values)))

    def mark_peers_read(
        self,
        owner_user_id: uuid.UUID,
        peer_upstream_uids: list[str],
        *,
        observed_at: datetime | None = None,
    ) -> int:
        peers = list(dict.fromkeys(str(peer or "").strip() for peer in peer_upstream_uids if str(peer or "").strip()))
        if not peers:
            return 0
        result = self.db.execute(
            update(Conversation)
            .where(
                Conversation.owner_user_id == owner_user_id,
                Conversation.provider == "tim",
                Conversation.kind == "direct",
                Conversation.peer_upstream_uid.in_(peers[:100]),
            )
            .values(
                unread_count=0,
                unread_observed_at=observed_at or utcnow(),
                updated_at=func.now(),
            )
        )
        return int(result.rowcount or 0)


class MessageRepository(Repository[Message]):
    model = Message

    def get(self, owner_user_id: uuid.UUID, message_id: uuid.UUID) -> Message | None:
        return self.db.scalar(
            select(Message).where(Message.id == message_id, Message.owner_user_id == owner_user_id)
        )

    def get_by_upstream(
        self, owner_user_id: uuid.UUID, provider: str, upstream_message_id: str
    ) -> Message | None:
        return self.db.scalar(
            select(Message).where(
                Message.owner_user_id == owner_user_id,
                Message.provider == provider,
                Message.upstream_message_id == upstream_message_id,
            )
        )

    def insert_idempotent(self, **values: Any) -> tuple[Message, bool]:
        stmt = (
            insert(Message)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_messages_owner_source")
            .returning(Message)
        )
        created = self.db.scalars(stmt).first()
        if created is not None:
            return created, True
        existing = self.get_by_upstream(
            values["owner_user_id"], values.get("provider", "tim"), values["upstream_message_id"]
        )
        if existing is None:
            raise RuntimeError("message conflict occurred but existing row was not found")
        return existing, False

    def list_for_conversation(
        self,
        owner_user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        before: datetime | None = None,
        limit: int = 100,
    ) -> list[Message]:
        stmt = select(Message).where(
            Message.owner_user_id == owner_user_id, Message.conversation_id == conversation_id
        )
        if before:
            stmt = stmt.where(Message.occurred_at < before)
        stmt = stmt.order_by(Message.occurred_at.desc()).limit(min(limit, 500))
        return list(self.db.scalars(stmt))

    def latest_for_conversations(
        self,
        owner_user_id: uuid.UUID,
        conversation_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, Message]:
        ids = list(dict.fromkeys(conversation_ids))
        if not ids:
            return {}
        stmt = (
            select(Message)
            .where(
                Message.owner_user_id == owner_user_id,
                Message.conversation_id.in_(ids),
            )
            .distinct(Message.conversation_id)
            .order_by(Message.conversation_id, Message.occurred_at.desc(), Message.created_at.desc())
        )
        return {message.conversation_id: message for message in self.db.scalars(stmt)}


class MediaObjectRepository(Repository[MediaObject]):
    model = MediaObject

    def get(self, owner_user_id: uuid.UUID, media_id: uuid.UUID) -> MediaObject | None:
        return self.db.scalar(
            select(MediaObject).where(
                MediaObject.id == media_id, MediaObject.owner_user_id == owner_user_id
            )
        )

    def active_usage_for_user(self, owner_user_id: uuid.UUID) -> int:
        value = self.db.scalar(
            select(func.coalesce(func.sum(MediaObject.size_bytes), 0)).where(
                MediaObject.owner_user_id == owner_user_id,
                MediaObject.counts_toward_quota.is_(True),
                MediaObject.deleted_at.is_(None),
                MediaObject.status.in_(("pending", "uploading", "available")),
            )
        )
        return int(value or 0)

    def expired_batch(self, *, at: datetime | None = None, limit: int = 100) -> list[MediaObject]:
        stmt = (
            select(MediaObject)
            .where(MediaObject.retention_expires_at <= (at or utcnow()), MediaObject.deleted_at.is_(None))
            .order_by(MediaObject.retention_expires_at)
            .with_for_update(skip_locked=True)
            .limit(min(limit, 500))
        )
        return list(self.db.scalars(stmt))

    def get_system_quota_for_update(self) -> SystemStorageQuota:
        quota = self.db.scalar(
            select(SystemStorageQuota).where(SystemStorageQuota.id == 1).with_for_update()
        )
        if quota is None:
            quota = SystemStorageQuota(id=1)
            self.db.add(quota)
            self.db.flush()
        return quota


class RelationshipRepository(Repository[Relationship]):
    model = Relationship

    def upsert(self, **values: Any) -> Relationship:
        stmt = insert(Relationship).values(**values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            constraint="uq_relationships_owner_subject",
            set_={
                "status": excluded.status,
                "started_at": excluded.started_at,
                "ended_at": excluded.ended_at,
                "metadata": excluded.metadata,
                "updated_at": func.now(),
            },
        ).returning(Relationship)
        return self.db.scalars(stmt).one()


class ActivityEventRepository(Repository[ActivityEvent]):
    model = ActivityEvent

    def insert_idempotent(self, **values: Any) -> ActivityEvent:
        if values.get("upstream_event_id") is None:
            return self.add(ActivityEvent(**values))
        stmt = (
            insert(ActivityEvent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_activity_events_owner_source")
            .returning(ActivityEvent)
        )
        created = self.db.scalars(stmt).first()
        if created:
            return created
        existing = self.db.scalar(
            select(ActivityEvent).where(
                ActivityEvent.owner_user_id == values["owner_user_id"],
                ActivityEvent.provider == values.get("provider", "beibeiwu"),
                ActivityEvent.upstream_event_id == values["upstream_event_id"],
            )
        )
        if existing is None:
            raise RuntimeError("activity event conflict occurred but row was not found")
        return existing


class RawUpstreamResponseRepository(Repository[RawUpstreamResponse]):
    model = RawUpstreamResponse

    def purge_expired(self, *, at: datetime | None = None) -> int:
        result = self.db.execute(
            delete(RawUpstreamResponse).where(RawUpstreamResponse.expires_at <= (at or utcnow()))
        )
        return int(result.rowcount or 0)


class SyncCursorRepository(Repository[SyncCursor]):
    model = SyncCursor

    def get(
        self, owner_user_id: uuid.UUID, source: str, stream: str, *, for_update: bool = False
    ) -> SyncCursor | None:
        stmt = select(SyncCursor).where(
            SyncCursor.owner_user_id == owner_user_id,
            SyncCursor.source == source,
            SyncCursor.stream == stream,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def upsert(self, **values: Any) -> SyncCursor:
        stmt = insert(SyncCursor).values(**values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            constraint="uq_sync_cursors_owner_stream",
            set_={
                "cursor": excluded.cursor,
                "watermark_at": excluded.watermark_at,
                "next_sync_at": excluded.next_sync_at,
                "last_attempted_at": excluded.last_attempted_at,
                "last_succeeded_at": excluded.last_succeeded_at,
                "last_error": excluded.last_error,
                "version": SyncCursor.version + 1,
                "updated_at": func.now(),
            },
        ).returning(SyncCursor)
        return self.db.scalars(stmt).one()


class OperationOutboxRepository(Repository[OperationOutbox]):
    model = OperationOutbox

    def enqueue(self, **values: Any) -> tuple[OperationOutbox, bool]:
        stmt = (
            insert(OperationOutbox)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_outbox_idempotency")
            .returning(OperationOutbox)
        )
        created = self.db.scalars(stmt).first()
        if created:
            return created, True
        existing = self.db.scalar(
            select(OperationOutbox).where(
                OperationOutbox.owner_user_id == values["owner_user_id"],
                OperationOutbox.operation_type == values["operation_type"],
                OperationOutbox.idempotency_key == values["idempotency_key"],
            )
        )
        if existing is None:
            raise RuntimeError("outbox conflict occurred but row was not found")
        return existing, False

    def claim(self, *, worker_id: str, limit: int = 20, lock_until: datetime) -> list[OperationOutbox]:
        now = utcnow()
        stmt = (
            select(OperationOutbox)
            .where(
                OperationOutbox.status.in_(("pending", "retry")),
                OperationOutbox.available_at <= now,
                or_(OperationOutbox.locked_until.is_(None), OperationOutbox.locked_until <= now),
                OperationOutbox.attempt_count < OperationOutbox.max_attempts,
            )
            .order_by(OperationOutbox.available_at, OperationOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(min(limit, 100))
        )
        rows = list(self.db.scalars(stmt))
        for row in rows:
            row.status = "processing"
            row.locked_by = worker_id
            row.locked_until = lock_until
            row.attempt_count += 1
        self.db.flush()
        return rows
