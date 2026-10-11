from __future__ import annotations

import gzip
import json
import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bbw_agent.model_gateway import ModelGatewayError, OpenAICompatibleGateway
from bbw_prod import config


class SettingsSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never inherit real deployment credentials or secret-file paths.
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cache = patch.object(config, "_settings", None)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_invalid_configuration_never_enters_cache(self) -> None:
        os.environ.update(BBW_ENV="production", BBW_COOKIE_SECURE="false",
                          BBW_DATABASE_URL="postgresql://test:fake-password@unused/test")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(config.ConfigurationError, "Secure"):
                    config.get_settings()
        self.assertIsNone(config._settings)

    def test_corrected_environment_recovers_after_validation_failure(self) -> None:
        os.environ.update(BBW_ENV="production", BBW_COOKIE_SECURE="false",
                          BBW_DATABASE_URL="postgresql://test:fake-password@unused/test")
        with self.assertRaises(config.ConfigurationError):
            config.get_settings()
        os.environ["BBW_COOKIE_SECURE"] = "true"
        settings = config.get_settings()
        self.assertTrue(settings.cookie_secure)
        self.assertIs(settings, config.get_settings())

    def test_settings_representation_omits_credentials(self) -> None:
        secrets = {
            "BBW_DATABASE_URL": "postgresql://test:fake-db-password@unused/test",
            "BBW_REDIS_URL": "redis://:fake-redis-password@unused/0",
            "BBW_CREDENTIAL_MASTER_KEY": "fake-master-key",
            "BBW_CREDENTIAL_KEYS_JSON": '{"1":"fake-keyring-secret"}',
            "BBW_PHONE_HMAC_KEY": "fake-phone-key",
            "BBW_SESSION_HMAC_KEY": "fake-session-key",
            "BBW_DEPLOYMENT_CONTROL_TOKEN": "fake-deployment-control-token",
        }
        os.environ.update(secrets)
        settings = config.Settings.from_env()
        for name, value in secrets.items():
            with self.subTest(name=name):
                self.assertNotIn(value, repr(settings))
                self.assertNotIn(value, str(settings))
        self.assertIn("environment='development'", repr(settings))
        self.assertEqual(settings.credential_master_key, secrets["BBW_CREDENTIAL_MASTER_KEY"])


class RecordingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


class RecordingTransport(httpx.MockTransport):
    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class GatewayResourceBoundaryTests(unittest.TestCase):
    def request(self, stream: RecordingStream, *, headers=None):
        def handle(request: httpx.Request) -> httpx.Response:
            self.request_headers = request.headers
            return httpx.Response(200, headers=headers or {}, stream=stream, request=request)

        settings = SimpleNamespace(
            environment="production",
            ai_byok_allowed_hosts=("api.example.com",),
            ai_byok_connect_timeout_seconds=1,
            ai_byok_read_timeout_seconds=5,
            ai_byok_max_response_bytes=1024,
        )
        self.transport = RecordingTransport(handle)
        gateway = OpenAICompatibleGateway(
            settings,
            transport=self.transport,
            resolver=lambda *_args, **_kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        )
        return gateway.test_connection(
            base_url="https://api.example.com/v1",
            api_key="sk-fake-test-only",
            model="test-model",
        )

    def test_compressed_response_is_rejected_before_read_or_decompression(self) -> None:
        # A tiny valid gzip body expands beyond the configured response budget.
        compressed = gzip.compress(b"x" * (128 * 1024))
        self.assertLess(len(compressed), 1024)
        for encoding in ("gzip", "deflate", "br", "zstd", "unknown", "identity, gzip"):
            stream = RecordingStream([compressed])
            with self.subTest(encoding=encoding):
                with self.assertRaises(ModelGatewayError) as raised:
                    self.request(stream, headers={"Content-Encoding": encoding})
                self.assertEqual(raised.exception.code, "provider_response_encoding_rejected")
                self.assertEqual(stream.reads, 0)
                self.assertTrue(stream.closed)
                self.assertTrue(self.transport.closed)

    def test_identity_response_remains_supported_and_requests_no_compression(self) -> None:
        body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
        for headers in ({}, {"Content-Encoding": "identity"}, {"Content-Encoding": "Identity"}):
            stream = RecordingStream([body[:10], body[10:]])
            with self.subTest(headers=headers):
                self.assertEqual(self.request(stream, headers=headers).text, "OK")
                self.assertEqual(self.request_headers["Accept-Encoding"], "identity")
                self.assertTrue(stream.closed)
                self.assertTrue(self.transport.closed)

    def test_stream_limit_stops_reading_and_closes_response(self) -> None:
        stream = RecordingStream([b"x" * 512, b"x" * 513, b"never-read"])
        with self.assertRaises(ModelGatewayError) as raised:
            self.request(stream)
        self.assertEqual(raised.exception.code, "provider_response_too_large")
        self.assertEqual(stream.reads, 2)
        self.assertTrue(stream.closed)

    def test_non_finite_optional_usage_does_not_crash_completion(self) -> None:
        body = json.dumps({
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": float("inf"), "completion_tokens": float("-inf"),
                      "total_tokens": 2},
        }).encode()
        stream = RecordingStream([body])
        completion = self.request(stream)
        self.assertEqual(completion.text, "OK")
        self.assertIsNone(completion.input_tokens)
        self.assertIsNone(completion.output_tokens)
        self.assertEqual(completion.total_tokens, 2)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
