from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from bbw_prod.models import (
    ChatMessage,
    ExternalAccount,
    MediaAttachment,
    MediaFlashClaim,
    Message,
    MessageDelivery,
    User,
    UserCredential,
)
from bbw_web.media_native import (
    FinalizedMediaObject,
    FlashAlreadyClaimed,
    MediaNativeService,
    MediaPrincipal,
    PresignedMediaRead,
    StagedMediaInspection,
    StagingUploadTarget,
    StorageObjectRef,
)
from bbw_web.media_native.repository import (
    SqlAlchemyMediaConversationPolicy,
    SqlAlchemyMediaNativeRepository,
)
from bbw_web.messaging.repository import SqlAlchemyCanonicalMessageStore


DATABASE_URL = os.getenv("BBW_TEST_DATABASE_URL", "")
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _Storage:
    def prepare_staging_upload(self, **values):
        object_ref = StorageObjectRef(
            deployment=values["deployment"],
            zone="staging",
            bucket="integration-media",
            namespace="staging",
            key=f'{values["owner_user_id"]}/{values["intent_id"]}',
            owner_user_id=values["owner_user_id"],
        )
        return StagingUploadTarget(
            intent_id=values["intent_id"],
            object_ref=object_ref,
            upload_url="https://upload.invalid/object?signature=ephemeral",
            expires_at=values["expires_at"],
        )

    def inspect_staging(self, intent):
        return StagedMediaInspection(
            object_ref=intent.staging_object,
            verified=True,
            detected_kind=intent.kind,
            detected_content_type=intent.declared_content_type,
            size_bytes=intent.expected_size_bytes,
            sha256=intent.expected_sha256,
            width=640,
            height=480,
        )

    def promote_verified(self, intent, inspection):
        return FinalizedMediaObject(
            object_ref=StorageObjectRef(
                deployment=intent.deployment,
                zone="private",
                bucket="integration-media",
                namespace="assets",
                key=f"{intent.owner_user_id}/{intent.id}",
                owner_user_id=intent.owner_user_id,
            ),
            content_type=inspection.detected_content_type,
            size_bytes=inspection.size_bytes,
            sha256=inspection.sha256,
            width=inspection.width,
            height=inspection.height,
        )

    def issue_private_read(self, asset, *, expires_at):
        return PresignedMediaRead(
            object_ref=asset.object_ref,
            url="https://read.invalid/object?signature=ephemeral",
            expires_at=expires_at,
        )


