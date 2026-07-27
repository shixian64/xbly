from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ByokAgentSchedulerSourceContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def test_scheduler_uses_the_existing_leader_and_a_single_control_job(self) -> None:
        source = self.read("bbw_web/scheduler.py")
        tree = ast.parse(source)
        tick = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_tick"
        )
        agent_tick = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_agent_tick"
        )

        self.assertEqual([arg.arg for arg in tick.args.args], ["queue"])
        self.assertEqual(tick.args.kwonlyargs, [])
        self.assertEqual(
            [arg.arg for arg in agent_tick.args.args], ["agent_control_queue"]
        )
        self.assertEqual(agent_tick.args.kwonlyargs, [])
        self.assertIn('AGENT_CONTROL_QUEUE = "agent-control"', source)
        self.assertIn(
            'AGENT_DISPATCH_JOB = "bbw_web.jobs.schedule_due_agent_runs"', source
        )
        self.assertIn('AGENT_DISPATCH_JOB_ID = "agent-dispatch-due-v1"', source)
        self.assertIn("AGENT_DISPATCH_INTERVAL_SECONDS = 10", source)
        self.assertIn("SCHEDULER_POLL_SECONDS = 1", source)
        self.assertIn('"BBW_AI_AGENT_BACKGROUND_ENABLED", False', source)
        self.assertIn("job_timeout=30", source)
        self.assertIn("result_ttl=0", source)
        self.assertIn("failure_ttl=30", source)
        self.assertIn("Queue(AGENT_CONTROL_QUEUE, connection=redis)", source)
        self.assertIn('lock_name = f"{redis_prefix}:scheduler:leader"', source)
        self.assertIn("LOCK_TTL_SECONDS = 15", source)
        self.assertIn(
            "agent_slot = int(timestamp // AGENT_DISPATCH_INTERVAL_SECONDS)", source
        )
        self.assertIn("time.sleep(SCHEDULER_POLL_SECONDS)", source)
        self.assertNotIn("from bbw_web.jobs import", source)

    def test_settings_are_fail_closed_and_bounded(self) -> None:
        source = self.read("bbw_prod/config.py")

        for field in (
            "ai_agent_background_enabled: bool",
            "ai_agent_dispatch_batch_size: int",
            "ai_agent_failure_backoff_seconds: int",
        ):
            self.assertIn(field, source)
        self.assertIn('"BBW_AI_AGENT_BACKGROUND_ENABLED", False', source)
        batch = source.split("ai_agent_dispatch_batch_size=_env_int(", 1)[1].split(
            "),", 1
        )[0]
        self.assertIn('"BBW_AI_AGENT_DISPATCH_BATCH_SIZE"', batch)
        self.assertIn("minimum=1", batch)
        self.assertIn("maximum=50", batch)
        backoff = source.split(
            "ai_agent_failure_backoff_seconds=_env_int(", 1
        )[1].split("),", 1)[0]
        self.assertIn('"BBW_AI_AGENT_FAILURE_BACKOFF_SECONDS"', backoff)
        self.assertIn("minimum=60", backoff)
        self.assertIn("maximum=3600", backoff)

    def test_compose_isolates_agent_work_and_allows_a_guarded_shutdown(self) -> None:
        compose = self.read("compose.yaml")
        agent = compose.split("\n  agent-worker:", 1)[1].split(
            "\n  transcode-worker:", 1
        )[0]
        scheduler = compose.split("\n  scheduler:", 1)[1].split("\n  caddy:", 1)[0]

        self.assertIn("BBW_RQ_QUEUES: agent-control,agent", agent)
        self.assertIn("secrets: *agent-worker-secrets", agent)
        self.assertNotIn('BBW_PHONE_HMAC_KEY_FILE: ""', agent)
        self.assertNotIn('BBW_SESSION_HMAC_KEY_FILE: ""', agent)
        for network in ("database", "queue", "egress"):
            self.assertIn(f"- {network}", agent)
        self.assertIn("stop_grace_period: 6m", agent)
        self.assertNotIn("BBW_RQ_WITH_SCHEDULER", agent)
        self.assertIn(
            'BBW_AI_AGENT_BACKGROUND_ENABLED: "${AI_AGENT_BACKGROUND_ENABLED:-false}"',
            scheduler,
        )

    def test_example_environment_keeps_background_execution_disabled(self) -> None:
        example = self.read(".env.example")

        self.assertIn("AI_AGENT_BACKGROUND_ENABLED=false", example)
        self.assertIn("AI_AGENT_DISPATCH_BATCH_SIZE=10", example)
        self.assertIn("AI_AGENT_FAILURE_BACKOFF_SECONDS=300", example)


if __name__ == "__main__":
    unittest.main()
