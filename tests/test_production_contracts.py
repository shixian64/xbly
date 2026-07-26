from __future__ import annotations

import ast
import base64
import hashlib
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
        roots = ("bbw_agent", "bbw_prod", "bbw_web", "migrations")
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

    def test_message_archival_is_batched_across_http_rq_and_database_work(self) -> None:
        archive_api = self.read("bbw_web/archive_api.py")
        persistence = self.read("bbw_web/persistence.py")
        jobs = self.read("bbw_web/jobs.py")
        app_js = self.read("bbw_web/static/app.js")
        frontend_archive = app_js.split("function rememberArchivedMessageKey", 1)[
            1
        ].split("async function withPending", 1)[0]

        self.assertIn("class MessageReportBatch(BaseModel):", archive_api)
        self.assertIn('items: list[MessageReport] = Field(min_length=1, max_length=20)', archive_api)
        self.assertIn('@router.post("/messages/batch"', archive_api)
        self.assertIn("persistence.enqueue_message_archive_batch(", archive_api)
        self.assertIn("def enqueue_message_archive_batch(", persistence)
        self.assertIn('"bbw_web.jobs.archive_message_batch_job"', persistence)
        self.assertIn('job_id = f"archive-message-batch-', persistence)
        self.assertIn("def archive_message_batch_job(", jobs)
        self.assertIn("conversations = _upsert_conversations(", jobs)
        self.assertIn("with session_scope() as db:", jobs)
        self.assertIn("_dispatch_media_outboxes(outbox_ids, limit=len(outbox_ids))", jobs)
        self.assertIn("const MESSAGE_ARCHIVE_BATCH_SIZE = 16;", app_js)
        self.assertIn("const MESSAGE_ARCHIVE_BATCH_DELAY_MS = 75;", app_js)
        self.assertIn("const MESSAGE_ARCHIVE_KEEPALIVE_MAX_BYTES = 48 * 1024;", app_js)
        self.assertIn('fetch("/api/archive/messages/batch"', frontend_archive)
        self.assertIn("items.map((item) => item.payload)", frontend_archive)
        self.assertIn("nextMessageArchiveBatch(now)", frontend_archive)
        self.assertIn("messageArchiveRequestByteLength(body)", frontend_archive)
        self.assertIn("messageArchiveBatchBody([...items, item])", frontend_archive)
        self.assertIn("if (response.status === 422)", frontend_archive)
        self.assertIn("items.slice(0, midpoint)", frontend_archive)
        self.assertIn("items.slice(midpoint)", frontend_archive)
        self.assertNotIn('fetch("/api/archive/messages",', frontend_archive)

    def test_product_events_leave_requests_through_a_bounded_batch_queue(self) -> None:
        persistence = self.read("bbw_web/persistence.py")
        jobs = self.read("bbw_web/jobs.py")
        capture = persistence.split("def capture_product_response", 1)[1]
        worker = persistence.split("def _product_event_worker", 1)[1].split(
            "def health", 1
        )[0]

        self.assertIn("PRODUCT_EVENT_QUEUE_MAX = 1024", persistence)
        self.assertIn("PRODUCT_EVENT_BATCH_SIZE = 32", persistence)
        self.assertIn("self._product_event_queue.put_nowait(", persistence)
        self.assertIn("grouped.setdefault(item.owner_user_id", worker)
        self.assertIn('"bbw_web.jobs.record_product_events_batch"', worker)
        self.assertIn('job_id=f"product-event-batch-', worker)
        self.assertIn("self._queue_product_event(", capture)
        self.assertNotIn('"bbw_web.jobs.record_product_event"', capture)
        self.assertNotIn("self.mark_conversations_read(", capture)
        self.assertIn("def record_product_events_batch(", jobs)
        self.assertIn("relationship_cache: dict[tuple[str, str]", jobs)
        self.assertIn('path == "/api/im/read"', jobs)
        self.assertIn("ConversationRepository(db).mark_peers_read(", jobs)
        self.assertIn("with session_scope() as db:", jobs)

    def test_history_responses_leave_requests_through_a_bounded_batch_queue(self) -> None:
        persistence = self.read("bbw_web/persistence.py")
        jobs = self.read("bbw_web/jobs.py")
        capture = persistence.split("def capture_product_response", 1)[1]
        worker = persistence.split("def _history_response_worker", 1)[1].split(
            "def health", 1
        )[0]

        self.assertIn("HISTORY_RESPONSE_QUEUE_MAX = 64", persistence)
        self.assertIn("HISTORY_RESPONSE_BATCH_SIZE = 8", persistence)
        self.assertIn("HISTORY_CONVERSATION_REFRESH_SECONDS = 120", persistence)
        self.assertIn("self._history_response_queue.put_nowait(", persistence)
        self.assertIn("_history_response_digest(path, query, response_data)", capture)
        self.assertIn("self._queue_history_response(", capture)
        self.assertNotIn('"bbw_web.jobs.ingest_history_response"', capture)
        self.assertIn('"bbw_web.jobs.ingest_history_responses_batch"', worker)
        self.assertIn('job_id=(\n                            f"history-response-batch-', worker)
        self.assertIn("def ingest_history_responses_batch(", jobs)
        self.assertIn("_ingest_history_response_in_session(", jobs)
        self.assertIn("with session_scope() as db:", jobs)
        self.assertIn('"history_response_queue": {', persistence)

    def test_authenticated_identity_uses_one_joined_binding_lookup(self) -> None:
        persistence = self.read("bbw_web/persistence.py")
        private_policy = self.read("bbw_web/private_message_policy.py")
        repositories = self.read("bbw_prod/repositories.py")
        jobs = self.read("bbw_web/jobs.py")
        require_identity = persistence.split("def require_identity", 1)[1].split(
            "def grant_message_peers", 1
        )[0]
        owner_binding = jobs.split("def _load_owner_binding", 1)[1].split(
            "def _merge_dict", 1
        )[0]

        self.assertIn("def get_user_binding(", repositories)
        self.assertIn("select(User, ExternalAccount)", repositories)
        self.assertIn("ExternalAccount.id == external_account_id", repositories)
        self.assertIn("get_user_binding(", require_identity)
        self.assertNotIn("UserRepository(db).get", require_identity)
        self.assertNotIn("get_for_user", require_identity)
        self.assertIn("get_user_binding(", owner_binding)

    def test_http_responses_expose_timing_and_warn_on_slow_api_requests(self) -> None:
        api = self.read("bbw_web/api.py")
        middleware = api.split("async def security_headers", 1)[1].split(
            '@app.get("/livez"', 1
        )[0]

        self.assertIn("SLOW_HTTP_REQUEST_MS = 1000.0", api)
        self.assertIn('response.headers["Server-Timing"]', middleware)
        self.assertIn('"event": "slow_http_request"', middleware)
        self.assertIn('request.url.path.startswith(("/api/", "/admin"))', middleware)
        self.assertIn('"threshold_ms": SLOW_HTTP_REQUEST_MS', middleware)

    def test_static_assets_bypass_session_and_legacy_dispatch(self) -> None:
        source = self.read("bbw_web/api.py")
        direct_static = source.split("def _static_asset_path", 1)[1].split(
            '@app.get("/admin"', 1
        )[0]

        self.assertIn('@app.api_route("/", methods=["GET", "HEAD"]', direct_static)
        self.assertIn('"/static/{asset_path:path}"', direct_static)
        self.assertIn("candidate.relative_to(STATIC_DIR.resolve())", direct_static)
        self.assertIn('"public, max-age=31536000, immutable"', direct_static)
        self.assertIn('"public, max-age=300"', direct_static)
        self.assertIn('"no-cache"', direct_static)
        self.assertIn("return FileResponse(", direct_static)
        self.assertNotIn("require_identity", direct_static)
        self.assertNotIn("_legacy_dispatch", direct_static)

        try:
            from fastapi import HTTPException
            from bbw_web import api
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        versioned_request = types.SimpleNamespace(query_params={"v": "content-hash"})
        plain_request = types.SimpleNamespace(query_params={})
        versioned = api.static_asset(versioned_request, "app.js")
        unversioned = api.static_asset(plain_request, "app.js")
        vendor = api.static_asset(
            plain_request,
            "vendor/tencent-cloud-chat-3.6.6.js",
        )
        image = api.static_asset(plain_request, "tuiemoji/emoji_3.png")

        self.assertEqual(
            versioned.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )
        self.assertEqual(unversioned.headers["cache-control"], "no-cache")
        self.assertEqual(
            vendor.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )
        self.assertEqual(image.headers["cache-control"], "public, max-age=300")
        with self.assertRaises(HTTPException):
            api.static_asset(plain_request, "../api.py")

    def test_conversation_summary_joins_latest_message_in_one_query(self) -> None:
        repositories = self.read("bbw_prod/repositories.py")
        persistence = self.read("bbw_web/persistence.py")
        summary = persistence.split("def conversation_summary_map", 1)[1].split(
            "def mark_conversations_read", 1
        )[0]

        self.assertIn("def list_for_peers_with_latest(", repositories)
        self.assertIn('Message.__table__.alias("latest_conversation_message")', repositories)
        self.assertIn(".correlate(Conversation)", repositories)
        self.assertIn("select(Conversation, Message)", repositories)
        self.assertIn("list_for_peers_with_latest(", summary)
        self.assertNotIn("latest_for_conversations(", summary)
        self.assertNotIn("MessageRepository", summary)

        try:
            from bbw_prod.repositories import ConversationRepository
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        conversation = types.SimpleNamespace(id=uuid.uuid4())
        message = types.SimpleNamespace(id=uuid.uuid4())

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def execute(self, statement: object) -> list[tuple[object, object]]:
                self.statements.append(statement)
                return [(conversation, message)]

        db = FakeDb()
        rows = ConversationRepository(db).list_for_peers_with_latest(
            uuid.uuid4(),
            ["9", "9", "", "10"],
        )

        self.assertEqual(rows, [(conversation, message)])
        self.assertEqual(len(db.statements), 1)
        statement = str(db.statements[0])
        self.assertIn("latest_conversation_message", statement)
        self.assertIn("LEFT OUTER JOIN messages", statement)
        self.assertIn("conversations.peer_upstream_uid", statement)

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
        self.assertIn(
            "critical,im-ingest,agent-control,agent,default,media,sync,",
            config,
        )
        self.assertIn('revision: str = "20260720_0004"', migration)

    def test_agent_worker_is_packaged_secret_complete_and_operable(self) -> None:
        dockerfile = self.read("Dockerfile")
        dockerignore = self.read(".dockerignore")
        compose = self.read("compose.yaml")
        deployment = self.read("docs/14_PRODUCTION_DEPLOYMENT.md")
        readme = self.read("README.md")
        jobs = self.read("bbw_web/jobs.py")
        migration_env = self.read("migrations/env.py")

        self.assertIn(
            "COPY --chown=${APP_UID}:${APP_GID} bbw_agent ./bbw_agent",
            dockerfile,
        )
        self.assertIn("!bbw_agent/", dockerignore)
        self.assertIn("!bbw_agent/**", dockerignore)

        agent_secrets = compose.split("x-agent-worker-secrets:", 1)[1].split(
            "x-transcode-secrets:", 1
        )[0]
        self.assertIn("phone_hmac_key", agent_secrets)
        self.assertIn("session_hmac_key", agent_secrets)
        agent_worker = compose.split("  agent-worker:", 1)[1].split(
            "  transcode-worker:", 1
        )[0]
        self.assertIn("BBW_RQ_QUEUES: agent-control,agent", agent_worker)
        self.assertIn("secrets: *agent-worker-secrets", agent_worker)
        self.assertNotIn('BBW_PHONE_HMAC_KEY_FILE: ""', agent_worker)
        self.assertNotIn('BBW_SESSION_HMAC_KEY_FILE: ""', agent_worker)

        for source in (deployment, readme):
            self.assertIn("agent-worker", source)
        self.assertIn("AgentRunRepository(db).fail_stale_running(", jobs)
        self.assertIn("mark_stale_running_for_manual_review", jobs)
        self.assertIn('"stale_model_runs_failed"', jobs)
        self.assertIn('"stale_action_executions_review"', jobs)
        self.assertIn("def _compare_server_default(", migration_env)
        self.assertEqual(
            migration_env.count(
                "compare_server_default=_compare_server_default"
            ),
            2,
        )
        self.assertNotIn("compare_server_default=False", migration_env)

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
        private_policy = self.read("bbw_web/private_message_policy.py")
        repositories = self.read("bbw_prod/repositories.py")
        api = self.read("bbw_web/api.py")
        bff_server = self.read("bbw_web/bff_server.py")

        for marker in (
            'MESSAGE_POLICY_PROVIDER = "web-policy"',
            'MESSAGE_POLICY_MATCH_KIND = "match"',
            'MESSAGE_POLICY_CONVERSATION_KIND = "message_peer"',
            'SOCIAL_RELATIONSHIP_PROVIDER = "beibeiwu"',
            'SOCIAL_FRIEND_KIND = "friend"',
            'SOCIAL_BLACKLIST_KIND = "blacklist"',
            'SOCIAL_BLACKLISTED_BY_KIND = "blacklisted_by"',
            "SOCIAL_MESSAGE_BLOCK_KINDS = (",
            "def grant_message_peers(",
            "def replace_social_message_relationships(",
            "def trusted_message_block_snapshot(",
            "def set_social_message_relationship(",
            "def can_message_peer(",
            "def message_policy_snapshot(",
            "def message_policy_allowed_peers(",
            "def message_policy_match_peers(",
            "def message_policy_blocked_peers(",
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
        self.assertIn("private_message_permission_query(", persistence)
        self.assertIn("conversation_exists = (", private_policy)
        self.assertIn("or_(grant_exists, conversation_exists)", private_policy)
        self.assertIn(
            "Conversation.provider == LOCAL_RELATIONSHIP_PROVIDER",
            private_policy,
        )
        self.assertNotIn(
            'Conversation.provider.in_(("tim", "web-local"))',
            private_policy,
        )
        self.assertIn("def exists_for_peer(", repositories)
        self.assertIn("Conversation.peer_upstream_uid == peer_upstream_uid", repositories)
        self.assertIn('Conversation.kind == kind', repositories)
        self.assertIn("persistence.can_message_peer(", api)
        self.assertIn("persistence.message_policy_snapshot(identity)", api)
        self.assertIn("MATCH_DM_GRANT_PERSISTENCE_FAILED", api)
        self.assertIn("SOCIAL_DM_POLICY_PERSISTENCE_FAILED", api)
        self.assertIn("CONVERSATION_DM_GRANT_PERSISTENCE_FAILED", api)
        self.assertIn('"/api/social/blacklist-me": "blacklisted_by"', api)
        self.assertIn("message_block_snapshot_guard", api)
        self.assertIn("message_block_snapshot_loader", api)
        self.assertIn(":message-block-snapshot:", api)
        self.assertIn("persistence.redis.lock(", api)
        self.assertIn("_restore_durable_message_block_snapshots", bff_server)
        self.assertIn("with guard_factory(path):", bff_server)
        self.assertIn("persistence.remember_message_policy_response(", api)

    def test_provider_health_is_observed_without_affecting_service_readiness(self) -> None:
        persistence_source = self.read("bbw_web/persistence.py")
        health_body = persistence_source.split("def health", 1)[1].split(
            "def _limit_key", 1
        )[0]
        self.assertIn('"ok": bool(database_ok and redis_ok)', health_body)
        self.assertIn('"dependencies": self.dependency_status.public_snapshot()', health_body)
        admin_api = self.read("bbw_web/admin_api.py")
        self.assertIn('"dependencies": (', admin_api)
        self.assertIn(
            "request.app.state.persistence.dependency_status.public_snapshot()",
            admin_api,
        )
        admin_js = self.read("bbw_web/static/admin.js")
        self.assertIn("overview.dependencies", admin_js)
        self.assertIn('"外部依赖"', admin_js)
        self.assertIn('unavailable: "不可用"', admin_js)

        try:
            from bbw_web.dependency_health import DependencyStatusRegistry
            from bbw_web.persistence import RuntimePersistence
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        runtime.runtime_provider = types.SimpleNamespace(provider_id="beibeiwu")
        runtime.dependency_status = DependencyStatusRegistry()

        runtime._observe_provider_response(types.SimpleNamespace(status=-1))
        unavailable = runtime.dependency_status.get("beibeiwu", "api")
        self.assertIsNotNone(unavailable)
        self.assertEqual(unavailable.availability.value, "unavailable")
        self.assertEqual(unavailable.failure.kind.value, "connection")

        runtime._observe_provider_response(types.SimpleNamespace(status=429))
        degraded = runtime.dependency_status.get("beibeiwu", "api")
        self.assertEqual(degraded.availability.value, "degraded")
        self.assertEqual(degraded.failure.kind.value, "rate_limited")

        runtime._observe_provider_response(types.SimpleNamespace(status=200))
        available = runtime.dependency_status.get("beibeiwu", "api")
        self.assertEqual(available.availability.value, "available")
        self.assertIsNone(available.failure)

    def test_durable_session_restore_is_composed_by_the_selected_provider(self) -> None:
        try:
            from datetime import UTC, datetime

            from bbw_web.persistence import RuntimePersistence
            from bbw_web.providers import ProviderRuntime, ProviderSessionState
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        recovered_state = types.SimpleNamespace(
            user_id=user_id,
            external_account_id=account_id,
            created_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 7, 25, 10, 5, tzinfo=UTC),
        )
        user = types.SimpleNamespace(
            id=user_id,
            status="active",
            display_name="恢复用户",
            profile={"id": "42", "nickname": "恢复用户"},
            match_pool_online_list_enabled=True,
            nearby_custom_city_enabled=False,
        )
        account = types.SimpleNamespace(
            id=account_id,
            provider="beibeiwu",
            upstream_uid="42",
            device_data={
                "phonebrand": "Web",
                "device_id": "device-42",
                "user_role": "member",
                "vip": "2",
            },
        )

        class FakeUserSessionService:
            revoked: list[tuple[str, str]] = []

            def __init__(self, *_args: object) -> None:
                pass

            def recover(self, sid: str) -> object:
                self.sid = sid
                return recovered_state

            def revoke(self, sid: str, *, reason: str) -> None:
                self.revoked.append((sid, reason))

        class FakeExternalAccountRepository:
            def __init__(self, _db: object) -> None:
                pass

            def get_user_binding(self, *_args: object, **_kwargs: object):
                return user, account

        class RecordingProvider:
            provider_id = "beibeiwu"

            def __init__(self) -> None:
                self.states: list[ProviderSessionState] = []
                self.runtime: ProviderRuntime | None = None

            def create_runtime_from_state(
                self,
                state: ProviderSessionState,
            ) -> ProviderRuntime:
                self.states.append(state)
                app = types.SimpleNamespace(
                    session=types.SimpleNamespace(uid=state.uid),
                    client=types.SimpleNamespace(
                        response_hook=None,
                        reauth_callback=None,
                    ),
                )
                self.runtime = ProviderRuntime(
                    provider_id=self.provider_id,
                    app=app,
                    native=types.SimpleNamespace(app=app),
                )
                return self.runtime

        @contextmanager
        def fake_session_scope():
            yield object()

        provider = RecordingProvider()
        runtime = RuntimePersistence.__new__(RuntimePersistence)
        runtime.runtime_provider = provider
        runtime.redis = object()
        runtime.settings = object()
        runtime.session_hmac_key = b"session-key"

        with (
            patch("bbw_web.persistence.session_scope", fake_session_scope),
            patch(
                "bbw_web.persistence.UserSessionService",
                FakeUserSessionService,
            ),
            patch(
                "bbw_web.persistence.ExternalAccountRepository",
                FakeExternalAccountRepository,
            ),
            patch.object(
                runtime,
                "_decrypt_account",
                return_value=("19100000000", "", "token-42"),
            ),
        ):
            restored = runtime.restore_web_user("sid-42")

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertIs(restored.app, provider.runtime.app)
            self.assertIs(restored.native, provider.runtime.native)
            self.assertEqual(len(provider.states), 1)
            state = provider.states[0]
            self.assertEqual(state.uid, "42")
            self.assertEqual(state.token, "token-42")
            self.assertEqual(state.phone, "19100000000")
            self.assertEqual(state.device_data["device_id"], "device-42")
            self.assertNotIn("token-42", repr(state))
            self.assertTrue(callable(restored.app.client.response_hook))
            self.assertTrue(callable(restored.app.client.reauth_callback))

            account.provider = "other-provider"
            rejected = runtime.restore_web_user("sid-other")

        self.assertIsNone(rejected)
        self.assertEqual(
            FakeUserSessionService.revoked,
            [("sid-other", "provider_unavailable")],
        )
        self.assertEqual(len(provider.states), 1)

    def test_message_peer_grants_use_one_lookup_and_one_batch_upsert(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        unchanged_started_at = object()
        reactivated_started_at = object()
        existing_rows = [
            types.SimpleNamespace(
                subject_upstream_uid="9",
                status="active",
                ended_at=None,
                started_at=unchanged_started_at,
                extra_data={
                    "source_path": "/api/im/conversations",
                    "grant_reason": "upstream_conversation",
                    "server_owned": True,
                },
            ),
            types.SimpleNamespace(
                subject_upstream_uid="10",
                status="inactive",
                ended_at=object(),
                started_at=reactivated_started_at,
                extra_data={"legacy_evidence": True},
            ),
        ]

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalars(self, statement: object) -> list[object]:
                self.statements.append(statement)
                return existing_rows

        class FakeRelationshipRepository:
            def __init__(self) -> None:
                self.batches: list[list[dict[str, object]]] = []

            def upsert_many(self, rows: list[dict[str, object]]) -> None:
                self.batches.append(rows)

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        db = FakeDb()
        repository = FakeRelationshipRepository()

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.persistence.session_scope", fake_session_scope),
            patch(
                "bbw_web.persistence.RelationshipRepository",
                return_value=repository,
            ),
        ):
            peers = runtime.grant_message_peers(
                identity=identity,
                peers=["9", "10", "11", "9", "42", ""],
                kind="message_peer",
                evidence={
                    "source_path": "/api/im/conversations",
                    "grant_reason": "upstream_conversation",
                },
            )

        self.assertEqual(peers, ["9", "10", "11"])
        self.assertEqual(len(db.statements), 1)
        self.assertEqual(len(repository.batches), 1)
        self.assertEqual(
            [row["subject_upstream_uid"] for row in repository.batches[0]],
            ["10", "11"],
        )
        reactivated = repository.batches[0][0]
        self.assertIs(reactivated["started_at"], reactivated_started_at)
        self.assertEqual(reactivated["status"], "active")
        self.assertIsNone(reactivated["ended_at"])
        self.assertEqual(
            reactivated["extra_data"],
            {
                "legacy_evidence": True,
                "source_path": "/api/im/conversations",
                "grant_reason": "upstream_conversation",
                "server_owned": True,
            },
        )

    def test_social_message_snapshot_skips_unchanged_rows_and_batches_deactivation(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        kept_started_at = object()
        removed_started_at = object()
        existing_rows = [
            types.SimpleNamespace(
                subject_upstream_uid="9",
                status="active",
                ended_at=None,
                started_at=kept_started_at,
                extra_data={
                    "server_owned": True,
                    "message_policy_source": "/api/social/friends",
                },
            ),
            types.SimpleNamespace(
                subject_upstream_uid="10",
                status="active",
                ended_at=None,
                started_at=removed_started_at,
                extra_data={"legacy_evidence": True},
            ),
        ]

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalars(self, statement: object) -> list[object]:
                self.statements.append(statement)
                return existing_rows

        class FakeRelationshipRepository:
            def __init__(self) -> None:
                self.batches: list[list[dict[str, object]]] = []

            def upsert_many(self, rows: list[dict[str, object]]) -> None:
                self.batches.append(rows)

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        db = FakeDb()
        repository = FakeRelationshipRepository()

        @contextmanager
        def fake_session_scope():
            yield db

        with (
            patch("bbw_web.persistence.session_scope", fake_session_scope),
            patch(
                "bbw_web.persistence.RelationshipRepository",
                return_value=repository,
            ),
        ):
            peers = runtime.replace_social_message_relationships(
                identity=identity,
                peers=["9"],
                kind="friend",
                deactivate_missing=True,
                source_path="/api/social/friends",
            )

        self.assertEqual(peers, ["9"])
        self.assertEqual(len(db.statements), 1)
        self.assertEqual(len(repository.batches), 1)
        self.assertEqual(len(repository.batches[0]), 1)
        removed = repository.batches[0][0]
        self.assertEqual(removed["subject_upstream_uid"], "10")
        self.assertEqual(removed["status"], "inactive")
        self.assertIs(removed["started_at"], removed_started_at)
        self.assertIsNotNone(removed["ended_at"])
        self.assertEqual(
            removed["extra_data"],
            {
                "legacy_evidence": True,
                "server_owned": True,
                "message_policy_source": "/api/social/friends",
            },
        )

    def test_browser_archived_tim_conversation_is_not_an_authorization_source(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalar(self, statement: object) -> bool:
                self.statements.append(statement)
                return False

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
            self.assertFalse(runtime.can_message_peer(identity, "9"))

        self.assertEqual(len(db.statements), 1)
        statement = db.statements[0]
        sql = str(statement)
        self.assertIn("conversations", sql)
        self.assertIn("relationships", sql)
        self.assertIn("policy_friend_override", sql)
        self.assertIn("policy_peer_friend_override", sql)
        self.assertIn("policy_own_block_override", sql)
        self.assertIn("policy_peer_block_override", sql)
        params = {str(value) for value in statement.compile().params.values()}
        self.assertIn("web-local", params)
        self.assertIn("beibeiwu", params)
        self.assertIn("friend", params)
        self.assertNotIn("tim", params)

    def test_web_local_canonical_conversation_remains_an_authorization_source(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalar(self, statement: object) -> bool:
                self.statements.append(statement)
                return True

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
        params = {str(value) for value in db.statements[0].compile().params.values()}
        self.assertIn("web-local", params)
        self.assertNotIn("tim", params)

    def test_trusted_upstream_conversation_response_creates_message_peer_grant(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )

        with patch.object(
            runtime,
            "grant_message_peers",
            return_value=["9"],
        ) as grant_message_peers:
            result = runtime.remember_message_policy_response(
                identity=identity,
                method="GET",
                path="/api/im/conversations",
                request_data={},
                response_data={"ok": True, "items": [{"peer_id": "9"}]},
                status=200,
            )

        self.assertEqual(result, ["9"])
        grant_message_peers.assert_called_once_with(
            identity=identity,
            peers=["9"],
            kind="message_peer",
            evidence={
                "source_path": "/api/im/conversations",
                "grant_reason": "upstream_conversation",
            },
            complete_snapshot=False,
        )

    def test_complete_trusted_conversation_snapshot_writes_atomic_marker(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def scalars(self, _statement: object) -> list[object]:
                return []

        class FakeRelationshipRepository:
            def __init__(self) -> None:
                self.batches: list[list[dict[str, object]]] = []

            def upsert_many(self, rows: list[dict[str, object]]) -> None:
                self.batches.append(rows)

        class FakeSyncCursorRepository:
            def __init__(self) -> None:
                self.rows: list[dict[str, object]] = []

            def upsert(self, **values: object) -> object:
                self.rows.append(values)
                return object()

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )
        relationships = FakeRelationshipRepository()
        cursors = FakeSyncCursorRepository()

        @contextmanager
        def fake_session_scope():
            yield FakeDb()

        with (
            patch("bbw_web.persistence.session_scope", fake_session_scope),
            patch(
                "bbw_web.persistence.RelationshipRepository",
                return_value=relationships,
            ),
            patch(
                "bbw_web.persistence.SyncCursorRepository",
                return_value=cursors,
            ),
        ):
            peers = runtime.grant_message_peers(
                identity=identity,
                peers=["9", "10"],
                kind="message_peer",
                evidence={
                    "source_path": "/api/im/conversations",
                    "grant_reason": "upstream_conversation",
                },
                complete_snapshot=True,
            )

        self.assertEqual(peers, ["9", "10"])
        self.assertEqual(len(relationships.batches), 1)
        self.assertEqual(
            [row["subject_upstream_uid"] for row in relationships.batches[0]],
            ["9", "10"],
        )
        for row in relationships.batches[0]:
            metadata = row["extra_data"]
            self.assertTrue(metadata["server_owned"])
            self.assertTrue(metadata["upstream_conversation_snapshot"])
            self.assertEqual(
                metadata["upstream_conversation_source_path"],
                "/api/im/conversations",
            )
        self.assertEqual(len(cursors.rows), 1)
        marker = cursors.rows[0]
        self.assertEqual(marker["source"], "beibeiwu")
        self.assertEqual(marker["stream"], "message-peer-snapshot")
        payload = json.loads(str(marker["cursor"]))
        self.assertEqual(payload["peer_count"], 2)
        self.assertEqual(payload["external_account_id"], str(identity.external_account_id))
        self.assertEqual(payload["upstream_uid"], "42")
        self.assertEqual(len(payload["peer_digest"]), 64)

    def test_active_friend_relationship_authorizes_private_message_peer(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalar(self, statement: object) -> bool:
                self.statements.append(statement)
                return True

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

    def test_both_blacklist_directions_override_global_private_message_permission(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalar(self, statement: object) -> bool:
                self.statements.append(statement)
                return False

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
            match_pool_online_list_enabled=True,
        )
        db = FakeDb()

        @contextmanager
        def fake_session_scope():
            yield db

        with patch("bbw_web.persistence.session_scope", fake_session_scope):
            self.assertFalse(runtime.can_message_peer(identity, "9"))

        self.assertEqual(len(db.statements), 1)
        params = {str(value) for value in db.statements[0].compile().params.values()}
        param_text = " ".join(params)
        self.assertIn("blacklist", param_text)
        self.assertIn("blacklisted_by", param_text)

    def test_recipient_legacy_block_snapshots_deny_proactive_private_message(self) -> None:
        try:
            from sqlalchemy.dialects import postgresql

            from bbw_web.private_message_policy import (
                private_message_permission_query,
            )
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        sender_user_id = uuid.uuid4()
        recipient_user_id = uuid.uuid4()
        compiled = private_message_permission_query(
            sender_user_id=sender_user_id,
            sender_upstream_uid="42",
            recipient_user_id=recipient_user_id,
            recipient_upstream_uid="9",
            proactive_private_message=True,
        ).compile(dialect=postgresql.dialect())
        sql = str(compiled).replace("\n", " ")

        # 拉黑判定必须同时覆盖六个方向:双方 local override 各一条,
        # 发送方名下 legacy blacklist/blacklisted_by 各一条,以及收件人
        # 名下 legacy blacklist/blacklisted_by 各一条。少任何一条都视为回退。
        self.assertEqual(sql.count("relationships.owner_user_id ="), 6)
        # 两条 local override 豁免子查询各被两个 legacy 分支引用一次。
        self.assertEqual(
            sql.count("policy_peer_block_override.owner_user_id ="), 2
        )
        self.assertEqual(
            sql.count("policy_own_block_override.owner_user_id ="), 2
        )

        # 把绑定参数代回编译文本,做结构性断言而非整段快照。
        resolved = sql
        for key in sorted(compiled.params, key=len, reverse=True):
            resolved = resolved.replace(
                f"%({key})s", repr(str(compiled.params[key]))
            )
        branches = resolved.split(" OR ")
        self.assertEqual(len(branches), 6)

        def branch_count(*, owner: uuid.UUID, kind: str, guard: str) -> int:
            return sum(
                1
                for branch in branches
                if f"relationships.owner_user_id = '{owner}'" in branch
                and "relationships.provider = 'beibeiwu'" in branch
                and "relationships.subject_upstream_uid = '42'" in branch
                and f"relationships.kind = '{kind}'" in branch
                and guard in branch
            )

        # 收件人名下 legacy blacklist(收件人拉黑发送人)分支,
        # 由 peer local override 豁免。
        self.assertEqual(
            branch_count(
                owner=recipient_user_id,
                kind="blacklist",
                guard="policy_peer_block_override",
            ),
            1,
        )
        # 收件人名下 legacy blacklisted_by(发送人拉黑收件人的对端快照)
        # 分支,由发送方 own local override 豁免。
        self.assertEqual(
            branch_count(
                owner=recipient_user_id,
                kind="blacklisted_by",
                guard="policy_own_block_override",
            ),
            1,
        )

        # 收件人 user_id 需绑定在:local 分支、peer 豁免子查询以及
        # 两条新 legacy 分支上;legacy provider 需绑定在四条 legacy 分支上。
        recipient_bind_count = sum(
            1
            for value in compiled.params.values()
            if value == recipient_user_id
        )
        self.assertGreaterEqual(recipient_bind_count, 4)
        legacy_bind_count = sum(
            1 for value in compiled.params.values() if value == "beibeiwu"
        )
        self.assertGreaterEqual(legacy_bind_count, 4)

    def test_blacklisted_by_snapshot_is_persisted_as_a_message_block(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )

        with patch.object(
            runtime,
            "replace_social_message_relationships",
            return_value=["9"],
        ) as replace_snapshot:
            result = runtime.remember_message_policy_response(
                identity=identity,
                method="GET",
                path="/api/social/blacklist-me",
                request_data={},
                response_data={
                    "ok": True,
                    "items": [{"uid": "9"}],
                    "list": [{"uid": "9"}],
                },
                status=200,
            )

        self.assertEqual(result, ["9"])
        replace_snapshot.assert_called_once_with(
            identity=identity,
            peers=["9"],
            kind="blacklisted_by",
            deactivate_missing=True,
            source_path="/api/social/blacklist-me",
        )

    def test_friend_summary_response_does_not_revoke_full_friend_snapshot(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )

        with patch.object(
            runtime,
            "replace_social_message_relationships",
            return_value=[],
        ) as replace_snapshot:
            result = runtime.remember_message_policy_response(
                identity=identity,
                method="GET",
                path="/api/social/friends",
                request_data={},
                response_data={"ok": True, "count": 3},
                status=200,
            )
            self.assertEqual(result, [])
            replace_snapshot.assert_not_called()

            runtime.remember_message_policy_response(
                identity=identity,
                method="GET",
                path="/api/social/friends",
                request_data={},
                response_data={"ok": True, "items": [], "list": [], "count": 0},
                status=200,
            )
            replace_snapshot.assert_called_once()

    def test_complete_block_snapshots_write_sync_cursor_markers_even_when_empty(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def scalars(self, _statement: object) -> list[object]:
                return []

        class FakeRelationshipRepository:
            def __init__(self) -> None:
                self.batches: list[list[dict[str, object]]] = []

            def upsert_many(self, rows: list[dict[str, object]]) -> None:
                self.batches.append(rows)

        class FakeSyncCursorRepository:
            def __init__(self) -> None:
                self.rows: list[dict[str, object]] = []

            def upsert(self, **values: object) -> object:
                self.rows.append(values)
                return object()

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )

        for kind, path in (
            ("blacklist", "/api/social/blacklist"),
            ("blacklisted_by", "/api/social/blacklist-me"),
        ):
            with self.subTest(kind=kind):
                relationships = FakeRelationshipRepository()
                cursors = FakeSyncCursorRepository()

                @contextmanager
                def fake_session_scope():
                    yield FakeDb()

                with (
                    patch("bbw_web.persistence.session_scope", fake_session_scope),
                    patch(
                        "bbw_web.persistence.RelationshipRepository",
                        return_value=relationships,
                    ),
                    patch(
                        "bbw_web.persistence.SyncCursorRepository",
                        return_value=cursors,
                    ),
                ):
                    result = runtime.replace_social_message_relationships(
                        identity=identity,
                        peers=[],
                        kind=kind,
                        deactivate_missing=True,
                        source_path=path,
                    )

                self.assertEqual(result, [])
                self.assertEqual(relationships.batches, [[]])
                self.assertEqual(len(cursors.rows), 1)
                marker = cursors.rows[0]
                self.assertEqual(marker["source"], "beibeiwu")
                self.assertEqual(
                    marker["stream"],
                    f"message-block-snapshot:{kind}",
                )
                self.assertIsNotNone(marker["last_succeeded_at"])
                self.assertIsNone(marker["last_error"])
                self.assertEqual(
                    json.loads(str(marker["cursor"])),
                    {
                        "complete": True,
                        "external_account_id": str(identity.external_account_id),
                        "kind": kind,
                        "schema": 1,
                        "source_path": path,
                        "upstream_uid": "42",
                    },
                )

        partial_relationships = FakeRelationshipRepository()
        partial_cursors = FakeSyncCursorRepository()

        @contextmanager
        def partial_session_scope():
            yield FakeDb()

        with (
            patch("bbw_web.persistence.session_scope", partial_session_scope),
            patch(
                "bbw_web.persistence.RelationshipRepository",
                return_value=partial_relationships,
            ),
            patch(
                "bbw_web.persistence.SyncCursorRepository",
                return_value=partial_cursors,
            ),
        ):
            runtime.replace_social_message_relationships(
                identity=identity,
                peers=["9"],
                kind="blacklist",
                deactivate_missing=False,
                source_path="/api/social/blacklist",
            )

        self.assertEqual(len(partial_relationships.batches), 1)
        self.assertEqual(partial_cursors.rows, [])

    def test_trusted_block_snapshot_requires_both_completion_markers(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        runtime = RuntimePersistence.__new__(RuntimePersistence)
        identity = UserIdentity(
            user_id=uuid.uuid4(),
            external_account_id=uuid.uuid4(),
            upstream_uid="42",
        )

        def cursor(kind: str, path: str) -> object:
            return types.SimpleNamespace(
                stream=f"message-block-snapshot:{kind}",
                cursor=json.dumps(
                    {
                        "complete": True,
                        "external_account_id": str(identity.external_account_id),
                        "kind": kind,
                        "schema": 1,
                        "source_path": path,
                        "upstream_uid": "42",
                    }
                ),
                watermark_at=object(),
                last_succeeded_at=object(),
                last_error=None,
            )

        complete_cursors = [
            cursor("blacklist", "/api/social/blacklist"),
            cursor("blacklisted_by", "/api/social/blacklist-me"),
        ]

        class FakeDb:
            def __init__(
                self,
                cursors: list[object],
                relationships: list[object],
            ) -> None:
                self.cursors = cursors
                self.relationships = relationships
                self.calls = 0

            def scalars(self, statement: object) -> list[object]:
                self.calls += 1
                if "sync_cursors" in str(statement):
                    return self.cursors
                return self.relationships

        def load_with(db: FakeDb):
            @contextmanager
            def fake_session_scope():
                yield db

            with patch("bbw_web.persistence.session_scope", fake_session_scope):
                return runtime.trusted_message_block_snapshot(identity)

        # Both success markers make an empty/empty snapshot authoritative.
        empty_db = FakeDb(complete_cursors, [])
        self.assertEqual(
            load_with(empty_db),
            {"blacklist": [], "blacklisted_by": []},
        )
        self.assertEqual(empty_db.calls, 2)

        # Old relationship data with only one marker remains untrusted.
        incomplete_db = FakeDb(complete_cursors[:1], [])
        self.assertIsNone(load_with(incomplete_db))
        self.assertEqual(incomplete_db.calls, 1)

        wrong_account_cursors = list(complete_cursors)
        wrong_account_cursors[0] = types.SimpleNamespace(
            **{
                **vars(complete_cursors[0]),
                "cursor": json.dumps(
                    {
                        "complete": True,
                        "external_account_id": str(uuid.uuid4()),
                        "kind": "blacklist",
                        "schema": 1,
                        "source_path": "/api/social/blacklist",
                        "upstream_uid": "42",
                    }
                ),
            }
        )
        self.assertIsNone(load_with(FakeDb(wrong_account_cursors, [])))

        trusted_rows = [
            types.SimpleNamespace(
                kind="blacklist",
                subject_upstream_uid="9",
                extra_data={
                    "server_owned": True,
                    "message_policy_source": "/api/social/blacklist",
                },
            ),
            types.SimpleNamespace(
                kind="blacklisted_by",
                subject_upstream_uid="10",
                extra_data={
                    "server_owned": True,
                    "message_policy_source": "/api/social/blacklist-me",
                },
            ),
        ]
        self.assertEqual(
            load_with(FakeDb(complete_cursors, trusted_rows)),
            {"blacklist": ["9"], "blacklisted_by": ["10"]},
        )

        untrusted_rows = [
            types.SimpleNamespace(
                kind="blacklist",
                subject_upstream_uid="9",
                extra_data={"source_path": "/api/social/blacklist"},
            )
        ]
        self.assertIsNone(load_with(FakeDb(complete_cursors, untrusted_rows)))

    def test_message_policy_snapshot_loads_all_lists_in_one_database_round_trip(self) -> None:
        try:
            from bbw_web.persistence import RuntimePersistence, UserIdentity
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def execute(self, statement: object) -> list[tuple[str, str]]:
                self.statements.append(statement)
                return [
                    ("allowed_peers", "9"),
                    ("allowed_peers", "10"),
                    ("allowed_peers", "42"),
                    ("match_peers", "9"),
                    ("match_peers", "10"),
                    ("blocked_peers", "10"),
                    ("blocked_peers", ""),
                ]

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
            snapshot = runtime.message_policy_snapshot(identity)

        self.assertEqual(
            snapshot,
            {
                "allowed_peers": ["9"],
                "match_peers": ["9"],
                "blocked_peers": ["10"],
            },
        )
        self.assertEqual(len(db.statements), 1)
        params = {str(value) for value in db.statements[0].compile().params.values()}
        self.assertIn("beibeiwu", params)
        self.assertIn("friend", params)
        self.assertIn("web-policy", params)
        sql = str(db.statements[0])
        self.assertIn("conversations.peer_upstream_uid", sql)
        self.assertIn("snapshot_friend_override", sql)
        self.assertIn("snapshot_reverse_friend_account", sql)
        self.assertIn("snapshot_reverse_own_friend_override", sql)
        self.assertIn("snapshot_outgoing_block_override", sql)
        self.assertIn("snapshot_incoming_block_override", sql)
        self.assertIn("message_policy_blocked_candidates", sql)
        self.assertIn("web-local", params)
        self.assertNotIn("tim", params)
        self.assertIn("direct", params)

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

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalars(self, statement: object) -> list[object]:
                self.statements.append(statement)
                return [conversation_id] if len(self.statements) == 1 else [newer, older]

        fake_db = FakeDb()

        @contextmanager
        def fake_session_scope():
            yield fake_db

        with patch("bbw_web.archive_api.session_scope", fake_session_scope):
            payload = archive_api.archived_messages(
                request,
                peer="9",
                limit=200,
                before=None,
            )

        self.assertEqual(len(fake_db.statements), 2)
        self.assertEqual(
            [item["id"] for item in payload["items"]],
            ["older-message", "newer-message"],
        )
        self.assertEqual(payload["items"][0]["flow"], "out")
        self.assertEqual(payload["items"][0]["source"], "archive")
        self.assertEqual(payload["items"][1]["text"], "较新的消息")
        self.assertTrue(payload["items"][1]["is_peer_read"])
        self.assertFalse(payload["has_more"])

    def test_archived_message_cursor_does_not_skip_deduplicated_rows(self) -> None:
        from datetime import UTC, datetime, timedelta

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
        base_time = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)

        def projected_message(
            *, canonical_id: str, occurred_at: datetime, provider: str
        ) -> object:
            return types.SimpleNamespace(
                id=uuid.uuid4(),
                provider=provider,
                upstream_message_id=(
                    canonical_id if provider == "web-local" else f"tim-{canonical_id}"
                ),
                extra_data={"canonical_message_id": canonical_id},
                body=canonical_id,
                message_type="text",
                sender_upstream_uid="9",
                recipient_upstream_uid="42",
                direction="incoming",
                status="received",
                occurred_at=occurred_at,
                created_at=occurred_at,
            )

        raw_rows: list[object] = []
        for offset, canonical_id in reversed(
            list(enumerate(("oldest", "middle", "newest")))
        ):
            occurred_at = base_time + timedelta(minutes=offset)
            raw_rows.extend(
                (
                    projected_message(
                        canonical_id=canonical_id,
                        occurred_at=occurred_at,
                        provider="web-local",
                    ),
                    projected_message(
                        canonical_id=canonical_id,
                        occurred_at=occurred_at,
                        provider="tim",
                    ),
                )
            )

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

        class FakeDb:
            def __init__(self) -> None:
                self.statements: list[object] = []

            def scalars(self, statement: object) -> list[object]:
                self.statements.append(statement)
                return [conversation_id] if len(self.statements) == 1 else raw_rows

        @contextmanager
        def fake_session_scope():
            yield FakeDb()

        with patch("bbw_web.archive_api.session_scope", fake_session_scope):
            payload = archive_api.archived_messages(
                request,
                peer="9",
                limit=2,
                before=None,
            )

        self.assertEqual(
            [item["canonical_message_id"] for item in payload["items"]],
            ["middle", "newest"],
        )
        self.assertTrue(payload["has_more"])
        self.assertEqual(
            payload["next_before"],
            (base_time + timedelta(minutes=1)).isoformat(),
        )
        cursor_occurred_at, cursor_created_at, cursor_id = (
            archive_api._decode_archive_message_cursor(payload["next_cursor"])
        )
        self.assertEqual(cursor_occurred_at, base_time + timedelta(minutes=1))
        self.assertEqual(cursor_created_at, base_time + timedelta(minutes=1))
        middle_projection_ids = {
            row.id
            for row in raw_rows
            if row.extra_data["canonical_message_id"] == "middle"
        }
        self.assertIn(cursor_id, middle_projection_ids)
        self.assertTrue(
            all("_archive_cursor" not in item for item in payload["items"])
        )

    def test_archive_cursor_and_read_merge_are_monotonic(self) -> None:
        from datetime import UTC, datetime

        try:
            from bbw_web import archive_api
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        occurred_at = datetime(2026, 7, 25, 8, 30, tzinfo=UTC)
        created_at = datetime(2026, 7, 25, 8, 31, tzinfo=UTC)
        message_id = uuid.uuid4()
        cursor = archive_api._encode_archive_message_cursor(
            types.SimpleNamespace(
                id=message_id,
                occurred_at=occurred_at,
                created_at=created_at,
            )
        )
        self.assertEqual(
            archive_api._decode_archive_message_cursor(cursor),
            (occurred_at, created_at, message_id),
        )

        canonical = {
            "id": str(message_id),
            "canonical_message_id": str(message_id),
            "provider": "web-local",
            "flow": "out",
            "from": "42",
            "to": "9",
            "is_peer_read": True,
            "read_at": occurred_at.isoformat(),
        }
        compatibility = {
            **canonical,
            "id": "tim-message-key",
            "provider": "tim",
            "message_random": "778899",
            "is_peer_read": False,
            "read_at": "",
        }
        merged = archive_api._merge_archived_message_items(
            canonical,
            compatibility,
        )
        self.assertTrue(merged["is_peer_read"])
        self.assertEqual(merged["read_at"], occurred_at.isoformat())

        source = self.read("bbw_web/archive_api.py")
        self.assertIn("Message.created_at < cursor_created_at", source)
        self.assertIn("Message.id < cursor_id", source)
        self.assertIn('"next_cursor": next_cursor', source)

    def test_archive_merge_uses_canonical_revoke_state(self) -> None:
        try:
            from bbw_web import archive_api
        except ImportError as exc:
            self.skipTest(f"production dependencies are not installed: {exc}")

        canonical = {
            "id": "canonical-message",
            "canonical_message_id": "canonical-message",
            "provider": "web-local",
            "flow": "out",
            "from": "42",
            "to": "9",
            "status": "sent",
            "revoked": False,
        }
        compatibility = {
            **canonical,
            "id": "tim-message-key",
            "provider": "tim",
            "message_random": "778899",
            "status": "revoked",
            "revoked": True,
        }

        active = archive_api._merge_archived_message_items(
            canonical,
            compatibility,
        )
        self.assertFalse(active["revoked"])
        self.assertEqual(active["status"], "sent")

        revoked = archive_api._merge_archived_message_items(
            {
                **canonical,
                "status": "revoked",
                "revoked": True,
                "text": "",
                "body": "",
                "recalled_text": "仅发送者可重新编辑",
            },
            {
                **compatibility,
                "status": "sent",
                "revoked": False,
                "text": "不能从 TIM 副本恢复的原文",
                "body": "不能从 TIM 副本恢复的原文",
            },
        )
        self.assertTrue(revoked["revoked"])
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["text"], "")
        self.assertEqual(revoked["body"], "")
        self.assertEqual(revoked["recalled_text"], "仅发送者可重新编辑")

        recipient = archive_api._merge_archived_message_items(
            {
                **canonical,
                "flow": "in",
                "status": "revoked",
                "revoked": True,
                "text": "",
                "body": "",
                "recalled_text": "",
            },
            {
                **compatibility,
                "flow": "in",
                "status": "sent",
                "revoked": False,
                "text": "接收方不能再得到的原文",
                "body": "接收方不能再得到的原文",
            },
        )
        self.assertEqual(recipient["text"], "")
        self.assertEqual(recipient["body"], "")
        self.assertEqual(recipient["recalled_text"], "")

    def test_archived_conversation_prefers_current_local_profile_over_snapshot(self) -> None:
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
        conversation = types.SimpleNamespace(
            id=conversation_id,
            provider="web-local",
            upstream_conversation_id="C2C9",
            peer_upstream_uid="9",
            title="游客",
            extra_data={
                "avatar": "https://example.invalid/old.jpg",
                "user": {
                    "nickname": "游客",
                    "avatar": "https://example.invalid/old.jpg",
                },
            },
            last_message_at=datetime(2026, 7, 25, tzinfo=UTC),
            unread_count=4,
            unread_observed_at=datetime(2026, 7, 24, tzinfo=UTC),
        )
        local_revoked_message = types.SimpleNamespace(
            body=None,
            message_type="text",
            status="revoked",
            occurred_at=datetime(2026, 7, 25, tzinfo=UTC),
            extra_data={"revoked": True},
        )
        newer_tim_projection = types.SimpleNamespace(
            id=uuid.uuid4(),
            provider="tim",
            upstream_conversation_id="C2C9",
            peer_upstream_uid="9",
            title="旧快照昵称",
            extra_data={"last_message": "TIM 兼容预览"},
            last_message_at=datetime(2026, 7, 25, tzinfo=UTC),
            unread_count=99,
            unread_observed_at=datetime(2026, 7, 25, tzinfo=UTC),
        )
        unresolved_conversation = types.SimpleNamespace(
            id=uuid.uuid4(),
            provider="tim",
            upstream_conversation_id="C2C10",
            peer_upstream_uid="10",
            title="游客",
            extra_data={"user": {"nickname": "游客"}},
            last_message_at=None,
            unread_count=0,
            unread_observed_at=None,
        )
        partial_name_conversation = types.SimpleNamespace(
            id=uuid.uuid4(),
            provider="tim",
            upstream_conversation_id="C2C11",
            peer_upstream_uid="11",
            title="旧昵称",
            extra_data={"avatar": "https://example.invalid/old-11.jpg"},
            last_message_at=None,
            unread_count=0,
            unread_observed_at=None,
        )
        partial_avatar_conversation = types.SimpleNamespace(
            id=uuid.uuid4(),
            provider="tim",
            upstream_conversation_id="C2C12",
            peer_upstream_uid="12",
            title="旧昵称",
            extra_data={"avatar": "https://example.invalid/old-12.jpg"},
            last_message_at=None,
            unread_count=0,
            unread_observed_at=None,
        )
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
            list_for_owner=lambda user_id, limit: [
                conversation,
                newer_tim_projection,
                unresolved_conversation,
                partial_name_conversation,
                partial_avatar_conversation,
            ]
        )
        message_repo = types.SimpleNamespace(
            latest_for_conversations=lambda user_id, conversation_ids: {
                conversation_id: local_revoked_message
            }
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
            patch(
                "bbw_web.archive_api._local_public_profile_map",
                return_value={
                    "9": {
                        "id": "9",
                        "nickname": "真实昵称",
                        "avatar": "https://example.invalid/current.jpg",
                        "portrait": "https://example.invalid/current.jpg",
                    },
                    "11": {"id": "11", "nickname": "新昵称"},
                    "12": {
                        "id": "12",
                        "avatar": "https://example.invalid/current-12.jpg",
                        "portrait": "https://example.invalid/current-12.jpg",
                    },
                },
            ),
        ):
            payload = archive_api.archived_conversations(request, limit=100)

        item = payload["items"][0]
        self.assertEqual(item["nickname"], "真实昵称")
        self.assertEqual(item["avatar"], "https://example.invalid/current.jpg")
        self.assertEqual(item["user"]["nickname"], "真实昵称")
        self.assertTrue(item["profile_resolved"])
        self.assertEqual(item["provider"], "web-local")
        self.assertEqual(item["unread_count"], 4)
        self.assertTrue(item["unread_authoritative"])
        self.assertEqual(item["last_message"], "消息已撤回")
        self.assertEqual(item["content"], "消息已撤回")
        unresolved_item = payload["items"][1]
        self.assertEqual(unresolved_item["nickname"], "10")
        self.assertFalse(unresolved_item["profile_resolved"])
        self.assertFalse(unresolved_item["unread_authoritative"])
        partial_name_item = payload["items"][2]
        self.assertEqual(partial_name_item["nickname"], "新昵称")
        self.assertEqual(partial_name_item["avatar"], "https://example.invalid/old-11.jpg")
        self.assertFalse(partial_name_item["profile_resolved"])
        partial_avatar_item = payload["items"][3]
        self.assertEqual(partial_avatar_item["nickname"], "旧昵称")
        self.assertEqual(
            partial_avatar_item["avatar"],
            "https://example.invalid/current-12.jpg",
        )
        self.assertFalse(partial_avatar_item["profile_resolved"])

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
        self.assertIn("authGeneration: 0", js)
        self.assertIn("function invalidateUserDetailRequests", js)
        self.assertIn("function isCurrentUserDetailRequest", js)
        load_admin_me = function_block("loadAdminMe")
        restore_visible_page = function_block("restoreVisiblePage")
        self.assertIn("expectedAuthGeneration", load_admin_me)
        self.assertIn("expectedAuthGeneration !== ADMIN_STATE.authGeneration", load_admin_me)
        self.assertIn("!ADMIN_STATE.authenticated", restore_visible_page)
        self.assertIn("expectedAuthGeneration !== ADMIN_STATE.authGeneration", restore_visible_page)
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
        self.assertIn(
            f'/static/app.js?v={hashlib.sha256((ROOT / "bbw_web" / "static" / "app.js").read_bytes()).hexdigest()[:16]}',
            html,
        )
        self.assertIn('<script defer src="/static/app.js?v=', html)

        show_login = js.split("function showLogin", 1)[1].split("function applyUser", 1)[0]
        self.assertIn('bootScreen.classList.add("hide")', show_login)
        self.assertIn('bootScreen.setAttribute("aria-busy", "false")', show_login)
        self.assertIn('$("screen-login").classList.toggle("hide", !show)', show_login)
        self.assertIn('$("screen-app").classList.toggle("hide", show)', show_login)

        restore = js.split("async function restoreSessionAtBoot()", 1)[1].split(
            "async function recoverSessionAfterBoot", 1
        )[0]
        classifier = js.split("function classifyBootSessionAttempt", 1)[1].split(
            "function completeRestoredSession", 1
        )[0]
        recovery = js.split("async function recoverSessionAfterBoot", 1)[1].split(
            "(async function boot()", 1
        )[0]
        boot = js.split("(async function boot()", 1)[1].split("})();", 1)[0]
        self.assertIn('api("/api/me"', restore)
        self.assertIn("BOOT_SESSION_TIMEOUT_MS", restore)
        self.assertIn("classifyBootSessionAttempt(result, error)", restore)
        self.assertIn("status === 429", classifier)
        self.assertIn('["offline", "network", "timeout"].includes(error.kind)', classifier)
        self.assertIn("transient: true", classifier)
        self.assertIn("while (token === bootSessionRecoveryToken", recovery)
        self.assertIn("waitForBootSessionRecovery", recovery)
        self.assertIn("await restoreSessionAtBoot()", boot)
        self.assertIn("scheduleDeferredFeatureLoad()", boot)
        self.assertGreater(boot.index("scheduleDeferredFeatureLoad()"), boot.index("completeRestoredSession(data)"))
        self.assertNotIn("void Promise.resolve(loadFeatures())", boot)
        self.assertNotIn("await loadFeatures();", boot)
        self.assertIn("completeRestoredSession(data)", boot)
        restored = js.split("function completeRestoredSession", 1)[1].split(
            "async function restoreSessionAtBoot", 1
        )[0]
        self.assertIn("showLogin(false)", restored)
        self.assertIn("showLogin(true, !recovery)", boot)
        self.assertIn('id="session-recovery-retry"', html)
        self.assertIn("class ApiRequestError extends Error", js)
        self.assertIn("responseRetryAfterMs(response)", js)
        nearby = js.split("async function pageNearby", 1)[1].split(
            "async function pageMessages", 1
        )[0]
        self.assertNotIn('api("/api/home"', nearby)
        restored = js.split("function completeRestoredSession", 1)[1].split(
            "async function restoreSessionAtBoot", 1
        )[0]
        self.assertIn("applyFeatureEnvelope(data)", restored)
        feature_envelope = js.split("function applyFeatureEnvelope", 1)[1].split(
            "async function loadFeatures", 1
        )[0]
        self.assertIn("data?.lab_enabled === true", feature_envelope)
        self.assertLess(
            restored.index("applyFeatureEnvelope(data)"),
            restored.index("const desired = hashRoute()"),
        )

    def test_request_observability_health_and_blue_green_contracts(self) -> None:
        api = self.read("bbw_web/api.py")
        caddy = self.read("Caddyfile")
        compose = self.read("compose.yaml")
        blue_green = self.read("compose.blue-green.yaml")
        healthcheck = self.read("docker/healthcheck.py")
        config = self.read("bbw_prod/config.py")
        init_secrets = self.read("docker/init-secrets.sh")
        deployment = self.read("docs/14_PRODUCTION_DEPLOYMENT.md")
        store = self.read("bbw_web/store.py")

        for marker in (
            '@app.get("/livez"',
            '@app.get("/readyz"',
            '@app.post("/internal/drain"',
            '@app.post("/internal/resume"',
            'response.headers["X-Request-ID"] = request_id',
            '"event": "http_request"',
            'f"session-restore:{limiter_identity}"',
            '"SESSION_RESTORE_RATE_LIMIT"',
        ):
            self.assertIn(marker, api)
        self.assertIn("def _require_deployment_control", api)
        self.assertIn("hmac.compare_digest(supplied, expected)", api)
        self.assertNotIn("def _require_loopback", api)
        control_guard = api.split("def _require_deployment_control", 1)[1].split(
            '@app.post("/internal/drain"', 1
        )[0]
        self.assertNotIn("request.client", control_guard)
        self.assertNotIn("X-Forwarded-For", control_guard)
        self.assertIn('"/api/auth/sms-send"', api)
        drain = api.split("def begin_drain", 1)[1].split(
            '@app.post("/internal/resume"', 1
        )[0]
        self.assertLess(
            drain.index("request.app.state.accepting_logins = False"),
            drain.index("legacy.STORE.stats()"),
        )
        self.assertIn("login_starts_in_flight", drain)
        self.assertIn('"pending_logins": sum(', store)
        self.assertIn("{$APP_STANDBY_UPSTREAM:app-green:8000}", caddy)
        self.assertIn("lb_policy first", caddy)
        self.assertIn("health_uri /readyz", caddy)
        self.assertIn("Active health checks are a failure fallback", caddy)
        self.assertIn("APP_STANDBY_UPSTREAM", compose)
        self.assertIn("BBW_DEPLOYMENT_CONTROL_TOKEN_FILE", compose)
        self.assertIn("deployment_control_token:", compose)
        self.assertIn("--timeout-graceful-shutdown", compose)
        self.assertIn("app-green:", blue_green)
        self.assertIn("service: app", blue_green)
        self.assertIn('request("/readyz")', healthcheck)
        self.assertIn('{"drain", "resume"}', healthcheck)
        self.assertIn('headers={"Authorization": f"Bearer {token}"}', healthcheck)
        self.assertIn("def load_deployment_control_token", config)
        self.assertIn("write_if_missing deployment_control_token", init_secrets)
        first_reload = deployment.index(
            "APP_UPSTREAM=app-green:8000 -e APP_STANDBY_UPSTREAM=app:8000 caddy caddy reload"
        )
        first_drain = deployment.index(
            "docker compose exec app python /app/docker/healthcheck.py drain"
        )
        self.assertLess(first_reload, first_drain)

    def test_session_probe_does_not_refresh_profile_upstream(self) -> None:
        source = self.read("bbw_web/bff_server.py")
        route = source.split('if path == "/api/me":', 1)[1].split(
            'if path in ("/api/app/home", "/api/home"):', 1
        )[0]

        self.assertNotIn("_enrich_session_profile", route)
        self.assertIn("user_dto = N.session_user_dto(u.app.whoami())", route)

    def test_raw_response_archival_is_bounded_async_and_batched(self) -> None:
        source = self.read("bbw_web/persistence.py")
        capture = source.split("def capture_upstream_response", 1)[1].split(
            "def capture_product_response", 1
        )[0]
        worker = source.split("def _raw_response_worker", 1)[1].split(
            "def health", 1
        )[0]

        self.assertIn("RAW_RESPONSE_QUEUE_MAX = 64", source)
        self.assertIn("pending.put_nowait(item)", capture)
        self.assertIn("except queue_module.Full", capture)
        self.assertNotIn("with session_scope()", capture)
        self.assertIn("RAW_RESPONSE_BATCH_SIZE", worker)
        self.assertIn("self._store_raw_response_batch(batch)", worker)
        self.assertIn("with session_scope() as db", worker)

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
