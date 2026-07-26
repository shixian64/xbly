"""Best-effort Banghua mirrors for Web-local social mutations.

PostgreSQL is the authority for every operation consumed here.  This worker
only mirrors an already committed local mutation to the legacy provider; a
provider, credential, mapping, or protocol failure can therefore only change
the outbox row and must never compensate the canonical local write.

The module intentionally does not register a scheduler or import
``bbw_web.jobs``.  ``dispatch_due`` is the small entry point that a scheduler
can call without coupling request handlers to Banghua.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ContextManager

from sqlalchemy import and_, or_, select

from bbw_prod.compatibility import (
    CompatibilityMode,
    compatibility_mode,
)
from bbw_prod.config import Settings, get_settings
from bbw_prod.crypto import CredentialCipher
from bbw_prod.db import session_scope
from bbw_prod.models import (
    LegacySocialBinding,
    OperationOutbox,
    SocialComment,
    SocialPost,
    SocialTopic,
    utcnow,
)
from bbw_prod.repositories import ExternalAccountRepository
from bbw_web.providers import ProviderSessionState, RuntimeProvider


COMPATIBILITY_PREFIX = "compatibility."
LEGACY_PROVIDER = "beibeiwu"
EXCLUDED_OPERATION_TYPES = frozenset(
    {
        "media.archive",
        "compatibility.media.archive",
    }
)
CLAIMABLE_STATUSES = frozenset({"pending", "retry"})
DEFAULT_LEASE_SECONDS = 120
DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
MAX_BACKOFF_SECONDS = 3600
MAX_ERROR_LENGTH = 512

# Every write currently emitted by native_social_api and LocalMomentsService is
# listed here.  Some are intentionally recognized-but-terminal because the APK
# API only exposes a non-idempotent toggle or no equivalent mutation at all.
RECOGNIZED_OPERATION_TYPES = frozenset(
    {
        "compatibility.profile.nick",
        "compatibility.profile.reset",
        "compatibility.profile.privacy",
        "compatibility.social.follow",
        "compatibility.social.unfollow",
        "compatibility.social.add-friend",
        "compatibility.social.agree-friend",
        "compatibility.social.reject-friend",
        "compatibility.social.cancel-friend",
        "compatibility.social.delete-friend",
        "compatibility.social.blacklist-add",
        "compatibility.social.blacklist-del",
        "compatibility.social.visit",
        "compatibility.social.topic.create",
        "compatibility.social.post.publish",
        "compatibility.social.post.delete",
        "compatibility.social.post.visibility",
        "compatibility.social.post.pin",
        "compatibility.social.post.comment-policy",
        "compatibility.social.comment.publish",
        "compatibility.social.comment.delete",
        "compatibility.social.comment.moderate",
        "compatibility.social.reaction.like",
        "compatibility.social.report.create",
        "compatibility.social.post.view",
    }
)

DIRECTLY_MIRRORED_OPERATION_TYPES = frozenset(
    {
        "compatibility.profile.nick",
        "compatibility.profile.reset",
        "compatibility.profile.privacy",
        "compatibility.social.follow",
        "compatibility.social.unfollow",
        "compatibility.social.add-friend",
        "compatibility.social.agree-friend",
        "compatibility.social.delete-friend",
        "compatibility.social.blacklist-add",
        "compatibility.social.blacklist-del",
        "compatibility.social.visit",
        "compatibility.social.topic.create",
        "compatibility.social.post.publish",
        "compatibility.social.post.delete",
        "compatibility.social.post.visibility",
        "compatibility.social.comment.publish",
        "compatibility.social.comment.delete",
        "compatibility.social.comment.moderate",
        "compatibility.social.report.create",
        "compatibility.social.post.view",
    }
)

UNSAFE_OR_UNMAPPABLE_OPERATION_TYPES = frozenset(
    RECOGNIZED_OPERATION_TYPES - DIRECTLY_MIRRORED_OPERATION_TYPES
)

_SAFE_CODE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_LOCAL_PREFIXES = {
    "post": "pst_",
    "comment": "cmt_",
    "topic": "tpc_",
}
_ENTITY_MODELS = {
    "post": SocialPost,
    "comment": SocialComment,
    "topic": SocialTopic,
}
_VISIBILITY_SCOPE = {
    "public": "公开",
    "followers": "好友及粉丝可见",
    "private": "仅自己可见",
}
_PROFILE_RESET_TYPE = {
    "signature": "个性签名设置",
    "city": "城市设置",
    "gender": "性别设置",
}
_GENDER_VALUE = {
    "male": "男",
    "female": "女",
    "other": "其他",
    "unspecified": "保密",
}


class CompatibilityDispatchError(RuntimeError):
    """Stable, non-secret failure suitable for an outbox status."""

    retryable = False

    def __init__(self, code: str) -> None:
        self.code = _stable_code(code)
        super().__init__(self.code)


class RetryableCompatibilityError(CompatibilityDispatchError):
    retryable = True


class PermanentCompatibilityError(CompatibilityDispatchError):
    retryable = False


@dataclass(frozen=True, slots=True)
class ClaimedOperation:
    id: uuid.UUID
    owner_user_id: uuid.UUID
    operation_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    locked_by: str


@dataclass(frozen=True, slots=True)
class RuntimeAccountState:
    account_id: uuid.UUID
    upstream_uid: str
    login_encrypted: dict[str, Any]
    token_encrypted: dict[str, Any]
    token_expires_at: datetime | None
    display_name: str
    profile: dict[str, Any]
    device_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BindingTarget:
    entity_type: str
    local_public_id: str


@dataclass(frozen=True, slots=True)
class CompatibilityCall:
    component: str
    method: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] | None = None
    accept_empty: bool = False
    replay_safe: bool = True
    binding: BindingTarget | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    upstream_id_missing: bool = False


DbScope = Callable[[], ContextManager[Any]]
Clock = Callable[[], datetime]


def _stable_code(value: Any) -> str:
    normalized = _SAFE_CODE.sub("_", str(value or "compatibility_failure")).strip("_")
    return (normalized or "compatibility_failure")[:MAX_ERROR_LENGTH]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _claim_statement(*, now: datetime, limit: int):
    unlocked = or_(
        OperationOutbox.locked_until.is_(None),
        OperationOutbox.locked_until <= now,
    )
    due = and_(
        OperationOutbox.status.in_(tuple(CLAIMABLE_STATUSES)),
        OperationOutbox.available_at <= now,
        unlocked,
    )
    abandoned = and_(
        OperationOutbox.status == "processing",
        unlocked,
    )
    return (
        select(OperationOutbox)
        .where(
            OperationOutbox.operation_type.like(f"{COMPATIBILITY_PREFIX}%"),
            OperationOutbox.operation_type.notin_(tuple(EXCLUDED_OPERATION_TYPES)),
            OperationOutbox.operation_type.not_like("compatibility.media.archive%"),
            or_(due, abandoned),
        )
        .order_by(OperationOutbox.available_at, OperationOutbox.created_at, OperationOutbox.id)
        .with_for_update(skip_locked=True)
        .limit(max(1, min(int(limit), MAX_BATCH_SIZE)))
    )


def _claim_due(
    *,
    db_scope: DbScope,
    worker_id: str,
    limit: int,
    lease_seconds: int,
    now: datetime,
) -> list[ClaimedOperation]:
    lease_until = now + timedelta(seconds=max(30, min(int(lease_seconds), 1800)))
    with db_scope() as db:
        rows = list(db.scalars(_claim_statement(now=now, limit=limit)))
        claimed: list[ClaimedOperation] = []
        for row in rows:
            if int(row.attempt_count or 0) >= int(row.max_attempts or 1):
                row.status = "failed"
                row.completed_at = now
                row.locked_by = None
                row.locked_until = None
                row.last_error = row.last_error or "compatibility_retry_budget_exhausted"
                continue
            row.status = "processing"
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.locked_by = worker_id
            row.locked_until = lease_until
            row.last_error = None
            claimed.append(
                ClaimedOperation(
                    id=row.id,
                    owner_user_id=row.owner_user_id,
                    operation_type=str(row.operation_type or ""),
                    aggregate_type=str(row.aggregate_type or ""),
                    aggregate_id=str(row.aggregate_id or ""),
                    payload=dict(row.payload or {}),
                    attempt_count=int(row.attempt_count),
                    max_attempts=int(row.max_attempts),
                    locked_by=worker_id,
                )
            )
        db.flush()
        return claimed


def _retry_delay(operation_id: uuid.UUID, attempt: int) -> timedelta:
    base = min(MAX_BACKOFF_SECONDS, 15 * (2 ** min(max(0, int(attempt) - 1), 8)))
    jitter = int(hashlib.sha256(operation_id.bytes).hexdigest()[:4], 16) % max(1, base // 5)
    return timedelta(seconds=min(MAX_BACKOFF_SECONDS, base + jitter))


def _finalize_claim(
    claim: ClaimedOperation,
    *,
    db_scope: DbScope,
    now: datetime,
    error: CompatibilityDispatchError | None,
) -> str:
    with db_scope() as db:
        row = db.scalar(
            select(OperationOutbox)
            .where(
                OperationOutbox.id == claim.id,
                OperationOutbox.status == "processing",
                OperationOutbox.locked_by == claim.locked_by,
            )
            .with_for_update()
        )
        if row is None:
            return "stale"
        row.locked_by = None
        row.locked_until = None
        if error is None:
            row.status = "completed"
            row.completed_at = now
            row.last_error = None
            return "completed"

        exhausted = int(row.attempt_count or 0) >= int(row.max_attempts or 1)
        if not error.retryable or exhausted:
            row.status = "failed"
            row.completed_at = now
            row.last_error = error.code
            return "failed"

        row.status = "retry"
        row.completed_at = None
        row.available_at = now + _retry_delay(row.id, int(row.attempt_count or 1))
        row.last_error = error.code
        return "retried"


def _mapping(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PermanentCompatibilityError(code)
    return {str(key): item for key, item in value.items()}


def _nonempty(value: Any, *, limit: int, code: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit:
        raise PermanentCompatibilityError(code)
    return normalized


def _body(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("body")
    return _mapping(raw if raw is not None else {}, code="invalid_social_body")


def _moment_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(payload.get("payload"), code="invalid_moments_payload")


def _target_uid(payload: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    for value in (
        payload.get("target_uid"),
        body.get("uid"),
        body.get("you"),
        body.get("target_uid"),
        body.get("yourid"),
        body.get("to"),
    ):
        normalized = str(value or "").strip()
        if normalized and len(normalized) <= 128:
            return normalized
    raise PermanentCompatibilityError("target_uid_missing")


def _provider_params(body: Mapping[str, Any], *, target_uid: str = "") -> dict[str, Any]:
    params: dict[str, Any] = {}
    nested = body.get("params")
    if isinstance(nested, Mapping):
        params.update({str(key): value for key, value in nested.items() if value is not None})
    denied = {
        "action",
        "mode",
        "params",
        "operation_id",
        "idempotency_key",
        "password",
        "token",
        "authorization",
    }
    params.update(
        {
            str(key): value
            for key, value in body.items()
            if str(key).lower() not in denied and value is not None
        }
    )
    if target_uid and not any(params.get(key) for key in ("uid", "you", "yourid", "target_uid")):
        params["uid"] = target_uid
    return params


def _has_media(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return any(_has_media(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_media(item) for item in value)
    return bool(str(value).strip())


class CompatibilityResolver:
    """Resolve canonical IDs without ever treating a local ID as an APK ID."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def require_upstream_id(self, entity_type: str, public_id: Any) -> str:
        normalized = _nonempty(
            public_id,
            limit=256,
            code=f"{entity_type}_id_missing",
        )
        prefix = _LOCAL_PREFIXES[entity_type]
        if not normalized.startswith(prefix):
            return normalized
        binding = self.db.scalar(
            select(LegacySocialBinding).where(
                LegacySocialBinding.provider == LEGACY_PROVIDER,
                LegacySocialBinding.entity_type == entity_type,
                LegacySocialBinding.local_public_id == normalized,
            )
        )
        if binding is None or not str(binding.upstream_id or "").strip():
            raise RetryableCompatibilityError(f"{entity_type}_upstream_binding_missing")
        return str(binding.upstream_id).strip()

    def post(self, public_id: Any) -> SocialPost:
        normalized = _nonempty(public_id, limit=40, code="post_id_missing")
        row = self.db.scalar(select(SocialPost).where(SocialPost.public_id == normalized))
        if row is None:
            raise PermanentCompatibilityError("local_post_missing")
        return row

    def comment(self, public_id: Any) -> SocialComment:
        normalized = _nonempty(public_id, limit=40, code="comment_id_missing")
        row = self.db.scalar(
            select(SocialComment).where(SocialComment.public_id == normalized)
        )
        if row is None:
            raise PermanentCompatibilityError("local_comment_missing")
        return row

    def bind_upstream_id(
        self,
        *,
        target: BindingTarget,
        upstream_id: str,
        owner_user_id: uuid.UUID,
        now: datetime,
    ) -> bool:
        entity_type = target.entity_type
        model = _ENTITY_MODELS[entity_type]
        local = self.db.scalar(
            select(model).where(model.public_id == target.local_public_id)
        )
        if local is None:
            raise PermanentCompatibilityError(f"local_{entity_type}_missing_after_mirror")

        existing_local = self.db.scalar(
            select(LegacySocialBinding).where(
                LegacySocialBinding.provider == LEGACY_PROVIDER,
                LegacySocialBinding.entity_type == entity_type,
                LegacySocialBinding.local_entity_id == local.id,
            )
        )
        if existing_local is not None:
            if str(existing_local.upstream_id) != upstream_id:
                raise PermanentCompatibilityError("upstream_binding_conflict")
            return False

        existing_upstream = self.db.scalar(
            select(LegacySocialBinding).where(
                LegacySocialBinding.provider == LEGACY_PROVIDER,
                LegacySocialBinding.entity_type == entity_type,
                LegacySocialBinding.upstream_id == upstream_id,
            )
        )
        if existing_upstream is not None:
            if existing_upstream.local_entity_id != local.id:
                raise PermanentCompatibilityError("upstream_binding_conflict")
            return False

        digest = str(getattr(local, "payload_digest", "") or "")
        if len(digest) != 64:
            digest = hashlib.sha256(
                f"{entity_type}\n{local.id}\n{target.local_public_id}\n{upstream_id}".encode(
                    "utf-8"
                )
            ).hexdigest()
        source_created_at = (
            getattr(local, "source_created_at", None)
            or getattr(local, "created_at", None)
            or now
        )
        self.db.add(
            LegacySocialBinding(
                provider=LEGACY_PROVIDER,
                entity_type=entity_type,
                upstream_id=upstream_id,
                local_entity_id=local.id,
                local_public_id=target.local_public_id,
                imported_by_user_id=owner_user_id,
                import_scope="owner",
                source_created_at=_as_utc(source_created_at),
                payload_digest=digest,
                extra_data={
                    "authority": "web-local",
                    "compatibility_mirror": True,
                    "schema": 1,
                },
                created_at=now,
                updated_at=now,
            )
        )
        self.db.flush()
        return True


