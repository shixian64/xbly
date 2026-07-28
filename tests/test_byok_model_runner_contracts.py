from __future__ import annotations

import json
import socket
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import httpcore
    import httpx

    from bbw_agent.model_gateway import (
        ModelGatewayError,
        OpenAICompatibleGateway,
        _PinnedNetworkBackend,
        resolve_model_base_url,
        validate_api_key,
        validate_model_base_url,
    )
    from bbw_agent.api import _runtime_still_enabled
    from bbw_agent.services import (
        AgentServiceError,
        RuntimeConfiguration,
        api_key_context,
        require_visible_access,
        settings_public,
    )
    from bbw_prod.crypto import CredentialCipher, EncryptionError, redact_raw_payload
    from bbw_prod.models import (
        AiAgentRun,
        AiAgentSetting,
        AiModelConnection,
        AiStyleProfile,
    )
    from bbw_web.persistence import _redact_request, _sanitize_profile
except ImportError as exc:  # pragma: no cover - dependency-less contract runner
    DEPENDENCY_IMPORT_ERROR: ImportError | None = exc
else:
    DEPENDENCY_IMPORT_ERROR = None


def _settings(
    *,
    environment: str = "production",
    allowed_hosts: tuple[str, ...] = ("api.example.com",),
) -> SimpleNamespace:
    return SimpleNamespace(
        environment=environment,
        ai_byok_allowed_hosts=allowed_hosts,
        ai_byok_connect_timeout_seconds=2,
        ai_byok_read_timeout_seconds=5,
        ai_byok_max_response_bytes=64 * 1024,
    )


