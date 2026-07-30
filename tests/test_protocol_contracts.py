from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_protocol.app import BeibeiwuApp  # noqa: E402
from bbw_protocol.cli import build_parser  # noqa: E402
from bbw_protocol.client import ApiResult, ProtocolClient, _parse_result  # noqa: E402
from bbw_protocol.modules.auth import AuthAPI  # noqa: E402
from bbw_protocol.modules.im import ImAPI  # noqa: E402
from bbw_protocol.modules.match import MatchAPI  # noqa: E402
from bbw_protocol.modules.profile import ProfileAPI  # noqa: E402
from bbw_protocol.modules.social import SocialAPI  # noqa: E402
from bbw_protocol.session import Session  # noqa: E402
from bbw_web import bff_server as BFF  # noqa: E402
from bbw_web.dependency_health import RequestDeadline  # noqa: E402
from bbw_web.normalize import (  # noqa: E402
    normalize_bottles,
    normalize_conversations,
    normalize_friend_applications,
    normalize_friends,
    normalize_messages,
    normalize_match_filters,
    normalize_rooms,
    normalize_slides,
    normalize_social_users,
    normalize_songs,
    normalize_stickers,
    normalize_task,
    normalize_topics,
    normalize_users,
    normalize_value,
)


class ParseResultContractTests(unittest.TestCase):
    def test_false_null_and_empty_are_not_business_success(self) -> None:
        empty = _parse_result(200, "", {})
        self.assertFalse(empty.ok)
        self.assertEqual(empty.code, "EMPTY_RESPONSE")
        self.assertEqual(empty.kind, "empty")

        false_value = _parse_result(200, "false", {})
        self.assertFalse(false_value.ok)
        self.assertIs(false_value.data, False)
        self.assertEqual(false_value.code, "FALSE_RESPONSE")

        plain_no = _parse_result(200, "NO", {})
        self.assertFalse(plain_no.ok)
        self.assertEqual(plain_no.code, "FALSE_RESPONSE")

        null_value = _parse_result(200, "null", {})
        self.assertFalse(null_value.ok)
        self.assertIsNone(null_value.data)
        self.assertEqual(null_value.code, "NULL_RESPONSE")

    def test_valid_json_scalars_and_lists_are_preserved(self) -> None:
        zero = _parse_result(200, "0", {})
        self.assertTrue(zero.ok)
        self.assertEqual(zero.data, 0)

        referral = _parse_result(200, "20978", {})
        self.assertTrue(referral.ok)
        self.assertEqual(referral.data, 20978)

        items = _parse_result(200, "[]", {})
        self.assertTrue(items.ok)
        self.assertEqual(items.data, [])

        true_value = _parse_result(200, "true", {})
        self.assertTrue(true_value.ok)
        self.assertIs(true_value.data, True)

    def test_http_error_cannot_be_promoted_by_scalar_payload(self) -> None:
        result = _parse_result(500, "[]", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.data, [])


