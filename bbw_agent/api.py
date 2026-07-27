"""Authenticated APIs for the administrator-gated BYOK draft runner."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    field_validator,
)

from bbw_prod.db import session_scope
from bbw_prod.services import AuditService

from .action_executor import (
    FOLLOW_USER,
    PUBLISH_TEXT_POST,
    SEND_PRIVATE_MESSAGE,
    UNFOLLOW_USER,
    AccountActionCommand,
    AccountActionError,
    execute_account_action,
    normalize_account_action,
    parameter_snapshot,
    preflight_account_action,
)
from .model_gateway import ModelGatewayError, OpenAICompatibleGateway
from .repositories import (
    AgentActionExecutionRepository,
    AgentAutonomySettingRepository,
    AgentAutonomyTaskRepository,
    AgentExecutionSettingRepository,
    AgentRunRepository,
    AgentSettingRepository,
    ModelConnectionRepository,
    StyleProfileRepository,
)
from .services import (
    AgentServiceError,
    RuntimeConfiguration,
    autonomy_access,
    autonomy_public,
    autonomy_task_public,
    autonomy_usage_today,
    build_reply_draft_plan,
    build_style_analysis_plan,
    connection_public,
    execution_access,
    execution_settings_public,
    load_execution_configuration,
    load_runtime_configuration,
    load_status,
    mark_connection_test,
    parse_style_profile,
    require_visible_access,
    save_agent_settings,
    save_autonomy_settings,
    save_connection,
    save_execution_settings,
    save_style_profile,
    settings_public,
    style_public,
)


_MODEL_CALL_SLOTS = threading.BoundedSemaphore(4)
_ACCOUNT_ACTION_SLOTS = threading.BoundedSemaphore(8)
_ACTION_CONFIRMATION_TTL_SECONDS = 300
_ACTION_CONFIRMATION_CONSUME_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConnectionBody(_StrictBody):
    label: str = Field(default="默认连接", min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=512)
    model: str = Field(min_length=1, max_length=160)
    api_key: SecretStr | None = Field(default=None, repr=False)
    enabled: StrictBool = True

    @field_validator("label", "base_url", "model", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("api_key", mode="before")
    @classmethod
    def empty_key_keeps_existing(cls, value: Any) -> Any:
        return None if value is None or value == "" else value


class AgentSettingsBody(_StrictBody):
    user_enabled: StrictBool = False
    custom_instructions: str = Field(default="", max_length=4000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_output_tokens: int = Field(default=512, ge=64, le=4096)
    context_message_limit: int = Field(default=30, ge=1, le=100)


class IdempotentRunBody(_StrictBody):
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("control characters are not allowed")
        return normalized


class ReplyDraftBody(IdempotentRunBody):
    peer_upstream_uid: str = Field(min_length=1, max_length=128)
    objective: str = Field(default="", max_length=2000)

    @field_validator("peer_upstream_uid", mode="before")
    @classmethod
    def normalize_peer(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("control characters are not allowed")
        return normalized


class ExecutionSettingsBody(_StrictBody):
    user_enabled: StrictBool = False
    auto_send_enabled: StrictBool = False
    selected_actions: list[str] = Field(default_factory=list, max_length=7)

    @field_validator("selected_actions", mode="before")
    @classmethod
    def normalize_selected_actions(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("selected_actions must be an array")
        normalized: list[str] = []
        for item in value:
            action = str(item or "").strip().lower()
            if action and action not in normalized:
                normalized.append(action)
        return normalized


class AutonomySettingsBody(_StrictBody):
    user_enabled: StrictBool = False
    auto_reply_enabled: StrictBool = False
    scheduled_post_enabled: StrictBool = False
    managed_relationships_enabled: StrictBool = False
    discovery_enabled: StrictBool = False
    text_match_enabled: StrictBool = False
    proactive_message_enabled: StrictBool = False
    follow_discovered_enabled: StrictBool = False
    friend_request_enabled: StrictBool = False
    allowed_actions: list[str] = Field(default_factory=list, max_length=7)
    operation_brief: str = Field(default="", max_length=4000)
    managed_target_uids: list[str] = Field(default_factory=list, max_length=100)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    active_start_minute: int = Field(default=0, ge=0, le=1439)
    active_end_minute: int = Field(default=0, ge=0, le=1439)
    minimum_action_interval_seconds: int = Field(default=300, ge=60, le=86400)
    daily_total_limit: int = Field(default=20, ge=1, le=200)
    daily_reply_limit: int = Field(default=10, ge=0, le=200)
    daily_post_limit: int = Field(default=1, ge=0, le=20)
    daily_relationship_limit: int = Field(default=5, ge=0, le=100)
    post_interval_minutes: int = Field(default=1440, ge=60, le=10080)
    discovery_interval_minutes: int = Field(default=30, ge=5, le=1440)
    consecutive_failure_limit: int = Field(default=3, ge=1, le=20)

    @field_validator("allowed_actions", mode="before")
    @classmethod
    def normalize_allowed_actions(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("allowed_actions must be an array")
        normalized: list[str] = []
        for item in value:
            action = str(item or "").strip().lower()
            if action and action not in normalized:
                normalized.append(action)
        return normalized

    @field_validator("managed_target_uids", mode="before")
    @classmethod
    def normalize_managed_targets(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("managed_target_uids must be an array")
        normalized: list[str] = []
        for item in value:
            target = str(item or "").strip()
            if (
                not target
                or len(target) > 128
                or any(ord(character) < 33 for character in target)
                or any(character in target for character in "*?[]")
            ):
                raise ValueError("managed target must be one exact user identifier")
            if target not in normalized:
                normalized.append(target)
        return normalized

    @field_validator("operation_brief", "timezone", mode="before")
    @classmethod
    def strip_autonomy_text(cls, value: Any) -> str:
        return str(value or "").strip()


class AccountActionBody(IdempotentRunBody):
    action: str = Field(min_length=1, max_length=64)
    target_upstream_uid: str = Field(default="", max_length=128)
    content: str = Field(default="", max_length=2000)
    visibility: str = Field(default="public", max_length=16)

    @field_validator("action", "target_upstream_uid", "visibility", mode="before")
    @classmethod
    def strip_action_text(cls, value: Any) -> str:
        return str(value or "").strip()


class ExecuteAccountActionBody(AccountActionBody):
    confirmation_token: SecretStr = Field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentContext:
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    match_pool_online_list_enabled: bool
    client_ip: str
    sid: str = field(repr=False)


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _persistence(request: Request) -> Any:
    return request.app.state.persistence


def _client_ip(request: Request) -> str:
    candidates: list[str] = []
    if bool(getattr(_settings(request), "trust_proxy_headers", False)):
        forwarded = str(request.headers.get("X-Forwarded-For") or "")
        candidates.extend(part.strip() for part in forwarded.split(",") if part.strip())
    if request.client and request.client.host:
        candidates.append(str(request.client.host))
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return "unknown"


def _enforce_same_origin(request: Request, response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="跨站模型请求已被拒绝")
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    origin = str(request.headers.get("Origin") or "").strip()
    host = str(request.headers.get("Host") or "").strip().lower()
    scheme = str(
        request.headers.get("X-Forwarded-Proto") or request.url.scheme
    ).strip().lower()
    try:
        parsed = urlsplit(origin)
    except ValueError:
        parsed = None
    if (
        not origin
        or not host
        or scheme not in {"http", "https"}
        or parsed is None
        or parsed.scheme.lower() != scheme
        or parsed.netloc.lower() != host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (fetch_site and fetch_site != "same-origin")
    ):
        raise HTTPException(status_code=403, detail="跨站模型请求已被拒绝")


def _raise_service_error(exc: AgentServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc


def _gateway_status(exc: ModelGatewayError) -> int:
    if exc.code == "provider_rate_limited":
        return 429
    if exc.code in {
        "api_key_invalid",
        "provider_auth_failed",
        "endpoint_invalid",
        "endpoint_ip_literal",
        "endpoint_not_allowlisted",
        "endpoint_port_not_allowed",
        "model_invalid",
    }:
        return 400
    return 502


def _raise_gateway_error(exc: ModelGatewayError) -> None:
    raise HTTPException(
        status_code=_gateway_status(exc), detail=exc.public_message
    ) from exc


def _agent_context(request: Request) -> AgentContext:
    sid = str(request.cookies.get(str(_settings(request).user_cookie_name)) or "")
    persistence = _persistence(request)
    identity = persistence.require_identity(sid)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not persistence.rate_limit(
        f"ai-agent:access:user:{identity.user_id}",
        limit=180,
        window_seconds=60,
    ):
        raise HTTPException(
            status_code=429,
            detail="模型助手访问过于频繁，请稍后重试",
            headers={"Retry-After": "60"},
        )
    try:
        with session_scope() as db:
            require_visible_access(db, identity.user_id)
    except AgentServiceError as exc:
        _raise_service_error(exc)
    return AgentContext(
        owner_user_id=identity.user_id,
        external_account_id=identity.external_account_id,
        upstream_uid=identity.upstream_uid,
        match_pool_online_list_enabled=bool(
            identity.match_pool_online_list_enabled
        ),
        client_ip=_client_ip(request),
        sid=sid,
    )


def _audit(db: Any, request: Request) -> AuditService:
    persistence = _persistence(request)
    return AuditService(
        db, _settings(request), persistence.session_hmac_key
    )


def _rate_limit(
    request: Request,
    context: AgentContext,
    scope: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    if not _persistence(request).rate_limit(
        f"ai-agent:{scope}:user:{context.owner_user_id}",
        limit=limit,
        window_seconds=window_seconds,
    ):
        raise HTTPException(
            status_code=429,
            detail="模型请求过于频繁，请稍后重试",
            headers={"Retry-After": str(min(window_seconds, 60))},
        )


def _raise_action_error(exc: AccountActionError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc


def _normalized_action(
    body: AccountActionBody, context: AgentContext
) -> AccountActionCommand:
    try:
        return normalize_account_action(
            identity=context,
            action_type=body.action,
            target_upstream_uid=body.target_upstream_uid,
            content=body.content,
            visibility=body.visibility,
            idempotency_key=body.idempotency_key,
        )
    except AccountActionError as exc:
        _raise_action_error(exc)


def _action_scope_rate_limit(
    request: Request,
    context: AgentContext,
    action_type: str,
    *,
    preparing: bool,
) -> None:
    limits = {
        SEND_PRIVATE_MESSAGE: (20, 60),
        PUBLISH_TEXT_POST: (6, 600),
        FOLLOW_USER: (12, 600),
        UNFOLLOW_USER: (12, 600),
    }
    limit, window = limits[action_type]
    if preparing:
        limit *= 2
    _rate_limit(
        request,
        context,
        f"action-{'prepare' if preparing else 'execute'}:{action_type}",
        limit=limit,
        window_seconds=window,
    )


@contextmanager
def _account_action_guard(
    request: Request, context: AgentContext
) -> Iterator[None]:
    lock = _persistence(request).redis.lock(
        f"{_settings(request).redis_prefix}:ai-action:user:{context.owner_user_id}",
        timeout=120,
        blocking_timeout=0,
        thread_local=False,
    )
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有账号操作正在执行，请稍后重试")
    slot = _ACCOUNT_ACTION_SLOTS.acquire(blocking=False)
    if not slot:
        try:
            lock.release()
        except Exception:
            pass
        raise HTTPException(status_code=429, detail="账号执行服务当前繁忙，请稍后重试")
    try:
        yield
    finally:
        _ACCOUNT_ACTION_SLOTS.release()
        try:
            lock.release()
        except Exception:
            pass


def _confirmation_digest(
    request: Request,
    context: AgentContext,
    command: AccountActionCommand,
) -> str:
    payload = json.dumps(
        {
            "owner_user_id": str(context.owner_user_id),
            "session_id": context.sid,
            "action": command.action_type,
            "target_upstream_uid": command.target_upstream_uid,
            "content": command.content,
            "visibility": command.visibility,
            "idempotency_key": command.idempotency_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    raw_key = _persistence(request).session_hmac_key
    key = raw_key if isinstance(raw_key, bytes) else str(raw_key).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _confirmation_key(request: Request, token: str) -> str:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{_settings(request).redis_prefix}:ai-action-confirm:{token_hash}"


def _store_confirmation(
    request: Request,
    context: AgentContext,
    command: AccountActionCommand,
    *,
    execution_version: int,
) -> str:
    value = json.dumps(
        {
            "owner_user_id": str(context.owner_user_id),
            "execution_version": int(execution_version),
            "digest": _confirmation_digest(request, context, command),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    for _ in range(3):
        token = secrets.token_urlsafe(32)
        stored = _persistence(request).redis.set(
            _confirmation_key(request, token),
            value,
            ex=_ACTION_CONFIRMATION_TTL_SECONDS,
            nx=True,
        )
        if stored:
            return token
    raise HTTPException(status_code=503, detail="暂时无法创建操作确认，请稍后重试")


def _load_confirmation(
    request: Request, token: str
) -> tuple[str, dict[str, Any]]:
    normalized = str(token or "").strip()
    if not 32 <= len(normalized) <= 160 or any(ord(char) < 33 for char in normalized):
        raise HTTPException(status_code=409, detail="操作确认已失效，请重新确认")
    key = _confirmation_key(request, normalized)
    raw = _persistence(request).redis.get(key)
    if not raw:
        raise HTTPException(status_code=409, detail="操作确认已失效，请重新确认")
    try:
        text_value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        payload = json.loads(text_value)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail="操作确认已失效，请重新确认") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="操作确认已失效，请重新确认")
    return key, {"raw": text_value, **payload}


def _consume_confirmation(request: Request, key: str, raw_value: str) -> None:
    consumed = _persistence(request).redis.eval(
        _ACTION_CONFIRMATION_CONSUME_SCRIPT,
        1,
        key,
        raw_value,
    )
    if int(consumed or 0) != 1:
        raise HTTPException(status_code=409, detail="操作确认已失效，请重新确认")


def _action_summary(command: AccountActionCommand) -> str:
    if command.action_type == SEND_PRIVATE_MESSAGE:
        return f"向用户 {command.target_upstream_uid} 发送一条私信，共 {len(command.content)} 字"
    if command.action_type == PUBLISH_TEXT_POST:
        visibility = {
            "public": "公开",
            "followers": "好友及粉丝可见",
            "private": "仅自己可见",
        }[command.visibility]
        return f"发布一条{visibility}的文字动态，共 {len(command.content)} 字"
    if command.action_type == FOLLOW_USER:
        return f"关注用户 {command.target_upstream_uid}"
    return f"取消关注用户 {command.target_upstream_uid}"


def _execution_idempotency_key(scope: str, value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"{scope}:{digest}"


def _execution_reuse_payload(row: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "reused": True,
        "execution_id": str(row.id),
        "action": row.action_type,
        "executed": True,
        "result_id": str(row.external_result_id or ""),
        "status": "succeeded",
    }


def _enqueue_started_action(
    db: Any,
    *,
    context: AgentContext,
    command: AccountActionCommand,
    execution_idempotency_key: str,
    execution_version: int,
    approval_source: str,
    trigger_source: str,
) -> tuple[Any, bool]:
    target_snapshot, parameters = parameter_snapshot(
        action_type=command.action_type,
        target_upstream_uid=command.target_upstream_uid,
        content=command.content,
        visibility=command.visibility,
    )
    executions = AgentActionExecutionRepository(db)
    row, created = executions.enqueue(
        owner_user_id=context.owner_user_id,
        external_account_id=context.external_account_id,
        action_type=command.action_type,
        idempotency_key=execution_idempotency_key,
        execution_setting_version=execution_version,
        target_snapshot=target_snapshot,
        parameter_snapshot=parameters,
        approval_source=approval_source,
        trigger_source=trigger_source,
    )
    if (
        dict(row.target_snapshot or {}) != target_snapshot
        or dict(row.parameter_snapshot or {}) != parameters
    ):
        raise AgentServiceError(
            "idempotency_conflict",
            "相同请求标识已用于不同的账号操作",
            status_code=409,
        )
    if not created:
        if row.status == "succeeded":
            return row, True
        if row.status != "queued":
            raise AgentServiceError(
                "idempotency_conflict",
                "相同账号操作已经处理或仍在执行，请勿重复提交",
                status_code=409,
            )
        if int(row.execution_setting_version) != int(execution_version):
            executions.cancel_queued(
                context.owner_user_id,
                row.id,
                stable_error_code="execution_state_changed",
            )
            raise AgentServiceError(
                "execution_state_changed",
                "账号执行设置已变化，请重新确认操作",
                status_code=409,
            )
    started = executions.start(context.owner_user_id, row.id)
    if started is None:
        raise AgentServiceError(
            "action_start_cancelled",
            "账号操作在开始前已被取消",
            status_code=409,
        )
    return started, False


def _dispatch_action_with_final_gate(
    request: Request,
    context: AgentContext,
    command: AccountActionCommand,
    *,
    execution_version: int,
    execution_idempotency_key: str,
    require_auto_send: bool,
) -> Any:
    """Hold administrator/user gate rows through the side-effect dispatch."""

    with session_scope() as db:
        load_execution_configuration(
            db,
            owner_user_id=context.owner_user_id,
            action_type=command.action_type,
            require_auto_send=require_auto_send,
            expected_version=execution_version,
            target_upstream_uid=command.target_upstream_uid,
            for_update=True,
        )
        return execute_account_action(
            identity=context,
            persistence=_persistence(request),
            action_type=command.action_type,
            target_upstream_uid=command.target_upstream_uid,
            content=command.content,
            visibility=command.visibility,
            idempotency_key=execution_idempotency_key,
            db=db,
        )


def _finish_action_success(
    request: Request,
    context: AgentContext,
    execution_id: uuid.UUID,
    result: Any,
    *,
    audit_action: str = "ai.account_action_executed",
) -> None:
    with session_scope() as db:
        finished = AgentActionExecutionRepository(db).succeed(
            context.owner_user_id,
            execution_id,
            external_result_id=result.result_id or None,
        )
        if finished is None:
            raise AgentServiceError(
                "execution_audit_lost",
                "账号操作已经返回结果，但执行审计状态异常，请联系管理员核查",
                status_code=500,
            )
        _audit(db, request).record(
            actor_type="user",
            action=audit_action,
            target_user_id=context.owner_user_id,
            resource_type="ai_agent_action_execution",
            resource_id=str(execution_id),
            client_ip=context.client_ip,
            details={
                "action_type": result.action_type,
                "status": "succeeded",
                "channel": result.channel,
                "compatibility_sync": result.compatibility_sync,
                "created": result.created,
                "changed": result.changed,
            },
        )


def _finish_action_failure(
    request: Request,
    context: AgentContext,
    execution_id: uuid.UUID,
    exc: AccountActionError,
) -> None:
    with session_scope() as db:
        executions = AgentActionExecutionRepository(db)
        finished = (
            executions.mark_outcome_unknown(
                context.owner_user_id,
                execution_id,
                stable_error_code=exc.code,
            )
            if exc.outcome_unknown
            else executions.fail(
                context.owner_user_id,
                execution_id,
                stable_error_code=exc.code,
            )
        )
        if finished is None:
            raise AgentServiceError(
                "execution_audit_lost",
                "账号操作结果审计状态异常，请联系管理员核查",
                status_code=500,
            )
        _audit(db, request).record(
            actor_type="user",
            action="ai.account_action_executed",
            target_user_id=context.owner_user_id,
            resource_type="ai_agent_action_execution",
            resource_id=str(execution_id),
            client_ip=context.client_ip,
            details={
                "status": (
                    "manual_review" if exc.outcome_unknown else "failed"
                ),
                "failure_code": exc.code,
                "outcome_unknown": exc.outcome_unknown,
            },
        )


@contextmanager
def _model_run_guard(
    request: Request, context: AgentContext
) -> Iterator[None]:
    settings = _settings(request)
    connect_timeout = int(
        getattr(settings, "ai_byok_connect_timeout_seconds", 10)
    )
    read_timeout = int(getattr(settings, "ai_byok_read_timeout_seconds", 60))
    timeout = connect_timeout + (2 * read_timeout) + 60
    lock = _persistence(request).redis.lock(
        f"{_settings(request).redis_prefix}:ai-run:user:{context.owner_user_id}",
        timeout=timeout,
        blocking_timeout=0,
        thread_local=False,
    )
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有模型任务正在运行，请稍后重试")
    slot = _MODEL_CALL_SLOTS.acquire(blocking=False)
    if not slot:
        try:
            lock.release()
        except Exception:
            pass
        raise HTTPException(status_code=429, detail="模型运行器当前繁忙，请稍后重试")
    try:
        yield
    finally:
        _MODEL_CALL_SLOTS.release()
        try:
            lock.release()
        except Exception:
            pass


def _runtime_still_enabled(
    db: Any,
    *,
    owner_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    settings_version: int,
    require_user_enabled: bool,
) -> None:
    require_visible_access(db, owner_user_id)
    settings = AgentSettingRepository(db).get(owner_user_id, for_update=True)
    if (
        settings is None
        or settings.active_connection_id != connection_id
        or int(settings.version) != int(settings_version)
        or (require_user_enabled and not settings.user_enabled)
    ):
        raise AgentServiceError(
            "runner_state_changed",
            "模型运行状态已变化，本次结果已取消",
            status_code=409,
        )
    connection = ModelConnectionRepository(db).get(owner_user_id, connection_id)
    if (
        connection is None
        or not connection.enabled
        or (
            require_user_enabled
            and str(connection.last_test_status or "") != "ok"
        )
    ):
        raise AgentServiceError(
            "connection_disabled",
            "模型连接已关闭，本次结果已取消",
            status_code=409,
        )


router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    dependencies=[Depends(_enforce_same_origin)],
)


@router.get("/status")
def agent_status(
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "status", limit=120, window_seconds=60)
    try:
        with session_scope() as db:
            status = load_status(
                db,
                context.owner_user_id,
                background_enabled=bool(
                    getattr(_settings(request), "ai_agent_background_enabled", False)
                ),
            )
    except AgentServiceError as exc:
        _raise_service_error(exc)
    return {"ok": True, **status}


@router.put("/connection")
def update_connection(
    body: ModelConnectionBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "connection-update", limit=20, window_seconds=60)
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    try:
        with session_scope() as db:
            row, created, key_changed = save_connection(
                db,
                owner_user_id=context.owner_user_id,
                settings=_settings(request),
                cipher=_persistence(request).cipher,
                label=body.label,
                base_url=body.base_url,
                model=body.model,
                api_key=api_key,
                enabled=body.enabled,
            )
            execution_disabled = False
            cancelled_actions = 0
            autonomy_disabled = False
            cancelled_autonomy_tasks = 0
            if not row.enabled:
                execution_disabled = AgentExecutionSettingRepository(
                    db
                ).disable_for_owner(context.owner_user_id)
                cancelled_actions = AgentActionExecutionRepository(
                    db
                ).cancel_active_for_owner(
                    context.owner_user_id,
                    stable_error_code="connection_disabled",
                )
                autonomy_disabled = AgentAutonomySettingRepository(
                    db
                ).disable_for_owner(
                    context.owner_user_id,
                    reason="connection_disabled",
                )
                cancelled_autonomy_tasks = AgentAutonomyTaskRepository(
                    db
                ).cancel_not_started_for_owner(
                    context.owner_user_id,
                    stable_error_code="connection_disabled",
                )
            _audit(db, request).record(
                actor_type="user",
                action="ai.model_connection_saved",
                target_user_id=context.owner_user_id,
                resource_type="ai_model_connection",
                resource_id=str(row.id),
                client_ip=context.client_ip,
                details={
                    "created": created,
                    "key_changed": key_changed,
                    "enabled": bool(row.enabled),
                    "provider": row.provider,
                    "host": urlsplit(row.base_url).hostname or "",
                    "execution_disabled": execution_disabled,
                    "cancelled_queued_actions": cancelled_actions,
                    "autonomy_disabled": autonomy_disabled,
                    "cancelled_autonomy_tasks": cancelled_autonomy_tasks,
                },
            )
            result = connection_public(row)
    except AgentServiceError as exc:
        _raise_service_error(exc)
    finally:
        api_key = None
    return {"ok": True, "connection": result}


@router.put("/settings")
def update_agent_settings(
    body: AgentSettingsBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "settings-update", limit=30, window_seconds=60)
    try:
        with session_scope() as db:
            row, connection = save_agent_settings(
                db,
                owner_user_id=context.owner_user_id,
                user_enabled=body.user_enabled,
                custom_instructions=body.custom_instructions,
                temperature=body.temperature,
                max_output_tokens=body.max_output_tokens,
                context_message_limit=body.context_message_limit,
            )
            cancelled = (
                0
                if row.user_enabled
                else AgentRunRepository(db).cancel_active_for_owner(
                    context.owner_user_id
                )
            )
            execution_disabled = False
            cancelled_actions = 0
            autonomy_disabled = False
            cancelled_autonomy_tasks = 0
            if not row.user_enabled:
                execution_disabled = AgentExecutionSettingRepository(
                    db
                ).disable_for_owner(context.owner_user_id)
                cancelled_actions = AgentActionExecutionRepository(
                    db
                ).cancel_active_for_owner(
                    context.owner_user_id,
                    stable_error_code="runner_disabled",
                )
                autonomy_disabled = AgentAutonomySettingRepository(
                    db
                ).disable_for_owner(
                    context.owner_user_id,
                    reason="runner_disabled",
                )
                cancelled_autonomy_tasks = AgentAutonomyTaskRepository(
                    db
                ).cancel_not_started_for_owner(
                    context.owner_user_id,
                    stable_error_code="runner_disabled",
                )
            _audit(db, request).record(
                actor_type="user",
                action="ai.agent_settings_saved",
                target_user_id=context.owner_user_id,
                resource_type="ai_agent_setting",
                resource_id=str(row.id),
                client_ip=context.client_ip,
                details={
                    "user_enabled": bool(row.user_enabled),
                    "temperature_milli": int(row.temperature_milli),
                    "max_output_tokens": int(row.max_output_tokens),
                    "context_message_limit": int(row.context_message_limit),
                    "custom_instructions_configured": bool(row.custom_instructions),
                    "cancelled_runs": cancelled,
                    "execution_disabled": execution_disabled,
                    "cancelled_queued_actions": cancelled_actions,
                    "autonomy_disabled": autonomy_disabled,
                    "cancelled_autonomy_tasks": cancelled_autonomy_tasks,
                },
            )
            result = settings_public(row, connection=connection)
    except AgentServiceError as exc:
        _raise_service_error(exc)
    return {"ok": True, "settings": result}


@router.put("/execution-settings")
def update_execution_settings(
    body: ExecutionSettingsBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "execution-settings", limit=20, window_seconds=60)
    try:
        with session_scope() as db:
            previous = AgentExecutionSettingRepository(db).get(
                context.owner_user_id
            )
            previous_version = int(previous.version) if previous is not None else 0
            row = save_execution_settings(
                db,
                owner_user_id=context.owner_user_id,
                user_enabled=body.user_enabled,
                auto_send_enabled=body.auto_send_enabled,
                selected_actions=body.selected_actions,
            )
            changed = int(row.version) != previous_version
            cancelled = (
                AgentActionExecutionRepository(db).cancel_active_for_owner(
                    context.owner_user_id,
                    stable_error_code="execution_settings_changed",
                )
                if changed
                else 0
            )
            autonomy_disabled = False
            cancelled_autonomy_tasks = 0
            if changed:
                autonomy_disabled = AgentAutonomySettingRepository(
                    db
                ).disable_for_owner(
                    context.owner_user_id,
                    reason="execution_settings_changed",
                )
                cancelled_autonomy_tasks = AgentAutonomyTaskRepository(
                    db
                ).cancel_not_started_for_owner(
                    context.owner_user_id,
                    stable_error_code="execution_settings_changed",
                )
            access = execution_access(db, context.owner_user_id)
            runner_settings = AgentSettingRepository(db).get(context.owner_user_id)
            connection = None
            if (
                runner_settings is not None
                and runner_settings.active_connection_id is not None
            ):
                connection = ModelConnectionRepository(db).get(
                    context.owner_user_id,
                    runner_settings.active_connection_id,
                )
            runner_ready = bool(
                runner_settings is not None
                and runner_settings.user_enabled
                and connection is not None
                and connection.enabled
                and connection.api_key_encrypted
            )
            public_execution = execution_settings_public(
                row,
                access=access,
                runner_ready=runner_ready,
            )
            _audit(db, request).record(
                actor_type="user",
                action="ai.execution_settings_changed",
                target_user_id=context.owner_user_id,
                resource_type="ai_agent_execution_setting",
                resource_id=str(row.id),
                client_ip=context.client_ip,
                details={
                    "user_enabled": bool(row.user_enabled),
                    "auto_send_enabled": bool(row.auto_send_enabled),
                    "selected_actions": list(row.allowed_actions or []),
                    "version": int(row.version),
                    "cancelled_queued_actions": cancelled,
                    "autonomy_disabled": autonomy_disabled,
                    "cancelled_autonomy_tasks": cancelled_autonomy_tasks,
                },
            )
    except AgentServiceError as exc:
        _raise_service_error(exc)
    return {"ok": True, "execution": public_execution}


@router.put("/autonomy-settings")
def update_autonomy_settings(
    body: AutonomySettingsBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "autonomy-settings", limit=20, window_seconds=60)
    try:
        with session_scope() as db:
            previous = AgentAutonomySettingRepository(db).get(
                context.owner_user_id,
            )
            previous_version = int(previous.version) if previous is not None else 0
            row = save_autonomy_settings(
                db,
                owner_user_id=context.owner_user_id,
                user_enabled=body.user_enabled,
                auto_reply_enabled=body.auto_reply_enabled,
                scheduled_post_enabled=body.scheduled_post_enabled,
                managed_relationships_enabled=body.managed_relationships_enabled,
                discovery_enabled=body.discovery_enabled,
                text_match_enabled=body.text_match_enabled,
                proactive_message_enabled=body.proactive_message_enabled,
                follow_discovered_enabled=body.follow_discovered_enabled,
                friend_request_enabled=body.friend_request_enabled,
                allowed_actions=body.allowed_actions,
                operation_brief=body.operation_brief,
                managed_target_uids=body.managed_target_uids,
                timezone=body.timezone,
                active_start_minute=body.active_start_minute,
                active_end_minute=body.active_end_minute,
                minimum_action_interval_seconds=body.minimum_action_interval_seconds,
                daily_total_limit=body.daily_total_limit,
                daily_reply_limit=body.daily_reply_limit,
                daily_post_limit=body.daily_post_limit,
                daily_relationship_limit=body.daily_relationship_limit,
                post_interval_minutes=body.post_interval_minutes,
                discovery_interval_minutes=body.discovery_interval_minutes,
                consecutive_failure_limit=body.consecutive_failure_limit,
            )
            changed = int(row.version) != previous_version
            cancelled_tasks = (
                AgentAutonomyTaskRepository(db).cancel_not_started_for_owner(
                    context.owner_user_id,
                    stable_error_code="autonomy_settings_changed",
                )
                if changed
                else 0
            )
            access = autonomy_access(db, context.owner_user_id)
            runner_setting = AgentSettingRepository(db).get(context.owner_user_id)
            connection = None
            if runner_setting is not None and runner_setting.active_connection_id:
                connection = ModelConnectionRepository(db).get(
                    context.owner_user_id,
                    runner_setting.active_connection_id,
                )
            execution_row = AgentExecutionSettingRepository(db).get(
                context.owner_user_id
            )
            execution = execution_settings_public(
                execution_row,
                access=execution_access(db, context.owner_user_id),
                runner_ready=bool(
                    runner_setting is not None
                    and runner_setting.user_enabled
                    and connection is not None
                    and connection.enabled
                    and connection.api_key_encrypted
                    and connection.last_test_status == "ok"
                ),
            )
            recent_tasks = AgentAutonomyTaskRepository(db).list_recent(
                context.owner_user_id,
                limit=20,
            )
            public_autonomy = autonomy_public(
                row,
                access=access,
                runner_ready=bool(
                    runner_setting is not None
                    and runner_setting.user_enabled
                    and connection is not None
                    and connection.enabled
                    and connection.api_key_encrypted
                    and connection.last_test_status == "ok"
                ),
                execution=execution,
                background_enabled=bool(
                    getattr(_settings(request), "ai_agent_background_enabled", False)
                ),
                recent_tasks=recent_tasks,
                usage_today=autonomy_usage_today(
                    db, context.owner_user_id, row
                ),
            )
            _audit(db, request).record(
                actor_type="user",
                action="ai.autonomy_settings_changed",
                target_user_id=context.owner_user_id,
                resource_type="ai_agent_autonomy_setting",
                resource_id=str(row.id),
                client_ip=context.client_ip,
                details={
                    "user_enabled": bool(row.user_enabled),
                    "auto_reply_enabled": bool(row.auto_reply_enabled),
                    "scheduled_post_enabled": bool(row.scheduled_post_enabled),
                    "managed_relationships_enabled": bool(
                        row.managed_relationships_enabled
                    ),
                    "discovery_enabled": bool(row.discovery_enabled),
                    "text_match_enabled": bool(row.text_match_enabled),
                    "proactive_message_enabled": bool(
                        row.proactive_message_enabled
                    ),
                    "follow_discovered_enabled": bool(
                        row.follow_discovered_enabled
                    ),
                    "friend_request_enabled": bool(row.friend_request_enabled),
                    "allowed_actions": list(row.allowed_actions or []),
                    "managed_target_count": len(row.managed_target_uids or []),
                    "timezone": row.timezone,
                    "active_start_minute": int(row.active_start_minute),
                    "active_end_minute": int(row.active_end_minute),
                    "daily_total_limit": int(row.daily_total_limit),
                    "version": int(row.version),
                    "cancelled_tasks": cancelled_tasks,
                },
            )
    except AgentServiceError as exc:
        _raise_service_error(exc)
    return {"ok": True, "autonomy": public_autonomy}


@router.get("/autonomy/tasks")
def list_autonomy_tasks(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "autonomy-tasks", limit=60, window_seconds=60)
    try:
        with session_scope() as db:
            access = autonomy_access(db, context.owner_user_id)
            if not access.visible:
                raise AgentServiceError(
                    "autonomous_agent_not_available",
                    "自动社交 Agent 当前不可用",
                    status_code=404,
                )
            tasks = AgentAutonomyTaskRepository(db).list_recent(
                context.owner_user_id,
                limit=limit,
            )
            items = [autonomy_task_public(task) for task in tasks]
    except AgentServiceError as exc:
        _raise_service_error(exc)
    return {"ok": True, "items": items}


@router.post("/actions/prepare")
def prepare_account_action(
    body: AccountActionBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    command = _normalized_action(body, context)
    _action_scope_rate_limit(
        request, context, command.action_type, preparing=True
    )
    try:
        with session_scope() as db:
            execution = load_execution_configuration(
                db,
                owner_user_id=context.owner_user_id,
                action_type=command.action_type,
            )
        preflight_account_action(
            identity=context,
            persistence=_persistence(request),
            command=command,
        )
        token = _store_confirmation(
            request,
            context,
            command,
            execution_version=execution.version,
        )
    except AgentServiceError as exc:
        _raise_service_error(exc)
    except AccountActionError as exc:
        _raise_action_error(exc)
    return {
        "ok": True,
        "confirmation_token": token,
        "expires_in": _ACTION_CONFIRMATION_TTL_SECONDS,
        "summary": _action_summary(command),
        "action": command.action_type,
    }


@router.post("/actions/execute")
def execute_prepared_account_action(
    body: ExecuteAccountActionBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    command = _normalized_action(body, context)
    _action_scope_rate_limit(
        request, context, command.action_type, preparing=False
    )
    token = body.confirmation_token.get_secret_value()
    key, confirmation = _load_confirmation(request, token)
    execution_row: Any = None
    token = ""
    try:
        expected_owner = str(confirmation.get("owner_user_id") or "")
        expected_digest = str(confirmation.get("digest") or "")
        try:
            expected_version = int(confirmation.get("execution_version"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=409, detail="操作确认已失效，请重新确认"
            ) from exc
        if (
            expected_owner != str(context.owner_user_id)
            or not hmac.compare_digest(
                expected_digest,
                _confirmation_digest(request, context, command),
            )
        ):
            raise HTTPException(status_code=409, detail="操作确认与当前请求不一致")

        with _account_action_guard(request, context):
            try:
                with session_scope() as db:
                    execution = load_execution_configuration(
                        db,
                        owner_user_id=context.owner_user_id,
                        action_type=command.action_type,
                        expected_version=expected_version,
                        for_update=True,
                    )
                    _consume_confirmation(
                        request, key, str(confirmation.get("raw") or "")
                    )
                    execution_row, reused = _enqueue_started_action(
                        db,
                        context=context,
                        command=command,
                        execution_idempotency_key=_execution_idempotency_key(
                            "manual", command.idempotency_key
                        ),
                        execution_version=execution.version,
                        approval_source="user_explicit",
                        trigger_source="user",
                    )
                    if reused:
                        return _execution_reuse_payload(execution_row)
            except AgentServiceError as exc:
                _raise_service_error(exc)

            try:
                result = _dispatch_action_with_final_gate(
                    request,
                    context,
                    command,
                    execution_version=expected_version,
                    execution_idempotency_key=_execution_idempotency_key(
                        "manual", command.idempotency_key
                    ),
                    require_auto_send=False,
                )
            except AgentServiceError as exc:
                stable = AccountActionError(
                    exc.code,
                    exc.public_message,
                    status_code=exc.status_code,
                )
                _finish_action_failure(
                    request, context, execution_row.id, stable
                )
                _raise_service_error(exc)
            except AccountActionError as exc:
                _finish_action_failure(
                    request, context, execution_row.id, exc
                )
                _raise_action_error(exc)
            except Exception as exc:
                stable = AccountActionError(
                    "account_action_internal_error",
                    "账号操作未返回可确认结果，请先检查账号状态后再决定是否重试",
                    status_code=500,
                    outcome_unknown=True,
                )
                _finish_action_failure(
                    request, context, execution_row.id, stable
                )
                raise HTTPException(
                    status_code=500, detail=stable.public_message
                ) from exc
            try:
                _finish_action_success(
                    request, context, execution_row.id, result
                )
            except AgentServiceError as exc:
                _raise_service_error(exc)
            return {
                "ok": True,
                "reused": False,
                "execution_id": str(execution_row.id),
                **result.public(),
            }
    finally:
        token = ""


@router.post("/connection/test")
def test_connection(
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "connection-test", limit=5, window_seconds=300)
    runtime: RuntimeConfiguration | None = None
    run_id: uuid.UUID | None = None
    with _model_run_guard(request, context):
        try:
            with session_scope() as db:
                runtime = load_runtime_configuration(
                    db,
                    owner_user_id=context.owner_user_id,
                    cipher=_persistence(request).cipher,
                    require_user_enabled=False,
                )
                run = AgentRunRepository(db).add_running(
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    run_type="connection_test",
                    idempotency_key=str(uuid.uuid4()),
                    model_snapshot=runtime.model,
                    peer_upstream_uid=None,
                    source_message_count=0,
                    prompt_char_count=0,
                )
                run_id = run.id
            with session_scope() as db:
                _runtime_still_enabled(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    settings_version=runtime.settings_version,
                    require_user_enabled=False,
                )
            completion = OpenAICompatibleGateway(_settings(request)).test_connection(
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                model=runtime.model,
            )
            with session_scope() as db:
                _runtime_still_enabled(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    settings_version=runtime.settings_version,
                    require_user_enabled=False,
                )
                mark_connection_test(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    success=True,
                    error_code=None,
                )
                finished = AgentRunRepository(db).succeed(
                    context.owner_user_id,
                    run_id,
                    output_text="",
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    latency_ms=completion.latency_ms,
                )
                if finished is None:
                    raise AgentServiceError(
                        "run_cancelled", "本次连接测试已被取消", status_code=409
                    )
                _audit(db, request).record(
                    actor_type="user",
                    action="ai.model_connection_tested",
                    target_user_id=context.owner_user_id,
                    resource_type="ai_model_connection",
                    resource_id=str(runtime.connection_id),
                    client_ip=context.client_ip,
                    details={"success": True, "latency_ms": completion.latency_ms},
                )
        except ModelGatewayError as exc:
            state_error: AgentServiceError | None = None
            if runtime is not None and run_id is not None:
                try:
                    with session_scope() as db:
                        _runtime_still_enabled(
                            db,
                            owner_user_id=context.owner_user_id,
                            connection_id=runtime.connection_id,
                            settings_version=runtime.settings_version,
                            require_user_enabled=False,
                        )
                        mark_connection_test(
                            db,
                            owner_user_id=context.owner_user_id,
                            connection_id=runtime.connection_id,
                            success=False,
                            error_code=exc.code,
                        )
                        failed = AgentRunRepository(db).fail(
                            context.owner_user_id, run_id, failure_code=exc.code
                        )
                        if failed is None:
                            raise AgentServiceError(
                                "run_cancelled",
                                "本次连接测试已被取消",
                                status_code=409,
                            )
                        _audit(db, request).record(
                            actor_type="user",
                            action="ai.model_connection_tested",
                            target_user_id=context.owner_user_id,
                            resource_type="ai_model_connection",
                            resource_id=str(runtime.connection_id),
                            client_ip=context.client_ip,
                            details={"success": False, "failure_code": exc.code},
                        )
                except AgentServiceError as runtime_error:
                    state_error = runtime_error
            if state_error is not None:
                _raise_service_error(state_error)
            _raise_gateway_error(exc)
        except AgentServiceError as exc:
            if run_id is not None:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_service_error(exc)
        finally:
            if runtime is not None:
                runtime.clear_secret()
    return {
        "ok": True,
        "message": "模型连接测试成功",
        "latency_ms": completion.latency_ms,
    }


@router.post("/style/analyze")
def analyze_style(
    body: IdempotentRunBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "style-analysis", limit=3, window_seconds=600)
    runtime: RuntimeConfiguration | None = None
    run_id: uuid.UUID | None = None
    with _model_run_guard(request, context):
        try:
            with session_scope() as db:
                runtime = load_runtime_configuration(
                    db,
                    owner_user_id=context.owner_user_id,
                    cipher=_persistence(request).cipher,
                    require_user_enabled=True,
                )
                runs = AgentRunRepository(db)
                existing = runs.get_by_idempotency(
                    context.owner_user_id,
                    run_type="style_analysis",
                    idempotency_key=body.idempotency_key,
                )
                if existing is not None and existing.status == "succeeded":
                    profile = style_public(
                        StyleProfileRepository(db).get(context.owner_user_id)
                    )
                    return {
                        "ok": True,
                        "reused": True,
                        "run_id": str(existing.id),
                        "style_profile": profile,
                    }
                plan = build_style_analysis_plan(
                    db, owner_user_id=context.owner_user_id
                )
                run, acquired = runs.enqueue_running(
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    run_type="style_analysis",
                    idempotency_key=body.idempotency_key,
                    model_snapshot=runtime.model,
                    peer_upstream_uid=None,
                    source_message_count=plan.source_message_count,
                    prompt_char_count=plan.prompt_char_count,
                )
                if not acquired:
                    if run.status == "succeeded":
                        profile = style_public(
                            StyleProfileRepository(db).get(context.owner_user_id)
                        )
                        return {
                            "ok": True,
                            "reused": True,
                            "run_id": str(run.id),
                            "style_profile": profile,
                        }
                    raise AgentServiceError(
                        "idempotency_conflict",
                        "相同请求已经处理或仍在运行，请重新发起",
                        status_code=409,
                    )
                run_id = run.id
            with session_scope() as db:
                _runtime_still_enabled(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    settings_version=runtime.settings_version,
                    require_user_enabled=True,
                )
            completion = OpenAICompatibleGateway(_settings(request)).complete(
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                model=runtime.model,
                messages=plan.messages,
                temperature=0.2,
                max_output_tokens=min(runtime.max_output_tokens, 1200),
            )
            summary, traits = parse_style_profile(completion.text)
            with session_scope() as db:
                _runtime_still_enabled(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    settings_version=runtime.settings_version,
                    require_user_enabled=True,
                )
                profile_row = save_style_profile(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    summary=summary,
                    traits=traits,
                    plan=plan,
                )
                finished = AgentRunRepository(db).succeed(
                    context.owner_user_id,
                    run_id,
                    output_text=summary,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    latency_ms=completion.latency_ms,
                )
                if finished is None:
                    raise AgentServiceError(
                        "run_cancelled", "本次风格分析已被取消", status_code=409
                    )
                _audit(db, request).record(
                    actor_type="user",
                    action="ai.style_profile_analyzed",
                    target_user_id=context.owner_user_id,
                    resource_type="ai_style_profile",
                    resource_id=str(profile_row.id),
                    client_ip=context.client_ip,
                    details={
                        "source_message_count": plan.source_message_count,
                        "latency_ms": completion.latency_ms,
                    },
                )
                profile = style_public(profile_row)
        except ModelGatewayError as exc:
            if run_id is not None:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_gateway_error(exc)
        except AgentServiceError as exc:
            if run_id is not None:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_service_error(exc)
        finally:
            if runtime is not None:
                runtime.clear_secret()
    return {
        "ok": True,
        "reused": False,
        "run_id": str(run_id),
        "style_profile": profile,
    }


@router.post("/drafts")
def generate_reply_draft(
    body: ReplyDraftBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "reply-draft", limit=10, window_seconds=60)
    runtime: RuntimeConfiguration | None = None
    run_id: uuid.UUID | None = None
    with _model_run_guard(request, context):
        try:
            with session_scope() as db:
                runtime = load_runtime_configuration(
                    db,
                    owner_user_id=context.owner_user_id,
                    cipher=_persistence(request).cipher,
                    require_user_enabled=True,
                )
                runs = AgentRunRepository(db)
                existing = runs.get_by_idempotency(
                    context.owner_user_id,
                    run_type="reply_draft",
                    idempotency_key=body.idempotency_key,
                )
                if (
                    existing is not None
                    and existing.status == "succeeded"
                    and existing.output_text is not None
                ):
                    return {
                        "ok": True,
                        "reused": True,
                        "run_id": str(existing.id),
                        "draft": existing.output_text,
                        "origin": "agent",
                        "executed": False,
                    }
                plan = build_reply_draft_plan(
                    db,
                    owner_user_id=context.owner_user_id,
                    peer_upstream_uid=body.peer_upstream_uid,
                    objective=body.objective,
                    runtime=runtime,
                )
                run, acquired = runs.enqueue_running(
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    run_type="reply_draft",
                    idempotency_key=body.idempotency_key,
                    model_snapshot=runtime.model,
                    peer_upstream_uid=body.peer_upstream_uid,
                    source_message_count=plan.source_message_count,
                    prompt_char_count=plan.prompt_char_count,
                )
                if not acquired:
                    if run.status == "succeeded" and run.output_text is not None:
                        return {
                            "ok": True,
                            "reused": True,
                            "run_id": str(run.id),
                            "draft": run.output_text,
                            "origin": "agent",
                            "executed": False,
                        }
                    raise AgentServiceError(
                        "idempotency_conflict",
                        "相同请求已经处理或仍在运行，请重新发起",
                        status_code=409,
                    )
                run_id = run.id
            with session_scope() as db:
                _runtime_still_enabled(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    settings_version=runtime.settings_version,
                    require_user_enabled=True,
                )
            completion = OpenAICompatibleGateway(_settings(request)).complete(
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                model=runtime.model,
                messages=plan.messages,
                temperature=runtime.temperature,
                max_output_tokens=runtime.max_output_tokens,
            )
            draft = completion.text.strip()
            if not draft:
                raise AgentServiceError(
                    "empty_draft", "模型没有生成可用草稿", status_code=502
                )
            with session_scope() as db:
                _runtime_still_enabled(
                    db,
                    owner_user_id=context.owner_user_id,
                    connection_id=runtime.connection_id,
                    settings_version=runtime.settings_version,
                    require_user_enabled=True,
                )
                finished = AgentRunRepository(db).succeed(
                    context.owner_user_id,
                    run_id,
                    output_text=draft,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    latency_ms=completion.latency_ms,
                )
                if finished is None:
                    raise AgentServiceError(
                        "run_cancelled", "本次草稿生成已被取消", status_code=409
                    )
                _audit(db, request).record(
                    actor_type="user",
                    action="ai.reply_draft_generated",
                    target_user_id=context.owner_user_id,
                    resource_type="ai_agent_run",
                    resource_id=str(run_id),
                    client_ip=context.client_ip,
                    details={
                        "source_message_count": plan.source_message_count,
                        "output_char_count": len(draft),
                        "latency_ms": completion.latency_ms,
                        "executed": False,
                    },
                )
        except ModelGatewayError as exc:
            if run_id is not None:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_gateway_error(exc)
        except AgentServiceError as exc:
            if run_id is not None:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_service_error(exc)
        finally:
            if runtime is not None:
                runtime.clear_secret()
    return {
        "ok": True,
        "reused": False,
        "run_id": str(run_id),
        "draft": draft,
        "origin": "agent",
        "executed": False,
        "message": "草稿已生成，发送前请人工检查",
    }


@router.post("/replies/send")
def generate_and_send_reply(
    body: ReplyDraftBody,
    request: Request,
    context: AgentContext = Depends(_agent_context),
) -> dict[str, Any]:
    _rate_limit(request, context, "reply-send", limit=10, window_seconds=60)
    runtime: RuntimeConfiguration | None = None
    run_id: uuid.UUID | None = None
    execution_row: Any = None
    execution_version = 0
    draft = ""
    reused_model_run = False
    action_key = _execution_idempotency_key("reply", body.idempotency_key)
    with _model_run_guard(request, context):
        try:
            with session_scope() as db:
                runtime = load_runtime_configuration(
                    db,
                    owner_user_id=context.owner_user_id,
                    cipher=_persistence(request).cipher,
                    require_user_enabled=True,
                )
                execution = load_execution_configuration(
                    db,
                    owner_user_id=context.owner_user_id,
                    action_type=SEND_PRIVATE_MESSAGE,
                    require_auto_send=True,
                )
                execution_version = execution.version

            preflight = normalize_account_action(
                identity=context,
                action_type=SEND_PRIVATE_MESSAGE,
                target_upstream_uid=body.peer_upstream_uid,
                content="预检",
                visibility="public",
                idempotency_key=body.idempotency_key,
            )
            preflight_account_action(
                identity=context,
                persistence=_persistence(request),
                command=preflight,
            )

            with session_scope() as db:
                runs = AgentRunRepository(db)
                existing = runs.get_by_idempotency(
                    context.owner_user_id,
                    run_type="reply_send",
                    idempotency_key=body.idempotency_key,
                )
                if (
                    existing is not None
                    and existing.status == "succeeded"
                    and existing.output_text is not None
                ):
                    prior_action = AgentActionExecutionRepository(
                        db
                    ).get_by_idempotency(
                        context.owner_user_id,
                        action_type=SEND_PRIVATE_MESSAGE,
                        idempotency_key=action_key,
                    )
                    if prior_action is None:
                        raise AgentServiceError(
                            "execution_record_missing",
                            "此前直发请求缺少执行记录，请使用新的请求标识重新生成",
                            status_code=409,
                        )
                    if prior_action.status == "succeeded":
                        return {
                            **_execution_reuse_payload(prior_action),
                            "run_id": str(existing.id),
                            "draft": existing.output_text,
                            "origin": "agent",
                        }
                    if (
                        prior_action.status != "queued"
                        or int(prior_action.execution_setting_version)
                        != execution_version
                    ):
                        raise AgentServiceError(
                            "idempotency_conflict",
                            "此前直发请求已失败、取消或仍在执行，请勿自动重试",
                            status_code=409,
                        )
                    run_id = existing.id
                    draft = existing.output_text
                    reused_model_run = True
                else:
                    plan = build_reply_draft_plan(
                        db,
                        owner_user_id=context.owner_user_id,
                        peer_upstream_uid=body.peer_upstream_uid,
                        objective=body.objective,
                        runtime=runtime,
                    )
                    run, acquired = runs.enqueue_running(
                        owner_user_id=context.owner_user_id,
                        connection_id=runtime.connection_id,
                        run_type="reply_send",
                        idempotency_key=body.idempotency_key,
                        model_snapshot=runtime.model,
                        peer_upstream_uid=body.peer_upstream_uid,
                        source_message_count=plan.source_message_count,
                        prompt_char_count=plan.prompt_char_count,
                    )
                    if not acquired:
                        raise AgentServiceError(
                            "idempotency_conflict",
                            "相同直发请求已经处理或仍在运行，请重新发起",
                            status_code=409,
                        )
                    run_id = run.id

            if not reused_model_run:
                with session_scope() as db:
                    _runtime_still_enabled(
                        db,
                        owner_user_id=context.owner_user_id,
                        connection_id=runtime.connection_id,
                        settings_version=runtime.settings_version,
                        require_user_enabled=True,
                    )
                    load_execution_configuration(
                        db,
                        owner_user_id=context.owner_user_id,
                        action_type=SEND_PRIVATE_MESSAGE,
                        require_auto_send=True,
                        expected_version=execution_version,
                    )
                completion = OpenAICompatibleGateway(_settings(request)).complete(
                    base_url=runtime.base_url,
                    api_key=runtime.api_key,
                    model=runtime.model,
                    messages=plan.messages,
                    temperature=runtime.temperature,
                    max_output_tokens=min(runtime.max_output_tokens, 800),
                )
                draft = completion.text.strip()
                if not draft:
                    raise AgentServiceError(
                        "empty_draft", "模型没有生成可发送的回复", status_code=502
                    )
                if len(draft) > 2000:
                    raise AgentServiceError(
                        "generated_message_too_long",
                        "模型生成的回复超过私信长度限制，本次未发送",
                        status_code=502,
                    )
                with session_scope() as db:
                    _runtime_still_enabled(
                        db,
                        owner_user_id=context.owner_user_id,
                        connection_id=runtime.connection_id,
                        settings_version=runtime.settings_version,
                        require_user_enabled=True,
                    )
                    load_execution_configuration(
                        db,
                        owner_user_id=context.owner_user_id,
                        action_type=SEND_PRIVATE_MESSAGE,
                        require_auto_send=True,
                        expected_version=execution_version,
                        for_update=True,
                    )
                    finished = AgentRunRepository(db).succeed(
                        context.owner_user_id,
                        run_id,
                        output_text=draft,
                        input_tokens=completion.input_tokens,
                        output_tokens=completion.output_tokens,
                        latency_ms=completion.latency_ms,
                    )
                    if finished is None:
                        raise AgentServiceError(
                            "run_cancelled", "本次直发生成已被取消", status_code=409
                        )

            command = normalize_account_action(
                identity=context,
                action_type=SEND_PRIVATE_MESSAGE,
                target_upstream_uid=body.peer_upstream_uid,
                content=draft,
                visibility="public",
                idempotency_key=body.idempotency_key,
            )
            preflight_account_action(
                identity=context,
                persistence=_persistence(request),
                command=command,
            )

            with _account_action_guard(request, context):
                with session_scope() as db:
                    current_execution = load_execution_configuration(
                        db,
                        owner_user_id=context.owner_user_id,
                        action_type=SEND_PRIVATE_MESSAGE,
                        require_auto_send=True,
                        expected_version=execution_version,
                        for_update=True,
                    )
                    execution_row, reused = _enqueue_started_action(
                        db,
                        context=context,
                        command=command,
                        execution_idempotency_key=action_key,
                        execution_version=current_execution.version,
                        approval_source="user_allowlist",
                        trigger_source="model",
                    )
                    if reused:
                        return {
                            **_execution_reuse_payload(execution_row),
                            "run_id": str(run_id),
                            "draft": draft,
                            "origin": "agent",
                        }
                try:
                    result = _dispatch_action_with_final_gate(
                        request,
                        context,
                        command,
                        execution_version=execution_version,
                        execution_idempotency_key=action_key,
                        require_auto_send=True,
                    )
                except AgentServiceError as exc:
                    stable = AccountActionError(
                        exc.code,
                        exc.public_message,
                        status_code=exc.status_code,
                    )
                    _finish_action_failure(
                        request, context, execution_row.id, stable
                    )
                    _raise_service_error(exc)
                except AccountActionError as exc:
                    _finish_action_failure(
                        request, context, execution_row.id, exc
                    )
                    _raise_action_error(exc)
                except Exception as exc:
                    stable = AccountActionError(
                        "account_action_internal_error",
                        "回复已生成，但账号发送执行失败，请先检查会话后再决定是否重试",
                        status_code=500,
                        outcome_unknown=True,
                    )
                    _finish_action_failure(
                        request, context, execution_row.id, stable
                    )
                    raise HTTPException(
                        status_code=500, detail=stable.public_message
                    ) from exc
                _finish_action_success(
                    request,
                    context,
                    execution_row.id,
                    result,
                    audit_action="ai.reply_generated_and_sent",
                )
                return {
                    "ok": True,
                    "reused": False,
                    "run_id": str(run_id),
                    "execution_id": str(execution_row.id),
                    "draft": draft,
                    "origin": "agent",
                    **result.public(),
                }
        except ModelGatewayError as exc:
            if run_id is not None:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_gateway_error(exc)
        except AccountActionError as exc:
            if run_id is not None and not reused_model_run:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_action_error(exc)
        except AgentServiceError as exc:
            if run_id is not None and not reused_model_run:
                with session_scope() as db:
                    AgentRunRepository(db).fail(
                        context.owner_user_id, run_id, failure_code=exc.code
                    )
            _raise_service_error(exc)
        finally:
            if runtime is not None:
                runtime.clear_secret()

    raise HTTPException(status_code=500, detail="直发请求未完成")
