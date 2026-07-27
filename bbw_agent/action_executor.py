"""Fixed, audited social-account actions for the built-in BYOK runner.

The model never receives a browser cookie, provider token, arbitrary URL or
generic HTTP client.  This module exposes only the small action vocabulary
listed in :data:`SUPPORTED_ACCOUNT_ACTIONS` and reuses the Web application's
existing owner-scoped services.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from sqlalchemy import select

from bbw_prod.db import session_scope
from bbw_prod.models import Relationship


SEND_PRIVATE_MESSAGE = "send_private_message"
PUBLISH_TEXT_POST = "publish_text_post"
FOLLOW_USER = "follow_user"
UNFOLLOW_USER = "unfollow_user"
BROWSE_ONLINE_USERS = "browse_online_users"
REQUEST_TEXT_MATCH = "request_text_match"
REQUEST_FRIEND = "request_friend"

SUPPORTED_ACCOUNT_ACTIONS = (
    SEND_PRIVATE_MESSAGE,
    PUBLISH_TEXT_POST,
    FOLLOW_USER,
    UNFOLLOW_USER,
    BROWSE_ONLINE_USERS,
    REQUEST_TEXT_MATCH,
    REQUEST_FRIEND,
)
SUPPORTED_ACCOUNT_ACTION_SET = frozenset(SUPPORTED_ACCOUNT_ACTIONS)
SUPPORTED_POST_VISIBILITIES = frozenset({"public", "followers", "private"})
MAX_ACTION_TEXT_LENGTH = 2_000
MAX_FRIEND_REQUEST_TEXT_LENGTH = 200
DEFAULT_FRIEND_REQUEST_TEXT = "你好，想和你认识一下"
TARGET_ACCOUNT_ACTIONS = frozenset(
    {SEND_PRIVATE_MESSAGE, FOLLOW_USER, UNFOLLOW_USER, REQUEST_FRIEND}
)
CONTENT_ACCOUNT_ACTIONS = frozenset(
    {SEND_PRIVATE_MESSAGE, PUBLISH_TEXT_POST, REQUEST_FRIEND}
)


class ActionIdentity(Protocol):
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    match_pool_online_list_enabled: bool
    sid: str


class AccountActionError(RuntimeError):
    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        status_code: int = 400,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(public_message)
        self.code = str(code or "account_action_failed")[:64]
        self.public_message = str(public_message or "账号操作失败")[:240]
        self.status_code = int(status_code)
        self.outcome_unknown = bool(outcome_unknown)


@dataclass(frozen=True, slots=True)
class AccountActionResult:
    action_type: str
    result_id: str
    channel: str
    compatibility_sync: str
    created: bool | None = None
    changed: bool | None = None
    idempotent_replay: bool = False

    def public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action_type,
            "executed": True,
            "result_id": self.result_id,
            "channel": self.channel,
            "compatibility_sync": self.compatibility_sync,
            "idempotent_replay": self.idempotent_replay,
        }
        if self.created is not None:
            payload["created"] = self.created
        if self.changed is not None:
            payload["changed"] = self.changed
        return payload


@dataclass(frozen=True, slots=True)
class AccountActionCommand:
    action_type: str
    target_upstream_uid: str
    content: str
    visibility: str
    idempotency_key: str


def normalize_action_type(value: object) -> str:
    action_type = str(value or "").strip().lower()
    if action_type not in SUPPORTED_ACCOUNT_ACTION_SET:
        raise AccountActionError("action_not_supported", "该账号操作不受支持")
    return action_type


def normalize_target(value: object, *, actor_upstream_uid: object) -> str:
    target = str(value or "").strip()
    actor = str(actor_upstream_uid or "").strip()
    if (
        not target
        or len(target) > 128
        or any(ord(char) < 33 for char in target)
    ):
        raise AccountActionError("action_target_invalid", "目标用户编号无效")
    if target == actor:
        raise AccountActionError("action_target_self", "不能对自己的账号执行该操作")
    return target


def normalize_content(value: object) -> str:
    content = str(value or "").strip()
    if not content:
        raise AccountActionError("action_content_empty", "操作内容不能为空")
    if len(content) > MAX_ACTION_TEXT_LENGTH:
        raise AccountActionError(
            "action_content_too_long", "操作内容不能超过两千字"
        )
    return content


def normalize_visibility(value: object) -> str:
    visibility = str(value or "public").strip().lower()
    if visibility not in SUPPORTED_POST_VISIBILITIES:
        raise AccountActionError("visibility_invalid", "动态可见范围无效")
    return visibility


def parameter_snapshot(
    *,
    action_type: str,
    target_upstream_uid: str,
    content: str,
    visibility: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return non-duplicating audit snapshots for an execution row.

    Message/post bodies remain in their canonical domain tables.  The action
    audit stores only a digest and character count, so it does not create a
    second plaintext copy of user content.
    """

    target_snapshot = {"upstream_uid": target_upstream_uid} if target_upstream_uid else {}
    parameters: dict[str, Any] = {"schema": 1}
    if content:
        parameters.update(
            {
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "content_char_count": len(content),
            }
        )
    if action_type == PUBLISH_TEXT_POST:
        parameters["visibility"] = visibility
    return target_snapshot, parameters


