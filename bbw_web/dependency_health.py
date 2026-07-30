"""External dependency capability state and safe public health DTOs.

This module deliberately has no knowledge of protocol or provider adapters.  It
gives those edges a small, thread-safe vocabulary for reporting whether one
provider can currently serve one product domain.  Raw exceptions never become
part of a public DTO; callers should log them separately at the integration
boundary.
"""

from __future__ import annotations

import json
import math
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from typing import Callable
from urllib.error import HTTPError, URLError


_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_MAX_COMPONENT_LENGTH = 64


class DependencyAvailability(str, Enum):
    """Product-visible availability of one provider/domain capability."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class DependencyErrorKind(str, Enum):
    """Stable, provider-neutral failure categories."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONFIGURATION = "configuration"
    CONTRACT = "contract"
    UPSTREAM = "upstream"
    UNKNOWN = "unknown"


_PUBLIC_ERROR_CODES = {
    DependencyErrorKind.TIMEOUT: "DEPENDENCY_TIMEOUT",
    DependencyErrorKind.CONNECTION: "DEPENDENCY_CONNECTION_FAILED",
    DependencyErrorKind.RATE_LIMITED: "DEPENDENCY_RATE_LIMITED",
    DependencyErrorKind.AUTHENTICATION: "DEPENDENCY_AUTHENTICATION_FAILED",
    DependencyErrorKind.AUTHORIZATION: "DEPENDENCY_AUTHORIZATION_FAILED",
    DependencyErrorKind.CONFIGURATION: "DEPENDENCY_CONFIGURATION_INVALID",
    DependencyErrorKind.CONTRACT: "DEPENDENCY_CONTRACT_CHANGED",
    DependencyErrorKind.UPSTREAM: "DEPENDENCY_UPSTREAM_FAILED",
    DependencyErrorKind.UNKNOWN: "DEPENDENCY_FAILED",
}

_DEFAULT_RETRYABLE = {
    DependencyErrorKind.TIMEOUT: True,
    DependencyErrorKind.CONNECTION: True,
    DependencyErrorKind.RATE_LIMITED: True,
    DependencyErrorKind.AUTHENTICATION: False,
    DependencyErrorKind.AUTHORIZATION: False,
    DependencyErrorKind.CONFIGURATION: False,
    DependencyErrorKind.CONTRACT: False,
    DependencyErrorKind.UPSTREAM: True,
    DependencyErrorKind.UNKNOWN: False,
}


def _component(value: str, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if (
        not normalized
        or len(normalized) > _MAX_COMPONENT_LENGTH
        or _COMPONENT_RE.fullmatch(normalized) is None
    ):
        raise ValueError(f"invalid dependency {field}")
    return normalized


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"dependency {field} must be timezone-aware")
    return value.astimezone(UTC)


def _public_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coerce_status_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _exception_status_code(error: BaseException) -> int | None:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        for candidate in (
            getattr(current, "status_code", None),
            getattr(getattr(current, "response", None), "status_code", None),
            getattr(current, "code", None) if isinstance(current, HTTPError) else None,
        ):
            status = _coerce_status_code(candidate)
            if status is not None:
                return status
        current = current.__cause__ or current.__context__
    return None


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    output: list[BaseException] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        output.append(current)
        current = current.__cause__ or current.__context__
    return tuple(output)


def _looks_like_timeout(error: BaseException) -> bool:
    for current in _exception_chain(error):
        if isinstance(current, (TimeoutError, socket.timeout)):
            return True
        if "timeout" in type(current).__name__.lower():
            return True
        if isinstance(current, URLError) and isinstance(current.reason, TimeoutError):
            return True
    return False


def _looks_like_connection_failure(error: BaseException) -> bool:
    connection_types = (
        ConnectionError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
    )
    for current in _exception_chain(error):
        if isinstance(current, connection_types):
            return True
        if isinstance(current, URLError) and not isinstance(current, HTTPError):
            return True
        name = type(current).__name__.lower()
        if any(token in name for token in ("connecterror", "networkerror", "protocolerror")):
            return True
    return False


@dataclass(frozen=True, slots=True)
class DependencyFailure:
    """Sanitized failure metadata; it intentionally stores no exception text."""

    kind: DependencyErrorKind
    retryable: bool
    status_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DependencyErrorKind(self.kind))
        object.__setattr__(self, "retryable", bool(self.retryable))
        object.__setattr__(self, "status_code", _coerce_status_code(self.status_code))

    @property
    def public_code(self) -> str:
        return _PUBLIC_ERROR_CODES[self.kind]

    def to_public_dto(self) -> dict[str, object]:
        """Return stable fields safe for unauthenticated health responses."""

        return {
            "kind": self.kind.value,
            "code": self.public_code,
            "retryable": self.retryable,
        }


