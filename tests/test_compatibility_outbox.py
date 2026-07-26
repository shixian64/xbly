from __future__ import annotations

import types
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from bbw_web import compatibility_outbox as worker


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def claim(operation_type: str, payload: dict | None = None, *, attempt: int = 1):
    return worker.ClaimedOperation(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        operation_type=operation_type,
        aggregate_type="social-native",
        aggregate_id="42",
        payload=payload or {},
        attempt_count=attempt,
        max_attempts=8,
        locked_by="test-worker",
    )


class FakeResolver:
    def __init__(self) -> None:
        self.ids = {
            ("post", "pst_local"): "901",
            ("comment", "cmt_parent"): "902",
            ("comment", "cmt_local"): "903",
        }
        self.post_row = types.SimpleNamespace(author_upstream_uid="77")
        self.comment_row = types.SimpleNamespace(author_upstream_uid="78")

    def require_upstream_id(self, entity_type, public_id):
        value = self.ids.get((entity_type, public_id))
        if value is None:
            raise worker.RetryableCompatibilityError(
                f"{entity_type}_upstream_binding_missing"
            )
        return value

    def post(self, _public_id):
        return self.post_row

    def comment(self, _public_id):
        return self.comment_row


class FakeDb:
    def __init__(self, row=None) -> None:
        self.row = row
        self.added = []
        self.flushed = 0

    def scalar(self, _statement):
        return self.row

    def add(self, value):
        self.added.append(value)

    def flush(self):
        self.flushed += 1


def scope_for(db):
    @contextmanager
    def scope():
        yield db

    return scope


class CompatibilityOutboxClaimTests(unittest.TestCase):
    def test_claim_uses_compatibility_filter_lease_and_skip_locked(self) -> None:
        statement = worker._claim_statement(now=NOW, limit=500)
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("operation_outbox.operation_type LIKE 'compatibility.%%'", sql)
        self.assertIn("operation_outbox.operation_type NOT LIKE 'compatibility.media.archive%%'", sql)
        self.assertIn("operation_outbox.status = 'processing'", sql)
        self.assertIn("operation_outbox.locked_until", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("LIMIT 100", sql)

    def test_expired_lease_at_retry_limit_is_terminalized_without_network(self) -> None:
        row = types.SimpleNamespace(
            id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            operation_type="compatibility.social.follow",
            aggregate_type="social-native",
            aggregate_id="88",
            payload={"target_uid": "88", "body": {"uid": "88"}},
            status="processing",
            attempt_count=8,
            max_attempts=8,
            locked_by="dead-worker",
            locked_until=NOW - timedelta(seconds=1),
            completed_at=None,
            last_error=None,
        )

        class ClaimDb(FakeDb):
            def scalars(self, _statement):
                return [row]

        claimed = worker._claim_due(
            db_scope=scope_for(ClaimDb()),
            worker_id="new-worker",
            limit=20,
            lease_seconds=120,
            now=NOW,
        )
        self.assertEqual(claimed, [])
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.completed_at, NOW)
        self.assertIsNone(row.locked_by)
        self.assertEqual(row.last_error, "compatibility_retry_budget_exhausted")

    def test_operation_matrix_covers_current_emitters(self) -> None:
        self.assertIn(
            "compatibility.social.post.publish",
            worker.DIRECTLY_MIRRORED_OPERATION_TYPES,
        )
        self.assertIn(
            "compatibility.social.reject-friend",
            worker.UNSAFE_OR_UNMAPPABLE_OPERATION_TYPES,
        )
        self.assertIn(
            "compatibility.social.reaction.like",
            worker.UNSAFE_OR_UNMAPPABLE_OPERATION_TYPES,
        )
        self.assertNotIn(
            "compatibility.media.archive", worker.RECOGNIZED_OPERATION_TYPES
        )

    def test_finalize_retries_without_exposing_exception_text(self) -> None:
        row = types.SimpleNamespace(
            id=uuid.uuid4(),
            status="processing",
            locked_by="test-worker",
            locked_until=NOW + timedelta(seconds=30),
            attempt_count=2,
            max_attempts=8,
            completed_at=None,
            available_at=NOW,
            last_error=None,
        )
        item = claim("compatibility.social.follow", attempt=2)
        item = worker.ClaimedOperation(
            id=row.id,
            owner_user_id=item.owner_user_id,
            operation_type=item.operation_type,
            aggregate_type=item.aggregate_type,
            aggregate_id=item.aggregate_id,
            payload=item.payload,
            attempt_count=2,
            max_attempts=8,
            locked_by="test-worker",
        )
        status = worker._finalize_claim(
            item,
            db_scope=scope_for(FakeDb(row)),
            now=NOW,
            error=worker.RetryableCompatibilityError("provider_unavailable"),
        )
        self.assertEqual(status, "retried")
        self.assertEqual(row.status, "retry")
        self.assertEqual(row.last_error, "provider_unavailable")
        self.assertIsNone(row.locked_by)
        self.assertGreater(row.available_at, NOW)

    def test_finalize_exhausted_retry_as_failed(self) -> None:
        row = types.SimpleNamespace(
            id=uuid.uuid4(),
            status="processing",
            locked_by="test-worker",
            locked_until=NOW + timedelta(seconds=30),
            attempt_count=8,
            max_attempts=8,
            completed_at=None,
            available_at=NOW,
            last_error=None,
        )
        item = claim("compatibility.social.follow", attempt=8)
        item = worker.ClaimedOperation(
            id=row.id,
            owner_user_id=item.owner_user_id,
            operation_type=item.operation_type,
            aggregate_type=item.aggregate_type,
            aggregate_id=item.aggregate_id,
            payload=item.payload,
            attempt_count=8,
            max_attempts=8,
            locked_by="test-worker",
        )
        status = worker._finalize_claim(
            item,
            db_scope=scope_for(FakeDb(row)),
            now=NOW,
            error=worker.RetryableCompatibilityError("provider_unavailable"),
        )
        self.assertEqual(status, "failed")
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.completed_at, NOW)


