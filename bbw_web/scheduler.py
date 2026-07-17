"""Low-memory scheduler that enqueues reconciliation and retention jobs."""

from __future__ import annotations

import os
import time

from redis import Redis
from rq import Queue
from rq.exceptions import InvalidJobOperation

LOCK_TTL_SECONDS = 55


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
        job_id=f"schedule-syncs:{slot}",
        job_timeout=120,
        result_ttl=300,
        failure_ttl=86400,
    )
    if slot % 15 == 0:
        _enqueue_once(
            queue,
            "bbw_web.jobs.cleanup_expired_data",
            job_id=f"cleanup-expired:{slot // 15}",
            job_timeout=900,
            result_ttl=300,
            failure_ttl=86400,
        )


def main() -> int:
    redis_url = os.getenv("BBW_REDIS_URL", "redis://redis:6379/0")
    redis_prefix = os.getenv("BBW_REDIS_PREFIX", "bbw").strip(": ") or "bbw"
    redis = Redis.from_url(redis_url)
    queue = Queue("sync", connection=redis)
    lock_name = f"{redis_prefix}:scheduler:leader"
    last_slot = -1
    while True:
        slot = int(time.time() // 60)
        if slot == last_slot:
            time.sleep(5)
            continue
        lock = redis.lock(lock_name, timeout=LOCK_TTL_SECONDS, blocking_timeout=1)
        if lock.acquire(blocking=False):
            try:
                _tick(queue)
                last_slot = slot
            finally:
                try:
                    lock.release()
                except Exception:
                    pass
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
