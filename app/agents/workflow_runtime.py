"""Checkpoint-driven executor. Events are audit output, never scheduling input."""
from __future__ import annotations

from contextlib import aclosing

from pydantic import ValidationError

from app.agents.blackboard import (
    AgentAction, AgentExecutionOutcome, AgentName, AgentStatus, BlackboardResultApplier,
    BlackboardState, BlackboardWriteError, FlowStage, RuntimeEvent, RuntimeEventType,
    RuntimeExecution,
)
from app.agents.dispatcher import AgentDispatcher
from app.agents.runtime_guard import (
    RuntimeInvariantError, annotate_runtime_events, validate_agent_batch, validate_runtime_transition,
)
from app.agents.runtime_store import NullRuntimeStore, RuntimeStore
from app.agents.workflow import STEP_TASKS, WorkflowCoordinator


class WorkflowRuntime:
    def __init__(self, coordinator: WorkflowCoordinator, dispatcher: AgentDispatcher, settings,
                 store: RuntimeStore | None = None):
        self.coordinator = coordinator
        self.dispatcher = dispatcher
        self.store = store or NullRuntimeStore()
        self.applier = BlackboardResultApplier()
        self.max_steps = max(1, int(getattr(settings, "agent_workflow_max_steps", 32)))

    async def run(self, initial_state: BlackboardState, *, resume: bool = False) -> RuntimeExecution:
        state = initial_state
        if state.workflow_version != "workflow-v2":
            raise RuntimeInvariantError("旧 checkpoint 必须由 event-v1 执行器恢复")
        # History is for trace display only. It is never inspected to schedule work.
        journal = list(self.store.load_events(state.request.request_id)) if resume else []
        terminal = {FlowStage.READY_FOR_GENERATION, FlowStage.FINALIZING_RESPONSE, FlowStage.COMPLETED, FlowStage.FAILED}
        if state.flow.current_stage in terminal:
            validate_runtime_transition(state, state)
            return RuntimeExecution(state=state, events=tuple(journal))
        if state.flow.current_stage == FlowStage.RECEIVED:
            decision = self.coordinator.start(state)
            event = self._event(decision.state, RuntimeEventType.TURN_STARTED, "显式工作流开始", {
                "promptInjectionSignals": list(state.request.prompt_injection_signals),
                "workflowVersion": state.workflow_version,
            })
            self._commit(state, decision.state, (event, *decision.events), journal)
            state = decision.state
        elif resume:
            self._validate_step(state)
            tasks = state.execution.tasks
            event = self._event(state, RuntimeEventType.TURN_RECOVERY_STARTED, "从任务收据恢复当前步骤", {
                "reusedCommandIds": [key for key, task in tasks.items() if task.outcome is not None],
                "rerunCommandIds": [key for key, task in tasks.items() if task.outcome is None],
            })
            self._commit(state, state, (event,), journal)

        for _ in range(self.max_steps):
            snapshot = self._validate_step(state)
            tasks = dict(state.execution.tasks)
            pending = tuple(task.command for task in tasks.values() if task.outcome is None)
            if pending:
                events = []
                for command in pending:
                    tasks[command.command_id] = tasks[command.command_id].model_copy(update={"started": True})
                    events.append(self._task_event(state, RuntimeEventType.AGENT_STARTED, command))
                    if command.action == AgentAction.GATHER_CONTEXT:
                        events.append(self._task_event(state, RuntimeEventType.CONTEXT_COMPACTION_STARTED, command,
                                                       metadata={"compactionId": command.command_id}))
                started = state.model_copy(update={
                    "execution": state.execution.model_copy(update={"tasks": tasks}), "revision": state.revision + 1,
                })
                self._commit(state, started, events, journal)
                state = started
                # aclosing guarantees cancellation cleanup if a commit or validation fails.
                async with aclosing(self.dispatcher.iter_outcomes(pending, snapshot)) as outcomes:
                    async for raw in outcomes:
                        task = state.execution.tasks.get(raw.command.command_id)
                        if task is None or task.command != raw.command or task.outcome is not None:
                            raise RuntimeInvariantError("执行结果不属于当前未完成任务")
                        outcome = self._checked_outcome(snapshot, raw)
                        tasks = dict(state.execution.tasks)
                        tasks[raw.command.command_id] = task.model_copy(update={"outcome": outcome})
                        updated = state.model_copy(update={
                            "execution": state.execution.model_copy(update={"tasks": tasks}),
                            "revision": state.revision + 1,
                        })
                        events = [self._task_event(updated,
                            RuntimeEventType.AGENT_COMPLETED if outcome.success else RuntimeEventType.AGENT_FAILED,
                            outcome.command, outcome=outcome)]
                        if outcome.command.action == AgentAction.GATHER_CONTEXT:
                            compaction = outcome.update.data.compaction if outcome.success else None
                            kind = (RuntimeEventType.CONTEXT_COMPACTION_COMPLETED if compaction
                                    else RuntimeEventType.CONTEXT_COMPACTION_FAILED)
                            events.append(self._task_event(updated, kind, outcome.command, metadata={
                                "compactionId": outcome.command.command_id,
                                "compacted": bool(compaction and compaction.compacted),
                                "sourceMessageCount": compaction.source_message_count if compaction else 0,
                                "retainedMessageCount": compaction.retained_message_count if compaction else 0,
                                "summaryChars": compaction.summary_chars if compaction else 0,
                                "summaryHash": compaction.summary_hash if compaction else "",
                                "error": outcome.error or ("" if compaction else "context update missing"),
                            }))
                        # The outcome becomes reusable only after this transaction commits.
                        self._commit(state, updated, events, journal)
                        state = updated

            if any(task.outcome is None for task in state.execution.tasks.values()):
                raise RuntimeInvariantError("Dispatcher 未返回全部任务结果")
            # Input revision is stable across receipt writes. Merge once against
            # that input; checkpoint revision advances independently and never rewinds.
            completed = [self._checked_outcome(snapshot, task.outcome) for task in state.execution.tasks.values()]
            merged = self.applier.apply_batch(snapshot, completed)
            statuses = dict(state.flow.agent_status)
            for outcome in completed:
                statuses[outcome.command.agent.value] = (
                    AgentStatus.DEGRADED if outcome.degraded else
                    AgentStatus.COMPLETED if outcome.success else AgentStatus.FAILED
                )
            merged = merged.model_copy(update={
                "execution": state.execution.model_copy(update={"tasks": {
                    outcome.command.command_id: state.execution.tasks[outcome.command.command_id].model_copy(
                        update={"outcome": outcome}) for outcome in completed
                }}),
                "flow": state.flow.model_copy(update={"agent_status": statuses}),
                "revision": state.revision + 1,
            })
            decision = self.coordinator.advance(merged)
            # Business merge and next-step scheduling share one checkpoint transaction.
            self._commit(state, decision.state, decision.events, journal)
            state = decision.state
            if state.flow.current_stage in terminal:
                return RuntimeExecution(state=state, events=tuple(journal))

        failed = state.model_copy(update={"execution": None})
        decision = self.coordinator.fail_budget(failed, "工作流步骤数超过上限")
        self._commit(state, decision.state, decision.events, journal)
        return RuntimeExecution(state=decision.state, events=tuple(journal))

    def _validate_step(self, state: BlackboardState) -> BlackboardState:
        execution = state.execution
        specs = STEP_TASKS.get(state.flow.current_stage)
        if not specs or execution is None or not execution.tasks or state.flow.active_batch is not None:
            raise RuntimeInvariantError("checkpoint 缺少有效工作流步骤/任务，或混入旧批次状态")
        commands = tuple(task.command for task in execution.tasks.values())
        if len(commands) != len(specs) or {(item.agent, item.action) for item in commands} != set(specs):
            raise RuntimeInvariantError("checkpoint 任务与工作流步骤定义不一致")
        if any(key != task.command.command_id or (task.outcome is not None and task.outcome.command != task.command)
               for key, task in execution.tasks.items()):
            raise RuntimeInvariantError("checkpoint 任务身份不一致")
        input_revision = commands[0].state_revision
        if input_revision > state.revision:
            raise RuntimeInvariantError("步骤输入版本不能晚于 checkpoint")
        # Strip task receipts from Agent views. Partial results are not business state yet.
        snapshot = state.model_copy(update={"execution": None, "revision": input_revision})
        validate_agent_batch(snapshot, commands)
        return snapshot

    def _checked_outcome(self, snapshot, outcome):
        if not outcome.success:
            return outcome.model_copy(update={"update": None, "degraded": False})
        try:
            if outcome.update is None:
                raise BlackboardWriteError("成功结果缺少 StateUpdate")
            applied = self.applier.apply_batch(snapshot, [outcome])
            typed_data = getattr(applied, outcome.update.section.value)
            if outcome.command.action in {AgentAction.PREPARE_RESPONSE, AgentAction.REVISE_RESPONSE}:
                if snapshot.response and typed_data.prompt_version <= snapshot.response.prompt_version:
                    raise BlackboardWriteError("修订 Prompt 必须增加版本号")
            return outcome.model_copy(update={"update": outcome.update.model_copy(update={"data": typed_data})})
        except (BlackboardWriteError, ValidationError, ValueError) as exc:
            return outcome.model_copy(update={"success": False, "degraded": False, "update": None,
                                              "error": f"invalid state update: {exc}"})

    def _commit(self, previous, current, events, journal):
        validate_runtime_transition(previous, current)
        annotated = annotate_runtime_events(current, events)
        self.store.save_many(current, annotated)
        journal.extend(annotated)

    @staticmethod
    def _event(state, kind, message, metadata=None):
        return RuntimeEvent(type=kind, request_id=state.request.request_id, actor=AgentName.RUNTIME.value,
                            message=message, metadata=metadata or {})

    @staticmethod
    def _task_event(state, kind, command, *, outcome=None, metadata=None):
        details = {"action": command.action.value, "inputRevision": command.state_revision}
        if outcome is not None:
            details.update(success=outcome.success, degraded=outcome.degraded,
                           attempts=outcome.attempts, durationMs=round(outcome.duration_ms, 3))
        details.update(metadata or {})
        return RuntimeEvent(type=kind, request_id=state.request.request_id, actor=command.agent.value,
                            batch_id=command.batch_id, command_id=command.command_id,
                            message=f"{command.action.value}: {kind.value}", outcome=outcome, metadata=details)
