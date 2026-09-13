"""Explicit workflow definition. No event queue or completion-event counting."""
from __future__ import annotations

import uuid

from app.agents.blackboard import (
    AgentAction, AgentCommand, AgentName, AgentStatus, BlackboardState,
    FlowStage, ResponseStatus, RuntimeEventType, WorkflowExecutionState, WorkflowTask,
)
from app.agents.runtime_guard import RuntimeInvariantError
from app.agents.state_coordinator import BlackboardCoordinator, CoordinatorDecision
from app.core.enums import IntentType, RiskLevel


# Nodes define their tasks here; advance() defines all outgoing edges below.
STEP_TASKS = {
    FlowStage.ANALYZING: (
        (AgentName.UNDERSTANDING, AgentAction.UNDERSTAND),
        (AgentName.SAFETY, AgentAction.ASSESS_RISK),
    ),
    FlowStage.RETRIEVING: ((AgentName.CONTEXT, AgentAction.GATHER_CONTEXT),),
    FlowStage.PREPARING_RESPONSE: ((AgentName.RESPONSE, AgentAction.PREPARE_RESPONSE),),
    FlowStage.PROMPT_REVIEW: ((AgentName.SAFETY, AgentAction.REVIEW_RESPONSE),),
    FlowStage.REVISING_RESPONSE: ((AgentName.RESPONSE, AgentAction.REVISE_RESPONSE),),
}


class WorkflowCoordinator(BlackboardCoordinator):
    """Reuse generation lifecycle gates, but drive Agent nodes by explicit edges.

    handle(state, event) is deliberately unavailable on the v2 control path.
    Events returned by decisions are audit records only.
    """

    def handle(self, state, event):
        raise RuntimeInvariantError("workflow-v2 不消费 Event；使用 start/advance")

    def start(self, state: BlackboardState) -> CoordinatorDecision:
        if state.flow.current_stage != FlowStage.RECEIVED or state.execution is not None:
            raise RuntimeInvariantError("只能从 RECEIVED 开始新工作流")
        return self.schedule(state, FlowStage.ANALYZING)

    def schedule(self, state: BlackboardState, stage: FlowStage) -> CoordinatorDecision:
        batch_id = uuid.uuid4().hex
        revision = state.revision + 1
        commands = tuple(
            AgentCommand(batch_id=batch_id, agent=agent, action=action, state_revision=revision)
            for agent, action in STEP_TASKS[stage]
        )
        statuses = dict(state.flow.agent_status)
        for command in commands:
            statuses[command.agent.value] = AgentStatus.RUNNING
        flow = state.flow.model_copy(update={
            "current_stage": stage, "active_batch": None, "agent_status": statuses, "error": None,
        })
        updated = state.model_copy(update={
            "flow": flow, "revision": revision,
            "execution": WorkflowExecutionState(tasks={
                command.command_id: WorkflowTask(command=command) for command in commands
            }),
        })
        return CoordinatorDecision(updated, commands, (self._event(
            updated, RuntimeEventType.STATE_UPDATED, f"进入工作流步骤 {stage.value}",
            {"step": stage.value, "commandIds": [item.command_id for item in commands]},
        ),))

    def advance(self, state: BlackboardState) -> CoordinatorDecision:
        """Called only after all task receipts have been saved and merged."""
        stage = state.flow.current_stage
        execution = state.execution
        if execution is None or not execution.tasks or any(task.outcome is None for task in execution.tasks.values()):
            raise RuntimeInvariantError("步骤任务尚未全部完成，禁止转换")
        failed = [task.command.agent.value for task in execution.tasks.values() if not task.outcome.success]
        state = state.model_copy(update={"execution": None})
        if failed and stage != FlowStage.PROMPT_REVIEW:
            return self._terminal(state, FlowStage.FAILED, f"步骤 {stage.value} 无可用结果：{', '.join(failed)}")
        if stage == FlowStage.ANALYZING:
            if state.understanding is None or state.safety is None:
                return self._terminal(state, FlowStage.FAILED, "分析结果缺失，禁止按低风险继续")
            intent, risk = state.understanding.intent, state.safety.risk_level
            route = (
                IntentType.RISK if risk == RiskLevel.HIGH or intent == IntentType.RISK
                else IntentType.CONSULT if intent == IntentType.CONSULT or risk == RiskLevel.MEDIUM
                else IntentType.CHAT
            )
            state = state.model_copy(update={"flow": state.flow.model_copy(update={"route": route})})
            next_stage = FlowStage.PREPARING_RESPONSE if route == IntentType.CHAT else FlowStage.RETRIEVING
            decision = self.schedule(state, next_stage)
            route_event = self._event(decision.state, RuntimeEventType.ROUTE_SELECTED,
                                     f"分析完成，路由为 {route.value}",
                                     {"route": route.value, "intent": intent.value, "risk": risk.value})
            return CoordinatorDecision(decision.state, decision.commands, (route_event, *decision.events))
        if stage == FlowStage.RETRIEVING:
            if state.context is None:
                return self._terminal(state, FlowStage.FAILED, "Context 结果缺失")
            return self.schedule(state, FlowStage.PREPARING_RESPONSE)
        if stage in {FlowStage.PREPARING_RESPONSE, FlowStage.REVISING_RESPONSE}:
            if state.response is None:
                return self._terminal(state, FlowStage.FAILED, "候选 Prompt 缺失")
            return self.schedule(state, FlowStage.PROMPT_REVIEW)
        if stage == FlowStage.PROMPT_REVIEW:
            review = state.safety.prompt_review if state.safety else None
            response = state.response
            if not failed and review and response and review.approved and review.prompt_version == response.prompt_version:
                # Finalize is a deterministic gate, not another Agent round trip.
                state = state.model_copy(update={"response": response.model_copy(update={
                    "generation_status": ResponseStatus.READY_FOR_GENERATION,
                })})
                return self._terminal(state, FlowStage.READY_FOR_GENERATION,
                                      "同版本 Prompt 审核通过，允许最终生成",
                                      RuntimeEventType.TURN_READY_FOR_GENERATION)
            attempts = state.flow.review_attempts + 1
            if attempts > self.max_prompt_revisions:
                return self._terminal(state, FlowStage.FAILED, "Prompt 审核失败且修订次数超限")
            state = state.model_copy(update={"flow": state.flow.model_copy(update={"review_attempts": attempts})})
            return self.schedule(state, FlowStage.REVISING_RESPONSE)
        raise RuntimeInvariantError(f"未知工作流步骤：{stage}")
