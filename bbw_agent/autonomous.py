"""Fail-closed orchestration core for unattended BYOK account operations.

This module deliberately has no HTTP client, browser automation, cookie,
provider credential, API key, SQLAlchemy model, or queue implementation.  It
only coordinates a narrow model-runner port, a durable store port and the
existing fixed-action execution boundary.

The durable implementation is expected to use ``AiAgentAutonomySetting``,
``AiAgentAutonomyTask`` and ``AiAgentAutonomyDailyUsage`` with matching
``AgentAutonomy*Repository`` classes.  Keeping those details behind the ports
below lets this state machine remain testable without database dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from typing import Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SEND_PRIVATE_MESSAGE = "send_private_message"
PUBLISH_TEXT_POST = "publish_text_post"
FOLLOW_USER = "follow_user"
UNFOLLOW_USER = "unfollow_user"
BROWSE_ONLINE_USERS = "browse_online_users"
REQUEST_TEXT_MATCH = "request_text_match"
REQUEST_FRIEND = "request_friend"

FIXED_ACCOUNT_ACTIONS = (
    SEND_PRIVATE_MESSAGE,
    PUBLISH_TEXT_POST,
    FOLLOW_USER,
    UNFOLLOW_USER,
    BROWSE_ONLINE_USERS,
    REQUEST_TEXT_MATCH,
    REQUEST_FRIEND,
)
FIXED_ACCOUNT_ACTION_SET = frozenset(FIXED_ACCOUNT_ACTIONS)
RELATIONSHIP_ACTIONS = frozenset({FOLLOW_USER, UNFOLLOW_USER, REQUEST_FRIEND})
STATIC_RELATIONSHIP_ACTIONS = frozenset({FOLLOW_USER, UNFOLLOW_USER})
MAX_AUTONOMOUS_TEXT_LENGTH = 2_000
MAX_AUTONOMOUS_FRIEND_REQUEST_LENGTH = 200
MAX_AUTONOMY_TASK_ATTEMPTS = 3
MAX_TARGET_LENGTH = 128
MAX_INSTRUCTION_LENGTH = 4_000
MAX_IDEMPOTENCY_LENGTH = 160
AUTONOMOUS_BROWSE_DAILY_LIMIT = 8
AUTONOMOUS_MATCH_DAILY_LIMIT = 6
AUTONOMOUS_OUTREACH_DAILY_LIMIT = 6
_STABLE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_AUTONOMOUS_LINK = re.compile(r"(?i)(?:https?://|www\.)\S+")
_AUTONOMOUS_CREDENTIAL = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|bearer|cookie|session[_ -]?id)\b"
    r"|\bsk-[a-z0-9_-]{8,}\b"
    r"|\beyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b)"
)
_AUTONOMOUS_EMAIL = re.compile(
    r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b"
)
_AUTONOMOUS_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


class AgentAutonomyError(RuntimeError):
    """Stable, secret-free orchestration error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        normalized = str(code or "autonomy_error").strip().lower()
        self.code = normalized if _STABLE_CODE.fullmatch(normalized) else "autonomy_error"
        self.public_message = str(message or "自动社交 Agent 任务失败")[:240]


class AgentAutonomyModelError(AgentAutonomyError):
    """A generation failure known to have happened before any side effect."""


class AutonomyTaskType(str, Enum):
    REPLY_TO_MESSAGE = "reply_to_message"
    SCHEDULED_POST = "scheduled_post"
    FOLLOW_TARGET = "follow_target"
    UNFOLLOW_TARGET = "unfollow_target"
    BROWSE_ONLINE = "browse_online"
    REQUEST_MATCH = "request_match"
    PROACTIVE_MESSAGE = "proactive_message"
    FOLLOW_DISCOVERED = "follow_discovered"
    REQUEST_FRIEND = "request_friend"


class AutonomyTaskStatus(str, Enum):
    QUEUED = "queued"
    DEFERRED = "deferred"
    LEASED = "leased"
    GENERATING = "generating"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    MANUAL_REVIEW = "manual_review"


CLAIMABLE_TASK_STATUSES = frozenset(
    {
        AutonomyTaskStatus.QUEUED,
        AutonomyTaskStatus.DEFERRED,
        AutonomyTaskStatus.LEASED,
        AutonomyTaskStatus.GENERATING,
    }
)
NOT_STARTED_TASK_STATUSES = frozenset(
    {
        AutonomyTaskStatus.QUEUED,
        AutonomyTaskStatus.DEFERRED,
        AutonomyTaskStatus.LEASED,
        AutonomyTaskStatus.GENERATING,
    }
)
TERMINAL_TASK_STATUSES = frozenset(
    {
        AutonomyTaskStatus.SUCCEEDED,
        AutonomyTaskStatus.FAILED,
        AutonomyTaskStatus.CANCELLED,
        AutonomyTaskStatus.STALE,
        AutonomyTaskStatus.MANUAL_REVIEW,
    }
)


