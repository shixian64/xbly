"""Web-native 私聊富媒体的纯业务编排与校验。"""

from __future__ import annotations

import hmac
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

from .contracts import (
    ASSET_STATUS_AVAILABLE,
    ATTACHMENT_STATUS_REVOKED,
    ATTACHMENT_STATUS_SENT,
    MEDIA_KIND_AUDIO,
    MEDIA_KIND_FILE,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    MEDIA_KINDS,
    STORAGE_ZONE_PRIVATE,
    STORAGE_ZONE_STAGING,
    UPLOAD_STATUS_COMPLETED,
    UPLOAD_STATUS_PENDING,
    AttachmentDeliveryResult,
    DirectMediaThread,
    FinalizedMediaObject,
    FlashClaimDenied,
    FlashClaimRequired,
    FlashClaimResult,
    InvalidMediaRequest,
    MediaAccessDenied,
    MediaAccessGrant,
    MediaAccount,
    MediaAsset,
    MediaAssetUnavailable,
    MediaAttachment,
    MediaAttachmentPayload,
    MediaBlocked,
    MediaContractViolation,
    MediaConversationPolicy,
    MediaIdentityUnavailable,
    MediaInspectionRejected,
    MediaIntegrityMismatch,
    MediaNativeRepository,
    MediaPeerUnavailable,
    MediaPrincipal,
    MediaRevocationDenied,
    MediaRevocationExpired,
    MediaRevokeResult,
    MediaStorageAdapter,
    MediaThreadForbidden,
    MediaTooLarge,
    MediaTypeRejected,
    PresignedMediaRead,
    StagedMediaInspection,
    StagingUploadTarget,
    StorageObjectRef,
    UploadAlreadyCompleted,
    UploadGrant,
    UploadIntent,
    UploadIntentExpired,
    UploadIntentUnavailable,
)


MIB = 1024 * 1024
MAX_MEDIA_BYTES = {
    MEDIA_KIND_IMAGE: 20 * MIB,
    MEDIA_KIND_AUDIO: 20 * MIB,
    MEDIA_KIND_VIDEO: 100 * MIB,
    MEDIA_KIND_FILE: 40 * MIB,
}
MAX_AUDIO_DURATION_SECONDS = 60
REVOKE_WINDOW_SECONDS = 2 * 60
FLASH_DISPLAY_SECONDS = 5
UPLOAD_INTENT_TTL_SECONDS = 15 * 60
NORMAL_ACCESS_TTL_SECONDS = 5 * 60
MAX_CLIENT_MESSAGE_ID_LENGTH = 160
MAX_FILENAME_LENGTH = 255


IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
AUDIO_CONTENT_TYPES = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
    }
)
VIDEO_CONTENT_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/webm",
    }
)
DENIED_FILE_CONTENT_TYPES = frozenset(
    {
        "application/javascript",
        "application/x-httpd-php",
        "application/x-msdownload",
        "application/x-sh",
        "image/svg+xml",
        "text/html",
        "text/javascript",
    }
)

_DEPLOYMENT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CONTENT_TYPE_RE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware_time(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MediaContractViolation(f"{field} 必须是带时区时间")
    return value


def _uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidMediaRequest(f"{field} 不合法") from exc


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidMediaRequest(f"{field} 不合法")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
    else:
        raise InvalidMediaRequest(f"{field} 不合法")
    if number <= 0:
        raise InvalidMediaRequest(f"{field} 必须大于 0")
    return number


def _normalize_deployment(value: object) -> str:
    deployment = str(value or "").strip().lower()
    if not _DEPLOYMENT_RE.fullmatch(deployment):
        raise InvalidMediaRequest("deployment 不合法")
    return deployment


def _normalize_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    if kind not in MEDIA_KINDS:
        raise InvalidMediaRequest("媒体类型不支持")
    return kind


def _normalize_content_type(value: object) -> str:
    content_type = str(value or "").split(";", 1)[0].strip().lower()
    if not _CONTENT_TYPE_RE.fullmatch(content_type):
        raise MediaTypeRejected("content_type 不合法")
    return content_type


def _normalize_sha256(value: object) -> str:
    digest = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise InvalidMediaRequest("sha256 必须是 64 位十六进制摘要")
    return digest


def _normalize_filename(value: object) -> str:
    filename = str(value or "").strip()
    if (
        not filename
        or len(filename) > MAX_FILENAME_LENGTH
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
    ):
        raise InvalidMediaRequest("filename 不合法")
    return filename


def _normalize_upstream_uid(value: object) -> str:
    uid = str(value or "").strip()
    if (
        not uid
        or uid.lower() in {"0", "none", "null"}
        or len(uid) > 128
        or any(ord(char) < 33 for char in uid)
    ):
        return ""
    return uid


def _normalize_client_message_id(value: object) -> str:
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > MAX_CLIENT_MESSAGE_ID_LENGTH
        or any(ord(char) < 32 for char in identifier)
    ):
        return ""
    return identifier