def classify_dependency_error(
    error: BaseException,
    *,
    status_code: int | None = None,
    kind: DependencyErrorKind | str | None = None,
    retryable: bool | None = None,
) -> DependencyFailure:
    """Classify an integration failure without retaining sensitive error text.

    ``kind`` is an explicit escape hatch for provider-specific adapters.  The
    default classifier handles common network and HTTP failures while keeping
    its output provider-neutral.
    """

    if not isinstance(error, BaseException):
        raise TypeError("dependency error must be an exception")

    resolved_status = _coerce_status_code(status_code)
    if resolved_status is None:
        resolved_status = _exception_status_code(error)

    if kind is not None:
        resolved_kind = DependencyErrorKind(kind)
    elif _looks_like_timeout(error) or resolved_status in {408, 504}:
        resolved_kind = DependencyErrorKind.TIMEOUT
    elif resolved_status == 401:
        resolved_kind = DependencyErrorKind.AUTHENTICATION
    elif resolved_status == 403:
        resolved_kind = DependencyErrorKind.AUTHORIZATION
    elif resolved_status == 429:
        resolved_kind = DependencyErrorKind.RATE_LIMITED
    elif resolved_status is not None and resolved_status >= 500:
        resolved_kind = DependencyErrorKind.UPSTREAM
    elif resolved_status is not None and resolved_status >= 400:
        resolved_kind = DependencyErrorKind.CONTRACT
    elif _looks_like_connection_failure(error):
        resolved_kind = DependencyErrorKind.CONNECTION
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        resolved_kind = DependencyErrorKind.CONTRACT
    else:
        resolved_kind = DependencyErrorKind.UNKNOWN

    return DependencyFailure(
        kind=resolved_kind,
        retryable=_DEFAULT_RETRYABLE[resolved_kind] if retryable is None else retryable,
        status_code=resolved_status,
    )


@dataclass(frozen=True, slots=True)
class DependencyCapabilityStatus:
    """Immutable status for one provider serving one product domain."""

    provider: str
    domain: str
    availability: DependencyAvailability
    checked_at: datetime
    changed_at: datetime
    failure: DependencyFailure | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _component(self.provider, field="provider"))
        object.__setattr__(self, "domain", _component(self.domain, field="domain"))
        object.__setattr__(self, "availability", DependencyAvailability(self.availability))
        object.__setattr__(self, "checked_at", _aware_utc(self.checked_at, field="checked_at"))
        object.__setattr__(self, "changed_at", _aware_utc(self.changed_at, field="changed_at"))
        if self.changed_at > self.checked_at:
            raise ValueError("dependency changed_at cannot be later than checked_at")
        if self.availability is DependencyAvailability.AVAILABLE and self.failure is not None:
            raise ValueError("available dependency cannot contain a failure")
        if self.failure is not None and not isinstance(self.failure, DependencyFailure):
            raise TypeError("dependency failure must be DependencyFailure")

    @property
    def is_available(self) -> bool:
        return self.availability is DependencyAvailability.AVAILABLE

    @property
    def is_degraded(self) -> bool:
        return self.availability is DependencyAvailability.DEGRADED

    @property
    def is_unavailable(self) -> bool:
        return self.availability is DependencyAvailability.UNAVAILABLE

    def to_public_dto(self) -> dict[str, object]:
        """Return a deterministic DTO containing no raw provider response."""

        return {
            "provider": self.provider,
            "domain": self.domain,
            "status": self.availability.value,
            "checked_at": _public_timestamp(self.checked_at),
            "changed_at": _public_timestamp(self.changed_at),
            "error": self.failure.to_public_dto() if self.failure is not None else None,
        }


Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


class DependencyDeadlineExceeded(TimeoutError):
    """The interactive request budget was exhausted before another call."""


