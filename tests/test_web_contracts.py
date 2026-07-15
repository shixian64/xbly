from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bbw_protocol.client import ApiResult
from bbw_protocol.adapters.im import ImAdapter
from bbw_protocol.modules.misc import MiscAPI
from bbw_web import bff_server
from bbw_web.bff_server import R, RE
from bbw_web.store import WebUser, _session_path


class BffEnvelopeTests(unittest.TestCase):
    def test_scalar_value_is_preserved_for_product_endpoints(self) -> None:
        result = ApiResult(True, 200, "20978", data=20978, kind="json_other")
        payload = R(result, include_value=True)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["value"], 20978)
        self.assertEqual(payload["value_type"], "number")

    def test_domain_entity_is_not_coerced_to_user(self) -> None:
        result = ApiResult(
            True,
            200,
            "",
            data={"rooms": [{"roomId": "r1", "roomName": "晚安房", "online": 4}]},
        )
        payload = RE(result, "room")
        self.assertEqual(payload["items"][0]["name"], "晚安房")
        self.assertEqual(payload["items"][0]["online_count"], 4)
        self.assertNotIn("nickname", payload["items"][0])

    def test_tuijian_slide_uses_title_instead_of_fake_username(self) -> None:
        result = ApiResult(
            True,
            200,
            "",
            data=[
                {
                    "id": "57",
                    "slidesort": "推荐",
                    "slidename": "乐园招聘",
                    "slidepicture": "images/banner.jpg",
                    "slideurl": "https://example.invalid/article",
                }
            ],
        )
        payload = RE(result, "slide")
        self.assertEqual(payload["entity"], "slide")
        self.assertEqual(payload["items"][0]["title"], "乐园招聘")
        self.assertEqual(payload["items"][0]["image"], "images/banner.jpg")
        self.assertNotIn("nickname", payload["items"][0])

    def test_sms_empty_transport_can_be_explicitly_accepted(self) -> None:
        result = ApiResult(
            False,
            200,
            "",
            data=None,
            code="EMPTY_RESPONSE",
            message="empty response",
            kind="empty",
        )
        payload = R(result, empty_ok=True)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])


class SecurityHelperTests(unittest.TestCase):
    def test_session_path_is_confined(self) -> None:
        self.assertIsNotNone(_session_path("726285"))
        self.assertIsNone(_session_path("../../outside"))
        self.assertIsNone(_session_path("a/b"))

    def test_phone_like_public_label_is_masked(self) -> None:
        app = SimpleNamespace(
            whoami=lambda: {
                "phone": "13800138000",
                "uid": "42",
                "logged_in": True,
            }
        )
        user = WebUser(
            web_sid="sid",
            app=app,
            native=SimpleNamespace(),
            label="13800138000",
        )
        public = user.public()
        self.assertEqual(public["label"], "138****8000")
        self.assertEqual(public["user"]["phone"], "138****8000")

    def test_tim_local_signing_is_lab_only(self) -> None:
        with patch.object(bff_server, "LAB_ENABLED", False):
            self.assertEqual(bff_server._tim_preference("local"), "server")
        with patch.object(bff_server, "LAB_ENABLED", True):
            self.assertEqual(bff_server._tim_preference("local"), "local")

    def test_product_tim_can_disable_local_signing_fallback(self) -> None:
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42", user_sign=""),
            im=SimpleNamespace(
                tencent_sign=lambda _uid: ApiResult(
                    False,
                    200,
                    "",
                    data={},
                    code="EMPTY_RESPONSE",
                )
            ),
        )
        adapter = ImAdapter(app)
        with self.assertRaisesRegex(RuntimeError, "usable TIM UserSig"):
            adapter.tim_login_payload(
                prefer="server",
                allow_local_fallback=False,
            )


class ProtocolRoutingTests(unittest.TestCase):
    def test_id2_meta_uses_i888_route(self) -> None:
        calls = []

        class FakeClient:
            def call_i888(self, action, params):
                calls.append((action, params))
                return SimpleNamespace(ok=True)

        MiscAPI(FakeClient()).id2_meta_verify(cert_no="demo")
        self.assertEqual(calls, [("Id2MetaVerifyRequest", {"cert_no": "demo"})])


if __name__ == "__main__":
    unittest.main()
