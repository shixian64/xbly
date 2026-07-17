from __future__ import annotations

import unittest

from bbw_protocol.adapters.tim_rest import RestResult, TimRestClient
from bbw_protocol.modules.im import ImAPI
from bbw_web.normalize import normalize_messages, normalize_stickers


class MessageMediaNormalizerTests(unittest.TestCase):
    def test_tim_rest_image_and_web_audio_keep_media_payload(self) -> None:
        image = normalize_messages(
            {
                "MsgBody": [
                    {
                        "MsgType": "TIMImageElem",
                        "MsgContent": {
                            "UUID": "image-uuid",
                            "ImageFormat": 2,
                            "ImageInfoArray": [
                                {
                                    "Type": 2,
                                    "Size": 1024,
                                    "Width": 300,
                                    "Height": 200,
                                    "URL": "https://example.invalid/thumb.jpg",
                                },
                                {
                                    "Type": 0,
                                    "Size": 4096,
                                    "Width": 1200,
                                    "Height": 800,
                                    "URL": "https://example.invalid/original.jpg",
                                },
                                {
                                    "Type": 1,
                                    "Size": 2048,
                                    "Width": 900,
                                    "Height": 600,
                                    "URL": "https://example.invalid/large.jpg",
                                },
                            ],
                        },
                    }
                ],
                "From_Account": "9",
                "To_Account": "42",
                "MsgTimeStamp": 1710000000,
                "MsgKey": "image-message",
            }
        )[0]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["object_name"], "TIMImageElem")
        self.assertEqual(image["from"], "9")
        self.assertEqual(image["payload"]["url"], "https://example.invalid/original.jpg")
        self.assertEqual(image["payload"]["thumbnail"], "https://example.invalid/thumb.jpg")
        self.assertEqual(image["payload"]["thumbnail_url"], "https://example.invalid/thumb.jpg")
        self.assertEqual(image["payload"]["large_url"], "https://example.invalid/large.jpg")
        self.assertEqual(image["payload"]["original_url"], "https://example.invalid/original.jpg")
        self.assertEqual(image["payload"]["width"], 1200)
        self.assertEqual(image["payload"]["height"], 800)
        self.assertEqual(image["payload"]["size"], 4096)
        self.assertEqual(len(image["payload"]["images"]), 3)

        audio = normalize_messages(
            {
                "id": "audio-message",
                "type": "TIMSoundElem",
                "payload": {
                    "url": "https://example.invalid/voice.m4a",
                    "UUID": "voice-uuid",
                    "second": 7,
                    "size": 321,
                },
            }
        )[0]
        self.assertEqual(audio["type"], "audio")
        self.assertEqual(audio["payload"]["duration"], 7)
        self.assertEqual(audio["payload"]["uuid"], "voice-uuid")

    def test_tim_image_original_url_falls_back_to_large_before_thumbnail(self) -> None:
        image = normalize_messages(
            {
                "objectName": "TIMImageElem",
                "payload": {
                    "ImageInfoArray": [
                        {"Type": 2, "URL": "https://example.invalid/thumb-only.jpg"},
                        {"Type": 1, "URL": "https://example.invalid/large-only.jpg"},
                    ]
                },
            }
        )[0]

        self.assertEqual(image["payload"]["url"], "https://example.invalid/large-only.jpg")
        self.assertEqual(
            image["payload"]["original_url"], "https://example.invalid/large-only.jpg"
        )
        self.assertEqual(
            image["payload"]["thumbnail"], "https://example.invalid/thumb-only.jpg"
        )
        self.assertEqual(
            image["payload"]["thumbnail_url"], "https://example.invalid/thumb-only.jpg"
        )

    def test_video_file_face_and_flash_are_not_coerced_to_text(self) -> None:
        rows = normalize_messages(
            {
                "messageList": [
                    {
                        "id": "video",
                        "objectName": "TIMVideoFileElem",
                        "payload": {
                            "videoUrl": "https://example.invalid/video.mp4",
                            "thumbUrl": "https://example.invalid/video.jpg",
                            "videoUUID": "video-uuid",
                            "second": 15,
                        },
                    },
                    {
                        "id": "file",
                        "objectName": "TIMFileElem",
                        "payload": {
                            "url": "https://example.invalid/a.zip",
                            "UUID": "file-uuid",
                            "fileName": "a.zip",
                            "fileSize": 99,
                        },
                    },
                    {
                        "id": "face",
                        "objectName": "TIMFaceElem",
                        "payload": {
                            "index": 12,
                            "data": "https://example.invalid/face.webp",
                        },
                    },
                    {
                        "id": "flash",
                        "objectName": "TIMTextElem",
                        "content": "点击查看5秒闪图",
                        "cloudCustomData": "unique-photo-id",
                    },
                ]
            }
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["video"]["type"], "video")
        self.assertEqual(by_id["video"]["payload"]["thumbnail_url"], "https://example.invalid/video.jpg")
        self.assertEqual(by_id["file"]["payload"]["file_name"], "a.zip")
        self.assertEqual(by_id["face"]["type"], "face")
        self.assertEqual(by_id["face"]["payload"]["index"], 12)
        self.assertEqual(by_id["flash"]["type"], "flash")
        self.assertEqual(by_id["flash"]["flash_unique_id"], "unique-photo-id")
        self.assertEqual(by_id["flash"]["cloud_custom_data"], "unique-photo-id")

    def test_text_payload_without_object_name_remains_compatible(self) -> None:
        message = normalize_messages({"payload": {"text": "历史消息"}, "msgUID": "m1"})[0]
        self.assertEqual(message["text"], "历史消息")
        self.assertEqual(message["type"], "text")
        self.assertEqual(message["object_name"], "TIMTextElem")

    def test_ios_tuiemoji_token_survives_history_normalization(self) -> None:
        message = normalize_messages(
            {
                "MsgBody": [
                    {
                        "MsgType": "TIMTextElem",
                        "MsgContent": {"Text": "你好 [TUIEmoji_Guffaw]"},
                    }
                ],
                "MsgKey": "ios-small-emoji",
            }
        )[0]

        self.assertEqual(message["type"], "text")
        self.assertEqual(message["text"], "你好 [TUIEmoji_Guffaw]")
        self.assertEqual(message["payload"]["text"], "你好 [TUIEmoji_Guffaw]")

    def test_v154_sticker_groups_flatten_to_face_message_contract(self) -> None:
        stickers = normalize_stickers(
            {
                "code": "200",
                "json_obj": [
                    {
                        "id": "12",
                        "name": "收藏",
                        "icon": "images/sticker/group.png",
                        "urls_array": [
                            {"url": "https://example.invalid/a.webp"},
                            {"url": "https://example.invalid/b.webp"},
                        ],
                        "wh_array": [
                            {"width": "160", "height": "120"},
                            {"width": "80", "height": "90"},
                        ],
                    }
                ],
            }
        )
        self.assertEqual(len(stickers), 2)
        self.assertEqual(stickers[0]["group_id"], "12")
        self.assertEqual(stickers[0]["index"], 12)
        self.assertEqual(stickers[0]["data"], "https://example.invalid/a.webp")
        self.assertEqual(stickers[0]["width"], 160)
        self.assertEqual(stickers[1]["height"], 90)


