from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ByokAccountActionSourceSafetyTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def test_confirmation_is_session_bound_and_atomically_consumed(self) -> None:
        api = self.read("bbw_agent/api.py")
        digest = api.split("def _confirmation_digest", 1)[1].split(
            "def _confirmation_key", 1
        )[0]
        consume_script = api.split(
            '_ACTION_CONFIRMATION_CONSUME_SCRIPT = """', 1
        )[1].split('"""', 1)[0]

        for binding in (
            '"owner_user_id": str(context.owner_user_id)',
            '"session_id": context.sid',
            '"action": command.action_type',
            '"target_upstream_uid": command.target_upstream_uid',
            '"content": command.content',
            '"visibility": command.visibility',
            '"idempotency_key": command.idempotency_key',
        ):
            self.assertIn(binding, digest)
        self.assertIn("redis.call('GET', KEYS[1])", consume_script)
        self.assertIn("current ~= ARGV[1]", consume_script)
        self.assertIn("redis.call('DEL', KEYS[1])", consume_script)

    def test_final_gate_reuses_one_transaction_and_orders_social_user_locks(self) -> None:
        api = self.read("bbw_agent/api.py")
        services = self.read("bbw_agent/services.py")
        executor = self.read("bbw_agent/action_executor.py")
        social = self.read("bbw_web/native_social_api.py")

        dispatch = api.split("def _dispatch_action_with_final_gate", 1)[1].split(
            "def _finish_action_success", 1
        )[0]
        self.assertIn("target_upstream_uid=command.target_upstream_uid", dispatch)
        self.assertIn("db=db", dispatch)

        pair_lock = services.split("def _lock_follow_execution_users", 1)[1].split(
            "def load_runtime_configuration", 1
        )[0]
        runner_access = services.split("def runner_access", 1)[1].split(
            "def require_visible_access", 1
        )[0]
        execution_access = services.split("def execution_access", 1)[1].split(
            "def require_execution_access", 1
        )[0]
        for access in (runner_access, execution_access):
            self.assertLess(
                access.index("ModelRunnerSystemSettingRepository(db).get(for_update=True)"),
                access.index("user_statement = select(User)"),
            )
        self.assertLess(
            pair_lock.index("ModelRunnerSystemSettingRepository(db).get(for_update=True)"),
            pair_lock.index("select(User)"),
        )
        self.assertIn(".order_by(User.id)", pair_lock)
        self.assertIn(".with_for_update()", pair_lock)

        self.assertIn("db: Any | None = None", executor)
        self.assertIn("db=db", executor)
        self.assertIn("db: Any | None = None", social)
        self.assertIn("nullcontext(db)", social)

    def test_fixed_vocabulary_and_reply_send_fail_closed_contracts(self) -> None:
        executor = self.read("bbw_agent/action_executor.py")
        api = self.read("bbw_agent/api.py")
        constants = executor.split("SUPPORTED_ACCOUNT_ACTIONS = (", 1)[1].split(
            ")", 1
        )[0]

        for action in (
            "SEND_PRIVATE_MESSAGE",
            "PUBLISH_TEXT_POST",
            "FOLLOW_USER",
            "UNFOLLOW_USER",
            "BROWSE_ONLINE_USERS",
            "REQUEST_TEXT_MATCH",
            "REQUEST_FRIEND",
        ):
            self.assertIn(action, constants)
        for forbidden in ("delete", "like", "http", "browser"):
            self.assertNotIn(forbidden, constants.lower())

        reply_send = api.split("def generate_and_send_reply", 1)[1]
        self.assertIn("if len(draft) > 2000:", reply_send)
        self.assertIn('"generated_message_too_long"', reply_send)
        self.assertIn("require_auto_send=True", reply_send)
        self.assertIn('prior_action.status != "queued"', reply_send)
        self.assertIn("请勿自动重试", reply_send)

        manual_execute = api.split("def execute_prepared_account_action", 1)[1].split(
            '@router.post("/connection/test")', 1
        )[0]
        self.assertIn("outcome_unknown=True", manual_execute)
        self.assertIn("请先检查账号状态后再决定是否重试", manual_execute)

    def test_message_policy_style_learning_and_token_redaction_fail_closed(self) -> None:
        executor = self.read("bbw_agent/action_executor.py")
        services = self.read("bbw_agent/services.py")
        persistence = self.read("bbw_web/persistence.py")
        crypto = self.read("bbw_prod/crypto.py")

        preflight = executor.split("def preflight_account_action", 1)[1].split(
            "def _send_private_message", 1
        )[0]
        self.assertIn("persistence.can_message_peer", preflight)
        self.assertIn("_ensure_not_blocked", preflight)

        style_plan = services.split("def build_style_analysis_plan", 1)[1].split(
            "def parse_style_profile", 1
        )[0]
        self.assertIn('metadata.get("origin")', style_plan)
        self.assertIn('client_message_key.startswith("agent:")', style_plan)

        self.assertIn('"confirmation_token"', persistence)
        self.assertIn('"confirmation_token"', crypto)


