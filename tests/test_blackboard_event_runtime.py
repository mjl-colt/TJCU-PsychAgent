import asyncio
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.blackboard import (
    AgentAction,
    AgentCommand,
    AgentName,
    AgentStateUpdate,
    BlackboardResultApplier,
    BlackboardSection,
    BlackboardState,
    ContextState,
    FlowStage,
    KnowledgeEvidence,
    PromptReviewState,
    ResponseState,
    ResponseStatus,
    RuntimeEvent,
    RuntimeEventType,
    SafetyState,
    UnderstandingState,
)
from app.agents.blackboard_runtime import BlackboardEventRuntime
from app.agents.dispatcher import AgentDispatcher
from app.agents.runtime_store import NullRuntimeStore, SqlAlchemyRuntimeStore
from app.agents.runtime_lease import RuntimeLeaseManager
from app.agents.state_coordinator import BlackboardCoordinator
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.core.database import Base
from app.models.entities import AgentRuntimeCheckpoint, AgentRuntimeEventRecord, AgentRuntimeLease
from app.schemas.dtos import AiMessage


def runtime_settings(**overrides):
    values = {
        "agent_runtime_max_events": 64,
        "agent_runtime_max_concurrency": 4,
        "agent_runtime_timeout_seconds": 0.5,
        "agent_runtime_safety_timeout_seconds": 0.5,
        "agent_runtime_max_retries": 0,
        "agent_runtime_retry_backoff_seconds": 0.0,
        "agent_runtime_max_prompt_revisions": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ParallelTracker:
    def __init__(self):
        self.started = set()
        self.both_started = asyncio.Event()

    async def rendezvous(self, name):
        self.started.add(name)
        if len(self.started) == 2:
            self.both_started.set()
        await asyncio.wait_for(self.both_started.wait(), timeout=0.25)


class FakeUnderstanding:
    name = AgentName.UNDERSTANDING

    def __init__(self, tracker):
        self.tracker = tracker

    async def run(self, command, state):
        await self.tracker.rendezvous(self.name)
        return AgentStateUpdate(
            section=BlackboardSection.UNDERSTANDING,
            data=UnderstandingState(
                intent=IntentType.CONSULT,
                topic="sleep",
            ),
        )


class FakeSafety:
    name = AgentName.SAFETY

    def __init__(self, tracker, stale_first_review=False):
        self.tracker = tracker
        self.stale_first_review = stale_first_review
        self.review_count = 0

    async def run(self, command, state):
        if command.action == AgentAction.ASSESS_RISK:
            await self.tracker.rendezvous(self.name)
            return AgentStateUpdate(
                section=BlackboardSection.SAFETY,
                data=SafetyState(
                    risk_level=RiskLevel.LOW,
                    assessment_method="TEST",
                    emotion=EmotionLabel.ANXIETY,
                ),
            )
        self.review_count += 1
        version = state.response.prompt_version
        if self.stale_first_review and self.review_count == 1:
            version += 1
        review = PromptReviewState(prompt_version=version, approved=True)
        return AgentStateUpdate(
            section=BlackboardSection.SAFETY,
            data=state.safety.model_copy(update={"prompt_review": review}),
        )


class FakeContext:
    name = AgentName.CONTEXT

    async def run(self, command, state):
        return AgentStateUpdate(
            section=BlackboardSection.CONTEXT,
            data=ContextState(
                model_history=(AiMessage(role="user", content=state.request.model_input),),
                retrieved_knowledge=(KnowledgeEvidence(source="test", content="sleep support"),),
            ),
        )


class FakeResponse:
    name = AgentName.RESPONSE

    async def run(self, command, state):
        if command.action == AgentAction.FINALIZE_RESPONSE:
            return AgentStateUpdate(
                section=BlackboardSection.RESPONSE,
                data=state.response.model_copy(update={"generation_status": ResponseStatus.READY_FOR_GENERATION}),
            )
        version = state.response.prompt_version + 1 if state.response else 1
        return AgentStateUpdate(
            section=BlackboardSection.RESPONSE,
            data=ResponseState(
                prompt_version=version,
                messages=(AiMessage(role="user", content=state.request.model_input),),
                mode="support",
                intent=state.flow.route,
                risk_level=state.safety.risk_level,
            ),
        )


class ExplodingSafety:
    name = AgentName.SAFETY

    async def run(self, command, state):
        raise TimeoutError("test timeout")


class BlackboardSchemaTests(unittest.TestCase):
    def test_invalid_risk_value_is_rejected(self):
        with self.assertRaises(ValidationError):
            SafetyState(risk_level="CRITICAL", assessment_method="TEST")

    def test_agent_cannot_write_another_section(self):
        state = BlackboardState.create("hello")
        command = AgentCommand(
            batch_id="batch",
            agent=AgentName.SAFETY,
            action=AgentAction.ASSESS_RISK,
            state_revision=state.revision,
        )
        from app.agents.blackboard import AgentExecutionOutcome, BlackboardWriteError

        outcome = AgentExecutionOutcome(
            command=command,
            success=True,
            update=AgentStateUpdate(
                section=BlackboardSection.UNDERSTANDING,
                data=UnderstandingState(intent="CHAT", topic="test"),
            ),
        )
        with self.assertRaises(BlackboardWriteError):
            BlackboardResultApplier().apply_batch(state, [outcome])

    def test_runtime_store_is_idempotent_and_sanitizes_raw_input(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        state = BlackboardState.create(
            "sanitized input",
            request_id="request-1",
        )
        event = RuntimeEvent(
            type=RuntimeEventType.TURN_STARTED,
            request_id="request-1",
            actor=AgentName.RUNTIME.value,
        )
        with Session(engine) as session:
            store = SqlAlchemyRuntimeStore(session)
            store.save(state, event)
            store.save(state, event)

            loaded = store.load("request-1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.request.model_input, "sanitized input")
            self.assertFalse(hasattr(loaded.request, "user_input"))
            self.assertEqual(session.query(AgentRuntimeCheckpoint).count(), 1)
            self.assertEqual(session.query(AgentRuntimeEventRecord).count(), 1)

    def test_runtime_store_batches_events_in_one_commit(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        state = BlackboardState.create("input", request_id="request-batch")
        events = [
            RuntimeEvent(
                type=RuntimeEventType.STATE_UPDATED,
                request_id="request-batch",
                actor=AgentName.RUNTIME.value,
                message=str(index),
            )
            for index in range(3)
        ]
        with Session(engine) as session:
            commits = 0

            def count_commit(_session):
                nonlocal commits
                commits += 1

            from sqlalchemy import event as sqlalchemy_event

            sqlalchemy_event.listen(session, "after_commit", count_commit)
            SqlAlchemyRuntimeStore(session).save_many(state, events)

            self.assertEqual(commits, 1)
            self.assertEqual(session.query(AgentRuntimeEventRecord).count(), 3)
            from app.services.report import ReportService

            metrics = ReportService(session).runtime_metrics()
            self.assertEqual(metrics["eventWindow"], 3)
            self.assertEqual(metrics["eventTypes"][RuntimeEventType.STATE_UPDATED.value], 3)

    def test_runtime_lease_serializes_the_same_request(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        settings = runtime_settings(
            agent_runtime_persistence_enabled=True,
            agent_runtime_lease_enabled=True,
            agent_runtime_lease_ttl_seconds=30,
        )
        with Session(engine) as session:
            manager = RuntimeLeaseManager(session, settings)
            first = manager.acquire("leased-request", "owner-a")
            second = manager.acquire("leased-request", "owner-b")

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertTrue(manager.renew(first))
            self.assertEqual(session.query(AgentRuntimeLease).count(), 1)

            manager.release(first)
            acquired_after_release = manager.acquire("leased-request", "owner-b")
            self.assertIsNotNone(acquired_after_release)
            manager.release(acquired_after_release)


class BlackboardRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_parallelizes_analysis_and_finishes(self):
        tracker = ParallelTracker()
        settings = runtime_settings()
        agents = [FakeUnderstanding(tracker), FakeSafety(tracker), FakeContext(), FakeResponse()]
        runtime = BlackboardEventRuntime(
            BlackboardCoordinator(settings),
            AgentDispatcher(agents, settings),
            settings,
            NullRuntimeStore(),
        )

        execution = await runtime.run(BlackboardState.create("我最近压力很大，总是睡不着"))

        self.assertNotEqual(execution.state.flow.current_stage, FlowStage.COMPLETED)
        self.assertEqual(execution.state.flow.route, IntentType.CONSULT)
        self.assertEqual(tracker.started, {AgentName.UNDERSTANDING, AgentName.SAFETY})
        self.assertIsNotNone(execution.state.context)
        self.assertEqual(execution.state.response.generation_status, ResponseStatus.READY_FOR_GENERATION)
        self.assertEqual(execution.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        self.assertEqual(len({event.event_id for event in execution.events}), len(execution.events))

    async def test_runtime_resumes_pending_commands_with_stable_ids(self):
        tracker = ParallelTracker()
        settings = runtime_settings()
        coordinator = BlackboardCoordinator(settings)
        initial = BlackboardState.create("我最近压力很大，总是睡不着", request_id="resume-request")
        started = coordinator.handle(
            initial,
            RuntimeEvent(
                type=RuntimeEventType.TURN_STARTED,
                request_id=initial.request.request_id,
                actor=AgentName.RUNTIME.value,
            ),
        )
        expected_command_ids = set(started.state.flow.active_batch.expected)
        runtime = BlackboardEventRuntime(
            coordinator,
            AgentDispatcher(
                [FakeUnderstanding(tracker), FakeSafety(tracker), FakeContext(), FakeResponse()],
                settings,
            ),
            settings,
        )

        execution = await runtime.run(started.state, resume=True)

        self.assertIn(RuntimeEventType.TURN_RECOVERY_STARTED, [event.type for event in execution.events])
        self.assertEqual(execution.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        recovery = next(
            event for event in execution.events if event.type == RuntimeEventType.TURN_RECOVERY_STARTED
        )
        self.assertEqual(set(recovery.metadata["rerunCommandIds"]), expected_command_ids)

    async def test_recovery_replays_durable_outcomes_without_rerunning_agents(self):
        tracker = ParallelTracker()
        settings = runtime_settings()
        coordinator = BlackboardCoordinator(settings)
        initial = BlackboardState.create("我最近压力很大，总是睡不着", request_id="replay-request")
        started = coordinator.handle(
            initial,
            RuntimeEvent(
                type=RuntimeEventType.TURN_STARTED,
                request_id=initial.request.request_id,
                actor=AgentName.RUNTIME.value,
            ),
        )
        dispatcher = AgentDispatcher([FakeUnderstanding(tracker), FakeSafety(tracker)], settings)
        outcomes = await dispatcher.dispatch(started.commands, started.state)
        applied = BlackboardResultApplier().apply_batch(started.state, outcomes)
        runtime = BlackboardEventRuntime(coordinator, dispatcher, settings)
        durable_outcomes = [runtime._outcome_event(applied, outcome) for outcome in outcomes]

        recovery_events = runtime._recovery_events(applied, durable_outcomes)

        marker = recovery_events[0]
        self.assertEqual(marker.type, RuntimeEventType.TURN_RECOVERY_STARTED)
        self.assertEqual(set(marker.metadata["replayedCommandIds"]), set(started.state.flow.active_batch.expected))
        self.assertEqual(marker.metadata["rerunCommandIds"], [])
        self.assertNotIn(RuntimeEventType.AGENT_BATCH_REQUESTED, [event.type for event in recovery_events])

    async def test_generation_lifecycle_makes_completed_mean_final_text_exists(self):
        tracker = ParallelTracker()
        settings = runtime_settings()
        coordinator = BlackboardCoordinator(settings)
        runtime = BlackboardEventRuntime(
            coordinator,
            AgentDispatcher(
                [FakeUnderstanding(tracker), FakeSafety(tracker), FakeContext(), FakeResponse()],
                settings,
            ),
            settings,
        )
        execution = await runtime.run(BlackboardState.create("我最近压力很大，总是睡不着"))

        self.assertEqual(execution.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        generating = coordinator.generation_started(execution.state)
        generated_text = "先从今晚固定起床时间开始。[K1]"
        completed = coordinator.generation_completed(
            generating.state,
            generated_text,
            cited_ids=("K1",),
        )

        self.assertEqual(generating.state.flow.current_stage, FlowStage.GENERATING)
        self.assertEqual(completed.state.flow.current_stage, FlowStage.COMPLETED)
        # This asserts lossless state persistence, not a required Chinese reply.
        self.assertEqual(completed.state.response.final_response, generated_text)
        self.assertEqual(completed.events[0].metadata["citedEvidenceIds"], ["K1"])
        self.assertEqual(
            [event.type for event in completed.events],
            [
                RuntimeEventType.GENERATION_COMPLETED,
                RuntimeEventType.TURN_COMPLETED,
            ],
        )

    async def test_stale_prompt_review_forces_a_new_version(self):
        tracker = ParallelTracker()
        settings = runtime_settings()
        safety = FakeSafety(tracker, stale_first_review=True)
        runtime = BlackboardEventRuntime(
            BlackboardCoordinator(settings),
            AgentDispatcher([FakeUnderstanding(tracker), safety, FakeContext(), FakeResponse()], settings),
            settings,
        )

        execution = await runtime.run(BlackboardState.create("我最近压力很大，总是睡不着"))

        self.assertNotEqual(execution.state.flow.current_stage, FlowStage.COMPLETED)
        self.assertEqual(execution.state.response.prompt_version, 2)
        self.assertEqual(execution.state.safety.prompt_review.prompt_version, 2)
        self.assertEqual(execution.state.flow.review_attempts, 1)

    async def test_safety_failure_uses_fail_closed_fallback(self):
        settings = runtime_settings()
        dispatcher = AgentDispatcher([ExplodingSafety()], settings)
        state = BlackboardState.create("普通内容")
        command = AgentCommand(
            batch_id="batch",
            agent=AgentName.SAFETY,
            action=AgentAction.ASSESS_RISK,
            state_revision=state.revision,
        )

        outcome = (await dispatcher.dispatch((command,), state))[0]

        self.assertTrue(outcome.success)
        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.update.data.risk_level, RiskLevel.MEDIUM)


if __name__ == "__main__":
    unittest.main()
