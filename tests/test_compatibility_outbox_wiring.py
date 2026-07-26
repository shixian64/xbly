from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from bbw_web import jobs, scheduler


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, function: str, **kwargs: object) -> None:
        self.calls.append((function, kwargs))


class CompatibilityOutboxWiringTests(unittest.TestCase):
    def test_scheduler_reaches_compatibility_through_schedule_due_syncs(self) -> None:
        queue = FakeQueue()
        with patch.object(scheduler.time, "time", return_value=120.0):
            scheduler._tick(queue)

        functions = [function for function, _kwargs in queue.calls]
        self.assertIn("bbw_web.jobs.schedule_due_syncs", functions)
        self.assertNotIn(
            "bbw_web.jobs.dispatch_due_compatibility_operations", functions
        )
        source = inspect.getsource(jobs.schedule_due_syncs)
        self.assertIn("_enqueue_compatibility_outbox_dispatch", source)
        self.assertIn('"compatibility_dispatched"', source)
        self.assertIn('"compatibility_queue_errors"', source)

    def test_schedule_helper_enqueues_one_bounded_compatibility_job(self) -> None:
        queue = FakeQueue()
        with patch.object(jobs, "Queue", return_value=queue):
            result = jobs._enqueue_compatibility_outbox_dispatch(
                connection=object()
            )

        self.assertEqual(len(queue.calls), 1)
        function, kwargs = queue.calls[0]
        self.assertEqual(
            function, "bbw_web.jobs.dispatch_due_compatibility_operations"
        )
        self.assertEqual(kwargs["job_id"], "dispatch-compatibility-outbox")
        self.assertEqual(kwargs["job_timeout"], 900)
        self.assertEqual(kwargs["result_ttl"], 30)
        self.assertEqual(kwargs["failure_ttl"], 90)
        self.assertEqual(
            result,
            {"ok": True, "dispatched": 1, "queue_errors": 0},
        )

    def test_rq_entrypoint_uses_configured_batch_and_keeps_external_failure_normal(self) -> None:
        degraded = {
            "claimed": 4,
            "completed": 1,
            "retried": 2,
            "failed": 1,
            "stale": 0,
            "completed_without_binding": 0,
        }
        with (
            patch.dict(
                "os.environ",
                {"BBW_COMPATIBILITY_OUTBOX_BATCH_SIZE": "37"},
                clear=False,
            ),
            patch(
                "bbw_web.compatibility_outbox.dispatch_due",
                return_value=degraded,
            ) as dispatch,
        ):
            result = jobs.dispatch_due_compatibility_operations()

        dispatch.assert_called_once_with(limit=37, lease_seconds=900)
        self.assertEqual(result, {"ok": True, **degraded})

    def test_explicit_batch_is_bounded_before_dispatch(self) -> None:
        empty = {
            "claimed": 0,
            "completed": 0,
            "retried": 0,
            "failed": 0,
            "stale": 0,
            "completed_without_binding": 0,
        }
        with patch(
            "bbw_web.compatibility_outbox.dispatch_due",
            return_value=empty,
        ) as dispatch:
            jobs.dispatch_due_compatibility_operations(limit=999)
        dispatch.assert_called_once_with(limit=100, lease_seconds=900)


if __name__ == "__main__":
    unittest.main()
