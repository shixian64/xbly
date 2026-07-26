from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest

from sqlalchemy.dialects import postgresql

from bbw_prod.models import (
    ChatMember,
    ChatMessage,
    ChatThread,
    Conversation,
    Message,
    MessageDelivery,
    MessageReceipt,
)
from bbw_prod.services import RetentionService
from bbw_web.messaging import (
    InvalidLocalMessage,
    LocalDeliveryResult,
    LocalMessageBlocked,
    LocalMessageForbidden,
    LocalMessageIdempotencyConflict,
    LocalMessageNotFound,
    LocalMessageRevocationDenied,
    LocalMessageRevocationExpired,
    LocalMessageSourceStale,
    LocalMessageView,
    LocalMessagingService,
    LocalPrincipal,
    LocalRevokeResult,
    PeerNotMigrated,
    TimMirrorIntent,
)
from bbw_web.messaging.contracts import LocalAccount
from bbw_web.messaging.repository import (
    REVOKED_TEXT_PLACEHOLDER,
    SqlAlchemyCanonicalMessageStore,
    direct_thread_key,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _MemoryStore:
    def __init__(self) -> None:
        self.sender = LocalAccount(uuid.uuid4(), uuid.uuid4(), "42", "发送方", 180)
        self.recipient = LocalAccount(uuid.uuid4(), uuid.uuid4(), "9", "接收方", 180)
        self.blocked = False
        self.message_allowed = True
        self.peer_migrated = True
        self.items: dict[tuple[uuid.UUID, str], tuple[tuple[object, ...], LocalDeliveryResult]] = {}
        self.store_calls: list[dict[str, object]] = []

    def resolve_principal(self, principal: LocalPrincipal) -> LocalAccount | None:
        if (
            principal.user_id == self.sender.user_id
            and principal.external_account_id == self.sender.external_account_id
            and principal.upstream_uid == self.sender.upstream_uid
        ):
            return self.sender
        return None

    def resolve_active_peer(self, upstream_uid: str, *, provider: str) -> LocalAccount | None:
        if self.peer_migrated and upstream_uid == self.recipient.upstream_uid:
            return self.recipient
        return None

    def is_blocked_between(self, left: LocalAccount, right: LocalAccount) -> bool:
        return self.blocked

    def can_send_private_message(
        self, sender: LocalAccount, recipient: LocalAccount
    ) -> bool:
        return self.message_allowed

    def store_text(self, **values: object) -> LocalDeliveryResult:
        sender = values["sender"]
        recipient = values["recipient"]
        client_message_id = str(values["client_message_id"])
        text = str(values["text"])
        quote = dict(values["quote"] or {})
        key = (sender.user_id, client_message_id)
        payload = (recipient.user_id, text, quote)
        existing = self.items.get(key)
        if existing is not None:
            if existing[0] != payload:
                raise LocalMessageIdempotencyConflict("client id reused")
            return replace(existing[1], created=False)
        message_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        result = LocalDeliveryResult(
            message=LocalMessageView(
                id=message_id,
                thread_id=thread_id,
                client_message_id=client_message_id,
                sender_user_id=sender.user_id,
                sender_upstream_uid=sender.upstream_uid,
                recipient_upstream_uid=recipient.upstream_uid,
                message_type="text",
                body=text,
                quote=quote,
                occurred_at=values["occurred_at"],
                status="accepted",
            ),
            sender_projection_id=uuid.uuid4(),
            recipient_projection_id=uuid.uuid4(),
            recipient_user_id=recipient.user_id,
            created=True,
            tim_mirror=TimMirrorIntent(
                delivery_id=uuid.uuid4(),
                message_id=message_id,
                from_upstream_uid=sender.upstream_uid,
                to_upstream_uid=recipient.upstream_uid,
                client_message_id=client_message_id,
                text=text,
                quote=quote,
            ),
        )
        self.items[key] = (payload, result)
        self.store_calls.append(dict(values))
        return result

    def list_direct_messages(self, **_values: object) -> list[LocalMessageView]:
        return [item[1].message for item in self.items.values()]

    def mark_direct_read(self, **_values: object) -> int:
        return 1

    def revoke_text(self, **values: object) -> LocalRevokeResult | None:
        actor = values["actor"]
        peer_uid = str(values["peer_upstream_uid"])
        message_id = values["canonical_message_id"]
        revoked_at = values["revoked_at"]
        for key, (payload, delivery) in list(self.items.items()):
            if delivery.message.id != message_id:
                continue
            if actor.user_id != self.sender.user_id:
                raise LocalMessageRevocationDenied("sender only")
            if peer_uid != self.recipient.upstream_uid:
                raise LocalMessageRevocationDenied("peer mismatch")
            if delivery.message.status == "revoked":
                return LocalRevokeResult(
                    message=delivery.message,
                    recalled_text=str(payload[1]),
                    revoked_at=delivery.message.occurred_at,
                    created=False,
                    tim_mirror=None,
                )
            if revoked_at > delivery.message.occurred_at + timedelta(minutes=2):
                raise LocalMessageRevocationExpired("expired")
            revoked = replace(
                delivery,
                message=replace(
                    delivery.message,
                    body="消息已撤回",
                    quote={},
                    status="revoked",
                ),
            )
            self.items[key] = (payload, revoked)
            return LocalRevokeResult(
                message=revoked.message,
                recalled_text=str(payload[1]),
                revoked_at=revoked_at,
                created=True,
                tim_mirror=None,
            )
        return None


class LocalMessagingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _MemoryStore()
        self.service = LocalMessagingService(self.store, clock=lambda: NOW)
        self.principal = LocalPrincipal(
            self.store.sender.user_id,
            self.store.sender.external_account_id,
            self.store.sender.upstream_uid,
        )

    def test_local_success_returns_two_projections_and_nonblocking_tim_intent(self) -> None:
        result = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="web-message:one",
            text="本地消息",
            quote={
                "message_id": "quoted-id",
                "sender_uid": "9",
                "text": "被引用内容",
                "kind": "text",
            },
        )

        self.assertTrue(result.created)
        self.assertNotEqual(result.sender_projection_id, result.recipient_projection_id)
        self.assertEqual(result.recipient_user_id, self.store.recipient.user_id)
        self.assertEqual(result.message.quote["message_id"], "quoted-id")
        self.assertEqual(result.tim_mirror.message_id, result.message.id)
        self.assertEqual(result.tim_mirror.client_message_id, "web-message:one")
        self.assertEqual(result.tim_mirror.quote, result.message.quote)

    def test_client_message_id_is_idempotent_and_bound_to_payload(self) -> None:
        first = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="same-key",
            text="相同内容",
        )
        repeated = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="same-key",
            text="相同内容",
        )
        self.assertEqual(first.message.id, repeated.message.id)
        self.assertFalse(repeated.created)

        with self.assertRaises(LocalMessageIdempotencyConflict):
            self.service.send_text(
                principal=self.principal,
                peer_upstream_uid="9",
                client_message_id="same-key",
                text="被篡改内容",
            )

    def test_expected_source_identity_is_optional_and_forwarded(self) -> None:
        self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="manual-send",
            text="人工发送",
        )
        self.assertEqual(
            self.store.store_calls[-1]["expected_source_message_identity"], ""
        )

        self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="automatic-reply",
            text="自动回复",
            expected_source_message_identity="canonical-message-001",
        )
        self.assertEqual(
            self.store.store_calls[-1]["expected_source_message_identity"],
            "canonical-message-001",
        )

    def test_deny_wins_and_unmigrated_peer_fail_explicitly(self) -> None:
        self.store.message_allowed = False
        with self.assertRaises(LocalMessageForbidden):
            self.service.send_text(
                principal=self.principal,
                peer_upstream_uid="9",
                client_message_id="not-authorized",
                text="不能发送",
            )
        self.assertEqual(self.store.store_calls, [])

        self.store.message_allowed = True
        self.store.blocked = True
        with self.assertRaises(LocalMessageBlocked):
            self.service.send_text(
                principal=self.principal,
                peer_upstream_uid="9",
                client_message_id="blocked",
                text="不能发送",
            )
        self.store.blocked = False
        self.store.peer_migrated = False
        with self.assertRaises(PeerNotMigrated):
            self.service.send_text(
                principal=self.principal,
                peer_upstream_uid="9",
                client_message_id="not-migrated",
                text="不能投递",
            )

    def test_text_limit_matches_tim_compatibility_limit(self) -> None:
        accepted = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="limit-ok",
            text="字" * 2000,
        )
        self.assertEqual(len(accepted.message.body), 2000)
        with self.assertRaises(InvalidLocalMessage):
            self.service.send_text(
                principal=self.principal,
                peer_upstream_uid="9",
                client_message_id="limit-rejected",
                text="字" * 2001,
            )

    def test_revoke_uses_canonical_id_is_sender_scoped_and_idempotent(self) -> None:
        sent = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="revoke-me",
            text="撤回后用于重新编辑",
        )

        revoked = self.service.revoke_text(
            principal=self.principal,
            peer_upstream_uid="9",
            canonical_message_id=str(sent.message.id),
        )
        repeated = self.service.revoke_text(
            principal=self.principal,
            peer_upstream_uid="9",
            canonical_message_id=sent.message.id,
        )

        self.assertTrue(revoked.created)
        self.assertFalse(repeated.created)
        self.assertEqual(revoked.message.status, "revoked")
        self.assertEqual(revoked.recalled_text, "撤回后用于重新编辑")
        self.assertEqual(repeated.recalled_text, revoked.recalled_text)

        with self.assertRaises(LocalMessageRevocationDenied):
            self.service.revoke_text(
                principal=self.principal,
                peer_upstream_uid="8",
                canonical_message_id=sent.message.id,
            )
        with self.assertRaises(LocalMessageNotFound):
            self.service.revoke_text(
                principal=self.principal,
                peer_upstream_uid="9",
                canonical_message_id="legacy-tim-key",
            )

    def test_revoke_deadline_allows_exactly_two_minutes(self) -> None:
        sent = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="deadline-exact",
            text="可撤回",
        )
        exact = LocalMessagingService(
            self.store,
            clock=lambda: NOW + timedelta(minutes=2),
        ).revoke_text(
            principal=self.principal,
            peer_upstream_uid="9",
            canonical_message_id=sent.message.id,
        )
        self.assertTrue(exact.created)

        expired_sent = self.service.send_text(
            principal=self.principal,
            peer_upstream_uid="9",
            client_message_id="deadline-expired",
            text="已过期",
        )
        with self.assertRaises(LocalMessageRevocationExpired):
            LocalMessagingService(
                self.store,
                clock=lambda: NOW + timedelta(minutes=2, microseconds=1),
            ).revoke_text(
                principal=self.principal,
                peer_upstream_uid="9",
                canonical_message_id=expired_sent.message.id,
            )


