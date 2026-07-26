from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.requests import Request  # noqa: E402

from bbw_prod.config import ConfigurationError, Settings  # noqa: E402
from bbw_prod.services import (  # noqa: E402
    AuthenticationFailed,
    LocalAuthenticationUnavailable,
    PermissionDenied,
)
from bbw_web import api as web_api  # noqa: E402
from bbw_web import bff_server  # noqa: E402
from bbw_web.providers import ProviderRuntime, ProviderUnavailable  # noqa: E402
from bbw_web.store import SessionStore  # noqa: E402


class UpstreamAuthenticationSettingsTests(unittest.TestCase):
    def test_provider_first_is_default_with_bounded_login_budget(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.upstream_auth_mode, "provider-first")
        self.assertEqual(settings.upstream_auth_timeout_seconds, 5)
        self.assertEqual(settings.local_password_auth_concurrency, 2)

    def test_local_only_mode_is_explicit_and_validated(self) -> None:
        with patch.dict(
            os.environ,
            {"BBW_UPSTREAM_AUTH_MODE": "LOCAL-ONLY"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.upstream_auth_mode, "local-only")

        with patch.dict(
            os.environ,
            {"BBW_UPSTREAM_AUTH_MODE": "unsafe-auto"},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_local_password_auth_concurrency_is_strictly_bounded(self) -> None:
        for configured in ("1", "8"):
            with self.subTest(configured=configured), patch.dict(
                os.environ,
                {"BBW_LOCAL_PASSWORD_AUTH_CONCURRENCY": configured},
                clear=True,
            ):
                settings = Settings.from_env()
                self.assertEqual(
                    settings.local_password_auth_concurrency, int(configured)
                )

        for invalid in ("0", "9", "not-an-integer"):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ,
                {"BBW_LOCAL_PASSWORD_AUTH_CONCURRENCY": invalid},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env()


class _TimeoutSession:
    uid = "0"
    token = "0"
    phone = ""
    password = ""
    nickname = ""

    @property
    def logged_in(self) -> bool:
        return False


class _TimeoutClient:
    def __init__(self) -> None:
        self.timeout = 30
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _TimeoutAuth:
    def __init__(self, client: _TimeoutClient) -> None:
        self.client = client
        self.observed_timeouts: list[float] = []

    def login_password(self, _phone: str, _password: str) -> object:
        self.observed_timeouts.append(float(self.client.timeout))
        return SimpleNamespace(
            ok=False,
            status=-1,
            raw="transport unavailable",
            code="",
            message="transport unavailable",
        )


class _TimeoutApplication:
    def __init__(self) -> None:
        self.session = _TimeoutSession()
        self.client = _TimeoutClient()
        self.auth = _TimeoutAuth(self.client)

    def set_device(self, **_kwargs: object) -> dict[str, str]:
        return {}


class _TimeoutProvider:
    provider_id = "test-provider"

    def __init__(self) -> None:
        self.application: _TimeoutApplication | None = None

    def create_runtime(self, _session: object = None) -> ProviderRuntime:
        self.application = _TimeoutApplication()
        return ProviderRuntime(
            provider_id=self.provider_id,
            app=self.application,
            native=SimpleNamespace(app=self.application),
        )

    def create_runtime_from_state(self, _state: object) -> ProviderRuntime:
        return self.create_runtime()

    def load_runtime(self, _path: object = None) -> ProviderRuntime:
        return self.create_runtime()


class UpstreamLoginBudgetTests(unittest.TestCase):
    def test_provider_login_uses_short_budget_and_restores_client_timeout(self) -> None:
        provider = _TimeoutProvider()
        store = SessionStore(
            runtime_provider=provider,
            auto_heartbeat=False,
            upstream_auth_timeout_sec=4,
        )
        self.addCleanup(store.close)

        with self.assertRaises(ProviderUnavailable):
            store.login_password(None, "13800138000", "password")

        assert provider.application is not None
        self.assertEqual(provider.application.auth.observed_timeouts, [4.0])
        self.assertEqual(provider.application.client.timeout, 30)
        self.assertEqual(provider.application.client.close_calls, 1)


class _LocalOnlyWebUser:
    def public(self) -> dict[str, object]:
        return {"authenticated": True, "uid": "42", "nickname": "本地账号"}


class _LocalOnlyStore:
    def __init__(self) -> None:
        self.provider_login_calls = 0
        self.sms_calls = 0
        self.put_calls: list[object] = []

    def get(self, _sid: str | None) -> None:
        return None

    def login_password(self, *_args: object, **_kwargs: object) -> object:
        self.provider_login_calls += 1
        raise AssertionError("local-only mode must not call the provider")

    def send_sms(self, *_args: object, **_kwargs: object) -> object:
        self.sms_calls += 1
        raise AssertionError("local-only mode must not call SMS upstream")

    def put(self, web_user: object) -> None:
        self.put_calls.append(web_user)

    def drop(self, _sid: str) -> None:
        return None


class _LocalOnlyPersistence:
    def __init__(self) -> None:
        self.local_login_calls: list[dict[str, object]] = []
        self.login_failures: list[dict[str, str]] = []

    def require_identity(self, _sid: str) -> None:
        return None

    def rate_limit(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def precheck_login_credentials(self, **_kwargs: object) -> object:
        raise AssertionError("local-only mode must not query public account state")

    def precheck_local_password_credentials(self, **_kwargs: object) -> str:
        return "13800138000"

    def complete_local_password_login(self, **kwargs: object) -> object:
        self.local_login_calls.append(dict(kwargs))
        return SimpleNamespace(
            raw_sid="local-session-id-0123456789abcdef",
            identity=SimpleNamespace(user_id="user-42"),
            web_user=_LocalOnlyWebUser(),
        )

    def record_login_failure(self, *, phone: str, client_ip: str) -> None:
        self.login_failures.append({"phone": phone, "client_ip": client_ip})


class _RejectedLocalOnlyPersistence(_LocalOnlyPersistence):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def complete_local_password_login(self, **kwargs: object) -> object:
        self.local_login_calls.append(dict(kwargs))
        raise self.error


class _PrecheckRejectedLocalOnlyPersistence(_LocalOnlyPersistence):
    def precheck_local_password_credentials(self, **_kwargs: object) -> str:
        raise PermissionError("需要完成人机验证后才能继续登录")


def _local_only_request(path: str, payload: dict[str, object]) -> Request:
    raw_body = json.dumps(payload).encode("utf-8")
    application = SimpleNamespace(
        state=SimpleNamespace(
            persistence=_LocalOnlyPersistence(),
            settings=SimpleNamespace(
                max_request_body_bytes=1024 * 1024,
                trust_proxy_headers=False,
                cookie_secure=False,
                upstream_auth_mode="local-only",
            ),
        )
    )
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw_body)).encode("ascii")),
                (b"host", b"testserver"),
                (b"user-agent", b"local-only-test"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": application,
        }
    )


class LocalOnlyAuthenticationDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store = bff_server.STORE
        self.store = _LocalOnlyStore()
        bff_server.STORE = self.store

    def tearDown(self) -> None:
        bff_server.STORE = self.previous_store

    def test_password_login_skips_provider_entirely(self) -> None:
        request = _local_only_request(
            "/api/auth/login",
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            },
        )
        raw_body = json.dumps(
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            }
        ).encode("utf-8")

        response = web_api._legacy_dispatch_sync(request, raw_body)
        payload = json.loads(bytes(response.body).decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["auth_source"], "web-local")
        self.assertEqual(payload["dependency_mode"], "web-local/local-only")
        self.assertEqual(self.store.provider_login_calls, 0)
        self.assertEqual(len(self.store.put_calls), 1)
        persistence = request.app.state.persistence
        self.assertEqual(len(persistence.local_login_calls), 1)

    def test_sms_is_rejected_without_contacting_upstream(self) -> None:
        request = _local_only_request(
            "/api/auth/sms-send", {"phone": "13800138000"}
        )
        raw_body = json.dumps({"phone": "13800138000"}).encode("utf-8")

        response = web_api._legacy_dispatch_sync(request, raw_body)
        payload = json.loads(bytes(response.body).decode("utf-8"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_DISABLED")
        self.assertEqual(self.store.sms_calls, 0)

    def test_argon2_gate_has_hard_non_queueing_capacity(self) -> None:
        gate = web_api._LocalPasswordAuthGate(2)

        with gate.claim() as first:
            with gate.claim() as second:
                with gate.claim() as third:
                    self.assertTrue(first)
                    self.assertTrue(second)
                    self.assertFalse(third)

    def test_saturated_argon2_gate_never_enters_local_verifier(self) -> None:
        request = _local_only_request(
            "/api/auth/login",
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            },
        )
        gate = web_api._LocalPasswordAuthGate(1)
        request.app.state.local_password_auth_gate = gate
        raw_body = json.dumps(
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            }
        ).encode("utf-8")

        with gate.claim() as acquired:
            self.assertTrue(acquired)
            response = web_api._legacy_dispatch_sync(request, raw_body)

        payload = json.loads(bytes(response.body).decode("utf-8"))
        persistence = request.app.state.persistence
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "LOCAL_AUTH_BUSY")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["retry_after"], 1)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(persistence.login_failures, [])

    def test_every_credential_rejection_counts_as_a_login_failure(self) -> None:
        cases = (
            LocalAuthenticationUnavailable("credential not migrated"),
            AuthenticationFailed("wrong password"),
            PermissionDenied("credential disabled"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                request = _local_only_request(
                    "/api/auth/login",
                    {
                        "phone": "13800138000",
                        "password": "web-password",
                        "mode": "password",
                    },
                )
                persistence = _RejectedLocalOnlyPersistence(error)
                request.app.state.persistence = persistence
                raw_body = json.dumps(
                    {
                        "phone": "13800138000",
                        "password": "web-password",
                        "mode": "password",
                    }
                ).encode("utf-8")

                response = web_api._legacy_dispatch_sync(request, raw_body)
                payload = json.loads(bytes(response.body).decode("utf-8"))

                self.assertEqual(response.status_code, 401)
                self.assertEqual(payload["code"], "LOCAL_AUTH_REJECTED")
                self.assertIn("仅支持已完成 Web 密码迁移", payload["error"])
                self.assertEqual(len(persistence.local_login_calls), 1)
                self.assertEqual(
                    persistence.login_failures,
                    [{"phone": "13800138000", "client_ip": "127.0.0.1"}],
                )

    def test_security_precheck_rejection_counts_in_local_only_mode(self) -> None:
        request = _local_only_request(
            "/api/auth/login",
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            },
        )
        persistence = _PrecheckRejectedLocalOnlyPersistence()
        request.app.state.persistence = persistence
        raw_body = json.dumps(
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            }
        ).encode("utf-8")

        response = web_api._legacy_dispatch_sync(request, raw_body)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(persistence.local_login_calls, [])
        self.assertEqual(
            persistence.login_failures,
            [{"phone": "13800138000", "client_ip": "127.0.0.1"}],
        )

    def test_internal_local_auth_failure_does_not_penalize_credentials(self) -> None:
        request = _local_only_request(
            "/api/auth/login",
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            },
        )
        persistence = _RejectedLocalOnlyPersistence(
            RuntimeError("database unavailable")
        )
        request.app.state.persistence = persistence
        raw_body = json.dumps(
            {
                "phone": "13800138000",
                "password": "web-password",
                "mode": "password",
            }
        ).encode("utf-8")

        with patch.object(web_api.LOGGER, "exception"):
            response = web_api._legacy_dispatch_sync(request, raw_body)
        payload = json.loads(bytes(response.body).decode("utf-8"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "LOCAL_AUTH_UNAVAILABLE")
        self.assertEqual(persistence.login_failures, [])


if __name__ == "__main__":
    unittest.main()
