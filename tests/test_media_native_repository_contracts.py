from __future__ import annotations

import inspect
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from bbw_prod.models import (
    MediaAsset,
    MediaAttachment,
    MediaFlashClaim,
    MediaUploadIntent,
    SystemStorageQuota,
)
from bbw_web.media_native import MediaAttachmentPayload
from bbw_web.media_native.contracts import ASSET_STATUS_DELETED
from bbw_web.media_native.repository import (
    SqlAlchemyMediaConversationPolicy,
    SqlAlchemyMediaNativeRepository,
    _as_asset,
    _as_upload_intent,
    _media_report,
    direct_thread_key,
)
from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class MediaNativeSchemaContractTests(unittest.TestCase):
    def test_models_cover_owner_bound_upload_asset_attachment_and_flash_claim(self) -> None:
        self.assertEqual(MediaUploadIntent.__tablename__, "media_upload_intents")
        self.assertEqual(MediaAsset.__tablename__, "media_assets")
        self.assertEqual(MediaAttachment.__tablename__, "media_attachments")
        self.assertEqual(MediaFlashClaim.__tablename__, "media_flash_claims")

        upload_constraints = {
            item.name for item in MediaUploadIntent.__table__.constraints
        }
        asset_constraints = {item.name for item in MediaAsset.__table__.constraints}
        attachment_constraints = {
            item.name for item in MediaAttachment.__table__.constraints
        }
        flash_constraints = {
            item.name for item in MediaFlashClaim.__table__.constraints
        }
        self.assertIn("uq_media_upload_intents_id_owner", upload_constraints)
        self.assertTrue(
            any(
                name.endswith("media_upload_intent_kind_size_limit")
                for name in upload_constraints
            )
        )
        self.assertIn("fk_media_upload_intents_completed_asset", upload_constraints)
        self.assertIn("fk_media_assets_upload_owner", asset_constraints)
        self.assertTrue(
            any(name.endswith("media_asset_kind_size_limit") for name in asset_constraints)
        )
        self.assertIn("fk_media_attachments_message_thread", attachment_constraints)
        self.assertIn("fk_media_attachments_asset_sender", attachment_constraints)
        self.assertTrue(
            any(
                name.endswith("media_attachment_payload_url_free")
                for name in attachment_constraints
            )
        )
        self.assertIn("uq_media_flash_claims_attachment", flash_constraints)
        self.assertIn("fk_media_flash_claims_attachment_recipient", flash_constraints)
        self.assertEqual(SystemStorageQuota.__tablename__, "system_storage_quota")

    def test_migration_0014_follows_0013_and_has_reversible_cycle_order(self) -> None:
        source = (
            ROOT
            / "migrations"
            / "versions"
            / "20260725_0014_web_native_media.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision: str = "20260725_0014"', source)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0013"',
            source,
        )
        for table in (
            "media_upload_intents",
            "media_assets",
            "media_attachments",
            "media_flash_claims",
        ):
            self.assertIn(f'"{table}"', source)
        self.assertIn("expected_size_bytes <= 20971520", source)
        self.assertIn("size_bytes <= 104857600", source)
        self.assertIn("size_bytes <= 41943040", source)
        self.assertIn("interval '5 seconds'", source)
        drop_fk = source.index('op.drop_constraint(\n        "fk_media_upload_intents_completed_asset"')
        drop_asset = source.index('op.drop_table("media_assets")')
        self.assertLess(drop_fk, drop_asset)

    def test_row_mappers_keep_staging_and_private_zones_separate(self) -> None:
        owner_id = uuid.uuid4()
        intent_id = uuid.uuid4()
        intent = _as_upload_intent(
            SimpleNamespace(
                id=intent_id,
                owner_user_id=owner_id,
                deployment="production",
                kind="image",
                filename="photo.jpg",
                declared_content_type="image/jpeg",
                expected_size_bytes=100,
                expected_sha256="ab" * 32,
                staging_bucket="media",
                staging_namespace="staging",
                staging_object_key=f"{owner_id}/{intent_id}",
                created_at=NOW,
                expires_at=NOW,
                status="pending",
                completed_asset_id=None,
            )
        )
        asset = _as_asset(
            SimpleNamespace(
                id=uuid.uuid4(),
                owner_user_id=owner_id,
                deployment="production",
                kind="image",
                filename="photo.jpg",
                content_type="image/jpeg",
                size_bytes=100,
                sha256="ab" * 32,
                private_bucket="media",
                private_namespace="assets",
                private_object_key=f"{owner_id}/asset",
                created_at=NOW,
                status="available",
                duration_seconds=None,
                width=100,
                height=80,
                expires_at=None,
            )
        )
        self.assertEqual(intent.staging_object.zone, "staging")
        self.assertEqual(asset.object_ref.zone, "private")
        self.assertNotEqual(intent.staging_object.namespace, asset.object_ref.namespace)


