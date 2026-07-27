from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import unittest
import uuid

from sqlalchemy.dialects import postgresql

from bbw_web.social_native import (
    FRIEND_REQUEST_TRANSITIONS,
    FriendRequestStateConflict,
    InvalidNickname,
    InvalidSocialInput,
    LocalSocialService,
    SocialBlocked,
    SocialIdempotencyConflict,
    SocialPrincipal,
    SocialSelfActionForbidden,
    SocialTargetNotMigrated,
    SocialTargetUnavailable,
)
from bbw_web.social_native.contracts import (
    RELATION_BLOCK,
    RELATION_FOLLOW,
    RELATION_FRIEND,
    REQUEST_ACCEPTED,
    REQUEST_BLOCKED,
    REQUEST_CANCELLED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    BlockView,
    FriendRequestMutationResult,
    FriendRequestView,
    ProfileMutationResult,
    RelationshipFlags,
    SocialAccount,
    SocialMutationResult,
)
from bbw_web.social_native.repository import (
    BLOCK_CLEANUP_DIRECTIONS,
    BLOCK_CLEANUP_KINDS,
    SqlAlchemyCanonicalSocialStore,
    active_account_query,
    block_between_query,
    target_binding_query,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _MemorySocialStore:
    """只依赖 contracts 的内存实现，用于验证 service 的产品语义。"""

    def __init__(self) -> None:
        self.accounts = {
            "42": SocialAccount(
                uuid.uuid4(),
                uuid.uuid4(),
                "42",
                "当前用户",
                {"avatar": "/media/me.jpg", "city": "福州"},
                NOW,
            ),
            "9": SocialAccount(
                uuid.uuid4(),
                uuid.uuid4(),
                "9",
                "目标用户",
                {"avatar": "/media/peer.jpg", "signature": "你好"},
                NOW,
            ),
            "10": SocialAccount(uuid.uuid4(), uuid.uuid4(), "10", "已停用", {}, NOW),
        }
        self.active = {"42", "9"}
        self.relations: set[tuple[str, str, str]] = set()
        self.requests: dict[tuple[str, str], FriendRequestView] = {}
        self.operations: dict[tuple[str, str], tuple[tuple[object, ...], object]] = {}

    @staticmethod
    def principal(account: SocialAccount) -> SocialPrincipal:
        return SocialPrincipal(
            account.user_id, account.external_account_id, account.upstream_uid
        )

    def resolve_principal(self, principal: SocialPrincipal) -> SocialAccount | None:
        account = self.accounts.get(principal.upstream_uid)
        if (
            account is None
            or account.upstream_uid not in self.active
            or account.user_id != principal.user_id
            or account.external_account_id != principal.external_account_id
        ):
            return None
        return account

    def resolve_active_target(self, upstream_uid: str, *, provider: str) -> SocialAccount | None:
        del provider
        return self.accounts.get(upstream_uid) if upstream_uid in self.active else None

    def target_binding_exists(
        self, upstream_uid: str, *, provider: str
    ) -> bool:
        del provider
        return upstream_uid in self.accounts

    def resolve_active_targets(self, upstream_uids, *, provider: str):
        del provider
        return {
            uid: self.accounts[uid]
            for uid in upstream_uids
            if uid in self.active and uid in self.accounts
        }

    def relationship_flags(
        self, viewer: SocialAccount, subject: SocialAccount
    ) -> RelationshipFlags:
        outgoing = self.requests.get((viewer.upstream_uid, subject.upstream_uid))
        incoming = self.requests.get((subject.upstream_uid, viewer.upstream_uid))
        return RelationshipFlags(
            following=(viewer.upstream_uid, subject.upstream_uid, RELATION_FOLLOW)
            in self.relations,
            followed_by=(subject.upstream_uid, viewer.upstream_uid, RELATION_FOLLOW)
            in self.relations,
            friend=(viewer.upstream_uid, subject.upstream_uid, RELATION_FRIEND)
            in self.relations
            and (subject.upstream_uid, viewer.upstream_uid, RELATION_FRIEND)
            in self.relations,
            blocked=(viewer.upstream_uid, subject.upstream_uid, RELATION_BLOCK)
            in self.relations,
            blocked_by=(subject.upstream_uid, viewer.upstream_uid, RELATION_BLOCK)
            in self.relations,
            outgoing_friend_request=outgoing.state if outgoing else None,
            incoming_friend_request=incoming.state if incoming else None,
        )

    def _idempotent(self, actor, operation_id, signature, result_factory):
        key = (actor.upstream_uid, operation_id)
        existing = self.operations.get(key)
        if existing is not None:
            if existing[0] != signature:
                raise SocialIdempotencyConflict("operation conflict")
            return replace(existing[1], idempotent_replay=True)
        result = result_factory()
        self.operations[key] = (signature, result)
        return result

    def update_profile(self, *, actor, patch, operation_id, occurred_at):
        signature = ("profile", tuple(sorted(patch.values.items())))

        def mutate():
            current = self.accounts[actor.upstream_uid]
            profile = dict(current.profile)
            display_name = current.display_name
            changed = False
            for key, value in patch.values.items():
                if key == "nickname":
                    changed = changed or display_name != value
                    display_name = value
                    profile["nickname"] = value
                else:
                    changed = changed or profile.get(key) != value
                    profile[key] = value
            updated = replace(
                current,
                display_name=display_name,
                profile=profile,
                updated_at=occurred_at,
            )
            self.accounts[actor.upstream_uid] = updated
            return ProfileMutationResult(updated, changed)

        return self._idempotent(actor, operation_id, signature, mutate)

    def _mutation(self, *, actor, target, action, state, changed, occurred_at, related=()):
        return SocialMutationResult(
            action,
            actor.upstream_uid,
            target.upstream_uid,
            state,
            changed,
            occurred_at,
            related_changes=tuple(related),
        )

    def set_following(self, *, actor, target, active, operation_id, occurred_at):
        signature = ("follow", target.upstream_uid, active)

        def mutate():
            flags = self.relationship_flags(actor, target)
            if active and (flags.blocked or flags.blocked_by):
                raise SocialBlocked("blocked")
            relation = (actor.upstream_uid, target.upstream_uid, RELATION_FOLLOW)
            changed = (relation in self.relations) != active
            if active:
                self.relations.add(relation)
            else:
                self.relations.discard(relation)
            return self._mutation(
                actor=actor,
                target=target,
                action="follow" if active else "unfollow",
                state="active" if active else "inactive",
                changed=changed,
                occurred_at=occurred_at,
            )

        return self._idempotent(actor, operation_id, signature, mutate)

    def create_friend_request(
        self, *, actor, target, message, operation_id, occurred_at
    ):
        signature = ("request", target.upstream_uid, message)

        def mutate():
            flags = self.relationship_flags(actor, target)
            if flags.blocked or flags.blocked_by:
                raise SocialBlocked("blocked")
            key = (actor.upstream_uid, target.upstream_uid)
            previous = self.requests.get(key)
            previous_state = previous.state if previous else None
            if previous_state == REQUEST_PENDING:
                return FriendRequestMutationResult(previous, False)
            if REQUEST_PENDING not in FRIEND_REQUEST_TRANSITIONS.get(
                previous_state, frozenset()
            ):
                raise FriendRequestStateConflict("invalid transition")
            request = FriendRequestView(
                previous.id if previous else uuid.uuid4(),
                actor.upstream_uid,
                target.upstream_uid,
                message,
                REQUEST_PENDING,
                occurred_at,
            )
            self.requests[key] = request
            return FriendRequestMutationResult(request, True)

        return self._idempotent(actor, operation_id, signature, mutate)

    def resolve_friend_request(
        self, *, actor, requester, resolution, operation_id, occurred_at
    ):
        signature = ("resolve", requester.upstream_uid, resolution)

        def mutate():
            key = (requester.upstream_uid, actor.upstream_uid)
            request = self.requests.get(key)
            if request is None:
                raise FriendRequestStateConflict("missing")
            if request.state == resolution:
                return FriendRequestMutationResult(request, False)
            if request.state != REQUEST_PENDING:
                raise FriendRequestStateConflict("invalid transition")
            updated = replace(request, state=resolution, resolved_at=occurred_at)
            self.requests[key] = updated
            if resolution == REQUEST_ACCEPTED:
                self.relations.add(
                    (actor.upstream_uid, requester.upstream_uid, RELATION_FRIEND)
                )
                self.relations.add(
                    (requester.upstream_uid, actor.upstream_uid, RELATION_FRIEND)
                )
                reverse_key = (actor.upstream_uid, requester.upstream_uid)
                reverse = self.requests.get(reverse_key)
                if reverse is not None and reverse.state == REQUEST_PENDING:
                    self.requests[reverse_key] = replace(
                        reverse, state=REQUEST_ACCEPTED, resolved_at=occurred_at
                    )
            return FriendRequestMutationResult(updated, True)

        return self._idempotent(actor, operation_id, signature, mutate)

    def cancel_friend_request(self, *, actor, target, operation_id, occurred_at):
        signature = ("cancel", target.upstream_uid)

        def mutate():
            key = (actor.upstream_uid, target.upstream_uid)
            request = self.requests[key]
            if request.state == REQUEST_CANCELLED:
                return FriendRequestMutationResult(request, False)
            if request.state != REQUEST_PENDING:
                raise FriendRequestStateConflict("invalid transition")
            updated = replace(
                request, state=REQUEST_CANCELLED, resolved_at=occurred_at
            )
            self.requests[key] = updated
            return FriendRequestMutationResult(updated, True)

        return self._idempotent(actor, operation_id, signature, mutate)

    def delete_friend(self, *, actor, target, operation_id, occurred_at):
        signature = ("delete-friend", target.upstream_uid)

        def mutate():
            pairs = {
                (actor.upstream_uid, target.upstream_uid, RELATION_FRIEND),
                (target.upstream_uid, actor.upstream_uid, RELATION_FRIEND),
            }
            changed = bool(self.relations.intersection(pairs))
            self.relations.difference_update(pairs)
            return self._mutation(
                actor=actor,
                target=target,
                action="friend.delete",
                state="inactive",
                changed=changed,
                occurred_at=occurred_at,
            )

        return self._idempotent(actor, operation_id, signature, mutate)

    def set_blocked(self, *, actor, target, active, operation_id, occurred_at):
        signature = ("block", target.upstream_uid, active)

        def mutate():
            block = (actor.upstream_uid, target.upstream_uid, RELATION_BLOCK)
            changed = (block in self.relations) != active
            related = []
            if active:
                self.relations.add(block)
                for kind in (RELATION_FRIEND, RELATION_FOLLOW):
                    for left, right in (
                        (actor.upstream_uid, target.upstream_uid),
                        (target.upstream_uid, actor.upstream_uid),
                    ):
                        relation = (left, right, kind)
                        if relation in self.relations:
                            self.relations.remove(relation)
                            related.append(f"{kind}:{left}-{right}")
                for key in (
                    (actor.upstream_uid, target.upstream_uid),
                    (target.upstream_uid, actor.upstream_uid),
                ):
                    request = self.requests.get(key)
                    if request is not None and request.state == REQUEST_PENDING:
                        self.requests[key] = replace(
                            request, state=REQUEST_BLOCKED, resolved_at=occurred_at
                        )
                        related.append(f"friend_request:{key[0]}-{key[1]}")
            else:
                self.relations.discard(block)
            return self._mutation(
                actor=actor,
                target=target,
                action="block" if active else "unblock",
                state="active" if active else "inactive",
                changed=changed or bool(related),
                occurred_at=occurred_at,
                related=related,
            )

        return self._idempotent(actor, operation_id, signature, mutate)

    def list_relation_accounts(self, *, owner, relation, limit, offset):
        if relation == "following":
            uids = [right for left, right, kind in self.relations if left == owner.upstream_uid and kind == RELATION_FOLLOW]
        elif relation == "followers":
            uids = [left for left, right, kind in self.relations if right == owner.upstream_uid and kind == RELATION_FOLLOW]
        else:
            uids = [right for left, right, kind in self.relations if left == owner.upstream_uid and kind == RELATION_FRIEND]
        return [self.accounts[uid] for uid in sorted(set(uids))[offset : offset + limit]]

    def list_friend_requests(self, *, owner, direction, state, limit, offset):
        rows = [
            request
            for (requester, target), request in self.requests.items()
            if request.state == state
            and (
                (direction == "outgoing" and requester == owner.upstream_uid)
                or (direction == "incoming" and target == owner.upstream_uid)
            )
        ]
        return rows[offset : offset + limit]

    def list_blocks(self, *, owner, direction, limit, offset):
        if direction == "outgoing":
            uids = [right for left, right, kind in self.relations if left == owner.upstream_uid and kind == RELATION_BLOCK]
        else:
            uids = [left for left, right, kind in self.relations if right == owner.upstream_uid and kind == RELATION_BLOCK]
        return [(self.accounts[uid], NOW) for uid in sorted(set(uids))[offset : offset + limit]]


class LocalSocialServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _MemorySocialStore()
        self.service = LocalSocialService(self.store, clock=lambda: NOW)
        self.actor = self.store.accounts["42"]
        self.target = self.store.accounts["9"]
        self.principal = self.store.principal(self.actor)
        self.target_principal = self.store.principal(self.target)

    def test_profile_me_user_users_and_strict_canonical_update(self) -> None:
        me = self.service.get_me(principal=self.principal)
        peer = self.service.get_user(principal=self.principal, upstream_uid="9")
        batch = self.service.get_users(
            principal=self.principal, upstream_uids=["9", "42", "9", "10"]
        )
        self.assertEqual(me.nickname, "当前用户")
        self.assertEqual(peer.nickname, "目标用户")
        self.assertEqual([item.upstream_uid for item in batch], ["9", "42"])

        updated = self.service.update_me(
            principal=self.principal,
            values={
                "nickname": "  Ａlice_01  ",
                "avatar": "/media/new.jpg",
                "signature": "本地资料",
                "gender": "other",
            },
            operation_id="profile-one",
        )
        self.assertTrue(updated.changed)
        self.assertEqual(updated.profile.display_name, "Alice_01")
        self.assertEqual(updated.profile.profile["signature"], "本地资料")
        replay = self.service.update_me(
            principal=self.principal,
            values={
                "nickname": "Ａlice_01",
                "avatar": "/media/new.jpg",
                "signature": "本地资料",
                "gender": "other",
            },
            operation_id="profile-one",
        )
        self.assertTrue(replay.idempotent_replay)

    def test_nickname_avatar_and_unknown_profile_fields_are_rejected(self) -> None:
        for nickname in ("一", "用户🙂", "名字\u200b", "--"):
            with self.subTest(nickname=nickname), self.assertRaises(InvalidNickname):
                self.service.update_me(
                    principal=self.principal,
                    values={"nickname": nickname},
                    operation_id=f"bad-{len(nickname)}-{ord(nickname[0])}",
                )
        with self.assertRaises(InvalidSocialInput):
            self.service.update_me(
                principal=self.principal,
                values={"avatar": "http://example.test/a.jpg"},
                operation_id="bad-avatar",
            )
        with self.assertRaises(InvalidSocialInput):
            self.service.update_me(
                principal=self.principal,
                values={"coin": "999"},
                operation_id="unknown-field",
            )

    def test_follow_is_directional_and_fans_are_reverse_derived(self) -> None:
        first = self.service.follow(
            principal=self.principal,
            target_upstream_uid="9",
            operation_id="follow-one",
        )
        replay = self.service.follow(
            principal=self.principal,
            target_upstream_uid="9",
            operation_id="follow-one",
        )
        actor_view = self.service.get_user(
            principal=self.principal, upstream_uid="9"
        )
        target_view = self.service.get_user(
            principal=self.target_principal, upstream_uid="42"
        )
        self.assertTrue(first.changed)
        self.assertTrue(replay.idempotent_replay)
        self.assertTrue(actor_view.relationship.following)
        self.assertFalse(actor_view.relationship.followed_by)
        self.assertTrue(target_view.relationship.followed_by)
        followers = self.service.list_relationships(
            principal=self.target_principal, relation="followers"
        )
        self.assertEqual([item.upstream_uid for item in followers], ["42"])

        with self.assertRaises(SocialIdempotencyConflict):
            self.service.unfollow(
                principal=self.principal,
                target_upstream_uid="9",
                operation_id="follow-one",
            )

    def test_friend_request_state_machine_accepts_and_deletes_both_sides(self) -> None:
        request = self.service.request_friend(
            principal=self.principal,
            target_upstream_uid="9",
            message="想认识你",
            operation_id="request-one",
        )
        accepted = self.service.resolve_friend_request(
            principal=self.target_principal,
            requester_upstream_uid="42",
            resolution=REQUEST_ACCEPTED,
            operation_id="accept-one",
        )
        self.assertEqual(request.request.state, REQUEST_PENDING)
        self.assertEqual(accepted.request.state, REQUEST_ACCEPTED)
        self.assertTrue(
            self.service.get_user(
                principal=self.principal, upstream_uid="9"
            ).relationship.friend
        )
        deleted = self.service.delete_friend(
            principal=self.principal,
            target_upstream_uid="9",
            operation_id="delete-one",
        )
        self.assertTrue(deleted.changed)
        self.assertFalse(
            self.service.get_user(
                principal=self.target_principal, upstream_uid="42"
            ).relationship.friend
        )
        with self.assertRaises(FriendRequestStateConflict):
            self.service.resolve_friend_request(
                principal=self.target_principal,
                requester_upstream_uid="42",
                resolution=REQUEST_REJECTED,
                operation_id="reject-after-accept",
            )

    def test_block_atomically_clears_both_directions_and_unblock_restores_nothing(self) -> None:
        for kind in (RELATION_FOLLOW, RELATION_FRIEND):
            self.store.relations.add(("42", "9", kind))
            self.store.relations.add(("9", "42", kind))
        self.store.requests[("42", "9")] = FriendRequestView(
            uuid.uuid4(), "42", "9", "A", REQUEST_PENDING, NOW
        )
        self.store.requests[("9", "42")] = FriendRequestView(
            uuid.uuid4(), "9", "42", "B", REQUEST_PENDING, NOW
        )

        blocked = self.service.block(
            principal=self.principal,
            target_upstream_uid="9",
            operation_id="block-one",
        )
        self.assertTrue(blocked.changed)
        self.assertEqual(len(blocked.related_changes), 6)
        self.assertEqual(
            self.store.requests[("42", "9")].state, REQUEST_BLOCKED
        )
        self.assertEqual(
            self.store.requests[("9", "42")].state, REQUEST_BLOCKED
        )
        actor_view = self.service.get_user(
            principal=self.principal, upstream_uid="9"
        )
        target_view = self.service.get_user(
            principal=self.target_principal, upstream_uid="42"
        )
        self.assertTrue(actor_view.relationship.blocked)
        self.assertTrue(target_view.relationship.blocked_by)
        self.assertFalse(actor_view.relationship.friend)
        self.assertFalse(actor_view.relationship.following)
        self.assertFalse(actor_view.relationship.followed_by)

        with self.assertRaises(SocialBlocked):
            self.service.follow(
                principal=self.target_principal,
                target_upstream_uid="42",
                operation_id="blocked-follow",
            )
        self.service.unblock(
            principal=self.principal,
            target_upstream_uid="9",
            operation_id="unblock-one",
        )
        unblocked_view = self.service.get_user(
            principal=self.principal, upstream_uid="9"
        )
        self.assertFalse(unblocked_view.relationship.blocked)
        self.assertFalse(unblocked_view.relationship.friend)
        self.assertFalse(unblocked_view.relationship.following)

    def test_self_and_inactive_targets_are_rejected_before_writes(self) -> None:
        with self.assertRaises(SocialSelfActionForbidden):
            self.service.follow(
                principal=self.principal,
                target_upstream_uid="42",
                operation_id="self",
            )
        with self.assertRaises(SocialTargetUnavailable):
            self.service.follow(
                principal=self.principal,
                target_upstream_uid="10",
                operation_id="inactive",
            )

    def test_profile_read_distinguishes_unmigrated_from_inactive_internally(self) -> None:
        with self.assertRaises(SocialTargetNotMigrated):
            self.service.get_user(
                principal=self.principal,
                upstream_uid="404",
            )
        with self.assertRaises(SocialTargetUnavailable) as inactive:
            self.service.get_user(
                principal=self.principal,
                upstream_uid="10",
            )
        self.assertNotIsInstance(inactive.exception, SocialTargetNotMigrated)


class SocialNativeSqlContractTests(unittest.TestCase):
    def test_active_target_query_requires_status_disabled_and_account_provider(self) -> None:
        compiled = active_account_query(
            upstream_uid="9", provider="beibeiwu"
        ).compile(dialect=postgresql.dialect())
        sql = str(compiled)
        values = {str(value) for value in compiled.params.values()}
        self.assertIn("users.status =", sql)
        self.assertIn("users.disabled_at IS NULL", sql)
        self.assertIn("external_accounts.provider =", sql)
        self.assertIn("external_accounts.upstream_uid =", sql)
        self.assertIn("active", values)
        self.assertIn("beibeiwu", values)
        self.assertIn("9", values)

    def test_target_binding_query_includes_inactive_accounts(self) -> None:
        compiled = target_binding_query(
            upstream_uid="9", provider="beibeiwu"
        ).compile(dialect=postgresql.dialect())
        sql = str(compiled)
        values = {str(value) for value in compiled.params.values()}
        self.assertIn("external_accounts.provider =", sql)
        self.assertIn("external_accounts.upstream_uid =", sql)
        self.assertNotIn("users.status", sql)
        self.assertNotIn("disabled_at", sql)
        self.assertIn("beibeiwu", values)
        self.assertIn("9", values)

    def test_block_query_is_symmetric_but_persists_only_blacklist_rows(self) -> None:
        left = SocialAccount(uuid.uuid4(), uuid.uuid4(), "42", "A")
        right = SocialAccount(uuid.uuid4(), uuid.uuid4(), "9", "B")
        compiled = block_between_query(left, right).compile(
            dialect=postgresql.dialect()
        )
        sql = str(compiled)
        values = {str(value) for value in compiled.params.values()}
        self.assertGreaterEqual(sql.count("relationships.owner_user_id ="), 2)
        self.assertGreaterEqual(
            sql.count("relationships.subject_upstream_uid ="), 2
        )
        self.assertIn("web-local", values)
        self.assertIn("blacklist", values)
        self.assertNotIn("blacklisted_by", values)
        self.assertIn(str(left.user_id), values)
        self.assertIn(str(right.user_id), values)
        self.assertIn("42", values)
        self.assertIn("9", values)

    def test_repository_block_contract_covers_every_destructive_edge(self) -> None:
        self.assertEqual(BLOCK_CLEANUP_KINDS, (RELATION_FRIEND, RELATION_FOLLOW))
        self.assertEqual(
            BLOCK_CLEANUP_DIRECTIONS, ("actor-target", "target-actor")
        )
        source = (ROOT / "bbw_web" / "social_native" / "repository.py").read_text(
            encoding="utf-8"
        )
        method = source.split("    def set_blocked(", 1)[1].split(
            "    def list_relation_accounts(", 1
        )[0]
        self.assertIn("for kind in BLOCK_CLEANUP_KINDS", method)
        self.assertIn("_block_pending_request", method)
        self.assertIn("if active:", method)
        self.assertNotIn("commit(", method)
        self.assertNotIn("rollback(", method)

    def test_repository_has_no_network_or_legacy_protocol_imports(self) -> None:
        source = (ROOT / "bbw_web" / "social_native" / "repository.py").read_text(
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
                name.startswith(("bbw_protocol", "httpx", "requests"))
                for name in imported
            )
        )

    def test_friend_request_state_machine_is_closed_and_explicit(self) -> None:
        self.assertEqual(FRIEND_REQUEST_TRANSITIONS[None], {REQUEST_PENDING})
        self.assertEqual(
            FRIEND_REQUEST_TRANSITIONS[REQUEST_PENDING],
            {
                REQUEST_ACCEPTED,
                REQUEST_REJECTED,
                REQUEST_CANCELLED,
                REQUEST_BLOCKED,
            },
        )
        for terminal in (
            REQUEST_ACCEPTED,
            REQUEST_REJECTED,
            REQUEST_CANCELLED,
            REQUEST_BLOCKED,
        ):
            self.assertEqual(FRIEND_REQUEST_TRANSITIONS[terminal], {REQUEST_PENDING})

    def test_repository_exposes_all_atomic_store_methods(self) -> None:
        for method in (
            "target_binding_exists",
            "update_profile",
            "set_following",
            "create_friend_request",
            "resolve_friend_request",
            "cancel_friend_request",
            "delete_friend",
            "set_blocked",
        ):
            self.assertTrue(callable(getattr(SqlAlchemyCanonicalSocialStore, method)))


if __name__ == "__main__":
    unittest.main()