class MessageDirection(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    UNKNOWN = "unknown"


class DispatchDecisionCode(str, Enum):
    ALLOWED = "allowed"
    LEASE_LOST = "lease_lost"
    ACCESS_REVOKED = "access_revoked"
    POLICY_CHANGED = "policy_changed"
    ACTION_NOT_ALLOWED = "action_not_allowed"
    HALTED = "halted"
    SOURCE_STALE = "source_stale"
    DAILY_BUDGET_EXHAUSTED = "daily_budget_exhausted"
    MINIMUM_INTERVAL = "minimum_interval"


class FixedActionOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class AgentAutonomyAccess:
    account_active: bool
    runner_system_enabled: bool
    runner_admin_granted: bool
    runner_user_enabled: bool
    model_connection_ready: bool
    account_actions_system_enabled: bool
    account_actions_admin_granted: bool
    account_actions_user_enabled: bool
    private_message_auto_send_enabled: bool
    autonomy_system_enabled: bool
    autonomy_admin_granted: bool

    @property
    def available(self) -> bool:
        return all(
            (
                self.account_active,
                self.runner_system_enabled,
                self.runner_admin_granted,
                self.runner_user_enabled,
                self.model_connection_ready,
                self.account_actions_system_enabled,
                self.account_actions_admin_granted,
                self.account_actions_user_enabled,
                self.autonomy_system_enabled,
                self.autonomy_admin_granted,
            )
        )


def _normalize_target(value: object) -> str:
    target = str(value or "").strip()
    if (
        not target
        or len(target) > MAX_TARGET_LENGTH
        or any(ord(char) < 33 for char in target)
        or any(char in target for char in "*?[]")
    ):
        raise ValueError("autonomy target must be one exact user identifier")
    return target


def _normalize_actions(values: Sequence[str] | frozenset[str]) -> frozenset[str]:
    actions = frozenset(str(value or "").strip() for value in values)
    if not actions <= FIXED_ACCOUNT_ACTION_SET:
        raise ValueError("autonomy policy contains an unsupported action")
    return actions


def _normalize_limits(
    values: Sequence[tuple[str, int]],
    *,
    total_limit: int,
) -> tuple[tuple[str, int], ...]:
    limits: dict[str, int] = {}
    for raw_action, raw_limit in values:
        action = str(raw_action or "").strip()
        if action not in FIXED_ACCOUNT_ACTION_SET:
            raise ValueError("daily action limit contains an unsupported action")
        limit = int(raw_limit)
        if not 0 <= limit <= total_limit:
            raise ValueError("daily action limit is outside the total hard budget")
        limits[action] = limit
    return tuple((action, limits[action]) for action in FIXED_ACCOUNT_ACTIONS if action in limits)


@dataclass(frozen=True, slots=True)
class AgentAutonomyPolicy:
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    version: int
    execution_setting_version: int
    runner_setting_version: int
    model_connection_id: uuid.UUID
    runner_configuration_fingerprint: str
    access: AgentAutonomyAccess
    user_enabled: bool
    auto_reply_enabled: bool
    scheduled_posts_enabled: bool
    relationship_actions_enabled: bool
    discovery_enabled: bool = False
    text_match_enabled: bool = False
    proactive_message_enabled: bool = False
    follow_discovered_enabled: bool = False
    friend_request_enabled: bool = False
    selected_actions: frozenset[str] = field(default_factory=frozenset)
    target_allowlist: frozenset[str] = field(default_factory=frozenset)
    daily_total_limit: int = 10
    daily_action_limits: tuple[tuple[str, int], ...] = ()
    minimum_interval_seconds: int = 60
    quiet_timezone: str = "UTC"
    quiet_start_minute: int | None = None
    quiet_end_minute: int | None = None
    max_consecutive_failures: int = 3
    consecutive_failures: int = 0
    halted: bool = False
    scheduled_post_max_lateness_seconds: int = 3_600

    def __post_init__(self) -> None:
        if (
            int(self.version) < 1
            or int(self.execution_setting_version) < 1
            or int(self.runner_setting_version) < 1
        ):
            raise ValueError("autonomy policy versions must be positive")
        if not isinstance(self.model_connection_id, uuid.UUID):
            raise ValueError("autonomy policy model connection is invalid")
        fingerprint = str(self.runner_configuration_fingerprint or "").strip().lower()
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("autonomy policy runner fingerprint is invalid")
        actions = _normalize_actions(self.selected_actions)
        targets = frozenset(_normalize_target(value) for value in self.target_allowlist)
        total_limit = int(self.daily_total_limit)
        if not 1 <= total_limit <= 1_000:
            raise ValueError("daily total limit must be between one and one thousand")
        limits = _normalize_limits(
            self.daily_action_limits,
            total_limit=total_limit,
        )
        if not 1 <= int(self.minimum_interval_seconds) <= 86_400:
            raise ValueError("minimum interval is outside the supported range")
        if not 1 <= int(self.max_consecutive_failures) <= 100:
            raise ValueError("consecutive failure threshold is invalid")
        if int(self.consecutive_failures) < 0:
            raise ValueError("consecutive failure count cannot be negative")
        if not 60 <= int(self.scheduled_post_max_lateness_seconds) <= 86_400:
            raise ValueError("scheduled post lateness window is invalid")
        try:
            ZoneInfo(str(self.quiet_timezone or ""))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("quiet timezone is invalid") from exc
        starts = self.quiet_start_minute
        ends = self.quiet_end_minute
        if (starts is None) != (ends is None):
            raise ValueError("quiet start and end must be configured together")
        if starts is not None:
            if not 0 <= int(starts) < 1_440 or not 0 <= int(ends) < 1_440:
                raise ValueError("quiet time is outside one day")
            if int(starts) == int(ends):
                raise ValueError("equal quiet boundaries are ambiguous")
        if self.auto_reply_enabled and SEND_PRIVATE_MESSAGE not in actions:
            raise ValueError("auto reply requires the private-message action")
        if self.scheduled_posts_enabled and PUBLISH_TEXT_POST not in actions:
            raise ValueError("scheduled posts require the publish action")
        if self.relationship_actions_enabled and not (
            actions & STATIC_RELATIONSHIP_ACTIONS
        ):
            raise ValueError("relationship automation requires a relationship action")
        if self.relationship_actions_enabled and not targets:
            raise ValueError("relationship automation requires an exact target allowlist")
        if self.discovery_enabled and BROWSE_ONLINE_USERS not in actions:
            raise ValueError("online discovery requires the browse action")
        if self.text_match_enabled and REQUEST_TEXT_MATCH not in actions:
            raise ValueError("text matching requires the match action")
        if self.proactive_message_enabled and SEND_PRIVATE_MESSAGE not in actions:
            raise ValueError("proactive messaging requires the private-message action")
        if self.follow_discovered_enabled and FOLLOW_USER not in actions:
            raise ValueError("discovered follows require the follow action")
        if self.friend_request_enabled and REQUEST_FRIEND not in actions:
            raise ValueError("friend requests require the friend-request action")
        object.__setattr__(self, "selected_actions", actions)
        object.__setattr__(self, "target_allowlist", targets)
        object.__setattr__(self, "daily_total_limit", total_limit)
        object.__setattr__(self, "daily_action_limits", limits)
        object.__setattr__(
            self,
            "runner_configuration_fingerprint",
            fingerprint,
        )

    @property
    def active(self) -> bool:
        return bool(
            self.access.available
            and self.user_enabled
            and not self.halted
            and self.consecutive_failures < self.max_consecutive_failures
        )

    def action_daily_limit(
        self,
        action_type: str,
        *,
        task_type: AutonomyTaskType | None = None,
    ) -> int:
        configured = dict(self.daily_action_limits)
        limit = int(configured.get(action_type, self.daily_total_limit))
        if task_type == AutonomyTaskType.PROACTIVE_MESSAGE:
            limit = min(limit, AUTONOMOUS_OUTREACH_DAILY_LIMIT)
        return limit


@dataclass(frozen=True, slots=True)
class AgentAutonomyTask:
    id: uuid.UUID
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    task_type: AutonomyTaskType
    action_type: str
    status: AutonomyTaskStatus
    idempotency_key: str
    policy_version: int
    execution_setting_version: int
    runner_setting_version: int
    model_connection_id: uuid.UUID
    runner_configuration_fingerprint: str
    target_upstream_uid: str = ""
    source_message_identity: str = ""
    generation_instruction: str = ""
    scheduled_for: datetime | None = None
    not_before: datetime | None = None
    attempt_count: int = 0
    lease_token: str = field(default="", repr=False)
    lease_until: datetime | None = None

    def __post_init__(self) -> None:
        action = str(self.action_type or "").strip()
        if action not in FIXED_ACCOUNT_ACTION_SET:
            raise ValueError("autonomy task action is unsupported")
        key = str(self.idempotency_key or "").strip()
        if (
            not 8 <= len(key) <= MAX_IDEMPOTENCY_LENGTH
            or any(ord(char) < 32 for char in key)
        ):
            raise ValueError("autonomy task idempotency key is invalid")
        if (
            int(self.policy_version) < 1
            or int(self.execution_setting_version) < 1
            or int(self.runner_setting_version) < 1
        ):
            raise ValueError("autonomy task versions must be positive")
        if not isinstance(self.model_connection_id, uuid.UUID):
            raise ValueError("autonomy task model connection is invalid")
        fingerprint = str(self.runner_configuration_fingerprint or "").strip().lower()
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("autonomy task runner fingerprint is invalid")
        if int(self.attempt_count) < 0:
            raise ValueError("autonomy task attempt count cannot be negative")
        target = str(self.target_upstream_uid or "").strip()
        source = str(self.source_message_identity or "").strip()
        instruction = str(self.generation_instruction or "").strip()
        if len(source) > 256 or any(ord(char) < 32 for char in source):
            raise ValueError("source message identity is invalid")
        if len(instruction) > MAX_INSTRUCTION_LENGTH:
            raise ValueError("generation instruction is too long")
        expected_action = {
            AutonomyTaskType.REPLY_TO_MESSAGE: SEND_PRIVATE_MESSAGE,
            AutonomyTaskType.SCHEDULED_POST: PUBLISH_TEXT_POST,
            AutonomyTaskType.FOLLOW_TARGET: FOLLOW_USER,
            AutonomyTaskType.UNFOLLOW_TARGET: UNFOLLOW_USER,
            AutonomyTaskType.BROWSE_ONLINE: BROWSE_ONLINE_USERS,
            AutonomyTaskType.REQUEST_MATCH: REQUEST_TEXT_MATCH,
            AutonomyTaskType.PROACTIVE_MESSAGE: SEND_PRIVATE_MESSAGE,
            AutonomyTaskType.FOLLOW_DISCOVERED: FOLLOW_USER,
            AutonomyTaskType.REQUEST_FRIEND: REQUEST_FRIEND,
        }[self.task_type]
        if action != expected_action:
            raise ValueError("autonomy task type and action do not match")
        if self.task_type == AutonomyTaskType.REPLY_TO_MESSAGE:
            target = _normalize_target(target)
            if not source:
                raise ValueError("reply task requires a source message identity")
        elif self.task_type == AutonomyTaskType.SCHEDULED_POST:
            if target or source or self.scheduled_for is None:
                raise ValueError("scheduled post task parameters are invalid")
        elif self.task_type in {
            AutonomyTaskType.FOLLOW_TARGET,
            AutonomyTaskType.UNFOLLOW_TARGET,
            AutonomyTaskType.FOLLOW_DISCOVERED,
        }:
            target = _normalize_target(target)
            if source or instruction:
                raise ValueError("relationship task parameters are invalid")
        elif self.task_type in {
            AutonomyTaskType.PROACTIVE_MESSAGE,
            AutonomyTaskType.REQUEST_FRIEND,
        }:
            target = _normalize_target(target)
            if source or self.scheduled_for is not None:
                raise ValueError("outreach task parameters are invalid")
        else:
            if target or source or instruction or self.scheduled_for is not None:
                raise ValueError("discovery task parameters are invalid")
        object.__setattr__(self, "action_type", action)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "target_upstream_uid", target)
        object.__setattr__(self, "source_message_identity", source)
        object.__setattr__(self, "generation_instruction", instruction)
        object.__setattr__(
            self,
            "runner_configuration_fingerprint",
            fingerprint,
        )