class MediaNativeRepositoryContractTests(unittest.TestCase):
    def test_native_uploads_reserve_and_commit_shared_media_quota(self) -> None:
        source = inspect.getsource(SqlAlchemyMediaNativeRepository)
        self.assertIn("select(SystemStorageQuota)", source)
        self.assertIn(".with_for_update()", source)
        self.assertIn("MediaUploadIntentRow.expires_at > at", source)
        self.assertIn("user.media_used_bytes += finalized.size_bytes", source)
        self.assertIn("system.used_bytes += finalized.size_bytes", source)
        self.assertIn("MediaQuotaExceeded", source)

    def test_lifecycle_cleanup_is_reference_safe_retryable_and_quota_atomic(self) -> None:
        claim_source = inspect.getsource(
            SqlAlchemyMediaNativeRepository.claim_expired_upload_intents
        )
        candidates_source = inspect.getsource(
            SqlAlchemyMediaNativeRepository.asset_cleanup_candidates
        )
        eligibility_source = inspect.getsource(
            SqlAlchemyMediaNativeRepository._asset_cleanup_eligibility
        )
        delete_source = inspect.getsource(
            SqlAlchemyMediaNativeRepository.delete_asset_and_release_quota
        )
        self.assertIn("UPLOAD_STATUS_CANCELLED", claim_source)
        self.assertIn("with_for_update(skip_locked=True)", claim_source)
        self.assertIn("~attachment_exists", candidates_source)
        self.assertIn("MediaAssetRow.expires_at.is_(None)", eligibility_source)
        self.assertIn("MediaAssetRow.expires_at <= at", eligibility_source)
        self.assertLess(
            delete_source.index("delete_object(asset)"),
            delete_source.index("user.media_used_bytes ="),
        )
        self.assertIn("row.status = ASSET_STATUS_DELETED", delete_source)

        claim_owner = uuid.uuid4()

        def intent_row(status, *, updated_at):
            intent_id = uuid.uuid4()
            return SimpleNamespace(
                id=intent_id,
                owner_user_id=claim_owner,
                deployment="production",
                kind="file",
                filename="document.pdf",
                declared_content_type="application/pdf",
                expected_size_bytes=100,
                expected_sha256="ab" * 32,
                staging_bucket="media",
                staging_namespace="production/web-media-staging",
                staging_object_key=f"production/web-media-staging/{claim_owner}/{intent_id}/source.pdf",
                created_at=NOW - timedelta(hours=3),
                updated_at=updated_at,
                expires_at=NOW - timedelta(hours=2),
                status=status,
                completed_asset_id=None,
            )

        pending_intent = intent_row("pending", updated_at=NOW - timedelta(hours=3))
        mature_cancelled = intent_row(
            "cancelled", updated_at=NOW - timedelta(hours=2)
        )

        class ClaimDB:
            def __init__(self):
                self.flushes = 0

            def scalars(self, _statement):
                return [pending_intent, mature_cancelled]

            def flush(self):
                self.flushes += 1

        claimed = SqlAlchemyMediaNativeRepository(ClaimDB()).claim_expired_upload_intents(
            at=NOW,
            cleanup_grace=timedelta(hours=1),
        )
        self.assertEqual(pending_intent.status, "cancelled")
        self.assertEqual(pending_intent.updated_at, NOW)
        self.assertEqual([item.id for item in claimed], [mature_cancelled.id])

        owner_id = uuid.uuid4()
        asset_id = uuid.uuid4()

        class FakeDB:
            def __init__(self, values):
                self.values = list(values)
                self.flushes = 0

            def scalar(self, _statement):
                return self.values.pop(0)

            def flush(self):
                self.flushes += 1

        def asset_row():
            return SimpleNamespace(
                id=asset_id,
                upload_intent_id=uuid.uuid4(),
                owner_user_id=owner_id,
                deployment="production",
                kind="file",
                filename="document.pdf",
                content_type="application/pdf",
                size_bytes=100,
                sha256="ab" * 32,
                private_bucket="media",
                private_namespace="production/web-media-private",
                private_object_key=f"production/web-media-private/{owner_id}/{asset_id}/file.pdf",
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(days=2),
                status="available",
                duration_seconds=None,
                width=None,
                height=None,
                expires_at=None,
            )

        system = SimpleNamespace(used_bytes=1000, version=7)
        user = SimpleNamespace(media_used_bytes=500)
        row = asset_row()
        deleted_assets = []
        repository = SqlAlchemyMediaNativeRepository(
            FakeDB([system, user, row, None])
        )

        self.assertTrue(
            repository.delete_asset_and_release_quota(
                asset_id=asset_id,
                owner_user_id=owner_id,
                at=NOW,
                unsent_grace=timedelta(days=1),
                delete_object=deleted_assets.append,
            )
        )
        self.assertEqual(len(deleted_assets), 1)
        self.assertEqual(row.status, ASSET_STATUS_DELETED)
        self.assertEqual(user.media_used_bytes, 400)
        self.assertEqual(system.used_bytes, 900)
        self.assertEqual(system.version, 8)

        repeated = SqlAlchemyMediaNativeRepository(FakeDB([system, user, None]))
        self.assertFalse(
            repeated.delete_asset_and_release_quota(
                asset_id=asset_id,
                owner_user_id=owner_id,
                at=NOW,
                unsent_grace=timedelta(days=1),
                delete_object=deleted_assets.append,
            )
        )
        self.assertEqual(len(deleted_assets), 1)
        self.assertEqual(user.media_used_bytes, 400)
        self.assertEqual(system.used_bytes, 900)

        protected_system = SimpleNamespace(used_bytes=1000, version=4)
        protected_user = SimpleNamespace(media_used_bytes=500)
        protected_row = asset_row()
        protected = SqlAlchemyMediaNativeRepository(
            FakeDB(
                [
                    protected_system,
                    protected_user,
                    protected_row,
                    uuid.uuid4(),
                ]
            )
        )
        self.assertFalse(
            protected.delete_asset_and_release_quota(
                asset_id=asset_id,
                owner_user_id=owner_id,
                at=NOW,
                unsent_grace=timedelta(days=1),
                delete_object=deleted_assets.append,
            )
        )
        self.assertEqual(len(deleted_assets), 1)
        self.assertEqual(protected_row.status, "available")
        self.assertEqual(protected_user.media_used_bytes, 500)
        self.assertEqual(protected_system.used_bytes, 1000)

        failed_system = SimpleNamespace(used_bytes=1000, version=3)
        failed_user = SimpleNamespace(media_used_bytes=500)
        failed_row = asset_row()

        def fail_delete(_asset):
            raise RuntimeError("R2 unavailable")

        failing = SqlAlchemyMediaNativeRepository(
            FakeDB([failed_system, failed_user, failed_row, None])
        )
        with self.assertRaisesRegex(RuntimeError, "R2 unavailable"):
            failing.delete_asset_and_release_quota(
                asset_id=asset_id,
                owner_user_id=owner_id,
                at=NOW,
                unsent_grace=timedelta(days=1),
                delete_object=fail_delete,
            )
        self.assertEqual(failed_row.status, "available")
        self.assertEqual(failed_user.media_used_bytes, 500)
        self.assertEqual(failed_system.used_bytes, 1000)
        self.assertEqual(failed_system.version, 3)

    def test_media_report_is_legacy_compatible_and_url_free(self) -> None:
        attachment_id = uuid.uuid4()
        payload = MediaAttachmentPayload(
            asset_id=uuid.uuid4(),
            kind="image",
            filename="photo.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            sha256="ab" * 32,
            width=1200,
            height=800,
            flash=True,
            flash_display_seconds=5,
        )

        report = _media_report(attachment_id, payload)

        self.assertEqual(
            set(report),
            {
                "attachment_id",
                "asset_id",
                "name",
                "mime",
                "size",
                "duration",
                "width",
                "height",
                "flash",
            },
        )
        self.assertNotIn("url", repr(report).lower())
        self.assertEqual(report["attachment_id"], str(attachment_id))

    def test_store_attachment_source_is_one_canonical_transaction_contract(self) -> None:
        store_source = inspect.getsource(
            SqlAlchemyMediaNativeRepository.store_attachment
        )
        module_source = inspect.getsource(
            __import__(
                "bbw_web.media_native.repository",
                fromlist=["SqlAlchemyMediaNativeRepository"],
            )
        )
        self.assertIn("insert(ChatMessage)", store_source)
        self.assertIn("insert(MediaAttachmentRow)", store_source)
        self.assertEqual(store_source.count("self._insert_projection("), 2)
        self.assertEqual(store_source.count("self._ensure_delivery("), 2)
        self.assertIn("insert(MessageDelivery)", module_source)
        self.assertIn('channel=TIM_MIRROR_CHANNEL', store_source)
        self.assertIn(
            "asset_row.expires_at = canonical.retention_expires_at",
            store_source,
        )
        self.assertIn('"media_report": _media_report(', module_source)
        self.assertIn('metadata["flash_id"] = str(attachment_id)', module_source)
        for object_name in (
            "TIMImageElem",
            "TIMSoundElem",
            "TIMVideoFileElem",
            "TIMFileElem",
        ):
            self.assertIn(object_name, module_source)

    def test_revoke_and_flash_claim_are_locked_and_bounded(self) -> None:
        revoke = inspect.getsource(
            SqlAlchemyMediaNativeRepository.revoke_attachment
        )
        reconcile = inspect.getsource(
            SqlAlchemyMediaNativeRepository._reconcile_tim_send_on_revoke
        )
        claim = inspect.getsource(
            SqlAlchemyMediaNativeRepository.claim_flash_once
        )
        self.assertIn(".with_for_update()", revoke)
        self.assertIn("timedelta(minutes=2)", revoke)
        self.assertIn('row.status = ATTACHMENT_STATUS_REVOKED', revoke)
        self.assertIn('projection.status = "revoked"', revoke)
        self.assertIn('{"pending", "retry", "failed"}', reconcile)
        self.assertIn('send_delivery.status = "cancelled"', reconcile)
        self.assertIn('send_status == "delivered"', reconcile)
        self.assertIn("self._ensure_revoke_delivery", reconcile)
        self.assertIn(".with_for_update()", claim)
        self.assertIn("timedelta(seconds=5)", claim)
        self.assertIn('constraint="uq_media_flash_claims_attachment"', claim)
        self.assertIn("raise FlashAlreadyClaimed", claim)

    def test_revoke_cancels_unsent_tim_and_reuses_required_compensation(self) -> None:
        class ScalarRows:
            def __init__(self, value):
                self.value = value

            def first(self):
                return self.value

        class FakeDB:
            def __init__(self, *, scalar_values, scalars_values=()):
                self.scalar_values = list(scalar_values)
                self.scalars_values = list(scalars_values)

            def scalar(self, _statement):
                return self.scalar_values.pop(0)

            def scalars(self, _statement):
                return ScalarRows(self.scalars_values.pop(0))

        canonical = SimpleNamespace(
            id=uuid.uuid4(),
            client_message_id="media:revoke-race",
        )
        attachment = SimpleNamespace(
            id=uuid.uuid4(),
            recipient_upstream_uid="9",
            sender_upstream_uid="42",
        )

        pending = SimpleNamespace(
            status="pending",
            payload={"operation": "send"},
            locked_by="worker",
            locked_until=NOW,
            last_error=None,
        )
        repository = SqlAlchemyMediaNativeRepository(
            FakeDB(scalar_values=[pending])
        )
        self.assertIsNone(
            repository._reconcile_tim_send_on_revoke(canonical, attachment)
        )
        self.assertEqual(pending.status, "cancelled")
        self.assertIsNone(pending.locked_by)
        self.assertIsNone(pending.locked_until)

        repeated_repository = SqlAlchemyMediaNativeRepository(
            FakeDB(scalar_values=[pending, None])
        )
        self.assertIsNone(
            repeated_repository._reconcile_tim_send_on_revoke(
                canonical, attachment
            )
        )

        delivered = SimpleNamespace(
            status="delivered",
            payload={"operation": "send"},
            locked_by=None,
            locked_until=None,
            last_error=None,
        )
        revoke_delivery = SimpleNamespace(id=uuid.uuid4())
        delivered_repository = SqlAlchemyMediaNativeRepository(
            FakeDB(
                scalar_values=[delivered],
                scalars_values=[revoke_delivery],
            )
        )
        self.assertIs(
            delivered_repository._reconcile_tim_send_on_revoke(
                canonical, attachment
            ),
            revoke_delivery,
        )

        revoke_delivery.payload = delivered_repository._revoke_delivery_payload(
            canonical, attachment
        )
        repeated_delivered_repository = SqlAlchemyMediaNativeRepository(
            FakeDB(
                scalar_values=[delivered, revoke_delivery],
                scalars_values=[None],
            )
        )
        self.assertIs(
            repeated_delivered_repository._reconcile_tim_send_on_revoke(
                canonical, attachment
            ),
            revoke_delivery,
        )

        processing = SimpleNamespace(
            status="processing",
            payload={"operation": "send"},
            locked_by="worker",
            locked_until=NOW,
            last_error=None,
        )
        processing_revoke = SimpleNamespace(id=uuid.uuid4())
        processing_repository = SqlAlchemyMediaNativeRepository(
            FakeDB(
                scalar_values=[processing],
                scalars_values=[processing_revoke],
            )
        )
        self.assertIs(
            processing_repository._reconcile_tim_send_on_revoke(
                canonical, attachment
            ),
            processing_revoke,
        )
        self.assertEqual(processing.status, "cancelled")

    def test_cancelled_tim_delivery_cannot_be_resurrected_by_worker_result(self) -> None:
        delivered = inspect.getsource(
            SqlAlchemyCanonicalMessageStore.mark_tim_delivered
        )
        failed = inspect.getsource(SqlAlchemyCanonicalMessageStore.mark_tim_failed)
        guard = 'if row.status != "processing":\n            return row'
        self.assertIn(guard, delivered)
        self.assertIn(guard, failed)

    def test_blacklist_policy_checks_both_directions_and_fails_closed_without_uid(self) -> None:
        left = uuid.uuid4()
        right = uuid.uuid4()

        class CaptureDB:
            def __init__(self, accounts):
                self.accounts = accounts
                self.statements = []

            def execute(self, statement):
                self.statements.append(statement)
                return list(self.accounts)

            def scalar(self, statement):
                self.statements.append(statement)
                return None

        capture = CaptureDB(((left, "42"), (right, "9")))
        self.assertFalse(
            SqlAlchemyMediaConversationPolicy(capture).is_blocked_between(left, right)
        )
        relationship = capture.statements[-1].compile(dialect=postgresql.dialect())
        sql = str(relationship)
        values = {str(value) for value in relationship.params.values()}
        self.assertGreaterEqual(sql.count("relationships.owner_user_id ="), 2)
        self.assertGreaterEqual(sql.count("relationships.subject_upstream_uid ="), 2)
        self.assertIn("42", values)
        self.assertIn("9", values)
        self.assertTrue(
            any(
                value == ["blacklist", "blacklisted_by"]
                for value in relationship.params.values()
            )
        )
        self.assertIn("media_left_local_block_override", sql)
        self.assertIn("media_right_local_block_override", sql)
        self.assertGreaterEqual(sql.count("NOT (EXISTS"), 2)
        self.assertIn("web-local", values)

        missing = CaptureDB(((left, "42"),))
        self.assertTrue(
            SqlAlchemyMediaConversationPolicy(missing).is_blocked_between(left, right)
        )
        self.assertEqual(len(missing.statements), 1)

    def test_direct_thread_key_is_symmetric_and_internal(self) -> None:
        left = uuid.uuid4()
        right = uuid.uuid4()
        self.assertEqual(direct_thread_key(left, right), direct_thread_key(right, left))
        self.assertIn(str(left), direct_thread_key(left, right))
        self.assertIn(str(right), direct_thread_key(left, right))


if __name__ == "__main__":
    unittest.main()