class CompatibilityOutboxMappingTests(unittest.TestCase):
    def test_native_social_follow_and_privacy_mapping(self) -> None:
        follow = worker._prepare_social_call(
            claim(
                "compatibility.social.follow",
                {
                    "target_uid": "88",
                    "body": {"uid": "88"},
                },
            ),
            FakeResolver(),
        )
        self.assertEqual((follow.component, follow.method), ("social", "follow"))
        self.assertEqual(follow.args, ("88",))
        self.assertEqual(dict(follow.kwargs or {}), {"quietly": "1"})

        privacy = worker._prepare_social_call(
            claim(
                "compatibility.profile.privacy",
                {"body": {"privacy": {"show_city": True, "show_age": False}}},
            ),
            FakeResolver(),
        )
        self.assertEqual((privacy.component, privacy.method), ("profile", "set_privacy"))
        self.assertEqual(
            dict(privacy.kwargs or {}), {"show_city": "1", "show_age": "0"}
        )

    def test_unmappable_friend_resolution_is_terminal(self) -> None:
        with self.assertRaises(worker.PermanentCompatibilityError) as raised:
            worker._prepare_social_call(
                claim(
                    "compatibility.social.reject-friend",
                    {"target_uid": "88", "body": {"uid": "88"}},
                ),
                FakeResolver(),
            )
        self.assertEqual(raised.exception.code, "friend_request_resolution_not_mappable")

    def test_native_avatar_path_is_never_sent_to_legacy_profile_api(self) -> None:
        asset_id = uuid.uuid4()
        with self.assertRaises(worker.PermanentCompatibilityError) as raised:
            worker._prepare_social_call(
                claim(
                    "compatibility.profile.reset",
                    {
                        "body": {
                            "field": "avatar",
                            "value": f"/api/media/native/{asset_id}/content",
                        }
                    },
                ),
                FakeResolver(),
            )
        self.assertEqual(
            raised.exception.code,
            "profile_avatar_upload_not_mappable",
        )

    def test_text_post_publish_maps_and_media_post_fails_safely(self) -> None:
        base = {
            "payload": {
                "post_public_id": "pst_local",
                "body": "正文",
                "title": "标题",
                "media": {},
                "visibility": "followers",
                "comment_policy": "open",
                "hide_comments": False,
                "plate": "动态",
                "topics": ["音乐", "城市"],
            }
        }
        mapped = worker._prepare_moments_call(
            claim("compatibility.social.post.publish", base), FakeResolver()
        )
        self.assertEqual(mapped.method, "publish_post")
        self.assertFalse(mapped.replay_safe)
        self.assertEqual(mapped.binding, worker.BindingTarget("post", "pst_local"))
        self.assertEqual(
            dict(mapped.kwargs or {})["visibility_scope"], "好友及粉丝可见"
        )
        self.assertIn("音乐", dict(mapped.kwargs or {})["topics"])

        with self.assertRaises(worker.PermanentCompatibilityError) as raised:
            worker._prepare_moments_call(
                claim(
                    "compatibility.social.post.publish",
                    {
                        "payload": {
                            **base["payload"],
                            "media": {
                                "pictures": [
                                    f"/api/media/native/{uuid.uuid4()}/content"
                                ]
                            },
                        }
                    },
                ),
                FakeResolver(),
            )
        self.assertEqual(raised.exception.code, "post_media_not_mappable")

    def test_comment_requires_post_binding_and_uses_upstream_ids(self) -> None:
        mapped = worker._prepare_moments_call(
            claim(
                "compatibility.social.comment.publish",
                {
                    "payload": {
                        "post_public_id": "pst_local",
                        "comment_public_id": "cmt_local",
                        "parent_public_id": "cmt_parent",
                        "body": "回复",
                    }
                },
            ),
            FakeResolver(),
        )
        self.assertEqual(mapped.method, "send_comment")
        self.assertEqual(mapped.args, ("回复", "901", "77"))
        self.assertEqual(dict(mapped.kwargs or {}), {"main_id": "902", "main_owner": "78"})
        self.assertEqual(mapped.binding, worker.BindingTarget("comment", "cmt_local"))

    def test_local_only_id_is_never_sent_upstream(self) -> None:
        db = FakeDb(None)
        resolver = worker.CompatibilityResolver(db)
        with self.assertRaises(worker.RetryableCompatibilityError) as raised:
            resolver.require_upstream_id("post", "pst_" + "a" * 32)
        self.assertEqual(raised.exception.code, "post_upstream_binding_missing")

    def test_extracts_only_explicit_entity_identifier(self) -> None:
        result = types.SimpleNamespace(
            data={"code": "200", "data": {"postid": "991", "authid": "42"}}
        )
        self.assertEqual(worker._extract_upstream_id(result, "post"), "991")
        no_id = types.SimpleNamespace(data={"code": "200", "message": "成功"})
        self.assertEqual(worker._extract_upstream_id(no_id, "post"), "")
        actor_id_only = types.SimpleNamespace(
            data={"id": "42", "message": "发布成功"}
        )
        self.assertEqual(worker._extract_upstream_id(actor_id_only, "post"), "")


