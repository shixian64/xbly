from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bbw_protocol.client import ApiResult
from bbw_protocol.adapters.im import ImAdapter
from bbw_protocol.modules.misc import MiscAPI
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
        self.assertIn('S.pageCache.delete("tasks")', app_js)
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
                    "userInfoList": {"id": "42", "nickname": "当前用户"},
                }
            ],
        )
        item = conversation_envelope(app, result, {})["items"][0]
        self.assertEqual(item["peer_id"], "9")
        self.assertEqual(item["avatar"], "")
        self.assertEqual(item["nickname"], "9")
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
                    "like": "3",
                    "comment_sum": "2",
                    "topic": '[{"topic":"旅行"}]',
                    "u_top": 1,
                }
            ]
        )
        self.assertEqual(posts[0]["content"], "第一行\n第二行")
        self.assertEqual(posts[0]["pictures"][0], "https://oss.banghua.xin/images/a.jpg")
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


class SocialBffRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_store = bff_server.STORE
        bff_server.STORE = SimpleNamespace()

    def tearDown(self) -> None:
        bff_server.STORE = self.old_store

    def _run_get(self, path: str, *, false_social: set[str] | None = None):
        result = ApiResult(True, 200, "[]", data=[])
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
            native=SimpleNamespace(
                tim_rest=SimpleNamespace(
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
        self.assertEqual(calls, [("conversations", "3")])
        self.assertEqual(response[1]["entity"], "conversation")

        calls, response = self._run_get("/api/im/messages?peer=9")
        self.assertEqual(calls, [("messages", "9")])
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
        self.assertEqual(calls, [("friend_apply", "1"), ("friends", None)])
        self.assertEqual(response[1]["items"], [])

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

    def test_nearby_moments_use_profile_region_and_report_missing_location(self) -> None:
        def run(raw_user, profile_data=None):
            calls = []
            result = ApiResult(True, 200, "[]", data=[])
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

        calls, response = run({}, {"id": "42", "region": "浙江-杭州"})
        self.assertEqual(calls[0][2]["filter_region"], "浙江-杭州")
        self.assertEqual(response[1]["location_region"], "浙江-杭州")

        calls, response = run({}, {})
        self.assertEqual(calls, [])
        self.assertEqual(response[1]["code"], "PROFILE_LOCATION_MISSING")
        self.assertTrue(response[1]["location_required"])

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

    def test_room_page_states_limited_web_support_truthfully(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        bff_server_py = (root / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")

        for marker in (
            "网页版当前仅提供房间榜单及接口状态查询，尚未接入实时语音。",
            "网页版暂不支持实时语音",
            "当前无法进入房间收听、上麦或通话。",
            "服务端当前没有返回可展示的房间，可稍后刷新。",
            "仅提交服务端建房请求，不代表网页版可进入房间通话",
            "仅检查凭证接口响应，不代表网页版已接入实时音频",
            'data-action="room-native-refresh">读取客户端房间列表',
            'api("/api/room/native-list"',
            "S.roomkitAvailable = Boolean(data?.capabilities?.roomkit_list);",
        ):
            self.assertIn(marker, app_js)
        self.assertNotIn("稍后再来听听", app_js)
        self.assertIn('data.code === "FALSE_RESPONSE" && data.entity === "room"', app_js)
        self.assertIn("function normalizeRoomCreateResult(data)", app_js)
        self.assertIn('code: "ROOM_CREATE_UNAVAILABLE"', app_js)
        self.assertIn("room_top_envelope(app.room.top())", bff_server_py)
        self.assertIn("room_create_envelope(", bff_server_py)
        self.assertIn('if path == "/api/room/native-list"', bff_server_py)
        self.assertIn('"roomkit_list": True', bff_server_py)
        self.assertIn('"invite_login": INVITE_LOGIN_ENABLED', bff_server_py)
        self.assertIn(".room-cover", app_css)
        self.assertIn(".room-owner-avatar", app_css)

    def test_match_page_uses_responsive_preference_workbench(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        match_hub_background = root / "bbw_web" / "static" / "match-hub-bg.png"

        for marker in (
            'class="match-hub-header"',
            'data-action="match-tab"',
            "async function switchMatchHubTab(tab)",
            'history.pushState(null, "", matchRouteHash(activeTab))',
            'return switchMatchHubTab(tab);',
            '["match", "匹配"]',
            '["room", "语音房"]',
            'class="match-overview"',
            'data-form="match-filter"',
            'class="match-mode-grid"',
            'class="match-compose-grid"',
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
        self.assertIn('url("/static/match-hub-bg.png")', app_css)
        self.assertTrue(match_hub_background.is_file())
        self.assertNotIn('statCard(display.online ?? "—", "在线免费")', app_js)

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
            'panel.innerHTML = momentsTabPanelHtml(view)',
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
        self.assertIn('String(data?.next_page || "")', moments_loader)
        self.assertIn('String(posts.at(-1)?.id || "")', moments_loader)
        self.assertIn(".moments-tab-panel.is-loading", app_css)

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
            "const PEER_PRESENCE_TTL_MS", 1
        )[0]

        self.assertNotIn("match-section-index", app_js)
        self.assertNotIn("match-section-index", app_css)
        self.assertNotIn("function firstChar", app_js)
        self.assertNotIn("avatar.textContent", app_js)
        self.assertIn("mediaUrl(validAvatarValue(url))", avatar_renderer)
        self.assertIn('if (!src) return "";', avatar_renderer)
        self.assertIn('class="avatar avatar-loading"', avatar_renderer)
        self.assertIn('aria-hidden="true"', avatar_renderer)
        self.assertIn('avatar.classList.remove("avatar-loading")', avatar_renderer)
        self.assertIn(".avatar.avatar-loading", app_css)
        self.assertIn("visibility: hidden", app_css)
        self.assertIn("data-avatar-image", avatar_renderer)
        self.assertIn('loading="eager"', avatar_renderer)
        self.assertNotIn('loading="lazy"', avatar_renderer)
        self.assertNotIn("onload=", avatar_renderer)
        self.assertNotIn("onerror=", avatar_renderer)
        self.assertIn("function revealLoadedAvatar(image)", app_js)
        self.assertIn("function discardFailedAvatar(image)", app_js)
        self.assertIn('matches("img[data-avatar-image]")', app_js)
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
        self.assertIn('const LEGACY_MATCH_ROUTES = { room: "room" }', app_js)
        self.assertIn('["room", "语音房"]', app_js)
        self.assertNotIn('{ id: "room", name: "语音房"', app_js)
        self.assertNotIn("room: pageRoom,", app_js)
        self.assertIn('name: "钱包与会员"', app_js)
        self.assertIn('name: "任务与奖励"', app_js)
        self.assertIn('const LEGACY_RELATION_ROUTES = { friends: "friends", visitors: "visitors" }', app_js)
        self.assertIn("function socialRouteHash", app_js)
        self.assertIn(".nav-children", app_css)
        self.assertIn(".relationship-tabs", app_css)
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
        self.assertIn("clearRelationshipCache", app_js)
        self.assertIn('data-action="social-tab"', app_js)
        self.assertNotIn("<strong>访客足迹</strong><span>谁看过我、我看过谁</span>", app_js)
        self.assertNotIn('class="message-shortcuts"', app_js)

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
        self.assertIn("avatarHtml(avatar)", conversation_card)
        self.assertIn("conversationProfilesByUid: new Map()", app_js)
        self.assertIn("function conversationAvatar(item)", app_js)
        self.assertIn("function preserveConversationAvatar(preferred, fallback)", app_js)
        self.assertIn("Boolean(conversation._avatar_from_fallback)", app_js)
        self.assertIn("_avatar_from_fallback: inherited", app_js)
        self.assertIn("function applyCachedConversationProfile(item)", app_js)
        self.assertIn("const avatar = currentAvatar || profileAvatar;", app_js)
        self.assertIn("async function hydrateConversationProfiles()", app_js)
        self.assertIn("function conversationProfileForPeer(rows, peer)", app_js)
        self.assertIn(".filter((item) => !conversationAvatar(item))", app_js)
        self.assertIn(".map(applyCachedConversationProfile);", app_js)
        self.assertIn("return profiles.length === 1 && idless.length === 1 ? idless[0] : null;", app_js)
        self.assertNotIn("profiles[0] ||", app_js)
        self.assertIn("function timUserProfileRows(result)", app_js)
        self.assertIn("function rememberTimConversationProfiles(rows)", app_js)
        self.assertIn("S.chat.getUserProfile({ userIDList })", app_js)
        self.assertIn("/api/profile/users?uids=", app_js)
        self.assertIn("conversationProfileFetchedAt: new Map()", app_js)
        self.assertIn("CONVERSATION_PROFILE_TTL_MS", app_js)
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
        unread_recalculation = app_js.split("function recalculateUnreadTotal", 1)[1].split(
            "function markConversationRead", 1
        )[0]

        self.assertEqual(app_js.count("void startMessageServices();"), 2)
        self.assertIn("function scheduleAuthenticatedServices", app_js)
        self.assertIn("scheduleAuthenticatedServices(1000);", app_js)
        self.assertIn("startMessageSyncTimer();", start_services)
        self.assertIn("loadArchivedConversationSummary()", start_services)
        self.assertIn("runMessageSyncCycle({ force: true })", start_services)
        self.assertIn("refreshConversationSummary()", summary_sync)
        self.assertIn("ensureTimConnected({ background: true })", background_sync)
        self.assertIn("return Promise.allSettled(tasks);", background_sync)
        self.assertNotIn('S.route === "msg" && !S.imConnected', background_sync)
        self.assertIn("S.conversationNextRefreshAt = Date.now() + CONVERSATION_REFRESH_ERROR_MS", summary_refresh)
        self.assertIn("applyConversationSummaries(itemsOf(data), { broadcast: true })", summary_refresh)
        self.assertIn("/api/archive/conversations?limit=100", app_js)
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
        self.assertIn("width: fit-content", app_css)
        self.assertIn("conversation-read-state", app_css)
        self.assertIn("presence-badge", app_css)

    def test_message_composer_supports_ctrl_enter_to_send(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('aria-keyshortcuts="Control+Enter"', app_js)
        self.assertIn('event.key !== "Enter"', app_js)
        self.assertIn("!event.ctrlKey", app_js)
        self.assertIn("event.shiftKey", app_js)
        self.assertIn('event.target.closest("#im-text")', app_js)
        self.assertIn("form.requestSubmit(submitter)", app_js)

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

    def test_message_page_uses_clean_chat_surface_and_topbar_read_action(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "bbw_web" / "static" / "app.js").read_text(encoding="utf-8")
        app_css = (root / "bbw_web" / "static" / "app.css").read_text(encoding="utf-8")
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")
        page_messages = app_js.split("async function pageMessages", 1)[1].split("async function pageMatching", 1)[0]

        self.assertIn('id="topbar-actions"', index_html)
        self.assertIn('id="mark-all-read-top" data-action="mark-all-read"', index_html)
        self.assertIn("function syncTopbarActions", app_js)
        self.assertIn('S.route !== "msg"', app_js)
        self.assertNotIn("message-toolbar", page_messages)
        self.assertNotIn('id="im-conn-status"', page_messages)
        self.assertIn(".message-page > .conversation-layout", app_css)
        self.assertIn("--message-content-max: 1800px", app_css)
        self.assertIn('document.body.classList.toggle("message-route-active"', app_js)
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

        self.assertIn("img-src 'self' data: blob: https:", server_py)
        self.assertIn("media-src 'self' data: blob: https:", server_py)

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
        self.assertIn("MESSAGE_PEER_SYNC_FALLBACK_MS = 8 * 1000", app_js)
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
            "@media (max-height: 560px) and (max-width: 960px) and (any-pointer: coarse)",
            1,
        )[1].split("html.keyboard-visible", 1)[0]

        self.assertIn("window.visualViewport", app_js)
        self.assertIn('setProperty("--app-viewport-height"', app_js)
        self.assertIn('window.visualViewport?.addEventListener("resize"', app_js)
        self.assertIn('window.visualViewport?.addEventListener("scroll"', app_js)
        self.assertIn("closeChatComposerPanelForKeyboard()", app_js)
        self.assertIn("selectionStart", app_js)
        self.assertIn("selectionEnd", app_js)
        self.assertNotIn('refreshChatComposerKeepingText({ focus: panel === "emoji" })', app_js)
        self.assertIn("(max-height: 560px)", app_css)
        self.assertIn("(max-width: 960px)", app_css)
        self.assertIn("(any-pointer: coarse)", app_css)
        self.assertIn(".conversation-layout", mobile_media)
        self.assertIn("grid-template-columns: 1fr", mobile_media)
        self.assertIn(".conversation-layout.has-active .conversation-list-pane", mobile_media)
        self.assertIn(".conversation-layout.has-active .chat-pane", mobile_media)
        self.assertIn("max-height: calc(100% - 84px)", short_media)
        self.assertIn("--safe-left: env(safe-area-inset-left", app_css)
        self.assertIn("--safe-right: env(safe-area-inset-right", app_css)
        tablet_layout = app_css.split("@media (max-width: 960px)", 1)[1].split("@media (min-width: 961px)", 1)[0]
        self.assertIn(".page-root.message-route", tablet_layout)
        self.assertIn("padding-bottom: calc(var(--bottom-h) + var(--safe-bottom) + 1.2rem)", tablet_layout)
        desktop_layout = app_css.split("@media (min-width: 961px)", 1)[1].split("@media (max-width: 640px)", 1)[0]
        self.assertIn("--side-w: clamp(196px, 18vw, 254px)", desktop_layout)
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

    def test_static_asset_cache_versions_match_mobile_media_release(self) -> None:
        root = Path(__file__).resolve().parents[1]
        index_html = (root / "bbw_web" / "static" / "index.html").read_text(encoding="utf-8")

        css_version = index_html.split('/static/app.css?v=', 1)[1].split('"', 1)[0]
        js_version = index_html.split('/static/app.js?v=', 1)[1].split('"', 1)[0]
        self.assertEqual(css_version, js_version)
        self.assertIn("mobile-media-retry-secure-viewport", css_version)


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
