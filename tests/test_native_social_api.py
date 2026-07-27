from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from sqlalchemy.dialects import postgresql

from bbw_prod.models import Relationship
from bbw_web import native_social_api as API
from bbw_web.media_native.references import BoundMediaAssetReference
from bbw_web.social_native import (
    SocialIdempotencyConflict,
    SocialTargetNotMigrated,
)
from bbw_web.social_native.contracts import (
    REQUEST_PENDING,
    FriendRequestView,
    ProfilePatch,
    RelationshipFlags,
    SocialAccount,
    SocialMutationResult,
    SocialProfileView,
)
from bbw_web.social_native.repository import SqlAlchemyCanonicalSocialStore


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _identity(uid: str = "账号-A_9") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        external_account_id=uuid.uuid4(),
        upstream_uid=uid,
    )


def _profile(
    uid: str,
    *,
    nickname: str = "本地用户",
    avatar: str | None = None,
) -> SocialProfileView:
    return SocialProfileView(
        user_id=uuid.uuid4(),
        upstream_uid=uid,
        nickname=nickname,
        avatar=avatar or f"/media/{uid}.jpg",
        signature="本地资料",
        city="福州",
        gender="other",
        relationship=RelationshipFlags(
            following=True,
            followed_by=True,
            friend=True,
            outgoing_friend_request=REQUEST_PENDING,
        ),
        updated_at=NOW,
    )


@contextmanager
def _scope(db: object):
    yield db


