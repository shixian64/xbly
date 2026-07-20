from __future__ import annotations

import ast
import base64
import importlib.util
import json
import sys
import types
import unittest
import uuid
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch


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

    def test_rq_job_ids_use_only_supported_characters(self) -> None:
        persistence = self.read("bbw_web/persistence.py")
        jobs = self.read("bbw_web/jobs.py")
        scheduler = self.read("bbw_web/scheduler.py")
        source = "\n".join((persistence, jobs, scheduler))

        for forbidden in (
            'job_id=f"archive-message:',
            'job_id=f"archive-media:',
            'job_id=f"sync-history:',
            'job_id=f"history-response:',
            'job_id=f"social-snapshot:',
            'job_id=f"product-event:',
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('job_id = f"archive-message-', persistence)
        self.assertIn('job_id=f"archive-media-', jobs)
        self.assertIn('job_id=f"sync-history-', jobs)

    def test_conversation_freshness_jobs_and_schema_are_wired(self) -> None:
        models = self.read("bbw_prod/models.py")
        repositories = self.read("bbw_prod/repositories.py")
        persistence = self.read("bbw_web/persistence.py")
        jobs = self.read("bbw_web/jobs.py")
        compose = self.read("compose.yaml")
        config = self.read("bbw_prod/config.py")
        migration = self.read(
            "migrations/versions/20260720_0004_conversation_unread_observed_at.py"
        )

        self.assertIn("unread_observed_at", models)
        self.assertIn("unread_is_newer", repositories)
        self.assertIn("metadata_without_preview", repositories)
        self.assertIn("def mark_peers_read", repositories)
        self.assertIn('Queue("im-ingest"', persistence)
        self.assertIn("self.im_ingest_queue.enqueue(", persistence)
        self.assertNotIn("self.sync_queue.enqueue(", persistence)
        self.assertIn("def mark_conversations_read", persistence)
        self.assertIn("interval_due_at", jobs)
        self.assertIn("due_at = min(due_candidates)", jobs)
        self.assertIn("sync-worker:", compose)
        self.assertIn("BBW_RQ_QUEUES: sync", compose)
        self.assertIn("im-ingest-worker:", compose)
        self.assertIn("BBW_RQ_QUEUES: im-ingest", compose)
        self.assertIn("critical,im-ingest,default,media,sync,transcode", config)
        self.assertIn('revision: str = "20260720_0004"', migration)

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
            "/users/{user_id}/match-pool-online-list",
            "/users/{user_id}/nearby-custom-city",
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
        self.assertIn("ADMIN_ENDPOINTS.userMatchPoolOnlineList", js)
        self.assertIn('id="admin-match-pool-online-list-dialog"', html)
        self.assertIn("ADMIN_ENDPOINTS.userNearbyCustomCity", js)
        self.assertIn('id="admin-nearby-custom-city-dialog"', html)
        self.assertIn('featureInput.setAttribute("role", "switch")', js)
        self.assertIn("user.match_pool_online_list_changed", api)
        self.assertIn("修改非匹配主动私信授权", html)
        self.assertIn("非匹配主动私信", js)
        self.assertIn("在线用户列表、资料、动态和好友申请始终可用", js)
        self.assertIn("user.nearby_custom_city_changed", api)
        self.assertIn("附近的人自定义城市", js)

    def test_private_message_policy_uses_server_owned_match_and_conversation_grants(self) -> None:
        persistence = self.read("bbw_web/persistence.py")
        repositories = self.read("bbw_prod/repositories.py")
        api = self.read("bbw_web/api.py")

        for marker in (
            'MESSAGE_POLICY_PROVIDER = "web-policy"',
            'MESSAGE_POLICY_MATCH_KIND = "match"',
            'MESSAGE_POLICY_CONVERSATION_KIND = "message_peer"',
            'SOCIAL_RELATIONSHIP_PROVIDER = "beibeiwu"',
            'SOCIAL_FRIEND_KIND = "friend"',
            "def grant_message_peers(",
            "def can_message_peer(",
            "def message_policy_allowed_peers(",
            "def message_policy_match_peers(",
            "def remember_message_policy_response(",
            '"/api/match/online"',
            '"/api/match/local"',
            '"/api/im/conversations"',
            '"/api/im/rest/send"',
            '"/api/im/flash/send"',
        ):
            self.assertIn(marker, persistence)
        self.assertIn("identity.match_pool_online_list_enabled", persistence)
        self.assertIn("metadata[\"server_owned\"] = True", persistence)
        self.assertIn("ConversationRepository(db).exists_for_peer(", persistence)
        self.assertIn("def exists_for_peer(", repositories)
        self.assertIn("Conversation.peer_upstream_uid == peer_upstream_uid", repositories)
        self.assertIn('Conversation.kind == kind', repositories)
        self.assertIn("persistence.can_message_peer(", api)
        self.assertIn("persistence.message_policy_allowed_peers(identity)", api)
        self.assertIn("MATCH_DM_GRANT_PERSISTENCE_FAILED", api)
        self.assertIn("persistence.remember_message_policy_response(", api)

    def test_archived_direct_conversation_authorizes_private_message_peer(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self, scalar_values: list[object | None]) -> None:
                self.scalar_values = list(scalar_values)
                self.statements: list[object] = []

            def scalar(self, statement: object) -> object | None:
                self.statements.append(statement)
                return self.scalar_values.pop(0)

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )

        for scalar_values, expected in (([None, object()], True), ([None, None], False)):
            with self.subTest(archived_conversation=expected):
                db = FakeDb(scalar_values)

                @contextmanager
                def fake_session_scope():
                    yield db

                with patch("bbw_web.persistence.session_scope", fake_session_scope):
                    self.assertIs(runtime.can_message_peer(identity, "9"), expected)
                self.assertEqual(len(db.statements), 2)

    def test_active_friend_relationship_authorizes_private_message_peer(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalar(self, statement: object) -> object:
                self.statements.append(statement)
                return object()

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        db = FakeDb()

        @contextmanager
        def fake_session_scope():
            yield db

        with patch("bbw_web.persistence.session_scope", fake_session_scope):
            self.assertTrue(runtime.can_message_peer(identity, "9"))

        self.assertEqual(len(db.statements), 1)
        statement = str(db.statements[0])
        self.assertIn("relationships.provider", statement)
        self.assertIn("relationships.kind", statement)
        params = {str(value) for value in db.statements[0].compile().params.values()}
        self.assertIn("beibeiwu", params)
        self.assertIn("friend", params)

    def test_message_policy_allowed_peers_restores_friend_and_match_grants(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statement = None

            def scalars(self, statement: object) -> list[str]:
                self.statement = statement
                return ["9", "10", "9", "42", ""]

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        db = FakeDb()

        @contextmanager
        def fake_session_scope():
            yield db

        with patch("bbw_web.persistence.session_scope", fake_session_scope):
            peers = runtime.message_policy_allowed_peers(identity)

        self.assertEqual(peers, ["9", "10"])
        params = {str(value) for value in db.statement.compile().params.values()}
        self.assertIn("beibeiwu", params)
        self.assertIn("friend", params)
        self.assertIn("web-policy", params)

    def test_archived_messages_are_returned_for_the_authenticated_owner(self) -> None:
        from datetime import UTC, datetime

        try:
            from bbw_web import archive_api
            from bbw_web.persistence import UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        identity = UserIdentity(
            user_id=owner_id,
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        conversation = types.SimpleNamespace(id=conversation_id)
        newer = types.SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="newer-message",
            extra_data={"message_key": "newer-key", "is_peer_read": True},
            body="较新的消息",
            message_type="text",
            sender_upstream_uid="9",
            recipient_upstream_uid="42",
            direction="incoming",
            status="sent",
            occurred_at=datetime(2026, 7, 17, 15, 31, tzinfo=UTC),
        )
        older = types.SimpleNamespace(
            id=uuid.uuid4(),
            upstream_message_id="older-message",
            extra_data={"object_name": "TIMTextElem"},
            body="较早的消息",
            message_type="text",
            sender_upstream_uid="42",
            recipient_upstream_uid="9",
            direction="outgoing",
            status="sent",
            occurred_at=datetime(2026, 7, 17, 15, 30, tzinfo=UTC),
        )
        conversation_calls: list[tuple[object, str]] = []
        message_calls: list[tuple[object, object, object, int]] = []
        persistence = types.SimpleNamespace(
            require_identity=lambda sid: identity if sid == "sid" else None,
            rate_limit=lambda *_args, **_kwargs: True,
        )
        request = types.SimpleNamespace(
            cookies={archive_api.legacy.COOKIE_NAME: "sid"},
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(persistence=persistence)
            ),
        )

        @contextmanager
        def fake_session_scope():
            yield object()

        conversation_repo = types.SimpleNamespace(
            get_by_peer=lambda user_id, peer: conversation_calls.append((user_id, peer))
            or conversation
        )

        def list_for_conversation(user_id, target_conversation_id, *, before, limit):
            message_calls.append((user_id, target_conversation_id, before, limit))
            return [newer, older]

        message_repo = types.SimpleNamespace(
            list_for_conversation=list_for_conversation
        )
        with (
            patch("bbw_web.archive_api.session_scope", fake_session_scope),
            patch(
                "bbw_web.archive_api.ConversationRepository",
                return_value=conversation_repo,
            ),
            patch(
                "bbw_web.archive_api.MessageRepository",
                return_value=message_repo,
            ),
        ):
            payload = archive_api.archived_messages(
                request,
                peer="9",
                limit=200,
                before=None,
            )

        self.assertEqual(conversation_calls, [(owner_id, "9")])
        self.assertEqual(message_calls, [(owner_id, conversation_id, None, 200)])
        self.assertEqual(
            [item["id"] for item in payload["items"]],
            ["older-message", "newer-message"],
        )
        self.assertEqual(payload["items"][0]["flow"], "out")
        self.assertEqual(payload["items"][0]["source"], "archive")
        self.assertEqual(payload["items"][1]["text"], "较新的消息")
        self.assertTrue(payload["items"][1]["is_peer_read"])
        self.assertFalse(payload["has_more"])

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

    def test_user_login_uses_a_real_two_stage_invitation_gate(self) -> None:
        html = self.read("bbw_web/static/index.html")
        js = self.read("bbw_web/static/app.js")
        api = self.read("bbw_web/api.py")
        persistence = self.read("bbw_web/persistence.py")
        services = self.read("bbw_prod/services.py")
        bff = self.read("bbw_web/bff_server.py")

        self.assertIn('id="login-credentials-step"', html)
        self.assertIn('id="login-invite-step" class="hide"', html)
        self.assertLess(html.index('id="password"'), html.index('id="invite-code"'))
        self.assertIn('id="login-back"', html)

        credential_step = js.split("async function submitLoginCredentials", 1)[1].split(
            "async function submitLoginInvite", 1
        )[0]
        invite_step = js.split("async function submitLoginInvite", 1)[1].split(
            "async function cancelPendingLogin", 1
        )[0]
        completion = js.split("function completeBrowserLogin", 1)[1].split(
            "async function submitLoginCredentials", 1
        )[0]
        self.assertNotIn('$("invite-code")', credential_step)
        self.assertNotIn("invite_code", credential_step)
        self.assertNotIn('$("password")', invite_step)
        self.assertNotIn("password:", invite_step)
        self.assertIn('body: JSON.stringify({ invite_code: inviteCode })', invite_step)
        self.assertIn("S.authenticated = true", completion)
        self.assertNotIn("S.authenticated = true", credential_step)
        self.assertNotIn("S.authenticated = true", invite_step)

        self.assertIn('@app.post("/api/auth/invite"', api)
        self.assertIn('@app.post("/api/auth/invite/cancel"', api)
        self.assertIn("_pending_cookie_name", api)
        self.assertIn("begin_pending_login", api)
        self.assertIn("finish_pending_login", api)
        pending_branch = api.split("if login_context is not None and login_context.requires_invite", 1)[1].split(
            "identity = persistence.complete_login", 1
        )[0]
        self.assertIn("return response", pending_branch)
        self.assertNotIn("name=legacy.COOKIE_NAME", pending_branch)

        self.assertIn("PENDING_LOGIN_SECONDS = 5 * 60", persistence)
        self.assertIn("self.cipher.encrypt_json", persistence)
        self.assertIn("self.redis.getdel", persistence)
        self.assertIn('purpose="web-login.pending"', persistence)
        self.assertIn("def precheck_credentials", services)
        self.assertIn("bool(self.settings.invite_required)", services)
        self.assertIn(
            "if require_invite and self.settings.invite_required and not invite_code",
            services,
        )
        credential_precheck = services.split("def precheck_credentials", 1)[1].split(
            "def precheck", 1
        )[0]
        self.assertEqual(
            credential_precheck.count("bool(self.settings.invite_required)"), 1
        )
        self.assertIn("existing.id,\n                None,\n                False", credential_precheck)
        self.assertIn("login_context.requires_invite", persistence)
        existing_completion = services.split("if by_phone is not None:", 1)[1].split(
            "if by_uid is not None:", 1
        )[0]
        self.assertIn("self.invites.validate(invite_code, for_update=True)", existing_completion)
        self.assertIn("self.invites.consume_locked(invite)", existing_completion)

        self.assertIn("authenticated\": False", api)
        self.assertIn("status_code=202", pending_branch)
        self.assertIn("def _cancel_pending_runtime", api)
        cancel_helper = api.split("def _cancel_pending_runtime", 1)[1].split(
            "async def _legacy_dispatch", 1
        )[0]
        self.assertLess(
            cancel_helper.index("persistence.require_identity(raw_sid)"),
            cancel_helper.index("web_user.app.auth.logout()"),
        )
        self.assertIn("def _auth_json_request_error", api)
        self.assertIn('content_type.startswith("application/json")', api)
        login_dispatch = api.split(
            'if request.method == "POST" and path in {"/api/auth/login", "/api/auth/sms-login"}:',
            1,
        )[1].split("# Never trust an authenticated object", 1)[0]
        self.assertLess(
            login_dispatch.index("_auth_json_request_error(request)"),
            login_dispatch.index("_cancel_pending_runtime"),
        )
        self.assertIn("if web_user.pending_until is None:", api)
        self.assertIn("normalized_phone = account_context.normalized_phone", api)
        self.assertIn('"invite_login": INVITE_LOGIN_ENABLED', bff)

    def test_user_session_bootstrap_does_not_flash_the_login_screen(self) -> None:
        html = self.read("bbw_web/static/index.html")
        css = self.read("bbw_web/static/app.css")
        js = self.read("bbw_web/static/app.js")

        self.assertIn('id="screen-boot" class="boot-screen"', html)
        self.assertIn('id="screen-login" class="login-screen hide"', html)
        self.assertIn('id="screen-app" class="app-shell hide"', html)
        self.assertIn(".boot-screen", css)
        hide_rule = css.split(".hide {", 1)[1].split("}", 1)[0]
        self.assertIn("display: none !important", hide_rule)
        self.assertIn("auth-bootstrap", html)

        show_login = js.split("function showLogin", 1)[1].split("function applyUser", 1)[0]
        self.assertIn('bootScreen.classList.add("hide")', show_login)
        self.assertIn('bootScreen.setAttribute("aria-busy", "false")', show_login)
        self.assertIn('$("screen-login").classList.toggle("hide", !show)', show_login)
        self.assertIn('$("screen-app").classList.toggle("hide", show)', show_login)

        boot = js.split("(async function boot()", 1)[1].split("})();", 1)[0]
        self.assertIn("await Promise.all([", boot)
        self.assertNotIn("await loadFeatures();", boot)
        self.assertLess(boot.index('api("/api/me"'), boot.index("showLogin(false)"))
        self.assertIn("showLogin(true, true)", boot)

    def test_model_and_migration_owner_and_audit_constraints(self) -> None:
        models = self.read("bbw_prod/models.py")
        migration = self.read("migrations/versions/20260716_0001_initial_production.py")
        permission_migration = self.read(
            "migrations/versions/20260717_0002_match_pool_online_list_permission.py"
        )
        nearby_permission_migration = self.read(
            "migrations/versions/20260717_0003_nearby_custom_city_permission.py"
        )
        self.assertIn("password_encrypted: Mapped[dict[str, Any] | None]", models)
        self.assertIn('name="fk_media_objects_message_owner"', models)
        self.assertIn('ForeignKey("admin_users.id", ondelete="RESTRICT")', models)
        self.assertIn('ForeignKey("users.id", ondelete="RESTRICT")', models)
        self.assertIn("nullable=True", migration.split("'password_encrypted'", 1)[1][:100])
        self.assertIn("fk_media_objects_message_owner", migration)
        self.assertIn("audit log retention period has not elapsed", migration)
        self.assertIn("match_pool_online_list_enabled: Mapped[bool]", models)
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "20260716_0001"', permission_migration)
        self.assertIn('"match_pool_online_list_enabled"', permission_migration)
        self.assertIn('server_default=sa.text("false")', permission_migration)
        self.assertIn('op.drop_column("users", "match_pool_online_list_enabled")', permission_migration)
        self.assertIn("nearby_custom_city_enabled: Mapped[bool]", models)
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "20260717_0002"', nearby_permission_migration)
        self.assertIn('"nearby_custom_city_enabled"', nearby_permission_migration)
        self.assertIn('server_default=sa.text("false")', nearby_permission_migration)
        self.assertIn('op.drop_column("users", "nearby_custom_city_enabled")', nearby_permission_migration)

    def test_new_login_flushes_user_before_external_account(self) -> None:
        services = self.read("bbw_prod/services.py")
        complete_login = services.split("def complete_login", 1)[1].split(
            "@dataclass(frozen=True, slots=True)\nclass AdminSessionState", 1
        )[0]
        add_user = complete_login.index("self.db.add(user)")
        flush_user = complete_login.index("self.db.flush()", add_user)
        add_account = complete_login.index("self.db.add(account)", flush_user)
        self.assertLess(add_user, flush_user)
        self.assertLess(flush_user, add_account)

    def test_product_login_does_not_wait_for_bootstrap_requests(self) -> None:
        source = self.read("bbw_web/bff_server.py")
        password_login = source.split('if path == "/api/auth/login":', 1)[1].split(
            'if path == "/api/auth/logout":', 1
        )[0]
        sms_login = source.split('if path == "/api/auth/sms-login":', 1)[1].split(
            "\n        u = self.user(sid)", 1
        )[0]
        self.assertNotIn("app.bootstrap()", password_login)
        self.assertNotIn("_enrich_session_profile", password_login)
        self.assertNotIn("_enrich_session_profile", sms_login)

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

    def test_pending_login_credentials_are_encrypted_bound_and_one_time(self) -> None:
        try:
            from bbw_prod.crypto import CredentialCipher
            from bbw_web.persistence import PendingLoginExpired, RuntimePersistence
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeRedis:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def set(self, name: str, value: str, **_kwargs: object) -> bool:
                self.values[name] = value
                return True

            def get(self, name: str) -> str | None:
                return self.values.get(name)

            def getdel(self, name: str) -> str | None:
                return self.values.pop(name, None)

            def delete(self, *names: str) -> int:
                removed = 0
                for name in names:
                    removed += int(self.values.pop(name, None) is not None)
                return removed

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        runtime.settings = types.SimpleNamespace(redis_prefix="contract")
        runtime.redis = FakeRedis()
        runtime.cipher = CredentialCipher({1: b"k" * 32}, 1)
        runtime.session_hmac_key = b"s" * 32
        raw_sid = "pending_contract_sid_1234567890"
        phone = "13800138000"
        password = "upstream-secret-password"

        runtime.begin_pending_login(
            raw_sid=raw_sid,
            phone=phone,
            password=password,
            mode="password",
            upstream_uid="42",
            client_ip="127.0.0.1",
            user_agent="contract-agent",
        )
        redis_value = runtime.redis.values[runtime._pending_login_key(raw_sid)]
        self.assertNotIn(phone, redis_value)
        self.assertNotIn(password, redis_value)

        pending = runtime.peek_pending_login(
            raw_sid,
            client_ip="127.0.0.1",
            user_agent="contract-agent",
        )
        self.assertEqual(pending.phone, phone)
        self.assertEqual(pending.password, password)
        claimed = runtime.claim_pending_login(
            raw_sid,
            client_ip="127.0.0.1",
            user_agent="contract-agent",
        )
        self.assertEqual(claimed.upstream_uid, "42")
        with self.assertRaises(PendingLoginExpired):
            runtime.claim_pending_login(
                raw_sid,
                client_ip="127.0.0.1",
                user_agent="contract-agent",
            )

        runtime.begin_pending_login(
            raw_sid=raw_sid,
            phone=phone,
            password=password,
            mode="password",
            upstream_uid="42",
            client_ip="127.0.0.1",
            user_agent="contract-agent",
        )
        with self.assertRaises(PendingLoginExpired):
            runtime.peek_pending_login(
                raw_sid,
                client_ip="127.0.0.2",
                user_agent="contract-agent",
            )


if __name__ == "__main__":
    unittest.main()
