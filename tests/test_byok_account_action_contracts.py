from __future__ import annotations

import importlib.util
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    from bbw_agent.repositories import (
        AgentActionExecutionRepository,
        AgentExecutionSettingRepository,
        SUPPORTED_ACCOUNT_ACTION_TYPES,
        _normalize_action_type,
    )
    from bbw_prod.models import (
        AiAgentActionExecution,
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
        for name, expected in expected_tables.items():
            with self.subTest(table=name):
                actual_spec = table_spec(recorder.tables[name])
                actual_spec["indexes"] |= recorder.indexes.get(name, set())
                self.assertEqual(actual_spec, table_spec(expected))

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


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokAccountActionRepositoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