@dataclass(frozen=True, slots=True)
class ConversationHead:
    peer_upstream_uid: str
    message_identity: str
    direction: MessageDirection
    message_type: str
    body: str
    occurred_at: datetime
    revoked: bool = False
    has_outgoing_after: bool = False

    def is_unanswered_inbound(self, expected_identity: str) -> bool:
        message_type = str(self.message_type or "").strip().lower()
        return bool(
            self.message_identity == str(expected_identity or "").strip()
            and self.direction == MessageDirection.INCOMING
            and message_type in {"text", "timtextelem"}
            and str(self.body or "").strip()
            and not self.revoked
            and not self.has_outgoing_after
        )


@dataclass(frozen=True, slots=True)
class GeneratedText:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class FixedActionCommand:
    action_type: str
    idempotency_key: str
    target_upstream_uid: str = ""
    content: str = ""
    visibility: str = "public"

    def __post_init__(self) -> None:
        action = str(self.action_type or "").strip()
        if action not in FIXED_ACCOUNT_ACTION_SET:
            raise ValueError("fixed action command is unsupported")
        key = str(self.idempotency_key or "").strip()
        if not 8 <= len(key) <= MAX_IDEMPOTENCY_LENGTH:
            raise ValueError("fixed action idempotency key is invalid")
        target = str(self.target_upstream_uid or "").strip()
        content = str(self.content or "").strip()
        visibility = str(self.visibility or "public").strip().lower()
        if action in {
            SEND_PRIVATE_MESSAGE,
            FOLLOW_USER,
            UNFOLLOW_USER,
            REQUEST_FRIEND,
        }:
            target = _normalize_target(target)
        elif target:
            raise ValueError("this fixed action does not accept a target")
        if action in {SEND_PRIVATE_MESSAGE, PUBLISH_TEXT_POST, REQUEST_FRIEND}:
            if not content or len(content) > MAX_AUTONOMOUS_TEXT_LENGTH:
                raise ValueError("fixed action content is invalid")
            if (
                action == REQUEST_FRIEND
                and len(content) > MAX_AUTONOMOUS_FRIEND_REQUEST_LENGTH
            ):
                raise ValueError("friend request content is too long")
        elif content:
            raise ValueError("relationship action does not accept content")
        if action == PUBLISH_TEXT_POST:
            if visibility != "public":
                raise ValueError("unattended posts are restricted to public text posts")
        elif visibility != "public":
            raise ValueError("non-post action does not accept visibility")
        object.__setattr__(self, "action_type", action)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "target_upstream_uid", target)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "visibility", visibility)


