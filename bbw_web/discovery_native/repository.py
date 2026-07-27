"""SQLAlchemy persistence for city-level discovery and local text matching.

The caller owns the transaction.  This repository never imports the legacy
protocol client and never performs network I/O.  A successful match consumes
both queue rows, writes the canonical ``MatchResult`` and grants both message
directions in that same transaction.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from bbw_prod.models import (
    ActivityEvent,
    ExternalAccount,
    MatchPreference as MatchPreferenceModel,
    MatchQueueEntry as MatchQueueEntryModel,
    MatchResult as MatchResultModel,
    Relationship,
    User,
    UserDiscoveryProfile,
)
from bbw_web.account_display import canonical_self_account_display
from bbw_web.legacy_media_reference import projected_profile_avatar

from .contracts import (
    DISCOVERY_PROVIDER,
    LEGACY_ACCOUNT_PROVIDER,
    MATCH_STATUS_ACTIVE,
    QUEUE_STATUS_MATCHED,
    QUEUE_STATUS_WAITING,
    TEXT_MATCH_KIND,
    DirectMessageAuthorizationIntent,
    DiscoveryAccount,
    DiscoveryCandidate,
    DiscoveryCard,
    DiscoveryContractViolation,
    DiscoveryPrincipal,
    DiscoveryProfile,
    LocalMatchResult,
    MatchCandidate,
    MatchFrequencyDecision,
    MatchPreference,
    MatchPreferenceConflict,
    MatchQueueEntry,
    MatchRequestConflict,
    PeerMessageGrant,
    TextMatchCommitPlan,
    TextMatchOutcome,
)


MESSAGE_POLICY_PROVIDER = "web-policy"
MESSAGE_POLICY_MATCH_KIND = "match"
SOCIAL_RELATIONSHIP_PROVIDERS = ("web-local", "beibeiwu")
SOCIAL_BLOCK_KINDS = ("blacklist", "blacklisted_by")
MATCH_FREQUENCY_EVENT_TYPE = "discovery.match.request"
MATCH_FREQUENCY_EVENT_PREFIX = "match-frequency:"
DISCOVERY_SCHEMA = 1


def normalized_city_code(city_name: object) -> str | None:
    """Return a stable opaque city code without geocoding or coordinates."""

    normalized = " ".join(
        unicodedata.normalize("NFKC", str(city_name or "")).strip().casefold().split()
    )
    if not normalized:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()
    return f"LOCAL-{digest[:24]}"


def _profile_value(profile: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        value = profile.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _city_values(profile: Mapping[str, object]) -> tuple[str | None, str | None]:
    raw_city = _profile_value(profile, "city", "region", "area")
    raw_name = _profile_value(profile, "city_name")
    if isinstance(raw_city, Mapping):
        raw_name = raw_name or _profile_value(raw_city, "city_name", "name", "label")
    else:
        raw_name = raw_name or raw_city
    city_name = " ".join(str(raw_name or "").strip().split()) or None
    if city_name is not None and len(city_name) > 80:
        city_name = city_name[:80].rstrip() or None
    if not city_name:
        return None, None
    # The Web receives only a city label from the legacy profile and custom
    # city UI.  Deriving the code from that same label avoids mixed official /
    # opaque codes causing two users in one city to miss each other.
    return normalized_city_code(city_name), city_name


def _profile_gender(profile: Mapping[str, object]) -> str:
    value = str(_profile_value(profile, "gender", "sex") or "unspecified").strip()
    aliases = {
        "男": "male",
        "male": "male",
        "m": "male",
        "1": "male",
        "女": "female",
        "female": "female",
        "f": "female",
        "2": "female",
        "其他": "other",
        "other": "other",
        "保密": "unspecified",
        "未设置": "unspecified",
        "unspecified": "unspecified",
    }
    return aliases.get(value.casefold(), "unspecified")


def _profile_property(profile: Mapping[str, object]) -> str | None:
    value = str(
        _profile_value(profile, "profile_property", "property", "attribute") or ""
    ).strip()
    if not value or value in {"不限", "any"}:
        return None
    if value.casefold() in {"z", "b"}:
        value = value.upper()
    if value not in {"双", "Z", "B"}:
        return None
    return value


def _profile_age(profile: Mapping[str, object]) -> int | None:
    value = _profile_value(profile, "age")
    if isinstance(value, bool):
        return None
    try:
        age = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return age if 18 <= age <= 120 else None


class SqlAlchemyDiscoveryStore:
    """PostgreSQL-backed implementation of ``DiscoveryNativeStore``."""

    def __init__(
        self,
        db: Session,
        *,
        account_provider: str = LEGACY_ACCOUNT_PROVIDER,
    ) -> None:
        self.db = db
        self.account_provider = str(account_provider or LEGACY_ACCOUNT_PROVIDER)

    @staticmethod
    def _account(user: User, account: ExternalAccount) -> DiscoveryAccount:
        return DiscoveryAccount(
            user_id=user.id,
            external_account_id=account.id,
            upstream_uid=str(account.upstream_uid or ""),
            display_name=str(user.display_name or ""),
            active=user.status == "active" and user.disabled_at is None,
            account_provider=str(account.provider or ""),
        )

    @staticmethod
    def _profile(row: UserDiscoveryProfile) -> DiscoveryProfile:
        return DiscoveryProfile(
            user_id=row.user_id,
            city_code=row.city_code,
            city_name=row.city_name,
            gender=row.gender,
            profile_property=row.profile_property,
            age=row.age,
            discoverable=bool(row.discoverable),
            last_seen_at=row.last_active_at,
        )

    @staticmethod
    def _preference(row: MatchPreferenceModel) -> MatchPreference:
        return MatchPreference(
            user_id=row.user_id,
            city_scope=row.city_scope,
            gender_preference=row.gender_preference,
            property_preference=row.property_preference,
            min_age=row.min_age,
            max_age=row.max_age,
            enabled=bool(row.enabled),
            version=row.version,
        )

    @staticmethod
    def _queue(row: MatchQueueEntryModel) -> MatchQueueEntry:
        return MatchQueueEntry(
            id=row.id,
            public_id=row.public_id,
            user_id=row.user_id,
            request_id=row.idempotency_key,
            queue_kind=row.queue_kind,
            status=row.status,
            city_code=row.city_code,
            preference_version=row.preference_version,
            enqueued_at=row.enqueued_at,
            expires_at=row.expires_at,
            matched_at=row.matched_at,
        )

    @staticmethod
    def _result(row: MatchResultModel) -> LocalMatchResult:
        return LocalMatchResult(
            id=row.id,
            public_id=row.public_id,
            match_key=row.match_key,
            user_low_id=row.user_low_id,
            user_high_id=row.user_high_id,
            user_low_queue_entry_id=row.user_low_queue_entry_id,
            user_high_queue_entry_id=row.user_high_queue_entry_id,
            initiated_by_user_id=row.initiated_by_user_id,
            status=row.status,
            matched_at=row.matched_at,
        )

    @staticmethod
    def _card(account: DiscoveryAccount, profile: DiscoveryProfile) -> DiscoveryCard:
        if not profile.city_code or not profile.city_name:
            raise DiscoveryContractViolation("匹配用户缺少城市级资料")
        return DiscoveryCard(
            user_id=account.user_id,
            upstream_uid=account.upstream_uid,
            display_name=account.display_name,
            city_code=profile.city_code,
            city_name=profile.city_name,
            gender=profile.gender,
            profile_property=profile.profile_property,
            age=profile.age,
            online=account.active,
        )

    def resolve_principal(
        self, principal: DiscoveryPrincipal
    ) -> DiscoveryAccount | None:
        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id == principal.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.id == principal.external_account_id,
                ExternalAccount.provider == principal.account_provider,
                ExternalAccount.upstream_uid == principal.upstream_uid,
            )
        ).one_or_none()
        return self._account(row[0], row[1]) if row is not None else None

    def canonical_profile_values(
        self, principal: DiscoveryPrincipal
    ) -> dict[str, object] | None:
        """Extract only city/gender/property/age from canonical ``User.profile``."""

        user = self.db.scalar(
            select(User).where(
                User.id == principal.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
            )
        )
        if user is None:
            return None
        profile = dict(user.profile or {})
        city_code, city_name = _city_values(profile)
        explicit_discoverable = profile.get("discoverable")
        if not isinstance(explicit_discoverable, bool):
            explicit_discoverable = profile.get("discovery_enabled")
        raw_privacy = profile.get("privacy")
        privacy = dict(raw_privacy) if isinstance(raw_privacy, Mapping) else {}
        privacy_allows_discovery = not any(
            privacy.get(field) is False
            for field in ("show_city", "show_location", "show_online_status")
        )
        discoverable = (
            bool(explicit_discoverable)
            if isinstance(explicit_discoverable, bool)
            else bool(city_code and city_name)
        )
        return {
            "city_code": city_code,
            "city_name": city_name,
            "gender": _profile_gender(profile),
            "profile_property": _profile_property(profile),
            "age": _profile_age(profile),
            "discoverable": (
                discoverable
                and privacy_allows_discovery
                and bool(city_code and city_name)
            ),
        }

    def canonical_user_display(
        self, principal: DiscoveryPrincipal
    ) -> dict[str, object] | None:
        """Return the authenticated user's small account view for ``app.js``."""

        row = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id == principal.user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.id == principal.external_account_id,
                ExternalAccount.provider == principal.account_provider,
                ExternalAccount.upstream_uid == principal.upstream_uid,
            )
        ).one_or_none()
        if row is None:
            return None
        user, account = row
        profile = dict(user.profile or {})
        account_data = dict(account.device_data or {})
        avatar = projected_profile_avatar(profile) or str(
            account_data.get("portrait") or ""
        )
        return {
            "avatar": avatar,
            "portrait": avatar,
            "signature": str(
                profile.get("signature") or account_data.get("user_sign") or ""
            )[:500],
            **canonical_self_account_display(profile, account_data),
        }

    def get_discovery_profile(self, user_id: uuid.UUID) -> DiscoveryProfile | None:
        row = self.db.scalar(
            select(UserDiscoveryProfile).where(UserDiscoveryProfile.user_id == user_id)
        )
        return self._profile(row) if row is not None else None

    def save_discovery_profile(self, profile: DiscoveryProfile) -> DiscoveryProfile:
        row = self.db.scalar(
            select(UserDiscoveryProfile)
            .where(UserDiscoveryProfile.user_id == profile.user_id)
            .with_for_update()
        )
        if row is None:
            row = UserDiscoveryProfile(id=uuid.uuid4(), user_id=profile.user_id)
            self.db.add(row)
        row.city_code = profile.city_code
        row.city_name = profile.city_name
        row.gender = profile.gender
        row.profile_property = profile.profile_property
        row.age = profile.age
        row.discoverable = profile.discoverable
        row.last_active_at = profile.last_seen_at
        self.db.flush()
        return self._profile(row)

    def touch_discovery_last_seen(
        self, *, user_id: uuid.UUID, seen_at: datetime
    ) -> DiscoveryProfile:
        row = self.db.scalar(
            select(UserDiscoveryProfile)
            .where(UserDiscoveryProfile.user_id == user_id)
            .with_for_update()
        )
        if row is None:
            raise DiscoveryContractViolation("发现资料不存在")
        row.last_active_at = seen_at
        return self._profile(row)

    def list_discovery_candidates(self, *, limit: int) -> list[DiscoveryCandidate]:
        rows = self.db.execute(
            select(User, ExternalAccount, UserDiscoveryProfile)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserDiscoveryProfile, UserDiscoveryProfile.user_id == User.id)
            .where(
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == self.account_provider,
                ExternalAccount.upstream_uid.is_not(None),
                UserDiscoveryProfile.discoverable.is_(True),
            )
            .order_by(
                UserDiscoveryProfile.last_active_at.desc().nullslast(),
                UserDiscoveryProfile.user_id,
            )
            .limit(limit)
        ).all()
        return [
            DiscoveryCandidate(self._account(user, account), self._profile(profile))
            for user, account, profile in rows
        ]

    def _account_uids(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        rows = self.db.execute(
            select(ExternalAccount.user_id, ExternalAccount.upstream_uid).where(
                ExternalAccount.user_id.in_(unique_ids),
                ExternalAccount.provider == self.account_provider,
                ExternalAccount.upstream_uid.is_not(None),
            )
        ).all()
        return {user_id: str(upstream_uid) for user_id, upstream_uid in rows}

    def is_blocked_between(
        self, left_user_id: uuid.UUID, right_user_id: uuid.UUID
    ) -> bool:
        if left_user_id == right_user_id:
            return True
        uids = self._account_uids((left_user_id, right_user_id))
        left_uid = uids.get(left_user_id)
        right_uid = uids.get(right_user_id)
        if not left_uid or not right_uid:
            return True
        blocked = self.db.scalar(
            select(Relationship.id)
            .where(
                Relationship.provider.in_(SOCIAL_RELATIONSHIP_PROVIDERS),
                Relationship.kind.in_(SOCIAL_BLOCK_KINDS),
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
                or_(
                    and_(
                        Relationship.owner_user_id == left_user_id,
                        Relationship.subject_upstream_uid == right_uid,
                    ),
                    and_(
                        Relationship.owner_user_id == right_user_id,
                        Relationship.subject_upstream_uid == left_uid,
                    ),
                ),
            )
            .limit(1)
        )
        return blocked is not None

    def get_match_preference(self, user_id: uuid.UUID) -> MatchPreference | None:
        row = self.db.scalar(
            select(MatchPreferenceModel).where(MatchPreferenceModel.user_id == user_id)
        )
        return self._preference(row) if row is not None else None

    def save_match_preference(
        self,
        preference: MatchPreference,
        *,
        expected_version: int | None,
    ) -> MatchPreference:
        row = self.db.scalar(
            select(MatchPreferenceModel)
            .where(MatchPreferenceModel.user_id == preference.user_id)
            .with_for_update()
        )
        current_version = row.version if row is not None else 0
        if expected_version is not None and expected_version != current_version:
            raise MatchPreferenceConflict("匹配偏好版本已变化")
        if preference.version != current_version + 1:
            raise MatchPreferenceConflict("匹配偏好版本已变化")
        if row is None:
            row = MatchPreferenceModel(id=uuid.uuid4(), user_id=preference.user_id)
            self.db.add(row)
        row.city_scope = preference.city_scope
        row.gender_preference = preference.gender_preference
        row.property_preference = preference.property_preference
        row.min_age = preference.min_age
        row.max_age = preference.max_age
        row.enabled = preference.enabled
        row.version = preference.version
        self.db.flush()
        return self._preference(row)

    @staticmethod
    def _advisory_lock_key(user_id: uuid.UUID) -> int:
        return int.from_bytes(user_id.bytes[:8], byteorder="big", signed=True)

    def _lock_frequency_owner(self, user_id: uuid.UUID) -> None:
        self.db.execute(
            select(func.pg_advisory_xact_lock(self._advisory_lock_key(user_id)))
        ).scalar_one()

    def _frequency_event_id(self, request_id: str) -> str:
        return f"{MATCH_FREQUENCY_EVENT_PREFIX}{request_id}"

    def consume_match_frequency(
        self,
        *,
        user_id: uuid.UUID,
        request_id: str,
        occurred_at: datetime,
        limit: int,
        window_seconds: int,
    ) -> MatchFrequencyDecision:
        self._lock_frequency_owner(user_id)
        event_id = self._frequency_event_id(request_id)
        existing = self.db.scalar(
            select(ActivityEvent.id).where(
                ActivityEvent.owner_user_id == user_id,
                ActivityEvent.provider == DISCOVERY_PROVIDER,
                ActivityEvent.upstream_event_id == event_id,
            )
        )
        if existing is not None:
            return MatchFrequencyDecision(allowed=True)

        window_start = occurred_at - timedelta(seconds=window_seconds)
        used, oldest = self.db.execute(
            select(func.count(ActivityEvent.id), func.min(ActivityEvent.occurred_at)).where(
                ActivityEvent.owner_user_id == user_id,
                ActivityEvent.provider == DISCOVERY_PROVIDER,
                ActivityEvent.event_type == MATCH_FREQUENCY_EVENT_TYPE,
                ActivityEvent.occurred_at > window_start,
                ActivityEvent.occurred_at <= occurred_at,
            )
        ).one()
        if int(used or 0) >= limit:
            retry_after = 1
            if oldest is not None:
                retry_after = max(
                    1,
                    math.ceil(
                        (oldest + timedelta(seconds=window_seconds) - occurred_at)
                        .total_seconds()
                    ),
                )
            return MatchFrequencyDecision(
                allowed=False,
                retry_after_seconds=retry_after,
            )

        self.db.add(
            ActivityEvent(
                id=uuid.uuid4(),
                owner_user_id=user_id,
                provider=DISCOVERY_PROVIDER,
                upstream_event_id=event_id,
                event_type=MATCH_FREQUENCY_EVENT_TYPE,
                occurred_at=occurred_at,
                details={
                    "authority": DISCOVERY_PROVIDER,
                    "request_id": request_id,
                    "schema": DISCOVERY_SCHEMA,
                    "window_seconds": window_seconds,
                },
            )
        )
        return MatchFrequencyDecision(allowed=True)

    def match_frequency_status(
        self,
        *,
        user_id: uuid.UUID,
        at: datetime,
        limit: int,
        window_seconds: int,
    ) -> dict[str, int]:
        window_start = at - timedelta(seconds=window_seconds)
        used, oldest = self.db.execute(
            select(func.count(ActivityEvent.id), func.min(ActivityEvent.occurred_at)).where(
                ActivityEvent.owner_user_id == user_id,
                ActivityEvent.provider == DISCOVERY_PROVIDER,
                ActivityEvent.event_type == MATCH_FREQUENCY_EVENT_TYPE,
                ActivityEvent.occurred_at > window_start,
                ActivityEvent.occurred_at <= at,
            )
        ).one()
        used_count = min(limit, max(0, int(used or 0)))
        retry_after = 0
        if used_count >= limit and oldest is not None:
            retry_after = max(
                1,
                math.ceil(
                    (oldest + timedelta(seconds=window_seconds) - at).total_seconds()
                ),
            )
        return {
            "limit": limit,
            "used": used_count,
            "remaining": max(0, limit - used_count),
            "window_seconds": window_seconds,
            "retry_after_seconds": retry_after,
        }

    def enqueue_text_match(
        self,
        *,
        account: DiscoveryAccount,
        profile: DiscoveryProfile,
        preference: MatchPreference,
        request_id: str,
        enqueued_at: datetime,
        expires_at: datetime,
    ) -> MatchQueueEntry:
        existing = self.db.scalar(
            select(MatchQueueEntryModel)
            .where(
                MatchQueueEntryModel.user_id == account.user_id,
                MatchQueueEntryModel.idempotency_key == request_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.city_code != profile.city_code
                or existing.preference_version != preference.version
            ):
                raise MatchRequestConflict("request_id 已绑定其他匹配参数")
            return self._queue(existing)

        waiting = self.db.scalar(
            select(MatchQueueEntryModel)
            .where(
                MatchQueueEntryModel.user_id == account.user_id,
                MatchQueueEntryModel.status == QUEUE_STATUS_WAITING,
            )
            .with_for_update()
        )
        if waiting is not None:
            if waiting.expires_at <= enqueued_at:
                waiting.status = "expired"
            else:
                waiting.status = "cancelled"
            waiting.cancelled_at = enqueued_at
            # Release the partial unique ``one waiting row per user`` before
            # inserting the replacement; do not rely on ORM flush ordering.
            self.db.flush()

        row = MatchQueueEntryModel(
            id=uuid.uuid4(),
            public_id=f"mqe_{uuid.uuid4().hex}",
            user_id=account.user_id,
            idempotency_key=request_id,
            queue_kind=TEXT_MATCH_KIND,
            status=QUEUE_STATUS_WAITING,
            city_code=profile.city_code,
            preference_version=preference.version,
            enqueued_at=enqueued_at,
            expires_at=expires_at,
        )
        self.db.add(row)
        self.db.flush()
        return self._queue(row)

    def list_waiting_text_candidates(
        self,
        *,
        exclude_user_id: uuid.UUID,
        at: datetime,
        limit: int,
    ) -> list[MatchCandidate]:
        rows = self.db.execute(
            select(
                User,
                ExternalAccount,
                UserDiscoveryProfile,
                MatchPreferenceModel,
                MatchQueueEntryModel,
            )
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserDiscoveryProfile, UserDiscoveryProfile.user_id == User.id)
            .join(MatchPreferenceModel, MatchPreferenceModel.user_id == User.id)
            .join(MatchQueueEntryModel, MatchQueueEntryModel.user_id == User.id)
            .where(
                User.id != exclude_user_id,
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == self.account_provider,
                ExternalAccount.upstream_uid.is_not(None),
                UserDiscoveryProfile.discoverable.is_(True),
                MatchPreferenceModel.enabled.is_(True),
                MatchQueueEntryModel.queue_kind == TEXT_MATCH_KIND,
                MatchQueueEntryModel.status == QUEUE_STATUS_WAITING,
                MatchQueueEntryModel.expires_at > at,
                MatchQueueEntryModel.preference_version
                == MatchPreferenceModel.version,
                MatchQueueEntryModel.city_code == UserDiscoveryProfile.city_code,
            )
            .order_by(MatchQueueEntryModel.enqueued_at, MatchQueueEntryModel.id)
            .limit(limit)
            .with_for_update(of=MatchQueueEntryModel, skip_locked=True)
        ).all()
        return [
            MatchCandidate(
                account=self._account(user, account),
                profile=self._profile(profile),
                preference=self._preference(preference),
                queue_entry=self._queue(queue),
            )
            for user, account, profile, preference, queue in rows
        ]

    def _active_accounts_for_update(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, DiscoveryAccount]:
        unique_ids = tuple(dict.fromkeys(user_ids))
        rows = self.db.execute(
            select(User, ExternalAccount)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .where(
                User.id.in_(unique_ids),
                User.status == "active",
                User.disabled_at.is_(None),
                ExternalAccount.provider == self.account_provider,
                ExternalAccount.upstream_uid.is_not(None),
            )
            .order_by(User.id)
            .with_for_update(of=User)
        ).all()
        return {user.id: self._account(user, account) for user, account in rows}

    def _message_grant(
        self,
        *,
        owner: DiscoveryAccount,
        peer: DiscoveryAccount,
        result: MatchResultModel,
        plan: TextMatchCommitPlan,
    ) -> None:
        row = self.db.scalar(
            select(Relationship)
            .where(
                Relationship.owner_user_id == owner.user_id,
                Relationship.provider == MESSAGE_POLICY_PROVIDER,
                Relationship.subject_upstream_uid == peer.upstream_uid,
                Relationship.kind == MESSAGE_POLICY_MATCH_KIND,
            )
            .with_for_update()
        )
        metadata = dict(row.extra_data or {}) if row is not None else {}
        metadata.update(
            {
                "authorization_intent_id": str(plan.authorization_intent_id),
                "authority": DISCOVERY_PROVIDER,
                "grant_reason": "text_match",
                "match_key": result.match_key,
                "match_result_id": str(result.id),
                "peer_user_id": str(peer.user_id),
                "schema": DISCOVERY_SCHEMA,
                "server_owned": True,
            }
        )
        if row is None:
            row = Relationship(
                id=uuid.uuid4(),
                owner_user_id=owner.user_id,
                provider=MESSAGE_POLICY_PROVIDER,
                subject_upstream_uid=peer.upstream_uid,
                kind=MESSAGE_POLICY_MATCH_KIND,
                status="active",
                started_at=plan.matched_at,
                ended_at=None,
                extra_data=metadata,
            )
            self.db.add(row)
            return
        row.status = "active"
        row.ended_at = None
        row.extra_data = metadata

    def _profiles_for_users(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, DiscoveryProfile]:
        rows = self.db.scalars(
            select(UserDiscoveryProfile).where(
                UserDiscoveryProfile.user_id.in_(tuple(dict.fromkeys(user_ids)))
            )
        )
        return {row.user_id: self._profile(row) for row in rows}

    def commit_text_match(
        self,
        *,
        requester: MatchCandidate,
        candidate: MatchCandidate,
        plan: TextMatchCommitPlan,
    ) -> TextMatchOutcome:
        accounts = self._active_accounts_for_update(
            (requester.account.user_id, candidate.account.user_id)
        )
        if set(accounts) != {requester.account.user_id, candidate.account.user_id}:
            raise MatchRequestConflict("匹配用户已不可用")
        if self.is_blocked_between(
            requester.account.user_id, candidate.account.user_id
        ):
            raise MatchRequestConflict("任一方黑名单关系禁止匹配")

        queue_rows = list(
            self.db.scalars(
                select(MatchQueueEntryModel)
                .where(
                    MatchQueueEntryModel.id.in_(
                        (requester.queue_entry.id, candidate.queue_entry.id)
                    )
                )
                .order_by(MatchQueueEntryModel.id)
                .with_for_update()
            )
        )
        by_id = {row.id: row for row in queue_rows}
        requester_row = by_id.get(requester.queue_entry.id)
        candidate_row = by_id.get(candidate.queue_entry.id)
        if requester_row is None or candidate_row is None:
            raise MatchRequestConflict("匹配队列已变化")

        result = self.db.scalar(
            select(MatchResultModel)
            .where(MatchResultModel.match_key == plan.match_key)
            .with_for_update()
        )
        created = result is None
        if result is None:
            for row, expected_user_id in (
                (requester_row, requester.account.user_id),
                (candidate_row, candidate.account.user_id),
            ):
                if (
                    row.user_id != expected_user_id
                    or row.status != QUEUE_STATUS_WAITING
                    or row.expires_at <= plan.matched_at
                ):
                    raise MatchRequestConflict("匹配队列已被消费或过期")
            result = MatchResultModel(
                id=uuid.uuid4(),
                public_id=f"mch_{uuid.uuid4().hex}",
                match_key=plan.match_key,
                user_low_id=plan.user_low_id,
                user_high_id=plan.user_high_id,
                user_low_queue_entry_id=plan.user_low_queue_entry_id,
                user_high_queue_entry_id=plan.user_high_queue_entry_id,
                initiated_by_user_id=plan.initiated_by_user_id,
                status=MATCH_STATUS_ACTIVE,
                matched_at=plan.matched_at,
            )
            self.db.add(result)
            for row in (requester_row, candidate_row):
                row.status = QUEUE_STATUS_MATCHED
                row.matched_at = plan.matched_at
        else:
            expected = (
                plan.user_low_id,
                plan.user_high_id,
                plan.user_low_queue_entry_id,
                plan.user_high_queue_entry_id,
            )
            actual = (
                result.user_low_id,
                result.user_high_id,
                result.user_low_queue_entry_id,
                result.user_high_queue_entry_id,
            )
            if actual != expected:
                raise MatchRequestConflict("匹配结果幂等键冲突")

        requester_account = accounts[requester.account.user_id]
        candidate_account = accounts[candidate.account.user_id]
        self._message_grant(
            owner=requester_account,
            peer=candidate_account,
            result=result,
            plan=plan,
        )
        self._message_grant(
            owner=candidate_account,
            peer=requester_account,
            result=result,
            plan=plan,
        )
        profiles = self._profiles_for_users(accounts)
        candidate_profile = profiles.get(candidate_account.user_id)
        if candidate_profile is None:
            raise MatchRequestConflict("匹配用户资料已不可用")
        authorization = DirectMessageAuthorizationIntent(
            id=plan.authorization_intent_id,
            match_result_id=result.id,
            grants=(
                PeerMessageGrant(
                    requester_account.user_id,
                    candidate_account.user_id,
                    candidate_account.upstream_uid,
                ),
                PeerMessageGrant(
                    candidate_account.user_id,
                    requester_account.user_id,
                    requester_account.upstream_uid,
                ),
            ),
            reason="text-match",
            granted_at=plan.matched_at,
        )
        return TextMatchOutcome(
            status=QUEUE_STATUS_MATCHED,
            request_id=requester_row.idempotency_key,
            queue_entry=self._queue(requester_row),
            created=created,
            match_result=self._result(result),
            peer=self._card(candidate_account, candidate_profile),
            message_authorization=authorization,
        )

    def _account_profile(
        self, user_id: uuid.UUID
    ) -> tuple[DiscoveryAccount, DiscoveryProfile] | None:
        row = self.db.execute(
            select(User, ExternalAccount, UserDiscoveryProfile)
            .join(ExternalAccount, ExternalAccount.user_id == User.id)
            .join(UserDiscoveryProfile, UserDiscoveryProfile.user_id == User.id)
            .where(
                User.id == user_id,
                ExternalAccount.provider == self.account_provider,
                ExternalAccount.upstream_uid.is_not(None),
            )
        ).one_or_none()
        if row is None:
            return None
        return self._account(row[0], row[1]), self._profile(row[2])

    def _authorization_for_result(
        self,
        *,
        result: MatchResultModel,
        left: DiscoveryAccount,
        right: DiscoveryAccount,
    ) -> DirectMessageAuthorizationIntent:
        row = self.db.scalar(
            select(Relationship).where(
                Relationship.owner_user_id == left.user_id,
                Relationship.provider == MESSAGE_POLICY_PROVIDER,
                Relationship.subject_upstream_uid == right.upstream_uid,
                Relationship.kind == MESSAGE_POLICY_MATCH_KIND,
                Relationship.status == "active",
                Relationship.ended_at.is_(None),
            )
        )
        metadata = dict(row.extra_data or {}) if row is not None else {}
        try:
            intent_id = uuid.UUID(str(metadata.get("authorization_intent_id") or ""))
        except ValueError:
            intent_id = uuid.uuid5(uuid.NAMESPACE_URL, f"web-match:{result.id}")
        return DirectMessageAuthorizationIntent(
            id=intent_id,
            match_result_id=result.id,
            grants=(
                PeerMessageGrant(left.user_id, right.user_id, right.upstream_uid),
                PeerMessageGrant(right.user_id, left.user_id, left.upstream_uid),
            ),
            reason="text-match",
            granted_at=result.matched_at,
        )

    def _outcome_from_queue(
        self, queue: MatchQueueEntryModel
    ) -> TextMatchOutcome:
        if queue.status != QUEUE_STATUS_MATCHED:
            return TextMatchOutcome(
                status=queue.status,
                request_id=queue.idempotency_key,
                queue_entry=self._queue(queue),
                created=False,
            )
        result = self.db.scalar(
            select(MatchResultModel).where(
                or_(
                    MatchResultModel.user_low_queue_entry_id == queue.id,
                    MatchResultModel.user_high_queue_entry_id == queue.id,
                )
            )
        )
        if result is None:
            raise DiscoveryContractViolation("已匹配队列缺少 MatchResult")
        peer_user_id = (
            result.user_high_id
            if queue.user_id == result.user_low_id
            else result.user_low_id
        )
        owner = self._account_profile(queue.user_id)
        peer = self._account_profile(peer_user_id)
        if owner is None or peer is None:
            raise DiscoveryContractViolation("匹配结果账号或资料不可用")
        owner_account, _owner_profile = owner
        peer_account, peer_profile = peer
        return TextMatchOutcome(
            status=QUEUE_STATUS_MATCHED,
            request_id=queue.idempotency_key,
            queue_entry=self._queue(queue),
            created=False,
            match_result=self._result(result),
            peer=self._card(peer_account, peer_profile),
            message_authorization=self._authorization_for_result(
                result=result,
                left=owner_account,
                right=peer_account,
            ),
        )

    def get_text_match_outcome(
        self, *, user_id: uuid.UUID, request_id: str
    ) -> TextMatchOutcome | None:
        queue = self.db.scalar(
            select(MatchQueueEntryModel).where(
                MatchQueueEntryModel.user_id == user_id,
                MatchQueueEntryModel.idempotency_key == request_id,
            )
        )
        return self._outcome_from_queue(queue) if queue is not None else None

    def current_waiting_outcome(
        self, *, user_id: uuid.UUID, at: datetime
    ) -> TextMatchOutcome | None:
        queue = self.db.scalar(
            select(MatchQueueEntryModel)
            .where(
                MatchQueueEntryModel.user_id == user_id,
                MatchQueueEntryModel.status == QUEUE_STATUS_WAITING,
                MatchQueueEntryModel.expires_at > at,
            )
            .order_by(MatchQueueEntryModel.enqueued_at.desc())
        )
        return self._outcome_from_queue(queue) if queue is not None else None

    def latest_match_outcome(
        self, *, user_id: uuid.UUID
    ) -> TextMatchOutcome | None:
        queue = self.db.scalar(
            select(MatchQueueEntryModel)
            .where(
                MatchQueueEntryModel.user_id == user_id,
                MatchQueueEntryModel.status == QUEUE_STATUS_MATCHED,
            )
            .order_by(
                MatchQueueEntryModel.matched_at.desc().nullslast(),
                MatchQueueEntryModel.id.desc(),
            )
            .limit(1)
        )
        return self._outcome_from_queue(queue) if queue is not None else None
