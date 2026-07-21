from __future__ import annotations

import ast
import sys
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web import bff_server as BFF  # noqa: E402
from bbw_protocol.client import ApiResult  # noqa: E402
from bbw_web.match_history import (  # noqa: E402
    MATCH_HISTORY_PROVIDER,
    MATCH_HISTORY_RETENTION_DAYS,
    load_match_history,
    record_match_history_response,
)
from bbw_web.jobs import _purge_expired_match_history  # noqa: E402


class MatchHistoryBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = BFF.STORE
        BFF.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        BFF.STORE = self.old_store

    @staticmethod
    def _run(path: str, loader=None):
        web_user = SimpleNamespace(app=SimpleNamespace())

        class Harness:
            def __init__(self) -> None:
                self.path = path
                self.response = None
                self._request_match_history_loader = loader

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid-match-history"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        BFF.Handler.do_GET(harness)
        return harness.response

    def test_history_loader_receives_validated_page(self) -> None:
        pages: list[int] = []

        def loader(page: int):
            pages.append(page)
            return {
                "ok": True,
                "items": [{"id": "9", "match_mode": "online"}],
                "count": 1,
                "page": page,
                "next_page": None,
                "has_more": False,
            }

        status, payload = self._run("/api/match/history?page=2", loader)

        self.assertEqual(status, 200)
        self.assertEqual(pages, [2])
        self.assertEqual(payload["items"][0]["id"], "9")

    def test_invalid_history_page_is_rejected(self) -> None:
        status, payload = self._run("/api/match/history?page=0", lambda _page: {})

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_legacy_runtime_without_database_loader_returns_empty_history(self) -> None:
        status, payload = self._run("/api/match/history")

        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        self.assertFalse(payload["has_more"])

    def test_memory_only_runtime_records_online_match_history(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"id":"9","nickname":"本地用户"}',
            data={"id": "9", "nickname": "本地用户", "city": "福州"},
        )

        class Match:
            def set_filter(self, _value):
                return ApiResult(True, 200, "true", data=True)

            def online_one(self, **_params):
                return result

        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(
                    uid="42",
                    raw_user={
                        "gender": "男",
                        "property": "Z",
                        "match_gender": "不限",
                        "match_property": "双",
                    },
                ),
                match=Match(),
            ),
            lock=threading.RLock(),
            match_history=[],
        )

        class PostHarness:
            path = "/api/match/online"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return {"gender": "不限", "properties": ["双"]}

            def sid(self):
                return "sid-match-history"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        post = PostHarness()
        BFF.Handler.do_POST(post)
        self.assertEqual(post.response[0], 200)
        self.assertTrue(post.response[1]["history_saved"])
        self.assertEqual(len(web_user.match_history), 1)

        class GetHarness(PostHarness):
            path = "/api/match/history?page=1"

        get = GetHarness()
        BFF.Handler.do_GET(get)
        self.assertEqual(get.response[1]["count"], 1)
        self.assertEqual(get.response[1]["items"][0]["id"], "9")
        self.assertEqual(get.response[1]["items"][0]["match_mode"], "online")

    def test_history_persistence_failure_is_reported_without_hiding_match(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"id":"9","nickname":"匹配用户"}',
            data={"id": "9", "nickname": "匹配用户"},
        )

        class Match:
            def set_filter(self, _value):
                return ApiResult(True, 200, "true", data=True)

            def online_one(self, **_params):
                return result

        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(
                    uid="42",
                    raw_user={
                        "gender": "男",
                        "property": "Z",
                        "match_gender": "不限",
                        "match_property": "双",
                    },
                ),
                match=Match(),
            ),
            lock=threading.RLock(),
            match_history=[],
        )

        class Harness:
            path = "/api/match/online"

            def __init__(self):
                self.response = None
                self._request_match_history_recorder = self.failed_recorder

            @staticmethod
            def failed_recorder(_path, _payload):
                raise RuntimeError("database unavailable")

            def _check_api_origin(self):
                return True

            def body(self):
                return {"gender": "不限", "properties": ["双"]}

            def sid(self):
                return "sid-match-history"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        BFF.Handler.do_POST(harness)
        status, payload = harness.response

        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["id"], "9")
        self.assertFalse(payload["history_saved"])
        self.assertIn("匹配成功", payload["history_warning"])

    def test_voice_match_records_memory_history_and_reports_durable_failure(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"id":"9","nickname":"语音用户"}',
            data={"id": "9", "nickname": "语音用户", "city": "厦门"},
        )
        match = SimpleNamespace(
            start_voice=lambda **_kwargs: result,
            normalize_voice_result=lambda value: {
                "outcome": "matched",
                "target": value.data,
            },
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42"), match=match),
            native=SimpleNamespace(),
            lock=threading.RLock(),
            match_history=[],
            match_message_peers=set(),
            voice_rong_credentials={
                "app_key": "app-key",
                "user_id": "42",
                "token": "rong-token",
                "nickname": "Me",
                "portrait": "",
            },
            voice_rong_credentials_at=BFF.time.time(),
            voice_match_state={},
        )

        class Harness:
            path = "/api/match/voice/start"

            def __init__(self):
                self.response = None
                self._request_match_history_recorder = self.failed_recorder

            @staticmethod
            def failed_recorder(_path, _payload):
                raise RuntimeError("database unavailable")

            def _check_api_origin(self):
                return True

            def body(self):
                return {}

            def sid(self):
                return "sid-match-history"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        BFF.Handler.do_POST(harness)
        status, payload = harness.response

        self.assertEqual(status, 200)
        self.assertFalse(payload["history_saved"])
        self.assertIn("匹配成功", payload["history_warning"])
        self.assertEqual(web_user.match_history[0]["id"], "9")
        self.assertEqual(web_user.match_history[0]["match_mode"], "voice")


class MatchHistoryPersistenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner_user_id = uuid.uuid4()

    def test_successful_matches_are_sanitized_and_idempotent_per_request(self) -> None:
        inserted: list[dict[str, object]] = []
        keys: set[str] = set()

        class FakeRepository:
            def insert_idempotent(self, **values):
                key = str(values["upstream_event_id"])
                if key not in keys:
                    keys.add(key)
                    inserted.append(values)
                return SimpleNamespace(id=uuid.uuid4())

        @contextmanager
        def fake_session_scope():
            yield object()

        response = {
            "ok": True,
            "items": [
                {"id": "9", "nickname": "测试用户", "token": "secret"},
                {"id": "9", "nickname": "重复用户"},
                {"id": "42", "nickname": "当前用户"},
            ],
            "filters": {"gender": "不限", "property": "双"},
        }
        with (
            patch("bbw_web.match_history.session_scope", fake_session_scope),
            patch("bbw_web.match_history.ActivityEventRepository", return_value=FakeRepository()),
        ):
            first = record_match_history_response(
                owner_user_id=self.owner_user_id,
                upstream_uid="42",
                method="POST",
                path="/api/match/online",
                response_data=response,
                status=200,
                request_id="request-1",
            )
            second = record_match_history_response(
                owner_user_id=self.owner_user_id,
                upstream_uid="42",
                method="POST",
                path="/api/match/online",
                response_data=response,
                status=200,
                request_id="request-1",
            )
            third = record_match_history_response(
                owner_user_id=self.owner_user_id,
                upstream_uid="42",
                method="POST",
                path="/api/match/online",
                response_data=response,
                status=200,
                request_id="request-2",
            )

        self.assertEqual(first, ["9"])
        self.assertEqual(second, ["9"])
        self.assertEqual(third, ["9"])
        self.assertEqual(len(inserted), 2)
        self.assertEqual(inserted[0]["event_type"], "match.online")
        self.assertEqual(inserted[0]["subject_upstream_uid"], "9")
        self.assertNotIn("token", inserted[0]["details"]["profile"])

    def test_history_query_returns_owner_scoped_page_metadata(self) -> None:
        row = SimpleNamespace(
            id=uuid.uuid4(),
            subject_upstream_uid="9",
            occurred_at=datetime(2026, 7, 17, 12, 30, tzinfo=UTC),
            details={
                "mode": "voice",
                "source_path": "/api/match/voice/start",
                "profile": {"id": "9", "nickname": "语音用户"},
                "filters": {},
            },
        )

        class FakeDb:
            def scalar(self, _statement):
                return 2

            def scalars(self, _statement):
                return [row]

        @contextmanager
        def fake_session_scope():
            yield FakeDb()

        with patch("bbw_web.match_history.session_scope", fake_session_scope):
            payload = load_match_history(
                owner_user_id=self.owner_user_id,
                page=1,
                page_size=1,
            )

        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_page"], 2)
        self.assertEqual(payload["items"][0]["id"], "9")
        self.assertEqual(payload["items"][0]["match_mode"], "voice")
        self.assertEqual(payload["items"][0]["matched_at"], "2026-07-17T12:30:00+00:00")

    def test_cleanup_purges_only_expired_match_history_in_bounded_batches(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]

        class FakeDb:
            def __init__(self):
                self.select_statement = None
                self.delete_statement = None

            def scalars(self, statement):
                self.select_statement = statement
                return ids

            def execute(self, statement):
                self.delete_statement = statement
                return SimpleNamespace(rowcount=len(ids))

        now = datetime(2026, 7, 17, 12, 30, tzinfo=UTC)
        db = FakeDb()

        deleted = _purge_expired_match_history(db, at=now, limit=5000)

        self.assertEqual(deleted, 2)
        self.assertIsNotNone(db.select_statement)
        self.assertIsNotNone(db.delete_statement)
        params = db.select_statement.compile().params
        self.assertIn(MATCH_HISTORY_PROVIDER, params.values())
        self.assertIn(
            now - timedelta(days=MATCH_HISTORY_RETENTION_DAYS),
            params.values(),
        )
        self.assertIn("activity_events", str(db.delete_statement))


