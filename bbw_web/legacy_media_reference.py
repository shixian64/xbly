"""Pure helpers for projecting migrated legacy media onto local Web routes.

Legacy importers keep the original upstream URL as migration evidence and add
an owner-bound ``_local_media`` sidecar.  Request paths use this module to
replace a URL only while the sidecar still matches the current source value;
changing an avatar or other canonical field therefore invalidates a stale
archive reference without a destructive rewrite.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Mapping


LOCAL_MEDIA_KEY = "_local_media"
LOCAL_MEDIA_SCHEMA = 1
LOCAL_MEDIA_MAX_ITEMS = 128
LOCAL_MEDIA_CONTENT_PREFIX = "/api/media"


@dataclass(frozen=True, slots=True)
class LocalMediaReference:
    slot: str
    media_id: uuid.UUID
    source_hash: str


def legacy_source_hash(value: Any) -> str:
    """Return the stable digest used by the v1 migration sidecar."""

    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()


def local_media_content_path(media_id: uuid.UUID | str) -> str:
    identifier = media_id if isinstance(media_id, uuid.UUID) else uuid.UUID(str(media_id))
    return f"{LOCAL_MEDIA_CONTENT_PREFIX}/{identifier}/content"


def local_media_references(container: Any) -> dict[str, LocalMediaReference]:
    """Parse one bounded v1 sidecar, rejecting ambiguous duplicate slots."""

    if not isinstance(container, Mapping):
        return {}
    sidecar = container.get(LOCAL_MEDIA_KEY)
    if not isinstance(sidecar, Mapping) or sidecar.get("schema") != LOCAL_MEDIA_SCHEMA:
        return {}
    items = sidecar.get("items")
    if not isinstance(items, list) or len(items) > LOCAL_MEDIA_MAX_ITEMS:
        return {}

    parsed: dict[str, LocalMediaReference] = {}
    for item in items:
        if not isinstance(item, Mapping):
            return {}
        slot = str(item.get("slot") or "").strip()
        digest = str(item.get("source_hash") or "").strip().lower()
        try:
            media_id = uuid.UUID(str(item.get("media_id") or ""))
        except (TypeError, ValueError, AttributeError):
            return {}
        if (
            not slot
            or len(slot) > 96
            or any(ord(character) < 33 for character in slot)
            or slot in parsed
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return {}
        parsed[slot] = LocalMediaReference(slot, media_id, digest)
    return parsed


def matching_local_media_reference(
    container: Any,
    *,
    slot: str,
    source_value: Any,
) -> LocalMediaReference | None:
    source = str(source_value or "").strip()
    if not source:
        return None
    reference = local_media_references(container).get(str(slot or ""))
    if reference is None or reference.source_hash != legacy_source_hash(source):
        return None
    return reference


def matching_local_media_path(
    container: Any,
    *,
    slot: str,
    source_value: Any,
) -> str:
    reference = matching_local_media_reference(
        container,
        slot=slot,
        source_value=source_value,
    )
    return local_media_content_path(reference.media_id) if reference is not None else ""


def projected_profile_avatar(profile: Any) -> str:
    if not isinstance(profile, Mapping):
        return ""
    source = str(profile.get("avatar") or profile.get("portrait") or "").strip()
    return matching_local_media_path(
        profile,
        slot="avatar",
        source_value=source,
    ) or source


def projected_social_post_media(media: Any) -> dict[str, Any]:
    if not isinstance(media, Mapping):
        return {}
    projected = dict(media)
    pictures = projected.get("pictures") or projected.get("images") or []
    if not isinstance(pictures, list):
        pictures = [pictures]
    local_pictures: list[Any] = []
    for index, value in enumerate(pictures):
        local_pictures.append(
            matching_local_media_path(
                media,
                slot=f"pictures[{index}]",
                source_value=value,
            )
            or value
        )
    if "pictures" in projected or local_pictures:
        projected["pictures"] = local_pictures
    if "images" in projected:
        projected["images"] = list(local_pictures)
    for slot in ("video", "cover"):
        source = projected.get(slot)
        local = matching_local_media_path(
            media,
            slot=slot,
            source_value=source,
        )
        if local:
            projected[slot] = local
    return projected


def projected_message_media(message_metadata: Any) -> dict[str, Any]:
    if not isinstance(message_metadata, Mapping):
        return {}
    report = message_metadata.get("media_report")
    if not isinstance(report, Mapping):
        return {}
    projected = dict(report)
    for field in ("url", "thumbnail"):
        source = projected.get(field)
        local = matching_local_media_path(
            message_metadata,
            slot=f"media_report.{field}",
            source_value=source,
        )
        if local:
            projected[field] = local
    return projected


__all__ = [
    "LOCAL_MEDIA_KEY",
    "LOCAL_MEDIA_SCHEMA",
    "LocalMediaReference",
    "legacy_source_hash",
    "local_media_content_path",
    "local_media_references",
    "matching_local_media_path",
    "matching_local_media_reference",
    "projected_message_media",
    "projected_profile_avatar",
    "projected_social_post_media",
]
