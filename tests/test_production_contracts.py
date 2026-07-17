from __future__ import annotations

import ast
import base64
import importlib.util
import json
import sys
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _IdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.external_scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "script" and values.get("src"):
            self.external_scripts.append(str(values["src"]))


class ProductionContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def test_production_python_sources_parse(self) -> None:
        roots = ("bbw_prod", "bbw_web", "migrations")
        parsed = 0
        for root in roots:
            for path in (ROOT / root).rglob("*.py"):
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
                parsed += 1
        self.assertGreaterEqual(parsed, 15)

    def test_admin_bootstrap_runs_inside_lifespan_cleanup_scope(self) -> None:
        source = self.read("bbw_web/api.py")
        lifespan = source.split("async def lifespan", 1)[1].split("app = FastAPI", 1)[0]
        self.assertLess(lifespan.index("try:"), lifespan.index("persistence.startup()"))
        self.assertLess(lifespan.index("persistence.startup()"), lifespan.index("bootstrap_initial_admin"))
        self.assertLess(lifespan.index("bootstrap_initial_admin(settings, persistence)"), lifespan.index("yield"))
        self.assertIn("persistence.close()", lifespan)
        self.assertIn("def admin_requires_javascript", source)
        self.assertIn("without parsing or", source)
        self.assertIn("def sanitized_validation_error", source)
        validation_handler = source.split("def sanitized_validation_error", 1)[1].split(
            '@app.middleware("http")', 1
        )[0]
        self.assertNotIn('error.get("input")', validation_handler)

    def test_admin_api_security_invariants_are_wired(self) -> None:
        source = self.read("bbw_web/admin_api.py")
        services = self.read("bbw_prod/services.py")
        config = self.read("bbw_prod/config.py")
        protocol_secrets = self.read("bbw_protocol/secrets.py")
        worker = self.read("bbw_web/worker.py")
        self.assertIn('"login-name-ip"', source)
        self.assertIn('"totp-verification"', source)
        self.assertIn("admin-api-pre-ip:", source)
        self.assertIn("admin-api-pre-sid:", source)
        self.assertIn("admin-lock:password-verification", source)
        self.assertIn("credentials.view_failed", source)
        self.assertIn("raw_response.view_failed", source)
        self.assertIn("_PUBLIC_LIST_JSON_MAX_CHARS = 16_000", source)
        raw_detail = source.split("def raw_response_detail", 1)[1].split("@router.get(\"/audits\")", 1)[0]
        self.assertIn("require_credentials_unlocked", raw_detail)
        self.assertIn("MAX_ACTIVE_SESSIONS = 3", services)
        self.assertIn("admin.totp-pending-secret", services)
        self.assertIn("totp_version", services)
        self.assertIn("def purge_stale_sessions", services)
        self.assertIn("production secrets must use *_FILE sources", config)
        self.assertIn("is not allowed in production; use", protocol_secrets)
        self.assertIn("CredentialCipher.from_settings(settings)", worker)
        begin = services.split("def begin_totp_enrollment", 1)[1].split(
            "def confirm_totp_enrollment", 1
        )[0]
        self.assertNotIn("admin.totp_enabled = False", begin)
        self.assertNotIn("admin.totp_secret_encrypted =", begin)

    def test_admin_routes_and_ui_contract(self) -> None:
        api = self.read("bbw_web/admin_api.py")
        expected = {
            "/login",
            "/logout",
            "/me",
            "/totp/start",
            "/totp/confirm",
            "/totp/cancel",
            "/credentials/unlock",
            "/credentials/lock",
            "/password",
            "/invites",
            "/invites/{invite_id}/disable",
            "/users",
            "/users/{user_id}",
            "/users/{user_id}/status",
            "/users/{user_id}/credentials",
            "/users/{user_id}/conversations",
            "/users/{user_id}/messages",
            "/users/{user_id}/media",
            "/users/{user_id}/media/{media_id}/access",
            "/users/{user_id}/relationships",
            "/users/{user_id}/activities",
            "/users/{user_id}/raw-responses",
            "/users/{user_id}/raw-responses/{response_id}",
            "/audits",
            "/overview",
        }
        for route in expected:
            self.assertIn(f'("{route}")', api)

        html = self.read("bbw_web/static/admin.html")
        js = self.read("bbw_web/static/admin.js")
        parser = _IdParser()
        parser.feed(html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertTrue(all(src.startswith("/static/") for src in parser.external_scripts))
        self.assertNotIn('<form id="admin-login-form" class="admin-form" novalidate>', html)
        self.assertIn('id="admin-login-form" class="admin-form" method="post"', html)
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "caches.open"):
            self.assertNotIn(forbidden, js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("pendingRawResponse", js)
        self.assertIn("credentialsUnlocked()", js)
        self.assertIn("requestServerSensitiveLockOnHide", js)
        self.assertIn("requestServerTotpCancelOnHide", js)
        self.assertNotIn("preserveUnlock: true", js)
        self.assertIn("ADMIN_ENDPOINTS.userStatus", js)
        self.assertIn('id="admin-user-status-dialog"', html)

    def test_admin_user_detail_race_and_sensitive_field_contracts(self) -> None:
        js = self.read("bbw_web/static/admin.js")

        def function_block(name: str) -> str:
            marker = f"function {name}("
            start = js.index(marker)
            candidates = [
                position
                for position in (
                    js.find("\nfunction ", start + len(marker)),
                    js.find("\nasync function ", start + len(marker)),
                )
                if position >= 0
            ]
            end = min(candidates) if candidates else len(js)
            return js[start:end]

        self.assertIn("userDetailGeneration: 0", js)
        self.assertIn("function invalidateUserDetailRequests", js)
        self.assertIn("function isCurrentUserDetailRequest", js)
        for name in (
            "loadUserProfile",
            "loadUserConversations",
            "loadConversationMessages",
            "loadUserMedia",
            "accessMedia",
            "loadUserRelationships",
            "loadUserActivities",
            "loadUserRawResponses",
            "loadRawResponseDetail",
            "viewUserCredentials",
        ):
            block = function_block(name)
            self.assertIn("requestState", block, name)
            self.assertIn("isCurrentUserDetailRequest", block, name)
        self.assertIn("invalidateUserDetailRequests();", function_block("closeUserDetail"))
        self.assertIn("invalidateUserDetailRequests();", function_block("selectUserTab"))
        self.assertNotIn("ADMIN_ENDPOINTS.user(ADMIN_STATE.selectedUserId)", js)
        self.assertNotIn("ADMIN_ENDPOINTS.userRawResponse(ADMIN_STATE.selectedUserId", js)

        sensitive_handlers = (
            (
                '$("admin-login-form").addEventListener',
                '$("admin-logout").addEventListener',
                'clearFieldValues("admin-password")',
            ),
            (
                '$("admin-totp-start-form").addEventListener',
                '$("admin-totp-copy-secret").addEventListener',
                'clearFieldValues("admin-totp-start-password", "admin-totp-current-code")',
            ),
            (
                '$("admin-totp-confirm-form").addEventListener',
                '$("admin-totp-cancel").addEventListener',
                'clearFieldValues("admin-totp-confirm-code")',
            ),
            (
                '$("admin-password-form").addEventListener',
                '$("admin-unlock-form").addEventListener',
                'clearFieldValues("admin-current-password", "admin-new-password", "admin-new-password-confirm")',
            ),
            (
                '$("admin-unlock-form").addEventListener',
                '$("admin-unlock-cancel").addEventListener',
                'clearFieldValues("admin-unlock-password", "admin-unlock-totp")',
            ),
        )
        for start_marker, end_marker, clear_call in sensitive_handlers:
            segment = js.split(start_marker, 1)[1].split(end_marker, 1)[0]
            self.assertIn("finally {", segment, start_marker)
            self.assertIn(clear_call, segment, start_marker)

    def test_model_and_migration_owner_and_audit_constraints(self) -> None:
        models = self.read("bbw_prod/models.py")
        migration = self.read("migrations/versions/20260716_0001_initial_production.py")
        self.assertIn("password_encrypted: Mapped[dict[str, Any] | None]", models)
        self.assertIn('name="fk_media_objects_message_owner"', models)
        self.assertIn('ForeignKey("admin_users.id", ondelete="RESTRICT")', models)
        self.assertIn('ForeignKey("users.id", ondelete="RESTRICT")', models)
        self.assertIn("nullable=True", migration.split("'password_encrypted'", 1)[1][:100])
        self.assertIn("fk_media_objects_message_owner", migration)
        self.assertIn("audit log retention period has not elapsed", migration)

    def test_raw_payload_redaction_handles_camel_case_nested_json_and_urls(self) -> None:
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is not installed")

        stub_name = "bbw_prod.config"
        previous = sys.modules.get(stub_name)
        stub = types.ModuleType(stub_name)
        stub.Settings = object
        sys.modules[stub_name] = stub
        module_name = "bbw_prod._crypto_contract_test"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                ROOT / "bbw_prod/crypto.py",
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            result = module.redact_raw_payload(
                {
                    "accessToken": "top-secret",
                    "sessionTokenValue": "session-secret",
                    "payload": json.dumps({"refreshToken": "nested-secret", "ok": True}),
                    "avatarUrl": "https://example.invalid/image.jpg?signature=secret#fragment",
                    "header": "Bearer abcdefghijklmnopqrstuvwxyz",
                    "generic": "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
                }
            )
        finally:
            sys.modules.pop(module_name, None)
            if previous is None:
                sys.modules.pop(stub_name, None)
            else:
                sys.modules[stub_name] = previous

        self.assertEqual(result["accessToken"], "[REDACTED]")
        self.assertEqual(result["sessionTokenValue"], "[REDACTED]")
        self.assertEqual(json.loads(result["payload"])["refreshToken"], "[REDACTED]")
        self.assertEqual(result["avatarUrl"], "https://example.invalid/image.jpg")
        self.assertEqual(result["header"], "Bearer [REDACTED]")
        self.assertEqual(result["generic"], "[REDACTED_JWT]")

    def test_credential_keyring_is_fail_closed(self) -> None:
        from bbw_prod.config import ConfigurationError, Settings

        key_a = base64.b64encode(b"a" * 32).decode("ascii")
        key_b = base64.b64encode(b"b" * 32).decode("ascii")

        def settings(keys_json: str, *, master_key: str = key_a) -> Settings:
            value = Settings.__new__(Settings)
            object.__setattr__(value, "credential_keys_json", keys_json)
            object.__setattr__(value, "credential_keys_file", None)
            object.__setattr__(value, "credential_key_version", 1)
            object.__setattr__(value, "credential_master_key", master_key)
            object.__setattr__(value, "credential_master_key_file", None)
            return value

        self.assertEqual(settings('{"1":"' + key_a + '"}').load_credential_keyring()[1], b"a" * 32)
        with self.assertRaises(ConfigurationError):
            settings('{"01":"' + key_a + '"}').load_credential_keyring()
        with self.assertRaises(ConfigurationError):
            settings('{"1":"' + key_a + '","1":"' + key_a + '"}').load_credential_keyring()
        with self.assertRaises(ConfigurationError):
            settings('{"1":"' + key_a + '"}', master_key=key_b).load_credential_keyring()


if __name__ == "__main__":
    unittest.main()
