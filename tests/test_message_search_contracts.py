from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from bbw_prod.repositories import MessageRepository, _message_search_index_pattern
from bbw_web.archive_api import _message_search_day_window


ROOT = Path(__file__).resolve().parents[1]


class MessageSearchBackendTests(unittest.TestCase):
    def test_windows_timezone_database_dependency_is_declared(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("tzdata>=2025.2,<2027", requirements)

    def test_short_message_queries_have_extractable_trigram_patterns(self) -> None:
        self.assertEqual(_message_search_index_pattern("A"), "%x61%")
        self.assertEqual(_message_search_index_pattern("报"), "%xe6x8axa5%")
        self.assertEqual(
            _message_search_index_pattern("报告"),
            "%xe6x8axa5xe5x91x8a%",
        )

    def test_search_date_uses_china_calendar_day_with_utc_bounds(self) -> None:
        start, end = _message_search_day_window("2026-07-23")

        self.assertEqual(start, datetime(2026, 7, 22, 16, 0, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 7, 23, 16, 0, tzinfo=UTC))

    def test_invalid_search_date_is_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            _message_search_day_window("2026-02-30")

    def test_extreme_search_dates_are_rejected_instead_of_overflowing(self) -> None:
        for value in ("0001-01-01", "9999-12-31"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                _message_search_day_window(value)
            self.assertEqual(raised.exception.status_code, 400)

    def test_around_query_honors_limit_one(self) -> None:
        occurred_at = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalars(self, statement: object) -> list[SimpleNamespace]:
                self.statements.append(statement)
                return [
                    SimpleNamespace(
                        occurred_at=occurred_at,
                        created_at=occurred_at,
                    )
                ]

        db = FakeDb()
        rows = MessageRepository(db).list_around_conversation(
            uuid.uuid4(),
            uuid.uuid4(),
            around=occurred_at,
            limit=1,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(db.statements), 1)
        sql = str(
            db.statements[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("LIMIT 1", sql)

    def test_message_search_uses_one_bounded_query_and_page_counts(self) -> None:
        first_conversation = uuid.uuid4()
        second_conversation = uuid.uuid4()

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalars(self, statement: object) -> list[SimpleNamespace]:
                self.statements.append(statement)
                return [
                    SimpleNamespace(conversation_id=first_conversation),
                    SimpleNamespace(conversation_id=first_conversation),
                    SimpleNamespace(conversation_id=second_conversation),
                ]

            def execute(self, _statement: object) -> None:
                raise AssertionError("搜索不应执行第二次无界计数查询")

        db = FakeDb()
        rows, group_counts, has_more = MessageRepository(db).search_for_owner(
            uuid.uuid4(),
            query="报",
            limit=2,
        )

        self.assertEqual(len(db.statements), 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(group_counts, {first_conversation: 2})
        self.assertTrue(has_more)
        sql = str(
            db.statements[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("LIMIT 3", sql)
        self.assertIn("message_search_index_text(messages.body) LIKE", sql)
        self.assertIn("message_search_media_name(messages.metadata) LIKE", sql)
        self.assertIn("message_search_quote_text(messages.metadata) LIKE", sql)
        self.assertIn("xe6x8axa5", sql)
        self.assertNotIn("metadata['media_report']", sql)
        self.assertNotIn("metadata['quote']", sql)
        self.assertNotIn("GROUP BY", sql)

    def test_link_filter_only_checks_message_body(self) -> None:
        class FakeDb:
            def __init__(self) -> None:
                self.statement: object | None = None

            def scalars(self, statement: object) -> list[object]:
                self.statement = statement
                return []

        db = FakeDb()
        MessageRepository(db).search_for_owner(
            uuid.uuid4(),
            kind="link",
            limit=2,
        )

        self.assertIsNotNone(db.statement)
        sql = str(
            db.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("message_search_index_text(messages.body) LIKE", sql)
        self.assertIn("x68x74x74x70", sql)
        self.assertNotIn("message_search_media_name", sql)
        self.assertNotIn("message_search_quote_text", sql)

    def test_peer_search_can_cover_all_provider_conversations(self) -> None:
        first_conversation = uuid.uuid4()
        second_conversation = uuid.uuid4()

        class FakeDb:
            def __init__(self) -> None:
                self.statement: object | None = None

            def scalars(self, statement: object) -> list[object]:
                self.statement = statement
                return []

        db = FakeDb()
        MessageRepository(db).search_for_owner(
            uuid.uuid4(),
            conversation_ids=[first_conversation, second_conversation],
            query="迁移",
            limit=20,
        )

        self.assertIsNotNone(db.statement)
        compiled = db.statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("messages.conversation_id IN", sql)
        self.assertEqual(
            compiled.params["conversation_id_1"],
            [first_conversation, second_conversation],
        )

    def test_message_search_trigram_indexes_have_a_concurrent_migration(self) -> None:
        migration = (
            ROOT
            / "migrations"
            / "versions"
            / "20260723_0007_message_search_trigram_indexes.py"
        ).read_text(encoding="utf-8")

        self.assertIn('revision: str = "20260723_0007"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260722_0006"',
            migration,
        )
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm", migration)
        self.assertEqual(migration.count("gin_trgm_ops"), 3)
        self.assertIn("message_search_index_text(body) gin_trgm_ops", migration)
        self.assertIn("message_search_media_name(metadata) gin_trgm_ops", migration)
        self.assertIn("message_search_quote_text(metadata) gin_trgm_ops", migration)
        self.assertIn("convert_to(lower(coalesce(value, '')), 'UTF8')", migration)
        self.assertNotIn("metadata['media_report']", migration)
        self.assertNotIn("metadata['quote']", migration)
        self.assertIn("CREATE INDEX CONCURRENTLY", migration)
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", migration)

    def test_message_search_wrapper_functions_are_schema_qualified(self) -> None:
        migration = (
            ROOT
            / "migrations"
            / "versions"
            / "20260724_0008_schema_qualify_message_search_functions.py"
        ).read_text(encoding="utf-8")

        self.assertIn('revision: str = "20260724_0008"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260723_0007"',
            migration,
        )
        self.assertEqual(migration.count("SELECT {helper}("), 2)
        self.assertIn('"public.message_search_index_text"', migration)


class MessageSearchFrontendContracts(unittest.TestCase):
    def _app_js(self) -> str:
        return (ROOT / "bbw_web" / "static" / "app.js").read_text(
            encoding="utf-8-sig"
        )

    def test_global_and_conversation_search_share_one_search_surface(self) -> None:
        app_js_path = ROOT / "bbw_web" / "static" / "app.js"
        app_css_path = ROOT / "bbw_web" / "static" / "app.css"
        index_path = ROOT / "bbw_web" / "static" / "index.html"
        app_js = app_js_path.read_text(encoding="utf-8-sig")
        app_css = app_css_path.read_text(encoding="utf-8-sig")
        index_html = index_path.read_text(encoding="utf-8-sig")

        for marker in (
            'data-action="open-global-message-search"',
            'data-action="open-conversation-message-search"',
            "function messageSearchGroups()",
            "async function jumpToMessageSearchResult",
            "/api/archive/search?",
            "around: new Date(timestamp).toISOString()",
            "is-search-target",
            "function setMessageSearchBackgroundInactive",
            "layout.inert = disabled",
            'layout.setAttribute("aria-hidden", "true")',
            'setMessageSearchBackgroundInactive(false)',
            "inert aria-hidden=",
            'role="dialog" aria-modal="true"',
        ):
            self.assertIn(marker, app_js)
        for marker in (
            ".message-search-view",
            ".message-search-group",
            ".message-search-result",
            ".chat-message-row.is-search-target .chat-line",
        ):
            self.assertIn(marker, app_css)

        app_hash = hashlib.sha256(app_js_path.read_bytes()).hexdigest()[:16]
        css_hash = hashlib.sha256(app_css_path.read_bytes()).hexdigest()[:16]
        self.assertIn(f'/static/app.js?v={app_hash}', index_html)
        self.assertIn(f'/static/app.css?v={css_hash}', index_html)

    def test_archive_media_filename_survives_frontend_normalization(self) -> None:
        source = self._app_js()
        conversion = source.split(
            "function messageSearchEntryFromPayload(item)", 1
        )[1].split("\nfunction localMessageSearchResults", 1)[0]

        self.assertIn(
            'const mediaName = String(item?.media?.name || "").trim();',
            conversion,
        )
        self.assertIn(
            "entry.media = { ...(entry.media || {}), name: mediaName };",
            conversion,
        )

    def test_search_preview_prefers_the_field_that_contains_the_query(self) -> None:
        source = self._app_js()
        preview = source.split(
            "function messageSearchEntryPreview(entry, query = S.messageSearchQuery)",
            1,
        )[1].split("\nfunction messageSearchDateValue", 1)[0]

        self.assertIn("candidate.searchable.toLocaleLowerCase", preview)
        self.assertIn("if (matched) return matched.preview;", preview)
        self.assertIn("preview: quoteText ? `引用：${quoteText}`", preview)

    def test_link_filter_matches_backend_body_only_semantics(self) -> None:
        source = self._app_js()
        matching = source.split("function messageMatchesSearch(entry, peer = \"\")", 1)[
            1
        ].split("\nfunction messageSearchEntryFromPayload", 1)[0]

        self.assertIn(
            '!/https?:\\/\\//i.test(String(entry?.text || ""))',
            matching,
        )
        self.assertNotIn("test(searchable)", matching)

    def test_search_target_identity_prefers_exact_message_id(self) -> None:
        source = self._app_js()
        identity = source.split(
            'function chatMessageRowByIdentity({ id = "", messageRandom = "", sequence = "" } = {})',
            1,
        )[1].split("\nfunction highlightSearchTargetMessage", 1)[0]

        id_match = identity.index("row.dataset.messageId === targetId")
        random_match = identity.index("row.dataset.messageRandom === targetRandom")
        sequence_match = identity.index("row.dataset.messageSequence === targetSequence")
        self.assertLess(id_match, sequence_match)
        self.assertLess(sequence_match, random_match)

    def test_search_jump_cancels_pending_auto_bottom_before_positioning(self) -> None:
        source = self._app_js()
        scrolling = source.split("function cancelChatLogAutoScroll", 1)[1].split(
            "\nfunction chatLogIsNearBottom", 1
        )[0]
        jump = source.split("async function jumpToMessageSearchResult", 1)[1].split(
            "\nfunction clearConversationBatchDeleteConfirmation", 1
        )[0]

        self.assertIn("CHAT_LOG_AUTO_SCROLL_GENERATIONS.get(log) !== generation", scrolling)
        self.assertIn("refreshChatLog({ suppressBottom: true });", jump)
        frame_wait = jump.index("await new Promise((resolve) => requestAnimationFrame(resolve));")
        cancellation = jump.index("cancelChatLogAutoScroll();", frame_wait)
        positioning = jump.index(
            'highlightSearchTargetMessage(identity, { behavior: "auto" })',
            cancellation,
        )
        self.assertLess(frame_wait, cancellation)
        self.assertLess(cancellation, positioning)


if __name__ == "__main__":
    unittest.main()