class RequestDeadline:
    """One monotonic deadline shared by every upstream call in a request.

    Adapters receive this object as an optional call-level override.  Their
    normal defaults remain unchanged for CLI and background jobs.
    """

    __slots__ = ("_clock", "_started_at", "_deadline")

    def __init__(
        self,
        total_seconds: float,
        *,
        clock: MonotonicClock | None = None,
    ) -> None:
        total = float(total_seconds)
        if not math.isfinite(total) or total <= 0:
            raise ValueError("deadline total_seconds must be positive")
        self._clock = clock or time.monotonic
        self._started_at = float(self._clock())
        self._deadline = self._started_at + total

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def deadline(self) -> float:
        return self._deadline

    def elapsed(self) -> float:
        return max(0.0, float(self._clock()) - self._started_at)

    def remaining(self, cap: float | None = None) -> float:
        value = max(0.0, self._deadline - float(self._clock()))
        if cap is None:
            return value
        normalized_cap = float(cap)
        if not math.isfinite(normalized_cap) or normalized_cap <= 0:
            return 0.0
        return min(value, normalized_cap)

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    def timeout(self, cap: float | None = None) -> float:
        remaining = self.remaining(cap)
        if remaining <= 0:
            raise DependencyDeadlineExceeded("interactive dependency deadline exceeded")
        return remaining


class DependencyCircuitOpen(RuntimeError):
    """A retryable dependency circuit is cooling down."""

    retryable = True

    def __init__(self, provider: str, domain: str, retry_after: int) -> None:
        super().__init__(f"dependency circuit open: {provider}/{domain}")
        self.provider = provider
        self.domain = domain
        self.retry_after = max(1, int(retry_after))


@dataclass(slots=True)
class _CircuitState:
    failures: list[float]
    opened_until: float = 0.0
    probe_in_flight: bool = False


