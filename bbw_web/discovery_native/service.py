"""城市级发现与 Web-native 文本匹配纯业务服务。"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Callable

from .contracts import (
    FILTER_GENDERS,
    MATCH_CITY_SCOPES,
    MATCH_STATUS_ACTIVE,
    PROFILE_GENDERS,
    QUEUE_STATUS_MATCHED,
    QUEUE_STATUS_WAITING,
    TEXT_MATCH_KIND,
    DirectMessageAuthorizationIntent,
    DiscoveryAccount,
    DiscoveryCandidate,
    DiscoveryCard,
    DiscoveryContractViolation,
    DiscoveryFilters,
    DiscoveryIdentityUnavailable,
    DiscoveryList,
    DiscoveryLocationRequired,
    DiscoveryNativeStore,
    DiscoveryPrincipal,
    DiscoveryProfile,
    DiscoveryProfileUnavailable,
    InvalidDiscoveryRequest,
    LocalMatchResult,
    MatchCandidate,
    MatchPreference,
    MatchPreferenceConflict,
    MatchPreferenceUnavailable,
    MatchQueueEntry,
    MatchRateLimited,
    MatchRequestConflict,
    PeerMessageGrant,
    TextMatchCommitPlan,
    TextMatchOutcome,
)


ONLINE_WINDOW_SECONDS = 5 * 60
ONLINE_FUTURE_SKEW_SECONDS = 60
MAX_DISCOVERY_LIST_SIZE = 50
DISCOVERY_SCAN_LIMIT = 500
TEXT_MATCH_RATE_LIMIT = 10
TEXT_MATCH_RATE_WINDOW_SECONDS = 60
TEXT_MATCH_QUEUE_TTL_SECONDS = 2 * 60
TEXT_MATCH_CANDIDATE_LIMIT = 200
MAX_REQUEST_ID_LENGTH = 160

_CITY_CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}\Z")
_GENDER_ALIASES = {
    "男": "male",
    "male": "male",
    "m": "male",
    "1": "male",
    "女": "female",
    "female": "female",
    "f": "female",
    "2": "female",
    "other": "other",
    "其他": "other",
    "unspecified": "unspecified",
    "未设置": "unspecified",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware_time(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DiscoveryContractViolation(f"{field} 必须是带时区时间")
    return value


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidDiscoveryRequest(f"{field} 不合法")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise InvalidDiscoveryRequest(f"{field} 不合法")


def _normalize_city_code(value: object, *, required: bool) -> str | None:
    code = str(value or "").strip().upper()
    if not code:
        if required:
            raise InvalidDiscoveryRequest("city_code 不能为空")
        return None
    if not _CITY_CODE_RE.fullmatch(code):
        raise InvalidDiscoveryRequest("city_code 不合法")
    return code


def _normalize_city_name(value: object, *, required: bool) -> str | None:
    name = str(value or "").strip()
    if not name:
        if required:
            raise InvalidDiscoveryRequest("city_name 不能为空")
        return None
    if len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise InvalidDiscoveryRequest("city_name 不合法")
    return name


def _normalize_city_pair(
    city_code: object,
    city_name: object,
    *,
    required: bool,
) -> tuple[str | None, str | None]:
    code_present = bool(str(city_code or "").strip())
    name_present = bool(str(city_name or "").strip())
    if code_present != name_present:
        raise InvalidDiscoveryRequest("city_code 与 city_name 必须同时提供")
    return (
        _normalize_city_code(city_code, required=required),
        _normalize_city_name(city_name, required=required),
    )


def _normalize_profile_gender(value: object) -> str:
    raw = str(value or "unspecified").strip().casefold()
    gender = _GENDER_ALIASES.get(raw, raw)
    if gender not in PROFILE_GENDERS:
        raise InvalidDiscoveryRequest("gender 不合法")
    return gender


def _normalize_filter_gender(value: object) -> str:
    raw = str(value or "any").strip().casefold()
    if raw in {"不限", "all", "*"}:
        raw = "any"
    raw = _GENDER_ALIASES.get(raw, raw)
    if raw not in FILTER_GENDERS:
        raise InvalidDiscoveryRequest("gender 筛选不合法")
    return raw


def _normalize_property(value: object, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise InvalidDiscoveryRequest("property 不能为空")
        return None
    property_value = str(value).strip()
    if property_value in {"", "any", "不限"} and not required:
        return None
    if (
        not property_value
        or len(property_value) > 80
        or any(ord(char) < 32 or ord(char) == 127 for char in property_value)
    ):
        raise InvalidDiscoveryRequest("property 不合法")
    return property_value


def _normalize_age(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    age = _strict_int(value, "age")
    if not 18 <= age <= 120:
        raise InvalidDiscoveryRequest("age 必须在 18 到 120 之间")
    return age


def _normalize_age_range(min_age: object, max_age: object) -> tuple[int, int]:
    minimum = _strict_int(min_age, "min_age")
    maximum = _strict_int(max_age, "max_age")
    if not 18 <= minimum <= maximum <= 120:
        raise InvalidDiscoveryRequest("年龄范围不合法")
    return minimum, maximum


def _normalize_request_id(value: object) -> str:
    request_id = str(value or "").strip()
    if (
        not request_id
        or len(request_id) > MAX_REQUEST_ID_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in request_id)
    ):
        raise InvalidDiscoveryRequest("request_id 不合法")
    return request_id


def canonical_match_key(
    user_low_queue_entry_id: uuid.UUID,
    user_high_queue_entry_id: uuid.UUID,
) -> str:
    material = (
        f"{TEXT_MATCH_KIND}:{user_low_queue_entry_id}:{user_high_queue_entry_id}"
    )
    return f"text:{hashlib.sha256(material.encode('ascii')).hexdigest()}"


class DiscoveryNativeService:
    def __init__(
        self,
        store: DiscoveryNativeStore,
        *,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        match_rate_limit: int = TEXT_MATCH_RATE_LIMIT,
        match_rate_window_seconds: int = TEXT_MATCH_RATE_WINDOW_SECONDS,
    ) -> None:
        if not 1 <= int(match_rate_limit) <= 100:
            raise ValueError("match_rate_limit 必须在 1 到 100 之间")
        if not 1 <= int(match_rate_window_seconds) <= 3600:
            raise ValueError("match_rate_window_seconds 必须在 1 到 3600 之间")
        self.store = store
        self.clock = clock
        self.id_factory = id_factory
        self.match_rate_limit = int(match_rate_limit)
        self.match_rate_window_seconds = int(match_rate_window_seconds)

    def _now(self) -> datetime:
        return _aware_time(self.clock(), "clock")

    def _principal_account(self, principal: DiscoveryPrincipal) -> DiscoveryAccount:
        account = self.store.resolve_principal(principal)
        if account is None or not account.active:
            raise DiscoveryIdentityUnavailable("当前 Web 账号不可使用本地发现")
        if (
            account.user_id != principal.user_id
            or account.external_account_id != principal.external_account_id
            or account.upstream_uid != principal.upstream_uid
            or account.account_provider != principal.account_provider
        ):
            raise DiscoveryContractViolation("身份解析结果未绑定当前账号")
        self._validate_account(account)
        return account

    @staticmethod
    def _validate_account(account: DiscoveryAccount) -> None:
        if (
            not isinstance(account, DiscoveryAccount)
            or not account.upstream_uid.strip()
            or len(account.upstream_uid) > 128
            or any(ord(char) < 33 for char in account.upstream_uid)
            or len(account.display_name) > 160
        ):
            raise DiscoveryContractViolation("发现账号数据不合法")

    def update_profile(
        self,
        *,
        principal: DiscoveryPrincipal,
        city_code: object = None,
        city_name: object = None,
        gender: object = "unspecified",
        profile_property: object = None,
        age: object = None,
        discoverable: bool = False,
    ) -> DiscoveryProfile:
        account = self._principal_account(principal)
        if not isinstance(discoverable, bool):
            raise InvalidDiscoveryRequest("discoverable 必须是布尔值")
        code, name = _normalize_city_pair(
            city_code,
            city_name,
            required=discoverable,
        )
        existing = self.store.get_discovery_profile(account.user_id)
        last_seen = existing.last_seen_at if existing is not None else None
        profile = DiscoveryProfile(
            user_id=account.user_id,
            city_code=code,
            city_name=name,
            gender=_normalize_profile_gender(gender),
            profile_property=_normalize_property(profile_property),
            age=_normalize_age(age, optional=True),
            discoverable=discoverable,
            last_seen_at=last_seen,
        )
        stored = self.store.save_discovery_profile(profile)
        if stored != profile:
            raise DiscoveryContractViolation("发现资料持久层改变了已校验字段")
        return stored

    def record_seen(self, *, principal: DiscoveryPrincipal) -> DiscoveryProfile:
        account = self._principal_account(principal)
        existing = self.store.get_discovery_profile(account.user_id)
        if existing is None:
            raise DiscoveryProfileUnavailable("请先创建城市级发现资料")
        self._validate_profile(existing, expected_user_id=account.user_id)
        now = self._now()
        touched = self.store.touch_discovery_last_seen(
            user_id=account.user_id,
            seen_at=now,
        )
        self._validate_profile(touched, expected_user_id=account.user_id)
        if touched.last_seen_at != now:
            raise DiscoveryContractViolation("last_seen 未绑定服务端时间")
        return touched

    def set_match_preference(
        self,
        *,
        principal: DiscoveryPrincipal,
        city_scope: object = "same-city",
        gender_preference: object = "any",
        property_preference: object = None,
        min_age: object = 18,
        max_age: object = 120,
        enabled: bool = True,
        expected_version: object = None,
    ) -> MatchPreference:
        account = self._principal_account(principal)
        scope = str(city_scope or "").strip().lower()
        if scope not in MATCH_CITY_SCOPES:
            raise InvalidDiscoveryRequest("city_scope 不合法")
        preference_gender = _normalize_filter_gender(gender_preference)
        minimum, maximum = _normalize_age_range(min_age, max_age)
        if not isinstance(enabled, bool):
            raise InvalidDiscoveryRequest("enabled 必须是布尔值")
        current = self.store.get_match_preference(account.user_id)
        expected: int | None = None
        if expected_version is not None:
            expected = _strict_int(expected_version, "expected_version")
            current_version = current.version if current is not None else 0
            if expected != current_version:
                raise MatchPreferenceConflict("匹配偏好版本已变化")
        if current is not None:
            self._validate_preference(current, expected_user_id=account.user_id)
        proposed = MatchPreference(
            user_id=account.user_id,
            city_scope=scope,
            gender_preference=preference_gender,
            property_preference=_normalize_property(property_preference),
            min_age=minimum,
            max_age=maximum,
            enabled=enabled,
            version=(current.version + 1) if current is not None else 1,
        )
        stored = self.store.save_match_preference(
            proposed,
            expected_version=expected,
        )
        if stored != proposed:
            raise DiscoveryContractViolation("匹配偏好持久层改变了已校验字段")
        return stored

    def online_users(
        self,
        *,
        principal: DiscoveryPrincipal,
        gender: object = "any",
        profile_property: object = None,
        min_age: object = 18,
        max_age: object = 120,
        city_code: object = None,
        city_name: object = None,
        limit: object = 50,
    ) -> DiscoveryList:
        account = self._principal_account(principal)
        own_profile = self.store.get_discovery_profile(account.user_id)
        if own_profile is not None:
            self._validate_profile(own_profile, expected_user_id=account.user_id)
        code, name = _normalize_city_pair(city_code, city_name, required=False)
        filters = self._filters(
            gender=gender,
            profile_property=profile_property,
            min_age=min_age,
            max_age=max_age,
            city_code=code,
            city_name=name,
        )
        return self._list_discovery(
            owner=account,
            own_profile=own_profile,
            filters=filters,
            limit=self._limit(limit),
        )

    def nearby_users(
        self,
        *,
        principal: DiscoveryPrincipal,
        city_code: object = None,
        city_name: object = None,
        gender: object = "any",
        profile_property: object = None,
        min_age: object = 18,
        max_age: object = 120,
        limit: object = 50,
    ) -> DiscoveryList:
        account = self._principal_account(principal)
        own_profile = self.store.get_discovery_profile(account.user_id)
        if own_profile is None:
            raise DiscoveryLocationRequired("请先设置城市级资料")
        self._validate_profile(own_profile, expected_user_id=account.user_id)
        custom = bool(str(city_code or "").strip() or str(city_name or "").strip())
        if custom:
            code, name = _normalize_city_pair(city_code, city_name, required=True)
        else:
            code, name = own_profile.city_code, own_profile.city_name
        if not code or not name:
            raise DiscoveryLocationRequired("当前资料缺少城市信息")
        filters = self._filters(
            gender=gender,
            profile_property=profile_property,
            min_age=min_age,
            max_age=max_age,
            city_code=code,
            city_name=name,
        )
        return self._list_discovery(
            owner=account,
            own_profile=own_profile,
            filters=filters,
            limit=self._limit(limit),
        )

    @staticmethod
    def _filters(
        *,
        gender: object,
        profile_property: object,
        min_age: object,
        max_age: object,
        city_code: str | None,
        city_name: str | None,
    ) -> DiscoveryFilters:
        minimum, maximum = _normalize_age_range(min_age, max_age)
        return DiscoveryFilters(
            gender=_normalize_filter_gender(gender),
            profile_property=_normalize_property(profile_property),
            min_age=minimum,
            max_age=maximum,
            city_code=city_code,
            city_name=city_name,
        )

    @staticmethod
    def _limit(value: object) -> int:
        limit = _strict_int(value, "limit")
        if limit <= 0:
            raise InvalidDiscoveryRequest("limit 必须大于 0")
        return min(limit, MAX_DISCOVERY_LIST_SIZE)

    def _list_discovery(
        self,
        *,
        owner: DiscoveryAccount,
        own_profile: DiscoveryProfile | None,
        filters: DiscoveryFilters,
        limit: int,
    ) -> DiscoveryList:
        now = self._now()
        candidates = self.store.list_discovery_candidates(limit=DISCOVERY_SCAN_LIMIT)
        if len(candidates) > DISCOVERY_SCAN_LIMIT:
            raise DiscoveryContractViolation("发现仓储返回结果超过扫描上限")
        accepted: list[tuple[bool, datetime, DiscoveryCard]] = []
        for candidate in candidates:
            self._validate_candidate(candidate)
            account = candidate.account
            profile = candidate.profile
            if account.user_id == owner.user_id or not account.active or not profile.discoverable:
                continue
            if not self._is_online(profile.last_seen_at, now=now):
                continue
            if self.store.is_blocked_between(owner.user_id, account.user_id):
                continue
            if not self._profile_matches(profile, filters):
                continue
            same_city = bool(
                own_profile is not None
                and own_profile.city_code
                and own_profile.city_code == profile.city_code
            )
            accepted.append(
                (
                    same_city,
                    profile.last_seen_at,
                    self._card(candidate, online=True),
                )
            )
        accepted.sort(
            key=lambda item: (item[0], item[1], str(item[2].user_id)),
            reverse=True,
        )
        return DiscoveryList(
            items=tuple(item[2] for item in accepted[:limit]),
            filters=filters,
            scanned_count=len(candidates),
        )

    @staticmethod
    def _profile_matches(profile: DiscoveryProfile, filters: DiscoveryFilters) -> bool:
        if filters.city_code is not None and profile.city_code != filters.city_code:
            return False
        if filters.gender != "any" and profile.gender != filters.gender:
            return False
        if (
            filters.profile_property is not None
            and profile.profile_property != filters.profile_property
        ):
            return False
        if profile.age is None:
            return filters.min_age == 18 and filters.max_age == 120
        return filters.min_age <= profile.age <= filters.max_age

    @staticmethod
    def _is_online(last_seen_at: datetime | None, *, now: datetime) -> bool:
        if last_seen_at is None:
            return False
        seen = _aware_time(last_seen_at, "last_seen_at")
        return (
            now - timedelta(seconds=ONLINE_WINDOW_SECONDS)
            <= seen
            <= now + timedelta(seconds=ONLINE_FUTURE_SKEW_SECONDS)
        )

    def request_text_match(
        self,
        *,
        principal: DiscoveryPrincipal,
        request_id: object,
    ) -> TextMatchOutcome:
        account = self._principal_account(principal)
        normalized_request_id = _normalize_request_id(request_id)
        profile = self.store.get_discovery_profile(account.user_id)
        if profile is None:
            raise DiscoveryProfileUnavailable("请先创建城市级发现资料")
        self._validate_profile(profile, expected_user_id=account.user_id)
        if not profile.discoverable or not profile.city_code:
            raise DiscoveryProfileUnavailable("当前资料未开启城市级发现")
        preference = self.store.get_match_preference(account.user_id)
        if preference is None:
            raise MatchPreferenceUnavailable("请先设置文本匹配偏好")
        self._validate_preference(preference, expected_user_id=account.user_id)
        if not preference.enabled:
            raise MatchPreferenceUnavailable("文本匹配偏好已停用")

        existing = self.store.get_text_match_outcome(
            user_id=account.user_id,
            request_id=normalized_request_id,
        )
        if existing is not None:
            self._validate_request_binding(
                existing.queue_entry,
                user_id=account.user_id,
                request_id=normalized_request_id,
                city_code=profile.city_code,
                preference_version=preference.version,
            )
            return replace(existing, created=False)

        now = self._now()
        frequency = self.store.consume_match_frequency(
            user_id=account.user_id,
            request_id=normalized_request_id,
            occurred_at=now,
            limit=self.match_rate_limit,
            window_seconds=self.match_rate_window_seconds,
        )
        if not frequency.allowed:
            raise MatchRateLimited(
                "文本匹配请求过于频繁",
                retry_after_seconds=frequency.retry_after_seconds,
            )
        entry = self.store.enqueue_text_match(
            account=account,
            profile=profile,
            preference=preference,
            request_id=normalized_request_id,
            enqueued_at=now,
            expires_at=now + timedelta(seconds=TEXT_MATCH_QUEUE_TTL_SECONDS),
        )
        self._validate_request_binding(
            entry,
            user_id=account.user_id,
            request_id=normalized_request_id,
            city_code=profile.city_code,
            preference_version=preference.version,
        )
        requester = MatchCandidate(account, profile, preference, entry)
        candidates = self.store.list_waiting_text_candidates(
            exclude_user_id=account.user_id,
            at=now,
            limit=TEXT_MATCH_CANDIDATE_LIMIT,
        )
        if len(candidates) > TEXT_MATCH_CANDIDATE_LIMIT:
            raise DiscoveryContractViolation("匹配仓储返回候选数超过上限")
        eligible: list[tuple[tuple[int, int, int, float, str], MatchCandidate]] = []
        for candidate in candidates:
            self._validate_match_candidate(candidate)
            if not self._eligible_match(requester, candidate, now=now):
                continue
            eligible.append((self._match_score(requester, candidate, now=now), candidate))
        if not eligible:
            return TextMatchOutcome(
                status=QUEUE_STATUS_WAITING,
                request_id=normalized_request_id,
                queue_entry=entry,
                created=True,
            )

        _score, chosen = max(eligible, key=lambda item: item[0])
        plan = self._commit_plan(requester, chosen, matched_at=now)
        outcome = self.store.commit_text_match(
            requester=requester,
            candidate=chosen,
            plan=plan,
        )
        self._validate_matched_outcome(
            outcome,
            requester=requester,
            candidate=chosen,
            plan=plan,
        )
        return outcome

    def _eligible_match(
        self,
        requester: MatchCandidate,
        candidate: MatchCandidate,
        *,
        now: datetime,
    ) -> bool:
        if candidate.account.user_id == requester.account.user_id:
            return False
        if not candidate.account.active or not candidate.profile.discoverable:
            return False
        if candidate.queue_entry.status != QUEUE_STATUS_WAITING:
            return False
        if candidate.queue_entry.expires_at <= now:
            return False
        if not self._is_online(candidate.profile.last_seen_at, now=now):
            return False
        if self.store.is_blocked_between(
            requester.account.user_id, candidate.account.user_id
        ):
            return False
        return self._preference_accepts(
            requester.preference,
            owner=requester.profile,
            peer=candidate.profile,
        ) and self._preference_accepts(
            candidate.preference,
            owner=candidate.profile,
            peer=requester.profile,
        )

    @staticmethod
    def _preference_accepts(
        preference: MatchPreference,
        *,
        owner: DiscoveryProfile,
        peer: DiscoveryProfile,
    ) -> bool:
        if not preference.enabled:
            return False
        if preference.city_scope == "same-city" and owner.city_code != peer.city_code:
            return False
        if (
            preference.gender_preference != "any"
            and peer.gender != preference.gender_preference
        ):
            return False
        if (
            preference.property_preference is not None
            and peer.profile_property != preference.property_preference
        ):
            return False
        return peer.age is not None and preference.min_age <= peer.age <= preference.max_age

    @staticmethod
    def _match_score(
        requester: MatchCandidate,
        candidate: MatchCandidate,
        *,
        now: datetime,
    ) -> tuple[int, int, int, float, str]:
        same_city = int(requester.profile.city_code == candidate.profile.city_code)
        specificity = sum(
            (
                requester.preference.gender_preference != "any",
                requester.preference.property_preference is not None,
                candidate.preference.gender_preference != "any",
                candidate.preference.property_preference is not None,
            )
        )
        age_gap = abs(int(requester.profile.age or 120) - int(candidate.profile.age or 120))
        waiting_seconds = max(
            0.0,
            (now - candidate.queue_entry.enqueued_at).total_seconds(),
        )
        return (
            same_city,
            specificity,
            -age_gap,
            waiting_seconds,
            str(candidate.account.user_id),
        )

    def _commit_plan(
        self,
        requester: MatchCandidate,
        candidate: MatchCandidate,
        *,
        matched_at: datetime,
    ) -> TextMatchCommitPlan:
        ordered = sorted(
            ((requester.account.user_id, requester.queue_entry.id),
             (candidate.account.user_id, candidate.queue_entry.id)),
            key=lambda item: item[0].int,
        )
        (low_user, low_entry), (high_user, high_entry) = ordered
        return TextMatchCommitPlan(
            match_key=canonical_match_key(low_entry, high_entry),
            user_low_id=low_user,
            user_high_id=high_user,
            user_low_queue_entry_id=low_entry,
            user_high_queue_entry_id=high_entry,
            initiated_by_user_id=requester.account.user_id,
            authorization_intent_id=self.id_factory(),
            matched_at=matched_at,
        )

    @staticmethod
    def _validate_request_binding(
        entry: MatchQueueEntry,
        *,
        user_id: uuid.UUID,
        request_id: str,
        city_code: str,
        preference_version: int,
    ) -> None:
        if not isinstance(entry, MatchQueueEntry):
            raise DiscoveryContractViolation("匹配仓储未返回队列项")
        if (
            entry.user_id != user_id
            or entry.request_id != request_id
            or entry.queue_kind != TEXT_MATCH_KIND
            or entry.city_code != city_code
            or entry.preference_version != preference_version
        ):
            raise MatchRequestConflict("request_id 已绑定其他匹配参数")
        _aware_time(entry.enqueued_at, "queue.enqueued_at")
        _aware_time(entry.expires_at, "queue.expires_at")
        if entry.expires_at <= entry.enqueued_at:
            raise DiscoveryContractViolation("匹配队列有效期不合法")

    def _validate_matched_outcome(
        self,
        outcome: TextMatchOutcome,
        *,
        requester: MatchCandidate,
        candidate: MatchCandidate,
        plan: TextMatchCommitPlan,
    ) -> None:
        if (
            not isinstance(outcome, TextMatchOutcome)
            or outcome.status != QUEUE_STATUS_MATCHED
            or outcome.request_id != requester.queue_entry.request_id
            or outcome.match_result is None
            or outcome.peer is None
            or outcome.message_authorization is None
        ):
            raise DiscoveryContractViolation("匹配事务未返回完整结果")
        result = outcome.match_result
        expected_result = (
            plan.match_key,
            plan.user_low_id,
            plan.user_high_id,
            plan.user_low_queue_entry_id,
            plan.user_high_queue_entry_id,
            plan.initiated_by_user_id,
            MATCH_STATUS_ACTIVE,
            plan.matched_at,
        )
        actual_result = (
            result.match_key,
            result.user_low_id,
            result.user_high_id,
            result.user_low_queue_entry_id,
            result.user_high_queue_entry_id,
            result.initiated_by_user_id,
            result.status,
            result.matched_at,
        )
        if actual_result != expected_result or result.user_low_id.int >= result.user_high_id.int:
            raise DiscoveryContractViolation("MatchResult 未按用户规范化")
        if outcome.peer.user_id != candidate.account.user_id:
            raise DiscoveryContractViolation("匹配结果返回了错误对端")
        authorization = outcome.message_authorization
        expected_grants = {
            (
                requester.account.user_id,
                candidate.account.user_id,
                candidate.account.upstream_uid,
            ),
            (
                candidate.account.user_id,
                requester.account.user_id,
                requester.account.upstream_uid,
            ),
        }
        actual_grants = {
            (grant.owner_user_id, grant.peer_user_id, grant.peer_upstream_uid)
            for grant in authorization.grants
        }
        if (
            authorization.id != plan.authorization_intent_id
            or authorization.match_result_id != result.id
            or authorization.reason != "text-match"
            or authorization.granted_at != plan.matched_at
            or actual_grants != expected_grants
        ):
            raise DiscoveryContractViolation("私聊授权 intent 未覆盖匹配双方")

    def _validate_candidate(self, candidate: DiscoveryCandidate) -> None:
        if not isinstance(candidate, DiscoveryCandidate):
            raise DiscoveryContractViolation("发现仓储返回了未知候选类型")
        self._validate_account(candidate.account)
        self._validate_profile(
            candidate.profile,
            expected_user_id=candidate.account.user_id,
        )

    def _validate_match_candidate(self, candidate: MatchCandidate) -> None:
        if not isinstance(candidate, MatchCandidate):
            raise DiscoveryContractViolation("匹配仓储返回了未知候选类型")
        self._validate_account(candidate.account)
        self._validate_profile(candidate.profile, expected_user_id=candidate.account.user_id)
        self._validate_preference(
            candidate.preference,
            expected_user_id=candidate.account.user_id,
        )
        entry = candidate.queue_entry
        if (
            entry.user_id != candidate.account.user_id
            or entry.queue_kind != TEXT_MATCH_KIND
            or entry.preference_version != candidate.preference.version
            or entry.city_code != candidate.profile.city_code
        ):
            raise DiscoveryContractViolation("匹配候选队列绑定错误")
        _aware_time(entry.enqueued_at, "candidate.enqueued_at")
        _aware_time(entry.expires_at, "candidate.expires_at")

    @staticmethod
    def _validate_profile(
        profile: DiscoveryProfile,
        *,
        expected_user_id: uuid.UUID,
    ) -> None:
        if not isinstance(profile, DiscoveryProfile) or profile.user_id != expected_user_id:
            raise DiscoveryContractViolation("发现资料未绑定用户")
        try:
            code, name = _normalize_city_pair(
                profile.city_code,
                profile.city_name,
                required=profile.discoverable,
            )
            gender = _normalize_profile_gender(profile.gender)
            property_value = _normalize_property(profile.profile_property)
            age = _normalize_age(profile.age, optional=True)
        except InvalidDiscoveryRequest as exc:
            raise DiscoveryContractViolation("发现资料字段不合法") from exc
        if (
            code != profile.city_code
            or name != profile.city_name
            or gender != profile.gender
            or property_value != profile.profile_property
            or age != profile.age
            or not isinstance(profile.discoverable, bool)
        ):
            raise DiscoveryContractViolation("发现资料未规范化")
        if profile.last_seen_at is not None:
            _aware_time(profile.last_seen_at, "profile.last_seen_at")

    @staticmethod
    def _validate_preference(
        preference: MatchPreference,
        *,
        expected_user_id: uuid.UUID,
    ) -> None:
        if (
            not isinstance(preference, MatchPreference)
            or preference.user_id != expected_user_id
            or preference.city_scope not in MATCH_CITY_SCOPES
            or preference.gender_preference not in FILTER_GENDERS
            or not isinstance(preference.enabled, bool)
            or preference.version < 1
            or not 18 <= preference.min_age <= preference.max_age <= 120
        ):
            raise DiscoveryContractViolation("匹配偏好不合法")
        try:
            normalized_property = _normalize_property(
                preference.property_preference
            )
        except InvalidDiscoveryRequest as exc:
            raise DiscoveryContractViolation("匹配 property 偏好不合法") from exc
        if normalized_property != preference.property_preference:
            raise DiscoveryContractViolation("匹配偏好未规范化")

    @staticmethod
    def _card(candidate: DiscoveryCandidate | MatchCandidate, *, online: bool) -> DiscoveryCard:
        profile = candidate.profile
        if not profile.city_code or not profile.city_name:
            raise DiscoveryContractViolation("可发现资料缺少城市")
        return DiscoveryCard(
            user_id=candidate.account.user_id,
            upstream_uid=candidate.account.upstream_uid,
            display_name=candidate.account.display_name,
            city_code=profile.city_code,
            city_name=profile.city_name,
            gender=profile.gender,
            profile_property=profile.profile_property,
            age=profile.age,
            online=online,
        )
