"""RQ worker entry point for durable media and synchronization jobs."""

from __future__ import annotations

import os

from redis import Redis
from rq import Queue, Worker

from bbw_prod.config import get_settings
from bbw_prod.crypto import CredentialCipher


def main() -> int:
    settings = get_settings()
    # Transcode-only jobs handle already-public CDN URLs and do not need the
    # application credential keyring. Other queues still fail closed before
    # consuming jobs when the active/historical keyring is invalid.
    if any(name != "transcode" for name in settings.rq_queues):
        CredentialCipher.from_settings(settings)
    connection = Redis.from_url(settings.redis_url)
    queues = [Queue(name, connection=connection) for name in settings.rq_queues]
    # Let RQ generate a unique worker name. A fixed name makes the normal worker
    # and isolated transcode worker collide in the same Redis registry,
    # preventing whichever service starts second.
    worker = Worker(queues, connection=connection)
    with_scheduler = str(os.getenv("BBW_RQ_WITH_SCHEDULER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # Delayed RQ Retry intervals use ScheduledJobRegistry. Only the isolated
    # transcode worker enables the built-in scheduler; the separate bbw
    # application scheduler does not promote RQ's scheduled jobs.
    worker.work(with_scheduler=with_scheduler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
