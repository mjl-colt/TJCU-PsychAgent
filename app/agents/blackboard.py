from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.schemas.dtos import AiMessage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentName(str, Enum):
    UNDERSTANDING = "UnderstandingAgent"
    SAFETY = "SafetyAgent"
    CONTEXT = "ContextAgent"
    RESPONSE = "ResponseAgent"
    COORDINATOR = "CoordinatorAgent"
    RUNTIME = "Runtime"


class BlackboardSection(str, Enum):
    UNDERSTANDING = "understanding"
    SAFETY = "safety"
    CONTEXT = "context"
    RESPONSE = "response"


class FlowStage(str, Enum):
    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    ROUTED = "ROUTED"
    RETRIEVING = "RETRIEVING"
    CONTEXT_READY = "CONTEXT_READY"
    PROMPT_REVIEW = "PROMPT_REVIEW"
    FINALIZING_PROMPT = "FINALIZING_PROMPT"
    READY_FOR_GENERATION = "READY_FOR_GENERATION"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class AgentAction(str, Enum):
    UNDERSTAND = "UNDERSTAND"
    ASSESS_RISK = "ASSESS_RISK"
    GATHER_CONTEXT = "GATHER_CONTEXT"
    PREPARE_RESPONSE = "PREPARE_RESPONSE"
    REVISE_RESPONSE = "REVISE_RESPONSE"
    REVIEW_RESPONSE = "REVIEW_RESPONSE"
    FINALIZE_RESPONSE = "FINALIZE_RESPONSE"


class ResponseStatus(str, Enum):
    WAITING_FOR_SAFETY_REVIEW = "WAITING_FOR_SAFETY_REVIEW"
    READY_FOR_GENERATION = "READY_FOR_GENERATION"


class RuntimeEventType(str, Enum):
    TURN_STARTED = "TURN_STARTED"
    TURN_RECOVERY_STARTED = "TURN_RECOVERY_STARTED"
    AGENT_BATCH_REQUESTED = "AGENT_BATCH_REQUESTED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    STATE_UPDATED = "STATE_UPDATED"
    ROUTE_SELECTED = "ROUTE_SELECTED"
    CONTEXT_COMPACTION_STARTED = "CONTEXT_COMPACTION_STARTED"
    CONTEXT_COMPACTION_COMPLETED = "CONTEXT_COMPACTION_COMPLETED"
    CONTEXT_COMPACTION_FAILED = "CONTEXT_COMPACTION_FAILED"
    TURN_READY_FOR_GENERATION = "TURN_READY_FOR_GENERATION"
    GENERATION_STARTED = "GENERATION_STARTED"
    GENERATION_COMPLETED = "GENERATION_COMPLETED"
    GENERATION_FAILED = "GENERATION_FAILED"
    TURN_COMPLETED = "TURN_COMPLETED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RequestState(FrozenModel):
    request_id: str
    user_id: int | None = None
    session_id: str = ""
    model_input: str
    prompt_injection_signals: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_user_input(cls, value):
        """Keep only the sanitized model input when reading older checkpoints."""

        legacy_fields = {"user_input", "input_trust", "created_at"}
        if isinstance(value, dict) and (legacy_fields & value.keys()):
            value = dict(value)
            if "user_input" in value:
                value.setdefault("model_input", value["user_input"])
            for field in legacy_fields:
                value.pop(field, None)
        return value


class UnderstandingState(FrozenModel):
    intent: IntentType
    topic: str
    reason: str = ""
    prompt_template_version: str = "legacy-understanding-unversioned"

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_unused_analysis(cls, value):
        """Old checkpoints carried analysis fields that no runtime decision consumed."""

        legacy_fields = {"entities", "emotion", "context_need", "intent_confidence"}
        if isinstance(value, dict) and (legacy_fields & value.keys()):
            value = dict(value)
            for field in legacy_fields:
                value.pop(field, None)
        return value


class PromptReviewState(FrozenModel):
    prompt_version: int = Field(ge=1)
    approved: bool
    issues: tuple[str, ...] = ()
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_event_metadata(cls, value):
        """Review timing and fallback status already live on the outcome event."""

        if isinstance(value, dict) and ({"reviewed_at", "degraded"} & value.keys()):
            value = dict(value)
            value.pop("reviewed_at", None)
            value.pop("degraded", None)
        return value


