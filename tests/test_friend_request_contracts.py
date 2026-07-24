from __future__ import annotations

import ast
from contextlib import contextmanager
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_protocol.client import ApiResult  # noqa: E402
from bbw_protocol.modules.social import SocialAPI  # noqa: E402
from bbw_web import bff_server as BFF  # noqa: E402
from bbw_web.jobs import record_product_events_batch  # noqa: E402


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
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "PRODUCT_RELATIONSHIP_MAP"
                for target in node.targets
            )
        )
        relationship_map = ast.literal_eval(assignment.value)

        self.assertEqual(
            relationship_map["/api/social/add-friend"],
            ("friend_request", "active"),
        )

    def test_product_event_batch_reuses_owner_binding_and_transaction(self) -> None:
        owner_id = uuid.uuid4()
        db = Mock()
        user = SimpleNamespace(id=owner_id)
        account = SimpleNamespace(upstream_uid="42")
        event_repository = Mock()
        relationship_repository = Mock()
        payloads = [
            {"path": "/api/social/follow", "status": 200},
            {"path": "/api/social/visit", "status": 200},
        ]

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.jobs.session_scope", new=fake_session_scope),
            patch(
                "bbw_web.jobs._load_owner_binding", return_value=(user, account)
            ) as load_binding,
            patch(
                "bbw_web.jobs.ActivityEventRepository",
                return_value=event_repository,
            ),
            patch(
                "bbw_web.jobs.RelationshipRepository",
                return_value=relationship_repository,
            ),
            patch(
                "bbw_web.jobs._record_product_event_in_session",
                side_effect=[
                    {"event_id": "event-1", "relationship_id": "relation-1"},
                    {"event_id": "event-2", "relationship_id": None},
                ],
            ) as record_event,
        ):
            result = record_product_events_batch(str(owner_id), payloads)

        load_binding.assert_called_once_with(db, owner_id, None)
        self.assertEqual(record_event.call_count, 2)
        self.assertIs(
            record_event.call_args_list[0].kwargs["relationship_cache"],
            record_event.call_args_list[1].kwargs["relationship_cache"],
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["relationship_updates"], 1)

    def test_product_event_batch_marks_conversations_read_off_request_path(self) -> None:
        owner_id = uuid.uuid4()
        db = Mock()
        user = SimpleNamespace(id=owner_id)
        account = SimpleNamespace(upstream_uid="42")
        event_repository = Mock()
        event_repository.insert_idempotent.return_value = SimpleNamespace(
            id=uuid.uuid4()
        )
        conversation_repository = Mock()
        conversation_repository.mark_peers_read.return_value = 2

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.jobs.session_scope", new=fake_session_scope),
            patch(
                "bbw_web.jobs._load_owner_binding", return_value=(user, account)
            ),
            patch(
                "bbw_web.jobs.ActivityEventRepository",
                return_value=event_repository,
            ),
            patch("bbw_web.jobs.RelationshipRepository", return_value=Mock()),
            patch(
                "bbw_web.jobs.ConversationRepository",
                return_value=conversation_repository,
            ),
        ):
            result = record_product_events_batch(
                str(owner_id),
                [
                    {
                        "path": "/api/im/read",
                        "status": 200,
                        "request": {"peers": ["9", "10", "9"]},
                        "response": {"ok": True, "read_peers": ["9", "10"]},
                    }
                ],
            )

        args = conversation_repository.mark_peers_read.call_args.args
        self.assertEqual(args, (owner_id, ["9", "10"]))
        self.assertIn("observed_at", conversation_repository.mark_peers_read.call_args.kwargs)
        self.assertEqual(result["conversations_marked_read"], 2)

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
