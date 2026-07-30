from __future__ import annotations

import ast
import re
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from bbw_agent.autonomous import (
    agent_candidate_compatibility,
    autonomy_message_identity,
    autonomy_reply_not_before,
)
from bbw_agent.contact_policy import (
    ContactRelationshipSignals,
    contact_policy_allows_auto_reply,
    derive_relationship_stage,
    relationship_stage_allows_address_terms,
    reply_risk_boundary,
)
from bbw_agent.style_sampling import (
    STYLE_SANITIZER_VERSION,
    STYLE_SAMPLING_POLICY_VERSION,
    StyleSampleCandidate,
    StyleSamplingError,
    stratified_style_samples,
    style_profile_is_current,
)


ROOT = Path(__file__).resolve().parents[1]


def candidate(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "uid": "candidate-1",
        "id": "candidate-1",
        "sex": "女",
        "property": "B",
        "age": 26,
    }
    values.update(changes)
    return values


class CandidateCompatibilityTests(unittest.TestCase):
    def decision(
        self,
        profile: dict[str, object] | None = None,
        *,
        owner: dict[str, object] | None = None,
        preference: object = None,
        target: str = "candidate-1",
    ):
        return agent_candidate_compatibility(
            owner_profile=owner
            or {"match_gender": "女", "match_property": "B"},
            candidate_profile=profile or candidate(),
            target_upstream_uid=target,
            match_preference=preference,
        )

    def test_z_owner_preferring_b_only_allows_b_candidate(self) -> None:
        self.assertTrue(self.decision().allowed)

        for property_value in ("Z", "双"):
            with self.subTest(property=property_value):
                decision = self.decision(candidate(property=property_value))
                self.assertFalse(decision.allowed)
                self.assertEqual(
                    decision.reason, "candidate_property_incompatible"
                )

    def test_gender_and_adult_age_are_explicit_hard_gates(self) -> None:
        self.assertEqual(
            self.decision(candidate(sex="男")).reason,
            "candidate_gender_incompatible",
        )
        self.assertEqual(
            self.decision(candidate(age=None)).reason,
            "candidate_age_missing",
        )
        self.assertEqual(
            self.decision(candidate(age=17)).reason,
            "candidate_underage_or_invalid",
        )

    def test_candidate_identity_must_match_the_task_target(self) -> None:
        decision = self.decision(candidate(id="different-user"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "candidate_identity_mismatch")

    def test_missing_owner_property_preference_fails_closed(self) -> None:
        decision = self.decision(owner={"match_gender": "女"})

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "owner_property_preference_missing")

    def test_native_age_preference_only_narrows_legacy_preferences(self) -> None:
        preference = SimpleNamespace(
            enabled=True,
            gender_preference="female",
            property_preference="B",
            min_age=25,
            max_age=30,
        )

        self.assertTrue(self.decision(preference=preference).allowed)
        self.assertEqual(
            self.decision(candidate(age=31), preference=preference).reason,
            "candidate_age_incompatible",
        )

    def test_conflicting_preference_sources_fail_closed(self) -> None:
        preference = SimpleNamespace(
            enabled=True,
            gender_preference="female",
            property_preference="Z",
            min_age=18,
            max_age=120,
        )

        decision = self.decision(preference=preference)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "owner_preferences_conflict")