def _prepare_social_call(
    claim: ClaimedOperation,
    resolver: CompatibilityResolver,
) -> CompatibilityCall:
    operation = claim.operation_type
    payload = claim.payload
    body = _body(payload)

    if operation == "compatibility.profile.nick":
        value = _nonempty(body.get("value") or body.get("name") or body.get("nickname"), limit=160, code="nickname_missing")
        return CompatibilityCall("profile", "reset_nickname", (value,))

    if operation == "compatibility.profile.reset":
        field = str(body.get("field") or "").strip().lower()
        value = str(body.get("value") or "").strip()
        if field == "nickname":
            value = _nonempty(value, limit=160, code="nickname_missing")
            return CompatibilityCall("profile", "reset_nickname", (value,))
        if field == "avatar":
            raise PermanentCompatibilityError("profile_avatar_upload_not_mappable")
        type_name = _PROFILE_RESET_TYPE.get(field)
        if not type_name:
            raise PermanentCompatibilityError("profile_field_not_mappable")
        if field == "gender":
            value = _GENDER_VALUE.get(value.lower(), value)
        if len(value) > 1000:
            raise PermanentCompatibilityError("profile_value_too_long")
        return CompatibilityCall(
            "profile",
            "reset_personal",
            (value,),
            {"type_": type_name, "as_form": False},
        )

    if operation == "compatibility.profile.privacy":
        privacy = body.get("privacy")
        values = _mapping(privacy, code="privacy_values_missing")
        if not values:
            raise PermanentCompatibilityError("privacy_values_missing")
        wire = {
            key: "1" if value is True else "0" if value is False else value
            for key, value in values.items()
        }
        return CompatibilityCall("profile", "set_privacy", kwargs=wire)

    if operation in {
        "compatibility.social.reject-friend",
        "compatibility.social.cancel-friend",
    }:
        raise PermanentCompatibilityError("friend_request_resolution_not_mappable")

    target_uid = _target_uid(payload, body)
    if operation == "compatibility.social.follow":
        return CompatibilityCall("social", "follow", (target_uid,), {"quietly": "1"})
    if operation == "compatibility.social.unfollow":
        return CompatibilityCall("social", "unfollow", (target_uid,))
    if operation == "compatibility.social.add-friend":
        message = str(
            body.get("leave_word")
            or body.get("yourwords")
            or body.get("message")
            or "你好，想和你成为好友"
        ).strip()[:100]
        return CompatibilityCall(
            "social",
            "add_friend",
            (target_uid, message),
            accept_empty=True,
            replay_safe=False,
        )
    if operation == "compatibility.social.agree-friend":
        return CompatibilityCall(
            "social",
            "agree_friend",
            (target_uid,),
            accept_empty=True,
            replay_safe=False,
        )
    if operation == "compatibility.social.delete-friend":
        return CompatibilityCall(
            "social",
            "delete_friend",
            kwargs=_provider_params(body, target_uid=target_uid),
            accept_empty=True,
        )
    if operation == "compatibility.social.blacklist-add":
        return CompatibilityCall(
            "social",
            "add_blacklist",
            kwargs=_provider_params(body, target_uid=target_uid),
        )
    if operation == "compatibility.social.blacklist-del":
        return CompatibilityCall(
            "social",
            "delete_blacklist",
            kwargs=_provider_params(body, target_uid=target_uid),
        )
    if operation == "compatibility.social.visit":
        return CompatibilityCall(
            "social",
            "record_profile_view",
            (target_uid,),
            replay_safe=False,
        )
    raise PermanentCompatibilityError("unsupported_social_compatibility_operation")


