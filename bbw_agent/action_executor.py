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
from bbw_prod.models import Relationship, User
from bbw_prod.repositories import OperationOutboxRepository


SEND_PRIVATE_MESSAGE = "send_private_message"
PUBLISH_TEXT_POST = "publish_text_post"
FOLLOW_USER = "follow_user"
UNFOLLOW_USER = "unfollow_user"

SUPPORTED_ACCOUNT_ACTIONS = (
    SEND_PRIVATE_MESSAGE,
    PUBLISH_TEXT_POST,
    FOLLOW_USER,
    UNFOLLOW_USER,
)
SUPPORTED_ACCOUNT_ACTION_SET = frozenset(SUPPORTED_ACCOUNT_ACTIONS)
SUPPORTED_POST_VISIBILITIES = frozenset({"public", "followers", "private"})
MAX_ACTION_TEXT_LENGTH = 2_000


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
    if normalized_action in {SEND_PRIVATE_MESSAGE, FOLLOW_USER, UNFOLLOW_USER}:
        target = normalize_target(
            target_upstream_uid, actor_upstream_uid=identity.upstream_uid
        )
    elif str(target_upstream_uid or "").strip():
        raise AccountActionError(
            "action_parameters_invalid", "发布动态不接受目标用户编号"
        )
    if normalized_action in {SEND_PRIVATE_MESSAGE, PUBLISH_TEXT_POST}:
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
        web_user = persistence.restore_web_user(identity.sid)
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
            "当前账号只启用了 Web 本地通道，无法操作尚未迁移的外部目标",
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
    elif command.action_type in {FOLLOW_USER, UNFOLLOW_USER}:
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
    from bbw_web.messaging import (
        InvalidLocalMessage,
        LocalIdentityUnavailable,
        LocalMessageBlocked,
        LocalMessageForbidden,
        LocalMessageIdempotencyConflict,
        PeerNotMigrated,
    )

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
    try:
        result = persistence.send_local_text_message(
            identity=web_identity,
            peer=target,
            client_message_id=client_message_id,
            text_value=content,
            quote={},
            expected_source_message_identity=expected_source_message_identity,
            db=db,
        )
        return AccountActionResult(
            action_type=SEND_PRIVATE_MESSAGE,
            result_id=str(result.message.id),
            channel="web-local",
            compatibility_sync=str(result.tim_mirror.status or "pending"),
            created=bool(result.created),
            idempotent_replay=not bool(result.created),
        )
    except PeerNotMigrated as exc:
        if not allow_external_fallback:
            raise AccountActionError(
                "external_channel_disabled",
                "无人值守任务只允许使用 Web 本地权威消息通道",
                status_code=409,
            ) from exc
    except LocalMessageBlocked as exc:
        raise AccountActionError(
            "message_target_blocked", str(exc), status_code=403
        ) from exc
    except LocalMessageForbidden as exc:
        raise AccountActionError(
            "message_target_forbidden", str(exc), status_code=403
        ) from exc
    except (InvalidLocalMessage, LocalMessageIdempotencyConflict) as exc:
        raise AccountActionError(
            getattr(exc, "code", "message_invalid"), str(exc), status_code=409
        ) from exc
    except LocalIdentityUnavailable as exc:
        if not allow_external_fallback:
            raise AccountActionError(
                "external_channel_disabled",
                "无人值守任务只允许使用 Web 本地权威消息通道",
                status_code=409,
            ) from exc
        # A provider-backed interactive request may still use the TIM server edge.
    except Exception as exc:
        raise AccountActionError(
            "local_message_unavailable",
            "Web 本地消息服务暂时不可用",
            status_code=503,
        ) from exc

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
            raise AccountActionError(
                "external_message_failed",
                "外部消息通道发送失败",
                status_code=502,
            )
        result_id = _provider_result_id(
            result, "MsgKey", "msg_key", "MsgUID", "msg_uid"
        )
        return AccountActionResult(
            action_type=SEND_PRIVATE_MESSAGE,
            result_id=result_id,
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
    from bbw_web.native_social_api import dispatch_social_native

    action_type = FOLLOW_USER if active else UNFOLLOW_USER
    path = "/api/social/follow" if active else "/api/social/unfollow"
    _ensure_not_blocked(identity.owner_user_id, target)
    response = dispatch_social_native(
        _identity_view(identity),
        "POST",
        path,
        {},
        {"uid": target, "operation_id": domain_idempotency_key(idempotency_key)},
        db=db,
    )
    if response is not None and response.status < 400 and response.payload.get("ok"):
        return AccountActionResult(
            action_type=action_type,
            result_id=target,
            channel="web-local",
            compatibility_sync=str(
                response.payload.get("compatibility_sync") or "pending"
            ),
            changed=bool(response.payload.get("changed")),
            idempotent_replay=bool(response.payload.get("idempotent_replay")),
        )
    code = str((response.payload if response else {}).get("code") or "")
    if code != "SOCIAL_TARGET_UNAVAILABLE":
        status = int(response.status if response is not None else 500)
        public_message = str(
            (response.payload if response else {}).get("error")
            or "社交关系操作失败"
        )
        raise AccountActionError(
            code.lower() or "social_action_failed",
            public_message,
            status_code=status,
        )
    if not allow_external_fallback:
        raise AccountActionError(
            "external_channel_disabled",
            "无人值守任务只允许使用 Web 本地权威社交关系通道",
            status_code=409,
        )

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
            raise AccountActionError(
                "external_social_action_failed",
                "外部社交关系操作失败",
                status_code=502,
            )
        return AccountActionResult(
            action_type=action_type,
            result_id=target,
            channel="external-provider",
            compatibility_sync="not_required",
            changed=True,
        )
    finally:
        _close_restored_web_user(web_user)


def _publish_text_post(
    *,
    identity: ActionIdentity,
    content: str,
    visibility: str,
    idempotency_key: str,
    db: Any | None = None,
) -> AccountActionResult:
    from bbw_web.moments_native import (
        LocalMomentsService,
        SocialContentError,
        SocialPrincipal,
        SqlAlchemyCanonicalSocialStore,
        SqlAlchemySocialPermissionPolicy,
    )

    try:
        with (nullcontext(db) if db is not None else session_scope()) as action_db:
            user = action_db.scalar(
                select(User).where(
                    User.id == identity.owner_user_id,
                    User.status == "active",
                    User.disabled_at.is_(None),
                )
            )
            if user is None:
                raise AccountActionError(
                    "account_unavailable", "当前账号已不可用", status_code=401
                )
            principal = SocialPrincipal(
                user_id=identity.owner_user_id,
                upstream_uid=str(identity.upstream_uid),
                display_name=str(user.display_name or ""),
            )
            result = LocalMomentsService(
                SqlAlchemyCanonicalSocialStore(action_db),
                SqlAlchemySocialPermissionPolicy(action_db),
            ).publish(
                principal=principal,
                client_request_id=domain_idempotency_key(idempotency_key),
                title="",
                body=content,
                media={},
                visibility=visibility,
                comment_policy="open",
                hide_comments=False,
                topics=(),
            )
            mirror = result.mirror
            compatibility_sync = "not_required"
            if mirror is not None:
                outbox, _outbox_created = OperationOutboxRepository(
                    action_db
                ).enqueue(
                    owner_user_id=principal.user_id,
                    operation_type=f"compatibility.{mirror.operation_type}"[:96],
                    aggregate_type=mirror.aggregate_type[:80],
                    aggregate_id=mirror.aggregate_public_id[:128],
                    idempotency_key=mirror.idempotency_key[:160],
                    payload={
                        "authority": "web-local",
                        "aggregate_public_id": mirror.aggregate_public_id,
                        "operation": mirror.operation_type,
                        "payload": mirror.payload,
                        "schema": 1,
                    },
                    status="pending",
                )
                compatibility_sync = str(outbox.status or "pending")
            return AccountActionResult(
                action_type=PUBLISH_TEXT_POST,
                result_id=result.post.public_id,
                channel="web-local",
                compatibility_sync=compatibility_sync,
                created=bool(result.created),
                idempotent_replay=not bool(result.created),
            )
    except AccountActionError:
        raise
    except SocialContentError as exc:
        raise AccountActionError(
            getattr(exc, "code", "social_content_invalid"),
            str(exc),
            status_code=409 if "conflict" in getattr(exc, "code", "") else 400,
        ) from exc


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
            content=command.content,
            visibility=command.visibility,
            idempotency_key=command.idempotency_key,
            db=db,
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