def _content_type_allowed(kind: str, content_type: str) -> bool:
    if kind == MEDIA_KIND_IMAGE:
        return content_type in IMAGE_CONTENT_TYPES
    if kind == MEDIA_KIND_AUDIO:
        return content_type in AUDIO_CONTENT_TYPES
    if kind == MEDIA_KIND_VIDEO:
        return content_type in VIDEO_CONTENT_TYPES
    return (
        kind == MEDIA_KIND_FILE
        and content_type not in DENIED_FILE_CONTENT_TYPES
        and not content_type.startswith(("image/", "audio/", "video/"))
    )


def _validate_size(kind: str, size_bytes: int) -> None:
    maximum = MAX_MEDIA_BYTES[kind]
    if size_bytes > maximum:
        raise MediaTooLarge(f"{kind} 超过 {maximum} 字节限制")


def _validate_object_ref(
    object_ref: StorageObjectRef,
    *,
    deployment: str,
    zone: str,
    owner_user_id: uuid.UUID,
) -> None:
    if not isinstance(object_ref, StorageObjectRef):
        raise MediaContractViolation("存储适配器返回了未知对象引用")
    if (
        object_ref.deployment != deployment
        or object_ref.zone != zone
        or object_ref.owner_user_id != owner_user_id
    ):
        raise MediaContractViolation("对象引用跨 deployment、zone 或用户边界")
    for value in (object_ref.bucket, object_ref.namespace, object_ref.key):
        if (
            not str(value or "").strip()
            or len(str(value)) > 1024
            or "://" in str(value)
            or "?" in str(value)
            or "#" in str(value)
            or any(ord(char) < 32 or ord(char) == 127 for char in str(value))
        ):
            raise MediaContractViolation("对象引用不合法")
    parts = object_ref.key.replace("\\", "/").split("/")
    if object_ref.key.startswith(("/", "\\")) or any(part in {"", ".", ".."} for part in parts):
        raise MediaContractViolation("对象 key 不合法")


def _validate_direct_thread(
    thread: DirectMediaThread | None,
    left_user_id: uuid.UUID,
    right_user_id: uuid.UUID,
) -> DirectMediaThread:
    members = frozenset({left_user_id, right_user_id})
    if (
        thread is None
        or thread.kind != "direct"
        or thread.status != "active"
        or thread.member_user_ids != members
    ):
        raise MediaThreadForbidden("当前用户不是有效私聊线程成员")
    return thread


