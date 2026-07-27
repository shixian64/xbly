"""Web 本地资料与关系服务的稳定错误契约。"""

from __future__ import annotations


class SocialNativeError(RuntimeError):
    """所有可安全映射为 API 错误的本地社交异常基类。"""

    code = "SOCIAL_NATIVE_ERROR"


class InvalidSocialInput(SocialNativeError):
    code = "INVALID_SOCIAL_INPUT"


class InvalidNickname(InvalidSocialInput):
    code = "INVALID_NICKNAME"


class SocialIdentityUnavailable(SocialNativeError):
    code = "SOCIAL_IDENTITY_UNAVAILABLE"


class SocialTargetUnavailable(SocialNativeError):
    code = "SOCIAL_TARGET_UNAVAILABLE"


class SocialTargetNotMigrated(SocialTargetUnavailable):
    """内部标记：目标没有本地账号绑定，但对外保持统一错误。"""


class SocialSelfActionForbidden(SocialNativeError):
    code = "SOCIAL_SELF_ACTION_FORBIDDEN"


class SocialBlocked(SocialNativeError):
    code = "SOCIAL_BLOCKED"


class AlreadyFriends(SocialNativeError):
    code = "ALREADY_FRIENDS"


class FriendRequestUnavailable(SocialNativeError):
    code = "FRIEND_REQUEST_UNAVAILABLE"


class FriendRequestStateConflict(SocialNativeError):
    code = "FRIEND_REQUEST_STATE_CONFLICT"


class SocialIdempotencyConflict(SocialNativeError):
    code = "SOCIAL_IDEMPOTENCY_CONFLICT"
