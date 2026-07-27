from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from bbw_web import native_discovery_api as API
from bbw_web.discovery_native import (
    QUEUE_STATUS_MATCHED,
    QUEUE_STATUS_WAITING,
    DiscoveryAccount,
    DiscoveryCard,
    DiscoveryFilters,
    DiscoveryList,
    DiscoveryLocationRequired,
    DiscoveryProfile,
    LocalMatchResult,
    MatchPreference,
    MatchQueueEntry,
    MatchRateLimited,
    TextMatchOutcome,
)
from bbw_web.discovery_native.repository import normalized_city_code


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _identity(uid: str = "账号-A") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        external_account_id=uuid.uuid4(),
        upstream_uid=uid,
        match_pool_online_list_enabled=False,
        nearby_custom_city_enabled=False,
    )


def _account(identity: SimpleNamespace) -> DiscoveryAccount:
    return DiscoveryAccount(
        identity.user_id,
        identity.external_account_id,
        identity.upstream_uid,
        "本地用户",
    )


def _profile(identity: SimpleNamespace, *, city: bool = True) -> DiscoveryProfile:
    return DiscoveryProfile(
        user_id=identity.user_id,
        city_code="CN-350100" if city else None,
        city_name="福州" if city else None,
        gender="male",
        profile_property="Z",
        age=30,
        discoverable=city,
        last_seen_at=NOW,
    )


def _preference(identity: SimpleNamespace, *, version: int = 1) -> MatchPreference:
    return MatchPreference(
        identity.user_id,
        "anywhere",
        "female",
        "Z",
        18,
        120,
        True,
        version,
    )


def _queue(identity: SimpleNamespace, request_id: str, *, matched: bool = False):
    return MatchQueueEntry(
        id=uuid.uuid4(),
        public_id=f"mqe_{uuid.uuid4().hex}",
        user_id=identity.user_id,
        request_id=request_id,
        queue_kind="text",
        status=QUEUE_STATUS_MATCHED if matched else QUEUE_STATUS_WAITING,
        city_code="CN-350100",
        preference_version=1,
        enqueued_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
        matched_at=NOW if matched else None,
    )


def _outcome(
    identity: SimpleNamespace,
    request_id: str,
    *,
    matched: bool,
    created: bool = True,
) -> TextMatchOutcome:
    queue = _queue(identity, request_id, matched=matched)
    if not matched:
        return TextMatchOutcome(
            QUEUE_STATUS_WAITING,
            request_id,
            queue,
            created,
        )
    peer_id = uuid.uuid4()
    low, high = sorted((identity.user_id, peer_id), key=lambda value: value.int)
    result = LocalMatchResult(
        id=uuid.uuid4(),
        public_id=f"mch_{uuid.uuid4().hex}",
        match_key="text:" + "a" * 64,
        user_low_id=low,
        user_high_id=high,
        user_low_queue_entry_id=queue.id if low == identity.user_id else uuid.uuid4(),
        user_high_queue_entry_id=queue.id if high == identity.user_id else uuid.uuid4(),
        initiated_by_user_id=identity.user_id,
        status="active",
        matched_at=NOW,
    )
    peer = DiscoveryCard(
        peer_id,
        "用户-乙_7",
        "乙用户",
        "CN-350100",
        "福州",
        "female",
        "Z",
        28,
        True,
    )
    return TextMatchOutcome(
        QUEUE_STATUS_MATCHED,
        request_id,
        queue,
        created,
        match_result=result,
        peer=peer,
    )


@contextmanager
def _scope(db: object):
    yield db


def _configured(identity: SimpleNamespace):
    store = Mock()
    service = Mock()
    profile = _profile(identity)
    store.canonical_profile_values.return_value = {
        "city_code": profile.city_code,
        "city_name": profile.city_name,
        "gender": profile.gender,
        "profile_property": profile.profile_property,
        "age": profile.age,
        "discoverable": profile.discoverable,
    }
    store.resolve_principal.return_value = _account(identity)
    store.canonical_user_display.return_value = {
        "avatar": "/media/me.jpg",
        "portrait": "/media/me.jpg",
        "is_realname": True,
        "money": "8",
        "logged_in": True,
    }
    service.record_seen.return_value = profile
    return store, service, profile


