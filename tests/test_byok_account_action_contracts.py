from __future__ import annotations

import importlib.util
import sys
import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    from bbw_agent.repositories import (
        ACTION_EXECUTION_TIMEOUT_FAILURE_CODE,
        AGENT_RUNNING_TIMEOUT,
        MODEL_RUN_TIMEOUT_FAILURE_CODE,
        AgentActionExecutionRepository,
        AgentAutonomyDailyUsageRepository,
        AgentAutonomySettingRepository,
        AgentAutonomyTaskRepository,
        AgentExecutionSettingRepository,
        AgentRunRepository,
        SUPPORTED_ACCOUNT_ACTION_TYPES,
        _normalize_action_type,
        autonomy_daily_budget_available,
    )
    from bbw_prod.models import (
        AiAgentActionExecution,
        AiAgentRun,
        AiAgentAutonomySetting,
        AiAgentExecutionSetting,
        AiModelRunnerSystemSetting,
        User,
    )
except ImportError as exc:  # pragma: no cover - dependency-less contract runner
    DEPENDENCY_IMPORT_ERROR: ImportError | None = exc
else:
    DEPENDENCY_IMPORT_ERROR = None


class ByokAccountActionSchemaContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def test_migration_0015_defaults_all_execution_gates_to_denied(self) -> None:
        migration = self.read(
            "migrations/versions/20260725_0015_byok_account_actions.py"
        )

        self.assertIn('revision: str = "20260725_0015"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0014"',
            migration,
        )
        for field in (
            '"byok_account_actions_enabled"',
            '"account_actions_enabled"',
            '"user_enabled"',
            '"auto_send_enabled"',
        ):
            self.assertIn(field, migration)
        self.assertGreaterEqual(
            migration.count('server_default=sa.text("false")'), 4
        )
        self.assertIn('server_default=sa.text("\'[]\'::jsonb")', migration)
        self.assertIn("allowed_actions <@ ", migration)
        self.assertIn('"ai_agent_execution_settings"', migration)
        self.assertIn('"ai_agent_action_executions"', migration)

    def test_execution_schema_enforces_owner_status_and_action_boundaries(self) -> None:
        migration = self.read(
            "migrations/versions/20260725_0015_byok_account_actions.py"
        )

        self.assertIn(
            '["external_account_id", "owner_user_id"]', migration
        )
        self.assertIn(
            '["external_accounts.id", "external_accounts.user_id"]', migration
        )
        for status in ("queued", "running", "succeeded", "failed", "cancelled"):
            self.assertIn(f"'{status}'", migration)
        for action_type in (
            "send_private_message",
            "publish_text_post",
            "follow_user",
            "unfollow_user",
        ):
            self.assertIn(f"'{action_type}'", migration)
        for field in (
            '"idempotency_key"',
            '"target_snapshot"',
            '"parameter_snapshot"',
            '"approval_source"',
            '"trigger_source"',
            '"stable_error_code"',
            '"external_result_id"',
            '"execution_setting_version"',
        ):
            self.assertIn(field, migration)
        self.assertIn("jsonb_typeof(target_snapshot) = 'object'", migration)
        self.assertIn("jsonb_typeof(parameter_snapshot) = 'object'", migration)
        self.assertIn(
            '"uq_ai_agent_action_executions_owner_action_idempotency"',
            migration,
        )

    def test_reply_send_run_type_is_added_and_downgrade_restores_old_set(self) -> None:
        migration = self.read(
            "migrations/versions/20260725_0015_byok_account_actions.py"
        )
        models = self.read("bbw_prod/models.py")

        self.assertIn("include_reply_send=True", migration)
        self.assertIn("include_reply_send=False", migration)
        self.assertIn("'reply_draft', 'reply_send'", migration)
        self.assertIn("'reply_draft', 'reply_send'", models)
        self.assertIn(
            'op.drop_column("users", "byok_account_actions_enabled")', migration
        )
        self.assertIn(
            '"ai_model_runner_system_settings", "account_actions_enabled"',
            migration,
        )
        downgrade = migration.split("def downgrade() -> None:", 1)[1]
        delete_reply_send = (
            'DELETE FROM ai_agent_runs WHERE run_type = \'reply_send\''
        )
        self.assertIn(delete_reply_send, downgrade)
        self.assertLess(
            downgrade.index(delete_reply_send),
            downgrade.index("include_reply_send=False"),
        )

    @unittest.skipIf(
        DEPENDENCY_IMPORT_ERROR is not None,
        f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
    )
    def test_migration_0015_tables_columns_constraints_and_indexes_match_models(self) -> None:
        path = ROOT / "migrations/versions/20260725_0015_byok_account_actions.py"
        spec = importlib.util.spec_from_file_location("migration_0015_contract", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Recorder:
            def __init__(self) -> None:
                self.added_columns = []
                self.tables = {}
                self.indexes = {}
                self.checks = []

            @staticmethod
            def f(name):
                return sa.schema.conv(name)

            def add_column(self, table, column):
                self.added_columns.append((table, column))

            def drop_constraint(self, *_args, **_kwargs):
                return None

            def create_check_constraint(self, name, table, sqltext):
                self.checks.append((name, table, str(sqltext)))

            def create_table(self, name, *items):
                metadata = sa.MetaData(naming_convention={
                    "ix": "ix_%(table_name)s_%(column_0_name)s",
                    "uq": "uq_%(table_name)s_%(column_0_name)s",
                    "ck": "ck_%(table_name)s_%(constraint_name)s",
                    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
                    "pk": "pk_%(table_name)s",
                })
                self.tables[name] = sa.Table(name, metadata, *items)

            def create_index(self, name, table, columns, *, unique=False):
                self.indexes.setdefault(table, set()).add(
                    (name, tuple(columns), bool(unique))
                )

        recorder = Recorder()
        original = module.op
        module.op = recorder
        try:
            module.upgrade()
        finally:
            module.op = original

        def normalized_sql(value):
            return "" if value is None else " ".join(str(value).split())

        def column_spec(column):
            default = (
                normalized_sql(column.server_default.arg)
                if column.server_default is not None
                else None
            )
            return (
                column.name,
                str(column.type.compile(dialect=postgresql.dialect())),
                bool(column.nullable),
                default,
            )

        def table_spec(table):
            uniques = {
                (constraint.name, tuple(column.name for column in constraint.columns))
                for constraint in table.constraints
                if isinstance(constraint, sa.UniqueConstraint)
            }
            checks = {
                (constraint.name, normalized_sql(constraint.sqltext))
                for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint)
            }
            foreign_keys = {
                (
                    constraint.name,
                    tuple(column.name for column in constraint.columns),
                    tuple(element.target_fullname for element in constraint.elements),
                    constraint.ondelete,
                )
                for constraint in table.constraints
                if isinstance(constraint, sa.ForeignKeyConstraint)
            }
            primary = tuple(column.name for column in table.primary_key.columns)
            indexes = {
                (index.name, tuple(column.name for column in index.columns), bool(index.unique))
                for index in table.indexes
            }
            return {
                "columns": {column_spec(column) for column in table.columns},
                "uniques": uniques,
                "checks": checks,
                "foreign_keys": foreign_keys,
                "primary": primary,
                "indexes": indexes,
            }

        expected_tables = {
            "ai_agent_execution_settings": AiAgentExecutionSetting.__table__,
            "ai_agent_action_executions": AiAgentActionExecution.__table__,
        }
        self.assertEqual(set(recorder.tables), set(expected_tables))
        replaced_checks = {
            "ai_agent_execution_settings": {
                "ck_ai_agent_execution_settings_"
                "ai_agent_execution_setting_allowed_actions_supported"
            },
            "ai_agent_action_executions": {
                "ck_ai_agent_action_executions_"
                "ai_agent_action_execution_type_valid"
            },
        }
        for name, expected in expected_tables.items():
            with self.subTest(table=name):
                actual_spec = table_spec(recorder.tables[name])
                actual_spec["indexes"] |= recorder.indexes.get(name, set())
                expected_spec = table_spec(expected)
                mutable = replaced_checks.get(name, set())
                actual_spec["checks"] = {
                    item for item in actual_spec["checks"] if item[0] not in mutable
                }
                expected_spec["checks"] = {
                    item for item in expected_spec["checks"] if item[0] not in mutable
                }
                self.assertEqual(actual_spec, expected_spec)

        added = {
            (table, column.name): column_spec(column)
            for table, column in recorder.added_columns
        }
        self.assertEqual(
            added[("users", "byok_account_actions_enabled")],
            column_spec(User.__table__.c.byok_account_actions_enabled),
        )
        self.assertEqual(
            added[("ai_model_runner_system_settings", "account_actions_enabled")],
            column_spec(
                AiModelRunnerSystemSetting.__table__.c.account_actions_enabled
            ),
        )
        self.assertIn(
            (
                "ck_ai_agent_runs_ai_agent_run_type_valid",
                "ai_agent_runs",
                "run_type IN ('connection_test', 'style_analysis', 'reply_draft', 'reply_send')",
            ),
            recorder.checks,
        )

    def test_repository_contract_is_owner_scoped_and_cancels_only_queued_work(self) -> None:
        repositories = self.read("bbw_agent/repositories.py")
        execution_repository = repositories.split(
            "class AgentActionExecutionRepository", 1
        )[1]

        self.assertIn("AiAgentActionExecution.owner_user_id == owner_user_id", execution_repository)
        self.assertIn(".on_conflict_do_nothing(", execution_repository)
        self.assertIn(
            '"uq_ai_agent_action_executions_owner_action_idempotency"',
            execution_repository,
        )
        self.assertIn(".with_for_update(skip_locked=True)", execution_repository)
        cancel_block = execution_repository.split("def _cancel_queued", 1)[1]
        self.assertIn('AiAgentActionExecution.status == "queued"', cancel_block)
        self.assertNotIn('AiAgentActionExecution.status == "running"', cancel_block)
        self.assertIn("def cancel_active_for_owner", execution_repository)
        self.assertIn("def cancel_all_active", execution_repository)


