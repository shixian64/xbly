from __future__ import annotations

import io
import json
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from bbw_web.moments_native.legacy_migration import (
    BanghuaMomentsReader,
    BatchImportSummary,
    ImportSummary,
    ImportWindow,
    ImportedPostRef,
    LegacyMomentsAccount,
    LegacyMomentsAuthenticationError,
    LegacyMomentsAuthenticationRejected,
    LegacyMomentsDataError,
    LegacyMomentsImportOrchestrator,
    LegacyMomentsLimitError,
    LegacyMomentsProviderError,
    SqlAlchemyLegacyMomentsWriter,
    _list_active_owner_ids,
    _runtime,
    main,
    run_all_active_legacy_moments_imports,
    run_legacy_moments_import,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def account() -> LegacyMomentsAccount:
    return LegacyMomentsAccount(
        owner_user_id=uuid.uuid4(),
        external_account_id=uuid.uuid4(),
        upstream_uid="42",
        display_name="迁移用户",
        profile={},
        device_data={},
        login_encrypted={},
        password_encrypted={},
        token_encrypted={},
        token_expires_at=None,
    )


def post_item(*, post_id: str, created_at: datetime, topics=()):
    return {
        "id": post_id,
        "author_id": "42",
        "nickname": "迁移用户",
        "title": "历史动态",
        "content": "正文",
        "pictures": ["https://media.invalid/a.jpg"],
        "video": "",
        "cover": "",
        "plate": "动态",
        "topics": list(topics),
        "like_count": 3,
        "comment_count": 2,
        "time": created_at.isoformat(),
        "comment_forbid": False,
        "hide_comment": False,
        "visibility_scope": "公开",
        "is_pinned": False,
    }


def comment_item(
    *, comment_id: str, post_id: str, created_at: datetime, main_id: str = ""
):
    return {
        "id": comment_id,
        "post_id": post_id,
        "author_id": "84",
        "nickname": "评论用户",
        "avatar": "",
        "content": f"评论 {comment_id}",
        "time": created_at.isoformat(),
        "like_count": 1,
        "is_forbidden": False,
        "main_id": main_id,
        "sub_id": "",
        "reply_to_name": "",
        "reply_count": 0,
    }


class _Reader:
    def __init__(self, *, posts, comments=None, fail_posts=False):
        self.posts = posts
        self.comments = comments or {}
        self.fail_posts = fail_posts
        self.events = []

    def fetch_post_page(self, page):
        self.events.append(("network-post", page))
        if self.fail_posts:
            raise LegacyMomentsProviderError("provider_down")
        return self.posts.get(page, ())

    def fetch_comment_page(
        self,
        *,
        post_upstream_id,
        author_upstream_uid,
        hide_comments,
        page,
    ):
        self.events.append(
            (
                "network-comment",
                post_upstream_id,
                author_upstream_uid,
                hide_comments,
                page,
            )
        )
        return self.comments.get((post_upstream_id, page), ())


class _Writer:
    def __init__(self, events):
        self.events = events
        self.posts = []
        self.comments = []
        self.failed = []
        self.completed = False

    def begin(self, _account, window: ImportWindow):
        self.events.append(("db-begin", window.coverage_started_at))

    def import_posts(self, _account, _window, posts, *, page):
        self.events.append(("db-posts", page, tuple(item.upstream_id for item in posts)))
        self.posts.extend(posts)
        return len(posts)

    def list_imported_posts(self, _account, _window):
        self.events.append(("db-list-posts",))
        return tuple(
            ImportedPostRef(
                local_public_id=f"pst_{item.upstream_id:0>32}"[-36:],
                upstream_id=item.upstream_id,
                author_upstream_uid=item.author_upstream_uid,
                hide_comments=item.hide_comments,
            )
            for item in self.posts
        )

    def import_comments(self, _account, _window, post, comments):
        self.events.append(
            ("db-comments", post.upstream_id, tuple(item.upstream_id for item in comments))
        )
        self.comments.extend(comments)
        return len(comments)

    def progress(self, _account, _window, **values):
        self.events.append(("db-progress", values["phase"]))

    def complete(self, _account, _window):
        self.events.append(("db-complete",))
        self.completed = True
        topics = {topic for item in self.posts for topic in item.topics}
        return (
            {
                "posts": len(self.posts),
                "comments": len(self.comments),
                "topics": len(topics),
            },
            "a" * 64,
        )

    def fail(self, _account, _window, *, code):
        self.events.append(("db-fail", code))
        self.failed.append(code)


class _Cipher:
    def __init__(self, events=None):
        self.events = events if events is not None else []

    def decrypt_text(self, encrypted, *, purpose, context):
        del purpose, context
        if encrypted.get("error"):
            raise ValueError("cipher failure")
        return encrypted.get("plain", "")

    def encrypt_text(self, value, *, purpose, context):
        del purpose, context
        self.events.append(("encrypt-token",))
        return {"ciphertext": f"encrypted:{len(str(value))}"}


class _AuthSession:
    def __init__(self, *, uid="0", token="0", phone=""):
        self.uid = uid
        self.token = token
        self.phone = phone
        self.password = ""
        self.nickname = "迁移用户"
        self.user_role = "普通用户"
        self.rp_verify_time = "1"
        self.vip = "0"
        self.svip = "0"
        self.money = "0"
        self.portrait = ""
        self.user_sign = ""
        self.login_id = ""

    @property
    def logged_in(self):
        return self.uid not in {"", "0"} and self.token not in {"", "0"}

    def device_dict(self):
        return {"device_id": "migration-device", "phonebrand": "Android"}


class _AuthProvider:
    provider_id = "beibeiwu"

    def __init__(self, result, *, events=None, transaction_state=None):
        self.result = result
        self.events = events if events is not None else []
        self.transaction_state = transaction_state or {"active": False}
        self.close_calls = 0

    def create_runtime_from_state(self, state):
        session = _AuthSession(uid=state.uid, token=state.token, phone=state.phone)
        provider = self

        class Auth:
            def login_password(self, _login, _password):
                if provider.transaction_state["active"]:
                    raise AssertionError("network authentication held a DB transaction")
                provider.events.append(("network-auth",))
                if isinstance(provider.result, Exception):
                    raise provider.result
                if getattr(provider.result, "authenticate", False):
                    session.uid = "42"
                    session.token = "refreshed-token"
                return provider.result

        class Client:
            def close(self):
                provider.close_calls += 1

        app = SimpleNamespace(
            session=session,
            auth=Auth(),
            client=Client(),
            social=SimpleNamespace(),
        )
        return SimpleNamespace(app=app, native=SimpleNamespace(app=app))


def _auth_result(status, *, ok=False, authenticate=False):
    return SimpleNamespace(
        ok=ok,
        status=status,
        code="AUTH_RESULT",
        message="provider authentication result",
        authenticate=authenticate,
    )


def _expired_account() -> LegacyMomentsAccount:
    return replace(
        account(),
        login_encrypted={"plain": "13800138000"},
        password_encrypted={"plain": "saved-password"},
        token_encrypted={"plain": "expired-token"},
        token_expires_at=NOW - timedelta(seconds=1),
    )


class LegacyMomentsImportOrchestratorTests(unittest.TestCase):
    def test_full_scan_imports_only_180_days_and_reads_before_each_write(self) -> None:
        reader = _Reader(
            posts={
                1: (
                    post_item(
                        post_id="101",
                        created_at=NOW - timedelta(days=30),
                        topics=("徒步", "上海"),
                    ),
                    post_item(
                        post_id="old",
                        created_at=NOW - timedelta(days=181),
                    ),
                ),
                2: (),
            },
            comments={
                ("101", 1): (
                    comment_item(
                        comment_id="201",
                        post_id="101",
                        created_at=NOW - timedelta(days=2),
                    ),
                    comment_item(
                        comment_id="202",
                        post_id="101",
                        main_id="201",
                        created_at=NOW - timedelta(days=1),
                    ),
                ),
                ("101", 2): (),
            },
        )
        events = reader.events
        writer = _Writer(events)

        summary = LegacyMomentsImportOrchestrator(
            reader, writer, clock=lambda: NOW
        ).run(account())

        self.assertEqual(summary.posts_seen, 1)
        self.assertEqual(summary.posts_created, 1)
        self.assertEqual(summary.comments_seen, 2)
        self.assertEqual(summary.comments_created, 2)
        self.assertEqual(summary.counts, {"posts": 1, "comments": 2, "topics": 2})
        self.assertTrue(writer.completed)
        self.assertEqual(writer.posts[0].topics, ("徒步", "上海"))
        self.assertEqual(writer.comments[1].parent_upstream_id, "201")
        self.assertLess(events.index(("network-post", 1)), events.index(("db-posts", 1, ("101",))))
        self.assertLess(
            events.index(("network-comment", "101", "42", False, 1)),
            events.index(("db-comments", "101", ("201", "202"))),
        )

    def test_provider_failure_marks_incomplete_and_never_completes(self) -> None:
        reader = _Reader(posts={}, fail_posts=True)
        writer = _Writer(reader.events)
        with self.assertRaises(LegacyMomentsProviderError):
            LegacyMomentsImportOrchestrator(
                reader, writer, clock=lambda: NOW
            ).run(account())
        self.assertEqual(writer.failed, ["legacy_moments_provider_unavailable"])
        self.assertFalse(writer.completed)

    def test_repeated_nonempty_page_cannot_generate_completion_marker(self) -> None:
        repeated = (
            post_item(post_id="101", created_at=NOW - timedelta(days=1)),
        )
        reader = _Reader(posts={1: repeated, 2: repeated})
        writer = _Writer(reader.events)
        with self.assertRaisesRegex(LegacyMomentsDataError, "repeated"):
            LegacyMomentsImportOrchestrator(
                reader, writer, clock=lambda: NOW
            ).run(account())
        self.assertFalse(writer.completed)
        self.assertEqual(writer.failed, ["legacy_moments_data_invalid"])

    def test_unknown_timestamp_fails_closed_instead_of_claiming_coverage(self) -> None:
        invalid = post_item(post_id="101", created_at=NOW)
        invalid["time"] = "某个时间"
        reader = _Reader(posts={1: (invalid,)})
        writer = _Writer(reader.events)
        with self.assertRaises(LegacyMomentsDataError):
            LegacyMomentsImportOrchestrator(
                reader, writer, clock=lambda: NOW
            ).run(account())
        self.assertFalse(writer.completed)


class LegacyMomentsReauthenticationTests(unittest.TestCase):
    def test_valid_token_restores_without_decrypting_password_or_network_login(self) -> None:
        migration_account = replace(
            _expired_account(),
            password_encrypted={"error": True},
            token_encrypted={"plain": "valid-token"},
            token_expires_at=NOW + timedelta(hours=1),
        )
        provider = _AuthProvider(_auth_result(401))

        resolved = _runtime(
            migration_account,
            cipher=_Cipher(),
            provider=provider,
            now=NOW,
        )

        self.assertIsNone(resolved.refreshed)
        self.assertEqual(provider.events, [])

    def test_expired_token_reauthenticates_without_db_transaction_then_persists_short(self) -> None:
        events = []
        transaction_state = {"active": False}
        provider = _AuthProvider(
            _auth_result(200, ok=True, authenticate=True),
            events=events,
            transaction_state=transaction_state,
        )
        cipher = _Cipher(events)
        migration_account = _expired_account()
        external = SimpleNamespace(
            token_encrypted={"ciphertext": "old"},
            token_expires_at=NOW - timedelta(seconds=1),
            last_authenticated_at=None,
            device_data={"existing": "kept"},
        )

        class Result:
            def one_or_none(self):
                return SimpleNamespace(), external

        class Db:
            def execute(self, _statement):
                events.append(("db-execute",))
                return Result()

        @contextmanager
        def db_scope():
            if transaction_state["active"]:
                raise AssertionError("nested database transaction")
            transaction_state["active"] = True
            events.append(("db-enter",))
            try:
                yield Db()
            finally:
                transaction_state["active"] = False
                events.append(("db-exit",))

        writer = SimpleNamespace(fail=lambda *_args, **_kwargs: None)
        expected = ImportSummary(
            post_pages=1,
            comment_pages=0,
            posts_seen=1,
            posts_created=1,
            comments_seen=0,
            comments_created=0,
            counts={"posts": 1, "comments": 0, "topics": 0},
            record_digest="a" * 64,
        )

        class Orchestrator:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self, _account):
                if transaction_state["active"]:
                    raise AssertionError("migration started inside auth transaction")
                events.append(("orchestrator",))
                return expected

        with patch(
            "bbw_web.moments_native.legacy_migration._load_account",
            return_value=migration_account,
        ), patch(
            "bbw_web.moments_native.legacy_migration.SqlAlchemyLegacyMomentsWriter",
            return_value=writer,
        ), patch(
            "bbw_web.moments_native.legacy_migration.LegacyMomentsImportOrchestrator",
            Orchestrator,
        ):
            summary = run_legacy_moments_import(
                migration_account.owner_user_id,
                settings=SimpleNamespace(),
                runtime_provider=provider,
                cipher=cipher,
                db_scope=db_scope,
                clock=lambda: NOW,
            )

        self.assertEqual(summary, expected)
        auth_index = events.index(("network-auth",))
        db_enters = [
            index for index, event in enumerate(events) if event == ("db-enter",)
        ]
        self.assertEqual(len(db_enters), 2)
        self.assertLess(db_enters[0], auth_index)
        self.assertLess(auth_index, db_enters[1])
        self.assertLess(db_enters[1], events.index(("orchestrator",)))
        self.assertEqual(
            external.token_encrypted,
            {"ciphertext": f"encrypted:{len('refreshed-token')}"},
        )
        self.assertIsNone(external.token_expires_at)
        self.assertEqual(external.last_authenticated_at, NOW)
        self.assertEqual(external.device_data["existing"], "kept")
        self.assertEqual(external.device_data["device_id"], "migration-device")
        self.assertEqual(provider.close_calls, 1)

    def test_auth_rejection_and_provider_outage_have_stable_distinct_failures(self) -> None:
        cases = (
            (
                _auth_result(401),
                LegacyMomentsAuthenticationRejected,
                "legacy_moments_authentication_rejected",
            ),
            (
                _auth_result(503),
                LegacyMomentsProviderError,
                "legacy_moments_provider_unavailable",
            ),
        )

        for result, error_type, code in cases:
            with self.subTest(code=code):
                migration_account = _expired_account()
                provider = _AuthProvider(result)
                failed = []
                writer = SimpleNamespace(
                    fail=lambda _account, _window, *, code: failed.append(code)
                )

                @contextmanager
                def db_scope():
                    yield SimpleNamespace()

                with patch(
                    "bbw_web.moments_native.legacy_migration._load_account",
                    return_value=migration_account,
                ), patch(
                    "bbw_web.moments_native.legacy_migration.SqlAlchemyLegacyMomentsWriter",
                    return_value=writer,
                ):
                    with self.assertRaises(error_type):
                        run_legacy_moments_import(
                            migration_account.owner_user_id,
                            settings=SimpleNamespace(),
                            runtime_provider=provider,
                            cipher=_Cipher(),
                            db_scope=db_scope,
                            clock=lambda: NOW,
                        )

                self.assertEqual(failed, [code])
                self.assertEqual(provider.close_calls, 1)

    def test_missing_token_without_saved_password_fails_before_network(self) -> None:
        migration_account = replace(
            _expired_account(),
            password_encrypted=None,
            token_encrypted=None,
            token_expires_at=None,
        )
        provider = _AuthProvider(_auth_result(200, ok=True, authenticate=True))

        with self.assertRaisesRegex(LegacyMomentsDataError, "password_credential_missing"):
            _runtime(
                migration_account,
                cipher=_Cipher(),
                provider=provider,
                now=NOW,
            )

        self.assertEqual(provider.events, [])


