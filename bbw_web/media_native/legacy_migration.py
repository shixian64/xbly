"""Strict, auditable migration of legacy profile, moments and C2C media.

The importer is intentionally an offline operation.  It first proves that the
social, moments and trusted message-peer snapshots are complete, then closes
the database transaction before reading TIM roaming history.  A media-domain
completion marker is written only after every retained source has a stable
``_local_media`` binding and every referenced private R2 object has been
verified.

Original upstream URLs remain in their canonical JSON containers.  The only
projection added by this module is the bounded v1 sidecar shared with request
paths through :mod:`bbw_web.legacy_media_reference`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ContextManager, Protocol, TextIO

from sqlalchemy import select

from bbw_prod.config import Settings, get_settings
from bbw_prod.db import session_scope
from bbw_prod.migration_readiness import (
    DOMAIN_MARKER_SCHEMA,
    DOMAIN_MARKER_SCOPES,
    DOMAIN_MARKER_STREAMS,
    MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS,
    MESSAGE_PEER_SNAPSHOT_STREAM,
    _message_peer_marker,
    _valid_domain_marker,
    _valid_message_peer_relationship_snapshot,
)
from bbw_prod.models import (
    ExternalAccount,
    MediaObject,
    Message,
    OperationOutbox,
    Relationship,
    SocialPost,
    SyncCursor,
    User,
    utcnow,
)
from bbw_prod.repositories import SyncCursorRepository
from bbw_prod.services import MediaQuotaService
from bbw_web.legacy_media_reference import (
    LOCAL_MEDIA_KEY,
    LOCAL_MEDIA_SCHEMA,
    legacy_source_hash,
    local_media_references,
)
from bbw_web.media_archive import download_and_prepare
from bbw_web.media_native.references import native_media_content_asset_id
from bbw_web.normalize import normalize_messages
from bbw_web.r2 import R2Storage


LEGACY_PROVIDER = "beibeiwu"
MESSAGE_POLICY_PROVIDER = "web-policy"
MESSAGE_POLICY_KIND = "message_peer"
MEDIA_STREAM = DOMAIN_MARKER_STREAMS["media"]
SOCIAL_STREAM = DOMAIN_MARKER_STREAMS["social"]
MOMENTS_STREAM = DOMAIN_MARKER_STREAMS["moments"]
MEDIA_SOURCE_PATHS = (
    "postgresql://users.profile",
    "postgresql://social_posts.media",
    "tim://openim/admin_getroammsg",
    "r2://private-media",
)
LEGACY_MEDIA_METADATA_KEY = "legacy_media"
MEDIA_ARCHIVE_OPERATION = "media.archive"

MAX_HISTORY_PAGES_PER_DIRECTION = 1000
MAX_HISTORY_PAGE_ITEMS = 100
MAX_HISTORY_MESSAGES_PER_ACCOUNT = 1_000_000
MAX_MEDIA_SOURCES_PER_ACCOUNT = 250_000
MAX_EXISTING_MEDIA_SCAN = 250_000
MAX_INGEST_BATCH = 500
PERMANENT_RETENTION_AT = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


class LegacyMediaMigrationError(RuntimeError):
    code = "legacy_media_migration_error"
    retryable = False


class LegacyMediaPrerequisiteError(LegacyMediaMigrationError):
    code = "legacy_media_prerequisite_not_ready"


class LegacyMediaProviderError(LegacyMediaMigrationError):
    code = "legacy_media_provider_unavailable"
    retryable = True


class LegacyMediaDataError(LegacyMediaMigrationError):
    code = "legacy_media_data_invalid"


class LegacyMediaLimitError(LegacyMediaMigrationError):
    code = "legacy_media_import_limit_exceeded"


class LegacyMediaIngestError(LegacyMediaMigrationError):
    code = "legacy_media_message_ingest_failed"
    retryable = True


class LegacyMediaArchiveError(LegacyMediaMigrationError):
    code = "legacy_media_archive_failed"
    retryable = True


class LegacyMediaVerificationError(LegacyMediaMigrationError):
    code = "legacy_media_verification_failed"


@dataclass(frozen=True, slots=True)
class LegacyMediaAccount:
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    chat_retention_days: int


@dataclass(frozen=True, slots=True)
class ImportWindow:
    coverage_started_at: datetime
    coverage_ended_at: datetime


@dataclass(frozen=True, slots=True)
class PlannedResource:
    resource_type: str
    resource_id: str
    container: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LegacyMediaSource:
    resource_type: str
    resource_id: str
    slot: str
    source_url: str
    source_hash: str
    kind: str
    original_name: str
    retention_expires_at: datetime
    message_id: uuid.UUID | None = None

    def identity(self) -> tuple[str, str, str]:
        return self.resource_type, self.resource_id, self.slot


@dataclass(frozen=True, slots=True)
class ArchivedMedia:
    resource_type: str
    resource_id: str
    slot: str
    source_hash: str
    media_id: uuid.UUID
    owner_user_id: uuid.UUID
    bucket: str
    object_key: str
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    message_id: uuid.UUID | None = None

    def identity(self) -> tuple[str, str, str]:
        return self.resource_type, self.resource_id, self.slot


@dataclass(frozen=True, slots=True)
class HistoryPage:
    peer_uid: str
    sender_uid: str
    recipient_uid: str
    page: int
    cursor: str
    next_cursor: str
    complete: bool
    items: tuple[Mapping[str, Any], ...]
    page_digest: str


@dataclass(frozen=True, slots=True)
class HistoryDirection:
    peer_uid: str
    sender_uid: str
    recipient_uid: str
    pages: tuple[HistoryPage, ...]

    @property
    def item_count(self) -> int:
        return sum(len(page.items) for page in self.pages)


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    window: ImportWindow
    directions: tuple[HistoryDirection, ...]
    page_count: int
    message_count: int
    record_digest: str


@dataclass(frozen=True, slots=True)
class LegacyMediaPlan:
    account: LegacyMediaAccount
    window: ImportWindow
    peers: tuple[str, ...]
    resources: tuple[PlannedResource, ...]
    sources: tuple[LegacyMediaSource, ...]
    social_record_digest: str
    moments_record_digest: str
    peer_digest: str


@dataclass(frozen=True, slots=True)
class LegacyMessageIngestResult:
    messages_written: int
    media_sources: tuple[LegacyMediaSource, ...]


@dataclass(frozen=True, slots=True)
class LegacyMediaImportSummary:
    history_pages: int
    history_messages: int
    messages_written: int
    metadata_records: int
    object_records: int
    archived_created: int
    archived_reused: int
    record_digest: str


class LegacyRoamingHistoryReader(Protocol):
    def fetch_page(
        self,
        *,
        sender_uid: str,
        recipient_uid: str,
        min_time: int,
        max_time: int,
        max_count: int,
        last_msg_key: str,
    ) -> Any: ...


class LegacyMessageIngestor(Protocol):
    def ingest(
        self,
        plan: LegacyMediaPlan,
        history: HistorySnapshot,
    ) -> LegacyMessageIngestResult: ...


class LegacyMediaArchiver(Protocol):
    def archive(
        self,
        account: LegacyMediaAccount,
        source: LegacyMediaSource,
    ) -> tuple[ArchivedMedia, bool]: ...

    def verify(self, artifact: ArchivedMedia) -> bool: ...


class LegacyMediaWriter(Protocol):
    def prepare(
        self,
        owner_user_id: uuid.UUID,
        *,
        ended_at: datetime,
    ) -> LegacyMediaPlan: ...

    def begin(self, plan: LegacyMediaPlan) -> None: ...

    def bind(
        self,
        plan: LegacyMediaPlan,
        sources: Sequence[LegacyMediaSource],
        artifacts: Sequence[ArchivedMedia],
    ) -> None: ...

    def verification_records(
        self,
        plan: LegacyMediaPlan,
        sources: Sequence[LegacyMediaSource],
    ) -> tuple[ArchivedMedia, ...]: ...

    def complete(
        self,
        plan: LegacyMediaPlan,
        history: HistorySnapshot,
        records: Sequence[ArchivedMedia],
    ) -> tuple[dict[str, int], str]: ...

    def fail(self, plan: LegacyMediaPlan, *, code: str) -> None: ...


DbScope = Callable[[], ContextManager[Any]]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _identifier(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text.lower() in {"0", "none", "null"}
        or len(text) > limit
        or any(ord(character) < 32 for character in text)
    ):
        return ""
    return text


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _marker_payload(cursor: Any) -> Mapping[str, Any]:
    try:
        marker = json.loads(str(getattr(cursor, "cursor", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LegacyMediaPrerequisiteError("migration prerequisite marker is invalid") from exc
    if not isinstance(marker, Mapping):
        raise LegacyMediaPrerequisiteError("migration prerequisite marker is invalid")
    return marker


def _marker_datetime(marker: Mapping[str, Any], field: str) -> datetime:
    raw = str(marker.get(field) or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyMediaPrerequisiteError("migration prerequisite coverage is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LegacyMediaPrerequisiteError("migration prerequisite coverage is invalid")
    return parsed.astimezone(UTC)


def _source(
    *,
    resource_type: str,
    resource_id: str,
    slot: str,
    source_url: Any,
    kind: str,
    original_name: Any = "",
    retention_expires_at: datetime = PERMANENT_RETENTION_AT,
    message_id: uuid.UUID | None = None,
) -> LegacyMediaSource | None:
    url = str(source_url or "").strip()
    if not url:
        return None
    if native_media_content_asset_id(url) is not None:
        # 已经是 Web-native 私有媒体的同源鉴权路径（例如迁移后更换的头像），
        # 原文件本就在私有 R2，无需也不可能按上游 URL 归档。
        return None
    normalized_slot = str(slot or "").strip()
    if (
        not normalized_slot
        or len(normalized_slot) > 96
        or any(ord(character) < 33 for character in normalized_slot)
    ):
        raise LegacyMediaDataError("legacy media slot is invalid")
    normalized_type = str(resource_type or "").strip()
    if normalized_type not in {"profile", "social_post", "message"}:
        raise LegacyMediaDataError("legacy media resource type is invalid")
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"image", "audio", "video", "attachment"}:
        raise LegacyMediaDataError("legacy media kind is invalid")
    return LegacyMediaSource(
        resource_type=normalized_type,
        resource_id=str(resource_id),
        slot=normalized_slot,
        source_url=url,
        source_hash=legacy_source_hash(url),
        kind=normalized_kind,
        original_name=str(original_name or "")[:255],
        retention_expires_at=_as_utc(retention_expires_at),
        message_id=message_id,
    )


def profile_media_sources(resource: PlannedResource) -> tuple[LegacyMediaSource, ...]:
    profile = resource.container
    value = profile.get("avatar") or profile.get("portrait")
    item = _source(
        resource_type="profile",
        resource_id=resource.resource_id,
        slot="avatar",
        source_url=value,
        kind="image",
    )
    return (item,) if item is not None else ()


def social_post_media_sources(resource: PlannedResource) -> tuple[LegacyMediaSource, ...]:
    media = resource.container
    raw_pictures = media.get("pictures") or media.get("images") or []
    if not isinstance(raw_pictures, list):
        raw_pictures = [raw_pictures]
    sources: list[LegacyMediaSource] = []
    for index, value in enumerate(raw_pictures):
        item = _source(
            resource_type="social_post",
            resource_id=resource.resource_id,
            slot=f"pictures[{index}]",
            source_url=value,
            kind="image",
        )
        if item is not None:
            sources.append(item)
    video = _source(
        resource_type="social_post",
        resource_id=resource.resource_id,
        slot="video",
        source_url=media.get("video"),
        kind="video",
    )
    cover = _source(
        resource_type="social_post",
        resource_id=resource.resource_id,
        slot="cover",
        source_url=media.get("cover"),
        kind="image",
    )
    if video is not None:
        sources.append(video)
    if cover is not None:
        sources.append(cover)
    return tuple(sources)


def message_media_sources(
    *,
    message_id: uuid.UUID,
    media_report: Mapping[str, Any],
    message_kind: str,
    retention_expires_at: datetime,
) -> tuple[LegacyMediaSource, ...]:
    normalized_kind = str(message_kind or "").strip().lower()
    primary_kind = {
        "image": "image",
        "flash": "image",
        "audio": "audio",
        "voice": "audio",
        "video": "video",
        "file": "attachment",
        "attachment": "attachment",
    }.get(normalized_kind, "attachment")
    resource_id = str(message_id)
    sources: list[LegacyMediaSource] = []
    primary = _source(
        resource_type="message",
        resource_id=resource_id,
        slot="media_report.url",
        source_url=media_report.get("url"),
        kind=primary_kind,
        original_name=media_report.get("name"),
        retention_expires_at=retention_expires_at,
        message_id=message_id,
    )
    thumbnail = _source(
        resource_type="message",
        resource_id=resource_id,
        slot="media_report.thumbnail",
        source_url=media_report.get("thumbnail"),
        kind="image",
        retention_expires_at=retention_expires_at,
        message_id=message_id,
    )
    if primary is not None:
        sources.append(primary)
    if thumbnail is not None:
        sources.append(thumbnail)
    return tuple(sources)


def _deduplicate_sources(
    sources: Sequence[LegacyMediaSource],
) -> tuple[LegacyMediaSource, ...]:
    by_identity: dict[tuple[str, str, str], LegacyMediaSource] = {}
    for source in sources:
        previous = by_identity.get(source.identity())
        if previous is not None and previous != source:
            raise LegacyMediaDataError("legacy media slot has conflicting sources")
        by_identity[source.identity()] = source
        if len(by_identity) > MAX_MEDIA_SOURCES_PER_ACCOUNT:
            raise LegacyMediaLimitError("legacy media source limit exceeded")
    return tuple(by_identity[key] for key in sorted(by_identity))


def local_media_sidecar(
    artifacts: Sequence[ArchivedMedia],
) -> dict[str, Any]:
    items = [
        {
            "media_id": str(artifact.media_id),
            "slot": artifact.slot,
            "source_hash": artifact.source_hash,
        }
        for artifact in sorted(artifacts, key=lambda item: item.slot)
    ]
    if len(items) != len({item["slot"] for item in items}):
        raise LegacyMediaDataError("legacy media sidecar contains duplicate slots")
    return {"schema": LOCAL_MEDIA_SCHEMA, "items": items}


def media_record_digest(records: Sequence[ArchivedMedia]) -> str:
    identities = [
        {
            "bucket": record.bucket,
            "content_type": record.content_type,
            "kind": record.kind,
            "media_id": str(record.media_id),
            "message_id": str(record.message_id) if record.message_id else "",
            "object_key": record.object_key,
            "owner_user_id": str(record.owner_user_id),
            "resource_id": record.resource_id,
            "resource_type": record.resource_type,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "slot": record.slot,
            "source_hash": record.source_hash,
        }
        for record in sorted(records, key=lambda item: item.identity())
    ]
    return _sha256_json(identities)


def _page_result_data(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        if result.get("ok") is False:
            raise LegacyMediaProviderError("TIM roaming history request failed")
        data = result.get("data", result)
    else:
        if not bool(getattr(result, "ok", False)):
            raise LegacyMediaProviderError("TIM roaming history request failed")
        data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        raise LegacyMediaDataError("TIM roaming history response is not an object")
    return data


def _strict_complete(value: Any) -> int:
    if isinstance(value, bool):
        raise LegacyMediaDataError("TIM roaming history Complete is invalid")
    if isinstance(value, int) and value in {0, 1}:
        return value
    if isinstance(value, str) and value in {"0", "1"}:
        return int(value)
    raise LegacyMediaDataError("TIM roaming history Complete is invalid")


def _raw_message_endpoint(item: Mapping[str, Any], names: Sequence[str]) -> str:
    for name in names:
        value = _identifier(item.get(name), limit=128)
        if value:
            return value
    return ""


def _raw_message_timestamp(item: Mapping[str, Any]) -> int:
    value: Any = None
    for name in ("MsgTimeStamp", "msg_time_stamp", "timestamp", "time"):
        if item.get(name) not in (None, ""):
            value = item.get(name)
            break
    try:
        timestamp = int(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise LegacyMediaDataError("TIM roaming history message timestamp is invalid") from exc
    if timestamp >= 10**12:
        timestamp //= 1000
    if timestamp < 0:
        raise LegacyMediaDataError("TIM roaming history message timestamp is invalid")
    return timestamp


def fetch_complete_roaming_history(
    reader: LegacyRoamingHistoryReader,
    *,
    account_uid: str,
    peers: Sequence[str],
    window: ImportWindow,
    max_pages_per_direction: int = MAX_HISTORY_PAGES_PER_DIRECTION,
    max_messages: int = MAX_HISTORY_MESSAGES_PER_ACCOUNT,
) -> HistorySnapshot:
    """Read every trusted peer in both directions and require ``Complete=1``."""

    own_uid = _identifier(account_uid, limit=128)
    if not own_uid:
        raise LegacyMediaDataError("legacy account UID is invalid")
    normalized_peers = tuple(sorted(set(_identifier(peer, limit=128) for peer in peers)))
    if any(not peer or peer == own_uid for peer in normalized_peers):
        raise LegacyMediaDataError("trusted message peer is invalid")
    if len(normalized_peers) > MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS:
        raise LegacyMediaLimitError("trusted message peer limit exceeded")
    page_limit = max(1, min(int(max_pages_per_direction), MAX_HISTORY_PAGES_PER_DIRECTION))
    message_limit = max(0, min(int(max_messages), MAX_HISTORY_MESSAGES_PER_ACCOUNT))
    min_time = max(0, int(_as_utc(window.coverage_started_at).timestamp()))
    max_time = max(min_time, int(_as_utc(window.coverage_ended_at).timestamp()))
    directions: list[HistoryDirection] = []
    total_messages = 0
    history_identities: list[dict[str, Any]] = []

    for peer_uid in normalized_peers:
        for sender_uid, recipient_uid in ((peer_uid, own_uid), (own_uid, peer_uid)):
            cursor = ""
            seen_cursors = {cursor}
            seen_pages: set[str] = set()
            pages: list[HistoryPage] = []
            for page_number in range(1, page_limit + 1):
                try:
                    result = reader.fetch_page(
                        sender_uid=sender_uid,
                        recipient_uid=recipient_uid,
                        min_time=min_time,
                        max_time=max_time,
                        max_count=MAX_HISTORY_PAGE_ITEMS,
                        last_msg_key=cursor,
                    )
                except LegacyMediaMigrationError:
                    raise
                except Exception as exc:
                    raise LegacyMediaProviderError(
                        "TIM roaming history request raised an exception"
                    ) from exc
                data = _page_result_data(result)
                complete_value = _strict_complete(data.get("Complete"))
                raw_items = data.get("MsgList")
                if not isinstance(raw_items, list):
                    raise LegacyMediaDataError("TIM roaming history MsgList is invalid")
                if len(raw_items) > MAX_HISTORY_PAGE_ITEMS:
                    raise LegacyMediaDataError("TIM roaming history page is oversized")
                items: list[Mapping[str, Any]] = []
                for raw in raw_items:
                    if not isinstance(raw, Mapping):
                        raise LegacyMediaDataError("TIM roaming history item is invalid")
                    actual_sender = _raw_message_endpoint(
                        raw,
                        ("From_Account", "from_user_id", "from"),
                    )
                    actual_recipient = _raw_message_endpoint(
                        raw,
                        ("To_Account", "to_user_id", "to"),
                    )
                    if actual_sender != sender_uid or actual_recipient != recipient_uid:
                        raise LegacyMediaDataError(
                            "TIM roaming history direction does not match the request"
                        )
                    timestamp = _raw_message_timestamp(raw)
                    if timestamp < min_time or timestamp > max_time:
                        raise LegacyMediaDataError(
                            "TIM roaming history item is outside the retention window"
                        )
                    items.append(dict(raw))
                next_cursor = str(data.get("LastMsgKey") or "").strip()
                if len(next_cursor) > 256 or any(ord(char) < 32 for char in next_cursor):
                    raise LegacyMediaDataError("TIM roaming history cursor is invalid")
                page_digest = _sha256_json(items)
                if page_digest in seen_pages and items:
                    raise LegacyMediaDataError("TIM roaming history page repeated")
                seen_pages.add(page_digest)
                if complete_value == 0:
                    if not items or not next_cursor:
                        raise LegacyMediaDataError(
                            "TIM roaming history ended without Complete=1"
                        )
                    if next_cursor in seen_cursors:
                        raise LegacyMediaDataError("TIM roaming history cursor repeated")
                    seen_cursors.add(next_cursor)
                page = HistoryPage(
                    peer_uid=peer_uid,
                    sender_uid=sender_uid,
                    recipient_uid=recipient_uid,
                    page=page_number,
                    cursor=cursor,
                    next_cursor=next_cursor,
                    complete=complete_value == 1,
                    items=tuple(items),
                    page_digest=page_digest,
                )
                pages.append(page)
                total_messages += len(items)
                if total_messages > message_limit:
                    raise LegacyMediaLimitError(
                        "TIM roaming history account message limit exceeded"
                    )
                history_identities.append(
                    {
                        "complete": page.complete,
                        "direction": (
                            "incoming" if sender_uid == peer_uid else "outgoing"
                        ),
                        "page": page_number,
                        "page_digest": page_digest,
                        "peer_hash": hashlib.sha256(peer_uid.encode("utf-8")).hexdigest(),
                        "records": len(items),
                    }
                )
                if complete_value == 1:
                    break
                cursor = next_cursor
            else:
                raise LegacyMediaLimitError(
                    "TIM roaming history page limit reached before Complete=1"
                )
            if not pages or pages[-1].complete is not True:
                raise LegacyMediaDataError("TIM roaming history direction is incomplete")
            directions.append(
                HistoryDirection(
                    peer_uid=peer_uid,
                    sender_uid=sender_uid,
                    recipient_uid=recipient_uid,
                    pages=tuple(pages),
                )
            )

    return HistorySnapshot(
        window=window,
        directions=tuple(directions),
        page_count=sum(len(direction.pages) for direction in directions),
        message_count=total_messages,
        record_digest=_sha256_json(history_identities),
    )


class LegacyMediaMigration:
    """Orchestrate strict history reads, local ingestion and verified archival."""

    def __init__(
        self,
        *,
        reader: LegacyRoamingHistoryReader,
        ingestor: LegacyMessageIngestor,
        archiver: LegacyMediaArchiver,
        writer: LegacyMediaWriter,
        clock=utcnow,
    ) -> None:
        self.reader = reader
        self.ingestor = ingestor
        self.archiver = archiver
        self.writer = writer
        self.clock = clock

    def run(self, owner_user_id: uuid.UUID | str) -> LegacyMediaImportSummary:
        try:
            owner_id = (
                owner_user_id
                if isinstance(owner_user_id, uuid.UUID)
                else uuid.UUID(str(owner_user_id))
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise LegacyMediaDataError("owner_user_id is invalid") from exc

        ended_at = _as_utc(self.clock())
        plan = self.writer.prepare(owner_id, ended_at=ended_at)
        self.writer.begin(plan)
        try:
            history = fetch_complete_roaming_history(
                self.reader,
                account_uid=plan.account.upstream_uid,
                peers=plan.peers,
                window=plan.window,
            )
            try:
                ingested = self.ingestor.ingest(plan, history)
            except LegacyMediaMigrationError:
                raise
            except Exception as exc:
                raise LegacyMediaIngestError(
                    "legacy message history could not be ingested"
                ) from exc
            sources = _deduplicate_sources(
                (*plan.sources, *ingested.media_sources)
            )
            artifacts: list[ArchivedMedia] = []
            archived_created = 0
            archived_reused = 0
            for source in sources:
                try:
                    artifact, created = self.archiver.archive(plan.account, source)
                except LegacyMediaMigrationError:
                    raise
                except Exception as exc:
                    raise LegacyMediaArchiveError(
                        "legacy media object could not be archived"
                    ) from exc
                if artifact.identity() != source.identity():
                    raise LegacyMediaVerificationError(
                        "archiver returned a mismatched media identity"
                    )
                if artifact.source_hash != source.source_hash:
                    raise LegacyMediaVerificationError(
                        "archiver returned a mismatched source digest"
                    )
                artifacts.append(artifact)
                archived_created += int(created)
                archived_reused += int(not created)
            self.writer.bind(plan, sources, artifacts)
            records = self.writer.verification_records(plan, sources)
            for record in records:
                try:
                    verified = self.archiver.verify(record)
                except Exception as exc:
                    raise LegacyMediaVerificationError(
                        "private media object verification failed"
                    ) from exc
                if verified is not True:
                    raise LegacyMediaVerificationError(
                        "private media object verification failed"
                    )
            counts, digest = self.writer.complete(plan, history, records)
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, LegacyMediaMigrationError)
                else f"legacy_media_exception.{type(exc).__name__}"
            )
            try:
                self.writer.fail(plan, code=code)
            except Exception:
                pass
            raise
        return LegacyMediaImportSummary(
            history_pages=history.page_count,
            history_messages=history.message_count,
            messages_written=ingested.messages_written,
            metadata_records=counts["metadata"],
            object_records=counts["objects"],
            archived_created=archived_created,
            archived_reused=archived_reused,
            record_digest=digest,
        )


class TimRoamingHistoryReader:
    """Adapter around the neutral TIM message-history transport."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    def fetch_page(
        self,
        *,
        sender_uid: str,
        recipient_uid: str,
        min_time: int,
        max_time: int,
        max_count: int,
        last_msg_key: str,
    ) -> Any:
        return self.transport.roaming_messages(
            sender_uid,
            recipient_uid,
            min_time=min_time,
            max_time=max_time,
            max_count=max_count,
            last_msg_key=last_msg_key,
        )