def _resolver(*addresses: str):
    def resolve(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        del host
        rows: list[tuple[object, ...]] = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            target = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            rows.append((family, socket.SOCK_STREAM, 6, "", target))
        return rows

    return resolve


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokUrlSecurityTests(unittest.TestCase):
    def test_api_key_format_rejects_whitespace_controls_and_non_ascii(self) -> None:
        self.assertEqual(validate_api_key("sk-valid-key"), "sk-valid-key")
        for value in ("short", " sk-leading-space", "sk-line\nbreak", "sk-密钥-value"):
            with self.subTest(value=value):
                with self.assertRaises(ModelGatewayError) as raised:
                    validate_api_key(value)
                self.assertEqual(raised.exception.code, "api_key_invalid")

    def test_public_allowlisted_https_url_is_normalized(self) -> None:
        normalized = validate_model_base_url(
            "HTTPS://API.EXAMPLE.COM:443/v1/",
            _settings(),
            resolver=_resolver("93.184.216.34"),
        )

        self.assertEqual(normalized, "https://api.example.com/v1")

    def test_unsafe_url_forms_are_rejected_before_use(self) -> None:
        cases = (
            ("http://api.example.com/v1", "endpoint_invalid"),
            ("https://user:password@api.example.com/v1", "endpoint_invalid"),
            ("https://api.example.com/v1?api_key=secret", "endpoint_invalid"),
            ("https://api.example.com/v1#secret", "endpoint_invalid"),
            ("https://127.0.0.1/v1", "endpoint_ip_literal"),
            ("https://api.example.com:8443/v1", "endpoint_port_not_allowed"),
            ("https://unlisted.example.net/v1", "endpoint_not_allowlisted"),
        )

        for value, expected_code in cases:
            with self.subTest(value=value):
                with self.assertRaises(ModelGatewayError) as raised:
                    validate_model_base_url(
                        value,
                        _settings(),
                        resolver=_resolver("93.184.216.34"),
                    )
                self.assertEqual(raised.exception.code, expected_code)

    def test_every_dns_answer_must_be_public(self) -> None:
        for addresses in (("127.0.0.1",), ("93.184.216.34", "10.0.0.8")):
            with self.subTest(addresses=addresses):
                with self.assertRaises(ModelGatewayError) as raised:
                    validate_model_base_url(
                        "https://api.example.com/v1",
                        _settings(),
                        resolver=_resolver(*addresses),
                    )
                self.assertEqual(raised.exception.code, "endpoint_private_address")

    def test_tcp_backend_uses_only_the_addresses_approved_for_the_original_host(self) -> None:
        resolved = resolve_model_base_url(
            "https://api.example.com/v1",
            _settings(),
            resolver=_resolver("93.184.216.34", "1.1.1.1"),
        )
        self.assertEqual(resolved.hostname, "api.example.com")
        self.assertEqual(resolved.port, 443)
        self.assertEqual(resolved.addresses, ("93.184.216.34", "1.1.1.1"))

        calls: list[tuple[str, int]] = []
        sentinel = object()

        class RecordingBackend:
            def connect_tcp(
                self,
                host: str,
                port: int,
                **_kwargs: object,
            ) -> object:
                calls.append((host, port))
                if host == "93.184.216.34":
                    raise httpcore.ConnectError("first approved address unavailable")
                return sentinel

            def sleep(self, _seconds: float) -> None:
                return None

        backend = _PinnedNetworkBackend(
            resolved.hostname, resolved.port, resolved.addresses
        )
        backend._backend = RecordingBackend()  # type: ignore[assignment]

        connected = backend.connect_tcp(resolved.hostname, resolved.port)
        self.assertIs(connected, sentinel)
        self.assertEqual(
            calls,
            [("93.184.216.34", 443), ("1.1.1.1", 443)],
        )

        with self.assertRaises(httpcore.ConnectError):
            backend.connect_tcp("rebound.example.net", resolved.port)
        with self.assertRaises(httpcore.ConnectError):
            backend.connect_tcp(resolved.hostname, 8443)
        self.assertEqual(len(calls), 2)

    def test_wildcard_allowlist_does_not_authorize_the_apex(self) -> None:
        wildcard_settings = _settings(allowed_hosts=("*.example.com",))
        self.assertEqual(
            validate_model_base_url(
                "https://models.example.com/v1",
                wildcard_settings,
                resolver=_resolver("93.184.216.34"),
            ),
            "https://models.example.com/v1",
        )
        with self.assertRaises(ModelGatewayError) as raised:
            validate_model_base_url(
                "https://example.com/v1",
                wildcard_settings,
                resolver=_resolver("93.184.216.34"),
            )
        self.assertEqual(raised.exception.code, "endpoint_not_allowlisted")

    def test_redirect_and_provider_error_body_never_escape_the_gateway(self) -> None:
        secret = "sk-sensitive-provider-key"
        requests: list[httpx.Request] = []

        def redirect_handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                302,
                headers={"Location": "https://127.0.0.1/private"},
                request=request,
            )

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(redirect_handler),
            resolver=_resolver("93.184.216.34"),
        )
        with self.assertRaises(ModelGatewayError) as redirect_error:
            gateway.test_connection(
                base_url="https://api.example.com/v1",
                api_key=secret,
                model="safe-model",
            )
        self.assertEqual(redirect_error.exception.code, "provider_redirect_rejected")
        self.assertEqual(len(requests), 1)
        self.assertNotIn(secret, str(redirect_error.exception))
        self.assertNotIn("127.0.0.1", str(redirect_error.exception))

        def rejected_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                json={"error": f"upstream echoed {secret}"},
                request=request,
            )

        rejected_gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(rejected_handler),
            resolver=_resolver("93.184.216.34"),
        )
        with self.assertRaises(ModelGatewayError) as rejected_error:
            rejected_gateway.test_connection(
                base_url="https://api.example.com/v1",
                api_key=secret,
                model="safe-model",
            )
        self.assertEqual(rejected_error.exception.code, "provider_rejected")
        self.assertNotIn(secret, str(rejected_error.exception))

    def test_response_body_limit_applies_to_streamed_content(self) -> None:
        class OversizedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"x" * (32 * 1024)
                yield b"x" * (32 * 1024 + 1)

        def oversized_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=OversizedStream(),
                request=request,
            )

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(oversized_handler),
            resolver=_resolver("93.184.216.34"),
        )
        with self.assertRaises(ModelGatewayError) as raised:
            gateway.test_connection(
                base_url="https://api.example.com/v1",
                api_key="sk-response-limit-key",
                model="safe-model",
            )
        self.assertEqual(raised.exception.code, "provider_response_too_large")

    def test_success_response_parses_text_usage_without_returning_authorization(self) -> None:
        secret = "sk-success-provider-key"
        outbound_authorizations: list[str] = []

        def success_handler(request: httpx.Request) -> httpx.Response:
            outbound_authorizations.append(str(request.headers.get("Authorization") or ""))
            self.assertNotIn(secret.encode("ascii"), request.content)
            request_payload = json.loads(request.content)
            self.assertEqual(request_payload["model"], "safe-model")
            self.assertEqual(request_payload["messages"][0]["content"], "Hello")
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "  generated reply  "}}],
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 5,
                        "total_tokens": 22,
                    },
                },
                request=request,
            )

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(success_handler),
            resolver=_resolver("93.184.216.34"),
        )
        completion = gateway.complete(
            base_url="https://api.example.com/v1",
            api_key=secret,
            model="safe-model",
            messages=({"role": "user", "content": "Hello"},),
            temperature=0.7,
            max_output_tokens=128,
        )

        self.assertEqual(outbound_authorizations, [f"Bearer {secret}"])
        self.assertEqual(completion.text, "generated reply")
        self.assertEqual(completion.input_tokens, 17)
        self.assertEqual(completion.output_tokens, 5)
        self.assertEqual(completion.total_tokens, 22)
        self.assertGreaterEqual(completion.latency_ms, 0)
        self.assertNotIn(secret, repr(completion))
        self.assertNotIn("Authorization", repr(completion))


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokSecretHandlingTests(unittest.TestCase):
    def test_api_key_encryption_is_bound_to_owner_connection_and_field(self) -> None:
        cipher = CredentialCipher({1: b"k" * 32}, current_version=1)
        owner_id = uuid.uuid4()
        other_owner_id = uuid.uuid4()
        connection_id = uuid.uuid4()
        other_connection_id = uuid.uuid4()
        secret = "sk-test-secret-value"
        context = api_key_context(owner_id, connection_id)

        encrypted = cipher.encrypt_text(
            secret,
            purpose="ai-model-connection.api-key",
            context=context,
        )

        self.assertNotIn(secret, json.dumps(encrypted, sort_keys=True))
        self.assertEqual(
            cipher.decrypt_text(
                encrypted,
                purpose="ai-model-connection.api-key",
                context=context,
            ),
            secret,
        )
        for wrong_purpose, wrong_context in (
            ("ai-model-connection.other", context),
            (
                "ai-model-connection.api-key",
                api_key_context(other_owner_id, connection_id),
            ),
            (
                "ai-model-connection.api-key",
                api_key_context(owner_id, other_connection_id),
            ),
        ):
            with self.subTest(purpose=wrong_purpose, context=wrong_context):
                with self.assertRaises(EncryptionError):
                    cipher.decrypt_text(
                        encrypted,
                        purpose=wrong_purpose,
                        context=wrong_context,
                    )

    def test_all_supported_api_key_spellings_are_redacted(self) -> None:
        secret = "sk-redact-me"
        payload = {
            "api_key": secret,
            "apikey": secret,
            "apiKey": secret,
            "x-api-key": secret,
            "X-API-Key": secret,
            "client_secret": secret,
            "clientSecret": secret,
            "provider_api_key_value": secret,
            "openaiClientSecretValue": secret,
            "nested": {"safe": "kept"},
        }

        for sanitizer in (_redact_request, _sanitize_profile, redact_raw_payload):
            with self.subTest(sanitizer=sanitizer.__name__):
                sanitized = sanitizer(payload)
                encoded = json.dumps(sanitized, sort_keys=True)
                self.assertNotIn(secret, encoded)
                self.assertEqual(sanitized["nested"]["safe"], "kept")

    def test_runtime_and_model_serialization_hide_the_plaintext_and_ciphertext(self) -> None:
        secret = "sk-runtime-secret"
        runtime = RuntimeConfiguration(
            owner_user_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            settings_version=1,
            configuration_fingerprint="f" * 64,
            base_url="https://api.example.com/v1",
            model="safe-model",
            api_key=secret,
            user_enabled=True,
            temperature=0.7,
            max_output_tokens=512,
            context_message_limit=30,
            custom_instructions="",
        )
        self.assertNotIn(secret, repr(runtime))
        runtime.clear_secret()
        self.assertEqual(runtime.api_key, "")

        connection = AiModelConnection(
            id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            label="默认连接",
            provider="openai_compatible",
            base_url="https://api.example.com/v1",
            model="safe-model",
            api_key_encrypted={"ciphertext": "opaque"},
            enabled=True,
            last_test_status="never",
        )
        serialized = connection.to_dict()
        self.assertNotIn("api_key_encrypted", serialized)
        self.assertNotIn("opaque", json.dumps(serialized, sort_keys=True))

        setting = AiAgentSetting(
            owner_user_id=uuid.uuid4(),
            custom_instructions="private writing preferences",
        )
        self.assertNotIn("custom_instructions", setting.to_dict())

        profile = AiStyleProfile(
            owner_user_id=uuid.uuid4(),
            summary="private style summary",
            traits={"tone": "private"},
        )
        serialized_profile = profile.to_dict()
        self.assertNotIn("summary", serialized_profile)
        self.assertNotIn("traits", serialized_profile)

        run = AiAgentRun(
            owner_user_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            run_type="reply_draft",
            status="running",
            idempotency_key="serialization-test",
            model_snapshot="private-model-value",
            output_text="private draft",
        )
        serialized_run = run.to_dict()
        self.assertNotIn("idempotency_key", serialized_run)
        self.assertNotIn("model_snapshot", serialized_run)
        self.assertNotIn("output_text", serialized_run)