class CanonicalMessagingSchemaTests(unittest.TestCase):
    class _ScalarResult:
        def __init__(self, rows: object) -> None:
            self.rows = list(rows if isinstance(rows, (list, tuple)) else [rows])

        def __iter__(self):
            return iter(self.rows)

        def first(self):
            return self.rows[0] if self.rows else None

    class _QueueDb:
        def __init__(self, *, scalar_values=(), scalars_values=()) -> None:
            self.scalar_values = list(scalar_values)
            self.scalars_values = list(scalars_values)
            self.flushed = 0

        def scalar(self, _statement):
            return self.scalar_values.pop(0)

        def scalars(self, _statement):
            return CanonicalMessagingSchemaTests._ScalarResult(
                self.scalars_values.pop(0)
            )

        def flush(self) -> None:
            self.flushed += 1

    @staticmethod
    def _revoke_rows(*, send_status: str = "pending"):
        sender_id = uuid.uuid4()
        recipient_id = uuid.uuid4()
        message_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        canonical = SimpleNamespace(
            id=message_id,
            thread_id=thread_id,
            sender_user_id=sender_id,
            client_message_id="text-revoke-client",
            message_type="text",
            body="不能泄露的原文字",
            status="accepted",
            occurred_at=NOW,
            extra_data={
                "sender_upstream_uid": "42",
                "recipient_upstream_uid": "9",
                "quote": {"text": "引用"},
            },
        )
        sender_projection = SimpleNamespace(
            owner_user_id=sender_id,
            direction="outgoing",
            body=canonical.body,
            status="sent",
            extra_data={"quote": {"text": "引用"}},
        )
        recipient_projection = SimpleNamespace(
            owner_user_id=recipient_id,
            direction="incoming",
            body=canonical.body,
            status="received",
            extra_data={"quote": {"text": "引用"}},
        )
        conversations = [
            SimpleNamespace(
                last_message_at=NOW,
                extra_data={
                    "last_message": canonical.body,
                    "last_message_id": str(message_id),
                },
            ),
            SimpleNamespace(
                last_message_at=NOW,
                extra_data={
                    "last_message": canonical.body,
                    "last_message_id": str(message_id),
                },
            ),
        ]
        send_delivery = SimpleNamespace(
            status=send_status,
            payload={
                "canonical_message_id": str(message_id),
                "message_type": "text",
            },
            locked_by="worker" if send_status == "processing" else None,
            locked_until=NOW if send_status == "processing" else None,
            last_error=None,
        )
        actor = LocalAccount(sender_id, uuid.uuid4(), "42", "", 180)
        return (
            canonical,
            sender_projection,
            recipient_projection,
            conversations,
            send_delivery,
            actor,
        )

    def test_repository_revoke_scrubs_both_projections_and_is_idempotent(self) -> None:
        (
            canonical,
            sender_projection,
            recipient_projection,
            conversations,
            send_delivery,
            actor,
        ) = self._revoke_rows()
        db = self._QueueDb(
            scalar_values=[canonical, send_delivery],
            scalars_values=[
                [sender_projection, recipient_projection],
                conversations,
            ],
        )
        result = SqlAlchemyCanonicalMessageStore(
            db, compatibility_mode="enabled"
        ).revoke_text(
            actor=actor,
            peer_upstream_uid="9",
            canonical_message_id=canonical.id,
            revoked_at=NOW + timedelta(minutes=2),
        )

        self.assertTrue(result.created)
        self.assertEqual(result.recalled_text, "不能泄露的原文字")
        self.assertIsNone(result.tim_mirror)
        self.assertEqual(canonical.status, "revoked")
        self.assertEqual(canonical.body, REVOKED_TEXT_PLACEHOLDER)
        self.assertNotIn("quote", canonical.extra_data)
        self.assertEqual(send_delivery.status, "cancelled")
        for projection in (sender_projection, recipient_projection):
            self.assertEqual(projection.status, "revoked")
            self.assertIsNone(projection.body)
            self.assertNotIn("quote", projection.extra_data)
        self.assertEqual(
            sender_projection.extra_data["recalled_text"],
            "不能泄露的原文字",
        )
        self.assertNotIn("recalled_text", recipient_projection.extra_data)
        self.assertEqual(
            {row.extra_data["last_message"] for row in conversations},
            {REVOKED_TEXT_PLACEHOLDER},
        )

        repeat_db = self._QueueDb(
            scalar_values=[canonical, send_delivery, None],
            scalars_values=[[sender_projection, recipient_projection]],
        )
        repeated = SqlAlchemyCanonicalMessageStore(
            repeat_db, compatibility_mode="enabled"
        ).revoke_text(
            actor=actor,
            peer_upstream_uid="9",
            canonical_message_id=canonical.id,
            revoked_at=NOW + timedelta(hours=1),
        )
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.recalled_text, "不能泄露的原文字")

    def test_delivered_or_racing_tim_send_gets_optional_revoke_outbox(self) -> None:
        for mode, expected_status in (("enabled", "pending"), ("retired", "cancelled")):
            with self.subTest(mode=mode):
                (
                    canonical,
                    sender_projection,
                    recipient_projection,
                    conversations,
                    send_delivery,
                    actor,
                ) = self._revoke_rows(send_status="delivered")
                revoke_delivery = SimpleNamespace(
                    id=uuid.uuid4(),
                    status=expected_status,
                )
                db = self._QueueDb(
                    scalar_values=[canonical, send_delivery],
                    scalars_values=[
                        [sender_projection, recipient_projection],
                        conversations,
                        [revoke_delivery],
                    ],
                )
                result = SqlAlchemyCanonicalMessageStore(
                    db, compatibility_mode=mode
                ).revoke_text(
                    actor=actor,
                    peer_upstream_uid="9",
                    canonical_message_id=canonical.id,
                    revoked_at=NOW,
                )

                self.assertIsNotNone(result.tim_mirror)
                self.assertEqual(result.tim_mirror.status, expected_status)

    def test_repository_revoke_rejects_recipient_and_expired_sender(self) -> None:
        canonical, sender_projection, recipient_projection, _, _, actor = (
            self._revoke_rows()
        )
        recipient = LocalAccount(
            recipient_projection.owner_user_id,
            uuid.uuid4(),
            "9",
            "",
            180,
        )
        with self.assertRaises(LocalMessageRevocationDenied):
            SqlAlchemyCanonicalMessageStore(
                self._QueueDb(scalar_values=[canonical]),
                compatibility_mode="enabled",
            ).revoke_text(
                actor=recipient,
                peer_upstream_uid="42",
                canonical_message_id=canonical.id,
                revoked_at=NOW,
            )

        with self.assertRaises(LocalMessageRevocationExpired):
            SqlAlchemyCanonicalMessageStore(
                self._QueueDb(
                    scalar_values=[canonical],
                    scalars_values=[[sender_projection, recipient_projection]],
                ),
                compatibility_mode="enabled",
            ).revoke_text(
                actor=actor,
                peer_upstream_uid="9",
                canonical_message_id=canonical.id,
                revoked_at=NOW + timedelta(minutes=2, microseconds=1),
            )

    def test_expected_source_final_compare_rejects_stale_conversation_heads(self) -> None:
        class ScalarQueueDB:
            def __init__(self, *values: object) -> None:
                self.values = list(values)
                self.statements: list[object] = []

            def scalar(self, statement: object) -> object:
                self.statements.append(statement)
                return self.values.pop(0)

        sender = LocalAccount(uuid.uuid4(), uuid.uuid4(), "42", "", 180)
        recipient = LocalAccount(uuid.uuid4(), uuid.uuid4(), "9", "", 180)
        message_id = uuid.uuid4()
        valid = {
            "id": message_id,
            "provider": "web-local",
            "upstream_message_id": str(message_id),
            "direction": "incoming",
            "message_type": "text",
            "body": "待回复消息",
            "status": "received",
            "occurred_at": NOW,
            "extra_data": {"canonical_message_id": "canonical-message-001"},
        }

        accepted_db = ScalarQueueDB(SimpleNamespace(**valid), None)
        SqlAlchemyCanonicalMessageStore(
            accepted_db, compatibility_mode="enabled"
        )._assert_expected_source_is_current(
            sender=sender,
            recipient=recipient,
            expected_source_message_identity="canonical-message-001",
        )
        self.assertEqual(len(accepted_db.statements), 2)
        latest_sql = str(
            accepted_db.statements[0].compile(dialect=postgresql.dialect())
        )
        outgoing_sql = str(
            accepted_db.statements[1].compile(dialect=postgresql.dialect())
        )
        self.assertIn("ORDER BY messages.occurred_at DESC", latest_sql)
        self.assertIn("messages.direction =", outgoing_sql)

        missing_db = ScalarQueueDB(None)
        with self.assertRaises(LocalMessageSourceStale) as missing:
            SqlAlchemyCanonicalMessageStore(
                missing_db, compatibility_mode="enabled"
            )._assert_expected_source_is_current(
                sender=sender,
                recipient=recipient,
                expected_source_message_identity="canonical-message-001",
            )
        self.assertEqual(missing.exception.code, "local_message_source_stale")

        stale_cases = {
            "identity_changed": ({**valid, "extra_data": {}}, None),
            "latest_is_outgoing": ({**valid, "direction": "outgoing"}, None),
            "latest_is_not_text": ({**valid, "message_type": "image"}, None),
            "latest_is_empty": ({**valid, "body": ""}, None),
            "latest_is_revoked": ({**valid, "status": "revoked"}, None),
            "metadata_is_revoked": (
                {**valid, "extra_data": {**valid["extra_data"], "revoked": True}},
                None,
            ),
            "outgoing_exists_after": (valid, uuid.uuid4()),
        }
        for name, (latest, outgoing_after) in stale_cases.items():
            with self.subTest(name=name):
                db = ScalarQueueDB(SimpleNamespace(**latest), outgoing_after)
                with self.assertRaises(LocalMessageSourceStale) as raised:
                    SqlAlchemyCanonicalMessageStore(
                        db, compatibility_mode="enabled"
                    )._assert_expected_source_is_current(
                        sender=sender,
                        recipient=recipient,
                        expected_source_message_identity="canonical-message-001",
                    )
                self.assertEqual(raised.exception.code, "local_message_source_stale")

    def test_all_local_sends_lock_the_direct_thread_before_final_compare(self) -> None:
        repository = (
            ROOT / "bbw_web" / "messaging" / "repository.py"
        ).read_text(encoding="utf-8")
        ensure_thread = repository.split("def _ensure_thread(", 1)[1].split(
            "def _assert_expected_source_is_current(", 1
        )[0]
        store_text = repository.split("def store_text(", 1)[1].split(
            "def list_direct_messages(", 1
        )[0]
        self.assertIn(".on_conflict_do_update(", ensure_thread)
        self.assertIn("select(ChatThread)", ensure_thread)
        self.assertIn(".with_for_update()", ensure_thread)
        self.assertIn("populate_existing=True", ensure_thread)
        self.assertLess(
            store_text.index("thread = self._ensure_thread("),
            store_text.index("self._assert_expected_source_is_current("),
        )
        self.assertLess(
            store_text.index("self._assert_expected_source_is_current("),
            store_text.index("insert(ChatMessage)"),
        )
        self.assertIn('Message.direction == "outgoing"', repository)
        self.assertGreaterEqual(
            repository.count("Message.provider == LOCAL_MESSAGE_PROVIDER"), 2
        )
        self.assertGreaterEqual(
            repository.count("Conversation.provider == LOCAL_MESSAGE_PROVIDER"), 2
        )
        self.assertIn("_projection_message_identity(latest) != expected", repository)
        self.assertIn("_projection_message_revoked(latest)", repository)

        service = (ROOT / "bbw_web" / "messaging" / "service.py").read_text(
            encoding="utf-8"
        )
        persistence = (ROOT / "bbw_web" / "persistence.py").read_text(
            encoding="utf-8"
        )
        contracts = (ROOT / "bbw_web" / "messaging" / "contracts.py").read_text(
            encoding="utf-8"
        )
        for source in (contracts, service, persistence):
            self.assertIn('expected_source_message_identity', source)
        self.assertIn('expected_source_message_identity: Any = ""', persistence)
        self.assertIn(
            "expected_source_message_identity=expected_source_message_identity",
            persistence,
        )

    def test_blacklist_query_checks_both_owners_and_both_block_kinds(self) -> None:
        class CaptureDB:
            statement = None

            def scalar(self, statement):
                self.statement = statement
                return None

        capture = CaptureDB()
        left = LocalAccount(uuid.uuid4(), uuid.uuid4(), "42", "", 180)
        right = LocalAccount(uuid.uuid4(), uuid.uuid4(), "9", "", 180)
        self.assertFalse(
            SqlAlchemyCanonicalMessageStore(capture).is_blocked_between(left, right)
        )
        compiled = capture.statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        values = {str(value) for value in compiled.params.values()}
        self.assertGreaterEqual(sql.count("relationships.owner_user_id ="), 2)
        self.assertGreaterEqual(sql.count("relationships.subject_upstream_uid ="), 2)
        self.assertIn(str(left.user_id), values)
        self.assertIn(str(right.user_id), values)
        self.assertIn(left.upstream_uid, values)
        self.assertIn(right.upstream_uid, values)
        self.assertTrue(
            any(
                value == ["blacklist", "blacklisted_by"]
                for value in compiled.params.values()
            )
        )
        self.assertIn("left_local_block_override", sql)
        self.assertIn("right_local_block_override", sql)
        self.assertGreaterEqual(sql.count("NOT (EXISTS"), 2)
        self.assertIn("web-local", values)

    def test_canonical_tables_cover_thread_members_message_receipt_and_outbox(self) -> None:
        self.assertEqual(ChatThread.__tablename__, "chat_threads")
        self.assertEqual(ChatMember.__tablename__, "chat_members")
        self.assertEqual(ChatMessage.__tablename__, "chat_messages")
        self.assertEqual(MessageReceipt.__tablename__, "message_receipts")
        self.assertEqual(MessageDelivery.__tablename__, "message_deliveries")
        constraints = {item.name for item in ChatMessage.__table__.constraints}
        self.assertIn("uq_chat_messages_sender_client", constraints)
        delivery_constraints = {item.name for item in MessageDelivery.__table__.constraints}
        self.assertIn("uq_message_deliveries_message_target", delivery_constraints)

    def test_direct_thread_key_is_symmetric_and_internal(self) -> None:
        left = uuid.uuid4()
        right = uuid.uuid4()
        self.assertEqual(direct_thread_key(left, right), direct_thread_key(right, left))
        self.assertIn(str(left), direct_thread_key(left, right))
        self.assertIn(str(right), direct_thread_key(left, right))

    def test_migration_0010_follows_local_auth_and_creates_tim_outbox(self) -> None:
        source = (
            ROOT
            / "migrations"
            / "versions"
            / "20260725_0010_canonical_local_messaging.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision: str = "20260725_0010"', source)
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "20260725_0009"', source)
        for table in (
            "chat_threads",
            "chat_members",
            "chat_messages",
            "message_receipts",
            "message_deliveries",
        ):
            self.assertIn(f'"{table}"', source)

    def test_repository_writes_two_owner_projections_and_pending_tim_delivery(self) -> None:
        source = (
            ROOT / "bbw_web" / "messaging" / "repository.py"
        ).read_text(encoding="utf-8")
        self.assertIn('direction="outgoing"', source)
        self.assertIn('direction="incoming"', source)
        self.assertIn("owner_user_id=owner.user_id", source)
        self.assertIn("channel=TIM_MIRROR_CHANNEL", source)
        self.assertIn('status="delivered" if is_local else "pending"', source)
        self.assertIn('required=is_local', source)
        self.assertIn('"canonical_message_id": str(canonical.id)', source)
        self.assertIn('"client_message_id": canonical.client_message_id', source)
        self.assertIn('"quote": dict(canonical.extra_data or {}).get("quote") or {}', source)
        self.assertGreaterEqual(source.count(".join(UserCredential"), 2)
        self.assertGreaterEqual(source.count("UserCredential.disabled_at.is_(None)"), 2)
        self.assertIn("def claim_tim_outbox(", source)
        self.assertIn("def mark_tim_delivered(", source)
        self.assertIn("def mark_tim_failed(", source)
        self.assertIn('"failed" if row.attempt_count >= row.max_attempts else "retry"', source)
        self.assertIn('MessageDelivery.status == "processing"', source)
        self.assertIn("Message.owner_user_id == peer.user_id", source)
        self.assertIn('Message.direction == "outgoing"', source)
        self.assertIn('"is_peer_read": True', source)
        self.assertIn('"read_at": occurred_at.isoformat()', source)
        self.assertIn("def revoke_text(", source)
        self.assertIn('target_key = f"text-revoke:{recipient_upstream_uid}"', source)
        self.assertIn('"operation": "revoke"', source)
        self.assertIn('required=False', source)
        self.assertIn('projection.status = "revoked"', source)
        self.assertIn('projection.body = None', source)
        self.assertIn('projection_metadata["recalled_text"] = recalled_text', source)

    def test_revoke_is_exposed_through_persistence_and_canonical_route(self) -> None:
        persistence = (ROOT / "bbw_web" / "persistence.py").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "bbw_web" / "api.py").read_text(encoding="utf-8")
        bff = (ROOT / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")
        jobs = (ROOT / "bbw_web" / "jobs.py").read_text(encoding="utf-8")

        self.assertIn("def revoke_local_text_message(", persistence)
        self.assertIn(").revoke_text(", persistence)
        self.assertIn('"bbw_web.jobs.mirror_tim_message_delivery"', persistence)
        self.assertIn("local_text_revoker=local_text_revoker", api)
        for field in (
            '"provider": "web-local"',
            '"canonical_message_id": message_id',
            '"revoked": True',
            '"revoked_at": result.revoked_at.isoformat()',
            '"recalled_text": result.recalled_text',
            '"tim_mirror_status": tim_mirror_status',
        ):
            self.assertIn(field, api)
        self.assertIn('data.get("canonical_message_id")', bff)
        self.assertLess(
            bff.index('local_revoker = getattr(self, "_request_local_text_revoker", None)'),
            bff.index("u.native.tim_rest.revoke_c2c(from_uid, to_uid, msg_key)"),
        )
        self.assertIn("def _tim_send_delivery_target_key(", jobs)
        self.assertIn('return to_uid if message_type == "text"', jobs)

    def test_retention_service_purges_expired_canonical_authority(self) -> None:
        class ScalarRows:
            def __iter__(self):
                return iter((uuid.uuid4(), uuid.uuid4()))

        class DeleteResult:
            rowcount = 2

        class CaptureDB:
            def __init__(self) -> None:
                self.select_statement = None
                self.delete_statement = None

            def scalars(self, statement):
                self.select_statement = statement
                return ScalarRows()

            def execute(self, statement):
                self.delete_statement = statement
                return DeleteResult()

        capture = CaptureDB()
        service = RetentionService(capture, object())

        self.assertEqual(
            service.purge_expired_canonical_messages(at=NOW, limit=100),
            2,
        )
        select_sql = str(
            capture.select_statement.compile(dialect=postgresql.dialect())
        )
        delete_sql = str(
            capture.delete_statement.compile(dialect=postgresql.dialect())
        )
        self.assertIn("chat_messages.retention_expires_at <=", select_sql)
        self.assertIn("DELETE FROM chat_messages", delete_sql)


if __name__ == "__main__":
    unittest.main()
