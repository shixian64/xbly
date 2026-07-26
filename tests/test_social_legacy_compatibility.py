from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
import uuid

from bbw_prod.models import Relationship
from bbw_web.social_native.contracts import (
    RELATION_BLOCK,
    RELATION_FOLLOW,
    RELATION_FRIEND,
    RELATION_FRIEND_REQUEST,
    REQUEST_ACCEPTED,
    REQUEST_BLOCKED,
    REQUEST_CANCELLED,
    REQUEST_PENDING,
    SOCIAL_NATIVE_PROVIDER,
    SocialAccount,
)
from bbw_web.social_native.repository import (
    LEGACY_RELATION_BLOCKED_BY,
    LEGACY_RELATION_FOLLOWER,
    SqlAlchemyCanonicalSocialStore,
    is_trusted_legacy_relationship,
    normalized_relationship_state,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _account(uid: str) -> SocialAccount:
    return SocialAccount(uuid.uuid4(), uuid.uuid4(), uid, uid)


def _row(
    owner: SocialAccount,
    subject: SocialAccount,
    *,
    kind: str,
    provider: str = "beibeiwu",
    status: str = "active",
    metadata: dict[str, object] | None = None,
    started_at: datetime = NOW,
) -> Relationship:
    return Relationship(
        id=uuid.uuid4(),
        owner_user_id=owner.user_id,
        provider=provider,
        subject_upstream_uid=subject.upstream_uid,
        kind=kind,
        status=status,
        started_at=started_at,
        ended_at=None if status in {"active", REQUEST_PENDING} else NOW,
        extra_data=dict(metadata or {}),
    )


class _MemoryDb:
    def __init__(self, rows: list[Relationship]) -> None:
        self.rows = rows

    def add(self, row: Relationship) -> None:
        self.rows.append(row)


class _Harness(SqlAlchemyCanonicalSocialStore):
    def __init__(
        self,
        rows: list[Relationship],
        accounts: tuple[SocialAccount, ...] = (),
    ) -> None:
        self.db = _MemoryDb(rows)
        self.accounts = {account.upstream_uid: account for account in accounts}

    @property
    def rows(self) -> list[Relationship]:
        return self.db.rows

    def _relation(self, *, owner_id, subject_uid, kind, lock=True):
        return next(
            (
                row
                for row in reversed(self.rows)
                if row.owner_user_id == owner_id
                and row.provider == SOCIAL_NATIVE_PROVIDER
                and row.subject_upstream_uid == subject_uid
                and row.kind == kind
            ),
            None,
        )

    def _relationship_rows(self, viewer, subject, *, lock=False):
        return [
            row
            for row in self.rows
            if (
                row.owner_user_id == viewer.user_id
                and row.subject_upstream_uid == subject.upstream_uid
            )
            or (
                row.owner_user_id == subject.user_id
                and row.subject_upstream_uid == viewer.upstream_uid
            )
        ]

    def _owner_related_rows(self, owner, *, kinds):
        return [
            row
            for row in self.rows
            if row.kind in kinds
            and (
                row.owner_user_id == owner.user_id
                or row.subject_upstream_uid == owner.upstream_uid
            )
        ]

    def _load_candidate_accounts(self, owner, *, upstream_uids, user_ids):
        return [
            account
            for account in self.accounts.values()
            if account.user_id != owner.user_id
            and (
                account.upstream_uid in upstream_uids
                or account.user_id in user_ids
            )
        ]

    def _lock_active_pair(self, actor, target):
        return {actor.user_id: object(), target.user_id: object()}

    def _claim_operation(self, **kwargs):
        return SimpleNamespace(occurred_at=kwargs["occurred_at"], details={}), True


class LegacyRelationshipResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = _account("actor-A")
        self.target = _account("target-B")

    def test_only_authenticated_legacy_rows_are_migration_evidence(self) -> None:
        trusted_snapshot = _row(
            self.actor,
            self.target,
            kind=RELATION_FOLLOW,
            metadata={"source_path": "/api/social/follows"},
        )
        trusted_server_owned = _row(
            self.actor,
            self.target,
            kind=RELATION_FRIEND,
            metadata={
                "server_owned": True,
                "message_policy_source": "/api/social/friends",
            },
        )
        untrusted = _row(
            self.actor,
            self.target,
            kind=RELATION_FOLLOW,
            metadata={"source_path": "/browser/archive"},
        )
        self.assertTrue(is_trusted_legacy_relationship(trusted_snapshot))
        self.assertTrue(is_trusted_legacy_relationship(trusted_server_owned))
        self.assertFalse(is_trusted_legacy_relationship(untrusted))

    def test_legacy_friend_follow_follower_and_blocks_remain_visible(self) -> None:
        rows = [
            _row(
                self.actor,
                self.target,
                kind=RELATION_FOLLOW,
                metadata={"source_path": "/api/social/follows"},
            ),
            _row(
                self.actor,
                self.target,
                kind=LEGACY_RELATION_FOLLOWER,
                metadata={"source_path": "/api/social/fans"},
            ),
            _row(
                self.actor,
                self.target,
                kind=RELATION_FRIEND,
                metadata={"source_path": "/api/social/friends"},
            ),
            _row(
                self.actor,
                self.target,
                kind=RELATION_BLOCK,
                metadata={
                    "server_owned": True,
                    "message_policy_source": "/api/social/blacklist",
                },
            ),
        ]
        flags = _Harness(rows).relationship_flags(self.actor, self.target)
        self.assertTrue(flags.following)
        self.assertTrue(flags.followed_by)
        self.assertTrue(flags.friend)
        self.assertTrue(flags.blocked)

    def test_local_inactive_row_permanently_overrides_legacy_active_row(self) -> None:
        legacy = _row(
            self.actor,
            self.target,
            kind=RELATION_FOLLOW,
            metadata={"source_path": "/api/social/follows"},
        )
        tombstone = _row(
            self.actor,
            self.target,
            kind=RELATION_FOLLOW,
            provider=SOCIAL_NATIVE_PROVIDER,
            status="inactive",
        )
        flags = _Harness([legacy, tombstone]).relationship_flags(
            self.actor, self.target
        )
        self.assertFalse(flags.following)

    def test_unfollow_localizes_legacy_edge_as_inactive_tombstone(self) -> None:
        legacy_started = NOW - timedelta(days=30)
        legacy = _row(
            self.actor,
            self.target,
            kind=RELATION_FOLLOW,
            metadata={"source_path": "/api/social/follows"},
            started_at=legacy_started,
        )
        store = _Harness([legacy])
        changed = store._set_active_relation(
            owner=self.actor,
            subject=self.target,
            kind=RELATION_FOLLOW,
            active=False,
            occurred_at=NOW,
            reason="unfollow",
        )
        local = store._relation(
            owner_id=self.actor.user_id,
            subject_uid=self.target.upstream_uid,
            kind=RELATION_FOLLOW,
        )
        self.assertTrue(changed)
        self.assertIsNotNone(local)
        self.assertEqual(local.status, "inactive")
        self.assertEqual(local.started_at, legacy_started)
        self.assertEqual(local.extra_data["localized_from_id"], str(legacy.id))
        self.assertFalse(store.relationship_flags(self.actor, self.target).following)

    def test_deleting_one_sided_legacy_friend_writes_both_tombstones(self) -> None:
        legacy = _row(
            self.actor,
            self.target,
            kind=RELATION_FRIEND,
            metadata={"source_path": "/api/social/friends"},
        )
        store = _Harness([legacy])
        result = store.delete_friend(
            actor=self.actor,
            target=self.target,
            operation_id="delete-old-friend",
            occurred_at=NOW,
        )
        local_friend_rows = [
            row
            for row in store.rows
            if row.provider == SOCIAL_NATIVE_PROVIDER
            and row.kind == RELATION_FRIEND
        ]
        self.assertTrue(result.changed)
        self.assertEqual(len(local_friend_rows), 2)
        self.assertTrue(all(row.status == "inactive" for row in local_friend_rows))
        self.assertFalse(store.relationship_flags(self.actor, self.target).friend)

    def test_unblocking_legacy_blacklist_does_not_allow_snapshot_revival(self) -> None:
        legacy = _row(
            self.actor,
            self.target,
            kind=RELATION_BLOCK,
            metadata={
                "server_owned": True,
                "message_policy_source": "/api/social/blacklist",
            },
        )
        store = _Harness([legacy])
        result = store.set_blocked(
            actor=self.actor,
            target=self.target,
            active=False,
            operation_id="unblock-old",
            occurred_at=NOW,
        )
        self.assertTrue(result.changed)
        self.assertFalse(store.relationship_flags(self.actor, self.target).blocked)
        local = store._relation(
            owner_id=self.actor.user_id,
            subject_uid=self.target.upstream_uid,
            kind=RELATION_BLOCK,
        )
        self.assertEqual(local.status, "inactive")

    def test_block_cleans_legacy_friend_follow_and_pending_request(self) -> None:
        rows = [
            _row(
                self.actor,
                self.target,
                kind=RELATION_FRIEND,
                metadata={"source_path": "/api/social/friends"},
            ),
            _row(
                self.actor,
                self.target,
                kind=RELATION_FOLLOW,
                metadata={"source_path": "/api/social/follows"},
            ),
            _row(
                self.actor,
                self.target,
                kind=RELATION_FRIEND_REQUEST,
                metadata={"source_path": "/api/social/friend-apply"},
            ),
        ]
        store = _Harness(rows)
        result = store.set_blocked(
            actor=self.actor,
            target=self.target,
            active=True,
            operation_id="block-old-edges",
            occurred_at=NOW,
        )
        flags = store.relationship_flags(self.actor, self.target)
        self.assertTrue(result.changed)
        self.assertTrue(flags.blocked)
        self.assertFalse(flags.friend)
        self.assertFalse(flags.following)
        self.assertEqual(flags.incoming_friend_request, REQUEST_BLOCKED)

    def test_old_pending_requests_can_be_accepted_or_cancelled(self) -> None:
        incoming = _row(
            self.actor,
            self.target,
            kind=RELATION_FRIEND_REQUEST,
            metadata={"source_path": "/api/social/friend-apply", "message": "你好"},
        )
        incoming_store = _Harness([incoming])
        accepted = incoming_store.resolve_friend_request(
            actor=self.actor,
            requester=self.target,
            resolution=REQUEST_ACCEPTED,
            operation_id="accept-old",
            occurred_at=NOW,
        )
        self.assertTrue(accepted.changed)
        self.assertEqual(accepted.request.state, REQUEST_ACCEPTED)
        self.assertTrue(
            incoming_store.relationship_flags(self.actor, self.target).friend
        )

        outgoing = _row(
            self.actor,
            self.target,
            kind=RELATION_FRIEND_REQUEST,
            metadata={"last_event_type": "api.api.social.add-friend"},
        )
        outgoing_store = _Harness([outgoing])
        cancelled = outgoing_store.cancel_friend_request(
            actor=self.actor,
            target=self.target,
            operation_id="cancel-old",
            occurred_at=NOW,
        )
        self.assertTrue(cancelled.changed)
        self.assertEqual(cancelled.request.state, REQUEST_CANCELLED)
        self.assertEqual(normalized_relationship_state(outgoing), REQUEST_PENDING)

    def test_legacy_rows_are_returned_by_all_local_lists(self) -> None:
        requester = _account("requester-C")
        rows = [
            _row(
                self.actor,
                self.target,
                kind=RELATION_FOLLOW,
                metadata={"source_path": "/api/social/follows"},
            ),
            _row(
                self.actor,
                self.target,
                kind=LEGACY_RELATION_FOLLOWER,
                metadata={"source_path": "/api/social/fans"},
            ),
            _row(
                self.actor,
                self.target,
                kind=RELATION_FRIEND,
                metadata={"source_path": "/api/social/friends"},
            ),
            _row(
                self.actor,
                self.target,
                kind=RELATION_BLOCK,
                metadata={
                    "server_owned": True,
                    "message_policy_source": "/api/social/blacklist",
                },
            ),
            _row(
                self.actor,
                requester,
                kind=RELATION_FRIEND_REQUEST,
                metadata={"source_path": "/api/social/friend-apply"},
            ),
        ]
        store = _Harness(rows, (self.actor, self.target, requester))
        self.assertEqual(
            [item.upstream_uid for item in store.list_relation_accounts(
                owner=self.actor, relation="following", limit=20, offset=0
            )],
            [self.target.upstream_uid],
        )
        self.assertEqual(
            [item.upstream_uid for item in store.list_relation_accounts(
                owner=self.actor, relation="followers", limit=20, offset=0
            )],
            [self.target.upstream_uid],
        )
        self.assertEqual(
            [item.upstream_uid for item in store.list_relation_accounts(
                owner=self.actor, relation="friends", limit=20, offset=0
            )],
            [self.target.upstream_uid],
        )
        self.assertEqual(
            [item[0].upstream_uid for item in store.list_blocks(
                owner=self.actor, direction="outgoing", limit=20, offset=0
            )],
            [self.target.upstream_uid],
        )
        requests = store.list_friend_requests(
            owner=self.actor,
            direction="incoming",
            state=REQUEST_PENDING,
            limit=20,
            offset=0,
        )
        self.assertEqual([item.requester_upstream_uid for item in requests], ["requester-C"])

    def test_legacy_blacklisted_by_mirror_derives_incoming_block(self) -> None:
        mirror = _row(
            self.actor,
            self.target,
            kind=LEGACY_RELATION_BLOCKED_BY,
            metadata={
                "server_owned": True,
                "message_policy_source": "/api/social/blacklist-me",
            },
        )
        flags = _Harness([mirror]).relationship_flags(self.actor, self.target)
        self.assertTrue(flags.blocked_by)


if __name__ == "__main__":
    unittest.main()
