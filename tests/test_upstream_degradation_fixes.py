from __future__ import annotations

import logging
import threading
import unittest
import uuid
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from bbw_protocol.adapters.jd_chat import JdChatClient, JdChatResult
from bbw_protocol.adapters.tim_rest import RestResult
from bbw_protocol.client import ApiResult
from bbw_web import bff_server, jobs
from bbw_web.dependency_health import (
    DependencyCircuitBreakerRegistry,
    DependencyCircuitOpen,
    DependencyDeadlineExceeded,
)
from bbw_web.interactive import PresenceCoordinator


class UpstreamDegradationResilienceTests(unittest.TestCase):
    def test_jd_transport_timeout_sets_negative_status_and_trips_breaker(self) -> None:
        client = JdChatClient("valid-token", base_url="https://example.invalid")
        try:
            with patch.object(
                client._http,
                "request",
                side_effect=httpx.ReadTimeout("The read operation timed out"),
            ):
                res = client.conversations(timeout=1.0)
            self.assertFalse(res.ok)
            self.assertEqual(res.status_code, -1)
            self.assertTrue(bff_server._dependency_retryable(res))

            missing_tok = JdChatClient("", base_url="https://example.invalid")
            try:
                rejected = missing_tok.conversations(timeout=1.0)
                self.assertEqual(rejected.status_code, 0)
                self.assertFalse(bff_server._dependency_retryable(rejected))
            finally:
                missing_tok.close()

            breakers = DependencyCircuitBreakerRegistry(
                failure_threshold=2,
                failure_window_seconds=60.0,
                cooldown_seconds=30.0,
            )
            with patch.object(
                client._http,
                "request",
                side_effect=httpx.ReadTimeout("The read operation timed out"),
            ):
                bff_server._jd_dependency_call(
                    client.conversations,
                    provider="jd-chat",
                    domain="im-read",
                    breakers=breakers,
                    timeout=1.0,
                )
                bff_server._jd_dependency_call(
                    client.conversations,
                    provider="jd-chat",
                    domain="im-read",
                    breakers=breakers,
                    timeout=1.0,
                )
                self.assertGreater(breakers.retry_after("jd-chat", "im-read"), 0)
                with self.assertRaises(DependencyCircuitOpen):
                    bff_server._jd_dependency_call(
                        client.conversations,
                        provider="jd-chat",
                        domain="im-read",
                        breakers=breakers,
                        timeout=1.0,
                    )
        finally:
            client.close()

    def test_tim_suspension_error_60020_trips_breaker(self) -> None:
        suspended = RestResult(
            ok=False,
            action="recentcontact/get_list",
            error_code=60020,
            error_info="Service suspended",
        )
        self.assertTrue(bff_server._dependency_retryable(suspended))
        self.assertTrue(
            bff_server._dependency_retryable({"ok": False, "error_code": 60020})
        )

    def test_presence_coordinator_suppresses_circuit_open_tracebacks(self) -> None:
        coordinator = PresenceCoordinator(max_workers=1)
        done = threading.Event()
        logger = logging.getLogger("bbw_web.interactive")

        with self.assertLogs(logger, level="DEBUG") as captured:
            coordinator.submit(
                "467615",
                "frontback",
                lambda: (_ for _ in ()).throw(
                    DependencyCircuitOpen("beibeiwu", "presence", 15)
                ),
            )
            coordinator.submit(
                "467615",
                "heartbeat",
                lambda: (
                    done.set(),
                    (_ for _ in ()).throw(DependencyDeadlineExceeded("deadline")),
                ),
            )
            self.assertTrue(done.wait(2.0))
        coordinator.close()

        self.assertFalse(
            any(record.levelno >= logging.ERROR for record in captured.records)
        )

    def test_voice_cancel_is_lock_free_and_uses_circuit_breaker(self) -> None:
        self.assertEqual(
            bff_server._request_lock_mode("POST", "/api/match/voice/cancel"),
            "none",
        )
        self.assertEqual(
            bff_server._request_lock_mode("POST", "/api/match/voice/finish"),
            "none",
        )

        breakers = DependencyCircuitBreakerRegistry(
            failure_threshold=2,
            failure_window_seconds=60.0,
            cooldown_seconds=30.0,
        )
        dns_err = ApiResult(
            False, -1, "", message="[Errno -2] Name or service not known", kind="error"
        )
        calls: list[dict[str, object]] = []
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="467615"),
                match=SimpleNamespace(
                    cancel_voice=lambda **kw: calls.append(kw) or dns_err
                ),
            ),
            native=SimpleNamespace(),
            lock=threading.RLock(),
            voice_match_state={},
        )

        responses: list[tuple[int, dict[str, object]]] = []
        for _ in range(3):
            handler = SimpleNamespace(
                path="/api/match/voice/cancel",
                command="POST",
                headers={"Content-Type": "application/json"},
                _check_api_origin=lambda: True,
                _release_request_locks=lambda: None,
                user=lambda _sid: web_user,
                sid=lambda: "sid-1",
                body=lambda: {"force_remote": True},
                ok=lambda payload, status=200, **_kw: responses.append((status, payload)),
                _request_dependency_breakers=breakers,
                _request_id="req-1",
            )
            with patch.object(bff_server, "STORE", object()):
                bff_server.Handler.do_POST(handler)

        self.assertEqual(len(calls), 2)
        self.assertEqual(responses[-1][0], 502)
        self.assertEqual(responses[-1][1]["code"], "DEPENDENCY_CIRCUIT_OPEN")

    def test_sync_account_history_short_circuits_peers_on_tim_60020(self) -> None:
        owner_id = uuid.uuid4()
        account_id = uuid.uuid4()
        roaming_calls: list[str] = []

        class SuspendedTim:
            def recent_contacts(self, *_args, **_kwargs):
                return RestResult(
                    ok=False,
                    action="recentcontact/get_list",
                    error_code=60020,
                    error_info="Service suspended",
                )

            def roaming_messages(self, sender, *_args, **_kwargs):
                roaming_calls.append(sender)
                return RestResult(
                    ok=False,
                    action="openim/admin_getroammsg",
                    error_code=60020,
                    error_info="Service suspended",
                )

            def close(self) -> None:
                return None

        with ExitStack() as stack:
            patches = {
                "session_scope": Mock(side_effect=lambda: nullcontext(SimpleNamespace())),
                "_load_owner_binding": Mock(
                    return_value=(
                        SimpleNamespace(profile={}),
                        SimpleNamespace(sync_enabled=True, upstream_uid="467615"),
                    )
                ),
                "SyncCursorRepository": Mock(
                    return_value=SimpleNamespace(get=lambda *_args: None)
                ),
                "_active_owner_ids": Mock(return_value={owner_id}),
                "_durable_message_conversations": Mock(
                    return_value=[
                        {"peer_id": "101"},
                        {"peer_id": "102"},
                        {"peer_id": "103"},
                    ]
                ),
                "ingest_history_response": Mock(return_value={"conversations": 3}),
                "_save_sync_result": Mock(),
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(jobs, name, value))
            res = jobs.sync_account_history(
                str(owner_id),
                str(account_id),
                transport_factory=lambda: SuspendedTim(),
            )

        self.assertTrue(res["ok"])
        self.assertEqual(roaming_calls, [])
        self.assertEqual(res["peer_errors"], 0)


if __name__ == "__main__":
    unittest.main()
