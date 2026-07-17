"""RQ worker entry point for durable media and synchronization jobs."""

from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from bbw_prod.config import get_settings
from bbw_prod.crypto import CredentialCipher


def main() -> int:
    settings = get_settings()
    # Fail before consuming jobs when the active key or historical keyring is
    # missing/conflicting. Otherwise the worker would look healthy and only
    # discover the problem after a credential/media job had already started.
    CredentialCipher.from_settings(settings)
    connection = Redis.from_url(settings.redis_url)
    queues = [Queue(name, connection=connection) for name in settings.rq_queues]
    worker = Worker(queues, connection=connection, name="bbw-worker")
    worker.work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