def _relationship_metadata(row: Any) -> Mapping[str, Any]:
    value = getattr(row, "extra_data", {})
    return value if isinstance(value, Mapping) else {}


def _resource_current_source(container: Mapping[str, Any], slot: str) -> str:
    if slot == "avatar":
        return str(container.get("avatar") or container.get("portrait") or "").strip()
    if slot.startswith("pictures[") and slot.endswith("]"):
        try:
            index = int(slot[9:-1])
        except (TypeError, ValueError) as exc:
            raise LegacyMediaDataError("legacy post media slot is invalid") from exc
        pictures = container.get("pictures") or container.get("images") or []
        if not isinstance(pictures, list):
            pictures = [pictures]
        if index < 0 or index >= len(pictures):
            return ""
        return str(pictures[index] or "").strip()
    if slot in {"video", "cover"}:
        return str(container.get(slot) or "").strip()
    if slot.startswith("media_report."):
        report = container.get("media_report")
        if not isinstance(report, Mapping):
            return ""
        field = slot.split(".", 1)[1]
        if field not in {"url", "thumbnail"}:
            raise LegacyMediaDataError("legacy message media slot is invalid")
        return str(report.get(field) or "").strip()
    raise LegacyMediaDataError("legacy media sidecar contains an unknown slot")