class MatchHistoryFrontendContractTests(unittest.TestCase):
    def test_production_wiring_uses_owner_scoped_activity_events(self) -> None:
        history_module = (ROOT / "bbw_web" / "match_history.py").read_text(encoding="utf-8-sig")
        persistence = (ROOT / "bbw_web" / "persistence.py").read_text(encoding="utf-8-sig")
        api = (ROOT / "bbw_web" / "api.py").read_text(encoding="utf-8-sig")
        bff = (ROOT / "bbw_web" / "bff_server.py").read_text(encoding="utf-8-sig")
        ast.parse(history_module)
        ast.parse(persistence)
        ast.parse(api)

        for marker in (
            'MATCH_HISTORY_PROVIDER = "web-match"',
            '"/api/match/online": ("match.online", "online")',
            '"/api/match/local": ("match.local", "local")',
            '"/api/match/voice/start": ("match.voice", "voice")',
            "def record_match_history_response(",
            "def load_match_history(",
            "ActivityEvent.owner_user_id == owner_user_id",
            "repo = ActivityEventRepository(db)",
            "repo.insert_idempotent(",
        ):
            self.assertIn(marker, history_module)
        self.assertIn("return record_match_history_response(", persistence)
        self.assertIn("return load_match_history(", persistence)
        self.assertIn("persistence.remember_match_history_response(", api)
        self.assertIn("persistence.match_history(", api)
        self.assertIn('"/api/match/voice/start",', api)
        self.assertIn("_remember_web_match_history(u, path, payload)", bff)
        self.assertIn('payload["history_saved"] = False', bff)

    def test_matching_page_loads_and_updates_durable_history(self) -> None:
        app_js = (ROOT / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8-sig")
        app_css = (ROOT / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8-sig")
        index_html = (ROOT / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8-sig")

        matching = app_js.split("async function pageMatching", 1)[1].split(
            "async function pageVoiceMatch", 1
        )[0]
        self.assertIn('api("/api/match/history?page=1"', matching)
        self.assertIn("Promise.allSettled", matching)
        self.assertIn("匹配历史", matching)
        self.assertIn('id="match-history"', matching)
        self.assertIn('data-action="match-history-refresh"', matching)
        self.assertIn('data-action="match-history-load-more"', app_js)
        self.assertIn("loadMatchHistory(1)", app_js)
        self.assertIn("data.history_saved === false", app_js)
        self.assertIn("匹配结果已保留在当前页面", app_js)
        self.assertIn(".match-history-card", app_css)
        self.assertIn(".match-history-mode", app_css)
        css_version = index_html.split('/static/app.css?v=', 1)[1].split('"', 1)[0]
        js_version = index_html.split('/static/app.js?v=', 1)[1].split('"', 1)[0]
        self.assertEqual(css_version, js_version)
        self.assertIn("-match-history", css_version)


if __name__ == "__main__":
    unittest.main()
