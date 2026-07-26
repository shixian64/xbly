"""Preview and explicitly archive optional legacy compatibility work.

Running the module without ``--confirm-retire`` is strictly read-only.  The
write path additionally requires ``BBW_COMPATIBILITY_MODE=retired`` and an
exact confirmation phrase so an ordinary deployment command cannot cancel
pending APK mirrors accidentally.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence, TextIO

from sqlalchemy import func, or_, select, update

from .compatibility import (
    COMPATIBILITY_ARCHIVED_REASON,
    TIM_MIRROR_ARCHIVED_REASON,
    CompatibilityMode,
    compatibility_mode,
)
from .config import get_settings
from .db import session_scope
from .models import MessageDelivery, OperationOutbox, utcnow


CONFIRMATION_PHRASE = "RETIRE-LEGACY-COMPATIBILITY"
OUTBOX_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})
TIM_TERMINAL_STATUSES = frozenset({"delivered", "cancelled"})


@dataclass(frozen=True, slots=True)
class RetirementPreview:
    compatibility_mode: str
    dry_run: bool
    ordinary_outbox_by_status: dict[str, int]
    ordinary_outbox_total: int
    ordinary_outbox_cancellable: int
    media_archive_excluded: int
    optional_tim_by_status: dict[str, int]
    optional_tim_total: int
    optional_tim_cancellable: int
    required_tim_excluded: int


@dataclass(frozen=True, slots=True)
class RetirementArchiveResult:
    compatibility_outbox_cancelled: int
    optional_tim_cancelled: int
    archived_at: str


def _value(row: Any, name: str, default: Any = None) -> Any:
    mapping = getattr(row, "_mapping", None)
    if isinstance(mapping, Mapping) and name in mapping:
        return mapping[name]
    return getattr(row, name, default)


def _status_counts(rows: Sequence[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        status = str(_value(row, "status") or "unknown").strip().lower()
        try:
            count = max(0, int(_value(row, "item_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            count = 0
        counts[status] += count
    return dict(sorted(counts.items()))


def _ordinary_outbox_filters() -> tuple[Any, ...]:
    return (
        OperationOutbox.operation_type.like("compatibility.%"),
        OperationOutbox.operation_type.not_like("compatibility.media.archive%"),
    )


def inspect_retirement(
    db: Any,
    *,
    mode: CompatibilityMode | str | None = None,
) -> RetirementPreview:
    resolved_mode = (
        CompatibilityMode(mode)
        if mode is not None
        else compatibility_mode()
    )
    outbox_rows = list(
        db.execute(
            select(
                OperationOutbox.status.label("status"),
                func.count(OperationOutbox.id).label("item_count"),
            )
            .where(*_ordinary_outbox_filters())
            .group_by(OperationOutbox.status)
        )
    )
    tim_rows = list(
        db.execute(
            select(
                MessageDelivery.status.label("status"),
                func.count(MessageDelivery.id).label("item_count"),
            )
            .where(
                MessageDelivery.channel == "tim",
                MessageDelivery.required.is_(False),
            )
            .group_by(MessageDelivery.status)
        )
    )
    media_archive_excluded = int(
        db.scalar(
            select(func.count(OperationOutbox.id)).where(
                or_(
                    OperationOutbox.operation_type == "media.archive",
                    OperationOutbox.operation_type.like(
                        "compatibility.media.archive%"
                    ),
                )
            )
        )
        or 0
    )
    required_tim_excluded = int(
        db.scalar(
            select(func.count(MessageDelivery.id)).where(
                MessageDelivery.channel == "tim",
                MessageDelivery.required.is_(True),
            )
        )
        or 0
    )
    outbox_counts = _status_counts(outbox_rows)
    tim_counts = _status_counts(tim_rows)
    return RetirementPreview(
        compatibility_mode=resolved_mode.value,
        dry_run=True,
        ordinary_outbox_by_status=outbox_counts,
        ordinary_outbox_total=sum(outbox_counts.values()),
        ordinary_outbox_cancellable=sum(
            count
            for status, count in outbox_counts.items()
            if status not in OUTBOX_TERMINAL_STATUSES
        ),
        media_archive_excluded=media_archive_excluded,
        optional_tim_by_status=tim_counts,
        optional_tim_total=sum(tim_counts.values()),
        optional_tim_cancellable=sum(
            count
            for status, count in tim_counts.items()
            if status not in TIM_TERMINAL_STATUSES
        ),
        required_tim_excluded=required_tim_excluded,
    )


def archive_retired_work(
    db: Any,
    *,
    now: datetime | None = None,
    mode: CompatibilityMode | str | None = None,
) -> RetirementArchiveResult:
    resolved_mode = (
        CompatibilityMode(mode)
        if mode is not None
        else compatibility_mode()
    )
    if resolved_mode is not CompatibilityMode.RETIRED:
        raise RuntimeError(
            "BBW_COMPATIBILITY_MODE must be retired before archiving legacy work"
        )
    archived_at = now or utcnow()
    outbox_result = db.execute(
        update(OperationOutbox)
        .where(
            *_ordinary_outbox_filters(),
            OperationOutbox.status.notin_(tuple(OUTBOX_TERMINAL_STATUSES)),
        )
        .values(
            status="cancelled",
            completed_at=archived_at,
            locked_by=None,
            locked_until=None,
            last_error=COMPATIBILITY_ARCHIVED_REASON,
        )
    )
    tim_result = db.execute(
        update(MessageDelivery)
        .where(
            MessageDelivery.channel == "tim",
            MessageDelivery.required.is_(False),
            MessageDelivery.status.notin_(tuple(TIM_TERMINAL_STATUSES)),
        )
        .values(
            status="cancelled",
            locked_by=None,
            locked_until=None,
            last_error=TIM_MIRROR_ARCHIVED_REASON,
        )
    )
    db.flush()
    return RetirementArchiveResult(
        compatibility_outbox_cancelled=max(
            0, int(getattr(outbox_result, "rowcount", 0) or 0)
        ),
        optional_tim_cancelled=max(
            0, int(getattr(tim_result, "rowcount", 0) or 0)
        ),
        archived_at=archived_at.isoformat(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="预览或显式封存 Banghua/TIM 可选兼容待办"
    )
    parser.add_argument(
        "--confirm-retire",
        metavar="PHRASE",
        help=(
            "执行封存；必须精确传入 "
            f"{CONFIRMATION_PHRASE}，且 BBW_COMPATIBILITY_MODE=retired"
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    mode = compatibility_mode(settings)
    output = stdout
    confirmation = str(args.confirm_retire or "")
    if confirmation and confirmation != CONFIRMATION_PHRASE:
        raise SystemExit("confirmation phrase does not match")

    with session_scope() as db:
        before = inspect_retirement(db, mode=mode)
        if not confirmation:
            payload: dict[str, Any] = asdict(before)
        else:
            archived = archive_retired_work(db, mode=mode)
            payload = {
                "dry_run": False,
                "before": asdict(before),
                "archived": asdict(archived),
            }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
