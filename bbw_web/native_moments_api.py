"""Authenticated Web-local moments routes.

Canonical PostgreSQL writes are authoritative.  Compatibility work is added
to ``operation_outbox`` in the same local transaction and this module never
contacts Banghua or another external service.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, HTTPException, Request
from sqlalchemy import select

from bbw_prod.db import session_scope
from bbw_prod.models import SocialReaction, User, UserDiscoveryProfile
from bbw_prod.repositories import OperationOutboxRepository
from bbw_web.legacy_media_reference import (
    projected_profile_avatar,
    projected_social_post_media,
)
from bbw_web.media_native.references import (
    MediaAssetReferenceError,
    SqlAlchemyMediaAssetReferenceRepository,
    ValidatedMediaAsset,
)
from bbw_web.moments_native import (
    InvalidSocialContent,
    LegacyHistoryOutsideWindow,
    LocalMomentsService,
    SocialCommentView,
    SocialCommentsDisabled,
    SocialContentError,
    SocialContentForbidden,
    SocialContentNotFound,
    SocialIdempotencyConflict,
    SocialMirrorIntent,
    SocialPostView,
    SocialPrincipal,
    SocialTopicView,
    SqlAlchemyCanonicalSocialStore,
    SqlAlchemySocialPermissionPolicy,
)


router = APIRouter(tags=["moments-native"])
LOCAL_SOURCE = "web-local"
DEFAULT_COOKIE_NAME = "bbw_sid"
POST_RATE_LIMIT = 90
MOMENT_TABS = frozenset({"推荐", "附近", "最新", "招募令", "关注", "我的"})

VISIBILITY_ALIASES = {
    "public": "public",
    "公开": "public",
    "followers": "followers",
    "仅好友可见": "followers",
    "好友及粉丝可见": "followers",
    "private": "private",
    "仅自己可见": "private",
}
VISIBILITY_LABELS = {
    "public": "公开",
    "followers": "好友及粉丝可见",
    "private": "仅自己可见",
}


def _cookie_name(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "user_cookie_name", "") or DEFAULT_COOKIE_NAME)


def _identity(request: Request) -> Any:
    persistence = request.app.state.persistence
    sid = str(request.cookies.get(_cookie_name(request)) or "")
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return identity


def _rate_limit(request: Request, identity: Any, action: str) -> None:
    persistence = request.app.state.persistence
    if not persistence.rate_limit(
        f"moments-native:{action}:{identity.user_id}",
        limit=POST_RATE_LIMIT,
        window_seconds=60,
    ):
        raise HTTPException(status_code=429, detail="动态操作过于频繁")


def _effective_origin(request: Request) -> tuple[str, str, int] | None:
    scheme = str(request.headers.get("X-Forwarded-Proto") or request.url.scheme).split(
        ",", 1
    )[0].strip().lower()
    host = str(request.headers.get("Host") or request.url.netloc).strip()
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        port = parsed.port or (443 if scheme == "https" else 80 if scheme == "http" else 0)
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not parsed.hostname or not port:
        return None
    return scheme, parsed.hostname.lower(), port


def _require_same_origin(request: Request) -> None:
    if str(request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")
    raw = str(request.headers.get("Origin") or "").strip()
    if not raw:
        return
    try:
        parsed = urlsplit(raw)
        port = parsed.port or (
            443 if parsed.scheme.lower() == "https" else 80 if parsed.scheme.lower() == "http" else 0
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="请求来源无效") from exc
    origin = (parsed.scheme.lower(), str(parsed.hostname or "").lower(), port)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or origin != _effective_origin(request)
    ):
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")


def _principal(identity: Any, db: Any) -> SocialPrincipal:
    try:
        user_id = (
            identity.user_id
            if isinstance(identity.user_id, uuid.UUID)
            else uuid.UUID(str(identity.user_id))
        )
        upstream_uid = str(identity.upstream_uid or "").strip()
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="登录身份绑定不完整") from exc
    user = db.scalar(
        select(User).where(
            User.id == user_id,
            User.status == "active",
            User.disabled_at.is_(None),
        )
    )
    if user is None or not upstream_uid:
        raise HTTPException(status_code=401, detail="登录身份已不可用")
    return SocialPrincipal(
        user_id=user_id,
        upstream_uid=upstream_uid,
        display_name=str(user.display_name or ""),
    )


@contextmanager
def _service_context(
    identity: Any,
) -> Iterator[tuple[Any, SocialPrincipal, LocalMomentsService]]:
    with session_scope() as db:
        principal = _principal(identity, db)
        store = SqlAlchemyCanonicalSocialStore(db)
        policy = SqlAlchemySocialPermissionPolicy(db)
        yield db, principal, LocalMomentsService(store, policy)


def _enqueue_mirror(
    db: Any,
    *,
    principal: SocialPrincipal,
    intent: SocialMirrorIntent | None,
    not_mappable_reason: str = "",
) -> str:
    if intent is None:
        return "not-required"
    terminal = str(not_mappable_reason or "").strip()
    row, created = OperationOutboxRepository(db).enqueue(
        owner_user_id=principal.user_id,
        operation_type=f"compatibility.{intent.operation_type}"[:96],
        aggregate_type=intent.aggregate_type[:80],
        aggregate_id=intent.aggregate_public_id[:128],
        idempotency_key=intent.idempotency_key[:160],
        payload={
            "authority": LOCAL_SOURCE,
            "aggregate_public_id": intent.aggregate_public_id,
            "operation": intent.operation_type,
            "payload": intent.payload,
            "schema": 1,
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
    status = str(row.status or "pending")
    return status if created or status != "pending" else "already-pending"


def _media_reference_repository(
    request: Request,
    db: Any,
    *,
    require_storage: bool,
) -> SqlAlchemyMediaAssetReferenceRepository:
    settings = request.app.state.settings
    deployment = str(
        getattr(settings, "environment", "development") or "development"
    ).strip().lower()
    private_bucket = str(getattr(settings, "r2_bucket", "") or "").strip()
    if require_storage:
        try:
            storage = request.app.state.persistence.get_r2_storage()
            private_bucket = str(getattr(storage, "bucket", "") or "").strip()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "media_storage_unavailable",
                    "message": "媒体存储暂时不可用",
                    "retryable": True,
                },
            ) from exc
    if not deployment or (require_storage and not private_bucket):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "media_storage_unavailable",
                "message": "媒体存储范围配置不完整",
                "retryable": True,
            },
        )
    return SqlAlchemyMediaAssetReferenceRepository(
        db,
        deployment=deployment,
        private_bucket=private_bucket or None,
    )


def _social_error(exc: SocialContentError) -> HTTPException:
    if isinstance(exc, SocialContentNotFound):
        status = 404
    elif isinstance(exc, SocialContentForbidden):
        status = 403
    elif isinstance(exc, (SocialCommentsDisabled, SocialIdempotencyConflict)):
        status = 409
    elif isinstance(exc, LegacyHistoryOutsideWindow):
        status = 422
    else:
        status = 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise InvalidSocialContent("布尔参数不合法")


def _optional_bool(value: object) -> bool | None:
    return None if value is None else _bool(value)


def _integer(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise InvalidSocialContent("分页参数不合法") from exc
    if not minimum <= result <= maximum:
        raise InvalidSocialContent("分页参数不合法")
    return result


def _before(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw or raw.isdigit():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidSocialContent("动态游标不合法") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidSocialContent("动态游标必须包含时区")
    return parsed.astimezone(UTC)


def _request_key(request: Request, body: Mapping[str, Any], prefix: str) -> str:
    value = (
        body.get("client_request_id")
        or body.get("operation_id")
        or body.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or f"{prefix}:{uuid.uuid4()}"
    )
    return str(value)


def _topics(body: Mapping[str, Any]) -> tuple[str, ...]:
    raw = body.get("topics")
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, Mapping):
                result.append(str(item.get("topic") or item.get("name") or ""))
            else:
                result.append(str(item or ""))
        return tuple(result)
    single = str(body.get("topic") or raw or "").strip()
    return (single,) if single else ()


def _media_asset_ids(body: Mapping[str, Any]) -> tuple[object, ...]:
    forbidden = {
        key
        for key in (
            "media",
            "pictures",
            "postpicture",
            "video",
            "postvideo",
            "cover",
            "video_cover",
            "media_url",
            "url",
        )
        if key in body
    }
    if forbidden:
        raise InvalidSocialContent(
            "动态媒体仅接受 media_asset_ids，不能提交 URL 或媒体描述"
        )
    raw = body.get("media_asset_ids")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidSocialContent("media_asset_ids 必须是 UUID 数组")
    return tuple(raw)


def _canonical_media(
    assets_by_slot: Mapping[str, ValidatedMediaAsset],
) -> dict[str, Any]:
    if not assets_by_slot:
        return {}
    video = assets_by_slot.get("video")
    if video is not None:
        return {"video": video.content_path}
    pictures: list[str] = []
    for index in range(len(assets_by_slot)):
        asset = assets_by_slot.get(f"pictures[{index}]")
        if asset is None:
            raise InvalidSocialContent("动态图片 slot 不连续")
        pictures.append(asset.content_path)
    return {"pictures": pictures}


def _category(body: Mapping[str, Any]) -> str:
    raw = str(body.get("category") or body.get("plate") or "动态").strip()
    return {
        "dynamic": "dynamic",
        "动态": "dynamic",
        "recruitment": "recruitment",
        "招募令": "recruitment",
    }.get(raw.casefold(), raw)


def _location_label(value: object) -> str:
    if isinstance(value, Mapping):
        for key in ("city_name", "name", "label", "region", "city", "area"):
            label = _location_label(value.get(key))
            if label:
                return label
        return ""
    normalized = " ".join(str(value or "").strip().split())
    if normalized.casefold() in {
        "",
        "0",
        "none",
        "null",
        "undefined",
        "不限",
        "未知",
        "未设置",
        "暂未设置",
        "-",
    }:
        return ""
    return normalized


def _viewer_location(
    db: Any, principal: SocialPrincipal
) -> tuple[tuple[object, ...], str]:
    user = db.scalar(select(User).where(User.id == principal.user_id))
    profile = dict(user.profile or {}) if user is not None else {}
    discovery = db.scalar(
        select(UserDiscoveryProfile).where(
            UserDiscoveryProfile.user_id == principal.user_id
        )
    )
    human_values: list[object] = []
    code_values: list[object] = []
    if discovery is not None:
        human_values.append(discovery.city_name)
        code_values.append(discovery.city_code)
    for key in ("city_name", "city", "region", "area"):
        if profile.get(key) not in (None, ""):
            human_values.append(profile[key])
    for key in ("city_code", "region_code"):
        if profile.get(key) not in (None, ""):
            code_values.append(profile[key])
    values = human_values + code_values
    label = next(
        (_location_label(value) for value in human_values if _location_label(value)),
        "已设置城市" if any(_location_label(value) for value in code_values) else "",
    )
    return tuple(values), label


def _liked_target_ids(
    db: Any,
    *,
    principal: SocialPrincipal,
    target_type: str,
    target_ids: list[uuid.UUID],
) -> set[uuid.UUID]:
    if not target_ids:
        return set()
    target_column = (
        SocialReaction.post_id if target_type == "post" else SocialReaction.comment_id
    )
    return set(
        db.scalars(
            select(target_column).where(
                SocialReaction.actor_user_id == principal.user_id,
                SocialReaction.reaction_type == "like",
                SocialReaction.active.is_(True),
                target_column.in_(target_ids),
            )
        )
    )


def _is_self(
    principal: SocialPrincipal | None,
    *,
    author_user_id: uuid.UUID | None,
    author_upstream_uid: str,
) -> bool:
    if principal is None:
        return False
    return author_user_id == principal.user_id or (
        author_user_id is None
        and author_upstream_uid == principal.upstream_uid
    )


def _topic_dict(topic: SocialTopicView) -> dict[str, Any]:
    return {
        "id": topic.public_id,
        "topic_id": topic.public_id,
        "name": topic.name,
        "title": topic.name,
        "post_count": topic.post_count,
    }


def _author_avatar_map(
    db: Any, posts: list[SocialPostView]
) -> dict[uuid.UUID, str]:
    if not callable(getattr(db, "execute", None)):
        return {}
    author_ids = list(
        dict.fromkeys(
            post.author_user_id
            for post in posts
            if post.author_user_id is not None
        )
    )
    if not author_ids:
        return {}
    return {
        user_id: projected_profile_avatar(profile)
        for user_id, profile in db.execute(
            select(User.id, User.profile).where(User.id.in_(author_ids))
        )
    }


def _post_dict(
    post: SocialPostView,
    *,
    principal: SocialPrincipal | None = None,
    is_liked: bool = False,
    author_avatar: str = "",
) -> dict[str, Any]:
    media = projected_social_post_media(post.media)
    pictures = media.get("pictures") or media.get("images") or []
    if not isinstance(pictures, list):
        pictures = [pictures]
    snapshot = dict(getattr(post, "author_snapshot", {}) or {})
    return {
        "id": post.public_id,
        "post_id": post.public_id,
        "author_id": post.author_upstream_uid,
        "authid": post.author_upstream_uid,
        "nickname": post.author_display_name,
        "authnickname": post.author_display_name,
        "avatar": str(
            author_avatar
            or snapshot.get("avatar")
            or snapshot.get("portrait")
            or ""
        ),
        "region": _location_label(
            snapshot.get("city_name")
            or snapshot.get("region")
            or snapshot.get("city")
            or snapshot.get("area")
        ),
        "gender": snapshot.get("gender") or "",
        "property": snapshot.get("property") or "",
        "age": snapshot.get("age") or "",
        "title": post.title,
        "content": post.body,
        "posttext": post.body,
        "pictures": pictures,
        "postpicture": pictures,
        "video": media.get("video") or "",
        "postvideo": media.get("video") or "",
        "cover": media.get("cover") or "",
        "topics": [topic.name for topic in post.topics],
        "topic_list": [_topic_dict(topic) for topic in post.topics],
        "plate": "招募令" if post.category == "recruitment" else "动态",
        "like_count": post.like_count,
        "comment_count": post.comment_count,
        "view_count": post.view_count,
        "time": post.published_at.isoformat(),
        "created_at": post.published_at.isoformat(),
        "comment_forbid": post.comment_policy == "disabled",
        "hide_comment": post.hide_comments,
        "visibility_scope": post.visibility,
        "visibility_label": VISIBILITY_LABELS.get(post.visibility, post.visibility),
        "is_pinned": post.is_pinned,
        "is_self": _is_self(
            principal,
            author_user_id=post.author_user_id,
            author_upstream_uid=post.author_upstream_uid,
        ),
        "is_liked": bool(is_liked),
        "status": post.status,
        "source": LOCAL_SOURCE,
    }


def _comment_dict(
    comment: SocialCommentView,
    *,
    principal: SocialPrincipal | None = None,
    is_liked: bool = False,
) -> dict[str, Any]:
    return {
        "id": comment.public_id,
        "comment_id": comment.public_id,
        "post_id": comment.post_public_id,
        "author_id": comment.author_upstream_uid,
        "authid": comment.author_upstream_uid,
        "nickname": comment.author_display_name,
        "content": comment.body,
        "comment_text": comment.body,
        "time": comment.source_created_at.isoformat(),
        "created_at": comment.source_created_at.isoformat(),
        "like_count": comment.like_count,
        "reply_count": comment.reply_count,
        "main_id": comment.parent_public_id,
        "parent_comment_id": comment.parent_public_id,
        "is_forbidden": comment.status == "hidden",
        "is_self": _is_self(
            principal,
            author_user_id=comment.author_user_id,
            author_upstream_uid=comment.author_upstream_uid,
        ),
        "is_liked": bool(is_liked),
        "status": comment.status,
        "source": LOCAL_SOURCE,
    }


def _envelope(items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "items": items,
        "list": items,
        "count": len(items),
        "source": LOCAL_SOURCE,
        **extra,
    }


def _post_body_id(body: Mapping[str, Any]) -> str:
    return str(body.get("postid") or body.get("post_id") or body.get("id") or "").strip()


def _comment_body_id(body: Mapping[str, Any]) -> str:
    return str(body.get("comment_id") or body.get("id") or "").strip()


def _mutation_response(
    payload: Mapping[str, Any], *, mirror_status: str | bool
) -> dict[str, Any]:
    if isinstance(mirror_status, bool):
        mirror_status = "pending" if mirror_status else "already-pending"
    return {
        "ok": True,
        **dict(payload),
        "source": LOCAL_SOURCE,
        "compatibility_sync": mirror_status,
    }


@router.get("/api/topics")
def topics(request: Request, q: str = "", limit: int = 50) -> dict[str, Any]:
    identity = _identity(request)
    try:
        with _service_context(identity) as (_db, _principal_value, service):
            items = [_topic_dict(item) for item in service.topics(query=q, limit=limit)]
            return _envelope(items)
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/topics/create")
def create_topic(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "topic-create")
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.create_topic(
                principal=principal,
                name=body.get("topic") or body.get("name") or "",
                description=body.get("description") or "",
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "created": result.created,
                    "topic": _topic_dict(result.topic),
                    "topic_id": result.topic.public_id,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.get("/api/moments/posts")
def posts(
    request: Request,
    uid: str = "",
    tab: str = "推荐",
    search: str = "",
    cursor: str = "",
    before: str = "",
    page: int = 1,
    limit: int = 30,
) -> dict[str, Any]:
    identity = _identity(request)
    try:
        page_size = _integer(limit, default=30, minimum=1, maximum=100)
        page_number = _integer(page, default=1, minimum=1, maximum=100_000)
        cursor_value = _before(before or cursor)
        normalized_tab = str(tab or "推荐").strip()
        if normalized_tab not in MOMENT_TABS:
            raise InvalidSocialContent("不支持的动态分类")
        with _service_context(identity) as (db, principal, service):
            target_uid = str(uid or "").strip()
            if target_uid or normalized_tab == "我的":
                target_uid = target_uid or principal.upstream_uid
                rows = service.posts_by_author(
                    principal=principal,
                    author_upstream_uid=target_uid,
                    before=cursor_value,
                    limit=page_size,
                    offset=0 if cursor_value is not None else (page_number - 1) * page_size,
                )
                liked_ids = _liked_target_ids(
                    db,
                    principal=principal,
                    target_type="post",
                    target_ids=[post.id for post in rows],
                )
                author_avatars = _author_avatar_map(db, rows)
                items = [
                    _post_dict(
                        post,
                        principal=principal,
                        is_liked=post.id in liked_ids,
                        author_avatar=author_avatars.get(post.author_user_id, ""),
                    )
                    for post in rows
                ]
                next_cursor = rows[-1].published_at.isoformat() if rows else ""
                return _envelope(
                    items,
                    feed_type="user",
                    target_uid=target_uid,
                    page=page_number,
                    next_page=(
                        page_number + 1 if len(items) == page_size else ""
                    ),
                    next_cursor=next_cursor,
                )
            location_values: tuple[object, ...] = ()
            location_region = ""
            if normalized_tab == "附近":
                location_values, location_region = _viewer_location(db, principal)
                if not location_values or not location_region:
                    return {
                        "ok": False,
                        "code": "PROFILE_LOCATION_MISSING",
                        "message": "当前资料缺少城市信息",
                        "error": "请先在个人资料中设置城市后再查看附近动态",
                        "items": [],
                        "list": [],
                        "count": 0,
                        "source": LOCAL_SOURCE,
                        "location_required": True,
                        "location_region": "",
                        "next_cursor": "",
                    }
            feed = service.feed(
                principal=principal,
                before=cursor_value,
                limit=page_size,
                query=search,
                following_only=normalized_tab == "关注",
                region=location_values,
                category=("recruitment" if normalized_tab == "招募令" else ""),
                order=("latest" if normalized_tab in {"附近", "最新"} else "ranked"),
            )
            liked_ids = _liked_target_ids(
                db,
                principal=principal,
                target_type="post",
                target_ids=[item.post.id for item in feed],
            )
            feed_posts = [item.post for item in feed]
            author_avatars = _author_avatar_map(db, feed_posts)
            items = []
            for item in feed:
                row = _post_dict(
                    item.post,
                    principal=principal,
                    is_liked=item.post.id in liked_ids,
                    author_avatar=author_avatars.get(
                        item.post.author_user_id, ""
                    ),
                )
                row["feed_score"] = item.score.total
                row["feed_score_explanation"] = list(item.score.explanation)
                items.append(row)
            next_cursor = feed[-1].post.published_at.isoformat() if feed else ""
            return _envelope(
                items,
                feed_type="ranked",
                tab=normalized_tab,
                search=str(search or "").strip(),
                location_region=location_region,
                next_cursor=next_cursor,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.get("/api/moments/comments")
def comments(
    request: Request,
    postid: str = "",
    post_id: str = "",
    before: str = "",
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    identity = _identity(request)
    try:
        page_size = _integer(limit, default=50, minimum=1, maximum=200)
        page_number = _integer(page, default=1, minimum=1, maximum=100_000)
        with _service_context(identity) as (db, principal, service):
            rows = service.comments(
                principal=principal,
                post_public_id=postid or post_id,
                before=_before(before),
                limit=page_size,
                offset=(page_number - 1) * page_size,
            )
            liked_ids = _liked_target_ids(
                db,
                principal=principal,
                target_type="comment",
                target_ids=[comment.id for comment in rows],
            )
            items = [
                _comment_dict(
                    comment,
                    principal=principal,
                    is_liked=comment.id in liked_ids,
                )
                for comment in rows
            ]
            return _envelope(
                items,
                post_id=postid or post_id,
                page=page_number,
                next_page=page_number + 1 if len(items) == page_size else "",
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


def _write_context(request: Request, action: str) -> Any:
    _require_same_origin(request)
    identity = _identity(request)
    _rate_limit(request, identity, action)
    return identity


@router.post("/api/moments/view")
def view_post(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "view")
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.record_view(
                principal=principal, post_public_id=_post_body_id(body)
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "post_id": result.post.public_id,
                    "view_count": result.post.view_count,
                    "counted": result.counted,
                    "task_assist": {
                        "checked": True,
                        "task_found": False,
                        "completed": False,
                        "retryable": False,
                        "state": "web-local-authoritative",
                    },
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/moments/publish")
def publish(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "publish")
    try:
        requested_asset_ids = _media_asset_ids(body)
        visibility = VISIBILITY_ALIASES.get(
            str(body.get("visibility_scope") or body.get("visibility") or "公开").strip(),
            "",
        )
        comment_policy = str(body.get("comment_policy") or "").strip().lower()
        if not comment_policy:
            comment_policy = (
                "disabled" if _bool(body.get("comment_forbid")) else "open"
            )
        with _service_context(identity) as (db, principal, service):
            references = (
                _media_reference_repository(
                    request,
                    db,
                    require_storage=True,
                )
                if requested_asset_ids
                else None
            )
            validated_assets: dict[str, ValidatedMediaAsset] = {}
            if references is not None:
                try:
                    validated_assets = references.preview_social_post_asset_ids(
                        owner_user_id=principal.user_id,
                        asset_ids=requested_asset_ids,
                    )
                except MediaAssetReferenceError as exc:
                    raise InvalidSocialContent(str(exc)) from exc
            media = _canonical_media(validated_assets)
            result = service.publish(
                principal=principal,
                client_request_id=_request_key(request, body, "publish"),
                title=body.get("title") or "",
                body=body.get("text") or body.get("posttext") or body.get("content") or "",
                media=media,
                visibility=visibility,
                comment_policy=comment_policy,
                hide_comments=_bool(body.get("hide_comment")),
                category=_category(body),
                topics=_topics(body),
            )
            if references is not None:
                try:
                    bound_assets = references.bind_social_post_assets(
                        owner_user_id=principal.user_id,
                        social_post_id=result.post.id,
                        assets_by_slot={
                            slot: asset.asset_id
                            for slot, asset in validated_assets.items()
                        },
                    )
                except MediaAssetReferenceError as exc:
                    raise InvalidSocialContent(str(exc)) from exc
                if {
                    slot: reference.content_path
                    for slot, reference in bound_assets.items()
                } != {
                    slot: asset.content_path
                    for slot, asset in validated_assets.items()
                }:
                    raise InvalidSocialContent("动态媒体引用与 canonical 内容不一致")
            mirror_status = _enqueue_mirror(
                db,
                principal=principal,
                intent=result.mirror,
                not_mappable_reason=(
                    "post_media_not_mappable" if validated_assets else ""
                ),
            )
            return _mutation_response(
                {
                    "created": result.created,
                    "post": _post_dict(
                        result.post,
                        principal=principal,
                        author_avatar=_author_avatar_map(
                            db, [result.post]
                        ).get(result.post.author_user_id, ""),
                    ),
                    "post_id": result.post.public_id,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/moments/comment")
def publish_comment(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "comment")
    try:
        parent = str(
            body.get("parent_comment_id")
            or body.get("main_id")
            or body.get("sub_id")
            or ""
        ).strip()
        if parent in {"", "0", "none", "null"}:
            parent = ""
        with _service_context(identity) as (db, principal, service):
            result = service.comment(
                principal=principal,
                post_public_id=_post_body_id(body),
                client_request_id=_request_key(request, body, "comment"),
                body=body.get("text") or body.get("comment_text") or body.get("content") or "",
                parent_comment_public_id=parent,
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "created": result.created,
                    "comment": _comment_dict(result.comment, principal=principal),
                    "comment_id": result.comment.public_id,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


def _like(
    request: Request,
    body: Mapping[str, Any],
    *,
    target_type: str,
) -> dict[str, Any]:
    identity = _write_context(request, f"like-{target_type}")
    target_id = _post_body_id(body) if target_type == "post" else _comment_body_id(body)
    if "active" in body:
        desired = _optional_bool(body.get("active"))
    elif "liked" in body or "ifauthlike" in body:
        desired = not _bool(body.get("liked", body.get("ifauthlike")))
    else:
        desired = None
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.set_like(
                principal=principal,
                target_type=target_type,
                target_public_id=target_id,
                active=desired,
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "target_type": target_type,
                    "target_id": result.reaction.target_public_id,
                    "liked": result.reaction.active,
                    "active": result.reaction.active,
                    "like_count": result.reaction.count,
                    "changed": result.reaction.changed,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/social/like-post")
def like_post(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return _like(request, body, target_type="post")


@router.post("/api/moments/comment-like")
def like_comment(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return _like(request, body, target_type="comment")


@router.post("/api/moments/comment-delete")
def delete_comment(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "comment-delete")
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.delete_comment(
                principal=principal, comment_public_id=_comment_body_id(body)
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "comment": _comment_dict(result.comment, principal=principal),
                    "deleted": True,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/moments/comment-forbid")
def forbid_comment(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "comment-forbid")
    try:
        hidden = _bool(
            body.get("hidden", body.get("forbidden")), default=True
        )
        with _service_context(identity) as (db, principal, service):
            result = service.set_comment_hidden(
                principal=principal,
                comment_public_id=_comment_body_id(body),
                hidden=hidden,
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "comment": _comment_dict(result.comment, principal=principal),
                    "forbidden": hidden,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/moments/post-delete")
def delete_post(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "post-delete")
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.delete_post(
                principal=principal, post_public_id=_post_body_id(body)
            )
            try:
                _media_reference_repository(
                    request,
                    db,
                    require_storage=False,
                ).release_social_post_media(
                    owner_user_id=principal.user_id,
                    social_post_id=result.post.id,
                )
            except MediaAssetReferenceError as exc:
                raise InvalidSocialContent(str(exc)) from exc
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "post": _post_dict(
                        result.post,
                        principal=principal,
                        author_avatar=_author_avatar_map(
                            db, [result.post]
                        ).get(result.post.author_user_id, ""),
                    ),
                    "deleted": True,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/moments/post-visibility")
def post_visibility(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "post-visibility")
    raw_scope = str(
        body.get("scope") or body.get("visibility_scope") or body.get("visibility") or ""
    ).strip()
    visibility = VISIBILITY_ALIASES.get(raw_scope, "")
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.set_visibility(
                principal=principal,
                post_public_id=_post_body_id(body),
                visibility=visibility,
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "post": _post_dict(
                        result.post,
                        principal=principal,
                        author_avatar=_author_avatar_map(
                            db, [result.post]
                        ).get(result.post.author_user_id, ""),
                    ),
                    "visibility_scope": result.post.visibility,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/moments/post-pin")
def post_pin(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "post-pin")
    try:
        with _service_context(identity) as (db, principal, service):
            if "pinned" in body or "active" in body:
                result = service.set_pinned(
                    principal=principal,
                    post_public_id=_post_body_id(body),
                    pinned=_bool(body.get("pinned", body.get("active"))),
                )
            else:
                result = service.toggle_pinned(
                    principal=principal, post_public_id=_post_body_id(body)
                )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "post": _post_dict(
                        result.post,
                        principal=principal,
                        author_avatar=_author_avatar_map(
                            db, [result.post]
                        ).get(result.post.author_user_id, ""),
                    ),
                    "pinned": result.post.is_pinned,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc


@router.post("/api/social/report")
def report(
    request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    identity = _write_context(request, "report")
    raw_type = str(body.get("target_type") or body.get("type") or "post").strip().lower()
    target_type = "comment" if raw_type in {"comment", "评论"} else "post"
    target_id = str(
        body.get("target_id")
        or body.get("itemid")
        or body.get("post_id")
        or body.get("comment_id")
        or body.get("id")
        or ""
    ).strip()
    try:
        with _service_context(identity) as (db, principal, service):
            result = service.report(
                principal=principal,
                target_type=target_type,
                target_public_id=target_id,
                idempotency_key=_request_key(request, body, "report"),
                reason_code=body.get("reason_code") or raw_type or "other",
                reason_text=body.get("reason") or body.get("reason_text") or "",
            )
            mirror_status = _enqueue_mirror(
                db, principal=principal, intent=result.mirror
            )
            return _mutation_response(
                {
                    "report_id": result.report.public_id,
                    "target_type": result.report.target_type,
                    "target_id": result.report.target_public_id,
                    "created": result.report.created,
                    "status": result.report.status,
                },
                mirror_status=mirror_status,
            )
    except SocialContentError as exc:
        raise _social_error(exc) from exc
