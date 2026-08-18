from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdminPhoneLoginContractTests(unittest.TestCase):
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
            "migrations/versions/20260818_0029_retire_phone_only_login_grants.py",
            "tests/test_admin_user_login.py",
            "tests/test_local_auth_credentials.py",
            "tests/test_production_contracts.py",
            "tests/test_provider_auth_boundaries.py",
            "tests/test_store_provider_integration.py",
        ):
            with self.subTest(path=path):
                ast.parse(self.read(path), filename=path)

    def test_obsolete_permission_is_not_part_of_the_runtime_model(self) -> None:
        models = self.read("bbw_prod/models.py")
        migration = self.read(
            "migrations/versions/20260818_0028_phone_only_login_permission.py"
        )
        retirement = self.read(
            "migrations/versions/20260818_0029_retire_phone_only_login_grants.py"
        )

        self.assertNotIn("phone_only_login_enabled: Mapped[bool]", models)
        # 该版本可能已经在已部署数据库执行，历史 revision 必须保留，应用层不再使用该列。
        self.assertIn('revision: str = "20260818_0028"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260815_0027"',
            migration,
        )
        self.assertIn('revision: str = "20260818_0029"', retirement)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260818_0028"',
            retirement,
        )
        self.assertIn("SET phone_only_login_enabled = false", retirement)
        self.assertNotIn("drop_column", retirement)

    def test_only_the_admin_surface_exposes_phone_login(self) -> None:
        index = self.read("bbw_web/static/index.html")
        app = self.read("bbw_web/static/app.js")
        admin_html = self.read("bbw_web/static/admin.html")
        admin_js = self.read("bbw_web/static/admin.js")
        admin_api = self.read("bbw_web/admin_api.py")
        credential_login = app.split("async function submitLoginCredentials", 1)[
            1
        ].split("async function submitLoginInvite", 1)[0]
        clear_sensitive = admin_js.split("function clearSensitiveDom", 1)[1].split(
            "function resetAdminState", 1
        )[0]

        self.assertNotIn('data-mode="onekey"', index)
        self.assertNotIn('S.loginMode === "onekey"', app)
        self.assertNotIn('mode: "onekey"', credential_login)
        self.assertIn('id="admin-user-login-open"', admin_html)
        self.assertIn('id="admin-user-login-dialog"', admin_html)
        self.assertIn('id="admin-user-login-phone"', admin_html)
        self.assertIn('id="admin-user-login-reason"', admin_html)
        self.assertIn("ADMIN_ENDPOINTS.userLogin", admin_js)
        self.assertIn("closeAdminUserLoginDialog();", clear_sensitive)
        self.assertIn('@router.post("/user-login")', admin_api)

        combined_admin = admin_html + admin_js + admin_api
        self.assertNotIn("userPhoneOnlyLogin", combined_admin)
        self.assertNotIn("pendingPhoneOnlyLogin", combined_admin)
        self.assertNotIn('/phone-only-login', combined_admin)
        self.assertNotIn("user.phone_only_login_changed", combined_admin)

    def test_public_login_rejects_onekey_and_admin_login_rechecks_binding(self) -> None:
        services = self.read("bbw_prod/services.py")
        persistence = self.read("bbw_web/persistence.py")
        api = self.read("bbw_web/api.py")
        bff = self.read("bbw_web/bff_server.py")
        admin_api = self.read("bbw_web/admin_api.py")
        store = self.read("bbw_web/store.py")

        public_api_login = api.split(
            'if request.method == "POST" and path == "/api/auth/login":', 1
        )[1].split("login_context: Any = None", 1)[0]
        legacy_login = bff.split('if path == "/api/auth/login":', 1)[1].split(
            'if path == "/api/auth/logout":', 1
        )[0]
        admin_login = admin_api.split("def admin_user_login(", 1)[1].split(
            '@router.post("/totp/start")', 1
        )[0]
        durable_completion = persistence.split("def complete_login(", 1)[1].split(
            "def restore_identity", 1
        )[0]
        onekey = store.split("def login_onekey", 1)[1].split(
            "def restore_from_disk", 1
        )[0]

        self.assertIn('if login_mode != "password"', public_api_login)
        self.assertNotIn("login_onekey", public_api_login)
        self.assertIn('if mode != "password"', legacy_login)
        self.assertNotIn("STORE.login_onekey", legacy_login)
        self.assertIn("allow_weak_onekey=False", bff)
        self.assertIn('"/api/admin/user-login"', api)

        self.assertIn("existing_upstream_uid", services)
        self.assertIn("require_existing_upstream_binding", services)
        self.assertIn("by_phone.upstream_uid != upstream_uid", services)
        self.assertNotIn("phone_only_login_enabled", services)
        self.assertIn("request_authorized=True", admin_login)
        self.assertIn("authenticated_uid != expected_uid", admin_login)
        self.assertIn("require_existing_upstream_binding=True", admin_login)
        self.assertIn("clear_login_failures=False", admin_login)
        self.assertIn('action="user.admin_phone_login"', admin_login)
        self.assertIn("requires a prechecked account binding", durable_completion)
        self.assertIn("completion.user.id != login_context.existing_user_id", durable_completion)
        self.assertIn(
            "completion.external_account.id\n                != login_context.existing_external_account_id",
            durable_completion,
        )

        self.assertIn("if not (self.allow_weak_onekey or request_authorized)", onekey)
        self.assertIn("self.upstream_auth_timeout_sec", onekey)
        self.assertIn("client.timeout = previous_timeout", onekey)
        self.assertIn("ProviderUnavailable", onekey)
        self.assertIn("ProviderAuthenticationRejected", onekey)


if __name__ == "__main__":
    unittest.main()
