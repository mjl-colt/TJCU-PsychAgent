from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.agents.blackboard import AgentCommand, BlackboardState, FlowStage, RuntimeEvent


class RuntimeInvariantError(RuntimeError):
    """Raised when a mandatory Runtime invariant is violated."""


def validate_agent_batch(state: BlackboardState, commands: Sequence[AgentCommand]) -> None:
    """Reject malformed dispatches before any Agent or model is called."""

    if state.flow.current_stage in {FlowStage.COMPLETED, FlowStage.FAILED}:
        raise RuntimeInvariantError("终态 Blackboard 不能继续调度 Agent")
    if not commands:
        raise RuntimeInvariantError("禁止调度空 Agent 批次")
    if len({command.command_id for command in commands}) != len(commands):
        raise RuntimeInvariantError("Agent 批次包含重复 commandId")
    if len({command.batch_id for command in commands}) != 1:
        raise RuntimeInvariantError("一次调度只能包含同一 batchId 的命令")
    if any(command.state_revision != state.revision for command in commands):
        raise RuntimeInvariantError("Agent 命令必须绑定当前 Blackboard revision")


def validate_runtime_transition(previous: BlackboardState, current: BlackboardState) -> None:
    """Apply safety and consistency checks on every Runtime state transition."""

    if previous.request != current.request:
        raise RuntimeInvariantError("Runtime 状态迁移不能修改请求身份或输入")
    if current.revision < previous.revision:
        raise RuntimeInvariantError("Runtime revision 不能倒退")
    if current.flow.current_stage in {
        FlowStage.READY_FOR_GENERATION,
        FlowStage.GENERATING,
        FlowStage.COMPLETED,
    }:
        review = current.safety.prompt_review if current.safety else None
        response = current.response
        if not response or not review or not review.approved or review.prompt_version != response.prompt_version:
            raise RuntimeInvariantError("生成阶段必须绑定已批准的同版本 Prompt")


def annotate_runtime_events(
    state: BlackboardState,
    events: Iterable[RuntimeEvent],
) -> tuple[RuntimeEvent, ...]:
    """Add the state coordinates needed for operational event diagnosis."""

    annotated: list[RuntimeEvent] = []
    for event in events:
        metadata = dict(event.metadata)
        metadata.setdefault("runtimeStage", state.flow.current_stage.value)
        metadata.setdefault("runtimeRevision", state.revision)
        annotated.append(event.model_copy(update={"metadata": metadata}))
    return tuple(annotated)
