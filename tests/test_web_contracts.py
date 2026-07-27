from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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
        self.assertTrue(item["profile_resolved"])
        self.assertEqual(calls, [])

    def test_conversation_list_uses_cached_profile_when_snapshot_already_exists(self) -> None:
        app = SimpleNamespace(profile=SimpleNamespace(get_user=lambda uid: None))
        item = {
            "peer_id": "9",
            "nickname": "旧昵称",
            "avatar": "https://oss.banghua.xin/images/users/existing.jpg",
            "user": {
                "nickname": "游客",
                "avatar": "https://oss.banghua.xin/images/users/existing.jpg",
            },
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

        self.assertEqual(item["avatar"], "https://oss.banghua.xin/images/users/cached.jpg")
        self.assertEqual(item["nickname"], "真实昵称")
        self.assertEqual(item["user"]["nickname"], "真实昵称")
        self.assertEqual(item["user"]["avatar"], "https://oss.banghua.xin/images/users/cached.jpg")
        self.assertTrue(item["profile_resolved"])

    def test_partial_cached_conversation_profile_remains_unresolved(self) -> None:
        app = SimpleNamespace(profile=SimpleNamespace(get_user=lambda uid: None))
        partial_profiles = (
            (
                {"id": "9", "avatar": "https://oss.banghua.xin/images/users/new.jpg"},
                "旧昵称",
                "https://oss.banghua.xin/images/users/new.jpg",
            ),
            (
                {"id": "9", "nickname": "新昵称"},
                "新昵称",
                "https://oss.banghua.xin/images/users/old.jpg",
            ),
        )
        for profile, expected_name, expected_avatar in partial_profiles:
            with self.subTest(profile=profile):
                item = {
                    "peer_id": "9",
                    "nickname": "旧昵称",
                    "avatar": "https://oss.banghua.xin/images/users/old.jpg",
                    "user": {},
                }
                cache = {"9": (bff_server.time.monotonic(), profile)}
                bff_server._attach_cached_conversation_profiles(app, [item], cache)
                self.assertEqual(item["nickname"], expected_name)
                self.assertEqual(item["avatar"], expected_avatar)
                self.assertFalse(item["profile_resolved"])

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
        self.assertTrue(archive_api._conversation_name_is_placeholder("游客", "9"))

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
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["unknown_count"], 1)
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
        self.assertTrue(payload["snapshot_complete"])

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

    def test_system_customer_service_history_is_available_to_every_user(self) -> None:
        calls, response = self._run_get(
            "/api/im/messages?peer=1",
            authorized_peer=False,
        )

        self.assertEqual(
            calls,
            [("roaming", "1", "42"), ("roaming", "42", "1")],
        )
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])

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
        self.assertTrue(response[1]["partial"])
        self.assertEqual(response[1]["unknown_count"], 1)
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
        self.assertFalse(harness.response[1]["forced_remote"])
        self.assertEqual(harness.response[1]["state"], "idle")
        self.assertIsNone(harness.response[1]["target"])

    def test_voice_force_cancel_removes_remote_queue_when_local_state_is_missing(self) -> None:
        calls = []
        removed = ApiResult(True, 200, "", data=None, kind="empty")
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                match=SimpleNamespace(
                    cancel_voice=lambda **kwargs: calls.append(kwargs) or removed
                ),
            ),
            native=SimpleNamespace(),
            lock=threading.RLock(),
            match_pool_online_list_enabled=False,
            nearby_custom_city_enabled=False,
            voice_match_state={},
        )

        harness = self._harness(
            "/api/match/voice/cancel", web_user, body={"force_remote": True}
        )
        bff_server.Handler.do_POST(harness)

        self.assertEqual(harness.response[0], 200)
        self.assertTrue(harness.response[1]["ok"])
        self.assertTrue(harness.response[1]["remote_required"])
        self.assertTrue(harness.response[1]["remote_ok"])
        self.assertTrue(harness.response[1]["forced_remote"])
        self.assertEqual(harness.response[1]["state"], "idle")
        self.assertEqual(calls, [{"id_": "42"}])


class PrivateMessagePermissionBffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def _run_rest_send(
        self,
        *,
        peer="9",
        enabled=False,
        friend_peers=(),
        match_peers=(),
        conversation_peers=(),
        blocked_peers=(),
        blocked_by_peers=(),
        authorizer=None,
        local_sender=None,
        authentication_source="provider",
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
            authentication_source=authentication_source,
        )

        class Harness:
            path = "/api/im/rest/send"

            def __init__(self):
                self.response = None
                if authorizer is not None:
                    self._request_message_peer_authorizer = authorizer
                if local_sender is not None:
                    self._request_local_text_sender = local_sender

            def _check_api_origin(self):
                return True

            def body(self):
                return {
                    "peer": peer,
                    "text": "你好",
                    "client_message_id": "web-client-message-1",
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
        return calls, harness.response

    def _run_mark_read(
        self,
        *,
        authorized=True,
        peers=None,
        conversation_ok=True,
        receipt_ok=True,
        local_marker=None,
        authentication_source="provider",
    ):
        conversation_calls = []
        receipt_calls = []
        conversation_result = SimpleNamespace(ok=conversation_ok)
        receipt_result = SimpleNamespace(
            ok=receipt_ok,
            error_code=0 if receipt_ok else 90001,
            data={"receipt_count": 2} if receipt_ok else {"stage": "receipt"},
        )
        allowed_peers = set(peers or ["9"]) if authorized else set()
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    mark_c2c_read=lambda account, peer: conversation_calls.append(
                        (account, peer)
                    )
                    or conversation_result,
                    sync_c2c_message_read_receipts=lambda account, peer, **kwargs: receipt_calls.append(
                        (account, peer, kwargs.get("messages"))
                    )
                    or receipt_result,
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
            authentication_source=authentication_source,
        )

        class Harness:
            path = "/api/im/read"

            def __init__(self):
                self.response = None
                self._request_match_pool_online_list_enabled = False
                if local_marker is not None:
                    self._request_local_read_marker = local_marker

            def _check_api_origin(self):
                return True

            def body(self):
                return (
                    {"peers": peers}
                    if peers is not None
                    else {
                        "peer": "9",
                        "receipt_messages": [
                            {
                                "from": "9",
                                "to": "42",
                                "sequence": 101,
                                "random": 202,
                                "time": 1710000000,
                            }
                        ],
                    }
                )

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)
        return conversation_calls, receipt_calls, harness.response

    def test_non_match_new_private_message_is_denied_before_rest_send(self) -> None:
        calls, response = self._run_rest_send()

        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)
        self.assertEqual(response[1]["code"], "PRIVATE_MESSAGE_PERMISSION_REQUIRED")

    def test_web_local_send_commits_before_and_without_tim(self) -> None:
        local_calls = []

        def send_local(peer, text, client_message_id, quote):
            local_calls.append((peer, text, client_message_id, quote))
            return {
                "handled": True,
                "status": 200,
                "payload": {
                    "ok": True,
                    "canonical_message_id": "canonical-local-1",
                    "message_id": "canonical-local-1",
                    "client_message_id": client_message_id,
                    "tim_mirror_status": "pending",
                    "compatibility_sync": "pending",
                },
            }

        tim_calls, response = self._run_rest_send(
            friend_peers={"9"},
            local_sender=send_local,
        )

        self.assertEqual(tim_calls, [])
        self.assertEqual(
            local_calls,
            [("9", "你好", "web-client-message-1", {})],
        )
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])
        self.assertEqual(response[1]["canonical_message_id"], "canonical-local-1")
        self.assertEqual(response[1]["tim_mirror_status"], "pending")

    def test_unmigrated_peer_falls_back_to_legacy_tim_send(self) -> None:
        local_calls = []

        def send_local(peer, text, client_message_id, quote):
            local_calls.append((peer, text, client_message_id, quote))
            return {"handled": False, "reason": "peer_not_migrated"}

        tim_calls, response = self._run_rest_send(
            friend_peers={"9"},
            local_sender=send_local,
        )

        self.assertEqual(len(local_calls), 1)
        self.assertEqual(tim_calls, [("42", "9", "你好")])
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["ok"])

    def test_local_password_mode_rejects_unmigrated_peer_without_tim(self) -> None:
        local_calls = []

        def send_local(peer, text, client_message_id, quote):
            local_calls.append((peer, text, client_message_id, quote))
            return {"handled": False, "reason": "peer_not_migrated"}

        tim_calls, response = self._run_rest_send(
            friend_peers={"9"},
            local_sender=send_local,
            authentication_source="local",
        )

        self.assertEqual(len(local_calls), 1)
        self.assertEqual(tim_calls, [])
        self.assertEqual(response[0], 409)
        self.assertEqual(response[1]["code"], "PEER_NOT_MIGRATED")
        self.assertFalse(response[1]["retryable"])

    def test_local_password_mode_never_falls_through_on_invalid_local_result(self) -> None:
        tim_calls, response = self._run_rest_send(
            friend_peers={"9"},
            local_sender=lambda *_args: None,
            authentication_source="local",
        )

        self.assertEqual(tim_calls, [])
        self.assertEqual(response[0], 503)
        self.assertEqual(
            response[1]["code"],
            "LOCAL_MESSAGE_SERVICE_UNAVAILABLE",
        )

    def test_rest_mode_read_report_uses_authenticated_account(self) -> None:
        calls, receipt_calls, response = self._run_mark_read()

        self.assertEqual(calls, [("42", "9")])
        self.assertEqual(receipt_calls[0][:2], ("42", "9"))
        self.assertEqual(receipt_calls[0][2][0]["sequence"], 101)
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["read"])
        self.assertEqual(response[1]["receipt_count"], 2)

        calls, receipt_calls, response = self._run_mark_read(authorized=False)
        self.assertEqual(calls, [])
        self.assertEqual(receipt_calls, [])
        self.assertEqual(response[0], 403)

        calls, receipt_calls, response = self._run_mark_read(peers=["9", "10", "9"])
        self.assertCountEqual(calls, [("42", "9"), ("42", "10")])
        self.assertCountEqual(
            [(account, peer) for account, peer, _messages in receipt_calls],
            [("42", "9"), ("42", "10")],
        )
        self.assertTrue(all(messages is None for _account, _peer, messages in receipt_calls))
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["read_peers"], ["9", "10"])

        calls, receipt_calls, response = self._run_mark_read(receipt_ok=False)
        self.assertEqual(calls, [("42", "9")])
        self.assertEqual(receipt_calls[0][:2], ("42", "9"))
        self.assertEqual(response[0], 502)
        self.assertEqual(response[1]["conversation_read_peers"], ["9"])
        self.assertEqual(response[1]["receipt_failed_peers"], ["9"])

    def test_local_read_remains_successful_when_tim_read_sync_is_unavailable(self) -> None:
        local_calls = []
        calls, receipt_calls, response = self._run_mark_read(
            conversation_ok=False,
            receipt_ok=False,
            local_marker=lambda peer: local_calls.append(peer) or 4,
        )

        self.assertEqual(local_calls, ["9"])
        self.assertEqual(calls, [("42", "9")])
        self.assertEqual(receipt_calls[0][:2], ("42", "9"))
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["read_peers"], ["9"])
        self.assertEqual(response[1]["local_read_peers"], ["9"])
        self.assertEqual(response[1]["local_read_counts"], {"9": 4})

    def test_local_password_mode_does_not_wait_for_tim_read_sync(self) -> None:
        local_calls = []
        calls, receipt_calls, response = self._run_mark_read(
            conversation_ok=False,
            receipt_ok=False,
            local_marker=lambda peer: local_calls.append(peer) or 3,
            authentication_source="local",
        )

        self.assertEqual(local_calls, ["9"])
        self.assertEqual(calls, [])
        self.assertEqual(receipt_calls, [])
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]["read_peers"], ["9"])
        self.assertEqual(response[1]["compatibility_sync_skipped_peers"], ["9"])

    def test_local_password_mode_rejects_nonlocal_read_without_tim(self) -> None:
        local_calls = []
        calls, receipt_calls, response = self._run_mark_read(
            local_marker=lambda peer: local_calls.append(peer) or None,
            authentication_source="local",
        )

        self.assertEqual(local_calls, ["9"])
        self.assertEqual(calls, [])
        self.assertEqual(receipt_calls, [])
        self.assertEqual(response[0], 409)
        self.assertEqual(response[1]["code"], "PEER_NOT_MIGRATED")
        self.assertEqual(response[1]["failed_peers"], ["9"])

    def test_system_customer_service_read_report_needs_no_private_message_grant(self) -> None:
        calls, receipt_calls, response = self._run_mark_read(
            authorized=False,
            peers=["1"],
        )

        self.assertEqual(calls, [("42", "1")])
        self.assertEqual(receipt_calls[0][:2], ("42", "1"))
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]["read"])

    def test_system_customer_service_cannot_receive_user_messages(self) -> None:
        calls, response = self._run_rest_send(
            peer="1",
            enabled=True,
            friend_peers={"1"},
            match_peers={"1"},
            conversation_peers={"1"},
            authorizer=lambda _peer: True,
        )

        self.assertEqual(calls, [])
        self.assertEqual(response[0], 403)
        self.assertEqual(response[1]["code"], "PRIVATE_MESSAGE_PERMISSION_REQUIRED")

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

    def test_restored_session_uses_trusted_durable_blocks_without_upstream_fetch(self) -> None:
        upstream_calls = []
        authorizer_calls = []
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
                    my_blacklist=lambda: upstream_calls.append("blacklist") or failed,
                    blacklist_me=lambda: upstream_calls.append("blacklist-me") or failed,
                ),
            ),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=0.0,
            message_blocks_retry_at=0.0,
        )
        harness = SimpleNamespace(
            _request_message_block_snapshot_loader=lambda: {
                "blacklist": [],
                "blacklisted_by": ["9"],
            },
            _request_message_peer_authorizer=lambda peer: authorizer_calls.append(peer)
            or True,
        )

        self.assertFalse(bff_server.Handler.can_message_peer(harness, web_user, "9"))
        self.assertTrue(bff_server.Handler.can_message_peer(harness, web_user, "10"))
        self.assertEqual(upstream_calls, [])
        self.assertEqual(authorizer_calls, ["10"])
        self.assertEqual(web_user.blocked_message_peers, set())
        self.assertEqual(web_user.blocked_by_message_peers, {"9"})
        self.assertGreater(web_user.blocked_message_peers_snapshot_at, 0.0)
        self.assertEqual(
            web_user.blocked_message_peers_snapshot_at,
            web_user.blocked_by_message_peers_snapshot_at,
        )

    def test_canonical_authorizer_overrides_stale_complete_unblock_snapshot(self) -> None:
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            authentication_source="local",
            blocked_message_peers={"9"},
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=1.0,
            blocked_by_message_peers_snapshot_at=1.0,
            message_blocks_retry_at=0.0,
        )
        calls = []
        harness = SimpleNamespace(
            _request_message_peer_authorizer=lambda peer: calls.append(peer) or True,
            _request_message_peer_authorizer_canonical=True,
        )

        self.assertTrue(bff_server.Handler.can_message_peer(harness, web_user, "9"))
        self.assertEqual(calls, ["9"])

    def test_canonical_authorizer_cannot_override_unpersisted_block_snapshot(self) -> None:
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
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=1.0,
            message_blocks_retry_at=0.0,
        )
        calls = []
        harness = SimpleNamespace(
            _request_message_block_snapshot_loader=lambda: None,
            _request_message_peer_authorizer=lambda peer: calls.append(peer) or True,
            _request_message_peer_authorizer_canonical=True,
        )

        self.assertFalse(bff_server.Handler.can_message_peer(harness, web_user, "9"))
        self.assertEqual(calls, [])

    def test_incomplete_durable_block_snapshot_still_fails_closed(self) -> None:
        upstream_calls = []
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
                    my_blacklist=lambda: upstream_calls.append("blacklist") or failed,
                    blacklist_me=lambda: upstream_calls.append("blacklist-me") or failed,
                ),
            ),
            blocked_message_peers=set(),
            blocked_by_message_peers=set(),
            blocked_message_peers_snapshot_at=0.0,
            blocked_by_message_peers_snapshot_at=0.0,
            message_blocks_retry_at=0.0,
        )
        harness = SimpleNamespace(
            _request_message_block_snapshot_loader=lambda: {"blacklist": []},
            _request_message_peer_authorizer=lambda _peer: self.fail(
                "authorizer must not run without both trusted block directions"
            ),
        )

        self.assertFalse(bff_server.Handler.can_message_peer(harness, web_user, "9"))
        self.assertEqual(upstream_calls, ["blacklist", "blacklist-me"])
        self.assertEqual(web_user.blocked_message_peers_snapshot_at, 0.0)
        self.assertEqual(web_user.blocked_by_message_peers_snapshot_at, 0.0)

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

    def test_local_password_mode_never_refreshes_block_snapshots_upstream(self) -> None:
        upstream_calls = []
        web_user = SimpleNamespace(
            app=SimpleNamespace(
                session=SimpleNamespace(uid="42"),
                social=SimpleNamespace(
                    my_blacklist=lambda: upstream_calls.append("blacklist"),
                    blacklist_me=lambda: upstream_calls.append("blacklist-me"),
                ),
            ),
            authentication_source="local",
            blocked_message_peers={"9"},
            blocked_by_message_peers={"10"},
            blocked_message_peers_snapshot_at=1.0,
            blocked_by_message_peers_snapshot_at=1.0,
            message_blocks_retry_at=0.0,
        )

        self.assertTrue(
            bff_server.Handler.ensure_message_blocks_loaded(
                SimpleNamespace(),
                web_user,
            )
        )
        self.assertEqual(upstream_calls, [])

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

    def test_canonical_revoke_commits_locally_without_calling_tim(self) -> None:
        canonical_id = "00000000-0000-0000-0000-000000000123"
        tim_calls = []
        local_calls = []
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            authentication_source="local",
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    revoke_c2c=lambda *args: tim_calls.append(args)
                )
            ),
        )

        class Harness:
            path = "/api/im/rest/revoke"

            def __init__(self):
                self.response = None
                self._request_local_text_revoker = self.revoke_local

            def revoke_local(self, peer, message_id):
                local_calls.append((peer, message_id))
                return {
                    "handled": True,
                    "status": 200,
                    "payload": {
                        "ok": True,
                        "source": "web-local",
                        "provider": "web-local",
                        "canonical_message_id": message_id,
                        "revoked": True,
                        "revoked_at": "2026-07-25T12:00:00+00:00",
                        "recalled_text": "重新编辑正文",
                        "created": True,
                        "tim_mirror_status": "cancelled",
                        "compatibility_sync": "cancelled",
                    },
                }

            def _check_api_origin(self):
                return True

            def body(self):
                return {"to": "9", "canonical_message_id": canonical_id}

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)

        self.assertEqual(local_calls, [("9", canonical_id)])
        self.assertEqual(tim_calls, [])
        self.assertEqual(harness.response[0], 200)
        self.assertEqual(harness.response[1]["source"], "web-local")
        self.assertTrue(harness.response[1]["revoked"])
        self.assertEqual(harness.response[1]["recalled_text"], "重新编辑正文")

    def test_local_only_legacy_revoke_never_contacts_tim(self) -> None:
        tim_calls = []
        web_user = SimpleNamespace(
            app=SimpleNamespace(session=SimpleNamespace(uid="42")),
            authentication_source="local",
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
                    revoke_c2c=lambda *args: tim_calls.append(args)
                )
            ),
        )

        class Harness:
            path = "/api/im/rest/revoke"
            _request_local_text_revoker = staticmethod(
                lambda *_args: {
                    "handled": False,
                    "reason": "local_message_not_found",
                }
            )

            def __init__(self):
                self.response = None

            def _check_api_origin(self):
                return True

            def body(self):
                return {"to": "9", "msg_key": "legacy-tim-key"}

            def sid(self):
                return "sid"

            def user(self, _sid):
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        harness = Harness()
        bff_server.Handler.do_POST(harness)

        self.assertEqual(tim_calls, [])
        self.assertEqual(harness.response[0], 409)
        self.assertEqual(
            harness.response[1]["code"],
            "LEGACY_MESSAGE_REVOKE_UNAVAILABLE",
        )


