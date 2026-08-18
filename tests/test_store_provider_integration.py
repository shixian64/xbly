from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web import store as store_module  # noqa: E402
from bbw_web.providers import ProviderRuntime  # noqa: E402
from bbw_web.store import SessionStore  # noqa: E402


class _FakeSession:
    def __init__(
        self,
        *,
        uid: str = "0",
        token: str = "0",
        nickname: str = "",
    ) -> None:
        self.uid = uid
        self.token = token
        self.nickname = nickname
        self.password = ""

    @property
    def logged_in(self) -> bool:
        return self.uid != "0" and self.token != "0"


class _FakeHeartbeat:
    def __init__(self, app: "_FakeApplication", interval_sec: float) -> None:
        self.app = app
        self.interval_sec = interval_sec
        self.running = False
        self.once_calls = 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def status(self) -> dict[str, object]:
        return {"running": self.running, "interval_sec": self.interval_sec}

    def once(self, first: bool | None = None) -> dict[str, object]:
        self.once_calls += 1
        return {
            "ok": True,
            "first": first,
            "running": self.running,
            "once_calls": self.once_calls,
        }


class _FakeApplication:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.client = _FakeClient()
        self.auth = _FakeAuth(session)
        self.device_seeds: list[str | None] = []
        self.heartbeat_intervals: list[float] = []
        self.device_error: Exception | None = None

    def set_device(self, *, seed: str | None = None, **_extra: str) -> dict[str, str]:
        self.device_seeds.append(seed)
        if self.device_error is not None:
            raise self.device_error
        return {"seed": str(seed or "")}

    def create_heartbeat(self, interval_sec: float = 55.0) -> _FakeHeartbeat:
        self.heartbeat_intervals.append(interval_sec)
        return _FakeHeartbeat(self, interval_sec)

    def start_heartbeat(self, interval_sec: float = 55.0) -> _FakeHeartbeat:
        heartbeat = self.create_heartbeat(interval_sec)
        heartbeat.start()
        return heartbeat


class _FakeClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeAuth:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.sms_calls: list[str] = []
        self.sms_error: Exception | None = None
        self.onekey_calls: list[str] = []
        self.onekey_error: Exception | None = None

    def send_sms(self, phone: str) -> dict[str, str]:
        self.sms_calls.append(phone)
        if self.sms_error is not None:
            raise self.sms_error
        return {"phone": phone}

    def login_onekey(self, phone: str) -> object:
        self.onekey_calls.append(phone)
        if self.onekey_error is not None:
            raise self.onekey_error
        self.session.uid = "42"
        self.session.token = "onekey-token-42"
        return SimpleNamespace(ok=True, code="", message="", raw="")


class _FakeNativeBundle:
    def __init__(self, app: _FakeApplication) -> None:
        self.app = app


class _RecordingProvider:
    provider_id = "test-provider"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.created_runtime: ProviderRuntime | None = None
        self.loaded_runtime: ProviderRuntime | None = None
        self.sms_error: Exception | None = None
        self.onekey_error: Exception | None = None
        self.device_error: Exception | None = None
        self.load_logged_in = True

    @staticmethod
    def _runtime(session: _FakeSession) -> ProviderRuntime:
        app = _FakeApplication(session)
        native = _FakeNativeBundle(app)
        return ProviderRuntime(
            provider_id=_RecordingProvider.provider_id,
            app=app,
            native=native,
        )

    def create_runtime(self, session: object = None) -> ProviderRuntime:
        self.calls.append(("create", session))
        runtime = self._runtime(_FakeSession())
        runtime.app.auth.sms_error = self.sms_error
        runtime.app.auth.onekey_error = self.onekey_error
        runtime.app.device_error = self.device_error
        self.created_runtime = runtime
        return runtime

    def create_runtime_from_state(self, state: object) -> ProviderRuntime:
        self.calls.append(("create_from_state", state))
        runtime = self._runtime(_FakeSession(uid="42", token="token-42"))
        self.created_runtime = runtime
        return runtime

    def load_runtime(self, path: str | Path | None = None) -> ProviderRuntime:
        self.calls.append(("load", path))
        runtime = self._runtime(
            _FakeSession(
                uid="42" if self.load_logged_in else "0",
                token="token-42" if self.load_logged_in else "0",
                nickname="恢复用户",
            )
        )
        self.loaded_runtime = runtime
        return runtime


