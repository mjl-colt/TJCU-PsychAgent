import unittest
from types import SimpleNamespace

from app.agents.blackboard import (
    AgentAction,
    AgentCommand,
    AgentName,
    BlackboardState,
    FlowStage,
    RuntimeEvent,
    RuntimeEventType,
)
from app.agents.event_projection import (
    EventProjectionError,
    RuntimeEventProjector,
    attach_state_projection,
    interrupted_compaction_ids,
)
from app.agents.runtime_guard import (
    RuntimeInvariantError,
    annotate_runtime_events,
    validate_agent_batch,
    validate_runtime_transition,
)
from app.schemas.dtos import AiMessage
from app.services.memory import compact_history_with_trace
from app.services.report import _public_runtime_event_payload


class RuntimeFoundationTests(unittest.TestCase):
    def test_legacy_checkpoint_inbox_is_ignored(self):
        state = BlackboardState.model_validate(
            {
                "request": {
                    "request_id": "req-legacy-inbox",
                    "user_input": "hello",
                    "model_input": "hello",
                    "input_trust": "UNTRUSTED",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                "understanding": {
                    "intent": "CONSULT",
                    "intent_confidence": 0.91,
                    "topic": "sleep",
                    "entities": ["legacy"],
                    "emotion": "anxious",
                    "context_need": "legacy duplicate",
                },
                "safety": {
                    "risk_level": "LOW",
                    "risk_confidence": 0.9,
                    "assessment_method": "legacy",
                    "force_risk_route": False,
                    "requires_followup": False,
                    "prompt_review": {
                        "prompt_version": 1,
                        "approved": True,
                        "reviewed_at": "2026-01-01T00:00:01Z",
                        "degraded": False,
                    },
                },
                "flow": {
                    "current_stage": "ANALYZING",
                    "inbox": [{"message_id": "req-legacy-inbox", "status": "CLAIMED"}],
                    "route_reason": "legacy duplicate",
                    "next_agents": ["ContextAgent"],
                    "completed": False,
                },
                "context": {
                    "short_term_memory": [],
                    "long_term_memory": [],
                    "retrieval_sufficient": False,
                    "final_context": "legacy duplicate",
                    "compaction": {
                        "compaction_id": "legacy-command",
                        "status": "COMPLETED",
                        "started_at": "2026-01-01T00:00:00Z",
                        "completed_at": "2026-01-01T00:00:01Z",
                    },
                },
                "response": {
                    "prompt_version": 1,
                    "candidate_prompt": "legacy duplicate",
                    "messages": [{"role": "user", "content": "hello"}],
                    "mode": "support",
                    "intent": "CONSULT",
                    "risk_level": "LOW",
                },
            }
        )

        self.assertFalse(hasattr(state.flow, "inbox"))
        self.assertNotIn("inbox", state.model_dump()["flow"])
        self.assertFalse(hasattr(state.context, "short_term_memory"))
        self.assertNotIn("long_term_memory", state.model_dump()["context"])
        self.assertNotIn("retrieval_sufficient", state.model_dump()["context"])
        self.assertNotIn("final_context", state.model_dump()["context"])
        self.assertNotIn("started_at", state.model_dump()["context"]["compaction"])
        self.assertNotIn("user_input", state.model_dump()["request"])
        self.assertNotIn("input_trust", state.model_dump()["request"])
        self.assertNotIn("created_at", state.model_dump()["request"])
        self.assertNotIn("entities", state.model_dump()["understanding"])
        self.assertNotIn("context_need", state.model_dump()["understanding"])
        self.assertNotIn("intent_confidence", state.model_dump()["understanding"])
        self.assertNotIn("force_risk_route", state.model_dump()["safety"])
        self.assertNotIn("risk_confidence", state.model_dump()["safety"])
        self.assertNotIn("reviewed_at", state.model_dump()["safety"]["prompt_review"])
        self.assertNotIn("route_reason", state.model_dump()["flow"])
        self.assertNotIn("next_agents", state.model_dump()["flow"])
        self.assertNotIn("completed", state.model_dump()["flow"])
        self.assertNotIn("candidate_prompt", state.model_dump()["response"])

    def test_mandatory_guard_rejects_duplicate_command_ids(self):
        state = BlackboardState.create("hello", request_id="req-duplicate-command")
        commands = [
            AgentCommand(
                command_id="same",
                batch_id="batch-1",
                agent=AgentName.UNDERSTANDING,
                action=AgentAction.UNDERSTAND,
                state_revision=state.revision,
            ),
            AgentCommand(
                command_id="same",
                batch_id="batch-1",
                agent=AgentName.SAFETY,
                action=AgentAction.ASSESS_RISK,
                state_revision=state.revision,
            ),
        ]

        with self.assertRaises(RuntimeInvariantError):
            validate_agent_batch(state, commands)

    def test_event_projection_rebuilds_latest_privacy_safe_state(self):
        state = BlackboardState.create("手机号 [已脱敏]", request_id="req-projection")
        first = attach_state_projection(
            state,
            [RuntimeEvent(type=RuntimeEventType.TURN_STARTED, request_id=state.request.request_id, actor="Runtime")],
        )
        advanced = state.model_copy(update={"revision": state.revision + 1})
        second = attach_state_projection(
            advanced,
            [RuntimeEvent(type=RuntimeEventType.STATE_UPDATED, request_id=state.request.request_id, actor="Runtime")],
        )

        result = RuntimeEventProjector().rebuild([*first, *second])

        self.assertIsNotNone(result)
        self.assertEqual(result.state.revision, advanced.revision)
        self.assertEqual(result.state.request.model_input, "手机号 [已脱敏]")
        self.assertFalse(hasattr(result.state.request, "user_input"))

    def test_event_projection_detects_tampering(self):
        state = BlackboardState.create("hello", request_id="req-tamper")
        event = attach_state_projection(
            state,
            [RuntimeEvent(type=RuntimeEventType.TURN_STARTED, request_id=state.request.request_id, actor="Runtime")],
        )[0]
        payload = dict(event.state_projection)
        payload["revision"] = 99
        tampered = event.model_copy(update={"state_projection": payload})

        with self.assertRaises(EventProjectionError):
            RuntimeEventProjector().rebuild([tampered])

    def test_event_projection_skips_legacy_inbox_schema(self):
        state = BlackboardState.create("hello", request_id="req-legacy-projection")
        event = attach_state_projection(
            state,
            [RuntimeEvent(type=RuntimeEventType.TURN_STARTED, request_id=state.request.request_id, actor="Runtime")],
        )[0]
        legacy_projection = dict(event.state_projection)
        legacy_flow = dict(legacy_projection["flow"])
        legacy_flow["inbox"] = [{"message_id": state.request.request_id, "status": "CLAIMED"}]
        legacy_projection["flow"] = legacy_flow
        legacy = event.model_copy(update={"state_projection": legacy_projection})

        self.assertIsNone(RuntimeEventProjector().rebuild([legacy]))

    def test_interrupted_compaction_is_detected(self):
        state = BlackboardState.create("hello", request_id="req-compact")
        start = RuntimeEvent(
            type=RuntimeEventType.CONTEXT_COMPACTION_STARTED,
            request_id=state.request.request_id,
            actor="ContextAgent",
            command_id="compact-1",
            metadata={"compactionId": "compact-1"},
        )
        self.assertEqual(interrupted_compaction_ids([start]), ("compact-1",))

        end = RuntimeEvent(
            type=RuntimeEventType.CONTEXT_COMPACTION_COMPLETED,
            request_id=state.request.request_id,
            actor="ContextAgent",
            command_id="compact-1",
            metadata={"compactionId": "compact-1"},
        )
        self.assertEqual(interrupted_compaction_ids([start, end]), ())

    def test_compaction_result_has_stable_audit_metadata(self):
        settings = SimpleNamespace(
            memory_compaction_enabled=True,
            memory_compaction_recent_messages=4,
            memory_summary_max_chars=220,
        )
        history = [AiMessage(role="user", content=f"message {index}") for index in range(10)]

        result = compact_history_with_trace(
            history,
            settings,
            "current",
            compaction_id="command-1",
        )

        self.assertTrue(result.compacted)
        self.assertEqual(result.source_message_count, 10)
        self.assertEqual(result.retained_message_count, 5)
        self.assertEqual(len(result.summary_hash), 64)

    def test_runtime_events_always_include_state_coordinates(self):
        state = BlackboardState.create("hello", request_id="req-event-audit")
        event = RuntimeEvent(type=RuntimeEventType.TURN_STARTED, request_id=state.request.request_id, actor="Runtime")

        annotated = annotate_runtime_events(state, [event])[0]

        self.assertEqual(annotated.event_id, event.event_id)
        self.assertEqual(annotated.request_id, event.request_id)
        self.assertEqual(annotated.metadata["runtimeStage"], FlowStage.RECEIVED.value)
        self.assertEqual(annotated.metadata["runtimeRevision"], 0)

    def test_mandatory_guard_blocks_unreviewed_generation(self):
        state = BlackboardState.create("hello", request_id="req-gate")
        invalid = state.model_copy(
            update={"flow": state.flow.model_copy(update={"current_stage": FlowStage.READY_FOR_GENERATION})}
        )
        with self.assertRaises(RuntimeInvariantError):
            validate_runtime_transition(state, invalid)

    def test_admin_event_projection_hides_prompt_and_history_bodies(self):
        payload = {
            "metadata": {"stateProjectionHash": "abc"},
            "state_projection": {
                "revision": 3,
                "request": {"model_input": "private conversation"},
                "flow": {"current_stage": "ANALYZING"},
            },
            "outcome": {
                "command": {"command_id": "c1"},
                "success": True,
                "update": {
                    "section": "response",
                    "data": {"prompt_version": 1, "candidate_prompt": "secret prompt", "messages": ["history"]},
                },
            },
        }

        public, projection = _public_runtime_event_payload(payload)

        self.assertNotIn("state_projection", public)
        self.assertNotIn("candidate_prompt", str(public))
        self.assertNotIn("secret prompt", str(public))
        self.assertEqual(projection["stage"], "ANALYZING")


if __name__ == "__main__":
    unittest.main()
