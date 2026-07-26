from __future__ import annotations

import io
import json
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from bbw_prod import migration_readiness


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
VALID_ARGON2ID_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=2$hgSFYbeBDzhtzVj32n3c+w$"
    "CF/Zm+UUweR/dpAqTcnhqiaeZrOI2lyMu7IlZwSWNv4"
)


def account_row(
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    uid: str,
    credential: bool = True,
    disabled: bool = False,
    status: str = "active",
    credential_hash: str = VALID_ARGON2ID_HASH,
) -> object:
    return SimpleNamespace(
        user_id=user_id,
        user_status=status,
        external_account_id=account_id,
        upstream_uid=uid,
        password_encrypted={"ciphertext": "not-output"},
        credential_id=uuid.uuid4() if credential else None,
        credential_disabled_at=NOW if disabled else None,
        credential_password_hash=credential_hash if credential else None,
    )


def snapshot_cursor(
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    uid: str,
    kind: str,
    valid: bool = True,
) -> object:
    marker = {
        "complete": True,
        "external_account_id": str(account_id),
        "kind": kind,
        "schema": migration_readiness.SNAPSHOT_SCHEMA,
        "source_path": migration_readiness.SNAPSHOT_PATHS[kind],
        "upstream_uid": uid if valid else "wrong-uid",
    }
    return SimpleNamespace(
        owner_user_id=user_id,
        source="beibeiwu",
        stream=migration_readiness.SNAPSHOT_STREAMS[kind],
        cursor=json.dumps(marker),
        watermark_at=NOW,
        last_succeeded_at=NOW,
        last_error=None,
    )


def message_peer_cursor(
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    uid: str,
    peers: tuple[str, ...] = (),
    valid: bool = True,
) -> object:
    marker = {
        "complete": True,
        "external_account_id": str(account_id),
        "peer_count": len(set(peers)),
        "peer_digest": migration_readiness._message_peer_snapshot_digest(peers),
        "schema": migration_readiness.MESSAGE_PEER_SNAPSHOT_SCHEMA,
        "source_path": migration_readiness.MESSAGE_PEER_SNAPSHOT_PATH,
        "upstream_uid": uid if valid else "wrong-uid",
    }
    return SimpleNamespace(
        owner_user_id=user_id,
        source="beibeiwu",
        stream=migration_readiness.MESSAGE_PEER_SNAPSHOT_STREAM,
        cursor=json.dumps(marker),
        watermark_at=NOW,
        last_succeeded_at=NOW,
        last_error=None,
    )