try:
    from fastapi import HTTPException

    from bbw_agent.action_executor import (
        BROWSE_ONLINE_USERS,
        FOLLOW_USER,
        PUBLISH_TEXT_POST,
        REQUEST_FRIEND,
        REQUEST_TEXT_MATCH,
        SEND_PRIVATE_MESSAGE,
        SUPPORTED_ACCOUNT_ACTIONS,
        UNFOLLOW_USER,
        AccountActionCommand,
        AccountActionError,
        _identity_view,
        normalize_account_action,
        parameter_snapshot,
        preflight_account_action,
    )
    from bbw_agent.api import (
        AgentContext,
        _confirmation_digest,
        _consume_confirmation,
        _enqueue_started_action,
        _load_confirmation,
        _store_confirmation,
    )
    from bbw_agent.services import AgentServiceError
except ImportError as exc:  # pragma: no cover - dependency-less contract runner
    DEPENDENCY_IMPORT_ERROR: ImportError | None = exc
else:
    DEPENDENCY_IMPORT_ERROR = None


class _AtomicRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def eval(self, _script: str, key_count: int, key: str, expected: str) -> int:
        if key_count != 1 or self.values.get(key) != expected:
            return 0
        del self.values[key]
        return 1


def _context(*, sid: str = "session-one", pool_open: bool = False) -> object:
    if DEPENDENCY_IMPORT_ERROR is not None:
        return SimpleNamespace()
    return AgentContext(
        owner_user_id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
        external_account_id=uuid.UUID("20000000-0000-0000-0000-000000000002"),
        upstream_uid="actor-uid",
        match_pool_online_list_enabled=pool_open,
        client_ip="127.0.0.1",
        sid=sid,
    )