class NativeSocialDispatchContractTests(unittest.TestCase):
    def test_exact_route_surface_and_unhandled_contract(self) -> None:
        self.assertEqual(
            API.GET_PATHS,
            {
                "/api/profile/me",
                "/api/profile/user",
                "/api/profile/users",
                "/api/social/follows",
                "/api/social/fans",
                "/api/social/friends",
                "/api/social/friend-apply",
                "/api/social/blacklist",
                "/api/social/blacklist-me",
                "/api/social/visitors",
            },
        )
        self.assertEqual(
            API.POST_PATHS,
            {
                "/api/profile/nick",
                "/api/profile/reset",
                "/api/profile/privacy",
                "/api/social/follow",
                "/api/social/unfollow",
                "/api/social/add-friend",
                "/api/social/agree-friend",
                "/api/social/reject-friend",
                "/api/social/cancel-friend",
                "/api/social/delete-friend",
                "/api/social/blacklist-add",
                "/api/social/blacklist-del",
                "/api/social/visit",
            },
        )
        self.assertEqual(API.SOCIAL_NATIVE_WRITE_PATHS, API.POST_PATHS)
        self.assertIsNone(
            API.dispatch_social_native(None, "GET", "/api/not-social", {}, {})
        )
        wrong_method = API.dispatch_social_native(
            _identity(), "POST", "/api/profile/me", {}, {}
        )
        self.assertEqual(wrong_method.status, 405)
        self.assertEqual(wrong_method.payload["code"], "METHOD_NOT_ALLOWED")

    def test_missing_or_invalid_identity_is_rejected_before_database_access(self) -> None:
        with patch.object(API, "session_scope") as session_scope:
            response = API.dispatch_social_native(
                None, "GET", "/api/profile/me", {}, {}
            )

        session_scope.assert_not_called()
        self.assertEqual(response.status, 401)
        self.assertFalse(response.payload["ok"])
        self.assertEqual(response.payload["code"], "SOCIAL_IDENTITY_UNAVAILABLE")
        self.assertEqual(response.payload["source"], "web-local")

    def test_profile_response_keeps_nonnumeric_uid_and_app_js_fields(self) -> None:
        identity = _identity("账号-A_9")
        service = Mock()
        service.get_user.return_value = _profile("用户-乙_7", nickname="乙用户")
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
        ):
            response = API.dispatch_social_native(
                identity,
                "GET",
                "/api/profile/user",
                {"uid": "用户-乙_7"},
                {},
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["count"], 1)
        self.assertEqual(response.payload["user"]["id"], "用户-乙_7")
        self.assertEqual(response.payload["items"], response.payload["list"])
        user = response.payload["user"]
        for key in (
            "uid",
            "nickname",
            "avatar",
            "is_friend",
            "is_friend_apply",
            "is_following",
            "is_follower",
            "blocked",
            "blocked_by",
            "source",
        ):
            self.assertIn(key, user)
        self.assertEqual(user["source"], "web-local")
        for private_key in (
            "money",
            "vip",
            "svip",
            "user_role",
            "rp_verify_time",
            "is_realname",
            "logged_in",
        ):
            self.assertNotIn(private_key, user)
        service.get_user.assert_called_once()
        self.assertEqual(
            service.get_user.call_args.kwargs["upstream_uid"], "用户-乙_7"
        )

    def test_unmigrated_profile_error_allows_only_internal_legacy_read_fallback(self) -> None:
        service = Mock()
        service.get_user.side_effect = SocialTargetNotMigrated(
            "目标账号不存在、未迁移或已停用"
        )
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
        ):
            response = API.dispatch_social_native(
                _identity(),
                "GET",
                "/api/profile/user",
                {"uid": "未迁移-9"},
                {},
            )

        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["code"], "SOCIAL_TARGET_UNAVAILABLE")
        self.assertNotIn("legacy_read_fallback_allowed", response.payload)
        self.assertTrue(response.legacy_read_fallback_allowed)

    def test_me_profile_includes_private_state_and_derives_realname(self) -> None:
        identity = _identity("账号-A_9")
        profile = _profile(identity.upstream_uid, nickname="当前用户")
        account = SocialAccount(
            identity.user_id,
            identity.external_account_id,
            identity.upstream_uid,
            "当前用户",
            {"money": "88"},
            NOW,
            account_display_data={
                "rp_verify_time": "1715268133",
                "portrait": "/media/account-avatar.jpg",
                "vip": "1",
                "svip": "2",
                "user_sign": "账号签名",
                "user_role": "member",
            },
        )
        service = Mock()
        service.get_me_with_account.return_value = (profile, account)
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
        ):
            response = API.dispatch_social_native(
                identity,
                "GET",
                "/api/profile/me",
                {},
                {},
            )

        self.assertEqual(response.status, 200)
        user = response.payload["user"]
        self.assertTrue(user["is_realname"])
        self.assertEqual(user["rp_verify_time"], "1715268133")
        self.assertEqual(user["money"], "88")
        self.assertEqual(user["vip"], "1")
        self.assertEqual(user["svip"], "2")
        self.assertEqual(user["user_role"], "member")
        self.assertTrue(user["logged_in"])
        self.assertEqual(response.payload["items"], [user])
        self.assertEqual(response.payload["list"], [user])

    def test_profile_users_preserves_order_and_envelope(self) -> None:
        service = Mock()
        service.get_users.return_value = [_profile("字母-A"), _profile("42")]
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
        ):
            response = API.dispatch_social_native(
                _identity(),
                "GET",
                "/api/profile/users",
                {"uids": ["字母-A,42"]},
                {},
            )
        self.assertEqual(
            [item["uid"] for item in response.payload["items"]], ["字母-A", "42"]
        )
        self.assertEqual(response.payload["count"], 2)
        self.assertEqual(response.payload["items"], response.payload["list"])

    def test_existing_operation_id_is_strictly_reused_for_canonical_and_outbox(self) -> None:
        identity = _identity("actor-A")
        service = Mock()
        service.follow.return_value = SocialMutationResult(
            action="follow",
            actor_upstream_uid="actor-A",
            target_upstream_uid="peer-B",
            state="active",
            changed=True,
            occurred_at=NOW,
        )
        outbox = Mock()
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
            patch.object(API, "OperationOutboxRepository", return_value=outbox) as repo,
        ):
            response = API.dispatch_social_native(
                identity,
                "POST",
                "/api/social/follow",
                {},
                {"uid": "peer-B", "operation_id": "browser-op-1"},
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["compatibility_sync"], "pending")
        self.assertEqual(response.payload["source"], "web-local")
        self.assertEqual(
            service.follow.call_args.kwargs["operation_id"], "browser-op-1"
        )
        repo.assert_called_once_with(db)
        enqueue = outbox.enqueue.call_args.kwargs
        self.assertEqual(enqueue["owner_user_id"], identity.user_id)
        self.assertEqual(enqueue["idempotency_key"], "browser-op-1")
        self.assertEqual(enqueue["status"], "pending")
        self.assertEqual(enqueue["payload"]["path"], "/api/social/follow")

    def test_missing_operation_id_is_generated_once_and_shared_with_outbox(self) -> None:
        generated = uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        service = Mock()
        request_id = uuid.uuid4()
        service.request_friend.return_value = SimpleNamespace(
            request=FriendRequestView(
                request_id,
                "actor-A",
                "peer-B",
                "你好",
                REQUEST_PENDING,
                NOW,
            ),
            changed=True,
            idempotent_replay=False,
        )
        outbox = Mock()
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
            patch.object(API, "OperationOutboxRepository", return_value=outbox),
            patch.object(API.uuid, "uuid4", return_value=generated),
        ):
            response = API.dispatch_social_native(
                _identity("actor-A"),
                "POST",
                "/api/social/add-friend",
                {},
                {"uid": "peer-B", "leave_word": "你好"},
            )

        operation_id = str(generated)
        self.assertEqual(
            service.request_friend.call_args.kwargs["operation_id"], operation_id
        )
        self.assertEqual(
            outbox.enqueue.call_args.kwargs["idempotency_key"], operation_id
        )
        self.assertEqual(response.payload["request_status"], REQUEST_PENDING)

    def test_idempotency_conflict_is_stable_and_does_not_enqueue_compatibility(self) -> None:
        service = Mock()
        service.follow.side_effect = SocialIdempotencyConflict("operation conflict")
        outbox = Mock()
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
            patch.object(API, "OperationOutboxRepository", return_value=outbox),
        ):
            response = API.dispatch_social_native(
                _identity("actor-A"),
                "POST",
                "/api/social/follow",
                {},
                {"uid": "peer-B", "operation_id": "same-key"},
            )

        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload["code"], "SOCIAL_IDEMPOTENCY_CONFLICT")
        outbox.enqueue.assert_not_called()

    def test_privacy_is_whitelisted_nested_and_queued_without_arbitrary_fields(self) -> None:
        identity = _identity("actor-A")
        actor = SocialAccount(
            identity.user_id,
            identity.external_account_id,
            "actor-A",
            "当前用户",
            {"privacy": {"show_city": True}},
            NOW,
        )
        store = Mock()
        store.resolve_principal.return_value = actor
        store.update_profile.return_value = SimpleNamespace(
            changed=True, idempotent_replay=False
        )
        outbox = Mock()
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=store),
            patch.object(API, "LocalSocialService", return_value=Mock()),
            patch.object(API, "OperationOutboxRepository", return_value=outbox),
        ):
            response = API.dispatch_social_native(
                identity,
                "POST",
                "/api/profile/privacy",
                {},
                {
                    "privacy": {
                        "show_city": False,
                        "allow_friend_request": True,
                    },
                    "operation_id": "privacy-one",
                },
            )

        patch_value = store.update_profile.call_args.kwargs["patch"]
        self.assertEqual(
            patch_value.values["privacy"],
            {"show_city": False, "allow_friend_request": True},
        )
        self.assertEqual(response.payload["privacy"], patch_value.values["privacy"])
        self.assertEqual(response.payload["compatibility_sync"], "pending")
        self.assertEqual(
            outbox.enqueue.call_args.kwargs["payload"]["body"],
            {
                "privacy": {
                    "show_city": False,
                    "allow_friend_request": True,
                }
            },
        )

    def test_unknown_privacy_field_is_rejected_before_outbox(self) -> None:
        outbox = Mock()
        db = object()
        with (
            patch.object(API, "session_scope", side_effect=lambda: _scope(db)),
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=Mock()),
            patch.object(API, "OperationOutboxRepository", return_value=outbox),
        ):
            response = API.dispatch_social_native(
                _identity(),
                "POST",
                "/api/profile/privacy",
                {},
                {"admin": True, "operation_id": "privacy-invalid"},
            )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload["code"], "INVALID_SOCIAL_INPUT")
        outbox.enqueue.assert_not_called()

    def test_native_avatar_is_bound_locally_and_compatibility_is_cancelled(self) -> None:
        identity = _identity("actor-A")
        asset_id = uuid.uuid4()
        content_path = f"/api/media/native/{asset_id}/content"
        account = SocialAccount(
            identity.user_id,
            identity.external_account_id,
            identity.upstream_uid,
            "当前用户",
            {"avatar": content_path},
            NOW,
        )
        service = Mock()
        service.update_me.return_value = SimpleNamespace(
            profile=account,
            changed=True,
            idempotent_replay=False,
        )
        service.get_me.return_value = _profile(
            identity.upstream_uid,
            avatar=content_path,
        )
        bound = BoundMediaAssetReference(
            reference_id=uuid.uuid4(),
            asset_id=asset_id,
            owner_user_id=identity.user_id,
            resource_type="profile",
            resource_id=identity.user_id,
            slot="avatar",
            kind="image",
            content_type="image/jpeg",
            size_bytes=1024,
            private_bucket="private-media",
            private_object_key=(
                f"production/web-media-private/{identity.user_id}/{asset_id}/avatar.jpg"
            ),
        )
        references = Mock()
        references.bind_profile_avatar.return_value = bound
        outbox = Mock()
        outbox.enqueue.return_value = (SimpleNamespace(status="cancelled"), True)
        db = object()
        with (
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
            patch.object(
                API,
                "SqlAlchemyMediaAssetReferenceRepository",
                return_value=references,
            ) as repository,
            patch.object(API, "OperationOutboxRepository", return_value=outbox),
        ):
            response = API.dispatch_social_native(
                identity,
                "POST",
                "/api/profile/reset",
                {},
                {
                    "field": "avatar",
                    "avatar_asset_id": str(asset_id),
                    "operation_id": "avatar-one",
                },
                db=db,
                media_reference_deployment="production",
                media_reference_bucket="private-media",
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["avatar_asset_id"], str(asset_id))
        self.assertEqual(response.payload["avatar"], content_path)
        self.assertEqual(response.payload["compatibility_sync"], "cancelled")
        self.assertEqual(
            service.update_me.call_args.kwargs["values"],
            {"avatar": content_path},
        )
        references.bind_profile_avatar.assert_called_once_with(
            owner_user_id=identity.user_id,
            asset_id=asset_id,
        )
        repository.assert_called_once_with(
            db,
            deployment="production",
            private_bucket="private-media",
        )
        enqueue = outbox.enqueue.call_args.kwargs
        self.assertEqual(enqueue["status"], "cancelled")
        self.assertEqual(
            enqueue["last_error"], "profile_avatar_upload_not_mappable"
        )

    def test_old_avatar_operation_replay_keeps_current_reference(self) -> None:
        identity = _identity("actor-A")
        old_asset_id = uuid.uuid4()
        current_asset_id = uuid.uuid4()
        current_path = f"/api/media/native/{current_asset_id}/content"
        account = SocialAccount(
            identity.user_id,
            identity.external_account_id,
            identity.upstream_uid,
            "当前用户",
            {"avatar": current_path},
            NOW,
        )
        service = Mock()
        service.update_me.return_value = SimpleNamespace(
            profile=account,
            changed=True,
            idempotent_replay=True,
        )
        service.get_me.return_value = _profile(
            identity.upstream_uid,
            avatar=current_path,
        )
        current = BoundMediaAssetReference(
            reference_id=uuid.uuid4(),
            asset_id=current_asset_id,
            owner_user_id=identity.user_id,
            resource_type="profile",
            resource_id=identity.user_id,
            slot="avatar",
            kind="image",
            content_type="image/jpeg",
            size_bytes=1024,
            private_bucket="private-media",
            private_object_key=(
                f"production/web-media-private/{identity.user_id}/"
                f"{current_asset_id}/avatar.jpg"
            ),
        )
        references = Mock()
        references.get_current_reference.return_value = current
        outbox = Mock()
        outbox.enqueue.return_value = (SimpleNamespace(status="cancelled"), False)
        with (
            patch.object(API, "SqlAlchemyCanonicalSocialStore", return_value=Mock()),
            patch.object(API, "LocalSocialService", return_value=service),
            patch.object(
                API,
                "SqlAlchemyMediaAssetReferenceRepository",
                return_value=references,
            ),
            patch.object(API, "OperationOutboxRepository", return_value=outbox),
        ):
            response = API.dispatch_social_native(
                identity,
                "POST",
                "/api/profile/reset",
                {},
                {
                    "field": "avatar",
                    "avatar_asset_id": str(old_asset_id),
                    "operation_id": "old-avatar-operation",
                },
                db=object(),
                media_reference_deployment="production",
                media_reference_bucket="private-media",
            )

        self.assertEqual(response.status, 200)
        self.assertTrue(response.payload["idempotent_replay"])
        self.assertEqual(response.payload["avatar_asset_id"], str(current_asset_id))
        self.assertEqual(response.payload["avatar"], current_path)
        references.get_current_reference.assert_called_once_with(current_asset_id)
        references.bind_profile_avatar.assert_not_called()


