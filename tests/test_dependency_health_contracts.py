from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bbw_web.dependency_health import (
    DependencyAvailability,
    DependencyCapabilityStatus,
    DependencyErrorKind,
    DependencyFailure,
    DependencyStatusRegistry,
    classify_dependency_error,
)


BASE_TIME = datetime(2026, 7, 25, 10, 30, tzinfo=UTC)


class DependencyErrorClassificationTests(unittest.TestCase):
    def test_common_network_and_http_errors_have_stable_categories(self) -> None:
        timeout = classify_dependency_error(TimeoutError("upstream timed out"))
        self.assertEqual(timeout.kind, DependencyErrorKind.TIMEOUT)
        self.assertTrue(timeout.retryable)

        connection = classify_dependency_error(ConnectionError("connection refused"))
        self.assertEqual(connection.kind, DependencyErrorKind.CONNECTION)
        self.assertTrue(connection.retryable)

        authentication = classify_dependency_error(
            RuntimeError("token rejected"),
            status_code=401,
        )
        self.assertEqual(authentication.kind, DependencyErrorKind.AUTHENTICATION)
        self.assertFalse(authentication.retryable)

        rate_limited = classify_dependency_error(
            RuntimeError("too many requests"),
            status_code=429,
        )
        self.assertEqual(rate_limited.kind, DependencyErrorKind.RATE_LIMITED)
        self.assertTrue(rate_limited.retryable)

        upstream = classify_dependency_error(
            RuntimeError("bad gateway"),
            status_code=502,
        )
        self.assertEqual(upstream.kind, DependencyErrorKind.UPSTREAM)
        self.assertTrue(upstream.retryable)

        contract = classify_dependency_error(
            RuntimeError("endpoint removed"),
            status_code=404,
        )
        self.assertEqual(contract.kind, DependencyErrorKind.CONTRACT)
        self.assertFalse(contract.retryable)

    def test_wrapped_response_status_and_explicit_override_are_supported(self) -> None:
        error = RuntimeError("request failed")
        error.response = SimpleNamespace(status_code=403)  # type: ignore[attr-defined]

        automatic = classify_dependency_error(error)
        self.assertEqual(automatic.kind, DependencyErrorKind.AUTHORIZATION)
        self.assertEqual(automatic.status_code, 403)

        explicit = classify_dependency_error(
            error,
            kind=DependencyErrorKind.CONFIGURATION,
            retryable=True,
        )
        self.assertEqual(explicit.kind, DependencyErrorKind.CONFIGURATION)
        self.assertTrue(explicit.retryable)

    def test_public_failure_dto_never_contains_raw_exception_text_or_http_status(self) -> None:
        secret = "Bearer secret-token-value"
        failure = classify_dependency_error(
            RuntimeError(f"GET https://private.invalid/path failed: {secret}"),
            kind=DependencyErrorKind.CONNECTION,
            status_code=503,
        )

        public = failure.to_public_dto()
        encoded = json.dumps(public, sort_keys=True)

        self.assertEqual(
            public,
            {
                "kind": "connection",
                "code": "DEPENDENCY_CONNECTION_FAILED",
                "retryable": True,
            },
        )
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("503", encoded)


