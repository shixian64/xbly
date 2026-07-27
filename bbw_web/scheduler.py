"""Low-memory scheduler that enqueues reconciliation and retention jobs."""

from __future__ import annotations

import os
import time

from redis import Redis
from rq import Queue
from rq.exceptions import InvalidJobOperation

LOCK_TTL_SECONDS = 15
SCHEDULER_POLL_SECONDS = 1
AGENT_DISPATCH_INTERVAL_SECONDS = 10
AGENT_CONTROL_QUEUE = "agent-control"
AGENT_DISPATCH_JOB = "bbw_web.jobs.schedule_due_agent_runs"
AGENT_DISPATCH_JOB_ID = "agent-dispatch-due-v1"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _enqueue_once(queue: Queue, function: str, **kwargs: object) -> None:
    try:
        queue.enqueue(function, **kwargs)
    except InvalidJobOperation:
        return
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def _tick(queue: Queue) -> None:
    # Job IDs make ticks idempotent if the scheduler restarts during a minute.
    slot = int(time.time() // 60)
    _enqueue_once(
        queue,
        "bbw_web.jobs.schedule_due_syncs",
        job_id=f"schedule-syncs-{slot}",
        job_timeout=120,
        result_ttl=300,
        failure_ttl=86400,
    )
    if slot % 15 == 0:
        _enqueue_once(
            queue,
            "bbw_web.jobs.cleanup_expired_data",
            job_id=f"cleanup-expired-{slot // 15}",
            job_timeout=900,
            result_ttl=300,
            failure_ttl=86400,
        )


def _agent_tick(agent_control_queue: Queue) -> None:
    if not _env_bool("BBW_AI_AGENT_BACKGROUND_ENABLED", False):
        return
    # A fixed ID leaves at most one control scan queued while a worker is busy.
    # Successful scans are removed immediately so the next ten-second slot can
    # run; database idempotency remains the execution authority.
    _enqueue_once(
        agent_control_queue,
        AGENT_DISPATCH_JOB,
        job_id=AGENT_DISPATCH_JOB_ID,
        job_timeout=30,
        result_ttl=0,
        failure_ttl=30,
    )


def main() -> int:
    redis_url = os.getenv("BBW_REDIS_URL", "redis://redis:6379/0")
    redis_prefix = os.getenv("BBW_REDIS_PREFIX", "bbw").strip(": ") or "bbw"
    redis = Redis.from_url(redis_url)
    queue = Queue("sync", connection=redis)
    agent_control_queue = Queue(AGENT_CONTROL_QUEUE, connection=redis)
    lock_name = f"{redis_prefix}:scheduler:leader"
    last_sync_slot = -1
    last_agent_slot = -1
    while True:
        timestamp = time.time()
        sync_slot = int(timestamp // 60)
        agent_slot = int(timestamp // AGENT_DISPATCH_INTERVAL_SECONDS)
        sync_due = sync_slot != last_sync_slot
        agent_due = agent_slot != last_agent_slot
        if not sync_due and not agent_due:
            time.sleep(SCHEDULER_POLL_SECONDS)
            continue
        lock = redis.lock(lock_name, timeout=LOCK_TTL_SECONDS, blocking_timeout=1)
        if lock.acquire(blocking=False):
            try:
                if sync_due:
                    _tick(queue)
                    last_sync_slot = sync_slot
                if agent_due:
                    _agent_tick(agent_control_queue)
                    last_agent_slot = agent_slot
            finally:
                try:
                    lock.release()
                except Exception:
                    pass
        time.sleep(SCHEDULER_POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