def _prepare_moments_call(
    claim: ClaimedOperation,
    resolver: CompatibilityResolver,
) -> CompatibilityCall:
    operation = claim.operation_type
    payload = _moment_payload(claim.payload)

    if operation == "compatibility.social.topic.create":
        public_id = _nonempty(payload.get("topic_public_id"), limit=40, code="topic_id_missing")
        name = _nonempty(payload.get("name"), limit=160, code="topic_name_missing")
        return CompatibilityCall(
            "content",
            "create_topic",
            (name,),
            replay_safe=False,
            binding=BindingTarget("topic", public_id),
        )

    if operation == "compatibility.social.post.publish":
        public_id = _nonempty(payload.get("post_public_id"), limit=40, code="post_id_missing")
        if _has_media(payload.get("media")):
            raise PermanentCompatibilityError("post_media_not_mappable")
        text = _nonempty(payload.get("body"), limit=2000, code="post_body_missing")
        visibility = _VISIBILITY_SCOPE.get(str(payload.get("visibility") or "public"))
        if not visibility:
            raise PermanentCompatibilityError("post_visibility_not_mappable")
        comment_policy = str(payload.get("comment_policy") or "open")
        if comment_policy not in {"open", "disabled"}:
            raise PermanentCompatibilityError("post_comment_policy_not_mappable")
        topics = payload.get("topics")
        topic_names = (
            [str(value).strip() for value in topics if str(value).strip()]
            if isinstance(topics, Sequence) and not isinstance(topics, (str, bytes))
            else []
        )
        topic_json = (
            json.dumps([{"topic": value} for value in topic_names], ensure_ascii=False)
            if topic_names
            else ""
        )
        return CompatibilityCall(
            "social",
            "publish_post",
            (text,),
            {
                "title": str(payload.get("title") or "").strip()[:240],
                "plate": str(payload.get("plate") or "动态")[:32],
                "visibility_scope": visibility,
                "comment_forbid": "1" if comment_policy == "disabled" else "0",
                "hide_comment": "1" if bool(payload.get("hide_comments")) else "0",
                "topics": topic_json,
            },
            accept_empty=True,
            replay_safe=False,
            binding=BindingTarget("post", public_id),
        )

    if operation in {
        "compatibility.social.post.pin",
        "compatibility.social.post.comment-policy",
        "compatibility.social.reaction.like",
    }:
        raise PermanentCompatibilityError("non_idempotent_toggle_not_mappable")

    if operation in {
        "compatibility.social.post.delete",
        "compatibility.social.post.visibility",
        "compatibility.social.post.view",
    }:
        public_id = _nonempty(payload.get("post_public_id"), limit=40, code="post_id_missing")
        upstream_id = resolver.require_upstream_id("post", public_id)
        if operation.endswith("post.delete"):
            return CompatibilityCall(
                "social", "delete_post", (upstream_id,), accept_empty=True
            )
        if operation.endswith("post.visibility"):
            scope = _VISIBILITY_SCOPE.get(str(payload.get("visibility") or ""))
            if not scope:
                raise PermanentCompatibilityError("post_visibility_not_mappable")
            return CompatibilityCall(
                "social",
                "change_post_visibility",
                (upstream_id, scope),
                accept_empty=True,
            )
        return CompatibilityCall(
            "social",
            "record_post_view",
            (upstream_id,),
            accept_empty=True,
            replay_safe=False,
        )

    if operation == "compatibility.social.comment.publish":
        post_public_id = _nonempty(payload.get("post_public_id"), limit=40, code="post_id_missing")
        comment_public_id = _nonempty(payload.get("comment_public_id"), limit=40, code="comment_id_missing")
        post = resolver.post(post_public_id)
        upstream_post_id = resolver.require_upstream_id("post", post_public_id)
        text = _nonempty(payload.get("body"), limit=500, code="comment_body_missing")
        kwargs: dict[str, Any] = {}
        parent_public_id = str(payload.get("parent_public_id") or "").strip()
        if parent_public_id:
            parent = resolver.comment(parent_public_id)
            kwargs.update(
                main_id=resolver.require_upstream_id("comment", parent_public_id),
                main_owner=str(parent.author_upstream_uid or "0"),
            )
        return CompatibilityCall(
            "social",
            "send_comment",
            (text, upstream_post_id, str(post.author_upstream_uid or "0")),
            kwargs,
            accept_empty=True,
            replay_safe=False,
            binding=BindingTarget("comment", comment_public_id),
        )

    if operation in {
        "compatibility.social.comment.delete",
        "compatibility.social.comment.moderate",
    }:
        public_id = _nonempty(payload.get("comment_public_id"), limit=40, code="comment_id_missing")
        upstream_id = resolver.require_upstream_id("comment", public_id)
        if operation.endswith("comment.delete"):
            return CompatibilityCall(
                "social", "delete_comment", (upstream_id,), accept_empty=True
            )
        if not bool(payload.get("hidden")):
            raise PermanentCompatibilityError("comment_unhide_not_mappable")
        return CompatibilityCall(
            "social", "forbid_comment", (upstream_id,), accept_empty=True
        )

    if operation == "compatibility.social.report.create":
        target_type = str(payload.get("target_type") or "").strip().lower()
        if target_type not in {"post", "comment"}:
            raise PermanentCompatibilityError("report_target_not_mappable")
        public_id = _nonempty(payload.get("target_public_id"), limit=40, code="report_target_missing")
        upstream_id = resolver.require_upstream_id(target_type, public_id)
        reason = ": ".join(
            value
            for value in (
                str(payload.get("reason_code") or "").strip(),
                str(payload.get("reason_text") or "").strip(),
            )
            if value
        )[:1000]
        return CompatibilityCall(
            "social",
            "report",
            (target_type, upstream_id, reason or "web report"),
            replay_safe=False,
        )
    raise PermanentCompatibilityError("unsupported_moments_compatibility_operation")


