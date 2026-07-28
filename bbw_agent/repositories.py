"""Owner-scoped persistence for the built-in BYOK runner."""

from __future__ import annotations

import uuid
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from bbw_prod.models import (
    AiAgentActionExecution,
    AiAgentAutonomyDailyUsage,
    AiAgentContactPolicy,
    AiAgentDiscoveryCandidate,
    AiAgentAutonomySetting,
    AiAgentAutonomyTask,
    AiAgentExecutionSetting,
    AiAgentRun,
    AiAgentSetting,
    AiModelConnection,
    AiModelRunnerSystemSetting,
    AiStyleProfile,
    Conversation,
    ExternalAccount,
    Message,
    MatchPreference,
    Relationship,
    User,
    utcnow,
)
from .autonomous import (
    AUTONOMY_RELATIONSHIP_ADDRESS_TERMS,
    agent_candidate_compatibility,
    autonomy_message_identity,
    autonomy_reply_message_is_eligible,
    autonomy_reply_message_is_fresh,
    autonomy_reply_not_before,
)
from .contact_policy import (
    CONTACT_MODE_SUGGEST_ONLY,
    CONTACT_POLICY_MODES,
    DEFAULT_MAXIMUM_REPLY_AGE_SECONDS,
    DEFAULT_MINIMUM_REPLY_DELAY_SECONDS,
    MAXIMUM_REPLY_AGE_SECONDS,
    MAXIMUM_REPLY_DELAY_SECONDS,
    MINIMUM_REPLY_AGE_SECONDS,
    MINIMUM_REPLY_DELAY_SECONDS,
    RELATIONSHIP_STAGES,
    ContactRelationshipSignals,
    contact_policy_allows_auto_reply,
    derive_relationship_stage,
    reply_risk_boundary,
)
from .style_sampling import style_profile_is_current


SUPPORTED_ACCOUNT_ACTION_TYPES = frozenset(
    {
        "send_private_message",
        "publish_text_post",
        "follow_user",
        "unfollow_user",
        "browse_online_users",
        "request_text_match",
        "request_friend",
    }
)
SUPPORTED_ACTION_APPROVAL_SOURCES = frozenset(
    {"user_explicit", "user_allowlist"}
)
SUPPORTED_ACTION_TRIGGER_SOURCES = frozenset(
    {"user", "model", "schedule", "system"}
)
AUTONOMY_TASK_TYPES = frozenset(
    {
        "reply_to_message",
        "scheduled_post",
        "follow_target",
        "unfollow_target",
        "browse_online",
        "request_match",
        "proactive_message",
        "follow_discovered",
        "request_friend",
    }
)
AUTONOMY_NOT_STARTED_STATUSES = frozenset(
    {"queued", "deferred", "leased", "generating"}
)
AUTONOMY_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "stale", "manual_review"}
)
AUTONOMY_TASK_ACTIONS = {
    "reply_to_message": "send_private_message",
    "scheduled_post": "publish_text_post",
    "follow_target": "follow_user",
    "unfollow_target": "unfollow_user",
    "browse_online": "browse_online_users",
    "request_match": "request_text_match",
    "proactive_message": "send_private_message",
    "follow_discovered": "follow_user",
    "request_friend": "request_friend",
}
AUTONOMY_MESSAGE_PROVIDERS = frozenset({"web-local", "tim"})
AUTONOMY_BROWSE_DAILY_LIMIT = 8
AUTONOMY_MATCH_DAILY_LIMIT = 6
AUTONOMY_OUTREACH_DAILY_LIMIT = 6
AUTONOMY_DYNAMIC_CANDIDATE_TASKS = frozenset(
    {"proactive_message", "follow_discovered", "request_friend"}
)
AGENT_RUNNING_TIMEOUT = timedelta(minutes=15)
MODEL_RUN_TIMEOUT_FAILURE_CODE = "model_run_timeout"
ACTION_EXECUTION_TIMEOUT_FAILURE_CODE = "execution_timeout_unknown"