class NativeSocialLegacyFriendRequestTests(unittest.TestCase):
    @staticmethod
    def _legacy_request(
        identity: SimpleNamespace,
        *,
        owner_user_id: uuid.UUID,
        subject_uid: str,
        metadata: dict[str, object],
    ) -> Relationship:
        return Relationship(
            id=uuid.uuid4(),
            owner_user_id=owner_user_id,
            provider="beibeiwu",
            subject_upstream_uid=subject_uid,
            kind="friend_request",
            status="active",
            started_at=NOW,
            ended_at=None,
            extra_data=metadata,
        )

    def test_legacy_incoming_mirror_apply_id_resolves_requester_uid(self) -> None:
        identity = _identity("actor-A")
        row = self._legacy_request(
            identity,
            owner_user_id=identity.user_id,
            subject_uid="requester-B",
            metadata={"source_path": "/api/social/friend-apply"},
        )
        db = SimpleNamespace(scalar=lambda _statement: row)
        uid = API._friend_request_peer_uid(
            db,
            principal=API._principal(identity),
            body={"apply_id": str(row.id)},
            direction="incoming",
        )
        self.assertEqual(uid, "requester-B")

    def test_legacy_outgoing_mirror_apply_id_resolves_owner_binding(self) -> None:
        identity = _identity("actor-A")
        peer_user_id = uuid.uuid4()
        row = self._legacy_request(
            identity,
            owner_user_id=peer_user_id,
            subject_uid=identity.upstream_uid,
            metadata={"source_path": "/api/social/friend-apply"},
        )

        class Db:
            def __init__(self) -> None:
                self.values = iter((row, SimpleNamespace(upstream_uid="peer-B")))

            def scalar(self, _statement):
                return next(self.values)

        uid = API._friend_request_peer_uid(
            Db(),
            principal=API._principal(identity),
            body={"id": str(row.id)},
            direction="outgoing",
        )
        self.assertEqual(uid, "peer-B")

    def test_untrusted_legacy_apply_id_is_rejected(self) -> None:
        identity = _identity("actor-A")
        row = self._legacy_request(
            identity,
            owner_user_id=identity.user_id,
            subject_uid="requester-B",
            metadata={"source_path": "/browser/archive"},
        )
        db = SimpleNamespace(scalar=lambda _statement: row)
        with self.assertRaises(API.InvalidSocialInput):
            API._friend_request_peer_uid(
                db,
                principal=API._principal(identity),
                body={"apply_id": str(row.id)},
                direction="incoming",
            )


