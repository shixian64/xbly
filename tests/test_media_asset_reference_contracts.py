from __future__ import annotations

import inspect
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from bbw_prod.models import MediaAssetReference, SocialPost
from bbw_web import media_api
from bbw_web.media_native.references import (
    BoundMediaAssetReference,
    MediaAssetReferenceAssetUnavailable,
    MediaAssetReferenceConflict,
    MediaAssetReferenceSlotInvalid,
    RELEASED_ASSET_CLEANUP_GRACE_SECONDS,
    SqlAlchemyMediaAssetReferenceRepository,
    native_media_content_asset_id,
    native_media_content_path,
)
from bbw_web.media_native.repository import SqlAlchemyMediaNativeRepository


ROOT = Path(__file__).resolve().parents[1]


def _asset(owner_user_id: uuid.UUID, *, kind: str = "image") -> SimpleNamespace:
    asset_id = uuid.uuid4()
    return SimpleNamespace(
        id=asset_id,
        owner_user_id=owner_user_id,
        deployment="production",
        kind=kind,
        content_type="image/jpeg" if kind == "image" else "video/mp4",
        size_bytes=1024,
        private_bucket="private-media",
        private_namespace="production/web-media-private",
        private_object_key=f"production/web-media-private/{owner_user_id}/{asset_id}",
        status="available",
        expires_at=None,
    )


def _bound_reference(
    *,
    owner_user_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    slot: str,
) -> BoundMediaAssetReference:
    asset_id = uuid.uuid4()
    return BoundMediaAssetReference(
        reference_id=uuid.uuid4(),
        asset_id=asset_id,
        owner_user_id=owner_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        slot=slot,
        kind="image",
        content_type="image/jpeg",
        size_bytes=1024,
        private_bucket="private-media",
        private_object_key=f"production/web-media-private/{owner_user_id}/{asset_id}",
    )


def _profile_avatar_reference(
    owner_user_id: uuid.UUID, asset_id: uuid.UUID
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        asset_id=asset_id,
        owner_user_id=owner_user_id,
        resource_type="profile",
        profile_user_id=owner_user_id,
        social_post_id=None,
        slot="avatar",
    )


def _social_post_reference(
    owner_user_id: uuid.UUID,
    social_post_id: uuid.UUID,
    asset_id: uuid.UUID,
    slot: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        asset_id=asset_id,
        owner_user_id=owner_user_id,
        resource_type="social_post",
        profile_user_id=None,
        social_post_id=social_post_id,
        slot=slot,
    )


class MediaAssetReferenceSchemaTests(unittest.TestCase):
    def test_model_is_owner_bound_slot_unique_and_url_free(self) -> None:
        self.assertEqual(MediaAssetReference.__tablename__, "media_asset_references")
        constraints = {
            constraint.name for constraint in MediaAssetReference.__table__.constraints
        }
        self.assertIn("uq_media_asset_references_asset", constraints)
        self.assertIn("uq_media_asset_references_profile_slot", constraints)
        self.assertIn("uq_media_asset_references_social_post_slot", constraints)
        self.assertIn("fk_media_asset_references_asset_owner", constraints)
        self.assertIn("fk_media_asset_references_post_owner", constraints)
        self.assertTrue(
            any(
                str(name).endswith("media_asset_reference_target_slot_valid")
                for name in constraints
            )
        )
        self.assertTrue(
            any(
                str(name).endswith("media_asset_reference_slot_not_url")
                for name in constraints
            )
        )
        self.assertFalse(
            any("url" in column.name for column in MediaAssetReference.__table__.columns)
        )
        social_constraints = {
            constraint.name for constraint in SocialPost.__table__.constraints
        }
        self.assertIn("uq_social_posts_id_author", social_constraints)

    def test_migration_0017_is_linear_reversible_and_has_matching_constraints(self) -> None:
        source = (
            ROOT
            / "migrations"
            / "versions"
            / "20260725_0017_media_asset_references.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision: str = "20260725_0017"', source)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260725_0016"',
            source,
        )
        for value in (
            "media_asset_references",
            "uq_media_asset_references_asset",
            "uq_media_asset_references_profile_slot",
            "uq_media_asset_references_social_post_slot",
            "fk_media_asset_references_asset_owner",
            "fk_media_asset_references_post_owner",
            "uq_social_posts_id_author",
        ):
            self.assertIn(value, source)
        self.assertIn("position('://' in slot) = 0", source)
        self.assertLess(
            source.index('op.drop_table("media_asset_references")'),
            source.index('op.drop_constraint(\n        op.f("uq_social_posts_id_author")'),
        )


