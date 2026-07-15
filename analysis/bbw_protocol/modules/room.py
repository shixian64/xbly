"""Voice room / agora / songs / redis channel."""

from __future__ import annotations

from typing import Any, Optional

from ..client import AGORA_RTC, AGORA_RTM, ApiResult, ProtocolClient


class RoomAPI:
    def __init__(self, client: ProtocolClient):
        self.c = client

    def create(self, audioroomtype: str = "处CP", my_id: Optional[str] = None) -> ApiResult:
        return self.c.call(
            "createRoom0",
            myID=my_id or self.c.session.uid,
            audioroomtype=audioroomtype,
        )

    def set(self, **params: Any) -> ApiResult:
        return self.c.call("roomSet0", params)

    def auth(self, uid: Optional[str] = None) -> ApiResult:
        return self.c.call("getRoomAuth", uid=uid or self.c.session.uid)

    def tips(self, type_: str = "1") -> ApiResult:
        return self.c.call("getRoomTips", type=type_)

    def top(self) -> ApiResult:
        return self.c.call("getRoomTop")

    def finish(self, **params: Any) -> ApiResult:
        return self.c.call("finishRoom", params)

    def agora_mem(self) -> ApiResult:
        return self.c.call("agoraRoomMem")

    def rtc_token(self, channel: str, uid: Optional[str] = None) -> ApiResult:
        return self.c.call_url(
            AGORA_RTC,
            uid=uid or self.c.session.uid,
            channelName=channel,
        )

    def rtm_token(self, uid: Optional[str] = None) -> ApiResult:
        return self.c.call_url(AGORA_RTM, uid=uid or self.c.session.uid)

    def song_list(self, room_id: str) -> ApiResult:
        return self.c.call("getSongList", roomId=room_id)

    def add_song(self, room_id: str, music_id: str) -> ApiResult:
        return self.c.call(
            "addSong",
            userId=self.c.session.uid,
            roomId=room_id,
            musicId=music_id,
        )

    def delete_song(self, room_id: str, song_id: str) -> ApiResult:
        return self.c.call("deleteSong", roomId=room_id, songId=song_id)

    def ktv_search(self, key_word: str, page: str = "1") -> ApiResult:
        return self.c.call("getKtvMusicList", key_word=key_word, pageIndex=page)

    def update_user_room_info(self, room_info: str) -> ApiResult:
        return self.c.call_redis(
            "updateUserRoomInfo",
            uid=self.c.session.uid,
            room_info=room_info,
        )

    def get_user_room_info(self, uid: Optional[str] = None) -> ApiResult:
        return self.c.call_redis("getUserRoomInfo", uid=uid or self.c.session.uid)

    def raw(self, action: str, **params: Any) -> ApiResult:
        return self.c.call(action, params)

    def raw_redis(self, action: str, **params: Any) -> ApiResult:
        return self.c.call_redis(action, params)