class InteractiveProtocolBudgetTests(unittest.TestCase):
    def test_reauthentication_receives_the_original_timeout_and_deadline(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.calls = []

            def request(self, *_args, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return SimpleNamespace(
                        status_code=401,
                        content='{"message":"请先登录"}'.encode("utf-8"),
                        headers={},
                    )
                return SimpleNamespace(
                    status_code=200,
                    content=b"true",
                    headers={},
                )

            def close(self) -> None:
                return None

        client = ProtocolClient(Session(uid="42", token="expired"))
        client._http.close()
        fake_http = FakeHttp()
        client._http = fake_http
        self.addCleanup(client.close)
        deadline = RequestDeadline(6)
        observed = {}

        def reauthenticate(*, timeout=None, deadline=None):
            observed.update(timeout=timeout, deadline=deadline)
            client.session.token = "refreshed"
            return True

        client.reauth_callback = reauthenticate
        result = client.request(
            "https://example.invalid/provider",
            timeout=3,
            deadline=deadline,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(fake_http.calls), 2)
        self.assertEqual(observed["timeout"], 3)
        self.assertIs(observed["deadline"], deadline)

    def test_password_reauthentication_forwards_call_level_budget(self) -> None:
        observed = {}
        deadline = RequestDeadline(6)
        client = SimpleNamespace(
            session=Session(uid="42", token="expired"),
            url=lambda action: f"https://example.invalid/{action}",
            request=lambda url, body, **kwargs: observed.update(
                url=url,
                body=body,
                kwargs=kwargs,
            )
            or ApiResult(False, 503, "", data=None),
        )

        AuthAPI(client).login_password(
            "19100000000",
            "secret",
            timeout=3,
            deadline=deadline,
        )

        self.assertEqual(observed["kwargs"]["timeout"], 3)
        self.assertIs(observed["kwargs"]["deadline"], deadline)


class BootstrapContractTests(unittest.TestCase):
    def test_product_bootstrap_can_skip_tencent_usersig_request(self) -> None:
        calls = []
        ok = ApiResult(True, 200, "true", data=True)
        app = BeibeiwuApp.__new__(BeibeiwuApp)
        app.content = SimpleNamespace(
            is_show_ad=lambda: ok,
            gift_list=lambda: ok,
            recommend=lambda: ok,
            chat_censorship=lambda: ok,
        )
        app.misc = SimpleNamespace(update_online=lambda **_kwargs: ok)
        app.session = SimpleNamespace(logged_in=True)
        app.profile = SimpleNamespace(get_me=lambda: ok, etiquette=lambda: ok)
        app.im = SimpleNamespace(
            tencent_sign=lambda: calls.append("txim") or ApiResult(
                True, 200, "secret-usersig", data="secret-usersig"
            )
        )

        result = app.bootstrap(include_im=False)

        self.assertEqual(calls, [])
        self.assertNotIn("txim", result)


class NormalizerContractTests(unittest.TestCase):
    def test_task_receivability_prefers_completed_progress_without_reopening_claimed(self) -> None:
        completed_with_stale_status = normalize_task(
            {"id": "1", "available": "不可领取", "progress": 3, "num": 3}
        )
        self.assertIsNotNone(completed_with_stale_status)
        self.assertTrue(completed_with_stale_status["can_receive"])
        self.assertEqual(completed_with_stale_status["status_text"], "可领取")

        blocked = normalize_task(
            {"id": "5", "available": "不可领取", "progress": 2, "num": 3}
        )
        self.assertIsNotNone(blocked)
        self.assertFalse(blocked["can_receive"])

        ready = normalize_task(
            {"id": "2", "available": "可领取", "progress": 0, "num": 3}
        )
        self.assertIsNotNone(ready)
        self.assertTrue(ready["can_receive"])

        claimed = normalize_task(
            {"id": "3", "available": "已领取", "progress": 3, "num": 3}
        )
        self.assertIsNotNone(claimed)
        self.assertTrue(claimed["is_claimed"])
        self.assertFalse(claimed["can_receive"])

        stale_claimed = normalize_task(
            {"id": "4", "available": "received", "progress": 0, "num": 30}
        )
        self.assertIsNotNone(stale_claimed)
        self.assertTrue(stale_claimed["is_claimed"])
        self.assertFalse(stale_claimed["can_receive"])

    def test_entity_lists_keep_domain_fields(self) -> None:
        slide = normalize_slides(
            {"slides": [{"id": "s1", "title": "活动", "image": "banner.jpg", "url": "/event"}]}
        )[0]
        self.assertEqual(slide["title"], "活动")
        self.assertEqual(slide["image"], "https://oss.banghua.xin/banner.jpg")
        self.assertEqual(slide["url"], "/event")
        self.assertNotIn("nickname", slide)

        topic = normalize_topics({"topics": [{"topic_id": "t1", "topic": "音乐", "postnum": "8"}]})[0]
        self.assertEqual(topic["name"], "音乐")
        self.assertEqual(topic["post_count"], 8)

        room = normalize_rooms(
            {"rooms": [{"roomId": "r1", "roomName": "晚安房", "channelName": "agora-1", "online": "6"}]}
        )[0]
        self.assertEqual(room["name"], "晚安房")
        self.assertEqual(room["channel"], "agora-1")
        self.assertEqual(room["online_count"], 6)

        roomkit_room = normalize_rooms(
            {
                "rooms": [
                    {
                        "roomId": "rk1",
                        "roomName": "原生语聊房",
                        "themePictureUrl": "https://example.invalid/theme.jpg",
                        "backgroundUrl": "https://example.invalid/background.jpg",
                        "createUser": {
                            "userId": "owner-1",
                            "userName": "房主",
                            "portrait": "https://example.invalid/owner.jpg",
                        },
                        "userTotal": 0,
                        "isPrivate": 1,
                        "stop": True,
                    }
                ]
            }
        )[0]
        self.assertEqual(roomkit_room["id"], "rk1")
        self.assertEqual(roomkit_room["cover"], "https://example.invalid/theme.jpg")
        self.assertEqual(roomkit_room["background_url"], "https://example.invalid/background.jpg")
        self.assertEqual(roomkit_room["owner_id"], "owner-1")
        self.assertEqual(roomkit_room["owner_name"], "房主")
        self.assertEqual(roomkit_room["owner_avatar"], "https://example.invalid/owner.jpg")
        self.assertEqual(roomkit_room["online_count"], 0)
        self.assertTrue(roomkit_room["is_private"])
        self.assertTrue(roomkit_room["is_stopped"])

        song = normalize_songs(
            {"songs": [{"musicId": "m1", "musicName": "晴天", "singer": "Jay", "playUrl": "song.mp3"}]}
        )[0]
        self.assertEqual(song["name"], "晴天")
        self.assertEqual(song["singer"], "Jay")
        self.assertEqual(song["url"], "song.mp3")

        bottle = normalize_bottles(
            {"bottles": [{"bottleId": "b1", "content": "hello", "uid": "9", "nickname": "N"}]}
        )[0]
        self.assertEqual(bottle["content"], "hello")
        self.assertEqual(bottle["user_id"], "9")

        apk_bottle = normalize_bottles(
            {
                "id": "b2",
                "uid1": "9",
                "uid2": "42",
                "leave_words_json": json.dumps(
                    [
                        {"uid": "9", "leave_word": "第一句留言"},
                        {"uid": "42", "leave_word": "一条回应"},
                    ],
                    ensure_ascii=False,
                ),
                "time": "1751677508",
                "pick_time": "1751677600",
                "picked_times": "3",
                "state": "正常",
            }
        )[0]
        self.assertEqual(apk_bottle["content"], "第一句留言")
        self.assertEqual(apk_bottle["user_id"], "9")
        self.assertEqual(apk_bottle["picker_id"], "42")
        self.assertEqual(apk_bottle["created_at"], "1751677508")
        self.assertEqual(apk_bottle["picked_at"], "1751677600")
        self.assertEqual(apk_bottle["picked_times"], 3)
        self.assertEqual(apk_bottle["reply_count"], 1)
        self.assertEqual(len(apk_bottle["leave_words"]), 2)

        sticker = normalize_stickers(
            {"stickers": [{"stickerId": "e1", "stickerName": "笑", "stickerUrl": "e.png", "isFavorite": "1"}]}
        )[0]
        self.assertEqual(sticker["image"], "https://oss.banghua.xin/e.png")
        self.assertTrue(sticker["favorite"])

    def test_generic_value_preserves_scalar_and_object(self) -> None:
        number = normalize_value(20978)
        self.assertEqual(number["value_type"], "number")
        self.assertEqual(number["value"], 20978)

        obj = normalize_value('{"token":"abc"}')
        self.assertEqual(obj["value_type"], "object")
        self.assertEqual(obj["value"]["token"], "abc")

    def test_match_filters_follow_apk_values_and_defaults(self) -> None:
        self.assertEqual(
            normalize_match_filters(
                {"userInfoList": {"match_gender": "女", "match_property": "Z"}}
            ),
            {"gender": "女", "property": "Z"},
        )
        self.assertEqual(
            normalize_match_filters({"match_gender": "未知", "match_property": "未知"}),
            {"gender": "不限", "property": "双"},
        )

    def test_social_user_fields_preserve_relationship_and_visit_metadata(self) -> None:
        user = normalize_users(
            [
                {
                    "id": "9",
                    "nickname": "N",
                    "portrait": "n.jpg",
                    "region": "上海",
                    "location": "2.4",
                    "online": "在线",
                    "time": "1710000000",
                    "customTime": "2024-03-10 00:00",
                    "friendsremark": "同学",
                    "friendstag": "熟人",
                    "letters": "N",
                    "apply_id": "apply-1",
                    "subid": "relation-1",
                    "isFriend": "1",
                }
            ]
        )[0]
        self.assertEqual(user["city"], "上海")
        self.assertEqual(user["distance"], "2.4")
        self.assertEqual(user["visit_time"], "1710000000")
        self.assertEqual(user["custom_time"], "2024-03-10 00:00")
        self.assertEqual(user["friend_remark"], "同学")
        self.assertEqual(user["friend_tag"], "熟人")
        self.assertEqual(user["letters"], "N")
        self.assertEqual(user["letter"], "N")
        self.assertEqual(user["apply_id"], "apply-1")
        self.assertEqual(user["relation_id"], "relation-1")
        self.assertTrue(user["is_friend"])

        visit_alias = normalize_users(
            [{"id": "10", "nickname": "V", "visitedAt": "1710000100"}]
        )[0]
        self.assertEqual(visit_alias["visit_time"], "1710000100")

        relation = normalize_users([{"id": "relation-row", "uid": "9", "nickname": "N"}])[0]
        self.assertEqual(relation["id"], "9")

        wrapped = normalize_users(
            {
                "userInfoList": [
                    {"uid": "10", "nickname": "F", "isFollow": "1", "isFan": "1"}
                ]
            }
        )[0]
        self.assertEqual(wrapped["id"], "10")
        self.assertTrue(wrapped["is_follower"])
        self.assertTrue(wrapped["is_fans"])

        application = normalize_friend_applications(
            [
                {
                    "id": "apply-1",
                    "uid": "10001",
                    "nickname": "当前用户",
                    "friendid": "9",
                    "friendnickname": "申请人",
                    "friendportrait": "images/friend.jpg",
                    "agree": "0",
                }
            ],
            current_uid="10001",
        )[0]
        self.assertEqual(application["id"], "9")
        self.assertEqual(application["nickname"], "申请人")
        self.assertEqual(application["apply_id"], "apply-1")
        self.assertEqual(application["request_status"], "pending")
        self.assertTrue(application["request_status_known"])
        self.assertTrue(application["is_pending"])
        self.assertFalse(application["is_accepted"])
        self.assertEqual(application["direction"], "incoming")
        self.assertEqual(application["status"], "pending")
        self.assertEqual(application["status_label"], "等待你处理")
        self.assertTrue(application["can_accept"])

        outgoing_application = normalize_friend_applications(
            [
                {
                    "id": "apply-2",
                    "myid": "9",
                    "yourid": "10001",
                    "agree": "0",
                    "yourleavewords": "你好",
                    "time": "1710000000",
                    "userInfoList": {"id": "9", "nickname": "申请目标"},
                }
            ],
            current_uid="10001",
        )[0]
        self.assertEqual(outgoing_application["id"], "9")
        self.assertEqual(outgoing_application["direction"], "outgoing")
        self.assertEqual(outgoing_application["status"], "pending")
        self.assertEqual(outgoing_application["status_label"], "等待对方同意")
        self.assertFalse(outgoing_application["can_accept"])
        self.assertEqual(outgoing_application["leave_words"], "你好")

        accepted_application = normalize_friend_applications(
            [
                {
                    "id": "apply-3",
                    "myid": "10001",
                    "yourid": "9",
                    "agree": "1",
                    "userInfoList": {"id": "9", "nickname": "已添加用户"},
                }
            ],
            current_uid="10001",
        )[0]
        self.assertEqual(accepted_application["request_status"], "accepted")
        self.assertFalse(accepted_application["is_pending"])
        self.assertTrue(accepted_application["is_accepted"])
        self.assertEqual(accepted_application["direction"], "incoming")
        self.assertEqual(accepted_application["status"], "accepted")
        self.assertEqual(accepted_application["status_label"], "已成为好友")
        self.assertFalse(accepted_application["can_accept"])

        friend = normalize_friends(
            [
                {
                    "id": "relation-1",
                    "uid": "10001",
                    "friendid": "9",
                    "friendnickname": "好友",
                    "friendonline": "Online",
                }
            ],
            current_uid="10001",
        )[0]
        self.assertEqual(friend["id"], "9")
        self.assertEqual(friend["nickname"], "好友")
        self.assertEqual(friend["relation_id"], "relation-1")
        self.assertEqual(friend["online"], "Online")

        relations = normalize_social_users(
            [
                {
                    "id": "follow-row",
                    "uid": "10001",
                    "nickname": "当前用户",
                    "yourid": "10",
                    "yournickname": "关注对象",
                    "yourportrait": "images/follow.jpg",
                }
            ],
            current_uid="10001",
        )
        self.assertEqual(relations[0]["id"], "10")
        self.assertEqual(relations[0]["nickname"], "关注对象")
        self.assertEqual(
            relations[0]["avatar"],
            "https://oss.banghua.xin/images/follow.jpg",
        )

    def test_history_conversation_keeps_receive_message_fields(self) -> None:
        item = normalize_conversations(
            [
                {
                    "id": "r1",
                    "conversation_user": "9",
                    "fromUserId": "9",
                    "toUserId": "42",
                    "objectName": "TIMTextElem",
                    "content": "你好",
                    "channelType": "C2C",
                    "msgTimestamp": "1710000000",
                    "msgUID": "m1",
                    "userInfoList": {
                        "id": "9",
                        "nickname": "N",
                        "portrait": "n.jpg",
                        "onlineStatus": "Online",
                    },
                }
            ]
        )[0]
        self.assertEqual(item["peer_id"], "9")
        self.assertEqual(item["nickname"], "N")
        self.assertEqual(item["last_message"], "你好")
        self.assertEqual(item["timestamp"], "1710000000")
        self.assertEqual(item["preview_timestamp"], "")
        self.assertTrue(item["preview_timestamp_inferred"])
        self.assertFalse(item["preview_authoritative"])
        self.assertEqual(item["msg_uid"], "m1")
        self.assertEqual(item["online"], "Online")

        nested_self = normalize_conversations(
            [
                {
                    "conversation_user": "9",
                    "fromUserId": "9",
                    "toUserId": "42",
                    "userInfoList": {"id": "42", "nickname": "Me"},
                }
            ]
        )[0]
        self.assertEqual(nested_self["peer_id"], "9")
        self.assertEqual(nested_self["nickname"], "9")
        self.assertIsNone(nested_self["user"])

        peer_portrait = normalize_conversations(
            [
                {
                    "conversation_user": "9",
                    "conversationNickname": "Peer",
                    "conversationPortrait": "images/users/peer.jpg",
                    "userInfoList": {"id": "42", "nickname": "Me"},
                }
            ]
        )[0]
        self.assertEqual(peer_portrait["nickname"], "Peer")
        self.assertEqual(
            peer_portrait["avatar"],
            "https://oss.banghua.xin/images/users/peer.jpg",
        )

        message = normalize_messages(
            {
                "messageList": [
                    {
                        "msgUID": "m2",
                        "fromUserId": "42",
                        "toUserId": "9",
                        "msgTimestamp": "1710000001",
                        "MsgSeq": "7",
                        "payload": {"text": "历史消息"},
                        "isPeerRead": True,
                        "readTime": "1710000042",
                    }
                ]
            }
        )[0]
        self.assertEqual(message["id"], "m2")
        self.assertEqual(message["text"], "历史消息")
        self.assertEqual(message["sequence"], "7")
        self.assertTrue(message["is_peer_read"])
        self.assertEqual(message["read_state"], "read")
        self.assertEqual(message["read_time"], "1710000042")


class SocialAndImRoutingContractTests(unittest.TestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.session = SimpleNamespace(uid="42", nickname="Me", portrait="me.jpg")
            self.calls = []

        def call(self, action, params=None, **kwargs):
            body = dict(params or {})
            body.update(kwargs)
            self.calls.append((action, body))
            return SimpleNamespace(ok=True)

        def url(self, action, **kwargs):
            return (action, kwargs)

        def request(self, url, body=None, **kwargs):
            self.calls.append((url, body, kwargs))
            return SimpleNamespace(ok=True)

        def call_url(self, url, params=None, **kwargs):
            body = dict(params or {})
            body.update(kwargs)
            self.calls.append((url, body))
            return SimpleNamespace(ok=True)

        def call_redis(self, action, params=None, **kwargs):
            body = dict(params or {})
            body.update(kwargs)
            self.calls.append((action, body))
            return SimpleNamespace(ok=True)

    def test_friend_and_visit_actions_match_apk_v154(self) -> None:
        client = self.FakeClient()
        api = SocialAPI(client)
        api.follow_list()
        api.friends()
        api.viewed_me("0")
        api.i_viewed("2")
        api.record_profile_view("9")
        self.assertEqual(client.calls[0], ("getFollowList", {"id": "42"}))
        self.assertEqual(client.calls[1], ("getAddFriend", {"uid": "42", "type": "好友"}))
        self.assertEqual(
            client.calls[2],
            ("ISawAndSawMe", {"pageindex": "0", "type": "谁看过我"}),
        )
        self.assertEqual(
            client.calls[3],
            ("ISawAndSawMe", {"pageindex": "2", "type": "我看过谁"}),
        )
        # xbly v154 is authoritative for id direction: current viewer -> target.
        # Display fields are retained only as compatibility extras for older code.
        self.assertEqual(
            client.calls[4],
            (
                "addsawme",
                {
                    "myid": "42",
                    "yourid": "9",
                    "yournickname": "Me",
                    "yourportrait": "me.jpg",
                },
            ),
        )

    def test_follow_and_fans_lists_use_case_sensitive_pageindex(self) -> None:
        client = self.FakeClient()
        api = SocialAPI(client)

        api.follow_users(page="2")
        api.fans_users("9", page="3")

        self.assertEqual(
            client.calls,
            [
                ("getFollowUser", {"id": "42", "pageindex": "2"}),
                ("getFansUser", {"id": "9", "pageindex": "3"}),
            ],
        )

    def test_history_conversation_uses_page_parameter(self) -> None:
        client = self.FakeClient()
        ImAPI(client).history_conversations("3")
        self.assertEqual(client.calls, [("getHistoryConversation", {"page": "3"})])

    def test_other_user_profile_uses_apk_get_user_attributes_zero_action(self) -> None:
        client = self.FakeClient()
        api = ProfileAPI(client)

        api.get_user("9")
        api.get_user("42")

        self.assertEqual(client.calls[0][0], "getUserAttributes0")
        self.assertEqual(client.calls[0][1]["userId"], "9")
        self.assertEqual(client.calls[1][0], "getUserAttributes")
        self.assertEqual(client.calls[1][1]["userId"], "42")

    def test_history_messages_uses_apk_message_detail_url(self) -> None:
        client = self.FakeClient()
        ImAPI(client).history_messages("9")
        url, body, kwargs = client.calls[0]
        self.assertIn("i=888&c=entry&do=Message_detail&m=socialchat&yourid=9", url)
        self.assertIsNone(body)
        self.assertEqual(kwargs, {"method": "GET"})

    def test_match_filter_uses_reset_match_id_and_value(self) -> None:
        client = self.FakeClient()
        MatchAPI(client).set_filter("女")
        self.assertEqual(client.calls, [("resetMatch", {"id": "42", "value": "女"})])

    def test_rong_register_uses_apk_case_sensitive_user_id_field(self) -> None:
        client = self.FakeClient()
        ImAPI(client).rong_register()
        _url, body = client.calls[0]
        self.assertEqual(body["userID"], "42")
        self.assertNotIn("userId", body)

    def test_voice_match_uses_apk_redis_action_and_literal_type(self) -> None:
        client = self.FakeClient()
        api = MatchAPI(client)
        api.start_voice()
        api.cancel_voice()
        self.assertEqual(
            client.calls,
            [
                ("xiaobeiMatchNew", {"id": "42", "type": "语音"}),
                ("removeXiaobeiMatch", {"id": "42", "type": "语音"}),
            ],
        )

    def test_voice_match_response_normalizes_wait_false_and_user(self) -> None:
        waiting = ApiResult(True, 200, "wait", data="wait", kind="text")
        insufficient = ApiResult(False, 200, "false", data="false", kind="text")
        matched = ApiResult(True, 200, '{"id":"9","nickname":"Peer"}', data={"id": "9", "nickname": "Peer"})
        self.assertEqual(MatchAPI.normalize_voice_result(waiting)["outcome"], "waiting")
        self.assertEqual(MatchAPI.normalize_voice_result(insufficient)["outcome"], "insufficient")
        normalized = MatchAPI.normalize_voice_result(matched)
        self.assertEqual(normalized["outcome"], "matched")
        self.assertEqual(normalized["target"]["id"], "9")

    def test_bottle_creation_and_rethrow_keep_distinct_apk_actions(self) -> None:
        client = self.FakeClient()
        api = MatchAPI(client)
        api.bottle_leave_word(leave_word="第一句留言")
        api.throw_bottle(id="b1")
        self.assertEqual(
            client.calls,
            [
                ("AddDraftBottleLeaveWord", {"leave_word": "第一句留言"}),
                ("ThrowADriftBottle", {"id": "b1"}),
            ],
        )


class CommerceRemovalContractTests(unittest.TestCase):
    def test_dedicated_purchase_and_recharge_surfaces_are_removed(self) -> None:
        parser = build_parser()
        commands = parser._subparsers._group_actions[0].choices
        for command in ("gifts", "svip-try", "exchange-vip", "pay-coin", "pay-vip", "pay-card"):
            self.assertNotIn(command, commands)

        self.assertFalse((ROOT / "bbw_protocol" / "adapters" / "pay.py").exists())
        bundle = (ROOT / "bbw_protocol" / "adapters" / "bundle.py").read_text(encoding="utf-8")
        economy = (ROOT / "bbw_protocol" / "modules" / "economy.py").read_text(encoding="utf-8")
        self.assertNotIn("self.pay", bundle)
        for marker in ("buy_coin_", "money_exchange_vip", "svip_try", "send_gift", "send_vip", "vip_level_order"):
            self.assertNotIn(marker, economy)

    def test_server_membership_fields_remain_in_session_contract(self) -> None:
        session = Session()
        session.update_from_user({"vip": "1700000000", "svip": "1800000000"})
        self.assertEqual(session.vip, "1700000000")
        self.assertEqual(session.svip, "1800000000")
        self.assertEqual(session.summary()["vip"], "1700000000")
        self.assertEqual(session.summary()["svip"], "1800000000")

    def test_low_level_client_blocks_commerce_actions_before_network(self) -> None:
        calls = []
        client = ProtocolClient(Session())
        client._http = SimpleNamespace(request=lambda *args, **kwargs: calls.append((args, kwargs)))

        result = client.call("buyCoinWechatXBXX", coinId="1")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 403)
        self.assertEqual(result.code, "COMMERCE_DISABLED")
        self.assertEqual(calls, [])


class CatalogMetadataContractTests(unittest.TestCase):
    def test_v154_catalog_counts_and_dynamic_login_actions(self) -> None:
        catalog = json.loads(
            (ROOT / "docs" / "api_catalog.json").read_text(encoding="utf-8")
        )
        dos = set(catalog["dos"])
        shorts = set(catalog["shorts"])
        actions = dos | shorts
        urls = set(catalog["full_urls"])
        deprecated = set(catalog["deprecated_actions"]["actions"])
        dynamic = {"signin0", "SigninOneKeyLogin1"}

        self.assertEqual(len(dos), catalog["counts"]["do"])
        self.assertEqual(len(shorts), catalog["counts"]["short_startHttp"])
        self.assertEqual(len(actions), catalog["counts"]["all_actions"])
        self.assertEqual(len(urls), catalog["counts"]["full_urls"])
        self.assertEqual(
            len(actions - deprecated), catalog["counts"]["active_in_v154"]
        )
        self.assertEqual(set(catalog["dynamic_do_actions"]), dynamic)
        self.assertTrue(dynamic <= actions)
        self.assertTrue(dynamic <= set(catalog["categories"]["auth"]))
        for action in dynamic:
            self.assertTrue(any(f"do={action}" in url for url in urls))


class MutationGuardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        with BFF.MUTATION_LOCK:
            BFF.RECENT_MUTATIONS.clear()

    def tearDown(self) -> None:
        with BFF.MUTATION_LOCK:
            BFF.RECENT_MUTATIONS.clear()

    def test_key_is_sid_path_and_complete_payload(self) -> None:
        payload = {"alipay": "a@b.com", "name": "测试", "amount": "1", "meta": {"a": 1, "b": 2}}
        self.assertTrue(BFF._mutation_allowed("sid-a", "/api/wallet/withdraw", payload, 60.0))
        # JSON object ordering does not change the complete parsed payload.
        reordered = {"meta": {"b": 2, "a": 1}, "amount": "1", "name": "测试", "alipay": "a@b.com"}
        self.assertFalse(BFF._mutation_allowed("sid-a", "/api/wallet/withdraw", reordered, 60.0))
        self.assertTrue(BFF._mutation_allowed("sid-b", "/api/wallet/withdraw", payload, 60.0))
        changed = {**payload, "amount": "2"}
        self.assertTrue(BFF._mutation_allowed("sid-a", "/api/wallet/withdraw", changed, 60.0))

    def test_authenticated_financial_post_returns_duplicate_contract(self) -> None:
        calls = []

        def withdraw(alipay: str, name: str, amount: str, authid: str):
            calls.append((alipay, name, amount, authid))
            return ApiResult(True, 200, "true", data=True)

        web_user = SimpleNamespace(
            app=SimpleNamespace(
                economy=SimpleNamespace(withdraw=withdraw),
                session=SimpleNamespace(uid="42"),
            ),
        )

        class Harness:
            path = "/api/wallet/withdraw"

            def __init__(self, sid: str, payload: dict):
                self._sid = sid
                self._payload = payload
                self.response = None
                self.authenticated = False

            def _check_api_origin(self):
                return True

            def body(self):
                return self._payload

            def sid(self):
                return self._sid

            def user(self, sid):
                self.authenticated = True
                return web_user

            def ok(self, obj, status=200, **_kwargs):
                self.response = (status, obj)
                return self.response

        old_store = BFF.STORE
        BFF.STORE = SimpleNamespace()
        try:
            first = Harness("sid-a", {"alipay": "a@b.com", "name": "测试", "amount": "1"})
            BFF.Handler.do_POST(first)
            self.assertTrue(first.authenticated)
            self.assertEqual(first.response[0], 200)
            self.assertEqual(calls, [("a@b.com", "测试", "1", "42")])

            duplicate = Harness("sid-a", {"alipay": "a@b.com", "name": "测试", "amount": "1"})
            BFF.Handler.do_POST(duplicate)
            self.assertTrue(duplicate.authenticated)
            self.assertEqual(duplicate.response[0], 409)
            self.assertEqual(duplicate.response[1]["code"], "DUPLICATE_REQUEST")
            self.assertIn("重复提交", duplicate.response[1]["error"])
            self.assertEqual(calls, [("a@b.com", "测试", "1", "42")])

            changed_payload = Harness("sid-a", {"alipay": "a@b.com", "name": "测试", "amount": "2"})
            BFF.Handler.do_POST(changed_payload)
            self.assertEqual(changed_payload.response[0], 200)

            other_sid = Harness("sid-b", {"alipay": "a@b.com", "name": "测试", "amount": "1"})
            BFF.Handler.do_POST(other_sid)
            self.assertEqual(other_sid.response[0], 200)
            self.assertEqual(
                calls,
                [
                    ("a@b.com", "测试", "1", "42"),
                    ("a@b.com", "测试", "2", "42"),
                    ("a@b.com", "测试", "1", "42"),
                ],
            )
        finally:
            BFF.STORE = old_store


if __name__ == "__main__":
    unittest.main()