class CapturingTimRestClient(TimRestClient):
    def __init__(self) -> None:
        self.calls = []

    def call(self, command, body, *, admin=None):
        self.calls.append((command, body, admin))
        return RestResult(ok=True, action=command, data=body)


class TimRestMediaContractTests(unittest.TestCase):
    def test_recent_contacts_and_roaming_history_use_documented_request_shapes(self) -> None:
        client = CapturingTimRestClient()

        contacts = client.recent_contacts(
            "42",
            timestamp=10,
            start_index=2,
            top_timestamp=8,
            top_start_index=1,
        )
        self.assertTrue(contacts.ok)
        self.assertEqual(client.calls[-1][0], "recentcontact/get_list")
        self.assertEqual(
            client.calls[-1][1],
            {
                "From_Account": "42",
                "TimeStamp": 10,
                "StartIndex": 2,
                "TopTimeStamp": 8,
                "TopStartIndex": 1,
                "AssistFlags": 7,
            },
        )

        roaming = client.roaming_messages(
            "42",
            "9",
            min_time=100,
            max_time=200,
            max_count=500,
            last_msg_key="last-key",
        )
        self.assertTrue(roaming.ok)
        self.assertEqual(client.calls[-1][0], "openim/admin_getroammsg")
        self.assertEqual(
            client.calls[-1][1],
            {
                "From_Account": "42",
                "To_Account": "9",
                "MaxCnt": 100,
                "MinTime": 100,
                "MaxTime": 200,
                "LastMsgKey": "last-key",
            },
        )

    def test_recent_contacts_and_roaming_history_reject_invalid_accounts(self) -> None:
        client = CapturingTimRestClient()

        self.assertFalse(client.recent_contacts("").ok)
        self.assertFalse(client.roaming_messages("42", "42").ok)
        self.assertFalse(client.roaming_messages("", "9").ok)
        self.assertEqual(client.calls, [])

    def test_c2c_revoke_uses_authenticated_sender_and_msg_key_shape(self) -> None:
        client = CapturingTimRestClient()

        result = client.revoke_c2c("42", "9", "message-key")

        self.assertTrue(result.ok)
        self.assertEqual(client.calls[-1][0], "openim/admin_msgwithdraw")
        self.assertEqual(
            client.calls[-1][1],
            {
                "From_Account": "42",
                "To_Account": "9",
                "MsgKey": "message-key",
            },
        )

    def test_face_and_remote_media_build_documented_msg_body(self) -> None:
        client = CapturingTimRestClient()
        result = client.send_face("42", "9", 12, "https://example.invalid/face.webp")
        self.assertTrue(result.ok)
        self.assertEqual(client.calls[-1][1]["MsgBody"][0]["MsgType"], "TIMFaceElem")
        self.assertEqual(client.calls[-1][1]["MsgBody"][0]["MsgContent"]["Index"], 12)

        result = client.send_remote_audio(
            "42",
            "9",
            url="https://example.invalid/voice.m4a",
            uuid="voice-uuid",
            duration=6,
            size=100,
        )
        self.assertTrue(result.ok)
        content = client.calls[-1][1]["MsgBody"][0]["MsgContent"]
        self.assertEqual(content["Second"], 6)
        self.assertEqual(content["Download_Flag"], 2)

        result = client.send_remote_file(
            "42",
            "9",
            url="https://example.invalid/a.zip",
            uuid="file-uuid",
            file_name="a.zip",
            file_size=10,
        )
        self.assertTrue(result.ok)
        self.assertEqual(client.calls[-1][1]["MsgBody"][0]["MsgType"], "TIMFileElem")

    def test_rest_rejects_local_media_instead_of_reporting_success(self) -> None:
        client = CapturingTimRestClient()
        result = client.send_remote_audio(
            "42",
            "9",
            url="C:\\recordings\\voice.m4a",
            uuid="voice-uuid",
            duration=5,
        )
        self.assertFalse(result.ok)
        self.assertIn("remote audio", result.error_info)
        self.assertEqual(client.calls, [])


class ImFlashRoutingContractTests(unittest.TestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.calls = []

        def call(self, action, params=None, **kwargs):
            body = dict(params or {})
            body.update(kwargs)
            self.calls.append((action, body))
            return object()

    def test_flash_routes_use_apk_parameter_names(self) -> None:
        client = self.FakeClient()
        api = ImAPI(client)
        api.flash_photo_send("9", "images/202607/a.jpg")
        api.flash_photo_get("unique-id")
        self.assertEqual(
            client.calls,
            [
                ("SendTencentFlashPhoto", {"targetId": "9", "photourl": "images/202607/a.jpg"}),
                ("GetflashphotoTencent", {"uniqueid": "unique-id"}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
