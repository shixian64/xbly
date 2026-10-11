"""Offline session revocation races; locks/MVCC are modeled, not PostgreSQL."""
from __future__ import annotations

from copy import copy
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import uuid

from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from bbw_prod.models import WebSession
from bbw_prod.repositories import WebSessionRepository
from bbw_prod.security import SessionTokenManager
from bbw_prod.services import UserSessionService


NOW = datetime(2026, 10, 11, tzinfo=UTC)
OWNER = uuid.UUID(int=10)
SETTINGS = SimpleNamespace(redis_prefix="offline", user_idle_ttl_seconds=3600)


def session_row(index: int = 1):
    return SimpleNamespace(
        id=uuid.UUID(int=index), user_id=OWNER, external_account_id=uuid.UUID(int=20),
        sid_hash=SessionTokenManager.hash_sid(f"fake-session-{index}"),
        auth_source="provider", created_at=NOW, last_seen_at=NOW,
        idle_expires_at=NOW + timedelta(hours=1),
        absolute_expires_at=NOW + timedelta(days=1), revoked_at=None,
    )


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kwargs):
        self.values[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)


class CommittedStore:
    def __init__(self):
        # Reverse insertion order catches missing deterministic bulk-lock order.
        self.rows = {row.sid_hash: row for row in (session_row(2), session_row(1))}
        self.locks = {sid_hash: Lock() for sid_hash in self.rows}


class ControlledTransaction:
    """Interpret actual repository SELECT options using READ COMMITTED snapshots.

    A row lock remains held through commit, not merely through scalar(). SELECT
    without a lock sees the last committed row even during an in-flight revoke.
    """
    def __init__(self, store):
        self.store = store
        self.local = {}
        self.held = []
        self.dirty = False
        self.attempted = Event()
        self.blocked = Event()
        self.queries = 0

    def _row(self, key, statement):
        self.queries += 1
        if statement._for_update_arg is not None and key not in self.held:
            lock = self.store.locks[key]
            if not lock.acquire(blocking=False):
                self.blocked.set()
                self.attempted.set()
                if not lock.acquire(timeout=3):
                    raise AssertionError("modeled row lock was not released")
            self.held.append(key)
        self.attempted.set()
        if key not in self.local or statement.get_execution_options().get("populate_existing"):
            self.local[key] = copy(self.store.rows[key])
        return self.local[key]

    def scalar(self, statement):
        key = statement._where_criteria[0].right.value
        if key not in self.store.rows:
            return None
        return self._row(key, statement)

    def scalars(self, statement):
        owner = statement._where_criteria[0].right.value
        rows = [row for row in self.store.rows.values() if row.user_id == owner
                and row.revoked_at is None and row.idle_expires_at > NOW
                and row.absolute_expires_at > NOW]
        if tuple(statement._order_by_clauses):
            rows.sort(key=lambda row: row.id)
        result = []
        for row in rows:
            current = self._row(row.sid_hash, statement)
            # PostgreSQL rechecks WHERE after waiting for an updated row.
            if current.revoked_at is None:
                result.append(current)
        return result

    def flush(self):
        self.dirty = True

    def commit(self):
        if self.dirty:
            for key, row in self.local.items():
                self.store.rows[key] = copy(row)
        while self.held:
            self.store.locks[self.held.pop()].release()
        self.dirty = False