def _normalize_action_type(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in SUPPORTED_ACCOUNT_ACTION_TYPES:
        raise ValueError("unsupported account action type")
    return normalized


def _normalize_allowed_actions(values: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        action_type = _normalize_action_type(value)
        if action_type not in normalized:
            normalized.append(action_type)
    return normalized


def _normalize_idempotency_key(value: str) -> str:
    normalized = str(value or "").strip()
    if not 8 <= len(normalized) <= 160 or any(
        ord(character) < 32 for character in normalized
    ):
        raise ValueError("invalid account action idempotency key")
    return normalized


def _normalize_stable_error_code(value: str, *, fallback: str) -> str:
    normalized = str(value or fallback).strip().lower()
    if not normalized:
        normalized = fallback
    if len(normalized) > 64 or any(
        not (character.isascii() and (character.isalnum() or character in "_.-"))
        for character in normalized
    ):
        raise ValueError("invalid stable error code")
    return normalized


def _autonomy_usage_category(task_type: str, action_type: str) -> str:
    normalized_task = str(task_type or "").strip()
    normalized_action = str(action_type or "").strip()
    if normalized_task == "reply_to_message":
        return "reply"
    if normalized_task == "proactive_message":
        return "outreach"
    if normalized_task == "browse_online":
        return "browse"
    if normalized_task == "request_match":
        return "match"
    if normalized_action == "publish_text_post":
        return "post"
    return "relationship"


def _autonomy_usage_count(usage: Any | None, category: str) -> int:
    if usage is None:
        return 0
    field = {
        "reply": "reply_actions",
        "outreach": "outreach_actions",
        "post": "post_actions",
        "relationship": "relationship_actions",
        "browse": "browse_actions",
        "match": "match_actions",
    }[category]
    return int(getattr(usage, field, 0) or 0)


def _increment_autonomy_usage(usage: Any, category: str) -> None:
    field = {
        "reply": "reply_actions",
        "outreach": "outreach_actions",
        "post": "post_actions",
        "relationship": "relationship_actions",
        "browse": "browse_actions",
        "match": "match_actions",
    }[category]
    setattr(usage, field, int(getattr(usage, field, 0) or 0) + 1)


def _setting_category_limit(setting: Any, category: str) -> int:
    total = max(1, int(getattr(setting, "daily_total_limit", 1) or 1))
    if category == "reply":
        return min(total, int(getattr(setting, "daily_reply_limit", 0) or 0))
    if category == "outreach":
        return min(
            total,
            int(getattr(setting, "daily_reply_limit", 0) or 0),
            AUTONOMY_OUTREACH_DAILY_LIMIT,
        )
    if category == "post":
        return min(total, int(getattr(setting, "daily_post_limit", 0) or 0))
    if category == "relationship":
        return min(
            total,
            int(getattr(setting, "daily_relationship_limit", 0) or 0),
        )
    if category == "browse":
        return min(total, AUTONOMY_BROWSE_DAILY_LIMIT)
    return min(total, AUTONOMY_MATCH_DAILY_LIMIT)


class ModelRunnerSystemSettingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, *, for_update: bool = False) -> AiModelRunnerSystemSetting | None:
        stmt = select(AiModelRunnerSystemSetting).where(
            AiModelRunnerSystemSetting.id == 1
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_or_create(
        self, *, for_update: bool = False
    ) -> AiModelRunnerSystemSetting:
        row = self.get(for_update=for_update)
        if row is not None:
            return row
        row = AiModelRunnerSystemSetting(
            id=1,
            enabled=False,
            account_actions_enabled=False,
        )
        self.db.add(row)
        self.db.flush()
        return row


class ModelConnectionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        owner_user_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiModelConnection | None:
        stmt = select(AiModelConnection).where(
            AiModelConnection.owner_user_id == owner_user_id,
            AiModelConnection.id == connection_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def first_for_owner(
        self, owner_user_id: uuid.UUID, *, for_update: bool = False
    ) -> AiModelConnection | None:
        stmt = (
            select(AiModelConnection)
            .where(AiModelConnection.owner_user_id == owner_user_id)
            .order_by(AiModelConnection.updated_at.desc(), AiModelConnection.id.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def add(self, row: AiModelConnection) -> AiModelConnection:
        self.db.add(row)
        self.db.flush()
        return row


class AgentSettingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self, owner_user_id: uuid.UUID, *, for_update: bool = False
    ) -> AiAgentSetting | None:
        stmt = select(AiAgentSetting).where(
            AiAgentSetting.owner_user_id == owner_user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_or_create(
        self,
        owner_user_id: uuid.UUID,
        *,
        active_connection_id: uuid.UUID | None = None,
        for_update: bool = False,
    ) -> AiAgentSetting:
        row = self.get(owner_user_id, for_update=for_update)
        if row is not None:
            if row.active_connection_id is None and active_connection_id is not None:
                row.active_connection_id = active_connection_id
            return row
        row = AiAgentSetting(
            id=uuid.uuid4(),
            owner_user_id=owner_user_id,
            active_connection_id=active_connection_id,
            user_enabled=False,
            mode="draft",
            temperature_milli=700,
            max_output_tokens=512,
            context_message_limit=30,
            version=1,
        )
        self.db.add(row)
        self.db.flush()
        return row


class AgentExecutionSettingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self, owner_user_id: uuid.UUID, *, for_update: bool = False
    ) -> AiAgentExecutionSetting | None:
        stmt = select(AiAgentExecutionSetting).where(
            AiAgentExecutionSetting.owner_user_id == owner_user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_or_create(
        self,
        owner_user_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiAgentExecutionSetting:
        row = self.get(owner_user_id, for_update=for_update)
        if row is not None:
            return row
        row = AiAgentExecutionSetting(
            id=uuid.uuid4(),
            owner_user_id=owner_user_id,
            user_enabled=False,
            auto_send_enabled=False,
            allowed_actions=[],
            version=1,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def configure(
        self,
        owner_user_id: uuid.UUID,
        *,
        user_enabled: bool,
        auto_send_enabled: bool,
        allowed_actions: list[str] | tuple[str, ...],
    ) -> AiAgentExecutionSetting:
        normalized_actions = _normalize_allowed_actions(allowed_actions)
        if auto_send_enabled and not user_enabled:
            raise ValueError("auto send requires the user execution switch")
        row = self.get_or_create(owner_user_id, for_update=True)
        changed = (
            row.user_enabled != bool(user_enabled)
            or row.auto_send_enabled != bool(auto_send_enabled)
            or list(row.allowed_actions or []) != normalized_actions
        )
        if changed:
            row.user_enabled = bool(user_enabled)
            row.auto_send_enabled = bool(auto_send_enabled)
            row.allowed_actions = normalized_actions
            row.version = max(1, int(row.version or 1)) + 1
            row.updated_at = utcnow()
            self.db.flush()
        return row

    def disable_for_owner(self, owner_user_id: uuid.UUID) -> bool:
        row = self.get(owner_user_id, for_update=True)
        if row is None or not (row.user_enabled or row.auto_send_enabled):
            return False
        row.user_enabled = False
        row.auto_send_enabled = False
        row.version = max(1, int(row.version or 1)) + 1
        row.updated_at = utcnow()
        self.db.flush()
        return True


class StyleProfileRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self, owner_user_id: uuid.UUID, *, for_update: bool = False
    ) -> AiStyleProfile | None:
        stmt = select(AiStyleProfile).where(
            AiStyleProfile.owner_user_id == owner_user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_current(
        self, owner_user_id: uuid.UUID, *, for_update: bool = False
    ) -> AiStyleProfile | None:
        row = self.get(owner_user_id, for_update=for_update)
        return row if style_profile_is_current(row) else None

    def upsert(
        self,
        *,
        owner_user_id: uuid.UUID,
        model_connection_id: uuid.UUID,
        summary: str,
        traits: dict[str, object],
        source_message_count: int,
        source_peer_count: int,
        source_last_message_at: datetime | None,
        sampling_policy_version: int,
        sanitizer_version: int,
    ) -> AiStyleProfile:
        row = self.get(owner_user_id, for_update=True)
        if row is None:
            row = AiStyleProfile(
                id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                model_connection_id=model_connection_id,
                summary=summary,
                traits=traits,
                source_message_count=source_message_count,
                source_peer_count=source_peer_count,
                sampling_policy_version=sampling_policy_version,
                sanitizer_version=sanitizer_version,
                source_last_message_at=source_last_message_at,
                generated_at=utcnow(),
            )
            self.db.add(row)
        else:
            row.model_connection_id = model_connection_id
            row.summary = summary
            row.traits = traits
            row.source_message_count = source_message_count
            row.source_peer_count = source_peer_count
            row.sampling_policy_version = sampling_policy_version
            row.sanitizer_version = sanitizer_version
            row.source_last_message_at = source_last_message_at
            row.generated_at = utcnow()
        self.db.flush()
        return row


class AgentRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        owner_user_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiAgentRun | None:
        stmt = select(AiAgentRun).where(
            AiAgentRun.owner_user_id == owner_user_id,
            AiAgentRun.id == run_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_idempotency(
        self,
        owner_user_id: uuid.UUID,
        *,
        run_type: str,
        idempotency_key: str,
        for_update: bool = False,
    ) -> AiAgentRun | None:
        stmt = select(AiAgentRun).where(
            AiAgentRun.owner_user_id == owner_user_id,
            AiAgentRun.run_type == run_type,
            AiAgentRun.idempotency_key == idempotency_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def add_running(
        self,
        *,
        owner_user_id: uuid.UUID,
        connection_id: uuid.UUID,
        run_type: str,
        idempotency_key: str,
        model_snapshot: str,
        peer_upstream_uid: str | None,
        source_message_count: int,
        prompt_char_count: int,
    ) -> AiAgentRun:
        row = AiAgentRun(
            id=uuid.uuid4(),
            owner_user_id=owner_user_id,
            connection_id=connection_id,
            run_type=run_type,
            status="running",
            idempotency_key=idempotency_key,
            model_snapshot=model_snapshot,
            peer_upstream_uid=peer_upstream_uid,
            source_message_count=max(0, int(source_message_count)),
            prompt_char_count=max(0, int(prompt_char_count)),
            output_char_count=0,
            started_at=utcnow(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def enqueue_running(
        self,
        *,
        owner_user_id: uuid.UUID,
        connection_id: uuid.UUID,
        run_type: str,
        idempotency_key: str,
        model_snapshot: str,
        peer_upstream_uid: str | None,
        source_message_count: int,
        prompt_char_count: int,
    ) -> tuple[AiAgentRun, bool]:
        """Create or acquire one durable idempotent model run.

        A process can disappear after committing ``running`` and before it can
        persist a terminal result.  Model generation has no account-side
        effect, so an attempt older than the bounded timeout may safely reuse
        the same audit row and idempotency key.  Fresh running attempts and
        ordinary terminal failures remain non-acquirable.
        """

        now = utcnow()
        created = self.db.scalars(
            insert(AiAgentRun)
            .values(
                id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                connection_id=connection_id,
                run_type=str(run_type or "").strip(),
                status="running",
                idempotency_key=_normalize_idempotency_key(idempotency_key),
                model_snapshot=str(model_snapshot or "")[:160],
                peer_upstream_uid=(
                    str(peer_upstream_uid or "").strip()[:128] or None
                ),
                source_message_count=max(0, int(source_message_count)),
                prompt_char_count=max(0, int(prompt_char_count)),
                output_char_count=0,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                constraint="uq_ai_agent_runs_owner_type_idempotency"
            )
            .returning(AiAgentRun)
        ).first()
        if created is not None:
            return created, True
        existing = self.get_by_idempotency(
            owner_user_id,
            run_type=str(run_type or "").strip(),
            idempotency_key=_normalize_idempotency_key(idempotency_key),
            for_update=True,
        )
        if existing is None:
            raise RuntimeError(
                "model run idempotency conflict occurred but row was not found"
            )
        stale_running = bool(
            existing.status == "running"
            and existing.started_at is not None
            and existing.started_at <= now - AGENT_RUNNING_TIMEOUT
        )
        timed_out = bool(
            existing.status == "failed"
            and existing.failure_code == MODEL_RUN_TIMEOUT_FAILURE_CODE
        )
        if stale_running or timed_out:
            existing.connection_id = connection_id
            existing.status = "running"
            existing.model_snapshot = str(model_snapshot or "")[:160]
            existing.peer_upstream_uid = (
                str(peer_upstream_uid or "").strip()[:128] or None
            )
            existing.source_message_count = max(0, int(source_message_count))
            existing.prompt_char_count = max(0, int(prompt_char_count))
            existing.output_text = None
            existing.output_char_count = 0
            existing.input_tokens = None
            existing.output_tokens = None
            existing.latency_ms = None
            existing.failure_code = None
            existing.started_at = now
            existing.completed_at = None
            existing.updated_at = now
            self.db.flush()
            return existing, True
        return existing, False

    def fail_stale_running(
        self,
        *,
        at: datetime | None = None,
        timeout: timedelta = AGENT_RUNNING_TIMEOUT,
    ) -> int:
        """Close abandoned model attempts; a later replay may reacquire them."""

        now = at or utcnow()
        result = self.db.execute(
            update(AiAgentRun)
            .where(
                AiAgentRun.status == "running",
                AiAgentRun.started_at <= now - timeout,
            )
            .values(
                status="failed",
                failure_code=MODEL_RUN_TIMEOUT_FAILURE_CODE,
                output_text=None,
                output_char_count=0,
                completed_at=now,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    def succeed(
        self,
        owner_user_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        output_text: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
    ) -> AiAgentRun | None:
        row = self.get(owner_user_id, run_id, for_update=True)
        if row is None or row.status != "running":
            return None
        row.status = "succeeded"
        row.output_text = output_text
        row.output_char_count = len(output_text)
        row.input_tokens = input_tokens
        row.output_tokens = output_tokens
        row.latency_ms = max(0, int(latency_ms))
        row.failure_code = None
        row.completed_at = utcnow()
        self.db.flush()
        return row

    def fail(
        self,
        owner_user_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        failure_code: str,
        latency_ms: int | None = None,
    ) -> AiAgentRun | None:
        row = self.get(owner_user_id, run_id, for_update=True)
        if row is None or row.status != "running":
            return None
        row.status = "failed"
        row.failure_code = str(failure_code or "model_failed")[:64]
        row.latency_ms = max(0, int(latency_ms)) if latency_ms is not None else None
        row.output_text = None
        row.output_char_count = 0
        row.completed_at = utcnow()
        self.db.flush()
        return row

    def cancel_active_for_owner(self, owner_user_id: uuid.UUID) -> int:
        now = utcnow()
        result = self.db.execute(
            update(AiAgentRun)
            .where(
                AiAgentRun.owner_user_id == owner_user_id,
                AiAgentRun.status == "running",
            )
            .values(
                status="cancelled",
                failure_code="access_revoked",
                output_text=None,
                output_char_count=0,
                completed_at=now,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    def cancel_all_active(self) -> int:
        now = utcnow()
        result = self.db.execute(
            update(AiAgentRun)
            .where(AiAgentRun.status == "running")
            .values(
                status="cancelled",
                failure_code="system_disabled",
                output_text=None,
                output_char_count=0,
                completed_at=now,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)


class AgentActionExecutionRepository:
    """Owner-scoped queue and state transitions for account-side effects."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        owner_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiAgentActionExecution | None:
        stmt = select(AiAgentActionExecution).where(
            AiAgentActionExecution.owner_user_id == owner_user_id,
            AiAgentActionExecution.id == execution_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_idempotency(
        self,
        owner_user_id: uuid.UUID,
        *,
        action_type: str,
        idempotency_key: str,
        for_update: bool = False,
    ) -> AiAgentActionExecution | None:
        stmt = select(AiAgentActionExecution).where(
            AiAgentActionExecution.owner_user_id == owner_user_id,
            AiAgentActionExecution.action_type == _normalize_action_type(action_type),
            AiAgentActionExecution.idempotency_key
            == _normalize_idempotency_key(idempotency_key),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def enqueue(
        self,
        *,
        owner_user_id: uuid.UUID,
        external_account_id: uuid.UUID,
        action_type: str,
        idempotency_key: str,
        execution_setting_version: int,
        target_snapshot: dict[str, object],
        parameter_snapshot: dict[str, object],
        approval_source: str,
        trigger_source: str,
    ) -> tuple[AiAgentActionExecution, bool]:
        normalized_action = _normalize_action_type(action_type)
        normalized_key = _normalize_idempotency_key(idempotency_key)
        normalized_approval = str(approval_source or "").strip()
        normalized_trigger = str(trigger_source or "").strip()
        if normalized_approval not in SUPPORTED_ACTION_APPROVAL_SOURCES:
            raise ValueError("unsupported account action approval source")
        if normalized_trigger not in SUPPORTED_ACTION_TRIGGER_SOURCES:
            raise ValueError("unsupported account action trigger source")
        if int(execution_setting_version) < 1:
            raise ValueError("execution setting version must be positive")
        if not isinstance(target_snapshot, dict) or not isinstance(
            parameter_snapshot, dict
        ):
            raise TypeError("account action snapshots must be JSON objects")

        now = utcnow()
        stmt = (
            insert(AiAgentActionExecution)
            .values(
                id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                external_account_id=external_account_id,
                action_type=normalized_action,
                status="queued",
                idempotency_key=normalized_key,
                execution_setting_version=int(execution_setting_version),
                target_snapshot=dict(target_snapshot),
                parameter_snapshot=dict(parameter_snapshot),
                approval_source=normalized_approval,
                trigger_source=normalized_trigger,
                stable_error_code=None,
                external_result_id=None,
                queued_at=now,
                started_at=None,
                completed_at=None,
                cancelled_at=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                constraint=(
                    "uq_ai_agent_action_executions_owner_action_idempotency"
                )
            )
            .returning(AiAgentActionExecution)
        )
        created = self.db.scalars(stmt).first()
        if created is not None:
            return created, True
        existing = self.get_by_idempotency(
            owner_user_id,
            action_type=normalized_action,
            idempotency_key=normalized_key,
            for_update=True,
        )
        if existing is None:
            raise RuntimeError(
                "account action idempotency conflict occurred but row was not found"
            )
        self._mark_stale_running_row(existing, at=now)
        return existing, False

    def _mark_stale_running_row(
        self,
        row: AiAgentActionExecution,
        *,
        at: datetime,
        timeout: timedelta = AGENT_RUNNING_TIMEOUT,
    ) -> bool:
        """Fail closed when a side effect may have escaped before a crash."""

        if (
            row.status != "running"
            or row.started_at is None
            or row.started_at > at - timeout
        ):
            return False
        row.status = "manual_review"
        row.stable_error_code = ACTION_EXECUTION_TIMEOUT_FAILURE_CODE
        row.external_result_id = None
        row.completed_at = at
        row.cancelled_at = None
        row.updated_at = at
        self.db.flush()
        return True

    def mark_stale_running_for_manual_review(
        self,
        *,
        at: datetime | None = None,
        timeout: timedelta = AGENT_RUNNING_TIMEOUT,
    ) -> int:
        """Close abandoned side effects without ever retrying them automatically."""

        now = at or utcnow()
        result = self.db.execute(
            update(AiAgentActionExecution)
            .where(
                AiAgentActionExecution.status == "running",
                AiAgentActionExecution.started_at <= now - timeout,
            )
            .values(
                status="manual_review",
                stable_error_code=ACTION_EXECUTION_TIMEOUT_FAILURE_CODE,
                external_result_id=None,
                completed_at=now,
                cancelled_at=None,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    def claim_next_queued(self) -> AiAgentActionExecution | None:
        stmt = (
            select(AiAgentActionExecution)
            .where(AiAgentActionExecution.status == "queued")
            .order_by(
                AiAgentActionExecution.queued_at,
                AiAgentActionExecution.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        row = self.db.scalar(stmt)
        if row is None:
            return None
        now = utcnow()
        row.status = "running"
        row.started_at = now
        row.completed_at = None
        row.cancelled_at = None
        row.stable_error_code = None
        row.updated_at = now
        self.db.flush()
        return row

    def start(
        self, owner_user_id: uuid.UUID, execution_id: uuid.UUID
    ) -> AiAgentActionExecution | None:
        row = self.get(owner_user_id, execution_id, for_update=True)
        if row is None or row.status != "queued":
            return None
        now = utcnow()
        row.status = "running"
        row.started_at = now
        row.completed_at = None
        row.cancelled_at = None
        row.stable_error_code = None
        row.updated_at = now
        self.db.flush()
        return row

    def succeed(
        self,
        owner_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        external_result_id: str | None = None,
    ) -> AiAgentActionExecution | None:
        row = self.get(owner_user_id, execution_id, for_update=True)
        if row is None or row.status != "running":
            return None
        now = utcnow()
        result_id = str(external_result_id or "").strip()
        row.status = "succeeded"
        row.external_result_id = result_id[:256] or None
        row.stable_error_code = None
        row.completed_at = now
        row.cancelled_at = None
        row.updated_at = now
        self.db.flush()
        return row

    def fail(
        self,
        owner_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        stable_error_code: str,
    ) -> AiAgentActionExecution | None:
        row = self.get(owner_user_id, execution_id, for_update=True)
        if row is None or row.status != "running":
            return None
        now = utcnow()
        row.status = "failed"
        row.stable_error_code = _normalize_stable_error_code(
            stable_error_code,
            fallback="account_action_failed",
        )
        row.external_result_id = None
        row.completed_at = now
        row.cancelled_at = None
        row.updated_at = now
        self.db.flush()
        return row

    def mark_outcome_unknown(
        self,
        owner_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        stable_error_code: str,
    ) -> AiAgentActionExecution | None:
        row = self.get(owner_user_id, execution_id, for_update=True)
        if row is None or row.status != "running":
            return None
        now = utcnow()
        row.status = "manual_review"
        row.stable_error_code = _normalize_stable_error_code(
            stable_error_code,
            fallback="account_action_outcome_unknown",
        )
        row.external_result_id = None
        row.completed_at = now
        row.cancelled_at = None
        row.updated_at = now
        self.db.flush()
        return row

    def cancel_queued(
        self,
        owner_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        stable_error_code: str = "user_cancelled",
    ) -> AiAgentActionExecution | None:
        row = self.get(owner_user_id, execution_id, for_update=True)
        if row is None or row.status != "queued":
            return None
        now = utcnow()
        row.status = "cancelled"
        row.stable_error_code = _normalize_stable_error_code(
            stable_error_code,
            fallback="user_cancelled",
        )
        row.external_result_id = None
        row.completed_at = now
        row.cancelled_at = now
        row.updated_at = now
        self.db.flush()
        return row

    def cancel_queued_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        stable_error_code: str = "access_revoked",
    ) -> int:
        return self._cancel_queued(
            owner_user_id=owner_user_id,
            stable_error_code=stable_error_code,
        )

    def cancel_all_queued(
        self,
        *,
        stable_error_code: str = "system_disabled",
    ) -> int:
        return self._cancel_queued(stable_error_code=stable_error_code)

    def cancel_active_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        stable_error_code: str = "access_revoked",
    ) -> int:
        """Cancel work that has not started; running side effects are audit facts."""

        return self.cancel_queued_for_owner(
            owner_user_id,
            stable_error_code=stable_error_code,
        )

    def cancel_all_active(
        self,
        *,
        stable_error_code: str = "system_disabled",
    ) -> int:
        """Cancel all queued work while preserving already-running executions."""

        return self.cancel_all_queued(stable_error_code=stable_error_code)

    def _cancel_queued(
        self,
        *,
        stable_error_code: str,
        owner_user_id: uuid.UUID | None = None,
    ) -> int:
        now = utcnow()
        conditions = [AiAgentActionExecution.status == "queued"]
        if owner_user_id is not None:
            conditions.append(
                AiAgentActionExecution.owner_user_id == owner_user_id
            )
        result = self.db.execute(
            update(AiAgentActionExecution)
            .where(*conditions)
            .values(
                status="cancelled",
                stable_error_code=_normalize_stable_error_code(
                    stable_error_code,
                    fallback="account_action_cancelled",
                ),
                external_result_id=None,
                completed_at=now,
                cancelled_at=now,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)


def _normalize_autonomy_targets(values: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        target = str(value or "").strip()
        if (
            not target
            or len(target) > 128
            or any(ord(character) < 33 for character in target)
            or any(character in target for character in "*?[]")
        ):
            raise ValueError("invalid autonomous target allowlist entry")
        if target not in normalized:
            normalized.append(target)
    if len(normalized) > 100:
        raise ValueError("autonomous target allowlist is too large")
    return normalized


def _autonomy_message_revoked(message: Message) -> bool:
    metadata = message.extra_data if isinstance(message.extra_data, Mapping) else {}
    return bool(
        str(message.status or "").lower() == "revoked"
        or str(metadata.get("revoked") or "").strip().lower() in {"1", "true"}
    )


def _autonomy_message_identity(message: Message) -> str:
    metadata = message.extra_data if isinstance(message.extra_data, Mapping) else {}
    return autonomy_message_identity(
        provider=message.provider,
        upstream_message_id=message.upstream_message_id,
        canonical_message_id=metadata.get("canonical_message_id"),
        direction=message.direction,
        message_type=message.message_type,
        body=message.body,
        revoked=_autonomy_message_revoked(message),
    )


@dataclass(frozen=True, slots=True)
class AutonomyConversationHeadRow:
    message_id: uuid.UUID
    peer_upstream_uid: str
    message_identity: str
    direction: str
    message_type: str
    body: str
    occurred_at: datetime
    revoked: bool
    conversation_title: str = ""


@dataclass(frozen=True, slots=True)
class ContactConversationState:
    signals: ContactRelationshipSignals
    conversation_ids: tuple[uuid.UUID, ...]
    conversation_titles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentAutonomyDispatchContext:
    task_id: uuid.UUID
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    upstream_uid: str
    match_pool_online_list_enabled: bool
    policy_version: int
    execution_setting_version: int
    action_type: str
    action_idempotency_key: str
    action_execution_id: uuid.UUID
    source_message_identity: str


@dataclass(frozen=True, slots=True)
class AgentAutonomyPreparedExecution:
    task_id: uuid.UUID
    owner_user_id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    stable_error_code: str
    result_id: str
    should_execute: bool


class AgentDiscoveryCandidateRepository:
    """Durable, owner-scoped results from Agent discovery scans."""

    PROFILE_FIELDS = frozenset(
        {
            "uid",
            "id",
            "nickname",
            "name",
            "city",
            "gender",
            "sex",
            "property",
            "age",
            "signature",
            "online",
            "is_online",
            "is_following",
            "is_friend",
            "is_friend_apply",
            "has_incoming_friend_apply",
            "friend_apply_status",
            "incoming_friend_apply_status",
            "is_follower",
            "is_fans",
        }
    )

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _target(value: object) -> str:
        target = str(value or "").strip()
        if (
            not target
            or len(target) > 128
            or any(ord(character) < 33 for character in target)
        ):
            raise ValueError("invalid discovery candidate target")
        return target

    @classmethod
    def _snapshot(cls, candidate: Mapping[str, Any], target: str) -> dict[str, Any]:
        result: dict[str, Any] = {"uid": target, "id": target}
        for field in cls.PROFILE_FIELDS:
            if field in {"uid", "id"}:
                continue
            value = candidate.get(field)
            if value is None or isinstance(value, (bool, int, float)):
                if field in candidate:
                    result[field] = value
            elif isinstance(value, str):
                result[field] = value[:1000]
        return result

    def remember_many(
        self,
        *,
        owner_user_id: uuid.UUID,
        candidates: Sequence[Mapping[str, Any]],
        source: str,
        seen_at: datetime | None = None,
    ) -> int:
        normalized_source = str(source or "online").strip().lower()
        if normalized_source not in {"online", "match"}:
            raise ValueError("invalid discovery candidate source")
        now = seen_at or utcnow()
        remembered = 0
        seen: set[str] = set()
        for candidate in list(candidates)[:50]:
            if not isinstance(candidate, Mapping):
                continue
            try:
                target = self._target(
                    candidate.get("uid")
                    or candidate.get("user_id")
                    or candidate.get("id")
                )
            except ValueError:
                continue
            if target in seen:
                continue
            seen.add(target)
            display_name = str(
                candidate.get("nickname") or candidate.get("name") or ""
            ).strip()[:160]
            statement = insert(AiAgentDiscoveryCandidate).values(
                id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                target_upstream_uid=target,
                source=normalized_source,
                display_name=display_name or None,
                profile_snapshot=self._snapshot(candidate, target),
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
            excluded = statement.excluded
            self.db.execute(
                statement.on_conflict_do_update(
                    constraint="uq_ai_agent_discovery_candidates_owner_target",
                    set_={
                        "source": excluded.source,
                        "display_name": excluded.display_name,
                        "profile_snapshot": excluded.profile_snapshot,
                        "last_seen_at": excluded.last_seen_at,
                        "updated_at": excluded.updated_at,
                    },
                )
            )
            remembered += 1
        return remembered

    def get(
        self,
        *,
        owner_user_id: uuid.UUID,
        target_upstream_uid: str,
        for_update: bool = False,
    ) -> AiAgentDiscoveryCandidate | None:
        stmt = select(AiAgentDiscoveryCandidate).where(
            AiAgentDiscoveryCandidate.owner_user_id == owner_user_id,
            AiAgentDiscoveryCandidate.target_upstream_uid
            == self._target(target_upstream_uid),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def list_recent_eligible(
        self,
        owner_user_id: uuid.UUID,
        *,
        seen_after: datetime,
        interaction_before: datetime,
        limit: int = 20,
        owner_profile: Mapping[str, object] | None = None,
        match_preference: object = None,
    ) -> list[AiAgentDiscoveryCandidate]:
        candidates = list(
            self.db.scalars(
                select(AiAgentDiscoveryCandidate)
                .where(
                    AiAgentDiscoveryCandidate.owner_user_id == owner_user_id,
                    AiAgentDiscoveryCandidate.last_seen_at >= seen_after,
                    or_(
                        AiAgentDiscoveryCandidate.last_interaction_at.is_(None),
                        AiAgentDiscoveryCandidate.last_interaction_at
                        <= interaction_before,
                    ),
                )
                .order_by(
                    AiAgentDiscoveryCandidate.last_seen_at.desc(),
                    AiAgentDiscoveryCandidate.id,
                )
                .limit(min(max(1, int(limit)), 50))
            )
        )
        if owner_profile is None:
            return candidates
        return [
            candidate
            for candidate in candidates
            if agent_candidate_compatibility(
                owner_profile=owner_profile,
                candidate_profile=(
                    candidate.profile_snapshot
                    if isinstance(candidate.profile_snapshot, Mapping)
                    else {}
                ),
                target_upstream_uid=candidate.target_upstream_uid,
                match_preference=match_preference,
            ).allowed
        ]

    def mark_interaction(
        self,
        *,
        owner_user_id: uuid.UUID,
        target_upstream_uid: str,
        action_type: str,
        at: datetime,
    ) -> bool:
        result = self.db.execute(
            update(AiAgentDiscoveryCandidate)
            .where(
                AiAgentDiscoveryCandidate.owner_user_id == owner_user_id,
                AiAgentDiscoveryCandidate.target_upstream_uid
                == self._target(target_upstream_uid),
            )
            .values(
                last_interaction_at=at,
                last_action_type=_normalize_action_type(action_type),
                updated_at=at,
            )
        )
        return bool(result.rowcount)


class AgentContactPolicyRepository:
    """Owner-scoped contact authorization and relationship facts."""

    _UNAVAILABLE_TITLE_MARKERS = ("已注销", "已封禁", "注销或封禁")

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def normalize_peer(value: object) -> str:
        return _normalize_autonomy_targets((str(value or ""),))[0]

    def get(
        self,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
        *,
        for_update: bool = False,
    ) -> AiAgentContactPolicy | None:
        peer = self.normalize_peer(peer_upstream_uid)
        stmt = select(AiAgentContactPolicy).where(
            AiAgentContactPolicy.owner_user_id == owner_user_id,
            AiAgentContactPolicy.peer_upstream_uid == peer,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    @staticmethod
    def _normalized_updates(updates: Mapping[str, object]) -> dict[str, object]:
        supported = {
            "mode",
            "stage_override",
            "paused",
            "minimum_reply_delay_seconds",
            "maximum_reply_age_seconds",
            "allow_address_terms",
        }
        unknown = set(updates) - supported
        if unknown:
            raise ValueError("unsupported contact policy field")
        normalized: dict[str, object] = {}
        if "mode" in updates:
            mode = str(updates.get("mode") or "").strip().lower()
            if mode not in CONTACT_POLICY_MODES:
                raise ValueError("unsupported contact policy mode")
            normalized["mode"] = mode
        if "stage_override" in updates:
            raw_stage = updates.get("stage_override")
            stage = str(raw_stage or "").strip().lower() or None
            if stage is not None and stage not in RELATIONSHIP_STAGES:
                raise ValueError("unsupported relationship stage override")
            normalized["stage_override"] = stage
        if "paused" in updates:
            paused = updates.get("paused")
            if type(paused) is not bool:
                raise ValueError("contact policy paused state is invalid")
            normalized["paused"] = paused
        if "minimum_reply_delay_seconds" in updates:
            raw_delay = updates.get("minimum_reply_delay_seconds")
            if type(raw_delay) is not int:
                raise ValueError("contact reply delay is invalid")
            delay = raw_delay
            if not MINIMUM_REPLY_DELAY_SECONDS <= delay <= MAXIMUM_REPLY_DELAY_SECONDS:
                raise ValueError("contact reply delay is outside the supported range")
            normalized["minimum_reply_delay_seconds"] = delay
        if "maximum_reply_age_seconds" in updates:
            raw_age = updates.get("maximum_reply_age_seconds")
            if type(raw_age) is not int:
                raise ValueError("contact reply age is invalid")
            maximum_age = raw_age
            if not MINIMUM_REPLY_AGE_SECONDS <= maximum_age <= MAXIMUM_REPLY_AGE_SECONDS:
                raise ValueError("contact reply age is outside the supported range")
            normalized["maximum_reply_age_seconds"] = maximum_age
        if "allow_address_terms" in updates:
            raw_terms = updates.get("allow_address_terms")
            if isinstance(raw_terms, (str, bytes)) or not isinstance(
                raw_terms, Sequence
            ):
                raise ValueError("contact address terms must be an array")
            supported_terms = set(AUTONOMY_RELATIONSHIP_ADDRESS_TERMS)
            terms: list[str] = []
            for raw_term in raw_terms:
                term = str(raw_term or "").strip()
                if term not in supported_terms:
                    raise ValueError("contact address term is unsupported")
                if term not in terms:
                    terms.append(term)
            if len(terms) > 20:
                raise ValueError("too many contact address terms")
            normalized["allow_address_terms"] = terms
        delay = int(
            normalized.get(
                "minimum_reply_delay_seconds",
                DEFAULT_MINIMUM_REPLY_DELAY_SECONDS,
            )
        )
        maximum_age = int(
            normalized.get(
                "maximum_reply_age_seconds",
                DEFAULT_MAXIMUM_REPLY_AGE_SECONDS,
            )
        )
        if (
            "minimum_reply_delay_seconds" in normalized
            and "maximum_reply_age_seconds" in normalized
            and maximum_age < delay
        ):
            raise ValueError("contact reply age must include the reply delay")
        return normalized

    def configure(
        self,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
        *,
        updates: Mapping[str, object],
    ) -> tuple[AiAgentContactPolicy, bool, bool]:
        peer = self.normalize_peer(peer_upstream_uid)
        normalized = self._normalized_updates(updates)
        inserted_id = self.db.scalar(
            insert(AiAgentContactPolicy)
            .values(
                id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                peer_upstream_uid=peer,
                mode=CONTACT_MODE_SUGGEST_ONLY,
                stage_override=None,
                paused=False,
                minimum_reply_delay_seconds=(
                    DEFAULT_MINIMUM_REPLY_DELAY_SECONDS
                ),
                maximum_reply_age_seconds=(
                    DEFAULT_MAXIMUM_REPLY_AGE_SECONDS
                ),
                allow_address_terms=[],
                version=1,
            )
            .on_conflict_do_nothing(
                constraint="uq_ai_agent_contact_policies_owner_peer"
            )
            .returning(AiAgentContactPolicy.id)
        )
        created = inserted_id is not None
        row = self.get(owner_user_id, peer, for_update=True)
        if row is None:
            raise RuntimeError("failed to create contact policy")

        effective_delay = int(
            normalized.get(
                "minimum_reply_delay_seconds",
                row.minimum_reply_delay_seconds,
            )
        )
        effective_age = int(
            normalized.get(
                "maximum_reply_age_seconds",
                row.maximum_reply_age_seconds,
            )
        )
        if effective_age < effective_delay:
            raise ValueError("contact reply age must include the reply delay")

        changed = created
        for field, value in normalized.items():
            current = getattr(row, field)
            if field == "allow_address_terms":
                current = list(current or [])
                value = list(value)  # type: ignore[arg-type]
            if current == value:
                continue
            setattr(row, field, value)
            changed = True
        if changed and not created:
            row.version = int(row.version) + 1
        if changed:
            row.updated_at = utcnow()
            self.db.flush()
        return row, created, changed

    def conversation_state(
        self,
        *,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
    ) -> ContactConversationState | None:
        peer = self.normalize_peer(peer_upstream_uid)
        conversation_rows = list(
            self.db.execute(
                select(Conversation.id, Conversation.title)
                .where(
                    Conversation.owner_user_id == owner_user_id,
                    Conversation.peer_upstream_uid == peer,
                )
                .order_by(
                    Conversation.last_message_at.desc().nullslast(),
                    Conversation.updated_at.desc(),
                )
            )
        )
        conversation_ids = tuple(row[0] for row in conversation_rows)
        if not conversation_ids:
            return None
        titles = tuple(
            str(row[1] or "").strip()[:200]
            for row in conversation_rows
            if str(row[1] or "").strip()
        )
        metadata_revoked = func.lower(
            func.coalesce(Message.extra_data.op("->>")("revoked"), "false")
        )
        message_conditions = (
            Message.owner_user_id == owner_user_id,
            Message.conversation_id.in_(conversation_ids),
            Message.direction.in_(("incoming", "outgoing")),
            func.lower(Message.status) != "revoked",
            metadata_revoked.notin_(("true", "1")),
        )
        aggregate = self.db.execute(
            select(
                func.count(Message.id),
                func.count(Message.id).filter(Message.direction == "incoming"),
                func.count(Message.id).filter(Message.direction == "outgoing"),
                func.max(Message.occurred_at).filter(
                    Message.direction == "incoming"
                ),
                func.max(Message.occurred_at).filter(
                    Message.direction == "outgoing"
                ),
            ).where(*message_conditions)
        ).one()
        latest = self.db.execute(
            select(Message.direction, Message.occurred_at)
            .where(*message_conditions)
            .order_by(
                Message.occurred_at.desc(),
                Message.created_at.desc(),
                Message.id.desc(),
            )
            .limit(1)
        ).first()
        blocked = bool(
            self.db.scalar(
                select(Relationship.id)
                .where(
                    Relationship.owner_user_id == owner_user_id,
                    Relationship.subject_upstream_uid == peer,
                    Relationship.kind.in_(("blacklist", "blacklisted_by")),
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                )
                .limit(1)
            )
        )
        available = not any(
            marker in title
            for title in titles
            for marker in self._UNAVAILABLE_TITLE_MARKERS
        )
        signals = ContactRelationshipSignals(
            total_message_count=int(aggregate[0] or 0),
            incoming_message_count=int(aggregate[1] or 0),
            outgoing_message_count=int(aggregate[2] or 0),
            last_incoming_at=aggregate[3],
            last_outgoing_at=aggregate[4],
            latest_direction=str(latest[0] or "") if latest else "",
            latest_message_at=latest[1] if latest else None,
            blocked=blocked,
            conversation_available=available,
        )
        return ContactConversationState(
            signals=signals,
            conversation_ids=conversation_ids,
            conversation_titles=titles,
        )

    def relationship_stage(
        self,
        *,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
        policy: AiAgentContactPolicy | None,
        now: datetime,
    ) -> str | None:
        state = self.conversation_state(
            owner_user_id=owner_user_id,
            peer_upstream_uid=peer_upstream_uid,
        )
        if state is None:
            return None
        return derive_relationship_stage(
            state.signals,
            now=now,
            mode=(policy.mode if policy is not None else CONTACT_MODE_SUGGEST_ONLY),
            stage_override=(policy.stage_override if policy is not None else None),
        )


class AgentAutonomySettingRepository:
    """Owner-scoped autonomous policy persistence; every default is disabled."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        owner_user_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiAgentAutonomySetting | None:
        stmt = select(AiAgentAutonomySetting).where(
            AiAgentAutonomySetting.owner_user_id == owner_user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_or_create(
        self,
        owner_user_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiAgentAutonomySetting:
        row = self.get(owner_user_id, for_update=for_update)
        if row is not None:
            return row
        row = AiAgentAutonomySetting(
            id=uuid.uuid4(),
            owner_user_id=owner_user_id,
            user_enabled=False,
            auto_reply_enabled=False,
            auto_reply_started_at=None,
            scheduled_post_enabled=False,
            managed_relationships_enabled=False,
            discovery_enabled=False,
            text_match_enabled=False,
            proactive_message_enabled=False,
            follow_discovered_enabled=False,
            friend_request_enabled=False,
            allowed_actions=[],
            operation_brief="",
            managed_target_uids=[],
            timezone="UTC",
            active_start_minute=0,
            active_end_minute=0,
            minimum_action_interval_seconds=30,
            daily_total_limit=20,
            daily_reply_limit=10,
            daily_post_limit=1,
            daily_relationship_limit=5,
            post_interval_minutes=1440,
            discovery_interval_minutes=30,
            discovery_interval_seconds=10,
            consecutive_failure_limit=3,
            consecutive_failures=0,
            version=1,
        )
        self.db.add(row)
        self.db.flush()
        if for_update:
            row = self.get(owner_user_id, for_update=True) or row
        return row

    def configure(
        self,
        owner_user_id: uuid.UUID,
        *,
        user_enabled: bool,
        auto_reply_enabled: bool,
        scheduled_post_enabled: bool,
        managed_relationships_enabled: bool,
        discovery_enabled: bool,
        text_match_enabled: bool,
        proactive_message_enabled: bool,
        follow_discovered_enabled: bool,
        friend_request_enabled: bool,
        allowed_actions: Sequence[str],
        operation_brief: str,
        managed_target_uids: Sequence[str],
        timezone: str,
        active_start_minute: int,
        active_end_minute: int,
        minimum_action_interval_seconds: int,
        daily_total_limit: int,
        daily_reply_limit: int,
        daily_post_limit: int,
        daily_relationship_limit: int,
        post_interval_minutes: int,
        discovery_interval_seconds: int,
        consecutive_failure_limit: int,
    ) -> AiAgentAutonomySetting:
        enabled = bool(user_enabled)
        reply = bool(auto_reply_enabled) if enabled else False
        posts = bool(scheduled_post_enabled) if enabled else False
        relationships = bool(managed_relationships_enabled) if enabled else False
        discovery = bool(discovery_enabled) if enabled else False
        matching = bool(text_match_enabled) if enabled else False
        outreach = bool(proactive_message_enabled) if enabled else False
        discovered_follow = bool(follow_discovered_enabled) if enabled else False
        friend_requests = bool(friend_request_enabled) if enabled else False
        actions = _normalize_allowed_actions(tuple(allowed_actions))
        targets = _normalize_autonomy_targets(tuple(managed_target_uids))
        brief = str(operation_brief or "").strip()
        zone = str(timezone or "UTC").strip()
        if len(brief) > 4000:
            raise ValueError("autonomous operation brief is too long")
        if not 1 <= len(zone) <= 64:
            raise ValueError("invalid autonomous timezone")
        start_minute = int(active_start_minute)
        end_minute = int(active_end_minute)
        minimum_interval = int(minimum_action_interval_seconds)
        total_limit = int(daily_total_limit)
        reply_limit = int(daily_reply_limit)
        post_limit = int(daily_post_limit)
        relationship_limit = int(daily_relationship_limit)
        post_interval = int(post_interval_minutes)
        discovery_interval = int(discovery_interval_seconds)
        legacy_discovery_interval = (
            30
            if discovery_interval == 10
            else max(5, min(1440, (discovery_interval + 59) // 60))
        )
        failure_limit = int(consecutive_failure_limit)
        if not 0 <= start_minute < 1440 or not 0 <= end_minute < 1440:
            raise ValueError("autonomous active time is invalid")
        if not 10 <= minimum_interval <= 86400:
            raise ValueError("autonomous minimum interval is invalid")
        if not 1 <= total_limit <= 200:
            raise ValueError("autonomous total daily limit is invalid")
        if not 0 <= reply_limit <= min(200, total_limit):
            raise ValueError("autonomous reply daily limit is invalid")
        if not 0 <= post_limit <= min(20, total_limit):
            raise ValueError("autonomous post daily limit is invalid")
        if not 0 <= relationship_limit <= min(100, total_limit):
            raise ValueError("autonomous relationship daily limit is invalid")
        if not 60 <= post_interval <= 10080:
            raise ValueError("autonomous post interval is invalid")
        if not 10 <= discovery_interval <= 86400:
            raise ValueError("autonomous discovery interval is invalid")
        if not 1 <= failure_limit <= 20:
            raise ValueError("autonomous failure threshold is invalid")
        if enabled and not (
            reply
            or posts
            or relationships
            or discovery
            or matching
            or outreach
            or discovered_follow
            or friend_requests
        ):
            raise ValueError("enabled autonomy requires one capability")
        if reply and "send_private_message" not in actions:
            raise ValueError("auto reply requires send_private_message")
        if reply and reply_limit < 1:
            raise ValueError("auto reply requires a positive daily budget")
        if posts and "publish_text_post" not in actions:
            raise ValueError("scheduled post requires publish_text_post")
        if posts and post_limit < 1:
            raise ValueError("scheduled posts require a positive daily budget")
        if relationships and not ({"follow_user", "unfollow_user"} & set(actions)):
            raise ValueError("relationship automation requires a relationship action")
        if relationships and {"follow_user", "unfollow_user"} <= set(actions):
            raise ValueError(
                "relationship automation requires one unambiguous target state"
            )
        if relationships and not targets:
            raise ValueError("relationship automation requires exact targets")
        if relationships and relationship_limit < 1:
            raise ValueError("relationship automation requires a positive daily budget")
        if discovery and "browse_online_users" not in actions:
            raise ValueError("discovery automation requires browse_online_users")
        if matching and "request_text_match" not in actions:
            raise ValueError("matching automation requires request_text_match")
        if outreach and "send_private_message" not in actions:
            raise ValueError("proactive messaging requires send_private_message")
        if outreach and reply_limit < 1:
            raise ValueError("proactive messaging requires a positive daily budget")
        if discovered_follow and "follow_user" not in actions:
            raise ValueError("discovered follow requires follow_user")
        if discovered_follow and relationship_limit < 1:
            raise ValueError("discovered follow requires a positive daily budget")
        if friend_requests and "request_friend" not in actions:
            raise ValueError("friend requests require request_friend")
        if friend_requests and relationship_limit < 1:
            raise ValueError("friend requests require a positive daily budget")

        row = self.get_or_create(owner_user_id, for_update=True)
        now = utcnow()
        reply_watermark = (
            row.auto_reply_started_at
            if reply and bool(row.auto_reply_enabled) and row.auto_reply_started_at
            else now if reply else None
        )
        values = {
            "user_enabled": enabled,
            "auto_reply_enabled": reply,
            "auto_reply_started_at": reply_watermark,
            "scheduled_post_enabled": posts,
            "managed_relationships_enabled": relationships,
            "discovery_enabled": discovery,
            "text_match_enabled": matching,
            "proactive_message_enabled": outreach,
            "follow_discovered_enabled": discovered_follow,
            "friend_request_enabled": friend_requests,
            "allowed_actions": actions,
            "operation_brief": brief,
            "managed_target_uids": targets,
            "timezone": zone,
            "active_start_minute": start_minute,
            "active_end_minute": end_minute,
            "minimum_action_interval_seconds": minimum_interval,
            "daily_total_limit": total_limit,
            "daily_reply_limit": reply_limit,
            "daily_post_limit": post_limit,
            "daily_relationship_limit": relationship_limit,
            "post_interval_minutes": post_interval,
            "discovery_interval_minutes": legacy_discovery_interval,
            "discovery_interval_seconds": discovery_interval,
            "consecutive_failure_limit": failure_limit,
        }
        changed = any(getattr(row, name) != value for name, value in values.items())
        halted = row.halted_at is not None
        if changed:
            for name, value in values.items():
                setattr(row, name, value)
            row.version = max(1, int(row.version or 1)) + 1
        if changed or halted:
            # 用户重新提交策略即视为确认恢复：即使内容未变，也要解除停机并
            # 重置连续失败计数；版本号只在策略内容变化时递增，避免误作废旧任务。
            row.consecutive_failures = 0
            row.halted_at = None
            row.halted_reason = None
            row.next_run_at = now if enabled else None
            row.updated_at = now
            self.db.flush()
        return row

    def disable_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        reason: str = "autonomy_disabled",
        halt_reason: str | None = None,
    ) -> bool:
        row = self.get(owner_user_id, for_update=True)
        if row is None:
            return False
        changed = bool(
            row.user_enabled
            or row.auto_reply_enabled
            or row.scheduled_post_enabled
            or row.managed_relationships_enabled
            or row.discovery_enabled
            or row.text_match_enabled
            or row.proactive_message_enabled
            or row.follow_discovered_enabled
            or row.friend_request_enabled
        )
        if not changed:
            return False
        now = utcnow()
        row.user_enabled = False
        row.auto_reply_enabled = False
        row.auto_reply_started_at = None
        row.scheduled_post_enabled = False
        row.managed_relationships_enabled = False
        row.discovery_enabled = False
        row.text_match_enabled = False
        row.proactive_message_enabled = False
        row.follow_discovered_enabled = False
        row.friend_request_enabled = False
        row.next_run_at = None
        row.halted_at = now
        row.halted_reason = _normalize_stable_error_code(
            halt_reason or reason,
            fallback="autonomy_disabled",
        )
        row.version = max(1, int(row.version or 1)) + 1
        row.updated_at = now
        self.db.flush()
        return True

    def disable_all(
        self,
        *,
        reason: str = "autonomy_disabled",
        halt_reason: str | None = None,
    ) -> int:
        now = utcnow()
        result = self.db.execute(
            update(AiAgentAutonomySetting)
            .where(
                or_(
                    AiAgentAutonomySetting.user_enabled.is_(True),
                    AiAgentAutonomySetting.auto_reply_enabled.is_(True),
                    AiAgentAutonomySetting.scheduled_post_enabled.is_(True),
                    AiAgentAutonomySetting.managed_relationships_enabled.is_(True),
                    AiAgentAutonomySetting.discovery_enabled.is_(True),
                    AiAgentAutonomySetting.text_match_enabled.is_(True),
                    AiAgentAutonomySetting.proactive_message_enabled.is_(True),
                    AiAgentAutonomySetting.follow_discovered_enabled.is_(True),
                    AiAgentAutonomySetting.friend_request_enabled.is_(True),
                )
            )
            .values(
                user_enabled=False,
                auto_reply_enabled=False,
                auto_reply_started_at=None,
                scheduled_post_enabled=False,
                managed_relationships_enabled=False,
                discovery_enabled=False,
                text_match_enabled=False,
                proactive_message_enabled=False,
                follow_discovered_enabled=False,
                friend_request_enabled=False,
                next_run_at=None,
                halted_at=now,
                halted_reason=_normalize_stable_error_code(
                    halt_reason or reason,
                    fallback="autonomy_disabled",
                ),
                version=AiAgentAutonomySetting.version + 1,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    def due(
        self,
        *,
        at: datetime | None = None,
        limit: int = 10,
        for_update: bool = False,
    ) -> list[AiAgentAutonomySetting]:
        now = at or utcnow()
        stmt = (
            select(AiAgentAutonomySetting)
            .where(
                AiAgentAutonomySetting.user_enabled.is_(True),
                AiAgentAutonomySetting.halted_at.is_(None),
                or_(
                    AiAgentAutonomySetting.next_run_at.is_(None),
                    AiAgentAutonomySetting.next_run_at <= now,
                ),
            )
            .order_by(AiAgentAutonomySetting.next_run_at.asc().nullsfirst())
            .limit(min(max(1, int(limit)), 50))
        )
        if for_update:
            stmt = stmt.with_for_update(skip_locked=True)
        return list(self.db.scalars(stmt))

    def get_policy_snapshot(
        self,
        owner_user_id: uuid.UUID,
        *,
        for_update: bool = False,
        target_upstream_uid: str = "",
    ) -> dict[str, Any] | None:
        def scalar(model: Any, *conditions: Any) -> Any:
            stmt = select(model).where(*conditions)
            if for_update:
                stmt = stmt.with_for_update()
            return self.db.scalar(stmt)

        # The global row is first. Existing login and account-suspension paths
        # then lock ExternalAccount before User, so workers must preserve that
        # order. When a local target exists, lock both account and user pairs in
        # stable UUID order before any single user row; the canonical social
        # repository uses the same ordered user-pair rule.
        system = scalar(
            AiModelRunnerSystemSetting,
            AiModelRunnerSystemSetting.id == 1,
        )
        account_candidate = self.db.scalar(
            select(ExternalAccount).where(
                ExternalAccount.user_id == owner_user_id
            )
        )
        target_account = None
        target_uid = str(target_upstream_uid or "").strip()
        if account_candidate is not None and target_uid:
            target_account = self.db.scalar(
                select(ExternalAccount).where(
                    ExternalAccount.provider == account_candidate.provider,
                    ExternalAccount.upstream_uid == target_uid,
                )
            )
        if for_update:
            account_ids = {
                row.id
                for row in (account_candidate, target_account)
                if row is not None
            }
            locked_accounts = (
                list(
                    self.db.scalars(
                        select(ExternalAccount)
                        .where(ExternalAccount.id.in_(account_ids))
                        .order_by(ExternalAccount.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                if account_ids
                else []
            )
            accounts_by_id = {row.id: row for row in locked_accounts}
            account = (
                accounts_by_id.get(account_candidate.id)
                if account_candidate is not None
                else None
            )
            locked_target = (
                accounts_by_id.get(target_account.id)
                if target_account is not None
                else None
            )
            target_mapping_current = True
            if target_uid and account is not None:
                current_target_id = self.db.scalar(
                    select(ExternalAccount.id).where(
                        ExternalAccount.provider == account.provider,
                        ExternalAccount.upstream_uid == target_uid,
                    )
                )
                target_mapping_current = current_target_id == (
                    locked_target.id if locked_target is not None else None
                )
            user_ids = {owner_user_id}
            if locked_target is not None:
                user_ids.add(locked_target.user_id)
            locked_users = list(
                self.db.scalars(
                    select(User)
                    .where(User.id.in_(user_ids))
                    .order_by(User.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            users_by_id = {row.id: row for row in locked_users}
            user = users_by_id.get(owner_user_id)
        else:
            account = account_candidate
            user = scalar(User, User.id == owner_user_id)
            locked_target = target_account
            target_mapping_current = True
        agent = scalar(
            AiAgentSetting,
            AiAgentSetting.owner_user_id == owner_user_id,
        )
        execution = scalar(
            AiAgentExecutionSetting,
            AiAgentExecutionSetting.owner_user_id == owner_user_id,
        )
        connection = None
        if agent is not None and agent.active_connection_id is not None:
            connection = scalar(
                AiModelConnection,
                AiModelConnection.owner_user_id == owner_user_id,
                AiModelConnection.id == agent.active_connection_id,
            )
        setting = self.get(owner_user_id, for_update=for_update)
        if setting is None:
            return None
        match_preference = scalar(
            MatchPreference,
            MatchPreference.user_id == owner_user_id,
        )
        contact_policy = (
            scalar(
                AiAgentContactPolicy,
                AiAgentContactPolicy.owner_user_id == owner_user_id,
                AiAgentContactPolicy.peer_upstream_uid == target_uid,
            )
            if target_uid
            else None
        )
        return {
            "autonomy_setting": setting,
            "system_setting": system,
            "user": user,
            "external_account": account,
            "agent_setting": agent,
            "execution_setting": execution,
            "connection": connection,
            "match_preference": match_preference,
            "contact_policy": contact_policy,
            "target_external_account": (
                locked_target if target_mapping_current else None
            ),
            "target_mapping_current": target_mapping_current,
        }


class AgentAutonomyDailyUsageRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        owner_user_id: uuid.UUID,
        usage_date: date,
        *,
        for_update: bool = False,
    ) -> AiAgentAutonomyDailyUsage | None:
        stmt = select(AiAgentAutonomyDailyUsage).where(
            AiAgentAutonomyDailyUsage.owner_user_id == owner_user_id,
            AiAgentAutonomyDailyUsage.usage_date == usage_date,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_many(
        self,
        owner_usage_dates: Mapping[uuid.UUID, date],
    ) -> dict[uuid.UUID, AiAgentAutonomyDailyUsage]:
        """批量预取各 owner 在其本地日期的用量行；无行的 owner 缺席。

        用两个 IN 条件取超集后在内存里按 (owner, date) 精确配对，避免
        调度循环里逐 owner 的单行 SELECT。
        """

        if not owner_usage_dates:
            return {}
        rows = self.db.scalars(
            select(AiAgentAutonomyDailyUsage).where(
                AiAgentAutonomyDailyUsage.owner_user_id.in_(
                    list(owner_usage_dates.keys())
                ),
                AiAgentAutonomyDailyUsage.usage_date.in_(
                    sorted(set(owner_usage_dates.values()))
                ),
            )
        )
        return {
            row.owner_user_id: row
            for row in rows
            if owner_usage_dates.get(row.owner_user_id) == row.usage_date
        }

    def get_or_create_for_update(
        self,
        owner_user_id: uuid.UUID,
        usage_date: date,
    ) -> AiAgentAutonomyDailyUsage:
        self.db.execute(
            insert(AiAgentAutonomyDailyUsage)
            .values(
                id=uuid.uuid4(),
                owner_user_id=owner_user_id,
                usage_date=usage_date,
                total_actions=0,
                reply_actions=0,
                outreach_actions=0,
                post_actions=0,
                relationship_actions=0,
                browse_actions=0,
                match_actions=0,
                failed_actions=0,
                outcome_unknown_actions=0,
            )
            .on_conflict_do_nothing(
                constraint="uq_ai_agent_autonomy_daily_usage_owner_date"
            )
        )
        row = self.get(owner_user_id, usage_date, for_update=True)
        if row is None:
            raise RuntimeError("failed to create autonomous daily usage row")
        return row


class AgentAutonomyTaskRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        owner_user_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AiAgentAutonomyTask | None:
        stmt = select(AiAgentAutonomyTask).where(
            AiAgentAutonomyTask.owner_user_id == owner_user_id,
            AiAgentAutonomyTask.id == task_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_idempotency(
        self,
        owner_user_id: uuid.UUID,
        idempotency_key: str,
    ) -> AiAgentAutonomyTask | None:
        return self.db.scalar(
            select(AiAgentAutonomyTask).where(
                AiAgentAutonomyTask.owner_user_id == owner_user_id,
                AiAgentAutonomyTask.idempotency_key
                == _normalize_idempotency_key(idempotency_key),
            )
        )

    def enqueue(
        self,
        *,
        owner_user_id: uuid.UUID,
        external_account_id: uuid.UUID,
        task_type: str,
        action_type: str,
        idempotency_key: str,
        policy_version: int,
        execution_setting_version: int,
        runner_setting_version: int,
        model_connection_id: uuid.UUID,
        runner_configuration_fingerprint: str,
        contact_policy_version: int | None = None,
        relationship_stage: str = "",
        source_message_id: uuid.UUID | None = None,
        source_message_identity: str = "",
        schedule_slot: str = "",
        target_upstream_uid: str = "",
        generation_instruction: str = "",
        scheduled_for: datetime | None = None,
        not_before: datetime | None = None,
    ) -> tuple[AiAgentAutonomyTask, bool]:
        normalized_type = str(task_type or "").strip()
        normalized_action = _normalize_action_type(action_type)
        if normalized_type not in AUTONOMY_TASK_TYPES:
            raise ValueError("unsupported autonomous task type")
        if AUTONOMY_TASK_ACTIONS[normalized_type] != normalized_action:
            raise ValueError("autonomous task type and action do not match")
        key = _normalize_idempotency_key(idempotency_key)
        source_identity = str(source_message_identity or "").strip()
        target = str(target_upstream_uid or "").strip()
        instruction = str(generation_instruction or "").strip()
        runner_fingerprint = str(
            runner_configuration_fingerprint or ""
        ).strip().lower()
        contact_version = (
            int(contact_policy_version)
            if contact_policy_version is not None
            else None
        )
        normalized_stage = str(relationship_stage or "").strip().lower()
        if len(source_identity) > 256 or any(ord(char) < 32 for char in source_identity):
            raise ValueError("invalid autonomous source identity")
        if target:
            target = _normalize_autonomy_targets((target,))[0]
        if len(instruction) > 4000:
            raise ValueError("autonomous generation instruction is too long")
        if contact_version is not None and contact_version < 1:
            raise ValueError("contact policy version must be positive")
        if normalized_stage and normalized_stage not in RELATIONSHIP_STAGES:
            raise ValueError("autonomous relationship stage is invalid")
        if normalized_type == "reply_to_message" and (
            contact_version is None or not normalized_stage
        ):
            raise ValueError("autonomous reply requires contact authorization")
        if int(runner_setting_version) < 1:
            raise ValueError("autonomous runner setting version must be positive")
        if not isinstance(model_connection_id, uuid.UUID):
            raise ValueError("autonomous model connection id is invalid")
        if len(runner_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in runner_fingerprint
        ):
            raise ValueError("autonomous runner fingerprint is invalid")
        values = {
            "id": uuid.uuid4(),
            "owner_user_id": owner_user_id,
            "external_account_id": external_account_id,
            "task_type": normalized_type,
            "action_type": normalized_action,
            "status": "queued",
            "idempotency_key": key,
            "policy_version": int(policy_version),
            "contact_policy_version": contact_version,
            "relationship_stage": normalized_stage or None,
            "execution_setting_version": int(execution_setting_version),
            "runner_setting_version": int(runner_setting_version),
            "model_connection_id": model_connection_id,
            "runner_configuration_fingerprint": runner_fingerprint,
            "source_message_id": source_message_id,
            "source_message_identity": source_identity or None,
            "schedule_slot": str(schedule_slot or "").strip() or None,
            "target_upstream_uid": target or None,
            "generation_instruction": instruction,
            "scheduled_for": scheduled_for,
            "not_before": not_before,
        }
        created = self.db.scalar(
            insert(AiAgentAutonomyTask)
            .values(**values)
            .on_conflict_do_nothing(
                constraint="uq_ai_agent_autonomy_tasks_owner_idempotency"
            )
            .returning(AiAgentAutonomyTask)
        )
        if created is not None:
            return created, True
        existing = self.get_by_idempotency(owner_user_id, key)
        if existing is None:
            raise RuntimeError("autonomous task idempotency lookup failed")
        return existing, False

    def list_recent(
        self,
        owner_user_id: uuid.UUID,
        *,
        limit: int = 20,
    ) -> list[AiAgentAutonomyTask]:
        return list(
            self.db.scalars(
                select(AiAgentAutonomyTask)
                .where(AiAgentAutonomyTask.owner_user_id == owner_user_id)
                .order_by(AiAgentAutonomyTask.created_at.desc())
                .limit(min(max(1, int(limit)), 100))
            )
        )

    def has_open_task(self, *, owner_user_id: uuid.UUID) -> bool:
        return bool(
            self.db.scalar(
                select(AiAgentAutonomyTask.id)
                .where(
                    AiAgentAutonomyTask.owner_user_id == owner_user_id,
                    ~AiAgentAutonomyTask.status.in_(
                        tuple(AUTONOMY_TERMINAL_STATUSES)
                    ),
                )
                .limit(1)
            )
        )

    def cancel_superseded_reply_tasks(
        self,
        *,
        owner_user_id: uuid.UUID,
        at: datetime,
    ) -> int:
        """Cancel queued reply work whose bound message is no longer the head."""

        rows = list(
            self.db.scalars(
                select(AiAgentAutonomyTask)
                .where(
                    AiAgentAutonomyTask.owner_user_id == owner_user_id,
                    AiAgentAutonomyTask.task_type == "reply_to_message",
                    AiAgentAutonomyTask.status.in_(("queued", "deferred")),
                )
                .order_by(AiAgentAutonomyTask.queued_at, AiAgentAutonomyTask.id)
                .with_for_update(skip_locked=True)
            )
        )
        cancelled = 0
        for task in rows:
            target = str(task.target_upstream_uid or "").strip()
            head = (
                self.get_conversation_head(
                    owner_user_id=owner_user_id,
                    peer_upstream_uid=target,
                )
                if target
                else None
            )
            if (
                head is not None
                and head.direction == "incoming"
                and not head.revoked
                and head.message_identity
                == str(task.source_message_identity or "")
            ):
                continue
            task.status = "stale"
            task.stable_error_code = "inbound_message_superseded"
            task.completed_at = at
            task.lease_owner = None
            task.lease_token = None
            task.lease_until = None
            task.updated_at = at
            cancelled += 1
        if cancelled:
            self.db.flush()
        return cancelled

    def cancel_reply_tasks_for_contact_policy_change(
        self,
        *,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
        at: datetime,
    ) -> int:
        """Invalidate reply work that has not entered final dispatch."""

        peer = AgentContactPolicyRepository.normalize_peer(peer_upstream_uid)
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.owner_user_id == owner_user_id,
                AiAgentAutonomyTask.target_upstream_uid == peer,
                AiAgentAutonomyTask.task_type == "reply_to_message",
                AiAgentAutonomyTask.status.in_(
                    tuple(AUTONOMY_NOT_STARTED_STATUSES)
                ),
            )
            .values(
                status="stale",
                stable_error_code="contact_policy_changed",
                completed_at=at,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                updated_at=at,
            )
        )
        return int(result.rowcount or 0)

    def has_open_task_for_target(
        self,
        *,
        owner_user_id: uuid.UUID,
        target_upstream_uid: str,
    ) -> bool:
        target = _normalize_autonomy_targets((target_upstream_uid,))[0]
        return bool(
            self.db.scalar(
                select(AiAgentAutonomyTask.id)
                .where(
                    AiAgentAutonomyTask.owner_user_id == owner_user_id,
                    AiAgentAutonomyTask.target_upstream_uid == target,
                    ~AiAgentAutonomyTask.status.in_(
                        tuple(AUTONOMY_TERMINAL_STATUSES)
                    ),
                )
                .limit(1)
            )
        )

    def has_open_task_type(
        self,
        *,
        owner_user_id: uuid.UUID,
        task_type: str,
    ) -> bool:
        normalized_type = str(task_type or "").strip()
        if normalized_type not in AUTONOMY_TASK_TYPES:
            raise ValueError("unsupported autonomous task type")
        return bool(
            self.db.scalar(
                select(AiAgentAutonomyTask.id)
                .where(
                    AiAgentAutonomyTask.owner_user_id == owner_user_id,
                    AiAgentAutonomyTask.task_type == normalized_type,
                    ~AiAgentAutonomyTask.status.in_(
                        tuple(AUTONOMY_TERMINAL_STATUSES)
                    ),
                )
                .limit(1)
            )
        )

    def target_is_blocked(
        self,
        *,
        owner_user_id: uuid.UUID,
        target_upstream_uid: str,
    ) -> bool:
        target = _normalize_autonomy_targets((target_upstream_uid,))[0]
        return bool(
            self.db.scalar(
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
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AiAgentAutonomyTask | None:
        worker = str(worker_id or "").strip()
        if not worker or len(worker) > 128:
            raise ValueError("invalid autonomous worker id")
        due = or_(
            and_(
                AiAgentAutonomyTask.status == "queued",
                or_(
                    AiAgentAutonomyTask.not_before.is_(None),
                    AiAgentAutonomyTask.not_before <= now,
                ),
            ),
            and_(
                AiAgentAutonomyTask.status == "deferred",
                AiAgentAutonomyTask.not_before.is_not(None),
                AiAgentAutonomyTask.not_before <= now,
            ),
            and_(
                AiAgentAutonomyTask.status.in_(("leased", "generating")),
                AiAgentAutonomyTask.lease_until.is_not(None),
                AiAgentAutonomyTask.lease_until <= now,
            ),
        )
        row = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(due)
            .order_by(
                AiAgentAutonomyTask.not_before.asc().nullsfirst(),
                AiAgentAutonomyTask.queued_at,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status = "leased"
        row.lease_owner = worker
        row.lease_token = secrets.token_urlsafe(32)
        row.lease_until = lease_until
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.started_at = row.started_at or now
        row.stable_error_code = None
        row.updated_at = now
        self.db.flush()
        return row

    def claim_task(
        self,
        task_id: uuid.UUID,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AiAgentAutonomyTask | None:
        worker = str(worker_id or "").strip()
        if not worker or len(worker) > 128:
            raise ValueError("invalid autonomous worker id")
        due = or_(
            and_(
                AiAgentAutonomyTask.status == "queued",
                or_(
                    AiAgentAutonomyTask.not_before.is_(None),
                    AiAgentAutonomyTask.not_before <= now,
                ),
            ),
            and_(
                AiAgentAutonomyTask.status == "deferred",
                AiAgentAutonomyTask.not_before.is_not(None),
                AiAgentAutonomyTask.not_before <= now,
            ),
            and_(
                AiAgentAutonomyTask.status.in_(("leased", "generating")),
                AiAgentAutonomyTask.lease_until.is_not(None),
                AiAgentAutonomyTask.lease_until <= now,
            ),
        )
        row = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(AiAgentAutonomyTask.id == task_id, due)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status = "leased"
        row.lease_owner = worker
        row.lease_token = secrets.token_urlsafe(32)
        row.lease_until = lease_until
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.started_at = row.started_at or now
        row.stable_error_code = None
        row.updated_at = now
        self.db.flush()
        return row

    def due_task_ids(
        self,
        *,
        now: datetime,
        limit: int = 10,
    ) -> list[uuid.UUID]:
        due = or_(
            and_(
                AiAgentAutonomyTask.status == "queued",
                or_(
                    AiAgentAutonomyTask.not_before.is_(None),
                    AiAgentAutonomyTask.not_before <= now,
                ),
            ),
            and_(
                AiAgentAutonomyTask.status == "deferred",
                AiAgentAutonomyTask.not_before.is_not(None),
                AiAgentAutonomyTask.not_before <= now,
            ),
            and_(
                AiAgentAutonomyTask.status.in_(("leased", "generating")),
                AiAgentAutonomyTask.lease_until.is_not(None),
                AiAgentAutonomyTask.lease_until <= now,
            ),
        )
        maximum = min(max(1, int(limit)), 50)
        rows = list(
            self.db.execute(
                select(AiAgentAutonomyTask, AiAgentAutonomySetting)
                .join(
                    AiAgentAutonomySetting,
                    AiAgentAutonomySetting.owner_user_id
                    == AiAgentAutonomyTask.owner_user_id,
                )
                .where(
                    due,
                    AiAgentAutonomySetting.user_enabled.is_(True),
                    AiAgentAutonomySetting.halted_at.is_(None),
                )
                .order_by(
                    AiAgentAutonomyTask.not_before.asc().nullsfirst(),
                    AiAgentAutonomyTask.queued_at,
                )
                .limit(min(maximum * 20, 1000))
            )
        )
        selected: list[uuid.UUID] = []
        selected_owners: set[uuid.UUID] = set()
        # 批量预取每个 owner 当天的用量行：候选最多 1000 条时逐行 get()
        # 会放大成同数量级的单行 SELECT。owner 与 setting 一一对应，本地
        # 日期按 owner 计算一次即可。
        owner_usage_dates: dict[uuid.UUID, date] = {}
        invalid_timezone_owners: set[uuid.UUID] = set()
        for task, setting in rows:
            owner = task.owner_user_id
            if owner in owner_usage_dates or owner in invalid_timezone_owners:
                continue
            try:
                owner_usage_dates[owner] = now.astimezone(
                    ZoneInfo(str(setting.timezone or "UTC"))
                ).date()
            except ZoneInfoNotFoundError:
                invalid_timezone_owners.add(owner)
        usage_by_owner = AgentAutonomyDailyUsageRepository(self.db).get_many(
            owner_usage_dates
        )
        for task, setting in rows:
            if task.owner_user_id in selected_owners:
                continue
            if setting.last_action_at is not None and setting.last_action_at + timedelta(
                seconds=int(setting.minimum_action_interval_seconds)
            ) > now:
                continue
            if task.owner_user_id in invalid_timezone_owners:
                continue
            usage = usage_by_owner.get(task.owner_user_id)
            total_count = int(usage.total_actions or 0) if usage is not None else 0
            if total_count >= int(setting.daily_total_limit):
                continue
            category = _autonomy_usage_category(
                str(task.task_type or ""),
                str(task.action_type or ""),
            )
            action_count = _autonomy_usage_count(usage, category)
            action_limit = _setting_category_limit(setting, category)
            if action_count >= action_limit:
                continue
            selected.append(task.id)
            selected_owners.add(task.owner_user_id)
            if len(selected) >= maximum:
                break
        return selected

    def begin_generation(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.status == "leased",
                AiAgentAutonomyTask.lease_token == lease_token,
                AiAgentAutonomyTask.lease_until >= now,
            )
            .values(
                status="generating",
                lease_until=lease_until,
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    def renew_lease(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.status.in_(("leased", "generating")),
                AiAgentAutonomyTask.lease_token == lease_token,
                AiAgentAutonomyTask.lease_until >= now,
            )
            .values(lease_until=lease_until, updated_at=now)
        )
        return bool(result.rowcount)

    def defer_task(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        not_before: datetime,
        stable_error_code: str,
        now: datetime,
    ) -> bool:
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.status.in_(("leased", "generating")),
                AiAgentAutonomyTask.lease_token == lease_token,
            )
            .values(
                status="deferred",
                not_before=not_before,
                stable_error_code=_normalize_stable_error_code(
                    stable_error_code,
                    fallback="autonomy_deferred",
                ),
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                updated_at=now,
            )
        )
        return bool(result.rowcount)

    def get_conversation_head(
        self,
        *,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
    ) -> AutonomyConversationHeadRow | None:
        row = self.db.execute(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.owner_user_id == owner_user_id,
                Conversation.owner_user_id == owner_user_id,
                Message.provider.in_(tuple(AUTONOMY_MESSAGE_PROVIDERS)),
                Conversation.provider.in_(tuple(AUTONOMY_MESSAGE_PROVIDERS)),
                Message.provider == Conversation.provider,
                Conversation.peer_upstream_uid == str(peer_upstream_uid or "").strip(),
            )
            .order_by(Message.occurred_at.desc(), Message.id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        message, conversation = row
        return AutonomyConversationHeadRow(
            message_id=message.id,
            peer_upstream_uid=str(conversation.peer_upstream_uid or ""),
            message_identity=_autonomy_message_identity(message),
            direction=str(message.direction or ""),
            message_type=str(message.message_type or ""),
            body=str(message.body or ""),
            occurred_at=message.occurred_at,
            revoked=_autonomy_message_revoked(message),
            conversation_title=str(conversation.title or ""),
        )

    def unanswered_inbound_candidates(
        self,
        *,
        owner_user_id: uuid.UUID | None = None,
        limit: int = 10,
    ) -> list[AutonomyConversationHeadRow]:
        later = aliased(Message)
        later_exists = exists(
            select(later.id).where(
                later.owner_user_id == Message.owner_user_id,
                later.conversation_id == Message.conversation_id,
                or_(
                    later.occurred_at > Message.occurred_at,
                    and_(
                        later.occurred_at == Message.occurred_at,
                        later.id > Message.id,
                    ),
                ),
            )
        )
        metadata_origin = func.coalesce(
            Message.extra_data.op("->>")("origin"), ""
        )
        client_key = func.coalesce(
            Message.extra_data.op("->>")("client_message_key"),
            Message.extra_data.op("->>")("client_message_id"),
            "",
        )
        metadata_revoked = func.lower(
            func.coalesce(Message.extra_data.op("->>")("revoked"), "false")
        )
        conditions: list[Any] = [
            Message.provider.in_(tuple(AUTONOMY_MESSAGE_PROVIDERS)),
            Conversation.provider.in_(tuple(AUTONOMY_MESSAGE_PROVIDERS)),
            Message.provider == Conversation.provider,
            Message.direction == "incoming",
            func.lower(Message.message_type).in_(("text", "timtextelem")),
            func.length(func.btrim(func.coalesce(Message.body, ""))) > 0,
            func.lower(Message.status) != "revoked",
            metadata_revoked.notin_(("true", "1")),
            func.lower(metadata_origin) != "agent",
            ~client_key.like("agent:%"),
            ~later_exists,
        ]
        if owner_user_id is not None:
            conditions.append(Message.owner_user_id == owner_user_id)
        rows = list(
            self.db.execute(
                select(Message, Conversation)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(*conditions)
                .order_by(Message.occurred_at.asc(), Message.id.asc())
                .limit(min(max(1, int(limit)), 50))
            )
        )
        candidates: list[AutonomyConversationHeadRow] = []
        for message, conversation in rows:
            peer = str(conversation.peer_upstream_uid or "").strip()
            sender = str(message.sender_upstream_uid or "").strip()
            if not peer or (sender and sender != peer):
                continue
            candidates.append(
                AutonomyConversationHeadRow(
                    message_id=message.id,
                    peer_upstream_uid=peer,
                    message_identity=_autonomy_message_identity(message),
                    direction="incoming",
                    message_type=str(message.message_type or ""),
                    body=str(message.body or ""),
                    occurred_at=message.occurred_at,
                    revoked=False,
                    conversation_title=str(conversation.title or ""),
                )
            )
        return candidates

    def relationship_is_active(
        self,
        *,
        owner_user_id: uuid.UUID,
        target_upstream_uid: str,
    ) -> bool:
        return (
            self.db.scalar(
                select(Relationship.id)
                .where(
                    Relationship.owner_user_id == owner_user_id,
                    Relationship.subject_upstream_uid == target_upstream_uid,
                    Relationship.kind.in_(("follow", "following")),
                    Relationship.status == "active",
                    Relationship.ended_at.is_(None),
                )
                .limit(1)
            )
            is not None
        )

    def _contact_reply_gate_code(
        self,
        *,
        task: AiAgentAutonomyTask,
        snapshot: Mapping[str, Any],
        now: datetime,
    ) -> Any | None:
        from bbw_agent.autonomous import DispatchDecisionCode

        if str(task.task_type or "") != "reply_to_message":
            return None
        contact_policy = snapshot.get("contact_policy")
        if contact_policy is None:
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        task_contact_version = getattr(task, "contact_policy_version", None)
        task_stage = str(getattr(task, "relationship_stage", "") or "").strip()
        if (
            task_contact_version is None
            or int(task_contact_version) != int(contact_policy.version)
            or not task_stage
        ):
            return DispatchDecisionCode.POLICY_CHANGED
        current_stage = AgentContactPolicyRepository(self.db).relationship_stage(
            owner_user_id=task.owner_user_id,
            peer_upstream_uid=str(task.target_upstream_uid or ""),
            policy=contact_policy,
            now=now,
        )
        if current_stage is None:
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        if current_stage != task_stage:
            return DispatchDecisionCode.POLICY_CHANGED
        if not contact_policy_allows_auto_reply(
            persisted=True,
            mode=str(contact_policy.mode or ""),
            paused=bool(contact_policy.paused),
            relationship_stage=current_stage,
        ):
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        return None

    @staticmethod
    def _reply_head_is_dispatchable(
        *,
        head: AutonomyConversationHeadRow | None,
        expected_identity: str,
        now: datetime,
        autonomy_setting: AiAgentAutonomySetting,
        contact_policy: AiAgentContactPolicy | None,
    ) -> bool:
        if contact_policy is None or head is None:
            return False
        return bool(
            head.message_identity == str(expected_identity or "")
            and head.direction == "incoming"
            and not head.revoked
            and str(head.message_type or "").strip().lower()
            in {"text", "timtextelem"}
            and str(head.body or "").strip()
            and autonomy_reply_message_is_eligible(
                head.body,
                peer_upstream_uid=head.peer_upstream_uid,
                conversation_title=head.conversation_title,
            )
            and not reply_risk_boundary(
                head.body,
                message_type=head.message_type,
            )
            and autonomy_reply_message_is_fresh(
                head.occurred_at,
                now=now,
                started_at=autonomy_setting.auto_reply_started_at,
                max_age_seconds=int(
                    contact_policy.maximum_reply_age_seconds
                ),
            )
            and autonomy_reply_not_before(
                head.occurred_at,
                now=now,
                delay_seconds=int(
                    contact_policy.minimum_reply_delay_seconds
                ),
            )
            <= now
        )

    def begin_dispatch(self, request: Any) -> Any:
        from bbw_agent.autonomous import (
            DispatchDecisionCode,
            DispatchReservationDecision,
        )

        snapshot = AgentAutonomySettingRepository(self.db).get_policy_snapshot(
            request.owner_user_id,
            for_update=True,
            target_upstream_uid=str(request.target_upstream_uid or ""),
        )
        task = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == request.task_id,
                AiAgentAutonomyTask.owner_user_id == request.owner_user_id,
            )
            .with_for_update()
        )
        if (
            task is None
            or task.status not in {"leased", "generating"}
            or task.lease_token != request.lease_token
            or task.lease_until is None
            or task.lease_until < request.now
        ):
            return DispatchReservationDecision(DispatchDecisionCode.LEASE_LOST)

        gate = self._dispatch_gate_code(task=task, request=request, snapshot=snapshot)
        if gate is not None:
            return DispatchReservationDecision(gate)
        assert snapshot is not None
        setting = snapshot["autonomy_setting"]

        if request.expected_source_message_identity:
            head = self.get_conversation_head(
                owner_user_id=request.owner_user_id,
                peer_upstream_uid=str(task.target_upstream_uid or ""),
            )
            if not self._reply_head_is_dispatchable(
                head=head,
                expected_identity=request.expected_source_message_identity,
                now=request.now,
                autonomy_setting=setting,
                contact_policy=snapshot.get("contact_policy"),
            ):
                return DispatchReservationDecision(
                    DispatchDecisionCode.SOURCE_STALE
                )

        usage = AgentAutonomyDailyUsageRepository(
            self.db
        ).get_or_create_for_update(request.owner_user_id, request.budget_day)
        if int(usage.total_actions or 0) >= int(request.daily_total_limit):
            return DispatchReservationDecision(
                DispatchDecisionCode.DAILY_BUDGET_EXHAUSTED
            )
        category = _autonomy_usage_category(task.task_type, task.action_type)
        category_count = _autonomy_usage_count(usage, category)
        if category_count >= int(request.daily_action_limit):
            return DispatchReservationDecision(
                DispatchDecisionCode.DAILY_BUDGET_EXHAUSTED
            )
        previous_action = setting.last_action_at or usage.last_action_at
        if previous_action is not None:
            retry_at = previous_action + timedelta(
                seconds=int(request.minimum_interval_seconds)
            )
            if retry_at > request.now:
                return DispatchReservationDecision(
                    DispatchDecisionCode.MINIMUM_INTERVAL,
                    retry_at=retry_at,
                )

        usage.total_actions = int(usage.total_actions or 0) + 1
        _increment_autonomy_usage(usage, category)
        usage.last_action_at = request.now
        usage.updated_at = request.now
        setting.last_action_at = request.now
        setting.last_run_at = request.now
        permit = secrets.token_urlsafe(32)
        task.status = "dispatching"
        task.action_idempotency_key = request.action_idempotency_key
        task.budget_day = request.budget_day
        task.dispatch_started_at = request.now
        task.lease_token = permit
        task.lease_until = None
        task.updated_at = request.now
        self.db.flush()
        return DispatchReservationDecision(
            DispatchDecisionCode.ALLOWED,
            permit_token=permit,
        )

    def _dispatch_gate_code(
        self,
        *,
        task: AiAgentAutonomyTask,
        request: Any,
        snapshot: Mapping[str, Any] | None,
    ) -> Any | None:
        from bbw_agent.autonomous import DispatchDecisionCode
        from bbw_agent.config_fingerprint import runner_configuration_fingerprint

        if snapshot is None:
            return DispatchDecisionCode.ACCESS_REVOKED
        system = snapshot.get("system_setting")
        user = snapshot.get("user")
        account = snapshot.get("external_account")
        agent = snapshot.get("agent_setting")
        execution = snapshot.get("execution_setting")
        setting = snapshot.get("autonomy_setting")
        connection = snapshot.get("connection")
        if not all((system, user, account, agent, execution, setting, connection)):
            return DispatchDecisionCode.ACCESS_REVOKED
        if not (
            bool(system.enabled)
            and bool(system.account_actions_enabled)
            and bool(system.autonomous_agent_enabled)
            and str(user.status) == "active"
            and user.disabled_at is None
            and bool(user.byok_model_runner_enabled)
            and bool(user.byok_account_actions_enabled)
            and bool(user.byok_autonomous_agent_enabled)
            and bool(agent.user_enabled)
            and bool(execution.user_enabled)
            and bool(setting.user_enabled)
            and bool(connection.enabled)
            and bool(connection.api_key_encrypted)
            and str(connection.last_test_status) == "ok"
        ):
            return DispatchDecisionCode.ACCESS_REVOKED
        if request.action_type == "send_private_message" and not bool(
            execution.auto_send_enabled
        ):
            return DispatchDecisionCode.ACCESS_REVOKED
        if (
            task.external_account_id != request.external_account_id
            or account.id != request.external_account_id
            or int(setting.version) != int(request.expected_policy_version)
            or int(execution.version)
            != int(request.expected_execution_setting_version)
            or int(agent.version) != int(request.expected_runner_setting_version)
            or agent.active_connection_id
            != request.expected_model_connection_id
            or connection.id != request.expected_model_connection_id
            or runner_configuration_fingerprint(agent, connection)
            != str(request.expected_runner_configuration_fingerprint or "")
            or int(task.policy_version) != int(request.expected_policy_version)
            or int(task.execution_setting_version)
            != int(request.expected_execution_setting_version)
            or int(task.runner_setting_version)
            != int(request.expected_runner_setting_version)
            or task.model_connection_id != request.expected_model_connection_id
            or str(task.runner_configuration_fingerprint or "")
            != str(request.expected_runner_configuration_fingerprint or "")
        ):
            return DispatchDecisionCode.POLICY_CHANGED
        if setting.halted_at is not None or int(setting.consecutive_failures or 0) >= int(
            setting.consecutive_failure_limit
        ):
            return DispatchDecisionCode.HALTED
        if (
            request.action_type not in set(setting.allowed_actions or [])
            or request.action_type not in set(execution.allowed_actions or [])
        ):
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        feature_enabled = {
            "reply_to_message": bool(setting.auto_reply_enabled),
            "scheduled_post": bool(setting.scheduled_post_enabled),
            "follow_target": bool(setting.managed_relationships_enabled),
            "unfollow_target": bool(setting.managed_relationships_enabled),
            "browse_online": bool(setting.discovery_enabled),
            "request_match": bool(setting.text_match_enabled),
            "proactive_message": bool(setting.proactive_message_enabled),
            "follow_discovered": bool(setting.follow_discovered_enabled),
            "request_friend": bool(setting.friend_request_enabled),
        }.get(str(task.task_type or ""), False)
        if not feature_enabled:
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        target = str(request.target_upstream_uid or "").strip()
        contact_gate = self._contact_reply_gate_code(
            task=task,
            snapshot=snapshot,
            now=getattr(request, "now", None) or utcnow(),
        )
        if contact_gate is not None:
            return contact_gate
        if task.task_type in {"follow_target", "unfollow_target"} and target not in set(
            setting.managed_target_uids or []
        ):
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        if task.task_type in AUTONOMY_DYNAMIC_CANDIDATE_TASKS:
            candidate = (
                AgentDiscoveryCandidateRepository(self.db).get(
                    owner_user_id=task.owner_user_id,
                    target_upstream_uid=target,
                )
                if target
                else None
            )
            if (
                candidate is None
                or target != str(task.target_upstream_uid or "").strip()
                or not agent_candidate_compatibility(
                    owner_profile=(
                        user.profile if isinstance(user.profile, Mapping) else {}
                    ),
                    candidate_profile=(
                        candidate.profile_snapshot
                        if isinstance(candidate.profile_snapshot, Mapping)
                        else {}
                    ),
                    target_upstream_uid=target,
                    match_preference=snapshot.get("match_preference"),
                ).allowed
            ):
                return DispatchDecisionCode.ACTION_NOT_ALLOWED
        if task.action_type in {
            "send_private_message",
            "follow_user",
            "unfollow_user",
            "request_friend",
        } and not target:
            return DispatchDecisionCode.ACTION_NOT_ALLOWED
        return None

    def prepare_action_execution(
        self,
        *,
        permit_token: str,
        action_type: str,
        action_idempotency_key: str,
        target_snapshot: dict[str, object],
        parameter_snapshot: dict[str, object],
    ) -> AgentAutonomyPreparedExecution | None:
        normalized_action = _normalize_action_type(action_type)
        normalized_key = _normalize_idempotency_key(action_idempotency_key)
        task = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.status == "dispatching",
                AiAgentAutonomyTask.lease_token == str(permit_token or ""),
                AiAgentAutonomyTask.action_type == normalized_action,
                AiAgentAutonomyTask.action_idempotency_key == normalized_key,
            )
            .with_for_update()
        )
        if task is None:
            return None
        executions = AgentActionExecutionRepository(self.db)
        execution, created = executions.enqueue(
            owner_user_id=task.owner_user_id,
            external_account_id=task.external_account_id,
            action_type=normalized_action,
            idempotency_key=normalized_key,
            execution_setting_version=int(task.execution_setting_version),
            target_snapshot=dict(target_snapshot),
            parameter_snapshot=dict(parameter_snapshot),
            approval_source="user_allowlist",
            trigger_source="schedule",
        )
        if (
            execution.external_account_id != task.external_account_id
            or int(execution.execution_setting_version)
            != int(task.execution_setting_version)
            or dict(execution.target_snapshot or {}) != dict(target_snapshot)
            or dict(execution.parameter_snapshot or {})
            != dict(parameter_snapshot)
        ):
            raise RuntimeError(
                "autonomous action idempotency snapshot conflict"
            )
        if (
            task.action_execution_id is not None
            and task.action_execution_id != execution.id
        ):
            raise RuntimeError(
                "autonomous task is linked to another action execution"
            )
        if task.action_execution_id is None:
            linked = self.record_action_execution_id(
                task_id=task.id,
                permit_token=str(permit_token or ""),
                action_execution_id=execution.id,
            )
            if not linked:
                raise RuntimeError(
                    "autonomous action execution link was not persisted"
                )
        should_execute = False
        if execution.status == "queued":
            started = executions.start(task.owner_user_id, execution.id)
            if started is None:
                raise RuntimeError(
                    "autonomous action execution could not be started"
                )
            execution = started
            should_execute = True
        elif created:
            raise RuntimeError(
                "new autonomous action execution has an invalid status"
            )
        return AgentAutonomyPreparedExecution(
            task_id=task.id,
            owner_user_id=task.owner_user_id,
            execution_id=execution.id,
            status=str(execution.status or ""),
            stable_error_code=str(execution.stable_error_code or ""),
            result_id=str(execution.external_result_id or ""),
            should_execute=should_execute,
        )

    def lock_dispatch_permit(
        self,
        *,
        permit_token: str,
        action_type: str,
        action_idempotency_key: str,
        action_execution_id: uuid.UUID,
        target_snapshot: Mapping[str, object],
        parameter_snapshot: Mapping[str, object],
    ) -> AgentAutonomyDispatchContext | None:
        normalized_action = _normalize_action_type(action_type)
        normalized_key = _normalize_idempotency_key(action_idempotency_key)
        candidate = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.status == "dispatching",
                AiAgentAutonomyTask.lease_token == str(permit_token or ""),
                AiAgentAutonomyTask.action_type == normalized_action,
                AiAgentAutonomyTask.action_idempotency_key
                == normalized_key,
            )
        )
        if candidate is None:
            return None
        snapshot = AgentAutonomySettingRepository(self.db).get_policy_snapshot(
            candidate.owner_user_id,
            for_update=True,
            target_upstream_uid=str(candidate.target_upstream_uid or ""),
        )
        task = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == candidate.id,
                AiAgentAutonomyTask.status == "dispatching",
                AiAgentAutonomyTask.lease_token == str(permit_token or ""),
                AiAgentAutonomyTask.action_type == normalized_action,
                AiAgentAutonomyTask.action_idempotency_key == normalized_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            return None
        execution = AgentActionExecutionRepository(self.db).get(
            task.owner_user_id,
            action_execution_id,
            for_update=True,
        )
        if (
            execution is None
            or task.action_execution_id != execution.id
            or execution.external_account_id != task.external_account_id
            or execution.action_type != normalized_action
            or execution.idempotency_key != normalized_key
            or int(execution.execution_setting_version)
            != int(task.execution_setting_version)
            or execution.status != "running"
            or dict(execution.target_snapshot or {}) != dict(target_snapshot)
            or dict(execution.parameter_snapshot or {})
            != dict(parameter_snapshot)
        ):
            return None
        if normalized_action == "publish_text_post":
            content_digest = str(
                dict(parameter_snapshot).get("content_sha256") or ""
            )
            if not content_digest:
                return None
            duplicate = self.db.scalar(
                select(AiAgentActionExecution.id)
                .where(
                    AiAgentActionExecution.owner_user_id == task.owner_user_id,
                    AiAgentActionExecution.id != execution.id,
                    AiAgentActionExecution.action_type == "publish_text_post",
                    AiAgentActionExecution.status == "succeeded",
                    AiAgentActionExecution.completed_at
                    >= utcnow() - timedelta(days=7),
                    AiAgentActionExecution.parameter_snapshot.op("->>")(
                        "content_sha256"
                    )
                    == content_digest,
                )
                .limit(1)
            )
            if duplicate is not None:
                return None
        request = type(
            "DispatchGateRequest",
            (),
            {
                "external_account_id": task.external_account_id,
                "expected_policy_version": task.policy_version,
                "expected_execution_setting_version": task.execution_setting_version,
                "expected_runner_setting_version": task.runner_setting_version,
                "expected_model_connection_id": task.model_connection_id,
                "expected_runner_configuration_fingerprint": (
                    task.runner_configuration_fingerprint
                ),
                "action_type": task.action_type,
                "target_upstream_uid": str(task.target_upstream_uid or ""),
            },
        )()
        if self._dispatch_gate_code(task=task, request=request, snapshot=snapshot) is not None:
            return None
        assert snapshot is not None
        setting = snapshot["autonomy_setting"]
        dispatch_now = utcnow()
        if task.task_type == "reply_to_message":
            head = self.get_conversation_head(
                owner_user_id=task.owner_user_id,
                peer_upstream_uid=str(task.target_upstream_uid or ""),
            )
            if not self._reply_head_is_dispatchable(
                head=head,
                expected_identity=str(task.source_message_identity or ""),
                now=dispatch_now,
                autonomy_setting=setting,
                contact_policy=snapshot.get("contact_policy"),
            ):
                return None
        elif task.task_type == "proactive_message" and self.get_conversation_head(
            owner_user_id=task.owner_user_id,
            peer_upstream_uid=str(task.target_upstream_uid or ""),
        ) is not None:
            return None
        try:
            local_now = dispatch_now.astimezone(
                ZoneInfo(str(setting.timezone or "UTC"))
            )
        except ZoneInfoNotFoundError:
            return None
        start_minute = int(setting.active_start_minute)
        end_minute = int(setting.active_end_minute)
        if start_minute != end_minute:
            minute = local_now.hour * 60 + local_now.minute
            active = (
                start_minute <= minute < end_minute
                if start_minute < end_minute
                else minute >= start_minute or minute < end_minute
            )
            if not active:
                return None
        account = snapshot["external_account"]
        user = snapshot["user"]
        return AgentAutonomyDispatchContext(
            task_id=task.id,
            owner_user_id=task.owner_user_id,
            external_account_id=task.external_account_id,
            upstream_uid=str(account.upstream_uid or ""),
            match_pool_online_list_enabled=bool(
                user.match_pool_online_list_enabled
            ),
            policy_version=int(task.policy_version),
            execution_setting_version=int(task.execution_setting_version),
            action_type=task.action_type,
            action_idempotency_key=str(task.action_idempotency_key or ""),
            action_execution_id=execution.id,
            source_message_identity=str(task.source_message_identity or ""),
        )

    def record_action_execution_id(
        self,
        *,
        task_id: uuid.UUID,
        permit_token: str,
        action_execution_id: uuid.UUID,
    ) -> bool:
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.status == "dispatching",
                AiAgentAutonomyTask.lease_token == permit_token,
            )
            .values(
                action_execution_id=action_execution_id,
                updated_at=utcnow(),
            )
        )
        return bool(result.rowcount)

    def record_model_run_id(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        model_run_id: uuid.UUID,
    ) -> bool:
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.status == "generating",
                AiAgentAutonomyTask.lease_token == lease_token,
                or_(
                    AiAgentAutonomyTask.model_run_id.is_(None),
                    AiAgentAutonomyTask.model_run_id == model_run_id,
                ),
            )
            .values(model_run_id=model_run_id, updated_at=utcnow())
        )
        return bool(result.rowcount)

    def _record_success_state(
        self,
        *,
        task: AiAgentAutonomyTask,
        setting: AiAgentAutonomySetting | None,
        now: datetime,
    ) -> None:
        if setting is not None:
            if task.action_type == "publish_text_post":
                setting.last_post_at = now
            if task.task_type == "browse_online":
                setting.last_discovery_at = now
            elif task.task_type == "request_match":
                setting.last_match_at = now
            elif task.task_type == "proactive_message":
                setting.last_outreach_at = now
        if task.task_type in AUTONOMY_DYNAMIC_CANDIDATE_TASKS and str(
            task.target_upstream_uid or ""
        ).strip():
            AgentDiscoveryCandidateRepository(self.db).mark_interaction(
                owner_user_id=task.owner_user_id,
                target_upstream_uid=str(task.target_upstream_uid or ""),
                action_type=str(task.action_type or ""),
                at=now,
            )

    def finish_task(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        completion: Any,
        now: datetime,
    ) -> bool:
        status = str(getattr(completion.status, "value", completion.status))
        if status not in AUTONOMY_TERMINAL_STATUSES:
            raise ValueError("invalid autonomous terminal status")
        stable_error_code = (
            _normalize_stable_error_code(
                getattr(completion, "stable_error_code", ""),
                fallback="autonomy_task_failed",
            )
            if status != "succeeded"
            else ""
        )
        result_id = str(getattr(completion, "result_id", "") or "")[:256]
        outcome_unknown = bool(getattr(completion, "outcome_unknown", False))
        count_failure = bool(
            getattr(completion, "count_failure", False)
        ) or outcome_unknown
        reset_failures = bool(getattr(completion, "reset_failures", False))
        force_halt = bool(getattr(completion, "force_halt", False))
        candidate = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.lease_token == lease_token,
                ~AiAgentAutonomyTask.status.in_(tuple(AUTONOMY_TERMINAL_STATUSES)),
            )
        )
        if candidate is None:
            return False
        setting = AgentAutonomySettingRepository(self.db).get(
            candidate.owner_user_id,
            for_update=True,
        )
        task = self.db.scalar(
            select(AiAgentAutonomyTask)
            .where(
                AiAgentAutonomyTask.id == task_id,
                AiAgentAutonomyTask.lease_token == lease_token,
                ~AiAgentAutonomyTask.status.in_(tuple(AUTONOMY_TERMINAL_STATUSES)),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            return False
        execution = None
        execution_binding_valid = True
        if task.action_execution_id is not None:
            execution = AgentActionExecutionRepository(self.db).get(
                task.owner_user_id,
                task.action_execution_id,
                for_update=True,
            )
            execution_binding_valid = bool(
                execution is not None
                and execution.external_account_id == task.external_account_id
                and execution.action_type == task.action_type
                and execution.idempotency_key == task.action_idempotency_key
                and int(execution.execution_setting_version)
                == int(task.execution_setting_version)
            )
        elif task.status == "dispatching":
            stable_error_code = (
                stable_error_code
                if status in {"failed", "cancelled", "stale"}
                and not outcome_unknown
                and stable_error_code
                else "action_execution_not_prepared"
            )
            status = "failed"
            result_id = ""
            outcome_unknown = False
            count_failure = True
            reset_failures = False
            force_halt = False

        if not execution_binding_valid:
            status = "manual_review"
            stable_error_code = "execution_audit_mismatch"
            result_id = ""
            outcome_unknown = True
            count_failure = True
            reset_failures = False
            force_halt = True
        elif execution is not None:
            executions = AgentActionExecutionRepository(self.db)
            if execution.status == "queued":
                cancelled = executions.cancel_queued(
                    task.owner_user_id,
                    execution.id,
                    stable_error_code=(
                        stable_error_code
                        if not outcome_unknown and stable_error_code
                        else "action_execution_not_started"
                    ),
                )
                if cancelled is None:
                    status = "manual_review"
                    stable_error_code = "execution_audit_sync_failed"
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
                elif status == "succeeded" or outcome_unknown:
                    status = "manual_review"
                    stable_error_code = "execution_audit_state_mismatch"
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
                else:
                    status = "cancelled"
                    stable_error_code = str(
                        cancelled.stable_error_code
                        or "action_execution_not_started"
                    )[:64]
                    result_id = ""
                    outcome_unknown = False
                    count_failure = False
                    reset_failures = False
                    force_halt = False
            elif execution.status == "running":
                if status == "succeeded":
                    synchronized = executions.succeed(
                        task.owner_user_id,
                        execution.id,
                        external_result_id=result_id or None,
                    )
                elif status == "manual_review" and outcome_unknown:
                    synchronized = executions.mark_outcome_unknown(
                        task.owner_user_id,
                        execution.id,
                        stable_error_code=stable_error_code,
                    )
                elif status == "failed" and not outcome_unknown:
                    synchronized = executions.fail(
                        task.owner_user_id,
                        execution.id,
                        stable_error_code=stable_error_code,
                    )
                elif (
                    status == "stale"
                    and not outcome_unknown
                    and stable_error_code == "local_message_source_stale"
                ):
                    synchronized = executions.fail(
                        task.owner_user_id,
                        execution.id,
                        stable_error_code=stable_error_code,
                    )
                else:
                    synchronized = executions.mark_outcome_unknown(
                        task.owner_user_id,
                        execution.id,
                        stable_error_code="execution_audit_state_mismatch",
                    )
                    status = "manual_review"
                    stable_error_code = "execution_audit_state_mismatch"
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
                if synchronized is None:
                    status = "manual_review"
                    stable_error_code = "execution_audit_sync_failed"
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
                elif synchronized.status == "succeeded":
                    status = "succeeded"
                    stable_error_code = ""
                    result_id = str(
                        synchronized.external_result_id or ""
                    )[:256]
                    outcome_unknown = False
                    count_failure = False
                    reset_failures = True
                    force_halt = False
                elif synchronized.status == "failed":
                    execution_error = str(
                        synchronized.stable_error_code
                        or "account_action_failed"
                    )[:64]
                    status = (
                        "stale"
                        if execution_error == "local_message_source_stale"
                        else "failed"
                    )
                    stable_error_code = execution_error
                    result_id = ""
                    outcome_unknown = False
                    count_failure = status == "failed"
                    reset_failures = False
                    force_halt = False
                elif synchronized.status == "manual_review":
                    status = "manual_review"
                    stable_error_code = str(
                        synchronized.stable_error_code
                        or "account_action_outcome_unknown"
                    )[:64]
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
                else:
                    status = "manual_review"
                    stable_error_code = "execution_audit_state_mismatch"
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
            else:
                expected_execution_status = {
                    "succeeded": "succeeded",
                    "failed": "failed",
                    "stale": "failed",
                    "cancelled": "cancelled",
                    "manual_review": "manual_review",
                }.get(status)
                if execution.status != expected_execution_status:
                    status = "manual_review"
                    stable_error_code = "execution_audit_state_mismatch"
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
                elif execution.status == "succeeded":
                    stable_error_code = ""
                    result_id = str(execution.external_result_id or "")[:256]
                    outcome_unknown = False
                    count_failure = False
                    reset_failures = True
                    force_halt = False
                elif execution.status == "failed":
                    execution_error = str(
                        execution.stable_error_code or "account_action_failed"
                    )[:64]
                    if (
                        status == "stale"
                        and execution_error != "local_message_source_stale"
                    ):
                        status = "manual_review"
                        stable_error_code = "execution_audit_state_mismatch"
                        result_id = ""
                        outcome_unknown = True
                        count_failure = True
                        reset_failures = False
                        force_halt = True
                    elif execution_error == "local_message_source_stale":
                        status = "stale"
                        stable_error_code = execution_error
                        result_id = ""
                        outcome_unknown = False
                        count_failure = False
                        reset_failures = False
                        force_halt = False
                    else:
                        status = "failed"
                        stable_error_code = execution_error
                        result_id = ""
                        outcome_unknown = False
                        count_failure = True
                        reset_failures = False
                        force_halt = False
                elif execution.status == "cancelled":
                    stable_error_code = str(
                        execution.stable_error_code
                        or "account_action_cancelled"
                    )[:64]
                    result_id = ""
                    outcome_unknown = False
                    count_failure = False
                    reset_failures = False
                    force_halt = False
                elif execution.status == "manual_review":
                    stable_error_code = str(
                        execution.stable_error_code
                        or "account_action_outcome_unknown"
                    )[:64]
                    result_id = ""
                    outcome_unknown = True
                    count_failure = True
                    reset_failures = False
                    force_halt = True
        task.status = status
        task.stable_error_code = stable_error_code or None
        task.result_id = result_id or None
        task.outcome_unknown = outcome_unknown
        task.completed_at = now
        task.lease_owner = None
        task.lease_token = None
        task.lease_until = None
        task.updated_at = now
        if setting is not None:
            setting.last_run_at = now
            if reset_failures:
                setting.consecutive_failures = 0
            if status == "succeeded":
                self._record_success_state(task=task, setting=setting, now=now)
            if count_failure:
                setting.consecutive_failures = int(setting.consecutive_failures or 0) + 1
            if force_halt or int(
                setting.consecutive_failures or 0
            ) >= int(setting.consecutive_failure_limit):
                setting.halted_at = now
                setting.halted_reason = task.stable_error_code or "autonomy_halted"
                setting.next_run_at = None
            setting.updated_at = now
        if (task.dispatch_started_at is None) != (task.budget_day is None):
            raise RuntimeError(
                "autonomous dispatch timestamp and reserved budget day disagree"
            )
        if task.budget_day is not None:
            usage = AgentAutonomyDailyUsageRepository(self.db).get(
                task.owner_user_id,
                task.budget_day,
                for_update=True,
            )
            if usage is None:
                raise RuntimeError(
                    "reserved autonomous daily usage row is missing"
                )
            if count_failure:
                usage.failed_actions = int(usage.failed_actions or 0) + 1
            if outcome_unknown:
                usage.outcome_unknown_actions = int(
                    usage.outcome_unknown_actions or 0
                ) + 1
            usage.updated_at = now
        self.db.flush()
        return True

    def cancel_not_started_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        stable_error_code: str = "autonomy_access_revoked",
        reason: str | None = None,
    ) -> int:
        return self._cancel_not_started(
            owner_user_id=owner_user_id,
            stable_error_code=reason or stable_error_code,
        )

    def cancel_all_not_started(
        self,
        *,
        stable_error_code: str = "autonomy_system_disabled",
        reason: str | None = None,
    ) -> int:
        return self._cancel_not_started(
            stable_error_code=reason or stable_error_code
        )

    def cancel_queued_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        stable_error_code: str = "autonomy_access_revoked",
        reason: str | None = None,
    ) -> int:
        return self.cancel_not_started_for_owner(
            owner_user_id,
            stable_error_code=stable_error_code,
            reason=reason,
        )

    def cancel_all_queued(
        self,
        *,
        stable_error_code: str = "autonomy_system_disabled",
        reason: str | None = None,
    ) -> int:
        return self.cancel_all_not_started(
            stable_error_code=stable_error_code,
            reason=reason,
        )

    def _cancel_not_started(
        self,
        *,
        stable_error_code: str,
        owner_user_id: uuid.UUID | None = None,
    ) -> int:
        conditions: list[Any] = [
            AiAgentAutonomyTask.status.in_(tuple(AUTONOMY_NOT_STARTED_STATUSES))
        ]
        if owner_user_id is not None:
            conditions.append(AiAgentAutonomyTask.owner_user_id == owner_user_id)
        now = utcnow()
        result = self.db.execute(
            update(AiAgentAutonomyTask)
            .where(*conditions)
            .values(
                status="cancelled",
                stable_error_code=_normalize_stable_error_code(
                    stable_error_code,
                    fallback="autonomy_cancelled",
                ),
                completed_at=now,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                outcome_unknown=False,
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)

    def mark_expired_dispatching_unknown(
        self,
        *,
        before: datetime,
        stable_error_code: str = "worker_lost",
    ) -> int:
        candidates = list(
            self.db.execute(
                select(
                    AiAgentAutonomyTask.id,
                    AiAgentAutonomyTask.owner_user_id,
                )
                .where(
                    AiAgentAutonomyTask.status == "dispatching",
                    AiAgentAutonomyTask.dispatch_started_at < before,
                )
                .limit(100)
            )
        )
        now = utcnow()
        code = _normalize_stable_error_code(
            stable_error_code,
            fallback="worker_lost",
        )
        changed = 0
        for task_id, owner_user_id in candidates:
            setting = AgentAutonomySettingRepository(self.db).get(
                owner_user_id,
                for_update=True,
            )
            task = self.db.scalar(
                select(AiAgentAutonomyTask)
                .where(
                    AiAgentAutonomyTask.id == task_id,
                    AiAgentAutonomyTask.status == "dispatching",
                    AiAgentAutonomyTask.dispatch_started_at < before,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if task is None:
                continue
            execution = None
            has_execution_reference = task.action_execution_id is not None
            if has_execution_reference:
                execution = AgentActionExecutionRepository(self.db).get(
                    owner_user_id,
                    task.action_execution_id,
                    for_update=True,
                )
            execution_binding_valid = bool(
                execution is not None
                and execution.external_account_id == task.external_account_id
                and execution.action_type == task.action_type
                and execution.idempotency_key == task.action_idempotency_key
                and int(execution.execution_setting_version)
                == int(task.execution_setting_version)
            )
            execution_status = (
                str(execution.status or "")
                if execution_binding_valid and execution is not None
                else ""
            )
            if execution_binding_valid and execution_status == "queued":
                assert execution is not None
                cancelled = AgentActionExecutionRepository(
                    self.db
                ).cancel_queued(
                    owner_user_id,
                    execution.id,
                    stable_error_code="worker_lost_before_action_start",
                )
                if cancelled is None:
                    raise RuntimeError(
                        "expired queued autonomous execution was not cancelled"
                    )
                execution_status = "cancelled"
            count_failure = False
            outcome_unknown = False
            reset_failures = False
            if not has_execution_reference:
                task.status = "failed"
                task.stable_error_code = "worker_lost_before_action_prepare"
                task.result_id = None
                count_failure = True
            elif not execution_binding_valid:
                task.status = "manual_review"
                task.stable_error_code = "execution_audit_mismatch"
                task.result_id = None
                outcome_unknown = True
                count_failure = True
            elif execution_status == "succeeded":
                assert execution is not None
                task.status = "succeeded"
                task.stable_error_code = None
                task.result_id = str(execution.external_result_id or "")[:256] or None
                reset_failures = True
            elif execution_status == "failed":
                assert execution is not None
                task.stable_error_code = (
                    str(execution.stable_error_code or "")[:64]
                    or "account_action_failed"
                )
                task.result_id = None
                if task.stable_error_code == "local_message_source_stale":
                    task.status = "stale"
                else:
                    task.status = "failed"
                    count_failure = True
            elif execution_status == "cancelled":
                assert execution is not None
                task.status = "cancelled"
                task.stable_error_code = (
                    str(execution.stable_error_code or "")[:64]
                    or "account_action_cancelled"
                )
                task.result_id = None
            elif execution_status == "manual_review":
                assert execution is not None
                task.status = "manual_review"
                task.stable_error_code = (
                    str(execution.stable_error_code or "")[:64]
                    or "account_action_outcome_unknown"
                )
                task.result_id = None
                outcome_unknown = True
                count_failure = True
            elif execution_status == "running":
                assert execution is not None
                task.status = "manual_review"
                task.stable_error_code = code
                task.result_id = None
                outcome_unknown = True
                count_failure = True
                marked = AgentActionExecutionRepository(
                    self.db
                ).mark_outcome_unknown(
                    owner_user_id,
                    execution.id,
                    stable_error_code=task.stable_error_code,
                )
                if marked is None:
                    raise RuntimeError(
                        "expired autonomous execution audit was not updated"
                    )
            else:
                task.status = "manual_review"
                task.stable_error_code = "execution_audit_state_mismatch"
                task.result_id = None
                outcome_unknown = True
                count_failure = True
            task.outcome_unknown = outcome_unknown
            task.completed_at = now
            task.lease_owner = None
            task.lease_token = None
            task.lease_until = None
            task.updated_at = now
            if setting is not None:
                setting.last_run_at = now
                if reset_failures:
                    setting.consecutive_failures = 0
                if task.status == "succeeded":
                    self._record_success_state(
                        task=task,
                        setting=setting,
                        now=now,
                    )
                if count_failure:
                    setting.consecutive_failures = int(
                        setting.consecutive_failures or 0
                    ) + 1
                if outcome_unknown or int(
                    setting.consecutive_failures or 0
                ) >= int(setting.consecutive_failure_limit):
                    setting.halted_at = now
                    setting.halted_reason = (
                        task.stable_error_code or code
                    )
                    setting.next_run_at = None
                setting.updated_at = now
            if task.budget_day is None:
                raise RuntimeError(
                    "expired dispatched task is missing its reserved budget day"
                )
            usage = AgentAutonomyDailyUsageRepository(self.db).get(
                owner_user_id,
                task.budget_day,
                for_update=True,
            )
            if usage is None:
                raise RuntimeError(
                    "reserved autonomous daily usage row is missing"
                )
            if count_failure:
                usage.failed_actions = int(usage.failed_actions or 0) + 1
            if outcome_unknown:
                usage.outcome_unknown_actions = int(
                    usage.outcome_unknown_actions or 0
                ) + 1
            usage.updated_at = now
            changed += 1
        self.db.flush()
        return changed


# Compatibility aliases retained for the administrator gate while callers
# converge on the explicit AgentAutonomy* names.
AutonomousAgentSettingRepository = AgentAutonomySettingRepository
AutonomousTaskRepository = AgentAutonomyTaskRepository