def domain_idempotency_key(value: object) -> str:
    """Derive a short collision-resistant key for downstream domain tables."""

    normalized = str(value or "").strip()
    return "agent:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_account_action(
    *,
    identity: ActionIdentity,
    action_type: object,
    target_upstream_uid: object = "",
    content: object = "",
    visibility: object = "public",
    idempotency_key: object,
) -> AccountActionCommand:
    normalized_action = normalize_action_type(action_type)
    normalized_key = str(idempotency_key or "").strip()
    if (
        len(normalized_key) < 8
        or len(normalized_key) > 160
        or any(ord(char) < 32 for char in normalized_key)
    ):
        raise AccountActionError("idempotency_key_invalid", "幂等请求标识无效")

    target = ""
    normalized_content = ""
    normalized_visibility = "public"
    if normalized_action in TARGET_ACCOUNT_ACTIONS:
        target = normalize_target(
            target_upstream_uid, actor_upstream_uid=identity.upstream_uid
        )
    elif str(target_upstream_uid or "").strip():
        raise AccountActionError(
            "action_parameters_invalid", "发布动态不接受目标用户编号"
        )
    if normalized_action in CONTENT_ACCOUNT_ACTIONS:
        if normalized_action == REQUEST_FRIEND:
            normalized_content = str(content or "").strip() or DEFAULT_FRIEND_REQUEST_TEXT
            if len(normalized_content) > MAX_FRIEND_REQUEST_TEXT_LENGTH:
                raise AccountActionError(
                    "friend_request_text_too_long",
                    "好友申请内容不能超过两百字",
                )
            if any(
                ord(character) < 32 and character not in {"\n", "\t"}
                for character in normalized_content
            ):
                raise AccountActionError(
                    "friend_request_text_invalid", "好友申请内容包含不允许的字符"
                )
        else:
            normalized_content = normalize_content(content)
    elif str(content or "").strip():
        raise AccountActionError(
            "action_parameters_invalid", "该账号操作不接受文字内容"
        )
    if normalized_action == PUBLISH_TEXT_POST:
        normalized_visibility = normalize_visibility(visibility)
    elif str(visibility or "public").strip().lower() != "public":
        raise AccountActionError(
            "action_parameters_invalid", "该账号操作不接受动态可见范围"
        )
    return AccountActionCommand(
        action_type=normalized_action,
        target_upstream_uid=target,
        content=normalized_content,
        visibility=normalized_visibility,
        idempotency_key=normalized_key,
    )


def _identity_view(identity: ActionIdentity) -> Any:
    """Adapt the agent context to existing duck-typed Web service contracts."""

    return type(
        "AgentActionIdentity",
        (),
        {
            "user_id": identity.owner_user_id,
            "external_account_id": identity.external_account_id,
            "upstream_uid": identity.upstream_uid,
            "match_pool_online_list_enabled": bool(
                identity.match_pool_online_list_enabled
            ),
        },
    )()


