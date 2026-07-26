"""Owner-bound Web-native media references for profiles and social posts.

The caller owns the SQLAlchemy transaction.  This repository flushes changes
so uniqueness and foreign-key failures surface inside the caller's atomic
profile/post mutation, but it never commits on its own.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bbw_prod.models import (
    MediaAsset as MediaAssetRow,
    MediaAssetReference as MediaAssetReferenceRow,
    MediaAttachment as MediaAttachmentRow,
    SocialPost,
    User,
    utcnow,
)

from .contracts import (
    ASSET_STATUS_AVAILABLE,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
)


PROFILE_RESOURCE = "profile"
SOCIAL_POST_RESOURCE = "social_post"
PROFILE_AVATAR_SLOT = "avatar"
SOCIAL_VIDEO_SLOT = "video"
SOCIAL_COVER_SLOT = "cover"
_PICTURE_SLOT = re.compile(r"pictures\[(0|[1-9][0-9]*)\]\Z")
_NATIVE_CONTENT_PATH = re.compile(
    r"/api/media/native/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/content\Z"
)


class MediaAssetReferenceError(RuntimeError):
    """Base error for canonical profile/post media binding failures."""


class MediaAssetReferenceTargetUnavailable(MediaAssetReferenceError):
    """The profile or social post does not belong to the requested owner."""


class MediaAssetReferenceAssetUnavailable(MediaAssetReferenceError):
    """The asset is missing, unavailable, attached to chat, or wrong-kind."""


class MediaAssetReferenceConflict(MediaAssetReferenceError):
    """The asset already has another current profile/post reference."""


class MediaAssetReferenceSlotInvalid(MediaAssetReferenceError):
    """The requested resource slot is not part of the canonical schema."""


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise MediaAssetReferenceSlotInvalid(f"{field} 不是有效 UUID") from exc


def native_media_content_path(asset_id: uuid.UUID | str) -> str:
    """Return the URL-free, same-origin path for a current native reference."""

    normalized = _uuid(asset_id, "asset_id")
    return f"/api/media/native/{normalized}/content"


def native_media_content_asset_id(value: object) -> uuid.UUID | None:
    """Parse only the exact canonical same-origin media content path."""

    match = _NATIVE_CONTENT_PATH.fullmatch(str(value or "").strip())
    if match is None:
        return None
    try:
        return uuid.UUID(match.group(1))
    except ValueError:
        return None


def _expected_kind(resource_type: str, slot: str) -> str:
    if resource_type == PROFILE_RESOURCE and slot == PROFILE_AVATAR_SLOT:
        return MEDIA_KIND_IMAGE
    if resource_type == SOCIAL_POST_RESOURCE:
        if slot == SOCIAL_VIDEO_SLOT:
            return MEDIA_KIND_VIDEO
        if slot == SOCIAL_COVER_SLOT or _PICTURE_SLOT.fullmatch(slot):
            return MEDIA_KIND_IMAGE
    raise MediaAssetReferenceSlotInvalid("媒体引用 slot 不合法")


def _resource_id(row: MediaAssetReferenceRow) -> uuid.UUID:
    if row.resource_type == PROFILE_RESOURCE and row.profile_user_id is not None:
        return row.profile_user_id
    if row.resource_type == SOCIAL_POST_RESOURCE and row.social_post_id is not None:
        return row.social_post_id
    raise MediaAssetReferenceError("媒体引用目标不完整")


@dataclass(frozen=True, slots=True)
class BoundMediaAssetReference:
    reference_id: uuid.UUID
    asset_id: uuid.UUID
    owner_user_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    slot: str
    kind: str
    content_type: str
    size_bytes: int
    private_bucket: str
    private_object_key: str

    @property
    def content_path(self) -> str:
        return native_media_content_path(self.asset_id)


@dataclass(frozen=True, slots=True)
class ValidatedMediaAsset:
    asset_id: uuid.UUID
    owner_user_id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int

    @property
    def content_path(self) -> str:
        return native_media_content_path(self.asset_id)


def _as_validated_asset(asset: MediaAssetRow) -> ValidatedMediaAsset:
    return ValidatedMediaAsset(
        asset_id=asset.id,
        owner_user_id=asset.owner_user_id,
        kind=asset.kind,
        content_type=asset.content_type,
        size_bytes=int(asset.size_bytes),
    )


def _as_bound_reference(
    row: MediaAssetReferenceRow,
    asset: MediaAssetRow,
) -> BoundMediaAssetReference:
    if row.asset_id != asset.id or row.owner_user_id != asset.owner_user_id:
        raise MediaAssetReferenceError("媒体引用与 asset owner 绑定不一致")
    return BoundMediaAssetReference(
        reference_id=row.id,
        asset_id=asset.id,
        owner_user_id=asset.owner_user_id,
        resource_type=row.resource_type,
        resource_id=_resource_id(row),
        slot=row.slot,
        kind=asset.kind,
        content_type=asset.content_type,
        size_bytes=int(asset.size_bytes),
        private_bucket=asset.private_bucket,
        private_object_key=asset.private_object_key,
    )


class SqlAlchemyMediaAssetReferenceRepository:
    """Atomic current-reference binding over Web-native ``MediaAsset`` rows."""

    def __init__(
        self,
        db: Session,
        *,
        deployment: str | None = None,
        private_bucket: str | None = None,
    ) -> None:
        self.db = db
        self.deployment = str(deployment or "").strip()
        self.private_bucket = str(private_bucket or "").strip()

    def _lock_profile(self, owner_user_id: uuid.UUID, *, active: bool) -> User:
        conditions: list[Any] = [User.id == owner_user_id]
        if active:
            conditions.extend((User.status == "active", User.disabled_at.is_(None)))
        row = self.db.scalar(
            select(User).where(*conditions).with_for_update()
        )
        if row is None:
            raise MediaAssetReferenceTargetUnavailable("资料账号不存在或不可用")
        return row

    def _lock_social_post(
        self,
        owner_user_id: uuid.UUID,
        social_post_id: uuid.UUID,
        *,
        published: bool,
    ) -> SocialPost:
        conditions: list[Any] = [
            SocialPost.id == social_post_id,
            SocialPost.author_user_id == owner_user_id,
        ]
        if published:
            conditions.append(SocialPost.status == "published")
        row = self.db.scalar(
            select(SocialPost).where(*conditions).with_for_update()
        )
        if row is None:
            raise MediaAssetReferenceTargetUnavailable("动态不存在或不属于当前账号")
        return row

    def _require_bindable_assets(
        self,
        *,
        owner_user_id: uuid.UUID,
        assets_by_slot: Mapping[str, uuid.UUID],
        resource_type: str,
    ) -> dict[uuid.UUID, MediaAssetRow]:
        asset_ids = sorted(set(assets_by_slot.values()), key=str)
        if len(asset_ids) != len(assets_by_slot):
            raise MediaAssetReferenceConflict("同一个 asset 不能绑定多个当前 slot")
        by_id = self._lock_available_unattached_assets(
            owner_user_id=owner_user_id,
            asset_ids=asset_ids,
        )
        self._validate_asset_kinds(
            assets_by_slot=assets_by_slot,
            resource_type=resource_type,
            assets=by_id,
        )
        return by_id

    @staticmethod
    def _validate_asset_kinds(
        *,
        assets_by_slot: Mapping[str, uuid.UUID],
        resource_type: str,
        assets: Mapping[uuid.UUID, MediaAssetRow],
    ) -> None:
        for slot, asset_id in assets_by_slot.items():
            expected_kind = _expected_kind(resource_type, slot)
            if assets[asset_id].kind != expected_kind:
                raise MediaAssetReferenceAssetUnavailable(
                    f"slot {slot} 需要 {expected_kind} asset"
                )

    def _lock_available_unattached_assets(
        self,
        *,
        owner_user_id: uuid.UUID,
        asset_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, MediaAssetRow]:
        if not asset_ids:
            return {}
        by_id = self._lock_owned_assets(
            owner_user_id=owner_user_id,
            asset_ids=asset_ids,
        )
        self._validate_available_unattached_assets(
            owner_user_id=owner_user_id,
            asset_ids=asset_ids,
            assets=by_id,
        )
        return by_id

    def _lock_owned_assets(
        self,
        *,
        owner_user_id: uuid.UUID,
        asset_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, MediaAssetRow]:
        if not asset_ids:
            return {}
        rows = list(
            self.db.scalars(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.id.in_(asset_ids),
                    MediaAssetRow.owner_user_id == owner_user_id,
                )
                .order_by(MediaAssetRow.id)
                .with_for_update()
            )
        )
        return {row.id: row for row in rows}

    def _read_owned_assets(
        self,
        *,
        owner_user_id: uuid.UUID,
        asset_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, MediaAssetRow]:
        if not asset_ids:
            return {}
        rows = list(
            self.db.scalars(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.id.in_(asset_ids),
                    MediaAssetRow.owner_user_id == owner_user_id,
                )
                .order_by(MediaAssetRow.id)
            )
        )
        return {row.id: row for row in rows}

    def _validate_available_unattached_assets(
        self,
        *,
        owner_user_id: uuid.UUID,
        asset_ids: Sequence[uuid.UUID],
        assets: Mapping[uuid.UUID, MediaAssetRow],
    ) -> None:
        now = utcnow()
        if set(assets) != set(asset_ids) or any(
            asset.status != ASSET_STATUS_AVAILABLE
            or (
                getattr(asset, "expires_at", None) is not None
                and asset.expires_at <= now
            )
            for asset in assets.values()
        ):
            raise MediaAssetReferenceAssetUnavailable(
                "媒体 asset 不存在、不可用或不属于当前账号"
            )
        attached_ids = set(
            self.db.scalars(
                select(MediaAttachmentRow.asset_id).where(
                    MediaAttachmentRow.asset_id.in_(asset_ids)
                )
            )
        )
        if attached_ids:
            raise MediaAssetReferenceAssetUnavailable(
                "私聊附件 asset 不能绑定为资料或动态媒体"
            )
        for asset in assets.values():
            self._validate_asset_storage(asset, owner_user_id=owner_user_id)

    def _validate_asset_storage(
        self,
        asset: MediaAssetRow,
        *,
        owner_user_id: uuid.UUID,
    ) -> None:
        deployment = self.deployment or str(asset.deployment or "").strip()
        expected_namespace = f"{deployment}/web-media-private"
        bucket = str(asset.private_bucket or "").strip()
        namespace = str(asset.private_namespace or "").strip()
        object_key = str(asset.private_object_key or "").strip()
        owner_prefix = f"{expected_namespace}/{owner_user_id}/"
        values = (deployment, bucket, namespace, object_key)
        if (
            not all(values)
            or (self.deployment and asset.deployment != self.deployment)
            or (self.private_bucket and bucket != self.private_bucket)
            or namespace != expected_namespace
            or not object_key.startswith(owner_prefix)
            or object_key.startswith(("/", "\\"))
            or any(
                "://" in value
                or "?" in value
                or "#" in value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in values
            )
            or any(part in {"", ".", ".."} for part in object_key.replace("\\", "/").split("/"))
        ):
            raise MediaAssetReferenceAssetUnavailable(
                "媒体 asset 越过 deployment、R2 bucket 或 private namespace 边界"
            )

    def _references_for_assets(
        self, asset_ids: Sequence[uuid.UUID]
    ) -> list[MediaAssetReferenceRow]:
        if not asset_ids:
            return []
        return list(
            self.db.scalars(
                select(MediaAssetReferenceRow)
                .where(MediaAssetReferenceRow.asset_id.in_(asset_ids))
                .order_by(MediaAssetReferenceRow.asset_id)
                .with_for_update()
            )
        )

    def get_current_reference(
        self, asset_id: uuid.UUID | str
    ) -> BoundMediaAssetReference | None:
        """Resolve an available asset only through its current canonical binding."""

        normalized = _uuid(asset_id, "asset_id")
        result = self.db.execute(
            select(MediaAssetReferenceRow, MediaAssetRow)
            .join(
                MediaAssetRow,
                (MediaAssetRow.id == MediaAssetReferenceRow.asset_id)
                & (MediaAssetRow.owner_user_id == MediaAssetReferenceRow.owner_user_id),
            )
            .where(
                MediaAssetReferenceRow.asset_id == normalized,
                MediaAssetRow.status == ASSET_STATUS_AVAILABLE,
            )
            .limit(1)
        ).first()
        if result is None:
            return None
        asset = result[1]
        expires_at = getattr(asset, "expires_at", None)
        if expires_at is not None and expires_at <= utcnow():
            return None
        try:
            self._validate_asset_storage(
                asset,
                owner_user_id=asset.owner_user_id,
            )
        except MediaAssetReferenceAssetUnavailable:
            return None
        return _as_bound_reference(result[0], result[1])

    def validate_profile_avatar_asset(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        asset_id: uuid.UUID | str,
    ) -> ValidatedMediaAsset:
        """Lock and validate an avatar asset before the profile mutation."""

        owner = _uuid(owner_user_id, "owner_user_id")
        normalized_asset = _uuid(asset_id, "asset_id")
        self._lock_profile(owner, active=True)
        assets = self._require_bindable_assets(
            owner_user_id=owner,
            assets_by_slot={PROFILE_AVATAR_SLOT: normalized_asset},
            resource_type=PROFILE_RESOURCE,
        )
        for reference in self._references_for_assets([normalized_asset]):
            if not (
                reference.resource_type == PROFILE_RESOURCE
                and reference.profile_user_id == owner
                and reference.slot == PROFILE_AVATAR_SLOT
            ):
                raise MediaAssetReferenceConflict(
                    "媒体 asset 已绑定其他资料或动态资源"
                )
        return _as_validated_asset(assets[normalized_asset])

    def validate_social_post_assets(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        assets_by_slot: Mapping[str, uuid.UUID | str],
    ) -> dict[str, ValidatedMediaAsset]:
        """Lock and validate new-post assets before the SocialPost row exists."""

        if not isinstance(assets_by_slot, Mapping):
            raise MediaAssetReferenceSlotInvalid("assets_by_slot 必须是 slot 到 asset UUID 的映射")
        owner = _uuid(owner_user_id, "owner_user_id")
        normalized: dict[str, uuid.UUID] = {}
        for raw_slot, raw_asset_id in assets_by_slot.items():
            slot = str(raw_slot)
            _expected_kind(SOCIAL_POST_RESOURCE, slot)
            normalized[slot] = _uuid(raw_asset_id, f"assets_by_slot[{slot}]")
        self._lock_profile(owner, active=True)
        assets = self._require_bindable_assets(
            owner_user_id=owner,
            assets_by_slot=normalized,
            resource_type=SOCIAL_POST_RESOURCE,
        )
        if self._references_for_assets(list(normalized.values())):
            raise MediaAssetReferenceConflict(
                "媒体 asset 已绑定其他资料或动态资源"
            )
        return {
            slot: _as_validated_asset(assets[asset_id_value])
            for slot, asset_id_value in sorted(normalized.items())
        }

    def validate_social_post_asset_ids(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        asset_ids: Sequence[uuid.UUID | str],
    ) -> dict[str, ValidatedMediaAsset]:
        """Validate the publish API's ordered IDs and derive canonical slots."""

        if isinstance(asset_ids, (str, bytes)) or not isinstance(asset_ids, Sequence):
            raise MediaAssetReferenceSlotInvalid("media_asset_ids 必须是 UUID 数组")
        if len(asset_ids) > 9:
            raise MediaAssetReferenceSlotInvalid("动态图片最多 9 张")
        owner = _uuid(owner_user_id, "owner_user_id")
        normalized = [_uuid(value, "media_asset_ids") for value in asset_ids]
        if len(set(normalized)) != len(normalized):
            raise MediaAssetReferenceConflict("media_asset_ids 不能重复")
        self._lock_profile(owner, active=True)
        assets = self._lock_available_unattached_assets(
            owner_user_id=owner,
            asset_ids=sorted(normalized, key=str),
        )
        if not normalized:
            return {}
        kinds = [assets[asset_id].kind for asset_id in normalized]
        if kinds == [MEDIA_KIND_VIDEO]:
            slots = {SOCIAL_VIDEO_SLOT: normalized[0]}
        elif all(kind == MEDIA_KIND_IMAGE for kind in kinds):
            slots = {
                f"pictures[{index}]": asset_id
                for index, asset_id in enumerate(normalized)
            }
        else:
            raise MediaAssetReferenceAssetUnavailable(
                "动态媒体仅支持最多 9 张图片或单个视频，且不能混用"
            )
        return {
            slot: _as_validated_asset(assets[asset_id])
            for slot, asset_id in slots.items()
        }

    def preview_social_post_asset_ids(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        asset_ids: Sequence[uuid.UUID | str],
    ) -> dict[str, ValidatedMediaAsset]:
        """Read media shape before post creation; final binding revalidates under locks.

        This deliberately takes no asset/reference locks.  The caller must invoke
        ``bind_social_post_assets`` in the same transaction after the canonical
        ``SocialPost`` exists; a failed final validation rolls that post write back.
        Avoiding an asset lock before the post lock keeps every mutation on the
        global target -> asset -> reference lock order.
        """

        if isinstance(asset_ids, (str, bytes)) or not isinstance(asset_ids, Sequence):
            raise MediaAssetReferenceSlotInvalid("media_asset_ids 必须是 UUID 数组")
        if len(asset_ids) > 9:
            raise MediaAssetReferenceSlotInvalid("动态图片最多 9 张")
        owner = _uuid(owner_user_id, "owner_user_id")
        normalized = [_uuid(value, "media_asset_ids") for value in asset_ids]
        if len(set(normalized)) != len(normalized):
            raise MediaAssetReferenceConflict("media_asset_ids 不能重复")
        ordered_ids = sorted(normalized, key=str)
        assets = self._read_owned_assets(
            owner_user_id=owner,
            asset_ids=ordered_ids,
        )
        self._validate_available_unattached_assets(
            owner_user_id=owner,
            asset_ids=ordered_ids,
            assets=assets,
        )
        if not normalized:
            return {}
        kinds = [assets[asset_id].kind for asset_id in normalized]
        if kinds == [MEDIA_KIND_VIDEO]:
            slots = {SOCIAL_VIDEO_SLOT: normalized[0]}
        elif all(kind == MEDIA_KIND_IMAGE for kind in kinds):
            slots = {
                f"pictures[{index}]": asset_id
                for index, asset_id in enumerate(normalized)
            }
        else:
            raise MediaAssetReferenceAssetUnavailable(
                "动态媒体仅支持最多 9 张图片或单个视频，且不能混用"
            )
        return {
            slot: _as_validated_asset(assets[asset_id])
            for slot, asset_id in slots.items()
        }

    def replace_profile_avatar(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        asset_id: uuid.UUID | str,
    ) -> BoundMediaAssetReference:
        """Atomically retain or replace the owner's single current avatar."""

        owner = _uuid(owner_user_id, "owner_user_id")
        normalized_asset = _uuid(asset_id, "asset_id")
        self._lock_profile(owner, active=True)
        current_snapshot = list(
            self.db.scalars(
                select(MediaAssetReferenceRow)
                .where(
                    MediaAssetReferenceRow.resource_type == PROFILE_RESOURCE,
                    MediaAssetReferenceRow.profile_user_id == owner,
                    MediaAssetReferenceRow.slot == PROFILE_AVATAR_SLOT,
                )
            )
        )
        union_ids = sorted(
            {normalized_asset, *(row.asset_id for row in current_snapshot)},
            key=str,
        )
        locked_assets = self._lock_owned_assets(
            owner_user_id=owner,
            asset_ids=union_ids,
        )
        assets = {normalized_asset: locked_assets.get(normalized_asset)}
        if assets[normalized_asset] is None:
            assets = {}
        self._validate_available_unattached_assets(
            owner_user_id=owner,
            asset_ids=[normalized_asset],
            assets=assets,
        )
        self._validate_asset_kinds(
            assets_by_slot={PROFILE_AVATAR_SLOT: normalized_asset},
            resource_type=PROFILE_RESOURCE,
            assets=assets,
        )
        locked_references = self._references_for_assets(union_ids)
        current = [
            reference
            for reference in locked_references
            if reference.resource_type == PROFILE_RESOURCE
            and reference.profile_user_id == owner
            and reference.slot == PROFILE_AVATAR_SLOT
        ]
        conflicts = [
            reference
            for reference in locked_references
            if reference.asset_id == normalized_asset
        ]
        for reference in conflicts:
            if not (
                reference.resource_type == PROFILE_RESOURCE
                and reference.profile_user_id == owner
                and reference.slot == PROFILE_AVATAR_SLOT
            ):
                raise MediaAssetReferenceConflict(
                    "媒体 asset 已绑定其他资料或动态资源"
                )
        if len(current) == 1 and current[0].asset_id == normalized_asset:
            assets[normalized_asset].expires_at = None
            self.db.flush()
            return _as_bound_reference(current[0], assets[normalized_asset])
        for reference in current:
            self.db.delete(reference)
        if current:
            self.db.flush()
        reference = MediaAssetReferenceRow(
            asset_id=normalized_asset,
            owner_user_id=owner,
            resource_type=PROFILE_RESOURCE,
            profile_user_id=owner,
            social_post_id=None,
            slot=PROFILE_AVATAR_SLOT,
        )
        assets[normalized_asset].expires_at = None
        self.db.add(reference)
        self.db.flush()
        return _as_bound_reference(reference, assets[normalized_asset])

    bind_profile_avatar = replace_profile_avatar

    def bind_social_post_media(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        social_post_id: uuid.UUID | str,
        assets_by_slot: Mapping[str, uuid.UUID | str],
    ) -> dict[str, BoundMediaAssetReference]:
        """Atomically reconcile all current pictures/video/cover slots for a post."""

        if not isinstance(assets_by_slot, Mapping):
            raise MediaAssetReferenceSlotInvalid("assets_by_slot 必须是 slot 到 asset UUID 的映射")
        owner = _uuid(owner_user_id, "owner_user_id")
        post_id = _uuid(social_post_id, "social_post_id")
        normalized: dict[str, uuid.UUID] = {}
        for raw_slot, raw_asset_id in assets_by_slot.items():
            slot = str(raw_slot)
            _expected_kind(SOCIAL_POST_RESOURCE, slot)
            if slot in normalized:
                raise MediaAssetReferenceSlotInvalid("动态媒体 slot 重复")
            normalized[slot] = _uuid(raw_asset_id, f"assets_by_slot[{slot}]")
        new_asset_ids = sorted(set(normalized.values()), key=str)
        if len(new_asset_ids) != len(normalized):
            raise MediaAssetReferenceConflict("同一个 asset 不能绑定多个当前 slot")
        self._lock_social_post(owner, post_id, published=True)
        current_snapshot = list(
            self.db.scalars(
                select(MediaAssetReferenceRow)
                .where(
                    MediaAssetReferenceRow.resource_type == SOCIAL_POST_RESOURCE,
                    MediaAssetReferenceRow.social_post_id == post_id,
                )
                .order_by(MediaAssetReferenceRow.slot)
            )
        )
        union_ids = sorted(
            {*new_asset_ids, *(row.asset_id for row in current_snapshot)},
            key=str,
        )
        locked_assets = self._lock_owned_assets(
            owner_user_id=owner,
            asset_ids=union_ids,
        )
        assets = {
            asset_id_value: locked_assets[asset_id_value]
            for asset_id_value in new_asset_ids
            if asset_id_value in locked_assets
        }
        self._validate_available_unattached_assets(
            owner_user_id=owner,
            asset_ids=new_asset_ids,
            assets=assets,
        )
        self._validate_asset_kinds(
            assets_by_slot=normalized,
            resource_type=SOCIAL_POST_RESOURCE,
            assets=assets,
        )
        locked_references = self._references_for_assets(union_ids)
        current = [
            reference
            for reference in locked_references
            if reference.resource_type == SOCIAL_POST_RESOURCE
            and reference.social_post_id == post_id
        ]
        for reference in locked_references:
            if reference.asset_id not in new_asset_ids:
                continue
            if not (
                reference.resource_type == SOCIAL_POST_RESOURCE
                and reference.social_post_id == post_id
            ):
                raise MediaAssetReferenceConflict(
                    "媒体 asset 已绑定其他资料或动态资源"
                )
        current_by_slot = {reference.slot: reference for reference in current}
        removed = [
            reference
            for reference in current
            if normalized.get(reference.slot) != reference.asset_id
        ]
        for reference in removed:
            self.db.delete(reference)
        if removed:
            self.db.flush()
        retained = {
            slot: reference
            for slot, reference in current_by_slot.items()
            if normalized.get(slot) == reference.asset_id
        }
        for asset in assets.values():
            asset.expires_at = None
        for slot, asset_id_value in sorted(normalized.items()):
            if slot in retained:
                continue
            reference = MediaAssetReferenceRow(
                asset_id=asset_id_value,
                owner_user_id=owner,
                resource_type=SOCIAL_POST_RESOURCE,
                profile_user_id=None,
                social_post_id=post_id,
                slot=slot,
            )
            self.db.add(reference)
            retained[slot] = reference
        self.db.flush()
        return {
            slot: _as_bound_reference(retained[slot], assets[asset_id_value])
            for slot, asset_id_value in sorted(normalized.items())
        }

    bind_social_post_assets = bind_social_post_media

    def release_profile_avatar(
        self, *, owner_user_id: uuid.UUID | str
    ) -> bool:
        owner = _uuid(owner_user_id, "owner_user_id")
        self._lock_profile(owner, active=False)
        rows = list(
            self.db.scalars(
                select(MediaAssetReferenceRow)
                .where(
                    MediaAssetReferenceRow.resource_type == PROFILE_RESOURCE,
                    MediaAssetReferenceRow.profile_user_id == owner,
                    MediaAssetReferenceRow.slot == PROFILE_AVATAR_SLOT,
                )
                .with_for_update()
            )
        )
        for row in rows:
            self.db.delete(row)
        if rows:
            self.db.flush()
        return bool(rows)

    def release_social_post_media(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        social_post_id: uuid.UUID | str,
        slots: Sequence[str] | None = None,
    ) -> int:
        owner = _uuid(owner_user_id, "owner_user_id")
        post_id = _uuid(social_post_id, "social_post_id")
        self._lock_social_post(owner, post_id, published=False)
        normalized_slots: set[str] | None = None
        if slots is not None:
            normalized_slots = set()
            for raw_slot in slots:
                slot = str(raw_slot)
                _expected_kind(SOCIAL_POST_RESOURCE, slot)
                normalized_slots.add(slot)
        statement = (
            select(MediaAssetReferenceRow)
            .where(
                MediaAssetReferenceRow.resource_type == SOCIAL_POST_RESOURCE,
                MediaAssetReferenceRow.social_post_id == post_id,
            )
            .with_for_update()
        )
        rows = list(self.db.scalars(statement))
        selected = [
            row
            for row in rows
            if normalized_slots is None or row.slot in normalized_slots
        ]
        for row in selected:
            self.db.delete(row)
        if selected:
            self.db.flush()
        return len(selected)

    def release_asset_reference(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        asset_id: uuid.UUID | str,
    ) -> bool:
        """Release one current reference through its owner-bound asset UUID."""

        owner = _uuid(owner_user_id, "owner_user_id")
        normalized_asset = _uuid(asset_id, "asset_id")
        row = self.db.scalar(
            select(MediaAssetReferenceRow).where(
                MediaAssetReferenceRow.asset_id == normalized_asset,
                MediaAssetReferenceRow.owner_user_id == owner,
            )
        )
        if row is None:
            return False
        if row.resource_type == PROFILE_RESOURCE:
            self._lock_profile(owner, active=False)
            target_conditions = (
                MediaAssetReferenceRow.resource_type == PROFILE_RESOURCE,
                MediaAssetReferenceRow.profile_user_id == owner,
                MediaAssetReferenceRow.slot == PROFILE_AVATAR_SLOT,
            )
        elif row.resource_type == SOCIAL_POST_RESOURCE and row.social_post_id is not None:
            self._lock_social_post(owner, row.social_post_id, published=False)
            target_conditions = (
                MediaAssetReferenceRow.resource_type == SOCIAL_POST_RESOURCE,
                MediaAssetReferenceRow.social_post_id == row.social_post_id,
                MediaAssetReferenceRow.slot == row.slot,
            )
        else:
            raise MediaAssetReferenceError("媒体引用目标不完整")
        current = self.db.scalar(
            select(MediaAssetReferenceRow)
            .where(
                MediaAssetReferenceRow.asset_id == normalized_asset,
                MediaAssetReferenceRow.owner_user_id == owner,
                *target_conditions,
            )
            .with_for_update()
        )
        if current is None:
            return False
        self.db.delete(current)
        self.db.flush()
        return True

    def release_resource_references(
        self,
        *,
        owner_user_id: uuid.UUID | str,
        resource_type: str,
        resource_id: uuid.UUID | str,
        slots: Sequence[str] | None = None,
    ) -> int:
        """Release all or selected current references for one canonical resource."""

        owner = _uuid(owner_user_id, "owner_user_id")
        normalized_resource = _uuid(resource_id, "resource_id")
        if resource_type == PROFILE_RESOURCE:
            normalized_slots = (
                None if slots is None else tuple(str(slot) for slot in slots)
            )
            if normalized_resource != owner or normalized_slots not in (
                None,
                (PROFILE_AVATAR_SLOT,),
                (),
            ):
                raise MediaAssetReferenceSlotInvalid("资料引用仅支持当前账号 avatar slot")
            if normalized_slots == ():
                return 0
            return int(self.release_profile_avatar(owner_user_id=owner))
        if resource_type == SOCIAL_POST_RESOURCE:
            return self.release_social_post_media(
                owner_user_id=owner,
                social_post_id=normalized_resource,
                slots=slots,
            )
        raise MediaAssetReferenceSlotInvalid("媒体引用 resource_type 不合法")


__all__ = [
    "BoundMediaAssetReference",
    "MediaAssetReferenceAssetUnavailable",
    "MediaAssetReferenceConflict",
    "MediaAssetReferenceError",
    "MediaAssetReferenceSlotInvalid",
    "MediaAssetReferenceTargetUnavailable",
    "PROFILE_AVATAR_SLOT",
    "PROFILE_RESOURCE",
    "SOCIAL_COVER_SLOT",
    "SOCIAL_POST_RESOURCE",
    "SOCIAL_VIDEO_SLOT",
    "SqlAlchemyMediaAssetReferenceRepository",
    "ValidatedMediaAsset",
    "native_media_content_asset_id",
    "native_media_content_path",
]