class DependencyCapabilityStatusTests(unittest.TestCase):
    def test_safe_public_dto_contains_provider_domain_state_and_utc_timestamps(self) -> None:
        failure = DependencyFailure(
            DependencyErrorKind.TIMEOUT,
            retryable=True,
            status_code=504,
        )
        status = DependencyCapabilityStatus(
            provider="Banghua",
            domain="Profile.Read",
            availability=DependencyAvailability.DEGRADED,
            checked_at=BASE_TIME + timedelta(milliseconds=125),
            changed_at=BASE_TIME,
            failure=failure,
        )

        self.assertTrue(status.is_degraded)
        self.assertFalse(status.is_available)
        self.assertEqual(
            status.to_public_dto(),
            {
                "provider": "banghua",
                "domain": "profile.read",
                "status": "degraded",
                "checked_at": "2026-07-25T10:30:00.125Z",
                "changed_at": "2026-07-25T10:30:00.000Z",
                "error": {
                    "kind": "timeout",
                    "code": "DEPENDENCY_TIMEOUT",
                    "retryable": True,
                },
            },
        )

    def test_identifiers_and_timestamps_are_validated_before_publication(self) -> None:
        with self.assertRaises(ValueError):
            DependencyCapabilityStatus(
                provider="banghua\nsecret",
                domain="auth",
                availability=DependencyAvailability.AVAILABLE,
                checked_at=BASE_TIME,
                changed_at=BASE_TIME,
            )

        with self.assertRaises(ValueError):
            DependencyCapabilityStatus(
                provider="banghua",
                domain="auth",
                availability=DependencyAvailability.AVAILABLE,
                checked_at=datetime(2026, 7, 25, 10, 30),
                changed_at=BASE_TIME,
            )

        with self.assertRaises(ValueError):
            DependencyCapabilityStatus(
                provider="banghua",
                domain="auth",
                availability=DependencyAvailability.AVAILABLE,
                checked_at=BASE_TIME,
                changed_at=BASE_TIME,
                failure=DependencyFailure(DependencyErrorKind.UNKNOWN, False),
            )


class DependencyStatusRegistryTests(unittest.TestCase):
    def test_repeated_condition_updates_checked_time_but_preserves_changed_time(self) -> None:
        moments = iter(
            [
                BASE_TIME,
                BASE_TIME + timedelta(seconds=10),
                BASE_TIME + timedelta(seconds=20),
                BASE_TIME + timedelta(seconds=30),
            ]
        )
        registry = DependencyStatusRegistry(clock=lambda: next(moments))

        first = registry.mark_available("banghua", "auth")
        second = registry.mark_available("banghua", "auth")
        degraded = registry.mark_failure(
            "banghua",
            "auth",
            TimeoutError("contains private request details"),
            fallback_available=True,
        )
        repeated = registry.mark_failure(
            "banghua",
            "auth",
            TimeoutError("different private details"),
            fallback_available=True,
        )

        self.assertEqual(first.changed_at, BASE_TIME)
        self.assertEqual(second.checked_at, BASE_TIME + timedelta(seconds=10))
        self.assertEqual(second.changed_at, BASE_TIME)
        self.assertEqual(degraded.changed_at, BASE_TIME + timedelta(seconds=20))
        self.assertEqual(repeated.checked_at, BASE_TIME + timedelta(seconds=30))
        self.assertEqual(repeated.changed_at, degraded.changed_at)

    def test_failure_without_fallback_is_unavailable_and_snapshot_is_sorted(self) -> None:
        registry = DependencyStatusRegistry()
        registry.mark_available("tim", "messaging", checked_at=BASE_TIME)
        failed = registry.mark_failure(
            "banghua",
            "auth",
            ConnectionError("https://private.invalid/?token=secret"),
            checked_at=BASE_TIME,
        )

        self.assertTrue(failed.is_unavailable)
        self.assertEqual(
            [(item["provider"], item["domain"], item["status"]) for item in registry.public_snapshot()],
            [
                ("banghua", "auth", "unavailable"),
                ("tim", "messaging", "available"),
            ],
        )
        encoded = json.dumps(registry.public_snapshot(), sort_keys=True)
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn("secret", encoded)

    def test_late_probe_cannot_overwrite_a_newer_status(self) -> None:
        registry = DependencyStatusRegistry()
        current = registry.mark_available(
            "banghua",
            "feed",
            checked_at=BASE_TIME + timedelta(seconds=10),
        )
        late = registry.mark_failure(
            "banghua",
            "feed",
            TimeoutError("late result"),
            checked_at=BASE_TIME,
        )

        self.assertIs(late, current)
        self.assertIs(registry.get("banghua", "feed"), current)
        self.assertEqual(registry.get("BANGHUA", "FEED"), current)


if __name__ == "__main__":
    unittest.main()