@dataclass(frozen=True, slots=True)
class DispatchReservationRequest:
    task_id: uuid.UUID
    owner_user_id: uuid.UUID
    external_account_id: uuid.UUID
    lease_token: str = field(repr=False)
    action_type: str
    action_idempotency_key: str
    expected_policy_version: int
    expected_execution_setting_version: int
    expected_runner_setting_version: int
    expected_model_connection_id: uuid.UUID
    expected_runner_configuration_fingerprint: str
    expected_source_message_identity: str
    target_upstream_uid: str
    budget_day: date
    daily_total_limit: int
    daily_action_limit: int
    minimum_interval_seconds: int
    now: datetime


@dataclass(frozen=True, slots=True)
class DispatchReservationDecision:
    code: DispatchDecisionCode
    permit_token: str = field(default="", repr=False)
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.code == DispatchDecisionCode.ALLOWED and not self.permit_token:
            raise ValueError("allowed dispatch decision requires a permit token")
        if self.code != DispatchDecisionCode.ALLOWED and self.permit_token:
            raise ValueError("denied dispatch decision cannot expose a permit token")


@dataclass(frozen=True, slots=True)
class FixedActionDispatchResult:
    outcome: FixedActionOutcome
    stable_error_code: str = ""
    result_id: str = ""

    def __post_init__(self) -> None:
        code = str(self.stable_error_code or "").strip().lower()
        if self.outcome == FixedActionOutcome.SUCCEEDED:
            code = ""
        elif not _STABLE_CODE.fullmatch(code):
            raise ValueError("failed fixed action requires a stable error code")
        object.__setattr__(self, "stable_error_code", code)
        object.__setattr__(self, "result_id", str(self.result_id or "")[:256])


@dataclass(frozen=True, slots=True)
class TaskCompletion:
    status: AutonomyTaskStatus
    stable_error_code: str = ""
    result_id: str = ""
    outcome_unknown: bool = False
    count_failure: bool = False
    reset_failures: bool = False
    force_halt: bool = False

    def __post_init__(self) -> None:
        if self.status not in TERMINAL_TASK_STATUSES:
            raise ValueError("task completion must use a terminal status")
        code = str(self.stable_error_code or "").strip().lower()
        if self.status == AutonomyTaskStatus.SUCCEEDED:
            if code or self.outcome_unknown or self.count_failure or self.force_halt:
                raise ValueError("successful completion cannot contain failure state")
        elif not _STABLE_CODE.fullmatch(code):
            raise ValueError("non-success completion requires a stable error code")
        if self.outcome_unknown and self.status != AutonomyTaskStatus.MANUAL_REVIEW:
            raise ValueError("unknown outcomes require manual review")
        if self.status == AutonomyTaskStatus.MANUAL_REVIEW and not self.outcome_unknown:
            raise ValueError("manual review requires an unknown outcome")
        if self.outcome_unknown and not self.count_failure:
            raise ValueError("unknown outcomes must count as failures")
        if self.outcome_unknown and not self.force_halt:
            raise ValueError("unknown outcomes must halt unattended execution")
        object.__setattr__(self, "stable_error_code", code)
        object.__setattr__(self, "result_id", str(self.result_id or "")[:256])


