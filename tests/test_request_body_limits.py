from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bbw_web.api import RequestBodyLimitMiddleware


def scope_for(content_type: str | None = "application/json", **kwargs):
    headers = [] if content_type is None else [(b"content-type", content_type.encode("ascii"))]
    settings = SimpleNamespace(max_json_body_bytes=32, max_request_body_bytes=128)
    return {
        "type": "http", "method": "POST", "path": "/api/test", "headers": headers,
        "app": SimpleNamespace(state=SimpleNamespace(settings=settings)), **kwargs,
    }


class JsonBodyLimitTests(unittest.TestCase):
    def test_fastapi_json_media_types_share_the_small_limit(self):
        app = FastAPI()
        app.state.settings = SimpleNamespace(max_json_body_bytes=32, max_request_body_bytes=128)
        app.add_middleware(RequestBodyLimitMiddleware)
        calls = []

        @app.post("/command")
        def command(payload: dict):
            calls.append(payload)
            return {"ok": True}

        with TestClient(app) as client:
            for media_type in (None, "application/json", "application/problem+json", "application/vnd.api+json; charset=utf-8"):
                with self.subTest(media_type=media_type):
                    headers = {} if media_type is None else {"Content-Type": media_type}
                    response = client.post("/command", content=b'{"value":"' + b"x" * 48 + b'"}', headers=headers)
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(response.json()["error"], "request body too large")
        self.assertEqual(calls, [])

    def test_duplicate_content_types_cannot_select_different_limits(self):
        app = FastAPI()
        app.state.settings = SimpleNamespace(max_json_body_bytes=32, max_request_body_bytes=128)
        app.add_middleware(RequestBodyLimitMiddleware)
        calls = []

        @app.post("/command")
        def command(payload: dict):
            calls.append(payload)
            return {"ok": True}

        with TestClient(app) as client:
            for media_types in (
                ("application/json", "application/octet-stream"),
                ("application/octet-stream", "application/json"),
                ("application/json", "application/json"),
            ):
                with self.subTest(media_types=media_types):
                    response = client.post(
                        "/command", content=b'{"value":"' + b"x" * 48 + b'"}',
                        headers=[("Content-Type", value) for value in media_types],
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"], "duplicate content type")
        self.assertEqual(calls, [])

    def test_small_json_remains_accepted(self):
        app = FastAPI()
        app.state.settings = SimpleNamespace(max_json_body_bytes=32, max_request_body_bytes=128)
        app.add_middleware(RequestBodyLimitMiddleware)

        @app.post("/command")
        def command(payload: dict):
            return payload

        with TestClient(app) as client:
            for media_type in ("application/json", "application/problem+json"):
                with self.subTest(media_type=media_type):
                    headers = {} if media_type is None else {"Content-Type": media_type}
                    response = client.post("/command", content=b'{"ok":true}', headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True})


class BodyReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_delivers_body_once_then_forwards_disconnect(self):
        received = []
        source = asyncio.Queue()
        source.put_nowait({"type": "http.request", "body": b'{"ok":', "more_body": True})
        source.put_nowait({"type": "http.request", "body": b'true}', "more_body": False})
        source.put_nowait({"type": "http.disconnect"})

        async def downstream(scope, receive, send):
            received.append(await receive())
            received.append(await receive())

        async def send(message):
            self.fail("the middleware should not reject this request")

        await RequestBodyLimitMiddleware(downstream)(scope_for(), source.get, send)
        self.assertEqual(received, [
            {"type": "http.request", "body": b'{"ok":true}', "more_body": False},
            {"type": "http.disconnect"},
        ])

    async def test_replay_does_not_busy_loop_or_invent_early_disconnect(self):
        source = asyncio.Queue()
        source.put_nowait({"type": "http.request", "body": b"{}", "more_body": False})

        async def downstream(scope, receive, send):
            await receive()
            pending = asyncio.create_task(receive())
            try:
                await asyncio.sleep(0)
                self.assertFalse(pending.done(), "post-body reads must wait for real ASGI events")
                source.put_nowait({"type": "http.disconnect"})
                self.assertEqual(await asyncio.wait_for(pending, 1), {"type": "http.disconnect"})
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        async def send(message):
            self.fail("the middleware should not reject this request")

        await RequestBodyLimitMiddleware(downstream)(scope_for(), source.get, send)

    async def test_chunked_json_and_binary_keep_their_respective_limits(self):
        for media_type, expected in ((None, 413), ("application/problem+json", 413), ("application/json", 413), ("multipart/form-data; boundary=test", 200)):
            with self.subTest(media_type=media_type):
                source = asyncio.Queue()
                source.put_nowait({"type": "http.request", "body": b"x" * 24, "more_body": True})
                source.put_nowait({"type": "http.request", "body": b"x" * 24, "more_body": False})
                replies = []

                async def send(message):
                    replies.append(message)

                async def downstream(scope, receive, send):
                    self.assertEqual((await receive())["body"], b"x" * 48)
                    await send({"type": "http.response.start", "status": 200})

                await RequestBodyLimitMiddleware(downstream)(scope_for(media_type), source.get, send)
                self.assertEqual(replies[0]["status"], expected)

    async def test_disconnected_upload_does_not_dispatch_or_retain_slot(self):
        source = asyncio.Queue()
        source.put_nowait({"type": "http.request", "body": b"{", "more_body": True})
        source.put_nowait({"type": "http.disconnect"})

        async def downstream(scope, receive, send):
            self.fail("an incomplete request must not reach the application")

        async def send(message):
            self.fail("a disconnected upload has no response")

        middleware = RequestBodyLimitMiddleware(downstream)
        await middleware(scope_for(), source.get, send)
        self.assertEqual(middleware._slots._value, 8)


if __name__ == "__main__":
    unittest.main()
