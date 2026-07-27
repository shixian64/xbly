"""可插拔的 Web-local 资料与关系 HTTP 路由适配层。

本模块只写 PostgreSQL：本地 canonical 变更和 compatibility outbox 位于
同一个事务，绝不在请求路径调用 Banghua 或其他外部网络。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from bbw_prod.db import session_scope
from bbw_prod.models import ActivityEvent, ExternalAccount, Relationship
from bbw_prod.repositories import OperationOutboxRepository
from bbw_web.account_display import canonical_self_account_display
from bbw_web.social_native import (
    InvalidSocialInput,
    LocalSocialService,
    SocialIdempotencyConflict,
    SocialIdentityUnavailable,
    SocialNativeError,
    SocialPrincipal,
    SocialSelfActionForbidden,
    SocialTargetNotMigrated,
    SocialTargetUnavailable,
)
from bbw_web.social_native.contracts import (
    LEGACY_ACCOUNT_PROVIDER,
    RELATION_FRIEND_REQUEST,
    REQUEST_ACCEPTED,
    REQUEST_BLOCKED,
    REQUEST_CANCELLED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    SOCIAL_NATIVE_PROVIDER,
    SOCIAL_NATIVE_SCHEMA,
    FriendRequestMutationResult,
    FriendRequestView,
    ProfilePatch,
    SocialAccount,
    SocialMutationResult,
    SocialProfileView,
)
from bbw_web.social_native.repository import (
    SqlAlchemyCanonicalSocialStore,
    is_trusted_legacy_relationship,
)
from bbw_web.social_native.service import normalize_operation_id, normalize_upstream_uid
from bbw_web.media_native.references import (
    MediaAssetReferenceError,
    PROFILE_AVATAR_SLOT,
    PROFILE_RESOURCE,
    SqlAlchemyMediaAssetReferenceRepository,
    native_media_content_asset_id,
    native_media_content_path,
)


GET_PATHS = frozenset(
    {
        "/api/profile/me",
        "/api/profile/user",
        "/api/profile/users",
        "/api/social/follows",
        "/api/social/fans",
        "/api/social/friends",
        "/api/social/friend-apply",
        "/api/social/blacklist",
        "/api/social/blacklist-me",
        "/api/social/visitors",
    }
)
POST_PATHS = frozenset(
    {
        "/api/profile/nick",
        "/api/profile/reset",
        "/api/profile/privacy",
        "/api/social/follow",
        "/api/social/unfollow",
        "/api/social/add-friend",
        "/api/social/agree-friend",
        "/api/social/reject-friend",
        "/api/social/cancel-friend",
        "/api/social/delete-friend",
        "/api/social/blacklist-add",
        "/api/social/blacklist-del",
        "/api/social/visit",
    }
)
HANDLED_PATHS = GET_PATHS | POST_PATHS
SOCIAL_NATIVE_WRITE_PATHS = POST_PATHS

PRIVACY_FIELDS = frozenset(
    {
        "allow_call",
        "allow_follow",
        "allow_friend_request",
        "allow_profile_visits",
        "allow_stranger_message",
        "discoverable_by_phone",
        "show_age",
        "show_city",
        "show_distance",
        "show_gender",
        "show_location",
        "show_moments",
        "show_online_status",
    }
)

PROFILE_RESET_FIELDS = {
    "nickname": "nickname",
    "昵称": "nickname",
    "昵称设置": "nickname",
    "signature": "signature",
    "签名": "signature",
    "签名设置": "signature",
    "个性签名": "signature",
    "个性签名设置": "signature",
    "city": "city",
    "城市": "city",
    "城市设置": "city",
    "所在地": "city",
    "gender": "gender",
    "性别": "gender",
    "性别设置": "gender",
    "avatar": "avatar",
    "头像": "avatar",
    "头像设置": "avatar",
}
GENDER_ALIASES = {
    "男": "male",
    "male": "male",
    "女": "female",
    "female": "female",
    "其他": "other",
    "other": "other",
    "保密": "unspecified",
    "未设置": "unspecified",
    "unspecified": "unspecified",
}


@dataclass(frozen=True, slots=True)
class NativeSocialResponse:
    status: int
    payload: dict[str, Any]
    legacy_read_fallback_allowed: bool = False


def _response(
    payload: Mapping[str, Any],
    status: int = 200,
    *,
    legacy_read_fallback_allowed: bool = False,
) -> NativeSocialResponse:
    return NativeSocialResponse(
        status=status,
        payload=dict(payload),
        legacy_read_fallback_allowed=legacy_read_fallback_allowed,
    )


def _error(exc: SocialNativeError) -> NativeSocialResponse:
    if exc.code == "SOCIAL_IDENTITY_UNAVAILABLE":
        status = 401
    elif exc.code == "SOCIAL_TARGET_UNAVAILABLE":
        status = 404
    elif exc.code in {
        "SOCIAL_BLOCKED",
        "ALREADY_FRIENDS",
        "FRIEND_REQUEST_STATE_CONFLICT",
        "SOCIAL_IDEMPOTENCY_CONFLICT",
    }:
        status = 409
    else:
        status = 400
    return _response(
        {
            "ok": False,
            "code": exc.code,
            "error": str(exc),
            "source": SOCIAL_NATIVE_PROVIDER,
        },
        status,
        legacy_read_fallback_allowed=isinstance(exc, SocialTargetNotMigrated),
    )


def _principal(identity: Any) -> SocialPrincipal:
    try:
        user_id = identity.user_id
        account_id = identity.external_account_id
        return SocialPrincipal(
            user_id=user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)),
            external_account_id=(
                account_id
                if isinstance(account_id, uuid.UUID)
                else uuid.UUID(str(account_id))
            ),
            upstream_uid=str(identity.upstream_uid or "").strip(),
            account_provider=str(
                getattr(identity, "account_provider", LEGACY_ACCOUNT_PROVIDER)
                or LEGACY_ACCOUNT_PROVIDER
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise SocialIdentityUnavailable("Web 身份绑定不完整") from exc


def _query_values(query: Mapping[str, Any], key: str) -> list[str]:
    raw = query.get(key)
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    result: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            normalized = item.strip()
            if normalized:
                result.append(normalized)
    return result


def _query_first(
    query: Mapping[str, Any], *keys: str, default: str = ""
) -> str:
    for key in keys:
        values = _query_values(query, key)
        if values:
            return values[0]
    return default


def _page(
    query: Mapping[str, Any], *, zero_based: bool = False, page_size: int = 50
) -> tuple[int, int, int]:
    default = 0 if zero_based else 1
    raw = _query_first(query, "page", "pageindex", default=str(default))
    if not raw.isdigit():
        raise InvalidSocialInput("分页参数不合法")
    page = int(raw)
    if page < default or page > 100_000:
        raise InvalidSocialInput("分页参数不合法")
    offset_page = page if zero_based else page - 1
    return page, page_size, offset_page * page_size


def _operation_id(body: Mapping[str, Any]) -> str:
    if "operation_id" in body:
        value: object = body.get("operation_id")
    else:
        return str(uuid.uuid4())
    normalized = normalize_operation_id(value)
    if not normalized:
        raise InvalidSocialInput("operation_id 不合法")
    return normalized


def _target_uid(
    body: Mapping[str, Any], current_uid: str, *preferred: str
) -> str:
    keys = preferred or ("uid", "target_uid", "you", "yourid", "to", "peer")
    candidates = [str(body.get(key) or "").strip() for key in keys]
    uid = next((value for value in candidates if value and value != current_uid), "")
    if not normalize_upstream_uid(uid):
        raise InvalidSocialInput("缺少有效的目标用户 UID")
    return uid


def _profile_dict(profile: SocialProfileView) -> dict[str, Any]:
    flags = profile.relationship
    outgoing_pending = flags.outgoing_friend_request == REQUEST_PENDING
    incoming_pending = flags.incoming_friend_request == REQUEST_PENDING
    return {
        "id": profile.upstream_uid,
        "uid": profile.upstream_uid,
        "user_id": profile.upstream_uid,
        "nickname": profile.nickname,
        "name": profile.nickname,
        "avatar": profile.avatar,
        "portrait": profile.avatar,
        "signature": profile.signature,
        "city": profile.city,
        "gender": profile.gender,
        "sex": profile.gender,
        "is_friend": flags.friend,
        "is_friend_apply": outgoing_pending,
        "has_incoming_friend_apply": incoming_pending,
        "friend_apply_status": flags.outgoing_friend_request,
        "incoming_friend_apply_status": flags.incoming_friend_request,
        "is_following": flags.following,
        "is_follower": flags.following,
        "is_fans": flags.followed_by,
        "is_followed_by": flags.followed_by,
        "blocked": flags.blocked,
        "blocked_by": flags.blocked_by,
        "source": SOCIAL_NATIVE_PROVIDER,
    }


def _self_profile_dict(
    profile: SocialProfileView,
    account: SocialAccount,
) -> dict[str, Any]:
    item = _profile_dict(profile)
    item.update(
        canonical_self_account_display(
            account.profile,
            account.account_display_data,
        )
    )
    return item


def _envelope(
    items: Sequence[Mapping[str, Any]], *, summary: bool = False, **extra: Any
) -> dict[str, Any]:
    rows = [dict(item) for item in items]
    payload: dict[str, Any] = {
        "ok": True,
        "items": [] if summary else rows,
        "list": [] if summary else rows,
        "count": len(rows),
        "source": SOCIAL_NATIVE_PROVIDER,
    }
    payload.update(extra)
    return payload


def _mutation_payload(
    result: SocialMutationResult, *, message: str
) -> dict[str, Any]:
    return {
        "ok": True,
        "message": message,
        "changed": result.changed,
        "state": result.state,
        "target_uid": result.target_upstream_uid,
        "related_changes": list(result.related_changes),
        "idempotent_replay": result.idempotent_replay,
        "source": SOCIAL_NATIVE_PROVIDER,
        "compatibility_sync": "pending",
    }


def _request_payload(
    result: FriendRequestMutationResult,
    *,
    direction: str,
    message: str,
) -> dict[str, Any]:
    request = result.request
    return {
        "ok": True,
        "message": message,
        "changed": result.changed,
        "idempotent_replay": result.idempotent_replay,
        "apply_id": str(request.id),
        "request_id": str(request.id),
        "request_status": request.state,
        "application_status": request.state,
        "direction": direction,
        "from_uid": request.requester_upstream_uid,
        "to_uid": request.target_upstream_uid,
        "source": SOCIAL_NATIVE_PROVIDER,
        "compatibility_sync": "pending",
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key).lower() not in {"password", "token", "authorization"}
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _enqueue_compatibility(
    db: Any,
    *,
    principal: SocialPrincipal,
    operation_id: str,
    path: str,
    body: Mapping[str, Any],
    target_uid: str = "",
    not_mappable_reason: str = "",
) -> str:
    operation_type = f"compatibility{path.replace('/api/', '.').replace('/', '.')}"
    terminal = str(not_mappable_reason or "").strip()
    result = OperationOutboxRepository(db).enqueue(
        owner_user_id=principal.user_id,
        operation_type=operation_type[:96],
        aggregate_type="social-native",
        aggregate_id=(target_uid or principal.upstream_uid)[:128],
        idempotency_key=operation_id,
        payload={
            "authority": SOCIAL_NATIVE_PROVIDER,
            "method": "POST",
            "path": path,
            "actor_uid": principal.upstream_uid,
            "target_uid": target_uid,
            "body": _json_safe(body),
            "schema": SOCIAL_NATIVE_SCHEMA,
        },
        status="cancelled" if terminal else "pending",
        **(
            {
                "completed_at": datetime.now(UTC),
                "last_error": terminal[:2000],
            }
            if terminal
            else {}
        ),
    )
    try:
        row, _created = result
    except (TypeError, ValueError):
        return "pending"
    status = getattr(row, "status", None)
    return status if isinstance(status, str) and status else "pending"


def _privacy_values(body: Mapping[str, Any]) -> dict[str, bool]:
    nested = body.get("privacy")
    raw = dict(nested) if isinstance(nested, Mapping) else {}
    params = body.get("params")
    if isinstance(params, Mapping):
        raw.update({str(key): value for key, value in params.items()})
    raw.update(
        {
            str(key): value
            for key, value in body.items()
            if key
            not in {"privacy", "params", "operation_id", "idempotency_key"}
        }
    )
    unknown = set(raw) - PRIVACY_FIELDS
    if unknown:
        raise InvalidSocialInput(f"不支持的隐私字段：{', '.join(sorted(unknown))}")
    if not raw:
        raise InvalidSocialInput("隐私设置不能为空")

    normalized: dict[str, bool] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            normalized[key] = value
            continue
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            normalized[key] = True
        elif text in {"0", "false", "no", "off"}:
            normalized[key] = False
        else:
            raise InvalidSocialInput(f"隐私字段 {key} 必须是布尔值")
    return normalized


def _activity_digest(actor_uid: str, target_uid: str) -> str:
    encoded = json.dumps(
        {
            "actor_uid": actor_uid,
            "schema": SOCIAL_NATIVE_SCHEMA,
            "target_uid": target_uid,
            "type": "profile.visit",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _insert_visit_projection(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    event_key: str,
    event_type: str,
    actor_uid: str,
    target_uid: str,
    occurred_at: datetime,
    projection: str,
) -> tuple[ActivityEvent, bool]:
    digest = _activity_digest(actor_uid, target_uid)
    details = {
        "authority": SOCIAL_NATIVE_PROVIDER,
        "idempotency_digest": digest,
        "projection": projection,
        "schema": SOCIAL_NATIVE_SCHEMA,
    }
    statement = (
        insert(ActivityEvent)
        .values(
            owner_user_id=owner_user_id,
            provider=SOCIAL_NATIVE_PROVIDER,
            upstream_event_id=event_key,
            event_type=event_type,
            actor_upstream_uid=actor_uid,
            subject_upstream_uid=target_uid,
            occurred_at=occurred_at,
            details=details,
        )
        .on_conflict_do_nothing(constraint="uq_activity_events_owner_source")
        .returning(ActivityEvent)
    )
    created = db.scalars(statement).first()
    if created is not None:
        return created, True
    existing = db.scalar(
        select(ActivityEvent).where(
            ActivityEvent.owner_user_id == owner_user_id,
            ActivityEvent.provider == SOCIAL_NATIVE_PROVIDER,
            ActivityEvent.upstream_event_id == event_key,
        )
    )
    existing_details = dict(existing.details or {}) if existing is not None else {}
    if (
        existing is None
        or existing.event_type != event_type
        or existing_details.get("idempotency_digest") != digest
        or existing_details.get("projection") != projection
    ):
        raise SocialIdempotencyConflict("operation_id 已被其他访客记录占用")
    return existing, False


def _record_visit(
    db: Any,
    *,
    store: SqlAlchemyCanonicalSocialStore,
    principal: SocialPrincipal,
    target_uid: str,
    operation_id: str,
    occurred_at: datetime,
) -> tuple[bool, SocialAccount]:
    actor = store.resolve_principal(principal)
    if actor is None:
        raise SocialIdentityUnavailable("当前 Web 账号不可用于本地访客记录")
    target = store.resolve_active_target(
        target_uid, provider=principal.account_provider
    )
    if target is None:
        raise SocialTargetUnavailable("目标账号不存在、未迁移或已停用")
    if target.user_id == actor.user_id:
        raise SocialSelfActionForbidden("不能记录自己访问自己")

    outgoing, outgoing_created = _insert_visit_projection(
        db,
        owner_user_id=actor.user_id,
        event_key=f"social:{operation_id}",
        event_type="social.profile.visit.outgoing",
        actor_uid=actor.upstream_uid,
        target_uid=target.upstream_uid,
        occurred_at=occurred_at,
        projection="outgoing",
    )
    _incoming, incoming_created = _insert_visit_projection(
        db,
        owner_user_id=target.user_id,
        event_key=f"social:visit-in:{actor.user_id}:{operation_id}",
        event_type="social.profile.visit.incoming",
        actor_uid=actor.upstream_uid,
        target_uid=target.upstream_uid,
        occurred_at=outgoing.occurred_at,
        projection="incoming",
    )
    return outgoing_created or incoming_created, target


def _visitor_items(
    db: Any,
    *,
    service: LocalSocialService,
    principal: SocialPrincipal,
    visit_type: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    actor = service.get_me(principal=principal)
    if visit_type == "seen_me":
        event_type = "social.profile.visit.incoming"
        peer_field = "actor_upstream_uid"
    elif visit_type == "seen_by_me":
        event_type = "social.profile.visit.outgoing"
        peer_field = "subject_upstream_uid"
    else:
        raise InvalidSocialInput("type 仅支持 seen_me 或 seen_by_me")
    events = list(
        db.scalars(
            select(ActivityEvent)
            .where(
                ActivityEvent.owner_user_id == actor.user_id,
                ActivityEvent.provider == SOCIAL_NATIVE_PROVIDER,
                ActivityEvent.event_type == event_type,
            )
            .order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id)
            .limit(1_000)
        )
    )
    latest: list[tuple[str, ActivityEvent]] = []
    seen: set[str] = set()
    for event in events:
        peer_uid = str(getattr(event, peer_field) or "").strip()
        if not peer_uid or peer_uid in seen:
            continue
        seen.add(peer_uid)
        latest.append((peer_uid, event))
    selected = latest[offset : offset + limit]
    profiles = {
        profile.upstream_uid: profile
        for profile in service.get_users(
            principal=principal, upstream_uids=[uid for uid, _event in selected]
        )
    }
    items: list[dict[str, Any]] = []
    for uid, event in selected:
        profile = profiles.get(uid)
        if profile is None:
            continue
        item = _profile_dict(profile)
        item.update(
            {
                "visit_time": event.occurred_at.isoformat(),
                "time": event.occurred_at.isoformat(),
                "visit_type": visit_type,
            }
        )
        items.append(item)
    return items


def _friend_request_peer_uid(
    db: Any,
    *,
    principal: SocialPrincipal,
    body: Mapping[str, Any],
    direction: str,
) -> str:
    direct = str(
        body.get("uid")
        or body.get("requester_uid")
        or body.get("target_uid")
        or ""
    ).strip()
    if normalize_upstream_uid(direct):
        return direct
    raw_id = str(body.get("apply_id") or body.get("id") or "").strip()
    try:
        request_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise InvalidSocialInput("缺少好友申请账号或有效申请编号") from exc
    row = db.scalar(
        select(Relationship).where(
            Relationship.id == request_id,
            Relationship.provider.in_(
                (SOCIAL_NATIVE_PROVIDER, LEGACY_ACCOUNT_PROVIDER)
            ),
            Relationship.kind == RELATION_FRIEND_REQUEST,
        )
    )
    if row is None or (
        row.provider == LEGACY_ACCOUNT_PROVIDER
        and not is_trusted_legacy_relationship(row)
    ):
        raise InvalidSocialInput("好友申请不存在或不属于当前账号")

    provider = str(row.provider or "")
    role = (
        "direct"
        if provider == SOCIAL_NATIVE_PROVIDER
        else SqlAlchemyCanonicalSocialStore._legacy_request_role(row)
    )
    direct_owned = row.owner_user_id == principal.user_id
    direct_targeted = row.subject_upstream_uid == principal.upstream_uid
    mirror_owned = direct_owned
    mirror_targeted = direct_targeted
    if direction == "outgoing":
        if role == "direct" and direct_owned:
            uid = str(row.subject_upstream_uid or "")
            if normalize_upstream_uid(uid):
                return uid
        if role != "incoming-mirror" or not mirror_targeted:
            raise InvalidSocialInput("好友申请不存在或不属于当前账号")
    else:
        if role == "incoming-mirror" and mirror_owned:
            uid = str(row.subject_upstream_uid or "")
            if normalize_upstream_uid(uid):
                return uid
        if role != "direct" or not direct_targeted:
            raise InvalidSocialInput("好友申请不存在或不属于当前账号")

    account = db.scalar(
        select(ExternalAccount).where(
            ExternalAccount.user_id == row.owner_user_id,
            ExternalAccount.provider == principal.account_provider,
        )
    )
    uid = str(account.upstream_uid or "") if account is not None else ""
    if not normalize_upstream_uid(uid):
        raise InvalidSocialInput("好友申请账号绑定不可用")
    return uid


def _friend_request_item(
    service: LocalSocialService,
    principal: SocialPrincipal,
    request: FriendRequestView,
    direction: str,
) -> dict[str, Any] | None:
    peer_uid = (
        request.requester_upstream_uid
        if direction == "incoming"
        else request.target_upstream_uid
    )
    try:
        profile = service.get_user(principal=principal, upstream_uid=peer_uid)
    except SocialNativeError:
        return None
    item = _profile_dict(profile)
    display_status = (
        REQUEST_CANCELLED if request.state == REQUEST_BLOCKED else request.state
    )
    item.update(
        {
            "apply_id": str(request.id),
            "relation_id": str(request.id),
            "direction": direction,
            "direction_label": "对方向我申请" if direction == "incoming" else "我向对方申请",
            "status": display_status,
            "canonical_status": request.state,
            "request_status": display_status,
            "can_accept": direction == "incoming" and request.state == REQUEST_PENDING,
            "is_friend": request.state == REQUEST_ACCEPTED,
            "is_friend_apply": request.state == REQUEST_PENDING,
            "from_uid": request.requester_upstream_uid,
            "to_uid": request.target_upstream_uid,
            "leave_words": request.message,
            "request_time": request.requested_at.isoformat(),
            "created_at": request.requested_at.isoformat(),
        }
    )
    return item


def _dispatch_get(
    db: Any,
    *,
    service: LocalSocialService,
    store: SqlAlchemyCanonicalSocialStore,
    principal: SocialPrincipal,
    path: str,
    query: Mapping[str, Any],
) -> NativeSocialResponse:
    summary = _query_first(query, "summary", default="0") == "1"
    if path == "/api/profile/me":
        profile, account = service.get_me_with_account(principal=principal)
        user = _self_profile_dict(profile, account)
        return _response(
            {
                "ok": True,
                "user": user,
                "items": [user],
                "list": [user],
                "count": 1,
                "source": SOCIAL_NATIVE_PROVIDER,
            }
        )
    if path == "/api/profile/user":
        uid = _query_first(query, "uid", default=principal.upstream_uid)
        user = _profile_dict(
            service.get_user(principal=principal, upstream_uid=uid)
        )
        return _response(
            {
                "ok": True,
                "user": user,
                "items": [user],
                "list": [user],
                "count": 1,
                "source": SOCIAL_NATIVE_PROVIDER,
            }
        )
    if path == "/api/profile/users":
        uids = _query_values(query, "uids") + _query_values(query, "uid")
        if not uids:
            raise InvalidSocialInput("缺少用户 UID")
        items = [
            _profile_dict(profile)
            for profile in service.get_users(principal=principal, upstream_uids=uids)
        ]
        return _response(_envelope(items))

    _page_number, limit, offset = _page(
        query, zero_based=path == "/api/social/visitors"
    )
    if path in {
        "/api/social/follows",
        "/api/social/fans",
        "/api/social/friends",
    }:
        relation = {
            "/api/social/follows": "following",
            "/api/social/fans": "followers",
            "/api/social/friends": "friends",
        }[path]
        items = [
            _profile_dict(profile)
            for profile in service.list_relationships(
                principal=principal,
                relation=relation,
                limit=limit,
                offset=offset,
            )
        ]
        return _response(_envelope(items, summary=summary))
    if path in {"/api/social/blacklist", "/api/social/blacklist-me"}:
        direction = "outgoing" if path.endswith("blacklist") else "incoming"
        blocks = service.list_blocks(
            principal=principal,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        items = []
        for block in blocks:
            item = _profile_dict(block.profile)
            item.update(
                {
                    "block_direction": direction,
                    "blocked_at": block.started_at.isoformat(),
                }
            )
            items.append(item)
        return _response(_envelope(items, summary=summary))
    if path == "/api/social/visitors":
        visit_type = _query_first(query, "type", default="seen_me")
        items = _visitor_items(
            db,
            service=service,
            principal=principal,
            visit_type=visit_type,
            limit=limit,
            offset=offset,
        )
        return _response(_envelope(items, summary=summary, visit_type=visit_type))
    if path == "/api/social/friend-apply":
        statuses = (
            REQUEST_PENDING,
            REQUEST_ACCEPTED,
            REQUEST_REJECTED,
            REQUEST_CANCELLED,
            REQUEST_BLOCKED,
        )
        combined: list[tuple[FriendRequestView, str]] = []
        for direction in ("incoming", "outgoing"):
            for state in statuses:
                combined.extend(
                    (request, direction)
                    for request in service.list_friend_requests(
                        principal=principal,
                        direction=direction,
                        state=state,
                        limit=200,
                        offset=0,
                    )
                )
        combined.sort(key=lambda value: value[0].requested_at, reverse=True)
        selected = combined[offset : offset + limit]
        items = [
            item
            for request, direction in selected
            if (
                item := _friend_request_item(
                    service, principal, request, direction
                )
            )
            is not None
        ]
        pending_incoming = sum(
            request.state == REQUEST_PENDING and direction == "incoming"
            for request, direction in combined
        )
        has_more = offset + limit < len(combined)
        next_page = str(offset // limit + 2) if has_more else ""
        payload = _envelope(
            items,
            summary=summary,
            pending_incoming_count=pending_incoming,
            pending_outgoing_count=sum(
                request.state == REQUEST_PENDING and direction == "outgoing"
                for request, direction in combined
            ),
            has_more=has_more,
            next_page=next_page,
        )
        if summary:
            payload["count"] = pending_incoming
        return _response(payload)
    raise AssertionError(f"unhandled native GET path: {path}")


def _dispatch_post(
    db: Any,
    *,
    service: LocalSocialService,
    store: SqlAlchemyCanonicalSocialStore,
    principal: SocialPrincipal,
    path: str,
    body: Mapping[str, Any],
    media_reference_deployment: str | None = None,
    media_reference_bucket: str | None = None,
) -> NativeSocialResponse:
    operation_id = _operation_id(body)
    target_uid = ""

    if path in {"/api/profile/nick", "/api/profile/reset"}:
        avatar_asset_id: uuid.UUID | None = None
        requested_avatar_path = ""
        bound_avatar = None
        if path == "/api/profile/nick":
            field = "nickname"
            value = body.get("name") if "name" in body else body.get("nickname")
        else:
            raw_field = str(
                body.get("field")
                or body.get("type")
                or body.get("type_")
                or ("头像设置" if body.get("avatar_asset_id") else "昵称设置")
            ).strip()
            field = PROFILE_RESET_FIELDS.get(raw_field, "")
            if not field:
                raise InvalidSocialInput("不支持的资料重置类型")
            if field == "avatar":
                forbidden = {
                    key
                    for key in ("value", "avatar", "portrait", "url", "media")
                    if key in body
                }
                if forbidden:
                    raise InvalidSocialInput(
                        "头像仅接受 avatar_asset_id，不能提交 URL 或媒体地址"
                    )
                avatar_asset_id = body.get("avatar_asset_id")
                if avatar_asset_id in (None, ""):
                    raise InvalidSocialInput("头像更新缺少 avatar_asset_id")
                try:
                    requested_avatar_path = native_media_content_path(avatar_asset_id)
                except MediaAssetReferenceError as exc:
                    raise InvalidSocialInput(str(exc)) from exc
                avatar_asset_id = native_media_content_asset_id(
                    requested_avatar_path
                )
                if avatar_asset_id is None:
                    raise InvalidSocialInput("头像 asset UUID 不合法")
                value = requested_avatar_path
            else:
                if "avatar_asset_id" in body:
                    raise InvalidSocialInput("avatar_asset_id 仅用于头像更新")
                value = body.get("value")
        if field == "gender":
            value = GENDER_ALIASES.get(str(value or "").strip().lower(), "")
        result = service.update_me(
            principal=principal,
            values={field: value},
            operation_id=operation_id,
        )
        if field == "avatar":
            current_avatar = str(
                (result.profile.profile or {}).get("avatar") or ""
            ).strip()
            references = SqlAlchemyMediaAssetReferenceRepository(
                db,
                deployment=media_reference_deployment,
                private_bucket=media_reference_bucket,
            )
            if result.idempotent_replay:
                current_asset_id = native_media_content_asset_id(current_avatar)
                if current_asset_id is not None:
                    bound_avatar = references.get_current_reference(current_asset_id)
                    if (
                        bound_avatar is None
                        or bound_avatar.resource_type != PROFILE_RESOURCE
                        or bound_avatar.resource_id != principal.user_id
                        or bound_avatar.owner_user_id != principal.user_id
                        or bound_avatar.slot != PROFILE_AVATAR_SLOT
                    ):
                        raise InvalidSocialInput("当前头像引用已失效或不一致")
                elif current_avatar.startswith("/api/media/native/"):
                    raise InvalidSocialInput("当前头像站内路径不合法")
            else:
                if current_avatar != requested_avatar_path:
                    raise InvalidSocialInput("头像资料写入与请求路径不一致")
                try:
                    bound_avatar = references.bind_profile_avatar(
                        owner_user_id=principal.user_id,
                        asset_id=avatar_asset_id,
                    )
                except MediaAssetReferenceError as exc:
                    raise InvalidSocialInput(str(exc)) from exc
                if bound_avatar.content_path != current_avatar:
                    raise InvalidSocialInput("头像引用路径与资料写入不一致")
        compatibility_status = _enqueue_compatibility(
            db,
            principal=principal,
            operation_id=operation_id,
            path=path,
            body={"field": field, "value": result.profile.profile.get(field)},
            not_mappable_reason=(
                "profile_avatar_upload_not_mappable"
                if field == "avatar"
                else ""
            ),
        )
        user = _self_profile_dict(
            service.get_me(principal=principal),
            result.profile,
        )
        avatar_response = {}
        if field == "avatar":
            avatar_response["avatar"] = user["avatar"]
            if bound_avatar is not None:
                avatar_response["avatar_asset_id"] = str(bound_avatar.asset_id)
        return _response(
            {
                "ok": True,
                "changed": result.changed,
                "idempotent_replay": result.idempotent_replay,
                "user": user,
                "items": [user],
                "list": [user],
                "source": SOCIAL_NATIVE_PROVIDER,
                "compatibility_sync": compatibility_status,
                **avatar_response,
            }
        )

    if path == "/api/profile/privacy":
        privacy = _privacy_values(body)
        actor = store.resolve_principal(principal)
        if actor is None:
            raise SocialIdentityUnavailable("当前 Web 账号不可用于本地资料更新")
        raw_existing_privacy = actor.profile.get("privacy")
        existing_privacy = (
            dict(raw_existing_privacy)
            if isinstance(raw_existing_privacy, Mapping)
            else {}
        )
        existing_privacy.update(privacy)
        result = store.update_profile(
            actor=actor,
            patch=ProfilePatch(values={"privacy": existing_privacy}),
            operation_id=operation_id,
            occurred_at=datetime.now(UTC),
        )
        compatibility_status = _enqueue_compatibility(
            db,
            principal=principal,
            operation_id=operation_id,
            path=path,
            body={"privacy": privacy},
        )
        return _response(
            {
                "ok": True,
                "changed": result.changed,
                "privacy": existing_privacy,
                "idempotent_replay": result.idempotent_replay,
                "source": SOCIAL_NATIVE_PROVIDER,
                "compatibility_sync": compatibility_status,
            }
        )

    if path in {"/api/social/follow", "/api/social/unfollow"}:
        target_uid = _target_uid(body, principal.upstream_uid, "uid", "you", "target_uid")
        if path.endswith("unfollow"):
            result = service.unfollow(
                principal=principal,
                target_upstream_uid=target_uid,
                operation_id=operation_id,
            )
            message = "已取消关注"
        else:
            result = service.follow(
                principal=principal,
                target_upstream_uid=target_uid,
                operation_id=operation_id,
            )
            message = "已关注"
        response = _mutation_payload(result, message=message)
    elif path == "/api/social/add-friend":
        target_uid = _target_uid(body, principal.upstream_uid, "uid", "target_uid", "myid")
        message_value = (
            body.get("leave_word")
            or body.get("yourwords")
            or body.get("message")
            or "你好，想和你成为好友"
        )
        result = service.request_friend(
            principal=principal,
            target_upstream_uid=target_uid,
            message=message_value,
            operation_id=operation_id,
        )
        response = _request_payload(
            result, direction="outgoing", message="好友申请已发送"
        )
    elif path in {"/api/social/agree-friend", "/api/social/reject-friend"}:
        target_uid = _friend_request_peer_uid(
            db, principal=principal, body=body, direction="incoming"
        )
        resolution = (
            REQUEST_ACCEPTED
            if path.endswith("agree-friend")
            else REQUEST_REJECTED
        )
        result = service.resolve_friend_request(
            principal=principal,
            requester_upstream_uid=target_uid,
            resolution=resolution,
            operation_id=operation_id,
        )
        response = _request_payload(
            result,
            direction="incoming",
            message="已同意好友申请" if resolution == REQUEST_ACCEPTED else "已拒绝好友申请",
        )
        response["verified"] = True
    elif path == "/api/social/cancel-friend":
        target_uid = _friend_request_peer_uid(
            db, principal=principal, body=body, direction="outgoing"
        )
        result = service.cancel_friend_request(
            principal=principal,
            target_upstream_uid=target_uid,
            operation_id=operation_id,
        )
        response = _request_payload(
            result, direction="outgoing", message="好友申请已取消"
        )
    elif path == "/api/social/delete-friend":
        target_uid = _target_uid(
            body,
            principal.upstream_uid,
            "uid",
            "target_uid",
            "yourid",
            "you",
            "to",
        )
        result = service.delete_friend(
            principal=principal,
            target_upstream_uid=target_uid,
            operation_id=operation_id,
        )
        response = _mutation_payload(result, message="好友已删除")
    elif path in {"/api/social/blacklist-add", "/api/social/blacklist-del"}:
        target_uid = _target_uid(
            body,
            principal.upstream_uid,
            "uid",
            "target_uid",
            "yourid",
            "you",
            "to",
        )
        if path.endswith("blacklist-add"):
            result = service.block(
                principal=principal,
                target_upstream_uid=target_uid,
                operation_id=operation_id,
            )
            message = "已加入黑名单"
        else:
            result = service.unblock(
                principal=principal,
                target_upstream_uid=target_uid,
                operation_id=operation_id,
            )
            message = "已移出黑名单"
        response = _mutation_payload(result, message=message)
    elif path == "/api/social/visit":
        target_uid = _target_uid(body, principal.upstream_uid, "uid", "yourid", "target_uid")
        changed, _target = _record_visit(
            db,
            store=store,
            principal=principal,
            target_uid=target_uid,
            operation_id=operation_id,
            occurred_at=datetime.now(UTC),
        )
        response = {
            "ok": True,
            "visited": True,
            "changed": changed,
            "target_uid": target_uid,
            "source": SOCIAL_NATIVE_PROVIDER,
            "compatibility_sync": "pending",
        }
    else:
        raise AssertionError(f"unhandled native POST path: {path}")

    compatibility_status = _enqueue_compatibility(
        db,
        principal=principal,
        operation_id=operation_id,
        path=path,
        body=body,
        target_uid=target_uid,
        # 拒绝/撤销好友申请没有可安全映射的上游语义（worker 端只会以
        # PermanentCompatibilityError 收场）。与头像一致：直接写 cancelled
        # + 稳定原因，不虚报 pending，也不污染 failed 统计。
        not_mappable_reason=(
            "friend_request_resolution_not_mappable"
            if path in {"/api/social/reject-friend", "/api/social/cancel-friend"}
            else ""
        ),
    )
    response["compatibility_sync"] = compatibility_status
    return _response(response)


def dispatch_social_native(
    identity: Any,
    method: object,
    path: object,
    query: Mapping[str, Any] | None,
    body: Mapping[str, Any] | None,
    *,
    db: Any | None = None,
    media_reference_deployment: str | None = None,
    media_reference_bucket: str | None = None,
) -> NativeSocialResponse | None:
    """处理 Web-local 社交路由；不属于本模块的路径返回 ``None``。

    ASGI 调用方必须对 ``SOCIAL_NATIVE_WRITE_PATHS`` 先执行 JSON、Origin 和
    Sec-Fetch-Site 同源检查；此纯同步 dispatcher 不接收或信任 HTTP headers。
    """

    normalized_path = str(path or "").split("?", 1)[0]
    normalized_method = str(method or "GET").upper()
    if normalized_path not in HANDLED_PATHS:
        return None
    if (
        normalized_method == "GET"
        and normalized_path not in GET_PATHS
        or normalized_method == "POST"
        and normalized_path not in POST_PATHS
        or normalized_method not in {"GET", "POST"}
    ):
        return _response(
            {
                "ok": False,
                "code": "METHOD_NOT_ALLOWED",
                "error": "请求方法不受支持",
                "source": SOCIAL_NATIVE_PROVIDER,
            },
            405,
        )

    try:
        principal = _principal(identity)
        with (nullcontext(db) if db is not None else session_scope()) as action_db:
            store = SqlAlchemyCanonicalSocialStore(action_db)
            service = LocalSocialService(store)
            if normalized_method == "GET":
                return _dispatch_get(
                    action_db,
                    service=service,
                    store=store,
                    principal=principal,
                    path=normalized_path,
                    query=query if isinstance(query, Mapping) else {},
                )
            return _dispatch_post(
                action_db,
                service=service,
                store=store,
                principal=principal,
                path=normalized_path,
                body=body if isinstance(body, Mapping) else {},
                media_reference_deployment=media_reference_deployment,
                media_reference_bucket=media_reference_bucket,
            )
    except SocialNativeError as exc:
        return _error(exc)
