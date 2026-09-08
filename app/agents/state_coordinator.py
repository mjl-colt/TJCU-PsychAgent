from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.agents.blackboard import (
    AgentAction,
    AgentCommand,
    AgentExecutionOutcome,
    AgentName,
    AgentStatus,
    BlackboardState,
    ExecutionBatchState,
    FlowStage,
    RuntimeEvent,
    RuntimeEventType,
)
from app.core.enums import IntentType, RiskLevel


@dataclass(frozen=True)
class CoordinatorDecision:
    state: BlackboardState
    commands: tuple[AgentCommand, ...] = ()
    events: tuple[RuntimeEvent, ...] = ()


class BlackboardCoordinator:
    """Business-state coordinator driven only by runtime events."""

    name = AgentName.COORDINATOR.value

    def __init__(self, settings):
        self.max_prompt_revisions = max(0, int(getattr(settings, "agent_runtime_max_prompt_revisions", 2)))

    def handle(self, state: BlackboardState, event: RuntimeEvent) -> CoordinatorDecision:
        if event.type == RuntimeEventType.TURN_STARTED:
            if state.flow.current_stage != FlowStage.RECEIVED:
                return CoordinatorDecision(state)
            return self._start_batch(
                state,
                FlowStage.ANALYZING,
                (
                    (AgentName.UNDERSTANDING, AgentAction.UNDERSTAND),
                    (AgentName.SAFETY, AgentAction.ASSESS_RISK),
                ),
                "Understanding 与 Safety 并行分析",
            )
        if event.type in {RuntimeEventType.AGENT_COMPLETED, RuntimeEventType.AGENT_FAILED}:
            return self._handle_agent_outcome(state, event)
        return CoordinatorDecision(state)

    def fail_budget(self, state: BlackboardState, message: str) -> CoordinatorDecision:
        return self._terminal(state, FlowStage.FAILED, message, RuntimeEventType.BUDGET_EXHAUSTED)

    def _handle_agent_outcome(self, state: BlackboardState, event: RuntimeEvent) -> CoordinatorDecision:
        outcome = event.outcome
        batch = state.flow.active_batch
        if outcome is None or batch is None or outcome.command.batch_id != batch.batch_id:
            return CoordinatorDecision(state)
        command_id = outcome.command.command_id
        if command_id in batch.completed_command_ids:
            return CoordinatorDecision(state)

        completed_ids = (*batch.completed_command_ids, command_id)
        statuses = dict(state.flow.agent_status)
        statuses[outcome.command.agent.value] = (
            AgentStatus.DEGRADED if outcome.degraded else AgentStatus.COMPLETED if outcome.success else AgentStatus.FAILED
        )
        updated_batch = batch.model_copy(update={"completed_command_ids": completed_ids})
        flow = state.flow.model_copy(
            update={
                "agent_status": statuses,
                "active_batch": updated_batch,
            }
        )
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        progress_event = self._event(
            state,
            RuntimeEventType.STATE_UPDATED,
            f"{outcome.command.agent.value} {outcome.command.action.value} completed",
            {
                "stage": state.flow.current_stage.value,
                "durationMs": round(outcome.duration_ms, 3),
                "attempts": outcome.attempts,
                "degraded": outcome.degraded,
                "success": outcome.success,
            },
        )
        if set(completed_ids) != set(batch.expected):
            return CoordinatorDecision(state, events=(progress_event,))

        flow = state.flow.model_copy(update={"active_batch": None})
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        actions = set(batch.actions.values())
        transition = self._advance_after_batch(state, actions, statuses)
        return CoordinatorDecision(
            transition.state,
            transition.commands,
            (progress_event, *transition.events),
        )

    def _advance_after_batch(
        self,
        state: BlackboardState,
        actions: set[AgentAction],
        statuses: dict[str, AgentStatus],
    ) -> CoordinatorDecision:
        if actions == {AgentAction.UNDERSTAND, AgentAction.ASSESS_RISK}:
            return self._route_after_analysis(state)

        if actions == {AgentAction.GATHER_CONTEXT}:
            return self._start_batch(
                state,
                FlowStage.CONTEXT_READY,
                ((AgentName.RESPONSE, AgentAction.PREPARE_RESPONSE),),
                "上下文准备完成，组装候选 Prompt",
            )

        if actions in ({AgentAction.PREPARE_RESPONSE}, {AgentAction.REVISE_RESPONSE}):
            if state.response is None:
                return self._terminal(state, FlowStage.FAILED, "ResponseAgent 未产生候选 Prompt")
            return self._start_batch(
                state,
                FlowStage.PROMPT_REVIEW,
                ((AgentName.SAFETY, AgentAction.REVIEW_RESPONSE),),
                f"审查候选 Prompt v{state.response.prompt_version}",
            )

        if actions == {AgentAction.REVIEW_RESPONSE}:
            return self._after_review(state, statuses)

        if actions == {AgentAction.FINALIZE_RESPONSE}:
            if state.response is None or state.response.generation_status.value != "READY_FOR_GENERATION":
                return self._terminal(state, FlowStage.FAILED, "ResponseAgent 未完成最终 Prompt 确认")
            return self._terminal(
                state,
                FlowStage.READY_FOR_GENERATION,
                "候选 Prompt 已通过安全审查，可以开始流式生成",
                RuntimeEventType.TURN_READY_FOR_GENERATION,
            )

        failed = [name for name, status in statuses.items() if status == AgentStatus.FAILED]
        return self._terminal(state, FlowStage.FAILED, f"无法处理完成批次，failed={failed}")

    def _route_after_analysis(self, state: BlackboardState) -> CoordinatorDecision:
        intent = state.understanding.intent if state.understanding else IntentType.CONSULT
        risk = state.safety.risk_level if state.safety else RiskLevel.HIGH
        if risk == RiskLevel.HIGH or intent == IntentType.RISK:
            route = IntentType.RISK
            reason = "Safety 高风险结果或风险意图触发安全路由"
        elif intent == IntentType.CONSULT or risk == RiskLevel.MEDIUM:
            route = IntentType.CONSULT
            reason = "咨询意图或中等风险需要上下文支持"
        else:
            route = IntentType.CHAT
            reason = "普通对话且风险为 LOW，跳过完整 RAG"

        flow = state.flow.model_copy(
            update={
                "current_stage": FlowStage.ROUTED,
                "route": route,
            }
        )
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        route_event = self._event(
            state,
            RuntimeEventType.ROUTE_SELECTED,
            reason,
            {"route": route.value, "risk": risk.value, "intent": intent.value},
        )
        if route == IntentType.CHAT and risk == RiskLevel.LOW:
            decision = self._start_batch(
                state,
                FlowStage.CONTEXT_READY,
                ((AgentName.RESPONSE, AgentAction.PREPARE_RESPONSE),),
                "CHAT + LOW 直接组装回复 Prompt",
            )
        else:
            decision = self._start_batch(
                state,
                FlowStage.RETRIEVING,
                ((AgentName.CONTEXT, AgentAction.GATHER_CONTEXT),),
                "支持或风险路由需要加载记忆与知识上下文",
            )
        return CoordinatorDecision(decision.state, decision.commands, (route_event, *decision.events))

    def _after_review(self, state: BlackboardState, statuses: dict[str, AgentStatus]) -> CoordinatorDecision:
        review = state.safety.prompt_review if state.safety else None
        response = state.response
        review_failed = statuses.get(AgentName.SAFETY.value) == AgentStatus.FAILED
        version_matches = bool(review and response and review.prompt_version == response.prompt_version)
        if not review_failed and review and review.approved and version_matches:
            return self._start_batch(
                state,
                FlowStage.FINALIZING_PROMPT,
                ((AgentName.RESPONSE, AgentAction.FINALIZE_RESPONSE),),
                f"Safety 已批准 Prompt v{review.prompt_version}",
            )

        attempts = state.flow.review_attempts + 1
        if attempts > self.max_prompt_revisions:
            reason = "Safety Review 超过最大修订次数"
            if review and not version_matches:
                reason = "Safety Review 版本与最新 Prompt 不一致且无法继续修订"
            return self._terminal(state, FlowStage.FAILED, reason)
        flow = state.flow.model_copy(update={"review_attempts": attempts})
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        return self._start_batch(
            state,
            FlowStage.PROMPT_REVIEW,
            ((AgentName.RESPONSE, AgentAction.REVISE_RESPONSE),),
            f"Safety Review 未通过，生成第 {attempts} 次修订",
        )

    def _start_batch(
        self,
        state: BlackboardState,
        stage: FlowStage,
        specs: tuple[tuple[AgentName, AgentAction], ...],
        message: str,
    ) -> CoordinatorDecision:
        batch_id = uuid.uuid4().hex
        next_revision = state.revision + 1
        commands = tuple(
            AgentCommand(
                batch_id=batch_id,
                agent=agent,
                action=action,
                state_revision=next_revision,
            )
            for agent, action in specs
        )
        batch = ExecutionBatchState(
            batch_id=batch_id,
            expected={command.command_id: command.agent for command in commands},
            actions={command.command_id: command.action for command in commands},
        )
        statuses = dict(state.flow.agent_status)
        for command in commands:
            statuses[command.agent.value] = AgentStatus.RUNNING
        flow = state.flow.model_copy(
            update={
                "current_stage": stage,
                "agent_status": statuses,
                "active_batch": batch,
                "error": None,
            }
        )
        state = state.model_copy(update={"flow": flow, "revision": next_revision})
        event = self._event(
            state,
            RuntimeEventType.STATE_UPDATED,
            message,
            {
                "stage": stage.value,
                "nextAgents": [command.agent.value for command in commands],
                "batchId": batch_id,
            },
        )
        return CoordinatorDecision(state, commands, (event,))

    def _terminal(
        self,
        state: BlackboardState,
        stage: FlowStage,
        message: str,
        event_type: RuntimeEventType = RuntimeEventType.TURN_COMPLETED,
    ) -> CoordinatorDecision:
        flow = state.flow.model_copy(
            update={
                "current_stage": stage,
                "active_batch": None,
                "error": message if stage == FlowStage.FAILED else None,
            }
        )
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        terminal_event = self._event(state, event_type, message)
        return CoordinatorDecision(state, events=(terminal_event,))

    def generation_started(self, state: BlackboardState) -> CoordinatorDecision:
        """Move an approved prompt into the externally streamed generation phase."""

        response = state.response
        review = state.safety.prompt_review if state.safety else None
        ready = bool(
            state.flow.current_stage == FlowStage.READY_FOR_GENERATION
            and response
            and response.generation_status.value == "READY_FOR_GENERATION"
            and review
            and review.approved
            and review.prompt_version == response.prompt_version
        )
        if not ready:
            return self._terminal(state, FlowStage.FAILED, "最终生成门禁校验失败")
        flow = state.flow.model_copy(update={"current_stage": FlowStage.GENERATING, "error": None})
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        return CoordinatorDecision(
            state,
            events=(self._event(state, RuntimeEventType.GENERATION_STARTED, "SSE 最终回复开始生成"),),
        )

    def generation_completed(
        self,
        state: BlackboardState,
        final_response: str,
        *,
        guardrail_replaced: bool = False,
        guardrail_issues: tuple[str, ...] = (),
        cited_ids: tuple[str, ...] = (),
    ) -> CoordinatorDecision:
        """Persist the generated text and close the whole user turn."""

        if state.flow.current_stage != FlowStage.GENERATING or state.response is None:
            return self._terminal(state, FlowStage.FAILED, "最终生成完成事件与当前阶段不一致")
        response = state.response.model_copy(update={"final_response": final_response})
        flow = state.flow.model_copy(update={"current_stage": FlowStage.COMPLETED, "error": None})
        state = state.model_copy(
            update={"response": response, "flow": flow, "revision": state.revision + 1}
        )
        events = [
            self._event(
                state,
                RuntimeEventType.GENERATION_COMPLETED,
                "SSE 最终回复生成完成",
                {
                    "responseChars": len(final_response),
                    "guardrailReplaced": guardrail_replaced,
                    "guardrailIssues": list(guardrail_issues),
                    "citedEvidenceIds": list(cited_ids),
                },
            ),
            self._event(state, RuntimeEventType.TURN_COMPLETED, "用户请求完整处理完成"),
        ]
        return CoordinatorDecision(
            state,
            events=tuple(events),
        )

    def generation_failed(self, state: BlackboardState, message: str) -> CoordinatorDecision:
        """Return an interrupted stream to the retryable ready state."""

        if state.flow.current_stage != FlowStage.GENERATING:
            return CoordinatorDecision(state)
        flow = state.flow.model_copy(update={"current_stage": FlowStage.READY_FOR_GENERATION, "error": message})
        state = state.model_copy(update={"flow": flow, "revision": state.revision + 1})
        return CoordinatorDecision(
            state,
            events=(self._event(state, RuntimeEventType.GENERATION_FAILED, message),),
        )

    def _event(
        self,
        state: BlackboardState,
        event_type: RuntimeEventType,
        message: str,
        metadata: dict | None = None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            type=event_type,
            request_id=state.request.request_id,
            actor=self.name,
            message=message,
            metadata=metadata or {},
        )
