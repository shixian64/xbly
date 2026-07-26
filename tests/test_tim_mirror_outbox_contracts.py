from __future__ import annotations

import json
import types
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from bbw_web import jobs
from bbw_web.message_quote import (
    extract_local_message_identity,
    extract_message_quote,
)
from bbw_web.transports import (
    MessageMirrorTransport,
    MessageRecallLookupTransport,
    MessageSendTransport,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _FakeSendTransport:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str, dict[str, object]]] = []

    def send_text(
        self,
        from_account: str,
        to_account: str,
        text: str,
        **options: object,
    ) -> object:
        self.calls.append((from_account, to_account, text, options))
        return self.result


class _FakeMirrorTransport:
    def __init__(
        self,
        *,
        send_result: object | None = None,
        revoke_result: object | None = None,
        history_results: list[object] | None = None,
    ) -> None:
        success = types.SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={"MsgKey": "tim-message-key"},
        )
        self.send_result = send_result or success
        self.revoke_result = revoke_result or success
        self.history_results = list(history_results or [])
        self.text_calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.element_calls: list[
            tuple[str, str, list[dict[str, object]], dict[str, object]]
        ] = []
        self.revoke_calls: list[tuple[str, str, str]] = []
        self.roaming_calls: list[tuple[str, str, dict[str, object]]] = []

    def send_text(
        self,
        from_account: str,
        to_account: str,
        text: str,
        **options: object,
    ) -> object:
        self.text_calls.append((from_account, to_account, text, options))
        return self.send_result

    def send_elements(
        self,
        from_account: str,
        to_account: str,
        elements: list[dict[str, object]],
        **options: object,
    ) -> object:
        self.element_calls.append((from_account, to_account, elements, options))
        return self.send_result

    def revoke_c2c(
        self, from_account: str, to_account: str, msg_key: str
    ) -> object:
        self.revoke_calls.append((from_account, to_account, msg_key))
        return self.revoke_result

    def roaming_messages(
        self,
        from_account: str,
        to_account: str,
        **params: object,
    ) -> object:
        self.roaming_calls.append((from_account, to_account, params))
        if self.history_results:
            return self.history_results.pop(0)
        return types.SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={"Complete": 1, "MsgList": []},
        )


def _payload() -> dict[str, object]:
    return {
        "canonical_message_id": str(uuid.UUID("00000000-0000-0000-0000-000000000123")),
        "client_message_id": "web-message:client-one",
        "from": "42",
        "to": "9",
        "text": "本地消息",
        "quote": {
            "message_id": "quoted-message",
            "sender_uid": "9",
            "sender_name": "对方",
            "text": "被引用内容",
            "kind": "text",
            "sent_at": "1784970000",
        },
    }


def _media_payload(kind: str, *, operation: str = "send") -> dict[str, object]:
    canonical_id = uuid.UUID("00000000-0000-0000-0000-000000000456")
    attachment_id = uuid.UUID("00000000-0000-0000-0000-000000000457")
    asset_id = uuid.UUID("00000000-0000-0000-0000-000000000458")
    return {
        "operation": operation,
        "canonical_message_id": str(canonical_id),
        "client_message_id": "media:client-one",
        "attachment_id": str(attachment_id),
        "asset_id": str(asset_id),
        "from": "42",
        "to": "9",
        "message_type": kind,
        "media_report": {
            "attachment_id": str(attachment_id),
            "asset_id": str(asset_id),
            "name": "sample.bin",
            "mime": "application/octet-stream",
            "size": 4096,
            "duration": 3.25,
            "width": 640,
            "height": 480,
        },
    }


