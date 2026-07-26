"""Web-local canonical moments service."""

from importlib import import_module
from typing import Any

from .contracts import (
    CanonicalSocialStore,
    FeedItem,
    FeedScore,
    InvalidSocialContent,
    LegacyCommentInput,
    LegacyHistoryOutsideWindow,
    LegacyPostInput,
    SocialAuthorRef,
    SocialCommentMutation,
    SocialCommentView,
    SocialCommentsDisabled,
    SocialContentError,
    SocialContentForbidden,
    SocialContentNotFound,
    SocialIdempotencyConflict,
    SocialMirrorIntent,
    SocialPermissionPolicy,
    SocialPostMutation,
    SocialPostView,
    SocialPrincipal,
    SocialReactionMutation,
    SocialReactionState,
    SocialReportMutation,
    SocialReportState,
    SocialTopicMutation,
    SocialTopicView,
    SocialViewMutation,
)
from .service import LocalMomentsService, explain_feed_score


__all__ = [
    "CanonicalSocialStore",
    "FeedItem",
    "FeedScore",
    "InvalidSocialContent",
    "LegacyCommentInput",
    "LegacyHistoryOutsideWindow",
    "LegacyPostInput",
    "LegacyMomentsImportOrchestrator",
    "LocalMomentsService",
    "SocialAuthorRef",
    "SocialCommentMutation",
    "SocialCommentView",
    "SocialCommentsDisabled",
    "SocialContentError",
    "SocialContentForbidden",
    "SocialContentNotFound",
    "SocialIdempotencyConflict",
    "SocialMirrorIntent",
    "SocialPermissionPolicy",
    "SocialPostMutation",
    "SocialPostView",
    "SocialPrincipal",
    "SocialReactionMutation",
    "SocialReactionState",
    "SocialReportMutation",
    "SocialReportState",
    "SocialTopicMutation",
    "SocialTopicView",
    "SocialViewMutation",
    "SqlAlchemyCanonicalSocialStore",
    "SqlAlchemyLegacyMomentsWriter",
    "SqlAlchemySocialPermissionPolicy",
    "explain_feed_score",
    "run_legacy_moments_import",
]


def __getattr__(name: str) -> Any:
    modules = {
        "SqlAlchemyCanonicalSocialStore": ".repository",
        "LegacyMomentsImportOrchestrator": ".legacy_migration",
        "SqlAlchemyLegacyMomentsWriter": ".legacy_migration",
        "SqlAlchemySocialPermissionPolicy": ".policy",
        "run_legacy_moments_import": ".legacy_migration",
    }
    module_name = modules.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