def _request(redis: _AtomicRedis) -> SimpleNamespace:
    persistence = SimpleNamespace(
        redis=redis,
        session_hmac_key=b"runtime-contract-hmac-key",
    )
    state = SimpleNamespace(
        settings=SimpleNamespace(redis_prefix="test"),
        persistence=persistence,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokAccountActionRuntimeSafetyTests(unittest.TestCase):
    def test_confirmation_binds_body_owner_session_and_consumes_once(self) -> None:
        redis = _AtomicRedis()
        request = _request(redis)
        context = _context()
        command = AccountActionCommand(
            action_type=SEND_PRIVATE_MESSAGE,
            target_upstream_uid="peer-uid",
            content="private body never stored in Redis",
            visibility="public",
            idempotency_key="manual-request-0001",
        )

        token = _store_confirmation(
            request, context, command, execution_version=7
        )
        key, confirmation = _load_confirmation(request, token)
        stored = json.loads(confirmation["raw"])

        self.assertEqual(stored["owner_user_id"], str(context.owner_user_id))
        self.assertEqual(stored["execution_version"], 7)
        self.assertNotIn(command.content, confirmation["raw"])
        self.assertNotEqual(
            stored["digest"],
            _confirmation_digest(
                request,
                _context(sid="different-session"),
                command,
            ),
        )
        changed = AccountActionCommand(
            action_type=command.action_type,
            target_upstream_uid=command.target_upstream_uid,
            content="changed body",
            visibility=command.visibility,
            idempotency_key=command.idempotency_key,
        )
        self.assertNotEqual(
            stored["digest"], _confirmation_digest(request, context, changed)
        )

        _consume_confirmation(request, key, confirmation["raw"])
        with self.assertRaises(HTTPException) as raised:
            _consume_confirmation(request, key, confirmation["raw"])
        self.assertEqual(raised.exception.status_code, 409)

    def test_private_message_policy_receives_live_pool_permission_flag(self) -> None:
        captured: list[object] = []

        class Persistence:
            def can_message_peer(self, identity: object, peer: str) -> bool:
                captured.append((identity, peer))
                return True

        command = AccountActionCommand(
            action_type=SEND_PRIVATE_MESSAGE,
            target_upstream_uid="peer-uid",
            content="hello",
            visibility="public",
            idempotency_key="message-request-0001",
        )
        context = _context(pool_open=True)
        preflight_account_action(
            identity=context,
            persistence=Persistence(),
            command=command,
        )

        identity, peer = captured[0]
        self.assertEqual(peer, "peer-uid")
        self.assertTrue(identity.match_pool_online_list_enabled)
        self.assertTrue(_identity_view(context).match_pool_online_list_enabled)

    def test_only_fixed_social_actions_and_bounded_text_are_accepted(self) -> None:
        self.assertEqual(
            SUPPORTED_ACCOUNT_ACTIONS,
            (
                SEND_PRIVATE_MESSAGE,
                PUBLISH_TEXT_POST,
                FOLLOW_USER,
                UNFOLLOW_USER,
                BROWSE_ONLINE_USERS,
                REQUEST_TEXT_MATCH,
                REQUEST_FRIEND,
            ),
        )
        context = _context()
        with self.assertRaises(AccountActionError) as unsupported:
            normalize_account_action(
                identity=context,
                action_type="arbitrary_http",
                idempotency_key="unsupported-request",
            )
        self.assertEqual(unsupported.exception.code, "action_not_supported")

        with self.assertRaises(AccountActionError) as too_long:
            normalize_account_action(
                identity=context,
                action_type=SEND_PRIVATE_MESSAGE,
                target_upstream_uid="peer-uid",
                content="x" * 2001,
                idempotency_key="too-long-request",
            )
        self.assertEqual(too_long.exception.code, "action_content_too_long")

    def test_idempotency_reuse_with_different_parameters_is_rejected(self) -> None:
        context = _context()
        command = AccountActionCommand(
            action_type=SEND_PRIVATE_MESSAGE,
            target_upstream_uid="new-peer",
            content="new-content",
            visibility="public",
            idempotency_key="same-client-request",
        )
        row = SimpleNamespace(
            id=uuid.uuid4(),
            status="succeeded",
            target_snapshot={"upstream_uid": "old-peer"},
            parameter_snapshot={"schema": 1},
            execution_setting_version=3,
        )

        class Repository:
            def __init__(self, _db: object) -> None:
                pass

            def enqueue(self, **_kwargs: object) -> tuple[object, bool]:
                return row, False

        with patch("bbw_agent.api.AgentActionExecutionRepository", Repository):
            with self.assertRaises(AgentServiceError) as raised:
                _enqueue_started_action(
                    object(),
                    context=context,
                    command=command,
                    execution_idempotency_key="manual:0123456789abcdef",
                    execution_version=3,
                    approval_source="user_explicit",
                    trigger_source="user",
                )
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_action_snapshot_keeps_only_content_digest_and_length(self) -> None:
        target, parameters = parameter_snapshot(
            action_type=SEND_PRIVATE_MESSAGE,
            target_upstream_uid="peer-uid",
            content="sensitive message",
            visibility="public",
        )

        self.assertEqual(target, {"upstream_uid": "peer-uid"})
        self.assertEqual(parameters["content_char_count"], 17)
        self.assertEqual(len(parameters["content_sha256"]), 64)
        self.assertNotIn("sensitive message", repr(parameters))


if __name__ == "__main__":
    unittest.main()
