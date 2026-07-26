"""可恢复、fail-closed 的 Banghua 社交域主动迁移。

该模块只由独立 CLI/运维任务调用。所有上游读取在任何数据写事务开始前
完成；只有 profile、关注/粉丝/好友、好友申请和双向黑名单全部证明读取
完整且本地写入可验证后，才会写 ``migration-domain:social`` 完成 Marker。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, ContextManager, Protocol, TextIO

from sqlalchemy import select

from bbw_prod.config import Settings, get_settings
from bbw_prod.crypto import CredentialCipher
from bbw_prod.db import session_scope
from bbw_prod.migration_readiness import (
    DOMAIN_MARKER_SCHEMA,
    DOMAIN_MARKER_SCOPES,
    DOMAIN_MARKER_STREAMS,
    SOCIAL_MARKER_SCOPE_PATHS,
    SOCIAL_MARKER_SOURCE_PATHS,
)
from bbw_prod.models import ExternalAccount, Relationship, SyncCursor, User, utcnow
from bbw_prod.repositories import RelationshipRepository, SyncCursorRepository
from bbw_prod.services import (
    WEB_LOCAL_PROFILE_FIELDS_KEY,
    merge_provider_profile_preserving_local,
)
from bbw_web.normalize import (
    normalize_friend_application,
    normalize_friends,
    normalize_social_user,
    normalize_user,
)
from bbw_web.providers import (
    ProviderAuthenticationRejected,
    ProviderSessionState,
    ProviderUnavailable,
    RuntimeProvider,
)


LEGACY_PROVIDER = "beibeiwu"
LOCAL_SOCIAL_PROVIDER = "web-local"
SOCIAL_STREAM = DOMAIN_MARKER_STREAMS["social"]
PROFILE_SOURCE_PATH = "/api/profile/user"
RELATION_SOURCE_PATHS = {
    "follow": "/api/social/follows",
    "follower": "/api/social/fans",
    "friend": "/api/social/friends",
    "friend_request": "/api/social/friend-apply",
    "blacklist": "/api/social/blacklist",
    "blacklisted_by": "/api/social/blacklist-me",
}
PAGINATED_RELATION_KINDS = frozenset({"follow", "follower", "friend_request"})
MAX_SOURCE_PAGES = 1000
MAX_PAGE_ITEMS = 500
MAX_SCOPE_RECORDS = 50_000
MAX_PROFILE_TEXT = 2000
MAX_APPLICATION_MESSAGE = 2000
_LIST_KEYS = (
    "items",
    "list",
    "users",
    "userlist",
    "userInfoList",
    "user_info_list",
    "followList",
    "follow_list",
    "fansList",
    "fans_list",
    "followUsers",
    "fansUsers",
    "friendList",
    "friendsList",
    "friend_list",
    "friends_list",
    "applyList",
    "apply_list",
    "rows",
    "records",
    "result",
    "data",
    "info",
    "json_obj",
    "json",
)


class LegacySocialMigrationError(RuntimeError):
    code = "legacy_social_migration_error"
    retryable = False


class LegacySocialProviderError(LegacySocialMigrationError):
    code = "legacy_social_provider_unavailable"
    retryable = True


class LegacySocialAuthenticationRejected(LegacySocialMigrationError):
    code = "legacy_social_authentication_rejected"
    retryable = False


class LegacySocialAuthenticationError(LegacySocialMigrationError):
    code = "legacy_social_authentication_failed"
    retryable = False


class LegacySocialDataError(LegacySocialMigrationError):
    code = "legacy_social_data_invalid"


class LegacySocialLimitError(LegacySocialMigrationError):
    code = "legacy_social_import_limit_exceeded"


@dataclass(frozen=True, slots=True)
class LegacySocialAccount:
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    display_name: str
    profile: dict[str, Any]
    device_data: dict[str, Any]
    login_encrypted: dict[str, Any]
    password_encrypted: dict[str, Any] | None
    token_encrypted: dict[str, Any] | None
    token_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ImportWindow:
    coverage_started_at: datetime
    coverage_ended_at: datetime


@dataclass(frozen=True, slots=True)
class SourcePage:
    source_path: str
    page: int
    items: tuple[Mapping[str, Any], ...]
    terminal: bool
    terminal_reason: str = ""


@dataclass(frozen=True, slots=True)
class SourceWatermark:
    source_path: str
    pages: int
    last_page: int
    records: int
    terminal: str

    def marker(self) -> dict[str, Any]:
        return {
            "source_complete": True,
            "pages": self.pages,
            "last_page": self.last_page,
            "records": self.records,
            "terminal": self.terminal,
        }


@dataclass(frozen=True, slots=True)
class RelationshipRecord:
    kind: str
    peer_uid: str
    status: str
    resolved_as: str
    direction: str
    application_id: str
    message: str
    profile: dict[str, Any]

    def identity(self) -> str:
        payload = {
            "application_id": self.application_id,
            "direction": self.direction,
            "kind": self.kind,
            "message": self.message,
            "peer_uid": self.peer_uid,
            "profile": self.profile,
            "resolved_as": self.resolved_as,
            "status": self.status,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class FetchedSocialSnapshot:
    window: ImportWindow
    profile: dict[str, Any]
    profile_source_digest: str
    records: dict[str, tuple[RelationshipRecord, ...]]
    watermarks: dict[str, SourceWatermark]
    counts: dict[str, int]
    coverage: dict[str, dict[str, Any]]
    record_digest: str


@dataclass(frozen=True, slots=True)
class WriteSummary:
    profile_written: int
    relationship_rows_written: int
    legacy_rows_deactivated: int


@dataclass(frozen=True, slots=True)
class ImportSummary:
    source_pages: int
    source_records: int
    profile_written: int
    relationship_rows_written: int
    legacy_rows_deactivated: int
    counts: dict[str, int]
    record_digest: str


@dataclass(frozen=True, slots=True)
class RefreshedProviderState:
    token: str
    authenticated_at: datetime
    device_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResolvedProviderRuntime:
    runtime: Any
    refreshed: RefreshedProviderState | None = None


class LegacySocialReader(Protocol):
    def fetch_profile(self) -> Mapping[str, Any]: ...

    def fetch_following_page(self, page: int) -> SourcePage: ...

    def fetch_followers_page(self, page: int) -> SourcePage: ...

    def fetch_friends(self) -> SourcePage: ...

    def fetch_friend_requests_page(self, page: int) -> SourcePage: ...

    def fetch_blacklist(self) -> SourcePage: ...

    def fetch_blacklisted_by(self) -> SourcePage: ...


class LegacySocialWriter(Protocol):
    def begin(self, account: LegacySocialAccount, window: ImportWindow) -> None: ...

    def apply(
        self, account: LegacySocialAccount, snapshot: FetchedSocialSnapshot
    ) -> WriteSummary: ...

    def complete(
        self, account: LegacySocialAccount, snapshot: FetchedSocialSnapshot
    ) -> None: ...

    def fail(
        self,
        account: LegacySocialAccount,
        window: ImportWindow,
        *,
        code: str,
    ) -> None: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _uid(value: Any) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or normalized.lower() in {"0", "none", "null", "undefined"}
        or len(normalized) > 128
        or any(ord(character) < 33 for character in normalized)
    ):
        return ""
    return normalized


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit or any(ord(character) == 0 for character in text):
        raise LegacySocialDataError("legacy_social_text_invalid")
    return text


def _json_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LegacySocialDataError("legacy_social_json_invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _decoded_json(value: str) -> Any:
    raw = str(value or "").strip()
    if not raw or raw[:1] not in "[{":
        raise LegacySocialDataError("legacy_social_payload_unknown")
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LegacySocialDataError("legacy_social_payload_invalid") from exc


def _strict_mapping_item(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = _decoded_json(value)
    if not isinstance(value, Mapping):
        raise LegacySocialDataError("legacy_social_item_not_object")
    _json_digest(value)
    return dict(value)


def _strict_list_payload(value: Any, *, depth: int = 0) -> tuple[Mapping[str, Any], ...]:
    if depth > 8:
        raise LegacySocialDataError("legacy_social_payload_too_deep")
    if isinstance(value, str):
        return _strict_list_payload(_decoded_json(value), depth=depth + 1)
    if isinstance(value, list):
        if len(value) > MAX_PAGE_ITEMS:
            raise LegacySocialLimitError("legacy_social_page_item_limit_exceeded")
        return tuple(_strict_mapping_item(item) for item in value)
    if not isinstance(value, Mapping):
        raise LegacySocialDataError("legacy_social_list_structure_unknown")
    for key in _LIST_KEYS:
        if key not in value:
            continue
        nested = value.get(key)
        if nested is value:
            break
        return _strict_list_payload(nested, depth=depth + 1)
    if value and all(str(key).isdigit() for key in value):
        return _strict_list_payload(list(value.values()), depth=depth + 1)
    raise LegacySocialDataError("legacy_social_list_structure_unknown")


def _profile_candidates(value: Any, *, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 8:
        raise LegacySocialDataError("legacy_profile_payload_too_deep")
    if isinstance(value, str):
        return _profile_candidates(_decoded_json(value), depth=depth + 1)
    if isinstance(value, list):
        return [_strict_mapping_item(item) for item in value]
    if not isinstance(value, Mapping):
        raise LegacySocialDataError("legacy_profile_structure_unknown")
    direct = normalize_user(value)
    if direct and _uid(direct.get("id")):
        return [dict(value)]
    for key in ("user", "profile", "data", "info", "json_obj", "json", "userInfoList"):
        if key in value:
            return _profile_candidates(value.get(key), depth=depth + 1)
    raise LegacySocialDataError("legacy_profile_structure_unknown")


def _explicit_has_more(value: Any, *, depth: int = 0) -> bool | None:
    if depth > 6 or not isinstance(value, Mapping):
        return None
    for key in ("has_more", "hasMore", "more", "next_page", "nextPage"):
        if key not in value:
            continue
        raw = value.get(key)
        if key in {"next_page", "nextPage"}:
            text = str(raw or "").strip().lower()
            return bool(text and text not in {"0", "false", "none", "null"})
        if isinstance(raw, bool):
            return raw
        text = str(raw or "").strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n", "", "none", "null"}:
            return False
        raise LegacySocialDataError("legacy_social_pagination_flag_invalid")
    for key in ("data", "info", "json_obj", "json"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            resolved = _explicit_has_more(nested, depth=depth + 1)
            if resolved is not None:
                return resolved
    return None


def _false_empty_result(result: Any) -> bool:
    status = int(getattr(result, "status", 0) or 0)
    raw = str(getattr(result, "raw", "") or "").strip().lower()
    return 200 <= status < 300 and (
        getattr(result, "data", None) is False or raw == "false"
    )


class BanghuaSocialReader:
    """仅通过 Provider facade 读取上游，不访问数据库。"""

    def __init__(self, profile_api: Any, social_api: Any, *, upstream_uid: str) -> None:
        self.profile_api = profile_api
        self.social_api = social_api
        self.upstream_uid = _uid(upstream_uid)
        if not self.upstream_uid:
            raise LegacySocialDataError("legacy_upstream_uid_missing")

    @staticmethod
    def _provider_call(call: Callable[[], Any], *, label: str) -> Any:
        try:
            return call()
        except LegacySocialMigrationError:
            raise
        except Exception as exc:
            raise LegacySocialProviderError(
                f"legacy_social_read_exception.{label}.{type(exc).__name__}"
            ) from exc

    @staticmethod
    def _page(
        result: Any,
        *,
        source_path: str,
        page: int,
        paginated: bool,
    ) -> SourcePage:
        if _false_empty_result(result):
            return SourcePage(source_path, page, (), True, "false-empty")
        if not bool(getattr(result, "ok", False)):
            status = int(getattr(result, "status", 0) or 0)
            raise LegacySocialProviderError(
                f"legacy_social_read_failed.{source_path}.{status}"
            )
        data = getattr(result, "data", None)
        items = _strict_list_payload(data)
        has_more = _explicit_has_more(data)
        if not paginated:
            if has_more is True:
                raise LegacySocialDataError("legacy_social_unpaginated_has_more")
            return SourcePage(source_path, page, items, True, "single-response")
        if has_more is True and not items:
            raise LegacySocialDataError("legacy_social_empty_page_claims_more")
        if has_more is False:
            return SourcePage(source_path, page, items, True, "explicit-complete")
        if not items:
            return SourcePage(source_path, page, (), True, "empty-page")
        return SourcePage(source_path, page, items, False, "")

    def fetch_profile(self) -> Mapping[str, Any]:
        result = self._provider_call(
            lambda: self.profile_api.get_user(self.upstream_uid), label="profile"
        )
        if _false_empty_result(result) or not bool(getattr(result, "ok", False)):
            if not _false_empty_result(result):
                status = int(getattr(result, "status", 0) or 0)
                raise LegacySocialProviderError(
                    f"legacy_profile_read_failed.{status}"
                )
            raise LegacySocialDataError("legacy_profile_empty")
        candidates = _profile_candidates(getattr(result, "data", None))
        normalized = [normalize_user(candidate) for candidate in candidates]
        matching = [
            dict(item)
            for item in normalized
            if item and _uid(item.get("id")) == self.upstream_uid
        ]
        if len(matching) != 1:
            raise LegacySocialDataError("legacy_profile_identity_unresolved")
        return matching[0]

    def fetch_following_page(self, page: int) -> SourcePage:
        result = self._provider_call(
            lambda: self.social_api.follow_users(
                self.upstream_uid, page=str(page)
            ),
            label="follows",
        )
        return self._page(
            result,
            source_path=RELATION_SOURCE_PATHS["follow"],
            page=page,
            paginated=True,
        )

    def fetch_followers_page(self, page: int) -> SourcePage:
        result = self._provider_call(
            lambda: self.social_api.fans_users(
                self.upstream_uid, page=str(page)
            ),
            label="fans",
        )
        return self._page(
            result,
            source_path=RELATION_SOURCE_PATHS["follower"],
            page=page,
            paginated=True,
        )

    def fetch_friends(self) -> SourcePage:
        result = self._provider_call(self.social_api.friends, label="friends")
        return self._page(
            result,
            source_path=RELATION_SOURCE_PATHS["friend"],
            page=1,
            paginated=False,
        )

    def fetch_friend_requests_page(self, page: int) -> SourcePage:
        result = self._provider_call(
            lambda: self.social_api.friend_apply_list(str(page)),
            label="friend-requests",
        )
        return self._page(
            result,
            source_path=RELATION_SOURCE_PATHS["friend_request"],
            page=page,
            paginated=True,
        )

    def fetch_blacklist(self) -> SourcePage:
        result = self._provider_call(self.social_api.my_blacklist, label="blacklist")
        return self._page(
            result,
            source_path=RELATION_SOURCE_PATHS["blacklist"],
            page=1,
            paginated=False,
        )

    def fetch_blacklisted_by(self) -> SourcePage:
        result = self._provider_call(
            self.social_api.blacklist_me, label="blacklisted-by"
        )
        return self._page(
            result,
            source_path=RELATION_SOURCE_PATHS["blacklisted_by"],
            page=1,
            paginated=False,
        )


def _public_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "nickname",
            "avatar",
            "signature",
            "city",
            "gender",
            "property",
            "age",
            "role",
            "vip",
            "svip",
        )
        if value.get(key) not in (None, "")
    }


def _profile_values(value: Mapping[str, Any], *, own_uid: str) -> dict[str, Any]:
    if _uid(value.get("id")) != own_uid:
        raise LegacySocialDataError("legacy_profile_uid_mismatch")
    normalized = dict(value)
    if "sex" in normalized and "gender" not in normalized:
        normalized["gender"] = normalized.get("sex")
    profile = _public_profile(normalized)
    nickname = _bounded_text(profile.get("nickname"), 160)
    if not nickname:
        raise LegacySocialDataError("legacy_profile_nickname_missing")
    profile["nickname"] = nickname
    for field in ("signature", "city", "gender", "property", "role"):
        if field in profile:
            profile[field] = _bounded_text(profile[field], MAX_PROFILE_TEXT)
    return profile


def _normalized_peer(
    kind: str,
    item: Mapping[str, Any],
    *,
    own_uid: str,
) -> dict[str, Any]:
    normalized: dict[str, Any] | None
    if kind in {"follow", "follower"}:
        normalized = normalize_social_user(item, own_uid)
    elif kind == "friend":
        friends = normalize_friends([item], own_uid)
        normalized = friends[0] if len(friends) == 1 else None
    elif kind == "friend_request":
        normalized = normalize_friend_application(item, own_uid)
    else:
        normalized = normalize_social_user(item, own_uid) or normalize_user(item)
    if not normalized:
        raise LegacySocialDataError(f"legacy_{kind}_item_unresolved")
    peer_uid = _uid(normalized.get("id"))
    if not peer_uid or peer_uid == own_uid:
        raise LegacySocialDataError(f"legacy_{kind}_uid_missing")
    normalized = dict(normalized)
    normalized["id"] = peer_uid
    return normalized


def _relationship_records(
    kind: str,
    raw_items: Sequence[Mapping[str, Any]],
    *,
    own_uid: str,
    friend_uids: frozenset[str] = frozenset(),
) -> tuple[RelationshipRecord, ...]:
    records: list[RelationshipRecord] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = _normalized_peer(kind, raw, own_uid=own_uid)
        peer_uid = item["id"]
        if peer_uid in seen:
            raise LegacySocialDataError(f"legacy_{kind}_duplicate_peer")
        seen.add(peer_uid)
        status = "active"
        resolved_as = ""
        direction = ""
        application_id = ""
        message = ""
        if kind == "friend_request":
            direction = str(item.get("direction") or "")
            if direction not in {"incoming", "outgoing"}:
                raise LegacySocialDataError("legacy_friend_request_direction_unknown")
            request_status = str(item.get("request_status") or item.get("status") or "")
            status_known = item.get("request_status_known") is True
            if not status_known and peer_uid in friend_uids:
                request_status = "accepted"
                status_known = True
            if not status_known:
                raise LegacySocialDataError("legacy_friend_request_status_unknown")
            if request_status == "expired":
                request_status = "cancelled"
            if request_status not in {"pending", "accepted", "rejected", "cancelled"}:
                raise LegacySocialDataError("legacy_friend_request_status_invalid")
            status = "active" if request_status == "pending" else "inactive"
            resolved_as = "" if request_status == "pending" else request_status
            application_id = _bounded_text(item.get("apply_id") or peer_uid, 256)
            message = _bounded_text(
                item.get("leave_words") or "", MAX_APPLICATION_MESSAGE
            )
        profile = _public_profile(item)
        records.append(
            RelationshipRecord(
                kind=kind,
                peer_uid=peer_uid,
                status=status,
                resolved_as=resolved_as,
                direction=direction,
                application_id=application_id,
                message=message,
                profile=profile,
            )
        )
    return tuple(sorted(records, key=lambda record: record.peer_uid))


def _validate_page(page: SourcePage, *, expected_path: str, expected_page: int) -> None:
    if page.source_path != expected_path or page.page != expected_page:
        raise LegacySocialDataError("legacy_social_page_identity_mismatch")
    if page.terminal:
        if page.terminal_reason not in {
            "empty-page",
            "false-empty",
            "explicit-complete",
            "single-response",
        }:
            raise LegacySocialDataError("legacy_social_terminal_unknown")
    elif not page.items:
        raise LegacySocialDataError("legacy_social_nonterminal_empty_page")
    if len(page.items) > MAX_PAGE_ITEMS:
        raise LegacySocialLimitError("legacy_social_page_item_limit_exceeded")


def _scan_pages(
    fetch: Callable[[int], SourcePage], *, source_path: str
) -> tuple[tuple[Mapping[str, Any], ...], SourceWatermark]:
    all_items: list[Mapping[str, Any]] = []
    signatures: set[str] = set()
    for page_number in range(1, MAX_SOURCE_PAGES + 1):
        page = fetch(page_number)
        _validate_page(
            page, expected_path=source_path, expected_page=page_number
        )
        if page.items:
            signature = _json_digest(page.items)
            if signature in signatures:
                raise LegacySocialDataError("legacy_social_page_repeated")
            signatures.add(signature)
            all_items.extend(page.items)
            if len(all_items) > MAX_SCOPE_RECORDS:
                raise LegacySocialLimitError("legacy_social_scope_limit_exceeded")
        if page.terminal:
            return (
                tuple(all_items),
                SourceWatermark(
                    source_path=source_path,
                    pages=page_number,
                    last_page=page_number,
                    records=len(all_items),
                    terminal=page.terminal_reason,
                ),
            )
    raise LegacySocialLimitError("legacy_social_page_limit_exceeded")


def _single_page(
    page: SourcePage, *, source_path: str
) -> tuple[tuple[Mapping[str, Any], ...], SourceWatermark]:
    _validate_page(page, expected_path=source_path, expected_page=1)
    if not page.terminal:
        raise LegacySocialDataError("legacy_social_single_page_incomplete")
    return (
        tuple(page.items),
        SourceWatermark(
            source_path=source_path,
            pages=1,
            last_page=1,
            records=len(page.items),
            terminal=page.terminal_reason,
        ),
    )


def fetch_complete_social_snapshot(
    reader: LegacySocialReader,
    account: LegacySocialAccount,
    *,
    coverage_started_at: datetime,
    clock=utcnow,
) -> FetchedSocialSnapshot:
    """完成全部网络读取和结构验证；本函数不访问数据库。"""

    profile = _profile_values(
        reader.fetch_profile(), own_uid=account.upstream_uid
    )
    following_raw, following_watermark = _scan_pages(
        reader.fetch_following_page,
        source_path=RELATION_SOURCE_PATHS["follow"],
    )
    followers_raw, followers_watermark = _scan_pages(
        reader.fetch_followers_page,
        source_path=RELATION_SOURCE_PATHS["follower"],
    )
    friends_raw, friends_watermark = _single_page(
        reader.fetch_friends(), source_path=RELATION_SOURCE_PATHS["friend"]
    )
    requests_raw, requests_watermark = _scan_pages(
        reader.fetch_friend_requests_page,
        source_path=RELATION_SOURCE_PATHS["friend_request"],
    )
    blacklist_raw, blacklist_watermark = _single_page(
        reader.fetch_blacklist(), source_path=RELATION_SOURCE_PATHS["blacklist"]
    )
    blocked_by_raw, blocked_by_watermark = _single_page(
        reader.fetch_blacklisted_by(),
        source_path=RELATION_SOURCE_PATHS["blacklisted_by"],
    )
    window = ImportWindow(
        coverage_started_at=_as_utc(coverage_started_at),
        coverage_ended_at=_as_utc(clock()),
    )
    if window.coverage_ended_at < window.coverage_started_at:
        raise LegacySocialDataError("legacy_social_coverage_clock_invalid")

    friends = _relationship_records(
        "friend", friends_raw, own_uid=account.upstream_uid
    )
    friend_uids = frozenset(record.peer_uid for record in friends)
    records = {
        "follow": _relationship_records(
            "follow", following_raw, own_uid=account.upstream_uid
        ),
        "follower": _relationship_records(
            "follower", followers_raw, own_uid=account.upstream_uid
        ),
        "friend": friends,
        "friend_request": _relationship_records(
            "friend_request",
            requests_raw,
            own_uid=account.upstream_uid,
            friend_uids=friend_uids,
        ),
        "blacklist": _relationship_records(
            "blacklist", blacklist_raw, own_uid=account.upstream_uid
        ),
        "blacklisted_by": _relationship_records(
            "blacklisted_by", blocked_by_raw, own_uid=account.upstream_uid
        ),
    }
    watermarks = {
        PROFILE_SOURCE_PATH: SourceWatermark(
            PROFILE_SOURCE_PATH, 1, 1, 1, "single-response"
        ),
        following_watermark.source_path: following_watermark,
        followers_watermark.source_path: followers_watermark,
        friends_watermark.source_path: friends_watermark,
        requests_watermark.source_path: requests_watermark,
        blacklist_watermark.source_path: blacklist_watermark,
        blocked_by_watermark.source_path: blocked_by_watermark,
    }
    if set(watermarks) != set(SOCIAL_MARKER_SOURCE_PATHS):
        raise LegacySocialDataError("legacy_social_source_coverage_invalid")
    counts = {
        "profile": 1,
        "relationships": sum(
            len(records[kind]) for kind in ("follow", "follower", "friend")
        ),
        "friend-requests": len(records["friend_request"]),
        "blocklists": len(records["blacklist"])
        + len(records["blacklisted_by"]),
    }
    coverage = {
        scope: {
            "source_complete": True,
            "records": counts[scope],
            "source_paths": list(paths),
        }
        for scope, paths in SOCIAL_MARKER_SCOPE_PATHS.items()
    }
    profile_digest = _json_digest(profile)
    identities = [f"profile:{profile_digest}"]
    for kind in sorted(records):
        identities.extend(
            f"{kind}:{record.peer_uid}:{record.identity()}"
            for record in records[kind]
        )
    record_digest = _json_digest(identities)
    return FetchedSocialSnapshot(
        window=window,
        profile=profile,
        profile_source_digest=profile_digest,
        records=records,
        watermarks=watermarks,
        counts=counts,
        coverage=coverage,
        record_digest=record_digest,
    )


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    mapping = getattr(row, "_mapping", None)
    if isinstance(mapping, Mapping) and name in mapping:
        return mapping[name]
    return getattr(row, name, default)


def _relationship_snapshot_rows(
    existing_rows: Sequence[Any],
    *,
    account: LegacySocialAccount,
    snapshot: FetchedSocialSnapshot,
    kind: str,
    records: Sequence[RelationshipRecord],
    source_path: str,
) -> tuple[list[dict[str, Any]], int]:
    """规划 legacy upsert；任何 web-local 行都不会进入写集合。"""

    existing_by_peer = {
        _uid(_row_value(row, "subject_upstream_uid")): row
        for row in existing_rows
        if str(_row_value(row, "provider") or "") == LEGACY_PROVIDER
        and str(_row_value(row, "kind") or "") == kind
        and _uid(_row_value(row, "subject_upstream_uid"))
    }
    expected = {record.peer_uid: record for record in records}
    rows: list[dict[str, Any]] = []
    deactivated = 0
    observed_at = snapshot.window.coverage_ended_at
    for peer_uid, record in sorted(expected.items()):
        existing = existing_by_peer.get(peer_uid)
        previous_metadata = _row_value(
            existing, "extra_data", _row_value(existing, "relationship_metadata", {})
        )
        metadata = dict(previous_metadata or {}) if isinstance(previous_metadata, Mapping) else {}
        metadata.update(
            {
                "server_owned": True,
                "source_path": source_path,
                "migration_domain": "social",
                "migration_record_digest": snapshot.record_digest,
                "migration_record_identity": record.identity(),
                "snapshot_absent": False,
                "snapshot_complete": True,
                "snapshot_observed_at": observed_at.isoformat(),
                "profile": dict(record.profile),
            }
        )
        if kind in {"blacklist", "blacklisted_by"}:
            metadata["message_policy_source"] = source_path
        if kind == "friend_request":
            metadata.update(
                {
                    "apply_id": record.application_id,
                    "direction": record.direction,
                    "message": record.message,
                    "resolved_as": record.resolved_as,
                    "last_event_type": (
                        "api.social.add-friend"
                        if record.direction == "outgoing"
                        else "api.social.friend-apply"
                    ),
                }
            )
        previous_status = str(_row_value(existing, "status") or "")
        started_at = _row_value(existing, "started_at")
        if not isinstance(started_at, datetime) or previous_status != record.status:
            started_at = observed_at
        rows.append(
            {
                "owner_user_id": account.owner_user_id,
                "provider": LEGACY_PROVIDER,
                "subject_upstream_uid": peer_uid,
                "kind": kind,
                "status": record.status,
                "started_at": started_at,
                "ended_at": None if record.status == "active" else observed_at,
                "extra_data": metadata,
            }
        )
    for peer_uid, existing in sorted(existing_by_peer.items()):
        if peer_uid in expected:
            continue
        previous_metadata = _row_value(
            existing, "extra_data", _row_value(existing, "relationship_metadata", {})
        )
        metadata = dict(previous_metadata or {}) if isinstance(previous_metadata, Mapping) else {}
        metadata.update(
            {
                "server_owned": True,
                "source_path": source_path,
                "migration_domain": "social",
                "migration_record_digest": snapshot.record_digest,
                "snapshot_absent": True,
                "snapshot_complete": True,
                "snapshot_observed_at": observed_at.isoformat(),
            }
        )
        if kind in {"blacklist", "blacklisted_by"}:
            metadata["message_policy_source"] = source_path
        was_active = (
            str(_row_value(existing, "status") or "") == "active"
            and _row_value(existing, "ended_at") is None
        )
        if was_active:
            deactivated += 1
        started_at = _row_value(existing, "started_at")
        if not isinstance(started_at, datetime):
            started_at = observed_at
        ended_at = _row_value(existing, "ended_at")
        rows.append(
            {
                "owner_user_id": account.owner_user_id,
                "provider": LEGACY_PROVIDER,
                "subject_upstream_uid": peer_uid,
                "kind": kind,
                "status": "inactive",
                "started_at": started_at,
                "ended_at": ended_at if isinstance(ended_at, datetime) else observed_at,
                "extra_data": metadata,
            }
        )
    return rows, deactivated


DbScope = Callable[[], ContextManager[Any]]


class SqlAlchemyLegacySocialWriter:
    """网络完成后分短事务写入，最后单独验证并生成 Marker。"""

    def __init__(self, *, db_scope: DbScope = session_scope, clock=utcnow) -> None:
        self.db_scope = db_scope
        self.clock = clock

    @staticmethod
    def _base_marker(
        account: LegacySocialAccount,
        window: ImportWindow,
        *,
        phase: str,
        complete: bool = False,
    ) -> dict[str, Any]:
        return {
            "complete": complete,
            "source_complete": complete,
            "schema": DOMAIN_MARKER_SCHEMA,
            "domain": "social",
            "owner_user_id": str(account.owner_user_id),
            "external_account_id": str(account.external_account_id),
            "upstream_uid": account.upstream_uid,
            "coverage_started_at": window.coverage_started_at.isoformat(),
            "coverage_ended_at": window.coverage_ended_at.isoformat(),
            "phase": phase,
        }

    def _write_cursor(
        self,
        db: Any,
        account: LegacySocialAccount,
        window: ImportWindow,
        *,
        marker: Mapping[str, Any],
        succeeded_at: datetime | None,
        error: str | None,
    ) -> None:
        attempted_at = _as_utc(self.clock())
        watermark = max(window.coverage_ended_at, attempted_at)
        SyncCursorRepository(db).upsert(
            owner_user_id=account.owner_user_id,
            source=LEGACY_PROVIDER,
            stream=SOCIAL_STREAM,
            cursor=json.dumps(
                dict(marker), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            watermark_at=watermark,
            next_sync_at=None,
            last_attempted_at=attempted_at,
            last_succeeded_at=succeeded_at,
            last_error=error,
            version=1,
        )

    @staticmethod
    def _binding(db: Any, account: LegacySocialAccount, *, lock: bool = False):
        statement = (
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id == account.owner_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.id == account.external_account_id,
                ExternalAccount.provider == LEGACY_PROVIDER,
                ExternalAccount.upstream_uid == account.upstream_uid,
            )
        )
        if lock:
            statement = statement.with_for_update()
        row = db.execute(statement).one_or_none()
        if row is None:
            raise LegacySocialDataError("legacy_account_binding_changed")
        return row[0], row[1]

    def begin(self, account: LegacySocialAccount, window: ImportWindow) -> None:
        with self.db_scope() as db:
            self._binding(db, account)
            self._write_cursor(
                db,
                account,
                window,
                marker=self._base_marker(account, window, phase="applying"),
                succeeded_at=None,
                error=None,
            )

    def _apply_profile(
        self, account: LegacySocialAccount, snapshot: FetchedSocialSnapshot
    ) -> int:
        with self.db_scope() as db:
            user, _external = self._binding(db, account, lock=True)
            current = dict(user.profile or {})
            incoming = {**current, **snapshot.profile}
            merged = merge_provider_profile_preserving_local(current, incoming)
            local_fields = set(current.get(WEB_LOCAL_PROFILE_FIELDS_KEY) or ())
            nickname = str(snapshot.profile.get("nickname") or "")
            changed = merged != current
            if nickname and "nickname" not in local_fields and user.display_name != nickname:
                user.display_name = nickname[:160]
                changed = True
            if changed:
                user.profile = merged
                user.updated_at = snapshot.window.coverage_ended_at
            return int(changed)

    def _apply_kind(
        self,
        account: LegacySocialAccount,
        snapshot: FetchedSocialSnapshot,
        *,
        kind: str,
    ) -> tuple[int, int]:
        with self.db_scope() as db:
            self._binding(db, account, lock=True)
            existing = list(
                db.scalars(
                    select(Relationship).where(
                        Relationship.owner_user_id == account.owner_user_id,
                        Relationship.kind == kind,
                        Relationship.provider.in_(
                            (LEGACY_PROVIDER, LOCAL_SOCIAL_PROVIDER)
                        ),
                    )
                )
            )
            rows, deactivated = _relationship_snapshot_rows(
                existing,
                account=account,
                snapshot=snapshot,
                kind=kind,
                records=snapshot.records[kind],
                source_path=RELATION_SOURCE_PATHS[kind],
            )
            RelationshipRepository(db).upsert_many(rows)
            return len(rows), deactivated

    def apply(
        self, account: LegacySocialAccount, snapshot: FetchedSocialSnapshot
    ) -> WriteSummary:
        profile_written = self._apply_profile(account, snapshot)
        relationship_rows_written = 0
        legacy_rows_deactivated = 0
        for kind in RELATION_SOURCE_PATHS:
            written, deactivated = self._apply_kind(
                account, snapshot, kind=kind
            )
            relationship_rows_written += written
            legacy_rows_deactivated += deactivated
        return WriteSummary(
            profile_written=profile_written,
            relationship_rows_written=relationship_rows_written,
            legacy_rows_deactivated=legacy_rows_deactivated,
        )

    @staticmethod
    def _verify_profile(user: User, snapshot: FetchedSocialSnapshot) -> None:
        current = dict(user.profile or {})
        local_fields = set(current.get(WEB_LOCAL_PROFILE_FIELDS_KEY) or ())
        for field, expected in snapshot.profile.items():
            if field in local_fields:
                continue
            if current.get(field) != expected:
                raise LegacySocialDataError("legacy_profile_write_not_verified")
        if (
            "nickname" not in local_fields
            and str(user.display_name or "")
            != str(snapshot.profile.get("nickname") or "")
        ):
            raise LegacySocialDataError("legacy_profile_name_write_not_verified")

    @staticmethod
    def _verify_relationships(
        rows: Sequence[Relationship], snapshot: FetchedSocialSnapshot
    ) -> None:
        by_kind: dict[str, dict[str, Relationship]] = {
            kind: {} for kind in RELATION_SOURCE_PATHS
        }
        for row in rows:
            kind = str(row.kind or "")
            peer = _uid(row.subject_upstream_uid)
            if kind not in by_kind or not peer or peer in by_kind[kind]:
                raise LegacySocialDataError("legacy_social_stored_row_invalid")
            by_kind[kind][peer] = row
        identities = [f"profile:{snapshot.profile_source_digest}"]
        for kind in sorted(snapshot.records):
            expected = {record.peer_uid: record for record in snapshot.records[kind]}
            stored = by_kind[kind]
            for peer, row in sorted(stored.items()):
                metadata = dict(row.extra_data or {})
                if (
                    metadata.get("server_owned") is not True
                    or metadata.get("source_path")
                    != RELATION_SOURCE_PATHS[kind]
                    or metadata.get("migration_domain") != "social"
                    or metadata.get("migration_record_digest")
                    != snapshot.record_digest
                    or metadata.get("snapshot_complete") is not True
                    or metadata.get("snapshot_observed_at")
                    != snapshot.window.coverage_ended_at.isoformat()
                    or (
                        kind in {"blacklist", "blacklisted_by"}
                        and metadata.get("message_policy_source")
                        != RELATION_SOURCE_PATHS[kind]
                    )
                ):
                    raise LegacySocialDataError(
                        "legacy_social_concurrent_write_detected"
                    )
                record = expected.get(peer)
                if record is None:
                    if (
                        row.status != "inactive"
                        or row.ended_at is None
                        or metadata.get("snapshot_absent") is not True
                    ):
                        raise LegacySocialDataError(
                            "legacy_social_stale_row_not_deactivated"
                        )
                    continue
                if (
                    row.status != record.status
                    or (record.status == "active" and row.ended_at is not None)
                    or (record.status != "active" and row.ended_at is None)
                    or metadata.get("snapshot_absent") is not False
                    or metadata.get("migration_record_identity")
                    != record.identity()
                    or metadata.get("profile") != record.profile
                    or (
                        kind == "friend_request"
                        and (
                            str(metadata.get("apply_id") or "")
                            != record.application_id
                            or str(metadata.get("direction") or "")
                            != record.direction
                            or str(metadata.get("message") or "")
                            != record.message
                            or str(metadata.get("resolved_as") or "")
                            != record.resolved_as
                            or str(metadata.get("last_event_type") or "")
                            != (
                                "api.social.add-friend"
                                if record.direction == "outgoing"
                                else "api.social.friend-apply"
                            )
                        )
                    )
                ):
                    raise LegacySocialDataError(
                        "legacy_social_relationship_write_not_verified"
                    )
                identities.append(
                    f"{kind}:{record.peer_uid}:{record.identity()}"
                )
            if set(expected) - set(stored):
                raise LegacySocialDataError("legacy_social_relationship_missing")
        if _json_digest(identities) != snapshot.record_digest:
            raise LegacySocialDataError("legacy_social_record_digest_mismatch")

    def complete(
        self, account: LegacySocialAccount, snapshot: FetchedSocialSnapshot
    ) -> None:
        with self.db_scope() as db:
            user, _external = self._binding(db, account, lock=True)
            rows = list(
                db.scalars(
                    select(Relationship).where(
                        Relationship.owner_user_id == account.owner_user_id,
                        Relationship.provider == LEGACY_PROVIDER,
                        Relationship.kind.in_(tuple(RELATION_SOURCE_PATHS)),
                    )
                )
            )
            self._verify_profile(user, snapshot)
            self._verify_relationships(rows, snapshot)
            marker = self._base_marker(
                account, snapshot.window, phase="complete", complete=True
            )
            marker.update(
                {
                    "scopes": list(DOMAIN_MARKER_SCOPES["social"]),
                    "counts": dict(snapshot.counts),
                    "source_paths": list(SOCIAL_MARKER_SOURCE_PATHS),
                    "coverage": dict(snapshot.coverage),
                    "watermarks": {
                        path: snapshot.watermarks[path].marker()
                        for path in SOCIAL_MARKER_SOURCE_PATHS
                    },
                    "record_digest": snapshot.record_digest,
                    "unresolved_records": 0,
                }
            )
            completed_at = _as_utc(self.clock())
            self._write_cursor(
                db,
                account,
                snapshot.window,
                marker=marker,
                succeeded_at=completed_at,
                error=None,
            )

    def fail(
        self,
        account: LegacySocialAccount,
        window: ImportWindow,
        *,
        code: str,
    ) -> None:
        marker = self._base_marker(account, window, phase="failed")
        marker["failure_code"] = str(code or "legacy_social_failure")[:160]
        with self.db_scope() as db:
            self._write_cursor(
                db,
                account,
                window,
                marker=marker,
                succeeded_at=None,
                error=marker["failure_code"],
            )


class LegacySocialImportOrchestrator:
    def __init__(
        self,
        reader: LegacySocialReader,
        writer: LegacySocialWriter,
        *,
        clock=utcnow,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.clock = clock

    def run(self, account: LegacySocialAccount) -> ImportSummary:
        started_at = _as_utc(self.clock())
        initial_window = ImportWindow(started_at, started_at)
        snapshot: FetchedSocialSnapshot | None = None
        try:
            snapshot = fetch_complete_social_snapshot(
                self.reader,
                account,
                coverage_started_at=started_at,
                clock=self.clock,
            )
            self.writer.begin(account, snapshot.window)
            writes = self.writer.apply(account, snapshot)
            self.writer.complete(account, snapshot)
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, LegacySocialMigrationError)
                else f"legacy_social_exception.{type(exc).__name__}"
            )
            ended_at = (
                snapshot.window.coverage_ended_at
                if snapshot is not None
                else _as_utc(self.clock())
            )
            try:
                self.writer.fail(
                    account,
                    ImportWindow(started_at, max(started_at, ended_at)),
                    code=code,
                )
            except Exception:
                # The original migration failure is the actionable cause.  A
                # failed best-effort status write must not replace it, and this
                # path never creates a new completion Marker.
                pass
            raise
        return ImportSummary(
            source_pages=sum(value.pages for value in snapshot.watermarks.values()),
            source_records=sum(value.records for value in snapshot.watermarks.values()),
            profile_written=writes.profile_written,
            relationship_rows_written=writes.relationship_rows_written,
            legacy_rows_deactivated=writes.legacy_rows_deactivated,
            counts=dict(snapshot.counts),
            record_digest=snapshot.record_digest,
        )


def _load_account(db: Any, owner_user_id: uuid.UUID) -> LegacySocialAccount:
    row = db.execute(
        select(User, ExternalAccount)
        .join(ExternalAccount, ExternalAccount.user_id == User.id)
        .where(
            User.id == owner_user_id,
            User.status == "active",
            User.disabled_at.is_(None),
            ExternalAccount.provider == LEGACY_PROVIDER,
        )
    ).one_or_none()
    if row is None:
        raise LegacySocialDataError("legacy_account_binding_missing")
    user, account = row
    upstream_uid = _uid(account.upstream_uid)
    if not upstream_uid:
        raise LegacySocialDataError("legacy_upstream_uid_missing")
    if not isinstance(account.login_account_encrypted, Mapping):
        raise LegacySocialDataError("legacy_login_credential_missing")
    if account.password_encrypted is not None and not isinstance(
        account.password_encrypted, Mapping
    ):
        raise LegacySocialDataError("legacy_password_credential_invalid")
    if account.token_encrypted is not None and not isinstance(
        account.token_encrypted, Mapping
    ):
        raise LegacySocialDataError("legacy_token_credential_invalid")
    return LegacySocialAccount(
        owner_user_id=user.id,
        external_account_id=account.id,
        upstream_uid=upstream_uid,
        display_name=str(user.display_name or ""),
        profile=dict(user.profile or {}),
        device_data=dict(account.device_data or {}),
        login_encrypted=dict(account.login_account_encrypted),
        password_encrypted=(
            dict(account.password_encrypted)
            if isinstance(account.password_encrypted, Mapping)
            else None
        ),
        token_encrypted=(
            dict(account.token_encrypted)
            if isinstance(account.token_encrypted, Mapping)
            else None
        ),
        token_expires_at=account.token_expires_at,
    )


def _runtime_state(
    account: LegacySocialAccount,
    *,
    uid: str,
    token: str,
    phone: str,
) -> ProviderSessionState:
    data = account.device_data
    return ProviderSessionState(
        uid=uid,
        token=token,
        phone=phone,
        nickname=account.display_name,
        user_role=str(data.get("user_role") or ""),
        rp_verify_time=str(data.get("rp_verify_time") or "0"),
        vip=str(data.get("vip") or "0"),
        svip=str(data.get("svip") or "0"),
        money=str(data.get("money") or "0"),
        portrait=str(data.get("portrait") or ""),
        user_sign=str(data.get("user_sign") or ""),
        login_id=str(data.get("login_id") or ""),
        raw_user=account.profile if uid == account.upstream_uid else {},
        device_data=data,
    )


def _credential_context(account: LegacySocialAccount, field: str) -> str:
    return f"external-account:{account.external_account_id}:{field}"


def _decrypt_required_credential(
    cipher: CredentialCipher,
    encrypted: Mapping[str, Any] | None,
    *,
    account: LegacySocialAccount,
    field: str,
    reject_zero: bool = True,
) -> str:
    if not isinstance(encrypted, Mapping):
        raise LegacySocialDataError(f"legacy_{field}_credential_missing")
    try:
        value = cipher.decrypt_text(
            dict(encrypted),
            purpose=f"external-account.{field}",
            context=_credential_context(account, field),
        )
    except Exception as exc:
        raise LegacySocialDataError(
            f"legacy_{field}_credential_decryption_failed"
        ) from exc
    normalized = str(value or "")
    if not normalized or not normalized.strip() or (
        reject_zero and normalized.strip() == "0"
    ):
        raise LegacySocialDataError(f"legacy_{field}_credential_empty")
    return normalized


def _session_refresh_state(
    session: Any, *, authenticated_at: datetime
) -> RefreshedProviderState:
    token = str(getattr(session, "token", "") or "").strip()
    if (
        not token
        or token == "0"
        or len(token) > 8192
        or any(ord(character) < 33 for character in token)
    ):
        raise LegacySocialDataError("legacy_reauthenticated_token_invalid")
    device_data: dict[str, Any] = {}
    device_dict = getattr(session, "device_dict", None)
    if callable(device_dict):
        try:
            values = device_dict()
        except Exception as exc:
            raise LegacySocialDataError(
                "legacy_reauthenticated_device_state_invalid"
            ) from exc
        if isinstance(values, Mapping):
            for field in (
                "phonebrand",
                "pushregid",
                "device_id",
                "version_code",
                "package_name",
                "user_agent",
            ):
                value = values.get(field)
                if value in (None, ""):
                    continue
                text = str(value)
                if len(text) > 2048 or "\x00" in text:
                    raise LegacySocialDataError(
                        "legacy_reauthenticated_device_state_invalid"
                    )
                device_data[field] = text
    for field in (
        "user_role",
        "rp_verify_time",
        "vip",
        "svip",
        "money",
        "portrait",
        "user_sign",
        "login_id",
    ):
        value = getattr(session, field, None)
        if value not in (None, ""):
            text = str(value)
            if len(text) > 4096 or "\x00" in text:
                raise LegacySocialDataError(
                    "legacy_reauthenticated_account_state_invalid"
                )
            device_data[field] = text
    return RefreshedProviderState(
        token=token,
        authenticated_at=_as_utc(authenticated_at),
        device_data=device_data,
    )


def _authentication_result_error(result: Any) -> LegacySocialMigrationError:
    try:
        status = int(getattr(result, "status", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    if status < 0 or 500 <= status < 600:
        return LegacySocialProviderError(
            "legacy_password_auth_provider_unavailable"
        )
    return LegacySocialAuthenticationRejected(
        "legacy_password_authentication_rejected"
    )


def _runtime(
    account: LegacySocialAccount,
    *,
    cipher: CredentialCipher,
    provider: RuntimeProvider,
    now: datetime,
) -> ResolvedProviderRuntime:
    now = _as_utc(now)
    login = _decrypt_required_credential(
        cipher,
        account.login_encrypted,
        account=account,
        field="login",
    ).strip()
    if not login or len(login) > 512 or any(ord(character) < 33 for character in login):
        raise LegacySocialDataError("legacy_login_credential_invalid")

    token_expired = bool(
        account.token_expires_at is not None
        and _as_utc(account.token_expires_at) <= now
    )
    token = ""
    if not token_expired and isinstance(account.token_encrypted, Mapping):
        try:
            token = cipher.decrypt_text(
                account.token_encrypted,
                purpose="external-account.token",
                context=_credential_context(account, "token"),
            )
        except Exception:
            token = ""
    token = str(token or "").strip()
    if len(token) > 8192 or any(ord(character) < 33 for character in token):
        token = ""
    if token and token != "0":
        try:
            runtime = provider.create_runtime_from_state(
                _runtime_state(
                    account,
                    uid=account.upstream_uid,
                    token=token,
                    phone=login,
                )
            )
        except ProviderUnavailable as exc:
            raise LegacySocialProviderError(
                "legacy_runtime_restore_provider_unavailable"
            ) from exc
        except ProviderAuthenticationRejected as exc:
            raise LegacySocialAuthenticationRejected(
                "legacy_runtime_restore_rejected"
            ) from exc
        except Exception as exc:
            raise LegacySocialAuthenticationError(
                "legacy_runtime_restore_failed"
            ) from exc
        session = getattr(getattr(runtime, "app", None), "session", None)
        if bool(getattr(session, "logged_in", False)):
            restored_uid = _uid(getattr(session, "uid", ""))
            if restored_uid != account.upstream_uid:
                _close_runtime(runtime)
                raise LegacySocialDataError("legacy_runtime_uid_mismatch")
            return ResolvedProviderRuntime(runtime=runtime)
        _close_runtime(runtime)

    password = _decrypt_required_credential(
        cipher,
        account.password_encrypted,
        account=account,
        field="password",
        reject_zero=False,
    )
    try:
        runtime = provider.create_runtime_from_state(
            _runtime_state(account, uid="0", token="0", phone=login)
        )
    except ProviderUnavailable as exc:
        raise LegacySocialProviderError(
            "legacy_password_runtime_provider_unavailable"
        ) from exc
    except ProviderAuthenticationRejected as exc:
        raise LegacySocialAuthenticationRejected(
            "legacy_password_runtime_rejected"
        ) from exc
    except Exception as exc:
        raise LegacySocialAuthenticationError(
            "legacy_password_runtime_create_failed"
        ) from exc

    session = getattr(getattr(runtime, "app", None), "session", None)
    try:
        result = runtime.app.auth.login_password(login, password)
    except ProviderUnavailable as exc:
        _close_runtime(runtime)
        raise LegacySocialProviderError(
            "legacy_password_auth_provider_unavailable"
        ) from exc
    except ProviderAuthenticationRejected as exc:
        _close_runtime(runtime)
        raise LegacySocialAuthenticationRejected(
            "legacy_password_authentication_rejected"
        ) from exc
    except Exception as exc:
        _close_runtime(runtime)
        raise LegacySocialAuthenticationError(
            "legacy_password_authentication_exception"
        ) from exc
    finally:
        if session is not None:
            try:
                session.password = ""
            except Exception:
                pass

    if not bool(getattr(result, "ok", False)) or not bool(
        getattr(session, "logged_in", False)
    ):
        error = _authentication_result_error(result)
        _close_runtime(runtime)
        raise error
    authenticated_uid = _uid(getattr(session, "uid", ""))
    if authenticated_uid != account.upstream_uid:
        _close_runtime(runtime)
        raise LegacySocialDataError("legacy_reauthenticated_uid_mismatch")
    try:
        refreshed = _session_refresh_state(session, authenticated_at=now)
    except Exception:
        _close_runtime(runtime)
        raise
    return ResolvedProviderRuntime(runtime=runtime, refreshed=refreshed)


def _close_runtime(runtime: Any) -> None:
    try:
        close = getattr(getattr(getattr(runtime, "app", None), "client", None), "close", None)
        if callable(close):
            close()
    except Exception:
        return


def _persist_refreshed_provider_state(
    account: LegacySocialAccount,
    refreshed: RefreshedProviderState,
    *,
    cipher: CredentialCipher,
    db_scope: DbScope,
) -> None:
    try:
        encrypted_token = cipher.encrypt_text(
            refreshed.token,
            purpose="external-account.token",
            context=_credential_context(account, "token"),
        )
    except Exception as exc:
        raise LegacySocialDataError(
            "legacy_reauthenticated_token_encryption_failed"
        ) from exc
    if not isinstance(encrypted_token, Mapping):
        raise LegacySocialDataError(
            "legacy_reauthenticated_token_encryption_invalid"
        )

    with db_scope() as db:
        row = db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id == account.owner_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.id == account.external_account_id,
                ExternalAccount.provider == LEGACY_PROVIDER,
                ExternalAccount.upstream_uid == account.upstream_uid,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise LegacySocialDataError(
                "legacy_account_binding_changed_after_authentication"
            )
        _user, external = row
        external.token_encrypted = dict(encrypted_token)
        external.token_expires_at = None
        external.last_authenticated_at = refreshed.authenticated_at
        external.device_data = {
            **dict(external.device_data or {}),
            **dict(refreshed.device_data or {}),
        }


def run_legacy_social_import(
    owner_user_id: uuid.UUID | str,
    *,
    settings: Settings | None = None,
    runtime_provider: RuntimeProvider | None = None,
    cipher: CredentialCipher | None = None,
    db_scope: DbScope = session_scope,
    clock=utcnow,
) -> ImportSummary:
    try:
        owner_id = (
            owner_user_id
            if isinstance(owner_user_id, uuid.UUID)
            else uuid.UUID(str(owner_user_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise LegacySocialDataError("owner_user_id_invalid") from exc
    with db_scope() as db:
        account = _load_account(db, owner_id)
    writer = SqlAlchemyLegacySocialWriter(db_scope=db_scope, clock=clock)
    started_at = _as_utc(clock())
    runtime = None
    orchestrator_started = False
    try:
        resolved_cipher = cipher or CredentialCipher.from_settings(
            settings or get_settings()
        )
        if runtime_provider is None:
            from bbw_web.providers import LegacyBanghuaProvider

            runtime_provider = LegacyBanghuaProvider()
        resolved = _runtime(
            account,
            cipher=resolved_cipher,
            provider=runtime_provider,
            now=started_at,
        )
        runtime = resolved.runtime
        if resolved.refreshed is not None:
            _persist_refreshed_provider_state(
                account,
                resolved.refreshed,
                cipher=resolved_cipher,
                db_scope=db_scope,
            )
        reader = BanghuaSocialReader(
            runtime.app.profile,
            runtime.app.social,
            upstream_uid=account.upstream_uid,
        )
        orchestrator_started = True
        return LegacySocialImportOrchestrator(
            reader, writer, clock=clock
        ).run(account)
    except Exception as exc:
        if not orchestrator_started:
            code = (
                exc.code
                if isinstance(exc, LegacySocialMigrationError)
                else f"legacy_social_exception.{type(exc).__name__}"
            )
            try:
                writer.fail(
                    account,
                    ImportWindow(started_at, max(started_at, _as_utc(clock()))),
                    code=code,
                )
            except Exception:
                pass
        raise
    finally:
        if runtime is not None:
            _close_runtime(runtime)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按经审核的内部 User UUID 主动迁移一个账号的完整社交域"
    )
    parser.add_argument(
        "--owner-user-id",
        required=True,
        help="经审核的内部 User UUID；不会出现在命令输出中",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_legacy_social_import(args.owner_user_id)
    except LegacySocialMigrationError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "retryable": exc.retryable},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=stdout or sys.stdout,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "legacy_social_internal_error",
                    "retryable": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=stdout or sys.stdout,
        )
        return 1
    payload = asdict(summary)
    payload.pop("record_digest", None)
    print(
        json.dumps({"ok": True, **payload}, ensure_ascii=False, sort_keys=True),
        file=stdout or sys.stdout,
    )
    return 0


__all__ = [
    "BanghuaSocialReader",
    "FetchedSocialSnapshot",
    "ImportSummary",
    "ImportWindow",
    "LegacySocialAccount",
    "LegacySocialAuthenticationError",
    "LegacySocialAuthenticationRejected",
    "LegacySocialDataError",
    "LegacySocialImportOrchestrator",
    "LegacySocialLimitError",
    "LegacySocialMigrationError",
    "LegacySocialProviderError",
    "RelationshipRecord",
    "SourcePage",
    "SourceWatermark",
    "SqlAlchemyLegacySocialWriter",
    "fetch_complete_social_snapshot",
    "run_legacy_social_import",
]


if __name__ == "__main__":
    raise SystemExit(main())