class StyleSamplingTests(unittest.TestCase):
    def test_samples_are_balanced_and_short_messages_are_capped(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        candidates = []
        for peer_index in range(10):
            for message_index in range(20):
                text = (
                    f"关于话题{peer_index}的第{message_index}条完整表达"
                    if message_index < 16
                    else f"问{message_index % 10}"
                )
                candidates.append(
                    StyleSampleCandidate(
                        peer_key=f"peer-{peer_index}",
                        text=text,
                        occurred_at=now - timedelta(seconds=len(candidates)),
                    )
                )

        selection = stratified_style_samples(candidates)
        counts = Counter(item.peer_key for item in selection.items)
        short_count = sum(len(item.text) <= 2 for item in selection.items)

        self.assertEqual(len(selection.items), 100)
        self.assertEqual(len(set(selection.samples)), 100)
        self.assertEqual(selection.source_peer_count, 10)
        self.assertLessEqual(max(counts.values()), 10)
        self.assertLessEqual(max(counts.values()) * 100, len(selection.items) * 15)
        self.assertLessEqual(short_count * 5, len(selection.items))

    def test_duplicate_filler_and_system_messages_are_excluded(self) -> None:
        candidates = [
            StyleSampleCandidate(f"peer-{index}", f"有效表达内容{index}")
            for index in range(8)
        ]
        candidates.extend(
            (
                StyleSampleCandidate("peer-0", "哈哈"),
                StyleSampleCandidate("peer-1", "12345"),
                StyleSampleCandidate("peer-2", "我们已经是好友了，来聊天吧"),
                StyleSampleCandidate("peer-3", "有效表达内容0"),
            )
        )

        selection = stratified_style_samples(candidates)

        self.assertEqual(len(selection.items), 8)
        self.assertNotIn("哈哈", selection.samples)
        self.assertNotIn("12345", selection.samples)

    def test_too_few_contacts_fail_closed(self) -> None:
        candidates = [
            StyleSampleCandidate(
                f"peer-{index % 7}",
                f"第{index}条足够长的有效表达",
            )
            for index in range(70)
        ]

        with self.assertRaisesRegex(
            StyleSamplingError,
            "insufficient_style_diversity",
        ):
            stratified_style_samples(candidates)

    def test_old_profile_versions_are_invalidated(self) -> None:
        current = SimpleNamespace(
            source_message_count=80,
            source_peer_count=8,
            sampling_policy_version=STYLE_SAMPLING_POLICY_VERSION,
            sanitizer_version=STYLE_SANITIZER_VERSION,
        )
        stale = SimpleNamespace(
            source_message_count=80,
            source_peer_count=8,
            sampling_policy_version=STYLE_SAMPLING_POLICY_VERSION - 1,
            sanitizer_version=STYLE_SANITIZER_VERSION,
        )

        self.assertTrue(style_profile_is_current(current))
        self.assertFalse(style_profile_is_current(stale))


class ReplyCadenceTests(unittest.TestCase):
    def test_reply_waits_thirty_seconds_from_latest_inbound(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

        self.assertEqual(
            autonomy_reply_not_before(now - timedelta(seconds=5), now=now),
            now + timedelta(seconds=25),
        )
        self.assertEqual(
            autonomy_reply_not_before(now - timedelta(seconds=40), now=now),
            now,
        )

    def test_message_identity_changes_when_mutable_content_changes(self) -> None:
        common = {
            "provider": "tim",
            "upstream_message_id": "message-1",
            "canonical_message_id": "canonical-1",
            "direction": "incoming",
            "message_type": "text",
        }
        original = autonomy_message_identity(body="明天见吗", **common)
        edited = autonomy_message_identity(body="后天见吗", **common)
        revoked = autonomy_message_identity(
            body="明天见吗",
            revoked=True,
            **common,
        )

        self.assertTrue(original.startswith("msgv2:"))
        self.assertNotEqual(original, edited)
        self.assertNotEqual(original, revoked)


class ContactPolicyTests(unittest.TestCase):
    def test_relationship_stages_are_deterministic(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

        self.assertEqual(
            derive_relationship_stage(ContactRelationshipSignals(), now=now),
            "new",
        )
        self.assertEqual(
            derive_relationship_stage(
                ContactRelationshipSignals(
                    total_message_count=2,
                    incoming_message_count=1,
                    outgoing_message_count=1,
                ),
                now=now,
            ),
            "engaged",
        )
        self.assertEqual(
            derive_relationship_stage(
                ContactRelationshipSignals(
                    total_message_count=20,
                    incoming_message_count=10,
                    outgoing_message_count=10,
                    last_incoming_at=now - timedelta(days=1),
                    last_outgoing_at=now - timedelta(days=2),
                ),
                now=now,
            ),
            "established",
        )
        self.assertEqual(
            derive_relationship_stage(
                ContactRelationshipSignals(
                    total_message_count=100,
                    incoming_message_count=50,
                    outgoing_message_count=50,
                ),
                now=now,
            ),
            "close",
        )

    def test_manual_and_inactive_safety_states_win(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        deep = ContactRelationshipSignals(
            total_message_count=120,
            incoming_message_count=60,
            outgoing_message_count=60,
            blocked=True,
        )
        self.assertEqual(
            derive_relationship_stage(deep, now=now, stage_override="close"),
            "inactive",
        )
        self.assertEqual(
            derive_relationship_stage(deep, now=now, mode="manual_only"),
            "manual_only",
        )

        unanswered = ContactRelationshipSignals(
            total_message_count=40,
            incoming_message_count=20,
            outgoing_message_count=20,
            latest_direction="outgoing",
            latest_message_at=now - timedelta(days=8),
        )
        self.assertEqual(
            derive_relationship_stage(unanswered, now=now),
            "inactive",
        )

    def test_auto_reply_inherits_global_policy_without_contact_opt_in(self) -> None:
        options = {
            "mode": "auto_low_risk",
            "paused": False,
            "relationship_stage": "engaged",
        }
        self.assertTrue(
            contact_policy_allows_auto_reply(persisted=False, **options)
        )
        self.assertTrue(
            contact_policy_allows_auto_reply(persisted=True, **options)
        )
        self.assertTrue(
            contact_policy_allows_auto_reply(
                persisted=False,
                mode="suggest_only",
                paused=False,
                relationship_stage="new",
            )
        )
        self.assertFalse(
            contact_policy_allows_auto_reply(
                persisted=True,
                risk_boundary="financial",
                **options,
            )
        )
        self.assertFalse(
            contact_policy_allows_auto_reply(
                persisted=True,
                mode="suggest_only",
                paused=False,
                relationship_stage="engaged",
            )
        )
        self.assertFalse(
            contact_policy_allows_auto_reply(
                persisted=True,
                mode="manual_only",
                paused=False,
                relationship_stage="established",
            )
        )
        self.assertFalse(
            contact_policy_allows_auto_reply(
                persisted=True,
                mode="auto_low_risk",
                paused=True,
                relationship_stage="close",
            )
        )

    def test_low_risk_mode_routes_sensitive_topics_to_manual_review(self) -> None:
        self.assertEqual(reply_risk_boundary("可以给我转账吗"), "financial")
        self.assertEqual(
            reply_risk_boundary("把你的具体地址发我"),
            "precise_location_or_meeting",
        )
        self.assertEqual(reply_risk_boundary("发张裸照"), "explicit_or_consent")
        self.assertEqual(reply_risk_boundary("你明天有空吗"), "")

    def test_address_terms_require_an_established_relationship(self) -> None:
        self.assertFalse(relationship_stage_allows_address_terms("new"))
        self.assertFalse(relationship_stage_allows_address_terms("engaged"))
        self.assertTrue(relationship_stage_allows_address_terms("established"))
        self.assertTrue(relationship_stage_allows_address_terms("close"))


class RelationshipAgentSourceContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8-sig")

    def function_source(
        self,
        relative: str,
        name: str,
        *,
        containing: str = "",
    ) -> str:
        source = self.read(relative)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                segment = ast.get_source_segment(source, node)
                if segment is not None and (
                    not containing or containing in segment
                ):
                    return segment
        self.fail(f"function not found: {relative}:{name}")

    def javascript_function_source(self, relative: str, name: str) -> str:
        source = self.read(relative)
        marker = f"function {name}("
        marker_index = source.find(marker)
        self.assertGreaterEqual(marker_index, 0, f"function not found: {name}")
        start = source.rfind("\n", 0, marker_index) + 1
        following = re.search(
            r"\n(?:async\s+)?function\s+[A-Za-z0-9_$]+\(",
            source[marker_index + len(marker) :],
        )
        end = (
            marker_index + len(marker) + following.start()
            if following is not None
            else len(source)
        )
        return source[start:end]

    def test_contact_policy_model_and_migration_default_to_suggestions(self) -> None:
        migration = self.read(
            "migrations/versions/20260727_0025_contact_agent_policies.py"
        )
        self.assertIn('revision: str = "20260727_0025"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260727_0024"',
            migration,
        )
        self.assertIn('"ai_agent_contact_policies"', migration)
        self.assertIn('server_default="suggest_only"', migration)
        self.assertIn('"contact_policy_version"', migration)
        self.assertIn('"relationship_stage"', migration)
        self.assertIn("SET status = 'stale'", migration)
        self.assertIn("contact_policy_required", migration)

        models = self.read("bbw_prod/models.py")
        policy = models.split("class AiAgentContactPolicy(", 1)[1]
        self.assertIn('__tablename__ = "ai_agent_contact_policies"', policy)
        self.assertIn('server_default="suggest_only"', policy)
        self.assertIn('"peer_upstream_uid", "allow_address_terms"', policy)

    def test_chat_suggestions_are_hidden_by_default_and_controlled_from_agent(self) -> None:
        migration = self.read(
            "migrations/versions/20260728_0026_chat_suggestion_visibility.py"
        )
        self.assertIn('revision: str = "20260728_0026"', migration)
        self.assertIn(
            'down_revision: Union[str, Sequence[str], None] = "20260727_0025"',
            migration,
        )
        self.assertIn('"chat_suggestions_enabled"', migration)
        self.assertIn('server_default=sa.text("false")', migration)

        models = self.read("bbw_prod/models.py")
        setting = models.split("class AiAgentSetting(", 1)[1].split(
            "class AiAgentExecutionSetting(", 1
        )[0]
        self.assertIn("chat_suggestions_enabled: Mapped[bool]", setting)
        self.assertIn('server_default=text("false")', setting)

        api = self.read("bbw_agent/api.py")
        body = api.split("class AgentSettingsBody(", 1)[1].split(
            "class IdempotentRunBody(", 1
        )[0]
        self.assertIn("chat_suggestions_enabled: StrictBool = False", body)
        update_settings = self.function_source(
            "bbw_agent/api.py", "update_agent_settings"
        )
        self.assertIn(
            "chat_suggestions_enabled=body.chat_suggestions_enabled",
            update_settings,
        )

        public_settings = self.function_source(
            "bbw_agent/services.py", "settings_public"
        )
        self.assertIn('"chat_suggestions_enabled": False', public_settings)
        self.assertIn(
            'getattr(row, "chat_suggestions_enabled", False)',
            public_settings,
        )
        save_settings = self.function_source(
            "bbw_agent/services.py", "save_agent_settings"
        )
        self.assertIn(
            "row.chat_suggestions_enabled = bool(chat_suggestions_enabled)",
            save_settings,
        )

        app = self.read("bbw_web/static/app.js")
        self.assertIn("aiAgentChatSuggestionsEnabled: false", app)
        self.assertIn("在聊天界面显示聊天建议", app)
        self.assertIn("默认关闭；开启后可在当前会话中生成", app)
        self.assertIn(
            "自动回复由“能力与节奏”中的全局开关独立控制",
            app,
        )
        self.assertNotIn("联系人自动回复授权", app)
        access = self.javascript_function_source(
            "bbw_web/static/app.js", "setAiAgentAccess"
        )
        self.assertIn("status?.settings?.chat_suggestions_enabled === true", access)
        self.assertIn("if (!nextEnabled)", access)
        self.assertIn("if (!nextChatSuggestionsEnabled)", access)
        chat = self.javascript_function_source(
            "bbw_web/static/app.js", "chatAgentAssistHtml"
        )
        self.assertIn("!S.aiAgentChatSuggestionsEnabled", chat)
        self.assertIn('aria-label="聊天建议"', chat)
        pane = self.javascript_function_source(
            "bbw_web/static/app.js", "chatPaneHtml"
        )
        self.assertNotIn("ContactPolicy", pane)
        self.assertNotIn("data-agent-contact-policy", pane)
        load = self.javascript_function_source(
            "bbw_web/static/app.js", "loadChatAssistStatus"
        )
        self.assertNotIn("!S.aiAgentChatSuggestionsEnabled", load)
        content = self.javascript_function_source(
            "bbw_web/static/app.js", "chatAgentAssistContentHtml"
        )
        self.assertIn("聊天建议", content)
        self.assertNotIn("联系人自动回复", content)
        self.assertNotIn("data-chat-agent-mode", content)
        self.assertNotIn("agentContactPolicyManagerContentHtml", app)
        self.assertNotIn("agentContactPoliciesSectionHtml", app)
        self.assertNotIn("data-agent-contact-policy", app)
        page = self.javascript_function_source(
            "bbw_web/static/app.js", "pageAgent"
        )
        self.assertNotIn("ContactPolicies", page)
        generate = self.javascript_function_source(
            "bbw_web/static/app.js", "generateChatAgentSuggestion"
        )
        self.assertIn("!S.aiAgentChatSuggestionsEnabled", generate)
        forms = self.javascript_function_source(
            "bbw_web/static/app.js", "handleProductForm"
        )
        self.assertIn("chat_suggestions_enabled: Boolean(", forms)

    def test_scheduler_inherits_global_auto_reply_without_contact_authorization(self) -> None:
        scheduler = self.function_source("bbw_web/jobs.py", "_schedule_autonomy_owner")
        self.assertIn("limit=20", scheduler)
        self.assertNotIn("if contact_policy is None:\n                    continue", scheduler)
        self.assertIn("contact_policy_allows_auto_reply(", scheduler)
        self.assertIn("persisted=contact_policy is not None", scheduler)
        self.assertIn("risk_boundary=risk_boundary", scheduler)
        self.assertIn("DEFAULT_MINIMUM_REPLY_DELAY_SECONDS", scheduler)
        self.assertIn("DEFAULT_MAXIMUM_REPLY_AGE_SECONDS", scheduler)
        self.assertIn("contact_policy_version=(", scheduler)
        self.assertIn("relationship_stage=relationship_stage", scheduler)

        enqueue = self.function_source(
            "bbw_agent/repositories.py",
            "enqueue",
            containing="autonomous reply requires a relationship stage",
        )
        self.assertIn('normalized_type == "reply_to_message"', enqueue)
        self.assertIn("autonomous reply requires a relationship stage", enqueue)
        self.assertNotIn("contact authorization", enqueue)

        configure = self.function_source(
            "bbw_agent/repositories.py",
            "configure",
            containing="AiAgentContactPolicy.id",
        )
        self.assertIn(".returning(AiAgentContactPolicy.id)", configure)
        self.assertIn("created = inserted_id is not None", configure)

        final_gate = self.function_source(
            "bbw_agent/repositories.py", "_contact_reply_gate_code"
        )
        self.assertIn('snapshot.get("contact_policy")', final_gate)
        self.assertIn("task_contact_version", final_gate)
        self.assertIn("current_stage != task_stage", final_gate)
        self.assertIn("contact_policy_allows_auto_reply(", final_gate)
        self.assertIn("persisted=contact_policy is not None", final_gate)

        public_policy = self.function_source(
            "bbw_agent/services.py", "contact_policy_public"
        )
        self.assertIn('"inherited": True', public_policy)
        self.assertIn('"mode": CONTACT_MODE_AUTO_LOW_RISK', public_policy)

    def test_conversation_routes_are_session_bound_and_stale_safe(self) -> None:
        status = self.function_source("bbw_agent/api.py", "conversation_assist_status")
        policy = self.function_source(
            "bbw_agent/api.py", "update_conversation_contact_policy"
        )
        suggest = self.function_source(
            "bbw_agent/api.py", "generate_conversation_suggestion"
        )
        feedback = self.function_source(
            "bbw_agent/api.py", "record_conversation_suggestion_feedback"
        )
        for route in (status, policy, suggest, feedback):
            self.assertIn("context.owner_user_id", route)
            self.assertNotIn("body.owner_user_id", route)
        self.assertIn("source_identity = head_before.message_identity", suggest)
        self.assertIn("head_after.message_identity != source_identity", suggest)
        self.assertIn("conversation_changed_during_generation", suggest)
        self.assertIn("generate_reply_draft(", suggest)
        self.assertNotIn("generate_and_send_reply(", suggest)
        self.assertNotIn("execute_account_action(", suggest)
        self.assertIn('action="ai.conversation_suggestion_feedback"', feedback)
        self.assertIn('startswith("assist:")', feedback)
        self.assertIn("generated_char_count", feedback)
        self.assertIn("edit_distance", feedback)
        self.assertNotIn("output_text", feedback)

    def test_chat_ui_keeps_suggestions_editable_and_never_sends_them_directly(self) -> None:
        app = self.read("bbw_web/static/app.js")
        for text in (
            "生成建议",
            "换一种表达",
            "采用",
            "不回复",
            "称呼白名单",
        ):
            self.assertIn(text, app)
        self.assertNotIn("data-agent-contact-policy", app)
        generate = self.javascript_function_source(
            "bbw_web/static/app.js", "generateChatAgentSuggestion"
        )
        self.assertIn("sourceDraftRevision", generate)
        self.assertIn("previousDraft", generate)
        self.assertIn("chatAssistGeneratingPeers", generate)
        self.assertIn("setChatComposerDraft(draft)", generate)
        self.assertIn("reportChatAgentSuggestionFeedback", generate)
        self.assertNotIn("sendTextMessage(", generate)
        self.assertNotIn("/replies/send", generate)
        self.assertLess(generate.index("const previousDraft"), generate.index("await api("))

        css = self.read("bbw_web/static/app.css")
        self.assertNotIn(".agent-contact-policy-controls", css)
        self.assertNotIn("#agent-contact-policy-manager", css)
        self.assertNotIn(".chat-agent-contact-policy", css)
        self.assertIn(".chat-agent-assist-head", css)
        self.assertIn(".chat-agent-assist-actions", css)
        self.assertIn("overflow-x: auto", css)


if __name__ == "__main__":
    unittest.main()