class NativeDiscoveryApiTests(unittest.TestCase):
    def test_exact_route_surface_and_unhandled_contract(self) -> None:
        self.assertEqual(
            API.GET_PATHS,
            {
                "/api/match/status",
                "/api/match/online-users",
                "/api/match/nearby-users",
            },
        )
        self.assertEqual(
            API.POST_PATHS,
            {"/api/match/online", "/api/match/local"},
        )
        self.assertEqual(API.DISCOVERY_NATIVE_WRITE_PATHS, API.POST_PATHS)
        self.assertIsNone(
            API.dispatch_discovery_native(None, "GET", "/api/not-match", {}, {})
        )
        wrong = API.dispatch_discovery_native(
            _identity(), "POST", "/api/match/status", {}, {}
        )
        self.assertEqual(wrong.status, 405)
        self.assertEqual(wrong.payload["code"], "METHOD_NOT_ALLOWED")

    def test_missing_identity_is_rejected_before_database_access(self) -> None:
        with patch.object(API, "session_scope") as scope:
            response = API.dispatch_discovery_native(
                None, "GET", "/api/match/status", {}, {}
            )

        scope.assert_not_called()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.payload["code"], "discovery_identity_unavailable")

    def test_online_list_uses_local_cards_and_app_compatible_fields(self) -> None:
        identity = _identity("账号-A_9")
        store, service, _owner_profile = _configured(identity)
        peer = DiscoveryCard(
            uuid.uuid4(),
            "用户-乙_7",
            "乙用户",
            "CN-350100",
            "福州",
            "female",
            "Z",
            28,
            True,
        )
        service.online_users.return_value = DiscoveryList(
            items=(peer,),
            filters=DiscoveryFilters("female", "Z", 25, 34),
            scanned_count=4,
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "GET",
                "/api/match/online-users",
                {"gender": "女", "property": "Z", "age": "25-34"},
                {},
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["count"], 1)
        self.assertEqual(response.payload["items"], response.payload["list"])
        item = response.payload["items"][0]
        self.assertEqual(item["id"], "用户-乙_7")
        self.assertEqual(item["uid"], "用户-乙_7")
        self.assertEqual(item["nickname"], "乙用户")
        self.assertEqual(item["gender"], "女")
        self.assertTrue(item["is_online"])
        self.assertEqual(response.payload["source"], "web-local")
        self.assertFalse(response.payload["external_dependency"])

    def test_nearby_ignores_browser_coordinates_and_passes_only_city_level_fields(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        service.nearby_users.return_value = DiscoveryList(
            items=(),
            filters=DiscoveryFilters(
                "any", None, 18, 120, "CN-350100", "福州"
            ),
            scanned_count=0,
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "GET",
                "/api/match/nearby-users",
                {"latitude": "26.08", "longitude": "119.30"},
                {},
            )

        call = service.nearby_users.call_args.kwargs
        self.assertNotIn("latitude", call)
        self.assertNotIn("longitude", call)
        self.assertIsNone(call["city_code"])
        self.assertEqual(response.payload["location_precision"], "city")
        self.assertNotIn("radius_km", response.payload["location"])

    def test_nearby_without_city_returns_location_required_without_using_coordinates(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        service.nearby_users.side_effect = DiscoveryLocationRequired("missing city")
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "GET",
                "/api/match/nearby-users",
                {"latitude": "26", "longitude": "119"},
                {},
            )

        self.assertEqual(response.status, 200)
        self.assertTrue(response.payload["location_required"])
        self.assertEqual(response.payload["items"], [])
        self.assertEqual(response.payload["location_precision"], "city")

    def test_custom_city_requires_capability_and_uses_stable_local_code(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            denied = API.dispatch_discovery_native(
                identity,
                "GET",
                "/api/match/nearby-users",
                {"city": "厦门"},
                {},
            )
        self.assertEqual(denied.status, 403)
        service.nearby_users.assert_not_called()

        identity.nearby_custom_city_enabled = True
        service.nearby_users.return_value = DiscoveryList(
            (),
            DiscoveryFilters(
                "any", None, 18, 120, normalized_city_code("厦门"), "厦门"
            ),
            0,
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            allowed = API.dispatch_discovery_native(
                identity,
                "GET",
                "/api/match/nearby-users",
                {"city": "厦门"},
                {},
            )
        self.assertEqual(allowed.status, 200)
        self.assertEqual(
            service.nearby_users.call_args.kwargs["city_code"],
            normalized_city_code("厦门"),
        )

    def test_explicit_request_id_sets_preference_then_returns_canonical_match(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        store.get_text_match_outcome.return_value = None
        store.get_match_preference.return_value = None
        preference = _preference(identity)
        service.set_match_preference.return_value = preference
        service.request_text_match.return_value = _outcome(
            identity, "browser-request-1", matched=True
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "POST",
                "/api/match/local",
                {},
                {
                    "request_id": "browser-request-1",
                    "gender": "女",
                    "properties": ["双", "Z"],
                },
            )

        preference_call = service.set_match_preference.call_args.kwargs
        self.assertEqual(preference_call["city_scope"], "same-city")
        self.assertEqual(preference_call["gender_preference"], "female")
        self.assertEqual(preference_call["property_preference"], "双")
        self.assertEqual(preference_call["expected_version"], 0)
        self.assertEqual(
            service.request_text_match.call_args.kwargs["request_id"],
            "browser-request-1",
        )
        self.assertTrue(response.payload["matched"])
        self.assertEqual(response.payload["message_peers"], ["用户-乙_7"])
        self.assertTrue(response.payload["canonical_result_saved"])
        self.assertFalse(response.payload["external_dependency"])

    def test_waiting_match_is_not_reported_as_history_save_failure(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        store.get_text_match_outcome.return_value = None
        store.get_match_preference.return_value = None
        service.set_match_preference.return_value = _preference(identity)
        service.request_text_match.return_value = _outcome(
            identity, "waiting-request", matched=False
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "POST",
                "/api/match/online",
                {},
                {"request_id": "waiting-request", "property": "Z"},
            )

        self.assertEqual(response.status, 200)
        self.assertFalse(response.payload["matched"])
        self.assertTrue(response.payload["waiting"])
        self.assertEqual(response.payload["items"], [])
        self.assertIsNone(response.payload["history_saved"])
        self.assertFalse(response.payload["canonical_result_saved"])
        self.assertEqual(response.payload["message"], "已进入本地匹配队列")

    def test_existing_request_replays_without_changing_preference_or_rate_identity(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        existing = _outcome(identity, "stable-request", matched=False, created=False)
        store.get_text_match_outcome.return_value = existing
        store.get_match_preference.return_value = _preference(identity)
        service.request_text_match.return_value = existing
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "POST",
                "/api/match/online",
                {},
                {"request_id": "stable-request", "gender": "女", "property": "Z"},
            )

        service.set_match_preference.assert_not_called()
        service.request_text_match.assert_called_once_with(
            principal=API._principal(identity),
            request_id="stable-request",
        )
        self.assertTrue(response.payload["idempotent_replay"])
        self.assertEqual(response.payload["request_id"], "stable-request")

    def test_existing_request_rejects_changed_match_parameters(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        store.get_text_match_outcome.return_value = _outcome(
            identity, "stable-request", matched=False, created=False
        )
        store.get_match_preference.return_value = _preference(identity)
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "POST",
                "/api/match/local",
                {},
                {"request_id": "stable-request", "gender": "男", "property": "B"},
            )

        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload["code"], "match_request_conflict")
        service.request_text_match.assert_not_called()

    def test_status_reports_shared_local_rate_without_fabricating_voice_quota(self) -> None:
        identity = _identity()
        store, service, profile = _configured(identity)
        store.get_match_preference.return_value = _preference(identity)
        store.match_frequency_status.return_value = {
            "limit": 10,
            "used": 3,
            "remaining": 7,
            "window_seconds": 60,
            "retry_after_seconds": 0,
        }
        store.current_waiting_outcome.return_value = None
        store.latest_match_outcome.return_value = None
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
            patch.object(API, "_now", return_value=NOW),
        ):
            response = API.dispatch_discovery_native(
                identity, "GET", "/api/match/status", {}, {}
            )

        status = response.payload["status"]
        self.assertEqual(status["online_free"], 7)
        self.assertEqual(status["local_free"], 7)
        self.assertEqual(status["rate_limit_scope"], "shared_text_match")
        self.assertNotIn("voice_free", status)
        self.assertNotIn("match_card", status)
        self.assertFalse(response.payload["voice_quota_available"])
        self.assertEqual(response.payload["user"]["city"], profile.city_name)

    def test_status_identifies_the_request_that_produced_latest_match(self) -> None:
        identity = _identity()
        store, service, _profile_value = _configured(identity)
        store.get_match_preference.return_value = _preference(identity)
        store.match_frequency_status.return_value = {
            "limit": 10,
            "used": 1,
            "remaining": 9,
            "window_seconds": 60,
            "retry_after_seconds": 0,
        }
        store.current_waiting_outcome.return_value = None
        store.latest_match_outcome.return_value = _outcome(
            identity, "completed-request", matched=True
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
            patch.object(API, "_now", return_value=NOW),
        ):
            response = API.dispatch_discovery_native(
                identity, "GET", "/api/match/status", {}, {}
            )

        latest = response.payload["latest_match"]
        self.assertEqual(latest["request_id"], "completed-request")
        self.assertEqual(latest["peer"]["id"], "用户-乙_7")

    def test_rate_limit_is_local_429_with_retry_after(self) -> None:
        identity = _identity()
        store, service, _owner_profile = _configured(identity)
        store.get_text_match_outcome.return_value = None
        store.get_match_preference.return_value = None
        service.set_match_preference.return_value = _preference(identity)
        service.request_text_match.side_effect = MatchRateLimited(
            "too frequent", retry_after_seconds=17
        )
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(object())),
            patch.object(API, "SqlAlchemyDiscoveryStore", return_value=store),
            patch.object(API, "DiscoveryNativeService", return_value=service),
        ):
            response = API.dispatch_discovery_native(
                identity,
                "POST",
                "/api/match/online",
                {},
                {"request_id": "limited-request", "property": "Z"},
            )

        self.assertEqual(response.status, 429)
        self.assertEqual(response.payload["code"], "match_rate_limited")
        self.assertEqual(response.payload["retry_after_seconds"], 17)


if __name__ == "__main__":
    unittest.main()
