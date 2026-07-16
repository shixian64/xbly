from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbw_protocol.adapters.pay import PayAdapter  # noqa: E402
from bbw_protocol.cli import build_parser  # noqa: E402
from bbw_protocol.client import ApiResult, _parse_result  # noqa: E402
from bbw_protocol.modules.im import ImAPI  # noqa: E402
from bbw_protocol.modules.social import SocialAPI  # noqa: E402
from bbw_web import bff_server as BFF  # noqa: E402
from bbw_web.normalize import (  # noqa: E402
    normalize_bottles,
    normalize_conversations,
    normalize_friend_applications,
    normalize_friends,
    normalize_messages,
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


class NormalizerContractTests(unittest.TestCase):
    def test_unavailable_task_is_never_receivable(self) -> None:
        blocked = normalize_task(
            {"id": "1", "available": "不可领取", "progress": 3, "num": 3}
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
        self.assertEqual(user["friend_remark"], "同学")
        self.assertEqual(user["friend_tag"], "熟人")
        self.assertEqual(user["letters"], "N")
        self.assertEqual(user["letter"], "N")
        self.assertEqual(user["apply_id"], "apply-1")
        self.assertEqual(user["relation_id"], "relation-1")
        self.assertTrue(user["is_friend"])

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
                    "uid": "726285",
                    "nickname": "当前用户",
                    "friendid": "9",
                    "friendnickname": "申请人",
                    "friendportrait": "images/friend.jpg",
                }
            ],
            current_uid="726285",
        )[0]
        self.assertEqual(application["id"], "9")
        self.assertEqual(application["nickname"], "申请人")
        self.assertEqual(application["apply_id"], "apply-1")

        friend = normalize_friends(
            [
                {
                    "id": "relation-1",
                    "uid": "726285",
                    "friendid": "9",
                    "friendnickname": "好友",
                }
            ],
            current_uid="726285",
        )[0]
        self.assertEqual(friend["id"], "9")
        self.assertEqual(friend["nickname"], "好友")
        self.assertEqual(friend["relation_id"], "relation-1")

        relations = normalize_social_users(
            [
                {
                    "id": "follow-row",
                    "uid": "726285",
                    "nickname": "当前用户",
                    "yourid": "10",
                    "yournickname": "关注对象",
                    "yourportrait": "images/follow.jpg",
                }
            ],
            current_uid="726285",
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
                    "userInfoList": {"id": "9", "nickname": "N", "portrait": "n.jpg"},
                }
            ]
        )[0]
        self.assertEqual(item["peer_id"], "9")
        self.assertEqual(item["nickname"], "N")
        self.assertEqual(item["last_message"], "你好")
        self.assertEqual(item["timestamp"], "1710000000")
        self.assertEqual(item["msg_uid"], "m1")

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

        message = normalize_messages(
            {
                "messageList": [
                    {
                        "msgUID": "m2",
                        "fromUserId": "42",
                        "toUserId": "9",
                        "msgTimestamp": "1710000001",
                        "payload": {"text": "历史消息"},
                    }
                ]
            }
        )[0]
        self.assertEqual(message["id"], "m2")
        self.assertEqual(message["text"], "历史消息")


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

    def test_history_conversation_uses_page_parameter(self) -> None:
        client = self.FakeClient()
        ImAPI(client).history_conversations("3")
        self.assertEqual(client.calls, [("getHistoryConversation", {"page": "3"})])

    def test_history_messages_uses_apk_message_detail_url(self) -> None:
        client = self.FakeClient()
        ImAPI(client).history_messages("9")
        url, body, kwargs = client.calls[0]
        self.assertIn("i=888&c=entry&do=Message_detail&m=socialchat&yourid=9", url)
        self.assertIsNone(body)
        self.assertEqual(kwargs, {"method": "GET"})


class FakeApp:
    def __init__(self, result: ApiResult):
        self.result = result
        self.calls = []
        self.session = SimpleNamespace(uid="42")
        self.economy = SimpleNamespace(
            buy_coin_wechat=lambda coin_id: result,
            buy_coin_alipay=lambda coin_id: result,
        )

    def call(self, action: str, **body):
        self.calls.append((action, body))
        return self.result


