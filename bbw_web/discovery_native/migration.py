"""Build auditable discovery cutover markers from canonical local state.

Discovery has no legacy queue or preference history to download.  Its profile
projection is derived only after the strict social migration marker proves the
upstream profile scan completed; preferences and text matches are Web-native
tables and are scanned in full.  This command therefore performs no network
I/O and never treats table existence alone as migration evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence, TextIO

from sqlalchemy import or_, select

from bbw_prod.db import session_scope
from bbw_prod.migration_readiness import (
    DOMAIN_MARKER_SCHEMA,
    DOMAIN_MARKER_SCOPES,
    DOMAIN_MARKER_STREAMS,
    _valid_domain_marker,
)
from bbw_prod.models import (
    ExternalAccount,
    MatchPreference as MatchPreferenceRow,
    MatchResult,
    SyncCursor,
    User,
    UserDiscoveryProfile,
    utcnow,
)
from bbw_prod.repositories import SyncCursorRepository

from .contracts import DiscoveryPrincipal
from .repository import SqlAlchemyDiscoveryStore
from .service import DiscoveryNativeService


LEGACY_PROVIDER = "beibeiwu"
DISCOVERY_STREAM = DOMAIN_MARKER_STREAMS["discovery"]
SOCIAL_STREAM = DOMAIN_MARKER_STREAMS["social"]
DISCOVERY_SOURCE_PATHS = (
    "/api/profile/user",
    "postgresql://match_preferences",
    "postgresql://match_results",
)


class DiscoveryMigrationError(RuntimeError):
    code = "discovery_migration_error"


class DiscoveryMigrationPrerequisiteError(DiscoveryMigrationError):
    code = "discovery_social_marker_not_ready"


class DiscoveryMigrationDataError(DiscoveryMigrationError):
    code = "discovery_migration_data_invalid"


@dataclass(frozen=True, slots=True)
class DiscoveryMigrationSummary:
    profile_records: int
    preference_records: int
    text_match_records: int
    record_digest: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _social_coverage(cursor: SyncCursor) -> datetime:
    try:
        marker = json.loads(str(cursor.cursor or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DiscoveryMigrationPrerequisiteError(
            "social migration marker is invalid"
        ) from exc
    if not isinstance(marker, Mapping):
        raise DiscoveryMigrationPrerequisiteError(
            "social migration marker is invalid"
        )
    raw = str(marker.get("coverage_started_at") or "").strip()
    try:
        started_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryMigrationPrerequisiteError(
            "social migration marker coverage is invalid"
        ) from exc
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise DiscoveryMigrationPrerequisiteError(
            "social migration marker coverage is invalid"
        )
    return started_at.astimezone(UTC)


def _record_digest(
    profile: UserDiscoveryProfile,
    preference: MatchPreferenceRow | None,
    matches: Sequence[MatchResult],
) -> str:
    identities = [
        "profile:"
        + json.dumps(
            {
                "age": profile.age,
                "city_code": profile.city_code,
                "city_name": profile.city_name,
                "discoverable": bool(profile.discoverable),
                "gender": profile.gender,
                "profile_property": profile.profile_property,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    ]
    if preference is not None:
        identities.append(
            "preference:"
            + json.dumps(
                {
                    "city_scope": preference.city_scope,
                    "enabled": bool(preference.enabled),
                    "gender_preference": preference.gender_preference,
                    "max_age": int(preference.max_age),
                    "min_age": int(preference.min_age),
                    "property_preference": preference.property_preference,
                    "version": int(preference.version),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    identities.extend(
        f"match:{row.public_id}:{row.match_key}:{row.status}:"
        f"{_as_utc(row.matched_at).isoformat()}"
        for row in matches
    )
    return hashlib.sha256(
        json.dumps(
            identities,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def initialize_discovery_migration(
    owner_user_id: uuid.UUID | str,
    *,
    db_scope=session_scope,
    clock=utcnow,
) -> DiscoveryMigrationSummary:
    """Materialize one profile and write a marker only after full local scans."""

    try:
        owner_id = (
            owner_user_id
            if isinstance(owner_user_id, uuid.UUID)
            else uuid.UUID(str(owner_user_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise DiscoveryMigrationDataError("owner_user_id_invalid") from exc

    completed_at = _as_utc(clock())
    with db_scope() as db:
        binding = db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id == owner_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == LEGACY_PROVIDER,
            )
            .with_for_update()
        ).one_or_none()
        if binding is None:
            raise DiscoveryMigrationDataError("active_account_binding_missing")
        user, account = binding
        upstream_uid = str(account.upstream_uid or "").strip()
        if not upstream_uid:
            raise DiscoveryMigrationDataError("upstream_uid_missing")

        social_cursor = db.scalar(
            select(SyncCursor).where(
                SyncCursor.owner_user_id == owner_id,
                SyncCursor.source == LEGACY_PROVIDER,
                SyncCursor.stream == SOCIAL_STREAM,
            )
        )
        if not _valid_domain_marker(
            social_cursor,
            domain="social",
            external_account_id=account.id,
            upstream_uid=upstream_uid,
        ):
            raise DiscoveryMigrationPrerequisiteError(
                "strict social migration must complete first"
            )
        social_coverage_started_at = _social_coverage(social_cursor)

        principal = DiscoveryPrincipal(
            user_id=user.id,
            external_account_id=account.id,
            upstream_uid=upstream_uid,
            account_provider=LEGACY_PROVIDER,
        )
        store = SqlAlchemyDiscoveryStore(db)
        values = store.canonical_profile_values(principal)
        if values is None:
            raise DiscoveryMigrationDataError("canonical_profile_missing")
        DiscoveryNativeService(store, clock=lambda: completed_at).update_profile(
            principal=principal,
            **values,
        )
        profile = db.scalar(
            select(UserDiscoveryProfile).where(
                UserDiscoveryProfile.user_id == owner_id
            )
        )
        if profile is None:
            raise DiscoveryMigrationDataError("discovery_profile_not_materialized")
        preference = db.scalar(
            select(MatchPreferenceRow).where(
                MatchPreferenceRow.user_id == owner_id
            )
        )
        matches = list(
            db.scalars(
                select(MatchResult)
                .where(
                    or_(
                        MatchResult.user_low_id == owner_id,
                        MatchResult.user_high_id == owner_id,
                    ),
                    MatchResult.matched_at <= completed_at,
                )
                .order_by(MatchResult.matched_at, MatchResult.id)
            )
        )
        coverage_candidates = [
            social_coverage_started_at,
            _as_utc(user.created_at),
            *(
                [_as_utc(preference.created_at)]
                if preference is not None
                else []
            ),
            *(_as_utc(row.matched_at) for row in matches),
        ]
        coverage_started_at = min(coverage_candidates)
        if coverage_started_at > completed_at:
            raise DiscoveryMigrationDataError("discovery_coverage_invalid")
        digest = _record_digest(profile, preference, matches)
        counts = {
            "profile": 1,
            "preferences": int(preference is not None),
            "text-match": len(matches),
        }
        marker = {
            "complete": True,
            "source_complete": True,
            "schema": DOMAIN_MARKER_SCHEMA,
            "domain": "discovery",
            "external_account_id": str(account.id),
            "upstream_uid": upstream_uid,
            "scopes": list(DOMAIN_MARKER_SCOPES["discovery"]),
            "counts": counts,
            "source_paths": list(DISCOVERY_SOURCE_PATHS),
            "record_digest": digest,
            "coverage_started_at": coverage_started_at.isoformat(),
            "coverage_ended_at": completed_at.isoformat(),
            "unresolved_records": 0,
            "profile_source_marker": SOCIAL_STREAM,
        }
        SyncCursorRepository(db).upsert(
            owner_user_id=owner_id,
            source=LEGACY_PROVIDER,
            stream=DISCOVERY_STREAM,
            cursor=json.dumps(
                marker,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            watermark_at=completed_at,
            next_sync_at=None,
            last_attempted_at=completed_at,
            last_succeeded_at=completed_at,
            last_error=None,
        )
        return DiscoveryMigrationSummary(
            profile_records=1,
            preference_records=int(preference is not None),
            text_match_records=len(matches),
            record_digest=digest,
        )


def _active_owner_ids(*, db_scope=session_scope) -> list[uuid.UUID]:
    with db_scope() as db:
        return list(
            db.scalars(
                select(User.id)
                .join(ExternalAccount, ExternalAccount.user_id == User.id)
                .where(
                    User.status == "active",
                    User.disabled_at.is_(None),
                    ExternalAccount.provider == LEGACY_PROVIDER,
                )
                .order_by(User.id)
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从严格社交迁移结果初始化 Web-native 发现领域并写完成 Marker"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--owner-user-id",
        help="经审核的内部 User UUID；不会出现在命令输出中",
    )
    target.add_argument(
        "--all-active",
        action="store_true",
        help="处理全部绑定 beibeiwu 的 active 账号",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    owners = (
        _active_owner_ids()
        if args.all_active
        else [str(args.owner_user_id or "")]
    )
    completed = 0
    failed = 0
    totals = {
        "profile_records": 0,
        "preference_records": 0,
        "text_match_records": 0,
    }
    failure_codes: dict[str, int] = {}
    for owner_id in owners:
        try:
            summary = initialize_discovery_migration(owner_id)
        except DiscoveryMigrationError as exc:
            failed += 1
            failure_codes[exc.code] = failure_codes.get(exc.code, 0) + 1
            continue
        except Exception:
            failed += 1
            code = "discovery_migration_internal_error"
            failure_codes[code] = failure_codes.get(code, 0) + 1
            continue
        completed += 1
        for key in totals:
            totals[key] += int(getattr(summary, key))
    print(
        json.dumps(
            {
                "ok": failed == 0,
                "accounts_total": len(owners),
                "accounts_completed": completed,
                "accounts_failed": failed,
                "failure_codes": failure_codes,
                **totals,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=stdout or sys.stdout,
    )
    return 0 if failed == 0 else 1


__all__ = [
    "DISCOVERY_SOURCE_PATHS",
    "DiscoveryMigrationDataError",
    "DiscoveryMigrationError",
    "DiscoveryMigrationPrerequisiteError",
    "DiscoveryMigrationSummary",
    "initialize_discovery_migration",
]


if __name__ == "__main__":
    raise SystemExit(main())