class _ScalarSession:
    def __init__(self, *values: object, rowcount: int = 0) -> None:
        self.values = list(values)
        self.rowcount = rowcount
        self.added: list[object] = []
        self.flushed = 0
        self.executed: list[object] = []

    def scalar(self, _statement: object) -> object:
        return self.values.pop(0)

    def add(self, row: object) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushed += 1

    def execute(self, statement: object) -> SimpleNamespace:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=self.rowcount)

    def scalars(self, statement: object) -> SimpleNamespace:
        self.executed.append(statement)
        return SimpleNamespace(first=lambda: self.values.pop(0))


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokAccountActionRepositoryTests(unittest.TestCase):
    def test_stale_model_run_is_reacquired_but_fresh_run_is_not(self) -> None:
        owner_id = uuid.uuid4()
        connection_id = uuid.uuid4()
        stale_started_at = datetime.now(UTC) - AGENT_RUNNING_TIMEOUT - timedelta(
            seconds=1
        )
        stale = AiAgentRun(
            id=uuid.uuid4(),
            owner_user_id=owner_id,
            connection_id=connection_id,
            run_type="reply_draft",
            status="running",
            idempotency_key="stale-model-run",
            model_snapshot="old-model",
            source_message_count=1,
            prompt_char_count=10,
            output_text="partial",
            output_char_count=7,
            started_at=stale_started_at,
        )
        stale_session = _ScalarSession(None, stale)

        claimed, acquired = AgentRunRepository(stale_session).enqueue_running(
            owner_user_id=owner_id,
            connection_id=connection_id,
            run_type="reply_draft",
            idempotency_key="stale-model-run",
            model_snapshot="new-model",
            peer_upstream_uid="peer-1",
            source_message_count=2,
            prompt_char_count=20,
        )

        self.assertIs(claimed, stale)
        self.assertTrue(acquired)
        self.assertEqual(stale.status, "running")
        self.assertEqual(stale.model_snapshot, "new-model")
        self.assertEqual(stale.peer_upstream_uid, "peer-1")
        self.assertEqual(stale.source_message_count, 2)
        self.assertEqual(stale.prompt_char_count, 20)
        self.assertIsNone(stale.output_text)
        self.assertEqual(stale.output_char_count, 0)
        self.assertIsNone(stale.completed_at)
        self.assertGreater(stale.started_at, stale_started_at)
        self.assertEqual(stale_session.flushed, 1)

        fresh = AiAgentRun(
            id=uuid.uuid4(),
            owner_user_id=owner_id,
            connection_id=connection_id,
            run_type="reply_draft",
            status="running",
            idempotency_key="fresh-model-run",
            model_snapshot="same-model",
            source_message_count=1,
            prompt_char_count=10,
            output_char_count=0,
            started_at=datetime.now(UTC),
        )
        fresh_session = _ScalarSession(None, fresh)
        replay, acquired = AgentRunRepository(fresh_session).enqueue_running(
            owner_user_id=owner_id,
            connection_id=connection_id,
            run_type="reply_draft",
            idempotency_key="fresh-model-run",
            model_snapshot="same-model",
            peer_upstream_uid=None,
            source_message_count=1,
            prompt_char_count=10,
        )

        self.assertIs(replay, fresh)
        self.assertFalse(acquired)
        self.assertEqual(fresh_session.flushed, 0)

    def test_cleanup_timeout_model_run_can_be_reacquired(self) -> None:
        owner_id = uuid.uuid4()
        connection_id = uuid.uuid4()
        timed_out = AiAgentRun(
            id=uuid.uuid4(),
            owner_user_id=owner_id,
            connection_id=connection_id,
            run_type="style_analysis",
            status="failed",
            idempotency_key="timed-out-model-run",
            model_snapshot="old-model",
            source_message_count=0,
            prompt_char_count=0,
            output_char_count=0,
            failure_code=MODEL_RUN_TIMEOUT_FAILURE_CODE,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            completed_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        session = _ScalarSession(None, timed_out)

        replay, acquired = AgentRunRepository(session).enqueue_running(
            owner_user_id=owner_id,
            connection_id=connection_id,
            run_type="style_analysis",
            idempotency_key="timed-out-model-run",
            model_snapshot="new-model",
            peer_upstream_uid=None,
            source_message_count=3,
            prompt_char_count=30,
        )

        self.assertIs(replay, timed_out)
        self.assertTrue(acquired)
        self.assertEqual(timed_out.status, "running")
        self.assertIsNone(timed_out.failure_code)
        self.assertIsNone(timed_out.completed_at)

    def test_stale_running_action_is_manual_review_and_never_requeued(self) -> None:
        owner_id = uuid.uuid4()
        account_id = uuid.uuid4()
        stale = AiAgentActionExecution(
            id=uuid.uuid4(),
            owner_user_id=owner_id,
            external_account_id=account_id,
            action_type="follow_user",
            status="running",
            idempotency_key="stale-action-run",
            execution_setting_version=1,
            target_snapshot={"peer": "target"},
            parameter_snapshot={},
            approval_source="user_allowlist",
            trigger_source="model",
            queued_at=datetime.now(UTC) - timedelta(hours=1),
            started_at=datetime.now(UTC) - AGENT_RUNNING_TIMEOUT - timedelta(
                seconds=1
            ),
        )
        session = _ScalarSession(None, stale)

        replay, created = AgentActionExecutionRepository(session).enqueue(
            owner_user_id=owner_id,
            external_account_id=account_id,
            action_type="follow_user",
            idempotency_key="stale-action-run",
            execution_setting_version=1,
            target_snapshot={"peer": "target"},
            parameter_snapshot={},
            approval_source="user_allowlist",
            trigger_source="model",
        )

        self.assertIs(replay, stale)
        self.assertFalse(created)
        self.assertEqual(stale.status, "manual_review")
        self.assertEqual(
            stale.stable_error_code,
            ACTION_EXECUTION_TIMEOUT_FAILURE_CODE,
        )
        self.assertIsNotNone(stale.completed_at)
        self.assertIsNone(stale.cancelled_at)
        self.assertEqual(session.flushed, 1)

    def test_periodic_cleanup_uses_bounded_running_cutoffs(self) -> None:
        at = datetime.now(UTC)
        model_session = _ScalarSession(rowcount=2)
        action_session = _ScalarSession(rowcount=3)

        self.assertEqual(
            AgentRunRepository(model_session).fail_stale_running(at=at), 2
        )
        self.assertEqual(
            AgentActionExecutionRepository(
                action_session
            ).mark_stale_running_for_manual_review(at=at),
            3,
        )

        model_compiled = model_session.executed[0].compile(
            dialect=postgresql.dialect()
        )
        action_compiled = action_session.executed[0].compile(
            dialect=postgresql.dialect()
        )
        self.assertIn("running", model_compiled.params.values())
        self.assertIn("failed", model_compiled.params.values())
        self.assertIn(
            MODEL_RUN_TIMEOUT_FAILURE_CODE, model_compiled.params.values()
        )
        self.assertIn("running", action_compiled.params.values())
        self.assertIn("manual_review", action_compiled.params.values())
        self.assertIn(
            ACTION_EXECUTION_TIMEOUT_FAILURE_CODE,
            action_compiled.params.values(),
        )

    def test_execution_settings_start_disabled_and_revoke_increments_version(self) -> None:
        owner_id = uuid.uuid4()
        create_session = _ScalarSession(None)
        created = AgentExecutionSettingRepository(create_session).get_or_create(
            owner_id
        )

        self.assertFalse(created.user_enabled)
        self.assertFalse(created.auto_send_enabled)
        self.assertEqual(created.allowed_actions, [])
        self.assertEqual(created.version, 1)
        self.assertEqual(create_session.added, [created])

        created.user_enabled = True
        created.auto_send_enabled = True
        created.allowed_actions = ["send_private_message"]
        revoke_session = _ScalarSession(created)
        changed = AgentExecutionSettingRepository(
            revoke_session
        ).disable_for_owner(owner_id)

        self.assertTrue(changed)
        self.assertFalse(created.user_enabled)
        self.assertFalse(created.auto_send_enabled)
        self.assertEqual(created.allowed_actions, ["send_private_message"])
        self.assertEqual(created.version, 2)
        self.assertEqual(revoke_session.flushed, 1)

    def test_only_supported_action_types_can_enter_the_queue(self) -> None:
        self.assertEqual(
            SUPPORTED_ACCOUNT_ACTION_TYPES,
            {
                "send_private_message",
                "publish_text_post",
                "follow_user",
                "unfollow_user",
                "browse_online_users",
                "request_text_match",
                "request_friend",
            },
        )
        for value in ("send_reply", "like_content", "delete_account", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _normalize_action_type(value)

    def test_action_snapshots_and_result_identifiers_are_not_generically_serialized(self) -> None:
        secret_body = "private message body"
        secret_target = "private target identifier"
        execution = AiAgentActionExecution(
            id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            action_type="send_private_message",
            status="succeeded",
            idempotency_key="private-idempotency-key",
            execution_setting_version=1,
            target_snapshot={"peer": secret_target},
            parameter_snapshot={"text": secret_body},
            approval_source="user_explicit",
            trigger_source="model",
            external_result_id="private-external-result-id",
        )

        serialized = execution.to_dict()
        for field in (
            "idempotency_key",
            "target_snapshot",
            "parameter_snapshot",
            "external_result_id",
        ):
            self.assertNotIn(field, serialized)
        self.assertNotIn(secret_body, repr(serialized))
        self.assertNotIn(secret_target, repr(serialized))

    def test_state_transitions_preserve_running_actions_during_revocation(self) -> None:
        owner_id = uuid.uuid4()
        execution_id = uuid.uuid4()
        queued = AiAgentActionExecution(
            id=execution_id,
            owner_user_id=owner_id,
            external_account_id=uuid.uuid4(),
            action_type="follow_user",
            status="queued",
            idempotency_key="queued-action-key",
            execution_setting_version=1,
            target_snapshot={"peer": "target"},
            parameter_snapshot={},
            approval_source="user_allowlist",
            trigger_source="model",
        )
        queue_session = _ScalarSession(queued)
        cancelled = AgentActionExecutionRepository(queue_session).cancel_queued(
            owner_id,
            execution_id,
            stable_error_code="access_revoked",
        )
        self.assertIs(cancelled, queued)
        self.assertEqual(queued.status, "cancelled")
        self.assertEqual(queued.stable_error_code, "access_revoked")
        self.assertIsNotNone(queued.completed_at)
        self.assertEqual(queued.completed_at, queued.cancelled_at)

        running = AiAgentActionExecution(
            id=execution_id,
            owner_user_id=owner_id,
            external_account_id=uuid.uuid4(),
            action_type="follow_user",
            status="running",
            idempotency_key="running-action-key",
            execution_setting_version=1,
            target_snapshot={"peer": "target"},
            parameter_snapshot={},
            approval_source="user_allowlist",
            trigger_source="model",
        )
        running_session = _ScalarSession(running)
        untouched = AgentActionExecutionRepository(
            running_session
        ).cancel_queued(
            owner_id,
            execution_id,
            stable_error_code="access_revoked",
        )
        self.assertIsNone(untouched)
        self.assertEqual(running.status, "running")
        self.assertEqual(running_session.flushed, 0)

    def test_bulk_revocation_update_filters_by_owner_and_queued_status(self) -> None:
        owner_id = uuid.uuid4()
        session = _ScalarSession(rowcount=3)
        cancelled = AgentActionExecutionRepository(
            session
        ).cancel_active_for_owner(owner_id)

        self.assertEqual(cancelled, 3)
        self.assertEqual(len(session.executed), 1)
        statement = session.executed[0]
        compiled = statement.compile(dialect=postgresql.dialect())
        params = compiled.params
        self.assertIn("queued", params.values())
        self.assertIn(owner_id, params.values())
        self.assertIn("cancelled", params.values())
        self.assertIn("access_revoked", params.values())
        self.assertNotIn("running", params.values())


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokAutonomySettingRepositoryTests(unittest.TestCase):
    _POLICY_KWARGS = {
        "user_enabled": True,
        "auto_reply_enabled": True,
        "scheduled_post_enabled": False,
        "managed_relationships_enabled": False,
        "discovery_enabled": False,
        "text_match_enabled": False,
        "proactive_message_enabled": False,
        "follow_discovered_enabled": False,
        "friend_request_enabled": False,
        "allowed_actions": ["send_private_message"],
        "operation_brief": "维持既有联系",
        "managed_target_uids": [],
        "timezone": "UTC",
        "active_start_minute": 0,
        "active_end_minute": 0,
        "minimum_action_interval_seconds": 300,
        "daily_total_limit": 20,
        "daily_reply_limit": 10,
        "daily_post_limit": 1,
        "daily_relationship_limit": 5,
        "post_interval_minutes": 1440,
        "discovery_interval_seconds": 1800,
        "consecutive_failure_limit": 3,
    }

    def _existing_row(self, owner_id: uuid.UUID) -> AiAgentAutonomySetting:
        return AiAgentAutonomySetting(
            id=uuid.uuid4(),
            owner_user_id=owner_id,
            auto_reply_started_at=datetime(2026, 7, 25, 11, 0, tzinfo=UTC),
            discovery_interval_minutes=30,
            consecutive_failures=0,
            version=7,
            **self._POLICY_KWARGS,
        )

    def test_resubmitting_unchanged_policy_clears_halt_without_version_bump(self) -> None:
        owner_id = uuid.uuid4()
        row = self._existing_row(owner_id)
        row.consecutive_failures = 3
        row.halted_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        row.halted_reason = "worker_lost"
        row.next_run_at = None
        session = _ScalarSession(row)

        result = AgentAutonomySettingRepository(session).configure(
            owner_id, **self._POLICY_KWARGS
        )

        self.assertIs(result, row)
        self.assertIsNone(row.halted_at)
        self.assertIsNone(row.halted_reason)
        self.assertEqual(row.consecutive_failures, 0)
        self.assertEqual(row.version, 7)
        self.assertIsNotNone(row.next_run_at)
        self.assertEqual(session.flushed, 1)

    def test_resubmitting_unchanged_policy_without_halt_is_a_noop(self) -> None:
        owner_id = uuid.uuid4()
        row = self._existing_row(owner_id)
        original_next_run = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)
        row.next_run_at = original_next_run
        session = _ScalarSession(row)

        result = AgentAutonomySettingRepository(session).configure(
            owner_id, **self._POLICY_KWARGS
        )

        self.assertIs(result, row)
        self.assertEqual(row.version, 7)
        self.assertEqual(row.next_run_at, original_next_run)
        self.assertEqual(session.flushed, 0)

    def test_changed_policy_still_bumps_version_and_clears_halt(self) -> None:
        owner_id = uuid.uuid4()
        row = self._existing_row(owner_id)
        row.halted_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        row.halted_reason = "consecutive_failures"
        row.consecutive_failures = 5
        session = _ScalarSession(row)
        changed_kwargs = dict(self._POLICY_KWARGS, operation_brief="新的运营目标")

        result = AgentAutonomySettingRepository(session).configure(
            owner_id, **changed_kwargs
        )

        self.assertIs(result, row)
        self.assertEqual(row.version, 8)
        self.assertIsNone(row.halted_at)
        self.assertIsNone(row.halted_reason)
        self.assertEqual(row.consecutive_failures, 0)

    def test_discovery_interval_uses_seconds_and_syncs_legacy_minutes(self) -> None:
        owner_id = uuid.uuid4()
        row = self._existing_row(owner_id)
        session = _ScalarSession(row)

        ten_second_kwargs = dict(
            self._POLICY_KWARGS,
            discovery_interval_seconds=10,
        )
        AgentAutonomySettingRepository(session).configure(
            owner_id,
            **ten_second_kwargs,
        )
        self.assertEqual(row.discovery_interval_seconds, 10)
        self.assertEqual(row.discovery_interval_minutes, 30)

        session = _ScalarSession(row)
        five_minute_kwargs = dict(
            self._POLICY_KWARGS,
            discovery_interval_seconds=300,
        )
        AgentAutonomySettingRepository(session).configure(
            owner_id,
            **five_minute_kwargs,
        )
        self.assertEqual(row.discovery_interval_seconds, 300)
        self.assertEqual(row.discovery_interval_minutes, 5)

    def test_discovery_interval_rejects_less_than_ten_seconds(self) -> None:
        owner_id = uuid.uuid4()
        row = self._existing_row(owner_id)
        session = _ScalarSession(row)
        invalid_kwargs = dict(
            self._POLICY_KWARGS,
            discovery_interval_seconds=9,
        )

        with self.assertRaisesRegex(
            ValueError,
            "autonomous discovery interval is invalid",
        ):
            AgentAutonomySettingRepository(session).configure(
                owner_id,
                **invalid_kwargs,
            )

    def test_auto_reply_watermark_resets_on_each_new_enablement(self) -> None:
        owner_id = uuid.uuid4()
        row = self._existing_row(owner_id)
        original_watermark = row.auto_reply_started_at
        session = _ScalarSession(row)
        disabled_kwargs = dict(
            self._POLICY_KWARGS,
            user_enabled=False,
            auto_reply_enabled=False,
        )

        AgentAutonomySettingRepository(session).configure(
            owner_id,
            **disabled_kwargs,
        )
        self.assertIsNone(row.auto_reply_started_at)

        session = _ScalarSession(row)
        AgentAutonomySettingRepository(session).configure(
            owner_id,
            **self._POLICY_KWARGS,
        )
        self.assertIsNotNone(row.auto_reply_started_at)
        self.assertNotEqual(row.auto_reply_started_at, original_watermark)
        self.assertEqual(row.version, 9)
        self.assertEqual(session.flushed, 1)


class _ScalarsSession:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.calls = 0

    def scalars(self, _statement: object) -> list[object]:
        self.calls += 1
        return self.rows

    def flush(self) -> None:
        self.calls += 1


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokAutonomyDailyUsageRepositoryTests(unittest.TestCase):
    def test_budget_availability_keeps_reply_capacity_when_browse_is_full(self) -> None:
        setting = SimpleNamespace(
            daily_total_limit=20,
            daily_reply_limit=10,
            daily_post_limit=1,
            daily_relationship_limit=5,
        )
        usage = SimpleNamespace(
            total_actions=13,
            reply_actions=0,
            outreach_actions=5,
            post_actions=0,
            relationship_actions=0,
            browse_actions=8,
            match_actions=0,
        )

        self.assertFalse(
            autonomy_daily_budget_available(
                usage,
                setting,
                task_type="browse_online",
                action_type="browse_online_users",
            )
        )
        self.assertTrue(
            autonomy_daily_budget_available(
                usage,
                setting,
                task_type="reply_to_message",
                action_type="send_private_message",
            )
        )

        usage.total_actions = 20
        self.assertFalse(
            autonomy_daily_budget_available(
                usage,
                setting,
                task_type="reply_to_message",
                action_type="send_private_message",
            )
        )

    def test_budget_cleanup_only_cancels_the_exhausted_category(self) -> None:
        now = datetime(2026, 7, 28, 6, 0, tzinfo=UTC)
        browse = SimpleNamespace(
            id=uuid.uuid4(),
            task_type="browse_online",
            action_type="browse_online_users",
            status="queued",
            stable_error_code=None,
            result_id=None,
            outcome_unknown=False,
            completed_at=None,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=now - timedelta(minutes=1),
        )
        reply = SimpleNamespace(
            id=uuid.uuid4(),
            task_type="reply_to_message",
            action_type="send_private_message",
            status="queued",
            stable_error_code=None,
            result_id=None,
            outcome_unknown=False,
            completed_at=None,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=now - timedelta(minutes=1),
        )
        session = _ScalarsSession([browse, reply])
        setting = SimpleNamespace(
            daily_total_limit=20,
            daily_reply_limit=10,
            daily_post_limit=1,
            daily_relationship_limit=5,
        )
        usage = SimpleNamespace(
            total_actions=13,
            reply_actions=0,
            outreach_actions=5,
            post_actions=0,
            relationship_actions=0,
            browse_actions=8,
            match_actions=0,
        )

        cancelled = AgentAutonomyTaskRepository(
            session
        ).cancel_budget_exhausted_not_started(
            owner_user_id=uuid.uuid4(),
            usage=usage,
            setting=setting,
            at=now,
        )

        self.assertEqual(cancelled, 1)
        self.assertEqual(browse.status, "cancelled")
        self.assertEqual(
            browse.stable_error_code,
            "dispatch_daily_budget_exhausted",
        )
        self.assertEqual(browse.completed_at, now)
        self.assertEqual(reply.status, "queued")

    def test_get_many_pairs_rows_by_owner_and_local_date(self) -> None:
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        matching = SimpleNamespace(owner_user_id=owner_a, usage_date=date(2026, 7, 26))
        # IN 超集会带回 owner_b 在 owner_a 本地日期的行；owner_b 期望的是
        # 另一个本地日期，必须被精确配对过滤掉。
        cross_pair = SimpleNamespace(owner_user_id=owner_b, usage_date=date(2026, 7, 26))
        session = _ScalarsSession([matching, cross_pair])

        result = AgentAutonomyDailyUsageRepository(session).get_many(
            {owner_a: date(2026, 7, 26), owner_b: date(2026, 7, 25)}
        )

        self.assertEqual(result, {owner_a: matching})
        self.assertEqual(session.calls, 1)

    def test_get_many_without_keys_skips_the_query(self) -> None:
        session = _ScalarsSession([])

        result = AgentAutonomyDailyUsageRepository(session).get_many({})

        self.assertEqual(result, {})
        self.assertEqual(session.calls, 0)


if __name__ == "__main__":
    unittest.main()
