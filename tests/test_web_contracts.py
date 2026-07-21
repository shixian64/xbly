from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from bbw_protocol.client import ApiResult
from bbw_protocol.adapters.im import ImAdapter
from bbw_protocol.modules.misc import MiscAPI
from bbw_protocol.modules.match import MatchAPI
from bbw_protocol.modules.social import SocialAPI
from bbw_protocol.session import Session
from bbw_web import bff_server
from bbw_web.bff_server import (
    R,
    RE,
    RM,
    conversation_envelope,
    empty_list_envelope,
    room_create_envelope,
    roomkit_list_envelope,
    room_top_envelope,
    task_receive_envelope,
)
from bbw_web.normalize import (
    explain_error,
    normalize_comments,
    normalize_messages,
    normalize_posts,
    resolve_media_url,
    session_user_dto,
)
from bbw_web.store import RequestGate, WebUser, _session_path


class RequestGateTests(unittest.TestCase):
    def test_reads_share_the_gate_and_writer_waits_for_all_readers(self) -> None:
        gate = RequestGate()
        first = gate.acquire_read()
        second = gate.acquire_read()
        writer_acquired = threading.Event()

        def write_request() -> None:
            lease = gate.acquire_write()
            writer_acquired.set()
            lease.release()

        thread = threading.Thread(target=write_request)
        thread.start()
        self.assertFalse(writer_acquired.wait(0.05))
        first.release()
        self.assertFalse(writer_acquired.wait(0.05))
        second.release()
        self.assertTrue(writer_acquired.wait(0.5))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())


class BffEnvelopeTests(unittest.TestCase):
    def test_local_web_server_rejects_a_second_listener_on_the_same_port(self) -> None:
        first = bff_server.ExclusiveThreadingHTTPServer(
            ("127.0.0.1", 0), bff_server.Handler
        )
        second = None
        try:
            address = first.server_address
            with self.assertRaises(OSError):
                second = bff_server.ExclusiveThreadingHTTPServer(address, bff_server.Handler)
        finally:
            if second is not None:
                second.server_close()
            first.server_close()

    def test_avatar_media_sentinels_are_not_treated_as_image_urls(self) -> None:
        for value in (
            "0",
            "NULL",
            "None",
            "nil",
            "false",
            "[]",
            "{}",
            "[object Object]",
        ):
            with self.subTest(value=value):
                self.assertEqual(resolve_media_url(value), "")
        self.assertEqual(resolve_media_url("data:audio/wav;base64,AA=="), "")
        self.assertEqual(
            resolve_media_url("data:image/png;base64,AA=="),
            "data:image/png;base64,AA==",
        )

    def test_apk_legacy_oss_urls_are_rewritten_to_the_canonical_media_host(self) -> None:
        suffix = "/video/202607/sample.mp4?quality=source#preview"
        for origin in (
            "https://moyuanoss.oss-cn-shanghai.aliyuncs.com",
            "http://appletattachment.oss-cn-beijing.aliyuncs.com",
            "//moyuanoss.oss-cn-shanghai.aliyuncs.com",
            "http://oss.banghua.xin",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(
                    resolve_media_url(origin + suffix),
                    "https://oss.banghua.xin" + suffix,
                )
        self.assertEqual(
            resolve_media_url("https://example.invalid/video.mp4"),
            "https://example.invalid/video.mp4",
        )
        self.assertEqual(
            resolve_media_url(
                "https://moyuanoss.oss-cn-shanghai.aliyuncs.com.evil/video.mp4"
            ),
            "https://moyuanoss.oss-cn-shanghai.aliyuncs.com.evil/video.mp4",
        )

    def test_logged_in_user_avatar_is_exposed_to_the_web_client(self) -> None:
        session = Session(
            uid="42",
            token="token",
            nickname="Me",
            portrait="/images/users/me.jpg",
        )
        dto = session_user_dto(session.summary())

        self.assertEqual(dto["avatar"], "https://oss.banghua.xin/images/users/me.jpg")
        self.assertEqual(dto["portrait"], dto["avatar"])

        web_user = WebUser(
            web_sid="sid",
            app=SimpleNamespace(whoami=session.summary),
            native=SimpleNamespace(),
        )
        self.assertEqual(web_user.public()["user"]["avatar"], "/images/users/me.jpg")

        session.update_from_user({"id": "42", "token": "token", "avatar": "images/new.jpg"})
        self.assertEqual(session.portrait, "images/new.jpg")
        session.update_from_user({"id": "42", "token": "token"})
        self.assertEqual(session.portrait, "images/new.jpg")

    def test_profile_avatar_fills_missing_login_avatar(self) -> None:
        dto = session_user_dto(
            {"uid": "42", "nickname": "Me", "logged_in": True},
            {"id": "42", "nickname": "Me", "portrait": "images/users/me.jpg"},
        )
        self.assertEqual(dto["avatar"], "https://oss.banghua.xin/images/users/me.jpg")

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

    def test_room_top_false_is_an_empty_recommendation_list(self) -> None:
        result = ApiResult(
            False,
            200,
            "false",
            data=False,
            code="FALSE_RESPONSE",
            message="service returned false",
            kind="json_other",
        )

        payload = room_top_envelope(result)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["code"], "")
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["entity"], "room")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["list"], [])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["availability"], "empty")

    def test_social_list_false_is_an_empty_list(self) -> None:
        result = ApiResult(
            False,
            200,
            "false",
            data=False,
            code="FALSE_RESPONSE",
            message="service returned false",
            kind="json_other",
        )

        payload = empty_list_envelope(result, R(result), "关注列表为空")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["availability"], "empty")

    def test_social_list_transport_failure_is_not_hidden(self) -> None:
        result = ApiResult(
            False,
            502,
            "false",
            data=False,
            code="FALSE_RESPONSE",
            message="service returned false",
            kind="json_other",
        )

        payload = empty_list_envelope(result, R(result), "关注列表为空")

        self.assertFalse(payload["ok"])

    def test_room_top_transport_error_is_not_hidden_as_empty(self) -> None:
        result = ApiResult(
            False,
            502,
            "false",
            data=False,
            code="FALSE_RESPONSE",
            message="service returned false",
            kind="json_other",
        )

        payload = room_top_envelope(result)

        self.assertFalse(payload["ok"])
        self.assertIsNotNone(payload["error"])

    def test_room_create_no_has_a_room_specific_rejection(self) -> None:
        result = ApiResult(
            False,
            200,
            "no",
            data="no",
            code="FALSE_RESPONSE",
            message="no",
            kind="text",
        )

        payload = room_create_envelope(result)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "ROOM_CREATE_UNAVAILABLE")
        self.assertEqual(payload["outcome"], "rejected")
        self.assertEqual(payload["upstream_code"], "FALSE_RESPONSE")
        self.assertEqual(payload["error"]["title"], "暂时无法创建语音房")
        self.assertIn("服务端未开放", payload["error"]["detail"])

    def test_roomkit_list_envelope_exposes_rooms_but_not_credentials(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"authorization":"must-not-leak"}',
            data={
                "rooms": [
                    {
                        "roomId": "r1",
                        "roomName": "晚安语音房",
                        "themePictureUrl": "https://example.invalid/room.jpg",
                        "createUser": {
                            "userId": "u1",
                            "userName": "房主",
                            "portrait": "https://example.invalid/owner.jpg",
                        },
                        "userTotal": 3,
                        "isPrivate": 1,
                    }
                ]
            },
            code="10000",
            message="success",
            kind="roomkit_json",
        )

        payload = roomkit_list_envelope(
            result,
            {
                "connected": True,
                "room_user_id": "room-u1",
                "has_im_token": True,
            },
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "roomkit")
        self.assertEqual(payload["items"][0]["id"], "r1")
        self.assertEqual(payload["items"][0]["owner_name"], "房主")
        self.assertEqual(payload["items"][0]["online_count"], 3)
        self.assertTrue(payload["items"][0]["is_private"])
        self.assertNotIn("authorization", str(payload).lower())
        self.assertNotIn("im_token", payload["roomkit_session"])

    def test_roomkit_failure_is_mapped_without_upstream_detail_leak(self) -> None:
        result = ApiResult(
            False,
            -1,
            "secret transport detail",
            code="ROOMKIT_TRANSPORT_ERROR",
            message="房间服务连接失败",
            kind="roomkit_error",
        )

        payload = roomkit_list_envelope(result, {"connected": False})

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "ROOMKIT_UNAVAILABLE")
        self.assertEqual(payload["upstream_code"], "ROOMKIT_TRANSPORT_ERROR")
        self.assertNotIn("secret transport detail", str(payload))

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
        self.assertEqual(
            payload["items"][0]["image"],
            "https://oss.banghua.xin/images/banner.jpg",
        )
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

    def test_empty_response_is_not_mislabeled_as_a_definite_failure(self) -> None:
        error = explain_error(
            code="EMPTY_RESPONSE",
            message="empty response; business outcome is unknown",
        )
        self.assertEqual(error["title"], "结果待确认")
        self.assertIn("确认", error["detail"])

    def test_task_receive_empty_response_is_verified_from_refreshed_state(self) -> None:
        result = ApiResult(
            False,
            200,
            "",
            code="EMPTY_RESPONSE",
            message="empty response; business outcome is unknown",
            kind="empty",
        )
        claimed = task_receive_envelope(
            result,
            "1",
            [{"id": "1", "is_claimed": True, "can_receive": False}],
        )
        self.assertTrue(claimed["ok"])
        self.assertEqual(claimed["outcome"], "claimed")

        unconfirmed = task_receive_envelope(
            result,
            "1",
            [{"id": "1", "is_claimed": False, "can_receive": True}],
        )
        self.assertFalse(unconfirmed["ok"])
        self.assertEqual(unconfirmed["outcome"], "unknown")
        self.assertEqual(unconfirmed["error"]["title"], "领取结果待确认")

    def test_task_claim_frontend_updates_only_the_claimed_card(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "bbw_web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function applyTaskClaimSuccess(button, data)", app_js)
        self.assertIn('card.outerHTML = taskCard({', app_js)
        self.assertIn('clearViewCacheKey("tasks")', app_js)
        self.assertNotIn('if (toastEnv(data, "领取请求已提交")) go("tasks", { force: true });', app_js)

    def test_conversation_entity_has_stable_summary_fields(self) -> None:
        result = ApiResult(
            True,
            200,
            "",
            data=[
                {
                    "content": "你好",
                    "msgTimestamp": "1710000000",
                    "unreadCount": "3",
                    "userInfoList": {"id": "9", "nickname": "N"},
                }
            ],
        )
        payload = RE(result, "conversation")
        self.assertEqual(payload["entity"], "conversation")
        self.assertEqual(payload["items"][0]["peer_id"], "9")
        self.assertEqual(payload["items"][0]["last_message"], "你好")
        self.assertEqual(payload["items"][0]["unread_count"], 3)

    def test_conversation_list_does_not_fetch_missing_peer_profiles(self) -> None:
        calls = []

        def get_user(uid: str) -> ApiResult:
            calls.append(uid)
            return ApiResult(
                True,
                200,
                "",
                data={
                    "userInfoList": {
                        "id": uid,
                        "nickname": "头像用户",
                        "portrait": "images/users/avatar.jpg",
                    }
                },
            )

        app = SimpleNamespace(profile=SimpleNamespace(get_user=get_user))
        result = ApiResult(
            True,
            200,
            "",
            data=[
                {
                    "conversation_user": "9",
                    "content": "你好",
                    "msgTimestamp": "1710000000",
                    "userInfoList": {"id": "42", "nickname": "当前用户"},
                }
            ],
        )
        item = conversation_envelope(app, result, {})["items"][0]
        self.assertEqual(item["peer_id"], "9")
        self.assertEqual(item["avatar"], "")
        self.assertEqual(item["nickname"], "9")
        self.assertEqual(item["preview_timestamp"], "")
        self.assertFalse(item["preview_authoritative"])
        self.assertTrue(item["preview_timestamp_inferred"])
        bff_server._attach_cached_conversation_summaries([item], {})
        self.assertTrue(item["preview_stale"])
        self.assertEqual(calls, [])

    def test_conversation_list_uses_fresh_cached_peer_profile(self) -> None:
        calls = []

        def get_user(uid: str) -> ApiResult:
            calls.append(uid)
            raise AssertionError("conversation summary must not fetch profiles")

        app = SimpleNamespace(profile=SimpleNamespace(get_user=get_user))
        result = ApiResult(
            True,
            200,
            "",
            data=[
                {
                    "conversation_user": "9",
                    "content": "你好",
                    "userInfoList": {"id": "42", "nickname": "当前用户"},
                }
            ],
        )
        cache = {
            "9": (
                bff_server.time.monotonic(),
                {
                    "id": "9",
                    "nickname": "头像用户",
                    "avatar": "https://oss.banghua.xin/images/users/avatar.jpg",
                },
            )
        }

        item = conversation_envelope(app, result, cache)["items"][0]

        self.assertEqual(item["avatar"], "https://oss.banghua.xin/images/users/avatar.jpg")
        self.assertEqual(item["nickname"], "头像用户")
        self.assertEqual(calls, [])

    def test_conversation_list_uses_cached_nickname_when_avatar_already_exists(self) -> None:
        app = SimpleNamespace(profile=SimpleNamespace(get_user=lambda uid: None))
        item = {
            "peer_id": "9",
            "nickname": "9",
            "avatar": "https://oss.banghua.xin/images/users/existing.jpg",
        }
        cache = {
            "9": (
                bff_server.time.monotonic(),
                {
                    "id": "9",
                    "nickname": "真实昵称",
                    "avatar": "https://oss.banghua.xin/images/users/cached.jpg",
                },
            )
        }

        bff_server._attach_cached_conversation_profiles(app, [item], cache)

        self.assertEqual(item["avatar"], "https://oss.banghua.xin/images/users/existing.jpg")
        self.assertEqual(item["nickname"], "真实昵称")

    def test_archived_conversation_local_profiles_supply_public_name_and_avatar(self) -> None:
        from bbw_web import archive_api

        class Result:
            @staticmethod
            def all():
                return [
                    (
                        "9",
                        "9",
                        {"nickname": "真实昵称"},
                        {"portrait": "images/users/local.jpg"},
                    ),
                    ("10", "10", {}, {}),
                ]

        class Db:
            statement = None

            def execute(self, statement):
                self.statement = statement
                return Result()

        db = Db()
        profiles = archive_api._local_public_profile_map(db, ["9", "9", "10"])

        self.assertEqual(
            profiles["9"],
            {
                "id": "9",
                "nickname": "真实昵称",
                "avatar": "images/users/local.jpg",
                "portrait": "images/users/local.jpg",
            },
        )
        self.assertNotIn("10", profiles)
        self.assertIn("beibeiwu", {str(value) for value in db.statement.compile().params.values()})

    def test_message_normalization_keeps_revoke_identity_and_state(self) -> None:
        messages = normalize_messages(
            [
                {
                    "MsgKey": "message-key",
                    "From_Account": "42",
                    "To_Account": "9",
                    "isRevoked": 1,
                }
            ]
        )

        self.assertEqual(messages[0]["id"], "message-key")
        self.assertEqual(messages[0]["msg_key"], "message-key")
        self.assertTrue(messages[0]["is_revoked"])

    def test_dynamic_post_and_comment_are_normalized(self) -> None:
        posts = normalize_posts(
            [
                {
                    "id": "p1",
                    "authid": "42",
                    "authnickname": "Me",
                    "authportrait": "/images/me.jpg",
                    "posttext": "第一行\\n第二行",
                    "postpicture": "images/a.jpg,images/b.jpg",
                    "postvideo": "https://moyuanoss.oss-cn-shanghai.aliyuncs.com/video/p1.mp4",
                    "cover": "https://appletattachment.oss-cn-beijing.aliyuncs.com/images/p1.jpg",
                    "like": "3",
                    "comment_sum": "2",
                    "topic": '[{"topic":"旅行"}]',
                    "u_top": 1,
                }
            ]
        )
        self.assertEqual(posts[0]["content"], "第一行\n第二行")
        self.assertEqual(posts[0]["pictures"][0], "https://oss.banghua.xin/images/a.jpg")
        self.assertEqual(posts[0]["video"], "https://oss.banghua.xin/video/p1.mp4")
        self.assertEqual(posts[0]["cover"], "https://oss.banghua.xin/images/p1.jpg")
        self.assertEqual(posts[0]["topics"], ["旅行"])
        self.assertTrue(posts[0]["is_pinned"])

        comments = normalize_comments(
            [{"id": "c1", "authid": "9", "nickname": "N", "comment_text": "你好", "like": "4"}]
        )
        self.assertEqual(comments[0]["content"], "你好")
        self.assertEqual(comments[0]["like_count"], 4)

    def test_dynamic_envelope_marks_current_user_and_treats_false_as_empty(self) -> None:
        result = ApiResult(
            True,
            200,
            '[{"id":"p1","authid":"42","posttext":"x"}]',
            data=[{"id": "p1", "authid": "42", "posttext": "x"}],
        )
        payload = RM(result, "post", "42")
        self.assertTrue(payload["items"][0]["is_self"])

        empty = RM(ApiResult(False, 200, "false", data=False), "post", "42")
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["items"], [])

    def test_tim_presence_result_is_normalized_for_product_ui(self) -> None:
        result = SimpleNamespace(
            ok=True,
            data={
                "QueryResult": [
                    {"To_Account": "9", "Status": "Online"},
                    {"To_Account": "10", "Status": "Offline"},
                ]
            },
        )
        payload = bff_server._normalize_presence_result(result, ["9", "10", "11"])

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["label"], "在线")
        self.assertEqual(payload["items"][1]["label"], "离线")
        self.assertEqual(payload["items"][2]["status"], "unknown")
        self.assertEqual(bff_server._presence_uids(["9,10", "10,11"]), ["9", "10", "11"])


class SecurityHelperTests(unittest.TestCase):
    def test_session_path_is_confined(self) -> None:
        self.assertIsNotNone(_session_path("10001"))
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

    def test_tim_local_signing_can_be_selected_by_the_bff(self) -> None:
        with patch.object(bff_server, "LAB_ENABLED", False):
            self.assertEqual(bff_server._tim_preference("local"), "local")
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

    def test_rong_credentials_accept_apk_user_id_casing_in_response(self) -> None:
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42", nickname="Me", portrait="me.jpg"),
            im=SimpleNamespace(
                rong_register=lambda **_kwargs: ApiResult(
                    True,
                    200,
                    '{"data":{"token":"rong-token","userID":"42"}}',
                    data={"data": {"token": "rong-token", "userID": "42"}},
                )
            ),
        )

        credentials = ImAdapter(app).rong_register()

        self.assertTrue(credentials.ok)
        self.assertEqual(credentials.user_id, "42")
        self.assertEqual(credentials.token, "rong-token")


class ProtocolRoutingTests(unittest.TestCase):
    def test_id2_meta_uses_i888_route(self) -> None:
        calls = []

        class FakeClient:
            def call_i888(self, action, params):
                calls.append((action, params))
                return SimpleNamespace(ok=True)

        MiscAPI(FakeClient()).id2_meta_verify(cert_no="demo")
        self.assertEqual(calls, [("Id2MetaVerifyRequest", {"cert_no": "demo"})])

    def test_dynamic_feed_uses_current_apk_tenant_and_parameters(self) -> None:
        calls = []

        class FakeClient:
            session = SimpleNamespace(uid="42")

            def url(self, action, i="999999", **_kwargs):
                return f"{i}:{action}"

            def request(self, url, body):
                calls.append((url, body))
                return SimpleNamespace(ok=True)

        SocialAPI(FakeClient()).posts("附近", "1", filter_region="上海")
        self.assertEqual(calls[0][0], "99999:Luntan0")
        self.assertEqual(calls[0][1]["start"], "1")
        self.assertEqual(calls[0][1]["platename"], "同城")
        self.assertEqual(calls[0][1]["pageindex"], "1")
        self.assertEqual(calls[0][1]["filter_region"], "上海")

        SocialAPI(FakeClient()).posts("最新", "1")
        self.assertEqual(calls[1][0], "99999:Luntan0")
        self.assertEqual(calls[1][1]["start"], "1")
        self.assertEqual(calls[1][1]["platename"], "首页")

        SocialAPI(FakeClient()).posts("附近", "99", filter_region="上海")
        self.assertEqual(calls[2][0], "99999:Luntan0")
        self.assertEqual(calls[2][1]["start"], "0")
        self.assertEqual(calls[2][1]["pageindex"], "99")

        SocialAPI(FakeClient()).posts("推荐", "1")
        self.assertEqual(calls[3][0], "999999:luntannewnewnew")
        self.assertNotIn("start", calls[3][1])
        self.assertEqual(calls[3][1]["platename"], "精华")

        SocialAPI(FakeClient()).user_posts("42", "2")
        self.assertEqual(calls[4][0], "99999:someonesluntannew")
        self.assertEqual(
            calls[4][1],
            {"myid": "42", "authid": "42", "pageindex": "2"},
        )

        SocialAPI(FakeClient()).profile_posts("9", "3")
        self.assertEqual(calls[5][0], "999999:someonesluntannew")
        self.assertEqual(
            calls[5][1],
            {"myid": "42", "authid": "9", "pageindex": "3"},
        )

        SocialAPI(FakeClient()).record_post_view("123")
        self.assertEqual(calls[6][0], "99999:luntanStatistic")
        self.assertEqual(
            calls[6][1],
            {"uid": "42", "type": "pv", "postId": "123"},
        )

        SocialAPI(FakeClient()).posts("招募令", "123456789012345678")
        self.assertEqual(calls[7][0], "999999:luntannewnewnew")
        self.assertNotIn("start", calls[7][1])
        self.assertEqual(calls[7][1]["platename"], "招募令")
        self.assertEqual(calls[7][1]["pageindex"], "123456789012345678")

        SocialAPI(FakeClient()).posts("关注", "987654321012345678")
        self.assertEqual(calls[8][0], "999999:luntannewnewnew")
        self.assertNotIn("start", calls[8][1])
        self.assertEqual(calls[8][1]["platename"], "关注")
        self.assertEqual(calls[8][1]["pageindex"], "987654321012345678")