@unittest.skipUnless(DATABASE_URL, "requires BBW_TEST_DATABASE_URL")
class MediaNativePostgresIntegrationTests(unittest.TestCase):
    def test_send_writes_canonical_two_projections_and_url_free_tim_outbox(self) -> None:
        engine = create_engine(DATABASE_URL)
        sender_id = uuid.uuid4()
        recipient_id = uuid.uuid4()
        sender_account_id = uuid.uuid4()
        recipient_account_id = uuid.uuid4()
        sender_uid = f"sender-{uuid.uuid4().hex}"
        recipient_uid = f"recipient-{uuid.uuid4().hex}"
        with Session(engine) as db, db.begin():
            db.add_all(
                [
                    User(id=sender_id, status="active", display_name="发送者"),
                    User(id=recipient_id, status="active", display_name="接收者"),
                ]
            )
            db.flush()
            db.add_all(
                [
                    ExternalAccount(
                        id=sender_account_id,
                        user_id=sender_id,
                        provider="beibeiwu",
                        upstream_uid=sender_uid,
                        phone_hmac=uuid.uuid4().hex,
                        login_account_encrypted={"ciphertext": "sender"},
                    ),
                    ExternalAccount(
                        id=recipient_account_id,
                        user_id=recipient_id,
                        provider="beibeiwu",
                        upstream_uid=recipient_uid,
                        phone_hmac=uuid.uuid4().hex,
                        login_account_encrypted={"ciphertext": "recipient"},
                    ),
                    UserCredential(
                        user_id=sender_id,
                        password_hash="$argon2id$v=19$m=8192,t=1,p=1$c2FsdHNhbHQ$hash",
                        enrollment_source="integration-test",
                        verified_at=NOW,
                        password_changed_at=NOW,
                    ),
                    UserCredential(
                        user_id=recipient_id,
                        password_hash="$argon2id$v=19$m=8192,t=1,p=1$c2FsdHNhbHQ$hash",
                        enrollment_source="integration-test",
                        verified_at=NOW,
                        password_changed_at=NOW,
                    ),
                ]
            )
            db.flush()
            repository = SqlAlchemyMediaNativeRepository(db)
            policy = SqlAlchemyMediaConversationPolicy(db)
            service = MediaNativeService(
                repository,
                _Storage(),
                policy,
                deployment="integration",
                clock=lambda: NOW,
            )
            principal = MediaPrincipal(
                sender_id,
                sender_account_id,
                sender_uid,
            )
            upload = service.create_upload_intent(
                principal=principal,
                kind="image",
                filename="photo.jpg",
                content_type="image/jpeg",
                size_bytes=1024,
                sha256="ab" * 32,
            )
            asset = service.complete_upload(
                principal=principal,
                intent_id=upload.intent.id,
            )
            delivered = service.send_attachment(
                principal=principal,
                peer_upstream_uid=recipient_uid,
                client_message_id=f"media:{uuid.uuid4()}",
                asset_id=asset.id,
            )

            self.assertTrue(delivered.created)
            self.assertEqual(
                db.scalar(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.id == delivered.attachment.message_id
                    )
                ),
                1,
            )
            projections = list(
                db.scalars(
                    select(Message).where(
                        Message.provider == "web-local",
                        Message.upstream_message_id
                        == str(delivered.attachment.message_id),
                    )
                )
            )
            self.assertEqual(len(projections), 2)
            self.assertEqual(
                {row.direction for row in projections}, {"outgoing", "incoming"}
            )
            for row in projections:
                report = dict(row.extra_data or {}).get("media_report") or {}
                self.assertEqual(
                    report["attachment_id"], str(delivered.attachment.id)
                )
                self.assertNotIn("url", repr(report).lower())
                self.assertEqual(row.extra_data["object_name"], "TIMImageElem")
            deliveries = list(
                db.scalars(
                    select(MessageDelivery).where(
                        MessageDelivery.message_id
                        == delivered.attachment.message_id
                    )
                )
            )
            self.assertEqual(len(deliveries), 2)
            tim = next(row for row in deliveries if row.channel == "tim")
            self.assertEqual(tim.status, "pending")
            self.assertFalse(tim.required)
            self.assertNotIn("url", repr(tim.payload).lower())
            attachment_row = db.get(MediaAttachment, delivered.attachment.id)
            self.assertEqual(attachment_row.asset_id, asset.id)
            self.assertNotIn("url", repr(attachment_row.payload).lower())

            revoked = service.revoke_attachment(
                principal=principal,
                attachment_id=delivered.attachment.id,
            )
            self.assertTrue(revoked.created)
            self.assertEqual(revoked.attachment.status, "revoked")
            self.assertEqual(db.get(ChatMessage, delivered.attachment.message_id).status, "revoked")
            revoked_projections = list(
                db.scalars(
                    select(Message).where(
                        Message.provider == "web-local",
                        Message.upstream_message_id
                        == str(delivered.attachment.message_id),
                    )
                )
            )
            self.assertEqual({row.status for row in revoked_projections}, {"revoked"})
            self.assertIsNone(revoked.tim_mirror)
            cancelled_send = db.scalar(
                select(MessageDelivery).where(
                    MessageDelivery.message_id == delivered.attachment.message_id,
                    MessageDelivery.channel == "tim",
                    MessageDelivery.target_key == f"media-send:{recipient_uid}",
                )
            )
            self.assertEqual(cancelled_send.status, "cancelled")
            self.assertEqual(
                db.scalar(
                    select(func.count(MessageDelivery.id)).where(
                        MessageDelivery.message_id
                        == delivered.attachment.message_id,
                        MessageDelivery.target_key
                        == f"media-revoke:{recipient_uid}",
                    )
                ),
                0,
            )
            repeated = service.revoke_attachment(
                principal=principal,
                attachment_id=delivered.attachment.id,
            )
            self.assertFalse(repeated.created)
            self.assertIsNone(repeated.tim_mirror)

            mirrored = service.send_attachment(
                principal=principal,
                peer_upstream_uid=recipient_uid,
                client_message_id=f"mirrored:{uuid.uuid4()}",
                asset_id=asset.id,
            )
            mirrored_send = db.scalar(
                select(MessageDelivery).where(
                    MessageDelivery.message_id == mirrored.attachment.message_id,
                    MessageDelivery.channel == "tim",
                    MessageDelivery.target_key == f"media-send:{recipient_uid}",
                )
            )
            mirrored_send.status = "delivered"
            mirrored_send.delivered_at = NOW
            db.flush()
            mirrored_revoke = service.revoke_attachment(
                principal=principal,
                attachment_id=mirrored.attachment.id,
            )
            self.assertIsNotNone(mirrored_revoke.tim_mirror)
            revoke_outbox = db.scalar(
                select(MessageDelivery).where(
                    MessageDelivery.id
                    == mirrored_revoke.tim_mirror.delivery_id
                )
            )
            self.assertEqual(revoke_outbox.payload["operation"], "revoke")
            self.assertNotIn("url", repr(revoke_outbox.payload).lower())
            mirrored_repeated = service.revoke_attachment(
                principal=principal,
                attachment_id=mirrored.attachment.id,
            )
            self.assertFalse(mirrored_repeated.created)
            self.assertEqual(
                mirrored_repeated.tim_mirror.delivery_id,
                mirrored_revoke.tim_mirror.delivery_id,
            )

            processing = service.send_attachment(
                principal=principal,
                peer_upstream_uid=recipient_uid,
                client_message_id=f"processing:{uuid.uuid4()}",
                asset_id=asset.id,
            )
            processing_send = db.scalar(
                select(MessageDelivery).where(
                    MessageDelivery.message_id == processing.attachment.message_id,
                    MessageDelivery.channel == "tim",
                    MessageDelivery.target_key == f"media-send:{recipient_uid}",
                )
            )
            processing_send.status = "processing"
            processing_send.locked_by = "integration-worker"
            processing_send.locked_until = NOW
            processing_send.attempt_count = 1
            db.flush()
            processing_revoke = service.revoke_attachment(
                principal=principal,
                attachment_id=processing.attachment.id,
            )
            self.assertIsNotNone(processing_revoke.tim_mirror)
            self.assertEqual(processing_send.status, "cancelled")
            store = SqlAlchemyCanonicalMessageStore(db)
            self.assertEqual(
                store.mark_tim_failed(
                    processing_send.id,
                    error="late worker failure",
                    retry_at=NOW,
                ).status,
                "cancelled",
            )
            self.assertEqual(
                store.mark_tim_delivered(
                    processing_send.id,
                    delivered_at=NOW,
                    upstream_message_id="late-worker-success",
                ).status,
                "cancelled",
            )

            flash = service.send_attachment(
                principal=principal,
                peer_upstream_uid=recipient_uid,
                client_message_id=f"flash:{uuid.uuid4()}",
                asset_id=asset.id,
                flash=True,
            )
            recipient_principal = MediaPrincipal(
                recipient_id,
                recipient_account_id,
                recipient_uid,
            )
            claimed = service.claim_flash(
                principal=recipient_principal,
                attachment_id=flash.attachment.id,
            )
            self.assertEqual(
                (claimed.claim.display_until - claimed.claim.claimed_at).total_seconds(),
                5,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count(MediaFlashClaim.id)).where(
                        MediaFlashClaim.attachment_id == flash.attachment.id
                    )
                ),
                1,
            )
            with self.assertRaises(FlashAlreadyClaimed):
                service.claim_flash(
                    principal=recipient_principal,
                    attachment_id=flash.attachment.id,
                )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
