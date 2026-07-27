"""HTTP dispatcher for Web-local discovery and text matching.

Only city-level location is accepted.  Browser latitude/longitude values are
never parsed, persisted or used for ranking.  Every handled route is backed by
PostgreSQL and remains available without Banghua.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from bbw_prod.db import session_scope
from bbw_web.discovery_native import (
    DISCOVERY_PROVIDER,
    QUEUE_STATUS_MATCHED,
    DiscoveryCard,
    DiscoveryIdentityUnavailable,
    DiscoveryList,
    DiscoveryLocationRequired,
    DiscoveryNativeError,
    DiscoveryNativeService,
    DiscoveryPrincipal,
    DiscoveryProfile,
    InvalidDiscoveryRequest,
    MatchPreference,
    MatchRateLimited,
    MatchRequestConflict,
    TextMatchOutcome,
)
from bbw_web.discovery_native.repository import (
    SqlAlchemyDiscoveryStore,
    normalized_city_code,
)
from bbw_web.discovery_native.service import (
    MAX_DISCOVERY_LIST_SIZE,
    TEXT_MATCH_RATE_LIMIT,
    TEXT_MATCH_RATE_WINDOW_SECONDS,
)


GET_PATHS = frozenset(
    {
        "/api/match/status",
        "/api/match/online-users",
        "/api/match/nearby-users",
    }
)
POST_PATHS = frozenset({"/api/match/online", "/api/match/local"})
HANDLED_PATHS = GET_PATHS | POST_PATHS
DISCOVERY_NATIVE_WRITE_PATHS = POST_PATHS

MATCH_GENDERS = ("不限", "男", "女")
MATCH_PROPERTIES = ("双", "Z", "B")
DISCOVERY_AGES = {
    "不限": (18, 120),
    "18-24": (18, 24),
    "25-34": (25, 34),
    "35-44": (35, 44),
    "45+": (45, 120),
}


@dataclass(frozen=True, slots=True)
class NativeDiscoveryResponse:
    status: int
    payload: dict[str, Any]


def _response(
    payload: Mapping[str, Any], status: int = 200
) -> NativeDiscoveryResponse:
    return NativeDiscoveryResponse(status=status, payload=dict(payload))


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(identity: Any) -> DiscoveryPrincipal:
    if identity is None:
        raise DiscoveryIdentityUnavailable("请先登录")
    try:
        raw_user_id = identity.user_id
        raw_account_id = identity.external_account_id
        user_id = (
            raw_user_id
            if isinstance(raw_user_id, uuid.UUID)
            else uuid.UUID(str(raw_user_id))
        )
        account_id = (
            raw_account_id
            if isinstance(raw_account_id, uuid.UUID)
            else uuid.UUID(str(raw_account_id))
        )
        upstream_uid = str(identity.upstream_uid or "").strip()
    except (AttributeError, TypeError, ValueError) as exc:
        raise DiscoveryIdentityUnavailable("当前 Web 登录身份不可用") from exc
    if (
        not upstream_uid
        or len(upstream_uid) > 128
        or any(ord(char) < 33 for char in upstream_uid)
    ):
        raise DiscoveryIdentityUnavailable("当前 Web 登录身份不可用")
    return DiscoveryPrincipal(
        user_id=user_id,
        external_account_id=account_id,
        upstream_uid=upstream_uid,
    )


def _capabilities(identity: Any) -> dict[str, bool]:
    broad_message_access = bool(
        getattr(identity, "match_pool_online_list_enabled", False)
    )
    return {
        "match_pool_online_list": True,
        "voice_match": False,
        "proactive_private_message": broad_message_access,
        "direct_im_credentials": broad_message_access,
        "nearby_custom_city": bool(
            getattr(identity, "nearby_custom_city_enabled", False)
        ),
    }


def _query_values(query: Mapping[str, Any], key: str) -> list[str]:
    raw = query.get(key)
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    result: list[str] = []
    for value in values:
        for part in str(value or "").split(","):
            normalized = part.strip()
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


def _strict_limit(value: object) -> int:
    raw = str(value or MAX_DISCOVERY_LIST_SIZE).strip()
    if not raw.isascii() or not raw.isdigit():
        raise InvalidDiscoveryRequest("limit 不合法")
    return min(MAX_DISCOVERY_LIST_SIZE, max(1, int(raw)))


def _gender(value: object, *, preference: bool = False) -> str:
    raw = str(value or "不限").strip()
    aliases = {
        "不限": "any",
        "any": "any",
        "all": "any",
        "*": "any",
        "男": "male",
        "male": "male",
        "女": "female",
        "female": "female",
        "其他": "other",
        "other": "other",
    }
    normalized = aliases.get(raw.casefold())
    if normalized is None:
        raise InvalidDiscoveryRequest("性别条件无效")
    return normalized


def _gender_label(value: str) -> str:
    return {
        "any": "不限",
        "male": "男",
        "female": "女",
        "other": "其他",
        "unspecified": "未设置",
    }.get(str(value), "未设置")


def _property(value: object, *, optional: bool) -> str | None:
    raw = str(value or "").strip()
    if optional and raw in {"", "不限", "any"}:
        return None
    if raw.casefold() in {"z", "b"}:
        raw = raw.upper()
    if raw not in MATCH_PROPERTIES:
        raise InvalidDiscoveryRequest("属性条件无效")
    return raw


def _age_range(value: object, query: Mapping[str, Any] | None = None) -> tuple[int, int]:
    query = query or {}
    minimum = _query_first(query, "min_age", default="")
    maximum = _query_first(query, "max_age", default="")
    if minimum or maximum:
        if not (
            minimum
            and maximum
            and minimum.isascii()
            and minimum.isdigit()
            and maximum.isascii()
            and maximum.isdigit()
        ):
            raise InvalidDiscoveryRequest("年龄条件无效")
        low, high = int(minimum), int(maximum)
        if 18 <= low <= high <= 120:
            return low, high
        raise InvalidDiscoveryRequest("年龄条件无效")
    raw = str(value or "不限").strip()
    if raw in DISCOVERY_AGES:
        return DISCOVERY_AGES[raw]
    raise InvalidDiscoveryRequest("年龄条件无效")


def _request_id(body: Mapping[str, Any]) -> str:
    provided = next(
        (
            body.get(key)
            for key in (
                "request_id",
                "operation_id",
                "client_request_id",
                "idempotency_key",
            )
            if key in body
        ),
        None,
    )
    if provided is None:
        return str(uuid.uuid4())
    request_id = str(provided or "").strip()
    if (
        not request_id
        or len(request_id) > 160
        or any(ord(char) < 32 or ord(char) == 127 for char in request_id)
    ):
        raise InvalidDiscoveryRequest("request_id 不合法")
    return request_id


def _profile_sync(
    *,
    service: DiscoveryNativeService,
    store: SqlAlchemyDiscoveryStore,
    principal: DiscoveryPrincipal,
) -> DiscoveryProfile:
    values = store.canonical_profile_values(principal)
    if values is None:
        raise DiscoveryIdentityUnavailable("当前 Web 账号不可用")
    service.update_profile(principal=principal, **values)
    return service.record_seen(principal=principal)


def _card_payload(card: DiscoveryCard) -> dict[str, Any]:
    return {
        "id": card.upstream_uid,
        "uid": card.upstream_uid,
        "user_id": card.upstream_uid,
        "canonical_user_id": str(card.user_id),
        "nickname": card.display_name,
        "name": card.display_name,
        "city": card.city_name,
        "city_code": card.city_code,
        "gender": _gender_label(card.gender),
        "sex": _gender_label(card.gender),
        "property": card.profile_property or "",
        "age": card.age,
        "online": "在线" if card.online else "离线",
        "is_online": card.online,
        "source": DISCOVERY_PROVIDER,
    }


def _profile_payload(
    *,
    principal: DiscoveryPrincipal,
    display_name: str,
    profile: DiscoveryProfile,
    display: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": principal.upstream_uid,
        "uid": principal.upstream_uid,
        "user_id": principal.upstream_uid,
        "canonical_user_id": str(principal.user_id),
        "nickname": display_name,
        "name": display_name,
        "city": profile.city_name or "",
        "city_code": profile.city_code or "",
        "gender": _gender_label(profile.gender),
        "sex": _gender_label(profile.gender),
        "property": profile.profile_property or "",
        "age": profile.age,
        "online": "在线",
        "is_online": True,
        "source": DISCOVERY_PROVIDER,
    }
    if isinstance(display, Mapping):
        payload.update(
            {
                key: display[key]
                for key in (
                    "avatar",
                    "portrait",
                    "signature",
                    "is_realname",
                    "money",
                    "vip",
                    "svip",
                    "user_role",
                    "rp_verify_time",
                    "logged_in",
                )
                if key in display
            }
        )
    return payload


def _queue_payload(outcome: TextMatchOutcome | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    queue = outcome.queue_entry
    return {
        "id": queue.public_id,
        "request_id": queue.request_id,
        "status": queue.status,
        "city_code": queue.city_code,
        "preference_version": queue.preference_version,
        "enqueued_at": queue.enqueued_at.isoformat(),
        "expires_at": queue.expires_at.isoformat(),
        "matched_at": queue.matched_at.isoformat() if queue.matched_at else None,
    }


def _preference_payload(preference: MatchPreference | None) -> dict[str, Any]:
    if preference is None:
        return {
            "city_scope": "anywhere",
            "gender": "不限",
            "property": "双",
            "properties": ["双"],
            "min_age": 18,
            "max_age": 120,
            "enabled": True,
            "version": 0,
        }
    property_value = preference.property_preference or "双"
    return {
        "city_scope": preference.city_scope,
        "gender": _gender_label(preference.gender_preference),
        "property": property_value,
        "properties": [property_value],
        "min_age": preference.min_age,
        "max_age": preference.max_age,
        "enabled": preference.enabled,
        "version": preference.version,
    }


def _list_payload(
    result: DiscoveryList,
    *,
    capabilities: Mapping[str, bool],
    nearby: bool,
) -> NativeDiscoveryResponse:
    items = [_card_payload(card) for card in result.items]
    filters = {
        "gender": _gender_label(result.filters.gender),
        "property": result.filters.profile_property or "不限",
        "age": next(
            (
                label
                for label, bounds in DISCOVERY_AGES.items()
                if bounds == (result.filters.min_age, result.filters.max_age)
            ),
            f"{result.filters.min_age}-{result.filters.max_age}",
        ),
    }
    payload: dict[str, Any] = {
        "ok": True,
        "items": items,
        "list": items,
        "count": len(items),
        "source_count": result.scanned_count,
        "filters": filters,
        "capabilities": dict(capabilities),
        "source": DISCOVERY_PROVIDER,
        "canonical": True,
        "external_dependency": False,
        "location_precision": "city",
    }
    if nearby and result.filters.city_code and result.filters.city_name:
        payload["location"] = {
            "mode": "city",
            "city": result.filters.city_name,
            "city_code": result.filters.city_code,
            "label": f"当前城市：{result.filters.city_name}",
            "precision": "city",
        }
    return _response(payload)


def _status_payload(
    *,
    identity: Any,
    principal: DiscoveryPrincipal,
    store: SqlAlchemyDiscoveryStore,
    profile: DiscoveryProfile,
    at: datetime,
) -> NativeDiscoveryResponse:
    account = store.resolve_principal(principal)
    if account is None:
        raise DiscoveryIdentityUnavailable("当前 Web 账号不可用")
    user_display = store.canonical_user_display(principal) or {}
    preference = store.get_match_preference(principal.user_id)
    frequency = store.match_frequency_status(
        user_id=principal.user_id,
        at=at,
        limit=TEXT_MATCH_RATE_LIMIT,
        window_seconds=TEXT_MATCH_RATE_WINDOW_SECONDS,
    )
    waiting = store.current_waiting_outcome(user_id=principal.user_id, at=at)
    latest = store.latest_match_outcome(user_id=principal.user_id)
    latest_item = _card_payload(latest.peer) if latest and latest.peer else None
    remaining = frequency["remaining"]
    filters = _preference_payload(preference)
    status = {
        "online_free": remaining,
        "local_free": remaining,
        "rate_limit": frequency,
        "rate_limit_scope": "shared_text_match",
        "waiting": _queue_payload(waiting),
        "latest_match": (
            {
                "id": latest.match_result.public_id,
                "request_id": latest.request_id,
                "matched_at": latest.match_result.matched_at.isoformat(),
                "peer": latest_item,
            }
            if latest and latest.match_result and latest_item
            else None
        ),
        "display": {
            "online": str(remaining),
            "local": str(remaining),
            "card": "—",
            "money": str(user_display.get("money") or "0"),
        },
        "filters": filters,
    }
    return _response(
        {
            "ok": True,
            "user": _profile_payload(
                principal=principal,
                display_name=account.display_name,
                profile=profile,
                display=user_display,
            ),
            "status": status,
            "display": status["display"],
            "filters": filters,
            "waiting": status["waiting"],
            "latest_match": status["latest_match"],
            "capabilities": _capabilities(identity),
            "source": DISCOVERY_PROVIDER,
            "canonical": True,
            "external_dependency": False,
            "location_precision": "city",
            "voice_quota_available": False,
        }
    )


def _dispatch_get(
    *,
    identity: Any,
    principal: DiscoveryPrincipal,
    store: SqlAlchemyDiscoveryStore,
    service: DiscoveryNativeService,
    path: str,
    query: Mapping[str, Any],
) -> NativeDiscoveryResponse:
    profile = _profile_sync(service=service, store=store, principal=principal)
    now = _now()
    if path == "/api/match/status":
        return _status_payload(
            identity=identity,
            principal=principal,
            store=store,
            profile=profile,
            at=now,
        )

    gender = _gender(_query_first(query, "gender", default="不限"))
    property_value = _property(
        _query_first(query, "property", "profile_property", default="不限"),
        optional=True,
    )
    minimum, maximum = _age_range(
        _query_first(query, "age", default="不限"), query
    )
    limit = _strict_limit(_query_first(query, "limit", default="50"))
    capabilities = _capabilities(identity)
    if path == "/api/match/online-users":
        result = service.online_users(
            principal=principal,
            gender=gender,
            profile_property=property_value,
            min_age=minimum,
            max_age=maximum,
            limit=limit,
        )
        return _list_payload(
            result,
            capabilities=capabilities,
            nearby=False,
        )

    custom_city = _query_first(query, "city", "city_name", default="").strip()
    city_code: str | None = None
    city_name: str | None = None
    if custom_city:
        if not capabilities["nearby_custom_city"]:
            return _response(
                {
                    "ok": False,
                    "code": "CUSTOM_CITY_PERMISSION_REQUIRED",
                    "error": "自定义城市筛选需要管理员授权",
                    "capabilities": capabilities,
                    "source": DISCOVERY_PROVIDER,
                },
                403,
            )
        if len(custom_city) > 80 or any(
            ord(char) < 32 or ord(char) == 127 for char in custom_city
        ):
            raise InvalidDiscoveryRequest("城市名称无效")
        city_name = " ".join(custom_city.split())
        city_code = normalized_city_code(city_name)
    result = service.nearby_users(
        principal=principal,
        city_code=city_code,
        city_name=city_name,
        gender=gender,
        profile_property=property_value,
        min_age=minimum,
        max_age=maximum,
        limit=limit,
    )
    return _list_payload(result, capabilities=capabilities, nearby=True)


def _requested_properties(
    body: Mapping[str, Any], current: MatchPreference | None
) -> list[str]:
    raw = body.get("properties")
    values: Sequence[object]
    if isinstance(raw, (list, tuple)):
        values = raw
    elif raw is not None:
        values = str(raw).split(",")
    elif "property" in body:
        values = (body.get("property"),)
    elif current is not None and current.property_preference:
        values = (current.property_preference,)
    else:
        values = ("双",)
    selected = {_property(value, optional=False) for value in values}
    return [value for value in MATCH_PROPERTIES if value in selected]


def _active_property(
    properties: Sequence[str], current: MatchPreference | None
) -> str:
    if not properties:
        raise InvalidDiscoveryRequest("请至少选择一个匹配属性")
    if current is None or current.property_preference not in properties:
        return properties[0]
    index = properties.index(current.property_preference)
    return properties[(index + 1) % len(properties)] if len(properties) > 1 else properties[0]


def _match_payload(
    outcome: TextMatchOutcome,
    *,
    filters: Mapping[str, Any],
    active_property: str,
) -> NativeDiscoveryResponse:
    matched = outcome.status == QUEUE_STATUS_MATCHED and outcome.peer is not None
    items = [_card_payload(outcome.peer)] if matched else []
    peer_uid = outcome.peer.upstream_uid if matched else ""
    return _response(
        {
            "ok": True,
            "items": items,
            "list": items,
            "count": len(items),
            "target": items[0] if items else None,
            "message_peers": [peer_uid] if peer_uid else [],
            "status": outcome.status,
            "waiting": outcome.status != QUEUE_STATUS_MATCHED,
            "matched": matched,
            "request_id": outcome.request_id,
            "queue": _queue_payload(outcome),
            "match_result_id": (
                outcome.match_result.public_id if outcome.match_result else None
            ),
            "matched_at": (
                outcome.match_result.matched_at.isoformat()
                if outcome.match_result
                else None
            ),
            "filters": dict(filters),
            "active_property": active_property,
            "filter_strategy": (
                "round_robin"
                if len(list(filters.get("properties") or [])) > 1
                else "single"
            ),
            "idempotent_replay": not outcome.created,
            # ``None`` means no match has happened yet.  A waiting queue entry
            # must not be reported as a failed attempt to save match history.
            "history_saved": bool(outcome.match_result) if matched else None,
            "canonical_result_saved": bool(outcome.match_result),
            "message": "匹配成功" if matched else "已进入本地匹配队列",
            "source": DISCOVERY_PROVIDER,
            "canonical": True,
            "external_dependency": False,
            "compatibility_sync": "not_required",
            "location_precision": "city",
        }
    )


def _dispatch_post(
    *,
    principal: DiscoveryPrincipal,
    store: SqlAlchemyDiscoveryStore,
    service: DiscoveryNativeService,
    path: str,
    body: Mapping[str, Any],
) -> NativeDiscoveryResponse:
    _profile_sync(service=service, store=store, principal=principal)
    request_id = _request_id(body)
    existing = store.get_text_match_outcome(
        user_id=principal.user_id,
        request_id=request_id,
    )
    if existing is not None:
        current = store.get_match_preference(principal.user_id)
        if current is None:
            raise MatchRequestConflict("request_id 对应的匹配偏好已不可用")
        expected_scope = (
            "same-city" if path == "/api/match/local" else "anywhere"
        )
        properties = _requested_properties(body, current)
        if (
            current.city_scope != expected_scope
            or current.property_preference not in properties
        ):
            raise MatchRequestConflict("request_id 已绑定其他匹配参数")
        if "gender" in body and _gender(
            body.get("gender"), preference=True
        ) != current.gender_preference:
            raise MatchRequestConflict("request_id 已绑定其他匹配参数")
        for field, expected in (
            ("min_age", current.min_age),
            ("max_age", current.max_age),
        ):
            if field in body and str(body.get(field)).strip() != str(expected):
                raise MatchRequestConflict("request_id 已绑定其他匹配参数")
        outcome = service.request_text_match(
            principal=principal,
            request_id=request_id,
        )
        filters = _preference_payload(current)
        filters["properties"] = properties
        return _match_payload(
            outcome,
            filters=filters,
            active_property=str(current.property_preference),
        )

    current = store.get_match_preference(principal.user_id)
    properties = _requested_properties(body, current)
    active_property = _active_property(properties, current)
    raw_gender = body.get("gender")
    if raw_gender is None:
        raw_gender = (
            _gender_label(current.gender_preference) if current is not None else "不限"
        )
    gender = _gender(raw_gender, preference=True)
    minimum = body.get("min_age", current.min_age if current is not None else 18)
    maximum = body.get("max_age", current.max_age if current is not None else 120)
    preference = service.set_match_preference(
        principal=principal,
        city_scope="same-city" if path == "/api/match/local" else "anywhere",
        gender_preference=gender,
        property_preference=active_property,
        min_age=minimum,
        max_age=maximum,
        enabled=True,
        expected_version=current.version if current is not None else 0,
    )
    outcome = service.request_text_match(
        principal=principal,
        request_id=request_id,
    )
    filters = _preference_payload(preference)
    filters["properties"] = properties
    return _match_payload(
        outcome,
        filters=filters,
        active_property=active_property,
    )


def _error(exc: DiscoveryNativeError) -> NativeDiscoveryResponse:
    if isinstance(exc, DiscoveryLocationRequired):
        return _response(
            {
                "ok": False,
                "code": exc.code,
                "error": {
                    "title": "需要城市资料",
                    "detail": "本地附近功能仅使用账号资料中的城市，不保存或使用经纬度。",
                    "code": exc.code,
                },
                "items": [],
                "list": [],
                "count": 0,
                "location_required": True,
                "location_precision": "city",
                "source": DISCOVERY_PROVIDER,
            }
        )
    if isinstance(exc, DiscoveryIdentityUnavailable):
        status = 401
    elif isinstance(exc, MatchRateLimited):
        status = 429
    elif exc.code in {
        "match_preference_conflict",
        "match_request_conflict",
        "discovery_profile_unavailable",
        "match_preference_unavailable",
    }:
        status = 409
    else:
        status = 400
    payload: dict[str, Any] = {
        "ok": False,
        "code": exc.code,
        "error": str(exc),
        "source": DISCOVERY_PROVIDER,
        "canonical": True,
        "external_dependency": False,
    }
    if isinstance(exc, MatchRateLimited):
        payload["retry_after_seconds"] = exc.retry_after_seconds
    return _response(payload, status)


def dispatch_discovery_native(
    identity: Any,
    method: object,
    path: object,
    query: Mapping[str, Any] | None,
    body: Mapping[str, Any] | None,
    *,
    db: Any | None = None,
) -> NativeDiscoveryResponse | None:
    """Handle Web-local discovery routes; return ``None`` for other paths.

    The ASGI caller must authenticate the session before dispatch and must run
    JSON content-type, Origin and Sec-Fetch-Site checks for every path in
    ``DISCOVERY_NATIVE_WRITE_PATHS``.
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
                "source": DISCOVERY_PROVIDER,
            },
            405,
        )
    try:
        principal = _principal(identity)
        with (nullcontext(db) if db is not None else session_scope()) as action_db:
            store = SqlAlchemyDiscoveryStore(action_db)
            service = DiscoveryNativeService(store)
            if normalized_method == "GET":
                return _dispatch_get(
                    identity=identity,
                    principal=principal,
                    store=store,
                    service=service,
                    path=normalized_path,
                    query=query if isinstance(query, Mapping) else {},
                )
            return _dispatch_post(
                principal=principal,
                store=store,
                service=service,
                path=normalized_path,
                body=body if isinstance(body, Mapping) else {},
            )
    except DiscoveryNativeError as exc:
        return _error(exc)


# Keep both natural naming orders available to the ASGI integration layer.
dispatch_native_discovery = dispatch_discovery_native
DISCOVERY_NATIVE_PATHS = HANDLED_PATHS