def _prepare_call(db: Any, claim: ClaimedOperation) -> CompatibilityCall:
    operation = claim.operation_type
    if operation not in RECOGNIZED_OPERATION_TYPES:
        raise PermanentCompatibilityError("unknown_compatibility_operation")
    resolver = CompatibilityResolver(db)
    if operation.startswith("compatibility.profile.") or operation in {
        "compatibility.social.follow",
        "compatibility.social.unfollow",
        "compatibility.social.add-friend",
        "compatibility.social.agree-friend",
        "compatibility.social.reject-friend",
        "compatibility.social.cancel-friend",
        "compatibility.social.delete-friend",
        "compatibility.social.blacklist-add",
        "compatibility.social.blacklist-del",
        "compatibility.social.visit",
    }:
        return _prepare_social_call(claim, resolver)
    return _prepare_moments_call(claim, resolver)


def _load_runtime_account(db: Any, owner_user_id: uuid.UUID) -> RuntimeAccountState:
    binding = ExternalAccountRepository(db).get_user_binding(
        owner_user_id,
        provider=LEGACY_PROVIDER,
    )
    if binding is None:
        raise PermanentCompatibilityError("legacy_account_binding_missing")
    user, account = binding
    if str(user.status or "") != "active" or user.disabled_at is not None:
        raise PermanentCompatibilityError("local_user_inactive")
    if not bool(account.sync_enabled):
        raise PermanentCompatibilityError("legacy_sync_disabled")
    upstream_uid = str(account.upstream_uid or "").strip()
    if not upstream_uid or upstream_uid == "0":
        raise PermanentCompatibilityError("legacy_upstream_uid_missing")
    if not isinstance(account.login_account_encrypted, Mapping):
        raise PermanentCompatibilityError("legacy_login_credential_missing")
    if not isinstance(account.token_encrypted, Mapping):
        raise RetryableCompatibilityError("legacy_login_token_missing")
    return RuntimeAccountState(
        account_id=account.id,
        upstream_uid=upstream_uid,
        login_encrypted=dict(account.login_account_encrypted),
        token_encrypted=dict(account.token_encrypted),
        token_expires_at=account.token_expires_at,
        display_name=str(user.display_name or ""),
        profile=dict(user.profile or {}),
        device_data=dict(account.device_data or {}),
    )


