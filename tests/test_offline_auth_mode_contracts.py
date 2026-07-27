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
from bbw_web import api as web_api  # noqa: E402
from bbw_web import bff_server  # noqa: E402
from bbw_web.providers import ProviderRuntime, ProviderUnavailable  # noqa: E402
from bbw_web.store import SessionStore  # noqa: E402


class UpstreamAuthenticationSettingsTests(unittest.TestCase):
    def test_provider_only_is_default_with_bounded_login_budget(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.upstream_auth_mode, "provider-only")
        self.assertEqual(settings.upstream_auth_timeout_seconds, 5)
        self.assertEqual(settings.local_password_auth_concurrency, 2)

    def test_provider_first_alias_is_normalized_and_local_only_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"BBW_UPSTREAM_AUTH_MODE": "PROVIDER-FIRST"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.upstream_auth_mode, "provider-only")

        for invalid in ("LOCAL-ONLY", "unsafe-auto"):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ,
                {"BBW_UPSTREAM_AUTH_MODE": invalid},
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


class _ProviderOnlyPersistence:
    def __init__(self) -> None:
        self.precheck_calls = 0
        self.local_login_calls = 0
        self.login_failures: list[dict[str, str]] = []

    def require_identity(self, _sid: str) -> None:
        return None

    def rate_limit(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def precheck_login_credentials(self, **_kwargs: object) -> object:
        self.precheck_calls += 1
        return SimpleNamespace(
            normalized_phone="13800138000",
            requires_invite=False,
        )

    def complete_local_password_login(self, **_kwargs: object) -> object:
        self.local_login_calls += 1
        raise AssertionError("provider-only login must never use local credentials")

    def record_login_failure(self, *, phone: str, client_ip: str) -> None:
        self.login_failures.append({"phone": phone, "client_ip": client_ip})

    def capture_product_response(self, **_kwargs: object) -> None:
        return None


class _UnavailableLoginHandler:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        type(self).calls += 1

    def do_POST(self) -> None:
        return None

    def finish_capture(self) -> tuple[int, list[tuple[str, str]], bytes]:
        body = json.dumps(
            {
                "ok": False,
                "code": "UPSTREAM_AUTH_UNAVAILABLE",
                "error": "登录服务暂时不可用，请稍后重试",
                "retryable": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return 503, [("Content-Type", "application/json")], body


def _login_request(*, upstream_auth_mode: str = "provider-only") -> tuple[Request, bytes, _ProviderOnlyPersistence]:
    payload = {
        "phone": "13800138000",
        "password": "provider-password",
        "mode": "password",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    persistence = _ProviderOnlyPersistence()
    application = SimpleNamespace(
        state=SimpleNamespace(
            persistence=persistence,
            settings=SimpleNamespace(
                max_request_body_bytes=1024 * 1024,
                trust_proxy_headers=False,
                cookie_secure=False,
                upstream_auth_mode=upstream_auth_mode,
            ),
        )
    )
    request = Request(
        {
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
                (b"user-agent", b"provider-only-test"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": application,
        }
    )
    return request, raw_body, persistence


class ProviderOnlyAuthenticationDispatchTests(unittest.TestCase):
    def test_upstream_unavailable_never_falls_back_to_local_password(self) -> None:
        request, raw_body, persistence = _login_request()
        _UnavailableLoginHandler.calls = 0

        with patch.object(web_api, "CapturingHandler", _UnavailableLoginHandler):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_UNAVAILABLE")
        self.assertTrue(payload["retryable"])
        self.assertEqual(_UnavailableLoginHandler.calls, 1)
        self.assertEqual(persistence.precheck_calls, 1)
        self.assertEqual(persistence.local_login_calls, 0)
        self.assertEqual(
            persistence.login_failures,
            [{"phone": "13800138000", "client_ip": "127.0.0.1"}],
        )

    def test_runtime_defensively_rejects_retired_local_only_mode(self) -> None:
        request, raw_body, persistence = _login_request(
            upstream_auth_mode="local-only"
        )

        with patch.object(
            web_api,
            "CapturingHandler",
            side_effect=AssertionError("retired mode must fail before dispatch"),
        ):
            response = web_api._legacy_dispatch_sync(request, raw_body)

        payload = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "UPSTREAM_AUTH_MODE_UNSUPPORTED")
        self.assertEqual(persistence.precheck_calls, 0)
        self.assertEqual(persistence.local_login_calls, 0)

    def test_legacy_argon_gate_remains_bounded_but_is_not_a_login_route(self) -> None:
        gate = web_api._LocalPasswordAuthGate(2)

        with gate.claim() as first:
            with gate.claim() as second:
                with gate.claim() as third:
                    self.assertTrue(first)
                    self.assertTrue(second)
                    self.assertFalse(third)


if __name__ == "__main__":
    unittest.main()
