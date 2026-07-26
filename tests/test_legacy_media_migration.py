from __future__ import annotations

import io
import inspect
import hashlib
import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bbw_web.legacy_media_reference import legacy_source_hash
from bbw_web.media_archive import MediaArchiveError, _sniff
from bbw_web.media_native.legacy_migration import (
    ArchivedMedia,
    ImportWindow,
    LegacyMediaAccount,
    LegacyMediaDataError,
    LegacyMediaImportSummary,
    LegacyMediaLimitError,
    LegacyMediaMigration,
    LegacyMediaPlan,
    LegacyMediaSource,
    LegacyMediaVerificationError,
    LegacyMessageIngestResult,
    PlannedResource,
    R2LegacyMediaArchiver,
    SqlAlchemyLegacyMediaWriter,
    SqlAlchemyLegacyMessageIngestor,
    fetch_complete_roaming_history,
    local_media_sidecar,
    main,
    media_record_digest,
    message_media_sources,
    profile_media_sources,
    social_post_media_sources,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
OWNER_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000102")
MEDIA_ID = uuid.UUID("00000000-0000-0000-0000-000000000103")


def raw_message(sender: str, recipient: str, *, key: str, timestamp: int) -> dict:
    return {
        "From_Account": sender,
        "To_Account": recipient,
        "MsgTimeStamp": timestamp,
        "MsgKey": key,
        "MsgBody": [
            {"MsgType": "TIMTextElem", "MsgContent": {"Text": key}}
        ],
    }


class StrictHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = ImportWindow(NOW - timedelta(days=2), NOW)
        self.timestamp = int((NOW - timedelta(hours=1)).timestamp())

    def test_both_directions_require_explicit_complete_and_advance_cursor(self) -> None:
        class Reader:
            def __init__(inner_self) -> None:
                inner_self.calls = []

            def fetch_page(inner_self, **values):
                inner_self.calls.append(values)
                sender = values["sender_uid"]
                recipient = values["recipient_uid"]
                cursor = values["last_msg_key"]
                if sender == "9" and not cursor:
                    return {
                        "Complete": 0,
                        "LastMsgKey": "next-in",
                        "MsgList": [
                            raw_message(
                                sender,
                                recipient,
                                key="incoming-1",
                                timestamp=self.timestamp,
                            )
                        ],
                    }
                return {
                    "Complete": 1,
                    "MsgList": [
                        raw_message(
                            sender,
                            recipient,
                            key=f"{sender}-{cursor or 'last'}",
                            timestamp=self.timestamp,
                        )
                    ],
                }

        reader = Reader()
        snapshot = fetch_complete_roaming_history(
            reader,
            account_uid="42",
            peers=("9",),
            window=self.window,
        )

        self.assertEqual(snapshot.page_count, 3)
        self.assertEqual(snapshot.message_count, 3)
        self.assertEqual(len(snapshot.directions), 2)
        self.assertEqual(reader.calls[1]["last_msg_key"], "next-in")
        self.assertEqual(len(snapshot.record_digest), 64)

    def test_empty_incomplete_page_is_not_treated_as_terminal(self) -> None:
        class Reader:
            def fetch_page(self, **_values):
                return {"Complete": 0, "LastMsgKey": "", "MsgList": []}

        with self.assertRaises(LegacyMediaDataError):
            fetch_complete_roaming_history(
                Reader(), account_uid="42", peers=("9",), window=self.window
            )

    def test_repeated_cursor_fails_closed(self) -> None:
        class Reader:
            def fetch_page(self, **values):
                return {
                    "Complete": 0,
                    "LastMsgKey": values["last_msg_key"] or "same",
                    "MsgList": [
                        raw_message(
                            values["sender_uid"],
                            values["recipient_uid"],
                            key="one",
                            timestamp=self_timestamp,
                        )
                    ],
                }

        self_timestamp = self.timestamp
        with self.assertRaises(LegacyMediaDataError):
            fetch_complete_roaming_history(
                Reader(), account_uid="42", peers=("9",), window=self.window
            )

    def test_unknown_item_shape_and_page_limit_fail_closed(self) -> None:
        class UnknownReader:
            def fetch_page(self, **_values):
                return {"Complete": 1, "MsgList": ["not-an-object"]}

        with self.assertRaises(LegacyMediaDataError):
            fetch_complete_roaming_history(
                UnknownReader(), account_uid="42", peers=("9",), window=self.window
            )

        class EndlessReader:
            def fetch_page(self, **values):
                return {
                    "Complete": 0,
                    "LastMsgKey": f"next-{values['last_msg_key']}",
                    "MsgList": [
                        raw_message(
                            values["sender_uid"],
                            values["recipient_uid"],
                            key=str(values["last_msg_key"]),
                            timestamp=self_timestamp,
                        )
                    ],
                }

        self_timestamp = self.timestamp
        with self.assertRaises(LegacyMediaLimitError):
            fetch_complete_roaming_history(
                EndlessReader(),
                account_uid="42",
                peers=("9",),
                window=self.window,
                max_pages_per_direction=2,
            )


class SourceAndSidecarTests(unittest.TestCase):
    def test_profile_post_and_message_slots_use_shared_source_hash(self) -> None:
        avatar = "https://media.example/avatar.jpg?version=2"
        profile = PlannedResource("profile", str(OWNER_ID), {"avatar": avatar})
        profile_source = profile_media_sources(profile)[0]
        self.assertEqual(profile_source.slot, "avatar")
        self.assertEqual(profile_source.source_hash, legacy_source_hash(avatar))

        post = PlannedResource(
            "social_post",
            str(uuid.uuid4()),
            {
                "pictures": ["https://media.example/one.jpg"],
                "video": "https://media.example/video.mp4",
                "cover": "https://media.example/cover.jpg",
            },
        )
        self.assertEqual(
            [source.slot for source in social_post_media_sources(post)],
            ["pictures[0]", "video", "cover"],
        )

        message_id = uuid.uuid4()
        message_sources = message_media_sources(
            message_id=message_id,
            media_report={
                "url": "https://media.example/video.mp4",
                "thumbnail": "https://media.example/thumb.jpg",
                "name": "video.mp4",
            },
            message_kind="video",
            retention_expires_at=NOW + timedelta(days=30),
        )
        self.assertEqual(
            [source.slot for source in message_sources],
            ["media_report.url", "media_report.thumbnail"],
        )
        self.assertEqual([source.kind for source in message_sources], ["video", "image"])

    def test_sidecar_is_sorted_url_free_and_digest_is_replayable(self) -> None:
        records = [
            ArchivedMedia(
                resource_type="profile",
                resource_id=str(OWNER_ID),
                slot="avatar",
                source_hash="a" * 64,
                media_id=MEDIA_ID,
                owner_user_id=OWNER_ID,
                bucket="private",
                object_key="legacy-media/object",
                kind="image",
                content_type="image/jpeg",
                size_bytes=100,
                sha256="b" * 64,
            )
        ]
        sidecar = local_media_sidecar(records)
        encoded = json.dumps(sidecar, sort_keys=True)
        self.assertNotIn("https://", encoded)
        self.assertEqual(sidecar["schema"], 1)
        self.assertEqual(sidecar["items"][0]["media_id"], str(MEDIA_ID))
        self.assertEqual(media_record_digest(records), media_record_digest(list(records)))


class MigrationOrchestrationTests(unittest.TestCase):
    def make_plan(self) -> LegacyMediaPlan:
        source_url = "https://media.example/avatar.jpg"
        source = LegacyMediaSource(
            resource_type="profile",
            resource_id=str(OWNER_ID),
            slot="avatar",
            source_url=source_url,
            source_hash=legacy_source_hash(source_url),
            kind="image",
            original_name="",
            retention_expires_at=datetime(9999, 12, 31, tzinfo=UTC),
        )
        return LegacyMediaPlan(
            account=LegacyMediaAccount(OWNER_ID, ACCOUNT_ID, "42", 180),
            window=ImportWindow(NOW - timedelta(days=180), NOW),
            peers=("9",),
            resources=(PlannedResource("profile", str(OWNER_ID), {"avatar": source_url}),),
            sources=(source,),
            social_record_digest="1" * 64,
            moments_record_digest="2" * 64,
            peer_digest="3" * 64,
        )

    def make_artifact(self, source: LegacyMediaSource) -> ArchivedMedia:
        return ArchivedMedia(
            resource_type=source.resource_type,
            resource_id=source.resource_id,
            slot=source.slot,
            source_hash=source.source_hash,
            media_id=MEDIA_ID,
            owner_user_id=OWNER_ID,
            bucket="private",
            object_key="legacy-media/object",
            kind="image",
            content_type="image/jpeg",
            size_bytes=100,
            sha256="b" * 64,
        )

    def test_network_read_occurs_after_short_begin_and_marker_after_head_verify(self) -> None:
        events = []
        plan = self.make_plan()
        artifact = self.make_artifact(plan.sources[0])

        class Writer:
            def prepare(self, _owner, *, ended_at):
                events.append("db-prepare")
                return plan

            def begin(self, _plan):
                events.append("db-begin")

            def bind(self, _plan, _sources, _artifacts):
                events.append("db-bind")

            def verification_records(self, _plan, _sources):
                events.append("db-list-verification")
                return (artifact,)

            def complete(self, _plan, _history, records):
                events.append("db-complete-marker")
                return {"metadata": 1, "objects": 1}, media_record_digest(records)

            def fail(self, _plan, *, code):
                events.append(("db-fail", code))

        class Reader:
            def fetch_page(self, **values):
                events.append("network-tim")
                return {"Complete": 1, "MsgList": []}

        class Ingestor:
            def ingest(self, _plan, _history):
                events.append("db-ingest")
                return LegacyMessageIngestResult(0, ())

        class Archiver:
            def archive(self, _account, _source):
                events.append("r2-archive")
                return artifact, True

            def verify(self, _artifact):
                events.append("r2-head-verify")
                return True

        summary = LegacyMediaMigration(
            reader=Reader(),
            ingestor=Ingestor(),
            archiver=Archiver(),
            writer=Writer(),
            clock=lambda: NOW,
        ).run(OWNER_ID)

        self.assertEqual(summary.metadata_records, 1)
        self.assertLess(events.index("db-begin"), events.index("network-tim"))
        self.assertLess(events.index("network-tim"), events.index("db-ingest"))
        self.assertLess(events.index("r2-head-verify"), events.index("db-complete-marker"))

    def test_failed_object_verification_never_writes_completion_marker(self) -> None:
        plan = self.make_plan()
        artifact = self.make_artifact(plan.sources[0])
        completed = []
        failures = []

        class Writer:
            def prepare(self, _owner, *, ended_at):
                return plan

            def begin(self, _plan):
                return None

            def bind(self, _plan, _sources, _artifacts):
                return None

            def verification_records(self, _plan, _sources):
                return (artifact,)

            def complete(self, *_args):
                completed.append(True)
                return {"metadata": 1, "objects": 1}, "0" * 64

            def fail(self, _plan, *, code):
                failures.append(code)

        class Reader:
            def fetch_page(self, **_values):
                return {"Complete": 1, "MsgList": []}

        class Ingestor:
            def ingest(self, _plan, _history):
                return LegacyMessageIngestResult(0, ())

        class Archiver:
            def archive(self, _account, _source):
                return artifact, False

            def verify(self, _artifact):
                return False

        with self.assertRaises(LegacyMediaVerificationError):
            LegacyMediaMigration(
                reader=Reader(),
                ingestor=Ingestor(),
                archiver=Archiver(),
                writer=Writer(),
                clock=lambda: NOW,
            ).run(OWNER_ID)
        self.assertEqual(completed, [])
        self.assertEqual(failures, ["legacy_media_verification_failed"])


class R2FailureConvergenceTests(unittest.TestCase):
    def make_source(self) -> LegacyMediaSource:
        url = "https://media.example/file.jpg"
        return LegacyMediaSource(
            resource_type="profile",
            resource_id=str(OWNER_ID),
            slot="avatar",
            source_url=url,
            source_hash=legacy_source_hash(url),
            kind="image",
            original_name="",
            retention_expires_at=datetime(9999, 12, 31, tzinfo=UTC),
        )

    def make_row(self, source: LegacyMediaSource):
        return SimpleNamespace(
            id=MEDIA_ID,
            owner_user_id=OWNER_ID,
            message_id=None,
            kind="image",
            status="uploading",
            r2_bucket="private",
            r2_object_key="legacy-media/stale",
            content_type="image/jpeg",
            size_bytes=100,
            sha256="b" * 64,
            extra_data={
                "legacy_media": {
                    "schema": 1,
                    "source_hash": source.source_hash,
                    "slot": source.slot,
                    "resource_type": source.resource_type,
                    "resource_id": source.resource_id,
                    "access_scope": source.resource_type,
                }
            },
        )

    def test_stale_object_is_deleted_before_quota_release(self) -> None:
        events = []
        source = self.make_source()
        row = self.make_row(source)

        class Storage:
            bucket = "private"

            def head_object(self, _key):
                events.append("head")
                return {"size": 99, "metadata": {"sha256": "c" * 64}}

            def delete(self, _key):
                events.append("delete")

        class Db:
            def scalar(self, _statement):
                events.append("db-lock")
                return row

        @contextmanager
        def db_scope():
            yield Db()

        class Quota:
            def __init__(self, _db, _settings):
                return None

            def release(self, **_values):
                events.append("release")

        archiver = R2LegacyMediaArchiver.__new__(R2LegacyMediaArchiver)
        archiver.storage = Storage()
        archiver.db_scope = db_scope
        archiver.settings = object()
        archiver.clock = lambda: NOW
        with patch(
            "bbw_web.media_native.legacy_migration.MediaQuotaService", Quota
        ):
            result = archiver._repair_existing(
                LegacyMediaAccount(OWNER_ID, ACCOUNT_ID, "42", 180),
                source,
                row,
            )
        self.assertIsNone(result)
        self.assertEqual(events, ["head", "delete", "db-lock", "release"])

    def test_delete_failure_keeps_uploading_row_and_quota_reserved(self) -> None:
        events = []
        source = self.make_source()
        row = self.make_row(source)

        class Storage:
            bucket = "private"

            def head_object(self, _key):
                return {"size": 99, "metadata": {"sha256": "c" * 64}}

            def delete(self, _key):
                events.append("delete")
                raise RuntimeError("R2 unavailable")

        @contextmanager
        def forbidden_db_scope():
            events.append("db-opened")
            yield object()

        archiver = R2LegacyMediaArchiver.__new__(R2LegacyMediaArchiver)
        archiver.storage = Storage()
        archiver.db_scope = forbidden_db_scope
        archiver.settings = object()
        archiver.clock = lambda: NOW
        with self.assertRaises(Exception):
            archiver._repair_existing(
                LegacyMediaAccount(OWNER_ID, ACCOUNT_ID, "42", 180),
                source,
                row,
            )
        self.assertEqual(events, ["delete"])

    def test_new_upload_cleanup_does_not_release_when_delete_fails(self) -> None:
        events = []
        source = self.make_source()

        class Prepared:
            size = 100
            sha256 = "b" * 64
            content_type = "image/jpeg"
            extension = "jpg"
            original_name = ""

            class Path:
                @contextmanager
                def open(self, _mode):
                    yield io.BytesIO(b"content")

            path = Path()

            def cleanup(self):
                events.append("cleanup")

        class Storage:
            bucket = "private"

            def object_key(self, *_args, **_kwargs):
                return "legacy-media/new"

            def upload_stream(self, **_values):
                raise RuntimeError("upload failed")

            def delete(self, _key):
                events.append("delete")
                raise RuntimeError("delete failed")

        media = SimpleNamespace(
            id=MEDIA_ID,
            status="pending",
            retention_expires_at=NOW,
        )

        class Quota:
            def __init__(self, _db, _settings):
                return None

            def reserve(self, **_values):
                events.append("reserve")
                return SimpleNamespace(media=media)

            def release(self, **_values):
                events.append("release")

        @contextmanager
        def db_scope():
            yield object()

        archiver = R2LegacyMediaArchiver.__new__(R2LegacyMediaArchiver)
        archiver.storage = Storage()
        archiver.db_scope = db_scope
        archiver.settings = object()
        archiver.clock = lambda: NOW
        archiver.allowed_hosts = ("media.example",)
        archiver._existing = lambda _account, _source: None
        with (
            patch(
                "bbw_web.media_native.legacy_migration.download_and_prepare",
                return_value=Prepared(),
            ),
            patch(
                "bbw_web.media_native.legacy_migration.MediaQuotaService", Quota
            ),
            self.assertRaises(Exception),
        ):
            archiver.archive(
                LegacyMediaAccount(OWNER_ID, ACCOUNT_ID, "42", 180), source
            )
        self.assertEqual(events, ["reserve", "cleanup", "delete"])


class LegacyArchiveOutboxTests(unittest.TestCase):
    def make_plan(self) -> LegacyMediaPlan:
        return LegacyMediaPlan(
            account=LegacyMediaAccount(OWNER_ID, ACCOUNT_ID, "42", 180),
            window=ImportWindow(NOW - timedelta(days=180), NOW),
            peers=(),
            resources=(),
            sources=(),
            social_record_digest="1" * 64,
            moments_record_digest="2" * 64,
            peer_digest="3" * 64,
        )

    def test_strict_ingestor_disables_duplicate_legacy_archive_enqueue(self) -> None:
        from bbw_web.jobs import _ingest_message

        parameter = inspect.signature(_ingest_message).parameters[
            "enqueue_media_archive"
        ]
        self.assertIs(parameter.default, True)
        source = inspect.getsource(SqlAlchemyLegacyMessageIngestor.ingest)
        self.assertIn("enqueue_media_archive=False", source)

    def test_exact_preexisting_archive_work_is_completed_only_after_verified_record(self) -> None:
        message_id = uuid.uuid4()
        source_url = "https://media.example/history.jpg?token=upstream"
        outbox_digest = hashlib.sha256(
            f"{message_id}\n{source_url}".encode("utf-8")
        ).hexdigest()
        outbox = SimpleNamespace(
            status="pending",
            payload={"message_id": str(message_id), "source_url_hash": outbox_digest},
            aggregate_id=str(message_id),
            idempotency_key=outbox_digest,
            completed_at=None,
            locked_by="worker",
            locked_until=NOW,
            last_error="retry",
        )
        message = SimpleNamespace(
            id=message_id,
            extra_data={"media_report": {"url": source_url}},
        )
        record = ArchivedMedia(
            resource_type="message",
            resource_id=str(message_id),
            slot="media_report.url",
            source_hash=legacy_source_hash(source_url),
            media_id=MEDIA_ID,
            owner_user_id=OWNER_ID,
            bucket="private",
            object_key="legacy-media/history",
            kind="image",
            content_type="image/jpeg",
            size_bytes=100,
            sha256="b" * 64,
            message_id=message_id,
        )

        class Db:
            def __init__(self):
                self.calls = 0

            def scalars(self, _statement):
                self.calls += 1
                return [outbox] if self.calls == 1 else [message]

        SqlAlchemyLegacyMediaWriter._finish_verified_archive_outboxes(
            Db(), self.make_plan(), (record,), completed_at=NOW
        )
        self.assertEqual(outbox.status, "completed")
        self.assertEqual(outbox.completed_at, NOW)
        self.assertIsNone(outbox.locked_by)
        self.assertIsNone(outbox.last_error)

    def test_unmatched_required_archive_work_blocks_media_marker(self) -> None:
        outbox = SimpleNamespace(
            status="pending",
            payload={"message_id": str(uuid.uuid4())},
            aggregate_id="",
            idempotency_key="wrong",
        )

        class Db:
            def __init__(self):
                self.calls = 0

            def scalars(self, _statement):
                self.calls += 1
                return [outbox] if self.calls == 1 else []

        with self.assertRaises(LegacyMediaVerificationError):
            SqlAlchemyLegacyMediaWriter._finish_verified_archive_outboxes(
                Db(), self.make_plan(), (), completed_at=NOW
            )


class CliPrivacyTests(unittest.TestCase):
    def test_all_active_output_contains_only_aggregate_counts(self) -> None:
        secret_uid = "private-account-uid"
        secret_url = "https://media.example/private.jpg?token=secret"
        summary = LegacyMediaImportSummary(
            history_pages=2,
            history_messages=3,
            messages_written=3,
            metadata_records=1,
            object_records=1,
            archived_created=1,
            archived_reused=0,
            record_digest="a" * 64,
        )
        output = io.StringIO()
        with (
            patch(
                "bbw_web.media_native.legacy_migration._active_owner_ids",
                return_value=(OWNER_ID,),
            ),
            patch(
                "bbw_web.media_native.legacy_migration.run_legacy_media_import",
                return_value=summary,
            ),
        ):
            status = main(["--all-active"], stdout=output)
        payload = output.getvalue()
        self.assertEqual(status, 0)
        self.assertNotIn(str(OWNER_ID), payload)
        self.assertNotIn(secret_uid, payload)
        self.assertNotIn(secret_url, payload)
        self.assertEqual(json.loads(payload)["accounts_completed"], 1)


class MediaSniffTests(unittest.TestCase):
    """媒体魔数嗅探契约：音频魔数必须先于 "#!" 活性内容拒绝命中，
    且 ftyp 纯音频品牌不得被判为 video。"""

    def sniff_bytes(self, head: bytes) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blob.bin"
            target.write_bytes(head)
            return _sniff(target)

    def test_amr_magic_is_audio_and_wins_over_shebang_rejection(self) -> None:
        content_type, extension = self.sniff_bytes(b"#!AMR\n" + b"\x3c\x54\x91" * 16)
        self.assertEqual((content_type, extension), ("audio/amr", "amr"))

    def test_amr_wb_magic_is_audio_and_wins_over_shebang_rejection(self) -> None:
        content_type, extension = self.sniff_bytes(b"#!AMR-WB\n" + b"\x44\x35" * 16)
        self.assertEqual((content_type, extension), ("audio/amr", "amr"))

    def test_ftyp_pure_audio_brands_map_to_audio_mp4(self) -> None:
        for brand in (b"M4A ", b"M4B "):
            with self.subTest(brand=brand):
                head = b"\x00\x00\x00\x20ftyp" + brand + b"\x00\x00\x00\x00mp42isom"
                self.assertEqual(self.sniff_bytes(head), ("audio/mp4", "m4a"))

    def test_ftyp_video_brands_keep_video_content_types(self) -> None:
        quicktime = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8
        self.assertEqual(self.sniff_bytes(quicktime), ("video/quicktime", "mov"))
        isom = b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x02\x00isomiso2"
        self.assertEqual(self.sniff_bytes(isom), ("video/mp4", "mp4"))

    def test_plain_shebang_is_still_rejected_as_active_content(self) -> None:
        with self.assertRaises(MediaArchiveError):
            self.sniff_bytes(b"#!/bin/sh\nrm -rf /\n")
        # 前置空白不能绕开活性内容拒绝。
        with self.assertRaises(MediaArchiveError):
            self.sniff_bytes(b"  #!/usr/bin/env python\nprint('x')\n")

    def test_amr_carveout_is_exact_and_does_not_leak_to_other_shebangs(self) -> None:
        # 只有完整魔数 "#!AMR\n" / "#!AMR-WB\n" 可以豁免；
        # 伪装前缀（缺少换行）仍必须按活性内容拒绝。
        with self.assertRaises(MediaArchiveError):
            self.sniff_bytes(b"#!AMRfake\necho pwned\n")


if __name__ == "__main__":
    unittest.main()