def _close_restored_web_user(web_user: Any) -> None:
    try:
        web_user.stop_heartbeat()
    except Exception:
        pass
    try:
        web_user.app.client.close()
    except Exception:
        pass


def _provider_web_user(identity: ActionIdentity, persistence: Any) -> Any:
    try:
        if str(getattr(identity, "sid", "") or ""):
            web_user = persistence.restore_web_user(identity.sid)
        else:
            restore_agent = getattr(persistence, "restore_agent_web_user", None)
            web_user = (
                restore_agent(
                    identity.owner_user_id,
                    identity.external_account_id,
                    expected_upstream_uid=str(identity.upstream_uid or ""),
                )
                if callable(restore_agent)
                else None
            )
    except Exception as exc:
        raise AccountActionError(
            "external_session_unavailable",
            "当前登录账号无法建立外部操作会话",
            status_code=409,
        ) from exc
    if (
        web_user is None
        or str(getattr(web_user, "internal_user_id", "") or "")
        != str(identity.owner_user_id)
        or str(getattr(web_user, "external_account_id", "") or "")
        != str(identity.external_account_id)
        or str(getattr(web_user.app.session, "uid", "") or "").strip()
        != str(identity.upstream_uid or "").strip()
    ):
        if web_user is not None:
            _close_restored_web_user(web_user)
        raise AccountActionError(
            "external_session_unavailable",
            "当前登录账号无法建立外部操作会话",
            status_code=409,
        )
    if str(getattr(web_user, "authentication_source", "") or "") == "local":
        _close_restored_web_user(web_user)
        raise AccountActionError(
            "external_channel_disabled",
            "当前会话不是原账号服务会话，请重新登录后再执行账号操作",
            status_code=409,
        )
    # Account actions must not silently broaden into a credential refresh or
    # password-based login while a final authorization gate is held.
    try:
        web_user.app.client.reauth_callback = None
    except Exception:
        pass
    return web_user


def _provider_result_id(result: Any, *keys: str) -> str:
    data = getattr(result, "data", None)
    if isinstance(data, Mapping):
        for key in keys:
            value = str(data.get(key) or "").strip()
            if value:
                return value[:256]
    return ""


