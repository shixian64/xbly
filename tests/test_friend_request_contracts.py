from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_protocol.client import ApiResult  # noqa: E402
from bbw_protocol.modules.social import SocialAPI  # noqa: E402
from bbw_web import bff_server as BFF  # noqa: E402


class FriendRequestProtocolContractTests(unittest.TestCase):
    def test_add_friend_uses_exact_v154_wire_parameters(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        class FakeClient:
            session = SimpleNamespace(uid="42")

            def call(self, action: str, params=None, **kwargs):
                body = dict(params or {})
                body.update(kwargs)
                calls.append((action, body))
                return ApiResult(True, 200, "true", data=True)

        result = SocialAPI(FakeClient()).add_friend("9", "你好，想认识你")

        self.assertTrue(result.ok)
        self.assertEqual(
            calls,
            [
                (
                    "addfriend0",
                    {
                        "type": "好友",
                        "room_master_id": "0",
                        "find_friend_id": "0",
                        "audio_friend": "",
                        "audio_during_friend": "0",
                        "myid": "9",
                        "yourid": "42",
                        "yourwords": "你好，想认识你",
                        "gift_string": "[]",
                    },
                )
            ],
        )


class FriendRequestBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = BFF.STORE
        BFF.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        BFF.STORE = self.old_store

    @staticmethod
    def _run(payload: dict[str, object]):
        calls: list[tuple[str, str]] = []
        limiter_calls: list[tuple[str, str, int, float]] = []
        result = ApiResult(True, 200, "true", data=True)
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            social=SimpleNamespace(
                add_friend=lambda uid, leave_word: calls.append((uid, leave_word)) or result,
            ),
        )
        web_user = SimpleNamespace(app=app)

        class Harness:
            path = "/api/social/add-friend"

            def __init__(self) -> None:
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return payload

            def sid(self):
                return "sid-friend-request"

            def user(self, _sid):
                return web_user

            def _allow_sensitive_action(self, action, key, *, limit, window_sec):
                limiter_calls.append((action, key, limit, window_sec))
                return True

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        BFF.Handler.do_POST(harness)
        return calls, limiter_calls, harness.response

    def test_add_friend_accepts_target_and_trimmed_message(self) -> None:
        calls, limiter_calls, response = self._run(
            {"uid": " 9 ", "leave_word": " 你好，想认识你 "}
        )

        self.assertEqual(calls, [("9", "你好，想认识你")])
        self.assertEqual(limiter_calls, [("friend-request", "42", 10, 60.0)])
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])

    def test_add_friend_rejects_self(self) -> None:
        calls, limiter_calls, response = self._run({"uid": "42", "leave_word": "你好"})

        self.assertEqual(calls, [])
        self.assertEqual(limiter_calls, [])
        self.assertEqual(response[0], 400)
        self.assertFalse(response[1]["ok"])
        self.assertEqual(response[1]["error"], "不能申请添加自己为好友")

    def test_add_friend_rejects_message_over_100_characters(self) -> None:
        calls, limiter_calls, response = self._run(
            {"uid": "9", "leave_word": "好" * 101}
        )

        self.assertEqual(calls, [])
        self.assertEqual(limiter_calls, [])
        self.assertEqual(response[0], 400)
        self.assertFalse(response[1]["ok"])
        self.assertEqual(response[1]["error"], "好友申请留言不能超过 100 个字符")


class FriendRequestPersistenceContractTests(unittest.TestCase):
    def test_add_friend_maps_to_active_friend_request_relationship(self) -> None:
        source = (ROOT / "bbw_web" / "jobs.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "record_product_event"
        )
        assignment = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "relationship_map"
                for target in node.targets
            )
        )
        relationship_map = ast.literal_eval(assignment.value)

        self.assertEqual(
            relationship_map["/api/social/add-friend"],
            ("friend_request", "active"),
        )

    def test_accepting_or_observing_friend_closes_pending_request(self) -> None:
        source = (ROOT / "bbw_web" / "jobs.py").read_text(encoding="utf-8-sig")

        agree_block = source.split('if path == "/api/social/agree-friend":', 1)[
            1
        ].split("return {", 1)[0]
        self.assertIn('kind="friend_request"', agree_block)
        self.assertIn('status="inactive"', agree_block)
        self.assertIn('"resolved_as": "accepted"', agree_block)

        snapshot_block = source.split('if route == "/api/social/friends":', 1)[
            1
        ].split('if route in {"/api/social/visitors"', 1)[0]
        self.assertIn('kind="friend_request"', snapshot_block)
        self.assertIn('status="inactive"', snapshot_block)
        self.assertIn('"resolved_as": "accepted"', snapshot_block)


if __name__ == "__main__":
    unittest.main()