class TimRestHistoryEnvelopeTests(unittest.TestCase):
    def test_recent_contacts_filter_non_c2c_self_and_duplicates(self) -> None:
        calls = []

        class Client:
            def recent_contacts(self, account_uid, **kwargs):
                calls.append((account_uid, kwargs))
                return SimpleNamespace(
                    ok=True,
                    data={
                        "SessionItem": [
                            {"Type": 1, "To_Account": "9", "MsgTime": 20, "MsgSeq": 2},
                            {"Type": 1, "To_Account": "9", "MsgTime": 10, "MsgSeq": 1},
                            {"Type": 1, "To_Account": "10", "MsgTime": 30, "MsgSeq": 3},
                            {"Type": 1, "To_Account": "42", "MsgTime": 30},
                            {"Type": 2, "To_Account": "group", "MsgTime": 40},
                        ],
                        "CompleteFlag": 1,
                    },
                )

            def c2c_unread_counts(self, _account_uid, peers):
                return SimpleNamespace(
                    ok=True,
                    data={
                        "C2CUnreadMsgNumList": [
                            {
                                "Peer_Account": peer,
                                "C2CUnreadMsgNum": 2 if peer == "10" else 0,
                            }
                            for peer in peers
                        ]
                    },
                )

        payload = bff_server._tim_recent_conversation_envelope(
            SimpleNamespace(), Client(), "42", {}
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [item["peer_id"] for item in payload["items"]],
            ["10", "9"],
        )
        self.assertEqual(
            [item["unread_count"] for item in payload["items"]],
            [2, 0],
        )
        self.assertTrue(all(item["unread_authoritative"] for item in payload["items"]))
        self.assertTrue(all(item["unread_observed_at"] for item in payload["items"]))
        self.assertEqual(payload["items"][0]["source"], "tim_rest")

        bff_server._attach_cached_conversation_summaries(
            payload["items"],
            {
                "10": {
                    "last_message": "cached preview",
                    "preview_timestamp": "1970-01-01T00:00:30+00:00",
                    "preview_sequence": "3",
                    "preview_source": "archive",
                    "preview_authoritative": True,
                }
            },
        )
        self.assertEqual(payload["items"][0]["last_message"], "cached preview")
        self.assertFalse(payload["items"][0]["preview_stale"])

        stale_items = [
            {
                "peer_id": "10",
                "timestamp": 30,
                "activity_sequence": 4,
                "source": "tim_rest",
            }
        ]
        bff_server._attach_cached_conversation_summaries(
            stale_items,
            {
                "10": {
                    "last_message": "previous preview",
                    "preview_timestamp": 30,
                    "preview_sequence": 3,
                    "preview_source": "archive",
                    "preview_authoritative": True,
                }
            },
        )
        self.assertTrue(stale_items[0]["preview_stale"])

        one_second_old = [{"peer_id": "10", "timestamp": 30, "source": "tim_rest"}]
        bff_server._attach_cached_conversation_summaries(
            one_second_old,
            {
                "10": {
                    "last_message": "one second old",
                    "preview_timestamp": 29,
                    "preview_source": "archive",
                    "preview_authoritative": True,
                }
            },
        )
        self.assertTrue(one_second_old[0]["preview_stale"])

    def test_roaming_history_merges_deduplicates_and_sorts_both_directions(self) -> None:
        calls = []

        class Client:
            def roaming_messages(self, sender, recipient, **kwargs):
                calls.append((sender, recipient, kwargs))
                if sender == "9":
                    rows = [
                        {
                            "From_Account": "9",
                            "To_Account": "42",
                            "MsgTimeStamp": 20,
                            "MsgKey": "incoming",
                            "MsgBody": [
                                {
                                    "MsgType": "TIMTextElem",
                                    "MsgContent": {"Text": "incoming"},
                                }
                            ],
                        }
                    ]
                else:
                    rows = [
                        {
                            "From_Account": "42",
                            "To_Account": "9",
                            "MsgTimeStamp": 10,
                            "MsgKey": "outgoing",
                            "MsgBody": [
                                {
                                    "MsgType": "TIMTextElem",
                                    "MsgContent": {"Text": "outgoing"},
                                }
                            ],
                        },
                        {
                            "From_Account": "9",
                            "To_Account": "42",
                            "MsgTimeStamp": 20,
                            "MsgKey": "incoming",
                            "MsgBody": [
                                {
                                    "MsgType": "TIMTextElem",
                                    "MsgContent": {"Text": "incoming"},
                                }
                            ],
                        },
                        {
                            "From_Account": "42",
                            "To_Account": "9",
                            "MsgTimeStamp": 30,
                            "MsgKey": "outgoing-new",
                            "MsgBody": [
                                {
                                    "MsgType": "TIMTextElem",
                                    "MsgContent": {"Text": "outgoing-new"},
                                }
                            ],
                        },
                    ]
                return SimpleNamespace(
                    ok=True,
                    data={"MsgList": rows, "Complete": 1},
                )

            def c2c_unread_counts(self, _account_uid, peers):
                return SimpleNamespace(
                    ok=True,
                    data={
                        "C2CUnreadMsgNumList": [
                            {"Peer_Account": peer, "C2CUnreadMsgNum": 1}
                            for peer in peers
                        ]
                    },
                )

        payload = bff_server._tim_roaming_message_envelope(Client(), "42", "9")

        self.assertEqual([(call[0], call[1]) for call in calls], [("9", "42"), ("42", "9")])
        self.assertEqual(
            [item["msg_key"] for item in payload["items"]],
            ["outgoing", "incoming", "outgoing-new"],
        )
        self.assertTrue(payload["items"][0]["is_peer_read"])
        self.assertFalse(payload["items"][2]["is_peer_read"])
        self.assertEqual(payload["count"], 3)

    def test_roaming_summary_uses_narrow_activity_window(self) -> None:
        calls = []

        class Client:
            def roaming_messages(self, sender, recipient, **kwargs):
                calls.append((sender, recipient, kwargs))
                return SimpleNamespace(ok=True, data={"MsgList": [], "Complete": 1})

            def c2c_unread_counts(self, *_args, **_kwargs):
                raise AssertionError("summary preview must not query unread state")

        payload = bff_server._tim_roaming_message_envelope(
            Client(),
            "42",
            "9",
            max_messages=50,
            around_time=30,
            include_read_state=False,
        )

        self.assertEqual(payload["items"], [])
        self.assertEqual([(call[0], call[1]) for call in calls], [("9", "42"), ("42", "9")])
        self.assertTrue(all(call[2]["min_time"] == 25 for call in calls))
        self.assertTrue(all(call[2]["max_time"] == 35 for call in calls))

    def test_message_sort_uses_sequence_within_same_second(self) -> None:
        items = [
            {"timestamp": 30, "sequence": "9", "msg_key": "later"},
            {"timestamp": 30, "sequence": "8", "msg_key": "earlier"},
        ]
        ordered = sorted(items, key=bff_server._tim_message_sort_key)
        self.assertEqual([item["msg_key"] for item in ordered], ["earlier", "later"])


class SocialBffRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def _run_get(
        self,
        path: str,
        *,
        false_social: set[str] | None = None,
        im_result: ApiResult | None = None,
        authorized_peer: bool = True,
    ):
        result = im_result if im_result is not None else ApiResult(True, 200, "[]", data=[])
        false_result = ApiResult(
            False,
            200,
            "false",
            data=False,
            code="FALSE_RESPONSE",
            message="service returned false",
            kind="json_other",
        )
        false_actions = false_social or set()
        social_result = lambda action, fallback: false_result if action in false_actions else fallback
        calls = []
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            profile=SimpleNamespace(
                get_user=lambda uid, **_kwargs: calls.append(("profile", uid))
                or ApiResult(
                    True,
                    200,
                    "[]",
                    data=[{"id": str(uid), "nickname": f"用户 {uid}"}],
                )
            ),
            social=SimpleNamespace(
                friends=lambda: calls.append(("friends", None)) or social_result("friends", result),
                friend_apply_list=lambda page: calls.append(("friend_apply", page))
                or social_result("friend_apply", result),
                follow_users=lambda _uid, page: calls.append(("follow_users", page))
                or social_result(
                    "follow_users",
                    ApiResult(
                        True,
                        200,
                        "[]",
                        data=[{"you": "9", "yournickname": "关注用户"}],
                    ),
                ),
                follow_list=lambda _uid: calls.append(("follow_list", None))
                or social_result("follow_list", result),
                fans_users=lambda _uid, page: calls.append(("fans_users", page))
                or social_result(
                    "fans_users",
                    ApiResult(
                        True,
                        200,
                        "[]",
                        data=[{"fansid": "10", "fansnickname": "粉丝用户"}],
                    ),
                ),
                viewed_me=lambda page: calls.append(("seen_me", page))
                or social_result("seen_me", result),
                i_viewed=lambda page: calls.append(("seen_by_me", page))
                or social_result("seen_by_me", result),
                my_blacklist=lambda: calls.append(("blacklist", None))
                or social_result("blacklist", result),
                blacklist_me=lambda: calls.append(("blacklist_me", None))
                or social_result("blacklist_me", result),
            ),
            im=SimpleNamespace(
                history_conversations=lambda page: calls.append(("conversations", page))
                or result,
                history_messages=lambda peer: calls.append(("messages", peer)) or result,
            ),
        )
        web_user = SimpleNamespace(
            app=app,
            profile_cache={},
            conversation_message_peers={"9"} if authorized_peer else set(),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=time.monotonic(),
            blocked_by_message_peers_snapshot_at=time.monotonic(),
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    recent_contacts=lambda user_id, **_kwargs: calls.append(
                        ("recent_contacts", user_id)
                    )
                    or SimpleNamespace(
                        ok=True,
                        data={"SessionItem": [], "CompleteFlag": 1},
                    ),
                    roaming_messages=lambda from_id, to_id, **_kwargs: calls.append(
                        ("roaming", from_id, to_id)
                    )
                    or SimpleNamespace(
                        ok=im_result is None,
                        data={"MsgList": [], "Complete": 1},
                    ),
                    c2c_unread_counts=lambda _account_uid, peers: SimpleNamespace(
                        ok=True,
                        data={
                            "C2CUnreadMsgNumList": [
                                {"Peer_Account": peer, "C2CUnreadMsgNum": 0}
                                for peer in peers
                            ]
                        },
                    ),
                    query_online=lambda user_ids: calls.append(("presence", user_ids))
                    or SimpleNamespace(
                        ok=True,
                        data={
                            "QueryResult": [
                                {"To_Account": uid, "Status": "Online"}
                                for uid in user_ids
                            ]
                        },
                    )
                )
            ),
        )

        class Harness:
            def __init__(self):
                self.path = path
                self.response = None

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_GET(harness)
        return calls, harness.response

    def _run_visit_post(self, payload: dict):
        result = ApiResult(True, 200, "true", data=True)
        calls = []
        app = SimpleNamespace(
            social=SimpleNamespace(
                record_profile_view=lambda uid: calls.append(("visit", uid)) or result,
            )
        )
        web_user = SimpleNamespace(app=app)

        class Harness:
            path = "/api/social/visit"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return payload

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return calls, harness.response

    def _run_moment_view_post(self, payload: dict, app=None):
        calls = []
        if app is None:
            result = ApiResult(True, 200, "", data=None, kind="empty")
            app = SimpleNamespace(
                social=SimpleNamespace(
                    record_post_view=lambda postid: calls.append(("view", postid)) or result,
                )
            )
        web_user = SimpleNamespace(app=app)

        class Harness:
            path = "/api/moments/view"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return payload

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return calls, harness.response

    def _run_agree_post(self, payload: dict):
        calls = []
        accepted = set()
        application = {
            "id": "apply-1",
            "uid": "42",
            "yourid": "9",
            "yournickname": "申请人",
        }

        def agree_friend(value):
            calls.append(("agree", value))
            if value == "9":
                accepted.add("9")
            return ApiResult(True, 200, "T", data=True)

        def friends():
            calls.append(("friends", None))
            rows = [{"id": "relation-1", "uid": "42", "friendid": "9", "friendnickname": "申请人"}] if accepted else []
            return ApiResult(True, 200, "[]", data=rows)

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            social=SimpleNamespace(
                agree_friend=agree_friend,
                friends=friends,
                friend_apply_list=lambda page: calls.append(("friend_apply", page))
                or ApiResult(True, 200, "[]", data=[application]),
            ),
        )
        web_user = SimpleNamespace(app=app)

        class Harness:
            path = "/api/social/agree-friend"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return payload

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return calls, harness.response

    def test_social_and_conversation_routes(self) -> None:
        calls, response = self._run_get("/api/social/friends")
        self.assertEqual(calls, [("friends", None)])
        self.assertEqual(response[0], 200)

        calls, _ = self._run_get("/api/social/visitors?type=seen_me&page=0")
        self.assertEqual(calls, [("seen_me", "0")])

        calls, _ = self._run_get("/api/social/visitors?type=seen_by_me&page=2")
        self.assertEqual(calls, [("seen_by_me", "2")])

        calls, response = self._run_get("/api/im/conversations?page=3")
        self.assertEqual(calls, [("recent_contacts", "42")])
        self.assertEqual(response[1]["entity"], "conversation")

        calls, response = self._run_get("/api/im/messages?peer=9")
        self.assertEqual(
            calls,
            [("roaming", "9", "42"), ("roaming", "42", "9")],
        )
        self.assertEqual(response[1]["entity"], "message")

        calls, response = self._run_get("/api/im/presence?uids=9,10")
        self.assertEqual(calls, [("presence", ["9", "10"])])
        self.assertEqual([item["label"] for item in response[1]["items"]], ["在线", "在线"])

        calls, response = self._run_get("/api/profile/users?uids=9,10")
        self.assertCountEqual(calls, [("profile", "9"), ("profile", "10")])
        self.assertEqual(response[1]["count"], 2)

        calls, response = self._run_visit_post({"uid": "9"})
        self.assertEqual(calls, [("visit", "9")])
        self.assertEqual(response[0], 200)

        calls, response = self._run_get("/api/social/friend-apply?page=1")
        self.assertEqual(calls, [("friend_apply", "1")])
        self.assertEqual(response[1]["items"], [])

    def test_moment_view_route_only_accepts_numeric_post_ids(self) -> None:
        calls, response = self._run_moment_view_post({"post_id": "123"})
        self.assertEqual(calls, [("view", "123")])
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["post_id"], "123")

        calls, response = self._run_moment_view_post({"post_id": "abc"})
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 400)
        self.assertFalse(response[1]["ok"])

    def test_moment_view_route_completes_an_active_view_task_after_probe(self) -> None:
        progress = 94
        lock = threading.Lock()
        view_calls = []

        def record_post_view(postid):
            nonlocal progress
            with lock:
                progress = min(100, progress + 1)
                view_calls.append(postid)
            return ApiResult(False, 200, "", data=None, kind="empty")

        def call(action, **_kwargs):
            with lock:
                current = progress
            data = (
                [
                    {
                        "id": "view-task",
                        "name": "观看动态100条",
                        "progress": current,
                        "num": 100,
                        "available": "可领取" if current >= 100 else "未完成",
                    }
                ]
                if action == "createHotActivityList"
                else []
            )
            return ApiResult(True, 200, "[]", data=data)

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            social=SimpleNamespace(record_post_view=record_post_view),
            call=call,
        )
        _, response = self._run_moment_view_post(
            {"post_id": "123", "assist_task": True},
            app=app,
        )

        assist = response[1]["task_assist"]
        self.assertEqual(response[0], 200)
        self.assertEqual(view_calls, ["123"] * 6)
        self.assertTrue(assist["task_found"])
        self.assertTrue(assist["completed"])
        self.assertEqual(assist["remaining_before"], 5)
        self.assertEqual(assist["remaining_after"], 0)
        self.assertEqual(assist["successful_repeat_views"], 5)

    def test_moment_view_task_assist_retries_when_progress_verification_is_delayed(self) -> None:
        view_calls = []

        def record_post_view(postid):
            view_calls.append(postid)
            return ApiResult(False, 200, "", data=None, kind="empty")

        task = {
            "id": "view-task",
            "name": "观看动态100条",
            "progress": 10,
            "num": 100,
            "available": "未完成",
        }

        def call(action, **_kwargs):
            return ApiResult(
                True,
                200,
                "[]",
                data=[task] if action == "createHotActivityList" else [],
            )

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            social=SimpleNamespace(record_post_view=record_post_view),
            call=call,
        )
        with patch.object(bff_server.time, "sleep", return_value=None):
            _, response = self._run_moment_view_post(
                {"post_id": "123", "assist_task": True},
                app=app,
            )

        assist = response[1]["task_assist"]
        self.assertEqual(view_calls, ["123", "123"])
        self.assertFalse(assist["completed"])
        self.assertTrue(assist["retryable"])
        self.assertEqual(assist["state"], "verification_pending")
        self.assertEqual(assist["remaining_after"], 90)

    def test_moment_view_task_assist_does_not_batch_when_longest_task_stalls(self) -> None:
        view_calls = []
        short_progress = 0

        def record_post_view(postid):
            nonlocal short_progress
            view_calls.append(postid)
            if len(view_calls) >= 2:
                short_progress = 1
            return ApiResult(False, 200, "", data=None, kind="empty")

        def call(action, **_kwargs):
            data = []
            if action == "createHotActivityList":
                data = [
                    {
                        "id": "long-view-task",
                        "name": "观看动态100条",
                        "progress": 0,
                        "num": 100,
                        "available": "未完成",
                    },
                    {
                        "id": "short-view-task",
                        "name": "浏览动态1条",
                        "progress": short_progress,
                        "num": 1,
                        "available": "可领取" if short_progress else "未完成",
                    },
                ]
            return ApiResult(True, 200, "[]", data=data)

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            social=SimpleNamespace(record_post_view=record_post_view),
            call=call,
        )
        with patch.object(bff_server.time, "sleep", return_value=None):
            _, response = self._run_moment_view_post(
                {"post_id": "123", "assist_task": True},
                app=app,
            )

        assist = response[1]["task_assist"]
        self.assertEqual(view_calls, ["123", "123"])
        self.assertEqual(assist["successful_repeat_views"], 1)
        self.assertEqual(assist["progress_delta"], 0)
        self.assertEqual(assist["state"], "verification_pending")
        self.assertTrue(assist["retryable"])

    def test_moment_view_task_assist_failure_does_not_lose_the_initial_view(self) -> None:
        with patch.object(
            bff_server,
            "_assist_moment_view_task",
            side_effect=RuntimeError("assist unavailable"),
        ):
            calls, response = self._run_moment_view_post(
                {"post_id": "123", "assist_task": True}
            )

        self.assertEqual(calls, [("view", "123")])
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["task_assist"]["state"], "unavailable")
        self.assertTrue(response[1]["task_assist"]["retryable"])

    def test_moment_view_task_assist_does_not_start_after_its_deadline(self) -> None:
        calls = []
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            call=lambda action, **_kwargs: calls.append(action),
        )

        assist = bff_server._assist_moment_view_task(app, "123", deadline=0.0)

        self.assertEqual(calls, [])
        self.assertEqual(assist["state"], "deadline_reached")
        self.assertTrue(assist["retryable"])

    def test_moment_view_task_assist_accepts_successful_empty_fallback(self) -> None:
        calls = []

        def call(action, **_kwargs):
            calls.append(action)
            if action == "createHotActivityList":
                return ApiResult(False, 503, "unavailable", data=None)
            return ApiResult(True, 200, "[]", data=[])

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            call=call,
        )

        assist = bff_server._assist_moment_view_task(app, "123")

        self.assertCountEqual(
            calls,
            ["createHotActivityList", "haveHotActivityList"],
        )
        self.assertTrue(assist["checked"])
        self.assertFalse(assist["task_found"])
        self.assertFalse(assist["retryable"])
        self.assertEqual(assist["state"], "not_found")

    def test_friend_applications_scan_past_accepted_first_page(self) -> None:
        calls = []
        pages = {
            "1": [
                {
                    "id": f"accepted-{index}",
                    "uid": "42",
                    "friendid": f"friend-{index}",
                    "agree": "1",
                }
                for index in range(bff_server.FRIEND_APPLICATION_PAGE_SIZE)
            ],
            "2": [
                {
                    "id": "pending-1",
                    "uid": "42",
                    "friendid": "9",
                    "friendnickname": "申请人一",
                    "agree": "0",
                },
                {
                    "id": "pending-2",
                    "uid": "42",
                    "friendid": "10",
                    "friendnickname": "申请人二",
                    "agree": "0",
                },
            ],
        }

        def friend_apply_list(page):
            calls.append(page)
            rows = pages.get(page, [])
            return ApiResult(True, 200, json.dumps(rows, ensure_ascii=False), data=rows)

        app = SimpleNamespace(social=SimpleNamespace(friend_apply_list=friend_apply_list))
        result, items, metadata = bff_server._friend_applications(
            app,
            "42",
            start_page=1,
            scan_pages=2,
        )

        self.assertTrue(result.ok)
        self.assertEqual(calls, ["1", "2"])
        self.assertEqual(len(items), bff_server.FRIEND_APPLICATION_PAGE_SIZE + 2)
        self.assertEqual(items[0]["status"], "accepted")
        self.assertEqual([item["id"] for item in items[-2:]], ["9", "10"])
        self.assertEqual(metadata["count"], bff_server.FRIEND_APPLICATION_PAGE_SIZE + 2)
        self.assertEqual(metadata["accepted_count"], bff_server.FRIEND_APPLICATION_PAGE_SIZE)
        self.assertEqual(metadata["pending_incoming_count"], 2)
        self.assertFalse(metadata["has_more"])
        self.assertEqual(metadata["next_page"], "")

    def test_message_history_rejects_unexpected_html_response(self) -> None:
        html = "<!DOCTYPE html><html><body>not message json</body></html>"
        calls, response = self._run_get(
            "/api/im/messages?peer=9",
            im_result=ApiResult(
                True,
                200,
                html,
                data=html,
                message=html[:200],
                kind="text",
                headers={"content-type": "text/html; charset=utf-8"},
            ),
        )

        self.assertEqual(calls, [("roaming", "9", "42"), ("messages", "9")])
        self.assertEqual(response[0], 502)
        self.assertEqual(response[1]["code"], "UPSTREAM_HISTORY_UNAVAILABLE")
        self.assertEqual(response[1]["items"], [])
        self.assertNotIn("DOCTYPE", json.dumps(response[1]))

    def test_message_history_rejects_peer_without_friend_match_or_conversation(self) -> None:
        calls, response = self._run_get(
            "/api/im/messages?peer=9",
            authorized_peer=False,
        )

        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)
        self.assertEqual(response[1]["code"], "PRIVATE_MESSAGE_PERMISSION_REQUIRED")

    def test_presence_prefers_web_ttl_and_skips_blocked_tim_rest(self) -> None:
        backend = SimpleNamespace(
            read_web_presence=lambda uids: {"9"} if "9" in uids else set(),
            presence_rest_retry_after=lambda: 480,
            mark_presence_rest_unavailable=lambda _code: None,
        )
        previous = bff_server.PRESENCE_BACKEND
        bff_server.PRESENCE_BACKEND = backend
        try:
            calls, response = self._run_get("/api/im/presence?uids=9,10")
        finally:
            bff_server.PRESENCE_BACKEND = previous

        self.assertEqual(calls, [])
        self.assertEqual(response[1]["items"][0]["status"], "online")
        self.assertEqual(response[1]["items"][0]["source"], "web")
        self.assertEqual(response[1]["items"][1]["status"], "unknown")
        self.assertEqual(response[1]["retry_after"], 480)

    def test_false_social_lists_render_as_empty_instead_of_errors(self) -> None:
        calls, response = self._run_get(
            "/api/social/follows", false_social={"follow_users"}
        )
        self.assertEqual(calls, [("follow_users", "1")])
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["items"], [])

        calls, response = self._run_get(
            "/api/social/fans", false_social={"fans_users"}
        )
        self.assertEqual(calls, [("fans_users", "1")])
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["items"], [])

        calls, response = self._run_get(
            "/api/social/blacklist", false_social={"blacklist"}
        )
        self.assertEqual(calls, [("blacklist", None)])
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["items"], [])

    def test_social_summary_routes_skip_profile_enrichment_and_duplicate_friend_reads(self) -> None:
        calls, response = self._run_get("/api/social/follows?summary=1")
        self.assertEqual(calls, [("follow_users", "1")])
        self.assertEqual(response[1]["count"], 1)

        calls, response = self._run_get("/api/social/fans?summary=1")
        self.assertEqual(calls, [("fans_users", "1")])
        self.assertEqual(response[1]["count"], 1)

        calls, response = self._run_get("/api/social/friend-apply?page=1&summary=1")
        self.assertEqual(calls, [("friend_apply", "1")])
        self.assertEqual(response[1]["count"], 0)

    def test_friend_application_directions_and_statuses_are_preserved(self) -> None:
        applications = ApiResult(
            True,
            200,
            "[]",
            data=[
                {
                    "id": "incoming-1",
                    "myid": "42",
                    "yourid": "9",
                    "agree": "0",
                    "userInfoList": {"id": "9", "nickname": "申请人"},
                },
                {
                    "id": "outgoing-1",
                    "myid": "10",
                    "yourid": "42",
                    "agree": "0",
                    "userInfoList": {"id": "10", "nickname": "申请目标"},
                },
                {
                    "id": "incoming-accepted",
                    "myid": "42",
                    "yourid": "11",
                    "userInfoList": {"id": "11", "nickname": "已添加用户"},
                },
            ],
        )
        friends = ApiResult(
            True,
            200,
            "[]",
            data=[{"id": "relation-11", "uid": "42", "friendid": "11", "friendnickname": "已添加用户"}],
        )
        app = SimpleNamespace(
            social=SimpleNamespace(
                friend_apply_list=lambda _page: applications,
                friends=lambda: friends,
            )
        )

        _, items, metadata = bff_server._friend_applications(app, "42", applications)
        by_id = {item["id"]: item for item in items}
        self.assertEqual(by_id["9"]["direction"], "incoming")
        self.assertTrue(by_id["9"]["can_accept"])
        self.assertEqual(by_id["10"]["direction"], "outgoing")
        self.assertEqual(by_id["10"]["status_label"], "等待对方同意")
        self.assertEqual(by_id["11"]["status"], "accepted")
        self.assertFalse(by_id["11"]["can_accept"])
        counts = bff_server._friend_application_counts(items)
        self.assertEqual(counts["incoming_count"], 2)
        self.assertEqual(counts["outgoing_count"], 1)
        self.assertEqual(counts["pending_incoming_count"], 1)
        self.assertEqual(counts["accepted_count"], 1)
        self.assertEqual(metadata["count"], 3)
        summary = bff_server._friend_application_summary(metadata)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["total_count"], 3)

    def test_nearby_moments_use_profile_region_and_report_missing_location(self) -> None:
        def run(raw_user, profile_data=None):
            calls = []
            result = ApiResult(
                True,
                200,
                "[]",
                data=[{"id": "101", "authid": "9", "posttext": "nearby"}],
            )
            session = SimpleNamespace(uid="42", raw_user=raw_user, nickname="N", portrait="")
            app = SimpleNamespace(
                session=session,
                social=SimpleNamespace(
                    posts=lambda tab, cursor, **kwargs: calls.append((tab, cursor, kwargs)) or result,
                ),
                profile=SimpleNamespace(
                    get_me=lambda: ApiResult(True, 200, "{}", data=profile_data or {}),
                ),
                whoami=lambda: {
                    "uid": "42",
                    "nickname": "N",
                    "portrait": "",
                    "phone": "",
                    "logged_in": True,
                },
            )
            web_user = SimpleNamespace(app=app, persist=lambda: None)

            class Harness:
                path = "/api/moments/posts?tab=%E9%99%84%E8%BF%91&cursor=1"

                def __init__(self):
                    self.response = None

                def _check_api_origin(self):
                    return True

                def sid(self):
                    return "sid"

                def user(self, _sid):
                    return web_user

                def ok(self, obj, status=200, **_kwargs):
                    self.response = (status, obj)
                    return self.response

            harness = Harness()
            bff_server.Handler.do_GET(harness)
            return calls, harness.response

        calls, response = run({"id": "42", "region": "上海-上海"})
        self.assertEqual(calls[0][2]["filter_region"], "上海-上海")
        self.assertEqual(response[1]["location_region"], "上海-上海")
        self.assertEqual(response[1]["cursor"], "1")
        self.assertEqual(response[1]["next_cursor"], "2")

        calls, response = run({}, {"id": "42", "region": "浙江-杭州"})
        self.assertEqual(calls[0][2]["filter_region"], "浙江-杭州")
        self.assertEqual(response[1]["location_region"], "浙江-杭州")

        calls, response = run({}, {})
        self.assertEqual(calls, [])
        self.assertEqual(response[1]["code"], "PROFILE_LOCATION_MISSING")
        self.assertTrue(response[1]["location_required"])

    def test_id_cursor_moment_tabs_continue_from_last_post_id(self) -> None:
        def run(tab: str, cursor: str):
            calls = []
            result = ApiResult(
                True,
                200,
                "[]",
                data=[
                    {"id": "99", "authid": "8", "posttext": "first"},
                    {"id": "100", "authid": "9", "posttext": "last"},
                ],
            )
            session = SimpleNamespace(uid="42", raw_user={}, nickname="N", portrait="")
            app = SimpleNamespace(
                session=session,
                social=SimpleNamespace(
                    posts=lambda feed_tab, feed_cursor, **kwargs: calls.append(
                        (feed_tab, feed_cursor, kwargs)
                    )
                    or result,
                ),
            )
            web_user = SimpleNamespace(app=app)

            class Harness:
                path = f"/api/moments/posts?tab={quote(tab)}&cursor={cursor}"

                def __init__(self):
                    self.response = None

                def _check_api_origin(self):
                    return True

                def sid(self):
                    return "sid"

                def user(self, _sid):
                    return web_user

                def ok(self, obj, status=200, **_kwargs):
                    self.response = (status, obj)
                    return self.response

            harness = Harness()
            bff_server.Handler.do_GET(harness)
            return calls, harness.response

        cases = (
            ("推荐", "1"),
            ("招募令", "123456789012345678"),
            ("关注", "987654321012345678"),
        )
        for tab, cursor in cases:
            with self.subTest(tab=tab):
                calls, response = run(tab, cursor)
                self.assertEqual(response[0], 200)
                self.assertEqual(calls[0][0:2], (tab, cursor))
                self.assertEqual(response[1]["cursor"], cursor)
                self.assertEqual(response[1]["next_cursor"], "100")

    def test_user_moments_target_uid_and_numbered_pages(self) -> None:
        def run(path):
            calls = []

            def user_posts(uid, page="1"):
                calls.append(("self", uid, page))
                if page == "4":
                    return ApiResult(False, 200, "false", data=False)
                return ApiResult(
                    True,
                    200,
                    "[]",
                    data=[{"id": f"post-{page}", "authid": uid, "posttext": "动态"}],
                )

            def profile_posts(uid, page="1"):
                calls.append(("profile", uid, page))
                if page == "4":
                    return ApiResult(False, 200, "false", data=False)
                return ApiResult(
                    True,
                    200,
                    "[]",
                    data=[{"id": f"post-{page}", "authid": uid, "posttext": "动态"}],
                )

            app = SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(user_posts=user_posts, profile_posts=profile_posts),
            )
            web_user = SimpleNamespace(app=app)

            class Harness:
                def __init__(self):
                    self.path = path
                    self.response = None

                def _check_api_origin(self):
                    return True

                def sid(self):
                    return "sid"

                def user(self, _sid):
                    return web_user

                def ok(self, obj, status=200, **_kwargs):
                    self.response = (status, obj)
                    return self.response

            harness = Harness()
            bff_server.Handler.do_GET(harness)
            return calls, harness.response

        calls, response = run("/api/moments/posts?uid=9&page=2")
        self.assertEqual(calls, [("profile", "9", "2")])
        self.assertEqual(response[1]["target_uid"], "9")
        self.assertEqual(response[1]["page"], "2")
        self.assertEqual(response[1]["next_page"], "3")
        self.assertFalse(response[1]["items"][0]["is_self"])

        calls, response = run("/api/moments/posts?tab=%E6%88%91%E7%9A%84&page=3")
        self.assertEqual(calls, [("self", "42", "3")])
        self.assertTrue(response[1]["items"][0]["is_self"])
        self.assertEqual(response[1]["next_page"], "4")

        calls, response = run("/api/moments/posts?uid=9&page=4")
        self.assertEqual(calls, [("profile", "9", "4")])
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["items"], [])
        self.assertEqual(response[1]["next_page"], "")

        calls, response = run("/api/moments/posts?uid=bad&page=1")
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 400)

    def test_agree_friend_uses_applicant_uid_and_verifies_friend_state(self) -> None:
        calls, response = self._run_agree_post({"uid": "9", "apply_id": "apply-1"})

        self.assertEqual(calls[:2], [("agree", "9"), ("friends", None)])
        self.assertTrue(response[1]["ok"])
        self.assertTrue(response[1]["verified"])
        self.assertEqual(response[1]["application_status"], "accepted")


class MatchRoutingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def test_saved_multi_properties_use_stable_rotation_order(self) -> None:
        self.assertEqual(
            bff_server._saved_match_properties(
                {"_web_match_properties": ["B", "双"]}, "Z"
            ),
            ["双", "B"],
        )
        self.assertEqual(bff_server._saved_match_properties({}, "Z"), ["Z"])

    def test_match_message_peer_ids_accept_normalized_and_legacy_shapes(self) -> None:
        self.assertEqual(
            bff_server._message_peer_ids(
                {
                    "items": [
                        {"id": "9"},
                        {"yourid": "10"},
                        {"target": {"userId": "11"}},
                    ]
                }
            ),
            {"9", "10", "11"},
        )

    def _run_match(self, path: str, payload: dict, raw_user=None):
        calls = []
        result = ApiResult(
            True,
            200,
            '{"id":"9","nickname":"N"}',
            data={"id": "9", "nickname": "N"},
        )

        class Match:
            def set_filter(self, value):
                calls.append(("filter", value))
                return ApiResult(False, 200, "", code="EMPTY_RESPONSE", kind="empty")

            def online_one(self, **params):
                calls.append(("online", params))
                return result

            def local_one(self, **params):
                calls.append(("local", params))
                return result

            def bottle_leave_word(self, **params):
                calls.append(("bottle_leave_word", params))
                return result

        session = SimpleNamespace(
            uid="42",
            raw_user=raw_user
            if raw_user is not None
            else {
                "gender": "男",
                "property": "Z",
                "match_gender": "不限",
                "match_property": "双",
            },
        )
        app = SimpleNamespace(session=session, match=Match())
        web_user = SimpleNamespace(app=app)

        class Harness:
            def __init__(self):
                self.path = path
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return payload

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return calls, session, harness.response

    def _run_online_users(
        self,
        enabled=None,
        query: str = "",
        request_enabled=None,
    ):
        calls = []
        result = ApiResult(
            True,
            200,
            '[{"id":"9","nickname":"N"}]',
            data=[{"id": "9", "nickname": "N"}],
        )

        class Match:
            def online_users(self, **params):
                calls.append(params)
                return result

        app = SimpleNamespace(
            session=SimpleNamespace(
                uid="42",
                raw_user={"id": "42", "gender": "男", "property": "Z"},
            ),
            match=Match(),
        )
        values = {"app": app}
        if enabled is not None:
            values["match_pool_online_list_enabled"] = enabled
        web_user = SimpleNamespace(**values)

        class Harness:
            def __init__(self):
                self.path = f"/api/match/online-users{query}"
                self.response = None
                if request_enabled is not None:
                    self._request_match_pool_online_list_enabled = request_enabled

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_GET(harness)
        return calls, harness.response

    def test_online_user_list_is_available_without_private_message_grant(self) -> None:
        calls, response = self._run_online_users(
            False,
            "?page=2&enabled=true&admin=true",
            request_enabled=False,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "42")
        self.assertEqual(calls[0]["pageIndex"], "2")
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])
        self.assertTrue(response[1]["capabilities"]["match_pool_online_list"])
        self.assertFalse(response[1]["capabilities"]["proactive_private_message"])

    def test_online_user_list_reports_private_message_permission_separately(self) -> None:
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                calls, response = self._run_online_users(
                    not enabled,
                    request_enabled=enabled,
                )

                self.assertEqual(len(calls), 1)
                self.assertEqual(response[0], 200)
                self.assertTrue(response[1]["ok"])
                self.assertTrue(response[1]["capabilities"]["match_pool_online_list"])
                self.assertIs(
                    response[1]["capabilities"]["proactive_private_message"],
                    enabled,
                )

    def test_online_user_list_accepts_gender_property_and_age_filters(self) -> None:
        calls, response = self._run_online_users(
            query="?gender=女&property=B&age=25-34&page=3"
        )

        self.assertEqual(calls[0]["gender"], "女")
        self.assertEqual(calls[0]["property"], "B")
        self.assertEqual(calls[0]["pageIndex"], "3")
        self.assertEqual(response[1]["filters"], {"gender": "女", "property": "B", "age": "25-34"})
        self.assertEqual(response[1]["items"], [])

    def test_nearby_users_default_to_profile_city_and_custom_city_requires_permission(self) -> None:
        calls = []
        result = ApiResult(
            True,
            200,
            '[{"id":"9","nickname":"同城","city":"福州"},{"id":"10","nickname":"异地","city":"厦门"}]',
            data=[
                {"id": "9", "nickname": "同城", "city": "福州", "gender": "女", "property": "B", "age": "30"},
                {"id": "10", "nickname": "异地", "city": "厦门", "gender": "女", "property": "B", "age": "30"},
            ],
        )

        class Match:
            def online_users(self, **params):
                calls.append(params)
                return result

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42", raw_user={"id": "42", "region": "福建-福州市"}),
            match=Match(),
        )
        web_user = SimpleNamespace(app=app, match_pool_online_list_enabled=False)

        class Harness:
            def __init__(self, query, custom_city_enabled=False):
                self.path = f"/api/match/nearby-users{query}"
                self.response = None
                self._request_nearby_custom_city_enabled = custom_city_enabled

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        default_harness = Harness("?gender=女&property=B&age=25-34")
        bff_server.Handler.do_GET(default_harness)
        self.assertEqual(default_harness.response[0], 200)
        self.assertEqual([item["id"] for item in default_harness.response[1]["items"]], ["9"])
        self.assertEqual(default_harness.response[1]["location"]["mode"], "profile_city")

        denied_harness = Harness("?city=厦门")
        bff_server.Handler.do_GET(denied_harness)
        self.assertEqual(denied_harness.response[0], 403)
        self.assertEqual(denied_harness.response[1]["code"], "CUSTOM_CITY_PERMISSION_REQUIRED")

        allowed_harness = Harness("?city=厦门", custom_city_enabled=True)
        bff_server.Handler.do_GET(allowed_harness)
        self.assertEqual([item["id"] for item in allowed_harness.response[1]["items"]], ["10"])
        self.assertEqual(allowed_harness.response[1]["location"]["mode"], "custom_city")

    def test_nearby_users_request_browser_location_when_profile_city_is_missing(self) -> None:
        result = ApiResult(True, 200, "[]", data=[])

        class Match:
            def online_users(self, **_params):
                return result

        app = SimpleNamespace(
            session=SimpleNamespace(uid="42", raw_user={"id": "42"}),
            match=Match(),
            profile=SimpleNamespace(get_me=lambda: ApiResult(True, 200, "[]", data=[])),
        )
        web_user = SimpleNamespace(
            app=app,
            match_pool_online_list_enabled=False,
            persist=lambda: None,
        )

        class Harness:
            path = "/api/match/nearby-users"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_GET(harness)
        self.assertEqual(harness.response[0], 200)
        self.assertTrue(harness.response[1]["location_required"])
        self.assertEqual(harness.response[1]["code"], "NEARBY_LOCATION_REQUIRED")

    def test_match_filter_is_saved_and_apk_profile_params_are_used(self) -> None:
        calls, session, response = self._run_match(
            "/api/match/online", {"gender": "女", "property": "B"}
        )

        self.assertEqual(calls[:2], [("filter", "女"), ("filter", "B")])
        self.assertEqual(
            calls[2],
            ("online", {"id": "42", "gender": "男", "property": "Z"}),
        )
        self.assertEqual(session.raw_user["match_gender"], "女")
        self.assertEqual(session.raw_user["match_property"], "B")
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["message_peers"], ["9"])
        self.assertEqual(
            response[1]["filters"],
            {"gender": "女", "property": "B", "properties": ["B"]},
        )

    def test_invalid_match_filter_is_rejected_before_request(self) -> None:
        calls, _session, response = self._run_match(
            "/api/match/local", {"gender": "全部", "property": "B"}
        )
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 400)
        self.assertFalse(response[1]["ok"])

    def test_bottle_compose_uses_apk_leave_word_action(self) -> None:
        calls, _session, response = self._run_match(
            "/api/match/bottle-throw", {"leave_word": "来自网页的漂流瓶"}
        )
        self.assertEqual(
            calls,
            [("bottle_leave_word", {"leave_word": "来自网页的漂流瓶"})],
        )
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])

        calls, _session, response = self._run_match(
            "/api/match/bottle-throw", {"leave_word": ""}
        )
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 400)
        self.assertFalse(response[1]["ok"])

    def test_invalid_multi_property_is_rejected_before_request(self) -> None:
        calls, _session, response = self._run_match(
            "/api/match/local",
            {"gender": "不限", "properties": ["双", "未知"]},
        )
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 400)
        self.assertFalse(response[1]["ok"])

    def test_multi_properties_rotate_without_double_match_charge(self) -> None:
        calls, session, response = self._run_match(
            "/api/match/online", {"gender": "不限", "properties": ["B", "双", "Z"]}
        )
        self.assertEqual(
            calls,
            [("online", {"id": "42", "gender": "男", "property": "Z"})],
        )
        self.assertEqual(session.raw_user["match_property"], "双")
        self.assertEqual(session.raw_user["_web_match_properties"], ["双", "Z", "B"])
        self.assertEqual(session.raw_user["_web_match_properties_cursor"], 1)
        self.assertEqual(response[1]["filters"]["properties"], ["双", "Z", "B"])
        self.assertEqual(response[1]["active_property"], "双")
        self.assertEqual(response[1]["filter_strategy"], "round_robin")

        calls, session, response = self._run_match(
            "/api/match/online",
            {"gender": "不限", "properties": ["双", "Z", "B"]},
            raw_user=session.raw_user,
        )
        self.assertEqual(calls[0], ("filter", "Z"))
        self.assertEqual(calls[1][0], "online")
        self.assertEqual(len([call for call in calls if call[0] == "online"]), 1)
        self.assertEqual(session.raw_user["match_property"], "Z")
        self.assertEqual(session.raw_user["_web_match_properties_cursor"], 2)
        self.assertEqual(response[1]["active_property"], "Z")

        calls, session, response = self._run_match(
            "/api/match/online",
            {"gender": "不限", "properties": ["双", "Z", "B"]},
            raw_user=session.raw_user,
        )
        self.assertEqual(calls[0], ("filter", "B"))
        self.assertEqual(calls[1][0], "online")
        self.assertEqual(len([call for call in calls if call[0] == "online"]), 1)
        self.assertEqual(session.raw_user["match_property"], "B")
        self.assertEqual(session.raw_user["_web_match_properties_cursor"], 0)
        self.assertEqual(response[1]["active_property"], "B")


class VoiceMatchBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    @staticmethod
    def _harness(path, web_user, body=None):
        class Harness:
            def __init__(self):
                self.path = path
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return dict(body or {})

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        return Harness()

    def test_voice_bootstrap_returns_only_browser_required_credentials(self) -> None:
        issued = SimpleNamespace(
            ok=True,
            app_key="app-key",
            user_id="42",
            token="rong-token",
            nickname="Me",
            portrait="me.jpg",
            raw={"must": "not leak"},
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            native=SimpleNamespace(im=SimpleNamespace(rong_register=lambda: issued)),
            lock=threading.RLock(),
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
        )
        harness = self._harness("/api/match/voice/bootstrap", web_user)
        bff_server.Handler.do_POST(harness)

        self.assertEqual(harness.response[0], 200)
        self.assertTrue(harness.response[1]["ok"])
        self.assertEqual(harness.response[1]["credentials"]["token"], "rong-token")
        self.assertNotIn("raw", harness.response[1])
        self.assertEqual(web_user.voice_rong_credentials["user_id"], "42")

    def test_voice_start_waits_once_and_rejects_duplicate_charge(self) -> None:
        calls = []
        result = ApiResult(True, 200, "wait", data="wait", kind="text")
        match = SimpleNamespace(
            start_voice=lambda **kwargs: calls.append(kwargs) or result,
            normalize_voice_result=MatchAPI.normalize_voice_result,
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42"), match=match),
            native=SimpleNamespace(),
            lock=threading.RLock(),
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
            voice_rong_credentials={
                "app_key": "app-key",
                "user_id": "42",
                "token": "rong-token",
                "nickname": "Me",
                "portrait": "",
            },
            voice_rong_credentials_at=bff_server.time.time(),
            voice_match_state={},
            match_message_peers=set(),
        )

        first = self._harness("/api/match/voice/start", web_user)
        bff_server.Handler.do_POST(first)
        second = self._harness("/api/match/voice/start", web_user)
        bff_server.Handler.do_POST(second)

        self.assertEqual(first.response[0], 200)
        self.assertEqual(first.response[1]["outcome"], "waiting")
        self.assertTrue(first.response[1]["active"])
        self.assertEqual(second.response[0], 409)
        self.assertEqual(second.response[1]["code"], "VOICE_MATCH_ALREADY_ACTIVE")
        self.assertEqual(calls, [{"id_": "42"}])

    def test_voice_cancel_clears_matched_target_without_removing_wait_queue(self) -> None:
        def unexpected_remove(**_kwargs):
            self.fail("a completed match must not call removeXiaobeiMatch")

        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                match=SimpleNamespace(cancel_voice=unexpected_remove),
            ),
            native=SimpleNamespace(),
            lock=threading.RLock(),
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
            voice_match_state={
                "state": "matched",
                "target": {"id": "9", "nickname": "Peer"},
                "started_at": bff_server.time.time(),
                "updated_at": bff_server.time.time(),
            },
        )

        harness = self._harness("/api/match/voice/cancel", web_user)
        bff_server.Handler.do_POST(harness)

        self.assertEqual(harness.response[0], 200)
        self.assertTrue(harness.response[1]["ok"])
        self.assertFalse(harness.response[1]["remote_required"])
        self.assertEqual(harness.response[1]["state"], "idle")
        self.assertIsNone(harness.response[1]["target"])


class PrivateMessagePermissionBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def _run_rest_send(
        self,
        *,
        enabled=False,
        friend_peers=(),
        match_peers=(),
        conversation_peers=(),
        blocked_peers=(),
        blocked_by_peers=(),
        authorizer=None,
    ):
        calls = []
        result = SimpleNamespace(
            ok=True,
            data={"MsgKey": "message-1"},
            to_dict=lambda: {"ok": True, "error_code": 0, "data": {"MsgKey": "message-1"}},
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                im=SimpleNamespace(
                    history_message_insert=lambda **_kwargs: ApiResult(
                        True, 200, "true", data=True
                    )
                ),
            ),
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    send_text=lambda sender, peer, text: calls.append((sender, peer, text)) or result
                )
            ),
            match_pool_online_list_enabled=enabled,
            friend_message_peers=set(friend_peers),
            match_message_peers=set(match_peers),
            conversation_message_peers=set(conversation_peers),
            blocked_message_peers=set(blocked_peers),
            blocked_by_message_peers=set(blocked_by_peers),
            blocked_message_peers_snapshot_at=time.monotonic(),
            blocked_by_message_peers_snapshot_at=time.monotonic(),
        )

        class Harness:
            path = "/api/im/rest/send"

            def __init__(self):
                self.response = None
                if authorizer is not None:
                    self._request_message_peer_authorizer = authorizer

            def _check_api_origin(self):
                return True

            def body(self):
                return {"peer": "9", "text": "你好"}

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return calls, harness.response

    def _run_mark_read(self, *, authorized=True, peers=None):
        calls = []
        result = SimpleNamespace(ok=True)
        allowed_peers = set(peers or ["9"]) if authorized else set()
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    mark_c2c_read=lambda account, peer: calls.append((account, peer))
                    or result
                )
            ),
            match_pool_online_list_enabled=False,
            friend_message_peers=allowed_peers,
            match_message_peers=set(),
            conversation_message_peers=set(),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=time.monotonic(),
            blocked_by_message_peers_snapshot_at=time.monotonic(),
        )

        class Harness:
            path = "/api/im/read"

            def __init__(self):
                self.response = None
                self._request_match_pool_online_list_enabled = False

            def _check_api_origin(self):
                return True

            def body(self):
                return {"peers": peers} if peers is not None else {"peer": "9"}

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return calls, harness.response

    def test_non_match_new_private_message_is_denied_before_rest_send(self) -> None:
        calls, response = self._run_rest_send()

        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)
        self.assertEqual(response[1]["code"], "PRIVATE_MESSAGE_PERMISSION_REQUIRED")

    def test_rest_mode_read_report_uses_authenticated_account(self) -> None:
        calls, response = self._run_mark_read()

        self.assertEqual(calls, [("42", "9")])
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["read"])

        calls, response = self._run_mark_read(authorized=False)
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)

        calls, response = self._run_mark_read(peers=["9", "10", "9"])
        self.assertCountEqual(calls, [("42", "9"), ("42", "10")])
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["read_peers"], ["9", "10"])

    def test_match_existing_conversation_and_live_authorizer_are_allowed(self) -> None:
        for kwargs in (
            {"enabled": True},
            {"friend_peers": {"9"}},
            {"match_peers": {"9"}},
            {"conversation_peers": {"9"}},
            {"authorizer": lambda peer: peer == "9"},
        ):
            with self.subTest(kwargs=kwargs):
                calls, response = self._run_rest_send(**kwargs)
                self.assertEqual(calls, [("42", "9", "你好")])
                self.assertEqual(response[0], 200)
                self.assertTrue(response[1]["ok"])

    def test_blacklist_and_persistent_authorizer_override_every_local_grant(self) -> None:
        granted = {
            "enabled": True,
            "friend_peers": {"9"},
            "match_peers": {"9"},
            "conversation_peers": {"9"},
        }

        calls, response = self._run_rest_send(
            **granted,
            blocked_peers={"9"},
            authorizer=lambda _peer: True,
        )
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)

        calls, response = self._run_rest_send(
            **granted,
            blocked_by_peers={"9"},
            authorizer=lambda _peer: True,
        )
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)

        calls, response = self._run_rest_send(
            **granted,
            authorizer=lambda _peer: False,
        )
        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)

    def test_first_private_message_check_loads_both_blacklist_directions(self) -> None:
        calls = []
        recorded = []
        empty = ApiResult(False, 200, "false", data=False)
        blocked_by = ApiResult(
            True,
            200,
            '[{"uid":"9","nickname":"对方"}]',
            data=[{"uid": "9", "nickname": "对方"}],
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: calls.append("blacklist") or empty,
                    blacklist_me=lambda: calls.append("blacklist-me") or blocked_by,
                ),
            ),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=0.0,
            message_blocks_retry_at=0.0,
        )
        harness = SimpleNamespace(
            _request_message_peer_authorizer=lambda _peer: True,
            _request_message_block_snapshot_recorder=lambda path, peers: recorded.append(
                (path, set(peers))
            ),
        )

        self.assertFalse(bff_server.Handler.can_message_peer(harness, web_user, "9"))
        self.assertEqual(calls, ["blacklist", "blacklist-me"])
        self.assertEqual(
            recorded,
            [
                ("/api/social/blacklist", set()),
                ("/api/social/blacklist-me", {"9"}),
            ],
        )
        self.assertEqual(web_user.blocked_by_message_peers, {"9"})

    def test_blacklist_fetch_and_persistence_run_inside_direction_guard(self) -> None:
        events = []
        empty = ApiResult(False, 200, "false", data=False)

        class Guard:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                events.append(("enter", self.path))

            def __exit__(self, _exc_type, _exc, _traceback):
                events.append(("exit", self.path))

        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: events.append(
                        ("fetch", "/api/social/blacklist")
                    )
                    or empty,
                    blacklist_me=lambda: events.append(
                        ("fetch", "/api/social/blacklist-me")
                    )
                    or empty,
                ),
            ),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=0.0,
            message_blocks_retry_at=0.0,
        )
        harness = SimpleNamespace(
            _request_message_block_snapshot_guard=lambda path: Guard(path),
            _request_message_block_snapshot_recorder=lambda path, _peers: events.append(
                ("persist", path)
            ),
        )

        self.assertTrue(
            bff_server.Handler.ensure_message_blocks_loaded(harness, web_user)
        )
        self.assertEqual(
            events,
            [
                ("enter", "/api/social/blacklist"),
                ("fetch", "/api/social/blacklist"),
                ("persist", "/api/social/blacklist"),
                ("exit", "/api/social/blacklist"),
                ("enter", "/api/social/blacklist-me"),
                ("fetch", "/api/social/blacklist-me"),
                ("persist", "/api/social/blacklist-me"),
                ("exit", "/api/social/blacklist-me"),
            ],
        )

    def test_blacklist_retry_window_starts_after_slow_refresh_failure(self) -> None:
        failed = ApiResult(
            False,
            503,
            "upstream unavailable",
            message="upstream unavailable",
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: failed,
                    blacklist_me=lambda: failed,
                ),
            ),
            blocked_message_peers={"9"},
            blocked_by_message_peers={"10"},
            blocked_message_peers_snapshot_at=1.0,
            blocked_by_message_peers_snapshot_at=1.0,
            message_blocks_retry_at=0.0,
        )

        with patch.object(
            bff_server.time,
            "monotonic",
            side_effect=[100.0, 135.0],
        ):
            self.assertTrue(
                bff_server.Handler.ensure_message_blocks_loaded(
                    SimpleNamespace(),
                    web_user,
                )
            )

        self.assertEqual(
            web_user.message_blocks_retry_at,
            135.0 + bff_server.MESSAGE_BLOCK_SNAPSHOT_RETRY_SEC,
        )

    def test_blacklist_read_failure_preserves_previous_snapshot(self) -> None:
        results = [
            ApiResult(False, 503, "upstream unavailable", message="upstream unavailable"),
            ApiResult(False, 200, "false", data=False),
        ]
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(my_blacklist=lambda: results.pop(0)),
            ),
            blocked_message_peers={"9"},
            blocked_message_peers_snapshot_at=time.monotonic(),
        )

        class Harness:
            path = "/api/social/blacklist"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        failed = Harness()
        bff_server.Handler.do_GET(failed)
        self.assertFalse(failed.response[1]["ok"])
        self.assertEqual(web_user.blocked_message_peers, {"9"})

        empty_snapshot = Harness()
        bff_server.Handler.do_GET(empty_snapshot)
        self.assertTrue(empty_snapshot.response[1]["ok"])
        self.assertEqual(web_user.blocked_message_peers, set())

    def test_new_block_is_enforced_when_snapshot_persistence_fails(self) -> None:
        previous_snapshot_at = time.monotonic() - (
            bff_server.MESSAGE_BLOCK_SNAPSHOT_TTL_SEC + 1
        )
        blocked = ApiResult(
            True,
            200,
            '[{"uid":"9","nickname":"对方"}]',
            data=[{"uid": "9", "nickname": "对方"}],
        )
        empty = ApiResult(False, 200, "false", data=False)
        authorizer_calls = []
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: blocked,
                    blacklist_me=lambda: empty,
                ),
            ),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=previous_snapshot_at,
            blocked_by_message_peers_snapshot_at=previous_snapshot_at,
            message_blocks_retry_at=0.0,
        )

        def fail_snapshot_write(_path, _peers):
            raise RuntimeError("database unavailable")

        harness = SimpleNamespace(
            _request_message_peer_authorizer=lambda peer: authorizer_calls.append(peer)
            or True,
            _request_message_block_snapshot_recorder=fail_snapshot_write,
        )

        self.assertFalse(bff_server.Handler.can_message_peer(harness, web_user, "9"))
        self.assertEqual(web_user.blocked_message_peers, {"9"})
        self.assertEqual(authorizer_calls, [])
        self.assertEqual(
            web_user.blocked_message_peers_snapshot_at,
            0.0,
        )

    def test_message_policy_fails_closed_when_initial_block_snapshots_are_unavailable(self) -> None:
        failed = ApiResult(
            False,
            503,
            "upstream unavailable",
            message="upstream unavailable",
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: failed,
                    blacklist_me=lambda: failed,
                ),
            ),
            match_pool_online_list_enabled=True,
            friend_message_peers={"9"},
            match_message_peers={"9"},
            conversation_message_peers={"9"},
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=0.0,
            message_blocks_retry_at=0.0,
        )

        class Harness:
            path = "/api/im/message-policy"

            def __init__(self):
                self.response = None
                self._request_match_pool_online_list_enabled = True
                self._request_message_policy_allowed_peers = ("9",)
                self._request_message_policy_match_peers = ("9",)
                self._request_message_policy_blocked_peers = ()

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_GET(harness)

        self.assertEqual(harness.response[0], 503)
        self.assertFalse(harness.response[1]["ok"])
        self.assertEqual(
            harness.response[1]["code"],
            "MESSAGE_BLOCK_POLICY_UNAVAILABLE",
        )

    def test_tim_credentials_require_global_proactive_private_message_permission(self) -> None:
        tim_calls = []
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            native=SimpleNamespace(
                im=SimpleNamespace(
                    tim_login_payload=lambda **kwargs: tim_calls.append(kwargs)
                    or {
                        "SDKAppID": 1600039823,
                        "userID": "42",
                        "userSig": "server-user-signature",
                        "source": "server",
                    },
                    rong_register=lambda: self.fail("credential mint must not run"),
                    bootstrap=lambda **_kwargs: self.fail("credential mint must not run"),
                )
            ),
            match_pool_online_list_enabled=False,
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=time.monotonic(),
            blocked_by_message_peers_snapshot_at=time.monotonic(),
        )

        class Harness:
            def __init__(self, path, enabled):
                self.path = path
                self.response = None
                self._request_match_pool_online_list_enabled = enabled

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        for enabled in (False, True):
            harness = Harness("/api/im/tim", enabled)
            bff_server.Handler.do_GET(harness)

            if enabled:
                self.assertEqual(harness.response[0], 200)
                self.assertTrue(harness.response[1]["ok"])
                self.assertEqual(harness.response[1]["userID"], "42")
                self.assertEqual(harness.response[1]["userSig"], "server-user-signature")
                self.assertTrue(harness.response[1]["sig_len"])
            else:
                self.assertEqual(harness.response[0], 403)
                self.assertFalse(harness.response[1]["ok"])
                self.assertEqual(
                    harness.response[1]["code"],
                    "IM_DIRECT_CREDENTIALS_DISABLED",
                )
            self.assertIs(
                bff_server.Handler.web_user_capabilities(harness, web_user)[
                    "direct_im_credentials"
                ],
                enabled,
            )

            for path in ("/api/im/rong", "/api/im/bootstrap"):
                with self.subTest(enabled=enabled, path=path):
                    harness = Harness(path, enabled)
                    bff_server.Handler.do_GET(harness)

                    self.assertEqual(harness.response[0], 403)
                    self.assertEqual(
                        harness.response[1]["code"],
                        "IM_DIRECT_CREDENTIALS_DISABLED",
                    )
                    self.assertIs(
                        harness.response[1]["capabilities"]["direct_im_credentials"],
                        enabled,
                    )

        failed = ApiResult(
            False,
            503,
            "upstream unavailable",
            message="upstream unavailable",
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: failed,
                    blacklist_me=lambda: failed,
                ),
            ),
            native=SimpleNamespace(
                im=SimpleNamespace(
                    tim_login_payload=lambda **_kwargs: self.fail(
                        "credential mint must wait for message policy"
                    )
                )
            ),
            match_pool_online_list_enabled=True,
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=0.0,
            message_blocks_retry_at=0.0,
        )
        unavailable = Harness("/api/im/tim", True)
        bff_server.Handler.do_GET(unavailable)
        self.assertEqual(unavailable.response[0], 503)
        self.assertEqual(
            unavailable.response[1]["code"],
            "MESSAGE_BLOCK_POLICY_UNAVAILABLE",
        )

        self.assertEqual(
            tim_calls,
            [{"prefer": "server", "allow_local_fallback": False}],
        )

    def test_message_policy_restores_grants_but_removes_blocked_peers(self) -> None:
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: ApiResult(
                        False, 503, "upstream unavailable", message="upstream unavailable"
                    ),
                    blacklist_me=lambda: ApiResult(
                        False, 503, "upstream unavailable", message="upstream unavailable"
                    ),
                ),
            ),
            match_pool_online_list_enabled=False,
            friend_message_peers={"11", "14"},
            match_message_peers={"9"},
            conversation_message_peers={"12"},
            blocked_message_peers={"11"},
            blocked_by_message_peers={"14"},
            blocked_message_peers_snapshot_at=1.0,
            blocked_by_message_peers_snapshot_at=1.0,
            message_blocks_retry_at=0.0,
        )

        class Harness:
            path = "/api/im/message-policy"

            def __init__(self):
                self.response = None
                self._request_match_pool_online_list_enabled = False
                self._request_message_policy_match_peers = ("10", "42")
                self._request_message_policy_allowed_peers = ("10", "13")
                self._request_message_policy_blocked_peers = ("10",)

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_GET(harness)

        self.assertEqual(harness.response[0], 200)
        self.assertEqual(harness.response[1]["match_peers"], ["9"])
        self.assertEqual(harness.response[1]["allowed_peers"], ["12", "13", "9"])
        self.assertEqual(harness.response[1]["blocked_peers"], ["10", "11", "14"])
        self.assertFalse(
            harness.response[1]["capabilities"]["proactive_private_message"]
        )

    def test_app_bootstrap_returns_server_tim_credential_for_direct_media(self) -> None:
        bootstrap_calls = []
        credential_calls = []
        ok = ApiResult(True, 200, "true", data=True)
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            bootstrap=lambda *, include_im: bootstrap_calls.append(include_im)
            or {"me": ok},
            whoami=lambda: {"uid": "42", "nickname": "N", "logged_in": True},
        )
        class Harness:
            path = "/api/app/bootstrap"

            def __init__(self, web_user, enabled):
                self._web_user = web_user
                self.response = None
                self._request_match_pool_online_list_enabled = enabled

            def _check_api_origin(self):
                return True

            def sid(self):
                return "sid"

            def user(self, _sid):
                return self._web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                web_user = SimpleNamespace(
                    app=app,
                    native=SimpleNamespace(
                        im=SimpleNamespace(
                            tim_login_payload=lambda **kwargs: credential_calls.append(kwargs)
                            or {
                                "SDKAppID": 1600039823,
                                "userID": "42",
                                "userSig": "server-user-signature",
                                "source": "server",
                            }
                        )
                    ),
                    match_pool_online_list_enabled=enabled,
                    blocked_message_peers=set(),
                    blocked_by_message_peers=set(),
                    blocked_message_peers_snapshot_at=time.monotonic(),
                    blocked_by_message_peers_snapshot_at=time.monotonic(),
                    persist=lambda: None,
                )
                harness = Harness(web_user, enabled)
                bff_server.Handler.do_GET(harness)

                self.assertEqual(harness.response[0], 200)
                if enabled:
                    self.assertTrue(harness.response[1]["tim"]["ok"])
                    self.assertEqual(harness.response[1]["tim"]["userID"], "42")
                    self.assertEqual(
                        harness.response[1]["tim"]["userSig"],
                        "server-user-signature",
                    )
                else:
                    self.assertFalse(harness.response[1]["tim"]["ok"])
                    self.assertEqual(
                        harness.response[1]["tim"]["code"],
                        "IM_DIRECT_CREDENTIALS_DISABLED",
                    )
                self.assertIs(
                    harness.response[1]["capabilities"]["direct_im_credentials"],
                    enabled,
                )
                self.assertNotIn("txim", harness.response[1]["batch"])

        self.assertEqual(bootstrap_calls, [False, False])
        self.assertEqual(
            credential_calls,
            [{"prefer": "server", "allow_local_fallback": False}],
        )


class ImRevokeBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def test_revoke_sender_is_taken_from_authenticated_session(self) -> None:
        calls = []
        result = SimpleNamespace(
            ok=True,
            to_dict=lambda: {
                "ok": True,
                "action": "openim/admin_msgwithdraw",
                "error_code": 0,
                "error_info": "",
                "data": {},
            },
        )
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    revoke_c2c=lambda sender, receiver, key: calls.append(
                        (sender, receiver, key)
                    )
                    or result
                )
            ),
        )

        class Harness:
            path = "/api/im/rest/revoke"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return {
                    "from": "forged-user",
                    "to": "9",
                    "msg_key": "message-key",
                }

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)

        self.assertEqual(calls, [("42", "9", "message-key")])
        self.assertEqual(harness.response[0], 200)
        self.assertTrue(harness.response[1]["ok"])


