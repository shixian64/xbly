"""Recoverable 180-day Banghua moments import.

The importer is intentionally outside the request path.  Every provider call
runs without an open PostgreSQL transaction; each fetched page is then applied
in a short local transaction, so a slow or dead legacy service cannot hold
local locks or make Web-native moments depend on Banghua.  Every retry starts
the provider scan from page one; canonical bindings make the replay idempotent
and a completion marker is written only after a full scan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ContextManager, Protocol, TextIO
from zoneinfo import ZoneInfo

from sqlalchemy import select

from bbw_prod.config import Settings, get_settings
from bbw_prod.crypto import CredentialCipher
from bbw_prod.db import session_scope
from bbw_prod.migration_readiness import (
    DOMAIN_MARKER_SCHEMA,
    DOMAIN_MARKER_SCOPES,
    DOMAIN_MARKER_STREAMS,
    MOMENTS_HISTORY_DAYS,
)
from bbw_prod.models import (
    ExternalAccount,
    LegacySocialBinding,
    SocialComment,
    SocialPost,
    SocialPostTopic,
    SocialTopic,
    SyncCursor,
    User,
    utcnow,
)
from bbw_prod.repositories import SyncCursorRepository
from bbw_web.normalize import extract_list, normalize_comments, normalize_posts
from bbw_web.providers import (
    ProviderAuthenticationRejected,
    ProviderSessionState,
    ProviderUnavailable,
    RuntimeProvider,
)

from .contracts import LegacyCommentInput, LegacyPostInput, SocialPrincipal
from .policy import SqlAlchemySocialPermissionPolicy
from .repository import SqlAlchemyCanonicalSocialStore
from .service import LocalMomentsService


LEGACY_PROVIDER = "beibeiwu"
MOMENTS_STREAM = DOMAIN_MARKER_STREAMS["moments"]
MOMENTS_SOURCE_PATHS = ("someonesluntannew", "getMainComment")
MAX_POST_PAGES = 1000
MAX_COMMENT_PAGES = 1000
MAX_POSTS = 50_000
MAX_COMMENTS_PER_POST = 10_000
MAX_PAGE_ITEMS = 500
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RELATIVE_TIME = re.compile(r"^(\d{1,4})\s*(分钟|小时|天)前$")


class LegacyMomentsMigrationError(RuntimeError):
    code = "legacy_moments_migration_error"
    retryable = False


class LegacyMomentsProviderError(LegacyMomentsMigrationError):
    code = "legacy_moments_provider_unavailable"
    retryable = True


class LegacyMomentsAuthenticationRejected(LegacyMomentsMigrationError):
    code = "legacy_moments_authentication_rejected"
    retryable = False


class LegacyMomentsAuthenticationError(LegacyMomentsMigrationError):
    code = "legacy_moments_authentication_failed"
    retryable = False


class LegacyMomentsDataError(LegacyMomentsMigrationError):
    code = "legacy_moments_data_invalid"


class LegacyMomentsLimitError(LegacyMomentsMigrationError):
    code = "legacy_moments_import_limit_exceeded"


@dataclass(frozen=True, slots=True)
class LegacyMomentsAccount:
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
class FetchedPost:
    upstream_id: str
    author_upstream_uid: str
    author_display_name: str
    title: str
    body: str
    media: dict[str, Any]
    visibility: str
    comment_policy: str
    hide_comments: bool
    source_created_at: datetime
    topics: tuple[str, ...]
    author_snapshot: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FetchedComment:
    upstream_id: str
    post_upstream_id: str
    parent_upstream_id: str
    author_upstream_uid: str
    author_display_name: str
    body: str
    status: str
    like_count: int
    reply_count: int
    source_created_at: datetime
    author_snapshot: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ImportedPostRef:
    local_public_id: str
    upstream_id: str
    author_upstream_uid: str
    hide_comments: bool


@dataclass(frozen=True, slots=True)
class ImportWindow:
    coverage_started_at: datetime
    coverage_ended_at: datetime


@dataclass(frozen=True, slots=True)
class ImportSummary:
    post_pages: int
    comment_pages: int
    posts_seen: int
    posts_created: int
    comments_seen: int
    comments_created: int
    counts: dict[str, int]
    record_digest: str


@dataclass(frozen=True, slots=True)
class BatchImportSummary:
    accounts_total: int
    accounts_succeeded: int
    accounts_failed: int
    authentication_rejected: int
    authentication_failed: int
    provider_unavailable: int
    data_invalid: int
    limit_exceeded: int
    internal_errors: int
    post_pages: int
    comment_pages: int
    posts_seen: int
    posts_created: int
    comments_seen: int
    comments_created: int
    posts: int
    comments: int
    topics: int


@dataclass(frozen=True, slots=True)
class RefreshedProviderState:
    token: str
    authenticated_at: datetime
    device_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResolvedProviderRuntime:
    runtime: Any
    refreshed: RefreshedProviderState | None = None


class LegacyMomentsReader(Protocol):
    def fetch_post_page(self, page: int) -> Sequence[Mapping[str, Any]]: ...

    def fetch_comment_page(
        self,
        *,
        post_upstream_id: str,
        author_upstream_uid: str,
        hide_comments: bool,
        page: int,
    ) -> Sequence[Mapping[str, Any]]: ...


class LegacyMomentsWriter(Protocol):
    def begin(self, account: LegacyMomentsAccount, window: ImportWindow) -> None: ...

    def import_posts(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        posts: Sequence[FetchedPost],
        *,
        page: int,
    ) -> int: ...

    def list_imported_posts(
        self, account: LegacyMomentsAccount, window: ImportWindow
    ) -> Sequence[ImportedPostRef]: ...

    def import_comments(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        post: ImportedPostRef,
        comments: Sequence[FetchedComment],
    ) -> int: ...

    def progress(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        *,
        phase: str,
        post_pages: int,
        comment_pages: int,
        posts_seen: int,
        comments_seen: int,
    ) -> None: ...

    def complete(
        self, account: LegacyMomentsAccount, window: ImportWindow
    ) -> tuple[dict[str, int], str]: ...

    def fail(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        *,
        code: str,
    ) -> None: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _identifier(value: Any, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or normalized.lower() in {"0", "none", "null"}
        or len(normalized) > limit
        or any(ord(character) < 32 for character in normalized)
    ):
        return ""
    return normalized


def _nonnegative(value: Any) -> int:
    try:
        parsed = int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, 2**31 - 1))


def _parse_legacy_time(value: Any, *, reference: datetime) -> datetime:
    reference = _as_utc(reference)
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise LegacyMomentsDataError("legacy_timestamp_missing")
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            number = None
        if number is not None:
            absolute = abs(number)
            if absolute >= 10**15:
                number /= 1_000_000
            elif absolute >= 10**12:
                number /= 1_000
            try:
                parsed = datetime.fromtimestamp(number, UTC)
            except (ValueError, OSError, OverflowError) as exc:
                raise LegacyMomentsDataError("legacy_timestamp_invalid") from exc
        else:
            relative = _RELATIVE_TIME.fullmatch(raw)
            if relative:
                amount = int(relative.group(1))
                unit = relative.group(2)
                delta = {
                    "分钟": timedelta(minutes=amount),
                    "小时": timedelta(hours=amount),
                    "天": timedelta(days=amount),
                }[unit]
                return reference - delta
            if raw == "刚刚":
                return reference
            local_reference = reference.astimezone(_SHANGHAI)
            if raw.startswith("今天 ") or raw.startswith("昨天 "):
                days = 1 if raw.startswith("昨天 ") else 0
                clock = raw.split(" ", 1)[1]
                try:
                    hour, minute = (int(part) for part in clock.split(":", 1))
                    local = (local_reference - timedelta(days=days)).replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )
                except (TypeError, ValueError) as exc:
                    raise LegacyMomentsDataError("legacy_timestamp_invalid") from exc
                return local.astimezone(UTC)
            parsed = None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
            if parsed is None:
                formats = (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M",
                    "%Y-%m-%d",
                )
                for format_string in formats:
                    try:
                        parsed = datetime.strptime(raw, format_string)
                        break
                    except ValueError:
                        continue
            if parsed is None:
                for format_string in ("%m-%d %H:%M", "%m/%d %H:%M"):
                    try:
                        local = datetime.strptime(raw, format_string).replace(
                            year=local_reference.year,
                            tzinfo=_SHANGHAI,
                        )
                    except ValueError:
                        continue
                    if local > local_reference + timedelta(days=2):
                        local = local.replace(year=local.year - 1)
                    parsed = local
                    break
            if parsed is None:
                raise LegacyMomentsDataError("legacy_timestamp_invalid")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    parsed = parsed.astimezone(UTC)
    if parsed < datetime(2000, 1, 1, tzinfo=UTC) or parsed > reference + timedelta(
        minutes=5
    ):
        raise LegacyMomentsDataError("legacy_timestamp_out_of_range")
    return parsed


def _visibility(value: Any) -> str:
    raw = str(value or "").strip()
    if raw in {"private", "仅自己可见"}:
        return "private"
    if raw in {"followers", "仅好友可见", "好友及粉丝可见"}:
        return "followers"
    return "public"


def _post_record(item: Mapping[str, Any], *, reference: datetime) -> FetchedPost:
    upstream_id = _identifier(item.get("id"), limit=256)
    author_uid = _identifier(item.get("author_id"), limit=128)
    if not upstream_id or not author_uid:
        raise LegacyMomentsDataError("legacy_post_identity_invalid")
    pictures = item.get("pictures") or []
    if not isinstance(pictures, list):
        pictures = [pictures]
    media = {
        "pictures": [str(value) for value in pictures if str(value or "").strip()],
        "video": str(item.get("video") or ""),
        "cover": str(item.get("cover") or ""),
    }
    media = {key: value for key, value in media.items() if value not in ("", [])}
    topics = item.get("topics") or ()
    if not isinstance(topics, (list, tuple)):
        topics = (topics,)
    snapshot = {
        "avatar": str(item.get("avatar") or ""),
        "age": str(item.get("age") or ""),
        "gender": str(item.get("gender") or ""),
        "property": str(item.get("property") or ""),
        "region": str(item.get("region") or ""),
    }
    metadata = {
        "legacy_comment_count": _nonnegative(item.get("comment_count")),
        "legacy_like_count": _nonnegative(item.get("like_count")),
        "legacy_pinned": bool(item.get("is_pinned")),
        "plate": str(item.get("plate") or "动态"),
        "posttip": str(item.get("posttip") or "")[:160],
    }
    return FetchedPost(
        upstream_id=upstream_id,
        author_upstream_uid=author_uid,
        author_display_name=str(item.get("nickname") or "")[:160],
        title=str(item.get("title") or "").strip(),
        body=str(item.get("content") or "").strip(),
        media=media,
        visibility=_visibility(item.get("visibility_scope")),
        comment_policy=("disabled" if bool(item.get("comment_forbid")) else "open"),
        hide_comments=bool(item.get("hide_comment")),
        source_created_at=_parse_legacy_time(item.get("time"), reference=reference),
        topics=tuple(str(value).strip() for value in topics if str(value or "").strip()),
        author_snapshot=snapshot,
        metadata=metadata,
    )


def _comment_record(
    item: Mapping[str, Any],
    *,
    post_upstream_id: str,
    reference: datetime,
) -> FetchedComment:
    upstream_id = _identifier(item.get("id"), limit=256)
    author_uid = _identifier(item.get("author_id"), limit=128)
    item_post_id = _identifier(item.get("post_id"), limit=256)
    if not upstream_id or not author_uid:
        raise LegacyMomentsDataError("legacy_comment_identity_invalid")
    if item_post_id and item_post_id != post_upstream_id:
        raise LegacyMomentsDataError("legacy_comment_post_mismatch")
    main_id = _identifier(item.get("main_id"), limit=256)
    parent_id = "" if main_id in {"", upstream_id} else main_id
    body = str(item.get("content") or "").strip()
    if not body:
        raise LegacyMomentsDataError("legacy_comment_body_missing")
    return FetchedComment(
        upstream_id=upstream_id,
        post_upstream_id=post_upstream_id,
        parent_upstream_id=parent_id,
        author_upstream_uid=author_uid,
        author_display_name=str(item.get("nickname") or "")[:160],
        body=body,
        status="hidden" if bool(item.get("is_forbidden")) else "active",
        like_count=_nonnegative(item.get("like_count")),
        reply_count=_nonnegative(item.get("reply_count")),
        source_created_at=_parse_legacy_time(item.get("time"), reference=reference),
        author_snapshot={"avatar": str(item.get("avatar") or "")},
        metadata={
            "legacy_main_id": main_id,
            "legacy_sub_id": str(item.get("sub_id") or "")[:256],
            "reply_to_name": str(item.get("reply_to_name") or "")[:160],
        },
    )


def _explicit_empty_page(data: Any) -> bool:
    """只有服务端明确的空形态才允许被当作「已读到结束」。

    未知结构 fail-open 会让 normalizer 静默返回空列表，被误判为空页并
    伪造 complete Marker；因此这里对空结果做白名单判定，其余一律由
    调用方失败关闭。
    """

    if isinstance(data, (list, tuple)):
        return len(data) == 0
    if isinstance(data, str):
        return data.strip().lower() in {"", "[]", "false", "null"}
    if isinstance(data, Mapping):
        if not data:
            return True
        pending: list[tuple[Any, int]] = [(value, 1) for value in data.values()]
        saw_empty_list = False
        seen: set[int] = set()
        while pending:
            value, depth = pending.pop()
            if depth > 32:
                return False
            if isinstance(value, (list, tuple)):
                if value:
                    # 任意包裹层中的非空列表都说明响应仍承载记录；顶层空
                    # 白名单键不得遮蔽它并伪造迁移完成。
                    return False
                saw_empty_list = True
                continue
            if isinstance(value, Mapping):
                identity = id(value)
                if identity in seen:
                    return False
                seen.add(identity)
                pending.extend((nested, depth + 1) for nested in value.values())
        return saw_empty_list
    return False


def _page_signature(items: Sequence[Mapping[str, Any]]) -> str:
    identities = [str(item.get("id") or "") for item in items]
    return hashlib.sha256(
        json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class BanghuaMomentsReader:
    """Provider adapter with no database access."""

    def __init__(self, social_api: Any, *, upstream_uid: str) -> None:
        self.social_api = social_api
        self.upstream_uid = upstream_uid

    @staticmethod
    def _items(result: Any, normalizer: Callable[[Any], list[dict[str, Any]]]):
        raw = str(getattr(result, "raw", "") or "").strip().lower()
        if getattr(result, "data", None) is False or raw == "false":
            return ()
        if not bool(getattr(result, "ok", False)):
            status = int(getattr(result, "status", 0) or 0)
            raise LegacyMomentsProviderError(
                f"legacy_provider_read_failed.{status}"
            )
        data = getattr(result, "data", None)
        items = normalizer(data)
        if len(items) > MAX_PAGE_ITEMS:
            raise LegacyMomentsLimitError("legacy_page_item_limit_exceeded")
        raw_items = extract_list(data)
        if raw_items:
            if len(items) != len(raw_items):
                # 页内存在 normalizer 无法解析的条目；静默丢弃会让计数与
                # 真实历史不一致，必须失败关闭。
                raise LegacyMomentsDataError("legacy_page_item_unparseable")
            return tuple(items)
        if items:
            return tuple(items)
        if _explicit_empty_page(data):
            return ()
        # 结构不可识别（字段改名、包裹层变化等）不得被当作空页读完。
        raise LegacyMomentsDataError("legacy_page_structure_unknown")

    def fetch_post_page(self, page: int) -> Sequence[Mapping[str, Any]]:
        try:
            result = self.social_api.user_posts(self.upstream_uid, page=str(page))
        except LegacyMomentsMigrationError:
            raise
        except Exception as exc:
            raise LegacyMomentsProviderError(
                f"legacy_post_read_exception.{type(exc).__name__}"
            ) from exc
        return self._items(result, normalize_posts)

    def fetch_comment_page(
        self,
        *,
        post_upstream_id: str,
        author_upstream_uid: str,
        hide_comments: bool,
        page: int,
    ) -> Sequence[Mapping[str, Any]]:
        try:
            result = self.social_api.main_comments(
                post_upstream_id,
                author_upstream_uid,
                hide_comment="1" if hide_comments else "0",
                page=str(page),
            )
        except LegacyMomentsMigrationError:
            raise
        except Exception as exc:
            raise LegacyMomentsProviderError(
                f"legacy_comment_read_exception.{type(exc).__name__}"
            ) from exc
        return self._items(result, normalize_comments)


DbScope = Callable[[], ContextManager[Any]]


class SqlAlchemyLegacyMomentsWriter:
    """Short-transaction canonical writer and completion-marker owner."""

    def __init__(self, *, db_scope: DbScope = session_scope, clock=utcnow) -> None:
        self.db_scope = db_scope
        self.clock = clock

    @staticmethod
    def _base_marker(
        account: LegacyMomentsAccount,
        window: ImportWindow,
        *,
        phase: str,
        complete: bool = False,
    ) -> dict[str, Any]:
        return {
            "complete": complete,
            "source_complete": complete,
            "schema": DOMAIN_MARKER_SCHEMA,
            "domain": "moments",
            "external_account_id": str(account.external_account_id),
            "upstream_uid": account.upstream_uid,
            "history_days": MOMENTS_HISTORY_DAYS,
            "coverage_started_at": window.coverage_started_at.isoformat(),
            "coverage_ended_at": window.coverage_ended_at.isoformat(),
            "phase": phase,
        }

    def _write_cursor(
        self,
        db: Any,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        *,
        marker: Mapping[str, Any],
        succeeded_at: datetime | None,
        error: str | None,
    ) -> None:
        attempted_at = _as_utc(self.clock())
        SyncCursorRepository(db).upsert(
            owner_user_id=account.owner_user_id,
            source=LEGACY_PROVIDER,
            stream=MOMENTS_STREAM,
            cursor=json.dumps(
                dict(marker), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            watermark_at=window.coverage_ended_at,
            next_sync_at=None,
            last_attempted_at=attempted_at,
            last_succeeded_at=succeeded_at,
            last_error=error,
        )

    def begin(self, account: LegacyMomentsAccount, window: ImportWindow) -> None:
        with self.db_scope() as db:
            self._write_cursor(
                db,
                account,
                window,
                marker=self._base_marker(account, window, phase="posts"),
                succeeded_at=None,
                error=None,
            )

    @staticmethod
    def _service(db: Any, account: LegacyMomentsAccount, window: ImportWindow):
        user = db.scalar(
            select(User).where(
                User.id == account.owner_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
        )
        bound = db.scalar(
            select(ExternalAccount.id).where(
                ExternalAccount.id == account.external_account_id,
                ExternalAccount.user_id == account.owner_user_id,
                ExternalAccount.provider == LEGACY_PROVIDER,
                ExternalAccount.upstream_uid == account.upstream_uid,
            )
        )
        if user is None or bound is None:
            raise LegacyMomentsDataError("legacy_account_binding_changed")
        principal = SocialPrincipal(
            user_id=user.id,
            upstream_uid=account.upstream_uid,
            display_name=str(user.display_name or account.display_name or ""),
        )
        store = SqlAlchemyCanonicalSocialStore(db)
        service = LocalMomentsService(
            store,
            SqlAlchemySocialPermissionPolicy(db),
            clock=lambda: window.coverage_ended_at,
        )
        return principal, store, service

    def import_posts(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        posts: Sequence[FetchedPost],
        *,
        page: int,
    ) -> int:
        created = 0
        with self.db_scope() as db:
            principal, _store, service = self._service(db, account, window)
            for record in posts:
                if record.author_upstream_uid != account.upstream_uid:
                    raise LegacyMomentsDataError("legacy_owner_post_author_mismatch")
                mutation = service.import_legacy_post(
                    requested_by=principal,
                    item=LegacyPostInput(
                        provider=LEGACY_PROVIDER,
                        upstream_id=record.upstream_id,
                        author_user_id=account.owner_user_id,
                        author_upstream_uid=record.author_upstream_uid,
                        author_display_name=record.author_display_name,
                        title=record.title,
                        body=record.body,
                        media=record.media,
                        visibility=record.visibility,
                        comment_policy=record.comment_policy,
                        hide_comments=record.hide_comments,
                        source_created_at=record.source_created_at,
                        topics=record.topics,
                        author_snapshot=record.author_snapshot,
                        metadata=record.metadata,
                    ),
                    visibility_verified=False,
                )
                created += int(mutation.created)
            marker = self._base_marker(account, window, phase="posts")
            marker["last_post_page"] = page
            self._write_cursor(
                db,
                account,
                window,
                marker=marker,
                succeeded_at=None,
                error=None,
            )
        return created

    def list_imported_posts(
        self, account: LegacyMomentsAccount, window: ImportWindow
    ) -> Sequence[ImportedPostRef]:
        with self.db_scope() as db:
            rows = list(
                db.execute(
                    select(
                        SocialPost.public_id,
                        LegacySocialBinding.upstream_id,
                        SocialPost.author_upstream_uid,
                        SocialPost.hide_comments,
                    )
                    .join(
                        LegacySocialBinding,
                        LegacySocialBinding.local_entity_id == SocialPost.id,
                    )
                    .where(
                        LegacySocialBinding.provider == LEGACY_PROVIDER,
                        LegacySocialBinding.entity_type == "post",
                        SocialPost.author_user_id == account.owner_user_id,
                        SocialPost.source == "legacy-import",
                        SocialPost.source_created_at >= window.coverage_started_at,
                        SocialPost.source_created_at <= window.coverage_ended_at,
                    )
                    .order_by(SocialPost.source_created_at, SocialPost.id)
                )
            )
        return tuple(
            ImportedPostRef(
                local_public_id=str(row.public_id),
                upstream_id=str(row.upstream_id),
                author_upstream_uid=str(row.author_upstream_uid),
                hide_comments=bool(row.hide_comments),
            )
            for row in rows
        )

    def import_comments(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        post: ImportedPostRef,
        comments: Sequence[FetchedComment],
    ) -> int:
        created = 0
        with self.db_scope() as db:
            principal, store, service = self._service(db, account, window)
            post_view = store.get_post(post.upstream_id, for_update=True)
            if post_view is None or post_view.public_id != post.local_public_id:
                raise LegacyMomentsDataError("legacy_imported_post_binding_missing")
            author_uids = sorted(
                {record.author_upstream_uid for record in comments}
            )
            author_users = {
                str(uid): user_id
                for uid, user_id in db.execute(
                    select(ExternalAccount.upstream_uid, ExternalAccount.user_id)
                    .join(User, User.id == ExternalAccount.user_id)
                    .where(
                        ExternalAccount.provider == LEGACY_PROVIDER,
                        ExternalAccount.upstream_uid.in_(author_uids),
                        User.status == "active",
                        User.disabled_at.is_(None),
                    )
                )
            } if author_uids else {}

            pending = list(comments)
            while pending:
                remaining: list[FetchedComment] = []
                progressed = False
                for record in pending:
                    parent = (
                        store.get_comment(record.parent_upstream_id)
                        if record.parent_upstream_id
                        else None
                    )
                    if record.parent_upstream_id and parent is None:
                        remaining.append(record)
                        continue
                    mutation = service.import_legacy_comment(
                        requested_by=principal,
                        post=post_view,
                        parent=parent,
                        item=LegacyCommentInput(
                            provider=LEGACY_PROVIDER,
                            upstream_id=record.upstream_id,
                            post_upstream_id=record.post_upstream_id,
                            parent_upstream_id=record.parent_upstream_id,
                            author_user_id=author_users.get(
                                record.author_upstream_uid
                            ),
                            author_upstream_uid=record.author_upstream_uid,
                            author_display_name=record.author_display_name,
                            body=record.body,
                            status=record.status,
                            like_count=record.like_count,
                            reply_count=record.reply_count,
                            source_created_at=record.source_created_at,
                            author_snapshot=record.author_snapshot,
                            metadata=record.metadata,
                        ),
                    )
                    created += int(mutation.created)
                    progressed = True
                if remaining and not progressed:
                    raise LegacyMomentsDataError(
                        "legacy_comment_parent_unresolved"
                    )
                pending = remaining
        return created

    def progress(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        *,
        phase: str,
        post_pages: int,
        comment_pages: int,
        posts_seen: int,
        comments_seen: int,
    ) -> None:
        marker = self._base_marker(account, window, phase=phase)
        marker.update(
            {
                "post_pages": post_pages,
                "comment_pages": comment_pages,
                "posts_seen": posts_seen,
                "comments_seen": comments_seen,
            }
        )
        with self.db_scope() as db:
            self._write_cursor(
                db,
                account,
                window,
                marker=marker,
                succeeded_at=None,
                error=None,
            )

    def complete(
        self, account: LegacyMomentsAccount, window: ImportWindow
    ) -> tuple[dict[str, int], str]:
        with self.db_scope() as db:
            post_rows = list(
                db.execute(
                    select(
                        SocialPost.id,
                        LegacySocialBinding.upstream_id,
                        LegacySocialBinding.payload_digest,
                    )
                    .join(
                        LegacySocialBinding,
                        LegacySocialBinding.local_entity_id == SocialPost.id,
                    )
                    .where(
                        LegacySocialBinding.provider == LEGACY_PROVIDER,
                        LegacySocialBinding.entity_type == "post",
                        SocialPost.author_user_id == account.owner_user_id,
                        SocialPost.source == "legacy-import",
                        SocialPost.source_created_at >= window.coverage_started_at,
                        SocialPost.source_created_at <= window.coverage_ended_at,
                    )
                    .order_by(LegacySocialBinding.upstream_id)
                )
            )
            post_ids = [row.id for row in post_rows]
            comment_rows = (
                list(
                    db.execute(
                        select(
                            LegacySocialBinding.upstream_id,
                            LegacySocialBinding.payload_digest,
                        )
                        .join(
                            SocialComment,
                            SocialComment.id
                            == LegacySocialBinding.local_entity_id,
                        )
                        .where(
                            LegacySocialBinding.provider == LEGACY_PROVIDER,
                            LegacySocialBinding.entity_type == "comment",
                            SocialComment.post_id.in_(post_ids),
                            SocialComment.source == "legacy-import",
                            SocialComment.source_created_at
                            >= window.coverage_started_at,
                            SocialComment.source_created_at
                            <= window.coverage_ended_at,
                        )
                        .order_by(LegacySocialBinding.upstream_id)
                    )
                )
                if post_ids
                else []
            )
            topic_rows = (
                list(
                    db.execute(
                        select(SocialTopic.normalized_name)
                        .join(
                            SocialPostTopic,
                            SocialPostTopic.topic_id == SocialTopic.id,
                        )
                        .where(SocialPostTopic.post_id.in_(post_ids))
                        .distinct()
                        .order_by(SocialTopic.normalized_name)
                    )
                )
                if post_ids
                else []
            )
            identities = [
                f"post:{row.upstream_id}:{row.payload_digest}" for row in post_rows
            ]
            identities.extend(
                f"comment:{row.upstream_id}:{row.payload_digest}"
                for row in comment_rows
            )
            identities.extend(f"topic:{row.normalized_name}" for row in topic_rows)
            digest = hashlib.sha256(
                json.dumps(
                    identities,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            counts = {
                "posts": len(post_rows),
                "comments": len(comment_rows),
                "topics": len(topic_rows),
            }
            marker = self._base_marker(
                account, window, phase="complete", complete=True
            )
            marker.update(
                {
                    "scopes": list(DOMAIN_MARKER_SCOPES["moments"]),
                    "counts": counts,
                    "source_paths": list(MOMENTS_SOURCE_PATHS),
                    "record_digest": digest,
                    "unresolved_records": 0,
                }
            )
            completed_at = _as_utc(self.clock())
            self._write_cursor(
                db,
                account,
                window,
                marker=marker,
                succeeded_at=completed_at,
                error=None,
            )
        return counts, digest

    def fail(
        self,
        account: LegacyMomentsAccount,
        window: ImportWindow,
        *,
        code: str,
    ) -> None:
        marker = self._base_marker(account, window, phase="failed")
        marker["failure_code"] = str(code or "legacy_moments_failure")[:160]
        with self.db_scope() as db:
            self._write_cursor(
                db,
                account,
                window,
                marker=marker,
                succeeded_at=None,
                error=marker["failure_code"],
            )


class LegacyMomentsImportOrchestrator:
    def __init__(
        self,
        reader: LegacyMomentsReader,
        writer: LegacyMomentsWriter,
        *,
        clock=utcnow,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.clock = clock

    def run(self, account: LegacyMomentsAccount) -> ImportSummary:
        ended_at = _as_utc(self.clock())
        window = ImportWindow(
            coverage_started_at=ended_at - timedelta(days=MOMENTS_HISTORY_DAYS),
            coverage_ended_at=ended_at,
        )
        post_pages = 0
        comment_pages = 0
        posts_seen = 0
        posts_created = 0
        comments_seen = 0
        comments_created = 0
        try:
            self.writer.begin(account, window)
            seen_post_ids: dict[str, FetchedPost] = {}
            page_signatures: set[str] = set()
            for page in range(1, MAX_POST_PAGES + 1):
                raw_items = tuple(self.reader.fetch_post_page(page))
                if not raw_items:
                    break
                signature = _page_signature(raw_items)
                if signature in page_signatures:
                    raise LegacyMomentsDataError("legacy_post_page_repeated")
                page_signatures.add(signature)
                post_pages += 1
                in_window: list[FetchedPost] = []
                for raw in raw_items:
                    record = _post_record(raw, reference=ended_at)
                    if record.source_created_at < window.coverage_started_at:
                        continue
                    if record.source_created_at > window.coverage_ended_at:
                        continue
                    previous = seen_post_ids.get(record.upstream_id)
                    if previous is not None:
                        if previous != record:
                            raise LegacyMomentsDataError(
                                "legacy_post_duplicate_conflict"
                            )
                        continue
                    seen_post_ids[record.upstream_id] = record
                    in_window.append(record)
                    posts_seen += 1
                    if posts_seen > MAX_POSTS:
                        raise LegacyMomentsLimitError("legacy_post_limit_exceeded")
                if in_window:
                    posts_created += self.writer.import_posts(
                        account, window, in_window, page=page
                    )
            else:
                raise LegacyMomentsLimitError("legacy_post_page_limit_exceeded")

            imported_posts = tuple(
                self.writer.list_imported_posts(account, window)
            )
            self.writer.progress(
                account,
                window,
                phase="comments",
                post_pages=post_pages,
                comment_pages=comment_pages,
                posts_seen=posts_seen,
                comments_seen=comments_seen,
            )
            for post in imported_posts:
                seen_comments: dict[str, FetchedComment] = {}
                signatures: set[str] = set()
                for page in range(1, MAX_COMMENT_PAGES + 1):
                    raw_items = tuple(
                        self.reader.fetch_comment_page(
                            post_upstream_id=post.upstream_id,
                            author_upstream_uid=post.author_upstream_uid,
                            hide_comments=post.hide_comments,
                            page=page,
                        )
                    )
                    if not raw_items:
                        break
                    signature = _page_signature(raw_items)
                    if signature in signatures:
                        raise LegacyMomentsDataError(
                            "legacy_comment_page_repeated"
                        )
                    signatures.add(signature)
                    comment_pages += 1
                    for raw in raw_items:
                        record = _comment_record(
                            raw,
                            post_upstream_id=post.upstream_id,
                            reference=ended_at,
                        )
                        if record.source_created_at < window.coverage_started_at:
                            continue
                        if record.source_created_at > window.coverage_ended_at:
                            continue
                        previous = seen_comments.get(record.upstream_id)
                        if previous is not None:
                            if previous != record:
                                raise LegacyMomentsDataError(
                                    "legacy_comment_duplicate_conflict"
                                )
                            continue
                        seen_comments[record.upstream_id] = record
                        comments_seen += 1
                        if len(seen_comments) > MAX_COMMENTS_PER_POST:
                            raise LegacyMomentsLimitError(
                                "legacy_comment_limit_exceeded"
                            )
                else:
                    raise LegacyMomentsLimitError(
                        "legacy_comment_page_limit_exceeded"
                    )
                if seen_comments:
                    comments_created += self.writer.import_comments(
                        account,
                        window,
                        post,
                        tuple(seen_comments.values()),
                    )
                self.writer.progress(
                    account,
                    window,
                    phase="comments",
                    post_pages=post_pages,
                    comment_pages=comment_pages,
                    posts_seen=posts_seen,
                    comments_seen=comments_seen,
                )
            counts, digest = self.writer.complete(account, window)
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, LegacyMomentsMigrationError)
                else f"legacy_moments_exception.{type(exc).__name__}"
            )
            try:
                self.writer.fail(account, window, code=code)
            except Exception:
                # Keep the original migration cause.  Failure recording is
                # best-effort and never creates a completion Marker.
                pass
            raise
        return ImportSummary(
            post_pages=post_pages,
            comment_pages=comment_pages,
            posts_seen=posts_seen,
            posts_created=posts_created,
            comments_seen=comments_seen,
            comments_created=comments_created,
            counts=counts,
            record_digest=digest,
        )


def _load_account(db: Any, owner_user_id: uuid.UUID) -> LegacyMomentsAccount:
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
        raise LegacyMomentsDataError("legacy_account_binding_missing")
    user, account = row
    upstream_uid = _identifier(account.upstream_uid, limit=128)
    if not upstream_uid:
        raise LegacyMomentsDataError("legacy_upstream_uid_missing")
    if not isinstance(account.login_account_encrypted, Mapping):
        raise LegacyMomentsDataError("legacy_login_credential_missing")
    if account.password_encrypted is not None and not isinstance(
        account.password_encrypted, Mapping
    ):
        raise LegacyMomentsDataError("legacy_password_credential_invalid")
    if account.token_encrypted is not None and not isinstance(
        account.token_encrypted, Mapping
    ):
        raise LegacyMomentsDataError("legacy_token_credential_invalid")
    return LegacyMomentsAccount(
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
    account: LegacyMomentsAccount,
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


def _credential_context(account: LegacyMomentsAccount, field: str) -> str:
    return f"external-account:{account.external_account_id}:{field}"


def _decrypt_required_credential(
    cipher: CredentialCipher,
    encrypted: Mapping[str, Any] | None,
    *,
    account: LegacyMomentsAccount,
    field: str,
    reject_zero: bool = True,
) -> str:
    if not isinstance(encrypted, Mapping):
        raise LegacyMomentsDataError(
            f"legacy_{field}_credential_missing"
        )
    try:
        value = cipher.decrypt_text(
            dict(encrypted),
            purpose=f"external-account.{field}",
            context=_credential_context(account, field),
        )
    except Exception as exc:
        raise LegacyMomentsDataError(
            f"legacy_{field}_credential_decryption_failed"
        ) from exc
    normalized = str(value or "")
    if not normalized or not normalized.strip() or (
        reject_zero and normalized.strip() == "0"
    ):
        raise LegacyMomentsDataError(f"legacy_{field}_credential_empty")
    return normalized


def _session_refresh_state(session: Any, *, authenticated_at: datetime) -> RefreshedProviderState:
    token = str(getattr(session, "token", "") or "").strip()
    if (
        not token
        or token == "0"
        or len(token) > 8192
        or any(ord(character) < 33 for character in token)
    ):
        raise LegacyMomentsDataError("legacy_reauthenticated_token_invalid")
    device_data: dict[str, Any] = {}
    device_dict = getattr(session, "device_dict", None)
    if callable(device_dict):
        try:
            values = device_dict()
        except Exception as exc:
            raise LegacyMomentsDataError(
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
                    raise LegacyMomentsDataError(
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
                raise LegacyMomentsDataError(
                    "legacy_reauthenticated_account_state_invalid"
                )
            device_data[field] = text
    return RefreshedProviderState(
        token=token,
        authenticated_at=_as_utc(authenticated_at),
        device_data=device_data,
    )


def _authentication_result_error(result: Any) -> LegacyMomentsMigrationError:
    try:
        status = int(getattr(result, "status", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    if status < 0 or 500 <= status < 600:
        return LegacyMomentsProviderError("legacy_password_auth_provider_unavailable")
    return LegacyMomentsAuthenticationRejected(
        "legacy_password_authentication_rejected"
    )


def _runtime(
    account: LegacyMomentsAccount,
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
    if not login or len(login) > 512 or any(ord(character) < 32 for character in login):
        raise LegacyMomentsDataError("legacy_login_credential_invalid")

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
            # A corrupt/stale token is recoverable only through the separately
            # encrypted password.  Never accept it as an authenticated state.
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
            raise LegacyMomentsProviderError(
                "legacy_runtime_restore_provider_unavailable"
            ) from exc
        except ProviderAuthenticationRejected as exc:
            raise LegacyMomentsAuthenticationRejected(
                "legacy_runtime_restore_rejected"
            ) from exc
        except Exception as exc:
            raise LegacyMomentsAuthenticationError(
                "legacy_runtime_restore_failed"
            ) from exc
        session = getattr(getattr(runtime, "app", None), "session", None)
        if bool(getattr(session, "logged_in", False)):
            restored_uid = _identifier(getattr(session, "uid", ""), limit=128)
            if restored_uid != account.upstream_uid:
                _close_runtime(runtime)
                raise LegacyMomentsDataError("legacy_runtime_uid_mismatch")
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
        raise LegacyMomentsProviderError(
            "legacy_password_runtime_provider_unavailable"
        ) from exc
    except ProviderAuthenticationRejected as exc:
        raise LegacyMomentsAuthenticationRejected(
            "legacy_password_runtime_rejected"
        ) from exc
    except Exception as exc:
        raise LegacyMomentsAuthenticationError(
            "legacy_password_runtime_create_failed"
        ) from exc

    session = getattr(getattr(runtime, "app", None), "session", None)
    try:
        result = runtime.app.auth.login_password(login, password)
    except ProviderUnavailable as exc:
        _close_runtime(runtime)
        raise LegacyMomentsProviderError(
            "legacy_password_auth_provider_unavailable"
        ) from exc
    except ProviderAuthenticationRejected as exc:
        _close_runtime(runtime)
        raise LegacyMomentsAuthenticationRejected(
            "legacy_password_authentication_rejected"
        ) from exc
    except Exception as exc:
        _close_runtime(runtime)
        raise LegacyMomentsAuthenticationError(
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
    authenticated_uid = _identifier(getattr(session, "uid", ""), limit=128)
    if authenticated_uid != account.upstream_uid:
        _close_runtime(runtime)
        raise LegacyMomentsDataError("legacy_reauthenticated_uid_mismatch")
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
    account: LegacyMomentsAccount,
    refreshed: RefreshedProviderState,
    *,
    cipher: CredentialCipher,
    db_scope: DbScope,
) -> None:
    """Persist successful network reauthentication in one short transaction."""

    try:
        encrypted_token = cipher.encrypt_text(
            refreshed.token,
            purpose="external-account.token",
            context=_credential_context(account, "token"),
        )
    except Exception as exc:
        raise LegacyMomentsDataError(
            "legacy_reauthenticated_token_encryption_failed"
        ) from exc
    if not isinstance(encrypted_token, Mapping):
        raise LegacyMomentsDataError(
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
            raise LegacyMomentsDataError(
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


def _list_active_owner_ids(db: Any) -> tuple[uuid.UUID, ...]:
    return tuple(
        db.scalars(
            select(User.id)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == LEGACY_PROVIDER,
            )
            .order_by(User.id)
        )
    )


def run_legacy_moments_import(
    owner_user_id: uuid.UUID | str,
    *,
    settings: Settings | None = None,
    runtime_provider: RuntimeProvider | None = None,
    cipher: CredentialCipher | None = None,
    db_scope: DbScope = session_scope,
    clock=utcnow,
) -> ImportSummary:
    """Run one account import; safe to retry after any provider/local failure."""

    try:
        owner_id = (
            owner_user_id
            if isinstance(owner_user_id, uuid.UUID)
            else uuid.UUID(str(owner_user_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise LegacyMomentsDataError("owner_user_id_invalid") from exc
    with db_scope() as db:
        account = _load_account(db, owner_id)
    writer = SqlAlchemyLegacyMomentsWriter(db_scope=db_scope, clock=clock)
    now = _as_utc(clock())
    window = ImportWindow(
        coverage_started_at=now - timedelta(days=MOMENTS_HISTORY_DAYS),
        coverage_ended_at=now,
    )
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
            now=now,
        )
        runtime = resolved.runtime
        if resolved.refreshed is not None:
            _persist_refreshed_provider_state(
                account,
                resolved.refreshed,
                cipher=resolved_cipher,
                db_scope=db_scope,
            )
        reader = BanghuaMomentsReader(
            runtime.app.social, upstream_uid=account.upstream_uid
        )
        orchestrator_started = True
        return LegacyMomentsImportOrchestrator(
            reader, writer, clock=lambda: now
        ).run(account)
    except Exception as exc:
        # The orchestrator records all failures after it takes control. Runtime
        # restore, password authentication and token persistence happen before
        # that point, so persist the same non-secret gate here as well.
        if not orchestrator_started:
            code = (
                exc.code
                if isinstance(exc, LegacyMomentsMigrationError)
                else f"legacy_moments_exception.{type(exc).__name__}"
            )
            try:
                writer.fail(account, window, code=code)
            except Exception:
                pass
        raise
    finally:
        if runtime is not None:
            _close_runtime(runtime)


def run_all_active_legacy_moments_imports(
    *,
    settings: Settings | None = None,
    runtime_provider: RuntimeProvider | None = None,
    cipher: CredentialCipher | None = None,
    db_scope: DbScope = session_scope,
    clock=utcnow,
) -> BatchImportSummary:
    """Migrate every active legacy binding without exposing account identity."""

    resolved_settings = settings or get_settings()
    resolved_cipher = cipher or CredentialCipher.from_settings(resolved_settings)
    if runtime_provider is None:
        from bbw_web.providers import LegacyBanghuaProvider

        runtime_provider = LegacyBanghuaProvider()
    with db_scope() as db:
        owner_ids = _list_active_owner_ids(db)

    totals = {
        "accounts_succeeded": 0,
        "authentication_rejected": 0,
        "authentication_failed": 0,
        "provider_unavailable": 0,
        "data_invalid": 0,
        "limit_exceeded": 0,
        "internal_errors": 0,
        "post_pages": 0,
        "comment_pages": 0,
        "posts_seen": 0,
        "posts_created": 0,
        "comments_seen": 0,
        "comments_created": 0,
        "posts": 0,
        "comments": 0,
        "topics": 0,
    }
    for owner_id in owner_ids:
        try:
            summary = run_legacy_moments_import(
                owner_id,
                settings=resolved_settings,
                runtime_provider=runtime_provider,
                cipher=resolved_cipher,
                db_scope=db_scope,
                clock=clock,
            )
        except LegacyMomentsAuthenticationRejected:
            totals["authentication_rejected"] += 1
            continue
        except LegacyMomentsAuthenticationError:
            totals["authentication_failed"] += 1
            continue
        except LegacyMomentsProviderError:
            totals["provider_unavailable"] += 1
            continue
        except LegacyMomentsLimitError:
            totals["limit_exceeded"] += 1
            continue
        except LegacyMomentsDataError:
            totals["data_invalid"] += 1
            continue
        except LegacyMomentsMigrationError:
            totals["internal_errors"] += 1
            continue
        except Exception:
            totals["internal_errors"] += 1
            continue

        totals["accounts_succeeded"] += 1
        for field in (
            "post_pages",
            "comment_pages",
            "posts_seen",
            "posts_created",
            "comments_seen",
            "comments_created",
        ):
            totals[field] += int(getattr(summary, field))
        for scope in ("posts", "comments", "topics"):
            totals[scope] += int(summary.counts.get(scope, 0) or 0)

    accounts_failed = len(owner_ids) - totals["accounts_succeeded"]
    return BatchImportSummary(
        accounts_total=len(owner_ids),
        accounts_succeeded=totals["accounts_succeeded"],
        accounts_failed=accounts_failed,
        authentication_rejected=totals["authentication_rejected"],
        authentication_failed=totals["authentication_failed"],
        provider_unavailable=totals["provider_unavailable"],
        data_invalid=totals["data_invalid"],
        limit_exceeded=totals["limit_exceeded"],
        internal_errors=totals["internal_errors"],
        post_pages=totals["post_pages"],
        comment_pages=totals["comment_pages"],
        posts_seen=totals["posts_seen"],
        posts_created=totals["posts_created"],
        comments_seen=totals["comments_seen"],
        comments_created=totals["comments_created"],
        posts=totals["posts"],
        comments=totals["comments"],
        topics=totals["topics"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="可恢复迁移一个或全部 active 账号最近 180 天历史动态"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--owner-user-id",
        help="经审核的内部 User UUID；不会出现在命令输出中",
    )
    target.add_argument(
        "--all-active",
        action="store_true",
        help="迁移全部 active legacy 账号；仅输出聚合计数",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.all_active:
            batch = run_all_active_legacy_moments_imports()
            payload = asdict(batch)
            ok = batch.accounts_failed == 0
            print(
                json.dumps(
                    {"ok": ok, **payload},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stdout or sys.stdout,
            )
            return 0 if ok else 1
        summary = run_legacy_moments_import(args.owner_user_id)
    except LegacyMomentsMigrationError as exc:
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
        # CLI output is an operator-facing contract.  Do not let an unexpected
        # adapter/database exception print a traceback that could contain a
        # provider response, signed URL, account identifier or credential
        # context.  The detailed exception remains available to the normal
        # application logging/observability path when this entry point is
        # wrapped by production operations.
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "legacy_moments_internal_error",
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
        json.dumps(
            {"ok": True, **payload},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=stdout or sys.stdout,
    )
    return 0


__all__ = [
    "BanghuaMomentsReader",
    "BatchImportSummary",
    "FetchedComment",
    "FetchedPost",
    "ImportSummary",
    "ImportWindow",
    "ImportedPostRef",
    "LegacyMomentsAccount",
    "LegacyMomentsAuthenticationError",
    "LegacyMomentsAuthenticationRejected",
    "LegacyMomentsDataError",
    "LegacyMomentsImportOrchestrator",
    "LegacyMomentsLimitError",
    "LegacyMomentsMigrationError",
    "LegacyMomentsProviderError",
    "SqlAlchemyLegacyMomentsWriter",
    "run_all_active_legacy_moments_imports",
    "run_legacy_moments_import",
]


if __name__ == "__main__":
    raise SystemExit(main())