class SocialFrontendContractTests(unittest.TestCase):
    def _app_fragment(self, app_js: str, start: str, end: str) -> str:
        start_index = app_js.find(start)
        self.assertGreaterEqual(start_index, 0, f"missing JavaScript boundary: {start}")
        end_index = app_js.find(end, start_index + len(start))
        self.assertGreater(end_index, start_index, f"missing JavaScript boundary: {end}")
        return app_js[start_index:end_index]

    def test_visitor_lists_show_exact_directional_visit_times(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        formatter = app_js.split("function formatVisitorTime(value)", 1)[1].split(
            "function formatBottleTime", 1
        )[0]
        visitor_card = app_js.split("function visitorCard(item, visitType", 1)[1].split(
            "function friendListHtml", 1
        )[0]
        visitor_panel = app_js.split('} else if (activeTab === "visitors") {', 1)[1].split(
            "} else {", 1
        )[0]

        self.assertIn("date.getFullYear()", formatter)
        self.assertIn("date.getMonth() + 1", formatter)
        self.assertIn("date.getHours()", formatter)
        self.assertIn("date.getMinutes()", formatter)
        self.assertIn('visitType === "seen_by_me" ? "访问时间" : "来访时间"', visitor_card)
        self.assertIn("user.custom_time", visitor_card)
        self.assertIn("(item) => visitorCard(item, type)", visitor_panel)

    def test_boot_retries_transient_session_restore_failures_before_showing_login(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        restore = app_js.split("async function restoreSessionAtBoot()", 1)[1].split(
            "async function recoverSessionAfterBoot", 1
        )[0]
        classifier = app_js.split("function classifyBootSessionAttempt", 1)[1].split(
            "function completeRestoredSession", 1
        )[0]
        recovery = app_js.split("async function recoverSessionAfterBoot", 1)[1].split(
            "(async function boot()", 1
        )[0]
        boot = app_js.split("(async function boot()", 1)[1].split("})();", 1)[0]

        self.assertIn("BOOT_SESSION_TIMEOUT_MS = 5000", app_js)
        self.assertIn("BOOT_SESSION_RETRY_DELAYS_MS", app_js)
        self.assertNotIn("while (true)", restore)
        self.assertIn('api("/api/me", { authOptional: true, timeout: BOOT_SESSION_TIMEOUT_MS })', restore)
        self.assertIn("classifyBootSessionAttempt(result, error)", restore)
        self.assertIn("status === 401", classifier)
        self.assertIn("status === 429", classifier)
        self.assertIn('["offline", "network", "timeout"].includes(error.kind)', classifier)
        self.assertIn("transient: true", classifier)
        self.assertIn("while (token === bootSessionRecoveryToken", recovery)
        self.assertIn("await waitForBootSessionRecovery(recovery.delayMs", recovery)
        self.assertIn("await restoreSessionAtBoot()", boot)
        self.assertNotIn('api("/api/me"', boot)
        self.assertIn("scheduleDeferredFeatureLoad()", boot)
        self.assertGreater(boot.index("scheduleDeferredFeatureLoad()"), boot.index("completeRestoredSession(data)"))
        self.assertIn("showLogin(true, !recovery)", boot)
        self.assertIn("recoverSessionAfterBoot(recoveryToken, recovery)", boot)
        self.assertIn('id="session-recovery-retry"', index_html)
        self.assertIn('id="boot-status-text"', index_html)
        self.assertIn("class ApiRequestError extends Error", app_js)
        self.assertIn("responseRetryAfterMs(response)", app_js)

    def test_frontend_keeps_bounded_local_performance_metrics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        api = self._app_fragment(app_js, "async function api(path", "function archiveHash")
        scheduler = self._app_fragment(
            app_js,
            "function scheduleDeferredFeatureLoad()",
            "function startSmsCountdown",
        )

        self.assertIn("PERFORMANCE_METRIC_LIMIT = 120", app_js)
        self.assertIn('Object.defineProperty(window, "getXBLYPerformanceMetrics"', app_js)
        self.assertIn('type: "api"', api)
        self.assertIn('response.headers.get("Server-Timing")', api)
        self.assertIn("serverDurationMs", api)
        self.assertIn('type: "long-task"', app_js)
        self.assertIn('type: "largest-contentful-paint"', app_js)
        self.assertIn('type: "navigation"', app_js)
        self.assertIn("requestIdleCallback", scheduler)
        self.assertIn("DEFERRED_FEATURES_IDLE_TIMEOUT_MS", scheduler)

    def test_friend_filter_batches_dom_updates_to_one_animation_frame(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        filter_scheduler = self._app_fragment(
            app_js,
            "function scheduleFriendFilter(input)",
            'document.addEventListener("input"',
        )

        self.assertIn("cancelAnimationFrame(S.friendFilterFrame)", filter_scheduler)
        self.assertIn("requestAnimationFrame(() =>", filter_scheduler)
        self.assertIn('surface.querySelectorAll("[data-friend-row]")', filter_scheduler)
        self.assertNotIn('document.querySelectorAll("[data-friend-row]")', filter_scheduler)
        self.assertIn("scheduleFriendFilter(input);", app_js)

    def test_mobile_chat_header_avoids_scroll_time_backdrop_blur(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        mobile_chat = app_css.rsplit(
            "body.chat-conversation-open .chat-head {", 1
        )[1].split("}", 1)[0]

        self.assertIn("backdrop-filter: none", mobile_chat)
        self.assertNotIn("blur(", mobile_chat)

    def test_view_caches_are_lru_bounded_for_long_browser_sessions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const PAGE_CACHE_LIMIT = 32;", app_js)
        self.assertIn("const PANEL_CACHE_LIMIT = 40;", app_js)
        self.assertIn("const PANEL_DOM_CACHE_LIMIT = 24;", app_js)
        self.assertIn("while (S.panelCache.size > PANEL_CACHE_LIMIT)", app_js)
        self.assertGreaterEqual(
            app_js.count("trimDomCache(S.pageCache, PAGE_CACHE_LIMIT)"),
            2,
        )

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
        visibility_handler = app_js.split("function updateMomentCardVisibility", 1)[1].split(
            "async function reportMomentView", 1
        )[0]
        self.assertIn(
            "suspendMomentVideos(card, { cancelCompat: true });",
            visibility_handler,
        )
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
        self.assertIn(
            "const user = { ...(S.user || {}), ...(data.user || {}) };",
            page_me,
        )
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
        restored_message_route = activate_route.split("if (routeDomRestored)", 1)[1].split(
            "if (!force && cached?.fresh) return", 1
        )[0]
        self.assertIn(
            "refreshMessageConversationRegion({ refreshList: true, refreshPane: true })",
            restored_message_route,
        )
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
            "if (!force && cached.fresh)", 1
        )[1].split("  } else {", 1)[0]

        snapshot = 'rememberCurrentPageSnapshot(routeCacheKey("nearby"));'
        self.assertIn(snapshot, fresh_cache_branch)
        self.assertLess(fresh_cache_branch.index(snapshot), fresh_cache_branch.index("return;"))

    def test_voice_matching_is_hidden_without_removing_its_implementation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")

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
        for retained in (
            "async function pageVoiceMatch(signal)",
            'data-action="voice-match-start"',
            "async function ensureVoiceCallReady",
            "async function startRongVoiceCall",
            'api("/api/match/voice/start"',
            "sampleRate: 48000",
            "registerUserInfo?.(",
            "session.getRemoteUsers?.()",
        ):
            self.assertIn(retained, app_js)
        self.assertIn("const VOICE_MATCH_ENABLED = false;", app_js)
        self.assertIn("const MATCH_HUB_TAB_ITEMS = [", app_js)
        self.assertIn('...(VOICE_MATCH_ENABLED ? [["voice", "语音匹配"]] : [])', app_js)
        self.assertIn("const MATCH_HUB_TABS = MATCH_HUB_TAB_ITEMS.map", app_js)
        self.assertIn('if (!VOICE_MATCH_ENABLED) return Promise.reject(new Error("语音匹配已停用"));', app_js)
        self.assertIn('if (!VOICE_MATCH_ENABLED) throw new Error("语音匹配已停用");', app_js)
        self.assertIn("S.matchTab = normalizeMatchTab(requestedMatchTab);", app_js)
        self.assertIn('history.replaceState(null, "", matchRouteHash(S.matchTab));', app_js)
        self.assertIn("function cleanupDisabledVoiceMatchQueue()", app_js)
        self.assertIn("voiceMatchDisabledCleanupGeneration: -1", app_js)
        self.assertIn("S.voiceMatchDisabledCleanupGeneration === S.sessionGeneration", app_js)
        self.assertIn('body: JSON.stringify({ force_remote: true })', app_js)
        self.assertIn("if (!VOICE_MATCH_ENABLED) tasks.push(cleanupDisabledVoiceMatchQueue());", app_js)
        self.assertIn("else await cleanupDisabledVoiceMatchQueue();", app_js)
        self.assertGreaterEqual(app_js.count("JSON.stringify({ force_remote: true })"), 2)
        match_header = app_js.split("function matchHubHeader", 1)[1].split(
            "function normalizeMomentsTab", 1
        )[0]
        self.assertIn("MATCH_HUB_TAB_ITEMS", match_header)
        self.assertIn('VOICE_MATCH_ENABLED ? " voice-enabled" : ""', match_header)
        self.assertIn('VOICE_MATCH_ENABLED ? "选择匹配、语音匹配或漂流瓶"', match_header)
        self.assertNotIn("/static/vendor/rong/", index_html)

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
        match_header = app_js.split("function matchHubHeader", 1)[1].split(
            "function normalizeMomentsTab", 1
        )[0]
        self.assertIn("MATCH_HUB_TAB_ITEMS", match_header)
        self.assertIn(".match-stats-grid", app_css)
        self.assertIn(".match-submit", app_css)
        self.assertIn(".match-hub-tabs", app_css)
        match_tabs_css = app_css.split(".match-hub-tabs {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", match_tabs_css)
        voice_match_tabs_css = app_css.split(".match-hub-tabs.voice-enabled {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", voice_match_tabs_css)
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
        discovery_card_css = app_css.split(".discovery-user-card .card-meta-list > .card-meta-item", 1)[
            1
        ].split("}", 1)[0]
        discovery_desktop_css = app_css.split(".discovery-user-card.has-avatar", 1)[1].split("}", 1)[0]

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
        self.assertNotIn("navigator.geolocation", nearby)
        self.assertIn('params.set("city", filters.city)', nearby)
        self.assertNotIn('params.set("latitude"', nearby)
        self.assertNotIn('params.set("longitude"', nearby)
        self.assertNotIn('class="welcome-strip"', nearby_page)
        self.assertNotIn('class="quick-entry-grid"', nearby_page)
        self.assertNotIn('data-action="match-users"', matching)
        self.assertNotIn("在线列表", matching)
        self.assertNotIn('data-form="dating-publish"', matching)
        self.assertNotIn("发布约会邀请", matching)
        self.assertIn("return userCard(user, {", nearby)
        self.assertIn("addFriend: true", nearby)
        self.assertIn('className: "discovery-user-card"', nearby)
        self.assertIn("metaItems,", nearby)
        self.assertIn("description,", nearby)
        self.assertIn("if (options.addFriend && id)", user_card)
        self.assertIn('data-action="add-friend"', user_card)
        self.assertIn('data-action="open-profile"', user_card)
        self.assertIn('class="card-meta-list"', user_card)
        self.assertIn('class="card-meta-item"', user_card)
        self.assertIn('class="card-description"', user_card)
        self.assertIn('"match_pool_online_list": True', bff_server_py)
        self.assertIn('"nearby_custom_city"', bff_server_py)
        self.assertIn(".discovery-tabs", app_css)
        self.assertIn(".discovery-filter-form", app_css)
        self.assertIn(".discovery-results .people-grid", app_css)
        self.assertIn(".discovery-user-card.has-avatar", app_css)
        self.assertIn("grid-template-columns: 56px minmax(0, 1fr)", discovery_desktop_css)
        self.assertIn(".discovery-user-card .card-meta-list", app_css)
        self.assertIn("white-space: nowrap", discovery_card_css)
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
        apply_capabilities = app_js.split("function applyCapabilities", 1)[1].split(
            "function setLoginMode", 1
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
        self.assertIn("void origin", can_start_private_chat)
        self.assertIn(
            "if (isSystemCustomerServicePeer(target)) return true",
            app_js,
        )
        self.assertIn("return canStartPrivateChat(target)", app_js)
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
        self.assertIn("messagePolicyRefreshPromise: null", app_js)
        self.assertIn(
            "if (S.messagePolicyRefreshPromise) return S.messagePolicyRefreshPromise;",
            app_js,
        )
        self.assertIn("isCurrentAuthenticatedSession(sessionGeneration)", app_js)
        self.assertIn("S.messagePolicyRefreshPromise = task", app_js)
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
            "const messageCapabilitiesChanged = applyCapabilities(data.capabilities, {",
            app_js,
        )
        self.assertIn("deferMessageReconnect: true", app_js)
        self.assertIn("function queueMessagePolicyCleanup()", app_js)
        self.assertIn(
            "const messageCapabilitiesChanged = proactiveChanged || directCredentialsChanged",
            apply_capabilities,
        )
        self.assertIn("const previous = S.messagePolicyCleanupPromise", app_js)
        self.assertIn("Promise.resolve(previous)", app_js)
        self.assertIn("S.messagePolicyCleanupPromise = tracked", app_js)
        self.assertIn(
            "S._imConnecting ||\n      S.messagePolicyCleanupPromise",
            apply_capabilities,
        )
        self.assertIn("deferMessageReconnect ||", app_js)
        self.assertIn("S.messagePolicyCleanupPromise", app_js)
        self.assertIn("return messageCapabilitiesChanged", apply_capabilities)
        self.assertIn(
            "messageCapabilitiesChanged ||\n        transition.readyChanged ||",
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
        self.assertIn("persistence.message_policy_snapshot(identity)", api_py)
        self.assertIn("SOCIAL_DM_POLICY_PERSISTENCE_FAILED", api_py)

    def test_deferred_policy_cleanup_reconnects_after_capability_rollback(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the frontend race test")

        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        apply_capabilities = app_js.split("function applyCapabilities", 1)[1].split(
            "function setLoginMode", 1
        )[0]
        policy_functions = app_js.split("function currentMessagePolicyFingerprint", 1)[
            1
        ].split("function messageSyncAccountId", 1)[0]
        script = (
            "function applyCapabilities"
            + apply_capabilities
            + "\nfunction currentMessagePolicyFingerprint"
            + policy_functions
            + r"""
const cleanupResolvers = [];
let reconnectAttempts = 0;
const S = {
  proactivePrivateMessageEnabled: false,
  directImCredentialsEnabled: true,
  nearbyCustomCityEnabled: false,
  authenticated: true,
  imMode: "sdk",
  chat: {},
  imConnecting: false,
  _imConnecting: null,
  imConnected: true,
  imNextReconnectAt: 0,
  messagePolicyCleanupPromise: null,
  messagePolicyCleanupGeneration: 0,
  messagePolicyGeneration: 0,
  messagePolicyReady: true,
  messagePolicyFingerprint: "",
  privateMessagePeers: new Set(["7"]),
  matchMessagePeers: new Set(),
  blockedPrivateMessagePeers: new Set(),
  sessionGeneration: 1,
};
function clearAllViewCaches() {}
function clearViewCacheKey() {}
function clearViewCachePrefix() {}
function syncPrivateMessageControls() {}
function updateImConnectionStatus() {}
function cleanupIM() {
  S.imMode = "";
  S.chat = null;
  S.imConnected = false;
  return new Promise((resolve) => cleanupResolvers.push(resolve));
}
function isCurrentAuthenticatedSession(generation) {
  return S.authenticated && generation === S.sessionGeneration;
}
function ensureTimConnected() {
  reconnectAttempts += 1;
  S.imConnected = true;
  return Promise.resolve(true);
}
function replaceMessagePolicyAllowedPeers(values) {
  S.privateMessagePeers = new Set(values || []);
}
function replaceMessagePolicyMatchPeers(values) {
  S.matchMessagePeers = new Set(values || []);
}
function replaceBlockedPrivateMessagePeers(values) {
  S.blockedPrivateMessagePeers = new Set(values || []);
}
function api() {
  return Promise.resolve({
    data: {
      ok: true,
      capabilities: {
        proactive_private_message: false,
        direct_im_credentials: true,
      },
      allowed_peers: ["7"],
      match_peers: [],
      blocked_peers: [],
    },
  });
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  S.messagePolicyFingerprint = currentMessagePolicyFingerprint();
  applyCapabilities({ proactive_private_message: true });
  await flush();
  if (cleanupResolvers.length !== 1) {
    throw new Error(`first cleanup did not start: ${cleanupResolvers.length}`);
  }

  await refreshMessagePolicy();
  if (S.proactivePrivateMessageEnabled !== false) {
    throw new Error("policy refresh did not roll the capability back");
  }
  if (reconnectAttempts !== 0) {
    throw new Error("reconnected before queued cleanup completed");
  }

  cleanupResolvers.shift()();
  await flush();
  await flush();
  if (cleanupResolvers.length !== 1) {
    throw new Error(`deferred cleanup did not start: ${cleanupResolvers.length}`);
  }
  cleanupResolvers.shift()();
  await flush();
  await flush();
  if (reconnectAttempts !== 1) {
    throw new Error(`expected one final reconnect, got ${reconnectAttempts}`);
  }
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        send_text = app_js.split("async function sendTextMessage", 1)[1].split(
            "function progressRatio", 1
        )[0]
        self.assertIn("await ensurePrivateChatPermission(target)", send_text)
        composer_panel = app_js.split("function chatComposerPanelHtml()", 1)[1].split(
            "function chatComposerQuoteHtml", 1
        )[0]
        self.assertIn(
            "const directMediaActions = S.messagePolicyReady",
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
        self.assertIn(
            f'/static/app.css?v={hashlib.sha256((root / "bbw_web" / "static" / "app.css").read_bytes()).hexdigest()[:16]}',
            index_html,
        )
        self.assertIn(
            f'/static/app.js?v={hashlib.sha256((root / "bbw_web" / "static" / "app.js").read_bytes()).hexdigest()[:16]}',
            index_html,
        )

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
        avatar_failure_handler = app_js.split("function discardFailedAvatar(image)", 1)[1].split(
            "const PEER_PRESENCE_TTL_MS", 1
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
        self.assertIn('classList?.contains("user-card")', avatar_failure_handler)
        self.assertIn('card.classList.remove("has-avatar")', avatar_failure_handler)
        self.assertIn('matches("img[data-avatar-image]")', app_js)
        self.assertIn("const previousAvatars = new Map();", conversation_list_renderer)
        self.assertIn('image?.getAttribute("src")', conversation_list_renderer)
        self.assertIn('document.createElement("template")', conversation_list_renderer)
        self.assertIn("const nextHtml = conversationListHtml();", conversation_list_renderer)
        self.assertIn("CONVERSATION_LIST_RENDER_HTML.get(list) === nextHtml", conversation_list_renderer)
        self.assertIn("nextAvatar.replaceWith(previous.avatar);", conversation_list_renderer)
        self.assertIn("list.replaceChildren(template.content);", conversation_list_renderer)
        self.assertIn("CONVERSATION_LIST_RENDER_HTML.set(list, nextHtml);", conversation_list_renderer)
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
        self.assertIn("const MATCH_HUB_TABS = MATCH_HUB_TAB_ITEMS.map", app_js)
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
        can_start_private_chat = app_js.split("function canStartPrivateChat(uid)", 1)[1].split(
            "async function ensurePrivateChatPermission", 1
        )[0]
        can_open_private_chat = app_js.split(
            "function canOpenPrivateChatEntry(uid, origin", 1
        )[1].split("async function ensurePrivateChatEntryPermission", 1)[0]
        conversation_card = app_js.split("function conversationCard(item)", 1)[1].split(
            "function visitorCard", 1
        )[0]

        self.assertIn('const SYSTEM_CUSTOMER_SERVICE_UID = "1"', app_js)
        self.assertIn("isSystemCustomerServicePeer(target)", can_start_private_chat)
        self.assertIn(
            "if (isSystemCustomerServicePeer(target)) return true",
            can_open_private_chat,
        )
        self.assertIn('data-chat-origin="conversation"', conversation_card)
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
        self.assertIn("const preferCachedProfile = conversation.profile_resolved !== true;", app_js)
        self.assertIn("const avatar = useProfileAvatar ? profileAvatar : currentAvatar || profileAvatar;", app_js)
        self.assertIn('value === "游客"', app_js)
        self.assertIn("display.profile_resolved !== true", app_js)
        self.assertIn("rememberConversationProfile(profileUid, user)", app_js)
        self.assertIn("async function hydrateConversationProfiles()", app_js)
        self.assertIn("function conversationProfileForPeer(rows, peer)", app_js)
        self.assertIn("function conversationProfileNeedsHydration(item)", app_js)
        self.assertIn(".filter(conversationProfileNeedsHydration)", app_js)
        self.assertIn("conversationNameIsPlaceholder(conversationDisplayName(display), peer)", app_js)
        self.assertIn(".map(applyCachedConversationProfile);", app_js)
        self.assertIn("return profiles.length === 1 && idless.length === 1 ? idless[0] : null;", app_js)
        self.assertNotIn("profiles[0] ||", app_js)
        self.assertIn("function timUserProfileRows(result)", app_js)
        self.assertIn("function rememberTimConversationProfiles(rows)", app_js)
        self.assertIn("function normalizedConversationProfile(profile, peer)", app_js)
        self.assertIn("_resolved: Boolean(nickname && avatar)", app_js)
        self.assertIn("_resolved: incoming._resolved === true", app_js)
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
        self.assertIn("function finalizeConversationProfileResolution(conversation, fallback)", app_js)
        self.assertIn("void hydrateConversationProfiles();", app_js)
        self.assertNotIn('data-action="im-connect"', app_js)
        self.assertNotIn('id="reload-page"', index_html)
        self.assertNotIn("avatarHtml(", chat_pane)
        self.assertNotIn("UID ${esc", chat_pane)
        self.assertIn('refreshList: action !== "select-conversation"', select_action)
        self.assertNotIn("refreshList: true", select_action)

    def test_conversation_profile_merge_resolves_after_name_and_avatar_merge(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the conversation profile merge test")

        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        merge_functions = "function preserveConversationDisplayName" + app_js.split(
            "function preserveConversationDisplayName", 1
        )[1].split("function normalizeTimConversation", 1)[0]
        script = (
            r"""
function conversationPeer(item) { return String(item?.peer_id || ""); }
function conversationDisplayName(item) { return String(item?.nickname || item?.user?.nickname || ""); }
function conversationAvatar(item) { return String(item?.avatar || item?.user?.avatar || ""); }
function conversationNameIsPlaceholder(value, peer) {
  const name = String(value || "").trim();
  return !name || name === "用户" || name === "游客" || name === peer || name === `用户 ${peer}`;
}
"""
            + merge_functions
            + r"""
const live = {
  peer_id: "9",
  nickname: "旧昵称",
  avatar: "https://example.invalid/old.jpg",
  profile_resolved: false,
  user: { nickname: "旧昵称", avatar: "https://example.invalid/old.jpg" },
};
const archived = {
  peer_id: "9",
  nickname: "新昵称",
  avatar: "https://example.invalid/new.jpg",
  profile_resolved: true,
  user: { nickname: "新昵称", avatar: "https://example.invalid/new.jpg" },
};
let merged = preserveConversationDisplayName(live, archived);
if (merged.profile_resolved === true) throw new Error("name merge resolved too early");
merged = preserveConversationAvatar(merged, archived);
if (merged.profile_resolved === true) throw new Error("avatar merge resolved too early");
merged = finalizeConversationProfileResolution(merged, archived);
if (merged.nickname !== "新昵称") throw new Error("stale nickname retained");
if (merged.avatar !== "https://example.invalid/new.jpg") throw new Error("stale avatar retained");
if (merged.profile_resolved !== true) throw new Error("complete merged profile unresolved");
"""
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_cached_profile_does_not_stop_hydration(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the partial profile test")

        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        profile_functions = "function conversationProfileResolved" + app_js.split(
            "function conversationProfileResolved", 1
        )[1].split("function preserveConversationDisplayName", 1)[0]
        apply_function = "function applyCachedConversationProfile" + app_js.split(
            "function applyCachedConversationProfile", 1
        )[1].split("function timUserProfileRows", 1)[0]
        script = (
            r"""
const S = {
  conversationProfilesByUid: new Map(),
  conversationProfileFetchedAt: new Map(),
};
function validAvatarValue(...values) {
  return values.map((value) => String(value || "").trim()).find(Boolean) || "";
}
function conversationPeer(item) { return String(item?.peer_id || ""); }
function conversationDisplayName(item) { return String(item?.nickname || item?.user?.nickname || ""); }
function conversationAvatar(item) { return validAvatarValue(item?.avatar, item?.user?.avatar); }
function conversationNameIsPlaceholder(value, peer) {
  const name = String(value || "").trim();
  return !name || name === "用户" || name === "游客" || name === peer || name === `用户 ${peer}`;
}
"""
            + profile_functions
            + apply_function
            + r"""
S.conversationProfilesByUid.set("9", {
  id: "9",
  nickname: "旧昵称",
  avatar: "https://example.invalid/old.jpg",
  portrait: "https://example.invalid/old.jpg",
  _resolved: true,
});
rememberConversationProfile("9", { id: "9", nickname: "新昵称" });
const cached = S.conversationProfilesByUid.get("9");
if (cached._resolved !== false) throw new Error("partial result marked complete");
const merged = applyCachedConversationProfile({
  peer_id: "9",
  nickname: "旧昵称",
  avatar: "https://example.invalid/old.jpg",
  profile_resolved: false,
  user: {},
});
if (merged.nickname !== "新昵称") throw new Error("new nickname not applied");
if (merged.avatar !== "https://example.invalid/old.jpg") throw new Error("existing avatar lost");
if (merged.profile_resolved !== false) throw new Error("partial merged profile marked complete");
"""
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
        self.assertIn("const tasks = [", start_services)
        self.assertIn("return Promise.allSettled(tasks);", start_services)
        self.assertNotIn(".then(() => runMessageSyncCycle", start_services)
        self.assertIn("refreshConversationSummary({ force })", summary_sync)
        self.assertIn("loadArchivedConversationSummary({ force: true })", summary_sync)
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
        self.assertIn("const shouldLoadArchive = force || !S.imArchiveLoadedPeers.has(target)", app_js)
        self.assertIn("if (shouldLoadArchive)", app_js)
        self.assertIn("S.imArchiveLoadedPeers.add(target)", app_js)
        self.assertIn("function chatLogIsNearBottom", app_js)
        self.assertIn("const shouldStickToBottom = forceBottom || chatLogShouldFollowBottom(log)", app_js)
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

    def test_web_uses_provider_tim_as_authority_and_archive_only_as_history(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        connect = app_js.split("async function ensureTimConnected", 1)[1].split(
            "async function cleanupIM", 1
        )[0]
        load_messages = app_js.split("async function loadConversationMessages", 1)[1].split(
            "function oldestPeerMessageTimestamp", 1
        )[0]
        normalize = app_js.split("function normalizeConversationSummary", 1)[1].split(
            "function filterDismissedConversations", 1
        )[0]
        dependency_mode = app_js.split("function applyDependencyMode", 1)[1].split(
            "function applyUser", 1
        )[0]
        composer_panel = app_js.split("function chatComposerPanelHtml()", 1)[1].split(
            "function chatComposerQuoteHtml", 1
        )[0]
        composer = app_js.split("function chatComposerHtml()", 1)[1].split(
            "function chatPaneHtml", 1
        )[0]

        self.assertIn('dependencyMode: "provider"', app_js)
        self.assertNotIn("function webLocalDependencyMode", app_js)
        self.assertIn('S.dependencyMode = "provider";', dependency_mode)
        self.assertIn("ensureTimSdkLoaded()", connect)
        self.assertNotIn("webLocalDependencyMode", connect)
        self.assertIn('api(`/api/im/messages?peer=', load_messages)
        self.assertIn('api(`/api/archive/messages?peer=', load_messages)
        self.assertIn("S.chat.getMessageList", load_messages)
        self.assertNotIn("localOnly", load_messages)
        self.assertNotIn("archiveOnly", load_messages)
        self.assertIn(
            "const stickerAction = S.directImCredentialsEnabled && S.messagePolicyReady",
            composer_panel,
        )
        self.assertIn("const richMessageActionsAvailable = S.messagePolicyReady;", composer)
        self.assertNotIn("/api/im/media/messages", app_js)
        self.assertNotIn("/api/im/media/uploads", app_js)

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = (
            "function conversationPreview(item) { return item.last_message || ''; }\n"
            "function conversationPreviewTimestamp(item) { return Number(item.preview_timestamp || 0); }\n"
            "function conversationPreviewAuthoritative(item) { return item.preview_authoritative === true; }\n"
            "function normalizeConversationSummary"
            + normalize
            + "\n"
            + "for (const provider of ['web-local', 'tim']) {\n"
            + "  const archived = normalizeConversationSummary({source:'archive',provider,"
            + "unread_count:5,unread_authoritative:true,last_message:'历史消息'}, {authority:'archive'});\n"
            + "  if (archived.unread_count !== 0 || archived.unread_authoritative !== false) "
            + "throw new Error('archive became authoritative: ' + provider);\n"
            + "}\n"
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_provider_history_fetches_original_service_for_current_and_older_messages(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        load_messages = app_js.split("async function loadConversationMessages", 1)[1].split(
            "function oldestPeerMessageTimestamp", 1
        )[0]
        load_older = app_js.split("async function loadOlderConversationMessages", 1)[1].split(
            "function recalculateUnreadTotal", 1
        )[0]

        self.assertIn('/api/im/messages?peer=', load_messages)
        self.assertIn('/api/archive/messages?peer=', load_messages)
        self.assertIn("S.chat.getMessageList", load_messages)
        self.assertIn('/api/im/messages?peer=', load_older)
        self.assertIn('/api/archive/messages?', load_older)
        self.assertNotIn("webLocalDependencyMode", load_messages)
        self.assertNotIn("webLocalDependencyMode", load_older)
        self.assertNotIn("localOnly", load_messages)
        self.assertNotIn("localOnly", load_older)
        self.assertNotIn("markConversationRead(target)", load_messages)

    def test_mobile_chat_keeps_following_the_bottom_while_layout_settles(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        viewport_sync = self._app_fragment(
            app_js,
            "function syncVisualViewport()",
            "function waitForVisualViewportRecovery",
        )
        scrolling = self._app_fragment(
            app_js,
            "function cancelChatLogAutoScroll",
            "function closeChatMessageActions",
        )
        media_loading = self._app_fragment(
            app_js,
            "function handleChatMediaLoad(image)",
            "function isMomentVideo(media)",
        )
        quoted_jump = self._app_fragment(
            app_js,
            "async function jumpToQuotedMessage(quote)",
            "async function retryFailedChatMessage",
        )
        scroll_listener = app_js.split(
            'document.addEventListener(\n  "scroll",', 1
        )[1].split("document.addEventListener(\"focusin\"", 1)[0]

        self.assertIn("const CHAT_LOG_BOTTOM_FOLLOW = new WeakMap()", app_js)
        self.assertIn("const CHAT_LOG_USER_SCROLL_INTENT_UNTIL = new WeakMap()", app_js)
        self.assertIn("const CHAT_LOG_LAST_SCROLL_TOP = new WeakMap()", app_js)
        self.assertIn("CHAT_LOG_BOTTOM_FOLLOW.set(log, false)", scrolling)
        self.assertIn("CHAT_LOG_BOTTOM_FOLLOW.set(log, true)", scrolling)
        self.assertIn("if (!preserveUserIntent) CHAT_LOG_USER_SCROLL_INTENT_UNTIL.delete(log)", scrolling)
        self.assertIn("CHAT_LOG_BOTTOM_SETTLE_DELAYS_MS.forEach", scrolling)
        self.assertIn("function cancelChatLogScheduledScroll", app_js)
        self.assertIn("CHAT_LOG_SCROLL_FRAMES", scrolling)
        self.assertIn("CHAT_LOG_SCROLL_TIMERS", scrolling)
        self.assertIn("CHAT_LOG_MAINTENANCE_FRAMES", scrolling)
        self.assertIn("function chatLogShouldFollowBottom", scrolling)
        self.assertIn("function chatLogHasRecentUserScrollIntent", scrolling)
        self.assertIn("function scheduleChatLogBottomMaintenance", scrolling)
        self.assertIn("composerFocused || chatLogShouldFollowBottom(chatLog)", viewport_sync)
        self.assertGreaterEqual(media_loading.count("scheduleChatLogBottomMaintenance("), 2)
        self.assertIn('!["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "].includes(event.key)', app_js)
        self.assertIn("const movedUp = hasPreviousTop && currentTop < previousTop - 1", scroll_listener)
        self.assertIn("if (hasUserIntent && movedUp)", scroll_listener)
        self.assertIn("cancelChatLogAutoScroll(log, { preserveUserIntent: true })", scroll_listener)
        near_bottom_branch = scroll_listener.split("} else if (chatLogIsNearBottom(log)) {", 1)[1].split(
            "} else if (hasUserIntent || movedUp)", 1
        )[0]
        self.assertIn("CHAT_LOG_BOTTOM_FOLLOW.set(log, true)", near_bottom_branch)
        self.assertIn("CHAT_LOG_USER_SCROLL_INTENT_UNTIL.delete(log)", scroll_listener)
        self.assertNotIn("CHAT_LOG_BOTTOM_FOLLOW.get(log) !== false", near_bottom_branch)
        self.assertIn("cancelChatLogAutoScroll(log, { preserveUserIntent: hasUserIntent })", scroll_listener)
        self.assertIn('cancelChatLogAutoScroll(target.closest("#im-log"))', quoted_jump)
        self.assertGreaterEqual(app_js.count("noteChatLogUserScrollIntent(chatLog)"), 3)

    def test_message_read_receipts_and_peer_presence_are_rendered(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        self.assertIn("/api/im/presence?uids=", app_js)
        self.assertIn("data-presence-uid", app_js)
        self.assertIn("presence: true", app_js)
        self.assertIn('"successUserList"', app_js)
        self.assertIn('Object.prototype.hasOwnProperty.call(item || {}, "statusType")', app_js)
        self.assertIn("statusType === 2 || statusType === 3", app_js)
        self.assertIn('element.dataset.presenceUnknownVisible !== "true"', app_js)
        self.assertIn('"presence-compact", true', app_js)
        self.assertIn('const resolvedUids = rememberPeerPresence(rows, "tim")', app_js)
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
        self.assertIn("function reportSdkMessageReadReceipts(peer)", app_js)
        self.assertIn("function sdkMessageReadReceiptReport(entry)", app_js)
        self.assertIn("receipt_messages: conversationReadReceiptReports(target)", app_js)
        self.assertIn("S.chat.sendMessageReadReceipt(entries.map((entry) => entry.rawMessage))", app_js)
        self.assertIn("scheduleSdkMessageReadReceipts(target);", app_js)
        self.assertIn("width: fit-content", app_css)
        self.assertIn("conversation-read-state", app_css)
        self.assertIn("presence-badge", app_css)

        user_card = app_js.split("function userCard", 1)[1].split("function formatSocialTime", 1)[0]
        chat_pane = app_js.split("function chatPaneHtml", 1)[1].split(
            "function renderConversationList", 1
        )[0]
        self.assertIn('<div class="card-actions">${presence}${actions.join("")}</div>', user_card)
        self.assertIn('presenceBadgeHtml(id, user, "", true)', user_card)
        self.assertIn('"presence-compact",\n    true', chat_pane)
        self.assertNotIn('<strong>${esc(name)}</strong>${presence}', user_card)
        self.assertIn(".card-actions > .presence-badge", app_css)

    def test_peer_presence_runtime_handles_sdk_shapes_and_all_subscription_chunks(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the frontend presence test")

        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        badge_functions = "function flagEnabled" + app_js.split(
            "function flagEnabled", 1
        )[1].split("function updatePeerPresenceDom", 1)[0]
        row_functions = "function presenceUidFromRow" + app_js.split(
            "function presenceUidFromRow", 1
        )[1].split("async function subscribePeerPresence", 1)[0]
        subscribe_function = "async function subscribePeerPresence" + app_js.split(
            "async function subscribePeerPresence", 1
        )[1].split("async function refreshVisiblePeerPresence", 1)[0]
        script = (
            r"""
const S = {
  presenceByUid: new Map(),
  subscribedPresenceUids: new Set(),
  imMode: "sdk",
  chat: null,
};
let refreshCalls = 0;
function esc(value) { return String(value); }
function updatePeerPresenceDom() {}
function refreshVisiblePeerPresence() { refreshCalls += 1; return Promise.resolve(); }
"""
            + badge_functions
            + row_functions
            + subscribe_function
            + r"""
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

(async () => {
  const rows = timPresenceRows({ data: { successUserList: [
    { userID: "1", statusType: 1 },
    { userID: "2", statusType: 2 },
    { userID: "3", statusType: 3 },
    { userID: "4", statusType: 0 },
  ] } });
  assert(rows.length === 4, "successUserList was not recognized");
  assert(presenceFromRow(rows[0]).status === "online", "statusType=1 should be online");
  assert(presenceFromRow(rows[1]).status === "offline", "statusType=2 should be offline");
  assert(presenceFromRow(rows[2]).status === "offline", "statusType=3 should be offline");
  assert(presenceFromRow(rows[3]).status === "unknown", "statusType=0 should be unknown");
  assert(presenceFromEntity({ is_online: "0" }).status === "offline", "string zero became online");
  assert(presenceFromEntity({ is_online: "false" }).status === "offline", "string false became online");
  assert(presenceFromRow({ uid: "5", is_online: "0" }).status === "offline", "row string zero became online");
  assert(presenceFromRow({ uid: "6", is_online: "false" }).status === "offline", "row string false became online");
  const unknownBadge = presenceBadgeHtml("4", {}, "presence-compact", true);
  assert(unknownBadge.includes("状态未知"), "visible unknown badge lost its label");
  assert(unknownBadge.includes('data-presence-unknown-visible="true"'), "unknown badge was not marked visible");

  handlePeerPresenceEvent({ data: { successUserList: [{ userID: "4", statusType: 0 }] } });
  assert(S.presenceByUid.get("4").updatedAt === 0, "unknown event did not expire cache immediately");
  assert(refreshCalls === 1, "unknown event did not trigger REST fallback refresh");

  const chunks = [];
  S.chat = {
    subscribeUserStatus({ userIDList }) {
      chunks.push([...userIDList]);
      return chunks.length === 2 ? Promise.reject(new Error("chunk failed")) : Promise.resolve();
    },
  };
  const uids = Array.from({ length: 205 }, (_, index) => String(index + 1));
  await subscribePeerPresence(uids);
  assert(chunks.map((chunk) => chunk.length).join(",") === "100,100,5", "subscription was not chunked");
  assert(S.subscribedPresenceUids.size === 105, "failed subscription chunk was recorded as successful");
  assert(!S.subscribedPresenceUids.has("101"), "failed chunk user was marked subscribed");
  assert(S.subscribedPresenceUids.has("205"), "final subscription chunk was skipped");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
            send_text.index("S.chat.sendMessage(message)"),
        )
        self.assertLess(
            send_text.index("appendLocalMessage(pending)"),
            send_text.index('await api("/api/im/rest/send"'),
        )
        self.assertIn("client_message_id: pendingID", send_text)
        self.assertIn("quote: messageQuote", send_text)
        self.assertIn("S.chat.createTextMessage(options)", send_text)
        self.assertIn("S.chat.sendMessage(message)", send_text)
        self.assertIn('api("/api/im/rest/send"', send_text)
        self.assertIn('source: "rest"', send_text)
        self.assertIn('provider: "tim-rest"', send_text)
        self.assertIn("response.message_id", send_text)
        self.assertIn("response.msg_uid", send_text)
        self.assertNotIn("canonicalMessageID(response)", send_text)
        self.assertNotIn("response.compatibility_sync", send_text)
        self.assertNotIn("response.tim_mirror_status", send_text)
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
        peer_entries = self._app_fragment(
            app_js,
            "function peerChatMessageEntries(peer = S.activePeer)",
            "function chatMessageRenderLimit",
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
        self.assertIn("peerChatMessageEntries(activePeer)", chat_log)
        self.assertIn("allEntries.every((entry) => entry.revoked)", chat_log)
        self.assertIn('String(entry.peer || "") === target', peer_entries)
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
        self.assertIn("const conversation = ensureConversationForPeer(uid", open_chat)
        self.assertIn(
            "S.activePeerName = conversationEntryDisplayName(conversation, requestedName, uid)",
            open_chat,
        )
        self.assertNotIn("S.activePeerName = button.dataset.name", open_chat)

    def test_escape_returns_single_pane_chat_to_conversation_list(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        close_helpers = self._app_fragment(
            app_js,
            "function closeActiveConversationForRemoval()",
            "function removeConversationListItems(peers)",
        )
        close_action = self._app_fragment(
            app_js,
            'if (action === "close-conversation") {',
            'if (action === "visitor-tab") {',
        )
        escape_handler = self._app_fragment(
            app_js,
            'document.addEventListener("keydown", (event) => {\n  if (event.key === "Escape") {',
            'document.addEventListener("click", (event) => {',
        )

        self.assertIn("function closeActiveMessageConversation()", close_helpers)
        self.assertIn("S.conversationListCollapsed = false", close_helpers)
        self.assertIn("closeActiveConversationForRemoval()", close_helpers)
        self.assertIn("refreshMessageConversationRegion({ refreshList: false })", close_helpers)
        self.assertLess(
            close_helpers.index("S.conversationListCollapsed = false"),
            close_helpers.index("refreshMessageConversationRegion({ refreshList: false })"),
        )
        self.assertIn("function messageConversationUsesSinglePane()", close_helpers)
        self.assertIn(
            '"(max-width: 640px), (max-height: 560px) and (max-width: 960px) and (any-pointer: coarse)"',
            close_helpers,
        )
        self.assertIn('window.getComputedStyle(listPane).display === "none"', close_helpers)
        self.assertIn('window.getComputedStyle(chatPane).display !== "none"', close_helpers)
        self.assertIn("function hasOpenDialogSurface()", close_helpers)
        self.assertIn('dialog[open], dialog.is-open, [role="dialog"].is-open', close_helpers)
        self.assertIn("closeActiveMessageConversation()", close_action)
        self.assertIn("hadOpenMessageActions", escape_handler)
        self.assertIn("hasOpenDialogSurface()", escape_handler)
        self.assertIn('$("sidebar").classList.contains("open")', escape_handler)
        self.assertIn("S.imComposerPanel", escape_handler)
        self.assertIn("closeChatComposerPanelForKeyboard()", escape_handler)
        self.assertIn("messageConversationUsesSinglePane()", escape_handler)
        self.assertIn("closeActiveMessageConversation()", escape_handler)
        self.assertLess(
            escape_handler.index("hasOpenDialogSurface()"),
            escape_handler.index("hadOpenMessageActions"),
        )
        self.assertLess(
            escape_handler.index("hadOpenMessageActions"),
            escape_handler.index("S.imComposerPanel"),
        )
        self.assertLess(
            escape_handler.index("S.imComposerPanel"),
            escape_handler.index("messageConversationUsesSinglePane()"),
        )
        self.assertLess(
            escape_handler.index("closeChatComposerPanelForKeyboard()"),
            escape_handler.index("closeActiveMessageConversation()"),
        )

    def test_conversation_close_tolerates_scrubbed_media_viewer(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        scrub_dom = self._app_fragment(
            app_js,
            "function scrubAuthenticatedDom()",
            "function deleteOriginDatabase(name)",
        )
        close_viewer = self._app_fragment(
            app_js,
            "function closeChatMediaViewer()",
            "function confirmFlashPhoto(file)",
        )

        self.assertIn('if (id === "chat-media-viewer") {', scrub_dom)
        self.assertIn("dialog.remove()", scrub_dom)
        self.assertLess(
            scrub_dom.index('if (id === "chat-media-viewer") {'),
            scrub_dom.index("dialog.replaceChildren()"),
        )
        self.assertIn('const body = dialog.querySelector("[data-viewer-body]")', close_viewer)
        self.assertIn("if (body) body.replaceChildren()", close_viewer)
        self.assertNotIn('dialog.querySelector("[data-viewer-body]").innerHTML', close_viewer)

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

    def test_local_conversation_profile_requires_name_and_avatar_from_one_update(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the local conversation profile test")

        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        name_functions = "function conversationNameIsPlaceholder" + app_js.split(
            "function conversationNameIsPlaceholder", 1
        )[1].split("function conversationProfileNeedsHydration", 1)[0]
        ensure_function = "function ensureConversationForPeer" + app_js.split(
            "function ensureConversationForPeer", 1
        )[1].split("function updateConversationActivity", 1)[0]
        script = (
            r"""
const S = { conversations: [], activePeer: "", unreadTotal: 0 };
function restoreDismissedConversationPeer() {}
function recalculateUnreadTotal() {}
function conversationPeer(item) { return String(item?.peer_id || ""); }
function conversationDisplayName(item) { return String(item?.nickname || ""); }
function conversationAvatar(item) { return String(item?.avatar || ""); }
function validAvatarValue(...values) {
  return values.map((value) => String(value || "").trim()).find(Boolean) || "";
}
"""
            + name_functions
            + ensure_function
            + r"""
S.conversations = [{
  peer_id: "9",
  nickname: "旧昵称",
  avatar: "https://example.invalid/old.jpg",
  profile_resolved: false,
}];
let updated = ensureConversationForPeer("9", { name: "新昵称", replaceName: true });
if (updated.profile_resolved !== false) throw new Error("name-only update marked complete");
S.conversations[0].profile_resolved = false;
updated = ensureConversationForPeer("9", { avatar: "https://example.invalid/new.jpg" });
if (updated.profile_resolved !== false) throw new Error("avatar-only update marked complete");
S.conversations[0].profile_resolved = false;
updated = ensureConversationForPeer("9", {
  name: "完整昵称",
  avatar: "https://example.invalid/complete.jpg",
  replaceName: true,
});
if (updated.profile_resolved !== true) throw new Error("complete update remained unresolved");
S.conversations = [];
const created = ensureConversationForPeer("10", { name: "只有昵称" });
if (created.profile_resolved !== false) throw new Error("partial new conversation marked complete");
S.conversations = [{
  peer_id: "11",
  nickname: "真实昵称",
  avatar: "https://example.invalid/resolved.jpg",
  profile_resolved: true,
}];
const preserved = ensureConversationForPeer("11", {
  name: "乐园用户",
  replaceName: true,
});
if (preserved.nickname !== "真实昵称") throw new Error("fallback name replaced resolved nickname");
if (preserved.profile_resolved !== true) throw new Error("resolved profile was downgraded");
if (conversationEntryDisplayName(preserved, "乐园用户", "11") !== "真实昵称") {
  throw new Error("chat heading preferred fallback name");
}
S.conversations = [];
const fallbackOnly = ensureConversationForPeer("12", { name: "乐园用户" });
if (fallbackOnly.nickname !== "用户 12") throw new Error("fallback name was persisted");
if (conversationEntryDisplayName(fallbackOnly, "乐园用户", "12") !== "用户 12") {
  throw new Error("fallback heading was not normalized");
}
"""
        )
        result = subprocess.run(
            [node, "-e", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_web_native_rich_media_recording_and_flash_are_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        media_send = self._app_fragment(
            app_js,
            "async function sendTimMediaFile",
            "function defineFileMetadata",
        )
        flash_send = self._app_fragment(
            app_js,
            "async function sendFlashPhoto",
            "async function handleChatUploadInput",
        )
        flash_view = self._app_fragment(
            app_js,
            "async function openFlashViewer",
            "async function pageNearby",
        )

        self.assertNotIn("/api/im/media/uploads", app_js)
        self.assertNotIn("/api/im/media/messages", app_js)
        self.assertIn("ensureTimMediaReady()", media_send)
        self.assertIn("chat.createImageMessage(options)", media_send)
        self.assertIn("chat.createAudioMessage(options)", media_send)
        self.assertIn("chat.createVideoMessage(options)", media_send)
        self.assertIn("chat.createFileMessage(options)", media_send)
        self.assertIn("chat.sendMessage(message)", media_send)
        self.assertNotIn('source: "web-local"', media_send)
        self.assertNotIn('provider: "web-local"', media_send)
        self.assertIn("new MediaRecorder(", app_js)
        self.assertIn("const form = new FormData();", flash_send)
        self.assertIn('form.append("file", file', flash_send)
        self.assertIn('api("/api/im/flash/send"', flash_send)
        self.assertNotIn('source: "web-local"', flash_send)
        self.assertIn('`/api/im/media/attachments/${encodeURIComponent(id)}/claim`', flash_view)
        self.assertIn('api("/api/im/flash/get"', app_js)
        self.assertIn('api("/api/im/flash/ack"', app_js)
        self.assertIn("archiveRevealedFlashPhoto(id, url)", app_js)
        self.assertIn("data?.ok === true && data?.acknowledged === true", app_js)
        self.assertIn("queueFlashRevealAcknowledgement(id)", app_js)
        self.assertIn("flushPendingFlashAcknowledgements()", app_js)
        self.assertIn("clearLocalStoragePreservingFlashAcknowledgements()", app_js)
        self.assertIn("key.startsWith(FLASH_ACK_STORAGE_PREFIX)", app_js)
        self.assertIn("normalizeStoredFlashAcknowledgements(stored, now)", app_js)
        self.assertNotIn("localStorage.clear()", app_js)
        self.assertIn("keepalive: true", app_js)
        self.assertIn("const rawUrl = firstMessageValue", flash_view)
        self.assertIn("const url = mediaUrl(rawUrl)", flash_view)
        self.assertNotIn("hold.controller?.abort()", app_js)

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
        moment_video_markup = moment_media.split("const videoHtml = video", 1)[1]
        close_profile = app_js.split("function closeProfileDialog()", 1)[1].split(
            "async function openProfile", 1
        )[0]
        transcode_service = compose.split("  transcode-worker:", 1)[1].split("  scheduler:", 1)[0]

        self.assertIn("moyuanoss\\.oss-cn-shanghai\\.aliyuncs\\.com", app_js)
        self.assertIn("appletattachment\\.oss-cn-beijing\\.aliyuncs\\.com", app_js)
        self.assertLess(media_url.index("APK_MEDIA_ORIGIN_RE"), media_url.index('raw.startsWith("//")'))
        self.assertIn('data-media-playback data-moment-video="true"', moment_media)
        self.assertIn('preload="none"', moment_media)
        self.assertNotIn('preload="metadata"', moment_media)
        self.assertIn('data-media-mode="original"', moment_media)
        self.assertIn('data-post-id="${esc(', moment_media)
        self.assertIn('data-original-source="${esc(video)}"', moment_media)
        self.assertIn('data-media-source="${esc(', moment_media)
        self.assertIn('data-video-frame-required="true"', moment_media)
        self.assertIn('data-video-state="poster"', moment_media)
        self.assertIn('data-action="play-moment-video"', moment_media)
        self.assertNotIn(' src="${esc(', moment_video_markup)
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
        self.assertIn('media.dataset.playbackRequested !== "1"', playback_error)
        self.assertLess(
            playback_error.index('media.dataset.playbackRequested !== "1"'),
            playback_error.index("const mediaErrorCode"),
        )
        self.assertNotIn('setMomentVideoFallback(media, "视频加载失败"', decode_fallback)
        self.assertIn("function cancelPendingVideoFrameCallback(video)", app_js)
        self.assertIn("video.cancelVideoFrameCallback(callbackId)", app_js)
        self.assertIn('media.dataset.mediaMode === "compat"', app_js)
        self.assertIn('media.dataset.mediaMode = "original"', app_js)
        self.assertIn("正在准备兼容版本…", app_js)
        self.assertIn("moment_video_compat", app_js)
        self.assertIn("MOMENT_VIDEO_COMPAT_POLL_DELAYS_MS", app_js)
        self.assertIn("function momentVideoRequestFailure(value, data", app_js)
        self.assertIn("function retryMomentVideoCompatibilityAfterOnline()", app_js)
        self.assertIn('video.dataset.compatRetryOnOnline = retryOnOnline ? "1" : "0"', app_js)
        self.assertIn("isTransientMomentVideoRequest(status)", app_js)
        self.assertIn("suggestedPollDelay = status.retryAfterMs", app_js)
        self.assertIn("Date.now() - startedAt < MOMENT_VIDEO_INITIAL_FRAME_WAIT_MS", app_js)
        self.assertIn('(compatMoment || !video.paused)', app_js)
        self.assertIn("视频暂时无法播放", app_js)
        self.assertIn("当前浏览器暂时无法播放此视频", app_js)
        self.assertIn(
            "suspendMomentVideos(dialog, { cancelCompat: true });",
            close_profile,
        )
        self.assertNotIn("可打开原视频或下载后播放", app_js)
        self.assertIn(".moment-video-wrap", app_css)
        self.assertNotIn(".moment-playback-actions", app_css)
        context_menu_guard = app_js.split(
            'document.addEventListener(\n  "contextmenu",', 1
        )[1].split('document.addEventListener(\n  "loadedmetadata",', 1)[0]
        self.assertIn('video[data-moment-video="true"]', context_menu_guard)
        self.assertIn("event.preventDefault()", context_menu_guard)
        moment_video_css = app_css.split(".moment-video {", 1)[1].split("}", 1)[0]
        self.assertIn("aspect-ratio: auto 16 / 9", moment_video_css)
        self.assertNotIn("aspect-ratio: 16 / 9", moment_video_css)
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

    def test_chat_images_are_reused_and_keep_stable_dimensions_during_message_refresh(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")

        image_body = self._app_fragment(app_js, "function chatMessageBodyHtml(entry)", "function chatMessageContentHtml")
        media_reuse = self._app_fragment(app_js, "function chatMediaNodeIdentity(image)", "function scrollChatLogToBottom")
        upload_handler = self._app_fragment(app_js, "async function handleChatUploadInput(input)", "async function sendChatSticker")
        conversation_refresh = self._app_fragment(
            app_js,
            "function refreshMessageConversationRegion({",
            "function syncChatComposerInput",
        )

        self.assertIn("function chatImageDimensionAttributes(media)", app_js)
        self.assertIn("chatImageDimensionAttributes(", image_body)
        self.assertIn('width="${width}" height="${height}"', app_js)
        self.assertIn("function captureReusableChatMedia(root)", media_reuse)
        self.assertIn("function restoreReusableChatMedia(root, captured)", media_reuse)
        self.assertIn("nextImage.replaceWith(previousImage)", media_reuse)
        self.assertIn("function renderChatLog(log", media_reuse)
        self.assertNotIn("log.innerHTML = chatLogHtml()", app_js)
        self.assertIn('captureReusableChatMedia(pane.querySelector("#im-log"))', conversation_refresh)
        self.assertIn('restoreReusableChatMedia(pane.querySelector("#im-log"), reusableChatMedia)', conversation_refresh)
        self.assertIn("async function readImageMetadata(file)", app_js)
        self.assertIn('if (kind === "image")', upload_handler)
        self.assertIn("meta = await readImageMetadata(file)", upload_handler)
        self.assertIn('const peer = String(S.activePeer || "").trim()', upload_handler)
        self.assertLess(
            upload_handler.index('const peer = String(S.activePeer || "").trim()'),
            upload_handler.index("meta = await readImageMetadata(file)"),
        )
        self.assertIn("await sendTimMediaFile(kind, file, { ...meta, peer })", upload_handler)
        self.assertIn("await sendFlashPhoto(file, { peer })", upload_handler)
        self.assertIn('peer: requestedPeer = ""', app_js)
        self.assertIn("retryMessageId: entry.id, peer: entry.peer", app_js)
        self.assertIn("height: auto", app_css.split(".chat-image-button img {", 1)[1].split("}", 1)[0])

    def test_media_picker_and_runtime_whitelists_match_web_native_media_service(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        composer = self._app_fragment(app_js, "function chatComposerHtml()", "function chatPaneHtml()")
        constants = self._app_fragment(app_js, "const TIM_VIDEO_MIME_TYPES", "function parseJsonValue")
        validator = self._app_fragment(app_js, "function validateChatFile", "async function sendFlashPhoto")
        normalizer = self._app_fragment(app_js, "function normalizeChatPickerFile", "function validateChatFile")

        video_input = composer.split('id="im-file-video"', 1)[1].split("/>", 1)[0]
        video_accept = video_input.split('accept="', 1)[1].split('"', 1)[0]
        self.assertEqual(video_accept, ".mp4,.mov,.webm,video/mp4,video/quicktime,video/webm")
        self.assertIn('const TIM_VIDEO_MIME_TYPES = new Set(["video/mp4", "video/quicktime", "video/webm"]);', constants)
        self.assertIn("const TIM_VIDEO_FILE_EXTENSION_RE = /\\.(?:mp4|mov|webm)$/i;", constants)
        self.assertIn("TIM_VIDEO_FILE_EXTENSION_RE.test(name)", validator)
        self.assertIn("TIM_VIDEO_MIME_TYPES.has(mime)", validator)
        self.assertIn('mov: "video/quicktime"', constants)
        self.assertIn("GENERIC_PICKER_MIME_TYPES.has(currentType)", normalizer)
        self.assertIn("return new File([file], file.name", normalizer)

        image_input = composer.split('id="im-file-image"', 1)[1].split("/>", 1)[0]
        flash_input = composer.split('id="im-file-flash"', 1)[1].split("/>", 1)[0]
        self.assertIn(".avif", image_input)
        self.assertIn("image/avif", image_input)
        self.assertIn(".avif", flash_input)
        self.assertIn("image/avif", flash_input)
        self.assertNotIn(".bmp", image_input)
        self.assertNotIn("image/bmp", image_input)
        image_types = constants.split("const TIM_IMAGE_MIME_TYPES", 1)[1].split(";", 1)[0]
        flash_image_types = constants.split("const FLASH_IMAGE_MIME_TYPES", 1)[1].split(";", 1)[0]
        self.assertIn('"image/avif"', image_types)
        self.assertIn('"image/avif"', flash_image_types)
        self.assertNotIn('"image/bmp"', image_types)
        self.assertNotIn('"image/bmp"', flash_image_types)
        self.assertIn("isFlash ? FLASH_IMAGE_FILE_EXTENSION_RE : TIM_IMAGE_FILE_EXTENSION_RE", validator)
        self.assertIn("isFlash ? FLASH_IMAGE_MIME_TYPES : TIM_IMAGE_MIME_TYPES", validator)

    def test_native_media_uses_private_access_and_releases_local_blob_urls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        image_media = self._app_fragment(app_js, "function normalizeImageMedia", "function normalizeEntryMedia")
        entry_media = self._app_fragment(app_js, "function normalizeEntryMedia", "function messageFlashID")
        object_urls = self._app_fragment(app_js, "function trackChatObjectUrl", "async function ensureTimMediaReady")
        sender = self._app_fragment(app_js, "async function sendTimMediaFile", "function defineFileMetadata")
        private_access = self._app_fragment(
            app_js,
            "async function requestNativeMediaAccess",
            "function closeChatMediaViewer",
        )
        logout = self._app_fragment(app_js, "async function logout(", "async function loadMomentComments")

        self.assertIn('"imageUrl"', image_media)
        self.assertIn('"remoteAudioUrl"', entry_media)
        self.assertIn('"remoteVideoUrl"', entry_media)
        self.assertIn("URL.revokeObjectURL(url)", object_urls)
        self.assertIn("collectBlobObjectUrls", object_urls)
        self.assertIn("ensureTimMediaReady()", sender)
        self.assertIn("chat.createImageMessage(options)", sender)
        self.assertIn("chat.sendMessage(message)", sender)
        self.assertNotIn('/api/im/media/messages', sender)
        self.assertNotIn('native: true', sender)
        self.assertIn("if (!mediaReferencesUrl(replacement.media, localMedia.url))", sender)
        self.assertIn("revokeChatObjectUrl(localMedia.url)", sender)
        self.assertIn('`/api/im/media/attachments/${encodeURIComponent(id)}/access`', private_access)
        self.assertIn("url: grant.url", private_access)
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
        self.assertIn("仍将上传并由服务端校验格式", metadata)
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

    def test_voice_recording_explains_secure_context_and_uses_native_media_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        availability = self._app_fragment(app_js, "function voiceRecordingAvailability", "function syncVisualViewport")
        recording = self._app_fragment(app_js, "async function startVoiceRecording", "function moveVoiceRecording")
        connection_status = self._app_fragment(
            app_js,
            "function imConnectionStatusText",
            "function updateImConnectionStatus",
        )
        native_sender = self._app_fragment(
            app_js,
            "async function sendTimMediaFile",
            "function defineFileMetadata",
        )
        self.assertIn("window.isSecureContext", availability)
        self.assertIn("录音需要安全网页环境或本机访问", availability)
        self.assertIn("voiceRecordingAvailability()", recording)
        self.assertIn('sendTimMediaFile("audio", file', recording)
        self.assertIn("文本备用通道", connection_status)
        self.assertNotIn("Web 本地文字和媒体可用", connection_status)
        self.assertIn("ensureTimMediaReady()", native_sender)
        self.assertIn("chat.createAudioMessage(options)", native_sender)
        self.assertNotIn('/api/im/media/messages', native_sender)

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

    def test_byok_runner_navigation_requires_current_server_authorization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        access = self._app_fragment(
            app_js,
            "function setAiAgentAccess",
            "function agentErrorMessage",
        )
        refresh = self._app_fragment(
            app_js,
            "async function refreshAiAgentAccess",
            "async function agentApi",
        )
        login = self._app_fragment(
            app_js,
            "async function completeBrowserLogin",
            "function clearPendingCredentialInputs",
        )
        restored = self._app_fragment(
            app_js,
            "async function completeRestoredSession",
            "async function restoreSessionAtBoot",
        )
        visibility = self._app_fragment(
            app_js,
            'document.addEventListener("visibilitychange"',
            'window.addEventListener("blur"',
        )

        self.assertIn("aiAgentAccessEnabled: false", app_js)
        self.assertIn("aiAgentAccessRefreshSeq: 0", app_js)
        self.assertIn('api("/api/agent/status"', refresh)
        self.assertIn("const generation = S.sessionGeneration", refresh)
        self.assertIn("const requestSeq = ++S.aiAgentAccessRefreshSeq", refresh)
        self.assertIn("generation === S.sessionGeneration", refresh)
        self.assertIn("requestSeq === S.aiAgentAccessRefreshSeq", refresh)
        self.assertIn("if (!responseIsCurrent()) return null", refresh)
        self.assertIn("[401, 403, 404].includes(result.status)", refresh)
        self.assertIn('go("me", { replace: true, force: true })', access)
        self.assertIn(
            "return S.aiAgentAccessEnabled ? [...MINE_NAV, AI_AGENT_NAV] : MINE_NAV",
            app_js,
        )
        self.assertIn("await refreshAiAgentAccess({ redirect: false })", login)
        self.assertIn("await refreshAiAgentAccess({ redirect: false })", restored)
        self.assertIn("refreshAiAgentAccess({ redirect: true })", visibility)

    def test_byok_api_key_and_view_state_are_cleared_at_session_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        api = self._app_fragment(app_js, "async function api(path", "function archiveHash")
        access = self._app_fragment(
            app_js,
            "function clearAgentApiKeyInputs",
            "function agentErrorMessage",
        )
        switch_mine = self._app_fragment(
            app_js,
            "async function switchMineTab",
            "function routeCacheKey",
        )
        activate_route = self._app_fragment(
            app_js,
            "async function activateRoute",
            "function loadingState",
        )
        logout = self._app_fragment(
            app_js,
            "async function logout(",
            "async function loadMomentComments",
        )
        forms = self._app_fragment(
            app_js,
            "async function handleProductForm",
            "function applyFeatureEnvelope",
        )
        connection_form = forms.split('if (kind === "agent-connection")', 1)[1].split(
            'if (kind === "agent-settings")', 1
        )[0]
        pagehide = self._app_fragment(
            app_js,
            'window.addEventListener("pagehide"',
            "syncVisualViewport();",
        )

        self.assertIn('input[name="api_key"]', access)
        self.assertIn("clearAgentApiKeyInputs(document)", access)
        self.assertIn("setAiAgentAccess(false, null, { redirect: false })", api)
        self.assertIn("setAiAgentAccess(false, null, { redirect: false })", logout)
        self.assertIn('S.route === "agent" && target !== "agent"', switch_mine)
        self.assertIn("clearAgentApiKeyInputs(panel)", switch_mine)
        self.assertIn('S.route === "agent" && target !== "agent"', activate_route)
        self.assertIn("clearAgentApiKeyInputs(root())", activate_route)
        self.assertIn("clearAgentApiKeyInputs(form)", connection_form)
        self.assertIn("clearAgentApiKeyInputs(document)", pagehide)
        self.assertLess(
            connection_form.index("clearAgentApiKeyInputs(form)"),
            connection_form.index('await agentApi("/api/agent/connection"'),
        )
        self.assertIn('normalized === "agent" || normalized === "mine:agent"', app_js)
        self.assertIn('if (S.route === "agent" || isSensitiveRouteCacheKey(key))', app_js)
        self.assertIn('if (target !== "msg" && target !== "agent")', activate_route)

    def test_byok_account_execution_is_separately_gated_and_twice_confirmed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        execution_state = self._app_fragment(
            app_js,
            "function normalizedAiAgentExecutionStatus",
            "function setAiAgentAccess",
        )
        execution_api = self._app_fragment(
            app_js,
            "async function agentExecutionApi",
            "function newAgentIdempotencyKey",
        )
        review = self._app_fragment(
            app_js,
            "function renderAiAgentExecutionConfirmation",
            "async function stageAiAgentExecution",
        )
        prepare = self._app_fragment(
            app_js,
            "async function stageAiAgentExecution",
            "async function executeAiAgentPendingExecution",
        )
        execute = self._app_fragment(
            app_js,
            "async function executeAiAgentPendingExecution",
            "function syncAgentExecutionActionForm",
        )
        section = self._app_fragment(
            app_js,
            "function agentExecutionSectionHtml",
            "async function pageAgent",
        )
        page = self._app_fragment(
            app_js,
            "async function pageAgent",
            "async function pageLab",
        )
        forms = self._app_fragment(
            app_js,
            "async function handleProductForm",
            "function applyFeatureEnvelope",
        )
        switch_mine = self._app_fragment(
            app_js,
            "async function switchMineTab",
            "function routeCacheKey",
        )
        pagehide = self._app_fragment(
            app_js,
            'window.addEventListener("pagehide"',
            "syncVisualViewport();",
        )

        for action in (
            "send_private_message",
            "publish_text_post",
            "follow_user",
            "unfollow_user",
        ):
            self.assertIn(action, app_js)
        self.assertIn("source.available !== true", execution_state)
        self.assertIn("source.admin_granted !== true", execution_state)
        self.assertIn("source.system_enabled !== true", execution_state)
        self.assertIn("AI_AGENT_EXECUTION_ACTIONS", execution_state)
        self.assertIn("allowedSet.has(action)", execution_state)
        self.assertIn("if (!execution) return \"\"", section)
        self.assertIn("默认不选择任何动作", section)
        self.assertIn("开启后不会自动监听或接管账号", section)
        self.assertIn("selected.has(action) ? \"checked\" : \"\"", section)
        self.assertNotIn("agentExecutionSectionHtml(", page)
        for removed_surface in ("回复草稿", "手动账号工具", "二次确认"):
            self.assertNotIn(removed_surface, page)
        self.assertIn("模型与表达设置", page)
        self.assertIn("语言风格", page)
        self.assertIn('agentExecutionApi("/api/agent/execution-settings"', forms)
        self.assertIn("user_enabled: userEnabled", forms)
        self.assertIn("auto_send_enabled: autoSendEnabled", forms)
        self.assertIn("selected_actions: selectedActions", forms)
        self.assertIn('agentExecutionApi("/api/agent/actions/prepare"', prepare)
        self.assertIn("prepared.confirmation_token", prepare)
        self.assertIn("executionGeneration !== S.aiAgentExecutionGeneration", prepare)
        self.assertLess(
            prepare.index('agentExecutionApi("/api/agent/actions/prepare"'),
            prepare.index("S.aiAgentPendingExecution = Object.freeze"),
        )
        self.assertNotIn("confirmationToken", review)
        self.assertIn("prepared.summary", prepare)
        self.assertIn("serverSummary", review)
        self.assertIn("agent-execution-countdown", review)
        self.assertIn("一次性确认凭证已过期", review)
        self.assertIn("window.confirm(", execute)
        self.assertIn('agentExecutionApi("/api/agent/replies/send"', execute)
        self.assertIn('agentExecutionApi("/api/agent/actions/execute"', execute)
        self.assertIn("confirmation_token: pending.confirmationToken", execute)
        self.assertIn('String(data.draft || "").trim()', execute)
        self.assertLess(
            execute.index("window.confirm("),
            execute.index('agentExecutionApi("/api/agent/replies/send"'),
        )
        self.assertLess(
            execute.index("window.confirm("),
            execute.index('agentExecutionApi("/api/agent/actions/execute"'),
        )
        self.assertIn("requireAutoSend: directReply", execute)
        self.assertIn("pending.executionGeneration !== S.aiAgentExecutionGeneration", execute)
        self.assertIn("clearAiAgentPendingExecution()", switch_mine)
        self.assertIn("setAiAgentExecutionStatus(null)", pagehide)
        self.assertIn("setAiAgentExecutionStatus(null)", execution_api)
        self.assertEqual(app_js.count('"/api/agent/actions/execute"'), 1)
        self.assertEqual(app_js.count('"/api/agent/replies/send"'), 1)

    def test_byok_connection_refreshes_stale_ready_ui_and_keeps_checkbox_focus_visible(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        form_pending = self._app_fragment(
            app_js,
            "function withFormPending",
            "function reportAsyncError",
        )
        pending = self._app_fragment(
            app_js,
            "function markAiAgentConnectionPending",
            "let aiAgentConfirmationCountdownTimer",
        )
        readiness = self._app_fragment(
            app_js,
            "function syncAiAgentAutonomyModelReadiness",
            "function aiAgentRunnerEnabledFromPage",
        )
        changed = self._app_fragment(
            app_js,
            "function aiAgentConnectionFormChanged",
            "function commitAiAgentConnectionFormDefaults",
        )
        refresh = self._app_fragment(
            app_js,
            "async function refreshAiAgentPageAfterConnection",
            "let aiAgentConfirmationCountdownTimer",
        )
        page = self._app_fragment(
            app_js,
            "async function pageAgent(signal,",
            "async function pageLab()",
        )
        forms = self._app_fragment(
            app_js,
            "async function handleProductForm",
            "function applyFeatureEnvelope",
        )
        connection_form = forms.split('if (kind === "agent-connection")', 1)[1].split(
            'if (kind === "agent-settings")', 1
        )[0]
        submit = self._app_fragment(
            app_js,
            'document.addEventListener("submit"',
            'document.addEventListener("keydown"',
        )

        self.assertIn('form.dataset.pending === "true"', form_pending)
        self.assertIn('querySelectorAll("input, select, textarea, button")', form_pending)
        self.assertIn("control.disabled = true", form_pending)
        self.assertIn("control.disabled = wasDisabled", form_pending)
        self.assertIn('form.dataset.form === "agent-connection"', submit)
        self.assertIn("const submittedValues = formValues(form)", submit)
        self.assertIn("withFormPending(form, submitter", submit)
        self.assertIn("handleProductForm(form, submitter, submittedValues)", submit)
        self.assertLess(
            submit.index("const submittedValues = formValues(form)"),
            submit.index("withFormPending(form, submitter"),
        )
        self.assertIn("input?.defaultValue", changed)
        self.assertIn("enabled?.defaultChecked", changed)
        self.assertIn("const connectionChanged = aiAgentConnectionFormChanged", connection_form)
        self.assertIn("if (connectionChanged) {", connection_form)
        self.assertLess(
            connection_form.index("if (connectionChanged) {"),
            connection_form.index('agentApi("/api/agent/connection"'),
        )
        self.assertLess(
            connection_form.index('agentApi("/api/agent/connection"'),
            connection_form.index('agentApi("/api/agent/connection/test"'),
        )
        self.assertIn("S.aiAgentModelReady = false", pending)
        self.assertIn("clearAiAgentPendingExecution()", pending)
        self.assertIn("syncAiAgentReadyControls(page, false)", pending)
        self.assertIn("syncAiAgentAutonomyModelReadiness(page, false)", pending)
        self.assertIn("effective_enabled: effectiveEnabled", readiness)
        self.assertIn("ready &&", readiness)
        self.assertIn("markAiAgentConnectionReady(form)", connection_form)
        self.assertLess(
            connection_form.index("markAiAgentConnectionReady(form)"),
            connection_form.index("refreshAiAgentPageAfterConnection(form)"),
        )
        self.assertIn('agentApi("/api/agent/status"', refresh)
        self.assertIn("pageData: status", refresh)
        self.assertIn("prefetchedData ||", page)
        self.assertIn("markAiAgentConnectionPending(form, { testing: testAfterSave })", connection_form)
        self.assertLess(
            connection_form.index("markAiAgentConnectionPending(form, { testing: testAfterSave })"),
            connection_form.index('agentApi("/api/agent/connection/test"'),
        )
        self.assertIn("connection.enabled === false", page)
        self.assertLess(
            page.index("connection.enabled === false"),
            page.index('connection.last_test_status === "ok"'),
        )
        self.assertIn('.check-line input[type="checkbox"]:focus-visible', app_css)
        self.assertIn("outline:", app_css.split('.check-line input[type="checkbox"]:focus-visible', 1)[1].split("}", 1)[0])
        capability = app_css.split(".agent-capability {", 1)[1].split("}", 1)[0]
        capability_focus = app_css.split(".agent-capability-summary:focus-visible", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: visible", capability)
        self.assertIn("outline-offset: -", capability_focus)

    def test_profile_editor_uses_apk_reset_without_local_avatar_or_password(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        page_me = self._app_fragment(app_js, "async function pageMe", "function agentConnectionStatusText")
        forms = self._app_fragment(app_js, "async function handleProductForm", "function applyFeatureEnvelope")
        details_form = forms.split('if (kind === "profile-details")', 1)[1].split(
            'if (kind === "referral-set")', 1
        )[0]

        self.assertIn('data-form="profile-details"', page_me)
        self.assertIn("资料以原账号服务为准", page_me)
        self.assertIn("原 APK 接口", page_me)
        for field in ("nickname", "signature", "city", "gender"):
            self.assertIn(f'name="{field}"', page_me)
        self.assertNotIn('data-form="profile-avatar"', page_me)
        self.assertNotIn("profile-avatar-file", page_me)
        self.assertNotIn('data-form="local-password-change"', page_me)
        self.assertNotIn("avatar_asset_id", app_js)
        self.assertNotIn('/api/auth/password', app_js)
        self.assertIn('api("/api/profile/reset"', details_form)
        self.assertIn("type: item.type", details_form)
        self.assertIn("value: item.value", details_form)
        self.assertIn("operation_id: newProfileOperationId(item.field)", details_form)
        self.assertIn("clearMomentCache()", details_form)

    def test_moment_composer_publishes_text_only_through_original_service(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        page = self._app_fragment(app_js, "async function pageMoments", "function syncMomentsTabUI")
        forms = self._app_fragment(app_js, "async function handleProductForm", "function applyFeatureEnvelope")
        publish = forms.split('if (kind === "moment-publish")', 1)[1].split(
            'if (kind === "moment-comment")', 1
        )[0]

        self.assertIn('placeholder="分享此刻的想法" required', page)
        self.assertNotIn('data-moment-media="image"', page)
        self.assertNotIn('data-moment-media="video"', page)
        self.assertNotIn("media_asset_ids", app_js)
        self.assertNotIn("createNativeMediaAsset", app_js)
        self.assertNotIn("/api/im/media/uploads", app_js)
        self.assertIn('api("/api/moments/publish"', publish)
        request_body = publish.split('body: JSON.stringify({', 1)[1].split("}),", 1)[0]
        for field in (
            "text,",
            "visibility_scope:",
            "topic:",
            'plate: "动态"',
            "comment_forbid:",
            "hide_comment:",
        ):
            self.assertIn(field, request_body)
        for local_media_field in ("media_asset_ids", "pictures:", "video:", "cover:"):
            self.assertNotIn(local_media_field, request_body)
        self.assertIn("form.reset()", publish)
        self.assertIn("clearMomentCache()", publish)

    def test_native_profile_and_moment_media_paths_remain_same_origin_and_current(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        media_url = self._app_fragment(app_js, "const LOCAL_PRIVATE_MEDIA_PATH_RE", "function validAvatarValue")
        moment_media = self._app_fragment(app_js, "function momentMediaHtml", "function momentOwnershipMenu")
        moment_card = self._app_fragment(app_js, "function renderMomentCard", "function momentCard")

        self.assertIn("(?:native\\/)?", media_url)
        self.assertIn("if (LOCAL_PRIVATE_MEDIA_PATH_RE.test(raw)) return raw", media_url)
        self.assertLess(
            media_url.index("if (LOCAL_PRIVATE_MEDIA_PATH_RE.test(raw)) return raw"),
            media_url.index("const canonical = raw.replace"),
        )
        self.assertIn('const video = pictures.length ? "" : mediaUrl(post.video)', moment_media)
        self.assertIn("pictureHtml || videoHtml", moment_media)
        self.assertNotIn("这条动态没有文字内容", moment_card)
        self.assertIn('String(post.content || "").trim()', moment_card)
        self.assertNotIn("clearComposeDrafts()", app_js)
        self.assertNotIn("composeObjectUrls", app_js)

    def test_static_asset_cache_versions_match_content_hashes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")

        css_version = index_html.split('/static/app.css?v=', 1)[1].split('"', 1)[0]
        js_version = index_html.split('/static/app.js?v=', 1)[1].split('"', 1)[0]
        css_hash = hashlib.sha256((root / "bbw_web" / "static" / "app.css").read_bytes()).hexdigest()[:16]
        js_hash = hashlib.sha256((root / "bbw_web" / "static" / "app.js").read_bytes()).hexdigest()[:16]
        self.assertEqual(css_version, css_hash)
        self.assertEqual(js_version, js_hash)


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
