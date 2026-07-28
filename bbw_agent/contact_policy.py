"""Pure contact-level policy and relationship-stage decisions.

This module intentionally has no database or provider dependencies.  The
autonomous scheduler and the conversation UI use the same fail-closed rules so
that a missing policy row can never be interpreted as permission to send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final


CONTACT_MODE_SUGGEST_ONLY: Final = "suggest_only"
CONTACT_MODE_AUTO_LOW_RISK: Final = "auto_low_risk"
CONTACT_MODE_MANUAL_ONLY: Final = "manual_only"
CONTACT_POLICY_MODES: Final = frozenset(
    {
        CONTACT_MODE_SUGGEST_ONLY,
        CONTACT_MODE_AUTO_LOW_RISK,
        CONTACT_MODE_MANUAL_ONLY,
    }
)

RELATIONSHIP_STAGE_NEW: Final = "new"
RELATIONSHIP_STAGE_ENGAGED: Final = "engaged"
RELATIONSHIP_STAGE_ESTABLISHED: Final = "established"
RELATIONSHIP_STAGE_CLOSE: Final = "close"
RELATIONSHIP_STAGE_MANUAL_ONLY: Final = "manual_only"
RELATIONSHIP_STAGE_INACTIVE: Final = "inactive"
RELATIONSHIP_STAGES: Final = frozenset(
    {
        RELATIONSHIP_STAGE_NEW,
        RELATIONSHIP_STAGE_ENGAGED,
        RELATIONSHIP_STAGE_ESTABLISHED,
        RELATIONSHIP_STAGE_CLOSE,
        RELATIONSHIP_STAGE_MANUAL_ONLY,
        RELATIONSHIP_STAGE_INACTIVE,
    }
)
AUTO_REPLY_RELATIONSHIP_STAGES: Final = frozenset(
    {
        RELATIONSHIP_STAGE_ENGAGED,
        RELATIONSHIP_STAGE_ESTABLISHED,
        RELATIONSHIP_STAGE_CLOSE,
    }
)

DEFAULT_MINIMUM_REPLY_DELAY_SECONDS: Final = 30
DEFAULT_MAXIMUM_REPLY_AGE_SECONDS: Final = 2 * 60 * 60
MINIMUM_REPLY_DELAY_SECONDS: Final = 10
MAXIMUM_REPLY_DELAY_SECONDS: Final = 300
MINIMUM_REPLY_AGE_SECONDS: Final = 60
MAXIMUM_REPLY_AGE_SECONDS: Final = 2 * 60 * 60
ESTABLISHED_MESSAGE_COUNT: Final = 20
CLOSE_MESSAGE_COUNT: Final = 100
RECENT_BIDIRECTIONAL_DAYS: Final = 7
UNANSWERED_OUTGOING_INACTIVE_DAYS: Final = 7


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ContactRelationshipSignals:
    """Deterministic facts used to infer one contact's relationship stage."""

    total_message_count: int = 0
    incoming_message_count: int = 0
    outgoing_message_count: int = 0
    last_incoming_at: datetime | None = None
    last_outgoing_at: datetime | None = None
    latest_direction: str = ""
    latest_message_at: datetime | None = None
    blocked: bool = False
    conversation_available: bool = True

    def __post_init__(self) -> None:
        counts = (
            int(self.total_message_count),
            int(self.incoming_message_count),
            int(self.outgoing_message_count),
        )
        if any(value < 0 for value in counts):
            raise ValueError("contact message counts cannot be negative")
        if counts[1] + counts[2] > counts[0]:
            raise ValueError("directional contact counts exceed the total")
        direction = str(self.latest_direction or "").strip().lower()
        if direction not in {"", "incoming", "outgoing"}:
            raise ValueError("latest contact message direction is invalid")
        object.__setattr__(self, "total_message_count", counts[0])
        object.__setattr__(self, "incoming_message_count", counts[1])
        object.__setattr__(self, "outgoing_message_count", counts[2])
        object.__setattr__(self, "last_incoming_at", _aware_utc(self.last_incoming_at))
        object.__setattr__(self, "last_outgoing_at", _aware_utc(self.last_outgoing_at))
        object.__setattr__(self, "latest_message_at", _aware_utc(self.latest_message_at))
        object.__setattr__(self, "latest_direction", direction)


