import asyncio
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.blackboard import (
    AgentAction, AgentName, AgentStateUpdate, BlackboardSection, BlackboardState,
    ContextState, FlowStage, PromptReviewState, ResponseState, RuntimeEvent,
    RuntimeEventType, SafetyState, UnderstandingState,
)
from app.agents.dispatcher import AgentDispatcher
from app.agents.generation_lifecycle import GenerationLifecycle
from app.agents.runtime_guard import RuntimeInvariantError
from app.agents.runtime_store import NullRuntimeStore, RuntimePersistenceError, SqlAlchemyRuntimeStore
from app.agents.runtime_lease import RuntimeLeaseManager
from app.agents.workflow import WorkflowCoordinator
from app.agents.workflow_runtime import WorkflowRuntime
from app.core.database import Base
from app.core.enums import IntentType, RiskLevel
from app.models.entities import AgentRuntimeCheckpoint, AgentRuntimeEventRecord
from app.schemas.dtos import AiMessage
from test_blackboard_event_runtime import runtime_settings


class Crash(BaseException):
    pass


class MemoryStore(NullRuntimeStore):
    """Round-trip JSON just like durable storage; optionally crash after commit."""
    def __init__(self):
        self.state = None
        self.events = []
        self.after_save = None

    def save_many(self, state, events):
        self.state = BlackboardState.model_validate_json(state.model_dump_json())
        self.events.extend(events)
        if self.after_save:
            self.after_save(self.state)

    def load_events(self, request_id):
        return list(self.events)


class Agents:
    def __init__(self, *, intent=IntentType.CONSULT, risk=RiskLevel.LOW, stale_review=False,
                 reject_review=False, block_safety=False, invalid_understanding=False):
        self.intent, self.risk = intent, risk
        self.stale_review, self.reject_review = stale_review, reject_review
        self.block_safety, self.invalid_understanding = block_safety, invalid_understanding
        self.calls = []
        self.safety_cancelled = False
        self.review_count = 0

    def all(self):
        owner = self
        class Agent:
            def __init__(self, name):
                self.name = name

            async def run(self, command, state):
                owner.calls.append(command)
                if command.action == AgentAction.UNDERSTAND:
                    await asyncio.sleep(0.005)
                    if owner.invalid_understanding:
                        return AgentStateUpdate(section=BlackboardSection.SAFETY,
                                                data=SafetyState(risk_level="LOW", assessment_method="TEST"))
                    return AgentStateUpdate(section=BlackboardSection.UNDERSTANDING,
                                            data=UnderstandingState(intent=owner.intent, topic="test"))
                if command.action == AgentAction.ASSESS_RISK:
                    try:
                        await asyncio.sleep(100 if owner.block_safety else 0.015)
                    except asyncio.CancelledError:
                        owner.safety_cancelled = True
                        raise
                    return AgentStateUpdate(section=BlackboardSection.SAFETY,
                                            data=SafetyState(risk_level=owner.risk, assessment_method="TEST"))
                if command.action == AgentAction.GATHER_CONTEXT:
                    return AgentStateUpdate(section=BlackboardSection.CONTEXT, data=ContextState())
                if command.action == AgentAction.REVIEW_RESPONSE:
                    owner.review_count += 1
                    version = state.response.prompt_version
                    if owner.stale_review and owner.review_count == 1:
                        version += 1
                    return AgentStateUpdate(section=BlackboardSection.SAFETY, data=state.safety.model_copy(update={
                        "prompt_review": PromptReviewState(prompt_version=version, approved=not owner.reject_review),
                    }))
                if command.action in {AgentAction.PREPARE_RESPONSE, AgentAction.REVISE_RESPONSE}:
                    return AgentStateUpdate(section=BlackboardSection.RESPONSE, data=ResponseState(
                        prompt_version=state.response.prompt_version + 1 if state.response else 1,
                        messages=(AiMessage(role="user", content=state.request.model_input),),
                        mode="support", intent=state.flow.route, risk_level=state.safety.risk_level,
                    ))
                raise AssertionError(f"unexpected task: {command.action}")
        return [Agent(name) for name in (AgentName.UNDERSTANDING, AgentName.SAFETY, AgentName.CONTEXT, AgentName.RESPONSE)]