class _ScalarSequenceDB:
    def __init__(self, *values: object) -> None:
        self.values = list(values)

    def scalar(self, _statement: object) -> object:
        return self.values.pop(0)


@unittest.skipIf(
    DEPENDENCY_IMPORT_ERROR is not None,
    f"production dependencies are not installed: {DEPENDENCY_IMPORT_ERROR}",
)
class ByokPermissionTests(unittest.TestCase):
    def test_runner_is_not_ready_until_the_active_connection_test_succeeds(self) -> None:
        setting = SimpleNamespace(
            user_enabled=True,
            mode="draft",
            custom_instructions=None,
            temperature_milli=700,
            max_output_tokens=512,
            context_message_limit=30,
            updated_at=SimpleNamespace(isoformat=lambda: "2026-07-26T00:00:00+00:00"),
        )
        connection = SimpleNamespace(
            enabled=True,
            api_key_encrypted={"ciphertext": "opaque"},
            last_test_status="failed",
        )

        public = settings_public(setting, connection=connection)
        self.assertFalse(public["ready"])
        self.assertFalse(public["chat_suggestions_enabled"])
        setting.chat_suggestions_enabled = True
        self.assertTrue(
            settings_public(setting, connection=connection)[
                "chat_suggestions_enabled"
            ]
        )
        connection.last_test_status = "ok"
        self.assertTrue(settings_public(setting, connection=connection)["ready"])

    def test_system_user_and_account_gates_all_fail_closed_with_same_response(self) -> None:
        active_granted_user = SimpleNamespace(
            status="active",
            disabled_at=None,
            byok_model_runner_enabled=True,
        )
        active_denied_user = SimpleNamespace(
            status="active",
            disabled_at=None,
            byok_model_runner_enabled=False,
        )
        disabled_granted_user = SimpleNamespace(
            status="disabled",
            disabled_at=object(),
            byok_model_runner_enabled=True,
        )
        system_on = SimpleNamespace(enabled=True)
        system_off = SimpleNamespace(enabled=False)

        denied_cases = (
            (active_granted_user, system_off),
            (active_denied_user, system_on),
            (disabled_granted_user, system_on),
            (None, system_on),
        )
        for user, system in denied_cases:
            with self.subTest(user=user, system=system):
                with self.assertRaises(AgentServiceError) as raised:
                    require_visible_access(
                        _ScalarSequenceDB(user, system), uuid.uuid4()
                    )
                self.assertEqual(raised.exception.code, "ai_agent_not_available")
                self.assertEqual(raised.exception.status_code, 404)
                self.assertEqual(raised.exception.public_message, "该功能当前不可用")

        access = require_visible_access(
            _ScalarSequenceDB(active_granted_user, system_on), uuid.uuid4()
        )
        self.assertTrue(access.visible)

    def test_runtime_result_is_cancelled_when_configuration_version_changes(self) -> None:
        owner_id = uuid.uuid4()
        connection_id = uuid.uuid4()
        user = SimpleNamespace(
            status="active",
            disabled_at=None,
            byok_model_runner_enabled=True,
        )
        system = SimpleNamespace(enabled=True)
        changed_settings = SimpleNamespace(
            active_connection_id=connection_id,
            version=2,
            user_enabled=True,
        )

        with self.assertRaises(AgentServiceError) as raised:
            _runtime_still_enabled(
                _ScalarSequenceDB(user, system, changed_settings),
                owner_user_id=owner_id,
                connection_id=connection_id,
                settings_version=1,
                require_user_enabled=True,
            )

        self.assertEqual(raised.exception.code, "runner_state_changed")
        self.assertEqual(raised.exception.status_code, 409)

    def test_runtime_result_is_cancelled_when_connection_test_is_not_ok(self) -> None:
        owner_id = uuid.uuid4()
        connection_id = uuid.uuid4()
        user = SimpleNamespace(
            status="active",
            disabled_at=None,
            byok_model_runner_enabled=True,
        )
        system = SimpleNamespace(enabled=True)
        settings = SimpleNamespace(
            active_connection_id=connection_id,
            version=1,
            user_enabled=True,
        )
        connection = SimpleNamespace(enabled=True, last_test_status="failed")

        with self.assertRaises(AgentServiceError) as raised:
            _runtime_still_enabled(
                _ScalarSequenceDB(user, system, settings, connection),
                owner_user_id=owner_id,
                connection_id=connection_id,
                settings_version=1,
                require_user_enabled=True,
            )

        self.assertEqual(raised.exception.code, "connection_disabled")
        self.assertEqual(raised.exception.status_code, 409)


class ByokManagementAndMigrationContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def test_management_ui_requires_reason_for_global_and_per_user_changes(self) -> None:
        admin_html = self.read("bbw_web/static/admin.html")
        admin_js = self.read("bbw_web/static/admin.js")

        for element_id in (
            "admin-byok-model-runner-control",
            "admin-byok-model-runner-global-dialog",
            "admin-byok-model-runner-global-form",
            "admin-byok-model-runner-global-reason",
            "admin-user-byok-model-runner-dialog",
            "admin-user-byok-model-runner-form",
            "admin-user-byok-model-runner-reason",
            "admin-byok-account-actions-control",
            "admin-byok-account-actions-global-dialog",
            "admin-byok-account-actions-global-form",
            "admin-byok-account-actions-global-reason",
            "admin-user-byok-account-actions-dialog",
            "admin-user-byok-account-actions-form",
            "admin-user-byok-account-actions-reason",
        ):
            self.assertIn(f'id="{element_id}"', admin_html)

        self.assertIn("byokModelRunner:", admin_js)
        self.assertIn("userByokModelRunner:", admin_js)
        self.assertIn("async function loadByokModelRunnerControl", admin_js)
        self.assertIn("byokAccountActions:", admin_js)
        self.assertIn("userByokAccountActions:", admin_js)
        self.assertIn("async function loadByokAccountActionsControl", admin_js)

        global_submit = admin_js.split(
            '$("admin-byok-model-runner-global-form").addEventListener("submit"',
            1,
        )[1].split(
            '$("admin-byok-model-runner-global-cancel").addEventListener',
            1,
        )[0]
        user_submit = admin_js.split(
            '$("admin-user-byok-model-runner-form").addEventListener("submit"',
            1,
        )[1].split(
            '$("admin-user-byok-model-runner-cancel").addEventListener',
            1,
        )[0]
        account_actions_global_submit = admin_js.split(
            '$("admin-byok-account-actions-global-form").addEventListener("submit"',
            1,
        )[1].split(
            '$("admin-byok-account-actions-global-cancel").addEventListener',
            1,
        )[0]
        account_actions_user_submit = admin_js.split(
            '$("admin-user-byok-account-actions-form").addEventListener("submit"',
            1,
        )[1].split(
            '$("admin-user-byok-account-actions-cancel").addEventListener',
            1,
        )[0]

        self.assertIn("ADMIN_ENDPOINTS.byokModelRunner", global_submit)
        self.assertIn(
            "JSON.stringify({ enabled: pending.enabled, reason })", global_submit
        )
        self.assertIn("ADMIN_ENDPOINTS.userByokModelRunner(pending.userId)", user_submit)
        self.assertIn(
            "JSON.stringify({ enabled: pending.enabled, reason })", user_submit
        )
        self.assertIn(
            "ADMIN_ENDPOINTS.byokAccountActions", account_actions_global_submit
        )
        self.assertIn(
            "JSON.stringify({ enabled: pending.enabled, reason })",
            account_actions_global_submit,
        )
        self.assertIn(
            "ADMIN_ENDPOINTS.userByokAccountActions(pending.userId)",
            account_actions_user_submit,
        )
        self.assertIn(
            "JSON.stringify({ enabled: pending.enabled, reason })",
            account_actions_user_submit,
        )

        for action, label in (
            (
                "user.byok_model_runner_changed",
                "修改用户 BYOK 模型运行器授权",
            ),
            (
                "ai.model_runner_control_view",
                "查看 BYOK 模型运行器全局状态",
            ),
            (
                "ai.model_runner_global_changed",
                "修改 BYOK 模型运行器全局开关",
            ),
            (
                "ai.account_actions_control_view",
                "查看模型账号动作执行全局状态",
            ),
            (
                "ai.account_actions_global_changed",
                "修改模型账号动作执行全局开关",
            ),
            (
                "user.byok_account_actions_changed",
                "修改用户模型账号动作执行授权",
            ),
        ):
            self.assertIn(f'"{action}": "{label}"', admin_js)

    def test_management_routes_are_admin_only_and_revoke_running_work(self) -> None:
        admin = self.read("bbw_web/admin_api.py")
        get_global = admin.split(
            "def get_byok_model_runner_control", 1
        )[1].split("def set_byok_model_runner_control", 1)[0]
        set_global = admin.split(
            "def set_byok_model_runner_control", 1
        )[1].split("def get_byok_account_actions_control", 1)[0]
        get_account_actions = admin.split(
            "def get_byok_account_actions_control", 1
        )[1].split("def set_byok_account_actions_control", 1)[0]
        set_account_actions = admin.split(
            "def set_byok_account_actions_control", 1
        )[1].split("def list_users", 1)[0]
        set_user = admin.split("def set_user_byok_model_runner", 1)[1].split(
            "def set_user_byok_account_actions", 1
        )[0]
        set_user_account_actions = admin.split(
            "def set_user_byok_account_actions", 1
        )[1].split(
            "def view_user_credentials", 1
        )[0]

        self.assertIn("context: AdminContext = Depends(_admin_context)", get_global)
        self.assertIn("context: AdminContext = Depends(_admin_context)", set_global)
        self.assertIn("context: AdminContext = Depends(_admin_context)", set_user)
        self.assertIn(
            "context: AdminContext = Depends(_admin_context)", get_account_actions
        )
        self.assertIn(
            "context: AdminContext = Depends(_admin_context)", set_account_actions
        )
        self.assertIn(
            "context: AdminContext = Depends(_admin_context)",
            set_user_account_actions,
        )
        self.assertIn("if body.enabled and _is_production", set_global)
        self.assertIn("not allowed_hosts", set_global)
        self.assertIn("AgentRunRepository(db).cancel_all_active()", set_global)
        self.assertIn("if not new_enabled:", set_user)
        self.assertIn("agent_settings.user_enabled = False", set_user)
        self.assertIn("cancel_active_for_owner", set_user)
        self.assertIn('resource_id="byok_model_runner"', set_user)
        self.assertIn("if body.enabled and not row.enabled", set_account_actions)
        self.assertIn("row.account_actions_enabled = new_enabled", set_account_actions)
        self.assertIn("cancel_all_active", set_account_actions)
        self.assertIn('resource_id="byok_account_actions"', set_account_actions)
        self.assertIn(
            "if new_enabled and not user.byok_model_runner_enabled",
            set_user_account_actions,
        )
        self.assertIn(
            "user.byok_account_actions_enabled = new_enabled",
            set_user_account_actions,
        )
        self.assertIn("disable_for_owner", set_user_account_actions)
        self.assertIn("cancel_active_for_owner", set_user_account_actions)
        self.assertIn(
            'action="user.byok_account_actions_changed"',
            set_user_account_actions,
        )

    def test_autonomous_agent_management_has_independent_admin_gates(self) -> None:
        admin = self.read("bbw_web/admin_api.py")
        admin_html = self.read("bbw_web/static/admin.html")
        admin_js = self.read("bbw_web/static/admin.js")

        for element_id in (
            "admin-byok-autonomous-agent-control",
            "admin-byok-autonomous-agent-global-dialog",
            "admin-byok-autonomous-agent-global-form",
            "admin-byok-autonomous-agent-global-reason",
            "admin-user-byok-autonomous-agent-dialog",
            "admin-user-byok-autonomous-agent-form",
            "admin-user-byok-autonomous-agent-reason",
        ):
            self.assertIn(f'id="{element_id}"', admin_html)

        self.assertIn("byokAutonomousAgent:", admin_js)
        self.assertIn("userByokAutonomousAgent:", admin_js)
        self.assertIn("async function loadByokAutonomousAgentControl", admin_js)
        autonomous_render = admin_js.split(
            "function renderByokAutonomousAgentControl", 1
        )[1].split("async function loadByokAutonomousAgentControl", 1)[0]
        self.assertIn(
            "const backgroundEnabled = feature.background_enabled === true;",
            autonomous_render,
        )
        global_submit = admin_js.split(
            '$("admin-byok-autonomous-agent-global-form").addEventListener("submit"',
            1,
        )[1].split(
            '$("admin-byok-autonomous-agent-global-cancel").addEventListener',
            1,
        )[0]
        user_submit = admin_js.split(
            '$("admin-user-byok-autonomous-agent-form").addEventListener("submit"',
            1,
        )[1].split(
            '$("admin-user-byok-autonomous-agent-cancel").addEventListener',
            1,
        )[0]
        self.assertIn("ADMIN_ENDPOINTS.byokAutonomousAgent", global_submit)
        self.assertIn(
            "JSON.stringify({ enabled: pending.enabled, reason })", global_submit
        )
        self.assertIn(
            "ADMIN_ENDPOINTS.userByokAutonomousAgent(pending.userId)", user_submit
        )
        self.assertIn(
            "JSON.stringify({ enabled: pending.enabled, reason })", user_submit
        )
        for action, label in (
            (
                "ai.autonomous_agent_control_view",
                "查看无人值守运营 Agent 全局状态",
            ),
            (
                "ai.autonomous_agent_global_changed",
                "修改无人值守运营 Agent 全局开关",
            ),
            (
                "user.byok_autonomous_agent_changed",
                "修改用户无人值守运营 Agent 授权",
            ),
            (
                "ai.autonomy_settings_changed",
                "修改无人值守自治设置",
            ),
        ):
            self.assertIn(f'"{action}": "{label}"', admin_js)
        self.assertIn("function populateAuditActionOptions", admin_js)
        self.assertIn("AUDIT_ACTION_LABELS", admin_js)
        self.assertIn('list="admin-audit-action-options"', admin_html)
        self.assertIn('<datalist id="admin-audit-action-options">', admin_html)

        get_global = admin.split(
            "def get_byok_autonomous_agent_control", 1
        )[1].split("def set_byok_autonomous_agent_control", 1)[0]
        set_global = admin.split(
            "def set_byok_autonomous_agent_control", 1
        )[1].split("def list_users", 1)[0]
        set_user = admin.split(
            "def set_user_byok_autonomous_agent", 1
        )[1].split("def view_user_credentials", 1)[0]
        for route in (get_global, set_global, set_user):
            self.assertIn(
                "context: AdminContext = Depends(_admin_context)", route
            )
        self.assertIn("if body.enabled and not row.enabled", set_global)
        self.assertIn(
            "if body.enabled and not row.account_actions_enabled", set_global
        )
        self.assertIn("_disable_system_autonomy", set_global)
        self.assertIn("_cancel_all_autonomy_tasks", admin)
        self.assertIn("cancel_all_queued", admin)
        self.assertIn("cancel_all_not_started", admin)
        self.assertIn(
            "if new_enabled and not user.byok_model_runner_enabled", set_user
        )
        self.assertIn(
            "if new_enabled and not user.byok_account_actions_enabled", set_user
        )
        self.assertIn("_set_user_autonomy_grant", set_user)
        self.assertIn("_disable_autonomy_for_owner", set_user)
        self.assertIn("_cancel_autonomy_tasks_for_owner", set_user)
        self.assertIn(
            'action="user.byok_autonomous_agent_changed"', set_user
        )

        for cascade_source in (
            "model_runner_disabled",
            "account_actions_disabled",
            "user_disabled",
            "model_runner_access_revoked",
            "execution_access_revoked",
        ):
            self.assertIn(f'"{cascade_source}"', admin)

    def test_legacy_persistence_redactors_cover_byok_key_variants(self) -> None:
        persistence = self.read("bbw_web/persistence.py")
        request_redactor = persistence.split("_REQUEST_SECRET_KEYS", 1)[1].split(
            "def _redact_request", 1
        )[0]
        profile_redactor = persistence.split("_PROFILE_SECRET_KEYS", 1)[1].split(
            "def _sanitize_profile", 1
        )[0]

        for spelling in (
            '"api_key"',
            '"apikey"',
            '"x-api-key"',
            '"client_secret"',
            '"clientsecret"',
        ):
            self.assertIn(spelling, request_redactor)
        for normalized in ('"apikey"', '"xapikey"', '"clientsecret"'):
            self.assertIn(normalized, profile_redactor)

    def test_gateway_source_contains_all_url_and_transport_guards(self) -> None:
        gateway = self.read("bbw_agent/model_gateway.py")

        self.assertIn('parsed.scheme.lower() != "https"', gateway)
        self.assertIn("parsed.username is not None", gateway)
        self.assertIn("parsed.password is not None", gateway)
        self.assertIn("or parsed.query", gateway)
        self.assertIn("or parsed.fragment", gateway)
        self.assertIn("ipaddress.ip_address(host)", gateway)
        self.assertIn("if not address.is_global", gateway)
        self.assertIn("endpoint_not_allowlisted", gateway)
        self.assertIn("port not in {None, 443}", gateway)
        self.assertIn("follow_redirects=False", gateway)
        self.assertIn("trust_env=False", gateway)
        self.assertIn("network_backend=_PinnedNetworkBackend", gateway)
        self.assertIn("return self._backend.connect_tcp(\n                    address,", gateway)
        self.assertIn(
            "endpoint = chat_completions_url(resolved_endpoint.base_url)", gateway
        )

    def test_migration_defaults_to_denied_and_enforces_owner_boundaries(self) -> None:
        migration = self.read(
            "migrations/versions/20260725_0012_byok_model_runner.py"
        )
        models = self.read("bbw_prod/models.py")

        self.assertIn('revision: str = "20260725_0012"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0011"',
            migration,
        )
        self.assertIn('"byok_model_runner_enabled"', migration)
        self.assertIn('server_default=sa.text("false")', migration)
        self.assertIn('"ai_model_runner_system_settings"', migration)
        self.assertIn("(id, enabled) VALUES (1, false)", migration)
        self.assertIn('"api_key_encrypted"', migration)
        self.assertIn(
            '["active_connection_id", "owner_user_id"]', migration
        )
        self.assertIn(
            '["connection_id", "owner_user_id"]', migration
        )
        self.assertIn('"ai_model_connections.id", "ai_model_connections.owner_user_id"', migration)
        self.assertIn('op.drop_column("users", "byok_model_runner_enabled")', migration)
        self.assertIn(
            '__sensitive_fields__ = frozenset({"api_key_encrypted"})', models
        )

    def test_agent_runtime_never_persists_prompt_headers_or_raw_provider_errors(self) -> None:
        models = self.read("bbw_prod/models.py")
        api = self.read("bbw_agent/api.py")
        gateway = self.read("bbw_agent/model_gateway.py")

        run_model = models.split("class AiAgentRun", 1)[1].split(
            "class SyncCursor", 1
        )[0]
        self.assertNotIn("prompt_text", run_model)
        self.assertNotIn("request_headers", run_model)
        self.assertNotIn("raw_response", run_model)
        self.assertIn("failure_code", run_model)
        self.assertIn("api_key: SecretStr", api)
        self.assertIn("runtime.clear_secret()", api)
        self.assertIn('"origin": "agent"', api)
        self.assertGreaterEqual(api.count('"executed": False'), 2)
        self.assertNotIn("bbw_protocol", api)
        self.assertIn("follow_redirects=False", gateway)
        self.assertIn("trust_env=False", gateway)
        self.assertNotIn("response.text", gateway)
        self.assertNotIn("response.json()", gateway)

    def test_admin_agent_record_read_endpoints_are_wired_and_non_sensitive(self) -> None:
        admin = self.read("bbw_web/admin_api.py")
        admin_html = self.read("bbw_web/static/admin.html")
        admin_js = self.read("bbw_web/static/admin.js")

        for route in (
            '@router.get("/users/{user_id}/agent-runs")',
            '@router.get("/users/{user_id}/agent-actions")',
            '@router.get("/users/{user_id}/autonomy-tasks")',
        ):
            self.assertIn(route, admin)
        for action in (
            'action="agent_run.list"',
            'action="agent_action.list"',
            'action="autonomy_task.list"',
        ):
            self.assertIn(action, admin)
        self.assertIn(
            "items = [autonomy_task_public(row) for row in rows]", admin
        )

        run_public = admin.split("def _agent_run_public", 1)[1].split(
            "def _agent_action_public", 1
        )[0]
        for sensitive in ("model_snapshot", "output_text", "idempotency_key"):
            self.assertNotIn(f'"{sensitive}"', run_public)
        action_public = admin.split("def _agent_action_public", 1)[1].split(
            "def _require_user", 1
        )[0]
        for sensitive in (
            "target_snapshot",
            "parameter_snapshot",
            "external_result_id",
            "idempotency_key",
        ):
            self.assertNotIn(f'"{sensitive}"', action_public)

        self.assertIn('data-user-tab="agent"', admin_html)
        self.assertIn("userAgentRuns:", admin_js)
        self.assertIn("userAgentActions:", admin_js)
        self.assertIn("userAutonomyTasks:", admin_js)
        self.assertIn("async function loadUserAgentRecords", admin_js)
        self.assertIn('agent: { label: "AI 助手记录", countKey: "agent_runs" }', admin_js)
        for action, label in (
            ("agent_run.list", "查看模型运行记录"),
            ("agent_action.list", "查看动作执行记录"),
            ("autonomy_task.list", "查看自治任务记录"),
        ):
            self.assertIn(f'"{action}": "{label}"', admin_js)


if __name__ == "__main__":
    unittest.main()