class CompatibilityRuntimeTests(unittest.TestCase):
    def test_runtime_is_rebuilt_through_provider_boundary(self) -> None:
        account_id = uuid.uuid4()

        class Cipher:
            def __init__(self):
                self.calls = []

            def decrypt_text(self, payload, *, purpose, context):
                self.calls.append((payload, purpose, context))
                return "token-secret" if purpose.endswith("token") else "login-secret"

        class Provider:
            def __init__(self):
                self.state = None

            def create_runtime_from_state(self, state):
                self.state = state
                app = types.SimpleNamespace(
                    session=types.SimpleNamespace(logged_in=True),
                    client=types.SimpleNamespace(close=lambda: None),
                )
                return types.SimpleNamespace(app=app)

        state = worker.RuntimeAccountState(
            account_id=account_id,
            upstream_uid="42",
            login_encrypted={"ciphertext": "login"},
            token_encrypted={"ciphertext": "token"},
            token_expires_at=NOW + timedelta(hours=1),
            display_name="本地用户",
            profile={"nickname": "本地用户"},
            device_data={"device_id": "stored-device"},
        )
        cipher = Cipher()
        provider = Provider()
        runtime = worker._create_runtime(
            state,
            cipher=cipher,
            runtime_provider=provider,
            now=NOW,
        )
        self.assertTrue(runtime.app.session.logged_in)
        self.assertEqual(provider.state.uid, "42")
        self.assertEqual(provider.state.token, "token-secret")
        self.assertEqual(provider.state.phone, "login-secret")
        self.assertIn(
            ({"ciphertext": "token"}, "external-account.token", f"external-account:{account_id}:token"),
            cipher.calls,
        )

    def test_non_replay_safe_ambiguous_result_is_terminal(self) -> None:
        result = types.SimpleNamespace(
            ok=False,
            status=-1,
            kind="error",
            code="",
        )
        call = worker.CompatibilityCall(
            "social", "publish_post", replay_safe=False
        )
        with self.assertRaises(worker.PermanentCompatibilityError) as raised:
            worker._ensure_success(result, call)
        self.assertEqual(raised.exception.code, "provider_result_outcome_ambiguous")

    def test_empty_success_is_accepted_only_for_declared_mutation(self) -> None:
        result = types.SimpleNamespace(
            ok=False,
            status=200,
            kind="empty",
            code="EMPTY_RESPONSE",
        )
        worker._ensure_success(
            result,
            worker.CompatibilityCall("social", "delete_post", accept_empty=True),
        )
        with self.assertRaises(worker.RetryableCompatibilityError):
            worker._ensure_success(
                result,
                worker.CompatibilityCall("social", "follow", accept_empty=False),
            )

    def test_module_does_not_bypass_provider_or_log_credentials(self) -> None:
        source = Path(worker.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from bbw_protocol", source)
        self.assertNotIn("import bbw_protocol", source)
        self.assertNotIn("LOGGER.", source)


class DispatchDueTests(unittest.TestCase):
    def test_dispatch_summary_keeps_failures_inside_outbox(self) -> None:
        completed = claim("compatibility.social.follow")
        failed = claim("compatibility.social.reject-friend")
        statuses = iter(("completed", "failed"))

        with (
            patch.object(worker, "_claim_due", return_value=[completed, failed]),
            patch.object(
                worker,
                "_execute_claim",
                side_effect=(
                    worker.ExecutionResult(upstream_id_missing=True),
                    worker.PermanentCompatibilityError("not_mappable"),
                ),
            ),
            patch.object(worker, "_finalize_claim", side_effect=lambda *a, **k: next(statuses)),
        ):
            summary = worker.dispatch_due(
                worker_id="test-worker",
                cipher=types.SimpleNamespace(),
                runtime_provider=types.SimpleNamespace(),
                clock=lambda: NOW,
            )

        self.assertEqual(
            summary,
            {
                "claimed": 2,
                "completed": 1,
                "retried": 0,
                "failed": 1,
                "stale": 0,
                "completed_without_binding": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