class SessionStoreProviderIntegrationTests(unittest.TestCase):
    def test_store_has_no_direct_protocol_imports(self) -> None:
        tree = ast.parse(Path(store_module.__file__).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            str(node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )

        self.assertFalse(
            {
                name
                for name in imports
                if name == "bbw_protocol" or name.startswith("bbw_protocol.")
            }
        )

    def test_default_provider_preserves_the_legacy_runtime(self) -> None:
        from bbw_protocol.adapters import NativeBundle
        from bbw_protocol.app import BeibeiwuApp
        from bbw_web.providers.legacy_banghua import LegacyBanghuaProvider

        store = SessionStore(auto_heartbeat=False)
        self.addCleanup(store.close)

        user = store.create(label="默认运行时")

        self.assertIsInstance(store.runtime_provider, LegacyBanghuaProvider)
        self.assertIsInstance(user.app, BeibeiwuApp)
        self.assertIsInstance(user.native, NativeBundle)
        self.assertIs(user.native.app, user.app)
        self.assertTrue(user.app.session.device_id)

    def test_injected_provider_owns_runtime_creation(self) -> None:
        provider = _RecordingProvider()
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(store.close)

        user = store.create(label="注入运行时")

        self.assertIs(store.runtime_provider, provider)
        self.assertEqual(provider.calls, [("create", None)])
        self.assertIs(user.app, provider.created_runtime.app)
        self.assertIs(user.native, provider.created_runtime.native)
        self.assertEqual(user.app.device_seeds, [user.web_sid[:12]])

    def test_failed_device_setup_closes_unregistered_runtime(self) -> None:
        provider = _RecordingProvider()
        provider.device_error = RuntimeError("device unavailable")
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)

        with self.assertRaisesRegex(RuntimeError, "device unavailable"):
            store.create()

        self.assertEqual(provider.created_runtime.app.client.close_calls, 1)
        self.assertEqual(store.users, {})

    def test_restore_uses_provider_loader_without_creating_a_throwaway_runtime(
        self,
    ) -> None:
        provider = _RecordingProvider()
        with tempfile.TemporaryDirectory() as directory:
            sessions_dir = Path(directory) / "sessions"
            metadata_dir = Path(directory) / "metadata"
            sessions_dir.mkdir()
            path = sessions_dir / "42.json"
            path.write_text("{}", encoding="utf-8")
            with (
                patch.object(store_module, "SESSIONS_DIR", sessions_dir),
                patch.object(store_module, "WEB_META_DIR", metadata_dir),
            ):
                store = SessionStore(
                    runtime_provider=provider,
                    auto_heartbeat=False,
                    persist_sessions=True,
                )
                self.addCleanup(store.close)
                user = store.restore_from_disk("42")

        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(provider.calls, [("load", path)])
        self.assertIs(user.app, provider.loaded_runtime.app)
        self.assertIs(user.native, provider.loaded_runtime.native)
        self.assertEqual(user.label, "恢复用户")
        self.assertTrue(user.persist_sessions)

    def test_invalid_restored_session_closes_provider_runtime(self) -> None:
        provider = _RecordingProvider()
        provider.load_logged_in = False
        with tempfile.TemporaryDirectory() as directory:
            sessions_dir = Path(directory) / "sessions"
            metadata_dir = Path(directory) / "metadata"
            sessions_dir.mkdir()
            (sessions_dir / "42.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(store_module, "SESSIONS_DIR", sessions_dir),
                patch.object(store_module, "WEB_META_DIR", metadata_dir),
            ):
                store = SessionStore(
                    runtime_provider=provider,
                    auto_heartbeat=False,
                    persist_sessions=True,
                )
                restored = store.restore_from_disk("42")

        self.assertIsNone(restored)
        self.assertEqual(provider.loaded_runtime.app.client.close_calls, 1)
        self.assertEqual(store.users, {})

    def test_send_sms_closes_its_one_shot_runtime(self) -> None:
        provider = _RecordingProvider()
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)

        result = store.send_sms("13800138000")

        self.assertEqual(result, {"phone": "13800138000"})
        self.assertEqual(provider.calls, [("create", None)])
        self.assertEqual(provider.created_runtime.app.auth.sms_calls, ["13800138000"])
        self.assertEqual(provider.created_runtime.app.client.close_calls, 1)
        self.assertEqual(store.users, {})

    def test_send_sms_closes_its_runtime_when_the_provider_call_fails(self) -> None:
        provider = _RecordingProvider()
        provider.sms_error = RuntimeError("sms unavailable")
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)

        with self.assertRaisesRegex(RuntimeError, "sms unavailable"):
            store.send_sms("13800138000")

        self.assertEqual(provider.created_runtime.app.client.close_calls, 1)

    def test_phone_only_login_requires_an_explicit_request_grant(self) -> None:
        provider = _RecordingProvider()
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(store.close)

        with self.assertRaises(PermissionError):
            store.login_onekey(None, "13800138000")
        self.assertEqual(provider.calls, [])

        user = store.login_onekey(
            None,
            "13800138000",
            request_authorized=True,
        )
        self.assertTrue(user.app.session.logged_in)
        self.assertEqual(user.app.auth.onekey_calls, ["13800138000"])

    def test_phone_only_login_cleans_up_a_failed_provisional_runtime(self) -> None:
        provider = _RecordingProvider()
        provider.onekey_error = RuntimeError("one-key unavailable")
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)

        with self.assertRaisesRegex(RuntimeError, "one-key unavailable"):
            store.login_onekey(
                None,
                "13800138000",
                request_authorized=True,
            )

        self.assertEqual(store.users, {})
        self.assertEqual(provider.created_runtime.app.client.close_calls, 1)

    def test_web_user_heartbeat_delegates_to_the_provider_application(self) -> None:
        provider = _RecordingProvider()
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(store.close)
        user = store.create()

        status = user.start_heartbeat(17.0)
        repeated = user.start_heartbeat(29.0)

        self.assertTrue(status["running"])
        self.assertEqual(status["interval_sec"], 17.0)
        self.assertEqual(repeated, status)
        self.assertIs(user.heartbeat.app, user.app)
        self.assertEqual(user.app.heartbeat_intervals, [17.0])

    def test_single_heartbeat_does_not_start_background_work(self) -> None:
        provider = _RecordingProvider()
        store = SessionStore(runtime_provider=provider, auto_heartbeat=False)
        self.addCleanup(store.close)
        user = store.create()

        result = user.heartbeat_once()

        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        self.assertFalse(user.heartbeat.running)
        self.assertEqual(user.heartbeat.once_calls, 1)


if __name__ == "__main__":
    unittest.main()