def domain_cursor(
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    uid: str,
    domain: str,
    valid: bool = True,
    peers: tuple[str, ...] = (),
) -> object:
    scopes = list(migration_readiness.DOMAIN_MARKER_SCOPES[domain])
    marker = {
        "complete": True,
        "source_complete": True,
        "schema": migration_readiness.DOMAIN_MARKER_SCHEMA,
        "domain": domain,
        "owner_user_id": str(user_id),
        "external_account_id": str(account_id),
        "upstream_uid": uid if valid else "wrong-uid",
        "scopes": scopes,
        "counts": {scope: 0 for scope in scopes},
        "source_paths": [f"legacy://{domain}"],
        "record_digest": migration_readiness._message_peer_snapshot_digest(()),
        "coverage_started_at": NOW.isoformat(),
        "coverage_ended_at": NOW.isoformat(),
        "unresolved_records": 0,
    }
    if domain == "social":
        marker["source_paths"] = list(
            migration_readiness.SOCIAL_MARKER_SOURCE_PATHS
        )
        scope_paths = {
            "profile": ["/api/profile/user"],
            "relationships": [
                "/api/social/follows",
                "/api/social/fans",
                "/api/social/friends",
            ],
            "friend-requests": ["/api/social/friend-apply"],
            "blocklists": [
                "/api/social/blacklist",
                "/api/social/blacklist-me",
            ],
        }
        marker["coverage"] = {
            scope: {
                "source_complete": True,
                "records": 1 if scope == "profile" else 0,
                "source_paths": paths,
            }
            for scope, paths in scope_paths.items()
        }
        marker["counts"]["profile"] = 1
        marker["watermarks"] = {
            path: {
                "source_complete": True,
                "pages": 1,
                "last_page": 1,
                "records": 1 if path == "/api/profile/user" else 0,
                "terminal": (
                    "empty-page"
                    if path
                    in {
                        "/api/social/follows",
                        "/api/social/fans",
                        "/api/social/friend-apply",
                    }
                    else "single-response"
                ),
            }
            for path in migration_readiness.SOCIAL_MARKER_SOURCE_PATHS
        }
    if domain == "moments":
        marker["history_days"] = migration_readiness.MOMENTS_HISTORY_DAYS
        marker["phase"] = "complete"
        marker["source_paths"] = list(
            migration_readiness.MOMENTS_MARKER_SOURCE_PATHS
        )
    if domain == "discovery":
        marker["counts"]["profile"] = 1
        marker["source_paths"] = list(
            migration_readiness.DISCOVERY_MARKER_SOURCE_PATHS
        )
        marker["profile_source_marker"] = (
            migration_readiness.DISCOVERY_PROFILE_SOURCE_MARKER
        )
    if domain == "media":
        peer_digest = migration_readiness._message_peer_snapshot_digest(peers)
        directions = len(set(peers)) * 2
        marker["phase"] = "complete"
        marker["source_paths"] = list(
            migration_readiness.MEDIA_MARKER_SOURCE_PATHS
        )
        marker["history"] = {
            "complete": True,
            "directions": directions,
            "messages": 0,
            "pages": directions,
            "record_digest": peer_digest,
        }
        marker["prerequisites"] = {
            "message_peer": peer_digest,
            "moments": migration_readiness._message_peer_snapshot_digest(()),
            "social": migration_readiness._message_peer_snapshot_digest(()),
        }
    return SimpleNamespace(
        owner_user_id=user_id,
        source="beibeiwu",
        stream=migration_readiness.DOMAIN_MARKER_STREAMS[domain],
        cursor=json.dumps(marker),
        watermark_at=NOW,
        last_succeeded_at=NOW,
        last_error=None,
    )


def all_domain_cursors(
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    uid: str,
    peers: tuple[str, ...] = (),
) -> list[object]:
    return [
        domain_cursor(
            user_id=user_id,
            account_id=account_id,
            uid=uid,
            domain=domain,
            peers=peers,
        )
        for domain in migration_readiness.DOMAIN_MARKER_STREAMS
    ]


def relationship_row(
    *,
    user_id: uuid.UUID,
    peer_uid: str,
    kind: str = "blacklist",
    trusted: bool = True,
) -> object:
    return SimpleNamespace(
        owner_user_id=user_id,
        provider="beibeiwu",
        subject_upstream_uid=peer_uid,
        kind=kind,
        relationship_metadata={
            "server_owned": trusted,
            "message_policy_source": migration_readiness.SNAPSHOT_PATHS[kind],
        },
    )


def message_peer_relationship_row(
    *,
    user_id: uuid.UUID,
    peer_uid: str,
    trusted: bool = True,
) -> object:
    return SimpleNamespace(
        owner_user_id=user_id,
        provider=migration_readiness.MESSAGE_POLICY_PROVIDER,
        subject_upstream_uid=peer_uid,
        kind=migration_readiness.MESSAGE_POLICY_CONVERSATION_KIND,
        relationship_metadata={
            "server_owned": trusted,
            "upstream_conversation_snapshot": True,
            "upstream_conversation_source_path": (
                migration_readiness.MESSAGE_PEER_SNAPSHOT_PATH
            ),
        },
    )