class SocialFrontendContractTests(unittest.TestCase):
    def test_boot_retries_transient_session_restore_failures_before_showing_login(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        restore = app_js.split("async function restoreSessionAtBoot()", 1)[1].split(
            "(async function boot()", 1
        )[0]
        boot = app_js.split("(async function boot()", 1)[1].split("})();", 1)[0]

        self.assertIn("BOOT_SESSION_RETRY_DELAYS_MS", app_js)
        self.assertIn("while (true)", restore)
        self.assertIn('api("/api/me", { authOptional: true, timeout: 8000 })', restore)
        self.assertIn("result.status === 200 || result.status === 401", restore)
        self.assertIn('setBootStatusText("服务暂时不可用，正在恢复登录状态…")', restore)
        self.assertIn("await new Promise((resolve) => setTimeout(resolve, delay))", restore)
        self.assertIn("await restoreSessionAtBoot()", boot)
        self.assertNotIn('api("/api/me"', boot)
        self.assertNotIn("Login screen remains available when the bootstrap request fails.", boot)
        self.assertIn('id="boot-status-text"', index_html)

    def test_moment_cards_report_apk_pv_when_they_enter_the_viewport(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")

        for marker in (
            "function observeMomentCards",
            "new IntersectionObserver",
            '{ threshold: 0.01 }',
            "function reportMomentView",
            "function updateMomentCardVisibility",
            "momentViewQueuedEntries",
            "drainMomentViewReports",
            "drainQueuedMomentViews",
            "momentViewTaskAssistSeq",
            "resetMomentViewTaskAssist",
            "momentViewRequestSeq",
            "retryMomentViewTaskAssist",
            "momentViewTaskAssistRetries",
            "MOMENT_VIEW_TASK_ASSIST_MAX_RETRIES",
            'card.dataset.momentViewVisible = "0"',
            "updateMomentCardVisibility(entry.target, momentCardIsVisible(entry.target))",
            "scanVisibleMomentCards(root(), { force: true })",
            'api("/api/moments/view"',
            "body: JSON.stringify({ post_id: postId, assist_task: assistTask })",
            'S.momentViewTaskAssistState = "pending"',
            "taskAssist?.completed",
            "if (!result.ok || !result.data?.ok)",
            'clearViewCacheKey("tasks")',
            "observeMomentCards(feed)",
            "disconnectMomentViewTracking()",
        ):
            self.assertIn(marker, app_js)
        self.assertIn('if path == "/api/moments/view":', server_py)
        self.assertIn("app.social.record_post_view(postid)", server_py)
        self.assertIn('payload["task_assist"] = _assist_moment_view_task(', server_py)
        self.assertIn("deadline=assist_deadline", server_py)
        self.assertIn("MOMENT_VIEW_TASK_BATCH_SIZE", server_py)

    def test_moment_view_task_assist_does_not_block_other_pv_reports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        drain_report = app_js.split("function drainMomentViewReports", 1)[1].split(
            "function drainQueuedMomentViews", 1
        )[0]
        report_view = app_js.split("async function reportMomentView", 1)[1].split(
            "function scanVisibleMomentCards", 1
        )[0]

        self.assertNotIn('S.momentViewTaskAssistState === "pending"', drain_report)
        self.assertIn('if (card.dataset.momentViewState === "pending") return;', report_view)
        self.assertNotIn(
            'card.dataset.momentViewState === "pending" || S.momentViewTaskAssistState === "pending"',
            report_view,
        )
        self.assertIn('const assistTask = S.momentViewTaskAssistState === "idle";', report_view)
        self.assertIn("body: JSON.stringify({ post_id: postId, assist_task: assistTask })", report_view)

    def test_me_page_renders_profile_before_loading_relationship_counts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        page_me = app_js.split("async function pageMe(signal)", 1)[1].split(
            "async function pageLab", 1
        )[0]
        hydrate = app_js.split("async function hydrateMeStats", 1)[1].split(
            "function hydrateRenderedRoute", 1
        )[0]

        self.assertIn('api("/api/profile/me", { signal })', page_me)
        self.assertNotIn("/api/social/", page_me)
        self.assertIn("?summary=1", hydrate)
        self.assertIn('data-me-stat="friends"', page_me)
        self.assertIn("PAGE_CACHE_TTL_MS = 2 * 60 * 1000", app_js)

    def test_menu_navigation_reuses_cached_views_and_revalidates_in_background(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        activate_route = app_js.split("async function activateRoute", 1)[1].split(
            "function loadingState", 1
        )[0]
        switch_mine = app_js.split("async function switchMineTab", 1)[1].split(
            "function routeCacheKey", 1
        )[0]
        switch_social = app_js.split("async function switchSocialTab", 1)[1].split(
            "async function loadMatchHubTab", 1
        )[0]
        switch_match = app_js.split("async function switchMatchHubTab", 1)[1].split(
            "async function pageWallet", 1
        )[0]
        load_discovery = app_js.split("async function loadDiscoveryPanel", 1)[1].split(
            "async function pageNearby", 1
        )[0]
        switch_moments = app_js.split("async function switchMomentsTab", 1)[1].split(
            "function relationshipToolsHtml", 1
        )[0]

        for marker in (
            "panelCache: new Map()",
            "routeDomCache: new Map()",
            "panelDomCache: new Map()",
            "function reusableCacheEntry",
            "function reusableRouteDomEntry",
            "function reusablePanelDomEntry",
            "function rememberPanelSnapshot",
            "PAGE_CACHE_MAX_AGE_MS",
            "FAST_VIEW_CACHE_TTL_MS",
            "FEED_VIEW_CACHE_TTL_MS",
            "RELATION_VIEW_CACHE_TTL_MS",
        ):
            self.assertIn(marker, app_js)
        self.assertIn("reusableCacheEntry(S.pageCache, cacheKey)", activate_route)
        self.assertIn("reusableRouteDomEntry(cacheKey", activate_route)
        self.assertIn("restoreRouteDomSnapshot(cacheKey, routeDom)", activate_route)
        self.assertIn("allowWithoutPageCache: target === \"msg\"", activate_route)
        self.assertIn("refreshMessageConversationRegion({ refreshList: true, refreshPane: false })", activate_route)
        self.assertIn('root().classList.add("is-refreshing")', activate_route)
        self.assertNotIn("permissionSensitiveRoute", activate_route)
        self.assertIn("minePanelCacheKey(target)", switch_mine)
        self.assertIn("reusableCacheEntry(S.panelCache, panelKey)", switch_mine)
        self.assertIn("restorePanelDomSnapshot(panel, panelDom)", switch_mine)
        self.assertIn("rememberCurrentPageSnapshot(routeKey)", switch_mine)
        self.assertIn("reusableCacheEntry(S.panelCache, cacheKey)", switch_social)
        self.assertIn("rememberPanelDomSnapshot(cacheKey, panel)", switch_social)
        self.assertIn("restorePanelDomSnapshot(panel, panelDom)", switch_social)
        self.assertIn("reusableCacheEntry(S.panelCache, cacheKey)", switch_match)
        self.assertIn("rememberPanelDomSnapshot(cacheKey, panel)", switch_match)
        self.assertIn("restorePanelDomSnapshot(panel, panelDom)", switch_match)
        self.assertIn("rememberPanelDomSnapshot(cacheKey, panel)", load_discovery)
        self.assertIn("restorePanelDomSnapshot(panel, panelDom)", load_discovery)
        self.assertIn("rememberPanelDomSnapshot(cacheKey, panel)", switch_moments)
        self.assertIn("restorePanelDomSnapshot(panel, panelDom)", switch_moments)
        self.assertIn(".page-root.is-refreshing::before", app_css)
        self.assertIn('content: "正在更新数据"', app_css)
        self.assertNotIn("animation: page-in", app_css)
        self.assertNotIn("content-visibility: auto", app_css)

    def test_nearby_panel_cache_hit_updates_the_full_page_snapshot(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        load_discovery = app_js.split("async function loadDiscoveryPanel", 1)[1].split(
            "async function pageNearby", 1
        )[0]
        fresh_cache_branch = load_discovery.split(
            "if (!force && !forceLocation && cached.fresh)", 1
        )[1].split("  } else {", 1)[0]

        snapshot = 'rememberCurrentPageSnapshot(routeCacheKey("nearby"));'
        self.assertIn(snapshot, fresh_cache_branch)
        self.assertLess(fresh_cache_branch.index(snapshot), fresh_cache_branch.index("return;"))

    def test_voice_room_ui_is_removed_but_voice_matching_is_first_class(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        for removed in (
            "语音房",
            "function pageRoom",
            "function roomCard",
            'data-action="room-auth"',
            'data-action="room-native-refresh"',
            'data-form="room-create"',
            'data-form="room-ktv"',
            'data-form="room-rtc"',
            "LEGACY_MATCH_ROUTES",
        ):
            self.assertNotIn(removed, app_js)
        for marker in (
            'const MATCH_HUB_TABS = ["match", "voice", "bottle"]',
            '["voice", "语音匹配"]',
            "async function pageVoiceMatch(signal)",
            'data-action="voice-match-start"',
            "async function ensureVoiceCallReady",
            "async function startRongVoiceCall",
            'api("/api/match/voice/start"',
            "sampleRate: 48000",
            "registerUserInfo?.(",
            "session.getRemoteUsers?.()",
        ):
            self.assertIn(marker, app_js)

    def test_match_page_uses_responsive_preference_workbench(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")
        match_hub_background = root / "bbw_web" / "static" / "match-hub-bg.png"

        for marker in (
            'class="match-hub-header"',
            'data-action="match-tab"',
            "async function switchMatchHubTab(tab)",
            'history.pushState(null, "", matchRouteHash(activeTab))',
            'return switchMatchHubTab(tab);',
            '["match", "匹配"]',
            '["voice", "语音匹配"]',
            '["bottle", "漂流瓶"]',
            "async function pageVoiceMatch(signal)",
            "async function pageBottle(signal)",
            'class="match-overview"',
            'data-form="match-filter"',
            'data-form="bottle-throw"',
            "data-match-filter-summary",
            'const MATCH_PROPERTIES = ["双", "Z", "B"]',
            'matchFilterOption("property", value, properties, "checkbox")',
            "支持多选",
            "每次只消耗一次匹配机会",
        ):
            self.assertIn(marker, app_js)
        self.assertIn(".match-stats-grid", app_css)
        self.assertIn(".match-submit", app_css)
        self.assertIn(".match-hub-tabs", app_css)
        self.assertIn(".match-panel-loading", app_css)
        self.assertIn(".voice-match-control-card", app_css)
        self.assertIn(".voice-call-dialog", app_css)
        self.assertIn('id="voice-call-dialog"', index_html)
        self.assertIn('if path == "/api/match/voice/bootstrap"', bff_server_py)
        self.assertIn('if path == "/api/match/voice/start"', bff_server_py)
        self.assertIn('if path == "/api/match/voice/cancel"', bff_server_py)
        self.assertIn('app.match.start_voice(id_=app.session.uid)', bff_server_py)
        for vendor in (
            "rong/rong-imlib-5.9.5.js",
            "rong/rong-rtc-5.7.2.js",
            "rong/rong-call-5.2.10.js",
            "rong/VERSION.txt",
        ):
            self.assertTrue((root / "bbw_web" / "static" / "vendor" / vendor).is_file())
        self.assertIn('url("/static/match-hub-bg.png")', app_css)
        self.assertTrue(match_hub_background.is_file())
        self.assertNotIn('statCard(display.online ?? "—", "在线免费")', app_js)
        switch_match = app_js.split("async function switchMatchHubTab", 1)[1].split(
            "async function pageWallet", 1
        )[0]
        self.assertIn("panel.innerHTML = content", switch_match)
        self.assertNotIn("root().innerHTML", switch_match)

    def test_online_user_list_remains_available_for_profiles_and_friend_requests(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")

        nearby = app_js.split("function normalizeDiscoveryTab", 1)[1].split(
            "async function pageMessages", 1
        )[0]
        nearby_page = app_js.split("async function pageNearby", 1)[1].split(
            "async function pageMessages", 1
        )[0]
        matching = app_js.split("async function pageMatching", 1)[1].split(
            "async function pageBottle", 1
        )[0]
        user_card = app_js.split("function userCard(item, options = {})", 1)[1].split(
            "function formatSocialTime", 1
        )[0]

        self.assertIn("proactivePrivateMessageEnabled: false", app_js)
        self.assertIn("directImCredentialsEnabled: false", app_js)
        self.assertIn("messagePolicyReady: false", app_js)
        self.assertIn('messagePolicyFingerprint: ""', app_js)
        self.assertIn("messagePolicyCleanupGeneration: 0", app_js)
        self.assertIn('["online", "在线列表"]', nearby)
        self.assertIn('["nearby", "附近的人"]', nearby)
        self.assertIn('data-form="nearby-filter"', nearby)
        self.assertIn('name="gender"', nearby)
        self.assertIn('name="property"', nearby)
        self.assertIn('name="age"', nearby)
        self.assertIn('"/api/match/online-users"', nearby)
        self.assertIn('"/api/match/nearby-users"', nearby)
        self.assertIn("async function loadDiscoveryPanel", nearby)
        self.assertIn("const nextHtml = discoveryPanelHtml", nearby)
        self.assertIn("panel.innerHTML = nextHtml", nearby)
        self.assertIn("navigator.geolocation.getCurrentPosition", nearby)
        self.assertNotIn('class="welcome-strip"', nearby_page)
        self.assertNotIn('class="quick-entry-grid"', nearby_page)
        self.assertNotIn('data-action="match-users"', matching)
        self.assertNotIn("在线列表", matching)
        self.assertNotIn('data-form="dating-publish"', matching)
        self.assertNotIn("发布约会邀请", matching)
        self.assertIn("return userCard(user, {", nearby)
        self.assertIn("addFriend: true", nearby)
        self.assertIn("if (options.addFriend && id)", user_card)
        self.assertIn('data-action="add-friend"', user_card)
        self.assertIn('data-action="open-profile"', user_card)
        self.assertIn('"match_pool_online_list": True', bff_server_py)
        self.assertIn('"nearby_custom_city"', bff_server_py)
        self.assertIn(".discovery-tabs", app_css)
        self.assertIn(".discovery-filter-form", app_css)
        self.assertNotIn("MATCH_POOL_ONLINE_LIST_FORBIDDEN", bff_server_py)

    def test_unprivileged_non_match_users_remain_hidden_in_ui_and_rest_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")
        api_py = (root / "bbw_web" / "api.py").read_text(encoding="utf-8")

        can_start_private_chat = app_js.split("function canStartPrivateChat(uid)", 1)[1].split(
            "function rememberMatchMessagePeers", 1
        )[0]
        user_card = app_js.split("function userCard(item, options = {})", 1)[1].split(
            "function formatSocialTime", 1
        )[0]
        open_profile = app_js.split("async function openProfile", 1)[1].split(
            "async function refreshMatchStats", 1
        )[0]

        self.assertIn("proactivePrivateMessageEnabled: false", app_js)
        self.assertIn("privateMessagePeers: new Set()", app_js)
        self.assertIn("matchMessagePeers: new Set()", app_js)
        self.assertIn("blockedPrivateMessagePeers: new Set()", app_js)
        self.assertIn("S.proactivePrivateMessageEnabled", can_start_private_chat)
        self.assertIn("!S.messagePolicyReady", can_start_private_chat)
        self.assertIn("S.privateMessagePeers.has(target)", can_start_private_chat)
        self.assertIn("S.matchMessagePeers.has(target)", can_start_private_chat)
        self.assertIn("hasExistingConversation(target)", can_start_private_chat)
        self.assertIn("S.blockedPrivateMessagePeers.has(target)", can_start_private_chat)
        self.assertIn("function canOpenPrivateChatEntry(uid, origin", app_js)
        self.assertIn("void origin", app_js)
        self.assertIn("return Boolean(target && canStartPrivateChat(target))", app_js)
        self.assertIn("function ensurePrivateChatEntryPermission(uid, origin", app_js)
        self.assertIn("function rememberMatchMessagePeers(data)", app_js)
        self.assertIn("function rememberMessagePolicyAllowedPeers(values)", app_js)
        self.assertIn("function rememberMessagePolicyMatchPeers(values)", app_js)
        self.assertIn("function replaceMessagePolicyAllowedPeers(values)", app_js)
        self.assertIn("function replaceMessagePolicyMatchPeers(values)", app_js)
        self.assertIn("function replaceBlockedPrivateMessagePeers(values)", app_js)
        self.assertIn(
            "replaceMessagePolicyAllowedPeers(data.allowed_peers)",
            app_js,
        )
        self.assertIn("replaceMessagePolicyMatchPeers(data.match_peers)", app_js)
        self.assertIn("replaceBlockedPrivateMessagePeers(data.blocked_peers)", app_js)
        self.assertIn('chatOrigin: "friends"', app_js)
        self.assertIn('chatOrigin: "match"', app_js)
        self.assertIn("rememberMessagePolicyAllowedPeers(friends)", app_js)
        self.assertIn('if (tab === "black") return userCard(item, { chat: false', app_js)
        self.assertIn("const renderChatAction = options.chat !== false", user_card)
        self.assertIn("const chatAllowed = canOpenPrivateChatEntry(id, chatOrigin)", user_card)
        self.assertIn('aria-disabled="${String(!chatAllowed)}"', user_card)
        self.assertIn('${chatAllowed ? "" : " hidden"}>聊天</button>', user_card)
        self.assertIn("button.hidden = !allowed", app_js)
        self.assertIn("button.dataset.chatOrigin", app_js)
        self.assertIn("ensurePrivateChatEntryPermission(uid, button.dataset.chatOrigin)", app_js)
        self.assertIn("button.disabled = !S.conversationBatchMode && !allowed", app_js)
        self.assertIn(
            "const chatAllowed = !isSelf && canOpenPrivateChatEntry(profileUid, normalizedChatOrigin)",
            open_profile,
        )
        self.assertIn('${chatAllowed ? "" : " hidden"}>聊天</button>', open_profile)
        self.assertIn('data-action="add-friend"', open_profile)
        self.assertIn('api("/api/im/message-policy"', app_js)
        self.assertIn("setMessagePolicyReady(false)", app_js)
        self.assertIn("function currentMessagePolicyFingerprint()", app_js)
        self.assertIn(
            "const policyChanged = next && S.messagePolicyFingerprint !== nextFingerprint",
            app_js,
        )
        self.assertIn("if (!readyChanged && !policyChanged)", app_js)
        self.assertIn(
            "if (!S.messagePolicyReady) setMessagePolicyReady(false)",
            app_js,
        )
        self.assertIn("currentMessagePolicyFingerprint()", app_js)
        self.assertIn(
            "applyCapabilities(data.capabilities, { deferMessageReconnect: true })",
            app_js,
        )
        self.assertIn("function queueMessagePolicyCleanup()", app_js)
        self.assertIn("if (proactiveChanged || directCredentialsChanged)", app_js)
        self.assertIn("const previous = S.messagePolicyCleanupPromise", app_js)
        self.assertIn("Promise.resolve(previous)", app_js)
        self.assertIn("S.messagePolicyCleanupPromise = tracked", app_js)
        self.assertIn("deferMessageReconnect ||", app_js)
        self.assertIn("S.messagePolicyCleanupPromise", app_js)
        self.assertIn(
            "if (transition.readyChanged || transition.policyChanged)",
            app_js,
        )
        self.assertIn("proactive_private_message", app_js)
        self.assertIn("direct_im_credentials", app_js)
        self.assertIn("if (!S.directImCredentialsEnabled)", app_js)
        self.assertNotIn("capabilities.match_pool_online_list === true", app_js)
        self.assertIn('clearViewCacheKey("nearby")', app_js)
        self.assertIn("Handler.web_user_capabilities(self, u)", bff_server_py)
        self.assertIn('"_request_match_pool_online_list_enabled"', bff_server_py)
        self.assertIn("identity.match_pool_online_list_enabled", api_py)
        self.assertIn('if path == "/api/im/message-policy"', bff_server_py)
        self.assertIn('"match_peers": sorted(match_peers)[:5000]', bff_server_py)
        self.assertIn('"allowed_peers": sorted(allowed_peers)[:5000]', bff_server_py)
        self.assertIn('"blocked_peers": sorted(blocked_peers)[:5000]', bff_server_py)
        self.assertIn("app.bootstrap(include_im=False)", bff_server_py)
        self.assertIn("Handler.can_message_peer(self, u, to_uid)", bff_server_py)
        self.assertIn("Handler.can_message_peer(self, u, target_id)", bff_server_py)
        self.assertIn("IM_DIRECT_CREDENTIALS_DISABLED", bff_server_py)
        self.assertIn('"direct_im_credentials": enabled', bff_server_py)
        self.assertIn('if path == "/api/im/tim"', bff_server_py)
        self.assertIn('if not capabilities.get("direct_im_credentials")', bff_server_py)
        self.assertIn("if not Handler.ensure_message_blocks_loaded(self, u):", bff_server_py)
        self.assertIn('prefer="server"', bff_server_py)
        self.assertIn("allow_local_fallback=False", bff_server_py)
        tim_connect = app_js.split("async function ensureTimConnected", 1)[1].split(
            "async function cleanupIM", 1
        )[0]
        self.assertIn("while (S.messagePolicyCleanupPromise)", tim_connect)
        self.assertIn("await Promise.resolve(cleanup)", tim_connect)
        self.assertIn(
            "S.messagePolicyCleanupGeneration === cleanupGeneration",
            tim_connect,
        )
        self.assertIn("!S.messagePolicyCleanupPromise", tim_connect)
        self.assertIn("const policyIsCurrent = () =>", tim_connect)
        self.assertIn("if (!policyIsCurrent()) return false;", tim_connect)
        self.assertIn("const ok = await connectTIM(", tim_connect)
        connect_tim = app_js.split("async function connectTIM", 1)[1].split(
            "async function diagnoseTimConnectionFailure", 1
        )[0]
        self.assertIn("connectionIsCurrent = null", connect_tim)
        self.assertIn("if (!connectionCurrent()) return false;", connect_tim)
        self.assertNotIn(
            "if (!S.proactivePrivateMessageEnabled) return false;",
            tim_connect,
        )
        self.assertIn("persistence.can_message_peer(", api_py)
        self.assertIn("persistence.message_policy_blocked_peers(identity)", api_py)
        self.assertIn("SOCIAL_DM_POLICY_PERSISTENCE_FAILED", api_py)

        send_text = app_js.split("async function sendTextMessage", 1)[1].split(
            "function progressRatio", 1
        )[0]
        self.assertIn("await ensurePrivateChatPermission(target)", send_text)
        composer_panel = app_js.split("function chatComposerPanelHtml()", 1)[1].split(
            "function chatComposerQuoteHtml", 1
        )[0]
        self.assertIn(
            "const directMediaActions = S.directImCredentialsEnabled && S.messagePolicyReady",
            composer_panel,
        )
        self.assertIn(
            "const stickerAction = S.directImCredentialsEnabled && S.messagePolicyReady",
            composer_panel,
        )
        self.assertIn('data-kind="flash"', composer_panel)

    def test_moments_and_social_tabs_update_only_their_content_panels(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        for marker in (
            'id="moments-tab-panel"',
            'id="social-tab-panel"',
            "async function switchMomentsTab(tab",
            "async function switchSocialTab(tab",
            "return switchMomentsTab(tab",
            'return switchSocialTab("visitors"',
            "return switchSocialTab(normalizeSocialTab(button.dataset.tab))",
            'history.pushState(null, "", socialRouteHash(activeTab, S.visitorTab))',
            'const nextHtml = momentsTabPanelHtml(view)',
            'panel.innerHTML = nextHtml',
            'panel.innerHTML = view.body',
        ):
            self.assertIn(marker, app_js)
        self.assertIn(".tab-panel-loading", app_css)
        moments_switch = app_js.split("async function switchMomentsTab", 1)[1].split("function relationshipToolsHtml", 1)[0]
        moments_loader = app_js.split("async function loadMomentsTab", 1)[1].split("function momentsTabPanelHtml", 1)[0]
        self.assertIn('panel.classList.add("is-loading")', moments_switch)
        self.assertNotIn("panel.innerHTML = `<div class=\"tab-panel-loading\"", moments_switch)
        self.assertIn("seq !== S.momentsFeedSeq", moments_switch)
        self.assertNotIn('start: "1"', moments_loader)
        self.assertIn('activeTab === "我的" ? { tab: activeTab, page: "1" }', moments_loader)
        self.assertIn('nextMomentsCursor(activeTab, "1", posts, data)', moments_loader)
        self.assertIn("function nextMomentsCursor", moments_loader)
        self.assertIn("data?.next_cursor", moments_loader)
        self.assertIn('String(data?.next_page || "")', moments_loader)
        self.assertIn("MOMENT_PAGE_CURSOR_TABS.has(tab)", moments_loader)
        self.assertIn('String(posts.at(-1)?.id || "")', moments_loader)
        self.assertIn("String(page + 1)", moments_loader)
        load_more = app_js.split('if (action === "moment-load-more")', 1)[1].split(
            'if (action === "moment-delete")', 1
        )[0]
        self.assertIn("existingIds", load_more)
        self.assertIn("fetchedPosts.filter", load_more)
        self.assertIn("nextMomentsCursor(tab, cursor, fetchedPosts, data)", load_more)
        self.assertIn(".moments-tab-panel.is-loading", app_css)

    def test_moment_comment_avatar_opens_profile_dialog(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        comment_card = app_js.split("function momentCommentCard", 1)[1].split(
            "function momentCommentsHtml", 1
        )[0]
        self.assertIn('class="moment-comment-avatar"', comment_card)
        self.assertIn('data-action="open-profile"', comment_card)
        self.assertIn('data-uid="${esc(', comment_card)
        self.assertIn('aria-label="查看${esc(name)}的资料"', comment_card)
        self.assertIn(".moment-comment-avatar", app_css)

    def test_moment_cards_hide_redundant_dynamic_and_public_badges(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        moment_card = app_js.split("function renderMomentCard", 1)[1].split(
            "function momentCard", 1
        )[0]
        visibility_action = app_js.split('if (action === "moment-visibility")', 1)[1].split(
            'if (action === "moment-pin")', 1
        )[0]
        self.assertIn('plate && plate !== "动态"', moment_card)
        self.assertIn('visibilityScope && visibilityScope !== "公开"', moment_card)
        self.assertIn('if (scope === "公开")', visibility_action)
        self.assertIn("badge?.remove()", visibility_action)

    def test_profile_dialog_can_load_target_user_moments(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")

        for marker in (
            'data-action="profile-moments-toggle"',
            'aria-controls="profile-moments"',
            'id="profile-moments"',
            'id="profile-moment-feed"',
            'data-action="profile-moment-load-more"',
            'new URLSearchParams({ uid, page: "1" })',
            'new URLSearchParams({ uid, page })',
            'data?.feed_type !== "user"',
            'String(data?.target_uid || "") !== String(uid || "")',
            'button.dataset.page = String(data?.next_page || Number(page) + 1)',
            "function renderMomentCard(item, { showAuthor = true } = {})",
            "function profileMomentCard(item)",
            "return renderMomentCard(item, { showAuthor: false });",
            "posts.map(profileMomentCard).join(\"\")",
        ):
            self.assertIn(marker, app_js)
        self.assertIn(".profile-moment-feed", app_css)
        self.assertIn(".profile-moment-time", app_css)
        self.assertIn(".profile-moments-button", app_css)
        self.assertIn("-profile-moments-compact-cards", index_html)

    def test_bottle_card_uses_apk_content_and_readable_visual_hierarchy(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")

        for marker in (
            "function formatBottleTime",
            'class="bottle-card-head"',
            'class="bottle-message',
            'class="bottle-meta"',
            '<textarea class="ui-scrollbar" id="bottle-text"',
            'body: JSON.stringify({ leave_word: text })',
        ):
            self.assertIn(marker, app_js)
        for marker in (".bottle-card::before", ".bottle-message", ".bottle-meta-item"):
            self.assertIn(marker, app_css)
        self.assertIn("app.match.bottle_leave_word(leave_word=text)", bff_server_py)

    def test_decorative_text_placeholders_are_not_used_as_icons(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        agents_md = (root / "AGENTS.md").read_text(encoding="utf-8")
        avatar_renderer = app_js.split("function avatarHtml(url)", 1)[1].split(
            "function conversationAvatarHtml(url)", 1
        )[0]
        conversation_avatar_renderer = app_js.split("function conversationAvatarHtml(url)", 1)[1].split(
            "function revealLoadedAvatar", 1
        )[0]
        conversation_list_renderer = app_js.split("function renderConversationList(list)", 1)[1].split(
            "function refreshMessageConversationRegion", 1
        )[0]
        conversation_region_refresh = app_js.split(
            "function refreshMessageConversationRegion", 1
        )[1].split('document.addEventListener("keydown"', 1)[0]
        self.assertNotIn("match-section-index", app_js)
        self.assertNotIn("match-section-index", app_css)
        self.assertNotIn("function firstChar", app_js)
        self.assertNotIn("avatar.textContent", app_js)
        self.assertIn("mediaUrl(validAvatarValue(url))", avatar_renderer)
        self.assertIn('if (!src) return "";', avatar_renderer)
        self.assertIn('class="avatar avatar-loading"', avatar_renderer)
        self.assertIn('aria-hidden="true"', avatar_renderer)
        self.assertIn('avatar.classList.remove("avatar-loading")', app_js)
        self.assertIn(".avatar.avatar-loading", app_css)
        self.assertIn("opacity: 0", app_css)
        self.assertIn("data-avatar-image", avatar_renderer)
        self.assertIn('loading="lazy"', avatar_renderer)
        self.assertIn('fetchpriority="low"', avatar_renderer)
        self.assertNotIn('loading="eager"', avatar_renderer)
        self.assertNotIn("avatar-loading", conversation_avatar_renderer)
        self.assertIn('loading="eager"', conversation_avatar_renderer)
        self.assertIn('fetchpriority="high"', conversation_avatar_renderer)
        self.assertNotIn("onload=", avatar_renderer)
        self.assertNotIn("onerror=", avatar_renderer)
        self.assertIn("function revealLoadedAvatar(image)", app_js)
        self.assertIn("function discardFailedAvatar(image)", app_js)
        self.assertIn('matches("img[data-avatar-image]")', app_js)
        self.assertIn("const previousAvatars = new Map();", conversation_list_renderer)
        self.assertIn('image?.getAttribute("src")', conversation_list_renderer)
        self.assertIn('document.createElement("template")', conversation_list_renderer)
        self.assertIn("nextAvatar.replaceWith(previous.avatar);", conversation_list_renderer)
        self.assertIn("list.replaceChildren(template.content);", conversation_list_renderer)
        self.assertIn("pendingAvatarSwaps.forEach(scheduleConversationAvatarSwap);", conversation_list_renderer)
        self.assertIn("function scheduleConversationAvatarSwap", app_js)
        self.assertIn('currentAvatar.dataset.pendingAvatarSrc !== nextSrc', app_js)
        self.assertIn("renderConversationList(list);", conversation_region_refresh)
        self.assertNotIn("list.innerHTML = conversationListHtml();", conversation_region_refresh)
        self.assertNotIn("content-visibility: auto;", app_css)
        self.assertNotIn("contain-intrinsic-size: auto 96px;", app_css)
        self.assertIn('id="side-avatar" aria-hidden="true" hidden></div>', index_html)
        self.assertIn("avatar.hidden = true", app_js)
        self.assertIn("禁止使用数字、字符串首个字符", agents_md)

    def test_relationship_center_and_parented_navigation_are_first_class(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")

        self.assertIn('id: "social"', app_js)
        self.assertIn('const SOCIAL_TABS = ["friends", "apply", "follows", "fans", "visitors", "black"]', app_js)
        self.assertIn('name: "关系中心"', app_js)
        self.assertIn('const MATCH_HUB_TABS = ["match", "voice", "bottle"]', app_js)
        self.assertNotIn("LEGACY_MATCH_ROUTES", app_js)
        self.assertNotIn('["room", "语音房"]', app_js)
        self.assertNotIn("function pageRoom", app_js)
        self.assertIn('name: "资产与权益"', app_js)
        self.assertIn('name: "任务与奖励"', app_js)
        self.assertIn('const LEGACY_RELATION_ROUTES = { friends: "friends", visitors: "visitors" }', app_js)
        self.assertIn("function socialRouteHash", app_js)
        self.assertIn(".nav-children", app_css)
        self.assertIn(".relationship-tabs", app_css)
        self.assertIn("function friendApplicationsHtml(envelope)", app_js)
        self.assertIn("function friendApplicationDirection(item)", app_js)
        self.assertIn('direction === "incoming" && status === "pending"', app_js)
        self.assertIn("function markFriendApplicationAccepted(button)", app_js)
        self.assertIn(".friend-application-groups", app_css)
        self.assertIn(".friend-application-status--accepted", app_css)
        self.assertNotIn("更多服务", index_html)
        self.assertIn('id="tools-nav-section"', index_html)
        self.assertNotIn('data-route="friends"', app_js)
        self.assertNotIn('data-route="visitors"', app_js)
        self.assertIn('{"id": "social", "name": "关系中心"', bff_server_py)
        for endpoint in (
            "/api/im/conversations",
            "/api/im/messages",
            "/api/social/friends",
            "/api/social/visitors",
            "/api/social/visit",
        ):
            self.assertIn(endpoint, app_js)
        self.assertIn('class="conversation-layout', app_js)
        self.assertIn('id="profile-dialog"', index_html)
        self.assertIn('mediaUrl(user.avatar || user.portrait)', app_js)
        self.assertIn('avatar.appendChild(image)', app_js)
        self.assertIn('readConversationPeers: new Map()', app_js)
        self.assertIn('refreshList: false', app_js)
        self.assertIn('data-action="agree-friend" data-id=', app_js)
        self.assertIn("function appendFriendApplicationItems(container, items)", app_js)
        self.assertNotIn("function friendApplicationHtml(data)", app_js)
        self.assertIn('data-action="friend-apply-load-more"', app_js)
        self.assertIn("function syncFriendApplicationCount", app_js)
        self.assertIn("markFriendApplicationAccepted(button)", app_js)
        self.assertIn("applyResult.value.data?.has_more === true", app_js)
        self.assertIn("clearRelationshipCache", app_js)
        self.assertIn('data-action="social-tab"', app_js)
        self.assertNotIn("<strong>访客足迹</strong><span>谁看过我、我看过谁</span>", app_js)
        self.assertNotIn('class="message-shortcuts"', app_js)

    def test_purchase_and_recharge_ui_and_bff_routes_are_removed_but_membership_remains(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")
        pay_adapter = root / "bbw_protocol" / "adapters" / "pay.py"

        for removed in (
            'data-action="buy-card"',
            'data-action="wallet-svip"',
            'data-action="wallet-exchange"',
            'data-form="wallet-coin"',
            'data-form="wallet-vip"',
            'api("/api/pay/card"',
            'api("/api/pay/coin"',
            'api("/api/pay/vip"',
        ):
            self.assertNotIn(removed, app_js)
        for removed in (
            'if path == "/api/pay/coin"',
            'if path == "/api/pay/vip"',
            'if path == "/api/pay/card"',
            'if path == "/api/pay/capabilities"',
            'if path == "/api/wallet/svip-try"',
            'if path == "/api/wallet/exchange-vip"',
            'if path == "/api/wallet/send-gift"',
        ):
            self.assertNotIn(removed, bff_server_py)
        self.assertFalse(pay_adapter.exists())
        self.assertIn("const membership = data.membership || user", app_js)
        self.assertIn("会员状态由服务端资料下发并自动刷新", app_js)
        self.assertIn('"membership": {', bff_server_py)
        self.assertIn('"source": "server"', bff_server_py)
        self.assertIn('"read_only": True', bff_server_py)
        self.assertIn('"vip": str(user.get("vip") or "0")', bff_server_py)
        self.assertIn('"svip": str(user.get("svip") or "0")', bff_server_py)

    def test_system_customer_service_conversation_is_read_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('const SYSTEM_CUSTOMER_SERVICE_UID = "1"', app_js)
        self.assertIn("isSystemCustomerServicePeer(S.activePeer)", app_js)
        self.assertIn("系统客服消息无需回复", app_js)
        self.assertIn("if (isSystemCustomerServicePeer(peer))", app_js)

    def test_message_page_uses_automatic_sync_and_local_conversation_creation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        conversation_card = app_js.split("function conversationCard(item)", 1)[1].split(
            "function visitorCard", 1
        )[0]
        chat_pane = app_js.split("function chatPaneHtml()", 1)[1].split("function refreshMessageConversationRegion", 1)[0]
        select_action = app_js.split(
            'if (action === "open-chat" || action === "select-conversation")', 1
        )[1].split('if (action === "close-conversation")', 1)[0]

        self.assertIn("messageSyncTimer: null", app_js)
        self.assertIn("ensureConversationForPeer(uid", app_js)
        self.assertIn("updateConversationActivity(peer", app_js)
        self.assertIn("conversationAvatarHtml(avatar)", conversation_card)
        self.assertIn("conversationProfilesByUid: new Map()", app_js)
        self.assertIn("function conversationAvatar(item)", app_js)
        self.assertIn("function preserveConversationAvatar(preferred, fallback)", app_js)
        self.assertIn("Boolean(conversation._avatar_from_fallback)", app_js)
        self.assertIn("_avatar_from_fallback: inherited", app_js)
        self.assertIn("function applyCachedConversationProfile(item)", app_js)
        self.assertIn("const avatar = currentAvatar || profileAvatar;", app_js)
        self.assertIn("async function hydrateConversationProfiles()", app_js)
        self.assertIn("function conversationProfileForPeer(rows, peer)", app_js)
        self.assertIn("function conversationProfileNeedsHydration(item)", app_js)
        self.assertIn(".filter(conversationProfileNeedsHydration)", app_js)
        self.assertIn("conversationNameIsPlaceholder(conversationDisplayName(item), peer)", app_js)
        self.assertIn(".map(applyCachedConversationProfile);", app_js)
        self.assertIn("return profiles.length === 1 && idless.length === 1 ? idless[0] : null;", app_js)
        self.assertNotIn("profiles[0] ||", app_js)
        self.assertIn("function timUserProfileRows(result)", app_js)
        self.assertIn("function rememberTimConversationProfiles(rows)", app_js)
        self.assertIn("function normalizedConversationProfile(profile, peer)", app_js)
        self.assertIn("_resolved: true", app_js)
        self.assertIn("S.chat.getUserProfile({ userIDList })", app_js)
        self.assertIn("await waitForConversationProfileSdk()", app_js)
        self.assertIn("CONVERSATION_PROFILE_SDK_WAIT_MS = 1200", app_js)
        self.assertIn("/api/profile/users?uids=", app_js)
        self.assertIn("CONVERSATION_PROFILE_REST_BATCH_SIZE = 12", app_js)
        self.assertIn("conversationProfileFetchedAt: new Map()", app_js)
        self.assertIn("CONVERSATION_PROFILE_TTL_MS", app_js)
        self.assertIn(
            "const ttl = conversationPeerNeedsHydration(peer)",
            app_js,
        )
        self.assertIn("function conversationProfileStorageKey", app_js)
        self.assertIn("sessionStorage.setItem(key", app_js)
        self.assertIn("syncConversationProfileAccount();", app_js)
        self.assertIn("function preserveConversationDisplayName(preferred, fallback)", app_js)
        self.assertIn("void hydrateConversationProfiles();", app_js)
        self.assertNotIn('data-action="im-connect"', app_js)
        self.assertNotIn('id="reload-page"', index_html)
        self.assertNotIn("avatarHtml(", chat_pane)
        self.assertNotIn("UID ${esc", chat_pane)
        self.assertIn('refreshList: action !== "select-conversation"', select_action)
        self.assertNotIn("refreshList: true", select_action)

    def test_authenticated_boot_preloads_conversations_and_realtime_unread_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        start_services = app_js.split("function startMessageServices()", 1)[1].split(
            "function buildNav", 1
        )[0]
        background_sync = app_js.split("function syncMessagesInBackground", 1)[1].split(
            "function startMessageSyncTimer", 1
        )[0]
        summary_sync = app_js.split("function syncConversationSummaryInBackground", 1)[1].split(
            "function syncMessagesInBackground", 1
        )[0]
        summary_refresh = app_js.split("function refreshConversationSummary", 1)[1].split(
            "function stopMessageSyncTimer", 1
        )[0]
        preview_hydration = app_js.split("async function hydrateStaleConversationPreviews", 1)[1].split(
            "function loadArchivedConversationSummary", 1
        )[0]
        unread_recalculation = app_js.split("function recalculateUnreadTotal", 1)[1].split(
            "function markConversationRead", 1
        )[0]

        self.assertEqual(app_js.count("void startMessageServices();"), 2)
        self.assertIn("function scheduleAuthenticatedServices", app_js)
        self.assertIn("scheduleAuthenticatedServices(1000);", app_js)
        self.assertIn("startMessageSyncTimer();", start_services)
        self.assertIn("loadArchivedConversationSummary()", start_services)
        self.assertIn("runMessageSyncCycle({ force: true })", start_services)
        self.assertIn("return Promise.allSettled([", start_services)
        self.assertNotIn(".then(() => runMessageSyncCycle", start_services)
        self.assertIn("refreshConversationSummary({ force })", summary_sync)
        self.assertIn("ensureTimConnected({ background: true })", background_sync)
        self.assertIn("return Promise.allSettled(tasks);", background_sync)
        self.assertNotIn('S.route === "msg" && !S.imConnected', background_sync)
        self.assertIn("S.conversationNextRefreshAt = Date.now() + CONVERSATION_REFRESH_ERROR_MS", summary_refresh)
        self.assertIn('authority: "live"', summary_refresh)
        self.assertNotIn("loadArchivedConversationSummary()\n    .then", summary_refresh)
        self.assertIn("/api/archive/conversations?limit=100", app_js)
        self.assertIn(
            "/api/archive/messages?peer=${encodeURIComponent(target)}&limit=200",
            app_js,
        )
        self.assertIn("imArchiveLoadedPeers: new Set()", app_js)
        self.assertIn("const shouldLoadArchive = !S.imArchiveLoadedPeers.has(target)", app_js)
        self.assertIn("if (shouldLoadArchive)", app_js)
        self.assertIn("S.imArchiveLoadedPeers.add(target)", app_js)
        self.assertIn("function chatLogIsNearBottom", app_js)
        self.assertIn("const shouldStickToBottom = forceBottom || chatLogIsNearBottom(log)", app_js)
        self.assertIn("function conversationMessageRevision", app_js)
        self.assertIn("nextRevision !== previousRevision", app_js)
        self.assertIn("void loadConversationMessages(activePeer, { force: true })", app_js)
        self.assertIn('typeof S.chat.isReady === "function"', app_js)
        self.assertIn('window.addEventListener("online"', app_js)
        self.assertIn('["archive", "http", "history"]', app_js)
        self.assertIn("function mergeConversationPair", app_js)
        self.assertIn("preview_timestamp", app_js)
        self.assertIn("unread_authoritative", app_js)
        self.assertIn("function hydrateStaleConversationPreviews", app_js)
        self.assertIn("conversationPreviewHydrationPromise", app_js)
        self.assertIn("generation === S.sessionGeneration", app_js)
        self.assertIn("refreshList: false", preview_hydration)
        self.assertIn('let changed = false;', preview_hydration)
        self.assertEqual(
            preview_hydration.count(
                "refreshMessageConversationRegion({ refreshList: true, refreshPane: false });"
            ),
            1,
        )
        self.assertIn(".sort(compareMessageOrder)", app_js)
        self.assertNotIn("previewTimestamp + 2000", app_js)
        self.assertIn("summary=1&at=", app_js)
        self.assertIn("BroadcastChannel(\"bbw-message-summary\")", app_js)
        self.assertIn("navigator.locks.request", app_js)
        self.assertIn("updateUnreadBadges();", unread_recalculation)
        self.assertIn("data-unread-badge", app_js)

        tim_conversation_sync = app_js.split(
            'if (typeof chat.getConversationList !== "function") return;', 1
        )[1].split("void refreshVisiblePeerPresence", 1)[0]
        self.assertIn("recalculateUnreadTotal();", tim_conversation_sync)

    def test_message_read_receipts_and_peer_presence_are_rendered(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        self.assertIn("/api/im/presence?uids=", app_js)
        self.assertIn("data-presence-uid", app_js)
        self.assertIn("presence: true", app_js)
        self.assertIn('const unknown = presence.status === "unknown";', app_js)
        self.assertIn('unknown ? "" : esc(', app_js)
        self.assertIn("MESSAGE_READ_BY_PEER", app_js)
        self.assertIn("USER_STATUS_UPDATED", app_js)
        self.assertIn('return optionalReadState(entry.peerRead', app_js)
        self.assertIn("chat-message-state", app_css)
        self.assertIn("chat-read-indicator", app_css)
        self.assertIn("chat-message-time", app_css)
        self.assertIn("消息发出时间", app_js)
        self.assertIn("消息已读时间", app_js)
        self.assertIn("info.timestamp < sentAt - 60_000", app_js)
        self.assertIn("function chatMessageReadTimeInfo", app_js)
        self.assertIn('return { label: "已发送", className: "is-sent", indicator: false };', app_js)
        self.assertIn('api("/api/im/read"', app_js)
        self.assertIn("function reportConversationRead(peer)", app_js)
        self.assertIn("function reportConversationsRead(peers)", app_js)
        self.assertIn("function scheduleConversationReadReport(peer", app_js)
        self.assertIn("width: fit-content", app_css)
        self.assertIn("conversation-read-state", app_css)
        self.assertIn("presence-badge", app_css)

        user_card = app_js.split("function userCard", 1)[1].split("function formatSocialTime", 1)[0]
        self.assertIn('<div class="card-actions">${presence}${actions.join("")}</div>', user_card)
        self.assertNotIn('<strong>${esc(name)}</strong>${presence}', user_card)
        self.assertIn(".card-actions > .presence-badge", app_css)

    def test_message_composer_supports_ctrl_enter_to_send(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('aria-keyshortcuts="Control+Enter"', app_js)
        self.assertIn('event.key !== "Enter"', app_js)
        self.assertIn("!event.ctrlKey", app_js)
        self.assertIn("event.shiftKey", app_js)
        self.assertIn('event.target.closest("#im-text")', app_js)
        self.assertIn("form.requestSubmit(submitter)", app_js)
        self.assertIn("submittedDraftRevision", app_js)
        self.assertIn("S.imComposerDraftRevision === submittedDraftRevision", app_js)
        self.assertIn("submittedPeerDraftRevision", app_js)
        self.assertIn("visiblePeerDraftUnchanged", app_js)
        self.assertIn('String(S.activePeer || "") === peer', app_js)
        self.assertIn("imComposerDrafts: new Map()", app_js)
        self.assertIn("restoreChatComposerDraft(uid)", app_js)

    def test_text_message_send_is_optimistic_without_button_spinner(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        send_text = app_js.split("async function sendTextMessage", 1)[1].split(
            "function progressRatio", 1
        )[0]
        send_form = app_js.split('if (kind === "im-send")', 1)[1].split(
            'if (kind === "lab-call")', 1
        )[0]
        submit_listener = app_js.split('document.addEventListener("submit"', 1)[1].split(
            'document.addEventListener("keydown"', 1
        )[0]

        self.assertIn('delivery: "sending"', send_text)
        self.assertIn("appendLocalMessage(pending)", send_text)
        self.assertLess(
            send_text.index("appendLocalMessage(pending)"),
            send_text.index("await S.chat.sendMessage(message)"),
        )
        self.assertIn("updateLocalMessage(pendingID, replacement)", send_text)
        self.assertIn('delivery: "failed"', send_text)
        self.assertIn("consumeSubmittedChatDraft", send_form)
        self.assertIn('if (form.dataset.form === "im-send")', submit_listener)
        self.assertIn("handleProductForm(form, submitter).catch", submit_listener)
        optimistic_branch = submit_listener.split('if (form.dataset.form === "im-send")', 1)[
            1
        ].split("return;", 1)[0]
        self.assertNotIn("withPending", optimistic_branch)
        self.assertIn('entry.delivery === "sending" && entry.kind !== "text"', app_js)
        self.assertIn('if (entry.kind === "text") return Boolean', app_js)

    def test_conversation_list_defaults_expanded_and_supports_horizontal_collapse(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        self.assertIn('data-action="toggle-conversation-list"', app_js)
        self.assertIn("conversationListCollapsed: false", app_js)
        self.assertNotIn("CONVERSATION_LIST_COLLAPSED_KEY", app_js)
        self.assertIn("conversation-expand-toggle", app_js)
        self.assertIn("is-list-collapsed", app_css)
        self.assertIn(".conversation-layout.is-list-collapsed .conversation-list-pane", app_css)
        self.assertIn("display: none", app_css)

    def test_conversation_list_supports_batch_selection_select_all_and_delete(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        self.assertIn("conversationBatchMode: false", app_js)
        self.assertIn("selectedConversationPeers: new Set()", app_js)
        self.assertIn('data-action="start-conversation-batch"', app_js)
        self.assertIn('data-action="finish-conversation-batch"', app_js)
        self.assertIn('type="checkbox" data-action="toggle-conversation-selection"', app_js)
        self.assertIn('data-action="toggle-conversation-select-all"', app_js)
        self.assertIn('data-action="delete-selected-conversations"', app_js)
        self.assertIn("function toggleAllConversationSelections()", app_js)
        self.assertIn("function startConversationBatchDeleteConfirmation(button)", app_js)
        self.assertIn("function removeSelectedConversationListItems()", app_js)
        self.assertIn("removeConversationListItems(selected)", app_js)
        self.assertIn("已从列表移除 ${result.count} 个聊天，消息仍然保留", app_js)
        self.assertIn("S.conversationBatchMode ||", app_js)
        self.assertIn("CONVERSATION_DISMISS_LIMIT = 500", app_js)
        self.assertIn(".conversation-batch-toolbar", app_css)
        self.assertIn(".conversation-select-control input", app_css)
        self.assertIn(".conversation-item.is-batch-selecting.is-selected .conversation-card", app_css)

    def test_message_page_uses_compact_chat_surface_and_list_read_action(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        page_messages = app_js.split("async function pageMessages", 1)[1].split("async function pageMatching", 1)[0]

        self.assertNotIn('id="mark-all-read-top"', index_html)
        self.assertIn('class="pane-head-actions"', app_js)
        self.assertIn('id="mark-all-read-list" data-action="mark-all-read"', app_js)
        self.assertLess(
            page_messages.index("conversationListControlsHtml()"),
            page_messages.index('id="conversation-list"'),
        )
        self.assertIn("function syncMessageReadAction", app_js)
        self.assertNotIn("function syncTopbarActions", app_js)
        self.assertNotIn("message-toolbar", page_messages)
        self.assertNotIn('id="im-conn-status"', page_messages)
        self.assertIn(".message-page > .conversation-layout", app_css)
        self.assertIn("--message-content-max: 1800px", app_css)
        self.assertIn('document.body.classList.toggle("message-route-active"', app_js)
        self.assertIn("body.chat-conversation-open .topbar", app_css)
        self.assertIn("--top-h: 0px", app_css)
        self.assertIn("--top-h: 52px", app_css)
        self.assertIn('id="open-menu"', index_html)
        self.assertNotIn('class="topbar-copy"', index_html)
        self.assertNotIn('id="page-title"', index_html)
        self.assertNotIn('id="page-subtitle"', index_html)
        self.assertIn("top: calc(var(--app-viewport-offset-top, 0px) + 52px + var(--safe-top))", app_css)
        self.assertIn(".chat-head {\n  min-height: 56px", app_css)
        self.assertIn("grid-template-columns: minmax(300px, 360px) minmax(0, 1fr)", app_css)
        self.assertNotIn(".message-toolbar", app_css)


class RichMessageFrontendContractTests(unittest.TestCase):
    def _app_fragment(self, app_js: str, start: str, end: str) -> str:
        start_index = app_js.find(start)
        self.assertGreaterEqual(start_index, 0, f"missing JavaScript boundary: {start}")
        end_index = app_js.find(end, start_index + len(start))
        self.assertGreater(end_index, start_index, f"missing JavaScript boundary: {end}")
        return app_js[start_index:end_index]

    def test_sent_messages_can_be_recalled_on_desktop_and_mobile(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")

        self.assertIn('data-action="revoke-chat-message"', app_js)
        self.assertIn('data-action="edit-revoked-message"', app_js)
        self.assertIn("S.chat.revokeMessage(entry.rawMessage)", app_js)
        self.assertIn('api("/api/im/rest/revoke"', app_js)
        self.assertIn("MESSAGE_REVOKED", app_js)
        self.assertIn("MESSAGE_REVOKE_DEFAULT_WINDOW_MS = 2 * 60 * 1000", app_js)
        self.assertIn('label: "服务端撤回"', app_js)
        self.assertIn("function editRevokedMessage(id)", app_js)
        self.assertIn("recalledText", app_js)
        self.assertIn("你撤回了一条消息", app_js)
        self.assertIn("@media (hover: hover) and (pointer: fine)", app_css)
        self.assertIn(".chat-message-action", app_css)
        self.assertIn(".chat-revoked-edit", app_css)
        self.assertIn('if path == "/api/im/rest/revoke"', server_py)
        self.assertIn("20023", server_py)

    def test_replayed_revocations_wait_for_history_before_rendering(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        revoke_handler = self._app_fragment(
            app_js,
            "function applyMessageRevokedEvent(event",
            "function attachTimHandlers",
        )
        tim_handlers = self._app_fragment(
            app_js,
            "function attachTimHandlers(chat, TIM, credential)",
            "function isCurrentAuthenticatedSession",
        )
        message_loader = self._app_fragment(
            app_js,
            "async function loadConversationMessages(peer",
            "function oldestPeerMessageTimestamp",
        )
        chat_log = self._app_fragment(
            app_js,
            "function chatLogHtml()",
            "function scrollChatLogToBottom",
        )
        message_matcher = self._app_fragment(
            app_js,
            "function messagesReferToSameMessage(left, right)",
            "function compareMessageOrder",
        )

        self.assertIn("imPendingRevocations: new Map()", app_js)
        self.assertIn("function timMessageDirection", app_js)
        self.assertIn("function messagesReferToSameMessage", app_js)
        self.assertIn(
            "if (leftDirection && rightDirection && leftDirection !== rightDirection) return false;",
            message_matcher,
        )
        self.assertIn("!S.imMessageLoadedPeers.has(String(peer))", revoke_handler)
        self.assertIn("queuePendingMessageRevocation(revoked)", revoke_handler)
        self.assertIn("else if (peer && (revoked.id || revoked.msgKey))", revoke_handler)
        self.assertIn("if (!peer) return;", tim_handlers)
        self.assertGreaterEqual(tim_handlers.count("if (deferReplayedMessageRevocation(entry)) return;"), 2)
        self.assertIn("function deferReplayedMessageRevocation", app_js)
        self.assertIn("mergePendingMessageRevocations(", message_loader)
        self.assertIn('if (!ok || data?.ok === false) throw new Error("服务器聊天记录暂时不可用")', message_loader)
        self.assertIn('if (!ok || data?.ok === false) throw new Error("归档聊天记录暂时不可用")', message_loader)
        self.assertIn("if (!fulfilled.length) return;", message_loader)
        self.assertLess(
            message_loader.index("if (!fulfilled.length) return;"),
            message_loader.index("mergePendingMessageRevocations("),
        )
        self.assertIn("if (!wasLoaded && S.activePeer === target) refreshChatLog();", message_loader)
        self.assertIn("entries.every((entry) => entry.revoked)", chat_log)
        self.assertIn('String(entry.peer || "") === activePeer', chat_log)
        self.assertIn("正在加载聊天记录", chat_log)

    def test_conversation_loading_starts_before_the_chat_pane_is_redrawn(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        route_restore = self._app_fragment(
            app_js,
            'if (target === "msg") {',
            "if (!force && cached?.fresh) return;",
        )
        open_chat = self._app_fragment(
            app_js,
            'if (action === "open-chat" || action === "select-conversation") {',
            'if (action === "close-conversation") {',
        )

        self.assertLess(
            route_restore.index("void loadConversationMessages(S.activePeer)"),
            route_restore.index("refreshMessageConversationRegion("),
        )
        self.assertLess(
            open_chat.index("void loadConversationMessages(uid)"),
            open_chat.index("refreshMessageConversationRegion({"),
        )

    def test_sdk_conversation_updates_are_authoritative_for_unread_count(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        local_conversation = self._app_fragment(
            app_js,
            "function ensureConversationForPeer(peer",
            "function updateConversationActivity(",
        )
        tim_handlers = self._app_fragment(
            app_js,
            "function attachTimHandlers(chat, TIM, credential)",
            "function isCurrentAuthenticatedSession",
        )

        self.assertIn("unread_observed_at: 0", local_conversation)
        self.assertIn("unread_authoritative: false", local_conversation)
        self.assertNotIn("currentUnread + 1", tim_handlers)
        self.assertNotIn("shouldIncrementUnread", tim_handlers)
        self.assertIn('unreadCount: entry.type !== "mine" && active ? 0 : undefined', tim_handlers)
        self.assertIn("TIM.EVENT?.CONVERSATION_LIST_UPDATED", tim_handlers)
        self.assertIn(".map(normalizeTimConversation)", tim_handlers)
        self.assertIn("S.conversations = mergeConversationSources(S.conversations, updated)", tim_handlers)

    def test_incoming_message_toast_prefers_the_sender_nickname(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        sender_info = self._app_fragment(
            app_js,
            "function incomingMessageSenderInfo(message, conversation, peer)",
            "function conversationNameIsPlaceholder",
        )

        self.assertIn("function incomingMessageSenderInfo(message, conversation, peer)", app_js)
        self.assertIn("message?.nick", app_js)
        self.assertIn("S.conversationProfilesByUid.get(target)", app_js)
        self.assertLess(sender_info.index("conversationDisplayName(conversation)"), sender_info.index("cachedProfile?.nickname"))
        self.assertIn(': { name: "", authoritative: false }', app_js)
        self.assertIn("name: sender.name", app_js)
        self.assertIn("replaceName: sender.authoritative", app_js)
        self.assertIn("replaceName || conversationNameIsPlaceholder(currentName, target)", app_js)
        self.assertIn("if (active && sender.authoritative) S.activePeerName = sender.name", app_js)
        self.assertIn("const notificationName = sender.name ||", app_js)
        self.assertIn('toast(`收到 ${notificationName} 的新消息`)', app_js)
        self.assertNotIn("toast(`收到来自 ${peer} 的新消息`)", app_js)

    def test_sticker_packages_use_image_grid_and_group_tabs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        sticker_panel = self._app_fragment(
            app_js,
            "function chatComposerPanelHtml()",
            "function chatComposerHtml()",
        )

        self.assertIn('imStickerActiveGroup: ""', app_js)
        self.assertIn('role="tablist" aria-label="表情包分组"', app_js)
        self.assertIn('data-action="select-sticker-group"', app_js)
        self.assertIn('data-sticker-image', app_js)
        self.assertIn('TUI_EMOJI_STICKER_GROUP_ID = "built-in-tuiemoji"', app_js)
        self.assertIn('name: "内置表情"', app_js)
        self.assertIn('data-action="insert-chat-tui-emoji"', app_js)
        self.assertIn('emoji?.url', app_js)
        self.assertIn('内置表情与收藏表情', app_js)
        self.assertNotIn('data-panel="emoji"', app_js)
        self.assertNotIn('const CHAT_TEXT_EMOTICONS', app_js)
        self.assertIn('first.groupName ||', app_js)
        self.assertIn('.chat-sticker-tabs', app_css)
        self.assertIn('.chat-sticker-tab.on', app_css)
        self.assertIn('repeat(auto-fill, minmax(68px, 1fr))', app_css)
        self.assertIn('repeat(4, minmax(0, 1fr))', app_css)
        self.assertNotIn('class="chat-sticker-group-head"', sticker_panel)

    def test_tim_rich_media_recording_flash_and_upload_plugin_are_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        for sdk_call in (
            "chat.createImageMessage(options)",
            "chat.createAudioMessage(options)",
            "chat.createFaceMessage({",
        ):
            self.assertIn(sdk_call, app_js)
        self.assertIn("new MediaRecorder(", app_js)
        self.assertIn('api("/api/im/flash/send"', app_js)
        self.assertIn('api("/api/im/flash/get"', app_js)
        self.assertIn("ensureTimUploadPluginLoaded()", app_js)
        self.assertIn(
            'registerPlugin({ "tim-upload-plugin": window.TIMUploadPlugin })',
            app_js,
        )

    def test_ios_tuiemoji_tokens_use_the_matching_apk_small_expression(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        assets = sorted((root / "bbw_web" / "static" / "tuiemoji").glob("emoji_*.png"))

        self.assertEqual(len(assets), 62)
        self.assertTrue((root / "bbw_web" / "static" / "tuiemoji" / "emoji_3.png").is_file())
        self.assertIn('["Guffaw", "大笑"]', app_js)
        self.assertIn("function messageTextHtml(value)", app_js)
        self.assertIn('/static/tuiemoji/emoji_${emoji.index}.png', app_js)
        self.assertIn("messageTextHtml(entry.text", app_js)
        self.assertIn("tuiEmojiPreviewText", app_js)
        self.assertIn(".chat-inline-emoji", app_css)

    def test_rich_media_csp_allows_blob_images_and_playback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")
        api_py = (root / "bbw_web" / "api.py").read_text(encoding="utf-8")

        self.assertIn("img-src 'self' data: blob: https:", server_py)
        self.assertIn("media-src 'self' data: blob: https:", server_py)
        self.assertIn("media-src 'self' data: blob: https:", api_py)

    def test_moment_videos_use_apk_url_rewrite_and_playback_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        media_url = app_js.split("function mediaUrl(value)", 1)[1].split("function validAvatarValue", 1)[0]
        moment_media = app_js.split("function momentMediaHtml(post)", 1)[1].split("function momentOwnershipMenu", 1)[0]
        transcode_service = compose.split("  transcode-worker:", 1)[1].split("  scheduler:", 1)[0]

        self.assertIn("moyuanoss\\.oss-cn-shanghai\\.aliyuncs\\.com", app_js)
        self.assertIn("appletattachment\\.oss-cn-beijing\\.aliyuncs\\.com", app_js)
        self.assertLess(media_url.index("APK_MEDIA_ORIGIN_RE"), media_url.index('raw.startsWith("//")'))
        self.assertIn('data-media-playback data-moment-video="true"', moment_media)
        self.assertIn('data-media-mode="original"', moment_media)
        self.assertIn('data-post-id="${esc(', moment_media)
        self.assertIn('data-original-source="${esc(video)}"', moment_media)
        self.assertIn('data-media-source="${esc(', moment_media)
        self.assertIn('data-video-frame-required="true"', moment_media)
        self.assertIn('controlslist="nodownload noremoteplayback"', moment_media)
        self.assertIn("disablepictureinpicture", moment_media)
        self.assertIn("disableremoteplayback", moment_media)
        self.assertIn('draggable="false"', moment_media)
        self.assertIn('data-action="retry-chat-playback"', moment_media)
        self.assertNotIn("<a ", moment_media)
        self.assertNotIn("href=", moment_media)
        self.assertNotIn("打开原视频", moment_media)
        self.assertNotIn("复制链接", moment_media)
        self.assertNotIn("<source", moment_media)
        self.assertIn("function scheduleVideoFrameCompatibilityCheck(video)", app_js)
        self.assertIn("function prepareMomentVideoCompatibility(video", app_js)
        self.assertIn('api("/api/media/compat-video/prepare"', app_js)
        self.assertIn("post_id: postId", app_js)
        self.assertIn("data.status === \"ready\"", app_js)
        self.assertIn("applyMomentVideoCompatibility(video", app_js)
        self.assertIn("video.dataset.mediaMode !== \"compat\"", app_js)
        self.assertIn("video.dataset.playbackRequested !== \"1\"", app_js)
        self.assertIn("HTMLMediaElement.HAVE_CURRENT_DATA", app_js)
        self.assertIn("MOMENT_VIDEO_INITIAL_FRAME_WAIT_MS", app_js)
        self.assertIn("mediaErrorCode === 3 || mediaErrorCode === 4", app_js)
        playback_error = app_js.split("function handleChatPlaybackError(media)", 1)[
            1
        ].split("function openChatMediaViewer", 1)[0]
        decode_fallback = playback_error.split("const mediaErrorCode", 1)[1].split(
            "const retryState", 1
        )[0]
        self.assertIn("prepareMomentVideoCompatibility(media)", decode_fallback)
        self.assertNotIn("playbackRequested", decode_fallback)
        self.assertNotIn('setMomentVideoFallback(media, "视频加载失败"', decode_fallback)
        self.assertIn("function cancelPendingVideoFrameCallback(video)", app_js)
        self.assertIn("video.cancelVideoFrameCallback(callbackId)", app_js)
        self.assertIn('media.dataset.mediaMode === "compat"', app_js)
        self.assertIn('media.dataset.mediaMode = "original"', app_js)
        self.assertIn("正在准备兼容版本…", app_js)
        self.assertIn("视频暂时无法播放", app_js)
        self.assertIn("当前浏览器暂时无法播放此视频", app_js)
        self.assertNotIn("可打开原视频或下载后播放", app_js)
        self.assertIn(".moment-video-wrap", app_css)
        self.assertNotIn(".moment-playback-actions", app_css)
        context_menu_guard = app_js.split(
            'document.addEventListener(\n  "contextmenu",', 1
        )[1].split('document.addEventListener(\n  "loadedmetadata",', 1)[0]
        self.assertIn('video[data-moment-video="true"]', context_menu_guard)
        self.assertIn("event.preventDefault()", context_menu_guard)
        moment_video_css = app_css.split(".moment-video {", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-touch-callout: none", moment_video_css)
        self.assertIn("user-select: none", moment_video_css)
        self.assertIn("stop_grace_period: 32m", transcode_service)

    def test_incoming_media_retries_and_reconciles_without_page_reload(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        self.assertIn("CHAT_MEDIA_RETRY_DELAYS_MS", app_js)
        self.assertIn("schedulePeerMediaReconcile(peer)", app_js)
        self.assertIn("function handleChatMediaError(image)", app_js)
        self.assertIn('img[data-media-source]', app_js)
        self.assertIn("MESSAGE_MODIFIED", app_js)
        self.assertIn("NET_STATE_CHANGE", app_js)
        self.assertIn("MESSAGE_PEER_SYNC_FALLBACK_MS = 12 * 1000", app_js)
        self.assertIn("MESSAGE_PEER_SYNC_REALTIME_MS = 90 * 1000", app_js)
        self.assertIn("MESSAGE_POLICY_SYNC_MS = 60 * 1000", app_js)
        self.assertIn("图片加载失败，点击重试", app_js)
        self.assertIn("function handleChatPlaybackError(media)", app_js)
        self.assertIn('data-action="retry-chat-playback"', app_js)
        self.assertIn('data-action="retry-chat-message"', app_js)
        self.assertIn("imMediaRetryState: new Map()", app_js)
        self.assertIn("if (S.imMediaReconcileTimers.has(target)) return", app_js)
        self.assertIn(".chat-media-fallback", app_css)
        self.assertIn(".chat-playback-fallback", app_css)
        self.assertNotIn('matches("img[data-media]")) event.target.hidden = true', app_js)

    def test_media_picker_and_runtime_whitelists_match_tim_2276(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        composer = self._app_fragment(app_js, "function chatComposerHtml()", "function chatPaneHtml()")
        constants = self._app_fragment(app_js, "const TIM_VIDEO_MIME_TYPES", "function parseJsonValue")
        validator = self._app_fragment(app_js, "function validateChatFile", "async function sendFlashPhoto")
        normalizer = self._app_fragment(app_js, "function normalizeChatPickerFile", "function validateChatFile")

        video_input = composer.split('id="im-file-video"', 1)[1].split("/>", 1)[0]
        video_accept = video_input.split('accept="', 1)[1].split('"', 1)[0]
        self.assertEqual(video_accept, ".mp4,.mov,video/mp4,video/quicktime,video/mov")
        self.assertIn('const TIM_VIDEO_MIME_TYPES = new Set(["video/mp4", "video/quicktime", "video/mov"]);', constants)
        self.assertIn("const TIM_VIDEO_FILE_EXTENSION_RE = /\\.(?:mp4|mov)$/i;", constants)
        self.assertIn("TIM_VIDEO_FILE_EXTENSION_RE.test(name)", validator)
        self.assertIn("TIM_VIDEO_MIME_TYPES.has(mime)", validator)
        self.assertIn('mov: "video/quicktime"', constants)
        self.assertIn("GENERIC_PICKER_MIME_TYPES.has(currentType)", normalizer)
        self.assertIn("return new File([file], file.name", normalizer)

        image_input = composer.split('id="im-file-image"', 1)[1].split("/>", 1)[0]
        flash_input = composer.split('id="im-file-flash"', 1)[1].split("/>", 1)[0]
        self.assertIn(".bmp", image_input)
        self.assertIn("image/bmp", image_input)
        self.assertNotIn(".bmp", flash_input)
        self.assertNotIn("image/bmp", flash_input)
        tim_image_types = constants.split("const TIM_IMAGE_MIME_TYPES", 1)[1].split(";", 1)[0]
        flash_image_types = constants.split("const FLASH_IMAGE_MIME_TYPES", 1)[1].split(";", 1)[0]
        self.assertIn('"image/bmp"', tim_image_types)
        self.assertNotIn('"image/bmp"', flash_image_types)
        self.assertIn("isFlash ? FLASH_IMAGE_FILE_EXTENSION_RE : TIM_IMAGE_FILE_EXTENSION_RE", validator)
        self.assertIn("isFlash ? FLASH_IMAGE_MIME_TYPES : TIM_IMAGE_MIME_TYPES", validator)

    def test_sdk_remote_media_fields_replace_and_revoke_local_blob_urls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        image_media = self._app_fragment(app_js, "function normalizeImageMedia", "function normalizeEntryMedia")
        entry_media = self._app_fragment(app_js, "function normalizeEntryMedia", "function messageFlashID")
        object_urls = self._app_fragment(app_js, "function trackChatObjectUrl", "async function ensureTimMediaReady")
        sender = self._app_fragment(app_js, "async function sendTimMediaFile", "function defineFileMetadata")
        logout = self._app_fragment(app_js, "async function logout()", "async function loadMomentComments")

        self.assertIn('"imageUrl"', image_media)
        self.assertIn('"remoteAudioUrl"', entry_media)
        self.assertIn('"remoteVideoUrl"', entry_media)
        self.assertIn("URL.revokeObjectURL(url)", object_urls)
        self.assertIn("collectBlobObjectUrls", object_urls)
        self.assertIn("revokeSdkTemporaryObjectUrls", sender)
        self.assertIn("replaceUploadedLocalMediaUrl", sender)
        self.assertIn("revokeChatObjectUrl(localMedia.url)", sender)
        self.assertIn("revokeAllChatObjectUrls()", logout)

    def test_flash_countdown_starts_only_after_image_load(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        close_viewer = self._app_fragment(app_js, "function closeFlashViewer()", "async function openFlashViewer")
        open_viewer = self._app_fragment(app_js, "async function openFlashViewer", "async function pageNearby")
        onload = self._app_fragment(open_viewer, "image.onload = () => {", "image.onerror = () => {")
        onerror = self._app_fragment(open_viewer, "image.onerror = () => {", "image.src = url")

        self.assertIn("image.onload = null", onload)
        self.assertIn("image.onerror = null", onload)
        self.assertIn("hold.timer = setTimeout", onload)
        self.assertIn("}, 5000);", onload)
        self.assertIn("closeFlashViewer()", onerror)
        self.assertIn("hold.image.onload = null", close_viewer)
        self.assertIn("hold.image.onerror = null", close_viewer)
        source_index = open_viewer.index("image.src = url")
        self.assertLess(open_viewer.index("hold.timer = setTimeout"), source_index)
        self.assertNotIn("setTimeout(", open_viewer[source_index:])

    def test_voice_recorder_construction_and_start_failures_stop_tracks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        cleanup = self._app_fragment(app_js, "function resetFailedVoiceRecording", "async function startVoiceRecording")
        recording = self._app_fragment(app_js, "async function startVoiceRecording", "function moveVoiceRecording")
        constructor_failure = self._app_fragment(recording, "let recorder;", "state.recorder = recorder")
        start_failure = self._app_fragment(recording, "recorder.onerror = (event) => {", "state.timer = setInterval")

        self.assertIn("new MediaRecorder(stream", constructor_failure)
        self.assertIn('"audio/mp4;codecs=mp4a.40.2"', app_js)
        self.assertIn("for (const candidate of [...supportedMimeTypes, \"\"])", constructor_failure)
        self.assertIn("catch (error)", constructor_failure)
        self.assertIn("resetFailedVoiceRecording(state)", constructor_failure)
        self.assertIn("recorder.start(250)", start_failure)
        self.assertIn("catch (error)", start_failure)
        self.assertIn("resetFailedVoiceRecording(state)", start_failure)
        self.assertIn("state.error = String", start_failure)
        self.assertIn("track.stop()", cleanup)

    def test_video_metadata_failure_falls_back_without_blocking_mov_upload(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        metadata = self._app_fragment(app_js, "async function readVideoMetadata", "function normalizeChatPickerFile")

        self.assertIn("const url = createChatObjectUrl(file)", metadata)
        self.assertIn("video.onerror = () =>", metadata)
        self.assertIn("finish(resolve", metadata)
        self.assertIn("timer = setTimeout(", metadata)
        self.assertIn("仍将尝试通过实时消息通道发送", metadata)
        self.assertIn("metadataWarning", metadata)
        self.assertIn("clearTimeout(timer)", metadata)
        self.assertIn("video.onloadedmetadata = null", metadata)
        self.assertIn("video.onerror = null", metadata)
        self.assertIn("revokeChatObjectUrl(url)", metadata)
        self.assertIn("throw error", metadata)

    def test_mobile_chat_handles_short_landscape_keyboard_and_safe_areas(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        mobile_media = app_css.split(
            "@media (max-width: 640px), (max-height: 560px) and (max-width: 960px) and (any-pointer: coarse)",
            1,
        )[1].split("@media (any-pointer: coarse)", 1)[0]
        short_media = app_css.split(
            "@media (max-height: 560px) and (max-width: 960px) and (min-aspect-ratio: 4 / 3)",
            1,
        )[1].split("html.keyboard-visible", 1)[0]

        self.assertIn("window.visualViewport", app_js)
        self.assertIn('setProperty("--app-viewport-height"', app_js)
        self.assertIn('setProperty("--app-viewport-offset-top"', app_js)
        self.assertIn("stableVisualViewportHeight", app_js)
        self.assertIn('window.visualViewport?.addEventListener("resize"', app_js)
        self.assertIn('window.visualViewport?.addEventListener("scroll"', app_js)
        self.assertIn("closeChatComposerPanelForKeyboard()", app_js)
        self.assertIn("waitForVisualViewportRecovery", app_js)
        self.assertIn("await waitForVisualViewportRecovery(targetHeight)", app_js)
        self.assertIn("viewportLooksCompressed", app_js)
        self.assertIn("keyboardTargetHeight", app_js)
        self.assertIn("selectionStart", app_js)
        self.assertIn("selectionEnd", app_js)
        self.assertNotIn('refreshChatComposerKeepingText({ focus: panel === "emoji" })', app_js)
        self.assertIn("(max-height: 560px)", app_css)
        self.assertIn("(max-width: 960px)", app_css)
        self.assertIn("(any-pointer: coarse)", app_css)
        self.assertIn("(min-aspect-ratio: 4 / 3)", app_css)
        self.assertIn(".conversation-layout", mobile_media)
        self.assertIn("grid-template-columns: 1fr", mobile_media)
        self.assertIn(".conversation-layout.has-active .conversation-list-pane", mobile_media)
        self.assertIn(".conversation-layout.has-active .chat-pane", mobile_media)
        self.assertIn("body.chat-conversation-open .page-root.message-route", app_css)
        self.assertIn("height: var(--app-viewport-height, 100dvh)", app_css)
        self.assertIn("max-height: none", short_media)
        self.assertIn("--safe-left: env(safe-area-inset-left", app_css)
        self.assertIn("--safe-right: env(safe-area-inset-right", app_css)
        tablet_layout = app_css.split("@media (max-width: 960px)", 1)[1].split("@media (min-width: 961px)", 1)[0]
        self.assertIn(".page-root.message-route", tablet_layout)
        self.assertIn("padding-bottom: calc(var(--bottom-h) + var(--safe-bottom) + 1.2rem)", tablet_layout)
        desktop_layout = app_css.split("@media (min-width: 961px)", 1)[1].split("@media (max-width: 640px)", 1)[0]
        self.assertIn("--side-w: clamp(130.667px, 12vw, 169.333px)", desktop_layout)
        self.assertIn("@media (min-width: 961px) and (max-width: 1180px)", app_css)
        self.assertIn("min-height: 44px", app_css)
        self.assertIn("font-size: 16px", app_css)

    def test_voice_recording_explains_secure_context_and_rest_text_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        availability = self._app_fragment(app_js, "function voiceRecordingAvailability", "function syncVisualViewport")
        recording = self._app_fragment(app_js, "async function startVoiceRecording", "function moveVoiceRecording")
        self.assertIn("window.isSecureContext", availability)
        self.assertIn("录音需要安全网页环境或本机访问", availability)
        self.assertIn("voiceRecordingAvailability()", recording)
        self.assertIn("定时同步模式（约 8 秒，仅支持文本发送）", app_js)
        self.assertIn("尝试启用文本备用通道", app_js)
        self.assertIn("当前仅可发送文本", app_js)

    def test_voice_messages_use_compact_bubbles_and_web_voice_to_text(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        vendor = root / "bbw_web" / "static" / "vendor" / "tencent-cloud-chat-3.6.6.js"

        self.assertTrue(vendor.is_file())
        self.assertIn("convertVoiceToText", vendor.read_text(encoding="utf-8"))
        self.assertIn('TIM_SDK_SRC = "/static/vendor/tencent-cloud-chat-3.6.6.js"', app_js)
        self.assertIn('data-action="toggle-chat-audio"', app_js)
        self.assertIn('data-action="voice-to-text"', app_js)
        self.assertIn("function convertChatVoiceToText", app_js)
        self.assertIn("voice_to_text_view_status", app_js)
        self.assertIn('class="chat-message-actions contextual"', app_js)
        self.assertIn(".chat-audio-button", app_css)
        self.assertIn(".chat-audio-progress", app_css)
        self.assertIn(".chat-voice-transcript", app_css)
        self.assertIn(".chat-message-actions", app_css)

    def test_voice_playback_refreshes_expired_tencent_media_urls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        audio_url = self._app_fragment(
            app_js,
            "function isTencentRichMediaUrl",
            "function normalizeMessageKind",
        )
        audio_render = self._app_fragment(
            app_js, "function chatMessageBodyHtml", "function chatMessageContentHtml"
        )
        audio_refresh = self._app_fragment(
            app_js, "function chatAudioIdentity", "function syncChatAudioPlaybackUi"
        )
        audio_recovery = self._app_fragment(
            app_js, "async function recoverChatAudioPlayback", "function syncChatAudioPlaybackUi"
        )
        audio_toggle = self._app_fragment(
            app_js, "async function toggleChatAudioPlayback", "function ensureChatMediaViewer"
        )
        playback_error = self._app_fragment(
            app_js, "function handleChatPlaybackError", "function openChatMediaViewer"
        )
        cleanup = self._app_fragment(app_js, "async function cleanupIM", "function stopPresenceTimer")

        self.assertIn("imAudioSourceRefreshes: new Map()", app_js)
        self.assertIn("imrich\\.qcloud\\.com", audio_url)
        self.assertIn("rich\\.my-imcloud\\.com", audio_url)
        self.assertIn("function isTencentRichMediaUrl", audio_url)
        self.assertIn('String(key).toLowerCase() === "authkey"', audio_url)
        self.assertLess(audio_url.index('"url"'), audio_url.index('"remoteAudioUrl"'))
        self.assertIn("!isUnauthenticatedTencentRichMediaUrl(url)", audio_url)
        self.assertIn('data-audio-message-random="${esc(', audio_render)
        self.assertIn('data-audio-message-sequence="${esc(', audio_render)
        self.assertIn('data-audio-message-time="${esc(entry.timestamp)}"', audio_render)
        self.assertIn('data-audio-peer="${esc(entry.peer)}"', audio_render)
        self.assertIn('data-audio-source-needs-refresh="${sourceNeedsRefresh ? "1" : "0"}"', audio_render)
        self.assertIn('sourceNeedsRefresh ? "" : ` src=', audio_render)
        self.assertIn("CHAT_AUDIO_REFRESH_PAGE_SIZE = 15", app_js)
        self.assertIn("CHAT_AUDIO_REFRESH_MAX_PAGES = 8", app_js)
        self.assertIn("CHAT_AUDIO_REFRESH_MAX_MS = 20 * 1000", app_js)
        self.assertIn('source: String(audio?.dataset?.mediaSource || "").trim()', audio_refresh)
        self.assertIn("function chatAudioEntryHasRefreshedSource", audio_refresh)
        self.assertIn("const sourceRequiresRenewal = isTencentRichMediaUrl(currentUrl)", audio_refresh)
        self.assertIn("candidateUrl !== currentUrl", audio_refresh)
        self.assertIn("chatAudioEntryHasRefreshedSource(entry, identity)", audio_refresh)
        self.assertIn("S.chat.findMessage(identity.id)", audio_refresh)
        self.assertIn("S.chat.getMessageListHopping({", audio_refresh)
        self.assertIn("time: Math.floor(identity.timestamp / 1000)", audio_refresh)
        self.assertIn("page < CHAT_AUDIO_REFRESH_MAX_PAGES", audio_refresh)
        self.assertIn("const remainingMs = deadline - Date.now()", audio_refresh)
        self.assertNotIn("while (true)", audio_refresh)
        self.assertIn("options.nextReqMessageID = nextReqMessageID", audio_refresh)
        self.assertIn("data.isCompleted", audio_refresh)
        self.assertIn("updateRefreshedChatAudioEntry", audio_refresh)
        self.assertIn("if (!chatAudioEntryHasRefreshedSource(sdkEntry, identity))", audio_refresh)
        self.assertIn("archiveMessageBestEffort(next", audio_refresh)
        self.assertIn("await recoverChatAudioPlayback(audio, { resumePlayback: true", audio_toggle)
        self.assertIn("reloadChatPlayback(audio, { manual })", audio_recovery)
        self.assertIn("!canRefreshChatAudioSourceFromSdk()", audio_recovery)
        self.assertIn("const shouldRefresh = mustRefresh || isTencentRichMediaUrl(source)", audio_recovery)
        self.assertIn("if (!shouldRefresh ||", audio_recovery)
        refresh_index = playback_error.index("refreshChatAudioSource(media")
        request_guard_index = playback_error.rfind(
            'media.dataset.playbackRequested === "1"', 0, refresh_index
        )
        self.assertGreaterEqual(request_guard_index, 0)
        self.assertIn("shouldRefresh &&", playback_error)
        self.assertLess(
            refresh_index,
            playback_error.index("chatMediaRetryState(source)"),
        )
        self.assertIn("reloadChatPlayback(media)", playback_error)
        self.assertIn("await recoverChatAudioPlayback(media, { resumePlayback: true", app_js)
        self.assertIn("语音播放地址已失效，请等待实时消息连接后重试", app_js)
        self.assertIn("S.imAudioSourceRefreshes.clear()", cleanup)

    def test_profile_and_structured_fields_use_chinese_display_labels(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        for marker in (
            'uid: "UID"',
            'age: "年龄"',
            'gender: "性别"',
            'property: "属性"',
            'city: "城市"',
            'online: "最近在线"',
            "displayFieldLabel(key)",
            "displayFieldValue(key, val)",
        ):
            self.assertIn(marker, app_js)
        for marker in (
            ">VOICE ROOMS<",
            ">GROWTH<",
            ">Action<",
            ">Channel<",
            ">图片/GIF<",
            "用户或内容 ID",
        ):
            self.assertNotIn(marker, app_js)

    def test_profile_queries_render_readable_dedicated_results(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        page_me = app_js.split("async function pageMe(signal)", 1)[1].split(
            "async function pageLab", 1
        )[0]
        actions = app_js.split('if (action === "face-status")', 1)[1].split(
            'if (action === "im-rong")', 1
        )[0]
        result_views = app_js.split("function profileQueryCard", 1)[1].split(
            "function setActiveProfileQuery", 1
        )[0]

        for marker in (
            "function faceStatusView",
            "function etiquetteStatusView",
            "function referralStatusView",
            "function referralSaveView",
            "function loadProfileQuery",
            'class="profile-query-result',
        ):
            self.assertIn(marker, app_js)
        self.assertIn('class="button-row profile-query-actions"', page_me)
        self.assertIn('aria-live="polite"', page_me)
        self.assertLess(page_me.index('id="me-result"'), page_me.index('data-form="referral-set"'))
        self.assertNotIn("operationView(data", actions)
        self.assertNotIn("detailsView(data", actions)
        for developer_detail in (
            "最新结果",
            "错误码",
            "认证记录",
            "当前账号",
            "服务端返回",
            "读取状态",
            "profile-query-result-facts",
            "profile-query-result-note",
        ):
            self.assertNotIn(developer_detail, result_views)
        self.assertIn(".profile-query-result-highlight", app_css)
        self.assertIn(".profile-query-actions .btn.is-active", app_css)
        self.assertIn("user-select: all", app_css)

    def test_responsive_navigation_is_exclusive_and_keeps_mine_children_available(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        mobile_media = app_css.split("@media (max-width: 960px)", 1)[1].split(
            "@media (min-width: 961px)", 1
        )[0]
        short_landscape_media = app_css.split(
            "@media (max-height: 560px) and (max-width: 960px) and (min-aspect-ratio: 4 / 3)",
            1,
        )[1].split("html.keyboard-visible", 1)[0]

        for marker in (
            'function navigationMode()',
            'function syncNavigationMode()',
            'function withMineSubnav(route, html)',
            'data-action="logout"',
            'window.addEventListener("resize", syncNavigationMode',
            'sidebar.inert = sidebarHidden',
            'bottomNav.inert = mode !== "bottom"',
        ):
            self.assertIn(marker, app_js)
        for route in ('id: "me"', 'id: "social"', 'id: "wallet"', 'id: "tasks"'):
            self.assertIn(route, app_js)

        self.assertIn("display: none !important", mobile_media)
        self.assertIn("#open-menu", mobile_media)
        self.assertIn(".drawer-mask", mobile_media)
        self.assertIn(".bottom-nav", mobile_media)
        self.assertIn("display: flex !important", mobile_media)
        self.assertIn(".mine-subnav", mobile_media)
        self.assertIn(".sidebar", short_landscape_media)
        self.assertIn("display: flex !important", short_landscape_media)
        self.assertIn(".bottom-nav", short_landscape_media)
        self.assertIn("display: none !important", short_landscape_media)

    def test_mine_tabs_are_stable_and_do_not_duplicate_moments_entry(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        page_me = app_js.split("async function pageMe(signal)", 1)[1].split(
            "async function pageLab", 1
        )[0]
        switch_mine = app_js.split("async function switchMineTab", 1)[1].split(
            "function routeCacheKey", 1
        )[0]

        self.assertIn('data-action="mine-tab"', app_js)
        self.assertIn('id="mine-tab-panel"', app_js)
        self.assertNotIn("MINE_MOMENTS_ROUTE", app_js)
        self.assertNotIn("pageMineMoments", app_js)
        self.assertIn('return [...MOMENT_TABS, "我的"]', app_js)
        self.assertNotIn("我的消息", page_me)
        self.assertNotIn("发布内容与权限管理", page_me)
        self.assertNotIn("乐园币、会员与礼物", page_me)
        self.assertIn("panel.innerHTML = html", switch_mine)
        self.assertNotIn("root().innerHTML", switch_mine)
        self.assertIn('panel.classList.add("is-loading")', switch_mine)
        self.assertIn("isMineRoute(S.route) && isMineRoute(target)", app_js)
        self.assertIn(".mine-tab-panel.is-loading", app_css)
        self.assertIn("scrollbar-gutter: stable", app_css)

    def test_static_asset_cache_versions_match_mobile_media_release(self) -> None:
        root = Path(__file__).resolve().parents[1]
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")

        css_version = index_html.split('/static/app.css?v=', 1)[1].split('"', 1)[0]
        js_version = index_html.split('/static/app.js?v=', 1)[1].split('"', 1)[0]
        self.assertEqual(css_version, js_version)
        self.assertIn("mobile-media-retry-secure-viewport", css_version)
        self.assertIn("conversation-avatar-stable", css_version)
        self.assertIn("conversation-avatar-stable-reload", css_version)
        self.assertIn("conversation-profile-fast", css_version)
        self.assertIn("conversation-list-stable-paint", css_version)
        self.assertIn("session-bootstrap-retry", css_version)
        self.assertIn("route-dom-cache", css_version)
        self.assertIn("panel-dom-cache", css_version)
        self.assertIn("private-message-policy-recheck", css_version)
        self.assertIn("private-message-entry-scope", css_version)
        self.assertTrue(
            css_version.endswith(
                "-voice-url-renewal-imcloud-revoke-replay-v2-unread-authoritative-private-message-policy-hardening-unread-tie-fix-message-policy-refresh-race-fix-message-policy-cleanup-queue-fix"
            )
        )


class FlashPhotoBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def _run_flash_get(self, result: ApiResult):
        app = SimpleNamespace(
            session=SimpleNamespace(uid="42"),
            im=SimpleNamespace(flash_photo_get=lambda **_kwargs: result),
        )
        web_user = SimpleNamespace(app=app)

        class Harness:
            path = "/api/im/flash/get"

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return {"uniqueid": "flash-1"}

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def _allow_sensitive_action(self, *_args, **_kwargs):
                return True

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return harness.response

    def test_nonzero_flash_photo_status_is_immediately_gone(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"photostatus":"1"}',
            data={
                "photostatus": "1",
                "photourl": "images/202607/private.jpg",
            },
            code="200",
            message="images/202607/upstream-message.jpg",
        )

        status, payload = self._run_flash_get(result)

        self.assertEqual(status, 410)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["photo_status"], "1")
        self.assertEqual(payload["message"], "闪图已查看、已失效或不存在")
        self.assertNotIn("path", payload)
        self.assertNotIn("upstream-message", repr(payload))

    def test_available_flash_photo_has_fixed_success_message(self) -> None:
        result = ApiResult(
            True,
            200,
            '{"photostatus":"0"}',
            data={
                "photostatus": "0",
                "photourl": "images/202607/available.png",
            },
            code="200",
            message="upstream raw message",
        )

        status, payload = self._run_flash_get(result)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["photo_status"], "0")
        self.assertEqual(payload["message"], "闪图已获取")
        self.assertEqual(payload["path"], "images/202607/available.png")
        self.assertNotIn("upstream raw message", repr(payload))


if __name__ == "__main__":
    unittest.main()