def _legacy_object_metadata(
    source: LegacyMediaSource,
) -> dict[str, Any]:
    return {
        "schema": LOCAL_MEDIA_SCHEMA,
        "source_hash": source.source_hash,
        "slot": source.slot,
        "resource_type": source.resource_type,
        "resource_id": source.resource_id,
        "access_scope": source.resource_type,
    }


def _artifact_from_row(row: MediaObject) -> ArchivedMedia:
    metadata = dict(row.extra_data or {}) if isinstance(row.extra_data, Mapping) else {}
    legacy = metadata.get(LEGACY_MEDIA_METADATA_KEY)
    if not isinstance(legacy, Mapping):
        raise LegacyMediaVerificationError("media object has no legacy binding")
    source_hash = str(legacy.get("source_hash") or "").strip().lower()
    if (
        legacy.get("schema") != LOCAL_MEDIA_SCHEMA
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        raise LegacyMediaVerificationError("media object legacy binding is invalid")
    return ArchivedMedia(
        resource_type=str(legacy.get("resource_type") or ""),
        resource_id=str(legacy.get("resource_id") or ""),
        slot=str(legacy.get("slot") or ""),
        source_hash=source_hash,
        media_id=row.id,
        owner_user_id=row.owner_user_id,
        bucket=str(row.r2_bucket or ""),
        object_key=str(row.r2_object_key or ""),
        kind=str(row.kind or ""),
        content_type=str(row.content_type or ""),
        size_bytes=int(row.size_bytes or 0),
        sha256=str(row.sha256 or "").strip().lower(),
        message_id=row.message_id,
    )


def _validate_artifact_shape(artifact: ArchivedMedia) -> None:
    if (
        artifact.resource_type not in {"profile", "social_post", "message"}
        or not artifact.resource_id
        or not artifact.slot
        or len(artifact.source_hash) != 64
        or any(character not in "0123456789abcdef" for character in artifact.source_hash)
        or not artifact.bucket
        or not artifact.object_key
        or artifact.size_bytes <= 0
        or len(artifact.sha256) != 64
        or any(character not in "0123456789abcdef" for character in artifact.sha256)
    ):
        raise LegacyMediaVerificationError("archived media metadata is incomplete")


class SqlAlchemyLegacyMediaWriter:
    """Load prerequisites and atomically bind verified local media references."""

    def __init__(self, *, db_scope: DbScope = session_scope, clock=utcnow) -> None:
        self.db_scope = db_scope
        self.clock = clock

    @staticmethod
    def _base_marker(
        plan: LegacyMediaPlan,
        *,
        phase: str,
        complete: bool = False,
    ) -> dict[str, Any]:
        return {
            "complete": complete,
            "source_complete": complete,
            "schema": DOMAIN_MARKER_SCHEMA,
            "domain": "media",
            "owner_user_id": str(plan.account.owner_user_id),
            "external_account_id": str(plan.account.external_account_id),
            "upstream_uid": plan.account.upstream_uid,
            "coverage_started_at": plan.window.coverage_started_at.isoformat(),
            "coverage_ended_at": plan.window.coverage_ended_at.isoformat(),
            "phase": phase,
        }

    def _has_complete_marker(self, db: Any, plan: LegacyMediaPlan) -> bool:
        """本地宽校验：既有 Marker 是否是本账号的 complete 记录。

        媒体 Marker 的完整严格校验（prerequisite 摘要、peer 数）由
        migration_readiness 负责；这里只判断"是否存在成功收尾的 complete
        Marker"，用于阻止重跑失败时把它降级为 history/failed。
        """

        cursor = SyncCursorRepository(db).get(
            plan.account.owner_user_id, LEGACY_PROVIDER, MEDIA_STREAM
        )
        if (
            cursor is None
            or cursor.last_succeeded_at is None
            or str(cursor.last_error or "").strip()
        ):
            return False
        try:
            marker = json.loads(str(cursor.cursor or ""))
        except (TypeError, ValueError):
            return False
        return (
            isinstance(marker, Mapping)
            and str(marker.get("phase") or "") == "complete"
            and marker.get("complete") is True
            and str(marker.get("external_account_id") or "")
            == str(plan.account.external_account_id)
            and str(marker.get("upstream_uid") or "") == plan.account.upstream_uid
        )

    def _write_cursor(
        self,
        db: Any,
        plan: LegacyMediaPlan,
        *,
        marker: Mapping[str, Any],
        succeeded_at: datetime | None,
        error: str | None,
    ) -> None:
        if str(dict(marker).get("phase") or "") != "complete" and (
            self._has_complete_marker(db, plan)
        ):
            # 已有成功收尾的 complete Marker 时，history/failed 写入一律
            # 跳过：校验性重跑中途失败必须保留既有完整性证明，成功时
            # 仍以新 complete Marker 收尾。
            return
        attempted_at = _as_utc(self.clock())
        SyncCursorRepository(db).upsert(
            owner_user_id=plan.account.owner_user_id,
            source=LEGACY_PROVIDER,
            stream=MEDIA_STREAM,
            cursor=json.dumps(
                dict(marker),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            watermark_at=max(plan.window.coverage_ended_at, attempted_at),
            next_sync_at=None,
            last_attempted_at=attempted_at,
            last_succeeded_at=succeeded_at,
            last_error=error,
            version=1,
        )

    def prepare(
        self,
        owner_user_id: uuid.UUID,
        *,
        ended_at: datetime,
    ) -> LegacyMediaPlan:
        ended_at = _as_utc(ended_at)
        with self.db_scope() as db:
            binding = db.execute(
                select(User, ExternalAccount)
                .join(ExternalAccount, ExternalAccount.user_id == User.id)
                .where(
                    User.id == owner_user_id,
                    User.status == "active",
                    User.disabled_at.is_(None),
                    ExternalAccount.provider == LEGACY_PROVIDER,
                    ExternalAccount.sync_enabled.is_(True),
                )
            ).one_or_none()
            if binding is None:
                raise LegacyMediaDataError("active legacy account binding is missing")
            user, external = binding
            upstream_uid = _identifier(external.upstream_uid, limit=128)
            if not upstream_uid:
                raise LegacyMediaDataError("legacy account UID is missing")

            cursors = {
                stream: db.scalar(
                    select(SyncCursor).where(
                        SyncCursor.owner_user_id == owner_user_id,
                        SyncCursor.source == LEGACY_PROVIDER,
                        SyncCursor.stream == stream,
                    )
                )
                for stream in (SOCIAL_STREAM, MOMENTS_STREAM, MESSAGE_PEER_SNAPSHOT_STREAM)
            }
            if not _valid_domain_marker(
                cursors[SOCIAL_STREAM],
                domain="social",
                external_account_id=external.id,
                upstream_uid=upstream_uid,
            ):
                raise LegacyMediaPrerequisiteError(
                    "strict social migration must complete first"
                )
            if not _valid_domain_marker(
                cursors[MOMENTS_STREAM],
                domain="moments",
                external_account_id=external.id,
                upstream_uid=upstream_uid,
            ):
                raise LegacyMediaPrerequisiteError(
                    "strict moments migration must complete first"
                )
            peer_marker = _message_peer_marker(
                cursors[MESSAGE_PEER_SNAPSHOT_STREAM],
                external_account_id=external.id,
                upstream_uid=upstream_uid,
            )
            if peer_marker is None:
                raise LegacyMediaPrerequisiteError(
                    "strict message-peer snapshot must complete first"
                )
            relationship_rows = list(
                db.scalars(
                    select(Relationship)
                    .where(
                        Relationship.owner_user_id == owner_user_id,
                        Relationship.provider == MESSAGE_POLICY_PROVIDER,
                        Relationship.kind == MESSAGE_POLICY_KIND,
                    )
                    .order_by(Relationship.subject_upstream_uid)
                    .limit(MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS + 1)
                )
            )
            if not _valid_message_peer_relationship_snapshot(
                relationship_rows,
                own_uid=upstream_uid,
                marker=peer_marker,
            ):
                raise LegacyMediaPrerequisiteError(
                    "trusted message-peer relationship snapshot is invalid"
                )
            peers = tuple(
                sorted(
                    str(row.subject_upstream_uid).strip()
                    for row in relationship_rows
                    if _relationship_metadata(row).get(
                        "upstream_conversation_snapshot"
                    )
                    is True
                )
            )

            social_marker = _marker_payload(cursors[SOCIAL_STREAM])
            moments_marker = _marker_payload(cursors[MOMENTS_STREAM])
            social_started_at = _marker_datetime(
                social_marker, "coverage_started_at"
            )
            moments_started_at = _marker_datetime(
                moments_marker, "coverage_started_at"
            )
            if (
                _marker_datetime(social_marker, "coverage_ended_at") > ended_at
                or _marker_datetime(moments_marker, "coverage_ended_at") > ended_at
            ):
                raise LegacyMediaPrerequisiteError(
                    "migration prerequisite coverage is in the future"
                )
            retention_days = max(1, int(user.chat_retention_days or 180))
            history_started_at = max(
                _as_utc(user.created_at),
                ended_at - timedelta(days=retention_days),
            )
            window = ImportWindow(
                coverage_started_at=history_started_at,
                coverage_ended_at=ended_at,
            )

            profile_resource = PlannedResource(
                resource_type="profile",
                resource_id=str(user.id),
                container=dict(user.profile or {}),
            )
            post_rows = list(
                db.scalars(
                    select(SocialPost)
                    .where(
                        SocialPost.author_user_id == owner_user_id,
                        SocialPost.source == "legacy-import",
                        SocialPost.source_created_at >= moments_started_at,
                        SocialPost.source_created_at <= ended_at,
                        SocialPost.deleted_at.is_(None),
                        SocialPost.status != "deleted",
                    )
                    .order_by(SocialPost.source_created_at, SocialPost.id)
                    .limit(MAX_MEDIA_SOURCES_PER_ACCOUNT + 1)
                )
            )
            if len(post_rows) > MAX_MEDIA_SOURCES_PER_ACCOUNT:
                raise LegacyMediaLimitError("legacy post media scan limit exceeded")
            post_resources = tuple(
                PlannedResource(
                    resource_type="social_post",
                    resource_id=str(post.id),
                    container=dict(post.media or {}),
                )
                for post in post_rows
            )

        resources = (profile_resource, *post_resources)
        sources = _deduplicate_sources(
            (
                *profile_media_sources(profile_resource),
                *(
                    source
                    for resource in post_resources
                    for source in social_post_media_sources(resource)
                ),
            )
        )
        return LegacyMediaPlan(
            account=LegacyMediaAccount(
                owner_user_id=owner_user_id,
                external_account_id=external.id,
                upstream_uid=upstream_uid,
                chat_retention_days=retention_days,
            ),
            window=window,
            peers=peers,
            resources=tuple(resources),
            sources=sources,
            social_record_digest=str(social_marker.get("record_digest") or ""),
            moments_record_digest=str(moments_marker.get("record_digest") or ""),
            peer_digest=str(peer_marker.get("peer_digest") or ""),
        )

    def begin(self, plan: LegacyMediaPlan) -> None:
        marker = self._base_marker(plan, phase="history")
        marker.update(
            {
                "prerequisites": {
                    "message_peer": plan.peer_digest,
                    "moments": plan.moments_record_digest,
                    "social": plan.social_record_digest,
                },
                "unresolved_records": len(plan.sources),
            }
        )
        with self.db_scope() as db:
            self._write_cursor(
                db,
                plan,
                marker=marker,
                succeeded_at=None,
                error=None,
            )

    @staticmethod
    def _apply_sidecar(
        container: Mapping[str, Any], artifacts: Sequence[ArchivedMedia]
    ) -> dict[str, Any]:
        updated = dict(container)
        if artifacts:
            updated[LOCAL_MEDIA_KEY] = local_media_sidecar(artifacts)
        else:
            updated.pop(LOCAL_MEDIA_KEY, None)
        return updated

    @staticmethod
    def _validate_current_sources(
        container: Mapping[str, Any], sources: Sequence[LegacyMediaSource]
    ) -> None:
        for source in sources:
            current = _resource_current_source(container, source.slot)
            if not current or legacy_source_hash(current) != source.source_hash:
                raise LegacyMediaDataError(
                    "legacy media source changed during migration"
                )

    def bind(
        self,
        plan: LegacyMediaPlan,
        sources: Sequence[LegacyMediaSource],
        artifacts: Sequence[ArchivedMedia],
    ) -> None:
        source_groups: dict[tuple[str, str], list[LegacyMediaSource]] = {}
        artifact_groups: dict[tuple[str, str], list[ArchivedMedia]] = {}
        for source in sources:
            source_groups.setdefault(
                (source.resource_type, source.resource_id), []
            ).append(source)
        for artifact in artifacts:
            artifact_groups.setdefault(
                (artifact.resource_type, artifact.resource_id), []
            ).append(artifact)
        if {
            source.identity() for source in sources
        } != {artifact.identity() for artifact in artifacts}:
            raise LegacyMediaVerificationError(
                "archived media set does not match source metadata"
            )

        resource_keys = {
            (resource.resource_type, resource.resource_id)
            for resource in plan.resources
        } | set(source_groups)
        with self.db_scope() as db:
            for resource_type, resource_id in sorted(resource_keys):
                grouped_sources = source_groups.get((resource_type, resource_id), ())
                grouped_artifacts = artifact_groups.get((resource_type, resource_id), ())
                if resource_type == "profile":
                    row = db.scalar(
                        select(User)
                        .where(
                            User.id == plan.account.owner_user_id,
                            User.id == uuid.UUID(resource_id),
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise LegacyMediaDataError("profile resource disappeared")
                    container = dict(row.profile or {})
                    self._validate_current_sources(container, grouped_sources)
                    row.profile = self._apply_sidecar(container, grouped_artifacts)
                elif resource_type == "social_post":
                    row = db.scalar(
                        select(SocialPost)
                        .where(
                            SocialPost.id == uuid.UUID(resource_id),
                            SocialPost.author_user_id == plan.account.owner_user_id,
                            SocialPost.source == "legacy-import",
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise LegacyMediaDataError("social post resource disappeared")
                    container = dict(row.media or {})
                    self._validate_current_sources(container, grouped_sources)
                    row.media = self._apply_sidecar(container, grouped_artifacts)
                elif resource_type == "message":
                    row = db.scalar(
                        select(Message)
                        .where(
                            Message.id == uuid.UUID(resource_id),
                            Message.owner_user_id == plan.account.owner_user_id,
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise LegacyMediaDataError("message resource disappeared")
                    container = dict(row.extra_data or {})
                    self._validate_current_sources(container, grouped_sources)
                    row.extra_data = self._apply_sidecar(
                        container, grouped_artifacts
                    )
                else:
                    raise LegacyMediaDataError("legacy media resource type is invalid")

    @staticmethod
    def _verify_resource_sidecar(
        container: Mapping[str, Any],
        sources: Sequence[LegacyMediaSource],
    ) -> dict[str, uuid.UUID]:
        references = local_media_references(container)
        expected_slots = {source.slot for source in sources}
        if set(references) != expected_slots:
            raise LegacyMediaVerificationError(
                "local media sidecar does not match retained sources"
            )
        result: dict[str, uuid.UUID] = {}
        for source in sources:
            current = _resource_current_source(container, source.slot)
            reference = references.get(source.slot)
            if (
                reference is None
                or not current
                or legacy_source_hash(current) != source.source_hash
                or reference.source_hash != source.source_hash
            ):
                raise LegacyMediaVerificationError(
                    "local media sidecar source verification failed"
                )
            result[source.slot] = reference.media_id
        return result

    def _verification_records_in_session(
        self,
        db: Any,
        plan: LegacyMediaPlan,
        sources: Sequence[LegacyMediaSource],
    ) -> tuple[ArchivedMedia, ...]:
        grouped: dict[tuple[str, str], list[LegacyMediaSource]] = {}
        for source in sources:
            grouped.setdefault((source.resource_type, source.resource_id), []).append(
                source
            )
        references: dict[tuple[str, str, str], uuid.UUID] = {}
        for (resource_type, resource_id), grouped_sources in sorted(grouped.items()):
            if resource_type == "profile":
                row = db.scalar(
                    select(User).where(
                        User.id == plan.account.owner_user_id,
                        User.id == uuid.UUID(resource_id),
                    )
                )
                container = dict(row.profile or {}) if row is not None else {}
            elif resource_type == "social_post":
                row = db.scalar(
                    select(SocialPost).where(
                        SocialPost.id == uuid.UUID(resource_id),
                        SocialPost.author_user_id == plan.account.owner_user_id,
                        SocialPost.source == "legacy-import",
                    )
                )
                container = dict(row.media or {}) if row is not None else {}
            elif resource_type == "message":
                row = db.scalar(
                    select(Message).where(
                        Message.id == uuid.UUID(resource_id),
                        Message.owner_user_id == plan.account.owner_user_id,
                    )
                )
                container = dict(row.extra_data or {}) if row is not None else {}
            else:
                raise LegacyMediaVerificationError(
                    "legacy media resource type is invalid"
                )
            if row is None:
                raise LegacyMediaVerificationError("legacy media resource is missing")
            ids = self._verify_resource_sidecar(container, grouped_sources)
            for slot, media_id in ids.items():
                references[(resource_type, resource_id, slot)] = media_id

        if len(references) != len(sources):
            raise LegacyMediaVerificationError(
                "legacy media metadata verification is incomplete"
            )
        media_ids = sorted(set(references.values()), key=str)
        rows = (
            list(
                db.scalars(
                    select(MediaObject).where(
                        MediaObject.owner_user_id == plan.account.owner_user_id,
                        MediaObject.id.in_(media_ids),
                    )
                )
            )
            if media_ids
            else []
        )
        by_id = {row.id: row for row in rows}
        records: list[ArchivedMedia] = []
        sources_by_identity = {source.identity(): source for source in sources}
        for identity, media_id in sorted(references.items()):
            row = by_id.get(media_id)
            if (
                row is None
                or row.status != "available"
                or row.deleted_at is not None
            ):
                raise LegacyMediaVerificationError(
                    "referenced media object is not available"
                )
            artifact = _artifact_from_row(row)
            _validate_artifact_shape(artifact)
            source = sources_by_identity[identity]
            if (
                artifact.identity() != identity
                or artifact.source_hash != source.source_hash
                or artifact.owner_user_id != plan.account.owner_user_id
                or artifact.message_id != source.message_id
            ):
                raise LegacyMediaVerificationError(
                    "referenced media object metadata does not match"
                )
            records.append(artifact)
        return tuple(records)

    def verification_records(
        self,
        plan: LegacyMediaPlan,
        sources: Sequence[LegacyMediaSource],
    ) -> tuple[ArchivedMedia, ...]:
        with self.db_scope() as db:
            return self._verification_records_in_session(db, plan, sources)

    @staticmethod
    def _finish_verified_archive_outboxes(
        db: Any,
        plan: LegacyMediaPlan,
        records: Sequence[ArchivedMedia],
        *,
        completed_at: datetime,
    ) -> None:
        """Close only exact legacy ``media.archive`` work after R2 verification.

        Historical sync may have queued the old asynchronous archiver before
        this strict importer existed.  The strict ingestor no longer creates
        new duplicate work, while exact pre-existing rows are marked completed
        only after their source hash is represented by a verified object.
        Any other non-completed media archive row keeps the media Marker from
        being written, so cutover cannot silently strand required work.
        """

        rows = list(
            db.scalars(
                select(OperationOutbox)
                .where(
                    OperationOutbox.owner_user_id == plan.account.owner_user_id,
                    OperationOutbox.operation_type == MEDIA_ARCHIVE_OPERATION,
                )
                .order_by(OperationOutbox.created_at, OperationOutbox.id)
                .with_for_update()
                .limit(MAX_MEDIA_SOURCES_PER_ACCOUNT + 1)
            )
        )
        if len(rows) > MAX_MEDIA_SOURCES_PER_ACCOUNT:
            raise LegacyMediaLimitError("legacy media archive outbox limit exceeded")
        if not rows:
            return
        verified: dict[tuple[uuid.UUID, str], ArchivedMedia] = {}
        message_ids: set[uuid.UUID] = set()
        for record in records:
            if record.message_id is None:
                continue
            message_ids.add(record.message_id)
            verified[(record.message_id, record.slot)] = record
        messages = (
            list(
                db.scalars(
                    select(Message).where(
                        Message.owner_user_id == plan.account.owner_user_id,
                        Message.id.in_(sorted(message_ids, key=str)),
                    )
                )
            )
            if message_ids
            else []
        )
        by_message_id = {message.id: message for message in messages}
        for row in rows:
            if str(row.status or "") == "completed":
                continue
            payload = dict(row.payload or {}) if isinstance(row.payload, Mapping) else {}
            raw_message_id = payload.get("message_id") or row.aggregate_id
            try:
                message_id = uuid.UUID(str(raw_message_id or ""))
            except (TypeError, ValueError, AttributeError):
                continue
            message = by_message_id.get(message_id)
            if message is None:
                continue
            metadata = (
                dict(message.extra_data or {})
                if isinstance(message.extra_data, Mapping)
                else {}
            )
            report = metadata.get("media_report")
            if not isinstance(report, Mapping):
                continue
            source_url = str(report.get("url") or report.get("thumbnail") or "").strip()
            slot = (
                "media_report.url"
                if str(report.get("url") or "").strip()
                else "media_report.thumbnail"
            )
            artifact = verified.get((message_id, slot))
            if (
                not source_url
                or artifact is None
                or artifact.source_hash != legacy_source_hash(source_url)
            ):
                continue
            outbox_digest = hashlib.sha256(
                f"{message_id}\n{source_url}".encode("utf-8", errors="replace")
            ).hexdigest()
            payload_digest = str(payload.get("source_url_hash") or "").strip().lower()
            if (
                str(row.idempotency_key or "") != outbox_digest
                or (payload_digest and payload_digest != outbox_digest)
            ):
                continue
            row.status = "completed"
            row.completed_at = completed_at
            row.locked_by = None
            row.locked_until = None
            row.last_error = None
        unresolved = [row for row in rows if str(row.status or "") != "completed"]
        if unresolved:
            raise LegacyMediaVerificationError(
                "required legacy media archive work is still unresolved"
            )

    def complete(
        self,
        plan: LegacyMediaPlan,
        history: HistorySnapshot,
        records: Sequence[ArchivedMedia],
    ) -> tuple[dict[str, int], str]:
        record_digest = media_record_digest(records)
        counts = {
            "metadata": len(records),
            "objects": len({record.media_id for record in records}),
        }
        with self.db_scope() as db:
            marker_cursor = db.scalar(
                select(SyncCursor)
                .where(
                    SyncCursor.owner_user_id == plan.account.owner_user_id,
                    SyncCursor.source == LEGACY_PROVIDER,
                    SyncCursor.stream == MEDIA_STREAM,
                )
                .with_for_update()
            )
            if marker_cursor is None:
                raise LegacyMediaVerificationError(
                    "media migration progress marker is missing"
                )
            sources = []
            for record in records:
                sources.append(
                    LegacyMediaSource(
                        resource_type=record.resource_type,
                        resource_id=record.resource_id,
                        slot=record.slot,
                        source_url="",
                        source_hash=record.source_hash,
                        kind=record.kind,
                        original_name="",
                        retention_expires_at=PERMANENT_RETENTION_AT,
                        message_id=record.message_id,
                    )
                )
            # Re-read all sidecars and MediaObject rows while holding the marker
            # lock.  Source URLs are recovered from the canonical containers;
            # the placeholder above is never trusted for hash verification.
            current_records = self._verification_records_in_session(
                db, plan, tuple(sources)
            )
            if (
                media_record_digest(current_records) != record_digest
                or len(current_records) != len(records)
            ):
                raise LegacyMediaVerificationError(
                    "media metadata changed after object verification"
                )
            completed_at = max(_as_utc(self.clock()), plan.window.coverage_ended_at)
            self._finish_verified_archive_outboxes(
                db,
                plan,
                current_records,
                completed_at=completed_at,
            )
            marker = self._base_marker(plan, phase="complete", complete=True)
            marker.update(
                {
                    "scopes": list(DOMAIN_MARKER_SCOPES["media"]),
                    "counts": counts,
                    "source_paths": list(MEDIA_SOURCE_PATHS),
                    "record_digest": record_digest,
                    "unresolved_records": 0,
                    "history": {
                        "complete": True,
                        "directions": len(history.directions),
                        "messages": history.message_count,
                        "pages": history.page_count,
                        "record_digest": history.record_digest,
                    },
                    "prerequisites": {
                        "message_peer": plan.peer_digest,
                        "moments": plan.moments_record_digest,
                        "social": plan.social_record_digest,
                    },
                }
            )
            self._write_cursor(
                db,
                plan,
                marker=marker,
                succeeded_at=completed_at,
                error=None,
            )
        return counts, record_digest

    def fail(self, plan: LegacyMediaPlan, *, code: str) -> None:
        marker = self._base_marker(plan, phase="failed")
        marker["unresolved_records"] = max(1, len(plan.sources))
        with self.db_scope() as db:
            self._write_cursor(
                db,
                plan,
                marker=marker,
                succeeded_at=None,
                error=str(code or "legacy_media_migration_failed")[:160],
            )


class SqlAlchemyLegacyMessageIngestor:
    """Ingest the already-complete network snapshot in bounded DB batches."""

    def __init__(
        self,
        settings: Settings,
        *,
        db_scope: DbScope = session_scope,
    ) -> None:
        self.settings = settings
        self.db_scope = db_scope

    def ingest(
        self,
        plan: LegacyMediaPlan,
        history: HistorySnapshot,
    ) -> LegacyMessageIngestResult:
        # Import lazily so merely inspecting this offline module does not boot
        # the background worker integration.
        from bbw_web.jobs import _history_message_report, _ingest_message

        pending: list[dict[str, Any]] = []
        seen: set[str] = set()
        for direction in history.directions:
            raw_items = [
                dict(item)
                for page in direction.pages
                for item in page.items
            ]
            normalized = normalize_messages(raw_items)
            if len(normalized) != len(raw_items):
                raise LegacyMediaDataError(
                    "TIM roaming history normalization was incomplete"
                )
            for item in normalized:
                report = _history_message_report(
                    item,
                    account_uid=plan.account.upstream_uid,
                    requested_peer=direction.peer_uid,
                )
                if report is None:
                    raise LegacyMediaDataError(
                        "TIM roaming history message identity is invalid"
                    )
                identity = _sha256_json(
                    {
                        "id": report.get("upstream_message_id"),
                        "key": report.get("message_key"),
                        "peer": direction.peer_uid,
                        "random": report.get("message_random"),
                        "sequence": report.get("message_sequence"),
                    }
                )
                if identity in seen:
                    continue
                seen.add(identity)
                pending.append(report)

        sources: list[LegacyMediaSource] = []
        written = 0
        for start in range(0, len(pending), MAX_INGEST_BATCH):
            batch = pending[start : start + MAX_INGEST_BATCH]
            with self.db_scope() as db:
                binding = db.execute(
                    select(User, ExternalAccount)
                    .join(ExternalAccount, ExternalAccount.user_id == User.id)
                    .where(
                        User.id == plan.account.owner_user_id,
                        User.status == "active",
                        User.disabled_at.is_(None),
                        ExternalAccount.id == plan.account.external_account_id,
                        ExternalAccount.provider == LEGACY_PROVIDER,
                        ExternalAccount.upstream_uid == plan.account.upstream_uid,
                    )
                ).one_or_none()
                if binding is None:
                    raise LegacyMediaDataError(
                        "legacy account binding changed during message ingestion"
                    )
                user, external = binding
                for report in batch:
                    row, _created, _outbox_id = _ingest_message(
                        db,
                        settings=self.settings,
                        user=user,
                        account=external,
                        report=report,
                        enqueue_media_archive=False,
                    )
                    written += 1
                    media = report.get("media")
                    if isinstance(media, Mapping):
                        sources.extend(
                            message_media_sources(
                                message_id=row.id,
                                media_report=media,
                                message_kind=str(report.get("message_type") or ""),
                                retention_expires_at=row.retention_expires_at,
                            )
                        )
        return LegacyMessageIngestResult(
            messages_written=written,
            media_sources=_deduplicate_sources(sources),
        )


class R2LegacyMediaArchiver:
    """Archive one source without holding a DB transaction during download/R2 I/O."""

    def __init__(
        self,
        settings: Settings,
        *,
        storage: R2Storage | None = None,
        db_scope: DbScope = session_scope,
        clock=utcnow,
    ) -> None:
        self.settings = settings
        self.storage = storage or R2Storage(settings)
        self.db_scope = db_scope
        self.clock = clock
        configured = getattr(settings, "media_allowed_hosts", ())
        if isinstance(configured, str):
            self.allowed_hosts = tuple(
                value.strip() for value in configured.split(",") if value.strip()
            )
        else:
            self.allowed_hosts = tuple(
                str(value).strip() for value in configured if str(value).strip()
            )
        if not self.allowed_hosts:
            raise LegacyMediaArchiveError("media archive allowlist is empty")

    @staticmethod
    def _matches_source(row: MediaObject, source: LegacyMediaSource) -> bool:
        metadata = dict(row.extra_data or {}) if isinstance(row.extra_data, Mapping) else {}
        legacy = metadata.get(LEGACY_MEDIA_METADATA_KEY)
        return bool(
            isinstance(legacy, Mapping)
            and legacy.get("schema") == LOCAL_MEDIA_SCHEMA
            and str(legacy.get("source_hash") or "") == source.source_hash
            and str(legacy.get("slot") or "") == source.slot
            and str(legacy.get("resource_type") or "") == source.resource_type
            and str(legacy.get("resource_id") or "") == source.resource_id
        )

    def _existing(
        self,
        account: LegacyMediaAccount,
        source: LegacyMediaSource,
    ) -> MediaObject | None:
        with self.db_scope() as db:
            rows = list(
                db.scalars(
                    select(MediaObject)
                    .where(
                        MediaObject.owner_user_id == account.owner_user_id,
                        MediaObject.deleted_at.is_(None),
                        MediaObject.status.in_(("pending", "uploading", "available")),
                    )
                    .order_by(MediaObject.created_at.desc())
                    .limit(MAX_EXISTING_MEDIA_SCAN + 1)
                )
            )
            if len(rows) > MAX_EXISTING_MEDIA_SCAN:
                raise LegacyMediaLimitError("legacy media object scan limit exceeded")
            row = next((item for item in rows if self._matches_source(item, source)), None)
            if row is None:
                return None
            # Return a detached immutable-enough row snapshot through the ORM
            # fields consumed by _artifact_from_row.
            db.expunge(row)
            return row

    def _head_matches(self, artifact: ArchivedMedia) -> bool:
        if artifact.bucket != self.storage.bucket:
            return False
        head = self.storage.head_object(artifact.object_key)
        if not isinstance(head, Mapping):
            return False
        metadata = head.get("metadata")
        remote_sha = (
            str(metadata.get("sha256") or "").strip().lower()
            if isinstance(metadata, Mapping)
            else ""
        )
        return bool(
            int(head.get("size") or -1) == artifact.size_bytes
            and remote_sha == artifact.sha256
        )

    def _repair_existing(
        self,
        account: LegacyMediaAccount,
        source: LegacyMediaSource,
        row: MediaObject,
    ) -> ArchivedMedia | None:
        artifact = _artifact_from_row(row)
        _validate_artifact_shape(artifact)
        if artifact.message_id != source.message_id:
            raise LegacyMediaVerificationError(
                "legacy media object message binding is invalid"
            )
        if row.status == "available":
            if not self._head_matches(artifact):
                raise LegacyMediaVerificationError(
                    "available legacy media object failed R2 verification"
                )
            return artifact
        if self._head_matches(artifact):
            with self.db_scope() as db:
                current = db.scalar(
                    select(MediaObject)
                    .where(
                        MediaObject.id == row.id,
                        MediaObject.owner_user_id == account.owner_user_id,
                    )
                    .with_for_update()
                )
                if current is None or not self._matches_source(current, source):
                    raise LegacyMediaVerificationError(
                        "legacy media reservation changed during repair"
                    )
                current.status = "available"
                current.retention_expires_at = source.retention_expires_at
                current.extra_data = {
                    **dict(current.extra_data or {}),
                    "archived_at": _as_utc(self.clock()).isoformat(),
                }
            return artifact
        # Deletion is deliberately ordered before quota release.  A HEAD
        # mismatch can mean that an object exists with corrupt/unexpected
        # metadata; releasing the ledger first would leave an unaccounted R2
        # orphan.  S3/R2 delete is idempotent when the key is already absent.
        try:
            self.storage.delete(artifact.object_key)
        except Exception as exc:
            raise LegacyMediaArchiveError(
                "stale legacy media object could not be deleted"
            ) from exc
        with self.db_scope() as db:
            current = db.scalar(
                select(MediaObject)
                .where(
                    MediaObject.id == row.id,
                    MediaObject.owner_user_id == account.owner_user_id,
                )
                .with_for_update()
            )
            if current is not None:
                MediaQuotaService(db, self.settings).release(
                    owner_user_id=account.owner_user_id,
                    media_id=current.id,
                    final_status="failed",
                )
        return None

    def archive(
        self,
        account: LegacyMediaAccount,
        source: LegacyMediaSource,
    ) -> tuple[ArchivedMedia, bool]:
        existing = self._existing(account, source)
        if existing is not None:
            repaired = self._repair_existing(account, source, existing)
            if repaired is not None:
                return repaired, False

        prepared = None
        reserved_id: uuid.UUID | None = None
        object_verified = False
        object_key = ""
        try:
            prepared = download_and_prepare(
                url=source.source_url,
                kind=source.kind,
                allowed_hosts=self.allowed_hosts,
                original_name=source.original_name,
                settings=self.settings,
            )
            object_key = self.storage.object_key(
                str(account.owner_user_id),
                prepared.extension,
                prefix="legacy-media",
            )
            with self.db_scope() as db:
                reservation = MediaQuotaService(db, self.settings).reserve(
                    owner_user_id=account.owner_user_id,
                    kind=source.kind,
                    size_bytes=prepared.size,
                    r2_bucket=self.storage.bucket,
                    r2_object_key=object_key,
                    content_type=prepared.content_type,
                    sha256=prepared.sha256,
                    message_id=source.message_id,
                    original_filename=source.original_name,
                    source_url=None,
                    metadata={
                        LEGACY_MEDIA_METADATA_KEY: _legacy_object_metadata(source)
                    },
                )
                reservation.media.status = "uploading"
                reservation.media.retention_expires_at = source.retention_expires_at
                reserved_id = reservation.media.id
            with prepared.path.open("rb") as handle:
                stored = self.storage.upload_stream(
                    key=object_key,
                    stream=handle,
                    size=prepared.size,
                    content_type=prepared.content_type,
                    sha256=prepared.sha256,
                    metadata={"archive": "legacy-media-v1"},
                )
            artifact = ArchivedMedia(
                resource_type=source.resource_type,
                resource_id=source.resource_id,
                slot=source.slot,
                source_hash=source.source_hash,
                media_id=reserved_id,
                owner_user_id=account.owner_user_id,
                bucket=self.storage.bucket,
                object_key=object_key,
                kind=source.kind,
                content_type=prepared.content_type,
                size_bytes=prepared.size,
                sha256=prepared.sha256,
                message_id=source.message_id,
            )
            object_verified = self._head_matches(artifact)
            if not object_verified:
                raise LegacyMediaVerificationError(
                    "new legacy media object failed R2 verification"
                )
            with self.db_scope() as db:
                row = MediaQuotaService(db, self.settings).mark_available(
                    account.owner_user_id, reserved_id
                )
                row.retention_expires_at = source.retention_expires_at
                row.extra_data = {
                    **dict(row.extra_data or {}),
                    "archived_at": _as_utc(self.clock()).isoformat(),
                    "etag": str(stored.etag or "")[:160],
                }
            return artifact, True
        except LegacyMediaMigrationError:
            raise
        except Exception as exc:
            raise LegacyMediaArchiveError("legacy media archival failed") from exc
        finally:
            if prepared is not None:
                prepared.cleanup()
            if reserved_id is not None and not object_verified:
                object_deleted = not object_key
                if object_key:
                    try:
                        self.storage.delete(object_key)
                        object_deleted = True
                    except Exception:
                        object_deleted = False
                if object_deleted:
                    try:
                        with self.db_scope() as db:
                            MediaQuotaService(db, self.settings).release(
                                owner_user_id=account.owner_user_id,
                                media_id=reserved_id,
                                final_status="failed",
                            )
                    except Exception:
                        pass

    def verify(self, artifact: ArchivedMedia) -> bool:
        _validate_artifact_shape(artifact)
        return self._head_matches(artifact)


def _active_owner_ids(*, db_scope: DbScope = session_scope) -> tuple[uuid.UUID, ...]:
    with db_scope() as db:
        return tuple(
            db.scalars(
                select(User.id)
                .join(ExternalAccount, ExternalAccount.user_id == User.id)
                .where(
                    User.status == "active",
                    User.disabled_at.is_(None),
                    ExternalAccount.provider == LEGACY_PROVIDER,
                    ExternalAccount.sync_enabled.is_(True),
                )
                .order_by(User.id)
            )
        )


def run_legacy_media_import(
    owner_user_id: uuid.UUID | str,
    *,
    settings: Settings | None = None,
    transport_factory: Callable[[], Any] | None = None,
    writer: LegacyMediaWriter | None = None,
    ingestor: LegacyMessageIngestor | None = None,
    archiver: LegacyMediaArchiver | None = None,
    clock=utcnow,
) -> LegacyMediaImportSummary:
    resolved_settings = settings or get_settings()
    if transport_factory is None:
        from bbw_web.jobs import _default_message_history_transport

        transport_factory = _default_message_history_transport
    resolved_writer = writer or SqlAlchemyLegacyMediaWriter(clock=clock)
    resolved_ingestor = ingestor or SqlAlchemyLegacyMessageIngestor(
        resolved_settings
    )
    resolved_archiver = archiver or R2LegacyMediaArchiver(
        resolved_settings, clock=clock
    )
    return LegacyMediaMigration(
        reader=TimRoamingHistoryReader(transport_factory()),
        ingestor=resolved_ingestor,
        archiver=resolved_archiver,
        writer=resolved_writer,
        clock=clock,
    ).run(owner_user_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="严格归档历史头像、动态和私聊媒体并写 media 完成 Marker"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--owner-user-id",
        help="经审核的内部 User UUID；不会出现在命令输出中",
    )
    target.add_argument(
        "--all-active",
        action="store_true",
        help="处理全部绑定 beibeiwu 的 active 账号",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    owners: Sequence[uuid.UUID | str] = (
        _active_owner_ids() if args.all_active else (str(args.owner_user_id or ""),)
    )
    totals = {
        "history_pages": 0,
        "history_messages": 0,
        "messages_written": 0,
        "metadata_records": 0,
        "object_records": 0,
        "archived_created": 0,
        "archived_reused": 0,
    }
    completed = 0
    failed = 0
    failure_codes: dict[str, int] = {}
    for owner_id in owners:
        try:
            summary = run_legacy_media_import(owner_id)
        except LegacyMediaMigrationError as exc:
            failed += 1
            failure_codes[exc.code] = failure_codes.get(exc.code, 0) + 1
            continue
        except Exception:
            failed += 1
            code = "legacy_media_internal_error"
            failure_codes[code] = failure_codes.get(code, 0) + 1
            continue
        completed += 1
        for field in totals:
            totals[field] += int(getattr(summary, field))
    print(
        json.dumps(
            {
                "ok": failed == 0,
                "accounts_total": len(owners),
                "accounts_completed": completed,
                "accounts_failed": failed,
                "failure_codes": failure_codes,
                **totals,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=stdout or sys.stdout,
    )
    return 0 if failed == 0 else 1


__all__ = [
    "ArchivedMedia",
    "HistoryDirection",
    "HistoryPage",
    "HistorySnapshot",
    "ImportWindow",
    "LegacyMediaAccount",
    "LegacyMediaArchiveError",
    "LegacyMediaDataError",
    "LegacyMediaImportSummary",
    "LegacyMediaIngestError",
    "LegacyMediaLimitError",
    "LegacyMediaMigration",
    "LegacyMediaMigrationError",
    "LegacyMediaPlan",
    "LegacyMediaPrerequisiteError",
    "LegacyMediaProviderError",
    "LegacyMediaSource",
    "LegacyMediaVerificationError",
    "LegacyMessageIngestResult",
    "MEDIA_SOURCE_PATHS",
    "R2LegacyMediaArchiver",
    "SqlAlchemyLegacyMediaWriter",
    "SqlAlchemyLegacyMessageIngestor",
    "TimRoamingHistoryReader",
    "fetch_complete_roaming_history",
    "legacy_source_hash",
    "local_media_sidecar",
    "main",
    "media_record_digest",
    "message_media_sources",
    "profile_media_sources",
    "run_legacy_media_import",
    "social_post_media_sources",
]


if __name__ == "__main__":
    raise SystemExit(main())
