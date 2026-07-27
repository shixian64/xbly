from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ByokAutonomousAgentSourceContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def fragment(self, source: str, start: str, end: str) -> str:
        self.assertIn(start, source)
        self.assertIn(end, source)
        return source.split(start, 1)[1].split(end, 1)[0]

    def function_source(
        self,
        relative: str,
        name: str,
        *,
        containing: str = "",
    ) -> tuple[str, ast.FunctionDef]:
        source = self.read(relative)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != name:
                continue
            segment = ast.get_source_segment(source, node)
            if segment is not None and (not containing or containing in segment):
                return segment, node
        self.fail(f"function not found: {relative}:{name}:{containing}")

    def assigned_string_constants(self, relative: str, name: str) -> set[str]:
        tree = ast.parse(self.read(relative))
        for node in tree.body:
            target = None
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and target.id == name and value is not None:
                return {
                    item.value
                    for item in ast.walk(value)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
        self.fail(f"assignment not found: {relative}:{name}")

    def assigned_attribute_names(self, relative: str, name: str) -> set[str]:
        tree = ast.parse(self.read(relative))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return {
                    item.attr
                    for item in ast.walk(node.value)
                    if isinstance(item, ast.Attribute)
                }
        self.fail(f"assignment not found: {relative}:{name}")

    def assert_false_model_field(self, source: str, name: str) -> None:
        self.assertRegex(
            source,
            re.escape(name)
            + r": Mapped\[bool\] = mapped_column\(\s*"
            + r"Boolean, nullable=False, default=False, "
            + r"server_default=text\(\"false\"\)",
        )

    def test_0016_migration_adds_fail_closed_gates_and_durable_state(self) -> None:
        source = self.read(
            "migrations/versions/20260725_0016_autonomous_social_agent.py"
        )
        self.assertIn('revision: str = "20260725_0016"', source)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0015"',
            source,
        )
        upgrade = self.fragment(source, "def upgrade() -> None:", "def downgrade() -> None:")
        for column in (
            "byok_autonomous_agent_enabled",
            "autonomous_agent_enabled",
        ):
            self.assertRegex(
                upgrade,
                rf'"{column}"[\s\S]{{0,180}}server_default=sa\.text\("false"\)',
            )

        settings = self.fragment(
            upgrade,
            'op.create_table(\n        "ai_agent_autonomy_settings"',
            'op.create_index(\n        "ix_ai_agent_autonomy_settings_due"',
        )
        for field in (
            "user_enabled",
            "auto_reply_enabled",
            "scheduled_post_enabled",
            "managed_relationships_enabled",
        ):
            self.assertRegex(
                settings,
                rf'"{field}"[\s\S]{{0,140}}server_default=sa\.text\("false"\)',
            )
        for constraint in (
            "ai_agent_autonomy_allowed_actions_supported",
            "ai_agent_autonomy_reply_requires_action",
            "ai_agent_autonomy_post_requires_action",
            "ai_agent_autonomy_relationship_requires_targets",
            "ai_agent_autonomy_enabled_has_capability",
        ):
            self.assertIn(constraint, settings)

        tasks = self.fragment(
            upgrade,
            'op.create_table(\n        "ai_agent_autonomy_tasks"',
            'op.create_index(\n        "ix_ai_agent_autonomy_tasks_due"',
        )
        self.assertIn("'dispatching'", tasks)
        self.assertIn("'manual_review'", tasks)
        self.assertIn("outcome_unknown = (status = 'manual_review')", tasks)
        self.assertIn("dispatch_started_at", tasks)
        self.assertIn(
            "dispatch_started_at IS NULL OR budget_day IS NOT NULL",
            tasks,
        )
        for field in (
            "runner_setting_version",
            "model_connection_id",
            "runner_configuration_fingerprint",
            "budget_day",
        ):
            self.assertIn(field, tasks)
        self.assertIn(
            "_replace_action_execution_unknown_constraints(",
            upgrade,
        )
        self.assertIn('"ai_agent_autonomy_daily_usage"', upgrade)

        downgrade = source.split("def downgrade() -> None:", 1)[1]
        for table in (
            "ai_agent_autonomy_daily_usage",
            "ai_agent_autonomy_tasks",
            "ai_agent_autonomy_settings",
        ):
            self.assertIn(f'op.drop_table("{table}")', downgrade)
        self.assertIn(
            'op.drop_column("users", "byok_autonomous_agent_enabled")', downgrade
        )
        self.assertIn(
            '"ai_model_runner_system_settings", "autonomous_agent_enabled"',
            downgrade,
        )
        delete_autonomy_runs = "DELETE FROM ai_agent_runs WHERE run_type IN"
        self.assertIn(delete_autonomy_runs, downgrade)
        self.assertLess(
            downgrade.index(delete_autonomy_runs),
            downgrade.index("include_autonomy=False"),
        )
        self.assertIn("include_manual_review=True", downgrade)
        self.assertNotIn(
            "SET status = 'failed' WHERE status = 'manual_review'",
            downgrade,
        )

    def test_0018_migration_adds_fixed_social_operations_and_candidate_state(self) -> None:
        source = self.read(
            "migrations/versions/20260727_0018_social_agent_operations.py"
        )
        self.assertIn('revision: str = "20260727_0018"', source)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0017"',
            source,
        )
        upgrade = self.fragment(source, "def upgrade() -> None:", "def downgrade() -> None:")
        for action in (
            "browse_online_users",
            "request_text_match",
            "request_friend",
        ):
            self.assertIn(action, source)
        for task_type in (
            "browse_online",
            "request_match",
            "proactive_message",
            "follow_discovered",
            "request_friend",
        ):
            self.assertIn(task_type, source)
        for field in (
            "discovery_enabled",
            "text_match_enabled",
            "proactive_message_enabled",
            "follow_discovered_enabled",
            "friend_request_enabled",
            "discovery_interval_minutes",
            "last_discovery_at",
            "last_match_at",
            "last_outreach_at",
            "browse_actions",
            "match_actions",
            "outreach_actions",
        ):
            self.assertIn(f'"{field}"', upgrade)
        self.assertIn('"ai_agent_discovery_candidates"', upgrade)
        self.assertIn(
            'name="uq_ai_agent_discovery_candidates_owner_target"', upgrade
        )
        downgrade = source.split("def downgrade() -> None:", 1)[1]
        self.assertIn('op.drop_table("ai_agent_discovery_candidates")', downgrade)
        self.assertIn(
            "reply_actions + post_actions + relationship_actions", downgrade
        )

    def test_0019_migration_and_scheduler_fail_closed_on_old_replies(self) -> None:
        migration = self.read(
            "migrations/versions/20260727_0019_safe_autonomy_replies.py"
        )
        self.assertIn('revision: str = "20260727_0019"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260727_0018"',
            migration,
        )
        self.assertIn('"auto_reply_started_at"', migration)
        self.assertIn("SET auto_reply_started_at = now()", migration)
        self.assertIn("ai_agent_autonomy_reply_requires_watermark", migration)

        models = self.read("bbw_prod/models.py")
        setting = self.fragment(
            models,
            "class AiAgentAutonomySetting(",
            "class AiStyleProfile(",
        )
        self.assertIn("auto_reply_started_at: Mapped[datetime | None]", setting)
        self.assertIn("ai_agent_autonomy_reply_requires_watermark", setting)

        scheduler, _ = self.function_source(
            "bbw_web/jobs.py", "_unanswered_autonomy_reply_candidates"
        )
        self.assertIn("Message.occurred_at >= not_before", scheduler)
        self.assertIn("autonomy_reply_message_is_eligible(", scheduler)
        self.assertIn("autonomy_reply_message_is_fresh(", scheduler)
        self.assertIn("Message.occurred_at.desc()", scheduler)
        self.assertNotIn("Message.occurred_at.asc()", scheduler)

        owner_scheduler, _ = self.function_source(
            "bbw_web/jobs.py", "_schedule_autonomy_owner"
        )
        self.assertIn("setting.auto_reply_started_at is not None", owner_scheduler)
        self.assertIn("tasks.has_open_task_type(", owner_scheduler)
        self.assertIn("limit=1", owner_scheduler)

    def test_0020_migration_allows_ten_second_agent_cadence(self) -> None:
        migration = self.read(
            "migrations/versions/20260727_0020_ten_second_agent_cadence.py"
        )
        self.assertIn('revision: str = "20260727_0020"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260727_0019"',
            migration,
        )
        upgrade = self.fragment(
            migration,
            "def upgrade() -> None:",
            "def downgrade() -> None:",
        )
        self.assertIn('server_default=sa.text("10")', upgrade)
        self.assertIn("SET minimum_action_interval_seconds = 10", upgrade)
        self.assertIn("WHERE minimum_action_interval_seconds = 300", upgrade)
        self.assertIn(
            "minimum_action_interval_seconds BETWEEN 10 AND 86400", upgrade
        )
        downgrade = migration.split("def downgrade() -> None:", 1)[1]
        self.assertIn("SET minimum_action_interval_seconds = 60", downgrade)
        self.assertIn('server_default=sa.text("300")', downgrade)
        self.assertIn(
            "minimum_action_interval_seconds BETWEEN 60 AND 86400", downgrade
        )

        models = self.read("bbw_prod/models.py")
        self.assertIn(
            'Integer, nullable=False, default=10, server_default="10"', models
        )
        self.assertIn(
            "minimum_action_interval_seconds BETWEEN 10 AND 86400", models
        )
        api = self.read("bbw_agent/api.py")
        self.assertIn(
            "minimum_action_interval_seconds: int = Field(default=10, ge=10, le=86400)",
            api,
        )

    def test_owner_scheduler_does_not_build_an_action_backlog(self) -> None:
        owner_scheduler, _ = self.function_source(
            "bbw_web/jobs.py", "_schedule_autonomy_owner"
        )
        self.assertIn(
            "AUTONOMY_CONTROL_INTERVAL_SECONDS = 10",
            self.read("bbw_web/jobs.py"),
        )
        self.assertIn(
            "tasks.has_open_task(owner_user_id=owner_user_id)", owner_scheduler
        )
        self.assertIn("remaining = min(1, max(1, int(task_limit)))", owner_scheduler)
        self.assertIn(
            "seconds=AUTONOMY_CONTROL_INTERVAL_SECONDS", owner_scheduler
        )

        repository, _ = self.function_source(
            "bbw_agent/repositories.py", "has_open_task"
        )
        self.assertIn(
            "AiAgentAutonomyTask.owner_user_id == owner_user_id", repository
        )
        self.assertIn("AUTONOMY_TERMINAL_STATUSES", repository)

    def test_agent_run_type_constraints_use_alembic_complete_names(self) -> None:
        for migration in (
            "migrations/versions/20260725_0015_byok_account_actions.py",
            "migrations/versions/20260725_0016_autonomous_social_agent.py",
        ):
            helper, _ = self.function_source(
                migration, "_replace_agent_run_type_constraint"
            )
            tree = ast.parse(helper)
            calls = {
                node.func.attr: node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"drop_constraint", "create_check_constraint"}
            }
            self.assertEqual(
                set(calls), {"drop_constraint", "create_check_constraint"}
            )
            for operation in calls.values():
                self.assertTrue(operation.args)
                formatted_name = operation.args[0]
                self.assertIsInstance(formatted_name, ast.Call)
                assert isinstance(formatted_name, ast.Call)
                self.assertIsInstance(formatted_name.func, ast.Attribute)
                assert isinstance(formatted_name.func, ast.Attribute)
                self.assertIsInstance(formatted_name.func.value, ast.Name)
                assert isinstance(formatted_name.func.value, ast.Name)
                self.assertEqual(formatted_name.func.value.id, "op")
                self.assertEqual(formatted_name.func.attr, "f")
                self.assertEqual(
                    ast.literal_eval(formatted_name.args[0]),
                    "ck_ai_agent_runs_ai_agent_run_type_valid",
                )

    def test_models_keep_every_autonomy_gate_and_capability_default_off(self) -> None:
        source = self.read("bbw_prod/models.py")
        user = self.fragment(source, "class User(", "class InviteCode(")
        system = self.fragment(
            source,
            "class AiModelRunnerSystemSetting(",
            "class AiModelConnection(",
        )
        setting = self.fragment(
            source,
            "class AiAgentAutonomySetting(",
            "class AiStyleProfile(",
        )
        task = self.fragment(
            source,
            "class AiAgentAutonomyTask(",
            "class AiAgentAutonomyDailyUsage(",
        )

        self.assert_false_model_field(user, "byok_autonomous_agent_enabled")
        self.assert_false_model_field(system, "autonomous_agent_enabled")
        for field in (
            "user_enabled",
            "auto_reply_enabled",
            "scheduled_post_enabled",
            "managed_relationships_enabled",
        ):
            self.assert_false_model_field(setting, field)
        self.assertIn(
            '__sensitive_fields__ = frozenset(\n        {"operation_brief", "managed_target_uids"}',
            setting,
        )
        self.assertIn('default="queued", server_default="queued"', task)
        self.assertIn('default=False, server_default=text("false")', task)
        self.assertIn(
            "dispatch_started_at IS NULL OR budget_day IS NOT NULL",
            task,
        )
        for sensitive in (
            '"source_message_identity"',
            '"target_upstream_uid"',
            '"generation_instruction"',
            '"lease_token"',
        ):
            self.assertIn(sensitive, task)

    def test_admin_and_user_routes_preserve_both_authorization_layers(self) -> None:
        admin = self.read("bbw_web/admin_api.py")
        self.assertIn('prefix="/api/admin"', admin)
        self.assertIn('@router.get("/byok-autonomous-agent")', admin)
        self.assertIn('@router.post("/byok-autonomous-agent")', admin)
        self.assertIn('@router.post("/users/{user_id}/byok-autonomous-agent")', admin)

        global_control = self.fragment(
            admin,
            '@router.post("/byok-autonomous-agent")',
            '@router.get("/users")',
        )
        self.assertIn("if body.enabled and not row.enabled:", global_control)
        self.assertIn(
            "if body.enabled and not row.account_actions_enabled:", global_control
        )
        self.assertIn("_disable_system_autonomy(", global_control)
        self.assertIn('stable_error_code="autonomous_agent_disabled"', global_control)

        user_control = self.fragment(
            admin,
            '@router.post("/users/{user_id}/byok-autonomous-agent")',
            '@router.post("/users/{user_id}/credentials")',
        )
        self.assertIn("new_enabled and not user.byok_model_runner_enabled", user_control)
        self.assertIn("new_enabled and not user.byok_account_actions_enabled", user_control)
        self.assertIn("_set_user_autonomy_grant(user, new_enabled)", user_control)
        self.assertIn("_disable_autonomy_for_owner(", user_control)
        self.assertIn("_cancel_autonomy_tasks_for_owner(", user_control)

        api = self.read("bbw_agent/api.py")
        self.assertIn('prefix="/api/agent"', api)
        self.assertIn('@router.get("/status")', api)
        body = self.fragment(api, "class AutonomySettingsBody(", "class AccountActionBody(")
        for field in (
            "user_enabled: StrictBool = False",
            "auto_reply_enabled: StrictBool = False",
            "scheduled_post_enabled: StrictBool = False",
            "managed_relationships_enabled: StrictBool = False",
        ):
            self.assertIn(field, body)
        self.assertIn("managed_target_uids: list[str]", body)
        self.assertIn("max_length=100", body)
        self.assertIn('any(character in target for character in "*?[]")', body)

        settings_route = self.fragment(
            api,
            '@router.put("/autonomy-settings")',
            '@router.get("/autonomy/tasks")',
        )
        self.assertIn("save_autonomy_settings(", settings_route)
        self.assertIn("cancel_not_started_for_owner(", settings_route)
        self.assertIn('stable_error_code="autonomy_settings_changed"', settings_route)
        tasks_route = self.fragment(
            api,
            '@router.get("/autonomy/tasks")',
            '@router.post("/actions/prepare")',
        )
        self.assertIn("if not access.visible:", tasks_route)
        self.assertIn("status_code=404", tasks_route)

        status = self.fragment(
            self.read("bbw_agent/services.py"),
            "def load_status(",
            "def save_connection(",
        )
        self.assertIn("public_autonomy = autonomy_public(", status)
        self.assertIn('"autonomy": public_autonomy', status)

    def test_save_autonomy_settings_uses_the_shared_write_lock_order(self) -> None:
        access, _ = self.function_source("bbw_agent/services.py", "autonomy_access")
        self.assertIn("system_statement = system_statement.with_for_update()", access)
        self.assertIn("user_statement = user_statement.with_for_update()", access)
        access_positions = {
            "system": access.index("system = db.scalar(system_statement)"),
            "external_account": access.index("select(ExternalAccount)"),
            "user": access.index("user = db.scalar(user_statement)"),
        }
        self.assertEqual(
            sorted(access_positions, key=access_positions.get),
            ["system", "external_account", "user"],
        )
        external_lock = access[
            access_positions["external_account"] : access_positions["user"]
        ]
        self.assertIn(".with_for_update()", external_lock)

        save, _ = self.function_source("bbw_agent/services.py", "save_autonomy_settings")
        positions = {
            "access_gate": save.index(
                "require_autonomy_access(db, owner_user_id, for_update=True)"
            ),
            "runner": save.index("runner = AgentSettingRepository(db).get("),
            "execution": save.index(
                "execution = AgentExecutionSettingRepository(db).get("
            ),
            "connection": save.index("connection = ModelConnectionRepository(db).get("),
            "autonomy": save.index("AgentAutonomySettingRepository(db).configure("),
        }
        expected = [
            "access_gate",
            "runner",
            "execution",
            "connection",
            "autonomy",
        ]
        actual = sorted(positions, key=positions.get)
        self.assertEqual(actual, expected)
        account_read = save[
            save.index("account = db.scalar(") : positions["runner"]
        ]
        self.assertIn("select(ExternalAccount)", account_read)
        self.assertNotIn(".with_for_update()", account_read)
        self.assertIn(
            "for_update=True",
            save[positions["runner"] : positions["execution"]],
        )
        self.assertIn(
            "for_update=True",
            save[positions["execution"] : positions["connection"]],
        )
        self.assertIn(
            "for_update=True",
            save[positions["connection"] : positions["autonomy"]],
        )
        configure, _ = self.function_source(
            "bbw_agent/repositories.py",
            "configure",
            containing="autonomous operation brief is too long",
        )
        self.assertIn("self.get_or_create(owner_user_id, for_update=True)", configure)

    def test_actor_and_target_rows_use_stable_sorted_locks(self) -> None:
        snapshot, _ = self.function_source(
            "bbw_agent/repositories.py", "get_policy_snapshot"
        )
        account_locks = self.fragment(
            snapshot,
            "locked_accounts =",
            "accounts_by_id =",
        )
        self.assertIn("ExternalAccount.id.in_(account_ids)", account_locks)
        self.assertLess(
            account_locks.index(".order_by(ExternalAccount.id)"),
            account_locks.index(".with_for_update()"),
        )
        user_locks = self.fragment(snapshot, "locked_users =", "users_by_id =")
        self.assertIn("User.id.in_(user_ids)", user_locks)
        self.assertLess(
            user_locks.index(".order_by(User.id)"),
            user_locks.index(".with_for_update()"),
        )

        follow_locks, _ = self.function_source(
            "bbw_agent/services.py", "_lock_follow_execution_users"
        )
        follow_account_locks = self.fragment(
            follow_locks,
            "locked_accounts =",
            "accounts_by_id =",
        )
        self.assertIn("ExternalAccount.id.in_(account_ids)", follow_account_locks)
        self.assertLess(
            follow_account_locks.index(".order_by(ExternalAccount.id)"),
            follow_account_locks.index(".with_for_update()"),
        )
        self.assertIn("User.id.in_(sorted(user_ids, key=str))", follow_locks)
        follow_user_locks = self.fragment(
            follow_locks,
            "list(\n        db.scalars(\n            select(User)",
            "\n    )\n",
        )
        self.assertLess(
            follow_user_locks.index(".order_by(User.id)"),
            follow_user_locks.index(".with_for_update()"),
        )

    def test_execution_settings_previous_version_read_is_not_prelocked(self) -> None:
        _, update = self.function_source(
            "bbw_agent/api.py", "update_execution_settings"
        )
        previous_reads = [
            node.value
            for node in ast.walk(update)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "previous"
            and isinstance(node.value, ast.Call)
        ]
        self.assertEqual(len(previous_reads), 1)
        call = ast.unparse(previous_reads[0])
        self.assertIn("AgentExecutionSettingRepository(db).get", call)
        self.assertNotIn("for_update=True", call)

    def test_scheduler_uses_dedicated_queues_and_enqueues_only_opaque_task_ids(self) -> None:
        scheduler = self.read("bbw_web/scheduler.py")
        self.assertIn('AGENT_CONTROL_QUEUE = "agent-control"', scheduler)
        self.assertIn(
            'AGENT_DISPATCH_JOB = "bbw_web.jobs.schedule_due_agent_runs"', scheduler
        )
        self.assertIn('Queue(AGENT_CONTROL_QUEUE, connection=redis)', scheduler)
        agent_tick, _ = self.function_source("bbw_web/scheduler.py", "_agent_tick")
        self.assertIn('"BBW_AI_AGENT_BACKGROUND_ENABLED", False', agent_tick)
        self.assertIn("agent_control_queue,\n        AGENT_DISPATCH_JOB", agent_tick)
        self.assertIn("result_ttl=0", agent_tick)

        schedule_source, schedule_node = self.function_source(
            "bbw_web/jobs.py", "schedule_due_agent_runs"
        )
        self.assertEqual(
            [argument.arg for argument in schedule_node.args.kwonlyargs],
            ["connection", "limit"],
        )
        self.assertIn('queue = Queue("agent", connection=redis_connection)', schedule_source)
        enqueue_calls = [
            node
            for node in ast.walk(schedule_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "enqueue"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "bbw_web.jobs.run_unattended_agent"
        ]
        self.assertEqual(len(enqueue_calls), 1)
        enqueue = enqueue_calls[0]
        self.assertEqual(len(enqueue.args), 2)
        self.assertEqual(ast.unparse(enqueue.args[1]), "str(task_id)")
        self.assertEqual(
            {keyword.arg for keyword in enqueue.keywords},
            {"job_id", "job_timeout", "result_ttl", "failure_ttl"},
        )
        enqueue_source = ast.get_source_segment(self.read("bbw_web/jobs.py"), enqueue)
        self.assertIsNotNone(enqueue_source)
        for forbidden in (
            "content",
            "body",
            "message_text",
            "generation_instruction",
            "operation_brief",
        ):
            self.assertNotIn(forbidden, str(enqueue_source))

        run_source, run_node = self.function_source(
            "bbw_web/jobs.py", "run_unattended_agent"
        )
        self.assertEqual([argument.arg for argument in run_node.args.args], ["task_id"])
        self.assertEqual(run_node.args.kwonlyargs, [])
        self.assertIsNone(run_node.args.vararg)
        self.assertIsNone(run_node.args.kwarg)
        self.assertIn('parsed_task_id = _uuid(task_id, field="task_id")', run_source)

    def test_unattended_executor_uses_only_short_lived_provider_sessions(self) -> None:
        send, _ = self.function_source("bbw_agent/action_executor.py", "_send_private_message")
        publish, _ = self.function_source("bbw_agent/action_executor.py", "_publish_text_post")
        follow, _ = self.function_source("bbw_agent/action_executor.py", "_follow_action")
        browse, _ = self.function_source("bbw_agent/action_executor.py", "_browse_online_users")
        match, _ = self.function_source("bbw_agent/action_executor.py", "_request_text_match")
        friend, _ = self.function_source("bbw_agent/action_executor.py", "_request_friend")
        for operation in (send, publish, follow, browse, match, friend):
            self.assertIn("_provider_web_user", operation)
            self.assertIn("_close_restored_web_user", operation)

        provider, _ = self.function_source(
            "bbw_agent/action_executor.py", "_provider_web_user"
        )
        self.assertIn("restore_agent_web_user", provider)
        self.assertIn("reauth_callback = None", provider)
        self.assertIn('"external_session_unavailable"', provider)
        self.assertIn('"external_channel_disabled"', provider)

        dispatcher, _ = self.function_source(
            "bbw_agent/runtime.py",
            "execute_fixed_action",
            containing="allow_external_fallback=True",
        )
        self.assertGreaterEqual(dispatcher.count("allow_external_fallback=True"), 2)
        context = self.fragment(
            self.read("bbw_agent/runtime.py"),
            "class AgentAutonomyDispatchContext:",
            "class AgentAutonomyDispatchGate(",
        )
        self.assertIn('return ""', context)
        self.assertIn("never restore a browser cookie session", context)

        worker, _ = self.function_source("bbw_web/jobs.py", "run_unattended_agent")
        self.assertIn("short-lived provider session", worker)
        self.assertNotIn("cookie", worker.lower())

    def test_side_effect_unknown_and_execution_updates_are_fail_closed(self) -> None:
        dispatcher, _ = self.function_source(
            "bbw_agent/runtime.py",
            "execute_fixed_action",
            containing="allow_external_fallback=True",
        )
        self.assertIn("invoked = False", dispatcher)
        self.assertGreaterEqual(dispatcher.count("invoked = True"), 2)
        self.assertIn("prepare_execution(", dispatcher)
        self.assertLess(
            dispatcher.index("prepared = prepare_execution("),
            dispatcher.index("with self.dispatch_gate.lock_dispatch_permit(",
                dispatcher.index("prepared = prepare_execution(")),
        )
        self.assertIn("getattr(exc, \"outcome_unknown\", False)", dispatcher)
        self.assertIn(") or not code", dispatcher)
        self.assertIn(
            "FixedActionOutcome.OUTCOME_UNKNOWN\n                            if outcome_unknown",
            dispatcher,
        )
        self.assertIn("if not invoked:", dispatcher)
        self.assertIn(
            "FixedActionOutcome.OUTCOME_UNKNOWN,\n                stable_error_code=code or \"dispatch_outcome_unknown\"",
            dispatcher,
        )

        self.assertIn("executions.mark_outcome_unknown(", dispatcher)
        self.assertIn("else executions.fail(", dispatcher)
        self.assertIn("if finished is None:", dispatcher)
        self.assertIn("succeeded = executions.succeed(", dispatcher)
        self.assertIn("if succeeded is None:", dispatcher)
        execution_succeed, _ = self.function_source(
            "bbw_agent/repositories.py",
            "succeed",
            containing="row.external_result_id = result_id[:256] or None",
        )
        execution_fail, _ = self.function_source(
            "bbw_agent/repositories.py",
            "fail",
            containing='fallback="account_action_failed"',
        )
        execution_unknown, _ = self.function_source(
            "bbw_agent/repositories.py",
            "mark_outcome_unknown",
        )
        self.assertIn('row.status = "manual_review"', execution_unknown)
        for terminal_update in (
            execution_succeed,
            execution_fail,
            execution_unknown,
        ):
            self.assertIn("self.db.flush()", terminal_update)
            self.assertIn("return row", terminal_update)

        update_route, _ = self.function_source(
            "bbw_agent/api.py", "update_execution_settings"
        )
        self.assertIn("public_execution = execution_settings_public(", update_route)
        self.assertIn(
            'return {"ok": True, "execution": public_execution}', update_route
        )

    def test_model_runs_are_linked_before_calls_and_persist_terminal_state(self) -> None:
        begin, _ = self.function_source("bbw_agent/runtime.py", "_begin_model_run")
        positions = {
            "create": begin.index("run, created = runs.enqueue_running("),
            "link": begin.index("linked = AgentAutonomyTaskRepository(db).record_model_run_id("),
            "guard": begin.index("if not linked:"),
        }
        self.assertEqual(
            sorted(positions, key=positions.get), ["create", "link", "guard"]
        )
        self.assertIn("idempotency_key=task.idempotency_key", begin)
        self.assertIn("runtime.configuration_fingerprint", begin)
        self.assertIn('"model_run_link_failed"', begin)
        self.assertIn('if run.status == "succeeded" and run.output_text and same_runtime:', begin)
        self.assertNotIn("runs.fail(", begin)

        record, _ = self.function_source(
            "bbw_agent/repositories.py", "record_model_run_id"
        )
        self.assertIn('AiAgentAutonomyTask.status == "generating"', record)
        self.assertIn("AiAgentAutonomyTask.lease_token == lease_token", record)
        self.assertIn("AiAgentAutonomyTask.model_run_id.is_(None)", record)
        self.assertIn("model_run_id=model_run_id", record)
        self.assertIn("return bool(result.rowcount)", record)

        succeed, _ = self.function_source("bbw_agent/runtime.py", "_succeed_model_run")
        fail, _ = self.function_source("bbw_agent/runtime.py", "_fail_model_run")
        self.assertIn("finished = AgentRunRepository(db).succeed(", succeed)
        self.assertIn("if finished is None:", succeed)
        self.assertIn('"model_run_cancelled"', succeed)
        self.assertIn("AgentRunRepository(db).fail(", fail)

        reply_generation, _ = self.function_source(
            "bbw_agent/runtime.py", "generate_reply"
        )
        self.assertLess(
            reply_generation.index("self._begin_model_run("),
            reply_generation.index("self._complete_social_text("),
        )
        self.assertLess(
            reply_generation.index("self._complete_social_text("),
            reply_generation.index("self._succeed_model_run("),
        )
        self.assertIn("self._fail_model_run(", reply_generation)

        post_generation, _ = self.function_source(
            "bbw_agent/runtime.py", "generate_scheduled_post"
        )
        self.assertLess(
            post_generation.index("self._begin_model_run("),
            post_generation.index("self.gateway_factory(self.settings).complete("),
        )
        self.assertLess(
            post_generation.index("self.gateway_factory(self.settings).complete("),
            post_generation.index("self._succeed_model_run("),
        )
        self.assertIn("self._fail_model_run(", post_generation)

        run_succeed, _ = self.function_source(
            "bbw_agent/repositories.py",
            "succeed",
            containing="row.output_char_count = len(output_text)",
        )
        run_fail, _ = self.function_source(
            "bbw_agent/repositories.py",
            "fail",
            containing='str(failure_code or "model_failed")[:64]',
        )
        self.assertIn('row.status = "succeeded"', run_succeed)
        self.assertIn("row.output_text = output_text", run_succeed)
        self.assertIn("row.completed_at = utcnow()", run_succeed)
        self.assertIn("self.db.flush()", run_succeed)
        self.assertIn("return row", run_succeed)
        self.assertIn('row.status = "failed"', run_fail)
        self.assertIn("row.output_text = None", run_fail)
        self.assertIn("row.completed_at = utcnow()", run_fail)
        self.assertIn("self.db.flush()", run_fail)
        self.assertIn("return row", run_fail)

    def test_autonomous_reply_prompt_has_time_and_contact_boundaries(self) -> None:
        draft, _ = self.function_source(
            "bbw_agent/services.py", "build_reply_draft_plan"
        )
        for binding in (
            "AUTONOMY_REPLY_SESSION_GAP_SECONDS",
            '"occurred_at"',
            '"seconds_ago"',
            '"allowed_address_terms"',
            "sanitize_social_style_profile(",
            "AUTONOMY_NO_REPLY_SENTINEL",
            "不得续接已经中断的旧话题",
            "列表为空时完全不要称呼对方",
            "AUTONOMY_GENERATED_TEXT_STYLE_RULES",
        ):
            self.assertIn(binding, draft)

        reply, _ = self.function_source("bbw_agent/runtime.py", "generate_reply")
        self.assertIn("autonomous=True", reply)
        self.assertIn("temperature=min(runtime.temperature, 0.3)", reply)
        self.assertIn("self._complete_social_text(", reply)

        validate, _ = self.function_source(
            "bbw_agent/runtime.py", "_validated_reply_text"
        )
        self.assertIn("generated_text_filler_violations(", validate)
        self.assertIn('"generated_text_filler_overuse"', validate)

        complete, _ = self.function_source(
            "bbw_agent/runtime.py", "_complete_social_text"
        )
        self.assertIn('exc.code != "generated_text_filler_overuse"', complete)
        self.assertEqual(complete.count("gateway.complete("), 2)
        self.assertIn("temperature=min(float(temperature), 0.1)", complete)
        self.assertIn("_combined_model_completion(completion, retry)", complete)

        outreach, _ = self.function_source(
            "bbw_agent/runtime.py", "_outreach_generation_messages"
        )
        self.assertIn("AUTONOMY_GENERATED_TEXT_STYLE_RULES", outreach)
        self.assertIn("首次私信和好友申请完全不要使用", outreach)

        style, _ = self.function_source(
            "bbw_agent/services.py", "build_style_analysis_plan"
        )
        self.assertIn("不同联系人之间的昵称", style)
        self.assertIn("不得写入 summary", style)

    def test_final_gate_binds_runner_snapshot_and_atomic_reply_source(self) -> None:
        gate, _ = self.function_source(
            "bbw_agent/repositories.py",
            "_dispatch_gate_code",
        )
        for binding in (
            "connection.api_key_encrypted",
            "request.expected_runner_setting_version",
            "request.expected_model_connection_id",
            "request.expected_runner_configuration_fingerprint",
            "runner_configuration_fingerprint(agent, connection)",
            "task.task_type in AUTONOMY_DYNAMIC_CANDIDATE_TASKS",
            "AgentDiscoveryCandidateRepository(self.db).get(",
        ):
            self.assertIn(binding, gate)

        permit, _ = self.function_source(
            "bbw_agent/repositories.py",
            "lock_dispatch_permit",
        )
        for binding in (
            "action_execution_id",
            "dict(execution.target_snapshot or {}) != dict(target_snapshot)",
            "dict(execution.parameter_snapshot or {})",
            'execution.status != "running"',
        ):
            self.assertIn(binding, permit)

        self.assertIn('if task.task_type == "reply_to_message":', permit)
        self.assertIn(
            'head.message_identity != str(task.source_message_identity or "")',
            permit,
        )
        self.assertIn('elif task.task_type == "proactive_message"', permit)

        send, _ = self.function_source(
            "bbw_agent/action_executor.py", "_send_private_message"
        )
        self.assertIn("web_user.native.tim_rest.send_text", send)
        self.assertIn('cloud_custom_data={"origin": "agent"}', send)
        self.assertIn("remember_agent_external_text_message", send)
        self.assertIn("db=db", send)

        archive, _ = self.function_source(
            "bbw_web/persistence.py", "remember_agent_external_text_message"
        )
        self.assertIn('"direction": "outgoing"', archive)
        self.assertIn('"source": "agent"', archive)
        self.assertIn("_ingest_message(", archive)
        self.assertIn("nullcontext(db)", archive)

        head, _ = self.function_source(
            "bbw_agent/repositories.py",
            "get_conversation_head",
        )
        self.assertIn("Message.provider.in_(tuple(AUTONOMY_MESSAGE_PROVIDERS))", head)
        self.assertIn(
            "Conversation.provider.in_(tuple(AUTONOMY_MESSAGE_PROVIDERS))", head
        )

        execution_load, _ = self.function_source(
            "bbw_agent/services.py",
            "load_execution_configuration",
        )
        positions = {
            "runner": execution_load.index(
                "runner_settings = AgentSettingRepository(db).get("
            ),
            "execution": execution_load.index(
                "execution = AgentExecutionSettingRepository(db).get("
            ),
            "connection": execution_load.index(
                "connection = ModelConnectionRepository(db).get("
            ),
        }
        self.assertEqual(
            sorted(positions, key=positions.get),
            ["runner", "execution", "connection"],
        )
        self.assertIn(
            "for_update=for_update",
            execution_load[positions["connection"] :],
        )

    def test_dispatching_tasks_are_neither_cancelled_nor_retried(self) -> None:
        claimable = self.assigned_attribute_names(
            "bbw_agent/autonomous.py", "CLAIMABLE_TASK_STATUSES"
        )
        self.assertEqual(claimable, {"QUEUED", "DEFERRED", "LEASED", "GENERATING"})
        self.assertNotIn("DISPATCHING", claimable)

        cancellable = self.assigned_string_constants(
            "bbw_agent/repositories.py", "AUTONOMY_NOT_STARTED_STATUSES"
        )
        self.assertEqual(cancellable, {"queued", "deferred", "leased", "generating"})
        self.assertNotIn("dispatching", cancellable)

        for function in ("claim_next", "claim_task", "due_task_ids"):
            fragment, _ = self.function_source("bbw_agent/repositories.py", function)
            self.assertIn('("leased", "generating")', fragment)
            self.assertNotIn('"dispatching"', fragment)

        cancel, _ = self.function_source(
            "bbw_agent/repositories.py", "_cancel_not_started"
        )
        self.assertIn("AUTONOMY_NOT_STARTED_STATUSES", cancel)
        self.assertNotIn('"dispatching"', cancel)

        expired, _ = self.function_source(
            "bbw_agent/repositories.py", "mark_expired_dispatching_unknown"
        )
        self.assertIn('AiAgentAutonomyTask.status == "dispatching"', expired)
        self.assertIn('task.status = "manual_review"', expired)
        self.assertIn("task.outcome_unknown = outcome_unknown", expired)
        self.assertIn('execution_status == "succeeded"', expired)
        self.assertIn('execution_status == "failed"', expired)
        self.assertIn("usage.outcome_unknown_actions", expired)
        self.assertIn("setting.halted_at = now", expired)
        self.assertIn("setting.next_run_at = None", expired)
        self.assertNotIn('task.status = "queued"', expired)
        self.assertNotIn(".enqueue(", expired)

    def test_ui_renders_autonomy_only_after_the_server_marks_it_visible(self) -> None:
        services = self.read("bbw_agent/services.py")
        access = self.fragment(
            services,
            "class AutonomyAccess:",
            "class ExecutionConfiguration:",
        )
        for gate in (
            "account_active",
            "runner_system_enabled",
            "runner_admin_granted",
            "account_actions_system_enabled",
            "account_actions_admin_granted",
            "system_enabled",
            "admin_granted",
        ):
            self.assertIn(f"self.{gate}", access)
        public = self.fragment(services, "def autonomy_public(", "def style_public(")
        self.assertIn("if not access.visible:", public)
        self.assertIn('return {"visible": False}', public)
        self.assertIn('"background_enabled": bool(background_enabled)', public)
        self.assertIn("and background_enabled", public)
        self.assertLess(public.index("if not access.visible:"), public.index("if row is not None:"))

        app = self.read("bbw_web/static/app.js")
        normalized = self.fragment(
            app,
            "function normalizedAiAgentAutonomyStatus(status)",
            "function setAiAgentAutonomyStatus(status)",
        )
        presentation = self.fragment(
            app,
            "function aiAgentAutonomyPresentation(autonomy)",
            "function syncAiAgentAutonomyStatusUi",
        )
        section = self.fragment(
            app,
            "function agentAutonomySectionHtml(autonomy)",
            "function agentExecutionSectionHtml(execution)",
        )
        page = self.fragment(app, "async function pageAgent(signal,", "async function pageLab()")
        css = self.read("bbw_web/static/app.css")
        self.assertIn("source.visible !== true", normalized)
        self.assertIn("background_enabled: source.background_enabled === true", normalized)
        self.assertIn('if (!autonomy?.visible) return "";', section)
        self.assertIn("自动社交 Agent 正在运行", presentation)
        self.assertIn('? "已自动停机"', presentation)
        self.assertLess(
            presentation.index('? "已自动停机"'),
            presentation.index('? "正在运行"'),
        )
        self.assertIn("agentAutonomySectionHtml(autonomy)", page)
        self.assertNotIn("agentExecutionSectionHtml(", page)
        for removed_surface in ("回复草稿", "手动账号工具", "二次确认"):
            self.assertNotIn(removed_surface, page)
        for hierarchy in (
            "发现新用户",
            "推进关系",
            "维护对话",
            "交流原则",
            "今日统计",
            "最近动作",
        ):
            self.assertIn(hierarchy, section)
        self.assertIn("仅展示已完成动作，不提供预算设置", section)
        self.assertNotIn("不限制每日预算", section)
        self.assertIn("模型与表达设置", page)
        self.assertIn("个人运行器", page)
        self.assertIn("语言风格", page)
        for selector in (
            ".agent-social-workspace",
            ".agent-capability-groups",
            ".agent-capability-group",
            ".agent-social-side",
            ".agent-settings-grid",
            ".agent-settings-side",
        ):
            self.assertIn(selector, css)
        for field in (
            "discovery_enabled",
            "text_match_enabled",
            "follow_discovered_enabled",
            "friend_request_enabled",
            "proactive_message_enabled",
            "auto_reply_enabled",
        ):
            self.assertIn(f'"{field}"', section)
        self.assertIn('name="${name}"', section)
        for hidden_budget in (
            "daily_total_limit",
            "daily_reply_limit",
            "daily_post_limit",
            "daily_relationship_limit",
        ):
            self.assertNotIn(f'name="{hidden_budget}"', section)
        for statistic in ("浏览", "匹配", "上下文回复", "主动私信", "关系操作"):
            self.assertIn(statistic, section)
        self.assertNotIn("无人值守", page)
        self.assertNotIn("等待人工检查", section)
        self.assertNotIn("等待人工检查", page)
        self.assertIn("自动停止重试并保留记录", section)
        self.assertIn('data-action="agent-refresh-autonomy-tasks"', section)
        self.assertIn('id="agent-autonomy-tasks"', section)
        self.assertIn('"/api/agent/autonomy/tasks?limit=50"', app)
        refresh_handler = self.fragment(
            app,
            'if (action === "agent-refresh-autonomy-tasks") {',
            'if (action === "match-tab") {',
        )
        self.assertIn(
            'await agentAutonomyApi("/api/agent/autonomy/tasks?limit=50"',
            refresh_handler,
        )
        self.assertNotIn("await agentApi(", refresh_handler)
        self.assertIn(
            'if ($("agent-autonomy-tasks") === container) container.innerHTML = previous;',
            refresh_handler,
        )
        self.assertIn("if (refreshed !== container) return;", refresh_handler)

        index = self.read("bbw_web/static/index.html")
        match = re.search(r'/static/app\.js\?v=([0-9a-f]{16})', index)
        self.assertIsNotNone(match)
        expected = hashlib.sha256(
            (ROOT / "bbw_web/static/app.js").read_bytes()
        ).hexdigest()[:16]
        self.assertEqual(match.group(1), expected)

        css_match = re.search(r'/static/app\.css\?v=([0-9a-f]{16})', index)
        self.assertIsNotNone(css_match)
        css_expected = hashlib.sha256(
            (ROOT / "bbw_web/static/app.css").read_bytes()
        ).hexdigest()[:16]
        self.assertEqual(css_match.group(1), css_expected)

    def test_mobile_agent_capability_checkbox_keeps_native_control_size(self) -> None:
        css = self.read("bbw_web/static/app.css")
        wrapping = self.fragment(
            css,
            ".agent-page .notice,",
            ".agent-overview {",
        )
        checkbox = self.fragment(
            css,
            '.agent-social-capability input[type="checkbox"] {',
            '.agent-social-capability input[type="checkbox"]:focus-visible {',
        )
        self.assertIn(".agent-page .result-panel", wrapping)
        self.assertIn("max-width: 100%", wrapping)
        self.assertIn("overflow-wrap: anywhere", wrapping)
        self.assertIn("width: 18px", checkbox)
        self.assertIn("min-width: 18px", checkbox)
        self.assertIn("height: 18px", checkbox)
        self.assertIn("flex: 0 0 18px", checkbox)
        self.assertIn("padding: 0", checkbox)

    def test_external_social_results_are_archived_in_normal_domain_records(self) -> None:
        match, _ = self.function_source(
            "bbw_agent/action_executor.py", "_request_text_match"
        )
        for call in (
            "_remember_discovery_candidates(",
            "persistence.grant_message_peers(",
            "persistence.remember_match_history_response(",
        ):
            self.assertIn(call, match)
        self.assertIn("db=db", match)

        follow, _ = self.function_source("bbw_agent/action_executor.py", "_follow_action")
        friend, _ = self.function_source("bbw_agent/action_executor.py", "_request_friend")
        self.assertIn("remember_agent_external_social_action", follow)
        self.assertIn("remember_agent_external_social_action", friend)

        persistence = self.read("bbw_web/persistence.py")
        social_archive, _ = self.function_source(
            "bbw_web/persistence.py", "remember_agent_external_social_action"
        )
        for relationship_kind in ('"follow"', '"friend_request"'):
            self.assertIn(relationship_kind, social_archive)
        self.assertIn("RelationshipRepository(action_db).upsert(", social_archive)
        self.assertIn('"agent_managed": True', social_archive)
        self.assertIn("remember_agent_external_text_message", persistence)

        candidates = self.fragment(
            self.read("bbw_agent/repositories.py"),
            "class AgentDiscoveryCandidateRepository:",
            "class AgentAutonomySettingRepository:",
        )
        self.assertIn(
            'constraint="uq_ai_agent_discovery_candidates_owner_target"',
            candidates,
        )
        self.assertIn("statement.on_conflict_do_update(", candidates)
        self.assertIn("seen: set[str] = set()", candidates)

    def test_style_profile_uses_an_existing_timestamp_formatter(self) -> None:
        app = self.read("bbw_web/static/app.js")
        style = self.fragment(
            app,
            "function agentStyleProfileHtml(profile)",
            "function aiAgentExecutionActionLabel(action)",
        )

        self.assertIn("formatBottleTime(profile.generated_at)", style)
        self.assertNotIn("formatDate(", app)


if __name__ == "__main__":
    unittest.main()