class TimMirrorCloudDataTests(unittest.TestCase):
    def test_cloud_data_contains_native_quote_and_canonical_identity(self) -> None:
        encoded = jobs._tim_mirror_cloud_custom_data(_payload())
        cloud = json.loads(encoded)

        self.assertIn("messageReply", cloud)
        self.assertEqual(cloud["messageReply"]["messageID"], "quoted-message")
        self.assertEqual(
            cloud["bbw_message"]["message_id"],
            "00000000-0000-0000-0000-000000000123",
        )
        self.assertEqual(
            cloud["bbw_message"]["client_message_id"],
            "web-message:client-one",
        )
        self.assertEqual(
            extract_local_message_identity(encoded),
            {
                "canonical_message_id": "00000000-0000-0000-0000-000000000123",
                "client_message_id": "web-message:client-one",
            },
        )
        self.assertEqual(extract_message_quote(encoded)["message_id"], "quoted-message")

    def test_history_report_preserves_mirror_identity_for_projection_dedup(self) -> None:
        cloud = jobs._tim_mirror_cloud_custom_data(_payload())
        report = jobs._history_message_report(
            {
                "from_user_id": "42",
                "to_user_id": "9",
                "id": "tim-history-id",
                "msg_key": "tim-key",
                "message_type": "text",
                "text": "本地消息",
                "timestamp": 1784970000,
                "cloud_custom_data": cloud,
            },
            account_uid="42",
            requested_peer="9",
        )

        self.assertIsNotNone(report)
        self.assertEqual(
            report["canonical_message_id"],
            "00000000-0000-0000-0000-000000000123",
        )
        self.assertEqual(report["client_message_id"], "web-message:client-one")
        self.assertEqual(report["client_message_key"], "web-message:client-one")
        self.assertEqual(report["quote"]["message_id"], "quoted-message")

    def test_history_identity_is_saved_in_message_projection_metadata(self) -> None:
        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        repository = Mock()
        stored = types.SimpleNamespace(id=uuid.uuid4())
        repository.insert_idempotent.return_value = (stored, True)
        report = {
            "peer_uid": "9",
            "direction": "outgoing",
            "source": "history",
            "upstream_message_id": "tim-history-id",
            "canonical_message_id": "00000000-0000-0000-0000-000000000123",
            "client_message_id": "web-message:client-one",
            "message_type": "text",
            "text": "本地消息",
            "sent_at": NOW.isoformat(),
        }
        with patch.object(jobs, "MessageRepository", return_value=repository):
            jobs._ingest_message(
                Mock(),
                settings=types.SimpleNamespace(message_retention_days=180),
                user=types.SimpleNamespace(
                    id=owner_id,
                    chat_retention_days=180,
                ),
                account=types.SimpleNamespace(upstream_uid="42"),
                report=report,
                conversation=types.SimpleNamespace(id=conversation_id),
                occurred_at=NOW,
            )

        metadata = repository.insert_idempotent.call_args.kwargs["extra_data"]
        self.assertEqual(
            metadata["canonical_message_id"],
            "00000000-0000-0000-0000-000000000123",
        )
        self.assertEqual(metadata["client_message_id"], "web-message:client-one")


