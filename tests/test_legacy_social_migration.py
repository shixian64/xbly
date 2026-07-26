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

from bbw_prod.migration_readiness import (
    DOMAIN_MARKER_SCOPES,
    SOCIAL_MARKER_SCOPE_PATHS,
    SOCIAL_MARKER_SOURCE_PATHS,
)
from bbw_web.social_native.legacy_migration import (
    LEGACY_PROVIDER,
    LOCAL_SOCIAL_PROVIDER,
    PROFILE_SOURCE_PATH,
    RELATION_SOURCE_PATHS,
    BanghuaSocialReader,
    ImportWindow,
    LegacySocialAccount,
    LegacySocialAuthenticationRejected,
    LegacySocialDataError,
    LegacySocialImportOrchestrator,
    LegacySocialLimitError,
    LegacySocialProviderError,
    SourcePage,
    SqlAlchemyLegacySocialWriter,
    WriteSummary,
    _relationship_snapshot_rows,
    _runtime,
    fetch_complete_social_snapshot,
    main,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def account() -> LegacySocialAccount:
    return LegacySocialAccount(
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


class _CompleteReader:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def fetch_profile(self):
        self.events.append(("network", PROFILE_SOURCE_PATH, 1))
        return {
            "id": "42",
            "nickname": "迁移用户",
            "signature": "历史资料",
        }

    def fetch_following_page(self, page: int):
        self.events.append(("network", RELATION_SOURCE_PATHS["follow"], page))
        if page == 1:
            return SourcePage(
                RELATION_SOURCE_PATHS["follow"],
                1,
                ({"yourid": "9", "yournickname": "关注用户"},),
                False,
            )
        return SourcePage(
            RELATION_SOURCE_PATHS["follow"], page, (), True, "empty-page"
        )

    def fetch_followers_page(self, page: int):
        self.events.append(("network", RELATION_SOURCE_PATHS["follower"], page))
        return SourcePage(
            RELATION_SOURCE_PATHS["follower"],
            page,
            ({"yourid": "10", "yournickname": "粉丝用户"},),
            True,
            "explicit-complete",
        )

    def fetch_friends(self):
        self.events.append(("network", RELATION_SOURCE_PATHS["friend"], 1))
        return SourcePage(
            RELATION_SOURCE_PATHS["friend"],
            1,
            (
                {
                    "uid": "42",
                    "friendid": "11",
                    "friendnickname": "好友用户",
                },
            ),
            True,
            "single-response",
        )

    def fetch_friend_requests_page(self, page: int):
        self.events.append(
            ("network", RELATION_SOURCE_PATHS["friend_request"], page)
        )
        return SourcePage(
            RELATION_SOURCE_PATHS["friend_request"],
            page,
            (
                {
                    "id": "apply-in",
                    "myid": "42",
                    "yourid": "12",
                    "yournickname": "申请用户",
                    "agree": "0",
                },
                {
                    "id": "apply-out",
                    "myid": "13",
                    "yourid": "42",
                    "yournickname": "已申请用户",
                    "agree": "1",
                },
            ),
            True,
            "explicit-complete",
        )

    def fetch_blacklist(self):
        self.events.append(("network", RELATION_SOURCE_PATHS["blacklist"], 1))
        return SourcePage(
            RELATION_SOURCE_PATHS["blacklist"],
            1,
            ({"yourid": "14", "yournickname": "已拉黑用户"},),
            True,
            "single-response",
        )

    def fetch_blacklisted_by(self):
        self.events.append(
            ("network", RELATION_SOURCE_PATHS["blacklisted_by"], 1)
        )
        return SourcePage(
            RELATION_SOURCE_PATHS["blacklisted_by"],
            1,
            ({"yourid": "15", "yournickname": "拉黑我的用户"},),
            True,
            "single-response",
        )


class _Writer:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.failed: list[str] = []
        self.completed = False
        self.snapshot = None

    def begin(self, _account, window: ImportWindow) -> None:
        self.events.append(("db-begin", window.coverage_ended_at))

    def apply(self, _account, snapshot):
        self.events.append(("db-apply", snapshot.record_digest))
        return WriteSummary(
            profile_written=1,
            relationship_rows_written=sum(
                len(records) for records in snapshot.records.values()
            ),
            legacy_rows_deactivated=0,
        )

    def complete(self, _account, snapshot) -> None:
        self.events.append(("db-complete", snapshot.record_digest))
        self.completed = True
        self.snapshot = snapshot

    def fail(self, _account, _window, *, code: str) -> None:
        self.events.append(("db-fail", code))
        self.failed.append(code)


def complete_snapshot(*, migration_account: LegacySocialAccount | None = None):
    resolved_account = migration_account or account()
    return fetch_complete_social_snapshot(
        _CompleteReader(),
        resolved_account,
        coverage_started_at=NOW,
        clock=lambda: NOW,
    )


class LegacySocialImportOrchestratorTests(unittest.TestCase):
    def test_complete_scan_reads_all_sources_before_first_local_write(self) -> None:
        migration_account = account()
        reader = _CompleteReader()
        writer = _Writer(reader.events)

        summary = LegacySocialImportOrchestrator(
            reader, writer, clock=lambda: NOW
        ).run(migration_account)

        self.assertTrue(writer.completed)
        self.assertEqual(
            summary.counts,
            {
                "profile": 1,
                "relationships": 3,
                "friend-requests": 2,
                "blocklists": 2,
            },
        )
        self.assertEqual(summary.source_pages, 8)
        self.assertEqual(summary.source_records, 8)
        first_write = next(
            index
            for index, event in enumerate(reader.events)
            if event[0] == "db-begin"
        )
        self.assertTrue(reader.events[:first_write])
        self.assertTrue(
            all(event[0] == "network" for event in reader.events[:first_write])
        )
        self.assertEqual(
            set(writer.snapshot.watermarks), set(SOCIAL_MARKER_SOURCE_PATHS)
        )
        self.assertEqual(
            writer.snapshot.coverage,
            {
                scope: {
                    "source_complete": True,
                    "records": summary.counts[scope],
                    "source_paths": list(paths),
                }
                for scope, paths in SOCIAL_MARKER_SCOPE_PATHS.items()
            },
        )

    def test_repeated_page_fails_without_begin_or_completion(self) -> None:
        class RepeatedPageReader(_CompleteReader):
            def fetch_following_page(self, page: int):
                self.events.append(
                    ("network", RELATION_SOURCE_PATHS["follow"], page)
                )
                return SourcePage(
                    RELATION_SOURCE_PATHS["follow"],
                    page,
                    ({"yourid": "9", "yournickname": "重复用户"},),
                    False,
                )

        reader = RepeatedPageReader()
        writer = _Writer(reader.events)
        with self.assertRaisesRegex(LegacySocialDataError, "page_repeated"):
            LegacySocialImportOrchestrator(
                reader, writer, clock=lambda: NOW
            ).run(account())

        self.assertFalse(writer.completed)
        self.assertEqual(writer.failed, ["legacy_social_data_invalid"])
        self.assertFalse(any(event[0] == "db-begin" for event in reader.events))

    def test_page_limit_fails_closed(self) -> None:
        class EndlessReader(_CompleteReader):
            def fetch_following_page(self, page: int):
                self.events.append(
                    ("network", RELATION_SOURCE_PATHS["follow"], page)
                )
                return SourcePage(
                    RELATION_SOURCE_PATHS["follow"],
                    page,
                    ({"yourid": str(100 + page), "yournickname": "用户"},),
                    False,
                )

        reader = EndlessReader()
        writer = _Writer(reader.events)
        with patch(
            "bbw_web.social_native.legacy_migration.MAX_SOURCE_PAGES", 2
        ), self.assertRaisesRegex(LegacySocialLimitError, "page_limit"):
            LegacySocialImportOrchestrator(
                reader, writer, clock=lambda: NOW
            ).run(account())

        self.assertFalse(writer.completed)
        self.assertEqual(writer.failed, ["legacy_social_import_limit_exceeded"])

    def test_missing_uid_and_ambiguous_friend_requests_fail_closed(self) -> None:
        class InvalidReader(_CompleteReader):
            invalid_follow = None
            invalid_request = None

            def fetch_following_page(self, page: int):
                self.events.append(
                    ("network", RELATION_SOURCE_PATHS["follow"], page)
                )
                return SourcePage(
                    RELATION_SOURCE_PATHS["follow"],
                    page,
                    (self.invalid_follow,),
                    True,
                    "explicit-complete",
                )

            def fetch_friend_requests_page(self, page: int):
                self.events.append(
                    ("network", RELATION_SOURCE_PATHS["friend_request"], page)
                )
                return SourcePage(
                    RELATION_SOURCE_PATHS["friend_request"],
                    page,
                    (self.invalid_request,),
                    True,
                    "explicit-complete",
                )

        cases = (
            (
                {"nickname": "无 UID"},
                {
                    "myid": "42",
                    "yourid": "12",
                    "agree": "0",
                },
                "item_unresolved",
            ),
            (
                {"yourid": "9", "yournickname": "关注用户"},
                {"uid": "12", "agree": "0"},
                "direction_unknown",
            ),
            (
                {"yourid": "9", "yournickname": "关注用户"},
                {"myid": "42", "yourid": "12"},
                "status_unknown",
            ),
        )
        for invalid_follow, invalid_request, error in cases:
            with self.subTest(error=error):
                reader = InvalidReader()
                reader.invalid_follow = invalid_follow
                reader.invalid_request = invalid_request
                writer = _Writer(reader.events)
                with self.assertRaisesRegex(LegacySocialDataError, error):
                    LegacySocialImportOrchestrator(
                        reader, writer, clock=lambda: NOW
                    ).run(account())
                self.assertFalse(writer.completed)
                self.assertEqual(writer.failed, ["legacy_social_data_invalid"])


class BanghuaSocialReaderTests(unittest.TestCase):
    def test_false_empty_is_an_explicit_terminal_list(self) -> None:
        calls: list[tuple[str, str]] = []
        false_result = SimpleNamespace(
            ok=False,
            status=200,
            raw="false",
            data=False,
        )
        reader = BanghuaSocialReader(
            SimpleNamespace(get_user=lambda _uid: None),
            SimpleNamespace(
                follow_users=lambda uid, page: calls.append((uid, page))
                or false_result
            ),
            upstream_uid="42",
        )

        page = reader.fetch_following_page(3)

        self.assertEqual(calls, [("42", "3")])
        self.assertTrue(page.terminal)
        self.assertEqual(page.terminal_reason, "false-empty")
        self.assertEqual(page.items, ())

    def test_unknown_payload_structure_is_not_treated_as_empty(self) -> None:
        result = SimpleNamespace(
            ok=True,
            status=200,
            raw='{"unexpected": {"uid": "9"}}',
            data={"unexpected": {"uid": "9"}},
        )
        reader = BanghuaSocialReader(
            SimpleNamespace(get_user=lambda _uid: None),
            SimpleNamespace(follow_users=lambda _uid, page: result),
            upstream_uid="42",
        )

        with self.assertRaisesRegex(LegacySocialDataError, "structure_unknown"):
            reader.fetch_following_page(1)


class LegacySocialReauthenticationTests(unittest.TestCase):
    def test_expired_token_uses_saved_password_and_classifies_failures(self) -> None:
        migration_account = replace(
            account(),
            login_encrypted={"plain": "13800138000"},
            password_encrypted={"plain": "saved-password"},
            token_encrypted={"plain": "expired-token"},
            token_expires_at=NOW - timedelta(seconds=1),
        )

        class Cipher:
            def decrypt_text(self, encrypted, **_kwargs):
                return encrypted["plain"]

        class Session:
            def __init__(self, uid, token, phone):
                self.uid = uid
                self.token = token
                self.phone = phone
                self.password = ""
                self.user_role = ""
                self.rp_verify_time = "0"
                self.vip = "0"
                self.svip = "0"
                self.money = "0"
                self.portrait = ""
                self.user_sign = ""
                self.login_id = ""

            @property
            def logged_in(self):
                return self.uid != "0" and self.token != "0"

            def device_dict(self):
                return {"device_id": "social-migration"}

        class Provider:
            provider_id = "beibeiwu"

            def __init__(self, status):
                self.status = status

            def create_runtime_from_state(self, state):
                session = Session(state.uid, state.token, state.phone)
                status = self.status

                class Auth:
                    def login_password(self, _login, _password):
                        if status == 200:
                            session.uid = "42"
                            session.token = "refreshed-token"
                        return SimpleNamespace(ok=status == 200, status=status)

                app = SimpleNamespace(
                    session=session,
                    auth=Auth(),
                    client=SimpleNamespace(close=lambda: None),
                )
                return SimpleNamespace(app=app)

        resolved = _runtime(
            migration_account,
            cipher=Cipher(),
            provider=Provider(200),
            now=NOW,
        )
        self.assertEqual(resolved.refreshed.token, "refreshed-token")
        self.assertEqual(
            resolved.refreshed.device_data["device_id"], "social-migration"
        )

        with self.assertRaises(LegacySocialAuthenticationRejected):
            _runtime(
                migration_account,
                cipher=Cipher(),
                provider=Provider(401),
                now=NOW,
            )
        with self.assertRaises(LegacySocialProviderError):
            _runtime(
                migration_account,
                cipher=Cipher(),
                provider=Provider(503),
                now=NOW,
            )


class LegacyRelationshipSnapshotTests(unittest.TestCase):
    def test_local_tombstone_is_never_written_and_stale_legacy_row_is_deactivated(
        self,
    ) -> None:
        migration_account = account()
        snapshot = complete_snapshot(migration_account=migration_account)
        local_tombstone = SimpleNamespace(
            provider=LOCAL_SOCIAL_PROVIDER,
            kind="follow",
            subject_upstream_uid="9",
            status="inactive",
            started_at=NOW,
            ended_at=NOW,
            extra_data={"local_authority": True},
        )
        stale_legacy = SimpleNamespace(
            provider=LEGACY_PROVIDER,
            kind="follow",
            subject_upstream_uid="99",
            status="active",
            started_at=NOW,
            ended_at=None,
            extra_data={"source_path": "/api/social/follows"},
        )

        rows, deactivated = _relationship_snapshot_rows(
            (local_tombstone, stale_legacy),
            account=migration_account,
            snapshot=snapshot,
            kind="follow",
            records=snapshot.records["follow"],
            source_path=RELATION_SOURCE_PATHS["follow"],
        )

        self.assertEqual(deactivated, 1)
        self.assertTrue(all(row["provider"] == LEGACY_PROVIDER for row in rows))
        self.assertEqual(local_tombstone.status, "inactive")
        stale = next(row for row in rows if row["subject_upstream_uid"] == "99")
        self.assertEqual(stale["status"], "inactive")
        self.assertIsNotNone(stale["ended_at"])
        self.assertTrue(stale["extra_data"]["snapshot_absent"])

    def test_relationship_planning_is_idempotent_and_preserves_block_evidence(
        self,
    ) -> None:
        migration_account = account()
        snapshot = complete_snapshot(migration_account=migration_account)
        first, first_deactivated = _relationship_snapshot_rows(
            (),
            account=migration_account,
            snapshot=snapshot,
            kind="blacklist",
            records=snapshot.records["blacklist"],
            source_path=RELATION_SOURCE_PATHS["blacklist"],
        )
        existing = tuple(SimpleNamespace(**row) for row in first)
        second, second_deactivated = _relationship_snapshot_rows(
            existing,
            account=migration_account,
            snapshot=snapshot,
            kind="blacklist",
            records=snapshot.records["blacklist"],
            source_path=RELATION_SOURCE_PATHS["blacklist"],
        )

        self.assertEqual(first_deactivated, 0)
        self.assertEqual(second_deactivated, 0)
        self.assertEqual(first, second)
        self.assertEqual(
            first[0]["extra_data"]["message_policy_source"],
            RELATION_SOURCE_PATHS["blacklist"],
        )


class SqlAlchemyLegacySocialWriterTests(unittest.TestCase):
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

        writer = SqlAlchemyLegacySocialWriter(
            db_scope=db_scope,
            clock=lambda: NOW,
        )
        with patch.object(
            writer,
            "_binding",
            return_value=(SimpleNamespace(), SimpleNamespace()),
        ), patch(
            "bbw_web.social_native.legacy_migration.SyncCursorRepository",
            CursorRepository,
        ):
            writer.begin(migration_account, window)

        marker = json.loads(str(captured["cursor"]))
        self.assertFalse(marker["complete"])
        self.assertEqual(marker["phase"], "applying")
        self.assertIsNone(captured["last_succeeded_at"])

    def test_complete_verifies_rows_then_writes_account_bound_marker(self) -> None:
        migration_account = account()
        snapshot = complete_snapshot(migration_account=migration_account)
        relationship_rows = []
        for kind, records in snapshot.records.items():
            planned, _deactivated = _relationship_snapshot_rows(
                (),
                account=migration_account,
                snapshot=snapshot,
                kind=kind,
                records=records,
                source_path=RELATION_SOURCE_PATHS[kind],
            )
            relationship_rows.extend(SimpleNamespace(**row) for row in planned)

        user = SimpleNamespace(
            profile=dict(snapshot.profile),
            display_name=snapshot.profile["nickname"],
        )

        class FakeDb:
            def scalars(self, _statement):
                return tuple(relationship_rows)

        @contextmanager
        def db_scope():
            yield FakeDb()

        captured: dict[str, object] = {}

        class CursorRepository:
            def upsert(self, **values):
                captured.update(values)
                return SimpleNamespace(**values)

        writer = SqlAlchemyLegacySocialWriter(
            db_scope=db_scope, clock=lambda: NOW
        )
        with patch.object(
            writer,
            "_binding",
            return_value=(user, SimpleNamespace()),
        ), patch(
            "bbw_web.social_native.legacy_migration.SyncCursorRepository",
            return_value=CursorRepository(),
        ):
            writer.complete(migration_account, snapshot)

        marker = json.loads(str(captured["cursor"]))
        self.assertTrue(marker["complete"])
        self.assertTrue(marker["source_complete"])
        self.assertEqual(
            marker["owner_user_id"], str(migration_account.owner_user_id)
        )
        self.assertEqual(
            marker["external_account_id"],
            str(migration_account.external_account_id),
        )
        self.assertEqual(marker["upstream_uid"], migration_account.upstream_uid)
        self.assertEqual(marker["scopes"], list(DOMAIN_MARKER_SCOPES["social"]))
        self.assertEqual(marker["counts"], snapshot.counts)
        self.assertEqual(marker["source_paths"], list(SOCIAL_MARKER_SOURCE_PATHS))
        self.assertEqual(marker["coverage"], snapshot.coverage)
        self.assertEqual(marker["unresolved_records"], 0)
        self.assertEqual(marker["record_digest"], snapshot.record_digest)


class LegacySocialCliTests(unittest.TestCase):
    def test_unexpected_exception_returns_stable_non_secret_json(self) -> None:
        output = io.StringIO()
        with patch(
            "bbw_web.social_native.legacy_migration.run_legacy_social_import",
            side_effect=RuntimeError("token=secret uid=42"),
        ):
            exit_code = main(
                ["--owner-user-id", str(uuid.uuid4())], stdout=output
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "ok": False,
                "code": "legacy_social_internal_error",
                "retryable": False,
            },
        )
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("42", output.getvalue())


if __name__ == "__main__":
    unittest.main()
