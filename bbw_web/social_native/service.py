"""Web 本地权威资料与关系业务服务。"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from bbw_web.legacy_media_reference import projected_profile_avatar

from .contracts import (
    REQUEST_ACCEPTED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    BlockView,
    CanonicalSocialStore,
    FriendRequestMutationResult,
    FriendRequestView,
    ProfileMutationResult,
    ProfilePatch,
    RelationshipFlags,
    SocialAccount,
    SocialMutationResult,
    SocialPrincipal,
    SocialProfileView,
)
from .errors import (
    InvalidNickname,
    InvalidSocialInput,
    SocialBlocked,
    SocialIdentityUnavailable,
    SocialSelfActionForbidden,
    SocialTargetUnavailable,
)


MIN_NICKNAME_LENGTH = 2
MAX_NICKNAME_LENGTH = 32
MAX_SIGNATURE_LENGTH = 280
MAX_CITY_LENGTH = 64
MAX_AVATAR_LENGTH = 2_048
MAX_FRIEND_REQUEST_MESSAGE_LENGTH = 100
MAX_OPERATION_ID_LENGTH = 160
MAX_PROFILE_BATCH = 100
MAX_RELATION_PAGE = 200

PROFILE_FIELDS = frozenset({"nickname", "avatar", "signature", "city", "gender"})
GENDER_VALUES = frozenset({"unspecified", "male", "female", "other"})
_NICKNAME_PUNCTUATION = frozenset({" ", "_", "-", ".", "·"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_upstream_uid(value: object) -> str:
    uid = str(value or "").strip()
    if (
        not uid
        or len(uid) > 128
        or uid.lower() in {"0", "none", "null"}
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in uid)
    ):
        return ""
    return uid


def normalize_operation_id(value: object) -> str:
    operation_id = str(value or "").strip()
    if (
        not operation_id
        or len(operation_id) > MAX_OPERATION_ID_LENGTH
        or any(unicodedata.category(char).startswith("C") for char in operation_id)
    ):
        return ""
    return operation_id


def normalize_nickname(value: object) -> str:
    """返回 NFKC 昵称；拒绝控制符、Emoji 和不可见/装饰字符。"""

    if not isinstance(value, str):
        raise InvalidNickname("昵称必须是字符串")
    nickname = unicodedata.normalize("NFKC", value).strip()
    if not MIN_NICKNAME_LENGTH <= len(nickname) <= MAX_NICKNAME_LENGTH:
        raise InvalidNickname("昵称长度必须为 2 至 32 个字符")
    if "  " in nickname:
        raise InvalidNickname("昵称不能包含连续空格")
    has_letter_or_number = False
    for char in nickname:
        category = unicodedata.category(char)
        if category[0] in {"L", "M", "N"}:
            has_letter_or_number = True
            continue
        if char not in _NICKNAME_PUNCTUATION:
            raise InvalidNickname("昵称包含不允许的字符")
    if not has_letter_or_number:
        raise InvalidNickname("昵称必须包含文字或数字")
    return nickname


def _normalize_profile_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise InvalidSocialInput(f"{field} 必须是字符串")
    normalized = unicodedata.normalize("NFC", value).strip()
    if len(normalized) > maximum:
        raise InvalidSocialInput(f"{field} 超过长度限制")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise InvalidSocialInput(f"{field} 包含控制字符")
    return normalized


def normalize_profile_patch(values: Mapping[str, object]) -> ProfilePatch:
    if not isinstance(values, Mapping):
        raise InvalidSocialInput("资料更新必须是对象")
    unknown = set(values) - PROFILE_FIELDS
    if unknown:
        raise InvalidSocialInput(f"不支持的资料字段：{', '.join(sorted(unknown))}")
    if not values:
        raise InvalidSocialInput("资料更新不能为空")

    normalized: dict[str, str] = {}
    if "nickname" in values:
        normalized["nickname"] = normalize_nickname(values["nickname"])
    if "signature" in values:
        normalized["signature"] = _normalize_profile_text(
            values["signature"], field="signature", maximum=MAX_SIGNATURE_LENGTH
        )
    if "city" in values:
        normalized["city"] = _normalize_profile_text(
            values["city"], field="city", maximum=MAX_CITY_LENGTH
        )
    if "gender" in values:
        gender = _normalize_profile_text(values["gender"], field="gender", maximum=16)
        if gender not in GENDER_VALUES:
            raise InvalidSocialInput("gender 取值不合法")
        normalized["gender"] = gender
    if "avatar" in values:
        avatar = _normalize_profile_text(
            values["avatar"], field="avatar", maximum=MAX_AVATAR_LENGTH
        )
        if avatar:
            parsed = urlsplit(avatar)
            is_https = parsed.scheme == "https" and bool(parsed.netloc)
            is_local = avatar.startswith("/") and not avatar.startswith("//")
            if not (is_https or is_local):
                raise InvalidSocialInput("avatar 必须是 HTTPS 地址或站内绝对路径")
        normalized["avatar"] = avatar
    return ProfilePatch(values=normalized)


def normalize_friend_request_message(value: object) -> str:
    return _normalize_profile_text(
        str(value or ""),
        field="好友申请留言",
        maximum=MAX_FRIEND_REQUEST_MESSAGE_LENGTH,
    )


class LocalSocialService:
    """本地数据库为资料和关系权威；上游镜像不参与本服务的成功判定。"""

    def __init__(
        self,
        store: CanonicalSocialStore,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.store = store
        self.clock = clock

    def _actor(self, principal: SocialPrincipal) -> SocialAccount:
        actor = self.store.resolve_principal(principal)
        if actor is None:
            raise SocialIdentityUnavailable("当前 Web 账号不可用于本地资料与关系")
        return actor

    def _target(self, actor: SocialAccount, principal: SocialPrincipal, value: object) -> SocialAccount:
        uid = normalize_upstream_uid(value)
        if not uid:
            raise InvalidSocialInput("目标 UID 不合法")
        if uid == actor.upstream_uid:
            raise SocialSelfActionForbidden("不能对自己执行该操作")
        target = self.store.resolve_active_target(uid, provider=principal.account_provider)
        if target is None:
            raise SocialTargetUnavailable("目标账号不存在、未迁移或已停用")
        if target.user_id == actor.user_id:
            raise SocialSelfActionForbidden("不能对自己执行该操作")
        return target

    @staticmethod
    def _operation(value: object) -> str:
        operation_id = normalize_operation_id(value)
        if not operation_id:
            raise InvalidSocialInput("operation_id 不合法")
        return operation_id

    @staticmethod
    def _profile(account: SocialAccount, flags: RelationshipFlags) -> SocialProfileView:
        profile = dict(account.profile or {})
        return SocialProfileView(
            user_id=account.user_id,
            upstream_uid=account.upstream_uid,
            nickname=str(account.display_name or profile.get("nickname") or ""),
            avatar=projected_profile_avatar(profile),
            signature=str(profile.get("signature") or ""),
            city=str(profile.get("city") or ""),
            gender=str(profile.get("gender") or "unspecified"),
            relationship=flags,
            updated_at=account.updated_at,
        )

    def get_me(self, *, principal: SocialPrincipal) -> SocialProfileView:
        actor = self._actor(principal)
        return self._profile(actor, RelationshipFlags())

    def get_user(
        self, *, principal: SocialPrincipal, upstream_uid: object
    ) -> SocialProfileView:
        actor = self._actor(principal)
        uid = normalize_upstream_uid(upstream_uid)
        if not uid:
            raise InvalidSocialInput("目标 UID 不合法")
        if uid == actor.upstream_uid:
            return self._profile(actor, RelationshipFlags())
        target = self.store.resolve_active_target(uid, provider=principal.account_provider)
        if target is None:
            raise SocialTargetUnavailable("目标账号不存在、未迁移或已停用")
        return self._profile(target, self.store.relationship_flags(actor, target))

    def get_users(
        self, *, principal: SocialPrincipal, upstream_uids: Iterable[object]
    ) -> list[SocialProfileView]:
        actor = self._actor(principal)
        normalized: list[str] = []
        for value in upstream_uids:
            uid = normalize_upstream_uid(value)
            if not uid:
                raise InvalidSocialInput("批量资料包含不合法 UID")
            if uid not in normalized:
                normalized.append(uid)
        if len(normalized) > MAX_PROFILE_BATCH:
            raise InvalidSocialInput("单次最多查询 100 个账号")
        targets = self.store.resolve_active_targets(
            [uid for uid in normalized if uid != actor.upstream_uid],
            provider=principal.account_provider,
        )
        views: list[SocialProfileView] = []
        for uid in normalized:
            if uid == actor.upstream_uid:
                views.append(self._profile(actor, RelationshipFlags()))
                continue
            target = targets.get(uid)
            if target is not None:
                views.append(self._profile(target, self.store.relationship_flags(actor, target)))
        return views

    def update_me(
        self,
        *,
        principal: SocialPrincipal,
        values: Mapping[str, object],
        operation_id: object,
    ) -> ProfileMutationResult:
        actor = self._actor(principal)
        return self.store.update_profile(
            actor=actor,
            patch=normalize_profile_patch(values),
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def set_following(
        self,
        *,
        principal: SocialPrincipal,
        target_upstream_uid: object,
        active: bool,
        operation_id: object,
    ) -> SocialMutationResult:
        actor = self._actor(principal)
        target = self._target(actor, principal, target_upstream_uid)
        if active:
            flags = self.store.relationship_flags(actor, target)
            if flags.blocked or flags.blocked_by:
                raise SocialBlocked("任一方黑名单关系禁止关注")
        return self.store.set_following(
            actor=actor,
            target=target,
            active=bool(active),
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def follow(self, **kwargs: object) -> SocialMutationResult:
        return self.set_following(active=True, **kwargs)

    def unfollow(self, **kwargs: object) -> SocialMutationResult:
        return self.set_following(active=False, **kwargs)

    def request_friend(
        self,
        *,
        principal: SocialPrincipal,
        target_upstream_uid: object,
        message: object = "",
        operation_id: object,
    ) -> FriendRequestMutationResult:
        actor = self._actor(principal)
        target = self._target(actor, principal, target_upstream_uid)
        flags = self.store.relationship_flags(actor, target)
        if flags.blocked or flags.blocked_by:
            raise SocialBlocked("任一方黑名单关系禁止申请好友")
        return self.store.create_friend_request(
            actor=actor,
            target=target,
            message=normalize_friend_request_message(message),
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def resolve_friend_request(
        self,
        *,
        principal: SocialPrincipal,
        requester_upstream_uid: object,
        resolution: str,
        operation_id: object,
    ) -> FriendRequestMutationResult:
        if resolution not in {REQUEST_ACCEPTED, REQUEST_REJECTED}:
            raise InvalidSocialInput("好友申请只能接受或拒绝")
        actor = self._actor(principal)
        requester = self._target(actor, principal, requester_upstream_uid)
        if resolution == REQUEST_ACCEPTED:
            flags = self.store.relationship_flags(actor, requester)
            if flags.blocked or flags.blocked_by:
                raise SocialBlocked("任一方黑名单关系禁止接受好友申请")
        return self.store.resolve_friend_request(
            actor=actor,
            requester=requester,
            resolution=resolution,
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def cancel_friend_request(
        self,
        *,
        principal: SocialPrincipal,
        target_upstream_uid: object,
        operation_id: object,
    ) -> FriendRequestMutationResult:
        actor = self._actor(principal)
        target = self._target(actor, principal, target_upstream_uid)
        return self.store.cancel_friend_request(
            actor=actor,
            target=target,
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def delete_friend(
        self,
        *,
        principal: SocialPrincipal,
        target_upstream_uid: object,
        operation_id: object,
    ) -> SocialMutationResult:
        actor = self._actor(principal)
        target = self._target(actor, principal, target_upstream_uid)
        return self.store.delete_friend(
            actor=actor,
            target=target,
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def set_blocked(
        self,
        *,
        principal: SocialPrincipal,
        target_upstream_uid: object,
        active: bool,
        operation_id: object,
    ) -> SocialMutationResult:
        actor = self._actor(principal)
        target = self._target(actor, principal, target_upstream_uid)
        return self.store.set_blocked(
            actor=actor,
            target=target,
            active=bool(active),
            operation_id=self._operation(operation_id),
            occurred_at=self.clock(),
        )

    def block(self, **kwargs: object) -> SocialMutationResult:
        return self.set_blocked(active=True, **kwargs)

    def unblock(self, **kwargs: object) -> SocialMutationResult:
        return self.set_blocked(active=False, **kwargs)

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        try:
            bounded_limit = min(max(1, int(limit)), MAX_RELATION_PAGE)
            bounded_offset = max(0, int(offset))
        except (TypeError, ValueError) as exc:
            raise InvalidSocialInput("分页参数不合法") from exc
        return bounded_limit, bounded_offset

    def list_relationships(
        self,
        *,
        principal: SocialPrincipal,
        relation: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SocialProfileView]:
        if relation not in {"following", "followers", "friends"}:
            raise InvalidSocialInput("关系列表类型不合法")
        owner = self._actor(principal)
        bounded_limit, bounded_offset = self._page(limit, offset)
        accounts = self.store.list_relation_accounts(
            owner=owner,
            relation=relation,
            limit=bounded_limit,
            offset=bounded_offset,
        )
        return [
            self._profile(account, self.store.relationship_flags(owner, account))
            for account in accounts
        ]

    def list_friend_requests(
        self,
        *,
        principal: SocialPrincipal,
        direction: str = "incoming",
        state: str = REQUEST_PENDING,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FriendRequestView]:
        if direction not in {"incoming", "outgoing"}:
            raise InvalidSocialInput("好友申请方向不合法")
        if state not in {
            REQUEST_PENDING,
            REQUEST_ACCEPTED,
            REQUEST_REJECTED,
            "cancelled",
            "blocked",
        }:
            raise InvalidSocialInput("好友申请状态不合法")
        owner = self._actor(principal)
        bounded_limit, bounded_offset = self._page(limit, offset)
        return self.store.list_friend_requests(
            owner=owner,
            direction=direction,
            state=state,
            limit=bounded_limit,
            offset=bounded_offset,
        )

    def list_blocks(
        self,
        *,
        principal: SocialPrincipal,
        direction: str = "outgoing",
        limit: int = 50,
        offset: int = 0,
    ) -> list[BlockView]:
        if direction not in {"outgoing", "incoming"}:
            raise InvalidSocialInput("黑名单方向不合法")
        owner = self._actor(principal)
        bounded_limit, bounded_offset = self._page(limit, offset)
        rows = self.store.list_blocks(
            owner=owner,
            direction=direction,
            limit=bounded_limit,
            offset=bounded_offset,
        )
        return [
            BlockView(
                profile=self._profile(account, self.store.relationship_flags(owner, account)),
                direction=direction,
                started_at=started_at,
            )
            for account, started_at in rows
        ]