def derive_relationship_stage(
    signals: ContactRelationshipSignals,
    *,
    now: datetime,
    mode: str = CONTACT_MODE_SUGGEST_ONLY,
    stage_override: str | None = None,
) -> str:
    """Infer a conservative relationship stage from owner-scoped facts.

    Safety states take priority over a user-selected closeness override.  A
    user can always force manual handling, but cannot use a stage override to
    bypass a block, an unavailable account, or an old unanswered outbound.
    """

    normalized_mode = str(mode or CONTACT_MODE_SUGGEST_ONLY).strip().lower()
    if normalized_mode not in CONTACT_POLICY_MODES:
        raise ValueError("unsupported contact policy mode")
    override = str(stage_override or "").strip().lower() or None
    if override is not None and override not in RELATIONSHIP_STAGES:
        raise ValueError("unsupported relationship stage override")
    current = _aware_utc(now)
    assert current is not None

    if normalized_mode == CONTACT_MODE_MANUAL_ONLY or override == RELATIONSHIP_STAGE_MANUAL_ONLY:
        return RELATIONSHIP_STAGE_MANUAL_ONLY
    if signals.blocked or not signals.conversation_available:
        return RELATIONSHIP_STAGE_INACTIVE
    latest_at = signals.latest_message_at
    if (
        signals.latest_direction == "outgoing"
        and latest_at is not None
        and latest_at
        <= current - timedelta(days=UNANSWERED_OUTGOING_INACTIVE_DAYS)
    ):
        return RELATIONSHIP_STAGE_INACTIVE
    if override is not None:
        return override
    if signals.total_message_count >= CLOSE_MESSAGE_COUNT:
        return RELATIONSHIP_STAGE_CLOSE

    recent_cutoff = current - timedelta(days=RECENT_BIDIRECTIONAL_DAYS)
    recently_bidirectional = bool(
        signals.last_incoming_at is not None
        and signals.last_outgoing_at is not None
        and signals.last_incoming_at >= recent_cutoff
        and signals.last_outgoing_at >= recent_cutoff
    )
    if (
        signals.total_message_count >= ESTABLISHED_MESSAGE_COUNT
        and recently_bidirectional
    ):
        return RELATIONSHIP_STAGE_ESTABLISHED
    if signals.incoming_message_count > 0 and signals.outgoing_message_count > 0:
        return RELATIONSHIP_STAGE_ENGAGED
    return RELATIONSHIP_STAGE_NEW


_RISK_BOUNDARIES: Final = (
    (
        "credentials",
        re.compile(
            r"(?:密码|验证码|口令|密钥|令牌|token|cookie|session|账号登录)",
            re.IGNORECASE,
        ),
    ),
    (
        "financial",
        re.compile(
            r"(?:转账|付款|收款|借钱|还钱|红包|银行卡|支付宝|微信支付|交易|投资|保证金|多少钱)",
            re.IGNORECASE,
        ),
    ),
    (
        "contact_details",
        re.compile(
            r"(?:手机号|电话号码|电话号|加我微信|微信号|加微信|加我qq|qq号|邮箱|vx\s*[:：]?|v信)",
            re.IGNORECASE,
        ),
    ),
    (
        "precise_location_or_meeting",
        re.compile(
            r"(?:具体地址|发定位|共享定位|经纬度|房间号|酒店|宾馆|线下见面|出来见|约个地方|见一面)",
            re.IGNORECASE,
        ),
    ),
    (
        "health_or_self_harm",
        re.compile(
            r"(?:医院|看医生|吃什么药|怀孕|艾滋|抑郁|自残|自杀|不想活|心理疾病)",
            re.IGNORECASE,
        ),
    ),
    (
        "legal_or_conflict",
        re.compile(
            r"(?:报警|举报|律师|起诉|法院|威胁|勒索|打你|杀你|弄死|曝光你)",
            re.IGNORECASE,
        ),
    ),
    (
        "identity_or_commitment",
        re.compile(
            r"(?:结婚|领证|男朋友|女朋友|对象|承诺|保证永远|身份关系)",
            re.IGNORECASE,
        ),
    ),
    (
        "explicit_or_consent",
        re.compile(
            r"(?:做爱|性交|约炮|裸照|成人视频|安全词|身体边界|同意状态|强迫|拒绝还|轻度\s*sm|s[/\\]?m)",
            re.IGNORECASE,
        ),
    ),
)


def reply_risk_boundary(body: object, *, message_type: object = "text") -> str:
    """Return a stable manual-review reason, or an empty string when low risk."""

    normalized_type = str(message_type or "").strip().lower()
    if normalized_type not in {"text", "timtextelem"}:
        return "unsupported_media"
    text = str(body or "").strip()
    if not text:
        return "empty_message"
    for code, pattern in _RISK_BOUNDARIES:
        if pattern.search(text):
            return code
    return ""


def contact_policy_allows_auto_reply(
    *,
    persisted: bool,
    mode: str,
    paused: bool,
    relationship_stage: str,
    risk_boundary: str = "",
) -> bool:
    """Return whether explicit contact authorization permits one auto reply."""

    return bool(
        persisted
        and str(mode or "").strip().lower() == CONTACT_MODE_AUTO_LOW_RISK
        and not paused
        and str(relationship_stage or "").strip().lower()
        in AUTO_REPLY_RELATIONSHIP_STAGES
        and not str(risk_boundary or "").strip()
    )


def relationship_stage_allows_address_terms(stage: object) -> bool:
    return str(stage or "").strip().lower() in {
        RELATIONSHIP_STAGE_ESTABLISHED,
        RELATIONSHIP_STAGE_CLOSE,
    }
