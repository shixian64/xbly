"""Web-native 私聊富媒体的 SQLAlchemy 持久化与会话授权适配器。

调用方持有事务。附件发送在同一事务内写 canonical ChatMessage、双方 Message
投影、本地投递回执和 pending TIM outbox；任何一步失败都不得单独提交。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select
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
    MediaAsset as MediaAssetRow,
    MediaAssetReference as MediaAssetReferenceRow,
    MediaAttachment as MediaAttachmentRow,
    MediaFlashClaim as MediaFlashClaimRow,
    MediaUploadIntent as MediaUploadIntentRow,
    Message,
    MessageDelivery,
    MessageReceipt,
    Relationship,
    SystemStorageQuota,
    User,
    UserCredential,
)
from bbw_web.private_message_policy import private_message_permission_query

from .contracts import (
    ASSET_STATUS_DELETED,
    ASSET_STATUS_AVAILABLE,
    ATTACHMENT_STATUS_REVOKED,
    ATTACHMENT_STATUS_SENT,
    LEGACY_ACCOUNT_PROVIDER,
    MEDIA_KIND_AUDIO,
    MEDIA_KIND_FILE,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    MEDIA_NATIVE_PROVIDER,
    STORAGE_ZONE_PRIVATE,
    STORAGE_ZONE_STAGING,
    UPLOAD_STATUS_COMPLETED,
    UPLOAD_STATUS_CANCELLED,
    UPLOAD_STATUS_PENDING,
    AttachmentDeliveryResult,
    DirectMediaThread,
    FinalizedMediaObject,
    FlashAlreadyClaimed,
    FlashClaim,
    FlashClaimDenied,
    MediaAccount,
    MediaAsset,
    MediaAssetUnavailable,
    MediaAttachment,
    MediaAttachmentPayload,
    MediaBlocked,
    MediaContractViolation,
    MediaIdempotencyConflict,
    MediaPrincipal,
    MediaQuotaExceeded,
    MediaRevocationDenied,
    MediaRevocationExpired,
    MediaRevokeResult,
    MediaThreadForbidden,
    StagedMediaInspection,
    StorageObjectRef,
    TimMediaMirrorIntent,
    TimMediaRevokeIntent,
    UploadAlreadyCompleted,
    UploadIntent,
    UploadIntentExpired,
    UploadIntentUnavailable,
)


MEDIA_MESSAGE_SCHEMA = 1
LOCAL_DELIVERY_CHANNEL = "local"
TIM_MIRROR_CHANNEL = "tim"
BLOCK_KINDS = ("blacklist", "blacklisted_by")
BLOCK_PROVIDERS = (LEGACY_ACCOUNT_PROVIDER, MEDIA_NATIVE_PROVIDER)


def effective_block_between_query(
    left_user_id: uuid.UUID,
    left_uid: str,
    right_user_id: uuid.UUID,
    right_uid: str,
):
    """Apply the same local-first block/tombstone rule as text messaging."""

    left_override = aliased(Relationship, name="media_left_local_block_override")
    right_override = aliased(Relationship, name="media_right_local_block_override")
    left_has_local = (
        select(left_override.id)
        .where(
            left_override.owner_user_id == left_user_id,
            left_override.provider == MEDIA_NATIVE_PROVIDER,
            left_override.subject_upstream_uid == right_uid,
            left_override.kind == "blacklist",
        )
        .exists()
    )
    right_has_local = (
        select(right_override.id)
        .where(
            right_override.owner_user_id == right_user_id,
            right_override.provider == MEDIA_NATIVE_PROVIDER,
            right_override.subject_upstream_uid == left_uid,
            right_override.kind == "blacklist",
        )
        .exists()
    )
    left_to_right = and_(
        Relationship.owner_user_id == left_user_id,
        Relationship.subject_upstream_uid == right_uid,
    )
    right_to_left = and_(
        Relationship.owner_user_id == right_user_id,
        Relationship.subject_upstream_uid == left_uid,
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
                    Relationship.provider == MEDIA_NATIVE_PROVIDER,
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

_OBJECT_NAMES = {
    MEDIA_KIND_IMAGE: "TIMImageElem",
    MEDIA_KIND_AUDIO: "TIMSoundElem",
    MEDIA_KIND_VIDEO: "TIMVideoFileElem",
    MEDIA_KIND_FILE: "TIMFileElem",
}
_PREVIEW_TEXT = {
    MEDIA_KIND_IMAGE: "[图片]",
    MEDIA_KIND_AUDIO: "[语音]",
    MEDIA_KIND_VIDEO: "[视频]",
    MEDIA_KIND_FILE: "[文件]",
}


def direct_thread_key(left_user_id: uuid.UUID, right_user_id: uuid.UUID) -> str:
    left, right = sorted((str(left_user_id), str(right_user_id)))
    return f"direct:{left}:{right}"


def _payload_digest(
    *,
    thread_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    client_message_id: str,
    payload: MediaAttachmentPayload,
) -> str:
    encoded = json.dumps(
        {
            "client_message_id": client_message_id,
            "payload": payload.as_persistent_dict(),
            "recipient_user_id": str(recipient_user_id),
            "schema": MEDIA_MESSAGE_SCHEMA,
            "thread_id": str(thread_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _media_report(
    attachment_id: uuid.UUID,
    payload: MediaAttachmentPayload,
) -> dict[str, object]:
    return {
        "attachment_id": str(attachment_id),
        "asset_id": str(payload.asset_id),
        "name": payload.filename,
        "mime": payload.content_type,
        "size": payload.size_bytes,
        "duration": payload.duration_seconds,
        "width": payload.width,
        "height": payload.height,
        "flash": payload.flash,
    }


def _as_account(user: User, account: ExternalAccount) -> MediaAccount:
    return MediaAccount(
        user_id=user.id,
        external_account_id=account.id,
        upstream_uid=str(account.upstream_uid or ""),
        account_provider=account.provider,
    )


def _as_upload_intent(row: MediaUploadIntentRow) -> UploadIntent:
    return UploadIntent(
        id=row.id,
        owner_user_id=row.owner_user_id,
        deployment=row.deployment,
        kind=row.kind,
        filename=row.filename,
        declared_content_type=row.declared_content_type,
        expected_size_bytes=int(row.expected_size_bytes),
        expected_sha256=row.expected_sha256,
        staging_object=StorageObjectRef(
            deployment=row.deployment,
            zone=STORAGE_ZONE_STAGING,
            bucket=row.staging_bucket,
            namespace=row.staging_namespace,
            key=row.staging_object_key,
            owner_user_id=row.owner_user_id,
        ),
        created_at=row.created_at,
        expires_at=row.expires_at,
        status=row.status,
        completed_asset_id=row.completed_asset_id,
    )


def _as_asset(row: MediaAssetRow) -> MediaAsset:
    return MediaAsset(
        id=row.id,
        owner_user_id=row.owner_user_id,
        deployment=row.deployment,
        kind=row.kind,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=int(row.size_bytes),
        sha256=row.sha256,
        object_ref=StorageObjectRef(
            deployment=row.deployment,
            zone=STORAGE_ZONE_PRIVATE,
            bucket=row.private_bucket,
            namespace=row.private_namespace,
            key=row.private_object_key,
            owner_user_id=row.owner_user_id,
        ),
        created_at=row.created_at,
        status=row.status,
        duration_seconds=row.duration_seconds,
        width=row.width,
        height=row.height,
        expires_at=row.expires_at,
    )


def _attachment_payload(row: MediaAttachmentRow) -> MediaAttachmentPayload:
    payload = dict(row.payload or {})
    return MediaAttachmentPayload(
        asset_id=uuid.UUID(str(payload.get("asset_id") or row.asset_id)),
        kind=str(payload.get("kind") or ""),
        filename=str(payload.get("filename") or ""),
        content_type=str(payload.get("content_type") or ""),
        size_bytes=int(payload.get("size_bytes") or 0),
        sha256=str(payload.get("sha256") or ""),
        duration_seconds=payload.get("duration_seconds"),
        width=payload.get("width"),
        height=payload.get("height"),
        flash=bool(payload.get("flash")),
        flash_display_seconds=payload.get("flash_display_seconds"),
    )


def _as_attachment(row: MediaAttachmentRow) -> MediaAttachment:
    return MediaAttachment(
        id=row.id,
        message_id=row.message_id,
        thread_id=row.thread_id,
        client_message_id=row.client_message_id,
        sender_user_id=row.sender_user_id,
        recipient_user_id=row.recipient_user_id,
        sender_upstream_uid=row.sender_upstream_uid,
        recipient_upstream_uid=row.recipient_upstream_uid,
        payload=_attachment_payload(row),
        sent_at=row.sent_at,
        status=row.status,
        revoked_at=row.revoked_at,
    )


def _ensure_thread_and_members(
    db: Session,
    sender: MediaAccount,
    recipient: MediaAccount,
) -> ChatThread:
    key = direct_thread_key(sender.user_id, recipient.user_id)
    statement = insert(ChatThread).values(
        kind="direct",
        direct_key=key,
        created_by_user_id=sender.user_id,
        status="active",
        extra_data={"authority": MEDIA_NATIVE_PROVIDER, "schema": MEDIA_MESSAGE_SCHEMA},
    )
    thread = db.scalars(
        statement.on_conflict_do_update(
            constraint="uq_chat_threads_direct_key",
            set_={"direct_key": statement.excluded.direct_key},
        ).returning(ChatThread)
    ).one()
    for account in sorted((sender, recipient), key=lambda item: str(item.user_id)):
        member_statement = insert(ChatMember).values(
            thread_id=thread.id,
            user_id=account.user_id,
            role="member",
            left_at=None,
            extra_data={"upstream_uid": account.upstream_uid},
        )
        db.execute(
            member_statement.on_conflict_do_update(
                constraint="uq_chat_members_thread_user",
                set_={"left_at": None, "role": "member"},
            )
        )
    db.flush()
    return thread


class SqlAlchemyMediaConversationPolicy:
    def __init__(self, db: Session) -> None:
        self.db = db

    def resolve_principal(self, principal: MediaPrincipal) -> MediaAccount | None:
        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                User.id == principal.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                UserCredential.disabled_at.is_(None),
                ExternalAccount.id == principal.external_account_id,
                ExternalAccount.provider == principal.account_provider,
                ExternalAccount.upstream_uid == principal.upstream_uid,
            )
        ).one_or_none()
        return _as_account(row[0], row[1]) if row is not None else None

    def resolve_active_peer(
        self,
        upstream_uid: str,
        *,
        provider: str,
    ) -> MediaAccount | None:
        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                User.status == "active",
                User.disabled_at.is_(None),
                UserCredential.disabled_at.is_(None),
                ExternalAccount.provider == provider,
                ExternalAccount.upstream_uid == upstream_uid,
            )
        ).one_or_none()
        return _as_account(row[0], row[1]) if row is not None else None

    def can_send_private_message(
        self,
        sender: MediaAccount,
        recipient: MediaAccount,
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

    def resolve_or_create_direct_thread(
        self,
        sender: MediaAccount,
        recipient: MediaAccount,
    ) -> DirectMediaThread | None:
        thread = _ensure_thread_and_members(self.db, sender, recipient)
        return DirectMediaThread(
            id=thread.id,
            member_user_ids=frozenset({sender.user_id, recipient.user_id}),
            kind=thread.kind,
            status=thread.status,
        )

    def resolve_thread(self, thread_id: uuid.UUID) -> DirectMediaThread | None:
        thread = self.db.scalar(select(ChatThread).where(ChatThread.id == thread_id))
        if thread is None:
            return None
        members = frozenset(
            self.db.scalars(
                select(ChatMember.user_id).where(
                    ChatMember.thread_id == thread.id,
                    ChatMember.left_at.is_(None),
                )
            )
        )
        return DirectMediaThread(
            id=thread.id,
            member_user_ids=members,
            kind=thread.kind,
            status=thread.status,
        )

    def is_blocked_between(
        self,
        left_user_id: uuid.UUID,
        right_user_id: uuid.UUID,
    ) -> bool:
        accounts = {
            user_id: upstream_uid
            for user_id, upstream_uid in self.db.execute(
                select(ExternalAccount.user_id, ExternalAccount.upstream_uid).where(
                    ExternalAccount.provider == LEGACY_ACCOUNT_PROVIDER,
                    ExternalAccount.user_id.in_((left_user_id, right_user_id)),
                )
            )
        }
        left_uid = str(accounts.get(left_user_id) or "")
        right_uid = str(accounts.get(right_user_id) or "")
        if not left_uid or not right_uid:
            return True
        blocked = self.db.scalar(
            effective_block_between_query(
                left_user_id,
                left_uid,
                right_user_id,
                right_uid,
            )
        )
        return blocked is not None


class SqlAlchemyMediaNativeRepository:
    def __init__(
        self,
        db: Session,
        *,
        system_quota_bytes: int | None = None,
        compatibility_mode: str | None = None,
    ) -> None:
        self.db = db
        self.compatibility_mode = (
            normalize_compatibility_mode(compatibility_mode)
            if compatibility_mode is not None
            else resolve_compatibility_mode()
        )
        self.system_quota_bytes = (
            max(1, int(system_quota_bytes))
            if system_quota_bytes is not None
            else None
        )

    def _quota_state_for_update(
        self,
        owner_user_id: uuid.UUID,
        *,
        at: datetime,
    ) -> tuple[User, SystemStorageQuota, int, int, int, int]:
        """Lock the shared quota ledger before the owner row.

        Pending, unexpired upload intents are durable reservations.  Counting
        them here prevents several concurrent direct-to-R2 uploads from each
        passing the same remaining-quota check before any asset is finalized.
        """

        system = self.db.scalar(
            select(SystemStorageQuota)
            .where(SystemStorageQuota.id == 1)
            .with_for_update()
        )
        if system is None:
            system = SystemStorageQuota(id=1)
            self.db.add(system)
            self.db.flush()
        user = self.db.scalar(
            select(User)
            .where(
                User.id == owner_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
            .with_for_update()
        )
        if user is None:
            raise MediaAssetUnavailable("当前账号无法使用媒体存储")

        user_pending = int(
            self.db.scalar(
                select(
                    func.coalesce(func.sum(MediaUploadIntentRow.expected_size_bytes), 0)
                ).where(
                    MediaUploadIntentRow.owner_user_id == owner_user_id,
                    MediaUploadIntentRow.status == UPLOAD_STATUS_PENDING,
                    MediaUploadIntentRow.expires_at > at,
                )
            )
            or 0
        )
        system_pending = int(
            self.db.scalar(
                select(
                    func.coalesce(func.sum(MediaUploadIntentRow.expected_size_bytes), 0)
                ).where(
                    MediaUploadIntentRow.status == UPLOAD_STATUS_PENDING,
                    MediaUploadIntentRow.expires_at > at,
                )
            )
            or 0
        )
        user_limit = int(user.media_quota_bytes)
        system_limit = int(system.quota_bytes)
        if self.system_quota_bytes is not None:
            system_limit = min(system_limit, self.system_quota_bytes)
        return (
            user,
            system,
            user_pending,
            system_pending,
            user_limit,
            system_limit,
        )

    @staticmethod
    def _require_quota_capacity(
        *,
        user: User,
        system: SystemStorageQuota,
        user_pending: int,
        system_pending: int,
        user_limit: int,
        system_limit: int,
        additional_bytes: int = 0,
    ) -> None:
        if int(user.media_used_bytes) + user_pending + additional_bytes > user_limit:
            raise MediaQuotaExceeded("用户媒体存储额度不足")
        if int(system.used_bytes) + system_pending + additional_bytes > system_limit:
            raise MediaQuotaExceeded("系统媒体存储额度不足")

    def create_upload_intent(self, intent: UploadIntent) -> UploadIntent:
        (
            user,
            system,
            user_pending,
            system_pending,
            user_limit,
            system_limit,
        ) = self._quota_state_for_update(
            intent.owner_user_id,
            at=intent.created_at,
        )
        existing = self.db.get(MediaUploadIntentRow, intent.id)
        if existing is not None:
            mapped = _as_upload_intent(existing)
            if mapped != intent:
                raise MediaIdempotencyConflict("upload intent id 已绑定其他参数")
            return mapped
        self._require_quota_capacity(
            user=user,
            system=system,
            user_pending=user_pending,
            system_pending=system_pending,
            user_limit=user_limit,
            system_limit=system_limit,
            additional_bytes=intent.expected_size_bytes,
        )
        row = MediaUploadIntentRow(
            id=intent.id,
            owner_user_id=intent.owner_user_id,
            deployment=intent.deployment,
            kind=intent.kind,
            filename=intent.filename,
            declared_content_type=intent.declared_content_type,
            expected_size_bytes=intent.expected_size_bytes,
            expected_sha256=intent.expected_sha256,
            staging_bucket=intent.staging_object.bucket,
            staging_namespace=intent.staging_object.namespace,
            staging_object_key=intent.staging_object.key,
            status=intent.status,
            expires_at=intent.expires_at,
            completed_asset_id=intent.completed_asset_id,
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )
        self.db.add(row)
        self.db.flush()
        return _as_upload_intent(row)

    def get_upload_intent(self, intent_id: uuid.UUID) -> UploadIntent | None:
        row = self.db.get(MediaUploadIntentRow, intent_id)
        return _as_upload_intent(row) if row is not None else None

    def complete_upload(
        self,
        *,
        intent: UploadIntent,
        inspection: StagedMediaInspection,
        finalized: FinalizedMediaObject,
        completed_at: datetime,
    ) -> MediaAsset:
        (
            user,
            system,
            user_pending,
            system_pending,
            user_limit,
            system_limit,
        ) = self._quota_state_for_update(
            intent.owner_user_id,
            at=completed_at,
        )
        row = self.db.scalar(
            select(MediaUploadIntentRow)
            .where(
                MediaUploadIntentRow.id == intent.id,
                MediaUploadIntentRow.owner_user_id == intent.owner_user_id,
            )
            .with_for_update()
        )
        if row is None:
            raise UploadIntentUnavailable("上传意图不存在")
        if row.status == UPLOAD_STATUS_COMPLETED:
            raise UploadAlreadyCompleted("上传意图已经完成")
        if row.status != UPLOAD_STATUS_PENDING:
            raise UploadIntentUnavailable("上传意图状态不可用")
        if completed_at >= row.expires_at:
            row.status = "expired"
            raise UploadIntentExpired("上传意图已过期")
        if _as_upload_intent(row) != intent:
            raise MediaIdempotencyConflict("上传意图在完成前发生变化")
        self._require_quota_capacity(
            user=user,
            system=system,
            user_pending=user_pending,
            system_pending=system_pending,
            user_limit=user_limit,
            system_limit=system_limit,
        )

        private = finalized.object_ref
        asset_row = MediaAssetRow(
            upload_intent_id=row.id,
            owner_user_id=row.owner_user_id,
            deployment=row.deployment,
            kind=intent.kind,
            filename=intent.filename,
            content_type=finalized.content_type,
            size_bytes=finalized.size_bytes,
            sha256=finalized.sha256,
            private_bucket=private.bucket,
            private_namespace=private.namespace,
            private_object_key=private.key,
            duration_seconds=finalized.duration_seconds,
            width=finalized.width,
            height=finalized.height,
            status=ASSET_STATUS_AVAILABLE,
            created_at=completed_at,
            updated_at=completed_at,
        )
        self.db.add(asset_row)
        self.db.flush()
        user.media_used_bytes += finalized.size_bytes
        system.used_bytes += finalized.size_bytes
        system.version += 1
        row.status = UPLOAD_STATUS_COMPLETED
        row.completed_asset_id = asset_row.id
        self.db.flush()
        return _as_asset(asset_row)

    def get_media_asset(self, asset_id: uuid.UUID) -> MediaAsset | None:
        row = self.db.get(MediaAssetRow, asset_id)
        return _as_asset(row) if row is not None else None

    def claim_expired_upload_intents(
        self,
        *,
        at: datetime,
        cleanup_grace: timedelta = timedelta(hours=1),
        limit: int = 200,
    ) -> list[UploadIntent]:
        """Fence expired intents, then return only mature cancelled cleanup rows.

        A completion request may have read ``pending`` immediately before expiry
        and still be inspecting or promoting its object.  The first cleanup pass
        therefore commits only the ``cancelled`` fence.  A later pass, after the
        bounded grace period, may delete storage and the intent row without
        allowing a late promotion to create an undiscoverable orphan.
        """

        rows = list(
            self.db.scalars(
                select(MediaUploadIntentRow)
                .where(
                    or_(
                        and_(
                            MediaUploadIntentRow.status.in_(
                                (UPLOAD_STATUS_PENDING, "expired")
                            ),
                            MediaUploadIntentRow.expires_at <= at,
                        ),
                        and_(
                            MediaUploadIntentRow.status == UPLOAD_STATUS_CANCELLED,
                            MediaUploadIntentRow.updated_at <= at - cleanup_grace,
                        ),
                    ),
                    MediaUploadIntentRow.completed_asset_id.is_(None),
                )
                .order_by(
                    MediaUploadIntentRow.expires_at,
                    MediaUploadIntentRow.id,
                )
                .limit(max(1, min(int(limit), 1000)))
                .with_for_update(skip_locked=True)
            )
        )
        cleanup_rows: list[MediaUploadIntentRow] = []
        for row in rows:
            if row.status == UPLOAD_STATUS_CANCELLED:
                cleanup_rows.append(row)
            else:
                row.status = UPLOAD_STATUS_CANCELLED
                row.updated_at = at
        self.db.flush()
        return [_as_upload_intent(row) for row in cleanup_rows]

    def delete_cancelled_upload_intent(self, intent_id: uuid.UUID) -> bool:
        row = self.db.scalar(
            select(MediaUploadIntentRow)
            .where(MediaUploadIntentRow.id == intent_id)
            .with_for_update()
        )
        if (
            row is None
            or row.status != UPLOAD_STATUS_CANCELLED
            or row.completed_asset_id is not None
        ):
            return False
        self.db.delete(row)
        self.db.flush()
        return True

    @staticmethod
    def _asset_cleanup_eligibility(*, at: datetime, unsent_grace: timedelta):
        return or_(
            and_(
                MediaAssetRow.expires_at.is_(None),
                MediaAssetRow.created_at <= at - unsent_grace,
            ),
            and_(
                MediaAssetRow.expires_at.is_not(None),
                MediaAssetRow.expires_at <= at,
            ),
        )

    def asset_cleanup_candidates(
        self,
        *,
        at: datetime,
        unsent_grace: timedelta,
        limit: int = 200,
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        attachment_exists = (
            select(MediaAttachmentRow.id)
            .where(MediaAttachmentRow.asset_id == MediaAssetRow.id)
            .exists()
        )
        current_reference_exists = (
            select(MediaAssetReferenceRow.id)
            .where(MediaAssetReferenceRow.asset_id == MediaAssetRow.id)
            .exists()
        )
        rows = self.db.execute(
            select(MediaAssetRow.id, MediaAssetRow.owner_user_id)
            .where(
                MediaAssetRow.status == ASSET_STATUS_AVAILABLE,
                self._asset_cleanup_eligibility(
                    at=at,
                    unsent_grace=unsent_grace,
                ),
                ~attachment_exists,
                ~current_reference_exists,
            )
            .order_by(MediaAssetRow.created_at, MediaAssetRow.id)
            .limit(max(1, min(int(limit), 1000)))
        )
        return [(row[0], row[1]) for row in rows]

    def delete_asset_and_release_quota(
        self,
        *,
        asset_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        at: datetime,
        unsent_grace: timedelta,
        delete_object: Callable[[MediaAsset], None],
    ) -> bool:
        """Delete R2 first, then atomically release both quota ledgers once.

        The system ledger, owner ledger and asset are locked in the same order
        as upload completion.  Storage errors occur before any counter mutation;
        commit errors leave the object deletion safely retryable because R2
        DeleteObject is idempotent and the row remains ``available``.
        """

        system = self.db.scalar(
            select(SystemStorageQuota)
            .where(SystemStorageQuota.id == 1)
            .with_for_update()
        )
        user = self.db.scalar(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
        )
        if system is None or user is None:
            raise MediaAssetUnavailable("媒体配额账本不存在")
        attachment_exists = (
            select(MediaAttachmentRow.id)
            .where(MediaAttachmentRow.asset_id == MediaAssetRow.id)
            .exists()
        )
        current_reference_exists = (
            select(MediaAssetReferenceRow.id)
            .where(MediaAssetReferenceRow.asset_id == MediaAssetRow.id)
            .exists()
        )
        row = self.db.scalar(
            select(MediaAssetRow)
            .where(
                MediaAssetRow.id == asset_id,
                MediaAssetRow.owner_user_id == owner_user_id,
                MediaAssetRow.status == ASSET_STATUS_AVAILABLE,
                self._asset_cleanup_eligibility(
                    at=at,
                    unsent_grace=unsent_grace,
                ),
                ~attachment_exists,
                ~current_reference_exists,
            )
            .with_for_update()
        )
        if row is None:
            return False
        reference_after_asset_lock = self.db.scalar(
            select(MediaAttachmentRow.id)
            .where(MediaAttachmentRow.asset_id == asset_id)
            .union_all(
                select(MediaAssetReferenceRow.id).where(
                    MediaAssetReferenceRow.asset_id == asset_id
                )
            )
            .limit(1)
        )
        if reference_after_asset_lock is not None:
            return False
        size_bytes = int(row.size_bytes)
        if int(user.media_used_bytes) < size_bytes or int(system.used_bytes) < size_bytes:
            raise MediaContractViolation("媒体配额账本小于待清理 asset")
        asset = _as_asset(row)
        delete_object(asset)
        row.status = ASSET_STATUS_DELETED
        row.updated_at = at
        user.media_used_bytes = int(user.media_used_bytes) - size_bytes
        system.used_bytes = int(system.used_bytes) - size_bytes
        system.version = int(system.version) + 1
        self.db.flush()
        return True

    def _require_authorized_thread(
        self,
        thread: DirectMediaThread,
        sender: MediaAccount,
        recipient: MediaAccount,
    ) -> ChatThread:
        row = self.db.scalar(
            select(ChatThread)
            .where(
                ChatThread.id == thread.id,
                ChatThread.kind == "direct",
                ChatThread.status == "active",
                ChatThread.direct_key
                == direct_thread_key(sender.user_id, recipient.user_id),
            )
            .with_for_update()
        )
        if row is None:
            raise MediaThreadForbidden("私聊线程不存在或状态无效")
        members = frozenset(
            self.db.scalars(
                select(ChatMember.user_id).where(
                    ChatMember.thread_id == row.id,
                    ChatMember.left_at.is_(None),
                )
            )
        )
        if members != frozenset({sender.user_id, recipient.user_id}):
            raise MediaThreadForbidden("私聊线程成员不完整")
        policy = SqlAlchemyMediaConversationPolicy(self.db)
        if policy.is_blocked_between(sender.user_id, recipient.user_id):
            raise MediaBlocked("任一方黑名单关系禁止媒体操作")
        if not policy.can_send_private_message(sender, recipient):
            raise MediaThreadForbidden("当前账号无权向该用户发送私聊媒体")
        return row

    def _user_details(
        self,
        sender: MediaAccount,
        recipient: MediaAccount,
    ) -> dict[uuid.UUID, tuple[str, int]]:
        rows = self.db.execute(
            select(User.id, User.display_name, User.chat_retention_days).where(
                User.id.in_((sender.user_id, recipient.user_id)),
                User.status == "active",
                User.disabled_at.is_(None),
            )
        ).all()
        details = {
            row.id: (str(row.display_name or ""), max(1, int(row.chat_retention_days or 180)))
            for row in rows
        }
        if set(details) != {sender.user_id, recipient.user_id}:
            raise MediaThreadForbidden("私聊账号状态不可用")
        return details

    def _ensure_conversations(
        self,
        thread: ChatThread,
        sender: MediaAccount,
        recipient: MediaAccount,
        details: dict[uuid.UUID, tuple[str, int]],
    ) -> dict[uuid.UUID, Conversation]:
        conversations: dict[uuid.UUID, Conversation] = {}
        for owner, peer in ((sender, recipient), (recipient, sender)):
            statement = insert(Conversation).values(
                owner_user_id=owner.user_id,
                provider=MEDIA_NATIVE_PROVIDER,
                upstream_conversation_id=f"{MEDIA_NATIVE_PROVIDER}:{thread.id}",
                peer_upstream_uid=peer.upstream_uid,
                kind="direct",
                title=details[peer.user_id][0] or None,
                unread_count=0,
                extra_data={
                    "authority": MEDIA_NATIVE_PROVIDER,
                    "canonical_thread_id": str(thread.id),
                    "peer_user_id": str(peer.user_id),
                    "schema": MEDIA_MESSAGE_SCHEMA,
                },
            )
            conversation = self.db.scalars(
                statement.on_conflict_do_update(
                    constraint="uq_conversations_owner_source",
                    set_={
                        "peer_upstream_uid": statement.excluded.peer_upstream_uid,
                        "title": statement.excluded.title,
                        "metadata": Conversation.extra_data.op("||")(
                            statement.excluded.metadata
                        ),
                    },
                ).returning(Conversation)
            ).one()
            conversations[owner.user_id] = conversation
        return conversations

    def _insert_projection(
        self,
        *,
        owner: MediaAccount,
        conversation: Conversation,
        canonical: ChatMessage,
        attachment_id: uuid.UUID,
        sender: MediaAccount,
        recipient: MediaAccount,
        payload: MediaAttachmentPayload,
        direction: str,
        retention_days: int,
    ) -> tuple[Message, bool]:
        message_type = "flash" if payload.flash else payload.kind
        metadata: dict[str, Any] = {
            "authority": MEDIA_NATIVE_PROVIDER,
            "canonical_message_id": str(canonical.id),
            "canonical_thread_id": str(canonical.thread_id),
            "client_message_id": canonical.client_message_id,
            "client_message_key": canonical.client_message_id,
            "is_peer_read": False,
            "message_key": str(canonical.id),
            "object_name": _OBJECT_NAMES[payload.kind],
            "schema": MEDIA_MESSAGE_SCHEMA,
            "source": MEDIA_NATIVE_PROVIDER,
            "media_report": _media_report(attachment_id, payload),
        }
        if payload.flash:
            metadata["flash_id"] = str(attachment_id)
        statement = (
            insert(Message)
            .values(
                owner_user_id=owner.user_id,
                conversation_id=conversation.id,
                provider=MEDIA_NATIVE_PROVIDER,
                upstream_message_id=str(canonical.id),
                direction=direction,
                sender_upstream_uid=sender.upstream_uid,
                recipient_upstream_uid=recipient.upstream_uid,
                message_type=message_type,
                body="[闪图]" if payload.flash else _PREVIEW_TEXT[payload.kind],
                status="sent" if direction == "outgoing" else "received",
                occurred_at=canonical.occurred_at,
                retention_expires_at=canonical.occurred_at
                + timedelta(days=retention_days),
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
                Message.provider == MEDIA_NATIVE_PROVIDER,
                Message.upstream_message_id == str(canonical.id),
            )
        )
        if (
            existing is None
            or existing.conversation_id != conversation.id
            or existing.direction != direction
            or str((existing.extra_data or {}).get("canonical_message_id") or "")
            != str(canonical.id)
            or dict((existing.extra_data or {}).get("media_report") or {})
            != _media_report(attachment_id, payload)
        ):
            raise MediaIdempotencyConflict("媒体消息投影与幂等键冲突")
        return existing, False

    def _ensure_delivery(
        self,
        *,
        canonical: ChatMessage,
        attachment: MediaAttachmentRow,
        sender: MediaAccount,
        recipient: MediaAccount,
        payload: MediaAttachmentPayload,
        channel: str,
        projection_ids: tuple[uuid.UUID, uuid.UUID],
    ) -> MessageDelivery:
        is_local = channel == LOCAL_DELIVERY_CHANNEL
        mirror_retired = (
            not is_local and self.compatibility_mode is CompatibilityMode.RETIRED
        )
        target_key = (
            f"media-local:{recipient.user_id}"
            if is_local
            else f"media-send:{recipient.upstream_uid}"
        )
        delivery_payload: dict[str, Any] = {
            "operation": "send",
            "canonical_message_id": str(canonical.id),
            "client_message_id": canonical.client_message_id,
            "attachment_id": str(attachment.id),
            "asset_id": str(payload.asset_id),
            "from": sender.upstream_uid,
            "to": recipient.upstream_uid,
            "message_type": "flash" if payload.flash else payload.kind,
            "object_name": _OBJECT_NAMES[payload.kind],
            "media_report": _media_report(attachment.id, payload),
            "schema": MEDIA_MESSAGE_SCHEMA,
        }
        if payload.flash:
            delivery_payload["flash_id"] = str(attachment.id)
        if is_local:
            delivery_payload["projection_message_ids"] = [
                str(item) for item in projection_ids
            ]
        statement = (
            insert(MessageDelivery)
            .values(
                message_id=canonical.id,
                channel=channel,
                target_key=target_key,
                target_user_id=recipient.user_id if is_local else None,
                target_upstream_uid=recipient.upstream_uid,
                required=is_local,
                status=(
                    "delivered"
                    if is_local
                    else "cancelled"
                    if mirror_retired
                    else "pending"
                ),
                delivered_at=canonical.occurred_at if is_local else None,
                last_error=TIM_MIRROR_CANCELLED_REASON if mirror_retired else None,
                payload=delivery_payload,
            )
            .on_conflict_do_nothing(
                constraint="uq_message_deliveries_message_target"
            )
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
        if existing is None or dict(existing.payload or {}) != delivery_payload:
            raise MediaIdempotencyConflict("媒体投递 outbox 与幂等键冲突")
        return existing

    def store_attachment(
        self,
        *,
        sender: MediaAccount,
        recipient: MediaAccount,
        thread: DirectMediaThread,
        asset: MediaAsset,
        client_message_id: str,
        payload: MediaAttachmentPayload,
        sent_at: datetime,
    ) -> AttachmentDeliveryResult:
        thread_row = self._require_authorized_thread(thread, sender, recipient)
        details = self._user_details(sender, recipient)
        current_reference_exists = (
            select(MediaAssetReferenceRow.id)
            .where(MediaAssetReferenceRow.asset_id == MediaAssetRow.id)
            .exists()
        )
        asset_row = self.db.scalar(
            select(MediaAssetRow)
            .where(
                MediaAssetRow.id == asset.id,
                MediaAssetRow.owner_user_id == sender.user_id,
                MediaAssetRow.status == ASSET_STATUS_AVAILABLE,
                ~current_reference_exists,
            )
            .with_for_update()
        )
        reference_after_asset_lock = (
            self.db.scalar(
                select(MediaAssetReferenceRow.id)
                .where(MediaAssetReferenceRow.asset_id == asset.id)
                .limit(1)
            )
            if asset_row is not None
            else None
        )
        if (
            asset_row is None
            or reference_after_asset_lock is not None
            or _as_asset(asset_row) != asset
        ):
            raise MediaAssetUnavailable("媒体 asset 不存在、不可用或已变化")
        digest = _payload_digest(
            thread_id=thread_row.id,
            recipient_user_id=recipient.user_id,
            client_message_id=client_message_id,
            payload=payload,
        )
        retention_days = max(details[sender.user_id][1], details[recipient.user_id][1])
        message_type = "flash" if payload.flash else payload.kind
        canonical_metadata = {
            "recipient_upstream_uid": recipient.upstream_uid,
            "recipient_user_id": str(recipient.user_id),
            "sender_upstream_uid": sender.upstream_uid,
            "asset_id": str(asset.id),
            "schema": MEDIA_MESSAGE_SCHEMA,
        }
        statement = (
            insert(ChatMessage)
            .values(
                thread_id=thread_row.id,
                sender_user_id=sender.user_id,
                client_message_id=client_message_id,
                payload_digest=digest,
                message_type=message_type,
                body=None,
                status="accepted",
                occurred_at=sent_at,
                retention_expires_at=sent_at + timedelta(days=retention_days),
                extra_data=canonical_metadata,
            )
            .on_conflict_do_nothing(constraint="uq_chat_messages_sender_client")
            .returning(ChatMessage)
        )
        canonical = self.db.scalars(statement).first()
        canonical_created = canonical is not None
        if canonical is None:
            canonical = self.db.scalar(
                select(ChatMessage).where(
                    ChatMessage.sender_user_id == sender.user_id,
                    ChatMessage.client_message_id == client_message_id,
                )
            )
            if (
                canonical is None
                or canonical.thread_id != thread_row.id
                or canonical.payload_digest != digest
                or canonical.message_type != message_type
            ):
                raise MediaIdempotencyConflict(
                    "client_message_id 已用于另一条消息"
                )

        if (
            asset_row.expires_at is None
            or asset_row.expires_at < canonical.retention_expires_at
        ):
            asset_row.expires_at = canonical.retention_expires_at

        attachment_id = uuid.uuid4()
        attachment_values = {
            "id": attachment_id,
            "message_id": canonical.id,
            "thread_id": thread_row.id,
            "asset_id": asset.id,
            "sender_user_id": sender.user_id,
            "recipient_user_id": recipient.user_id,
            "client_message_id": client_message_id,
            "sender_upstream_uid": sender.upstream_uid,
            "recipient_upstream_uid": recipient.upstream_uid,
            "payload": payload.as_persistent_dict(),
            "status": ATTACHMENT_STATUS_SENT,
            "flash": payload.flash,
            "flash_display_seconds": payload.flash_display_seconds,
            "sent_at": sent_at,
        }
        attachment_row = self.db.scalars(
            insert(MediaAttachmentRow)
            .values(**attachment_values)
            .on_conflict_do_nothing(constraint="uq_media_attachments_message")
            .returning(MediaAttachmentRow)
        ).first()
        attachment_created = attachment_row is not None
        if attachment_row is None:
            attachment_row = self.db.scalar(
                select(MediaAttachmentRow).where(
                    MediaAttachmentRow.message_id == canonical.id
                )
            )
            if (
                attachment_row is None
                or attachment_row.asset_id != asset.id
                or attachment_row.sender_user_id != sender.user_id
                or attachment_row.recipient_user_id != recipient.user_id
                or dict(attachment_row.payload or {}) != payload.as_persistent_dict()
            ):
                raise MediaIdempotencyConflict("canonical 消息已绑定其他附件")

        conversations = self._ensure_conversations(
            thread_row, sender, recipient, details
        )
        sender_projection, _ = self._insert_projection(
            owner=sender,
            conversation=conversations[sender.user_id],
            canonical=canonical,
            attachment_id=attachment_row.id,
            sender=sender,
            recipient=recipient,
            payload=payload,
            direction="outgoing",
            retention_days=details[sender.user_id][1],
        )
        recipient_projection, recipient_projection_created = self._insert_projection(
            owner=recipient,
            conversation=conversations[recipient.user_id],
            canonical=canonical,
            attachment_id=attachment_row.id,
            sender=sender,
            recipient=recipient,
            payload=payload,
            direction="incoming",
            retention_days=details[recipient.user_id][1],
        )
        preview_text = "[闪图]" if payload.flash else _PREVIEW_TEXT[payload.kind]
        if canonical_created:
            thread_row.last_message_at = canonical.occurred_at
            recipient_member = self.db.scalar(
                select(ChatMember).where(
                    ChatMember.thread_id == thread_row.id,
                    ChatMember.user_id == recipient.user_id,
                )
            )
            recipient_member.unread_count = int(recipient_member.unread_count or 0) + 1
        for owner_id, conversation in conversations.items():
            if (
                conversation.last_message_at is None
                or conversation.last_message_at <= canonical.occurred_at
            ):
                conversation.last_message_at = canonical.occurred_at
                conversation.extra_data = {
                    **dict(conversation.extra_data or {}),
                    "last_message": preview_text,
                    "preview_authoritative": True,
                    "preview_timestamp": canonical.occurred_at.isoformat(),
                    "source": MEDIA_NATIVE_PROVIDER,
                }
            if owner_id == recipient.user_id and recipient_projection_created:
                conversation.unread_count = int(conversation.unread_count or 0) + 1
                conversation.unread_observed_at = canonical.occurred_at
        self.db.execute(
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
        projection_ids = (sender_projection.id, recipient_projection.id)
        self._ensure_delivery(
            canonical=canonical,
            attachment=attachment_row,
            sender=sender,
            recipient=recipient,
            payload=payload,
            channel=LOCAL_DELIVERY_CHANNEL,
            projection_ids=projection_ids,
        )
        tim_delivery = self._ensure_delivery(
            canonical=canonical,
            attachment=attachment_row,
            sender=sender,
            recipient=recipient,
            payload=payload,
            channel=TIM_MIRROR_CHANNEL,
            projection_ids=projection_ids,
        )
        self.db.flush()
        return AttachmentDeliveryResult(
            attachment=_as_attachment(attachment_row),
            sender_projection_id=sender_projection.id,
            recipient_projection_id=recipient_projection.id,
            created=canonical_created and attachment_created,
            tim_mirror=TimMediaMirrorIntent(
                delivery_id=tim_delivery.id,
                message_id=canonical.id,
                attachment_id=attachment_row.id,
                from_upstream_uid=sender.upstream_uid,
                to_upstream_uid=recipient.upstream_uid,
                client_message_id=client_message_id,
                payload=payload,
                status=str(tim_delivery.status or "pending"),
            ),
        )

    def get_media_attachment(
        self,
        attachment_id: uuid.UUID,
    ) -> MediaAttachment | None:
        row = self.db.get(MediaAttachmentRow, attachment_id)
        return _as_attachment(row) if row is not None else None

    def _ensure_revoke_delivery(
        self,
        canonical: ChatMessage,
        attachment: MediaAttachmentRow,
    ) -> MessageDelivery:
        target_key = f"media-revoke:{attachment.recipient_upstream_uid}"
        payload = self._revoke_delivery_payload(canonical, attachment)
        mirror_retired = self.compatibility_mode is CompatibilityMode.RETIRED
        statement = (
            insert(MessageDelivery)
            .values(
                message_id=canonical.id,
                channel=TIM_MIRROR_CHANNEL,
                target_key=target_key,
                target_upstream_uid=attachment.recipient_upstream_uid,
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
        existing = self._existing_revoke_delivery(canonical, attachment)
        if existing is None:
            raise MediaIdempotencyConflict("媒体撤回 outbox 不存在")
        return existing

    @staticmethod
    def _revoke_delivery_payload(
        canonical: ChatMessage,
        attachment: MediaAttachmentRow,
    ) -> dict[str, Any]:
        return {
            "operation": "revoke",
            "canonical_message_id": str(canonical.id),
            "client_message_id": canonical.client_message_id,
            "attachment_id": str(attachment.id),
            "from": attachment.sender_upstream_uid,
            "to": attachment.recipient_upstream_uid,
            "schema": MEDIA_MESSAGE_SCHEMA,
        }

    def _existing_revoke_delivery(
        self,
        canonical: ChatMessage,
        attachment: MediaAttachmentRow,
    ) -> MessageDelivery | None:
        target_key = f"media-revoke:{attachment.recipient_upstream_uid}"
        existing = self.db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.message_id == canonical.id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.target_key == target_key,
            )
            .with_for_update()
        )
        if existing is not None and dict(existing.payload or {}) != (
            self._revoke_delivery_payload(canonical, attachment)
        ):
            raise MediaIdempotencyConflict("媒体撤回 outbox 冲突")
        return existing

    def _reconcile_tim_send_on_revoke(
        self,
        canonical: ChatMessage,
        attachment: MediaAttachmentRow,
    ) -> MessageDelivery | None:
        """取消尚未投递的 TIM send；仅在可能已外发时创建 revoke。"""

        send_target_key = f"media-send:{attachment.recipient_upstream_uid}"
        send_delivery = self.db.scalar(
            select(MessageDelivery)
            .where(
                MessageDelivery.message_id == canonical.id,
                MessageDelivery.channel == TIM_MIRROR_CHANNEL,
                MessageDelivery.target_key == send_target_key,
            )
            .with_for_update()
        )
        if send_delivery is None:
            # 旧数据或被外部清理后的状态不可判定，补偿性撤回最安全。
            return self._ensure_revoke_delivery(canonical, attachment)
        if str((send_delivery.payload or {}).get("operation") or "") != "send":
            raise MediaIdempotencyConflict("媒体发送 outbox 类型冲突")

        send_status = str(send_delivery.status or "")
        if send_status in {"pending", "retry", "failed"}:
            send_delivery.status = "cancelled"
            send_delivery.locked_by = None
            send_delivery.locked_until = None
            send_delivery.last_error = "cancelled by local media revoke"
            return None
        if send_status == "cancelled":
            # processing 竞态的首次撤回会同时创建 revoke；重复撤回应复用它。
            return self._existing_revoke_delivery(canonical, attachment)
        if send_status == "processing":
            # worker 可能已发出请求。阻止失败后重试，并安排补偿性撤回。
            send_delivery.status = "cancelled"
            send_delivery.locked_by = None
            send_delivery.locked_until = None
            send_delivery.last_error = "cancelled while TIM media send was processing"
            return self._ensure_revoke_delivery(canonical, attachment)
        if send_status == "delivered":
            return self._ensure_revoke_delivery(canonical, attachment)

        # 未知终态不能证明上游未收到消息，按可能已投递处理。
        return self._ensure_revoke_delivery(canonical, attachment)

    def revoke_attachment(
        self,
        *,
        attachment: MediaAttachment,
        actor: MediaAccount,
        revoked_at: datetime,
    ) -> MediaRevokeResult:
        row = self.db.scalar(
            select(MediaAttachmentRow)
            .where(MediaAttachmentRow.id == attachment.id)
            .with_for_update()
        )
        if row is None or row.sender_user_id != actor.user_id:
            raise MediaRevocationDenied("只有发送者可以撤回媒体")
        canonical = self.db.scalar(
            select(ChatMessage)
            .where(ChatMessage.id == row.message_id)
            .with_for_update()
        )
        if canonical is None:
            raise MediaRevocationDenied("canonical 消息不存在")
        created = row.status != ATTACHMENT_STATUS_REVOKED
        if created:
            if revoked_at > row.sent_at + timedelta(minutes=2):
                raise MediaRevocationExpired("媒体撤回期限为 2 分钟")
            row.status = ATTACHMENT_STATUS_REVOKED
            row.revoked_at = revoked_at
            canonical.status = "revoked"
            canonical.extra_data = {
                **dict(canonical.extra_data or {}),
                "revoked": True,
                "revoked_at": revoked_at.isoformat(),
            }
            projections = list(
                self.db.scalars(
                    select(Message)
                    .where(
                        Message.provider == MEDIA_NATIVE_PROVIDER,
                        Message.upstream_message_id == str(canonical.id),
                    )
                    .with_for_update()
                )
            )
            for projection in projections:
                projection.status = "revoked"
                projection.extra_data = {
                    **dict(projection.extra_data or {}),
                    "revoked": True,
                    "revoked_at": revoked_at.isoformat(),
                }
        delivery = self._reconcile_tim_send_on_revoke(canonical, row)
        self.db.flush()
        return MediaRevokeResult(
            attachment=_as_attachment(row),
            created=created,
            tim_mirror=(
                TimMediaRevokeIntent(
                    delivery_id=delivery.id,
                    message_id=canonical.id,
                    attachment_id=row.id,
                    from_upstream_uid=row.sender_upstream_uid,
                    to_upstream_uid=row.recipient_upstream_uid,
                    client_message_id=row.client_message_id,
                    status=str(delivery.status or "pending"),
                )
                if delivery is not None
                else None
            ),
        )

    def claim_flash_once(
        self,
        *,
        attachment: MediaAttachment,
        claimant: MediaAccount,
        claimed_at: datetime,
        display_until: datetime,
    ) -> FlashClaim:
        row = self.db.scalar(
            select(MediaAttachmentRow)
            .where(MediaAttachmentRow.id == attachment.id)
            .with_for_update()
        )
        if (
            row is None
            or not row.flash
            or row.status != ATTACHMENT_STATUS_SENT
            or row.recipient_user_id != claimant.user_id
            or display_until != claimed_at + timedelta(seconds=5)
        ):
            raise FlashClaimDenied("闪照领取条件不满足")
        claim = self.db.scalars(
            insert(MediaFlashClaimRow)
            .values(
                attachment_id=row.id,
                claimant_user_id=claimant.user_id,
                claimed_at=claimed_at,
                display_until=display_until,
            )
            .on_conflict_do_nothing(
                constraint="uq_media_flash_claims_attachment"
            )
            .returning(MediaFlashClaimRow)
        ).first()
        if claim is None:
            raise FlashAlreadyClaimed("闪照已经领取")
        self.db.flush()
        return FlashClaim(
            id=claim.id,
            attachment_id=claim.attachment_id,
            claimant_user_id=claim.claimant_user_id,
            claimed_at=claim.claimed_at,
            display_until=claim.display_until,
        )