class MediaNativeService:
    """本地媒体写入成功优先，TIM 只通过持久 outbox intent 兼容。"""

    def __init__(
        self,
        repository: MediaNativeRepository,
        storage: MediaStorageAdapter,
        policy: MediaConversationPolicy,
        *,
        deployment: str,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.policy = policy
        self.deployment = _normalize_deployment(deployment)
        self.clock = clock
        self.id_factory = id_factory

    def _now(self) -> datetime:
        return _aware_time(self.clock(), "clock")

    def _principal_account(self, principal: MediaPrincipal) -> MediaAccount:
        account = self.policy.resolve_principal(principal)
        if account is None:
            raise MediaIdentityUnavailable("当前 Web 账号不可使用本地媒体")
        if (
            account.user_id != principal.user_id
            or account.external_account_id != principal.external_account_id
            or account.upstream_uid != principal.upstream_uid
            or account.account_provider != principal.account_provider
        ):
            raise MediaContractViolation("身份策略返回了未绑定账号")
        return account

    def create_upload_intent(
        self,
        *,
        principal: MediaPrincipal,
        kind: object,
        filename: object,
        content_type: object,
        size_bytes: object,
        sha256: object,
    ) -> UploadGrant:
        owner = self._principal_account(principal)
        normalized_kind = _normalize_kind(kind)
        normalized_name = _normalize_filename(filename)
        normalized_type = _normalize_content_type(content_type)
        normalized_size = _positive_int(size_bytes, "size_bytes")
        normalized_digest = _normalize_sha256(sha256)
        if not _content_type_allowed(normalized_kind, normalized_type):
            raise MediaTypeRejected("声明类型与媒体类别不兼容")
        _validate_size(normalized_kind, normalized_size)

        now = self._now()
        expires_at = now + timedelta(seconds=UPLOAD_INTENT_TTL_SECONDS)
        intent_id = self.id_factory()
        target = self.storage.prepare_staging_upload(
            deployment=self.deployment,
            intent_id=intent_id,
            owner_user_id=owner.user_id,
            kind=normalized_kind,
            content_type=normalized_type,
            size_bytes=normalized_size,
            sha256=normalized_digest,
            expires_at=expires_at,
        )
        self._validate_staging_target(
            target,
            intent_id=intent_id,
            owner_user_id=owner.user_id,
            expires_at=expires_at,
        )
        intent = UploadIntent(
            id=intent_id,
            owner_user_id=owner.user_id,
            deployment=self.deployment,
            kind=normalized_kind,
            filename=normalized_name,
            declared_content_type=normalized_type,
            expected_size_bytes=normalized_size,
            expected_sha256=normalized_digest,
            staging_object=target.object_ref,
            created_at=now,
            expires_at=expires_at,
        )
        stored = self.repository.create_upload_intent(intent)
        if stored != intent:
            raise MediaContractViolation("上传意图持久层改变了已校验字段")
        return UploadGrant(intent=stored, target=target)

    def _validate_staging_target(
        self,
        target: StagingUploadTarget,
        *,
        intent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        expires_at: datetime,
    ) -> None:
        if not isinstance(target, StagingUploadTarget):
            raise MediaContractViolation("存储适配器未返回 staging target")
        _validate_object_ref(
            target.object_ref,
            deployment=self.deployment,
            zone=STORAGE_ZONE_STAGING,
            owner_user_id=owner_user_id,
        )
        if target.intent_id != intent_id:
            raise MediaContractViolation("staging target 未绑定当前 intent")
        target_expiry = _aware_time(target.expires_at, "target.expires_at")
        if target_expiry <= self._now() or target_expiry > expires_at:
            raise MediaContractViolation("staging 上传授权有效期不合法")
        if not target.upload_url.strip() or any(ord(char) < 32 for char in target.upload_url):
            raise MediaContractViolation("staging 上传 URL 不合法")
        if target.method != "PUT":
            raise MediaContractViolation("staging 仅允许 PUT")

    def complete_upload(
        self,
        *,
        principal: MediaPrincipal,
        intent_id: object,
    ) -> MediaAsset:
        owner = self._principal_account(principal)
        normalized_id = _uuid(intent_id, "intent_id")
        intent = self.repository.get_upload_intent(normalized_id)
        if intent is None or intent.owner_user_id != owner.user_id:
            raise UploadIntentUnavailable("上传意图不存在或不属于当前用户")
        if intent.deployment != self.deployment:
            raise UploadIntentUnavailable("上传意图不属于当前 deployment")
        if intent.status == UPLOAD_STATUS_COMPLETED:
            raise UploadAlreadyCompleted("上传意图已经完成")
        if intent.status != UPLOAD_STATUS_PENDING:
            raise UploadIntentUnavailable("上传意图状态不可用")
        now = self._now()
        if now >= _aware_time(intent.expires_at, "intent.expires_at"):
            raise UploadIntentExpired("上传意图已过期")
        _validate_object_ref(
            intent.staging_object,
            deployment=self.deployment,
            zone=STORAGE_ZONE_STAGING,
            owner_user_id=owner.user_id,
        )

        inspection = self.storage.inspect_staging(intent)
        self._validate_inspection(intent, inspection)
        finalized = self.storage.promote_verified(intent, inspection)
        self._validate_finalized(intent, inspection, finalized)
        asset = self.repository.complete_upload(
            intent=intent,
            inspection=inspection,
            finalized=finalized,
            completed_at=now,
        )
        self._validate_asset(
            asset,
            expected_owner=owner.user_id,
            expected=finalized,
            expected_kind=intent.kind,
            expected_filename=intent.filename,
        )
        return asset

    def _validate_inspection(
        self, intent: UploadIntent, inspection: StagedMediaInspection
    ) -> None:
        if not isinstance(inspection, StagedMediaInspection) or not inspection.verified:
            raise MediaInspectionRejected("存储适配器未确认真实媒体")
        _validate_object_ref(
            inspection.object_ref,
            deployment=self.deployment,
            zone=STORAGE_ZONE_STAGING,
            owner_user_id=intent.owner_user_id,
        )
        if inspection.object_ref != intent.staging_object:
            raise MediaIntegrityMismatch("检测对象与上传意图不一致")
        try:
            detected_kind = _normalize_kind(inspection.detected_kind)
        except InvalidMediaRequest as exc:
            raise MediaTypeRejected("存储适配器返回了未知媒体类别") from exc
        detected_type = _normalize_content_type(inspection.detected_content_type)
        if detected_kind != intent.kind or not _content_type_allowed(
            detected_kind, detected_type
        ):
            raise MediaTypeRejected("真实媒体类型与声明类别不一致")
        if (
            detected_type != intent.declared_content_type
            and intent.declared_content_type != "application/octet-stream"
            and intent.kind != MEDIA_KIND_FILE
        ):
            raise MediaTypeRejected("真实 content_type 与声明不一致")
        try:
            actual_size = _positive_int(
                inspection.size_bytes, "inspection.size_bytes"
            )
        except InvalidMediaRequest as exc:
            raise MediaIntegrityMismatch("真实媒体大小不合法") from exc
        _validate_size(detected_kind, actual_size)
        try:
            actual_digest = _normalize_sha256(inspection.sha256)
        except InvalidMediaRequest as exc:
            raise MediaIntegrityMismatch("真实媒体 sha256 不合法") from exc
        if actual_size != intent.expected_size_bytes or not hmac.compare_digest(
            actual_digest, intent.expected_sha256
        ):
            raise MediaIntegrityMismatch("真实媒体大小或 sha256 不一致")
        self._validate_dimensions_and_duration(intent.kind, inspection)

    @staticmethod
    def _validate_dimensions_and_duration(
        kind: str, inspection: StagedMediaInspection
    ) -> None:
        duration = inspection.duration_seconds
        if kind == MEDIA_KIND_IMAGE:
            if (
                isinstance(inspection.width, bool)
                or isinstance(inspection.height, bool)
                or not isinstance(inspection.width, int)
                or not isinstance(inspection.height, int)
                or inspection.width <= 0
                or inspection.height <= 0
            ):
                raise MediaInspectionRejected("图片必须通过尺寸解码检测")
            return
        if kind in {MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO}:
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or float(duration) <= 0
            ):
                raise MediaInspectionRejected("音视频必须通过容器时长检测")
            if kind == MEDIA_KIND_AUDIO and float(duration) > MAX_AUDIO_DURATION_SECONDS:
                raise MediaInspectionRejected("语音长度超过 60 秒")

    def _validate_finalized(
        self,
        intent: UploadIntent,
        inspection: StagedMediaInspection,
        finalized: FinalizedMediaObject,
    ) -> None:
        if not isinstance(finalized, FinalizedMediaObject):
            raise MediaContractViolation("存储适配器未返回 finalized object")
        _validate_object_ref(
            finalized.object_ref,
            deployment=self.deployment,
            zone=STORAGE_ZONE_PRIVATE,
            owner_user_id=intent.owner_user_id,
        )
        staging = intent.staging_object
        private = finalized.object_ref
        if (
            staging.bucket == private.bucket
            and staging.namespace == private.namespace
        ) or staging == private:
            raise MediaContractViolation("staging 与 private 必须使用隔离命名空间")
        if (
            finalized.content_type != _normalize_content_type(
                inspection.detected_content_type
            )
            or finalized.size_bytes != inspection.size_bytes
            or not hmac.compare_digest(
                _normalize_sha256(finalized.sha256),
                _normalize_sha256(inspection.sha256),
            )
            or finalized.duration_seconds != inspection.duration_seconds
            or finalized.width != inspection.width
            or finalized.height != inspection.height
        ):
            raise MediaIntegrityMismatch("private 对象与已检测 staging 对象不一致")

    def _validate_asset(
        self,
        asset: MediaAsset,
        *,
        expected_owner: uuid.UUID,
        expected: FinalizedMediaObject | None = None,
        expected_kind: str | None = None,
        expected_filename: str | None = None,
    ) -> None:
        if not isinstance(asset, MediaAsset):
            raise MediaContractViolation("媒体持久层未返回 asset")
        if (
            asset.owner_user_id != expected_owner
            or asset.deployment != self.deployment
            or asset.status != ASSET_STATUS_AVAILABLE
        ):
            raise MediaAssetUnavailable("媒体 asset 不可用")
        _validate_object_ref(
            asset.object_ref,
            deployment=self.deployment,
            zone=STORAGE_ZONE_PRIVATE,
            owner_user_id=expected_owner,
        )
        try:
            normalized_kind = _normalize_kind(asset.kind)
            normalized_type = _normalize_content_type(asset.content_type)
            normalized_size = _positive_int(asset.size_bytes, "asset.size_bytes")
            normalized_digest = _normalize_sha256(asset.sha256)
            normalized_filename = _normalize_filename(asset.filename)
        except (InvalidMediaRequest, MediaTypeRejected) as exc:
            raise MediaContractViolation("asset 元数据不合法") from exc
        if (
            normalized_kind != asset.kind
            or normalized_type != asset.content_type
            or normalized_digest != asset.sha256
            or normalized_filename != asset.filename
            or not _content_type_allowed(normalized_kind, normalized_type)
        ):
            raise MediaContractViolation("asset 类型元数据不一致")
        _validate_size(normalized_kind, normalized_size)
        self._validate_dimensions_and_duration(
            normalized_kind,
            StagedMediaInspection(
                object_ref=asset.object_ref,
                verified=True,
                detected_kind=normalized_kind,
                detected_content_type=normalized_type,
                size_bytes=normalized_size,
                sha256=normalized_digest,
                duration_seconds=asset.duration_seconds,
                width=asset.width,
                height=asset.height,
            ),
        )
        if expected_kind is not None and normalized_kind != expected_kind:
            raise MediaContractViolation("asset 改变了上传媒体类别")
        if expected_filename is not None and normalized_filename != expected_filename:
            raise MediaContractViolation("asset 改变了上传文件名")
        if expected is not None and (
            asset.object_ref != expected.object_ref
            or asset.content_type != expected.content_type
            or asset.size_bytes != expected.size_bytes
            or not hmac.compare_digest(asset.sha256, expected.sha256)
            or asset.duration_seconds != expected.duration_seconds
            or asset.width != expected.width
            or asset.height != expected.height
        ):
            raise MediaContractViolation("asset 与 finalized object 不一致")
        if asset.expires_at is not None:
            _aware_time(asset.expires_at, "asset.expires_at")

    def send_attachment(
        self,
        *,
        principal: MediaPrincipal,
        peer_upstream_uid: object,
        client_message_id: object,
        asset_id: object,
        flash: bool = False,
    ) -> AttachmentDeliveryResult:
        sender = self._principal_account(principal)
        peer_uid = _normalize_upstream_uid(peer_upstream_uid)
        message_key = _normalize_client_message_id(client_message_id)
        if not peer_uid or peer_uid == sender.upstream_uid:
            raise InvalidMediaRequest("聊天对象 UID 不合法")
        if not message_key:
            raise InvalidMediaRequest("client_message_id 不合法")
        if not isinstance(flash, bool):
            raise InvalidMediaRequest("flash 必须是布尔值")
        recipient = self.policy.resolve_active_peer(
            peer_uid, provider=principal.account_provider
        )
        if recipient is None:
            raise MediaPeerUnavailable("对方尚未迁移到 Web")
        if recipient.user_id == sender.user_id:
            raise InvalidMediaRequest("不能向自己发送媒体")
        if (
            recipient.upstream_uid != peer_uid
            or recipient.account_provider != principal.account_provider
        ):
            raise MediaContractViolation("聊天对象解析结果未绑定请求 UID")
        self._deny_if_blocked(sender.user_id, recipient.user_id)
        if not self.policy.can_send_private_message(sender, recipient):
            raise MediaThreadForbidden("当前账号无权向该用户发送私聊媒体")
        thread = _validate_direct_thread(
            self.policy.resolve_or_create_direct_thread(sender, recipient),
            sender.user_id,
            recipient.user_id,
        )
        asset = self.repository.get_media_asset(_uuid(asset_id, "asset_id"))
        if asset is None or asset.owner_user_id != sender.user_id:
            raise MediaAssetUnavailable("媒体不存在或不属于发送者")
        self._validate_asset(asset, expected_owner=sender.user_id)
        now = self._now()
        if asset.expires_at is not None and now >= asset.expires_at:
            raise MediaAssetUnavailable("媒体已过期")
        if flash and asset.kind != MEDIA_KIND_IMAGE:
            raise InvalidMediaRequest("闪照只能使用图片 asset")
        payload = MediaAttachmentPayload(
            asset_id=asset.id,
            kind=asset.kind,
            filename=asset.filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            sha256=asset.sha256,
            duration_seconds=asset.duration_seconds,
            width=asset.width,
            height=asset.height,
            flash=flash,
            flash_display_seconds=FLASH_DISPLAY_SECONDS if flash else None,
        )
        result = self.repository.store_attachment(
            sender=sender,
            recipient=recipient,
            thread=thread,
            asset=asset,
            client_message_id=message_key,
            payload=payload,
            sent_at=now,
        )
        self._validate_delivery_result(
            result,
            sender=sender,
            recipient=recipient,
            thread=thread,
            payload=payload,
            client_message_id=message_key,
        )
        return result

    @staticmethod
    def _validate_delivery_result(
        result: AttachmentDeliveryResult,
        *,
        sender: MediaAccount,
        recipient: MediaAccount,
        thread: DirectMediaThread,
        payload: MediaAttachmentPayload,
        client_message_id: str,
    ) -> None:
        if not isinstance(result, AttachmentDeliveryResult):
            raise MediaContractViolation("媒体持久层未返回本地投递结果")
        attachment = result.attachment
        mirror = result.tim_mirror
        if (
            attachment.thread_id != thread.id
            or attachment.sender_user_id != sender.user_id
            or attachment.recipient_user_id != recipient.user_id
            or attachment.client_message_id != client_message_id
            or attachment.payload != payload
            or attachment.status != ATTACHMENT_STATUS_SENT
            or mirror.message_id != attachment.message_id
            or mirror.attachment_id != attachment.id
            or mirror.payload != payload
            or mirror.from_upstream_uid != sender.upstream_uid
            or mirror.to_upstream_uid != recipient.upstream_uid
            or mirror.client_message_id != client_message_id
        ):
            raise MediaContractViolation("本地附件或 TIM outbox intent 未绑定当前请求")

    def request_access(
        self,
        *,
        principal: MediaPrincipal,
        attachment_id: object,
    ) -> MediaAccessGrant:
        actor = self._principal_account(principal)
        attachment = self._authorized_attachment(
            actor, _uuid(attachment_id, "attachment_id"), check_block=True
        )
        if attachment.payload.flash:
            raise FlashClaimRequired("闪照必须通过一次性领取访问")
        asset = self._attachment_asset(attachment)
        now = self._now()
        return self._issue_access(
            attachment=attachment,
            asset=asset,
            now=now,
            expires_at=now + timedelta(seconds=NORMAL_ACCESS_TTL_SECONDS),
        )

    def revoke_attachment(
        self,
        *,
        principal: MediaPrincipal,
        attachment_id: object,
    ) -> MediaRevokeResult:
        actor = self._principal_account(principal)
        attachment = self._authorized_attachment(
            actor,
            _uuid(attachment_id, "attachment_id"),
            check_block=False,
            allow_revoked=True,
        )
        if attachment.sender_user_id != actor.user_id:
            raise MediaRevocationDenied("只有发送者可以撤回媒体")
        now = self._now()
        deadline = _aware_time(attachment.sent_at, "attachment.sent_at") + timedelta(
            seconds=REVOKE_WINDOW_SECONDS
        )
        if attachment.status != ATTACHMENT_STATUS_REVOKED and now > deadline:
            raise MediaRevocationExpired("媒体撤回期限为 2 分钟")
        result = self.repository.revoke_attachment(
            attachment=attachment,
            actor=actor,
            revoked_at=now,
        )
        if (
            not isinstance(result, MediaRevokeResult)
            or result.attachment.id != attachment.id
            or result.attachment.status != ATTACHMENT_STATUS_REVOKED
            or result.attachment.revoked_at is None
        ):
            raise MediaContractViolation("撤回结果未绑定本地附件")
        if result.tim_mirror is not None and (
            result.tim_mirror.attachment_id != attachment.id
            or result.tim_mirror.message_id != attachment.message_id
        ):
            raise MediaContractViolation("TIM 撤回 intent 未绑定本地附件")
        return result

    def claim_flash(
        self,
        *,
        principal: MediaPrincipal,
        attachment_id: object,
    ) -> FlashClaimResult:
        claimant = self._principal_account(principal)
        attachment = self._authorized_attachment(
            claimant,
            _uuid(attachment_id, "attachment_id"),
            check_block=True,
        )
        if not attachment.payload.flash or attachment.payload.kind != MEDIA_KIND_IMAGE:
            raise FlashClaimDenied("该附件不是闪照")
        if attachment.recipient_user_id != claimant.user_id:
            raise FlashClaimDenied("只有闪照接收者可以领取")
        asset = self._attachment_asset(attachment)
        now = self._now()
        display_until = now + timedelta(seconds=FLASH_DISPLAY_SECONDS)
        claim = self.repository.claim_flash_once(
            attachment=attachment,
            claimant=claimant,
            claimed_at=now,
            display_until=display_until,
        )
        if (
            claim.attachment_id != attachment.id
            or claim.claimant_user_id != claimant.user_id
            or claim.claimed_at != now
            or claim.display_until != display_until
        ):
            raise MediaContractViolation("闪照领取结果未绑定当前请求")
        access = self._issue_access(
            attachment=attachment,
            asset=asset,
            now=now,
            expires_at=display_until,
            display_until=display_until,
        )
        return FlashClaimResult(claim=claim, access=access)

    def _authorized_attachment(
        self,
        actor: MediaAccount,
        attachment_id: uuid.UUID,
        *,
        check_block: bool,
        allow_revoked: bool = False,
    ) -> MediaAttachment:
        attachment = self.repository.get_media_attachment(attachment_id)
        if attachment is None:
            raise MediaAccessDenied("媒体附件不存在")
        participants = frozenset(
            {attachment.sender_user_id, attachment.recipient_user_id}
        )
        if actor.user_id not in participants:
            raise MediaAccessDenied("当前用户不是附件参与者")
        _aware_time(attachment.sent_at, "attachment.sent_at")
        if attachment.sender_user_id == attachment.recipient_user_id:
            raise MediaContractViolation("私聊附件的发送者与接收者不能相同")
        if (
            attachment.status == ATTACHMENT_STATUS_SENT
            and attachment.revoked_at is not None
        ) or (
            attachment.status == ATTACHMENT_STATUS_REVOKED
            and attachment.revoked_at is None
        ):
            raise MediaContractViolation("附件状态与撤回时间不一致")
        _validate_direct_thread(
            self.policy.resolve_thread(attachment.thread_id),
            attachment.sender_user_id,
            attachment.recipient_user_id,
        )
        if check_block:
            self._deny_if_blocked(
                attachment.sender_user_id, attachment.recipient_user_id
            )
        if not allow_revoked and (
            attachment.status == ATTACHMENT_STATUS_REVOKED
            or attachment.revoked_at is not None
        ):
            raise MediaAccessDenied("媒体附件已撤回")
        if attachment.status not in {
            ATTACHMENT_STATUS_SENT,
            ATTACHMENT_STATUS_REVOKED if allow_revoked else ATTACHMENT_STATUS_SENT,
        }:
            raise MediaAccessDenied("媒体附件状态不可用")
        return attachment

    def _attachment_asset(self, attachment: MediaAttachment) -> MediaAsset:
        asset = self.repository.get_media_asset(attachment.payload.asset_id)
        if asset is None or asset.owner_user_id != attachment.sender_user_id:
            raise MediaAssetUnavailable("媒体 asset 不存在或绑定错误")
        self._validate_asset(asset, expected_owner=attachment.sender_user_id)
        expected_payload = MediaAttachmentPayload(
            asset_id=asset.id,
            kind=asset.kind,
            filename=asset.filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            sha256=asset.sha256,
            duration_seconds=asset.duration_seconds,
            width=asset.width,
            height=asset.height,
            flash=attachment.payload.flash,
            flash_display_seconds=(
                FLASH_DISPLAY_SECONDS if attachment.payload.flash else None
            ),
        )
        if attachment.payload != expected_payload:
            raise MediaContractViolation("附件 payload 与 asset 不一致")
        now = self._now()
        if asset.expires_at is not None and now >= asset.expires_at:
            raise MediaAssetUnavailable("媒体已过期")
        return asset

    def _issue_access(
        self,
        *,
        attachment: MediaAttachment,
        asset: MediaAsset,
        now: datetime,
        expires_at: datetime,
        display_until: datetime | None = None,
    ) -> MediaAccessGrant:
        read = self.storage.issue_private_read(asset, expires_at=expires_at)
        self._validate_presigned_read(read, asset=asset, now=now, maximum=expires_at)
        return MediaAccessGrant(
            attachment_id=attachment.id,
            asset_id=asset.id,
            url=read.url,
            expires_at=read.expires_at,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            display_until=display_until,
        )

    @staticmethod
    def _validate_presigned_read(
        read: PresignedMediaRead,
        *,
        asset: MediaAsset,
        now: datetime,
        maximum: datetime,
    ) -> None:
        if not isinstance(read, PresignedMediaRead):
            raise MediaContractViolation("存储适配器未返回临时读取授权")
        read_expiry = _aware_time(read.expires_at, "read.expires_at")
        if (
            read.object_ref != asset.object_ref
            or read_expiry <= now
            or read_expiry > maximum
            or not read.url.strip()
            or any(ord(char) < 32 for char in read.url)
        ):
            raise MediaContractViolation("临时读取授权越过对象或时间边界")

    def _deny_if_blocked(
        self, left_user_id: uuid.UUID, right_user_id: uuid.UUID
    ) -> None:
        if self.policy.is_blocked_between(left_user_id, right_user_id):
            raise MediaBlocked("任一方黑名单关系禁止媒体操作")
