from __future__ import annotations

import threading
import unittest

from bbw_web.interactive import PresenceCoordinator, ProfileLookupCoordinator


class ProfileLookupCoordinatorTests(unittest.TestCase):
    def test_same_account_uid_is_single_flight_across_requests(self) -> None:
        coordinator = ProfileLookupCoordinator(max_workers=1, max_pending=2)
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        results: list[object] = []

        def fetch(uid: str):
            calls.append(uid)
            started.set()
            release.wait(2)
            return {"id": uid, "nickname": "测试用户"}

        def request() -> None:
            results.append(
                coordinator.fetch_many(
                    account_key="42",
                    uids=["9"],
                    fetcher=fetch,
                    budget_seconds=1,
                    max_sync=12,
                )
            )

        first = threading.Thread(target=request)
        second = threading.Thread(target=request)
        first.start()
        self.assertTrue(started.wait(1))
        second.start()
        release.set()
        first.join(2)
        second.join(2)
        coordinator.close()

        self.assertEqual(calls, ["9"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.completed["9"]["id"] == "9" for result in results))

    def test_sync_limit_preserves_contract_and_reports_deferred_uids(self) -> None:
        coordinator = ProfileLookupCoordinator(max_workers=2, max_pending=4)
        result = coordinator.fetch_many(
            account_key="42",
            uids=["1", "2", "3"],
            fetcher=lambda uid: {"id": uid},
            budget_seconds=1,
            max_sync=2,
        )
        coordinator.close()

        self.assertEqual(set(result.completed), {"1", "2"})
        self.assertEqual(result.pending, ("3",))


class PresenceCoordinatorTests(unittest.TestCase):
    def test_per_account_work_is_serialized_and_duplicate_kind_is_coalesced(self) -> None:
        coordinator = PresenceCoordinator(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[str] = []

        def first_heartbeat() -> None:
            calls.append("heartbeat-first")
            started.set()
            release.wait(2)

        self.assertTrue(coordinator.submit("42", "heartbeat", first_heartbeat))
        self.assertTrue(started.wait(1))
        self.assertTrue(
            coordinator.submit("42", "heartbeat", lambda: calls.append("heartbeat-old"))
        )
        self.assertTrue(
            coordinator.submit("42", "heartbeat", lambda: calls.append("heartbeat-latest"))
        )
        self.assertTrue(
            coordinator.submit(
                "42",
                "frontback",
                lambda: (calls.append("frontback"), finished.set()),
            )
        )
        release.set()
        self.assertTrue(finished.wait(2))
        coordinator.close()

        self.assertEqual(
            calls,
            ["heartbeat-first", "heartbeat-latest", "frontback"],
        )


if __name__ == "__main__":
    unittest.main()
