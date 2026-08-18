from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from starlette.requests import Request  # noqa: E402

from bbw_web import api as web_api  # noqa: E402
from bbw_web import bff_server  # noqa: E402
from bbw_web.providers import (  # noqa: E402
    ProviderAuthenticationRejected,
    ProviderRuntime,
    ProviderUnavailable,
    ProviderUpstreamInterrupted,
)
from bbw_web.store import SessionStore  # noqa: E402


class _Session:
    def __init__(self) -> None:
        self.uid = "0"
        self.token = "0"
        self.phone = ""
        self.password = ""
        self.nickname = ""

    @property
    def logged_in(self) -> bool:
        return self.uid != "0" and self.token != "0"


class _Client:
    def __init__(self) -> None:
        self.close_calls = 0
        self.timeout = 30.0

    def close(self) -> None:
        self.close_calls += 1


class _Auth:
    def __init__(self, session: _Session, outcome: object, client: _Client) -> None:
        self.session = session
        self.outcome = outcome
        self.client = client
        self.onekey_timeouts: list[float] = []

    def login_password(self, _phone: str, _password: str) -> object:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if bool(getattr(self.outcome, "authenticate_session", False)):
            self.session.uid = "42"
            self.session.token = "token-42"
        return self.outcome

    def login_onekey(self, _phone: str) -> object:
        self.onekey_timeouts.append(float(self.client.timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if bool(getattr(self.outcome, "authenticate_session", False)):
            self.session.uid = "42"
            self.session.token = "token-42"
        return self.outcome


class _Application:
    def __init__(self, outcome: object) -> None:
        self.session = _Session()
        self.client = _Client()
        self.auth = _Auth(self.session, outcome, self.client)

    def set_device(self, **_kwargs: str) -> dict[str, str]:
        return {}


class _Provider:
    provider_id = "test-provider"

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.runtime: ProviderRuntime | None = None

    def create_runtime(self, session: object = None) -> ProviderRuntime:
        del session
        app = _Application(self.outcome)
        self.runtime = ProviderRuntime(
            provider_id=self.provider_id,
            app=app,
            native=SimpleNamespace(app=app),
        )
        return self.runtime

    def create_runtime_from_state(self, _state: object) -> ProviderRuntime:
        return self.create_runtime()

    def load_runtime(self, _path: object = None) -> ProviderRuntime:
        return self.create_runtime()


def _result(status: int, *, ok: bool = False, authenticated: bool = False) -> object:
    return SimpleNamespace(
        ok=ok,
        status=status,
        raw="provider response",
        code="AUTH_RESULT",
        message="provider authentication result",
        authenticate_session=authenticated,
    )


class SessionStoreAuthenticationBoundaryTests(unittest.TestCase):
    def _login_error(self, outcome: object) -> tuple[Exception, _Provider, SessionStore]:
        provider = _Provider(outcome)
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(store.close)
        try:
            store.login_password(None, "13800138000", "password")
        except Exception as exc:
            return exc, provider, store
        self.fail("login unexpectedly succeeded")

    def _onekey_error(self, outcome: object) -> tuple[Exception, _Provider, SessionStore]:
        provider = _Provider(outcome)
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(store.close)
        try:
            store.login_onekey(
                None,
                "13800138000",
                request_authorized=True,
            )
        except Exception as exc:
            return exc, provider, store
        self.fail("one-key login unexpectedly succeeded")

    def test_onekey_login_uses_and_restores_the_authentication_timeout(self) -> None:
        provider = _Provider(_result(200, ok=True, authenticated=True))
        store = SessionStore(
            runtime_provider=provider,
            auto_heartbeat=False,
            upstream_auth_timeout_sec=4.0,
        )
        self.addCleanup(store.close)

        store.login_onekey(
            None,
            "13800138000",
            request_authorized=True,
        )

        assert provider.runtime is not None
        self.assertEqual(provider.runtime.app.auth.onekey_timeouts, [4.0])
        self.assertEqual(provider.runtime.app.client.timeout, 30.0)

    def test_onekey_login_maps_transport_and_server_results_to_unavailable(self) -> None:
        for status in (-1, -7, 500, 502, 503, 599):
            with self.subTest(status=status):
                error, provider, store = self._onekey_error(_result(status))
                self.assertIsInstance(error, ProviderUnavailable)
                self.assertEqual(error.upstream_status, status)
                self.assertEqual(store.users, {})
                assert provider.runtime is not None
                self.assertEqual(provider.runtime.app.client.timeout, 30.0)
                self.assertEqual(provider.runtime.app.client.close_calls, 1)

    def test_onekey_login_maps_business_results_to_authentication_rejected(self) -> None:
        for status in (0, 200, 400, 401, 403, 404, 429, 499):
            with self.subTest(status=status):
                error, _provider, _store = self._onekey_error(_result(status))
                self.assertIsInstance(error, ProviderAuthenticationRejected)
                self.assertNotIsInstance(error, ProviderUnavailable)
                self.assertEqual(error.upstream_status, status)

    def test_onekey_login_wraps_unfolded_transport_interruptions(self) -> None:
        original = httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

        error, _provider, store = self._onekey_error(original)

        self.assertIsInstance(error, ProviderUpstreamInterrupted)
        self.assertIs(error.__cause__, original)
        self.assertEqual(store.users, {})

    def test_only_transport_and_server_results_are_unavailable(self) -> None:
        for status in (-1, -7, 500, 502, 503, 599):
            with self.subTest(status=status):
                error, provider, store = self._login_error(_result(status))
                self.assertIsInstance(error, ProviderUnavailable)
                self.assertEqual(error.upstream_status, status)
                self.assertEqual(store.users, {})
                assert provider.runtime is not None
                self.assertEqual(provider.runtime.app.client.close_calls, 1)

    def test_business_and_http_rejections_never_signal_unavailable(self) -> None:
        for status in (0, 200, 400, 401, 403, 404, 429, 499):
            with self.subTest(status=status):
                error, provider, store = self._login_error(_result(status))
                self.assertIsInstance(error, ProviderAuthenticationRejected)
                self.assertNotIsInstance(error, ProviderUnavailable)
                self.assertEqual(error.upstream_status, status)
                self.assertEqual(store.users, {})
                assert provider.runtime is not None
                self.assertEqual(provider.runtime.app.client.close_calls, 1)

    def test_success_without_an_authenticated_session_is_rejected(self) -> None:
        error, _provider, _store = self._login_error(_result(200, ok=True))

        self.assertIsInstance(error, ProviderAuthenticationRejected)
        self.assertNotIsInstance(error, ProviderUnavailable)

    def test_arbitrary_adapter_exception_is_preserved_and_cleaned_up(self) -> None:
        original = RuntimeError("unexpected adapter failure")

        error, provider, store = self._login_error(original)

        self.assertIs(error, original)
        self.assertNotIsInstance(error, ProviderUnavailable)
        self.assertEqual(store.users, {})
        assert provider.runtime is not None
        self.assertEqual(provider.runtime.app.client.close_calls, 1)

    def test_unfolded_transport_interruption_maps_to_interrupted(self) -> None:
        # 协议层只折算超时/网络/代理故障；RemoteProtocolError（陈旧
        # keep-alive 被上游先行关闭）会原样上抛到 store 层，应包装为
        # 专用的 ProviderUpstreamInterrupted 而非 ProviderUnavailable。
        original = httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

        error, provider, store = self._login_error(original)

        self.assertIsInstance(error, ProviderUpstreamInterrupted)
        self.assertNotIsInstance(error, ProviderUnavailable)
        self.assertIs(getattr(error, "retryable", False), True)
        self.assertIs(error.__cause__, original)
        self.assertEqual(store.users, {})
        assert provider.runtime is not None
        self.assertEqual(provider.runtime.app.client.close_calls, 1)


class BffAuthenticationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store = bff_server.STORE

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    @staticmethod
    def _request(error: Exception) -> tuple[int, dict[str, object]]:
        bff_server.STORE = SimpleNamespace(
            login_password=lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
        )

        class Harness:
            path = "/api/auth/login"

            def __init__(self) -> None:
                self.response: tuple[int, dict[str, object]] | None = None

            def _check_api_origin(self) -> bool:
                return True

            def body(self) -> dict[str, str]:
                return {"phone": "13800138000", "password": "password"}

            def sid(self) -> None:
                return None

            def _allow_sensitive_action(self, *_args: object, **_kwargs: object) -> bool:
                return True

            def ok(
                self,
                payload: dict[str, object],
                status: int = 200,
                **_kwargs: object,
            ) -> tuple[int, dict[str, object]]:
                self.response = (status, payload)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        assert harness.response is not None
        return harness.response

    def test_unavailable_has_stable_fallback_signal(self) -> None:
        status, payload = self._request(
            ProviderUnavailable("connection failed", upstream_status=-1)
        )

        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_UNAVAILABLE")
        self.assertIs(payload["retryable"], True)

    def test_rejection_has_stable_non_fallback_signal(self) -> None:
        status, payload = self._request(
            ProviderAuthenticationRejected("wrong password", upstream_status=401)
        )

        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_REJECTED")
        self.assertIs(payload["retryable"], False)

    def test_arbitrary_exception_does_not_claim_upstream_unavailable(self) -> None:
        status, payload = self._request(RuntimeError("adapter bug"))

        self.assertEqual(status, 400)
        self.assertNotEqual(payload.get("code"), "UPSTREAM_AUTH_UNAVAILABLE")

    def test_interruption_has_retryable_signal_distinct_from_unavailable(self) -> None:
        # 瞬态连接中断必须呈现为 503 可重试，且 code 不能等于
        # UPSTREAM_AUTH_UNAVAILABLE，否则会误触发本地密码回退闸门。
        status, payload = self._request(
            ProviderUpstreamInterrupted("stale keep-alive connection closed")
        )

        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_INTERRUPTED")
        self.assertNotEqual(payload["code"], "UPSTREAM_AUTH_UNAVAILABLE")
        self.assertIs(payload["retryable"], True)


class ApiLocalFallbackBoundaryTests(unittest.TestCase):
    class Store:
        def __init__(self, upstream_error: Exception | None) -> None:
            self.upstream_error = upstream_error
            self.web_user = ApiLocalFallbackBoundaryTests.WebUser()
            self.login_calls: list[dict[str, object]] = []
            self.onekey_calls: list[dict[str, object]] = []
            self.put_calls: list[object] = []
            self.drop_calls: list[str] = []

        def login_password(self, *args: object, **kwargs: object) -> object:
            self.login_calls.append({"args": args, "kwargs": kwargs})
            if self.upstream_error is not None:
                raise self.upstream_error
            return self.web_user

        def login_onekey(self, *args: object, **kwargs: object) -> object:
            self.onekey_calls.append({"args": args, "kwargs": kwargs})
            if self.upstream_error is not None:
                raise self.upstream_error
            return self.web_user

        def put(self, web_user: object) -> None:
            self.put_calls.append(web_user)

        def drop(self, sid: str) -> None:
            self.drop_calls.append(sid)

        def get(self, sid: str) -> object | None:
            return self.web_user if sid == self.web_user.web_sid else None

    class WebUser:
        web_sid = "upstream-session-id-0123456789abcdef"

        def public(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "uid": "42",
                "nickname": "本地账号",
            }

        def clear_pending(self) -> None:
            return None

    class Persistence:
        def __init__(
            self,
            local_error: Exception | None = None,
            account_error: Exception | None = None,
        ) -> None:
            self.local_error = local_error
            self.account_error = account_error
            self.local_login_calls: list[dict[str, object]] = []
            self.login_failures: list[dict[str, str]] = []
            self.captured: list[dict[str, object]] = []
            self.public_precheck_calls: list[dict[str, object]] = []
            self.local_precheck_calls: list[dict[str, object]] = []
            self.account_precheck_calls: list[str] = []
            self.complete_login_calls: list[dict[str, object]] = []

        def require_identity(self, _sid: str) -> None:
            return None

        def rate_limit(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def precheck_login_credentials(self, **kwargs: object) -> object:
            self.public_precheck_calls.append(dict(kwargs))
            return self.precheck_account(phone=str(kwargs.get("phone") or ""))

        def precheck_local_password_credentials(self, **kwargs: object) -> str:
            self.local_precheck_calls.append(dict(kwargs))
            return str(kwargs.get("phone") or "")

        def precheck_account(self, *, phone: str) -> object:
            self.account_precheck_calls.append(phone)
            if self.account_error is not None:
                raise self.account_error
            return self._login_context()

        def _login_context(self) -> object:
            return SimpleNamespace(
                normalized_phone="13800138000",
                requires_invite=False,
                local_password_available=True,
                existing_upstream_uid="42",
            )

        def complete_local_password_login(self, **kwargs: object) -> object:
            self.local_login_calls.append(dict(kwargs))
            if self.local_error is not None:
                raise self.local_error
            return SimpleNamespace(
                raw_sid="local-session-id-0123456789abcdef",
                identity=SimpleNamespace(user_id="user-42"),
                web_user=ApiLocalFallbackBoundaryTests.WebUser(),
            )

        def complete_login(self, **kwargs: object) -> object:
            self.complete_login_calls.append(dict(kwargs))
            return SimpleNamespace(user_id="user-42")

        def record_login_failure(self, *, phone: str, client_ip: str) -> None:
            self.login_failures.append({"phone": phone, "client_ip": client_ip})

        def capture_product_response(self, **kwargs: object) -> None:
            self.captured.append(dict(kwargs))

    def setUp(self) -> None:
        self.previous_store = bff_server.STORE

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    @staticmethod
    def _request(
        upstream_error: Exception | None,
        *,
        local_error: Exception | None = None,
        account_error: Exception | None = None,
        store: object | None = None,
        mode: str = "password",
    ) -> tuple[object, object, Persistence]:
        if store is None:
            store = ApiLocalFallbackBoundaryTests.Store(upstream_error)
        persistence = ApiLocalFallbackBoundaryTests.Persistence(
            local_error,
            account_error,
        )
        bff_server.STORE = store
        raw_body = json.dumps(
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": mode,
            }
        ).encode("utf-8")
        application = SimpleNamespace(
            state=SimpleNamespace(
                persistence=persistence,
                settings=SimpleNamespace(
                    max_request_body_bytes=1024 * 1024,
                    trust_proxy_headers=False,
                    cookie_secure=False,
                ),
            )
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw_body)).encode("ascii")),
                (b"host", b"testserver"),
                (b"user-agent", b"auth-boundary-test"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": application,
        }
        response = web_api._legacy_dispatch_sync(Request(scope), raw_body)
        return response, store, persistence

    def test_public_onekey_login_is_rejected_before_any_provider_call(self) -> None:
        response, store, persistence = self._request(None, mode="onekey")

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "不支持的登录方式")
        self.assertEqual(store.login_calls, [])
        self.assertEqual(store.onekey_calls, [])
        self.assertEqual(persistence.complete_login_calls, [])
        self.assertEqual(persistence.login_failures, [])
        self.assertEqual(persistence.public_precheck_calls, [])
        self.assertEqual(persistence.account_precheck_calls, [])

    def test_upstream_unavailable_never_uses_local_password(self) -> None:
        response, store, persistence = self._request(
            ProviderUnavailable("connection failed", upstream_status=-1)
        )

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_UNAVAILABLE")
        self.assertIs(payload["retryable"], True)
        self.assertEqual(len(persistence.public_precheck_calls), 1)
        self.assertEqual(persistence.account_precheck_calls, ["13800138000"])
        self.assertEqual(persistence.local_precheck_calls, [])
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(persistence.complete_login_calls, [])
        self.assertEqual(len(store.login_calls), 1)
        self.assertEqual(store.put_calls, [])
        self.assertNotIn(bff_server.COOKIE_NAME, response.headers.get("set-cookie", ""))

    def test_suspended_account_is_rejected_before_provider_login(self) -> None:
        response, store, persistence = self._request(
            ProviderUnavailable("connection failed", upstream_status=-1),
            account_error=PermissionError("账号不可用"),
        )

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(payload["error"], "账号不可用")
        self.assertEqual(len(persistence.public_precheck_calls), 1)
        self.assertEqual(persistence.account_precheck_calls, ["13800138000"])
        self.assertEqual(persistence.local_precheck_calls, [])
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(persistence.complete_login_calls, [])
        self.assertEqual(store.login_calls, [])
        self.assertEqual(persistence.login_failures, [])
        self.assertEqual(store.put_calls, [])

    def test_provider_success_is_persisted_without_local_password(self) -> None:
        response, store, persistence = self._request(None)

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 200)
        self.assertIs(payload["ok"], True)
        self.assertEqual(len(persistence.public_precheck_calls), 1)
        self.assertEqual(persistence.account_precheck_calls, ["13800138000"])
        self.assertEqual(persistence.local_precheck_calls, [])
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(len(persistence.complete_login_calls), 1)
        self.assertEqual(len(store.login_calls), 1)
        self.assertEqual(store.drop_calls, [])
        cookie_headers = [
            value.decode("latin-1")
            for name, value in response.raw_headers
            if name.lower() == b"set-cookie"
        ]
        self.assertIn(
            f"{bff_server.COOKIE_NAME}={store.web_user.web_sid}",
            "\n".join(cookie_headers),
        )

    def test_explicit_upstream_rejection_never_calls_local_password(self) -> None:
        response, store, persistence = self._request(
            ProviderAuthenticationRejected("wrong password", upstream_status=401)
        )

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_REJECTED")
        self.assertEqual(len(persistence.public_precheck_calls), 1)
        self.assertEqual(persistence.account_precheck_calls, ["13800138000"])
        self.assertEqual(persistence.local_precheck_calls, [])
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(persistence.complete_login_calls, [])
        self.assertEqual(store.put_calls, [])
        self.assertNotIn(bff_server.COOKIE_NAME, response.headers.get("set-cookie", ""))

    def test_arbitrary_adapter_exception_never_calls_local_password(self) -> None:
        response, store, persistence = self._request(
            RuntimeError("unexpected adapter failure")
        )

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(payload.get("code"), "UPSTREAM_AUTH_UNAVAILABLE")
        self.assertEqual(len(persistence.public_precheck_calls), 1)
        self.assertEqual(persistence.account_precheck_calls, ["13800138000"])
        self.assertEqual(persistence.local_precheck_calls, [])
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(persistence.complete_login_calls, [])
        self.assertEqual(store.put_calls, [])
        self.assertNotIn(bff_server.COOKIE_NAME, response.headers.get("set-cookie", ""))

    def test_remote_protocol_error_is_retryable_and_never_falls_back(self) -> None:
        # 端到端：真实 SessionStore 的上游登录抛 RemoteProtocolError（陈旧
        # keep-alive 被上游先行关闭）→ store 包装为 ProviderUpstreamInterrupted
        # → BFF 返回 503 可重试；api.py 的本地密码回退闸门只认
        # UPSTREAM_AUTH_UNAVAILABLE，因此不得进入本地密码回退。
        provider = _Provider(
            httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        )
        real_store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(real_store.close)

        response, store, persistence = self._request(None, store=real_store)

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_INTERRUPTED")
        self.assertNotEqual(payload["code"], "UPSTREAM_AUTH_UNAVAILABLE")
        self.assertIs(payload["retryable"], True)
        # 未进入本地密码回退，也未留下会话或 cookie。
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(len(persistence.public_precheck_calls), 1)
        self.assertEqual(persistence.account_precheck_calls, ["13800138000"])
        self.assertEqual(persistence.local_precheck_calls, [])
        self.assertEqual(persistence.complete_login_calls, [])
        self.assertEqual(store.users, {})
        self.assertNotIn(bff_server.COOKIE_NAME, response.headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()