class ContextCompactionState(FrozenModel):
    compaction_id: str
    source_message_count: int = Field(default=0, ge=0)
    retained_message_count: int = Field(default=0, ge=0)
    summary_chars: int = Field(default=0, ge=0)
    compacted: bool = False
    summary_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_timestamps(cls, value):
        """Compaction event timestamps are authoritative; state copies were redundant."""

        if isinstance(value, dict) and ({"status", "error", "started_at", "completed_at"} & value.keys()):
            value = dict(value)
            value.pop("status", None)
            value.pop("error", None)
            value.pop("started_at", None)
            value.pop("completed_at", None)
        return value


class SafetyState(FrozenModel):
    risk_level: RiskLevel
    risk_type: str | None = None
    risk_signals: tuple[str, ...] = ()
    assessment_method: str
    response_constraints: tuple[str, ...] = ()
    emotion: EmotionLabel = EmotionLabel.NORMAL
    emotion_score: float = 0.0
    summary: str = ""
    prompt_review: PromptReviewState | None = None
    prompt_template_version: str = "legacy-safety-unversioned"

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_derived_flags(cls, value):
        """Risk level is canonical; the old HIGH-risk aliases were duplicates."""

        legacy_fields = {"force_risk_route", "requires_followup", "risk_confidence"}
        if isinstance(value, dict) and (legacy_fields & value.keys()):
            value = dict(value)
            for field in legacy_fields:
                value.pop(field, None)
        return value


class KnowledgeEvidence(FrozenModel):
    chunk_id: int | None = None
    source: str
    content: str
    score: float = 0.0


class ContextState(FrozenModel):
    memory_brief: str = "无相关历史记忆。"
    model_history: tuple[AiMessage, ...] = ()
    rewritten_query: str = ""
    retrieved_knowledge: tuple[KnowledgeEvidence, ...] = ()
    skill_context: str = ""
    prompt_template_version: str = "legacy-context-unversioned"
    skill_versions: tuple[str, ...] = ()
    compaction: ContextCompactionState | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_duplicate_memory(cls, value):
        """Drop memory copies that were never read after context assembly."""

        legacy_fields = {
            "short_term_memory",
            "long_term_memory",
            "retrieval_sufficient",
            "final_context",
        }
        if isinstance(value, dict) and (legacy_fields & value.keys()):
            value = dict(value)
            for field in legacy_fields:
                value.pop(field, None)
        return value


class ResponsePolicyContract(FrozenModel):
    """Machine-readable obligations for one response prompt and final answer."""

    contract_version: str = "response-policy-v1"
    requires_non_diagnostic: bool = False
    requires_immediate_safety_check: bool = False
    requires_human_support: bool = False
    requires_emergency_escalation: bool = False
    requires_rag_citations: bool = False
    allowed_citation_ids: tuple[str, ...] = ()


class ResponseState(FrozenModel):
    prompt_version: int = Field(ge=1)
    messages: tuple[AiMessage, ...]
    mode: str
    intent: IntentType
    risk_level: RiskLevel
    generation_status: ResponseStatus = ResponseStatus.WAITING_FOR_SAFETY_REVIEW
    final_response: str | None = None
    safe_fallback: bool = False
    policy_contract: ResponsePolicyContract = Field(default_factory=ResponsePolicyContract)
    prompt_template_version: str = "legacy-response-unversioned"
    prompt_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_candidate_prompt(cls, value):
        """Messages are the canonical Prompt; the joined text was a duplicate."""

        if isinstance(value, dict) and ("candidate_prompt" in value or "policy_contract" not in value):
            value = dict(value)
            value.pop("candidate_prompt", None)
            if "policy_contract" not in value:
                high_risk = value.get("risk_level") == RiskLevel.HIGH.value
                support = value.get("mode") != "normal_chat"
                value["policy_contract"] = ResponsePolicyContract(
                    requires_non_diagnostic=support,
                    requires_immediate_safety_check=high_risk,
                    requires_human_support=high_risk,
                    requires_emergency_escalation=high_risk,
                ).model_dump()
        return value


class ExecutionBatchState(FrozenModel):
    batch_id: str
    expected: dict[str, AgentName]
    actions: dict[str, AgentAction]
    completed_command_ids: tuple[str, ...] = ()