class NativeSocialVisitContractTests(unittest.TestCase):
    def test_visit_uses_two_owner_scoped_activity_projections(self) -> None:
        identity = _identity("actor-A")
        actor = SocialAccount(
            identity.user_id,
            identity.external_account_id,
            "actor-A",
            "Actor",
        )
        target = SocialAccount(uuid.uuid4(), uuid.uuid4(), "peer-B", "Peer")
        store = Mock()
        store.resolve_principal.return_value = actor
        store.resolve_active_target.return_value = target
        outgoing = SimpleNamespace(occurred_at=NOW)
        with patch.object(
            API,
            "_insert_visit_projection",
            side_effect=[(outgoing, True), (SimpleNamespace(occurred_at=NOW), True)],
        ) as insert_projection:
            changed, resolved = API._record_visit(
                object(),
                store=store,
                principal=API._principal(identity),
                target_uid="peer-B",
                operation_id="visit-one",
                occurred_at=NOW,
            )

        self.assertTrue(changed)
        self.assertIs(resolved, target)
        self.assertEqual(insert_projection.call_count, 2)
        outgoing_call, incoming_call = insert_projection.call_args_list
        self.assertEqual(outgoing_call.kwargs["owner_user_id"], actor.user_id)
        self.assertEqual(outgoing_call.kwargs["projection"], "outgoing")
        self.assertEqual(incoming_call.kwargs["owner_user_id"], target.user_id)
        self.assertEqual(incoming_call.kwargs["projection"], "incoming")
        self.assertEqual(incoming_call.kwargs["actor_uid"], "actor-A")
        self.assertEqual(incoming_call.kwargs["target_uid"], "peer-B")

    def test_visit_projection_sql_is_idempotent_on_owner_source_constraint(self) -> None:
        class ScalarRows:
            def __init__(self, value):
                self.value = value

            def first(self):
                return self.value

        class CaptureDB:
            statement = None

            def scalars(self, statement):
                self.statement = statement
                return ScalarRows(SimpleNamespace(occurred_at=NOW))

        db = CaptureDB()
        _event, created = API._insert_visit_projection(
            db,
            owner_user_id=uuid.uuid4(),
            event_key="social:visit-one",
            event_type="social.profile.visit.outgoing",
            actor_uid="actor-A",
            target_uid="peer-B",
            occurred_at=NOW,
            projection="outgoing",
        )
        compiled = db.statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertTrue(created)
        self.assertIn("ON CONFLICT ON CONSTRAINT uq_activity_events_owner_source DO NOTHING", sql)
        self.assertIn("RETURNING", sql)
        self.assertIn("activity_events", sql)


