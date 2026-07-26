from __future__ import annotations

import unittest
from types import SimpleNamespace

from bbw_web.jobs import (
    _tim_c2c_unread_counts,
    _tim_recent_conversations,
    _tim_roaming_history,
)
from bbw_web.transports import MessageHistoryTransport, MessageRecallLookupTransport


class _FakeMessageHistoryTransport:
    def __init__(self) -> None:
        self.recent_calls: list[tuple[str, dict[str, int]]] = []
        self.roaming_calls: list[tuple[str, str]] = []
        self.unread_calls: list[tuple[str, list[str]]] = []

    def recent_contacts(self, user_id: str, **cursor: int) -> object:
        self.recent_calls.append((user_id, cursor))
        return SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={
                "CompleteFlag": 1,
                "SessionItem": [
                    {
                        "Type": "C2C",
                        "To_Account": "9",
                        "MsgTime": 100,
                        "MsgSeq": 2,
                        "UnreadMsgCount": 1,
                    }
                ],
            },
        )

    def roaming_messages(
        self,
        from_account: str,
        to_account: str,
        **_params: object,
    ) -> object:
        self.roaming_calls.append((from_account, to_account))
        return SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={"Complete": 1, "MsgList": []},
        )

    def c2c_unread_counts(
        self,
        to_account: str,
        peer_accounts: list[str],
    ) -> object:
        self.unread_calls.append((to_account, peer_accounts))
        return SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={
                "C2CUnreadMsgNumList": [
                    {"Peer_Account": peer, "C2CUnreadMsgNum": 0}
                    for peer in peer_accounts
                ]
            },
        )


class MessageTransportContractTests(unittest.TestCase):
    def test_history_helpers_depend_only_on_the_neutral_transport_shape(self) -> None:
        transport = _FakeMessageHistoryTransport()
        self.assertIsInstance(transport, MessageHistoryTransport)
        self.assertIsInstance(transport, MessageRecallLookupTransport)

        conversations = _tim_recent_conversations(
            transport,
            "42",
            max_pages=1,
        )
        unread, unread_requests = _tim_c2c_unread_counts(
            transport,
            "42",
            ["9"],
        )
        messages, roaming_requests = _tim_roaming_history(
            transport,
            account_uid="42",
            peer_uid="9",
            min_time=0,
            max_time=100,
            max_messages=10,
        )

        self.assertEqual([item["peer_id"] for item in conversations], ["9"])
        self.assertEqual(unread, {"9": 0})
        self.assertEqual(unread_requests, 1)
        self.assertEqual(messages, [])
        self.assertEqual(roaming_requests, 3)
        self.assertEqual(
            transport.roaming_calls,
            [("9", "42"), ("42", "9")],
        )


if __name__ == "__main__":
    unittest.main()