class _SequenceDB:
    def __init__(self, *, scalar_values=(), scalars_values=()) -> None:
        self.scalar_values = list(scalar_values)
        self.scalars_values = list(scalars_values)
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.flushes = 0
        self.statements: list[object] = []

    def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_values.pop(0)

    def scalars(self, statement):
        self.statements.append(statement)
        return list(self.scalars_values.pop(0))

    def execute(self, statement):
        self.statements.append(statement)
        return []

    def add(self, row) -> None:
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.added.append(row)

    def delete(self, row) -> None:
        self.deleted.append(row)

    def flush(self) -> None:
        self.flushes += 1


class MediaAssetReferenceRepositoryTests(unittest.TestCase):
    def test_profile_replacement_is_atomic_and_returns_fixed_native_path(self) -> None:
        owner_id = uuid.uuid4()
        old_asset_id = uuid.uuid4()
        new_asset = _asset(owner_id)
        old_reference = SimpleNamespace(
            id=uuid.uuid4(),
            asset_id=old_asset_id,
            owner_user_id=owner_id,
            resource_type="profile",
            profile_user_id=owner_id,
            social_post_id=None,
            slot="avatar",
        )
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=owner_id)],
            scalars_values=[
                [old_reference],
                [new_asset],
                [],
                [old_reference],
            ],
        )
        bound = SqlAlchemyMediaAssetReferenceRepository(db).bind_profile_avatar(
            owner_user_id=owner_id,
            asset_id=new_asset.id,
        )
        self.assertEqual(db.deleted, [old_reference])
        self.assertEqual(len(db.added), 1)
        self.assertEqual(bound.asset_id, new_asset.id)
        self.assertIsNone(new_asset.expires_at)
        self.assertEqual(
            bound.content_path,
            f"/api/media/native/{new_asset.id}/content",
        )
        self.assertEqual(native_media_content_path(new_asset.id), bound.content_path)
        self.assertNotEqual(
            bound.content_path,
            f"/api/media/native/{old_asset_id}/content",
        )

    def test_binding_rejects_any_asset_that_is_already_a_private_attachment(self) -> None:
        owner_id = uuid.uuid4()
        asset = _asset(owner_id)
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=owner_id)],
            scalars_values=[
                [],
                [asset],
                [asset.id],
            ],
        )
        with self.assertRaises(MediaAssetReferenceAssetUnavailable):
            SqlAlchemyMediaAssetReferenceRepository(db).bind_profile_avatar(
                owner_user_id=owner_id,
                asset_id=asset.id,
            )
        self.assertEqual(db.added, [])

    def test_social_batch_validates_kinds_and_preserves_same_binding_ids(self) -> None:
        owner_id = uuid.uuid4()
        post_id = uuid.uuid4()
        picture = _asset(owner_id)
        video = _asset(owner_id, kind="video")
        picture_ref = SimpleNamespace(
            id=uuid.uuid4(),
            asset_id=picture.id,
            owner_user_id=owner_id,
            resource_type="social_post",
            profile_user_id=None,
            social_post_id=post_id,
            slot="pictures[0]",
        )
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=post_id, status="published")],
            scalars_values=[
                [picture_ref],
                [picture, video],
                [],
                [picture_ref],
            ],
        )
        bound = SqlAlchemyMediaAssetReferenceRepository(db).bind_social_post_assets(
            owner_user_id=owner_id,
            social_post_id=post_id,
            assets_by_slot={"pictures[0]": picture.id, "video": video.id},
        )
        self.assertEqual(bound["pictures[0]"].reference_id, picture_ref.id)
        self.assertEqual(bound["video"].kind, "video")
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.deleted, [])
        snapshot_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
        asset_lock_sql = str(db.statements[2].compile(dialect=postgresql.dialect()))
        reference_lock_sql = str(db.statements[4].compile(dialect=postgresql.dialect()))
        self.assertNotIn("FOR UPDATE", snapshot_sql)
        self.assertIn("FOR UPDATE", asset_lock_sql)
        self.assertIn("FOR UPDATE", reference_lock_sql)

    def test_social_preview_derives_ordered_slots_without_prelocking_assets(self) -> None:
        owner_id = uuid.uuid4()
        first = _asset(owner_id)
        second = _asset(owner_id)
        db = _SequenceDB(scalars_values=[[first, second], []])
        preview = SqlAlchemyMediaAssetReferenceRepository(
            db,
            deployment="production",
            private_bucket="private-media",
        ).preview_social_post_asset_ids(
            owner_user_id=owner_id,
            asset_ids=[first.id, second.id],
        )
        self.assertEqual(
            list(preview),
            ["pictures[0]", "pictures[1]"],
        )
        asset_sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
        self.assertNotIn("FOR UPDATE", asset_sql)

    def test_social_preview_rejects_invalid_counts_duplicates_and_kind_mix(self) -> None:
        owner_id = uuid.uuid4()
        repository = SqlAlchemyMediaAssetReferenceRepository(
            _SequenceDB(),
            deployment="production",
            private_bucket="private-media",
        )
        with self.assertRaises(MediaAssetReferenceSlotInvalid):
            repository.preview_social_post_asset_ids(
                owner_user_id=owner_id,
                asset_ids=[uuid.uuid4() for _ in range(10)],
            )
        duplicate = uuid.uuid4()
        with self.assertRaises(MediaAssetReferenceConflict):
            repository.preview_social_post_asset_ids(
                owner_user_id=owner_id,
                asset_ids=[duplicate, duplicate],
            )

        picture = _asset(owner_id)
        video = _asset(owner_id, kind="video")
        mixed = SqlAlchemyMediaAssetReferenceRepository(
            _SequenceDB(scalars_values=[[picture, video], []]),
            deployment="production",
            private_bucket="private-media",
        )
        with self.assertRaises(MediaAssetReferenceAssetUnavailable):
            mixed.preview_social_post_asset_ids(
                owner_user_id=owner_id,
                asset_ids=[picture.id, video.id],
            )

        audio = _asset(owner_id, kind="audio")
        unsupported = SqlAlchemyMediaAssetReferenceRepository(
            _SequenceDB(scalars_values=[[audio], []]),
            deployment="production",
            private_bucket="private-media",
        )
        with self.assertRaises(MediaAssetReferenceAssetUnavailable):
            unsupported.preview_social_post_asset_ids(
                owner_user_id=owner_id,
                asset_ids=[audio.id],
            )

    def test_reference_binding_rejects_wrong_deployment_bucket_and_namespace(self) -> None:
        owner_id = uuid.uuid4()
        cases = []
        wrong_deployment = _asset(owner_id)
        wrong_deployment.deployment = "staging"
        cases.append(wrong_deployment)
        wrong_bucket = _asset(owner_id)
        wrong_bucket.private_bucket = "other-media"
        cases.append(wrong_bucket)
        wrong_namespace = _asset(owner_id)
        wrong_namespace.private_namespace = "production/other-private"
        cases.append(wrong_namespace)
        wrong_key = _asset(owner_id)
        wrong_key.private_object_key = (
            f"production/web-media-private/{uuid.uuid4()}/{wrong_key.id}/source.jpg"
        )
        cases.append(wrong_key)

        for asset in cases:
            with self.subTest(asset_id=asset.id):
                repository = SqlAlchemyMediaAssetReferenceRepository(
                    _SequenceDB(scalars_values=[[asset], []]),
                    deployment="production",
                    private_bucket="private-media",
                )
                with self.assertRaises(MediaAssetReferenceAssetUnavailable):
                    repository.preview_social_post_asset_ids(
                        owner_user_id=owner_id,
                        asset_ids=[asset.id],
                    )

    def test_native_content_path_parser_accepts_only_exact_canonical_path(self) -> None:
        asset_id = uuid.uuid4()
        path = native_media_content_path(asset_id)
        self.assertEqual(native_media_content_asset_id(path), asset_id)
        for invalid in (
            f"https://example.invalid{path}",
            f"{path}?download=1",
            path.upper(),
            f"//api/media/native/{asset_id}/content",
        ):
            self.assertIsNone(native_media_content_asset_id(invalid))

    def test_current_reference_read_hides_cross_deployment_asset(self) -> None:
        owner_id = uuid.uuid4()
        asset = _asset(owner_id)
        reference = SimpleNamespace(
            id=uuid.uuid4(),
            asset_id=asset.id,
            owner_user_id=owner_id,
            resource_type="profile",
            profile_user_id=owner_id,
            social_post_id=None,
            slot="avatar",
        )

        class Result:
            def first(self):
                return reference, asset

        class DB:
            def execute(self, _statement):
                return Result()

        allowed = SqlAlchemyMediaAssetReferenceRepository(
            DB(),
            deployment="production",
            private_bucket="private-media",
        ).get_current_reference(asset.id)
        self.assertEqual(allowed.asset_id, asset.id)

        hidden = SqlAlchemyMediaAssetReferenceRepository(
            DB(),
            deployment="staging",
            private_bucket="private-media",
        ).get_current_reference(asset.id)
        self.assertIsNone(hidden)

    def test_cleanup_and_private_message_send_both_exclude_current_references(self) -> None:
        cleanup_db = _SequenceDB()
        SqlAlchemyMediaNativeRepository(cleanup_db).asset_cleanup_candidates(
            at=datetime(2026, 7, 25, tzinfo=UTC),
            unsent_grace=timedelta(hours=1),
        )
        statement = cleanup_db.statements[-1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
        sql = str(statement)
        self.assertIn("media_asset_references", sql)
        self.assertIn("NOT (EXISTS", sql)

        delete_source = inspect.getsource(
            SqlAlchemyMediaNativeRepository.delete_asset_and_release_quota
        )
        send_source = inspect.getsource(SqlAlchemyMediaNativeRepository.store_attachment)
        self.assertIn("current_reference_exists", delete_source)
        self.assertIn("~current_reference_exists", delete_source)
        self.assertIn("reference_after_asset_lock", delete_source)
        self.assertIn("current_reference_exists", send_source)
        self.assertIn("~current_reference_exists", send_source)
        self.assertIn("reference_after_asset_lock", send_source)


class MediaAssetReferenceReleaseStampTests(unittest.TestCase):
    """释放盖章（expires_at = 释放时刻 + 宽限期）与释放路径锁序契约。"""

    GRACE = timedelta(seconds=RELEASED_ASSET_CLEANUP_GRACE_SECONDS)

    def _assert_stamped_between(self, asset, before, after) -> None:
        # expires_at 应为 utcnow() + 宽限期；utcnow 落在调用前后两次采样之间，
        # 允许由此产生的秒级误差。
        self.assertIsNotNone(asset.expires_at)
        released_at = asset.expires_at - self.GRACE
        self.assertGreaterEqual(released_at, before)
        self.assertLessEqual(released_at, after)

    def test_release_grace_matches_unsent_cleanup_default(self) -> None:
        from bbw_web.jobs import WEB_NATIVE_MEDIA_UNSENT_GRACE_SECONDS

        self.assertEqual(RELEASED_ASSET_CLEANUP_GRACE_SECONDS, 24 * 60 * 60)
        self.assertEqual(
            RELEASED_ASSET_CLEANUP_GRACE_SECONDS,
            WEB_NATIVE_MEDIA_UNSENT_GRACE_SECONDS,
        )

    def test_release_profile_avatar_stamps_asset_and_locks_asset_before_reference(
        self,
    ) -> None:
        owner_id = uuid.uuid4()
        asset = _asset(owner_id)
        reference = _profile_avatar_reference(owner_id, asset.id)
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=owner_id)],
            scalars_values=[
                [reference],
                [asset],
                [reference],
            ],
        )
        before = datetime.now(UTC)
        released = SqlAlchemyMediaAssetReferenceRepository(db).release_profile_avatar(
            owner_user_id=owner_id
        )
        after = datetime.now(UTC)
        self.assertTrue(released)
        self.assertEqual(db.deleted, [reference])
        self.assertGreaterEqual(db.flushes, 1)
        self._assert_stamped_between(asset, before, after)

        # 语句顺序：无锁预读定位 → asset FOR UPDATE（populate_existing）
        # → 引用 FOR UPDATE（populate_existing）。
        snapshot_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
        asset_lock_sql = str(db.statements[2].compile(dialect=postgresql.dialect()))
        reference_lock_sql = str(db.statements[3].compile(dialect=postgresql.dialect()))
        self.assertIn("media_asset_references", snapshot_sql)
        self.assertNotIn("FOR UPDATE", snapshot_sql)
        self.assertIn("media_assets", asset_lock_sql)
        self.assertIn("FOR UPDATE", asset_lock_sql)
        self.assertIn("media_asset_references", reference_lock_sql)
        self.assertIn("FOR UPDATE", reference_lock_sql)
        self.assertTrue(
            db.statements[2].get_execution_options().get("populate_existing")
        )
        self.assertTrue(
            db.statements[3].get_execution_options().get("populate_existing")
        )

    def test_release_social_post_media_stamps_only_assets_without_existing_expiry(
        self,
    ) -> None:
        owner_id = uuid.uuid4()
        post_id = uuid.uuid4()
        released_asset = _asset(owner_id)
        already_expiring = _asset(owner_id)
        preset_deadline = datetime(2026, 8, 1, tzinfo=UTC)
        already_expiring.expires_at = preset_deadline
        first_ref = _social_post_reference(
            owner_id, post_id, released_asset.id, "pictures[0]"
        )
        second_ref = _social_post_reference(
            owner_id, post_id, already_expiring.id, "pictures[1]"
        )
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=post_id, status="published")],
            scalars_values=[
                [first_ref, second_ref],
                [released_asset, already_expiring],
                [first_ref, second_ref],
            ],
        )
        before = datetime.now(UTC)
        count = SqlAlchemyMediaAssetReferenceRepository(db).release_social_post_media(
            owner_user_id=owner_id,
            social_post_id=post_id,
        )
        after = datetime.now(UTC)
        self.assertEqual(count, 2)
        self.assertEqual(db.deleted, [first_ref, second_ref])
        self._assert_stamped_between(released_asset, before, after)
        # 已有 expires_at 的 asset 保持原值，不被释放盖章覆盖。
        self.assertEqual(already_expiring.expires_at, preset_deadline)

        snapshot_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
        asset_lock_sql = str(db.statements[2].compile(dialect=postgresql.dialect()))
        reference_lock_sql = str(db.statements[3].compile(dialect=postgresql.dialect()))
        self.assertNotIn("FOR UPDATE", snapshot_sql)
        self.assertIn("media_assets", asset_lock_sql)
        self.assertIn("FOR UPDATE", asset_lock_sql)
        self.assertIn("media_asset_references", reference_lock_sql)
        self.assertIn("FOR UPDATE", reference_lock_sql)
        self.assertTrue(
            db.statements[2].get_execution_options().get("populate_existing")
        )

    def test_release_asset_reference_stamps_asset_and_locks_asset_before_reference(
        self,
    ) -> None:
        owner_id = uuid.uuid4()
        asset = _asset(owner_id)
        reference = _profile_avatar_reference(owner_id, asset.id)
        db = _SequenceDB(
            scalar_values=[
                reference,
                SimpleNamespace(id=owner_id),
                reference,
            ],
            scalars_values=[[asset]],
        )
        before = datetime.now(UTC)
        released = SqlAlchemyMediaAssetReferenceRepository(db).release_asset_reference(
            owner_user_id=owner_id,
            asset_id=asset.id,
        )
        after = datetime.now(UTC)
        self.assertTrue(released)
        self.assertEqual(db.deleted, [reference])
        self._assert_stamped_between(asset, before, after)

        # 第一条定位查询必须无锁；asset 锁（populate_existing）先于引用行锁。
        preread_sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
        asset_lock_sql = str(db.statements[2].compile(dialect=postgresql.dialect()))
        reference_lock_sql = str(db.statements[3].compile(dialect=postgresql.dialect()))
        self.assertIn("media_asset_references", preread_sql)
        self.assertNotIn("FOR UPDATE", preread_sql)
        self.assertIn("media_assets", asset_lock_sql)
        self.assertIn("FOR UPDATE", asset_lock_sql)
        self.assertTrue(
            db.statements[2].get_execution_options().get("populate_existing")
        )
        self.assertIn("media_asset_references", reference_lock_sql)
        self.assertIn("FOR UPDATE", reference_lock_sql)

    def test_replace_profile_avatar_stamps_old_asset_and_clears_new_expiry(self) -> None:
        owner_id = uuid.uuid4()
        old_asset = _asset(owner_id)
        new_asset = _asset(owner_id)
        # 新 asset 携带未来的清理时限也允许绑定，绑定后必须清零。
        new_asset.expires_at = datetime.now(UTC) + timedelta(hours=1)
        old_reference = _profile_avatar_reference(owner_id, old_asset.id)
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=owner_id)],
            scalars_values=[
                [old_reference],
                [old_asset, new_asset],
                [],
                [old_reference],
            ],
        )
        before = datetime.now(UTC)
        bound = SqlAlchemyMediaAssetReferenceRepository(db).replace_profile_avatar(
            owner_user_id=owner_id,
            asset_id=new_asset.id,
        )
        after = datetime.now(UTC)
        self.assertEqual(bound.asset_id, new_asset.id)
        self.assertEqual(db.deleted, [old_reference])
        self.assertEqual(len(db.added), 1)
        # 旧 asset 从释放时刻起算宽限期；新绑定 asset 的 expires_at 置 None。
        self._assert_stamped_between(old_asset, before, after)
        self.assertIsNone(new_asset.expires_at)

    def test_bind_social_post_media_never_stamps_asset_retained_by_new_binding(
        self,
    ) -> None:
        owner_id = uuid.uuid4()
        post_id = uuid.uuid4()
        dropped_asset = _asset(owner_id)
        rebound_asset = _asset(owner_id)
        dropped_ref = _social_post_reference(
            owner_id, post_id, dropped_asset.id, "pictures[0]"
        )
        rebound_ref = _social_post_reference(
            owner_id, post_id, rebound_asset.id, "pictures[1]"
        )
        db = _SequenceDB(
            scalar_values=[SimpleNamespace(id=post_id, status="published")],
            scalars_values=[
                [dropped_ref, rebound_ref],
                [dropped_asset, rebound_asset],
                [],
                [dropped_ref, rebound_ref],
            ],
        )
        before = datetime.now(UTC)
        bound = SqlAlchemyMediaAssetReferenceRepository(db).bind_social_post_media(
            owner_user_id=owner_id,
            social_post_id=post_id,
            assets_by_slot={"pictures[0]": rebound_asset.id},
        )
        after = datetime.now(UTC)
        self.assertEqual(bound["pictures[0]"].asset_id, rebound_asset.id)
        self.assertEqual(db.deleted, [dropped_ref, rebound_ref])
        self.assertEqual(len(db.added), 1)
        # 换槽保留的 asset 仍在新绑定中，不盖章；彻底释放的 asset 才盖章。
        self._assert_stamped_between(dropped_asset, before, after)
        self.assertIsNone(rebound_asset.expires_at)