class LegacyMomentsBatchImportTests(unittest.TestCase):
    def test_all_active_query_does_not_silently_exclude_sync_disabled_accounts(self) -> None:
        statements = []

        class Db:
            def scalars(self, statement):
                statements.append(statement)
                return ()

        self.assertEqual(_list_active_owner_ids(Db()), ())
        rendered = str(statements[0])
        self.assertIn("users.status", rendered)
        self.assertIn("external_accounts.provider", rendered)
        self.assertNotIn("sync_enabled", rendered)

    def test_all_active_continues_and_returns_aggregate_counts_only(self) -> None:
        owner_ids = tuple(uuid.uuid4() for _ in range(7))
        success = ImportSummary(
            post_pages=2,
            comment_pages=3,
            posts_seen=4,
            posts_created=4,
            comments_seen=5,
            comments_created=5,
            counts={"posts": 4, "comments": 5, "topics": 2},
            record_digest="b" * 64,
        )
        outcomes = {
            owner_ids[0]: success,
            owner_ids[1]: LegacyMomentsAuthenticationRejected(),
            owner_ids[2]: LegacyMomentsProviderError(),
            owner_ids[3]: LegacyMomentsDataError(),
            owner_ids[4]: LegacyMomentsLimitError(),
            owner_ids[5]: LegacyMomentsAuthenticationError(),
            owner_ids[6]: RuntimeError("secret account context"),
        }

        def run_one(owner_id, **_kwargs):
            outcome = outcomes[owner_id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        @contextmanager
        def db_scope():
            yield SimpleNamespace()

        with patch(
            "bbw_web.moments_native.legacy_migration._list_active_owner_ids",
            return_value=owner_ids,
        ), patch(
            "bbw_web.moments_native.legacy_migration.run_legacy_moments_import",
            side_effect=run_one,
        ):
            batch = run_all_active_legacy_moments_imports(
                settings=SimpleNamespace(),
                runtime_provider=SimpleNamespace(),
                cipher=_Cipher(),
                db_scope=db_scope,
                clock=lambda: NOW,
            )

        self.assertEqual(batch.accounts_total, 7)
        self.assertEqual(batch.accounts_succeeded, 1)
        self.assertEqual(batch.accounts_failed, 6)
        self.assertEqual(batch.authentication_rejected, 1)
        self.assertEqual(batch.authentication_failed, 1)
        self.assertEqual(batch.provider_unavailable, 1)
        self.assertEqual(batch.data_invalid, 1)
        self.assertEqual(batch.limit_exceeded, 1)
        self.assertEqual(batch.internal_errors, 1)
        self.assertEqual(batch.posts, 4)
        self.assertEqual(batch.comments, 5)
        self.assertEqual(batch.topics, 2)


class BanghuaMomentsReaderTests(unittest.TestCase):
    def test_false_page_is_empty_and_wire_arguments_are_server_owned(self) -> None:
        calls = []
        social = SimpleNamespace(
            user_posts=lambda uid, page: calls.append(("posts", uid, page))
            or SimpleNamespace(ok=False, status=200, raw="false", data=False),
            main_comments=lambda post, author, **kwargs: calls.append(
                ("comments", post, author, kwargs)
            )
            or SimpleNamespace(ok=False, status=200, raw="false", data=False),
        )
        reader = BanghuaMomentsReader(social, upstream_uid="42")
        self.assertEqual(reader.fetch_post_page(3), ())
        self.assertEqual(
            reader.fetch_comment_page(
                post_upstream_id="101",
                author_upstream_uid="42",
                hide_comments=True,
                page=4,
            ),
            (),
        )
        self.assertEqual(calls[0], ("posts", "42", "3"))
        self.assertEqual(
            calls[1],
            (
                "comments",
                "101",
                "42",
                {"hide_comment": "1", "page": "4"},
            ),
        )

    @staticmethod
    def _page(data, *, raw=""):
        return SimpleNamespace(ok=True, status=200, raw=raw, data=data)

    @staticmethod
    def _reader(result):
        social = SimpleNamespace(
            user_posts=lambda _uid, page: result,
            main_comments=lambda _post, _author, **_kwargs: result,
        )
        return BanghuaMomentsReader(social, upstream_uid="42")

    def test_unknown_page_structure_fails_closed_instead_of_reading_empty(self) -> None:
        # 未知包裹结构（字段改名/新增包装层）不得被静默当作空页读完，
        # 否则会伪造 complete Marker。帖子与评论两个入口共用同一守卫。
        result = self._page(
            {"unknownWrap": "opaque-token"},
            raw='{"unknownWrap":"opaque-token"}',
        )
        reader = self._reader(result)
        with self.assertRaisesRegex(
            LegacyMomentsDataError, "legacy_page_structure_unknown"
        ):
            reader.fetch_post_page(1)
        with self.assertRaisesRegex(
            LegacyMomentsDataError, "legacy_page_structure_unknown"
        ):
            reader.fetch_comment_page(
                post_upstream_id="101",
                author_upstream_uid="42",
                hide_comments=False,
                page=1,
            )

    def test_partially_unparseable_page_items_fail_closed(self) -> None:
        # 页内 3 条原始条目、其中 1 条无法被 normalizer 解析：静默丢弃会让
        # 导入计数与真实历史不一致，必须失败关闭而不是只导入 2 条。
        raw_items = [
            post_item(post_id="301", created_at=NOW - timedelta(days=3)),
            post_item(post_id="302", created_at=NOW - timedelta(days=2)),
            {"corrupted": "payload"},
        ]
        reader = self._reader(self._page({"list": raw_items}))
        with self.assertRaisesRegex(
            LegacyMomentsDataError, "legacy_page_item_unparseable"
        ):
            reader.fetch_post_page(1)

    def test_explicit_empty_shapes_still_read_as_empty(self) -> None:
        # 服务端明确的空形态仍然允许被当作「已读到结束」。
        cases = (
            ("dict-empty-list", {"list": []}, '{"list":[]}'),
            ("dict-empty", {}, "{}"),
            ("empty-list", [], "[]"),
            ("json-empty-array-string", "[]", "[]"),
            ("empty-string", "", ""),
        )
        for label, data, raw in cases:
            with self.subTest(label=label):
                reader = self._reader(self._page(data, raw=raw))
                self.assertEqual(reader.fetch_post_page(1), ())

    def test_renamed_or_shadowed_page_lists_must_not_read_as_empty(self) -> None:
        # 回归守卫：非空数据被空的白名单键遮蔽（list 为空但条目在 posts），
        # 或列表字段整体改名（luntanList），都必须失败关闭，不得伪造空页。
        shadowed = {
            "list": [],
            "posts": [
                post_item(
                    post_id=str(400 + index),
                    created_at=NOW - timedelta(days=1),
                )
                for index in range(10)
            ],
        }
        renamed = {
            "luntanList": [
                post_item(post_id="501", created_at=NOW - timedelta(days=1))
            ],
            "banners": [],
        }
        nested_shadowed = {
            "list": [],
            "data": {
                "posts": [
                    post_item(post_id="502", created_at=NOW - timedelta(days=1))
                ]
            },
        }
        for label, data in (
            ("shadowed", shadowed),
            ("renamed", renamed),
            ("nested-shadowed", nested_shadowed),
        ):
            with self.subTest(label=label):
                reader = self._reader(self._page(data))
                with self.assertRaisesRegex(
                    LegacyMomentsDataError, "legacy_page_structure_unknown"
                ):
                    reader.fetch_post_page(1)

    def test_nonempty_list_beside_empty_ads_never_returns_empty(self) -> None:
        # "result" 属于 extract_list 白名单：条目必须被完整解析返回，
        # 绝不能因为同页存在空的 "ads" 列表而被误判为空页。
        parsed_reader = self._reader(
            self._page(
                {
                    "result": [
                        post_item(
                            post_id="601", created_at=NOW - timedelta(days=1)
                        ),
                        post_item(
                            post_id="602", created_at=NOW - timedelta(days=2)
                        ),
                    ],
                    "ads": [],
                }
            )
        )
        items = parsed_reader.fetch_post_page(1)
        self.assertEqual([item["id"] for item in items], ["601", "602"])

        # 白名单外的非空列表（ads）承载条目时，读取端解析不出任何内容，
        # 必须失败关闭而不是把空的白名单键 "result" 当作空页读完。
        unknown_reader = self._reader(
            self._page(
                {
                    "ads": [
                        post_item(
                            post_id="603", created_at=NOW - timedelta(days=1)
                        )
                    ],
                    "result": [],
                }
            )
        )
        with self.assertRaisesRegex(
            LegacyMomentsDataError, "legacy_page_structure_unknown"
        ):
            unknown_reader.fetch_post_page(1)


class SqlAlchemyLegacyMomentsWriterTests(unittest.TestCase):
    def test_begin_invalidates_an_existing_complete_marker_before_writes(self) -> None:
        migration_account = account()
        window = ImportWindow(
            coverage_started_at=NOW - timedelta(days=180),
            coverage_ended_at=NOW,
        )
        captured: dict[str, object] = {}

        class CursorRepository:
            def __init__(self, _db):
                pass

            def upsert(self, **values):
                captured.update(values)
                return SimpleNamespace(**values)

        @contextmanager
        def db_scope():
            yield SimpleNamespace()

        writer = SqlAlchemyLegacyMomentsWriter(
            db_scope=db_scope,
            clock=lambda: NOW,
        )
        with patch(
            "bbw_web.moments_native.legacy_migration.SyncCursorRepository",
            CursorRepository,
        ):
            writer.begin(migration_account, window)

        marker = json.loads(str(captured["cursor"]))
        self.assertFalse(marker["complete"])
        self.assertEqual(marker["phase"], "posts")
        self.assertIsNone(captured["last_succeeded_at"])


class LegacyMomentsCliTests(unittest.TestCase):
    def test_all_active_cli_emits_only_aggregate_counts(self) -> None:
        batch = BatchImportSummary(
            accounts_total=3,
            accounts_succeeded=2,
            accounts_failed=1,
            authentication_rejected=0,
            authentication_failed=0,
            provider_unavailable=1,
            data_invalid=0,
            limit_exceeded=0,
            internal_errors=0,
            post_pages=4,
            comment_pages=5,
            posts_seen=6,
            posts_created=6,
            comments_seen=7,
            comments_created=7,
            posts=6,
            comments=7,
            topics=2,
        )
        hidden_owner = str(uuid.uuid4())
        output = io.StringIO()
        with patch(
            "bbw_web.moments_native.legacy_migration.run_all_active_legacy_moments_imports",
            return_value=batch,
        ):
            exit_code = main(["--all-active"], stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["accounts_total"], 3)
        self.assertNotIn("record_digest", payload)
        self.assertNotIn("owner_user_id", payload)
        self.assertNotIn("upstream_uid", payload)
        self.assertNotIn(hidden_owner, output.getvalue())

    def test_single_success_does_not_emit_record_digest(self) -> None:
        output = io.StringIO()
        summary = ImportSummary(
            post_pages=1,
            comment_pages=2,
            posts_seen=3,
            posts_created=3,
            comments_seen=4,
            comments_created=4,
            counts={"posts": 3, "comments": 4, "topics": 1},
            record_digest="c" * 64,
        )
        with patch(
            "bbw_web.moments_native.legacy_migration.run_legacy_moments_import",
            return_value=summary,
        ):
            exit_code = main(
                ["--owner-user-id", str(uuid.uuid4())], stdout=output
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertNotIn("record_digest", payload)

    def test_unexpected_exception_returns_stable_non_secret_json(self) -> None:
        output = io.StringIO()
        with patch(
            "bbw_web.moments_native.legacy_migration.run_legacy_moments_import",
            side_effect=RuntimeError("token=secret-account-context"),
        ):
            exit_code = main(
                ["--owner-user-id", str(uuid.uuid4())],
                stdout=output,
            )

        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload,
            {
                "ok": False,
                "code": "legacy_moments_internal_error",
                "retryable": False,
            },
        )
        self.assertNotIn("secret-account-context", output.getvalue())


if __name__ == "__main__":
    unittest.main()
