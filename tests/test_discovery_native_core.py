from __future__ import annotations

import unittest
import uuid
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime, timedelta

from bbw_web.discovery_native import (
    MATCH_STATUS_ACTIVE,
    QUEUE_STATUS_MATCHED,
    QUEUE_STATUS_WAITING,
    TEXT_MATCH_KIND,
    DirectMessageAuthorizationIntent,
    DiscoveryAccount,
    DiscoveryCandidate,
    DiscoveryLocationRequired,
    DiscoveryNativeService,
    DiscoveryPrincipal,
    DiscoveryProfile,
    InvalidDiscoveryRequest,
    LocalMatchResult,
    MatchCandidate,
    MatchFrequencyDecision,
    MatchPreference,
    MatchPreferenceConflict,
    MatchQueueEntry,
    MatchRateLimited,
    MatchRequestConflict,
    PeerMessageGrant,
    TextMatchOutcome,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _MemoryStore:
    def __init__(self) -> None:
        self.accounts: dict[uuid.UUID, DiscoveryAccount] = {}
        self.accounts_by_uid: dict[str, DiscoveryAccount] = {}
        self.profiles: dict[uuid.UUID, DiscoveryProfile] = {}
        self.preferences: dict[uuid.UUID, MatchPreference] = {}
        self.queues: dict[tuple[uuid.UUID, str], MatchQueueEntry] = {}
        self.outcomes: dict[tuple[uuid.UUID, str], TextMatchOutcome] = {}
        self.blocked: set[frozenset[uuid.UUID]] = set()
        self.frequency_requests: set[tuple[uuid.UUID, str]] = set()
        self.frequency_calls = 0
        self.force_rate_denied = False
        self.commit_count = 0

    def add_account(self, account: DiscoveryAccount) -> None:
        self.accounts[account.user_id] = account
        self.accounts_by_uid[account.upstream_uid] = account

    def resolve_principal(self, principal: DiscoveryPrincipal):
        account = self.accounts.get(principal.user_id)
        if account is None:
            return None
        if (
            account.external_account_id == principal.external_account_id
            and account.upstream_uid == principal.upstream_uid
            and account.account_provider == principal.account_provider
        ):
            return account
        return None

    def get_discovery_profile(self, user_id: uuid.UUID):
        return self.profiles.get(user_id)

    def save_discovery_profile(self, profile: DiscoveryProfile):
        self.profiles[profile.user_id] = profile
        return profile

    def touch_discovery_last_seen(self, *, user_id: uuid.UUID, seen_at: datetime):
        profile = replace(self.profiles[user_id], last_seen_at=seen_at)
        self.profiles[user_id] = profile
        return profile

    def list_discovery_candidates(self, *, limit: int):
        return [
            DiscoveryCandidate(account, self.profiles[account.user_id])
            for account in list(self.accounts.values())[:limit]
            if account.user_id in self.profiles
        ]

    def is_blocked_between(self, left_user_id: uuid.UUID, right_user_id: uuid.UUID):
        return frozenset({left_user_id, right_user_id}) in self.blocked

    def get_match_preference(self, user_id: uuid.UUID):
        return self.preferences.get(user_id)

    def save_match_preference(self, preference, *, expected_version):
        current = self.preferences.get(preference.user_id)
        current_version = current.version if current is not None else 0
        if expected_version is not None and expected_version != current_version:
            raise MatchPreferenceConflict("version conflict")
        self.preferences[preference.user_id] = preference
        return preference

    def get_text_match_outcome(self, *, user_id: uuid.UUID, request_id: str):
        key = (user_id, request_id)
        outcome = self.outcomes.get(key)
        if outcome is not None:
            return outcome
        entry = self.queues.get(key)
        if entry is None:
            return None
        return TextMatchOutcome(
            status=entry.status,
            request_id=request_id,
            queue_entry=entry,
            created=False,
        )

    def consume_match_frequency(self, **values):
        key = (values["user_id"], values["request_id"])
        if key not in self.frequency_requests:
            self.frequency_calls += 1
            self.frequency_requests.add(key)
        return MatchFrequencyDecision(
            allowed=not self.force_rate_denied,
            retry_after_seconds=17 if self.force_rate_denied else 0,
        )

    def enqueue_text_match(self, **values):
        account = values["account"]
        profile = values["profile"]
        preference = values["preference"]
        request_id = values["request_id"]
        key = (account.user_id, request_id)
        existing = self.queues.get(key)
        if existing is not None:
            if (
                existing.city_code != profile.city_code
                or existing.preference_version != preference.version
            ):
                raise MatchRequestConflict("request conflict")
            return existing
        entry = MatchQueueEntry(
            id=uuid.uuid4(),
            public_id=f"mqe_{uuid.uuid4().hex}",
            user_id=account.user_id,
            request_id=request_id,
            queue_kind=TEXT_MATCH_KIND,
            status=QUEUE_STATUS_WAITING,
            city_code=profile.city_code,
            preference_version=preference.version,
            enqueued_at=values["enqueued_at"],
            expires_at=values["expires_at"],
        )
        self.queues[key] = entry
        return entry

    def list_waiting_text_candidates(self, *, exclude_user_id, at, limit):
        candidates = []
        for entry in self.queues.values():
            if (
                entry.user_id == exclude_user_id
                or entry.status != QUEUE_STATUS_WAITING
                or entry.expires_at <= at
            ):
                continue
            account = self.accounts[entry.user_id]
            candidates.append(
                MatchCandidate(
                    account=account,
                    profile=self.profiles[entry.user_id],
                    preference=self.preferences[entry.user_id],
                    queue_entry=entry,
                )
            )
        return candidates[:limit]

    def commit_text_match(self, *, requester, candidate, plan):
        self.commit_count += 1
        requester_entry = replace(
            requester.queue_entry,
            status=QUEUE_STATUS_MATCHED,
            matched_at=plan.matched_at,
        )
        candidate_entry = replace(
            candidate.queue_entry,
            status=QUEUE_STATUS_MATCHED,
            matched_at=plan.matched_at,
        )
        self.queues[(requester.account.user_id, requester.queue_entry.request_id)] = requester_entry
        self.queues[(candidate.account.user_id, candidate.queue_entry.request_id)] = candidate_entry
        result = LocalMatchResult(
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
        authorization = DirectMessageAuthorizationIntent(
            id=plan.authorization_intent_id,
            match_result_id=result.id,
            grants=(
                PeerMessageGrant(
                    requester.account.user_id,
                    candidate.account.user_id,
                    candidate.account.upstream_uid,
                ),
                PeerMessageGrant(
                    candidate.account.user_id,
                    requester.account.user_id,
                    requester.account.upstream_uid,
                ),
            ),
            reason="text-match",
            granted_at=plan.matched_at,
        )
        peer = self._card(candidate)
        outcome = TextMatchOutcome(
            status=QUEUE_STATUS_MATCHED,
            request_id=requester.queue_entry.request_id,
            queue_entry=requester_entry,
            created=True,
            match_result=result,
            peer=peer,
            message_authorization=authorization,
        )
        self.outcomes[(requester.account.user_id, requester.queue_entry.request_id)] = outcome
        reverse = TextMatchOutcome(
            status=QUEUE_STATUS_MATCHED,
            request_id=candidate.queue_entry.request_id,
            queue_entry=candidate_entry,
            created=True,
            match_result=result,
            peer=self._card(requester),
            message_authorization=authorization,
        )
        self.outcomes[(candidate.account.user_id, candidate.queue_entry.request_id)] = reverse
        return outcome

    @staticmethod
    def _card(candidate: MatchCandidate):
        profile = candidate.profile
        from bbw_web.discovery_native import DiscoveryCard

        return DiscoveryCard(
            user_id=candidate.account.user_id,
            upstream_uid=candidate.account.upstream_uid,
            display_name=candidate.account.display_name,
            city_code=str(profile.city_code),
            city_name=str(profile.city_name),
            gender=profile.gender,
            profile_property=profile.profile_property,
            age=profile.age,
            online=True,
        )


class DiscoveryNativeCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.store = _MemoryStore()
        self.owner = self._account("42", "当前用户")
        self.store.add_account(self.owner)
        self.service = DiscoveryNativeService(
            self.store,
            clock=lambda: self.now,
        )
        self.principal = self._principal(self.owner)
        self.store.profiles[self.owner.user_id] = self._profile(
            self.owner,
            city_code="CN-350100",
            city_name="福州",
            gender="male",
            profile_property="Z",
            age=30,
        )

    @staticmethod
    def _account(uid: str, name: str, *, active: bool = True):
        return DiscoveryAccount(
            uuid.uuid4(),
            uuid.uuid4(),
            uid,
            name,
            active=active,
        )

    @staticmethod
    def _principal(account: DiscoveryAccount):
        return DiscoveryPrincipal(
            account.user_id,
            account.external_account_id,
            account.upstream_uid,
            account.account_provider,
        )

    def _profile(
        self,
        account: DiscoveryAccount,
        *,
        city_code: str,
        city_name: str,
        gender: str,
        profile_property: str,
        age: int,
        seen_delta: int = 0,
        discoverable: bool = True,
    ):
        return DiscoveryProfile(
            user_id=account.user_id,
            city_code=city_code,
            city_name=city_name,
            gender=gender,
            profile_property=profile_property,
            age=age,
            discoverable=discoverable,
            last_seen_at=self.now - timedelta(seconds=seen_delta),
        )

    def _add_candidate(
        self,
        uid: str,
        *,
        city_code: str = "CN-350100",
        city_name: str = "福州",
        gender: str = "female",
        profile_property: str = "Z",
        age: int = 28,
        seen_delta: int = 0,
        active: bool = True,
    ) -> DiscoveryAccount:
        account = self._account(uid, f"用户{uid}", active=active)
        self.store.add_account(account)
        self.store.profiles[account.user_id] = self._profile(
            account,
            city_code=city_code,
            city_name=city_name,
            gender=gender,
            profile_property=profile_property,
            age=age,
            seen_delta=seen_delta,
        )
        return account

    def _set_preference(
        self,
        account: DiscoveryAccount,
        *,
        city_scope: str = "anywhere",
        gender: str = "any",
        property_value: str | None = None,
        min_age: int = 18,
        max_age: int = 120,
    ) -> MatchPreference:
        preference = MatchPreference(
            user_id=account.user_id,
            city_scope=city_scope,
            gender_preference=gender,
            property_preference=property_value,
            min_age=min_age,
            max_age=max_age,
            enabled=True,
            version=1,
        )
        self.store.preferences[account.user_id] = preference
        return preference

    def _enqueue_candidate(
        self,
        account: DiscoveryAccount,
        request_id: str,
        *,
        enqueued_delta: int = 30,
    ) -> MatchQueueEntry:
        preference = self.store.preferences[account.user_id]
        profile = self.store.profiles[account.user_id]
        return self.store.enqueue_text_match(
            account=account,
            profile=profile,
            preference=preference,
            request_id=request_id,
            enqueued_at=self.now - timedelta(seconds=enqueued_delta),
            expires_at=self.now + timedelta(seconds=120),
        )

    def test_profile_contract_has_city_only_and_rejects_unknown_location_input(self) -> None:
        profile = self.service.update_profile(
            principal=self.principal,
            city_code="cn-350100",
            city_name="福州",
            gender="男",
            profile_property="Z",
            age=30,
            discoverable=True,
        )

        self.assertEqual(profile.city_code, "CN-350100")
        serialized = asdict(profile)
        self.assertEqual(
            set(serialized),
            {
                "user_id",
                "city_code",
                "city_name",
                "gender",
                "profile_property",
                "age",
                "discoverable",
                "last_seen_at",
            },
        )
        field_names = {field.name for field in fields(DiscoveryProfile)}
        self.assertEqual(field_names, set(serialized))
        with self.assertRaises(TypeError):
            self.service.update_profile(
                principal=self.principal,
                city_code="CN-350100",
                city_name="福州",
                discoverable=True,
                latitude=26.0,
            )

    def test_online_is_derived_only_from_stored_last_seen(self) -> None:
        online = self._add_candidate("9", seen_delta=299)
        self._add_candidate("10", seen_delta=301)
        self._add_candidate("11", active=False)
        blocked = self._add_candidate("12")
        self.store.blocked.add(frozenset({self.owner.user_id, blocked.user_id}))

        result = self.service.online_users(principal=self.principal)

        self.assertEqual([item.user_id for item in result.items], [online.user_id])
        self.assertTrue(result.items[0].online)

    def test_online_and_nearby_apply_gender_property_age_and_exact_city(self) -> None:
        accepted = self._add_candidate("9", gender="female", age=26)
        self._add_candidate("10", gender="male", age=26)
        self._add_candidate("11", gender="female", profile_property="B", age=26)
        self._add_candidate("12", gender="female", age=40)
        remote = self._add_candidate(
            "13",
            city_code="CN-350200",
            city_name="厦门",
            gender="female",
            age=26,
        )

        online = self.service.online_users(
            principal=self.principal,
            gender="female",
            profile_property="Z",
            min_age=25,
            max_age=34,
        )
        nearby = self.service.nearby_users(
            principal=self.principal,
            gender="female",
            profile_property="Z",
            min_age=25,
            max_age=34,
        )

        self.assertEqual(
            {item.user_id for item in online.items},
            {accepted.user_id, remote.user_id},
        )
        self.assertEqual([item.user_id for item in nearby.items], [accepted.user_id])
        self.assertEqual(nearby.filters.city_code, "CN-350100")
        self.assertNotIn("distance", asdict(nearby.items[0]))

    def test_nearby_requires_complete_city_pair(self) -> None:
        self.store.profiles[self.owner.user_id] = replace(
            self.store.profiles[self.owner.user_id],
            city_code=None,
            city_name=None,
            discoverable=False,
        )
        with self.assertRaises(DiscoveryLocationRequired):
            self.service.nearby_users(principal=self.principal)
        with self.assertRaises(InvalidDiscoveryRequest):
            self.service.nearby_users(
                principal=self.principal,
                city_code="CN-350100",
            )

    def test_text_match_prefers_same_city_then_returns_canonical_result_and_grants(self) -> None:
        self._set_preference(
            self.owner,
            gender="female",
            property_value="Z",
            min_age=25,
            max_age=34,
        )
        remote = self._add_candidate(
            "8", city_code="CN-350200", city_name="厦门", age=28
        )
        same_city = self._add_candidate("9", age=29)
        for account in (remote, same_city):
            self._set_preference(
                account,
                gender="male",
                property_value="Z",
                min_age=25,
                max_age=35,
            )
        self._enqueue_candidate(remote, "remote-request", enqueued_delta=100)
        self._enqueue_candidate(same_city, "same-city-request", enqueued_delta=10)

        outcome = self.service.request_text_match(
            principal=self.principal,
            request_id="owner-request",
        )

        self.assertEqual(outcome.status, QUEUE_STATUS_MATCHED)
        self.assertEqual(outcome.peer.user_id, same_city.user_id)
        result = outcome.match_result
        self.assertLess(result.user_low_id.int, result.user_high_id.int)
        self.assertEqual(
            {result.user_low_id, result.user_high_id},
            {self.owner.user_id, same_city.user_id},
        )
        grants = outcome.message_authorization.grants
        self.assertEqual({grant.owner_user_id for grant in grants}, {self.owner.user_id, same_city.user_id})
        self.assertEqual({grant.peer_user_id for grant in grants}, {self.owner.user_id, same_city.user_id})
        self.assertEqual(self.store.commit_count, 1)

    def test_match_excludes_blocked_disabled_offline_and_incompatible_candidates(self) -> None:
        self._set_preference(self.owner, gender="female")
        blocked = self._add_candidate("9")
        disabled = self._add_candidate("10", active=False)
        offline = self._add_candidate("11", seen_delta=301)
        incompatible = self._add_candidate("12", gender="male")
        for account in (blocked, disabled, offline, incompatible):
            self._set_preference(account, gender="male")
            self._enqueue_candidate(account, f"candidate-{account.upstream_uid}")
        self.store.blocked.add(frozenset({self.owner.user_id, blocked.user_id}))

        outcome = self.service.request_text_match(
            principal=self.principal,
            request_id="waiting-request",
        )

        self.assertEqual(outcome.status, QUEUE_STATUS_WAITING)
        self.assertIsNone(outcome.match_result)
        self.assertEqual(self.store.commit_count, 0)

    def test_same_request_is_idempotent_without_second_rate_charge_or_result(self) -> None:
        self._set_preference(self.owner, gender="female")
        candidate = self._add_candidate("9")
        self._set_preference(candidate, gender="male")
        self._enqueue_candidate(candidate, "candidate-request")

        first = self.service.request_text_match(
            principal=self.principal,
            request_id="stable-request",
        )
        repeated = self.service.request_text_match(
            principal=self.principal,
            request_id="stable-request",
        )

        self.assertEqual(first.match_result.id, repeated.match_result.id)
        self.assertFalse(repeated.created)
        self.assertEqual(self.store.frequency_calls, 1)
        self.assertEqual(self.store.commit_count, 1)

    def test_same_request_with_changed_preference_is_conflict(self) -> None:
        preference = self.service.set_match_preference(
            principal=self.principal,
            city_scope="anywhere",
            gender_preference="female",
            enabled=True,
        )
        first = self.service.request_text_match(
            principal=self.principal,
            request_id="conflict-request",
        )
        self.assertEqual(first.status, QUEUE_STATUS_WAITING)
        self.service.set_match_preference(
            principal=self.principal,
            city_scope="anywhere",
            gender_preference="any",
            expected_version=preference.version,
        )

        with self.assertRaises(MatchRequestConflict):
            self.service.request_text_match(
                principal=self.principal,
                request_id="conflict-request",
            )

    def test_local_frequency_limit_reports_retry_after(self) -> None:
        self._set_preference(self.owner)
        self.store.force_rate_denied = True
        with self.assertRaises(MatchRateLimited) as captured:
            self.service.request_text_match(
                principal=self.principal,
                request_id="rate-limited",
            )
        self.assertEqual(captured.exception.retry_after_seconds, 17)

    def test_unsupported_legacy_modes_are_absent(self) -> None:
        self.assertFalse(hasattr(self.service, "request_voice_match"))
        self.assertFalse(hasattr(self.service, "request_bottle_match"))
        self.assertFalse(hasattr(self.service, "request_dating_match"))


if __name__ == "__main__":
    unittest.main()
