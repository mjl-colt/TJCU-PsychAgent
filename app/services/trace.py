
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.result import AgentRunResult
from app.models.entities import AgentRunTrace, ChatSession, UserAccount


class AgentTraceService:
    def __init__(self, db: Session):
        self.db = db

    def save_run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        memory_brief: str,
        agent_run: AgentRunResult,
        report_id: int | None,
        *,
        commit: bool = True,
    ) -> AgentRunTrace:
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=report_id,
            intent=agent_run.intent.value,
            risk_level=agent_run.risk_level.value,
            original_input=original_input,
            sanitized_input=sanitized_input,
            memory_brief=memory_brief,
            agent_steps_json=_json(_agent_steps_with_collaboration(agent_run)),
            retrieved_knowledge_json=_json(agent_run.retrieved_knowledge),
            response_messages_json=_json(agent_run.response_messages),
            assessment_json=_json(agent_run.assessment or {}),
        )
        self.db.add(trace)
        if commit:
            self.db.commit()
            self.db.refresh(trace)
        else:
            self.db.flush()
        return trace


def _json(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, default=str)


def _agent_steps_with_collaboration(agent_run: AgentRunResult) -> list[Any]:
    entries: list[Any] = []
    state = agent_run.runtime_state
    if state is not None:
        entries.append(
            {
                "kind": "governance_snapshot",
                "requestId": agent_run.request_id,
                "stateRevision": state.revision,
                "inputTrust": "UNTRUSTED",
                "promptInjectionSignals": list(state.request.prompt_injection_signals),
                "understandingPrompt": state.understanding.prompt_template_version if state.understanding else None,
                "safetyPrompt": state.safety.prompt_template_version if state.safety else None,
                "contextPrompt": state.context.prompt_template_version if state.context else None,
                "skillVersions": list(state.context.skill_versions) if state.context else [],
                "responsePrompt": state.response.prompt_template_version if state.response else None,
                "responsePromptHash": state.response.prompt_hash if state.response else None,
            }
        )
    entries.extend(
        {
            "kind": "agent_event",
            "type": getattr(event.type, "value", event.type),
            "actor": event.actor,
            "batchId": event.batch_id,
            "commandId": event.command_id,
            "message": event.message,
            "metadata": event.metadata,
        }
        for event in agent_run.collaboration_events
    )
    return entries


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value