class MigrationReadinessEvaluationTests(unittest.TestCase):
    def test_only_active_accounts_with_credentials_uid_and_two_markers_are_ready(self) -> None:
        ready_user = uuid.uuid4()
        ready_account = uuid.uuid4()
        missing_credential_user = uuid.uuid4()
        missing_credential_account = uuid.uuid4()
        invalid_snapshot_user = uuid.uuid4()
        invalid_snapshot_account = uuid.uuid4()
        rows = [
            account_row(
                user_id=ready_user,
                account_id=ready_account,
                uid="42",
            ),
            account_row(
                user_id=missing_credential_user,
                account_id=missing_credential_account,
                uid="43",
                credential=False,
            ),
            account_row(
                user_id=invalid_snapshot_user,
                account_id=invalid_snapshot_account,
                uid="44",
            ),
            account_row(
                user_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                uid="45",
                status="suspended",
            ),
        ]
        cursors = [
            snapshot_cursor(
                user_id=ready_user,
                account_id=ready_account,
                uid="42",
                kind=kind,
            )
            for kind in migration_readiness.SNAPSHOT_KINDS
        ]
        cursors.append(
            message_peer_cursor(
                user_id=ready_user,
                account_id=ready_account,
                uid="42",
            )
        )
        cursors.extend(
            all_domain_cursors(
                user_id=ready_user,
                account_id=ready_account,
                uid="42",
            )
        )
        cursors.extend(
            snapshot_cursor(
                user_id=invalid_snapshot_user,
                account_id=invalid_snapshot_account,
                uid="44",
                kind=kind,
                valid=kind != "blacklisted_by",
            )
            for kind in migration_readiness.SNAPSHOT_KINDS
        )

        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=cursors,
        )

        self.assertEqual(report.provider_accounts, 4)
        self.assertEqual(report.active_accounts, 3)
        self.assertEqual(report.local_login_ready_accounts, 2)
        self.assertEqual(report.local_message_identity_ready_accounts, 2)
        self.assertEqual(report.message_peer_snapshot_ready_accounts, 1)
        self.assertEqual(report.fully_ready_accounts, 1)
        self.assertEqual(report.local_core_ready_accounts, 1)
        self.assertEqual(report.all_domains_ready_accounts, 1)
        self.assertEqual(report.missing_credentials, 1)
        self.assertEqual(report.backfill_candidates, 1)
        self.assertEqual(report.invalid_block_snapshot_accounts, 1)
        self.assertFalse(report.ready)

    def test_duplicate_uid_blocks_all_affected_accounts(self) -> None:
        rows = [
            account_row(
                user_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                uid="duplicate",
            )
            for _ in range(2)
        ]

        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=[],
        )

        self.assertEqual(report.duplicate_uid_values, 1)
        self.assertEqual(report.duplicate_uid_accounts, 2)
        self.assertEqual(report.local_message_identity_ready_accounts, 0)
        self.assertFalse(report.ready)

    def test_duplicate_external_account_binding_blocks_affected_user(self) -> None:
        user_id = uuid.uuid4()
        rows = [
            account_row(
                user_id=user_id,
                account_id=uuid.uuid4(),
                uid=uid,
            )
            for uid in ("42", "43")
        ]

        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=[],
        )

        self.assertEqual(report.duplicate_user_bindings, 1)
        self.assertEqual(report.duplicate_user_binding_accounts, 2)
        self.assertEqual(report.local_login_ready_accounts, 0)
        self.assertFalse(report.ready)

    def test_invalid_hash_or_untrusted_relationship_cannot_pass_cutover_gate(self) -> None:
        invalid_hash_user = uuid.uuid4()
        relationship_user = uuid.uuid4()
        relationship_account = uuid.uuid4()
        rows = [
            account_row(
                user_id=invalid_hash_user,
                account_id=uuid.uuid4(),
                uid="51",
                credential_hash="not-an-argon2-hash",
            ),
            account_row(
                user_id=relationship_user,
                account_id=relationship_account,
                uid="52",
            ),
        ]
        cursors = [
            snapshot_cursor(
                user_id=relationship_user,
                account_id=relationship_account,
                uid="52",
                kind=kind,
            )
            for kind in migration_readiness.SNAPSHOT_KINDS
        ]

        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=cursors,
            relationship_rows=[
                relationship_row(
                    user_id=relationship_user,
                    peer_uid="99",
                    trusted=False,
                )
            ],
        )

        self.assertEqual(report.enabled_credentials, 2)
        self.assertEqual(report.invalid_credentials, 1)
        self.assertEqual(report.local_login_ready_accounts, 1)
        self.assertEqual(report.invalid_block_snapshot_accounts, 1)
        self.assertEqual(report.invalid_block_relationship_accounts, 1)
        self.assertFalse(report.ready)

    def test_resource_exhausting_argon2_parameters_fail_readiness(self) -> None:
        unsafe_hash = VALID_ARGON2ID_HASH.replace("m=65536,t=3", "m=1048576,t=3")
        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=[
                account_row(
                    user_id=uuid.uuid4(),
                    account_id=uuid.uuid4(),
                    uid="61",
                    credential_hash=unsafe_hash,
                )
            ],
            cursor_rows=[],
        )

        self.assertEqual(report.invalid_credentials, 1)
        self.assertEqual(report.local_login_ready_accounts, 0)
        self.assertFalse(report.ready)

    def test_trusted_conversation_snapshot_is_required_and_digest_checked(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        rows = [
            account_row(
                user_id=user_id,
                account_id=account_id,
                uid="71",
            )
        ]
        block_cursors = [
            snapshot_cursor(
                user_id=user_id,
                account_id=account_id,
                uid="71",
                kind=kind,
            )
            for kind in migration_readiness.SNAPSHOT_KINDS
        ]

        missing = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=block_cursors,
        )
        self.assertEqual(missing.block_snapshot_ready_accounts, 1)
        self.assertEqual(missing.missing_message_peer_snapshot_accounts, 1)
        self.assertEqual(missing.local_core_ready_accounts, 0)
        self.assertEqual(missing.fully_ready_accounts, 0)
        self.assertFalse(missing.ready)

        peers = ("72", "73")
        ready_cursors = [
            *block_cursors,
            message_peer_cursor(
                user_id=user_id,
                account_id=account_id,
                uid="71",
                peers=peers,
            ),
            *all_domain_cursors(
                user_id=user_id,
                account_id=account_id,
                uid="71",
                peers=peers,
            ),
        ]
        ready_relationships = [
            message_peer_relationship_row(user_id=user_id, peer_uid=peer)
            for peer in peers
        ]
        unchecked = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=ready_cursors,
            relationship_rows=ready_relationships,
        )
        self.assertEqual(unchecked.cutover_ready_accounts, 1)
        self.assertFalse(unchecked.r2_storage_ready)
        self.assertEqual(
            unchecked.r2_capability_error_code,
            migration_readiness.R2_CAPABILITY_ERROR_NOT_CHECKED,
        )
        self.assertFalse(unchecked.ready)

        read_failed = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=ready_cursors,
            relationship_rows=ready_relationships,
            r2_capability=migration_readiness.R2CapabilityResult(
                write_ready=True,
                read_ready=False,
                delete_ready=True,
                error_code=migration_readiness.R2_CAPABILITY_ERROR_GET,
            ),
        )
        self.assertEqual(read_failed.cutover_ready_accounts, 1)
        self.assertTrue(read_failed.local_core_ready)
        self.assertTrue(read_failed.domain_data_ready)
        self.assertFalse(read_failed.r2_storage_ready)
        self.assertFalse(read_failed.cutover_ready)
        self.assertFalse(read_failed.ready)

        ready = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=ready_cursors,
            relationship_rows=ready_relationships,
            r2_capability=migration_readiness.R2_CAPABILITY_READY,
        )
        self.assertEqual(ready.message_peer_snapshot_ready_accounts, 1)
        self.assertEqual(ready.local_core_ready_accounts, 1)
        self.assertEqual(ready.all_domains_ready_accounts, 1)
        self.assertEqual(ready.fully_ready_accounts, 1)
        self.assertTrue(ready.local_core_ready)
        self.assertTrue(ready.domain_data_ready)
        self.assertTrue(ready.r2_write_ready)
        self.assertTrue(ready.r2_read_ready)
        self.assertTrue(ready.r2_delete_ready)
        self.assertTrue(ready.r2_storage_ready)
        self.assertIsNone(ready.r2_capability_error_code)
        self.assertTrue(ready.cutover_ready)
        self.assertTrue(ready.ready)

        tampered = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=[
                *block_cursors,
                message_peer_cursor(
                    user_id=user_id,
                    account_id=account_id,
                    uid="71",
                    peers=peers,
                ),
                *all_domain_cursors(
                    user_id=user_id,
                    account_id=account_id,
                    uid="71",
                    peers=peers,
                ),
            ],
            relationship_rows=[
                message_peer_relationship_row(user_id=user_id, peer_uid="72")
            ],
        )
        self.assertEqual(tampered.invalid_message_peer_snapshot_accounts, 1)
        self.assertEqual(
            tampered.invalid_message_peer_relationship_accounts,
            1,
        )
        self.assertFalse(tampered.ready)

    def test_domain_markers_and_compatibility_outbox_are_independent_cutover_gates(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        rows = [account_row(user_id=user_id, account_id=account_id, uid="81")]
        core_cursors = [
            *(
                snapshot_cursor(
                    user_id=user_id,
                    account_id=account_id,
                    uid="81",
                    kind=kind,
                )
                for kind in migration_readiness.SNAPSHOT_KINDS
            ),
            message_peer_cursor(
                user_id=user_id,
                account_id=account_id,
                uid="81",
            ),
        ]

        missing_domains = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=core_cursors,
        )
        self.assertTrue(missing_domains.local_core_ready)
        self.assertFalse(missing_domains.domain_data_ready)
        self.assertEqual(missing_domains.missing_social_marker_accounts, 1)
        self.assertEqual(missing_domains.missing_moments_marker_accounts, 1)
        self.assertEqual(missing_domains.fully_ready_accounts, 0)

        blocked_by_outbox = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=rows,
            cursor_rows=[
                *core_cursors,
                *all_domain_cursors(
                    user_id=user_id,
                    account_id=account_id,
                    uid="81",
                ),
            ],
            outbox_rows=[
                SimpleNamespace(status="retry", item_count=2, oldest_at=NOW),
                SimpleNamespace(status="failed", item_count=1, oldest_at=NOW),
                SimpleNamespace(status="completed", item_count=9, oldest_at=NOW),
            ],
        )
        self.assertTrue(blocked_by_outbox.local_core_ready)
        self.assertTrue(blocked_by_outbox.domain_data_ready)
        self.assertEqual(blocked_by_outbox.cutover_ready_accounts, 1)
        self.assertEqual(blocked_by_outbox.compatibility_outbox_total, 12)
        self.assertEqual(blocked_by_outbox.compatibility_outbox_unfinished, 3)
        self.assertFalse(blocked_by_outbox.compatibility_outbox_ready)
        self.assertFalse(blocked_by_outbox.cutover_ready)
        self.assertFalse(blocked_by_outbox.ready)

    def test_unfinished_tim_deliveries_block_only_final_cutover(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        uid = "82"
        cursors = [
            *(
                snapshot_cursor(
                    user_id=user_id,
                    account_id=account_id,
                    uid=uid,
                    kind=kind,
                )
                for kind in migration_readiness.SNAPSHOT_KINDS
            ),
            message_peer_cursor(
                user_id=user_id,
                account_id=account_id,
                uid=uid,
            ),
            *all_domain_cursors(
                user_id=user_id,
                account_id=account_id,
                uid=uid,
            ),
        ]

        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=[
                account_row(user_id=user_id, account_id=account_id, uid=uid)
            ],
            cursor_rows=cursors,
            tim_delivery_rows=[
                SimpleNamespace(status="pending", item_count=2, oldest_at=NOW),
                SimpleNamespace(status="retry", item_count=3, oldest_at=NOW),
                SimpleNamespace(status="processing", item_count=5, oldest_at=NOW),
                SimpleNamespace(status="failed", item_count=7, oldest_at=NOW),
                SimpleNamespace(status="mystery", item_count=11, oldest_at=NOW),
                SimpleNamespace(status="delivered", item_count=13, oldest_at=NOW),
                SimpleNamespace(status="cancelled", item_count=17, oldest_at=NOW),
            ],
        )

        self.assertTrue(report.local_core_ready)
        self.assertTrue(report.domain_data_ready)
        self.assertTrue(report.compatibility_outbox_ready)
        self.assertEqual(report.tim_delivery_total, 58)
        self.assertEqual(report.tim_delivery_pending, 2)
        self.assertEqual(report.tim_delivery_retry, 3)
        self.assertEqual(report.tim_delivery_processing, 5)
        self.assertEqual(report.tim_delivery_failed, 7)
        self.assertEqual(report.tim_delivery_other_unfinished, 11)
        self.assertEqual(report.tim_delivery_unfinished, 28)
        self.assertEqual(report.tim_delivery_oldest_unfinished_at, NOW.isoformat())
        self.assertFalse(report.tim_delivery_ready)
        self.assertFalse(report.cutover_ready)
        self.assertFalse(report.ready)

    def test_inspection_queries_all_tim_channel_delivery_statuses(self) -> None:
        statements = []

        class ReadOnlyDb:
            def execute(self, statement):
                statements.append(statement)
                return []

        report = migration_readiness.inspect_readiness(ReadOnlyDb())

        rendered = [str(statement) for statement in statements]
        self.assertTrue(any("operation_outbox" in sql for sql in rendered))
        self.assertTrue(
            any(
                "message_deliveries" in sql and "message_deliveries.channel" in sql
                for sql in rendered
            )
        )
        self.assertEqual(report.tim_delivery_total, 0)
        self.assertTrue(report.tim_delivery_ready)
        self.assertFalse(report.ready)

    def test_invalid_domain_marker_cannot_claim_history_coverage(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        invalid = domain_cursor(
            user_id=user_id,
            account_id=account_id,
            uid="91",
            domain="moments",
        )
        marker = json.loads(invalid.cursor)
        marker["counts"]["comments"] = -1
        invalid.cursor = json.dumps(marker)
        report = migration_readiness.evaluate_readiness(
            provider="beibeiwu",
            account_rows=[account_row(user_id=user_id, account_id=account_id, uid="91")],
            cursor_rows=[invalid],
        )
        self.assertEqual(report.invalid_moments_marker_accounts, 1)
        self.assertEqual(report.moments_ready_accounts, 0)
        self.assertFalse(report.domain_data_ready)

    def test_moments_and_discovery_markers_require_importer_contracts(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        cases = (
            (
                "moments",
                lambda marker: marker.update({"phase": "comments"}),
                "invalid_moments_marker_accounts",
            ),
            (
                "moments",
                lambda marker: marker.update(
                    {"source_paths": ["legacy://moments"]}
                ),
                "invalid_moments_marker_accounts",
            ),
            (
                "discovery",
                lambda marker: marker.update(
                    {"source_paths": ["legacy://discovery"]}
                ),
                "invalid_discovery_marker_accounts",
            ),
            (
                "discovery",
                lambda marker: marker["counts"].update({"profile": 0}),
                "invalid_discovery_marker_accounts",
            ),
            (
                "discovery",
                lambda marker: marker.update(
                    {"profile_source_marker": "untrusted"}
                ),
                "invalid_discovery_marker_accounts",
            ),
        )

        for domain, mutate, error_field in cases:
            with self.subTest(domain=domain, mutation=mutate):
                cursors = [
                    message_peer_cursor(
                        user_id=user_id,
                        account_id=account_id,
                        uid="93",
                    ),
                    *all_domain_cursors(
                        user_id=user_id,
                        account_id=account_id,
                        uid="93",
                    ),
                ]
                target = next(
                    cursor
                    for cursor in cursors
                    if cursor.stream
                    == migration_readiness.DOMAIN_MARKER_STREAMS[domain]
                )
                marker = json.loads(target.cursor)
                mutate(marker)
                target.cursor = json.dumps(marker)

                report = migration_readiness.evaluate_readiness(
                    provider="beibeiwu",
                    account_rows=[
                        account_row(
                            user_id=user_id,
                            account_id=account_id,
                            uid="93",
                        )
                    ],
                    cursor_rows=cursors,
                )

                self.assertEqual(getattr(report, error_field), 1)
                self.assertFalse(report.domain_data_ready)

    def test_media_marker_is_bound_to_strict_history_and_prerequisites(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        cases = (
            lambda marker: marker.update({"phase": "history"}),
            lambda marker: marker.update({"owner_user_id": str(uuid.uuid4())}),
            lambda marker: marker.update({"source_paths": ["r2://private-media"]}),
            lambda marker: marker["history"].update({"complete": False}),
            lambda marker: marker["history"].update({"pages": 1}),
            lambda marker: marker["history"].update({"directions": 2}),
            lambda marker: marker["history"].update({"record_digest": "bad"}),
            lambda marker: marker["prerequisites"].update({"social": "f" * 64}),
        )

        for mutate in cases:
            with self.subTest(mutation=mutate):
                cursors = [
                    message_peer_cursor(
                        user_id=user_id,
                        account_id=account_id,
                        uid="94",
                    ),
                    *all_domain_cursors(
                        user_id=user_id,
                        account_id=account_id,
                        uid="94",
                    ),
                ]
                media_cursor = next(
                    cursor
                    for cursor in cursors
                    if cursor.stream
                    == migration_readiness.DOMAIN_MARKER_STREAMS["media"]
                )
                marker = json.loads(media_cursor.cursor)
                mutate(marker)
                media_cursor.cursor = json.dumps(marker)

                report = migration_readiness.evaluate_readiness(
                    provider="beibeiwu",
                    account_rows=[
                        account_row(
                            user_id=user_id,
                            account_id=account_id,
                            uid="94",
                        )
                    ],
                    cursor_rows=cursors,
                )

                self.assertEqual(report.invalid_media_marker_accounts, 1)
                self.assertEqual(report.media_ready_accounts, 0)
                self.assertFalse(report.domain_data_ready)

    def test_social_marker_identity_coverage_and_pagination_are_fail_closed(self) -> None:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        mutations = (
            lambda marker: marker.update({"owner_user_id": str(uuid.uuid4())}),
            lambda marker: marker["coverage"]["relationships"].update(
                {"source_paths": ["/api/social/friends"]}
            ),
            lambda marker: marker["watermarks"][
                "/api/social/follows"
            ].update({"pages": 2, "last_page": 1}),
            lambda marker: marker["watermarks"][
                "/api/social/follows"
            ].update({"terminal": "single-response"}),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                cursor = domain_cursor(
                    user_id=user_id,
                    account_id=account_id,
                    uid="92",
                    domain="social",
                )
                marker = json.loads(cursor.cursor)
                mutate(marker)
                cursor.cursor = json.dumps(marker)

                report = migration_readiness.evaluate_readiness(
                    provider="beibeiwu",
                    account_rows=[
                        account_row(
                            user_id=user_id,
                            account_id=account_id,
                            uid="92",
                        )
                    ],
                    cursor_rows=[cursor],
                )

                self.assertEqual(report.invalid_social_marker_accounts, 1)
                self.assertEqual(report.social_ready_accounts, 0)
                self.assertFalse(report.domain_data_ready)


class MigrationReadinessCliTests(unittest.TestCase):
    def test_cli_keeps_database_read_only_and_emits_aggregate_json_only(self) -> None:
        report = migration_readiness.MigrationReadinessReport(
            provider="beibeiwu",
            provider_accounts=1,
            active_accounts=1,
            inactive_accounts=0,
            enabled_credentials=1,
            local_login_ready_accounts=1,
            local_message_identity_ready_accounts=1,
            block_snapshot_ready_accounts=1,
            message_peer_snapshot_ready_accounts=1,
            local_core_ready_accounts=1,
            social_ready_accounts=1,
            moments_ready_accounts=1,
            discovery_ready_accounts=1,
            media_ready_accounts=1,
            all_domains_ready_accounts=1,
            cutover_ready_accounts=1,
            fully_ready_accounts=1,
            missing_uid_accounts=0,
            duplicate_user_bindings=0,
            duplicate_user_binding_accounts=0,
            duplicate_uid_values=0,
            duplicate_uid_accounts=0,
            missing_credentials=0,
            disabled_credentials=0,
            invalid_credentials=0,
            backfill_candidates=0,
            missing_block_snapshot_accounts=0,
            invalid_block_snapshot_accounts=0,
            invalid_block_relationship_accounts=0,
            missing_message_peer_snapshot_accounts=0,
            invalid_message_peer_snapshot_accounts=0,
            invalid_message_peer_relationship_accounts=0,
            missing_social_marker_accounts=0,
            invalid_social_marker_accounts=0,
            missing_moments_marker_accounts=0,
            invalid_moments_marker_accounts=0,
            missing_discovery_marker_accounts=0,
            invalid_discovery_marker_accounts=0,
            missing_media_marker_accounts=0,
            invalid_media_marker_accounts=0,
            compatibility_outbox_total=0,
            compatibility_outbox_pending=0,
            compatibility_outbox_retry=0,
            compatibility_outbox_processing=0,
            compatibility_outbox_failed=0,
            compatibility_outbox_other_unfinished=0,
            compatibility_outbox_unfinished=0,
            compatibility_outbox_oldest_unfinished_at=None,
            media_archive_outbox_total=0,
            media_archive_outbox_pending=0,
            media_archive_outbox_retry=0,
            media_archive_outbox_processing=0,
            media_archive_outbox_failed=0,
            media_archive_outbox_other_unfinished=0,
            media_archive_outbox_unfinished=0,
            media_archive_outbox_oldest_unfinished_at=None,
            tim_delivery_total=0,
            tim_delivery_pending=0,
            tim_delivery_retry=0,
            tim_delivery_processing=0,
            tim_delivery_failed=0,
            tim_delivery_other_unfinished=0,
            tim_delivery_unfinished=0,
            tim_delivery_oldest_unfinished_at=None,
            local_core_ready=True,
            domain_data_ready=True,
            compatibility_outbox_ready=True,
            media_archive_outbox_ready=True,
            tim_delivery_ready=True,
            r2_write_ready=True,
            r2_read_ready=True,
            r2_delete_ready=True,
            r2_storage_ready=True,
            r2_capability_error_code=None,
            cutover_ready=True,
            ready=True,
        )

        class ReadOnlyDb:
            def add(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("readiness CLI must not write")

            def flush(self) -> None:
                raise AssertionError("readiness CLI must not write")

        @contextmanager
        def fake_session_scope():
            yield ReadOnlyDb()

        output = io.StringIO()
        with patch.object(
            migration_readiness, "session_scope", fake_session_scope
        ), patch.object(
            migration_readiness, "inspect_readiness", return_value=report
        ):
            exit_code = migration_readiness.main(
                ["--require-ready"],
                stdout=output,
                r2_probe=lambda: migration_readiness.R2_CAPABILITY_READY,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ready"])
        self.assertNotIn("user_id", payload)
        self.assertNotIn("upstream_uid", payload)
        self.assertNotIn("ciphertext", output.getvalue())

        failed_capability = migration_readiness.R2CapabilityResult(
            write_ready=True,
            read_ready=False,
            delete_ready=True,
            error_code=migration_readiness.R2_CAPABILITY_ERROR_GET,
        )
        blocked_report = replace(
            report,
            r2_read_ready=False,
            r2_storage_ready=False,
            r2_capability_error_code=migration_readiness.R2_CAPABILITY_ERROR_GET,
            cutover_ready=False,
            ready=False,
        )
        blocked_output = io.StringIO()
        with patch.object(
            migration_readiness, "session_scope", fake_session_scope
        ), patch.object(
            migration_readiness, "inspect_readiness", return_value=blocked_report
        ) as inspect_mock:
            blocked_exit_code = migration_readiness.main(
                ["--require-ready"],
                stdout=blocked_output,
                r2_probe=lambda: failed_capability,
            )

        self.assertEqual(blocked_exit_code, 1)
        self.assertFalse(json.loads(blocked_output.getvalue())["r2_storage_ready"])
        self.assertIs(
            inspect_mock.call_args.kwargs["r2_capability"], failed_capability
        )


if __name__ == "__main__":
    unittest.main()
