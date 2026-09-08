from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.agents.result import AgentStep
from app.agents.runtime_lease import RuntimeLease, RuntimeLeaseBusyError, RuntimeLeaseManager
from app.core.config import Settings
from app.core.enums import IntentType, MessageRole
from app.agents.blackboard import BlackboardState
from app.models.entities import (
    AgentRuntimeCheckpoint,
    AgentTurnMaterialization,
    ChatMessage,
    ChatSession,
    PsychologicalReport,
    UserAccount,
    now,
)
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from app.services.mcp_client import MindBridgeMcpToolClient
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer
from app.services.tool_queue import ToolQueueService
from app.services.trace import AgentTraceService


@dataclass
class AgentToolPlan:
    report_id: int | None
    risk_level: str | None

    @property
    def requires_tools(self) -> bool:
        return self.report_id is not None


@dataclass
class AgentHarnessOutcome:
    session: ChatSession
    user_id: int
    session_id: int
    session_public_id: str
    original_input: str
    model_input: str
    intent: IntentType
    risk_level: str | None
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    agent_steps: list[AgentStep]
    retrieved_knowledge: list[SearchResult]
    report_id: int | None
    tool_plan: AgentToolPlan
    trace_id: int | None
    request_id: str
    runtime_state: BlackboardState
    replayed_response: str | None = None
    lease: RuntimeLease | None = None