class PaymentContractTests(unittest.TestCase):
    def test_pay_vip_cli_accepts_the_required_product_id(self) -> None:
        args = build_parser().parse_args(["pay-vip", "--vipid", "5"])
        self.assertEqual(args.vipid, "5")

    def test_vip_requires_and_passes_vipid(self) -> None:
        valid = ApiResult(
            True,
            200,
            "",
            data={"data": {"prepayid": "prepay", "sign": "signed"}},
            code="200",
        )
        app = FakeApp(valid)
        result = PayAdapter(app).prepare_vip_wechat("vip", vipid="5")
        self.assertTrue(result.ok)
        self.assertEqual(app.calls[0][0], "Payunifiedorder2vipXBXX")
        self.assertEqual(app.calls[0][1]["vipid"], "5")
        self.assertEqual(result.order_params["prepayId"], "prepay")

        missing_app = FakeApp(valid)
        missing = PayAdapter(missing_app).prepare_vip_wechat("vip")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.code, "MISSING_VIPID")
        self.assertEqual(missing_app.calls, [])

    def test_order_success_requires_channel_fields(self) -> None:
        incomplete = ApiResult(True, 200, "{}", data={}, code="200")
        app = FakeApp(incomplete)
        wechat = PayAdapter(app).prepare_vip_wechat("svip", vipid="7")
        self.assertFalse(wechat.ok)
        self.assertEqual(wechat.code, "INVALID_ORDER_PAYLOAD")

        coin = PayAdapter(app).prepare_coin_wechat("1")
        self.assertFalse(coin.ok)
        self.assertEqual(coin.code, "INVALID_ORDER_PAYLOAD")

    def test_alipay_vip_requires_order_string(self) -> None:
        valid = ApiResult(
            True,
            200,
            "",
            data={"result": {"orderString": "app_id=1&sign=abc"}},
            code="200",
        )
        app = FakeApp(valid)
        result = PayAdapter(app).prepare_vip_alipay("vip", vipid="8")
        self.assertTrue(result.ok)
        self.assertEqual(app.calls[0][1]["vipid"], "8")
        self.assertEqual(result.order_params["orderString"], "app_id=1&sign=abc")


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
        payload = {"channel": "wechat", "coin_id": "1", "meta": {"a": 1, "b": 2}}
        self.assertTrue(BFF._mutation_allowed("sid-a", "/api/pay/coin", payload, 60.0))
        # JSON object ordering does not change the complete parsed payload.
        reordered = {"meta": {"b": 2, "a": 1}, "coin_id": "1", "channel": "wechat"}
        self.assertFalse(BFF._mutation_allowed("sid-a", "/api/pay/coin", reordered, 60.0))
        self.assertTrue(BFF._mutation_allowed("sid-b", "/api/pay/coin", payload, 60.0))
        self.assertTrue(BFF._mutation_allowed("sid-a", "/api/pay/vip", payload, 60.0))
        changed = {**payload, "coin_id": "2"}
        self.assertTrue(BFF._mutation_allowed("sid-a", "/api/pay/coin", changed, 60.0))

    def test_authenticated_financial_post_returns_duplicate_contract(self) -> None:
        calls = []

        def buy_match_card(card_id: str):
            calls.append(card_id)
            return SimpleNamespace(to_dict=lambda: {"ok": True, "card_id": card_id})

        web_user = SimpleNamespace(
            app=SimpleNamespace(),
            native=SimpleNamespace(pay=SimpleNamespace(buy_match_card=buy_match_card)),
        )

        class Harness:
            path = "/api/pay/card"

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
            first = Harness("sid-a", {"card_id": "1"})
            BFF.Handler.do_POST(first)
            self.assertTrue(first.authenticated)
            self.assertEqual(first.response[0], 200)
            self.assertEqual(calls, ["1"])

            duplicate = Harness("sid-a", {"card_id": "1"})
            BFF.Handler.do_POST(duplicate)
            self.assertTrue(duplicate.authenticated)
            self.assertEqual(duplicate.response[0], 409)
            self.assertEqual(duplicate.response[1]["code"], "DUPLICATE_REQUEST")
            self.assertIn("重复提交", duplicate.response[1]["error"])
            self.assertEqual(calls, ["1"])

            changed_payload = Harness("sid-a", {"card_id": "2"})
            BFF.Handler.do_POST(changed_payload)
            self.assertEqual(changed_payload.response[0], 200)

            other_sid = Harness("sid-b", {"card_id": "1"})
            BFF.Handler.do_POST(other_sid)
            self.assertEqual(other_sid.response[0], 200)
            self.assertEqual(calls, ["1", "2", "1"])
        finally:
            BFF.STORE = old_store


if __name__ == "__main__":
    unittest.main()
