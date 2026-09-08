from __future__ import annotations

import asyncio
import uuid

from pydantic import ValidationError

from app.agents.blackboard import (
    AgentCommand,
    AgentExecutionOutcome,
    AgentName,
    BlackboardResultApplier,
    BlackboardState,
    BlackboardWriteError,
    FlowStage,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeExecution,
)
from app.agents.dispatcher import AgentDispatcher
from app.agents.event_projection import attach_state_projection, interrupted_compaction_ids
from app.agents.event_bus import InMemoryEventBus
from app.agents.runtime_guard import annotate_runtime_events, validate_agent_batch, validate_runtime_transition
from app.agents.runtime_store import NullRuntimeStore, RuntimeStore
from app.agents.state_coordinator import BlackboardCoordinator, CoordinatorDecision


class BlackboardEventRuntime:
    """Request-scoped event-driven Blackboard executor.

    Coordinator decisions publish command batches.  The dispatcher executes a
    batch, the result applier atomically merges validated state patches, and
    completion events wake the coordinator.  No component polls Blackboard.
    """

    def __init__(
        self,
        coordinator: BlackboardCoordinator,
        dispatcher: AgentDispatcher,
        settings,
        store: RuntimeStore | None = None,
    ):
        self.coordinator = coordinator
        self.dispatcher = dispatcher
        self.max_events = max(8, int(getattr(settings, "agent_runtime_max_events", 64)))
        self.idle_timeout_seconds = max(
            1.0,
            float(getattr(settings, "agent_runtime_idle_timeout_seconds", 30.0)),
        )
        self.store = store or NullRuntimeStore()
        self.applier = BlackboardResultApplier()

    async def run(self, initial_state: BlackboardState, *, resume: bool = False) -> RuntimeExecution:
        state = initial_state
        bus = InMemoryEventBus()
        journal: list[RuntimeEvent] = self.store.load_events(state.request.request_id) if resume else []
        # Event ids from previous processes stay replayable.  Only duplicates
        # observed during this in-memory run are suppressed; otherwise a crash
        # after persisting AGENT_BATCH_REQUESTED but before dispatch would make
        # that command impossible to resume.
        processed_event_ids: set[str] = set()
        if resume:
            if state.flow.current_stage in {
                FlowStage.READY_FOR_GENERATION,
                FlowStage.COMPLETED,
                FlowStage.FAILED,
            }:
                return RuntimeExecution(
                    state=state,
                    events=tuple(journal),
                )
            recovery_events = self._recovery_events(state, journal)
            if not recovery_events:
                decision = self.coordinator.fail_budget(state, "checkpoint 缺少可恢复的活动批次")
                state = decision.state
                self._record_many(journal, state, decision.events)
                return RuntimeExecution(
                    state=state,
                    events=tuple(journal),
                )
            for recovery_event in recovery_events:
                await bus.publish(recovery_event)
        else:
            await bus.publish(
                RuntimeEvent(
                    type=RuntimeEventType.TURN_STARTED,
                    request_id=state.request.request_id,
                    actor=AgentName.RUNTIME.value,
                    message="用户请求已写入 Blackboard",
                    metadata={
                        "inputTrust": "UNTRUSTED",
                        "promptInjectionSignals": list(state.request.prompt_injection_signals),
                    },
                )
            )

        for _ in range(self.max_events):
            try:
                event = await asyncio.wait_for(bus.consume(), timeout=self.idle_timeout_seconds)
            except asyncio.TimeoutError:
                decision = self.coordinator.fail_budget(state, "Runtime 等待事件超时")
                validate_runtime_transition(state, decision.state)
                state = decision.state
                self._record_many(journal, state, decision.events)
                return RuntimeExecution(
                    state=state,
                    events=tuple(journal),
                )
            try:
                if event.event_id in processed_event_ids:
                    continue
                processed_event_ids.add(event.event_id)

                if event.type == RuntimeEventType.AGENT_BATCH_REQUESTED:
                    # Persist the scheduling decision and start markers before
                    # awaiting network-bound agents. This keeps the event log
                    # chronologically useful when an agent hangs or times out.
                    self._record_many(journal, state, (event, *self._started_events(state, event)))
                    state, batch_events = await self._execute_batch(state, event)
                    # This is the durable outcome boundary. Persist the merged
                    # state and every outcome before asking the Coordinator to
                    # consume them.  Recovery can replay these exact outcomes
                    # instead of invoking an already-finished model again.
                    self._record_many(journal, state, batch_events)
                    for batch_event in batch_events:
                        await bus.publish(batch_event)
                else:
                    previous = state
                    decision = self.coordinator.handle(state, event)
                    validate_runtime_transition(previous, decision.state)
                    state = decision.state
                    self._record_many(journal, state, (event, *decision.events))
                    if decision.commands:
                        await bus.publish(
                            RuntimeEvent(
                                type=RuntimeEventType.AGENT_BATCH_REQUESTED,
                                request_id=state.request.request_id,
                                actor=AgentName.COORDINATOR.value,
                                target=AgentName.RUNTIME.value,
                                batch_id=decision.commands[0].batch_id,
                                message="执行 Coordinator 发布的 Agent 批次",
                                metadata={
                                    "agents": [command.agent.value for command in decision.commands],
                                    "actions": [command.action.value for command in decision.commands],
                                },
                                commands=decision.commands,
                            )
                        )
                if state.flow.current_stage in {
                    FlowStage.READY_FOR_GENERATION,
                    FlowStage.COMPLETED,
                    FlowStage.FAILED,
                }:
                    return RuntimeExecution(
                        state=state,
                        events=tuple(journal),
                    )
            finally:
                bus.task_done()

        decision = self.coordinator.fail_budget(state, "事件数量超过 Runtime 安全上限")
        validate_runtime_transition(state, decision.state)
        state = decision.state
        self._record_many(journal, state, decision.events)
        return RuntimeExecution(
            state=state,
            events=tuple(journal),
        )

    def _recovery_events(
        self,
        state: BlackboardState,
        historical_events: list[RuntimeEvent],
    ) -> tuple[RuntimeEvent, ...]:
        active = state.flow.active_batch
        if active is None:
            if state.flow.current_stage.value == "RECEIVED":
                return (
                    RuntimeEvent(
                        event_id=self._stable_event_id(state.request.request_id, "recovery", str(state.revision)),
                        type=RuntimeEventType.TURN_STARTED,
                        request_id=state.request.request_id,
                        actor=AgentName.RUNTIME.value,
                        message="从 RECEIVED checkpoint 重新启动用户请求",
                        metadata={"recovered": True, "stateRevision": state.revision},
                    ),
                )
            return ()

        pending_ids = {
            command_id
            for command_id in active.expected
            if command_id not in active.completed_command_ids
        }
        replayable = {
            event.command_id: event
            for event in historical_events
            if event.batch_id == active.batch_id
            and event.command_id in pending_ids
            and event.outcome is not None
            and event.type in {RuntimeEventType.AGENT_COMPLETED, RuntimeEventType.AGENT_FAILED}
        }
        commands = tuple(
            AgentCommand(
                command_id=command_id,
                batch_id=active.batch_id,
                agent=agent,
                action=active.actions[command_id],
                state_revision=state.revision,
            )
            for command_id, agent in active.expected.items()
            if command_id in pending_ids and command_id not in replayable
        )
        replay_events = tuple(replayable[command_id] for command_id in active.expected if command_id in replayable)
        if not commands and not replay_events:
            return ()
        recovery_marker = RuntimeEvent(
            event_id=self._stable_event_id(
                state.request.request_id,
                "recovery",
                active.batch_id,
                str(state.revision),
            ),
            type=RuntimeEventType.TURN_RECOVERY_STARTED,
            request_id=state.request.request_id,
            actor=AgentName.RUNTIME.value,
            batch_id=active.batch_id,
            message="从 checkpoint 续跑未完成 Agent 命令",
            metadata={
                "recovered": True,
                "replayedCommandIds": [event.command_id for event in replay_events],
                "rerunCommandIds": [command.command_id for command in commands],
                "interruptedCompactionIds": list(interrupted_compaction_ids(historical_events)),
                "stateRevision": state.revision,
            },
        )
        if not commands:
            return recovery_marker, *replay_events
        batch_request = RuntimeEvent(
            event_id=self._stable_event_id(
                state.request.request_id,
                "recovery-batch",
                active.batch_id,
                str(state.revision),
            ),
            type=RuntimeEventType.AGENT_BATCH_REQUESTED,
            request_id=state.request.request_id,
            actor=AgentName.RUNTIME.value,
            target=AgentName.RUNTIME.value,
            batch_id=active.batch_id,
            message="恢复并执行 checkpoint 中未完成的 Agent 批次",
            metadata={"recovered": True},
            commands=commands,
        )
        return recovery_marker, *replay_events, batch_request

    async def _execute_batch(
        self,
        state: BlackboardState,
        event: RuntimeEvent,
    ) -> tuple[BlackboardState, list[RuntimeEvent]]:
        active = state.flow.active_batch
        if active is None or active.batch_id != event.batch_id:
            return state, []
        commands = tuple(
            command.model_copy(update={"state_revision": state.revision})
            for command in event.commands
        )
        validate_agent_batch(state, commands)
        raw_outcomes = await self.dispatcher.dispatch(commands, state)
        outcomes = self._reject_invalid_updates(state, raw_outcomes)
        previous = state
        state = self.applier.apply_batch(state, outcomes)
        completion_events = [self._outcome_event(state, outcome) for outcome in outcomes]
        completion_events.extend(self._compaction_completion_events(state, outcomes))
        validate_runtime_transition(previous, state)
        return state, completion_events

    def _started_events(self, state: BlackboardState, event: RuntimeEvent) -> list[RuntimeEvent]:
        events = [
            RuntimeEvent(
                event_id=self._stable_event_id(
                    state.request.request_id,
                    "started",
                    command.command_id,
                    str(command.state_revision),
                ),
                type=RuntimeEventType.AGENT_STARTED,
                request_id=state.request.request_id,
                actor=command.agent.value,
                target=command.agent.value,
                batch_id=command.batch_id,
                command_id=command.command_id,
                message=f"{command.action.value} started",
                metadata={"action": command.action.value, "stateRevision": command.state_revision},
            )
            for command in event.commands
        ]
        for command in event.commands:
            if command.action.value == "GATHER_CONTEXT":
                events.append(
                    RuntimeEvent(
                        event_id=self._stable_event_id(
                            state.request.request_id,
                            "compaction-started",
                            command.command_id,
                        ),
                        type=RuntimeEventType.CONTEXT_COMPACTION_STARTED,
                        request_id=state.request.request_id,
                        actor=command.agent.value,
                        target=command.agent.value,
                        batch_id=command.batch_id,
                        command_id=command.command_id,
                        message="ContextAgent 开始评估并压缩历史上下文",
                        metadata={"compactionId": command.command_id},
                    )
                )
        return events

    def _compaction_completion_events(
        self,
        state: BlackboardState,
        outcomes: list[AgentExecutionOutcome],
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        for outcome in outcomes:
            if outcome.command.action.value != "GATHER_CONTEXT":
                continue
            compaction = state.context.compaction if outcome.success and state.context else None
            failed = not outcome.success or compaction is None
            event_type = (
                RuntimeEventType.CONTEXT_COMPACTION_FAILED
                if failed
                else RuntimeEventType.CONTEXT_COMPACTION_COMPLETED
            )
            metadata = {
                "compactionId": outcome.command.command_id,
                "compacted": bool(compaction and compaction.compacted),
                "sourceMessageCount": compaction.source_message_count if compaction else 0,
                "retainedMessageCount": compaction.retained_message_count if compaction else 0,
                "summaryChars": compaction.summary_chars if compaction else 0,
                "summaryHash": compaction.summary_hash if compaction else "",
                "error": outcome.error or ("" if compaction else "context update missing"),
            }
            events.append(
                RuntimeEvent(
                    event_id=self._stable_event_id(
                        state.request.request_id,
                        event_type.value,
                        outcome.command.command_id,
                    ),
                    type=event_type,
                    request_id=state.request.request_id,
                    actor=outcome.command.agent.value,
                    target=AgentName.COORDINATOR.value,
                    batch_id=outcome.command.batch_id,
                    command_id=outcome.command.command_id,
                    message=(
                        "上下文压缩事务完成"
                        if not failed
                        else "上下文压缩事务失败，将按 Agent 降级或重试策略处理"
                    ),
                    metadata=metadata,
                )
            )
        return events

    def _reject_invalid_updates(
        self,
        state: BlackboardState,
        outcomes: list[AgentExecutionOutcome],
    ) -> list[AgentExecutionOutcome]:
        checked: list[AgentExecutionOutcome] = []
        for outcome in outcomes:
            if outcome.update is None:
                checked.append(outcome)
                continue
            try:
                self.applier.apply_batch(state, [outcome])
            except (BlackboardWriteError, ValidationError, ValueError, TypeError) as exc:
                checked.append(
                    AgentExecutionOutcome(
                        command=outcome.command,
                        success=False,
                        attempts=outcome.attempts,
                        duration_ms=outcome.duration_ms,
                        error=f"invalid state update: {type(exc).__name__}: {exc}",
                    )
                )
            else:
                checked.append(outcome)
        return checked

    def _outcome_event(self, state: BlackboardState, outcome: AgentExecutionOutcome) -> RuntimeEvent:
        command = outcome.command
        event_type = RuntimeEventType.AGENT_COMPLETED if outcome.success else RuntimeEventType.AGENT_FAILED
        return RuntimeEvent(
            event_id=self._stable_event_id(
                state.request.request_id,
                event_type.value,
                command.command_id,
            ),
            type=event_type,
            request_id=state.request.request_id,
            actor=command.agent.value,
            target=AgentName.COORDINATOR.value,
            batch_id=command.batch_id,
            command_id=command.command_id,
            message=(
                f"{command.action.value} completed"
                if outcome.success
                else f"{command.action.value} failed: {outcome.error}"
            ),
            metadata={
                "action": command.action.value,
                "durationMs": round(outcome.duration_ms, 3),
                "attempts": outcome.attempts,
                "degraded": outcome.degraded,
                "success": outcome.success,
            },
            outcome=outcome,
        )

    def _record_many(
        self,
        journal: list[RuntimeEvent],
        state: BlackboardState,
        events: tuple[RuntimeEvent, ...] | list[RuntimeEvent],
    ) -> None:
        if not events:
            return
        annotated = annotate_runtime_events(state, events)
        recorded = attach_state_projection(state, annotated)
        known_ids = {event.event_id for event in journal}
        journal.extend(event for event in recorded if event.event_id not in known_ids)
        self.store.save_many(state, recorded)

    @staticmethod
    def _stable_event_id(*parts: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, "mindbridge:" + ":".join(parts)).hex