def _create_runtime(
    state: RuntimeAccountState,
    *,
    cipher: CredentialCipher,
    runtime_provider: RuntimeProvider,
    now: datetime,
) -> Any:
    if state.token_expires_at is not None and _as_utc(state.token_expires_at) <= now:
        raise RetryableCompatibilityError("legacy_login_token_expired")
    context = lambda field: f"external-account:{state.account_id}:{field}"  # noqa: E731
    try:
        login = cipher.decrypt_text(
            state.login_encrypted,
            purpose="external-account.login",
            context=context("login"),
        )
        token = cipher.decrypt_text(
            state.token_encrypted,
            purpose="external-account.token",
            context=context("token"),
        )
    except Exception as exc:
        raise PermanentCompatibilityError("legacy_credential_decryption_failed") from exc
    if not str(token or "").strip() or str(token).strip() == "0":
        raise RetryableCompatibilityError("legacy_login_token_missing")

    data = state.device_data
    try:
        runtime = runtime_provider.create_runtime_from_state(
            ProviderSessionState(
                uid=state.upstream_uid,
                token=str(token),
                phone=str(login or ""),
                nickname=state.display_name,
                user_role=str(data.get("user_role") or ""),
                rp_verify_time=str(data.get("rp_verify_time") or "0"),
                vip=str(data.get("vip") or "0"),
                svip=str(data.get("svip") or "0"),
                money=str(data.get("money") or "0"),
                portrait=str(data.get("portrait") or ""),
                user_sign=str(data.get("user_sign") or ""),
                login_id=str(data.get("login_id") or ""),
                raw_user=state.profile,
                device_data=data,
            )
        )
    except Exception as exc:
        raise RetryableCompatibilityError("legacy_runtime_restore_failed") from exc
    app = getattr(runtime, "app", None)
    if not bool(getattr(getattr(app, "session", None), "logged_in", False)):
        _close_runtime(runtime)
        raise RetryableCompatibilityError("legacy_runtime_not_logged_in")
    return runtime