class NativeSocialAuthorityContractTests(unittest.TestCase):
    def test_repository_update_profile_always_writes_authority_markers(self) -> None:
        actor = SocialAccount(
            uuid.uuid4(),
            uuid.uuid4(),
            "actor-A",
            "旧昵称",
            {"city": "福州", "_web_local_fields": ["city"]},
            NOW,
        )
        user = SimpleNamespace(
            display_name="旧昵称",
            profile=dict(actor.profile),
            updated_at=NOW,
        )
        event = SimpleNamespace(occurred_at=NOW, details={})

        class Harness(SqlAlchemyCanonicalSocialStore):
            def __init__(self):
                pass

            def _lock_active_actor(self, _actor):
                return user

            def _claim_operation(self, **_kwargs):
                return event, True

        result = Harness().update_profile(
            actor=actor,
            patch=ProfilePatch(values={"nickname": "新昵称", "signature": "签名"}),
            operation_id="profile-one",
            occurred_at=NOW,
        )

        self.assertTrue(result.changed)
        self.assertEqual(user.display_name, "新昵称")
        self.assertEqual(
            user.profile["_web_local_fields"], ["city", "nickname", "signature"]
        )
        self.assertEqual(user.profile["_web_local_updated_at"], NOW.isoformat())

    def test_replaying_an_old_profile_operation_cannot_move_authority_time_back(self) -> None:
        later = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)
        marked, changed = SqlAlchemyCanonicalSocialStore._mark_web_local_profile_fields(
            {
                "_web_local_fields": ["city"],
                "_web_local_updated_at": later.isoformat(),
            },
            ["nickname"],
            NOW,
        )
        self.assertTrue(changed)
        self.assertEqual(marked["_web_local_fields"], ["city", "nickname"])
        self.assertEqual(marked["_web_local_updated_at"], later.isoformat())

    def test_local_only_module_has_no_banghua_protocol_or_network_import(self) -> None:
        source = (ROOT / "bbw_web" / "native_social_api.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            any(
                name.startswith(
                    ("bbw_protocol", "httpx", "httpcore", "requests", "urllib.request")
                )
                for name in imported
            )
        )
        self.assertNotIn("LegacyBanghuaProvider", source)

    def test_dispatch_documents_origin_gate_owned_by_asgi_caller(self) -> None:
        doc = API.dispatch_social_native.__doc__ or ""
        self.assertIn("SOCIAL_NATIVE_WRITE_PATHS", doc)
        self.assertIn("Origin", doc)
        self.assertIn("Sec-Fetch-Site", doc)


if __name__ == "__main__":
    unittest.main()