class DependencyCircuitBreakerRegistry:
    """Process-wide, thread-safe retryable-failure circuit breakers.

    A single registry is injected into all per-account runtimes.  After the
    cooldown, only one half-open probe is admitted so many browser tabs cannot
    stampede a recovering provider.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        failure_window_seconds: float = 10.0,
        cooldown_seconds: float = 20.0,
        clock: MonotonicClock | None = None,
    ) -> None:
        threshold = int(failure_threshold)
        window = float(failure_window_seconds)
        cooldown = float(cooldown_seconds)
        if threshold < 1:
            raise ValueError("circuit breaker failure_threshold must be positive")
        if not math.isfinite(window) or window <= 0:
            raise ValueError("circuit breaker failure_window_seconds must be positive")
        if not math.isfinite(cooldown) or cooldown <= 0:
            raise ValueError("circuit breaker cooldown_seconds must be positive")
        self.failure_threshold = threshold
        self.failure_window_seconds = window
        self.cooldown_seconds = cooldown
        self._clock = clock or time.monotonic
        self._lock = RLock()
        self._states: dict[tuple[str, str], _CircuitState] = {}

    @staticmethod
    def _key(provider: str, domain: str) -> tuple[str, str]:
        return (
            _component(provider, field="provider"),
            _component(domain, field="domain"),
        )

    def _prune(self, state: _CircuitState, now: float) -> None:
        cutoff = now - self.failure_window_seconds
        state.failures[:] = [stamp for stamp in state.failures if stamp > cutoff]

    def before_call(self, provider: str, domain: str) -> None:
        key = self._key(provider, domain)
        now = float(self._clock())
        with self._lock:
            state = self._states.setdefault(key, _CircuitState([]))
            self._prune(state, now)
            if state.opened_until > now:
                raise DependencyCircuitOpen(
                    key[0],
                    key[1],
                    math.ceil(state.opened_until - now),
                )
            if state.opened_until > 0:
                if state.probe_in_flight:
                    raise DependencyCircuitOpen(key[0], key[1], 1)
                state.probe_in_flight = True

    def record_success(self, provider: str, domain: str) -> None:
        key = self._key(provider, domain)
        with self._lock:
            state = self._states.setdefault(key, _CircuitState([]))
            state.failures.clear()
            state.opened_until = 0.0
            state.probe_in_flight = False

    def record_failure(
        self,
        provider: str,
        domain: str,
        *,
        retryable: bool = True,
    ) -> None:
        key = self._key(provider, domain)
        if not retryable:
            # A reachable business/contract rejection must not poison the
            # transport circuit and also completes a possible half-open probe.
            self.record_success(*key)
            return
        now = float(self._clock())
        with self._lock:
            state = self._states.setdefault(key, _CircuitState([]))
            self._prune(state, now)
            state.failures.append(now)
            if state.probe_in_flight or len(state.failures) >= self.failure_threshold:
                state.opened_until = now + self.cooldown_seconds
            state.probe_in_flight = False

    def retry_after(self, provider: str, domain: str) -> int:
        key = self._key(provider, domain)
        now = float(self._clock())
        with self._lock:
            state = self._states.get(key)
            if state is None or state.opened_until <= now:
                return 0
            return max(1, math.ceil(state.opened_until - now))

    def snapshot(self) -> dict[tuple[str, str], dict[str, object]]:
        now = float(self._clock())
        with self._lock:
            output: dict[tuple[str, str], dict[str, object]] = {}
            for key, state in self._states.items():
                self._prune(state, now)
                retry_after = (
                    max(1, math.ceil(state.opened_until - now))
                    if state.opened_until > now
                    else 0
                )
                output[key] = {
                    "open": retry_after > 0,
                    "retry_after": retry_after,
                    "recent_failures": len(state.failures),
                    "probe_in_flight": state.probe_in_flight,
                }
            return output


class DependencyStatusRegistry:
    """Thread-safe in-memory registry suitable for future health endpoints."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._statuses: dict[tuple[str, str], DependencyCapabilityStatus] = {}

    @staticmethod
    def _condition(status: DependencyCapabilityStatus) -> tuple[object, ...]:
        failure = status.failure
        return (
            status.availability,
            failure.kind if failure is not None else None,
            failure.retryable if failure is not None else None,
            failure.status_code if failure is not None else None,
        )

    def record(
        self,
        provider: str,
        domain: str,
        availability: DependencyAvailability | str,
        *,
        failure: DependencyFailure | None = None,
        checked_at: datetime | None = None,
    ) -> DependencyCapabilityStatus:
        observed_at = _aware_utc(
            checked_at if checked_at is not None else self._clock(),
            field="checked_at",
        )
        normalized_provider = _component(provider, field="provider")
        normalized_domain = _component(domain, field="domain")
        normalized_availability = DependencyAvailability(availability)
        key = (normalized_provider, normalized_domain)

        with self._lock:
            previous = self._statuses.get(key)
            if previous is not None and observed_at < previous.checked_at:
                return previous

            provisional = DependencyCapabilityStatus(
                provider=normalized_provider,
                domain=normalized_domain,
                availability=normalized_availability,
                checked_at=observed_at,
                changed_at=observed_at,
                failure=failure,
            )
            changed_at = (
                previous.changed_at
                if previous is not None and self._condition(previous) == self._condition(provisional)
                else observed_at
            )
            status = DependencyCapabilityStatus(
                provider=normalized_provider,
                domain=normalized_domain,
                availability=normalized_availability,
                checked_at=observed_at,
                changed_at=changed_at,
                failure=failure,
            )
            self._statuses[key] = status
            return status

    def mark_available(
        self,
        provider: str,
        domain: str,
        *,
        checked_at: datetime | None = None,
    ) -> DependencyCapabilityStatus:
        return self.record(
            provider,
            domain,
            DependencyAvailability.AVAILABLE,
            checked_at=checked_at,
        )

    def mark_degraded(
        self,
        provider: str,
        domain: str,
        *,
        failure: DependencyFailure | None = None,
        checked_at: datetime | None = None,
    ) -> DependencyCapabilityStatus:
        return self.record(
            provider,
            domain,
            DependencyAvailability.DEGRADED,
            failure=failure,
            checked_at=checked_at,
        )

    def mark_unavailable(
        self,
        provider: str,
        domain: str,
        *,
        failure: DependencyFailure | None = None,
        checked_at: datetime | None = None,
    ) -> DependencyCapabilityStatus:
        return self.record(
            provider,
            domain,
            DependencyAvailability.UNAVAILABLE,
            failure=failure,
            checked_at=checked_at,
        )

    def mark_failure(
        self,
        provider: str,
        domain: str,
        error: BaseException,
        *,
        fallback_available: bool = False,
        status_code: int | None = None,
        kind: DependencyErrorKind | str | None = None,
        retryable: bool | None = None,
        checked_at: datetime | None = None,
    ) -> DependencyCapabilityStatus:
        failure = classify_dependency_error(
            error,
            status_code=status_code,
            kind=kind,
            retryable=retryable,
        )
        return self.record(
            provider,
            domain,
            (
                DependencyAvailability.DEGRADED
                if fallback_available
                else DependencyAvailability.UNAVAILABLE
            ),
            failure=failure,
            checked_at=checked_at,
        )

    def get(self, provider: str, domain: str) -> DependencyCapabilityStatus | None:
        key = (
            _component(provider, field="provider"),
            _component(domain, field="domain"),
        )
        with self._lock:
            return self._statuses.get(key)

    def snapshot(self) -> tuple[DependencyCapabilityStatus, ...]:
        with self._lock:
            return tuple(self._statuses[key] for key in sorted(self._statuses))

    def public_snapshot(self) -> list[dict[str, object]]:
        return [status.to_public_dto() for status in self.snapshot()]