def _provider_outcome_unknown(result: Any) -> bool:
    """Return whether a mutating provider call lacks a reliable outcome."""

    try:
        status = int(getattr(result, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    try:
        error_code = int(getattr(result, "error_code", 0) or 0)
    except (TypeError, ValueError):
        error_code = 0
    code = str(getattr(result, "code", "") or "").strip().upper()
    kind = str(getattr(result, "kind", "") or "").strip().lower()
    return (
        status < 0
        or error_code < 0
        or code in {"EMPTY_RESPONSE", "NULL_RESPONSE"}
        or kind in {"empty", "unknown"}
    )


def _ensure_not_blocked(owner_user_id: uuid.UUID, target: str) -> None:
    with session_scope() as db:
        blocked = db.scalar(
            select(Relationship.id)
            .where(
                Relationship.owner_user_id == owner_user_id,
                Relationship.subject_upstream_uid == target,
                Relationship.kind.in_(("blacklist", "blacklisted_by")),
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
            )
            .limit(1)
        )
    if blocked is not None:
        raise AccountActionError(
            "action_target_blocked", "黑名单关系禁止执行该操作", status_code=403
        )


def preflight_account_action(
    *,
    identity: ActionIdentity,
    persistence: Any,
    command: AccountActionCommand,
) -> None:
    """Run side-effect-free target policy checks before confirmation/model use."""

    if command.action_type == SEND_PRIVATE_MESSAGE:
        try:
            allowed = bool(
                persistence.can_message_peer(
                    _identity_view(identity), command.target_upstream_uid
                )
            )
        except Exception as exc:
            raise AccountActionError(
                "message_policy_unavailable",
                "当前无法校验私信对象权限",
                status_code=503,
            ) from exc
        if not allowed:
            raise AccountActionError(
                "message_target_forbidden",
                "当前账号没有向该用户主动发送私信的权限",
                status_code=403,
            )
    elif command.action_type in {FOLLOW_USER, UNFOLLOW_USER, REQUEST_FRIEND}:
        _ensure_not_blocked(
            identity.owner_user_id, command.target_upstream_uid
        )


def _send_private_message(
    *,
    identity: ActionIdentity,
    persistence: Any,
    target: str,
    content: str,
    idempotency_key: str,
    expected_source_message_identity: str = "",
    db: Any | None = None,
    allow_external_fallback: bool = True,
) -> AccountActionResult:
    del expected_source_message_identity, allow_external_fallback
    web_identity = _identity_view(identity)
    preflight_account_action(
        identity=identity,
        persistence=persistence,
        command=AccountActionCommand(
            action_type=SEND_PRIVATE_MESSAGE,
            target_upstream_uid=target,
            content=content,
            visibility="public",
            idempotency_key=idempotency_key,
        ),
    )

    client_message_id = domain_idempotency_key(idempotency_key)
    web_user = _provider_web_user(identity, persistence)
    try:
        try:
            result = web_user.native.tim_rest.send_text(
                str(identity.upstream_uid),
                target,
                content,
                cloud_custom_data={"origin": "agent"},
                idempotency_key=client_message_id,
            )
        except Exception as exc:
            raise AccountActionError(
                "external_message_outcome_unknown",
                "外部消息通道未返回明确结果，请先检查会话后再决定是否重试",
                status_code=502,
                outcome_unknown=True,
            ) from exc
        if not bool(getattr(result, "ok", False)):
            if _provider_outcome_unknown(result):
                raise AccountActionError(
                    "external_message_outcome_unknown",
                    "外部消息通道未返回明确结果，请先检查会话后再决定是否重试",
                    status_code=502,
                    outcome_unknown=True,
                )
            raise AccountActionError(
                "external_message_failed",
                "外部消息通道发送失败",
                status_code=502,
            )
        result_id = _provider_result_id(
            result, "MsgKey", "msg_key", "MsgUID", "msg_uid"
        )
        archived_result_id = ""
        remember_outgoing = getattr(
            persistence, "remember_agent_external_text_message", None
        )
        if callable(remember_outgoing):
            try:
                archived_result_id = str(
                    remember_outgoing(
                        identity=web_identity,
                        peer_upstream_uid=target,
                        text_value=content,
                        client_message_id=client_message_id,
                        upstream_message_id=result_id,
                        db=db,
                    )
                    or ""
                )
            except Exception as exc:
                raise AccountActionError(
                    "external_message_archive_failed",
                    "消息已发送，但消息记录未能安全保存，请先检查会话",
                    status_code=500,
                    outcome_unknown=True,
                ) from exc
        return AccountActionResult(
            action_type=SEND_PRIVATE_MESSAGE,
            result_id=archived_result_id or result_id or client_message_id,
            channel="tim-rest",
            compatibility_sync="not_required",
            created=True,
        )
    finally:
        _close_restored_web_user(web_user)


def _follow_action(
    *,
    identity: ActionIdentity,
    persistence: Any,
    target: str,
    idempotency_key: str,
    active: bool,
    db: Any | None = None,
    allow_external_fallback: bool = True,
) -> AccountActionResult:
    del idempotency_key, allow_external_fallback
    action_type = FOLLOW_USER if active else UNFOLLOW_USER
    _ensure_not_blocked(identity.owner_user_id, target)
    web_user = _provider_web_user(identity, persistence)
    try:
        try:
            result = (
                web_user.app.social.follow(target)
                if active
                else web_user.app.social.unfollow(target)
            )
        except Exception as exc:
            raise AccountActionError(
                "external_social_outcome_unknown",
                "外部社交服务未返回明确结果，请先检查账号状态后再决定是否重试",
                status_code=502,
                outcome_unknown=True,
            ) from exc
        if not bool(getattr(result, "ok", False)):
            if _provider_outcome_unknown(result):
                raise AccountActionError(
                    "external_social_outcome_unknown",
                    "外部社交服务未返回明确结果，请先检查账号状态后再决定是否重试",
                    status_code=502,
                    outcome_unknown=True,
                )
            raise AccountActionError(
                "external_social_action_failed",
                "外部社交关系操作失败",
                status_code=502,
            )
        archived_result_id = ""
        remember_social = getattr(
            persistence, "remember_agent_external_social_action", None
        )
        if callable(remember_social):
            try:
                archived_result_id = str(
                    remember_social(
                        identity=_identity_view(identity),
                        target_upstream_uid=target,
                        action_type=action_type,
                        db=db,
                    )
                    or ""
                )
            except Exception as exc:
                raise AccountActionError(
                    "external_social_archive_failed",
                    "关系操作已执行，但本地关系记录未能安全保存，请先检查关系状态",
                    status_code=500,
                    outcome_unknown=True,
                ) from exc
        return AccountActionResult(
            action_type=action_type,
            result_id=archived_result_id or target,
            channel="external-provider",
            compatibility_sync="not_required",
            changed=True,
        )
    finally:
        _close_restored_web_user(web_user)


def _candidate_items(value: object, *, actor_upstream_uid: str) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        target = str(
            item.get("uid")
            or item.get("user_id")
            or item.get("id")
            or ""
        ).strip()
        if (
            not target
            or target == str(actor_upstream_uid or "").strip()
            or target in seen
            or len(target) > 128
            or any(ord(character) < 33 for character in target)
        ):
            continue
        item["uid"] = target
        item["id"] = target
        seen.add(target)
        normalized.append(item)
    return normalized[:50]


def _remember_discovery_candidates(
    *,
    identity: ActionIdentity,
    items: list[dict[str, Any]],
    source: str,
    db: Any | None,
) -> None:
    from bbw_agent.repositories import AgentDiscoveryCandidateRepository

    with (nullcontext(db) if db is not None else session_scope()) as action_db:
        AgentDiscoveryCandidateRepository(action_db).remember_many(
            owner_user_id=identity.owner_user_id,
            candidates=items,
            source=source,
        )


def _browse_online_users(
    *,
    identity: ActionIdentity,
    persistence: Any,
    db: Any | None,
    allow_external_fallback: bool,
) -> AccountActionResult:
    del allow_external_fallback
    from bbw_web.normalize import normalize_match_filters, normalize_users

    web_user = _provider_web_user(identity, persistence)
    try:
        raw_user = getattr(web_user.app.session, "raw_user", {}) or {}
        filters = normalize_match_filters(
            raw_user if isinstance(raw_user, dict) else {}
        )
        try:
            result = web_user.app.match.online_users(
                id=web_user.app.session.uid,
                gender=filters["gender"],
                property=filters["property"],
                pageIndex="1",
            )
        except Exception as exc:
            raise AccountActionError(
                "external_discovery_unavailable",
                "在线列表暂时不可用",
                status_code=502,
            ) from exc
        if not bool(getattr(result, "ok", False)):
            raise AccountActionError(
                "external_discovery_failed",
                "原账号服务没有返回可用的在线列表",
                status_code=502,
            )
        items = _candidate_items(
            normalize_users(getattr(result, "data", None)),
            actor_upstream_uid=str(identity.upstream_uid),
        )
    finally:
        _close_restored_web_user(web_user)

    _remember_discovery_candidates(
        identity=identity,
        items=items,
        source="online",
        db=db,
    )
    digest = hashlib.sha256(
        "\n".join(str(item.get("uid") or "") for item in items).encode("utf-8")
    ).hexdigest()[:24]
    return AccountActionResult(
        action_type=BROWSE_ONLINE_USERS,
        result_id=f"browse:{len(items)}:{digest}",
        channel="external-provider",
        compatibility_sync="not_required",
        created=True,
    )


def _request_text_match(
    *,
    identity: ActionIdentity,
    persistence: Any,
    idempotency_key: str,
    db: Any | None,
    allow_external_fallback: bool,
) -> AccountActionResult:
    del allow_external_fallback
    request_id = domain_idempotency_key(idempotency_key)
    web_user = _provider_web_user(identity, persistence)
    try:
        from bbw_web.normalize import normalize_match_filters, normalize_match_result

        raw_user = getattr(web_user.app.session, "raw_user", {}) or {}
        raw_user = raw_user if isinstance(raw_user, dict) else {}
        filters = normalize_match_filters(raw_user)
        try:
            result = web_user.app.match.online_one(
                id=web_user.app.session.uid,
                gender=str(raw_user.get("sex") or ""),
                property=str(raw_user.get("property") or ""),
            )
        except Exception as exc:
            raise AccountActionError(
                "external_match_outcome_unknown",
                "在线匹配结果无法确认，请先检查匹配记录",
                status_code=502,
                outcome_unknown=True,
            ) from exc
        payload = normalize_match_result(result)
        payload["filters"] = {
            "gender": filters["gender"],
            "property": filters["property"],
            "properties": [filters["property"]],
        }
        if not payload.get("ok"):
            if _provider_outcome_unknown(result):
                raise AccountActionError(
                    "external_match_outcome_unknown",
                    "在线匹配结果无法确认，请先检查匹配记录",
                    status_code=502,
                    outcome_unknown=True,
                )
            raise AccountActionError(
                "external_match_failed",
                "在线匹配没有返回成功结果",
                status_code=502,
            )
        items = _candidate_items(
            list(payload.get("items") or []),
            actor_upstream_uid=str(identity.upstream_uid),
        )
        payload["items"] = items
        payload["list"] = items
        payload["message_peers"] = [str(item.get("uid") or "") for item in items]
        _remember_discovery_candidates(
            identity=identity,
            items=items,
            source="match",
            db=db,
        )
        if items:
            persistence.grant_message_peers(
                identity=_identity_view(identity),
                peers=[str(item.get("uid") or "") for item in items],
                kind="match",
                evidence={"source": "agent", "request_id": request_id},
            )
        persistence.remember_match_history_response(
            identity=_identity_view(identity),
            method="POST",
            path="/api/match/online",
            response_data=payload,
            status=200,
            request_id=request_id,
            db=db,
        )
        result_id = str(items[0].get("uid") or "") if items else request_id
        return AccountActionResult(
            action_type=REQUEST_TEXT_MATCH,
            result_id=result_id,
            channel="external-provider",
            compatibility_sync="not_required",
            created=True,
        )
    finally:
        _close_restored_web_user(web_user)


def _request_friend(
    *,
    identity: ActionIdentity,
    persistence: Any,
    target: str,
    content: str,
    idempotency_key: str,
    db: Any | None,
    allow_external_fallback: bool,
) -> AccountActionResult:
    del idempotency_key, allow_external_fallback
    _ensure_not_blocked(identity.owner_user_id, target)
    web_user = _provider_web_user(identity, persistence)
    try:
        try:
            result = web_user.app.social.add_friend(target, content)
        except Exception as exc:
            raise AccountActionError(
                "external_friend_request_outcome_unknown",
                "好友申请结果无法确认，请先检查好友申请记录",
                status_code=502,
                outcome_unknown=True,
            ) from exc
        if not bool(getattr(result, "ok", False)):
            if _provider_outcome_unknown(result):
                raise AccountActionError(
                    "external_friend_request_outcome_unknown",
                    "好友申请结果无法确认，请先检查好友申请记录",
                    status_code=502,
                    outcome_unknown=True,
                )
            raise AccountActionError(
                "external_friend_request_failed",
                "原账号服务未接受好友申请",
                status_code=502,
            )
        archived_result_id = ""
        remember_social = getattr(
            persistence, "remember_agent_external_social_action", None
        )
        if callable(remember_social):
            try:
                archived_result_id = str(
                    remember_social(
                        identity=_identity_view(identity),
                        target_upstream_uid=target,
                        action_type=REQUEST_FRIEND,
                        db=db,
                    )
                    or ""
                )
            except Exception as exc:
                raise AccountActionError(
                    "external_friend_request_archive_failed",
                    "好友申请已发送，但本地申请记录未能安全保存，请先检查关系状态",
                    status_code=500,
                    outcome_unknown=True,
                ) from exc
        return AccountActionResult(
            action_type=REQUEST_FRIEND,
            result_id=archived_result_id or target,
            channel="external-provider",
            compatibility_sync="not_required",
            changed=True,
        )
    finally:
        _close_restored_web_user(web_user)


def _publish_text_post(
    *,
    identity: ActionIdentity,
    persistence: Any,
    content: str,
    visibility: str,
    idempotency_key: str,
    db: Any | None = None,
) -> AccountActionResult:
    del db
    visibility_scope = {
        "public": "公开",
        "followers": "好友及粉丝可见",
        "private": "仅自己可见",
    }[visibility]
    web_user = _provider_web_user(identity, persistence)
    try:
        try:
            result = web_user.app.social.publish_post(
                content,
                visibility_scope=visibility_scope,
            )
        except Exception as exc:
            raise AccountActionError(
                "external_post_outcome_unknown",
                "动态发布结果无法确认，请先检查原账号动态后再决定是否重试",
                status_code=502,
                outcome_unknown=True,
            ) from exc
        if not bool(getattr(result, "ok", False)):
            if _provider_outcome_unknown(result):
                raise AccountActionError(
                    "external_post_outcome_unknown",
                    "动态发布结果无法确认，请先检查原账号动态后再决定是否重试",
                    status_code=502,
                    outcome_unknown=True,
                )
            raise AccountActionError(
                "external_post_failed",
                "原账号服务未接受动态发布请求",
                status_code=502,
            )
        return AccountActionResult(
            action_type=PUBLISH_TEXT_POST,
            result_id=_provider_result_id(result, "postid", "post_id", "id")
            or domain_idempotency_key(idempotency_key),
            channel="external-provider",
            compatibility_sync="not_required",
            created=True,
        )
    finally:
        _close_restored_web_user(web_user)


def execute_account_action(
    *,
    identity: ActionIdentity,
    persistence: Any,
    action_type: object,
    target_upstream_uid: object = "",
    content: object = "",
    visibility: object = "public",
    idempotency_key: object,
    expected_source_message_identity: object = "",
    db: Any | None = None,
    allow_external_fallback: bool = True,
) -> AccountActionResult:
    command = normalize_account_action(
        identity=identity,
        action_type=action_type,
        target_upstream_uid=target_upstream_uid,
        content=content,
        visibility=visibility,
        idempotency_key=idempotency_key,
    )
    if command.action_type == SEND_PRIVATE_MESSAGE:
        return _send_private_message(
            identity=identity,
            persistence=persistence,
            target=command.target_upstream_uid,
            content=command.content,
            idempotency_key=command.idempotency_key,
            expected_source_message_identity=str(
                expected_source_message_identity or ""
            ),
            db=db,
            allow_external_fallback=allow_external_fallback,
        )
    if command.action_type == PUBLISH_TEXT_POST:
        return _publish_text_post(
            identity=identity,
            persistence=persistence,
            content=command.content,
            visibility=command.visibility,
            idempotency_key=command.idempotency_key,
            db=db,
        )
    if command.action_type == BROWSE_ONLINE_USERS:
        return _browse_online_users(
            identity=identity,
            persistence=persistence,
            db=db,
            allow_external_fallback=allow_external_fallback,
        )
    if command.action_type == REQUEST_TEXT_MATCH:
        return _request_text_match(
            identity=identity,
            persistence=persistence,
            idempotency_key=command.idempotency_key,
            db=db,
            allow_external_fallback=allow_external_fallback,
        )
    if command.action_type == REQUEST_FRIEND:
        return _request_friend(
            identity=identity,
            persistence=persistence,
            target=command.target_upstream_uid,
            content=command.content,
            idempotency_key=command.idempotency_key,
            db=db,
            allow_external_fallback=allow_external_fallback,
        )
    return _follow_action(
        identity=identity,
        persistence=persistence,
        target=command.target_upstream_uid,
        idempotency_key=command.idempotency_key,
        active=command.action_type == FOLLOW_USER,
        db=db,
        allow_external_fallback=allow_external_fallback,
    )