def _default_agent_status() -> dict[str, AgentStatus]:
    return {
        AgentName.UNDERSTANDING.value: AgentStatus.PENDING,
        AgentName.SAFETY.value: AgentStatus.PENDING,
        AgentName.CONTEXT.value: AgentStatus.PENDING,
        AgentName.RESPONSE.value: AgentStatus.PENDING,
    }


class FlowState(FrozenModel):
    current_stage: FlowStage = FlowStage.RECEIVED
    agent_status: dict[str, AgentStatus] = Field(default_factory=_default_agent_status)
    route: IntentType | None = None
    active_batch: ExecutionBatchState | None = None
    review_attempts: int = 0
    error: str | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_inbox(cls, value):
        """Read checkpoints written before redundant flow projections were removed."""

        legacy_fields = {"inbox", "route_reason", "next_agents", "completed"}
        if isinstance(value, dict) and (legacy_fields & value.keys()):
            value = dict(value)
            for field in legacy_fields:
                value.pop(field, None)
        return value


class BlackboardState(FrozenModel):
    request: RequestState
    understanding: UnderstandingState | None = None
    safety: SafetyState | None = None
    context: ContextState | None = None
    response: ResponseState | None = None
    flow: FlowState = Field(default_factory=FlowState)
    revision: int = Field(default=0, ge=0)

    @classmethod
    def create(
        cls,
        model_input: str,
        user_id: int | None = None,
        session_id: str = "",
        request_id: str | None = None,
        prompt_injection_signals: tuple[str, ...] = (),
    ) -> "BlackboardState":
        return cls(
            request=RequestState(
                request_id=request_id or uuid.uuid4().hex,
                user_id=user_id,
                session_id=session_id,
                model_input=model_input,
                prompt_injection_signals=prompt_injection_signals,
            )
        )


class AgentCommand(FrozenModel):
    command_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    batch_id: str
    agent: AgentName
    action: AgentAction
    state_revision: int = Field(ge=0)


class AgentStateUpdate(FrozenModel):
    section: BlackboardSection
    data: Any


class AgentExecutionOutcome(FrozenModel):
    command: AgentCommand
    update: AgentStateUpdate | None = None
    success: bool
    degraded: bool = False
    attempts: int = Field(default=1, ge=1)
    duration_ms: float = Field(default=0.0, ge=0.0)
    error: str | None = None


class RuntimeEvent(FrozenModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: RuntimeEventType
    request_id: str
    actor: str
    target: str = ""
    batch_id: str = ""
    command_id: str = ""
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    commands: tuple[AgentCommand, ...] = ()
    outcome: AgentExecutionOutcome | None = None
    state_projection: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    schema_version: int = 2


class BlackboardWriteError(RuntimeError):
    pass


class BlackboardResultApplier:
    """Validate and atomically apply one parallel batch of agent updates."""

    permissions = {
        AgentName.UNDERSTANDING: BlackboardSection.UNDERSTANDING,
        AgentName.SAFETY: BlackboardSection.SAFETY,
        AgentName.CONTEXT: BlackboardSection.CONTEXT,
        AgentName.RESPONSE: BlackboardSection.RESPONSE,
    }
    schemas = {
        BlackboardSection.UNDERSTANDING: UnderstandingState,
        BlackboardSection.SAFETY: SafetyState,
        BlackboardSection.CONTEXT: ContextState,
        BlackboardSection.RESPONSE: ResponseState,
    }

    def apply_batch(
        self,
        state: BlackboardState,
        outcomes: list[AgentExecutionOutcome],
    ) -> BlackboardState:
        updates: dict[str, BaseModel] = {}
        base_revision = state.revision
        for outcome in outcomes:
            if outcome.update is None:
                continue
            command = outcome.command
            update = outcome.update
            if command.state_revision != base_revision:
                raise BlackboardWriteError(
                    f"stale agent result: command revision={command.state_revision}, current={base_revision}"
                )
            allowed = self.permissions.get(command.agent)
            if update.section != allowed:
                raise BlackboardWriteError(f"{command.agent.value} cannot write {update.section.value}")
            if update.section.value in updates:
                raise BlackboardWriteError(f"parallel batch contains duplicate writes to {update.section.value}")
            schema = self.schemas[update.section]
            updates[update.section.value] = schema.model_validate(update.data)
        if not updates:
            return state
        return state.model_copy(update={**updates, "revision": base_revision + 1})


class RuntimeExecution(FrozenModel):
    state: BlackboardState
    events: tuple[RuntimeEvent, ...]