class TimMediaMirrorTransportTests(unittest.TestCase):
    def test_fake_transport_covers_mirror_and_recall_lookup_edges(self) -> None:
        transport = _FakeMirrorTransport()
        self.assertIsInstance(transport, MessageMirrorTransport)
        self.assertIsInstance(transport, MessageRecallLookupTransport)

    def test_image_audio_video_fallback_and_file_elements_use_fake_transport(self) -> None:
        asset = types.SimpleNamespace(
            size_bytes=4096,
            filename="sample.bin",
            content_type="application/octet-stream",
        )
        cases = (
            ("image", "TIMImageElem"),
            ("audio", "TIMSoundElem"),
            ("video", "TIMFileElem"),
            ("file", "TIMFileElem"),
        )
        for kind, expected_type in cases:
            with self.subTest(kind=kind):
                transport = _FakeMirrorTransport()
                payload = _media_payload(kind)
                if kind == "image":
                    payload["media_report"]["mime"] = "image/png"
                if kind == "video":
                    payload["media_report"]["name"] = "clip.mp4"
                with (
                    patch.object(
                        jobs,
                        "_tim_media_asset_and_url",
                        return_value=(asset, "https://media.example/private"),
                    ),
                    patch.object(
                        jobs,
                        "_tim_mirror_delivery_is_processing",
                        return_value=True,
                    ),
                ):
                    result = jobs._tim_media_send_result(
                        uuid.uuid4(), payload, transport
                    )

                self.assertIs(result, transport.send_result)
                self.assertEqual(len(transport.element_calls), 1)
                element = transport.element_calls[0][2][0]
                self.assertEqual(element["MsgType"], expected_type)
                if kind == "video":
                    self.assertEqual(
                        element["MsgContent"]["FileName"], "clip.mp4"
                    )

    def test_flash_checks_delivery_state_before_any_upstream_send(self) -> None:
        delivery_id = uuid.uuid4()
        payload = _media_payload("flash")
        transport = _FakeMirrorTransport()
        with patch.object(
            jobs,
            "_tim_mirror_delivery_is_processing",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "cancelled before upstream"):
                jobs._tim_media_send_result(delivery_id, payload, transport)
        self.assertEqual(transport.text_calls, [])
        self.assertEqual(transport.element_calls, [])

        with patch.object(
            jobs,
            "_tim_mirror_delivery_is_processing",
            return_value=True,
        ):
            jobs._tim_media_send_result(delivery_id, payload, transport)
        self.assertEqual(len(transport.text_calls), 1)
        self.assertIn("闪照", transport.text_calls[0][2])

    def test_revoke_recovers_missing_msg_key_from_deterministic_history(self) -> None:
        payload = _media_payload("image", operation="revoke")
        sequence, random_value = jobs._tim_mirror_dedup_identity(payload)
        history = types.SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={
                "Complete": 1,
                "MsgList": [
                    {
                        "MsgKey": "recovered-key",
                        "MsgSeq": sequence,
                        "MsgRandom": random_value,
                    }
                ],
            },
        )
        transport = _FakeMirrorTransport(history_results=[history])
        with (
            patch.object(
                jobs,
                "_tim_media_send_upstream_key",
                return_value=("delivered", ""),
            ),
            patch.object(
                jobs, "_remember_tim_media_send_upstream_key"
            ) as remembered,
        ):
            result = jobs._tim_media_revoke_result(payload, transport)

        self.assertIs(result, transport.revoke_result)
        self.assertEqual(transport.revoke_calls, [("42", "9", "recovered-key")])
        self.assertEqual(len(transport.roaming_calls), 1)
        remembered.assert_called_once_with(payload, "recovered-key")

    def test_text_revoke_uses_the_canonical_text_send_delivery_target(self) -> None:
        payload = {
            **_payload(),
            "operation": "revoke",
            "message_type": "text",
        }
        self.assertEqual(jobs._tim_send_delivery_target_key(payload), "9")
        self.assertEqual(
            jobs._tim_send_delivery_target_key(_media_payload("image")),
            "media-send:9",
        )
        legacy_media_revoke = _media_payload("image", operation="revoke")
        legacy_media_revoke.pop("message_type")
        self.assertEqual(
            jobs._tim_send_delivery_target_key(legacy_media_revoke),
            "media-send:9",
        )

        transport = _FakeMirrorTransport()
        with patch.object(
            jobs,
            "_tim_media_send_upstream_key",
            return_value=("delivered", "text-tim-key"),
        ):
            result = jobs._tim_media_revoke_result(payload, transport)

        self.assertIs(result, transport.revoke_result)
        self.assertEqual(transport.revoke_calls, [("42", "9", "text-tim-key")])

    def test_missing_msg_key_keeps_compensation_revoke_retryable(self) -> None:
        delivery_id = uuid.uuid4()
        payload = _media_payload("image", operation="revoke")
        claimed = jobs._ClaimedTimMirror(delivery_id, payload, 2, 8)
        transport = _FakeMirrorTransport()
        with (
            patch.object(jobs, "_claim_tim_message_delivery", return_value=claimed),
            patch.object(
                jobs,
                "_tim_media_send_upstream_key",
                return_value=("delivered", ""),
            ),
            patch.object(
                jobs,
                "_mark_tim_mirror_failed",
                return_value=("retry", 60),
            ) as failed,
            patch.object(jobs, "_mark_tim_mirror_delivered") as delivered,
        ):
            result = jobs.mirror_tim_message_delivery(
                str(delivery_id), transport=transport
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry"])
        self.assertEqual(result["retry_after"], 60)
        self.assertEqual(transport.revoke_calls, [])
        self.assertEqual(len(transport.roaming_calls), 1)
        self.assertIn("not ready for revoke", failed.call_args.kwargs["error"])
        delivered.assert_not_called()

    def test_cancelled_send_remembers_upstream_key_for_compensation(self) -> None:
        delivery_id = uuid.uuid4()
        payload = _media_payload("image")
        claimed = jobs._ClaimedTimMirror(delivery_id, payload, 1, 8)
        upstream_result = types.SimpleNamespace(
            ok=True,
            error_code=0,
            error_info="",
            data={"MsgKey": "late-send-key"},
        )
        transport = _FakeMirrorTransport(send_result=upstream_result)
        with (
            patch.object(jobs, "_claim_tim_message_delivery", return_value=claimed),
            patch.object(
                jobs, "_tim_media_send_result", return_value=upstream_result
            ),
            patch.object(
                jobs, "_mark_tim_mirror_delivered", return_value="cancelled"
            ),
            patch.object(
                jobs, "_remember_cancelled_tim_upstream_id"
            ) as remembered,
        ):
            result = jobs.mirror_tim_message_delivery(
                str(delivery_id), transport=transport
            )

        self.assertEqual(result["status"], "cancelled")
        remembered.assert_called_once_with(delivery_id, "late-send-key")


class TimMirrorWorkerTests(unittest.TestCase):
    def test_send_transport_is_provider_neutral(self) -> None:
        transport = _FakeSendTransport(types.SimpleNamespace(ok=True))
        self.assertIsInstance(transport, MessageSendTransport)

    def test_success_marks_only_tim_delivery_and_keeps_identity(self) -> None:
        delivery_id = uuid.uuid4()
        claimed = jobs._ClaimedTimMirror(delivery_id, _payload(), 1, 8)
        transport = _FakeSendTransport(
            types.SimpleNamespace(
                ok=True,
                error_code=0,
                error_info="",
                data={"MsgKey": "tim-message-key"},
            )
        )
        with (
            patch.object(jobs, "_claim_tim_message_delivery", return_value=claimed),
            patch.object(
                jobs,
                "_mark_tim_mirror_delivered",
                return_value="delivered",
            ) as marked,
            patch.object(jobs, "_mark_tim_mirror_failed") as failed,
        ):
            result = jobs.mirror_tim_message_delivery(
                str(delivery_id), transport=transport
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["local_delivery_unchanged"])
        self.assertEqual(result["upstream_message_id"], "tim-message-key")
        marked.assert_called_once_with(
            delivery_id,
            upstream_message_id="tim-message-key",
        )
        failed.assert_not_called()
        self.assertEqual(transport.calls[0][:3], ("42", "9", "本地消息"))
        cloud = transport.calls[0][3]["cloud_custom_data"]
        self.assertEqual(
            extract_local_message_identity(cloud)["client_message_id"],
            "web-message:client-one",
        )
        self.assertEqual(
            transport.calls[0][3]["idempotency_key"],
            "00000000-0000-0000-0000-000000000123",
        )

    def test_failure_records_durable_backoff_without_raising(self) -> None:
        delivery_id = uuid.uuid4()
        claimed = jobs._ClaimedTimMirror(delivery_id, _payload(), 3, 8)
        transport = _FakeSendTransport(
            types.SimpleNamespace(
                ok=False,
                error_code=70009,
                error_info="REST unavailable",
                data={},
            )
        )
        with (
            patch.object(jobs, "_claim_tim_message_delivery", return_value=claimed),
            patch.object(
                jobs,
                "_mark_tim_mirror_failed",
                return_value=("retry", 120),
            ) as failed,
            patch.object(jobs, "_mark_tim_mirror_delivered") as delivered,
        ):
            result = jobs.mirror_tim_message_delivery(
                str(delivery_id), transport=transport
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry"])
        self.assertEqual(result["retry_after"], 120)
        self.assertTrue(result["local_delivery_unchanged"])
        failed.assert_called_once()
        delivered.assert_not_called()

    def test_backoff_is_exponential_and_bounded(self) -> None:
        self.assertEqual(jobs._tim_mirror_backoff_seconds(1), 30)
        self.assertEqual(jobs._tim_mirror_backoff_seconds(2), 60)
        self.assertEqual(jobs._tim_mirror_backoff_seconds(3), 120)
        self.assertEqual(jobs._tim_mirror_backoff_seconds(20), 3600)

    def test_single_row_claim_blocks_a_duplicate_worker(self) -> None:
        delivery_id = uuid.uuid4()
        row = types.SimpleNamespace(
            id=delivery_id,
            channel="tim",
            required=False,
            status="pending",
            attempt_count=0,
            max_attempts=8,
            available_at=NOW,
            locked_by=None,
            locked_until=None,
            last_error=None,
            payload=_payload(),
        )

        class FakeDB:
            def scalar(self, _statement: object) -> object:
                return row

            def flush(self) -> None:
                return None

        @contextmanager
        def fake_session_scope():
            yield FakeDB()

        with (
            patch.object(jobs, "session_scope", fake_session_scope),
            patch.object(jobs, "utcnow", return_value=NOW),
        ):
            claimed = jobs._claim_tim_message_delivery(delivery_id)
            duplicate = jobs._claim_tim_message_delivery(delivery_id)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.attempt, 1)
        self.assertEqual(row.status, "processing")
        self.assertEqual(
            row.locked_until,
            NOW + timedelta(seconds=jobs.TIM_MIRROR_LOCK_SECONDS),
        )
        self.assertIsNone(duplicate)


class TimMirrorDispatchTests(unittest.TestCase):
    def test_due_scanner_enqueues_stable_attempt_job_ids(self) -> None:
        first = uuid.uuid4()
        second = uuid.uuid4()
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class FakeQueue:
            def __init__(self, _name: str, *, connection: object) -> None:
                self.connection = connection

            def enqueue(self, *args: object, **kwargs: object) -> None:
                calls.append((args, kwargs))

        with (
            patch.object(
                jobs,
                "_due_tim_mirror_deliveries",
                return_value=[(first, 1), (second, 4)],
            ),
            patch.object(jobs, "Queue", FakeQueue),
        ):
            result = jobs.dispatch_due_tim_message_deliveries(
                connection=object(), limit=20
            )

        self.assertEqual(result["dispatched"], 2)
        self.assertEqual(
            calls[0][0],
            ("bbw_web.jobs.mirror_tim_message_delivery", str(first)),
        )
        self.assertEqual(
            calls[0][1]["job_id"],
            f"mirror-tim-message-{first}-1",
        )
        self.assertEqual(
            calls[1][1]["job_id"],
            f"mirror-tim-message-{second}-4",
        )

    def test_schedule_due_syncs_dispatches_tim_mirrors(self) -> None:
        source = (ROOT / "bbw_web" / "jobs.py").read_text(encoding="utf-8")
        schedule = source.split("def schedule_due_syncs()", 1)[1].split(
            "\ndef _save_sync_result", 1
        )[0]
        self.assertIn("dispatch_due_tim_message_deliveries(", schedule)
        self.assertIn('"tim_mirror_dispatched"', schedule)
        self.assertIn(".with_for_update(skip_locked=True)", source)


if __name__ == "__main__":
    unittest.main()
