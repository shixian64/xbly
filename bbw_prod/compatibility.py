"""Legacy Banghua/TIM compatibility lifecycle controls.

The mode is intentionally environment-backed so every process can reject
legacy work before importing a protocol adapter or opening a network client.
PostgreSQL remains authoritative in every mode:

``enabled``
    Create and dispatch optional legacy mirrors.
``paused``
    Keep new mirrors pending, but do not schedule or execute them.
``retired``
    Never dispatch legacy work.  New optional mirrors are written as terminal
    ``cancelled`` audit records; an explicit operator command archives older
    pending work.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any


class CompatibilityMode(str, Enum):
    ENABLED = "enabled"
    PAUSED = "paused"
    RETIRED = "retired"


COMPATIBILITY_MODE_ENV = "BBW_COMPATIBILITY_MODE"
COMPATIBILITY_CANCELLED_REASON = "legacy_compatibility_retired"
COMPATIBILITY_ARCHIVED_REASON = "legacy_compatibility_archived_by_operator"
TIM_MIRROR_CANCELLED_REASON = "optional_tim_mirror_retired"
TIM_MIRROR_ARCHIVED_REASON = "optional_tim_mirror_archived_by_operator"


def normalize_compatibility_mode(value: Any) -> CompatibilityMode:
    normalized = str(value or CompatibilityMode.ENABLED.value).strip().lower()
    try:
        return CompatibilityMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in CompatibilityMode)
        raise ValueError(f"compatibility mode must be one of: {allowed}") from exc


def compatibility_mode(settings: Any | None = None) -> CompatibilityMode:
    """Resolve a mode without importing a provider or validating secrets."""

    if settings is not None and getattr(settings, "compatibility_mode", None) is not None:
        return normalize_compatibility_mode(settings.compatibility_mode)
    return normalize_compatibility_mode(
        os.getenv(COMPATIBILITY_MODE_ENV, CompatibilityMode.ENABLED.value)
    )


def compatibility_dispatch_enabled(settings: Any | None = None) -> bool:
    return compatibility_mode(settings) is CompatibilityMode.ENABLED


def is_cancellable_compatibility_operation(operation_type: Any) -> bool:
    """Return whether an optional legacy mutation may be retired.

    Historical media archive work is migration evidence, not an optional APK
    mirror.  It must finish (or be handled separately) before retirement and
    is therefore never silently cancelled by this lifecycle switch.
    """

    operation = str(operation_type or "").strip().lower()
    return operation.startswith("compatibility.") and not operation.startswith(
        "compatibility.media.archive"
    )


def new_compatibility_outbox_status(
    operation_type: Any,
    *,
    settings: Any | None = None,
) -> str:
    if (
        compatibility_mode(settings) is CompatibilityMode.RETIRED
        and is_cancellable_compatibility_operation(operation_type)
    ):
        return "cancelled"
    return "pending"


def new_optional_tim_status(*, settings: Any | None = None) -> str:
    if compatibility_mode(settings) is CompatibilityMode.RETIRED:
        return "cancelled"
    return "pending"


__all__ = [
    "COMPATIBILITY_ARCHIVED_REASON",
    "COMPATIBILITY_CANCELLED_REASON",
    "COMPATIBILITY_MODE_ENV",
    "TIM_MIRROR_ARCHIVED_REASON",
    "TIM_MIRROR_CANCELLED_REASON",
    "CompatibilityMode",
    "compatibility_dispatch_enabled",
    "compatibility_mode",
    "is_cancellable_compatibility_operation",
    "new_compatibility_outbox_status",
    "new_optional_tim_status",
    "normalize_compatibility_mode",
]
