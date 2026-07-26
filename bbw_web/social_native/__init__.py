"""Web 本地权威资料与关系核心。"""

from importlib import import_module
from typing import Any

from .contracts import (
    FRIEND_REQUEST_TRANSITIONS,
    SOCIAL_NATIVE_PROVIDER,
    BlockView,
    CanonicalSocialStore,
    FriendRequestMutationResult,
    FriendRequestView,
    ProfileMutationResult,
    RelationshipFlags,
    SocialMutationResult,
    SocialPrincipal,
    SocialProfileView,
)
from .errors import (
    AlreadyFriends,
    FriendRequestStateConflict,
    FriendRequestUnavailable,
    InvalidNickname,
    InvalidSocialInput,
    SocialBlocked,
    SocialIdempotencyConflict,
    SocialIdentityUnavailable,
    SocialNativeError,
    SocialSelfActionForbidden,
    SocialTargetUnavailable,
)
from .service import LocalSocialService, normalize_nickname, normalize_profile_patch

__all__ = [
    "AlreadyFriends",
    "BlockView",
    "CanonicalSocialStore",
    "FRIEND_REQUEST_TRANSITIONS",
    "FriendRequestMutationResult",
    "FriendRequestStateConflict",
    "FriendRequestUnavailable",
    "FriendRequestView",
    "InvalidNickname",
    "InvalidSocialInput",
    "LocalSocialService",
    "ProfileMutationResult",
    "RelationshipFlags",
    "SOCIAL_NATIVE_PROVIDER",
    "SocialBlocked",
    "SocialIdempotencyConflict",
    "SocialIdentityUnavailable",
    "SocialMutationResult",
    "SocialNativeError",
    "SocialPrincipal",
    "SocialProfileView",
    "SocialSelfActionForbidden",
    "SocialTargetUnavailable",
    "SqlAlchemyCanonicalSocialStore",
    "normalize_nickname",
    "normalize_profile_patch",
]


def __getattr__(name: str) -> Any:
    if name != "SqlAlchemyCanonicalSocialStore":
        raise AttributeError(name)
    module = import_module(".repository", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