@dataclass(frozen=True, slots=True)
class AgentAutonomyRunResult:
    status: str
    code: str
    task_id: uuid.UUID | None = None


class AgentAutonomyStore(Protocol):
    """Durable lease, policy, budget and task transition boundary.

    ``claim_next`` may reclaim expired ``leased`` or ``generating`` work, but
    must never reclaim ``dispatching`` work.  Expired dispatching tasks require
    manual review because the external outcome may be unknown.

    ``begin_dispatch`` must be one atomic transaction that rechecks all global,
    administrator, user, policy-version and fixed-action gates; revalidates the
    expected inbound message when supplied; reserves both daily hard budgets;
    enforces the minimum interval; and changes the task to ``dispatching``.
    A budget reservation is not released after a failed or unknown action.
    """

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AgentAutonomyTask | None: ...

    def load_policy(self, owner_user_id: uuid.UUID) -> AgentAutonomyPolicy | None: ...

    def load_conversation_head(
        self,
        *,
        owner_user_id: uuid.UUID,
        peer_upstream_uid: str,
    ) -> ConversationHead | None: ...

    def begin_generation(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool: ...

    def renew_lease(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool: ...

    def begin_dispatch(
        self,
        request: DispatchReservationRequest,
    ) -> DispatchReservationDecision: ...

    def defer_task(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        not_before: datetime,
        stable_error_code: str,
        now: datetime,
    ) -> bool: ...

    def finish_task(
        self,
        *,
        task_id: uuid.UUID,
        lease_token: str,
        completion: TaskCompletion,
        now: datetime,
    ) -> bool: ...


class AgentAutonomyModelRunner(Protocol):
    def generate_reply(
        self,
        *,
        task: AgentAutonomyTask,
        head: ConversationHead,
        policy: AgentAutonomyPolicy,
    ) -> GeneratedText: ...

    def generate_scheduled_post(
        self,
        *,
        task: AgentAutonomyTask,
        policy: AgentAutonomyPolicy,
    ) -> GeneratedText: ...

    def generate_proactive_message(
        self,
        *,
        task: AgentAutonomyTask,
        policy: AgentAutonomyPolicy,
    ) -> GeneratedText: ...

    def generate_friend_request(
        self,
        *,
        task: AgentAutonomyTask,
        policy: AgentAutonomyPolicy,
    ) -> GeneratedText: ...


class AgentAutonomyFixedActionDispatcher(Protocol):
    """Adapter to the fixed account-action execution layer only."""

    def execute_fixed_action(
        self,
        *,
        command: FixedActionCommand,
        permit_token: str,
    ) -> FixedActionDispatchResult: ...


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("autonomy timestamps must be timezone-aware")
    return value.astimezone(UTC)


def task_is_reclaimable(task: AgentAutonomyTask, *, now: datetime) -> bool:
    current = _aware_utc(now)
    if task.status not in CLAIMABLE_TASK_STATUSES:
        return False
    if task.status == AutonomyTaskStatus.QUEUED:
        return task.not_before is None or _aware_utc(task.not_before) <= current
    if task.status == AutonomyTaskStatus.DEFERRED:
        return task.not_before is not None and _aware_utc(task.not_before) <= current
    return task.lease_until is not None and _aware_utc(task.lease_until) <= current


def deterministic_task_key(
    *,
    owner_user_id: uuid.UUID,
    task_type: AutonomyTaskType,
    source_identity: str,
) -> str:
    source = str(source_identity or "").strip()
    if not source or len(source) > 512 or any(ord(char) < 32 for char in source):
        raise ValueError("autonomy task source identity is invalid")
    payload = json.dumps(
        {
            "owner_user_id": str(owner_user_id),
            "source_identity": source,
            "task_type": task_type.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"autonomy:{task_type.value}:{hashlib.sha256(payload).hexdigest()}"


def deterministic_action_key(task: AgentAutonomyTask) -> str:
    payload = f"{task.owner_user_id}:{task.action_type}:{task.idempotency_key}"
    return f"autonomous:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def quiet_window_end(
    policy: AgentAutonomyPolicy,
    *,
    now: datetime,
) -> datetime | None:
    if policy.quiet_start_minute is None or policy.quiet_end_minute is None:
        return None
    current = _aware_utc(now)
    zone = ZoneInfo(policy.quiet_timezone)
    local = current.astimezone(zone)
    minute = local.hour * 60 + local.minute
    start = int(policy.quiet_start_minute)
    end = int(policy.quiet_end_minute)
    if start < end:
        in_quiet = start <= minute < end
        end_day = local.date()
    else:
        in_quiet = minute >= start or minute < end
        end_day = local.date() + timedelta(days=1) if minute >= start else local.date()
    if not in_quiet:
        return None
    local_end = datetime.combine(
        end_day,
        time(hour=end // 60, minute=end % 60),
        tzinfo=zone,
    )
    return local_end.astimezone(UTC)


def policy_budget_day(policy: AgentAutonomyPolicy, *, now: datetime) -> date:
    return _aware_utc(now).astimezone(ZoneInfo(policy.quiet_timezone)).date()


def _safe_generated_text(value: GeneratedText, *, action_type: str) -> str:
    text_value = str(value.text or "").strip()
    if not text_value:
        raise AgentAutonomyModelError(
            "empty_generated_text",
            "模型没有生成可执行的文字内容",
        )
    if len(text_value) > MAX_AUTONOMOUS_TEXT_LENGTH:
        raise AgentAutonomyModelError(
            "generated_text_too_long",
            "模型生成内容超过两千字，本次未执行",
        )
    if any(
        ord(character) < 32 and character not in {"\n", "\t"}
        for character in text_value
    ):
        raise AgentAutonomyModelError(
            "generated_content_control_character",
            "模型生成内容含有不允许的控制字符，本次未执行",
        )
    if _AUTONOMOUS_LINK.search(text_value):
        raise AgentAutonomyModelError(
            "generated_content_link_rejected",
            "自动社交内容不能发送外部链接，本次未执行",
        )
    if _AUTONOMOUS_CREDENTIAL.search(text_value):
        raise AgentAutonomyModelError(
            "generated_content_sensitive_rejected",
            "模型生成内容可能包含凭据或会话信息，本次未执行",
        )
    if action_type == PUBLISH_TEXT_POST and (
        _AUTONOMOUS_EMAIL.search(text_value)
        or _AUTONOMOUS_PHONE.search(text_value)
    ):
        raise AgentAutonomyModelError(
            "generated_content_privacy_rejected",
            "公开动态可能包含联系方式，本次未自动发布",
        )
    if action_type == REQUEST_FRIEND and len(text_value) > MAX_AUTONOMOUS_FRIEND_REQUEST_LENGTH:
        raise AgentAutonomyModelError(
            "friend_request_text_too_long",
            "好友申请内容超过两百字，本次未发送",
        )
    return text_value


class AgentAutonomyOrchestrator:
    def __init__(
        self,
        *,
        store: AgentAutonomyStore,
        model_runner: AgentAutonomyModelRunner,
        dispatcher: AgentAutonomyFixedActionDispatcher,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 300,
    ) -> None:
        if not 30 <= int(lease_seconds) <= 1_800:
            raise ValueError("autonomy task lease must be between 30 and 1800 seconds")
        self.store = store
        self.model_runner = model_runner
        self.dispatcher = dispatcher
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = int(lease_seconds)

    def run_once(self, *, worker_id: str) -> AgentAutonomyRunResult:
        worker = str(worker_id or "").strip()
        if not worker or len(worker) > 128 or any(ord(char) < 33 for char in worker):
            raise ValueError("autonomy worker identifier is invalid")
        now = _aware_utc(self.clock())
        task = self.store.claim_next(
            worker_id=worker,
            now=now,
            lease_until=now + timedelta(seconds=self.lease_seconds),
        )
        if task is None:
            return AgentAutonomyRunResult(status="idle", code="no_due_task")
        if task.status != AutonomyTaskStatus.LEASED or not task.lease_token:
            return AgentAutonomyRunResult(
                status="rejected",
                code="invalid_claim",
                task_id=task.id,
            )
        if task.attempt_count > MAX_AUTONOMY_TASK_ATTEMPTS:
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.FAILED,
                    stable_error_code="task_attempts_exhausted",
                    count_failure=True,
                ),
                now=now,
            )

        policy = self.store.load_policy(task.owner_user_id)
        preflight = self._preflight(task=task, policy=policy, now=now)
        if preflight is not None:
            return preflight
        assert policy is not None

        quiet_end = quiet_window_end(policy, now=now)
        if quiet_end is not None:
            return self._defer(
                task,
                not_before=quiet_end,
                code="quiet_hours",
                now=now,
            )

        head: ConversationHead | None = None
        if task.task_type == AutonomyTaskType.REPLY_TO_MESSAGE:
            head = self.store.load_conversation_head(
                owner_user_id=task.owner_user_id,
                peer_upstream_uid=task.target_upstream_uid,
            )
            if head is None or not head.is_unanswered_inbound(
                task.source_message_identity
            ):
                return self._finish(
                    task,
                    TaskCompletion(
                        status=AutonomyTaskStatus.STALE,
                        stable_error_code="inbound_message_already_handled",
                    ),
                    now=now,
                )
        elif task.task_type == AutonomyTaskType.PROACTIVE_MESSAGE:
            head = self.store.load_conversation_head(
                owner_user_id=task.owner_user_id,
                peer_upstream_uid=task.target_upstream_uid,
            )
            if head is not None:
                return self._finish(
                    task,
                    TaskCompletion(
                        status=AutonomyTaskStatus.STALE,
                        stable_error_code=(
                            "inbound_message_requires_reply"
                            if head.direction == MessageDirection.INCOMING
                            else "proactive_message_already_sent"
                        ),
                    ),
                    now=now,
                )

        content = ""
        if task.task_type in {
            AutonomyTaskType.REPLY_TO_MESSAGE,
            AutonomyTaskType.SCHEDULED_POST,
            AutonomyTaskType.PROACTIVE_MESSAGE,
            AutonomyTaskType.REQUEST_FRIEND,
        }:
            if not self.store.begin_generation(
                task_id=task.id,
                lease_token=task.lease_token,
                now=now,
                lease_until=now + timedelta(seconds=self.lease_seconds),
            ):
                return AgentAutonomyRunResult(
                    status="lease_lost",
                    code="lease_lost_before_generation",
                    task_id=task.id,
                )
            try:
                if task.task_type == AutonomyTaskType.REPLY_TO_MESSAGE:
                    assert head is not None
                    generated = self.model_runner.generate_reply(
                        task=task,
                        head=head,
                        policy=policy,
                    )
                elif task.task_type == AutonomyTaskType.SCHEDULED_POST:
                    generated = self.model_runner.generate_scheduled_post(
                        task=task,
                        policy=policy,
                    )
                elif task.task_type == AutonomyTaskType.PROACTIVE_MESSAGE:
                    generated = self.model_runner.generate_proactive_message(
                        task=task,
                        policy=policy,
                    )
                else:
                    generated = self.model_runner.generate_friend_request(
                        task=task,
                        policy=policy,
                    )
                content = _safe_generated_text(
                    generated,
                    action_type=task.action_type,
                )
            except AgentAutonomyModelError as exc:
                return self._finish_generation_failure(task, exc, now=self._now())
            except Exception:
                return self._finish_generation_failure(
                    task,
                    AgentAutonomyModelError(
                        "model_generation_failed",
                        "模型生成失败，本次未执行账号操作",
                    ),
                    now=self._now(),
                )

            now = self._now()
            if not self.store.renew_lease(
                task_id=task.id,
                lease_token=task.lease_token,
                now=now,
                lease_until=now + timedelta(seconds=self.lease_seconds),
            ):
                return AgentAutonomyRunResult(
                    status="lease_lost",
                    code="lease_lost_after_generation",
                    task_id=task.id,
                )
            quiet_end = quiet_window_end(policy, now=now)
            if quiet_end is not None:
                return self._defer(
                    task,
                    not_before=quiet_end,
                    code="quiet_hours",
                    now=now,
                )
            if task.task_type == AutonomyTaskType.REPLY_TO_MESSAGE:
                head = self.store.load_conversation_head(
                    owner_user_id=task.owner_user_id,
                    peer_upstream_uid=task.target_upstream_uid,
                )
                if head is None or not head.is_unanswered_inbound(
                    task.source_message_identity
                ):
                    return self._finish(
                        task,
                        TaskCompletion(
                            status=AutonomyTaskStatus.STALE,
                            stable_error_code="inbound_message_changed_during_generation",
                        ),
                        now=now,
                    )
            elif task.task_type == AutonomyTaskType.PROACTIVE_MESSAGE:
                head = self.store.load_conversation_head(
                    owner_user_id=task.owner_user_id,
                    peer_upstream_uid=task.target_upstream_uid,
                )
                if head is not None:
                    return self._finish(
                        task,
                        TaskCompletion(
                            status=AutonomyTaskStatus.STALE,
                            stable_error_code=(
                                "inbound_message_requires_reply"
                                if head.direction == MessageDirection.INCOMING
                                else "proactive_message_changed_during_generation"
                            ),
                        ),
                        now=now,
                    )
            elif (
                task.task_type == AutonomyTaskType.SCHEDULED_POST
                and now - _aware_utc(task.scheduled_for)
                > timedelta(seconds=policy.scheduled_post_max_lateness_seconds)
            ):
                return self._finish(
                    task,
                    TaskCompletion(
                        status=AutonomyTaskStatus.STALE,
                        stable_error_code="scheduled_post_expired_during_generation",
                    ),
                    now=now,
                )

        command = FixedActionCommand(
            action_type=task.action_type,
            target_upstream_uid=task.target_upstream_uid,
            content=content,
            visibility="public",
            idempotency_key=deterministic_action_key(task),
        )
        reservation = self.store.begin_dispatch(
            DispatchReservationRequest(
                task_id=task.id,
                owner_user_id=task.owner_user_id,
                external_account_id=task.external_account_id,
                lease_token=task.lease_token,
                action_type=task.action_type,
                action_idempotency_key=command.idempotency_key,
                expected_policy_version=task.policy_version,
                expected_execution_setting_version=task.execution_setting_version,
                expected_runner_setting_version=task.runner_setting_version,
                expected_model_connection_id=task.model_connection_id,
                expected_runner_configuration_fingerprint=(
                    task.runner_configuration_fingerprint
                ),
                expected_source_message_identity=task.source_message_identity,
                target_upstream_uid=task.target_upstream_uid,
                budget_day=policy_budget_day(policy, now=now),
                daily_total_limit=policy.daily_total_limit,
                daily_action_limit=policy.action_daily_limit(
                    task.action_type,
                    task_type=task.task_type,
                ),
                minimum_interval_seconds=policy.minimum_interval_seconds,
                now=now,
            )
        )
        denied = self._handle_dispatch_denial(task, reservation, now=now)
        if denied is not None:
            return denied

        dispatch_task = replace(task, lease_token=reservation.permit_token)

        try:
            dispatched = self.dispatcher.execute_fixed_action(
                command=command,
                permit_token=reservation.permit_token,
            )
        except Exception:
            return self._finish(
                dispatch_task,
                TaskCompletion(
                    status=AutonomyTaskStatus.MANUAL_REVIEW,
                    stable_error_code="dispatch_outcome_unknown",
                    outcome_unknown=True,
                    count_failure=True,
                    force_halt=True,
                ),
                now=self._now(),
            )
        if dispatched.outcome == FixedActionOutcome.SUCCEEDED:
            return self._finish(
                dispatch_task,
                TaskCompletion(
                    status=AutonomyTaskStatus.SUCCEEDED,
                    result_id=dispatched.result_id,
                    reset_failures=True,
                ),
                now=self._now(),
            )
        if dispatched.outcome == FixedActionOutcome.OUTCOME_UNKNOWN:
            return self._finish(
                dispatch_task,
                TaskCompletion(
                    status=AutonomyTaskStatus.MANUAL_REVIEW,
                    stable_error_code=dispatched.stable_error_code,
                    outcome_unknown=True,
                    count_failure=True,
                    force_halt=True,
                ),
                now=self._now(),
            )
        if dispatched.stable_error_code == "local_message_source_stale":
            return self._finish(
                dispatch_task,
                TaskCompletion(
                    status=AutonomyTaskStatus.STALE,
                    stable_error_code=dispatched.stable_error_code,
                ),
                now=self._now(),
            )
        return self._finish(
            dispatch_task,
            TaskCompletion(
                status=AutonomyTaskStatus.FAILED,
                stable_error_code=dispatched.stable_error_code,
                count_failure=True,
            ),
            now=self._now(),
        )

    def _now(self) -> datetime:
        return _aware_utc(self.clock())

    def _preflight(
        self,
        *,
        task: AgentAutonomyTask,
        policy: AgentAutonomyPolicy | None,
        now: datetime,
    ) -> AgentAutonomyRunResult | None:
        if policy is None or not policy.access.available or not policy.user_enabled:
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_access_revoked",
                ),
                now=now,
            )
        if (
            task.external_account_id != policy.external_account_id
            or task.policy_version != policy.version
            or task.execution_setting_version != policy.execution_setting_version
            or task.runner_setting_version != policy.runner_setting_version
            or task.model_connection_id != policy.model_connection_id
            or task.runner_configuration_fingerprint
            != policy.runner_configuration_fingerprint
        ):
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_policy_changed",
                ),
                now=now,
            )
        if policy.halted or policy.consecutive_failures >= policy.max_consecutive_failures:
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_halted",
                ),
                now=now,
            )
        if task.action_type not in policy.selected_actions:
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_action_not_allowed",
                ),
                now=now,
            )
        feature_enabled = {
            AutonomyTaskType.REPLY_TO_MESSAGE: policy.auto_reply_enabled,
            AutonomyTaskType.SCHEDULED_POST: policy.scheduled_posts_enabled,
            AutonomyTaskType.FOLLOW_TARGET: policy.relationship_actions_enabled,
            AutonomyTaskType.UNFOLLOW_TARGET: policy.relationship_actions_enabled,
            AutonomyTaskType.BROWSE_ONLINE: policy.discovery_enabled,
            AutonomyTaskType.REQUEST_MATCH: policy.text_match_enabled,
            AutonomyTaskType.PROACTIVE_MESSAGE: policy.proactive_message_enabled,
            AutonomyTaskType.FOLLOW_DISCOVERED: policy.follow_discovered_enabled,
            AutonomyTaskType.REQUEST_FRIEND: policy.friend_request_enabled,
        }[task.task_type]
        if not feature_enabled:
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_task_type_disabled",
                ),
                now=now,
            )
        if (
            task.task_type in {
                AutonomyTaskType.REPLY_TO_MESSAGE,
                AutonomyTaskType.PROACTIVE_MESSAGE,
            }
            and not policy.access.private_message_auto_send_enabled
        ):
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_auto_send_disabled",
                ),
                now=now,
            )
        if (
            task.task_type in {
                AutonomyTaskType.FOLLOW_TARGET,
                AutonomyTaskType.UNFOLLOW_TARGET,
            }
            and task.target_upstream_uid not in policy.target_allowlist
        ):
            return self._finish(
                task,
                TaskCompletion(
                    status=AutonomyTaskStatus.CANCELLED,
                    stable_error_code="autonomy_target_not_allowlisted",
                ),
                now=now,
            )
        if task.task_type == AutonomyTaskType.SCHEDULED_POST:
            scheduled = _aware_utc(task.scheduled_for)
            if scheduled > now:
                return self._defer(
                    task,
                    not_before=scheduled,
                    code="scheduled_time_not_reached",
                    now=now,
                )
            if now - scheduled > timedelta(
                seconds=policy.scheduled_post_max_lateness_seconds
            ):
                return self._finish(
                    task,
                    TaskCompletion(
                        status=AutonomyTaskStatus.STALE,
                        stable_error_code="scheduled_post_too_late",
                    ),
                    now=now,
                )
        return None

    def _handle_dispatch_denial(
        self,
        task: AgentAutonomyTask,
        decision: DispatchReservationDecision,
        *,
        now: datetime,
    ) -> AgentAutonomyRunResult | None:
        if decision.code == DispatchDecisionCode.ALLOWED:
            return None
        if decision.code == DispatchDecisionCode.LEASE_LOST:
            return AgentAutonomyRunResult(
                status="lease_lost",
                code="lease_lost_before_dispatch",
                task_id=task.id,
            )
        if decision.code == DispatchDecisionCode.MINIMUM_INTERVAL:
            if decision.retry_at is None:
                return self._finish(
                    task,
                    TaskCompletion(
                        status=AutonomyTaskStatus.FAILED,
                        stable_error_code="invalid_interval_decision",
                        count_failure=True,
                    ),
                    now=now,
                )
            return self._defer(
                task,
                not_before=decision.retry_at,
                code="minimum_interval",
                now=now,
            )
        status = (
            AutonomyTaskStatus.STALE
            if decision.code == DispatchDecisionCode.SOURCE_STALE
            else AutonomyTaskStatus.CANCELLED
        )
        return self._finish(
            task,
            TaskCompletion(
                status=status,
                stable_error_code=f"dispatch_{decision.code.value}",
            ),
            now=now,
        )

    def _finish_generation_failure(
        self,
        task: AgentAutonomyTask,
        exc: AgentAutonomyModelError,
        *,
        now: datetime,
    ) -> AgentAutonomyRunResult:
        return self._finish(
            task,
            TaskCompletion(
                status=AutonomyTaskStatus.FAILED,
                stable_error_code=exc.code,
                count_failure=True,
            ),
            now=now,
        )

    def _defer(
        self,
        task: AgentAutonomyTask,
        *,
        not_before: datetime,
        code: str,
        now: datetime,
    ) -> AgentAutonomyRunResult:
        deferred = self.store.defer_task(
            task_id=task.id,
            lease_token=task.lease_token,
            not_before=_aware_utc(not_before),
            stable_error_code=code,
            now=now,
        )
        return AgentAutonomyRunResult(
            status="deferred" if deferred else "lease_lost",
            code=code if deferred else "lease_lost_while_deferring",
            task_id=task.id,
        )

    def _finish(
        self,
        task: AgentAutonomyTask,
        completion: TaskCompletion,
        *,
        now: datetime,
    ) -> AgentAutonomyRunResult:
        finished = self.store.finish_task(
            task_id=task.id,
            lease_token=task.lease_token,
            completion=completion,
            now=now,
        )
        return AgentAutonomyRunResult(
            status=completion.status.value if finished else "completion_not_recorded",
            code=(
                completion.stable_error_code
                if finished and completion.stable_error_code
                else "ok"
                if finished
                else "completion_not_recorded"
            ),
            task_id=task.id,
        )