def initial():
    return BlackboardState.create("考试压力", request_id="workflow-test", workflow_version="workflow-v2")


def runtime(agents, store=None, **options):
    settings = runtime_settings(**options)
    return WorkflowRuntime(WorkflowCoordinator(settings), AgentDispatcher(agents.all(), settings), settings, store)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_and_no_event_queue_or_finalize_task(self):
        cases = [(intent, risk, not (intent == IntentType.CHAT and risk == RiskLevel.LOW))
                 for intent in IntentType for risk in RiskLevel]
        for intent, risk, context_expected in cases:
            with self.subTest(intent=intent, risk=risk):
                agents = Agents(intent=intent, risk=risk)
                with patch("app.agents.event_bus.InMemoryEventBus", side_effect=AssertionError("no bus")):
                    result = await runtime(agents).run(initial())
                self.assertEqual(result.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
                self.assertEqual(result.state.context is not None, context_expected)
                expected_route = (IntentType.RISK if intent == IntentType.RISK or risk == RiskLevel.HIGH
                                  else IntentType.CONSULT if context_expected else IntentType.CHAT)
                self.assertEqual(result.state.flow.route, expected_route)
                self.assertIsNone(result.state.execution)
                self.assertIsNone(result.state.flow.active_batch)
                self.assertNotIn(AgentAction.FINALIZE_RESPONSE, [item.action for item in agents.calls])
                self.assertNotIn(RuntimeEventType.AGENT_BATCH_REQUESTED, [item.type for item in result.events])

    async def test_first_completed_task_is_durable_before_sibling_finishes(self):
        agents, store = Agents(block_safety=True), MemoryStore()
        def crash_after_understanding(state):
            if state.execution and any(task.outcome for task in state.execution.tasks.values()):
                raise Crash()
        store.after_save = crash_after_understanding
        with self.assertRaises(Crash):
            await runtime(agents, store).run(initial())
        self.assertTrue(agents.safety_cancelled)
        self.assertIsNone(store.state.understanding)  # receipt saved, business merge not yet done
        task_ids = {task.command.agent: key for key, task in store.state.execution.tasks.items()}
        self.assertIsNotNone(store.state.execution.tasks[task_ids[AgentName.UNDERSTANDING]].outcome)
        self.assertIsNone(store.state.execution.tasks[task_ids[AgentName.SAFETY]].outcome)
        store.after_save = None
        # No audit event replay is needed for v2 recovery.
        store.events.clear()
        recovered_agents = Agents()
        result = await runtime(recovered_agents, store).run(store.state, resume=True)
        self.assertEqual(result.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        self.assertNotIn(AgentAction.UNDERSTAND, [item.action for item in recovered_agents.calls])
        safety = next(item for item in recovered_agents.calls if item.action == AgentAction.ASSESS_RISK)
        self.assertEqual(safety.command_id, task_ids[AgentName.SAFETY])
        original = next(item for item in agents.calls if item.action == AgentAction.ASSESS_RISK)
        self.assertEqual(safety.state_revision, original.state_revision)

    async def test_all_saved_outcomes_resume_without_reexecuting_analysis(self):
        store = MemoryStore()
        def crash_after_all(state):
            if state.flow.current_stage == FlowStage.ANALYZING and state.execution:
                if all(task.outcome for task in state.execution.tasks.values()):
                    raise Crash()
        store.after_save = crash_after_all
        with self.assertRaises(Crash):
            await runtime(Agents(), store).run(initial())
        store.after_save = None
        agents = Agents()
        result = await runtime(agents, store).run(store.state, resume=True)
        self.assertEqual(result.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        self.assertFalse({AgentAction.UNDERSTAND, AgentAction.ASSESS_RISK} & {item.action for item in agents.calls})

    async def test_commit_failure_before_dispatch_prevents_model_calls(self):
        agents, store = Agents(), MemoryStore()
        store.save_many = lambda *_: (_ for _ in ()).throw(RuntimePersistenceError("database down"))
        with self.assertRaises(RuntimePersistenceError):
            await runtime(agents, store).run(initial())
        self.assertEqual(agents.calls, [])

    async def test_result_commit_failure_cancels_sibling_and_never_routes(self):
        agents, store = Agents(block_safety=True), MemoryStore()
        original_save = store.save_many
        def fail_receipt(state, events):
            if state.execution and any(task.outcome for task in state.execution.tasks.values()):
                raise RuntimePersistenceError("commit failed")
            original_save(state, events)
        store.save_many = fail_receipt
        with self.assertRaises(RuntimePersistenceError):
            await runtime(agents, store).run(initial())
        self.assertTrue(agents.safety_cancelled)
        self.assertEqual(store.state.flow.current_stage, FlowStage.ANALYZING)
        self.assertTrue(all(task.outcome is None for task in store.state.execution.tasks.values()))

    async def test_stale_review_revises_and_reviews_again(self):
        agents = Agents(stale_review=True)
        result = await runtime(agents).run(initial())
        self.assertEqual(result.state.response.prompt_version, 2)
        self.assertEqual(result.state.safety.prompt_review.prompt_version, 2)
        self.assertEqual(result.state.flow.review_attempts, 1)

    async def test_review_budget_is_terminal(self):
        agents = Agents(reject_review=True)
        result = await runtime(agents, agent_runtime_max_prompt_revisions=1).run(initial())
        self.assertEqual(result.state.flow.current_stage, FlowStage.FAILED)
        self.assertEqual(agents.review_count, 2)

    async def test_invalid_section_does_not_become_low_risk_success(self):
        result = await runtime(Agents(invalid_understanding=True)).run(initial())
        self.assertEqual(result.state.flow.current_stage, FlowStage.FAILED)
        self.assertIsNone(result.state.understanding)

    async def test_finished_checkpoint_does_not_run_agents(self):
        ready = (await runtime(Agents()).run(initial())).state
        agents = Agents()
        await runtime(agents).run(ready, resume=True)
        self.assertEqual(agents.calls, [])

    async def test_old_version_and_malformed_tasks_are_rejected(self):
        with self.assertRaises(RuntimeInvariantError):
            await runtime(Agents()).run(BlackboardState.create("old"), resume=True)
        state = WorkflowCoordinator(runtime_settings()).start(initial()).state
        tasks = dict(state.execution.tasks)
        tasks.pop(next(iter(tasks)))
        invalid = state.model_copy(update={"execution": state.execution.model_copy(update={"tasks": tasks})})
        with self.assertRaises(RuntimeInvariantError):
            await runtime(Agents()).run(invalid, resume=True)

    async def test_generation_checkpoint_reuses_checked_output(self):
        store = MemoryStore()
        ready = (await runtime(Agents(), store).run(initial())).state
        lifecycle = GenerationLifecycle(WorkflowCoordinator(runtime_settings()), store)
        generating = lifecycle.started(ready)
        checked = lifecycle.output_ready(generating, "checked answer")
        self.assertEqual(checked.flow.current_stage, FlowStage.FINALIZING_RESPONSE)
        agents = Agents()
        result = await runtime(agents, store).run(store.state, resume=True)
        self.assertEqual(result.state.response.final_response, "checked answer")
        self.assertEqual(agents.calls, [])
        completed = lifecycle.completed(result.state, "checked answer")
        self.assertEqual(completed.flow.current_stage, FlowStage.COMPLETED)

    async def test_interrupted_generation_is_retryable(self):
        store = MemoryStore()
        ready = (await runtime(Agents(), store).run(initial())).state
        lifecycle = GenerationLifecycle(WorkflowCoordinator(runtime_settings()), store)
        failed = lifecycle.failed(lifecycle.started(ready), "network interrupted")
        self.assertEqual(failed.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        self.assertIsNone(failed.response.final_response)

    async def test_sql_checkpoint_is_authoritative_without_event_projections(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            store = SqlAlchemyRuntimeStore(db)
            result = await runtime(Agents(), store).run(initial())
            self.assertEqual(store.load(initial().request.request_id), result.state)
            self.assertEqual(db.query(AgentRuntimeCheckpoint).count(), 1)
            for row in db.query(AgentRuntimeEventRecord).all():
                self.assertIsNone(json.loads(row.payload_json)["state_projection"])
            db.query(AgentRuntimeEventRecord).delete()
            db.commit()
            self.assertEqual(store.load(initial().request.request_id), result.state)

    async def test_sql_checkpoint_read_failure_is_not_a_new_request(self):
        class BrokenDB:
            def query(self, *_):
                raise RuntimeError("database down")
            def rollback(self):
                pass
        for required in (True, False):
            with self.subTest(required=required), self.assertRaises(RuntimePersistenceError):
                SqlAlchemyRuntimeStore(BrokenDB(), persistence_required=required).load("existing-request")

    async def test_lost_owner_cannot_commit_checkpoint(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            manager = RuntimeLeaseManager(db, runtime_settings())
            first = manager.acquire(initial().request.request_id, "first")
            store = SqlAlchemyRuntimeStore(db, lease=first)
            event = RuntimeEvent(type=RuntimeEventType.TURN_STARTED,
                                 request_id=initial().request.request_id, actor="Runtime")
            store.save(initial(), event)
            manager.release(first)
            manager.acquire(initial().request.request_id, "second")
            with self.assertRaises(RuntimePersistenceError):
                store.save(initial().model_copy(update={"revision": 1}), event)
            self.assertEqual(db.query(AgentRuntimeCheckpoint).one().state_version, 0)

    async def test_stale_checkpoint_write_rolls_back_audit(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            store = SqlAlchemyRuntimeStore(db)
            event = RuntimeEvent(type=RuntimeEventType.STATE_UPDATED,
                                 request_id=initial().request.request_id, actor="Runtime")
            store.save(initial().model_copy(update={"revision": 3}), event)
            with self.assertRaises(RuntimePersistenceError):
                store.save(initial(), event.model_copy(update={"event_id": "stale"}))
            self.assertEqual(db.query(AgentRuntimeEventRecord).count(), 1)
            self.assertEqual(store.load(initial().request.request_id).revision, 3)

    async def test_service_selects_legacy_executor_for_unversioned_checkpoint(self):
        from app.agents.event_driven_runtime import AgentRuntimeService
        from app.agents.blackboard import RuntimeExecution
        from app.agents.event_projection import RuntimeEventProjector
        state = BlackboardState.create("old", user_id=1, session_id="s", request_id="legacy")
        old_json = state.model_dump_json(exclude={"workflow_version", "execution"})
        old_event = RuntimeEvent(type=RuntimeEventType.TURN_STARTED, request_id="legacy", actor="Runtime",
                                 state_projection=json.loads(old_json), metadata={
                                     "stateProjectionHash": hashlib.sha256(old_json.encode("utf-8")).hexdigest(),
                                 })
        state = RuntimeEventProjector().rebuild([old_event]).state
        self.assertEqual(state.workflow_version, "event-v1")
        service = AgentRuntimeService.__new__(AgentRuntimeService)
        service.db, service.settings = Mock(), runtime_settings()
        service.memory, service.model_registry = Mock(), Mock()
        service._to_result = lambda execution, _: execution
        store = Mock()
        store.load.return_value = state
        runner = Mock()
        runner.run = AsyncMock(return_value=RuntimeExecution(state=state, events=()))
        with patch("app.agents.event_driven_runtime.SqlAlchemyRuntimeStore", return_value=store), \
             patch("app.agents.event_driven_runtime.BlackboardEventRuntime", return_value=runner) as legacy, \
             patch("app.agents.event_driven_runtime.WorkflowRuntime") as new:
            await service.run_async(SimpleNamespace(id=1), SimpleNamespace(public_id="s"), "old", "legacy")
        legacy.assert_called_once()
        new.assert_not_called()


class ChatWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def make_service(self, store):
        from app.services.chat import ChatService
        service = ChatService.__new__(ChatService)
        service.db = Mock()
        service.settings = SimpleNamespace(agent_runtime_persistence_enabled=True,
                                           agent_runtime_persistence_required=True,
                                           agent_runtime_lease_enabled=False)
        service.ai = Mock()
        service.agent_harness = Mock()
        service.agent_harness.dispatch_tools = AsyncMock()
        service.output_guardrail = Mock()
        service.output_guardrail.validate.return_value = SimpleNamespace(
            content="checked answer", allowed=True, cited_ids=(), issues=(),
        )
        state = (await runtime(Agents(), store).run(initial())).state
        outcome = SimpleNamespace(runtime_state=state, lease=None, request_id=state.request.request_id,
                                  session_public_id="s", session_id=1, user_id=1, replayed_response=None,
                                  response_messages=list(state.response.messages), intent=IntentType.CONSULT,
                                  tool_plan=SimpleNamespace(requires_tools=False))
        return service, outcome

    async def test_business_failure_resumes_saved_output_without_llm(self):
        store = MemoryStore()
        service, outcome = await self.make_service(store)
        async def generate(_):
            yield "model answer"
        service.ai.stream = Mock(side_effect=generate)
        service.agent_harness.save_assistant_message.side_effect = RuntimeError("business database down")
        with patch("app.services.chat.SqlAlchemyRuntimeStore", return_value=store):
            with self.assertRaises(RuntimeError):
                _ = [item async for item in service._stream_outcome_with_lease(outcome)]
            self.assertEqual(store.state.flow.current_stage, FlowStage.FINALIZING_RESPONSE)
            outcome.runtime_state = store.state
            outcome.replayed_response = store.state.response.final_response
            service.agent_harness.save_assistant_message.side_effect = None
            events = [item async for item in service._stream_outcome_with_lease(outcome)]
        self.assertEqual(service.ai.stream.call_count, 1)
        self.assertEqual(store.state.flow.current_stage, FlowStage.COMPLETED)
        self.assertTrue(any("event: done" in item for item in events))

    async def test_failed_review_never_calls_final_llm(self):
        store = MemoryStore()
        service, outcome = await self.make_service(store)
        outcome.runtime_state = outcome.runtime_state.model_copy(update={
            "flow": outcome.runtime_state.flow.model_copy(update={"current_stage": FlowStage.FAILED}),
        })
        with patch("app.services.chat.SqlAlchemyRuntimeStore", return_value=store):
            events = [item async for item in service._stream_outcome_with_lease(outcome)]
        service.ai.stream.assert_not_called()
        self.assertTrue(any("event: error" in item for item in events))
        self.assertFalse(any("event: done" in item for item in events))

    async def test_empty_generation_records_retryable_failure(self):
        store = MemoryStore()
        service, outcome = await self.make_service(store)
        async def empty(_):
            if False:
                yield ""
        service.ai.stream = Mock(side_effect=empty)
        with patch("app.services.chat.SqlAlchemyRuntimeStore", return_value=store):
            with self.assertRaises(RuntimeError):
                _ = [item async for item in service._stream_outcome_with_lease(outcome)]
        self.assertEqual(store.state.flow.current_stage, FlowStage.READY_FOR_GENERATION)
        service.agent_harness.save_assistant_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