class MindBridgeAgentHarness:
    """Runtime harness for one 心理ai agent turn.

    The harness owns business orchestration around the agent runtime. HTTP/SSE
    code can stay thin while this class manages input preparation, persistence,
    risk report creation, tool planning, and trace data.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.memory = RedisShortTermMemoryStore(settings)

    def run(self, user: UserAccount, request: ChatRequest) -> AgentHarnessOutcome:
        original_input = request.message.strip()
        if not original_input:
            raise ValueError("消息不能为空")
        if len(original_input) > self.settings.agent_max_input_chars:
            raise ValueError(f"消息长度不能超过 {self.settings.agent_max_input_chars} 个字符")
        model_input = self.privacy.sanitize(original_input)
        request_id = request.requestId or uuid.uuid4().hex
        lease = self._acquire_lease(request_id)
        try:
            session = self._resolve_session(user, request.sessionId, original_input, request_id)
            agent_run = EventDrivenAgentRuntimeService(self.db, self.settings).run(
                user,
                session,
                model_input,
                request_id,
            )
            return self._finish_run(user, session, original_input, model_input, agent_run, None)
        finally:
            self.release_lease(lease)

    async def run_async(self, user: UserAccount, request: ChatRequest) -> AgentHarnessOutcome:
        """Async request entry point used by the SSE route.

        Agent model calls can now overlap without blocking token streaming for
        unrelated requests.  Existing synchronous persistence is intentionally
        kept behind the harness boundary for compatibility with the current
        SQLAlchemy setup.
        """

        original_input = request.message.strip()
        if not original_input:
            raise ValueError("消息不能为空")
        if len(original_input) > self.settings.agent_max_input_chars:
            raise ValueError(f"消息长度不能超过 {self.settings.agent_max_input_chars} 个字符")
        model_input = self.privacy.sanitize(original_input)
        request_id = request.requestId or uuid.uuid4().hex
        lease = self._acquire_lease(request_id)
        try:
            session = self._resolve_session(user, request.sessionId, original_input, request_id)
            agent_run = await EventDrivenAgentRuntimeService(self.db, self.settings).run_async(
                user,
                session,
                model_input,
                request_id,
            )
            return self._finish_run(user, session, original_input, model_input, agent_run, lease)
        except BaseException:
            self.release_lease(lease)
            raise

    def _finish_run(self, user, session, original_input, model_input, agent_run, lease) -> AgentHarnessOutcome:
        # SSE starts after the FastAPI request dependency may have closed its
        # SQLAlchemy session.  Persist immutable identifiers in the outcome so
        # the streaming phase never has to dereference a detached ORM object.
        user_id = int(user.id)
        session_id = int(session.id)
        session_public_id = str(session.public_id)
        materialized = (
            self.db.query(AgentTurnMaterialization)
            .filter(AgentTurnMaterialization.request_id == agent_run.request_id)
            .first()
        )
        if materialized is not None:
            return self._materialized_outcome(
                user,
                session,
                original_input,
                model_input,
                agent_run,
                materialized,
                lease,
            )
        if agent_run.replayed_response is not None:
            return self._materialized_outcome(
                user,
                session,
                original_input,
                model_input,
                agent_run,
                None,
                lease,
            )

        user_message = ChatMessage(
            user_id=user.id,
            session_id=session.id,
            role=MessageRole.USER.value,
            content=original_input,
        )
        self.db.add(user_message)
        session.touch()
        self.db.add(session)
        self.db.flush()
        report = self._create_report(user, session, original_input, agent_run, commit=False)
        risk_level = report.risk_level if report is not None else None
        trace = AgentTraceService(self.db).save_run(
            user=user,
            session=session,
            original_input=original_input,
            sanitized_input=model_input,
            memory_brief=agent_run.memory_brief,
            agent_run=agent_run,
            report_id=report.id if report is not None else None,
            commit=False,
        )
        materialized = AgentTurnMaterialization(
            request_id=agent_run.request_id,
            user_id=user.id,
            session_id=session.id,
            user_message_id=user_message.id,
            report_id=report.id if report is not None else None,
            trace_id=trace.id,
        )
        self.db.add(materialized)
        self.db.commit()
        self.memory.append(session.public_id, MessageRole.USER.value, original_input)
        tool_plan = AgentToolPlan(report_id=report.id if report is not None else None, risk_level=risk_level)
        return AgentHarnessOutcome(
            session=session,
            user_id=user_id,
            session_id=session_id,
            session_public_id=session_public_id,
            original_input=original_input,
            model_input=model_input,
            intent=agent_run.intent,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            agent_steps=agent_run.steps,
            retrieved_knowledge=agent_run.retrieved_knowledge,
            report_id=report.id if report is not None else None,
            tool_plan=tool_plan,
            trace_id=trace.id,
            request_id=agent_run.request_id,
            runtime_state=agent_run.runtime_state,
            replayed_response=agent_run.replayed_response,
            lease=lease,
        )

    def _materialized_outcome(
        self,
        user,
        session,
        original_input,
        model_input,
        agent_run,
        materialized: AgentTurnMaterialization | None,
        lease: RuntimeLease | None,
    ) -> AgentHarnessOutcome:
        user_id = int(user.id)
        session_id = int(session.id)
        session_public_id = str(session.public_id)
        report = self.db.get(PsychologicalReport, materialized.report_id) if materialized and materialized.report_id else None
        report_id = report.id if report is not None else None
        risk_level = report.risk_level if report is not None else agent_run.risk_level.value
        pending_report_id = report_id if materialized and not materialized.tools_dispatched else None
        replayed_response = (
            materialized.final_response
            if materialized and materialized.final_response
            else agent_run.replayed_response
        )
        return AgentHarnessOutcome(
            session=session,
            user_id=user_id,
            session_id=session_id,
            session_public_id=session_public_id,
            original_input=original_input,
            model_input=model_input,
            intent=agent_run.intent,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            agent_steps=agent_run.steps,
            retrieved_knowledge=agent_run.retrieved_knowledge,
            report_id=report_id,
            tool_plan=AgentToolPlan(report_id=pending_report_id, risk_level=risk_level),
            trace_id=materialized.trace_id if materialized else None,
            request_id=agent_run.request_id,
            runtime_state=agent_run.runtime_state,
            replayed_response=replayed_response,
            lease=lease,
        )

    def _acquire_lease(self, request_id: str) -> RuntimeLease:
        owner_id = f"chat-{uuid.uuid4().hex}"
        lease = RuntimeLeaseManager(self.db, self.settings).acquire(request_id, owner_id)
        if lease is None:
            raise RuntimeLeaseBusyError("该 requestId 正由另一个请求处理，请稍后使用同一 requestId 重试")
        return lease

    def renew_lease(self, lease: RuntimeLease | None) -> None:
        if lease is None:
            return
        if not RuntimeLeaseManager(self.db, self.settings).renew(lease):
            from app.agents.runtime_lease import RuntimeLeaseLostError

            raise RuntimeLeaseLostError("请求执行租约已丢失，停止继续生成")

    def release_lease(self, lease: RuntimeLease | None) -> None:
        if lease is None:
            return
        RuntimeLeaseManager(self.db, self.settings).release(lease)

    def save_assistant_message(
        self,
        user_id: int,
        session_id: int,
        session_public_id: str,
        content: str,
        request_id: str,
    ) -> None:
        materialized = (
            self.db.query(AgentTurnMaterialization)
            .filter(AgentTurnMaterialization.request_id == request_id)
            .first()
        )
        if materialized is None:
            self.save_message_by_id(user_id, session_id, session_public_id, MessageRole.ASSISTANT, content)
            return
        if materialized.assistant_message_id is not None:
            return
        message = ChatMessage(
            user_id=user_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT.value,
            content=content,
        )
        self.db.add(message)
        self.db.flush()
        materialized.assistant_message_id = message.id
        materialized.final_response = content
        materialized.updated_at = now()
        session = self.db.get(ChatSession, session_id)
        if session is not None:
            session.touch()
            self.db.add(session)
        self.db.add(materialized)
        self.db.commit()
        self.memory.append(session_public_id, MessageRole.ASSISTANT.value, content)

    def save_message_by_id(
        self,
        user_id: int,
        session_id: int,
        session_public_id: str,
        role: MessageRole,
        content: str,
    ) -> None:
        self.db.add(ChatMessage(user_id=user_id, session_id=session_id, role=role.value, content=content))
        session = self.db.get(ChatSession, session_id)
        if session is not None:
            session.touch()
            self.db.add(session)
        self.db.commit()
        self.memory.append(session_public_id, role.value, content)

    def mark_tools_dispatched(self, request_id: str) -> None:
        materialized = (
            self.db.query(AgentTurnMaterialization)
            .filter(AgentTurnMaterialization.request_id == request_id)
            .first()
        )
        if materialized is None or materialized.tools_dispatched:
            return
        materialized.tools_dispatched = True
        materialized.updated_at = now()
        self.db.add(materialized)
        self.db.commit()

    async def dispatch_tools(self, tool_plan: AgentToolPlan) -> list[str]:
        if tool_plan.report_id is None:
            return []
        if self.settings.tool_queue_enabled:
            ToolQueueService(self.db, self.settings).enqueue_report(tool_plan.report_id, tool_plan.risk_level)
            return ["queued"]
        return await MindBridgeMcpToolClient(self.settings).handle_report(tool_plan.report_id, tool_plan.risk_level)

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> None:
        self.db.add(ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content))
        session.touch()
        self.db.add(session)
        self.db.commit()
        self.memory.append(session.public_id, role.value, content)

    def _resolve_session(
        self,
        user: UserAccount,
        public_id: str | None,
        text: str,
        request_id: str,
    ) -> ChatSession:
        checkpoint = (
            self.db.query(AgentRuntimeCheckpoint)
            .filter(AgentRuntimeCheckpoint.request_id == request_id)
            .first()
        )
        if checkpoint is not None:
            state = BlackboardState.model_validate_json(checkpoint.state_json)
            if state.request.user_id != user.id:
                raise ValueError("requestId 不属于当前用户")
            if public_id and public_id != state.request.session_id:
                raise ValueError("requestId 与 sessionId 不匹配")
            session = (
                self.db.query(ChatSession)
                .filter(
                    ChatSession.public_id == state.request.session_id,
                    ChatSession.user_id == user.id,
                )
                .first()
            )
            if session is None:
                raise ValueError("checkpoint 对应的 Session 不存在")
            return session
        if public_id:
            session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id).first()
            if session is None:
                raise ValueError("Session not found")
            return session
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=text[:36])
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def _create_report(
        self,
        user: UserAccount,
        session: ChatSession,
        text: str,
        agent_run,
        *,
        commit: bool = True,
    ) -> PsychologicalReport | None:
        if not agent_run.requires_report or agent_run.assessment is None:
            return None
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=text,
            intent=agent_run.intent.value,
            emotion=agent_run.assessment.emotion.value,
            emotion_score=agent_run.assessment.emotion_score,
            risk_level=agent_run.assessment.risk.value,
            summary=agent_run.assessment.summary,
        )
        self.db.add(report)
        if commit:
            self.db.commit()
            self.db.refresh(report)
        else:
            self.db.flush()
        return report