def _close_runtime(runtime: Any) -> None:
    try:
        client = getattr(getattr(runtime, "app", None), "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except Exception:
        # Closing the isolated HTTP client is best effort and must not turn an
        # already classified provider outcome into a local rollback/retry.
        return


def _invoke(runtime: Any, call: CompatibilityCall) -> Any:
    component = getattr(runtime.app, call.component, None)
    method = getattr(component, call.method, None)
    if not callable(method):
        raise PermanentCompatibilityError("provider_method_unavailable")
    try:
        return method(*call.args, **dict(call.kwargs or {}))
    except CompatibilityDispatchError:
        raise
    except Exception as exc:
        name = type(exc).__name__
        definitely_not_sent = name in {
            "ConnectError",
            "ConnectTimeout",
            "ConnectionError",
            "NameResolutionError",
        }
        if call.replay_safe or definitely_not_sent:
            raise RetryableCompatibilityError(
                f"provider_call_exception.{name}"
            ) from exc
        raise PermanentCompatibilityError("provider_call_outcome_ambiguous") from exc


def _ensure_success(result: Any, call: CompatibilityCall) -> None:
    if bool(getattr(result, "ok", False)):
        return
    status = int(getattr(result, "status", 0) or 0)
    kind = str(getattr(result, "kind", "") or "")
    if call.accept_empty and kind == "empty" and 200 <= status < 300:
        return
    result_code = _stable_code(getattr(result, "code", "") or "unknown")
    code = f"provider_rejected.{status}.{result_code}"
    if status in {408, 425, 429} or status >= 500:
        raise RetryableCompatibilityError(code)
    if status <= 0 or 200 <= status < 300:
        if call.replay_safe:
            raise RetryableCompatibilityError(code)
        raise PermanentCompatibilityError("provider_result_outcome_ambiguous")
    if status in {401, 403}:
        # A later provider login can refresh the encrypted token.  Retry only
        # replay-safe setters; unsafe creates remain terminal to avoid doubles.
        if call.replay_safe:
            raise RetryableCompatibilityError(code)
    raise PermanentCompatibilityError(code)


def _candidate_upstream_id(
    value: Any,
    keys: tuple[str, ...],
    *,
    allow_generic_id: bool = False,
) -> str:
    if isinstance(value, Mapping):
        for key in (key for key in keys if key != "id"):
            candidate = value.get(key)
            if not isinstance(candidate, (Mapping, list, tuple)):
                normalized = str(candidate or "").strip()
                if (
                    normalized
                    and normalized != "0"
                    and len(normalized) <= 256
                    and not normalized.startswith(tuple(_LOCAL_PREFIXES.values()))
                ):
                    return normalized
        for nested_key in ("post", "comment", "topic", "data", "item", "result", "json_obj"):
            if nested_key in value:
                nested = _candidate_upstream_id(
                    value[nested_key],
                    keys,
                    allow_generic_id=True,
                )
                if nested:
                    return nested
        if allow_generic_id and "id" in keys:
            candidate = value.get("id")
            if not isinstance(candidate, (Mapping, list, tuple)):
                normalized = str(candidate or "").strip()
                if (
                    normalized
                    and normalized != "0"
                    and len(normalized) <= 256
                    and not normalized.startswith(tuple(_LOCAL_PREFIXES.values()))
                ):
                    return normalized
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value[:10]:
            nested = _candidate_upstream_id(
                item,
                keys,
                allow_generic_id=True,
            )
            if nested:
                return nested
    return ""


def _extract_upstream_id(result: Any, entity_type: str) -> str:
    keys = {
        "post": ("post_id", "postid", "postId", "id"),
        "comment": ("comment_id", "commentID", "commentId", "id"),
        "topic": ("topic_id", "topicid", "topicId", "id"),
    }[entity_type]
    return _candidate_upstream_id(getattr(result, "data", None), keys)


def _execute_claim(
    claim: ClaimedOperation,
    *,
    db_scope: DbScope,
    cipher: CredentialCipher,
    runtime_provider: RuntimeProvider,
    now: datetime,
) -> ExecutionResult:
    with db_scope() as db:
        call = _prepare_call(db, claim)
        account_state = _load_runtime_account(db, claim.owner_user_id)
        payload_actor = str(claim.payload.get("actor_uid") or "").strip()
        if payload_actor and payload_actor != account_state.upstream_uid:
            raise PermanentCompatibilityError("outbox_actor_binding_mismatch")

    if not _claim_still_processing(claim, db_scope=db_scope):
        raise PermanentCompatibilityError("compatibility_claim_cancelled")

    runtime = _create_runtime(
        account_state,
        cipher=cipher,
        runtime_provider=runtime_provider,
        now=now,
    )
    try:
        # The retirement CLI may cancel a processing row while this worker is
        # restoring its isolated runtime.  Recheck the lease immediately before
        # the only provider call so an archived row cannot escape upstream.
        if not _claim_still_processing(claim, db_scope=db_scope):
            raise PermanentCompatibilityError("compatibility_claim_cancelled")
        result = _invoke(runtime, call)
        _ensure_success(result, call)
    finally:
        _close_runtime(runtime)

    if call.binding is None:
        return ExecutionResult()
    upstream_id = _extract_upstream_id(result, call.binding.entity_type)
    if not upstream_id:
        # The mutation itself succeeded.  Replaying a create just to obtain an
        # ID risks a duplicate, so complete it and let dependent operations
        # wait/fail on the missing explicit mapping.
        return ExecutionResult(upstream_id_missing=True)
    try:
        with db_scope() as db:
            CompatibilityResolver(db).bind_upstream_id(
                target=call.binding,
                upstream_id=upstream_id,
                owner_user_id=claim.owner_user_id,
                now=now,
            )
    except CompatibilityDispatchError:
        raise
    except Exception as exc:
        # Upstream has already accepted a non-idempotent create.  Do not retry
        # the network call when only the local compatibility binding failed.
        raise PermanentCompatibilityError(
            "binding_persistence_failed_after_upstream_success"
        ) from exc
    return ExecutionResult()


def _claim_still_processing(
    claim: ClaimedOperation,
    *,
    db_scope: DbScope,
) -> bool:
    with db_scope() as db:
        row_id = db.scalar(
            select(OperationOutbox.id).where(
                OperationOutbox.id == claim.id,
                OperationOutbox.status == "processing",
                OperationOutbox.locked_by == claim.locked_by,
            )
        )
    return row_id is not None


def _default_runtime_provider() -> RuntimeProvider:
    # Keep the concrete protocol implementation behind the existing provider
    # boundary and import it only in the worker that actually has due work.
    from bbw_web.providers import LegacyBanghuaProvider

    return LegacyBanghuaProvider()


def dispatch_due(
    *,
    limit: int = DEFAULT_BATCH_SIZE,
    worker_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    settings: Settings | None = None,
    runtime_provider: RuntimeProvider | None = None,
    cipher: CredentialCipher | None = None,
    db_scope: DbScope = session_scope,
    clock: Clock = utcnow,
) -> dict[str, int]:
    """Claim and mirror a bounded batch of due compatibility operations.

    Network calls run outside PostgreSQL transactions.  The return value is a
    scheduler-friendly summary; failures are represented in outbox state and
    are not re-raised, so a dead Banghua service cannot stop unrelated jobs.
    """

    summary = {
        "claimed": 0,
        "completed": 0,
        "retried": 0,
        "failed": 0,
        "stale": 0,
        "completed_without_binding": 0,
    }
    if compatibility_mode(settings) is not CompatibilityMode.ENABLED:
        return summary

    resolved_worker_id = str(worker_id or _worker_id())[:128]
    claimed = _claim_due(
        db_scope=db_scope,
        worker_id=resolved_worker_id,
        limit=max(1, min(int(limit), MAX_BATCH_SIZE)),
        lease_seconds=lease_seconds,
        now=_as_utc(clock()),
    )
    summary["claimed"] = len(claimed)
    if not claimed:
        return summary

    try:
        resolved_cipher = cipher or CredentialCipher.from_settings(settings or get_settings())
    except Exception:
        for claim in claimed:
            status = _finalize_claim(
                claim,
                db_scope=db_scope,
                now=_as_utc(clock()),
                error=RetryableCompatibilityError("credential_keyring_unavailable"),
            )
            summary[status] += 1
        return summary

    try:
        provider = runtime_provider or _default_runtime_provider()
    except Exception:
        for claim in claimed:
            status = _finalize_claim(
                claim,
                db_scope=db_scope,
                now=_as_utc(clock()),
                error=RetryableCompatibilityError("legacy_provider_unavailable"),
            )
            summary[status] += 1
        return summary

    for claim in claimed:
        error: CompatibilityDispatchError | None = None
        execution = ExecutionResult()
        try:
            execution = _execute_claim(
                claim,
                db_scope=db_scope,
                cipher=resolved_cipher,
                runtime_provider=provider,
                now=_as_utc(clock()),
            )
        except CompatibilityDispatchError as exc:
            error = exc
        except Exception as exc:
            error = RetryableCompatibilityError(
                f"compatibility_worker_exception.{type(exc).__name__}"
            )
        status = _finalize_claim(
            claim,
            db_scope=db_scope,
            now=_as_utc(clock()),
            error=error,
        )
        summary[status] += 1
        if status == "completed" and execution.upstream_id_missing:
            summary["completed_without_binding"] += 1
    return summary


__all__ = [
    "DIRECTLY_MIRRORED_OPERATION_TYPES",
    "EXCLUDED_OPERATION_TYPES",
    "RECOGNIZED_OPERATION_TYPES",
    "UNSAFE_OR_UNMAPPABLE_OPERATION_TYPES",
    "CompatibilityDispatchError",
    "PermanentCompatibilityError",
    "RetryableCompatibilityError",
    "dispatch_due",
]