class UserSessionRevocationTests(TestCase):
    def setUp(self):
        self.store = CommittedStore()
        self.redis = MemoryRedis()
        for target in ("bbw_prod.services.utcnow", "bbw_prod.repositories.utcnow"):
            clock = patch(target, return_value=NOW)
            clock.start()
            self.addCleanup(clock.stop)

    def service(self, transaction):
        return UserSessionService(transaction, self.redis, SETTINGS, b"x" * 32)

    def revoke(self, service, bulk):
        if bulk:
            return service.revoke_all_for_user(OWNER, reason="offline-test")
        return service.revoke("fake-session-1", reason="offline-test", now=NOW)

    def run_later(self, transaction, operation):
        results, errors = [], []
        def run():
            try:
                results.append(operation())
            except BaseException as error:
                errors.append(error)
            finally:
                transaction.commit()
        thread = Thread(target=run, daemon=True)
        thread.start()
        return thread, results, errors

    def assert_revoked(self, bulk):
        observer = ControlledTransaction(self.store)
        try:
            for index in ((1, 2) if bulk else (1,)):
                service = self.service(observer)
                self.assertIsNone(service.recover(f"fake-session-{index}", now=NOW))
                self.assertIsNone(service.touch(f"fake-session-{index}", now=NOW))
            if not bulk:
                self.assertIsNotNone(self.service(observer).recover("fake-session-2", now=NOW))
        finally:
            observer.commit()

    def check_recover_before_revoke(self, bulk):
        first, later = ControlledTransaction(self.store), ControlledTransaction(self.store)
        recovered = self.service(first).recover("fake-session-1", now=NOW)
        self.assertIsNotNone(recovered)
        thread, results, errors = self.run_later(later, lambda: self.revoke(self.service(later), bulk))
        try:
            self.assertTrue(later.attempted.wait(2))
            self.assertTrue(later.blocked.is_set(), "revoke must await cache writer commit")
        finally:
            first.commit()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [2 if bulk else True])
        self.assert_revoked(bulk)

    def check_revoke_before_recover(self, bulk):
        first, later = ControlledTransaction(self.store), ControlledTransaction(self.store)
        self.revoke(self.service(first), bulk)
        thread, results, errors = self.run_later(
            later, lambda: self.service(later).recover("fake-session-1", now=NOW))
        try:
            self.assertTrue(later.attempted.wait(2))
            self.assertTrue(later.blocked.is_set(), "recover must await revocation commit")
        finally:
            first.commit()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [None])
        self.assert_revoked(bulk)

    def test_recover_before_single_revoke(self):
        self.check_recover_before_revoke(False)

    def test_recover_before_bulk_revoke(self):
        self.check_recover_before_revoke(True)

    def test_single_revoke_before_recover(self):
        self.check_revoke_before_recover(False)

    def test_bulk_revoke_before_recover(self):
        self.check_revoke_before_recover(True)

    def test_bulk_revoke_acquires_session_locks_in_stable_id_order(self):
        transaction = ControlledTransaction(self.store)
        try:
            self.revoke(self.service(transaction), True)
            self.assertEqual(transaction.held, [session_row(i).sid_hash for i in (1, 2)])
        finally:
            transaction.commit()

    def test_normal_recovery_keeps_cache_fast_path(self):
        first = ControlledTransaction(self.store)
        try:
            recovered = self.service(first).recover("fake-session-1", now=NOW)
            self.assertEqual(recovered.session_id, uuid.UUID(int=1))
            self.assertEqual(first.queries, 1)
        finally:
            first.commit()
        cached = ControlledTransaction(self.store)
        service = self.service(cached)
        self.assertEqual(service.recover("fake-session-1", now=NOW), recovered)
        self.assertEqual(service.touch("fake-session-1", now=NOW), recovered)
        self.assertEqual(cached.queries, 0)


    def test_malformed_session_cookie_is_unauthenticated_without_storage_access(self):
        for raw_sid in ("", "caf\u00e9", "\u7528\u6237", "bad\ud800", None, 42):
            for method, expected in (("recover", None), ("touch", None), ("revoke", False)):
                transaction = ControlledTransaction(self.store)
                service = self.service(transaction)
                with self.subTest(sid=repr(raw_sid), method=method), patch.object(
                    self.redis, "get", side_effect=AssertionError("invalid SID reached Redis")
                ):
                    self.assertIs(getattr(service, method)(raw_sid, now=NOW), expected)
                    self.assertEqual(transaction.queries, 0)

    def test_short_legacy_ascii_sid_still_recovers(self):
        row = session_row()
        row.sid_hash = SessionTokenManager.hash_sid("x")
        self.store.rows[row.sid_hash] = row
        self.store.locks[row.sid_hash] = Lock()
        transaction = ControlledTransaction(self.store)
        try:
            state = self.service(transaction).recover("x", now=NOW)
            self.assertEqual(state.session_id, row.id)
        finally:
            transaction.commit()


class SessionIdentityMapRefreshTests(TestCase):
    """Actual SQLAlchemy identity-map semantics; SQLite does not test PG locking."""
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        WebSession.__table__.create(self.engine)
        self.values = vars(session_row())
        # SQLite treats an all-digit UUID as numeric; keep UUID fixture text.
        self.values["id"] = uuid.UUID("a0000000-0000-0000-0000-000000000001")
        self.values["external_account_id"] = uuid.UUID("b0000000-0000-0000-0000-000000000001")
        with self.engine.begin() as connection:
            connection.execute(WebSession.__table__.insert().values(**self.values))
        self.db = Session(self.engine, autoflush=False, expire_on_commit=False)
        self.addCleanup(self.db.close)
        self.repo = WebSessionRepository(self.db)
        self.loaded = self.repo.get_by_hash(self.values["sid_hash"])

    def test_locking_lookup_refreshes_previously_loaded_session(self):
        with self.engine.begin() as connection:
            connection.execute(update(WebSession).where(WebSession.id == self.loaded.id)
                               .values(revoked_at=NOW))
        self.assertIsNone(self.loaded.revoked_at)
        refreshed = self.repo.get_by_hash(self.values["sid_hash"], for_update=True)
        self.assertIs(refreshed, self.loaded)
        self.assertIsNotNone(refreshed.revoked_at)

    def test_locked_bulk_lookup_refreshes_previously_loaded_session(self):
        expected = NOW + timedelta(minutes=1)
        with self.engine.begin() as connection:
            connection.execute(update(WebSession).where(WebSession.id == self.loaded.id)
                               .values(last_seen_at=expected))
        refreshed = self.repo.active_for_user(OWNER, at=NOW, for_update=True)
        self.assertEqual(len(refreshed), 1)
        self.assertIs(refreshed[0], self.loaded)
        self.assertEqual(refreshed[0].last_seen_at.replace(tzinfo=UTC), expected)
