"""Web-native 私聊富媒体的领域契约。

这里的类型刻意不依赖 SQLAlchemy、FastAPI、TIM 或具体对象存储。持久层只保存
稳定标识与对象引用；预签名 URL 仅存在于短生命周期的 grant 类型中。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


MEDIA_NATIVE_PROVIDER = "web-local"
LEGACY_ACCOUNT_PROVIDER = "beibeiwu"

MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_AUDIO = "audio"
MEDIA_KIND_VIDEO = "video"
MEDIA_KIND_FILE = "file"
MEDIA_KINDS = frozenset(
    {MEDIA_KIND_IMAGE, MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO, MEDIA_KIND_FILE}
)

STORAGE_ZONE_STAGING = "staging"
STORAGE_ZONE_PRIVATE = "private"

UPLOAD_STATUS_PENDING = "pending"
UPLOAD_STATUS_COMPLETED = "completed"
UPLOAD_STATUS_CANCELLED = "cancelled"
ASSET_STATUS_AVAILABLE = "available"
ASSET_STATUS_DELETED = "deleted"
ATTACHMENT_STATUS_SENT = "sent"
ATTACHMENT_STATUS_REVOKED = "revoked"


class MediaNativeError(RuntimeError):
    code = "media_native_error"


class MediaContractViolation(MediaNativeError):
    code = "media_contract_violation"


class InvalidMediaRequest(MediaNativeError):
    code = "invalid_media_request"


class MediaIdentityUnavailable(MediaNativeError):
    code = "media_identity_unavailable"


class MediaPeerUnavailable(MediaNativeError):
    code = "media_peer_unavailable"


class MediaBlocked(MediaNativeError):
    code = "media_blocked"


class MediaThreadForbidden(MediaNativeError):
    code = "media_thread_forbidden"


class UploadIntentUnavailable(MediaNativeError):
    code = "upload_intent_unavailable"


class UploadIntentExpired(MediaNativeError):
    code = "upload_intent_expired"


class UploadAlreadyCompleted(MediaNativeError):
    code = "upload_already_completed"


class MediaInspectionRejected(MediaNativeError):
    code = "media_inspection_rejected"


class MediaIntegrityMismatch(MediaNativeError):
    code = "media_integrity_mismatch"


class MediaTypeRejected(MediaNativeError):
    code = "media_type_rejected"


class MediaTooLarge(MediaNativeError):
    code = "media_too_large"


class MediaQuotaExceeded(MediaNativeError):
    code = "media_quota_exceeded"


class MediaAssetUnavailable(MediaNativeError):
    code = "media_asset_unavailable"


class MediaIdempotencyConflict(MediaNativeError):
    code = "media_idempotency_conflict"


class MediaAccessDenied(MediaNativeError):
    code = "media_access_denied"


class MediaRevocationDenied(MediaNativeError):
    code = "media_revocation_denied"


class MediaRevocationExpired(MediaNativeError):
    code = "media_revocation_expired"


class FlashClaimRequired(MediaNativeError):
    code = "flash_claim_required"


class FlashClaimDenied(MediaNativeError):
    code = "flash_claim_denied"


class FlashAlreadyClaimed(MediaNativeError):
    code = "flash_already_claimed"


@dataclass(frozen=True, slots=True)
class MediaPrincipal:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class MediaAccount:
    user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    account_provider: str = LEGACY_ACCOUNT_PROVIDER


@dataclass(frozen=True, slots=True)
class DirectMediaThread:
    id: uuid.UUID
    member_user_ids: frozenset[uuid.UUID]
    kind: str = "direct"
    status: str = "active"


@dataclass(frozen=True, slots=True)
class StorageObjectRef:
    """不含任何访问凭据的私有对象引用。"""

    deployment: str
    zone: str
    bucket: str
    namespace: str
    key: str
    owner_user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class StagingUploadTarget:
    """临时上传授权；此类型绝不能传给持久层。"""

    intent_id: uuid.UUID
    object_ref: StorageObjectRef
    upload_url: str
    expires_at: datetime
    required_headers: tuple[tuple[str, str], ...] = ()
    method: str = "PUT"


@dataclass(frozen=True, slots=True)
class UploadIntent:
    id: uuid.UUID
    owner_user_id: uuid.UUID
    deployment: str
    kind: str
    filename: str
    declared_content_type: str
    expected_size_bytes: int
    expected_sha256: str
    staging_object: StorageObjectRef
    created_at: datetime
    expires_at: datetime
    status: str = UPLOAD_STATUS_PENDING
    completed_asset_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class UploadGrant:
    intent: UploadIntent
    target: StagingUploadTarget


@dataclass(frozen=True, slots=True)
class StagedMediaInspection:
    """对象存储适配器对真实 staging 对象的检测结果。"""

    object_ref: StorageObjectRef
    verified: bool
    detected_kind: str
    detected_content_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class FinalizedMediaObject:
    """适配器完成 staging -> private 提升后的稳定对象。"""

    object_ref: StorageObjectRef
    content_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class MediaAsset:
    id: uuid.UUID
    owner_user_id: uuid.UUID
    deployment: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    object_ref: StorageObjectRef
    created_at: datetime
    status: str = ASSET_STATUS_AVAILABLE
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MediaAttachmentPayload:
    """可安全持久化到消息和 TIM outbox 的媒体元数据。"""

    asset_id: uuid.UUID
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    flash: bool = False
    flash_display_seconds: int | None = None

    def as_persistent_dict(self) -> dict[str, object]:
        return {
            "asset_id": str(self.asset_id),
            "kind": self.kind,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "flash": self.flash,
            "flash_display_seconds": self.flash_display_seconds,
        }


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    id: uuid.UUID
    message_id: uuid.UUID
    thread_id: uuid.UUID
    client_message_id: str
    sender_user_id: uuid.UUID
    recipient_user_id: uuid.UUID
    sender_upstream_uid: str
    recipient_upstream_uid: str
    payload: MediaAttachmentPayload
    sent_at: datetime
    status: str = ATTACHMENT_STATUS_SENT
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TimMediaMirrorIntent:
    """本地提交后由 worker 异步处理的 TIM 发送意图。"""

    delivery_id: uuid.UUID
    message_id: uuid.UUID
    attachment_id: uuid.UUID
    from_upstream_uid: str
    to_upstream_uid: str
    client_message_id: str
    payload: MediaAttachmentPayload
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class AttachmentDeliveryResult:
    attachment: MediaAttachment
    sender_projection_id: uuid.UUID
    recipient_projection_id: uuid.UUID
    created: bool
    tim_mirror: TimMediaMirrorIntent


@dataclass(frozen=True, slots=True)
class TimMediaRevokeIntent:
    delivery_id: uuid.UUID
    message_id: uuid.UUID
    attachment_id: uuid.UUID
    from_upstream_uid: str
    to_upstream_uid: str
    client_message_id: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class MediaRevokeResult:
    attachment: MediaAttachment
    created: bool
    # 原 TIM 发送尚未投递时会在同一事务直接取消，因此无需额外撤回。
    tim_mirror: TimMediaRevokeIntent | None


@dataclass(frozen=True, slots=True)
class PresignedMediaRead:
    """存储适配器返回的临时读取授权，不可持久化。"""

    object_ref: StorageObjectRef
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MediaAccessGrant:
    attachment_id: uuid.UUID
    asset_id: uuid.UUID
    url: str
    expires_at: datetime
    content_type: str
    size_bytes: int
    display_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class FlashClaim:
    id: uuid.UUID
    attachment_id: uuid.UUID
    claimant_user_id: uuid.UUID
    claimed_at: datetime
    display_until: datetime


@dataclass(frozen=True, slots=True)
class FlashClaimResult:
    claim: FlashClaim
    access: MediaAccessGrant


@runtime_checkable
class MediaConversationPolicy(Protocol):
    def resolve_principal(self, principal: MediaPrincipal) -> MediaAccount | None: ...

    def resolve_active_peer(
        self, upstream_uid: str, *, provider: str
    ) -> MediaAccount | None: ...

    def can_send_private_message(
        self, sender: MediaAccount, recipient: MediaAccount
    ) -> bool: ...

    def resolve_or_create_direct_thread(
        self, sender: MediaAccount, recipient: MediaAccount
    ) -> DirectMediaThread | None: ...

    def resolve_thread(self, thread_id: uuid.UUID) -> DirectMediaThread | None: ...

    def is_blocked_between(
        self, left_user_id: uuid.UUID, right_user_id: uuid.UUID
    ) -> bool: ...


@runtime_checkable
class UploadIntentStore(Protocol):
    def create_upload_intent(self, intent: UploadIntent) -> UploadIntent: ...

    def get_upload_intent(self, intent_id: uuid.UUID) -> UploadIntent | None: ...

    def complete_upload(
        self,
        *,
        intent: UploadIntent,
        inspection: StagedMediaInspection,
        finalized: FinalizedMediaObject,
        completed_at: datetime,
    ) -> MediaAsset:
        """原子消费 pending intent 并创建 asset；同一 intent 只能成功一次。"""
        ...


@runtime_checkable
class MediaAssetStore(Protocol):
    def get_media_asset(self, asset_id: uuid.UUID) -> MediaAsset | None: ...


@runtime_checkable
class MediaAttachmentStore(Protocol):
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
        """原子写本地消息、双方投影及 pending TIM outbox。"""
        ...

    def get_media_attachment(
        self, attachment_id: uuid.UUID
    ) -> MediaAttachment | None: ...

    def revoke_attachment(
        self,
        *,
        attachment: MediaAttachment,
        actor: MediaAccount,
        revoked_at: datetime,
    ) -> MediaRevokeResult:
        """原子撤回本地消息，并按原发送状态取消发送或写 TIM 撤回 outbox。"""
        ...


@runtime_checkable
class FlashClaimStore(Protocol):
    def claim_flash_once(
        self,
        *,
        attachment: MediaAttachment,
        claimant: MediaAccount,
        claimed_at: datetime,
        display_until: datetime,
    ) -> FlashClaim:
        """按 attachment/claimant 建唯一约束并原子地只允许首次领取。"""
        ...


@runtime_checkable
class MediaNativeRepository(
    UploadIntentStore,
    MediaAssetStore,
    MediaAttachmentStore,
    FlashClaimStore,
    Protocol,
):
    pass


@runtime_checkable
class MediaStorageAdapter(Protocol):
    def prepare_staging_upload(
        self,
        *,
        deployment: str,
        intent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        kind: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        expires_at: datetime,
    ) -> StagingUploadTarget: ...

    def inspect_staging(self, intent: UploadIntent) -> StagedMediaInspection:
        """读取真实对象并执行魔数、容器、解码及媒体时长检测。"""
        ...

    def promote_verified(
        self, intent: UploadIntent, inspection: StagedMediaInspection
    ) -> FinalizedMediaObject:
        """幂等地把已验证对象从 staging 提升到隔离的 private 区。"""
        ...

    def issue_private_read(
        self, asset: MediaAsset, *, expires_at: datetime
    ) -> PresignedMediaRead: ...

    def delete_upload_artifacts(self, intent: UploadIntent) -> int:
        """幂等删除 intent 的 staging 对象及隔离 private 前缀。"""
        ...

    def delete_private_asset(self, asset: MediaAsset) -> None:
        """幂等删除一个已持久化 asset 的 private 对象。"""
        ...
