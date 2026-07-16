from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from bbw_protocol import BeibeiwuApp
from bbw_protocol.adapters.roomkit import (
    ROOMKIT_BUSINESS_TOKEN,
    ROOMKIT_CHANNEL,
    ROOMKIT_VOICE_TYPE,
    RoomKitAdapter,
)
from bbw_protocol.session import Session


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SequenceOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        if not self.responses:
            raise AssertionError("unexpected RoomKit request")
        return self.responses.pop(0)


def logged_in_app() -> BeibeiwuApp:
    session = Session(
        uid="42",
        token="banghua-token",
        nickname="测试用户",
        portrait="https://oss.banghua.xin/avatar.jpg",
        device_id="stable-device-id",
        version_code="154",
        raw_user={"sex": "女"},
    )
    return BeibeiwuApp(session)


class RoomKitAdapterContractTests(unittest.TestCase):
    def test_login_is_cached_and_room_list_uses_independent_headers(self) -> None:
        login = FakeResponse(
            {
                "code": 10000,
                "msg": "success",
                "data": {
                    "authorization": "room-authorization",
                    "imToken": "room-im-token",
                    "userId": "room-user-42",
                    "userName": "测试用户",
                    "portrait": "https://oss.banghua.xin/avatar.jpg",
                    "sex": 1,
                },
            }
        )
        room_payload = {
            "code": 10000,
            "msg": "success",
            "data": {
                "rooms": [
                    {
                        "roomId": "r1",
                        "roomName": "晚安语音房",
                        "userTotal": 3,
                    }
                ],
                "images": [],
            },
        }
        opener = SequenceOpener(login, FakeResponse(room_payload), FakeResponse(room_payload))
        app = logged_in_app()
        adapter = RoomKitAdapter(app, opener=opener)

        first = adapter.rooms(page=1, size=10)
        second = adapter.rooms(page=2, size=20)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(app.session.token, "banghua-token")

        login_req = opener.calls[0][0]
        login_headers = {key.lower(): value for key, value in login_req.header_items()}
        login_form = parse_qs(login_req.data.decode("utf-8"))
        self.assertEqual(login_req.get_method(), "POST")
        self.assertTrue(login_req.full_url.endswith("/user/login"))
        self.assertEqual(login_headers["businesstoken"], ROOMKIT_BUSINESS_TOKEN)
        self.assertNotIn("authorization", login_headers)
        self.assertNotIn("author-token", login_headers)
        self.assertNotIn(ROOMKIT_BUSINESS_TOKEN, login_req.full_url)
        self.assertEqual(login_form["mobile"], ["42"])
        self.assertEqual(login_form["sex"], ["1"])
        self.assertEqual(login_form["verifyCode"], ["111111"])
        self.assertEqual(login_form["deviceId"], ["stable-device-id"])
        self.assertEqual(login_form["channel"], [ROOMKIT_CHANNEL])
        self.assertEqual(login_form["version"], ["154"])

        first_list_req = opener.calls[1][0]
        first_headers = {key.lower(): value for key, value in first_list_req.header_items()}
        first_query = parse_qs(urlparse(first_list_req.full_url).query)
        self.assertEqual(first_list_req.get_method(), "GET")
        self.assertEqual(first_headers["businesstoken"], ROOMKIT_BUSINESS_TOKEN)
        self.assertEqual(first_headers["authorization"], "room-authorization")
        self.assertNotIn("author-token", first_headers)
        self.assertEqual(urlparse(first_list_req.full_url).path, "/mic/room/list")
        self.assertNotIn("room-authorization", first_list_req.full_url)
        self.assertNotIn(ROOMKIT_BUSINESS_TOKEN, first_list_req.full_url)
        self.assertEqual(first_query["page"], ["1"])
        self.assertEqual(first_query["size"], ["10"])
        self.assertEqual(first_query["type"], [str(ROOMKIT_VOICE_TYPE)])

        second_query = parse_qs(urlparse(opener.calls[2][0].full_url).query)
        self.assertEqual(second_query["page"], ["2"])
        self.assertEqual(second_query["size"], ["20"])

        public = adapter.public_status()
        self.assertTrue(public["connected"])
        self.assertTrue(public["has_im_token"])
        self.assertEqual(public["room_user_id"], "room-user-42")
        self.assertNotIn("authorization", public)
        self.assertNotIn("im_token", public)

    def test_roomkit_credentials_are_isolated_and_uid_change_reauthenticates(self) -> None:
        room_payload = {"code": 10000, "msg": "success", "data": {"rooms": [], "images": []}}

        def login_payload(auth: str, room_uid: str):
            return {
                "code": 10000,
                "msg": "success",
                "data": {
                    "authorization": auth,
                    "imToken": f"im-{room_uid}",
                    "userId": room_uid,
                },
            }

        app_a = logged_in_app()
        app_b = logged_in_app()
        app_b.session.uid = "77"
        app_b.session.nickname = "另一用户"
        opener_a = SequenceOpener(
            FakeResponse(login_payload("auth-A", "room-42")),
            FakeResponse(room_payload),
            FakeResponse(login_payload("auth-C", "room-43")),
            FakeResponse(room_payload),
        )
        opener_b = SequenceOpener(
            FakeResponse(login_payload("auth-B", "room-77")),
            FakeResponse(room_payload),
        )
        adapter_a = RoomKitAdapter(app_a, opener=opener_a)
        adapter_b = RoomKitAdapter(app_b, opener=opener_b)

        self.assertTrue(adapter_a.rooms().ok)
        self.assertTrue(adapter_b.rooms().ok)
        headers_a = {key.lower(): value for key, value in opener_a.calls[1][0].header_items()}
        headers_b = {key.lower(): value for key, value in opener_b.calls[1][0].header_items()}
        self.assertEqual(headers_a["authorization"], "auth-A")
        self.assertEqual(headers_b["authorization"], "auth-B")

        app_a.session.uid = "43"
        app_a.session.nickname = "切换用户"
        self.assertTrue(adapter_a.rooms().ok)
        relogin_form = parse_qs(opener_a.calls[2][0].data.decode("utf-8"))
        relist_headers = {key.lower(): value for key, value in opener_a.calls[3][0].header_items()}
        self.assertEqual(relogin_form["mobile"], ["43"])
        self.assertEqual(relist_headers["authorization"], "auth-C")

    def test_missing_roomkit_authorization_stops_before_list_request(self) -> None:
        missing_auth = {
            "code": 10000,
            "msg": "success",
            "data": {"userId": "room-user-42", "imToken": "token-only"},
        }
        opener = SequenceOpener(FakeResponse(missing_auth), FakeResponse(missing_auth))
        adapter = RoomKitAdapter(logged_in_app(), opener=opener)

        first = adapter.rooms()
        second = adapter.rooms()

        self.assertFalse(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(first.code, "ROOMKIT_AUTH_MISSING")
        self.assertEqual(second.code, "ROOMKIT_AUTH_MISSING")
        self.assertEqual(len(opener.calls), 2)
        self.assertFalse(adapter.public_status()["connected"])

    def test_logged_out_session_does_not_call_roomkit(self) -> None:
        opener = SequenceOpener()
        adapter = RoomKitAdapter(BeibeiwuApp(Session()), opener=opener)

        result = adapter.rooms()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "ROOMKIT_LOGIN_REQUIRED")
        self.assertEqual(opener.calls, [])

    def test_native_bundle_status_only_contains_public_roomkit_state(self) -> None:
        status = logged_in_app().native.status()["roomkit"]

        self.assertFalse(status["connected"])
        self.assertEqual(status["banghua_uid"], "42")
        self.assertNotIn("authorization", status)
        self.assertNotIn("im_token", status)

    def test_transport_exception_is_sanitized(self) -> None:
        class FailingOpener:
            def __call__(self, _req, timeout=None):
                raise OSError(f"connection failed with secret {ROOMKIT_BUSINESS_TOKEN}")

        adapter = RoomKitAdapter(logged_in_app(), opener=FailingOpener())

        result = adapter.rooms()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "ROOMKIT_TRANSPORT_ERROR")
        self.assertEqual(result.message, "房间服务连接失败")
        self.assertNotIn(ROOMKIT_BUSINESS_TOKEN, result.raw)
        self.assertNotIn(ROOMKIT_BUSINESS_TOKEN, result.message)


if __name__ == "__main__":
    unittest.main()
