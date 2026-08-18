from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PhoneOnlyLoginContractTests(unittest.TestCase):
    @staticmethod
    def read(path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_changed_python_sources_parse(self) -> None:
        for path in (
            "bbw_prod/models.py",
            "bbw_prod/services.py",
            "bbw_web/admin_api.py",
            "bbw_web/api.py",
            "bbw_web/bff_server.py",
            "bbw_web/persistence.py",
            "bbw_web/store.py",
            "migrations/versions/20260818_0028_phone_only_login_permission.py",
            "tests/test_local_auth_credentials.py",
            "tests/test_production_contracts.py",
            "tests/test_provider_auth_boundaries.py",
            "tests/test_store_provider_integration.py",
        ):
            with self.subTest(path=path):
                ast.parse(self.read(path), filename=path)

    def test_permission_defaults_off_and_has_a_linear_migration(self) -> None:
        models = self.read("bbw_prod/models.py")
        migration = self.read(
            "migrations/versions/20260818_0028_phone_only_login_permission.py"
        )

        self.assertIn("phone_only_login_enabled: Mapped[bool]", models)
        self.assertIn('server_default=text("false")', models)
        self.assertIn('revision: str = "20260818_0028"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260815_0027"',
            migration,
        )
        self.assertIn('server_default=sa.text("false")', migration)

    def test_browser_and_admin_surfaces_expose_the_permission(self) -> None:
        index = self.read("bbw_web/static/index.html")
        app = self.read("bbw_web/static/app.js")
        admin_html = self.read("bbw_web/static/admin.html")
        admin_js = self.read("bbw_web/static/admin.js")
        admin_api = self.read("bbw_web/admin_api.py")
        clear_sensitive = admin_js.split("function clearSensitiveDom", 1)[1].split(
            "function resetAdminState", 1
        )[0]
        phone_dialog = admin_js.split("function closePhoneOnlyLoginDialog", 1)[
            1
        ].split("function closeMatchPoolOnlineListDialog", 1)[0]
        credential_login = app.split("async function submitLoginCredentials", 1)[
            1
        ].split("async function submitLoginInvite", 1)[0]
        permission_endpoint = admin_api.split(
            'def set_user_phone_only_login(', 1
        )[1].split('def set_user_match_pool_online_list(', 1)[0]

        self.assertIn('data-mode="onekey">手机号登录</button>', index)
        self.assertIn('S.loginMode === "onekey"', app)
        self.assertIn('mode: "onekey"', app)
        self.assertIn('id="admin-phone-only-login-dialog"', admin_html)
        self.assertIn("ADMIN_ENDPOINTS.userPhoneOnlyLogin", admin_js)
        self.assertIn(
            '@router.post("/users/{user_id}/phone-only-login")', admin_api
        )
        self.assertIn('action="user.phone_only_login_changed"', admin_api)
        self.assertIn(
            'not str(account.upstream_uid or "").strip()', permission_endpoint
        )
        self.assertIn("closePhoneOnlyLoginDialog();", clear_sensitive)
        self.assertIn("ADMIN_STATE.pendingPhoneOnlyLogin = null;", phone_dialog)
        self.assertIn(
            'clearInputValues($("admin-phone-only-login-form"));', phone_dialog
        )
        self.assertIn(
            'const labPhoneOnlyLogin = S.loginMode === "onekey" && S.labEnabled;',
            credential_login,
        )
        self.assertIn(
            "S.inviteLoginAvailable === false && !labPhoneOnlyLogin",
            credential_login,
        )

    def test_server_checks_the_grant_before_and_after_provider_authentication(self) -> None:
        services = self.read("bbw_prod/services.py")
        api = self.read("bbw_web/api.py")
        bff = self.read("bbw_web/bff_server.py")
        store = self.read("bbw_web/store.py")
        onekey = store.split("def login_onekey", 1)[1].split(
            "def restore_from_disk", 1
        )[0]

        self.assertIn("phone_only_login_enabled: bool = False", services)
        self.assertIn("if phone_only_login and not bool(user.phone_only_login_enabled)", services)
        self.assertIn('str(existing.upstream_uid or "").strip()', services)
        self.assertIn("by_phone.upstream_uid != upstream_uid", services)
        self.assertIn('code": "PHONE_ONLY_LOGIN_NOT_ENABLED"', api)
        self.assertIn("phone_only_login_authorized=phone_only_login_authorized", api)
        self.assertIn("_request_phone_only_login_authorized", bff)
        self.assertIn("request_authorized=request_authorized", bff)
        self.assertIn("if not (self.allow_weak_onekey or request_authorized)", store)
        self.assertIn("self.upstream_auth_timeout_sec", onekey)
        self.assertIn("client.timeout = previous_timeout", onekey)
        self.assertIn("ProviderUnavailable", onekey)
        self.assertIn("ProviderAuthenticationRejected", onekey)


if __name__ == "__main__":
    unittest.main()
