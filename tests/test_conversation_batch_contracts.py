from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, call, patch

from sqlalchemy.dialects import postgresql

from bbw_prod.repositories import ConversationRepository
from bbw_web.jobs import (
    _conversation_candidate,
    _conversation_candidate_changes,
    _ingest_message,
    _merge_conversation_candidates,
    _upsert_conversations,
    ingest_history_response,
    ingest_history_responses_batch,
)
from bbw_web.persistence import (
    HISTORY_CONVERSATION_REFRESH_SECONDS,
    _history_response_digest,
)


ROOT = Path(__file__).resolve().parents[1]


class ConversationBatchTests(unittest.TestCase):
    def test_conversation_history_digest_ignores_volatile_time_within_refresh_bucket(
        self,
    ) -> None:
        first = {
            "ok": True,
            "items": [
                {
                    "peer_id": "1001",
                    "unread_count": 2,
                    "unread_observed_at": 240.0,
                }
            ],
        }
        second = {
            "ok": True,
            "items": [
                {
                    "peer_id": "1001",
                    "unread_count": 2,
                    "unread_observed_at": 359.0,
                }
            ],
        }

        first_digest = _history_response_digest(
            "/api/im/conversations", {"page": "1"}, first, observed_at=240.0
        )
        self.assertEqual(
            first_digest,
            _history_response_digest(
                "/api/im/conversations", {"page": "1"}, second, observed_at=359.0
            ),
        )
        changed = {**second, "items": [{**second["items"][0], "unread_count": 3}]}
        self.assertNotEqual(
            first_digest,
            _history_response_digest(
                "/api/im/conversations", {"page": "1"}, changed, observed_at=359.0
            ),
        )
        self.assertNotEqual(
            first_digest,
            _history_response_digest(
                "/api/im/conversations",
                {"page": "1"},
                second,
                observed_at=240.0 + HISTORY_CONVERSATION_REFRESH_SECONDS,
            ),
        )
        self.assertNotEqual(
            _history_response_digest(
                "/api/im/messages", {"peer": "1001"}, {"ok": True, "items": []}
            ),
            _history_response_digest(
                "/api/im/messages", {"peer": "1002"}, {"ok": True, "items": []}
            ),
        )

    def test_history_response_batch_reuses_binding_and_transaction(self) -> None:
        owner_id = uuid.uuid4()
        account_id = uuid.uuid4()
        db = Mock()
        user = SimpleNamespace(id=owner_id)
        account = SimpleNamespace(id=account_id, upstream_uid="2002")

        @contextmanager
        def fake_session_scope():
            yield db

        responses = [
            {
                "path": "/api/im/conversations",
                "query": {"page": "1"},
                "response_data": {"ok": True, "items": []},
            },
            {
                "path": "/api/im/messages",
                "query": {"peer": "1001"},
                "response_data": {"ok": True, "items": []},
            },
        ]
        with (
            patch("bbw_web.jobs.session_scope", fake_session_scope),
            patch("bbw_web.jobs.get_settings", return_value=SimpleNamespace()),
            patch(
                "bbw_web.jobs._load_owner_binding", return_value=(user, account)
            ) as load_binding,
            patch(
                "bbw_web.jobs._ingest_history_response_in_session",
                side_effect=[
                    {
                        "ok": True,
                        "conversations": 3,
                        "messages_created": 0,
                        "messages_existing": 0,
                    },
                    {
                        "ok": True,
                        "conversations": 0,
                        "messages_created": 2,
                        "messages_existing": 1,
                    },
                ],
            ) as ingest_response,
            patch("bbw_web.jobs._dispatch_media_outboxes") as dispatch_media,
        ):
            result = ingest_history_responses_batch(
                str(owner_id), str(account_id), responses
            )

        load_binding.assert_called_once_with(db, owner_id, account_id)
        self.assertEqual(ingest_response.call_count, 2)
        self.assertIs(
            ingest_response.call_args_list[0].kwargs["outbox_ids"],
            ingest_response.call_args_list[1].kwargs["outbox_ids"],
        )
        dispatch_media.assert_not_called()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["conversations"], 3)
        self.assertEqual(result["messages_created"], 2)
        self.assertEqual(result["messages_existing"], 1)

    def test_duplicate_candidates_keep_fresh_preview_and_unread_state(self) -> None:
        owner_id = uuid.uuid4()
        older = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        newer = older + timedelta(minutes=5)
        fresh = _conversation_candidate(
            owner_user_id=owner_id,
            peer_uid="1001",
            reported_id="C2C1001",
            unread_count=3,
            unread_observed_at=newer,
            last_message_at=newer,
            metadata={"last_message": "new", "avatar": "old-avatar"},
        )
        stale = _conversation_candidate(
            owner_user_id=owner_id,
            peer_uid="1001",
            reported_id="C2C1001",
            unread_count=9,
            unread_observed_at=older,
            last_message_at=older,
            metadata={"last_message": "old", "avatar": "new-avatar"},
        )

        merged = _merge_conversation_candidates(fresh, stale)

        self.assertEqual(merged["unread_count"], 3)
        self.assertEqual(merged["unread_observed_at"], newer)
        self.assertEqual(merged["last_message_at"], newer)
        self.assertEqual(merged["extra_data"]["last_message"], "new")
        self.assertEqual(merged["extra_data"]["avatar"], "new-avatar")

    def test_placeholder_conversation_title_is_not_persisted(self) -> None:
        owner_id = uuid.uuid4()

        for title in ("游客", "用户", "1001", "用户 1001"):
            with self.subTest(title=title):
                candidate = _conversation_candidate(
                    owner_user_id=owner_id,
                    peer_uid="1001",
                    reported_id="C2C1001",
                    title=title,
                )

                self.assertIsNone(candidate["title"])

    def test_unchanged_candidate_skips_database_update(self) -> None:
        owner_id = uuid.uuid4()
        observed_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        candidate = _conversation_candidate(
            owner_user_id=owner_id,
            peer_uid="1001",
            reported_id="C2C1001",
            title="用户",
            unread_count=2,
            unread_observed_at=observed_at,
            last_message_at=observed_at,
            metadata={"last_message": "hello", "last_source": "history"},
        )
        existing = SimpleNamespace(
            upstream_conversation_id="C2C1001",
            peer_upstream_uid="1001",
            kind="direct",
            title="用户",
            unread_count=2,
            unread_observed_at=observed_at,
            last_message_at=observed_at,
            extra_data={"last_message": "hello", "last_source": "history"},
        )
        repository = Mock()
        repository.list_by_upstream_ids.return_value = {"C2C1001": existing}
        repository.upsert_many.return_value = []

        with patch("bbw_web.jobs.ConversationRepository", return_value=repository):
            resolved = _upsert_conversations(
                Mock(), owner_user_id=owner_id, candidates=[candidate]
            )

        self.assertIs(resolved["C2C1001"], existing)
        repository.list_by_upstream_ids.assert_called_once()
        repository.upsert_many.assert_called_once_with([])
        self.assertFalse(_conversation_candidate_changes(existing, candidate))

    def test_duplicate_batch_keys_are_written_once(self) -> None:
        owner_id = uuid.uuid4()
        older = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        newer = older + timedelta(minutes=5)
        candidates = [
            _conversation_candidate(
                owner_user_id=owner_id,
                peer_uid="1001",
                reported_id="C2C1001",
                last_message_at=older,
                metadata={"last_message": "old"},
            ),
            _conversation_candidate(
                owner_user_id=owner_id,
                peer_uid="1001",
                reported_id="C2C1001",
                last_message_at=newer,
                metadata={"last_message": "new"},
            ),
        ]
        written = SimpleNamespace(upstream_conversation_id="C2C1001")
        repository = Mock()
        repository.list_by_upstream_ids.return_value = {}
        repository.upsert_many.return_value = [written]

        with patch("bbw_web.jobs.ConversationRepository", return_value=repository):
            resolved = _upsert_conversations(
                Mock(), owner_user_id=owner_id, candidates=candidates
            )

        rows = repository.upsert_many.call_args.args[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_message_at"], newer)
        self.assertEqual(rows[0]["extra_data"]["last_message"], "new")
        self.assertIs(resolved["C2C1001"], written)

    def test_message_batch_can_reuse_resolved_conversation(self) -> None:
        owner_id = uuid.uuid4()
        conversation = SimpleNamespace(id=uuid.uuid4())
        message = SimpleNamespace(id=uuid.uuid4())
        repository = Mock()
        repository.insert_idempotent.return_value = (message, True)
        occurred_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        report = {
            "peer_uid": "1001",
            "direction": "incoming",
            "upstream_message_id": "custom-message-id",
            "source": "history",
            "sent_at": "invalid-time-that-must-not-be-parsed",
            "message_type": "text",
            "text": "hello",
        }

        with (
            patch("bbw_web.jobs._upsert_conversation") as upsert_conversation,
            patch("bbw_web.jobs._parse_time") as parse_time,
            patch("bbw_web.jobs.MessageRepository", return_value=repository),
        ):
            row, created, outbox_id = _ingest_message(
                Mock(),
                settings=SimpleNamespace(message_retention_days=180),
                user=SimpleNamespace(id=owner_id, chat_retention_days=180),
                account=SimpleNamespace(upstream_uid="2002"),
                report=report,
                conversation=conversation,
                occurred_at=occurred_at,
            )

        self.assertIs(row, message)
        self.assertTrue(created)
        self.assertIsNone(outbox_id)
        upsert_conversation.assert_not_called()
        parse_time.assert_not_called()
        self.assertEqual(
            repository.insert_idempotent.call_args.kwargs["occurred_at"], occurred_at
        )

    def test_history_message_page_resolves_conversation_once(self) -> None:
        owner_id = uuid.uuid4()
        account_id = uuid.uuid4()
        db = Mock()
        user = SimpleNamespace(id=owner_id, chat_retention_days=180)
        account = SimpleNamespace(id=account_id, upstream_uid="2002")
        conversation = SimpleNamespace(id=uuid.uuid4())
        observed_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC).isoformat()
        reports = [
            {
                "peer_uid": "1001",
                "direction": "incoming",
                "upstream_message_id": f"message-{index}",
                "source": "history",
                "sent_at": observed_at,
                "message_type": "text",
                "text": f"message {index}",
            }
            for index in range(100)
        ]

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.jobs.session_scope", fake_session_scope),
            patch("bbw_web.jobs.get_settings", return_value=SimpleNamespace()),
            patch("bbw_web.jobs._load_owner_binding", return_value=(user, account)),
            patch("bbw_web.jobs._envelope_items", return_value=list(range(100))),
            patch("bbw_web.jobs._history_message_report", side_effect=reports),
            patch(
                "bbw_web.jobs._upsert_conversations",
                return_value={"C2C1001": conversation},
            ) as upsert_conversations,
            patch(
                "bbw_web.jobs._ingest_message",
                return_value=(SimpleNamespace(), True, None),
            ) as ingest_message,
        ):
            result = ingest_history_response(
                str(owner_id),
                str(account_id),
                "/api/im/messages",
                {"peer": "1001"},
                {"ok": True, "items": list(range(100))},
            )

        upsert_conversations.assert_called_once()
        self.assertEqual(len(upsert_conversations.call_args.kwargs["candidates"]), 100)
        self.assertEqual(ingest_message.call_count, 100)
        self.assertTrue(
            all(
                call.kwargs["conversation"] is conversation
                for call in ingest_message.call_args_list
            )
        )
        self.assertEqual(result["messages_created"], 100)

    def test_history_message_fallback_time_is_reused_for_conversation_and_message(
        self,
    ) -> None:
        owner_id = uuid.uuid4()
        account_id = uuid.uuid4()
        db = Mock()
        user = SimpleNamespace(id=owner_id, chat_retention_days=180)
        account = SimpleNamespace(id=account_id, upstream_uid="2002")
        conversation = SimpleNamespace(id=uuid.uuid4())
        missing_fallback = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        invalid_fallback = missing_fallback + timedelta(microseconds=1)
        reports = [
            {
                "peer_uid": "1001",
                "direction": "incoming",
                "upstream_message_id": "missing-time",
                "source": "history",
                "message_type": "text",
                "text": "missing",
            },
            {
                "peer_uid": "1001",
                "direction": "incoming",
                "upstream_message_id": "invalid-time",
                "source": "history",
                "sent_at": "not-a-time",
                "message_type": "text",
                "text": "invalid",
            },
        ]

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.jobs.session_scope", fake_session_scope),
            patch("bbw_web.jobs.get_settings", return_value=SimpleNamespace()),
            patch("bbw_web.jobs._load_owner_binding", return_value=(user, account)),
            patch("bbw_web.jobs._envelope_items", return_value=[0, 1]),
            patch("bbw_web.jobs._history_message_report", side_effect=reports),
            patch(
                "bbw_web.jobs._parse_time",
                side_effect=[missing_fallback, invalid_fallback],
            ) as parse_time,
            patch(
                "bbw_web.jobs._upsert_conversations",
                return_value={"C2C1001": conversation},
            ) as upsert_conversations,
            patch(
                "bbw_web.jobs._ingest_message",
                return_value=(SimpleNamespace(), True, None),
            ) as ingest_message,
        ):
            ingest_history_response(
                str(owner_id),
                str(account_id),
                "/api/im/messages",
                {"peer": "1001"},
                {"ok": True, "items": [0, 1]},
            )

        self.assertEqual(parse_time.call_args_list, [call(None), call("not-a-time")])
        candidates = upsert_conversations.call_args.kwargs["candidates"]
        self.assertIs(candidates[0]["last_message_at"], missing_fallback)
        self.assertIs(candidates[1]["last_message_at"], invalid_fallback)
        self.assertIs(
            ingest_message.call_args_list[0].kwargs["occurred_at"],
            missing_fallback,
        )
        self.assertIs(
            ingest_message.call_args_list[1].kwargs["occurred_at"],
            invalid_fallback,
        )

    def test_repository_compiles_multi_row_conflict_update(self) -> None:
        owner_id = uuid.uuid4()
        statement = ConversationRepository._upsert_statement(
            [
                {
                    "owner_user_id": owner_id,
                    "provider": "tim",
                    "upstream_conversation_id": "C2C1001",
                    "peer_upstream_uid": "1001",
                    "kind": "direct",
                    "title": None,
                    "unread_count": 0,
                    "unread_observed_at": None,
                    "last_message_at": None,
                    "extra_data": {},
                }
            ]
        )

        sql = str(statement.compile(dialect=postgresql.dialect()))

        self.assertIn("ON CONFLICT", sql)
        self.assertIn("unread_observed_at IS NOT NULL", sql)
        self.assertIn("coalesce(excluded.title, conversations.title)", sql.lower())

    def test_message_sequence_index_migration_is_concurrent(self) -> None:
        migration = (
            ROOT
            / "migrations"
            / "versions"
            / "20260722_0006_message_sequence_lookup_index.py"
        ).read_text(encoding="utf-8")

        self.assertIn('revision: str = "20260722_0006"', migration)
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "20260720_0005"', migration)
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", migration)
        self.assertIn("CREATE INDEX CONCURRENTLY", migration)
        self.assertIn("metadata ->> 'message_sequence'", migration)


if __name__ == "__main__":
    unittest.main()