class _ReferenceRepository:
    reference: BoundMediaAssetReference | None = None

    def __init__(self, _db, **_scope) -> None:
        pass

    def get_current_reference(self, _asset_id):
        return self.reference


class MediaAssetReferenceAccessTests(unittest.TestCase):
    def test_profile_reference_requires_current_active_profile(self) -> None:
        owner_id = uuid.uuid4()
        viewer_id = uuid.uuid4()
        reference = _bound_reference(
            owner_user_id=owner_id,
            resource_type="profile",
            resource_id=owner_id,
            slot="avatar",
        )
        _ReferenceRepository.reference = reference
        identity = SimpleNamespace(user_id=viewer_id, upstream_uid="viewer")
        with patch.object(
            media_api,
            "SqlAlchemyMediaAssetReferenceRepository",
            _ReferenceRepository,
        ):
            self.assertIs(
                media_api._load_native_media_reference(
                    _SequenceDB(
                        scalar_values=[
                            SimpleNamespace(
                                id=owner_id,
                                profile={"avatar": reference.content_path},
                            )
                        ]
                    ),
                    asset_id=reference.asset_id,
                    identity=identity,
                ),
                reference,
            )
            with self.assertRaises(HTTPException) as raised:
                media_api._load_native_media_reference(
                    _SequenceDB(scalar_values=[None]),
                    asset_id=reference.asset_id,
                    identity=identity,
                )
            with self.assertRaises(HTTPException) as stale:
                media_api._load_native_media_reference(
                    _SequenceDB(
                        scalar_values=[
                            SimpleNamespace(
                                id=owner_id,
                                profile={"avatar": "/api/media/native/old/content"},
                            )
                        ]
                    ),
                    asset_id=reference.asset_id,
                    identity=identity,
                )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(stale.exception.status_code, 404)

    def test_social_reference_reuses_local_visibility_policy_and_hides_old_paths(self) -> None:
        owner_id = uuid.uuid4()
        post_id = uuid.uuid4()
        viewer_id = uuid.uuid4()
        reference = _bound_reference(
            owner_user_id=owner_id,
            resource_type="social_post",
            resource_id=post_id,
            slot="pictures[0]",
        )
        _ReferenceRepository.reference = reference
        post = SimpleNamespace(
            id=post_id,
            author_user_id=owner_id,
            author_upstream_uid="author",
            visibility="followers",
            media={"pictures": [reference.content_path]},
        )
        identity = SimpleNamespace(user_id=viewer_id, upstream_uid="viewer")
        with patch.object(
            media_api,
            "SqlAlchemyMediaAssetReferenceRepository",
            _ReferenceRepository,
        ), patch.object(
            media_api.SqlAlchemySocialPermissionPolicy,
            "can_view",
            return_value=False,
        ) as can_view:
            with self.assertRaises(HTTPException) as denied:
                media_api._load_native_media_reference(
                    _SequenceDB(scalar_values=[post]),
                    asset_id=reference.asset_id,
                    identity=identity,
                )
        self.assertEqual(denied.exception.status_code, 404)
        can_view.assert_called_once()

        stale_post = SimpleNamespace(
            id=post_id,
            author_user_id=owner_id,
            author_upstream_uid="author",
            visibility="public",
            media={"pictures": ["/api/media/native/old/content"]},
        )
        with patch.object(
            media_api,
            "SqlAlchemyMediaAssetReferenceRepository",
            _ReferenceRepository,
        ), patch.object(
            media_api.SqlAlchemySocialPermissionPolicy,
            "can_view",
            return_value=True,
        ) as stale_policy:
            with self.assertRaises(HTTPException) as stale:
                media_api._load_native_media_reference(
                    _SequenceDB(scalar_values=[stale_post]),
                    asset_id=reference.asset_id,
                    identity=identity,
                )
        self.assertEqual(stale.exception.status_code, 404)
        stale_policy.assert_not_called()

        _ReferenceRepository.reference = None
        with patch.object(
            media_api,
            "SqlAlchemyMediaAssetReferenceRepository",
            _ReferenceRepository,
        ):
            with self.assertRaises(HTTPException) as released:
                media_api._load_native_media_reference(
                    _SequenceDB(),
                    asset_id=reference.asset_id,
                    identity=identity,
                )
        self.assertEqual(released.exception.status_code, 404)

    def test_http_route_requires_login_and_only_emits_short_lived_redirect(self) -> None:
        owner_id = uuid.uuid4()
        reference = _bound_reference(
            owner_user_id=owner_id,
            resource_type="profile",
            resource_id=owner_id,
            slot="avatar",
        )

        class Persistence:
            storage = SimpleNamespace(bucket="private-media")

            def require_identity(self, sid):
                if sid != "valid":
                    return None
                return SimpleNamespace(user_id=owner_id, upstream_uid="owner")

            def rate_limit(self, *_args, **_kwargs):
                return True

            def get_r2_storage(self):
                return self.storage

        app = FastAPI()
        app.state.persistence = Persistence()
        app.state.settings = SimpleNamespace(environment="production")
        app.include_router(media_api.router)

        @contextmanager
        def fake_session_scope():
            yield object()

        with patch.object(
            media_api,
            "session_scope",
            fake_session_scope,
        ), patch.object(
            media_api,
            "_load_native_media_reference",
            return_value=reference,
        ) as load_reference, patch.object(
            media_api,
            "_signed_url",
            return_value=("https://r2.invalid/read?short=1", 60),
        ), TestClient(app) as client:
            missing = client.get(
                f"/api/media/native/{reference.asset_id}/content",
                follow_redirects=False,
            )
            allowed = client.get(
                f"/api/media/native/{reference.asset_id}/content",
                cookies={media_api.legacy.COOKIE_NAME: "valid"},
                follow_redirects=False,
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(allowed.status_code, 307)
        self.assertEqual(allowed.headers["location"], "https://r2.invalid/read?short=1")
        self.assertEqual(allowed.headers["cache-control"], "private, no-store")
        load_reference.assert_called_once_with(
            unittest.mock.ANY,
            asset_id=reference.asset_id,
            identity=unittest.mock.ANY,
            deployment="production",
            private_bucket="private-media",
        )


if __name__ == "__main__":
    unittest.main()
