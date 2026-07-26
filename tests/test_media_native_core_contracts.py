from __future__ import annotations

import unittest
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from bbw_web.media_native import (
    ATTACHMENT_STATUS_REVOKED,
    FLASH_DISPLAY_SECONDS,
    MAX_AUDIO_DURATION_SECONDS,
    MAX_MEDIA_BYTES,
    REVOKE_WINDOW_SECONDS,
    STORAGE_ZONE_PRIVATE,
    STORAGE_ZONE_STAGING,
    UPLOAD_STATUS_COMPLETED,
    AttachmentDeliveryResult,
    DirectMediaThread,
    FinalizedMediaObject,
    FlashAlreadyClaimed,
    FlashClaim,
    FlashClaimDenied,
    FlashClaimRequired,
    InvalidMediaRequest,
    MediaAccessDenied,
    MediaAccount,
    MediaAsset,
    MediaAssetUnavailable,
    MediaAttachment,
    MediaBlocked,
    MediaContractViolation,
    MediaIdempotencyConflict,
    MediaInspectionRejected,
    MediaIntegrityMismatch,
    MediaNativeService,
    MediaPrincipal,
    MediaRevocationDenied,
    MediaRevocationExpired,
    MediaThreadForbidden,
    MediaTooLarge,
    MediaTypeRejected,
    MediaRevokeResult,
    PresignedMediaRead,
    StagedMediaInspection,
    StagingUploadTarget,
    StorageObjectRef,
    TimMediaMirrorIntent,
    TimMediaRevokeIntent,
    UploadAlreadyCompleted,
    UploadIntent,
    UploadIntentUnavailable,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
DIGEST = "ab" * 32
MIME_BY_KIND = {
    "image": "image/jpeg",
    "audio": "audio/mp4",
    "video": "video/mp4",
    "file": "application/pdf",
}


class _MemoryPolicy:
    def __init__(self) -> None:
        self.sender = MediaAccount(uuid.uuid4(), uuid.uuid4(), "42")
        self.recipient = MediaAccount(uuid.uuid4(), uuid.uuid4(), "9")
        self.intruder = MediaAccount(uuid.uuid4(), uuid.uuid4(), "77")
        self.accounts = {
            account.upstream_uid: account
            for account in (self.sender, self.recipient, self.intruder)
        }
        self.thread = DirectMediaThread(
            uuid.uuid4(),
            frozenset({self.sender.user_id, self.recipient.user_id}),
        )
        self.blocked = False
        self.message_allowed = True
        self.invalid_thread = False
        self.thread_resolve_calls = 0

    def resolve_principal(self, principal: MediaPrincipal) -> MediaAccount | None:
        account = self.accounts.get(principal.upstream_uid)
        if account is None:
            return None
        if (
            account.user_id == principal.user_id
            and account.external_account_id == principal.external_account_id
            and account.account_provider == principal.account_provider
        ):
            return account
        return None

    def resolve_active_peer(self, upstream_uid: str, *, provider: str):
        account = self.accounts.get(upstream_uid)
        return account if account is not None and account.account_provider == provider else None

    def can_send_private_message(self, sender, recipient):
        return self.message_allowed

    def resolve_or_create_direct_thread(self, sender, recipient):
        self.thread_resolve_calls += 1
        if self.invalid_thread:
            return replace(self.thread, member_user_ids=frozenset({sender.user_id}))
        if {sender.user_id, recipient.user_id} == set(self.thread.member_user_ids):
            return self.thread
        return DirectMediaThread(
            uuid.uuid4(), frozenset({sender.user_id, recipient.user_id})
        )

    def resolve_thread(self, thread_id: uuid.UUID):
        if thread_id != self.thread.id:
            return None
        if self.invalid_thread:
            return replace(self.thread, member_user_ids=frozenset({self.sender.user_id}))
        return self.thread

    def is_blocked_between(self, left_user_id: uuid.UUID, right_user_id: uuid.UUID):
        return self.blocked


class _MemoryStorage:
    def __init__(self) -> None:
        self.inspection_override: dict[str, object] = {}
        self.final_override: dict[str, object] = {}
        self.target_override: dict[str, object] = {}
        self.issue_read_calls = []
        self.promote_calls = 0

    def prepare_staging_upload(self, **values):
        object_ref = StorageObjectRef(
            deployment=str(self.target_override.get("deployment", values["deployment"])),
            zone=str(self.target_override.get("zone", STORAGE_ZONE_STAGING)),
            bucket="private-media",
            namespace=str(self.target_override.get("namespace", "staging")),
            key=f'{values["owner_user_id"]}/{values["intent_id"]}',
            owner_user_id=self.target_override.get(
                "owner_user_id", values["owner_user_id"]
            ),
        )
        return StagingUploadTarget(
            intent_id=values["intent_id"],
            object_ref=object_ref,
            upload_url=f'https://upload.invalid/{values["intent_id"]}?signature=secret',
            expires_at=values["expires_at"],
            required_headers=(("content-type", values["content_type"]),),
        )

    def inspect_staging(self, intent: UploadIntent):
        defaults: dict[str, object] = {
            "object_ref": intent.staging_object,
            "verified": True,
            "detected_kind": intent.kind,
            "detected_content_type": intent.declared_content_type,
            "size_bytes": intent.expected_size_bytes,
            "sha256": intent.expected_sha256,
            "duration_seconds": (
                MAX_AUDIO_DURATION_SECONDS
                if intent.kind == "audio"
                else 12.0 if intent.kind == "video" else None
            ),
            "width": 1200 if intent.kind == "image" else None,
            "height": 800 if intent.kind == "image" else None,
        }
        defaults.update(self.inspection_override)
        return StagedMediaInspection(**defaults)

    def promote_verified(self, intent: UploadIntent, inspection: StagedMediaInspection):
        self.promote_calls += 1
        private_ref = StorageObjectRef(
            deployment=intent.deployment,
            zone=STORAGE_ZONE_PRIVATE,
            bucket="private-media",
            namespace="assets",
            key=f"{intent.owner_user_id}/{intent.id}",
            owner_user_id=intent.owner_user_id,
        )
        defaults: dict[str, object] = {
            "object_ref": private_ref,
            "content_type": inspection.detected_content_type,
            "size_bytes": inspection.size_bytes,
            "sha256": inspection.sha256,
            "duration_seconds": inspection.duration_seconds,
            "width": inspection.width,
            "height": inspection.height,
        }
        defaults.update(self.final_override)
        return FinalizedMediaObject(**defaults)

    def issue_private_read(self, asset: MediaAsset, *, expires_at: datetime):
        self.issue_read_calls.append((asset, expires_at))
        return PresignedMediaRead(
            object_ref=asset.object_ref,
            url=f"https://read.invalid/{asset.id}?signature=secret",
            expires_at=expires_at,
        )


class _MemoryRepository:
    def __init__(self) -> None:
        self.intents: dict[uuid.UUID, UploadIntent] = {}
        self.assets: dict[uuid.UUID, MediaAsset] = {}
        self.attachments: dict[uuid.UUID, MediaAttachment] = {}
        self.deliveries: dict[tuple[uuid.UUID, str], AttachmentDeliveryResult] = {}
        self.revokes: dict[uuid.UUID, MediaRevokeResult] = {}
        self.claims: dict[tuple[uuid.UUID, uuid.UUID], FlashClaim] = {}
        self.persisted_attachment_payloads = []

    def create_upload_intent(self, intent: UploadIntent):
        self.intents[intent.id] = intent
        return intent

    def get_upload_intent(self, intent_id: uuid.UUID):
        return self.intents.get(intent_id)

    def complete_upload(self, *, intent, inspection, finalized, completed_at):
        current = self.intents.get(intent.id)
        if current is None or current.status != "pending":
            raise UploadAlreadyCompleted("intent 已被消费")
        asset = MediaAsset(
            id=uuid.uuid4(),
            owner_user_id=intent.owner_user_id,
            deployment=intent.deployment,
            kind=intent.kind,
            filename=intent.filename,
            content_type=finalized.content_type,
            size_bytes=finalized.size_bytes,
            sha256=finalized.sha256,
            object_ref=finalized.object_ref,
            created_at=completed_at,
            duration_seconds=finalized.duration_seconds,
            width=finalized.width,
            height=finalized.height,
        )
        self.assets[asset.id] = asset
        self.intents[intent.id] = replace(
            current,
            status=UPLOAD_STATUS_COMPLETED,
            completed_asset_id=asset.id,
        )
        return asset

    def get_media_asset(self, asset_id: uuid.UUID):
        return self.assets.get(asset_id)

    def store_attachment(self, **values):
        sender = values["sender"]
        recipient = values["recipient"]
        thread = values["thread"]
        asset = values["asset"]
        client_message_id = values["client_message_id"]
        payload = values["payload"]
        key = (sender.user_id, client_message_id)
        existing = self.deliveries.get(key)
        if existing is not None:
            if (
                existing.attachment.recipient_user_id != recipient.user_id
                or existing.attachment.payload != payload
            ):
                raise MediaIdempotencyConflict("client_message_id 已绑定其他媒体")
            return replace(existing, created=False)
        attachment_id = uuid.uuid4()
        message_id = uuid.uuid4()
        attachment = MediaAttachment(
            id=attachment_id,
            message_id=message_id,
            thread_id=thread.id,
            client_message_id=client_message_id,
            sender_user_id=sender.user_id,
            recipient_user_id=recipient.user_id,
            sender_upstream_uid=sender.upstream_uid,
            recipient_upstream_uid=recipient.upstream_uid,
            payload=payload,
            sent_at=values["sent_at"],
        )
        result = AttachmentDeliveryResult(
            attachment=attachment,
            sender_projection_id=uuid.uuid4(),
            recipient_projection_id=uuid.uuid4(),
            created=True,
            tim_mirror=TimMediaMirrorIntent(
                delivery_id=uuid.uuid4(),
                message_id=message_id,
                attachment_id=attachment_id,
                from_upstream_uid=sender.upstream_uid,
                to_upstream_uid=recipient.upstream_uid,
                client_message_id=client_message_id,
                payload=payload,
            ),
        )
        self.attachments[attachment_id] = attachment
        self.deliveries[key] = result
        self.persisted_attachment_payloads.append(payload.as_persistent_dict())
        return result

    def get_media_attachment(self, attachment_id: uuid.UUID):
        return self.attachments.get(attachment_id)

    def revoke_attachment(self, *, attachment, actor, revoked_at):
        existing = self.revokes.get(attachment.id)
        if existing is not None:
            return replace(existing, created=False)
        revoked = replace(
            attachment,
            status=ATTACHMENT_STATUS_REVOKED,
            revoked_at=revoked_at,
        )
        self.attachments[attachment.id] = revoked
        result = MediaRevokeResult(
            attachment=revoked,
            created=True,
            tim_mirror=TimMediaRevokeIntent(
                delivery_id=uuid.uuid4(),
                message_id=attachment.message_id,
                attachment_id=attachment.id,
                from_upstream_uid=attachment.sender_upstream_uid,
                to_upstream_uid=attachment.recipient_upstream_uid,
                client_message_id=attachment.client_message_id,
            ),
        )
        self.revokes[attachment.id] = result
        return result

    def claim_flash_once(self, *, attachment, claimant, claimed_at, display_until):
        key = (attachment.id, claimant.user_id)
        if key in self.claims:
            raise FlashAlreadyClaimed("闪照已经领取")
        claim = FlashClaim(
            id=uuid.uuid4(),
            attachment_id=attachment.id,
            claimant_user_id=claimant.user_id,
            claimed_at=claimed_at,
            display_until=display_until,
        )
        self.claims[key] = claim
        return claim


class MediaNativeCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.policy = _MemoryPolicy()
        self.repository = _MemoryRepository()
        self.storage = _MemoryStorage()
        self.service = MediaNativeService(
            self.repository,
            self.storage,
            self.policy,
            deployment="production",
            clock=lambda: self.now,
        )
        self.sender = self._principal(self.policy.sender)
        self.recipient = self._principal(self.policy.recipient)
        self.intruder = self._principal(self.policy.intruder)

    @staticmethod
    def _principal(account: MediaAccount) -> MediaPrincipal:
        return MediaPrincipal(
            account.user_id,
            account.external_account_id,
            account.upstream_uid,
            account.account_provider,
        )

    def _upload(self, kind: str = "image", *, size: int = 1024):
        return self.service.create_upload_intent(
            principal=self.sender,
            kind=kind,
            filename=f"sample.{kind}",
            content_type=MIME_BY_KIND[kind],
            size_bytes=size,
            sha256=DIGEST,
        )

    def _asset(self, kind: str = "image") -> MediaAsset:
        grant = self._upload(kind)
        return self.service.complete_upload(
            principal=self.sender,
            intent_id=grant.intent.id,
        )

    def _send(self, *, kind: str = "image", flash: bool = False):
        asset = self._asset(kind)
        return self.service.send_attachment(
            principal=self.sender,
            peer_upstream_uid=self.policy.recipient.upstream_uid,
            client_message_id=f"media:{uuid.uuid4()}",
            asset_id=asset.id,
            flash=flash,
        )

    def test_confirmed_doubled_limits_are_enforced_at_intent_boundary(self) -> None:
        self.assertEqual(
            MAX_MEDIA_BYTES,
            {
                "image": 20 * 1024 * 1024,
                "audio": 20 * 1024 * 1024,
                "video": 100 * 1024 * 1024,
                "file": 40 * 1024 * 1024,
            },
        )
        for kind, maximum in MAX_MEDIA_BYTES.items():
            with self.subTest(kind=kind, boundary="accepted"):
                self._upload(kind, size=maximum)
            with self.subTest(kind=kind, boundary="rejected"):
                with self.assertRaises(MediaTooLarge):
                    self._upload(kind, size=maximum + 1)

    def test_completion_consumes_verified_staging_once_and_keeps_urls_ephemeral(self) -> None:
        grant = self._upload()
        intent_payload = asdict(self.repository.intents[grant.intent.id])
        self.assertNotIn("url", repr(intent_payload).lower())
        self.assertIn("signature=secret", grant.target.upload_url)
        self.assertEqual(grant.intent.staging_object.zone, STORAGE_ZONE_STAGING)

        asset = self.service.complete_upload(
            principal=self.sender,
            intent_id=grant.intent.id,
        )

        self.assertEqual(asset.object_ref.zone, STORAGE_ZONE_PRIVATE)
        self.assertNotEqual(
            asset.object_ref.namespace,
            grant.intent.staging_object.namespace,
        )
        self.assertEqual(self.storage.promote_calls, 1)
        with self.assertRaises(UploadAlreadyCompleted):
            self.service.complete_upload(
                principal=self.sender,
                intent_id=grant.intent.id,
            )
        self.assertEqual(self.storage.promote_calls, 1)

    def test_upload_intent_is_bound_to_user_and_deployment(self) -> None:
        grant = self._upload()
        with self.assertRaises(UploadIntentUnavailable):
            self.service.complete_upload(
                principal=self.recipient,
                intent_id=grant.intent.id,
            )

        self.storage.target_override["deployment"] = "staging"
        with self.assertRaises(MediaContractViolation):
            self._upload()

    def test_real_inspection_controls_type_size_hash_and_isolation(self) -> None:
        grant = self._upload()
        self.storage.inspection_override["verified"] = False
        with self.assertRaises(MediaInspectionRejected):
            self.service.complete_upload(
                principal=self.sender, intent_id=grant.intent.id
            )

        self.storage.inspection_override = {"size_bytes": 1025}
        with self.assertRaises(MediaIntegrityMismatch):
            self.service.complete_upload(
                principal=self.sender, intent_id=grant.intent.id
            )

        self.storage.inspection_override = {"sha256": "cd" * 32}
        with self.assertRaises(MediaIntegrityMismatch):
            self.service.complete_upload(
                principal=self.sender, intent_id=grant.intent.id
            )

        self.storage.inspection_override = {
            "detected_kind": "file",
            "detected_content_type": "application/octet-stream",
            "width": None,
            "height": None,
        }
        with self.assertRaises(MediaTypeRejected):
            self.service.complete_upload(
                principal=self.sender, intent_id=grant.intent.id
            )

        self.storage.inspection_override = {}
        self.storage.final_override = {
            "object_ref": replace(
                grant.intent.staging_object,
                zone=STORAGE_ZONE_PRIVATE,
            )
        }
        with self.assertRaises(MediaContractViolation):
            self.service.complete_upload(
                principal=self.sender, intent_id=grant.intent.id
            )

    def test_audio_requires_real_duration_and_accepts_only_through_60_seconds(self) -> None:
        accepted = self._upload("audio")
        self.storage.inspection_override["duration_seconds"] = 60.0
        asset = self.service.complete_upload(
            principal=self.sender,
            intent_id=accepted.intent.id,
        )
        self.assertEqual(asset.duration_seconds, 60.0)

        rejected = self._upload("audio")
        self.storage.inspection_override["duration_seconds"] = 60.001
        with self.assertRaisesRegex(MediaInspectionRejected, "60"):
            self.service.complete_upload(
                principal=self.sender,
                intent_id=rejected.intent.id,
            )

    def test_file_category_cannot_bypass_real_image_type(self) -> None:
        grant = self._upload("file")
        self.storage.inspection_override.update(
            {
                "detected_kind": "image",
                "detected_content_type": "image/jpeg",
                "width": 10,
                "height": 10,
            }
        )
        with self.assertRaises(MediaTypeRejected):
            self.service.complete_upload(
                principal=self.sender,
                intent_id=grant.intent.id,
            )

    def test_office_file_uses_verified_zip_container_instead_of_browser_mime(self) -> None:
        grant = self.service.create_upload_intent(
            principal=self.sender,
            kind="file",
            filename="document.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            size_bytes=1024,
            sha256=DIGEST,
        )
        self.storage.inspection_override["detected_content_type"] = "application/zip"

        asset = self.service.complete_upload(
            principal=self.sender,
            intent_id=grant.intent.id,
        )

        self.assertEqual(asset.filename, "document.docx")
        self.assertEqual(asset.content_type, "application/zip")

    def test_local_attachment_is_authority_and_tim_is_only_url_free_outbox_intent(self) -> None:
        delivery = self._send()

        self.assertTrue(delivery.created)
        self.assertNotEqual(
            delivery.sender_projection_id,
            delivery.recipient_projection_id,
        )
        self.assertEqual(delivery.tim_mirror.message_id, delivery.attachment.message_id)
        self.assertEqual(delivery.tim_mirror.payload, delivery.attachment.payload)
        self.assertEqual(self.storage.issue_read_calls, [])
        persisted = self.repository.persisted_attachment_payloads[-1]
        self.assertNotIn("url", repr(persisted).lower())
        self.assertNotIn("object_ref", persisted)
        self.assertNotIn("bucket", persisted)
        self.assertEqual(persisted["asset_id"], str(delivery.attachment.payload.asset_id))

    def test_send_requires_owner_membership_and_deny_wins(self) -> None:
        asset = self._asset()
        self.policy.message_allowed = False
        with self.assertRaises(MediaThreadForbidden):
            self.service.send_attachment(
                principal=self.sender,
                peer_upstream_uid="9",
                client_message_id="not-authorized",
                asset_id=asset.id,
            )
        self.assertEqual(self.policy.thread_resolve_calls, 0)
        self.assertEqual(self.repository.attachments, {})

        self.policy.message_allowed = True
        self.policy.blocked = True
        with self.assertRaises(MediaBlocked):
            self.service.send_attachment(
                principal=self.sender,
                peer_upstream_uid="9",
                client_message_id="blocked",
                asset_id=asset.id,
            )

        self.policy.blocked = False
        self.policy.invalid_thread = True
        with self.assertRaises(MediaThreadForbidden):
            self.service.send_attachment(
                principal=self.sender,
                peer_upstream_uid="9",
                client_message_id="not-member",
                asset_id=asset.id,
            )

        self.policy.invalid_thread = False
        foreign = replace(asset, id=uuid.uuid4(), owner_user_id=self.policy.recipient.user_id)
        self.repository.assets[foreign.id] = foreign
        with self.assertRaises(MediaAssetUnavailable):
            self.service.send_attachment(
                principal=self.sender,
                peer_upstream_uid="9",
                client_message_id="foreign-asset",
                asset_id=foreign.id,
            )

    def test_access_requires_participant_active_thread_and_no_blacklist(self) -> None:
        delivery = self._send()
        access = self.service.request_access(
            principal=self.recipient,
            attachment_id=delivery.attachment.id,
        )
        self.assertIn("signature=secret", access.url)
        self.assertEqual(access.asset_id, delivery.attachment.payload.asset_id)

        with self.assertRaises(MediaAccessDenied):
            self.service.request_access(
                principal=self.intruder,
                attachment_id=delivery.attachment.id,
            )

        self.policy.blocked = True
        with self.assertRaises(MediaBlocked):
            self.service.request_access(
                principal=self.recipient,
                attachment_id=delivery.attachment.id,
            )

        self.policy.blocked = False
        self.policy.invalid_thread = True
        with self.assertRaises(MediaThreadForbidden):
            self.service.request_access(
                principal=self.recipient,
                attachment_id=delivery.attachment.id,
            )

    def test_revoke_is_sender_only_two_minutes_exact_and_idempotent(self) -> None:
        recipient_delivery = self._send()
        with self.assertRaises(MediaRevocationDenied):
            self.service.revoke_attachment(
                principal=self.recipient,
                attachment_id=recipient_delivery.attachment.id,
            )

        accepted = self._send()
        self.now = accepted.attachment.sent_at + timedelta(
            seconds=REVOKE_WINDOW_SECONDS
        )
        revoked = self.service.revoke_attachment(
            principal=self.sender,
            attachment_id=accepted.attachment.id,
        )
        self.assertTrue(revoked.created)
        self.assertEqual(revoked.attachment.status, ATTACHMENT_STATUS_REVOKED)

        self.now += timedelta(days=1)
        repeated = self.service.revoke_attachment(
            principal=self.sender,
            attachment_id=accepted.attachment.id,
        )
        self.assertFalse(repeated.created)

        self.now = NOW
        expired = self._send()
        self.now = expired.attachment.sent_at + timedelta(
            seconds=REVOKE_WINDOW_SECONDS + 1
        )
        with self.assertRaises(MediaRevocationExpired):
            self.service.revoke_attachment(
                principal=self.sender,
                attachment_id=expired.attachment.id,
            )

    def test_flash_is_image_only_requires_one_recipient_claim_and_five_seconds(self) -> None:
        with self.assertRaisesRegex(InvalidMediaRequest, "闪照"):
            self._send(kind="video", flash=True)

        delivery = self._send(flash=True)
        with self.assertRaises(FlashClaimRequired):
            self.service.request_access(
                principal=self.recipient,
                attachment_id=delivery.attachment.id,
            )
        with self.assertRaises(FlashClaimDenied):
            self.service.claim_flash(
                principal=self.sender,
                attachment_id=delivery.attachment.id,
            )

        result = self.service.claim_flash(
            principal=self.recipient,
            attachment_id=delivery.attachment.id,
        )
        self.assertEqual(
            (result.claim.display_until - result.claim.claimed_at).total_seconds(),
            FLASH_DISPLAY_SECONDS,
        )
        self.assertEqual(result.access.expires_at, result.claim.display_until)
        self.assertEqual(result.access.display_until, result.claim.display_until)
        self.assertIn("signature=secret", result.access.url)
        with self.assertRaises(FlashAlreadyClaimed):
            self.service.claim_flash(
                principal=self.recipient,
                attachment_id=delivery.attachment.id,
            )


if __name__ == "__main__":
    unittest.main()
