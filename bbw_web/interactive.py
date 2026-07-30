"""Bounded process-level coordinators for interactive legacy integrations."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from bbw_web.dependency_health import RequestDeadline


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProfileLookupBatch:
    """Completed and deferred profile lookups for one bounded HTTP request."""

    completed: Mapping[str, Optional[dict[str, Any]]]
    pending: tuple[str, ...]
    saturated: tuple[str, ...] = ()


class ProfileLookupCoordinator:
    """Process-wide bounded executor with per-account/UID single-flight."""

    def __init__(self, *, max_workers: int = 8, max_pending: int = 32) -> None:
        workers = max(1, int(max_workers))
        capacity = max(workers, int(max_pending))
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="bbw-profile-lookup",
        )
        self._capacity = threading.BoundedSemaphore(capacity)
        self._lock = threading.RLock()
        self._flights: dict[tuple[str, str], Future[Optional[dict[str, Any]]]] = {}
        self._closed = False

    def _release(
        self,
        key: tuple[str, str],
        future: Future[Optional[dict[str, Any]]],
    ) -> None:
        with self._lock:
            if self._flights.get(key) is future:
                self._flights.pop(key, None)
        self._capacity.release()

    def _future(
        self,
        account_key: str,
        uid: str,
        fetcher: Callable[[str], Optional[dict[str, Any]]],
    ) -> tuple[Future[Optional[dict[str, Any]]] | None, bool]:
        key = (str(account_key or "").strip(), str(uid or "").strip())
        with self._lock:
            existing = self._flights.get(key)
            if existing is not None:
                return existing, False
            if self._closed or not self._capacity.acquire(blocking=False):
                return None, False
            try:
                future = self._executor.submit(fetcher, key[1])
            except Exception:
                self._capacity.release()
                raise
            self._flights[key] = future
            future.add_done_callback(
                lambda completed, flight_key=key: self._release(flight_key, completed)
            )
            return future, True

    def fetch_many(
        self,
        *,
        account_key: str,
        uids: list[str],
        fetcher: Callable[[str], Optional[dict[str, Any]]],
        budget_seconds: float,
        max_sync: int,
    ) -> ProfileLookupBatch:
        targets = list(
            dict.fromkeys(
                str(uid or "").strip()
                for uid in uids
                if str(uid or "").strip()
            )
        )
        admitted = targets[: max(0, int(max_sync))]
        deferred = list(targets[len(admitted) :])
        futures: dict[str, Future[Optional[dict[str, Any]]]] = {}
        saturated: list[str] = []
        for uid in admitted:
            future, _created = self._future(account_key, uid, fetcher)
            if future is None:
                saturated.append(uid)
                continue
            futures[uid] = future

        if futures:
            deadline = RequestDeadline(max(0.001, float(budget_seconds)))
            wait(tuple(futures.values()), timeout=deadline.remaining())

        completed: dict[str, Optional[dict[str, Any]]] = {}
        pending = list(deferred)
        for uid, future in futures.items():
            if not future.done():
                pending.append(uid)
                continue
            try:
                value = future.result()
            except Exception:
                pending.append(uid)
                continue
            completed[uid] = dict(value) if isinstance(value, Mapping) else None
        pending.extend(saturated)
        return ProfileLookupBatch(
            completed=completed,
            pending=tuple(dict.fromkeys(pending)),
            saturated=tuple(saturated),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


PresenceCallback = Callable[[], Any]


@dataclass(slots=True)
class _PresenceState:
    pending: dict[str, PresenceCallback] = field(default_factory=dict)
    running: bool = False


class PresenceCoordinator:
    """Serialize and coalesce presence writes per account off the HTTP path."""

    def __init__(self, *, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="bbw-presence",
        )
        self._lock = threading.Lock()
        self._states: dict[str, _PresenceState] = {}
        self._closed = False

    def submit(
        self,
        account_key: str,
        operation: str,
        callback: PresenceCallback,
    ) -> bool:
        key = str(account_key or "").strip()
        name = str(operation or "").strip().lower()
        if not key or not name or not callable(callback):
            return False
        with self._lock:
            if self._closed:
                return False
            state = self._states.setdefault(key, _PresenceState())
            # The latest front/back state wins; duplicate heartbeat ticks are
            # also collapsed while one account already has work queued.
            state.pending[name] = callback
            if state.running:
                return True
            state.running = True
            try:
                self._executor.submit(self._drain, key)
            except Exception:
                state.running = False
                state.pending.pop(name, None)
                if not state.pending:
                    self._states.pop(key, None)
                return False
        return True

    def _drain(self, account_key: str) -> None:
        while True:
            with self._lock:
                state = self._states.get(account_key)
                if state is None:
                    return
                if not state.pending or self._closed:
                    state.running = False
                    if not state.pending:
                        self._states.pop(account_key, None)
                    return
                # Dict replacement preserves the operation's original slot,
                # which is enough to keep front/back and heartbeat serialized.
                operation, callback = next(iter(state.pending.items()))
                state.pending.pop(operation, None)
            try:
                callback()
            except Exception:
                LOGGER.exception(
                    "presence update failed; account=%s operation=%s",
                    account_key[:128],
                    operation[:64],
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for state in self._states.values():
                state.pending.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
