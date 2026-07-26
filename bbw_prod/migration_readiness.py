"""Cutover readiness report for retiring legacy providers and APK mirrors.

The command emits aggregate counts and stable capability codes only. It never
decrypts credentials, prints account identifiers, or mutates PostgreSQL/Redis.
It does create and immediately remove one isolated private R2 probe object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence, TextIO

from sqlalchemy import and_, func, or_, select

from .db import session_scope
from .models import (
    ExternalAccount,
    MessageDelivery,
    OperationOutbox,
    Relationship,
    SyncCursor,
    User,
    UserCredential,
)
from .security import is_safe_user_password_hash


SNAPSHOT_SCHEMA = 1
SNAPSHOT_KINDS = ("blacklist", "blacklisted_by")
SNAPSHOT_PATHS = {
    "blacklist": "/api/social/blacklist",
    "blacklisted_by": "/api/social/blacklist-me",
}
SNAPSHOT_STREAMS = {
    kind: f"message-block-snapshot:{kind}" for kind in SNAPSHOT_KINDS
}
SNAPSHOT_MAX_RELATIONSHIPS = 5000
SNAPSHOT_TRUSTED_SOURCES = {
    "blacklist": frozenset(
        {
            "/api/social/blacklist",
            "/api/social/blacklist-add",
            "/api/social/blacklist-del",
        }
    ),
    "blacklisted_by": frozenset({"/api/social/blacklist-me"}),
}
MESSAGE_POLICY_PROVIDER = "web-policy"
MESSAGE_POLICY_CONVERSATION_KIND = "message_peer"
MESSAGE_PEER_SNAPSHOT_SCHEMA = 1
MESSAGE_PEER_SNAPSHOT_STREAM = "message-peer-snapshot"
MESSAGE_PEER_SNAPSHOT_PATH = "/api/im/conversations"
MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS = 5000

# A local table existing is not proof that the corresponding legacy history was
# migrated. Each domain therefore needs an account-bound, auditable completion
# marker. Importers are the only components that should write these markers;
# this readiness command remains strictly read-only.
DOMAIN_MARKER_SCHEMA = 1
DOMAIN_MARKER_MAX_RECORDS = 10_000_000
DOMAIN_MARKER_STREAMS = {
    "social": "migration-domain:social",
    "moments": "migration-domain:moments",
    "discovery": "migration-domain:discovery",
    "media": "migration-domain:media",
}
DOMAIN_MARKER_SCOPES = {
    "social": (
        "profile",
        "relationships",
        "friend-requests",
        "blocklists",
    ),
    "moments": ("posts", "comments", "topics"),
    "discovery": ("profile", "preferences", "text-match"),
    "media": ("metadata", "objects"),
}
SOCIAL_MARKER_SOURCE_PATHS = (
    "/api/profile/user",
    "/api/social/follows",
    "/api/social/fans",
    "/api/social/friends",
    "/api/social/friend-apply",
    "/api/social/blacklist",
    "/api/social/blacklist-me",
)
SOCIAL_MARKER_SCOPE_PATHS = {
    "profile": ("/api/profile/user",),
    "relationships": (
        "/api/social/follows",
        "/api/social/fans",
        "/api/social/friends",
    ),
    "friend-requests": ("/api/social/friend-apply",),
    "blocklists": (
        "/api/social/blacklist",
        "/api/social/blacklist-me",
    ),
}
SOCIAL_MARKER_TERMINALS = frozenset(
    {"single-response", "empty-page", "false-empty", "explicit-complete"}
)
MOMENTS_HISTORY_DAYS = 180
MOMENTS_MARKER_SOURCE_PATHS = ("someonesluntannew", "getMainComment")
DISCOVERY_MARKER_SOURCE_PATHS = (
    "/api/profile/user",
    "postgresql://match_preferences",
    "postgresql://match_results",
)
DISCOVERY_PROFILE_SOURCE_MARKER = DOMAIN_MARKER_STREAMS["social"]
MEDIA_MARKER_SOURCE_PATHS = (
    "postgresql://users.profile",
    "postgresql://social_posts.media",
    "tim://openim/admin_getroammsg",
    "r2://private-media",
)
MEDIA_HISTORY_MAX_PAGES_PER_DIRECTION = 1000
MEDIA_HISTORY_MAX_MESSAGES_PER_ACCOUNT = 1_000_000
COMPATIBILITY_OUTBOX_PREFIX = "compatibility."
# 与 bbw_web.jobs.MEDIA_OPERATION 保持一致（bbw_prod 不能反向依赖 bbw_web）。
MEDIA_ARCHIVE_OPERATION_TYPE = "media.archive"
LEGACY_MEDIA_ARCHIVE_OPERATION_PREFIX = "compatibility.media.archive"
R2_CAPABILITY_PROBE_PREFIX = "health/migration-readiness"
R2_CAPABILITY_PROBE_CONTENT_TYPE = "application/octet-stream"
R2_CAPABILITY_ERROR_NOT_CHECKED = "r2_probe_not_checked"
R2_CAPABILITY_ERROR_INITIALIZATION = "r2_initialization_failed"
R2_CAPABILITY_ERROR_WRITE = "r2_write_failed"
R2_CAPABILITY_ERROR_WRITE_VERIFICATION = "r2_write_verification_failed"
R2_CAPABILITY_ERROR_HEAD = "r2_head_failed"
R2_CAPABILITY_ERROR_HEAD_VERIFICATION = "r2_head_verification_failed"
R2_CAPABILITY_ERROR_GET = "r2_get_failed"
R2_CAPABILITY_ERROR_GET_VERIFICATION = "r2_get_verification_failed"
R2_CAPABILITY_ERROR_DELETE = "r2_delete_failed"
R2_CAPABILITY_ERROR_DELETE_CONFIRMATION = "r2_delete_confirmation_failed"
R2_CAPABILITY_ERROR_DELETE_VERIFICATION = "r2_delete_verification_failed"


def _ordinary_compatibility_outbox_predicate() -> Any:
    return and_(
        OperationOutbox.operation_type.like(f"{COMPATIBILITY_OUTBOX_PREFIX}%"),
        OperationOutbox.operation_type.not_like(
            f"{LEGACY_MEDIA_ARCHIVE_OPERATION_PREFIX}%"
        ),
    )


def _media_archive_outbox_predicate() -> Any:
    return or_(
        OperationOutbox.operation_type == MEDIA_ARCHIVE_OPERATION_TYPE,
        OperationOutbox.operation_type.like(
            f"{LEGACY_MEDIA_ARCHIVE_OPERATION_PREFIX}%"
        ),
    )


@dataclass(frozen=True, slots=True)
class R2CapabilityResult:
    write_ready: bool
    read_ready: bool
    delete_ready: bool
    error_code: str | None

    @property
    def ready(self) -> bool:
        return bool(
            self.write_ready
            and self.read_ready
            and self.delete_ready
            and self.error_code is None
        )


R2_CAPABILITY_NOT_CHECKED = R2CapabilityResult(
    write_ready=False,
    read_ready=False,
    delete_ready=False,
    error_code=R2_CAPABILITY_ERROR_NOT_CHECKED,
)
R2_CAPABILITY_READY = R2CapabilityResult(
    write_ready=True,
    read_ready=True,
    delete_ready=True,
    error_code=None,
)


def probe_r2_capabilities(
    *,
    settings: Any | None = None,
    storage: Any | None = None,
) -> R2CapabilityResult:
    """Prove current private R2 PUT, HEAD/GET and DELETE capabilities.

    The random key and payload are deliberately excluded from the result. All
    provider exceptions are reduced to stable operation codes so CLI output
    cannot disclose an endpoint, credential source, bucket, or object key.
    """

    if storage is None:
        try:
            from bbw_web.r2 import R2Storage

            from .config import get_settings

            storage = R2Storage(settings or get_settings())
        except Exception:
            return R2CapabilityResult(
                write_ready=False,
                read_ready=False,
                delete_ready=False,
                error_code=R2_CAPABILITY_ERROR_INITIALIZATION,
            )

    key = f"{R2_CAPABILITY_PROBE_PREFIX}/{uuid.uuid4().hex}.probe"
    payload = b"bbw-cutover-r2-probe-v1\n" + secrets.token_bytes(32)
    digest = hashlib.sha256(payload).hexdigest()
    put_completed = False
    write_ready = False
    read_ready = False
    delete_ready = False
    operation_error: str | None = None

    try:
        stored = storage.upload_bytes(
            key=key,
            data=payload,
            content_type=R2_CAPABILITY_PROBE_CONTENT_TYPE,
            metadata={"probe": "migration-readiness-v1"},
        )
        put_completed = True
        try:
            stored_size = int(getattr(stored, "size", -1))
        except (TypeError, ValueError, OverflowError):
            stored_size = -1
        if (
            str(getattr(stored, "key", "") or "") != key
            or stored_size != len(payload)
            or str(getattr(stored, "sha256", "") or "") != digest
        ):
            operation_error = R2_CAPABILITY_ERROR_WRITE_VERIFICATION
        else:
            write_ready = True
    except Exception:
        operation_error = R2_CAPABILITY_ERROR_WRITE

    if write_ready:
        try:
            head = storage.head_object(key)
        except Exception:
            operation_error = R2_CAPABILITY_ERROR_HEAD
        else:
            head_metadata = head.get("metadata") if isinstance(head, Mapping) else None
            try:
                head_size = int(head.get("size", -1)) if isinstance(head, Mapping) else -1
            except (TypeError, ValueError, OverflowError):
                head_size = -1
            if (
                not isinstance(head, Mapping)
                or head_size != len(payload)
                or not isinstance(head_metadata, Mapping)
                or str(head_metadata.get("sha256") or "") != digest
            ):
                operation_error = R2_CAPABILITY_ERROR_HEAD_VERIFICATION
            else:
                try:
                    downloaded = storage.get_bytes(key, max_bytes=4096)
                except Exception:
                    operation_error = R2_CAPABILITY_ERROR_GET
                else:
                    if downloaded != payload:
                        operation_error = R2_CAPABILITY_ERROR_GET_VERIFICATION
                    else:
                        read_ready = True

    delete_error: str | None = None
    try:
        storage.delete(key)
    except Exception:
        delete_error = R2_CAPABILITY_ERROR_DELETE
    else:
        try:
            remaining = storage.head_object(key)
        except Exception:
            delete_error = R2_CAPABILITY_ERROR_DELETE_CONFIRMATION
        else:
            if remaining is not None:
                delete_error = R2_CAPABILITY_ERROR_DELETE_VERIFICATION
            elif put_completed:
                delete_ready = True

    if put_completed and delete_error is not None:
        operation_error = delete_error
    if not (write_ready and read_ready and delete_ready):
        return R2CapabilityResult(
            write_ready=write_ready,
            read_ready=read_ready,
            delete_ready=delete_ready,
            error_code=operation_error or R2_CAPABILITY_ERROR_WRITE,
        )
    return R2_CAPABILITY_READY


@dataclass(frozen=True, slots=True)
class MigrationReadinessReport:
    provider: str
    provider_accounts: int
    active_accounts: int
    inactive_accounts: int
    enabled_credentials: int
    local_login_ready_accounts: int
    local_message_identity_ready_accounts: int
    block_snapshot_ready_accounts: int
    message_peer_snapshot_ready_accounts: int
    local_core_ready_accounts: int
    social_ready_accounts: int
    moments_ready_accounts: int
    discovery_ready_accounts: int
    media_ready_accounts: int
    all_domains_ready_accounts: int
    cutover_ready_accounts: int
    fully_ready_accounts: int
    missing_uid_accounts: int
    duplicate_user_bindings: int
    duplicate_user_binding_accounts: int
    duplicate_uid_values: int
    duplicate_uid_accounts: int
    missing_credentials: int
    disabled_credentials: int
    invalid_credentials: int
    backfill_candidates: int
    missing_block_snapshot_accounts: int
    invalid_block_snapshot_accounts: int
    invalid_block_relationship_accounts: int
    missing_message_peer_snapshot_accounts: int
    invalid_message_peer_snapshot_accounts: int
    invalid_message_peer_relationship_accounts: int
    missing_social_marker_accounts: int
    invalid_social_marker_accounts: int
    missing_moments_marker_accounts: int
    invalid_moments_marker_accounts: int
    missing_discovery_marker_accounts: int
    invalid_discovery_marker_accounts: int
    missing_media_marker_accounts: int
    invalid_media_marker_accounts: int
    compatibility_outbox_total: int
    compatibility_outbox_pending: int
    compatibility_outbox_retry: int
    compatibility_outbox_processing: int
    compatibility_outbox_failed: int
    compatibility_outbox_other_unfinished: int
    compatibility_outbox_unfinished: int
    compatibility_outbox_oldest_unfinished_at: str | None
    media_archive_outbox_total: int
    media_archive_outbox_pending: int
    media_archive_outbox_retry: int
    media_archive_outbox_processing: int
    media_archive_outbox_failed: int
    media_archive_outbox_other_unfinished: int
    media_archive_outbox_unfinished: int
    media_archive_outbox_oldest_unfinished_at: str | None
    tim_delivery_total: int
    tim_delivery_pending: int
    tim_delivery_retry: int
    tim_delivery_processing: int
    tim_delivery_failed: int
    tim_delivery_other_unfinished: int
    tim_delivery_unfinished: int
    tim_delivery_oldest_unfinished_at: str | None
    local_core_ready: bool
    domain_data_ready: bool
    compatibility_outbox_ready: bool
    media_archive_outbox_ready: bool
    tim_delivery_ready: bool
    r2_write_ready: bool
    r2_read_ready: bool
    r2_delete_ready: bool
    r2_storage_ready: bool
    r2_capability_error_code: str | None
    cutover_ready: bool
    ready: bool


def _value(row: Any, name: str, default: Any = None) -> Any:
    mapping = getattr(row, "_mapping", None)
    if isinstance(mapping, Mapping) and name in mapping:
        return mapping[name]
    return getattr(row, name, default)


def _uid(value: Any) -> str:
    uid = str(value or "").strip()
    if (
        not uid
        or uid.lower() in {"0", "none", "null"}
        or len(uid) > 128
        or any(ord(char) < 33 for char in uid)
    ):
        return ""
    return uid


def _valid_password_hash(value: Any) -> bool:
    return is_safe_user_password_hash(value)


def _valid_relationship_snapshot(rows: Sequence[Any], *, own_uid: str) -> bool:
    if len(rows) > SNAPSHOT_MAX_RELATIONSHIPS:
        return False
    for row in rows:
        kind = str(_value(row, "kind") or "")
        peer = _uid(_value(row, "subject_upstream_uid"))
        metadata = _value(
            row,
            "relationship_metadata",
            _value(row, "extra_data", {}),
        )
        if not isinstance(metadata, Mapping):
            return False
        if (
            kind not in SNAPSHOT_TRUSTED_SOURCES
            or not peer
            or peer == own_uid
            or metadata.get("server_owned") is not True
            or metadata.get("message_policy_source")
            not in SNAPSHOT_TRUSTED_SOURCES[kind]
        ):
            return False
    return True


def _valid_snapshot_marker(
    cursor: Any,
    *,
    kind: str,
    external_account_id: Any,
    upstream_uid: str,
) -> bool:
    if (
        cursor is None
        or getattr(cursor, "last_succeeded_at", None) is None
        or getattr(cursor, "watermark_at", None) is None
        or str(getattr(cursor, "last_error", "") or "").strip()
    ):
        return False
    try:
        marker = json.loads(str(getattr(cursor, "cursor", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(marker, Mapping)
        and marker.get("complete") is True
        and marker.get("schema") == SNAPSHOT_SCHEMA
        and marker.get("kind") == kind
        and marker.get("source_path") == SNAPSHOT_PATHS[kind]
        and marker.get("external_account_id") == str(external_account_id)
        and marker.get("upstream_uid") == upstream_uid
    )


def _message_peer_snapshot_digest(peers: Sequence[str]) -> str:
    normalized = sorted(set(str(peer or "").strip() for peer in peers))
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _message_peer_marker(
    cursor: Any,
    *,
    external_account_id: Any,
    upstream_uid: str,
) -> Mapping[str, Any] | None:
    if (
        cursor is None
        or getattr(cursor, "last_succeeded_at", None) is None
        or getattr(cursor, "watermark_at", None) is None
        or str(getattr(cursor, "last_error", "") or "").strip()
    ):
        return None
    try:
        marker = json.loads(str(getattr(cursor, "cursor", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(marker, Mapping):
        return None
    peer_count = marker.get("peer_count")
    peer_digest = str(marker.get("peer_digest") or "")
    if (
        marker.get("complete") is not True
        or marker.get("schema") != MESSAGE_PEER_SNAPSHOT_SCHEMA
        or marker.get("source_path") != MESSAGE_PEER_SNAPSHOT_PATH
        or marker.get("external_account_id") != str(external_account_id)
        or marker.get("upstream_uid") != upstream_uid
        or not isinstance(peer_count, int)
        or not 0 <= peer_count <= MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS
        or len(peer_digest) != 64
        or any(character not in "0123456789abcdef" for character in peer_digest)
    ):
        return None
    return marker


def _valid_message_peer_relationship_snapshot(
    rows: Sequence[Any],
    *,
    own_uid: str,
    marker: Mapping[str, Any],
) -> bool:
    if len(rows) > MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS:
        return False
    peers: list[str] = []
    for row in rows:
        metadata = _value(
            row,
            "relationship_metadata",
            _value(row, "extra_data", {}),
        )
        if not isinstance(metadata, Mapping):
            return False
        if metadata.get("upstream_conversation_snapshot") is not True:
            continue
        peer = _uid(_value(row, "subject_upstream_uid"))
        if (
            not peer
            or peer == own_uid
            or metadata.get("server_owned") is not True
            or metadata.get("upstream_conversation_source_path")
            != MESSAGE_PEER_SNAPSHOT_PATH
        ):
            return False
        peers.append(peer)
    if len(peers) != len(set(peers)):
        return False
    return bool(
        len(peers) == marker.get("peer_count")
        and _message_peer_snapshot_digest(peers) == marker.get("peer_digest")
    )


def _marker_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _valid_sha256_digest(value: Any) -> bool:
    digest = str(value or "")
    return bool(
        len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _domain_marker_payload(cursor: Any) -> Mapping[str, Any] | None:
    try:
        marker = json.loads(str(getattr(cursor, "cursor", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return marker if isinstance(marker, Mapping) else None


def _valid_domain_marker(
    cursor: Any,
    *,
    domain: str,
    external_account_id: Any,
    upstream_uid: str,
    media_prerequisite_digests: Mapping[str, str] | None = None,
    media_peer_count: int | None = None,
) -> bool:
    if (
        cursor is None
        or getattr(cursor, "last_succeeded_at", None) is None
        or getattr(cursor, "watermark_at", None) is None
        or str(getattr(cursor, "last_error", "") or "").strip()
    ):
        return False
    try:
        marker = json.loads(str(getattr(cursor, "cursor", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(marker, Mapping):
        return False

    expected_scopes = DOMAIN_MARKER_SCOPES[domain]
    scopes = marker.get("scopes")
    counts = marker.get("counts")
    source_paths = marker.get("source_paths")
    digest = str(marker.get("record_digest") or "")
    coverage_started_at = _marker_datetime(marker.get("coverage_started_at"))
    coverage_ended_at = _marker_datetime(marker.get("coverage_ended_at"))
    watermark_at = _marker_datetime(getattr(cursor, "watermark_at", None))
    if (
        marker.get("complete") is not True
        or marker.get("source_complete") is not True
        or marker.get("schema") != DOMAIN_MARKER_SCHEMA
        or marker.get("domain") != domain
        or marker.get("external_account_id") != str(external_account_id)
        or marker.get("upstream_uid") != upstream_uid
        or not isinstance(scopes, list)
        or len(scopes) != len(set(str(item) for item in scopes))
        or set(scopes) != set(expected_scopes)
        or not isinstance(counts, Mapping)
        or set(counts) != set(expected_scopes)
        or not isinstance(source_paths, list)
        or not source_paths
        or any(
            not str(path or "").strip() or len(str(path)) > 256
            for path in source_paths
        )
        or not _valid_sha256_digest(digest)
        or coverage_started_at is None
        or coverage_ended_at is None
        or watermark_at is None
        or coverage_started_at > coverage_ended_at
        or watermark_at < coverage_ended_at
        or marker.get("unresolved_records", 0) != 0
    ):
        return False
    for scope in expected_scopes:
        value = counts.get(scope)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= DOMAIN_MARKER_MAX_RECORDS
        ):
            return False
    if domain == "social":
        owner_user_id = str(getattr(cursor, "owner_user_id", "") or "")
        coverage = marker.get("coverage")
        watermarks = marker.get("watermarks")
        if (
            marker.get("owner_user_id") != owner_user_id
            or counts.get("profile") != 1
            or len(source_paths) != len(set(source_paths))
            or tuple(source_paths) != SOCIAL_MARKER_SOURCE_PATHS
            or not isinstance(coverage, Mapping)
            or set(coverage) != set(expected_scopes)
            or not isinstance(watermarks, Mapping)
            or set(watermarks) != set(SOCIAL_MARKER_SOURCE_PATHS)
        ):
            return False
        for path in SOCIAL_MARKER_SOURCE_PATHS:
            watermark = watermarks.get(path)
            if not isinstance(watermark, Mapping):
                return False
            pages = watermark.get("pages")
            records = watermark.get("records")
            last_page = watermark.get("last_page")
            if (
                watermark.get("source_complete") is not True
                or not isinstance(pages, int)
                or isinstance(pages, bool)
                or not 1 <= pages <= 1000
                or not isinstance(records, int)
                or isinstance(records, bool)
                or not 0 <= records <= DOMAIN_MARKER_MAX_RECORDS
                or not isinstance(last_page, int)
                or isinstance(last_page, bool)
                or not 1 <= last_page <= 1000
                or last_page != pages
                or str(watermark.get("terminal") or "")
                not in SOCIAL_MARKER_TERMINALS
            ):
                return False
            terminal = str(watermark.get("terminal") or "")
            if path == "/api/profile/user":
                if pages != 1 or records != 1 or terminal != "single-response":
                    return False
            elif path in {
                "/api/social/friends",
                "/api/social/blacklist",
                "/api/social/blacklist-me",
            }:
                if pages != 1 or terminal not in {"single-response", "false-empty"}:
                    return False
            elif terminal not in {
                "empty-page",
                "false-empty",
                "explicit-complete",
            }:
                return False
        for scope in expected_scopes:
            scope_coverage = coverage.get(scope)
            if not isinstance(scope_coverage, Mapping):
                return False
            scope_paths = scope_coverage.get("source_paths")
            if (
                scope_coverage.get("source_complete") is not True
                or scope_coverage.get("records") != counts[scope]
                or not isinstance(scope_paths, list)
                or not scope_paths
                or len(scope_paths) != len(set(scope_paths))
                or tuple(scope_paths) != SOCIAL_MARKER_SCOPE_PATHS[scope]
                or sum(int(watermarks[path]["records"]) for path in scope_paths)
                != counts[scope]
            ):
                return False
    if domain == "moments":
        if (
            marker.get("history_days") != MOMENTS_HISTORY_DAYS
            or marker.get("phase") != "complete"
            or tuple(source_paths) != MOMENTS_MARKER_SOURCE_PATHS
        ):
            return False
    if domain == "discovery":
        if (
            tuple(source_paths) != DISCOVERY_MARKER_SOURCE_PATHS
            or counts.get("profile") != 1
            or marker.get("profile_source_marker")
            != DISCOVERY_PROFILE_SOURCE_MARKER
        ):
            return False
    if domain == "media":
        owner_user_id = str(getattr(cursor, "owner_user_id", "") or "")
        history = marker.get("history")
        prerequisites = marker.get("prerequisites")
        if (
            marker.get("owner_user_id") != owner_user_id
            or marker.get("phase") != "complete"
            or tuple(source_paths) != MEDIA_MARKER_SOURCE_PATHS
            or counts.get("objects", 0) > counts.get("metadata", 0)
            or not isinstance(history, Mapping)
            or history.get("complete") is not True
            or not isinstance(prerequisites, Mapping)
            or set(prerequisites) != {"message_peer", "moments", "social"}
            or not isinstance(media_prerequisite_digests, Mapping)
            or set(media_prerequisite_digests)
            != {"message_peer", "moments", "social"}
            or any(
                not _valid_sha256_digest(prerequisites.get(name))
                or prerequisites.get(name) != media_prerequisite_digests.get(name)
                for name in ("message_peer", "moments", "social")
            )
            or not isinstance(media_peer_count, int)
            or isinstance(media_peer_count, bool)
            or not 0 <= media_peer_count <= MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS
        ):
            return False
        pages = history.get("pages")
        messages = history.get("messages")
        directions = history.get("directions")
        expected_directions = media_peer_count * 2
        if (
            not isinstance(pages, int)
            or isinstance(pages, bool)
            or not isinstance(messages, int)
            or isinstance(messages, bool)
            or not isinstance(directions, int)
            or isinstance(directions, bool)
            or directions != expected_directions
            or not 0 <= messages <= MEDIA_HISTORY_MAX_MESSAGES_PER_ACCOUNT
            or not _valid_sha256_digest(history.get("record_digest"))
        ):
            return False
        if directions == 0:
            if pages != 0:
                return False
        elif not (
            directions
            <= pages
            <= directions * MEDIA_HISTORY_MAX_PAGES_PER_DIRECTION
        ):
            return False
    return True


def _media_prerequisite_state(
    cursors_by_user_stream: Mapping[tuple[str, str], Any],
    *,
    user_key: str,
    external_account_id: Any,
    upstream_uid: str,
) -> tuple[Mapping[str, str] | None, int | None]:
    social_cursor = cursors_by_user_stream.get(
        (user_key, DOMAIN_MARKER_STREAMS["social"])
    )
    moments_cursor = cursors_by_user_stream.get(
        (user_key, DOMAIN_MARKER_STREAMS["moments"])
    )
    peer_cursor = cursors_by_user_stream.get(
        (user_key, MESSAGE_PEER_SNAPSHOT_STREAM)
    )
    if not _valid_domain_marker(
        social_cursor,
        domain="social",
        external_account_id=external_account_id,
        upstream_uid=upstream_uid,
    ) or not _valid_domain_marker(
        moments_cursor,
        domain="moments",
        external_account_id=external_account_id,
        upstream_uid=upstream_uid,
    ):
        return None, None
    peer_marker = _message_peer_marker(
        peer_cursor,
        external_account_id=external_account_id,
        upstream_uid=upstream_uid,
    )
    social_marker = _domain_marker_payload(social_cursor)
    moments_marker = _domain_marker_payload(moments_cursor)
    if (
        peer_marker is None
        or social_marker is None
        or moments_marker is None
        or not _valid_sha256_digest(social_marker.get("record_digest"))
        or not _valid_sha256_digest(moments_marker.get("record_digest"))
        or not _valid_sha256_digest(peer_marker.get("peer_digest"))
    ):
        return None, None
    peer_count = peer_marker.get("peer_count")
    if (
        not isinstance(peer_count, int)
        or isinstance(peer_count, bool)
        or not 0 <= peer_count <= MESSAGE_PEER_SNAPSHOT_MAX_RELATIONSHIPS
    ):
        return None, None
    return (
        {
            "message_peer": str(peer_marker["peer_digest"]),
            "moments": str(moments_marker["record_digest"]),
            "social": str(social_marker["record_digest"]),
        },
        peer_count,
    )


def _outbox_summary(rows: Sequence[Any]) -> dict[str, Any]:
    # ``cancelled`` is terminal only after an explicit retirement action (or
    # for new optional mirrors created while already in retired mode).
    return _unfinished_status_summary(
        rows,
        terminal_statuses=frozenset({"completed", "cancelled"}),
    )


def _media_archive_summary(rows: Sequence[Any]) -> dict[str, Any]:
    # ``media.archive`` 待办只有复验后的 ``completed`` 是合法终态；文档
    # §4.5 明确"不得直接取消"，因此 cancelled 一并计入未完成并阻断切流。
    return _unfinished_status_summary(
        rows,
        terminal_statuses=frozenset({"completed"}),
    )


def _tim_delivery_summary(rows: Sequence[Any]) -> dict[str, Any]:
    # ``cancelled`` is a deliberate terminal state used when a local media
    # revoke prevents or compensates an in-flight APK mirror.  It does not
    # represent an external write that still needs dispatch.
    return _unfinished_status_summary(
        rows,
        terminal_statuses=frozenset({"delivered", "cancelled"}),
    )


def _unfinished_status_summary(
    rows: Sequence[Any], *, terminal_statuses: frozenset[str]
) -> dict[str, Any]:
    counts = Counter()
    total = 0
    oldest: datetime | None = None
    for row in rows:
        status = str(_value(row, "status") or "").strip().lower()
        try:
            count = int(_value(row, "item_count", _value(row, "count", 0)) or 0)
        except (TypeError, ValueError, OverflowError):
            count = 0
        count = max(0, count)
        counts[status] += count
        total += count
        if status not in terminal_statuses and count:
            candidate = _marker_datetime(
                _value(row, "oldest_at", _value(row, "oldest_created_at"))
            )
            if candidate is not None and (oldest is None or candidate < oldest):
                oldest = candidate

    known_unfinished = sum(
        counts[status] for status in ("pending", "retry", "processing", "failed")
    )
    other_unfinished = sum(
        count
        for status, count in counts.items()
        if status
        not in terminal_statuses | {"pending", "retry", "processing", "failed"}
    )
    unfinished = known_unfinished + other_unfinished
    return {
        "total": total,
        "pending": counts["pending"],
        "retry": counts["retry"],
        "processing": counts["processing"],
        "failed": counts["failed"],
        "other_unfinished": other_unfinished,
        "unfinished": unfinished,
        "oldest_unfinished_at": oldest.isoformat() if oldest is not None else None,
    }


def evaluate_readiness(
    *,
    provider: str,
    account_rows: Sequence[Any],
    cursor_rows: Sequence[Any],
    relationship_rows: Sequence[Any] = (),
    outbox_rows: Sequence[Any] = (),
    media_archive_rows: Sequence[Any] = (),
    tim_delivery_rows: Sequence[Any] = (),
    r2_capability: R2CapabilityResult | None = None,
) -> MigrationReadinessReport:
    normalized_provider = str(provider or "").strip()
    resolved_r2_capability = (
        r2_capability
        if isinstance(r2_capability, R2CapabilityResult)
        else R2_CAPABILITY_NOT_CHECKED
    )
    uid_counts = Counter(
        uid
        for uid in (_uid(_value(row, "upstream_uid")) for row in account_rows)
        if uid
    )
    user_counts = Counter(
        str(_value(row, "user_id") or "")
        for row in account_rows
        if str(_value(row, "user_id") or "")
    )
    duplicate_users = {
        user_id for user_id, count in user_counts.items() if count > 1
    }
    duplicate_uids = {uid for uid, count in uid_counts.items() if count > 1}
    cursors_by_user_stream = {
        (
            str(getattr(row, "owner_user_id", "")),
            str(getattr(row, "stream", "") or ""),
        ): row
        for row in cursor_rows
        if str(getattr(row, "source", "") or "") == normalized_provider
    }
    block_relationships_by_user: dict[str, list[Any]] = {}
    message_peer_relationships_by_user: dict[str, list[Any]] = {}
    for row in relationship_rows:
        user_key = str(_value(row, "owner_user_id") or "")
        row_provider = str(
            _value(row, "provider", normalized_provider) or normalized_provider
        )
        row_kind = str(_value(row, "kind") or "")
        if row_provider == normalized_provider and row_kind in SNAPSHOT_KINDS:
            block_relationships_by_user.setdefault(user_key, []).append(row)
        elif (
            row_provider == MESSAGE_POLICY_PROVIDER
            and row_kind == MESSAGE_POLICY_CONVERSATION_KIND
        ):
            message_peer_relationships_by_user.setdefault(user_key, []).append(row)

    active_accounts = 0
    enabled_credentials = 0
    local_login_ready = 0
    local_message_identity_ready = 0
    block_snapshot_ready = 0
    message_peer_snapshot_ready = 0
    local_core_ready_accounts = 0
    domain_ready_counts = Counter()
    all_domains_ready_accounts = 0
    cutover_ready_accounts = 0
    missing_domain_markers = Counter()
    invalid_domain_markers = Counter()
    missing_uid = 0
    duplicate_user_binding_accounts = 0
    duplicate_uid_accounts = 0
    missing_credentials = 0
    disabled_credentials = 0
    invalid_credentials = 0
    backfill_candidates = 0
    missing_snapshots = 0
    invalid_snapshots = 0
    invalid_relationship_snapshots = 0
    missing_message_peer_snapshots = 0
    invalid_message_peer_snapshots = 0
    invalid_message_peer_relationship_snapshots = 0

    for row in account_rows:
        if str(_value(row, "user_status") or "") != "active":
            continue
        active_accounts += 1
        credential_id = _value(row, "credential_id")
        credential_disabled_at = _value(row, "credential_disabled_at")
        credential_enabled = credential_id is not None and credential_disabled_at is None
        credential_valid = credential_enabled and _valid_password_hash(
            _value(row, "credential_password_hash")
        )
        if credential_enabled:
            enabled_credentials += 1
        if credential_id is None:
            missing_credentials += 1
            if _value(row, "password_encrypted") is not None:
                backfill_candidates += 1
        elif credential_disabled_at is not None:
            disabled_credentials += 1
        elif not credential_valid:
            invalid_credentials += 1

        user_key = str(_value(row, "user_id") or "")
        if user_key in duplicate_users:
            duplicate_user_binding_accounts += 1
            continue
        if credential_valid:
            local_login_ready += 1

        upstream_uid = _uid(_value(row, "upstream_uid"))
        if not upstream_uid:
            missing_uid += 1
            continue
        if upstream_uid in duplicate_uids:
            duplicate_uid_accounts += 1
            continue

        external_account_id = _value(row, "external_account_id")
        media_prerequisite_digests, media_peer_count = _media_prerequisite_state(
            cursors_by_user_stream,
            user_key=user_key,
            external_account_id=external_account_id,
            upstream_uid=upstream_uid,
        )
        domain_results: dict[str, bool] = {}
        for domain, stream in DOMAIN_MARKER_STREAMS.items():
            domain_cursor = cursors_by_user_stream.get((user_key, stream))
            if domain_cursor is None:
                missing_domain_markers[domain] += 1
                domain_results[domain] = False
            elif not _valid_domain_marker(
                domain_cursor,
                domain=domain,
                external_account_id=external_account_id,
                upstream_uid=upstream_uid,
                media_prerequisite_digests=(
                    media_prerequisite_digests if domain == "media" else None
                ),
                media_peer_count=media_peer_count if domain == "media" else None,
            ):
                invalid_domain_markers[domain] += 1
                domain_results[domain] = False
            else:
                domain_ready_counts[domain] += 1
                domain_results[domain] = True
        domains_ready = all(domain_results.values())
        if domains_ready:
            all_domains_ready_accounts += 1

        if not credential_valid:
            continue
        local_message_identity_ready += 1

        markers = {
            kind: cursors_by_user_stream.get((user_key, SNAPSHOT_STREAMS[kind]))
            for kind in SNAPSHOT_KINDS
        }
        block_ready = False
        if any(marker is None for marker in markers.values()):
            missing_snapshots += 1
        elif not all(
            _valid_snapshot_marker(
                markers[kind],
                kind=kind,
                external_account_id=external_account_id,
                upstream_uid=upstream_uid,
            )
            for kind in SNAPSHOT_KINDS
        ):
            invalid_snapshots += 1
        elif not _valid_relationship_snapshot(
            block_relationships_by_user.get(user_key, ()),
            own_uid=upstream_uid,
        ):
            invalid_snapshots += 1
            invalid_relationship_snapshots += 1
        else:
            block_ready = True
            block_snapshot_ready += 1

        message_peer_cursor = cursors_by_user_stream.get(
            (user_key, MESSAGE_PEER_SNAPSHOT_STREAM)
        )
        if message_peer_cursor is None:
            missing_message_peer_snapshots += 1
            message_peer_ready = False
        else:
            message_peer_marker = _message_peer_marker(
                message_peer_cursor,
                external_account_id=external_account_id,
                upstream_uid=upstream_uid,
            )
            if message_peer_marker is None:
                invalid_message_peer_snapshots += 1
                message_peer_ready = False
            elif not _valid_message_peer_relationship_snapshot(
                message_peer_relationships_by_user.get(user_key, ()),
                own_uid=upstream_uid,
                marker=message_peer_marker,
            ):
                invalid_message_peer_snapshots += 1
                invalid_message_peer_relationship_snapshots += 1
                message_peer_ready = False
            else:
                message_peer_ready = True
                message_peer_snapshot_ready += 1

        local_core_ready = block_ready and message_peer_ready
        if local_core_ready:
            local_core_ready_accounts += 1
        if local_core_ready and domains_ready:
            cutover_ready_accounts += 1

    provider_accounts = len(account_rows)
    outbox = _outbox_summary(outbox_rows)
    media_archives = _media_archive_summary(media_archive_rows)
    tim_deliveries = _tim_delivery_summary(tim_delivery_rows)
    local_core_ready = bool(
        active_accounts > 0
        and local_core_ready_accounts == active_accounts
        and not duplicate_users
        and not duplicate_uids
    )
    domain_data_ready = bool(
        active_accounts > 0
        and all_domains_ready_accounts == active_accounts
        and not duplicate_users
        and not duplicate_uids
    )
    compatibility_outbox_ready = outbox["unfinished"] == 0
    # media Marker 只在写入时点校验该账号的 media.archive 待办；Marker 之后
    # 新产生的待办若不在此单独把关，--require-ready 会出现盲区。
    media_archive_outbox_ready = media_archives["unfinished"] == 0
    tim_delivery_ready = tim_deliveries["unfinished"] == 0
    r2_storage_ready = resolved_r2_capability.ready
    cutover_ready = bool(
        local_core_ready
        and domain_data_ready
        and cutover_ready_accounts == active_accounts
        and compatibility_outbox_ready
        and media_archive_outbox_ready
        and tim_delivery_ready
        and r2_storage_ready
    )
    return MigrationReadinessReport(
        provider=normalized_provider,
        provider_accounts=provider_accounts,
        active_accounts=active_accounts,
        inactive_accounts=provider_accounts - active_accounts,
        enabled_credentials=enabled_credentials,
        local_login_ready_accounts=local_login_ready,
        local_message_identity_ready_accounts=local_message_identity_ready,
        block_snapshot_ready_accounts=block_snapshot_ready,
        message_peer_snapshot_ready_accounts=message_peer_snapshot_ready,
        local_core_ready_accounts=local_core_ready_accounts,
        social_ready_accounts=domain_ready_counts["social"],
        moments_ready_accounts=domain_ready_counts["moments"],
        discovery_ready_accounts=domain_ready_counts["discovery"],
        media_ready_accounts=domain_ready_counts["media"],
        all_domains_ready_accounts=all_domains_ready_accounts,
        cutover_ready_accounts=cutover_ready_accounts,
        # Retained as a compatibility field, but now intentionally means the
        # complete per-account cutover gate rather than only auth + text.
        fully_ready_accounts=cutover_ready_accounts,
        missing_uid_accounts=missing_uid,
        duplicate_user_bindings=len(duplicate_users),
        duplicate_user_binding_accounts=duplicate_user_binding_accounts,
        duplicate_uid_values=len(duplicate_uids),
        duplicate_uid_accounts=duplicate_uid_accounts,
        missing_credentials=missing_credentials,
        disabled_credentials=disabled_credentials,
        invalid_credentials=invalid_credentials,
        backfill_candidates=backfill_candidates,
        missing_block_snapshot_accounts=missing_snapshots,
        invalid_block_snapshot_accounts=invalid_snapshots,
        invalid_block_relationship_accounts=invalid_relationship_snapshots,
        missing_message_peer_snapshot_accounts=missing_message_peer_snapshots,
        invalid_message_peer_snapshot_accounts=invalid_message_peer_snapshots,
        invalid_message_peer_relationship_accounts=(
            invalid_message_peer_relationship_snapshots
        ),
        missing_social_marker_accounts=missing_domain_markers["social"],
        invalid_social_marker_accounts=invalid_domain_markers["social"],
        missing_moments_marker_accounts=missing_domain_markers["moments"],
        invalid_moments_marker_accounts=invalid_domain_markers["moments"],
        missing_discovery_marker_accounts=missing_domain_markers["discovery"],
        invalid_discovery_marker_accounts=invalid_domain_markers["discovery"],
        missing_media_marker_accounts=missing_domain_markers["media"],
        invalid_media_marker_accounts=invalid_domain_markers["media"],
        compatibility_outbox_total=outbox["total"],
        compatibility_outbox_pending=outbox["pending"],
        compatibility_outbox_retry=outbox["retry"],
        compatibility_outbox_processing=outbox["processing"],
        compatibility_outbox_failed=outbox["failed"],
        compatibility_outbox_other_unfinished=outbox["other_unfinished"],
        compatibility_outbox_unfinished=outbox["unfinished"],
        compatibility_outbox_oldest_unfinished_at=outbox[
            "oldest_unfinished_at"
        ],
        media_archive_outbox_total=media_archives["total"],
        media_archive_outbox_pending=media_archives["pending"],
        media_archive_outbox_retry=media_archives["retry"],
        media_archive_outbox_processing=media_archives["processing"],
        media_archive_outbox_failed=media_archives["failed"],
        media_archive_outbox_other_unfinished=media_archives[
            "other_unfinished"
        ],
        media_archive_outbox_unfinished=media_archives["unfinished"],
        media_archive_outbox_oldest_unfinished_at=media_archives[
            "oldest_unfinished_at"
        ],
        tim_delivery_total=tim_deliveries["total"],
        tim_delivery_pending=tim_deliveries["pending"],
        tim_delivery_retry=tim_deliveries["retry"],
        tim_delivery_processing=tim_deliveries["processing"],
        tim_delivery_failed=tim_deliveries["failed"],
        tim_delivery_other_unfinished=tim_deliveries["other_unfinished"],
        tim_delivery_unfinished=tim_deliveries["unfinished"],
        tim_delivery_oldest_unfinished_at=tim_deliveries[
            "oldest_unfinished_at"
        ],
        local_core_ready=local_core_ready,
        domain_data_ready=domain_data_ready,
        compatibility_outbox_ready=compatibility_outbox_ready,
        media_archive_outbox_ready=media_archive_outbox_ready,
        tim_delivery_ready=tim_delivery_ready,
        r2_write_ready=resolved_r2_capability.write_ready,
        r2_read_ready=resolved_r2_capability.read_ready,
        r2_delete_ready=resolved_r2_capability.delete_ready,
        r2_storage_ready=r2_storage_ready,
        r2_capability_error_code=resolved_r2_capability.error_code,
        cutover_ready=cutover_ready,
        ready=cutover_ready,
    )


def inspect_readiness(
    db: Any,
    *,
    provider: str = "beibeiwu",
    r2_capability: R2CapabilityResult | None = None,
) -> MigrationReadinessReport:
    account_rows = list(
        db.execute(
            select(
                User.id.label("user_id"),
                User.status.label("user_status"),
                ExternalAccount.id.label("external_account_id"),
                ExternalAccount.upstream_uid.label("upstream_uid"),
                ExternalAccount.password_encrypted.label("password_encrypted"),
                UserCredential.id.label("credential_id"),
                UserCredential.disabled_at.label("credential_disabled_at"),
                UserCredential.password_hash.label("credential_password_hash"),
            )
            .select_from(ExternalAccount)
            .join(User, User.id == ExternalAccount.user_id)
            .outerjoin(UserCredential, UserCredential.user_id == User.id)
            .where(ExternalAccount.provider == provider)
            .order_by(User.id)
        )
    )
    user_ids = [
        _value(row, "user_id")
        for row in account_rows
        if _value(row, "user_id") is not None
    ]
    cursor_rows = (
        list(
            db.scalars(
                select(SyncCursor).where(
                    SyncCursor.owner_user_id.in_(user_ids),
                    SyncCursor.source == provider,
                    SyncCursor.stream.in_(
                        (
                            *SNAPSHOT_STREAMS.values(),
                            MESSAGE_PEER_SNAPSHOT_STREAM,
                            *DOMAIN_MARKER_STREAMS.values(),
                        )
                    ),
                )
            )
        )
        if user_ids
        else []
    )
    relationship_rows = (
        list(
            db.execute(
                select(
                    Relationship.owner_user_id.label("owner_user_id"),
                    Relationship.subject_upstream_uid.label(
                        "subject_upstream_uid"
                    ),
                    Relationship.provider.label("provider"),
                    Relationship.kind.label("kind"),
                    Relationship.extra_data.label("relationship_metadata"),
                ).where(
                    Relationship.owner_user_id.in_(user_ids),
                    or_(
                        and_(
                            Relationship.provider == provider,
                            Relationship.kind.in_(SNAPSHOT_KINDS),
                        ),
                        and_(
                            Relationship.provider == MESSAGE_POLICY_PROVIDER,
                            Relationship.kind == MESSAGE_POLICY_CONVERSATION_KIND,
                        ),
                    ),
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                )
            )
        )
        if user_ids
        else []
    )
    outbox_rows = list(
        db.execute(
            select(
                OperationOutbox.status.label("status"),
                func.count(OperationOutbox.id).label("item_count"),
                func.min(OperationOutbox.created_at).label("oldest_at"),
            )
            .where(_ordinary_compatibility_outbox_predicate())
            .group_by(OperationOutbox.status)
        )
    )
    media_archive_rows = list(
        db.execute(
            select(
                OperationOutbox.status.label("status"),
                func.count(OperationOutbox.id).label("item_count"),
                func.min(OperationOutbox.created_at).label("oldest_at"),
            )
            .where(_media_archive_outbox_predicate())
            .group_by(OperationOutbox.status)
        )
    )
    tim_delivery_rows = list(
        db.execute(
            select(
                MessageDelivery.status.label("status"),
                func.count(MessageDelivery.id).label("item_count"),
                func.min(MessageDelivery.created_at).label("oldest_at"),
            )
            .where(MessageDelivery.channel == "tim")
            .group_by(MessageDelivery.status)
        )
    )
    return evaluate_readiness(
        provider=provider,
        account_rows=account_rows,
        cursor_rows=cursor_rows,
        relationship_rows=relationship_rows,
        outbox_rows=outbox_rows,
        media_archive_rows=media_archive_rows,
        tim_delivery_rows=tim_delivery_rows,
        r2_capability=r2_capability,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "检查 Web 本地核心、领域历史、R2 当前能力和兼容链路退役就绪度"
        )
    )
    parser.add_argument(
        "--provider",
        default="beibeiwu",
        help="检查指定账号 Provider（默认 beibeiwu）",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="完整退役门槛未满足时返回退出码 1",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    r2_probe: Callable[[], R2CapabilityResult] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    provider = str(args.provider or "").strip()
    if not provider or len(provider) > 40:
        raise SystemExit("--provider must contain 1 to 40 characters")
    try:
        r2_capability = (r2_probe or probe_r2_capabilities)()
    except Exception:
        r2_capability = R2CapabilityResult(
            write_ready=False,
            read_ready=False,
            delete_ready=False,
            error_code=R2_CAPABILITY_ERROR_INITIALIZATION,
        )
    if not isinstance(r2_capability, R2CapabilityResult):
        r2_capability = R2_CAPABILITY_NOT_CHECKED
    with session_scope() as db:
        report = inspect_readiness(
            db,
            provider=provider,
            r2_capability=r2_capability,
        )
    print(
        json.dumps(asdict(report), ensure_ascii=False, sort_keys=True),
        file=stdout or sys.stdout,
    )
    return 1 if args.require_ready and not report.ready else 0


if __name__ == "__main__":
    raise SystemExit(main())
