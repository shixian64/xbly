from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_protocol.adapters import NativeBundle  # noqa: E402
from bbw_protocol.app import BeibeiwuApp  # noqa: E402
from bbw_protocol.session import Session  # noqa: E402
from bbw_web.providers import (  # noqa: E402
    ProviderApplication,
    ProviderNativeBundle,
    ProviderSessionState,
    RuntimeProvider,
)
from bbw_web.providers.legacy_banghua import (  # noqa: E402
    LEGACY_BANGHUA_PROVIDER_ID,
    LegacyBanghuaProvider,
)


class LegacyBanghuaProviderContractTests(unittest.TestCase):
    def test_default_provider_preserves_existing_runtime_object_graph(self) -> None:
        session = Session(uid="42", token="token-42", nickname="测试用户")
        provider = LegacyBanghuaProvider()

        runtime = provider.create_runtime(session)
        self.addCleanup(runtime.app.client.close)

        self.assertEqual(runtime.provider_id, LEGACY_BANGHUA_PROVIDER_ID)
        self.assertIsInstance(provider, RuntimeProvider)
        self.assertIsInstance(runtime.app, BeibeiwuApp)
        self.assertIsInstance(runtime.native, NativeBundle)
        self.assertIsInstance(runtime.app, ProviderApplication)
        self.assertIsInstance(runtime.native, ProviderNativeBundle)
        self.assertIs(runtime.app.session, session)
        self.assertIs(runtime.native.app, runtime.app)

        heartbeat = runtime.app.create_heartbeat(interval_sec=17.0, jitter_sec=0.0)
        self.addCleanup(heartbeat.stop)
        self.assertFalse(heartbeat.running)
        self.assertEqual(heartbeat.interval_sec, 17.0)

    def test_load_runtime_uses_session_load_before_composing_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            Session(uid="84", token="token-84", nickname="恢复用户").save(str(path))

            runtime = LegacyBanghuaProvider().load_runtime(path)
            self.addCleanup(runtime.app.client.close)

        self.assertEqual(runtime.app.session.uid, "84")
        self.assertEqual(runtime.app.session.token, "token-84")
        self.assertEqual(runtime.app.session.nickname, "恢复用户")
        self.assertIs(runtime.native.app, runtime.app)

    def test_provider_rebuilds_runtime_from_neutral_persisted_state(self) -> None:
        state = ProviderSessionState(
            uid="126",
            token="token-126",
            phone="19100000000",
            nickname="迁移用户",
            user_role="member",
            rp_verify_time="1",
            vip="2",
            svip="3",
            portrait="https://example.invalid/avatar.jpg",
            raw_user={"id": "126", "private": "kept server-side"},
            device_data={"phonebrand": "Web", "device_id": "device-126"},
        )

        runtime = LegacyBanghuaProvider().create_runtime_from_state(state)
        self.addCleanup(runtime.app.client.close)

        self.assertEqual(runtime.app.session.uid, "126")
        self.assertEqual(runtime.app.session.token, "token-126")
        self.assertEqual(runtime.app.session.password, "")
        self.assertEqual(runtime.app.session.nickname, "迁移用户")
        self.assertEqual(runtime.app.session.phonebrand, "Web")
        self.assertEqual(runtime.app.session.device_id, "device-126")
        self.assertEqual(runtime.app.session.raw_user["id"], "126")
        self.assertIs(runtime.native.app, runtime.app)

    def test_injected_factories_receive_the_same_session_and_application(self) -> None:
        events: list[tuple[str, object]] = []
        created_session = SimpleNamespace(marker="created")
        loaded_session = SimpleNamespace(marker="loaded")

        def session_factory() -> object:
            events.append(("session_factory", created_session))
            return created_session

        def session_loader(path: str | None) -> object:
            events.append(("session_loader", path))
            return loaded_session

        def application_factory(session: object) -> object:
            app = SimpleNamespace(session=session)
            events.append(("application_factory", session))
            return app

        def native_bundle_factory(app: object) -> object:
            native = SimpleNamespace(app=app)
            events.append(("native_bundle_factory", app))
            return native

        provider = LegacyBanghuaProvider(
            session_factory=session_factory,
            session_loader=session_loader,
            application_factory=application_factory,
            native_bundle_factory=native_bundle_factory,
        )

        created = provider.create_runtime()
        loaded = provider.load_runtime(Path("saved-session.json"))

        self.assertIs(created.app.session, created_session)
        self.assertIs(created.native.app, created.app)
        self.assertIs(loaded.app.session, loaded_session)
        self.assertIs(loaded.native.app, loaded.app)
        self.assertEqual(
            events,
            [
                ("session_factory", created_session),
                ("application_factory", created_session),
                ("native_bundle_factory", created.app),
                ("session_loader", "saved-session.json"),
                ("application_factory", loaded_session),
                ("native_bundle_factory", loaded.app),
            ],
        )


if __name__ == "__main__":
    unittest.main()
