from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from sqlalchemy.dialects import postgresql

from bbw_prod.models import (
    ActivityEvent,
    MatchQueueEntry as MatchQueueEntryModel,
    MatchResult as MatchResultModel,
    Relationship,
)
from bbw_web.discovery_native import (
    MATCH_STATUS_ACTIVE,
    QUEUE_STATUS_MATCHED,
    QUEUE_STATUS_WAITING,
    TEXT_MATCH_KIND,
    DiscoveryAccount,
    DiscoveryPrincipal,
    DiscoveryProfile,
    MatchCandidate,
    MatchPreference,
    TextMatchCommitPlan,
)
from bbw_web.discovery_native.repository import (
    MATCH_FREQUENCY_EVENT_TYPE,
    MESSAGE_POLICY_MATCH_KIND,
    MESSAGE_POLICY_PROVIDER,
    SqlAlchemyDiscoveryStore,
    normalized_city_code,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _Rows:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def all(self):
        return list(self.rows)

    def one(self):
        return self.rows[0]

    def scalar_one(self):
        return self.rows[0] if self.rows else None


class DiscoveryRepositoryContractTests(unittest.TestCase):
    def test_city_code_is_stable_opaque_and_never_contains_coordinates(self) -> None:
        first = normalized_city_code(" 福 州 ")
        second = normalized_city_code("福 州")

        self.assertEqual(first, second)
        self.assertRegex(first, r"^LOCAL-[A-F0-9]{24}$")
        self.assertNotIn("26.", first)
        self.assertIsNone(normalized_city_code(""))

    def test_canonical_profile_extraction_is_city_level_only(self) -> None:
        principal = DiscoveryPrincipal(uuid.uuid4(), uuid.uuid4(), "账号-A")
        user = SimpleNamespace(
            id=principal.user_id,
            status="active",
            disabled_at=None,
            profile={
                "city": "福州",
                "gender": "女",
                "property": "z",
                "age": "29",
                "latitude": 26.08,
                "longitude": 119.30,
            },
        )
        db = SimpleNamespace(scalar=lambda _statement: user)

        values = SqlAlchemyDiscoveryStore(db).canonical_profile_values(principal)

        self.assertEqual(values["city_name"], "福州")
        self.assertEqual(values["city_code"], normalized_city_code("福州"))
        self.assertEqual(values["gender"], "female")
        self.assertEqual(values["profile_property"], "Z")
        self.assertEqual(values["age"], 29)
        self.assertTrue(values["discoverable"])
        self.assertNotIn("latitude", values)
        self.assertNotIn("longitude", values)

    def test_canonical_privacy_opt_out_disables_discovery_projection(self) -> None:
        principal = DiscoveryPrincipal(uuid.uuid4(), uuid.uuid4(), "账号-A")
        user = SimpleNamespace(
            id=principal.user_id,
            status="active",
            disabled_at=None,
            profile={
                "city": "福州",
                "gender": "男",
                "privacy": {"show_online_status": False},
            },
        )
        db = SimpleNamespace(scalar=lambda _statement: user)

        values = SqlAlchemyDiscoveryStore(db).canonical_profile_values(principal)

        self.assertEqual(values["city_code"], normalized_city_code("福州"))
        self.assertFalse(values["discoverable"])

    def test_waiting_candidate_query_locks_only_queue_rows_and_skips_claimed(self) -> None:
        class Capture:
            statement = None

            def execute(self, statement):
                self.statement = statement
                return _Rows()

        db = Capture()
        SqlAlchemyDiscoveryStore(db).list_waiting_text_candidates(
            exclude_user_id=uuid.uuid4(),
            at=NOW,
            limit=200,
        )

        sql = str(
            db.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).upper()
        self.assertIn("FOR UPDATE OF MATCH_QUEUE_ENTRIES SKIP LOCKED", sql)
        self.assertIn("MATCH_QUEUE_ENTRIES.STATUS = 'WAITING'", sql)
        self.assertIn("USERS.STATUS = 'ACTIVE'", sql)
        self.assertIn("USER_DISCOVERY_PROFILES.DISCOVERABLE IS TRUE", sql)
        self.assertIn(
            "MATCH_QUEUE_ENTRIES.PREFERENCE_VERSION = MATCH_PREFERENCES.VERSION",
            sql,
        )

    def test_frequency_is_local_atomic_and_idempotency_event_is_durable(self) -> None:
        class FrequencyDB:
            def __init__(self) -> None:
                self.execute_calls = 0
                self.added = []

            def execute(self, _statement):
                self.execute_calls += 1
                if self.execute_calls == 1:
                    return _Rows([None])
                return _Rows([(3, NOW - timedelta(seconds=20))])

            def scalar(self, _statement):
                return None

            def add(self, value):
                self.added.append(value)

        db = FrequencyDB()
        decision = SqlAlchemyDiscoveryStore(db).consume_match_frequency(
            user_id=uuid.uuid4(),
            request_id="request-one",
            occurred_at=NOW,
            limit=10,
            window_seconds=60,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(len(db.added), 1)
        event = db.added[0]
        self.assertIsInstance(event, ActivityEvent)
        self.assertEqual(event.event_type, MATCH_FREQUENCY_EVENT_TYPE)
        self.assertEqual(event.details["request_id"], "request-one")

    def test_new_request_replaces_only_the_owners_existing_wait(self) -> None:
        owner = DiscoveryAccount(uuid.uuid4(), uuid.uuid4(), "owner", "Owner")
        profile = DiscoveryProfile(
            owner.user_id,
            "CN-350100",
            "福州",
            "male",
            "Z",
            30,
            True,
            NOW,
        )
        preference = MatchPreference(
            owner.user_id, "same-city", "female", "Z", 18, 120, True, 2
        )
        waiting = MatchQueueEntryModel(
            id=uuid.uuid4(),
            public_id=f"mqe_{uuid.uuid4().hex}",
            user_id=owner.user_id,
            idempotency_key="old-request",
            queue_kind=TEXT_MATCH_KIND,
            status=QUEUE_STATUS_WAITING,
            city_code="CN-350100",
            preference_version=1,
            enqueued_at=NOW - timedelta(seconds=20),
            expires_at=NOW + timedelta(seconds=100),
        )

        class QueueDB:
            def __init__(self) -> None:
                self.scalars = [None, waiting]
                self.added = []
                self.flush_count = 0

            def scalar(self, _statement):
                return self.scalars.pop(0)

            def add(self, value):
                self.added.append(value)

            def flush(self):
                self.flush_count += 1

        db = QueueDB()
        created = SqlAlchemyDiscoveryStore(db).enqueue_text_match(
            account=owner,
            profile=profile,
            preference=preference,
            request_id="new-request",
            enqueued_at=NOW,
            expires_at=NOW + timedelta(seconds=120),
        )

        self.assertEqual(waiting.status, "cancelled")
        self.assertEqual(waiting.cancelled_at, NOW)
        self.assertEqual(created.status, QUEUE_STATUS_WAITING)
        self.assertEqual(created.request_id, "new-request")
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.flush_count, 1)

    def test_commit_plan_consumes_both_queues_and_requests_two_grants(self) -> None:
        left = DiscoveryAccount(uuid.uuid4(), uuid.uuid4(), "left", "Left")
        right = DiscoveryAccount(uuid.uuid4(), uuid.uuid4(), "right", "Right")
        if left.user_id.int > right.user_id.int:
            left, right = right, left
        profile_left = DiscoveryProfile(
            left.user_id, "CN-350100", "福州", "male", "Z", 30, True, NOW
        )
        profile_right = DiscoveryProfile(
            right.user_id, "CN-350100", "福州", "female", "Z", 28, True, NOW
        )
        preference_left = MatchPreference(
            left.user_id, "same-city", "female", "Z", 18, 120, True, 1
        )
        preference_right = MatchPreference(
            right.user_id, "same-city", "male", "Z", 18, 120, True, 1
        )
        left_row = MatchQueueEntryModel(
            id=uuid.uuid4(),
            public_id=f"mqe_{uuid.uuid4().hex}",
            user_id=left.user_id,
            idempotency_key="left-request",
            queue_kind=TEXT_MATCH_KIND,
            status=QUEUE_STATUS_WAITING,
            city_code="CN-350100",
            preference_version=1,
            enqueued_at=NOW - timedelta(seconds=20),
            expires_at=NOW + timedelta(seconds=100),
        )
        right_row = MatchQueueEntryModel(
            id=uuid.uuid4(),
            public_id=f"mqe_{uuid.uuid4().hex}",
            user_id=right.user_id,
            idempotency_key="right-request",
            queue_kind=TEXT_MATCH_KIND,
            status=QUEUE_STATUS_WAITING,
            city_code="CN-350100",
            preference_version=1,
            enqueued_at=NOW - timedelta(seconds=10),
            expires_at=NOW + timedelta(seconds=110),
        )
        requester = MatchCandidate(
            left, profile_left, preference_left, SqlAlchemyDiscoveryStore._queue(left_row)
        )
        candidate = MatchCandidate(
            right,
            profile_right,
            preference_right,
            SqlAlchemyDiscoveryStore._queue(right_row),
        )
        plan = TextMatchCommitPlan(
            match_key="text:" + "a" * 64,
            user_low_id=left.user_id,
            user_high_id=right.user_id,
            user_low_queue_entry_id=left_row.id,
            user_high_queue_entry_id=right_row.id,
            initiated_by_user_id=left.user_id,
            authorization_intent_id=uuid.uuid4(),
            matched_at=NOW,
        )

        class CommitDB:
            def __init__(self) -> None:
                self.added = []

            def scalars(self, _statement):
                return [left_row, right_row]

            def scalar(self, _statement):
                return None

            def add(self, value):
                self.added.append(value)

        class Harness(SqlAlchemyDiscoveryStore):
            def __init__(self, db):
                super().__init__(db)
                self.grants = []

            def _active_accounts_for_update(self, _user_ids):
                return {left.user_id: left, right.user_id: right}

            def is_blocked_between(self, _left, _right):
                return False

            def _profiles_for_users(self, _user_ids):
                return {left.user_id: profile_left, right.user_id: profile_right}

            def _message_grant(self, **values):
                self.grants.append(values)

        db = CommitDB()
        store = Harness(db)
        outcome = store.commit_text_match(
            requester=requester,
            candidate=candidate,
            plan=plan,
        )

        self.assertEqual(outcome.status, QUEUE_STATUS_MATCHED)
        self.assertEqual(left_row.status, QUEUE_STATUS_MATCHED)
        self.assertEqual(right_row.status, QUEUE_STATUS_MATCHED)
        self.assertEqual(len(store.grants), 2)
        self.assertEqual(
            {grant["owner"].user_id for grant in store.grants},
            {left.user_id, right.user_id},
        )
        result_rows = [row for row in db.added if isinstance(row, MatchResultModel)]
        self.assertEqual(len(result_rows), 1)
        self.assertEqual(result_rows[0].status, MATCH_STATUS_ACTIVE)

    def test_message_grant_rows_use_web_policy_match_and_server_owned_evidence(self) -> None:
        owner = DiscoveryAccount(uuid.uuid4(), uuid.uuid4(), "owner", "Owner")
        peer = DiscoveryAccount(uuid.uuid4(), uuid.uuid4(), "peer", "Peer")
        result = MatchResultModel(
            id=uuid.uuid4(),
            public_id=f"mch_{uuid.uuid4().hex}",
            match_key="text:" + "b" * 64,
            user_low_id=min(owner.user_id, peer.user_id),
            user_high_id=max(owner.user_id, peer.user_id),
            user_low_queue_entry_id=uuid.uuid4(),
            user_high_queue_entry_id=uuid.uuid4(),
            initiated_by_user_id=owner.user_id,
            status=MATCH_STATUS_ACTIVE,
            matched_at=NOW,
        )
        plan = TextMatchCommitPlan(
            result.match_key,
            result.user_low_id,
            result.user_high_id,
            result.user_low_queue_entry_id,
            result.user_high_queue_entry_id,
            result.initiated_by_user_id,
            uuid.uuid4(),
            NOW,
        )

        class GrantDB:
            def __init__(self) -> None:
                self.added = []

            def scalar(self, _statement):
                return None

            def add(self, value):
                self.added.append(value)

        db = GrantDB()
        SqlAlchemyDiscoveryStore(db)._message_grant(
            owner=owner,
            peer=peer,
            result=result,
            plan=plan,
        )

        self.assertEqual(len(db.added), 1)
        row = db.added[0]
        self.assertIsInstance(row, Relationship)
        self.assertEqual(row.provider, MESSAGE_POLICY_PROVIDER)
        self.assertEqual(row.kind, MESSAGE_POLICY_MATCH_KIND)
        self.assertEqual(row.owner_user_id, owner.user_id)
        self.assertEqual(row.subject_upstream_uid, peer.upstream_uid)
        self.assertTrue(row.extra_data["server_owned"])
        self.assertEqual(row.extra_data["match_result_id"], str(result.id))

    def test_repository_has_no_legacy_protocol_or_network_imports(self) -> None:
        source = (ROOT / "bbw_web" / "discovery_native" / "repository.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            any(
                name.startswith(("bbw_protocol", "httpx", "requests"))
                for name in imported
            )
        )


if __name__ == "__main__":
    unittest.main()
