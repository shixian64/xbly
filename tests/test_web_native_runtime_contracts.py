from __future__ import annotations

import ast
import json
import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_web.providers import ProviderSessionState  # noqa: E402
from bbw_web.providers.web_native import (  # noqa: E402
    UPSTREAM_DISABLED_CODE,
    WEB_NATIVE_PROVIDER_ID,
    WebNativeProvider,
    WebNativeSession,
)


class WebNativeRuntimeContractTests(unittest.TestCase):
    def test_module_has_no_protocol_core_import(self) -> None:
        source_path = ROOT / "bbw_web" / "providers" / "web_native.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
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

    def test_local_session_is_authenticated_by_uid_without_legacy_token(self) -> None:
        provider = WebNativeProvider(account_provider_id="beibeiwu")
        runtime = provider.create_runtime_from_state(
            ProviderSessionState(
                uid="42",
                token="legacy-token-must-not-be-used",
                phone="13800138000",
                nickname="本地用户",
                portrait="https://example.invalid/avatar.jpg",
                raw_user={"id": "42", "nickname": "本地用户"},
                device_data={"device_id": "stored-device"},
            )
        )

        self.assertEqual(provider.provider_id, "beibeiwu")
        self.assertEqual(runtime.provider_id, WEB_NATIVE_PROVIDER_ID)
        self.assertTrue(runtime.app.session.logged_in)
        self.assertEqual(runtime.app.session.uid, "42")
        self.assertEqual(runtime.app.session.token, "")
        self.assertEqual(runtime.app.session.device_id, "stored-device")
        self.assertEqual(runtime.app.whoami()["nickname"], "本地用户")

    def test_native_session_copy_drops_credentials_without_mutating_source(self) -> None:
        source = WebNativeSession(
            uid="42", token="fake-upstream-token", password="fake-upstream-password",
            nickname="local-user", device_id="device-id",
        )
        runtime = WebNativeProvider().create_runtime(source)
        self.assertEqual(runtime.app.session.token, "")
        self.assertEqual(runtime.app.session.password, "")
        self.assertEqual(source.token, "fake-upstream-token")
        self.assertEqual(source.password, "fake-upstream-password")
        self.assertEqual(runtime.app.session.device_id, "device-id")
        self.assertTrue(runtime.app.session.logged_in)

    def test_loading_native_snapshot_does_not_retain_upstream_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            path.write_text(json.dumps({
                "uid": "42", "token": "fake-upstream-token",
                "password": "fake-upstream-password", "nickname": "local-user",
            }), encoding="utf-8")
            original = path.read_bytes()
            runtime = WebNativeProvider().load_runtime(path)
            self.assertEqual(runtime.app.session.token, "")
            self.assertEqual(runtime.app.session.password, "")
            self.assertEqual(runtime.app.session.nickname, "local-user")
            self.assertTrue(runtime.app.session.logged_in)
            self.assertEqual(path.read_bytes(), original)

    def test_legacy_domains_fail_immediately_with_stable_result(self) -> None:
        runtime = WebNativeProvider().create_runtime_from_state(
            ProviderSessionState(uid="42", token="")
        )

        result = runtime.app.content.recommend()
        tim_health = runtime.native.tim_rest.health(sample_uid="42")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.code, UPSTREAM_DISABLED_CODE)
        self.assertFalse(tim_health["ok"])
        self.assertEqual(tim_health["code"], UPSTREAM_DISABLED_CODE)
        self.assertFalse(runtime.app.create_heartbeat().running)


if __name__ == "__main__":
    unittest.main()
