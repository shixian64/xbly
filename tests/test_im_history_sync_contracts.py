from __future__ import annotations

import os
import subprocess
import sys
import unittest

from bbw_protocol.adapters.tim_rest import RestResult
from bbw_web.jobs import _tim_recent_conversations, _tim_roaming_history


class TimHistorySyncContractTests(unittest.TestCase):
    def test_worker_can_import_sync_jobs_without_roomkit_secret(self) -> None:
        environment = dict(os.environ)
        environment["BBW_ROOMKIT_BUSINESS_TOKEN_FILE"] = ""

        result = subprocess.run(
            [sys.executable, "-c", "import bbw_web.jobs"],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recent_conversations_paginate_filter_and_deduplicate(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0

            def recent_contacts(self, account_uid, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return RestResult(
                        ok=True,
                        action="recentcontact/get_list",
                        data={
                            "SessionItem": [
                                {"Type": 1, "To_Account": "9", "MsgTime": 1710000020},
                                {"Type": 2, "To_Account": "group", "MsgTime": 1710000030},
                            ],
                            "CompleteFlag": 0,
                            "TimeStamp": 1710000020,
                            "StartIndex": 1,
                        },
                    )
                return RestResult(
                    ok=True,
                    action="recentcontact/get_list",
                    data={
                        "SessionItem": [
                            {"Type": 1, "To_Account": "9", "MsgTime": 1710000020},
                            {"Type": 1, "To_Account": "10", "MsgTime": 1710000010},
                            {"Type": 1, "To_Account": account_uid, "MsgTime": 1710000040},
                        ],
                        "CompleteFlag": 1,
                    },
                )

        client = Client()
        rows = _tim_recent_conversations(client, "42", max_pages=5)

        self.assertEqual(client.calls, 2)
        self.assertEqual([row["peer_id"] for row in rows], ["9", "10"])

    def test_roaming_history_merges_both_directions_and_deduplicates(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def roaming_messages(self, sender, recipient, **_kwargs):
                self.calls.append((sender, recipient))
                key = "incoming" if sender == "9" else "outgoing"
                timestamp = 1710000020 if sender == "9" else 1710000010
                return RestResult(
                    ok=True,
                    action="openim/admin_getroammsg",
                    data={
                        "MsgList": [
                            {
                                "From_Account": sender,
                                "To_Account": recipient,
                                "MsgTimeStamp": timestamp,
                                "MsgKey": key,
                                "MsgBody": [
                                    {
                                        "MsgType": "TIMTextElem",
                                        "MsgContent": {"Text": key},
                                    }
                                ],
                            },
                            {
                                "From_Account": sender,
                                "To_Account": recipient,
                                "MsgTimeStamp": timestamp,
                                "MsgKey": key,
                                "MsgBody": [
                                    {
                                        "MsgType": "TIMTextElem",
                                        "MsgContent": {"Text": key},
                                    }
                                ],
                            },
                        ],
                        "Complete": 1,
                    },
                )

        client = Client()
        rows, request_count = _tim_roaming_history(
            client,
            account_uid="42",
            peer_uid="9",
            min_time=0,
            max_time=100,
            max_messages=100,
        )

        self.assertEqual(client.calls, [("9", "42"), ("42", "9")])
        self.assertEqual(request_count, 2)
        self.assertEqual([row["msg_key"] for row in rows], ["outgoing", "incoming"])


if __name__ == "__main__":
    unittest.main()
