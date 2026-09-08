from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult

if TYPE_CHECKING:
    from app.agents.blackboard import BlackboardState, RuntimeEvent


@dataclass
class AgentStep:
    step: int
    agent: str
    action: str
    observation: str


@dataclass
class AgentRunResult:
    intent: IntentType
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    retrieved_knowledge: list[SearchResult]
    response_messages: list[AiMessage]
    memory_brief: str
    collaboration_events: list["RuntimeEvent"] = field(default_factory=list)
    runtime_state: "BlackboardState | None" = None
    request_id: str = ""
    replayed_response: str | None = None

    @property
    def requires_report(self) -> bool:
        return self.intent != IntentType.CHAT

    @property
    def steps(self) -> list[AgentStep]:
        """Compatibility view derived from the canonical runtime event list."""

        return [
            AgentStep(
                index,
                event.actor,
                event.type.value,
                event.message or str(event.metadata)[:240],
            )
            for index, event in enumerate(self.collaboration_events, start=1)
        ]
