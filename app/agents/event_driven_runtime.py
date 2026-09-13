from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.agents.blackboard import BlackboardState, FlowStage, ResponseStatus, RuntimeExecution
from app.agents.blackboard_runtime import BlackboardEventRuntime
from app.agents.dispatcher import AgentDispatcher
from app.agents.result import AgentRunResult
from app.agents.runtime_services import AgentRuntimeServices
from app.agents.generation_lifecycle import GenerationLifecycle
from app.services.prompt_security import PromptSecurityService
from app.agents.runtime_store import NullRuntimeStore, SqlAlchemyRuntimeStore
from app.agents.runtime_lease import RuntimeLease, RuntimeLeaseLostError, RuntimeLeaseManager
from app.agents.state_agents import (
    StatefulContextAgent,
    StatefulResponseAgent,
    StatefulSafetyAgent,
    StatefulUnderstandingAgent,
)
from app.agents.state_coordinator import BlackboardCoordinator
from app.agents.workflow import WorkflowCoordinator
from app.agents.workflow_runtime import WorkflowRuntime
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import PromptTemplates
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from app.services.memory import RedisShortTermMemoryStore


class AgentRuntimeService:
    """New requests use workflow-v2; old checkpoints retain event-v1 semantics."""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.memory = RedisShortTermMemoryStore(settings)
        self.model_registry = AgentModelRegistry(settings)

    def run(
        self,
        user: UserAccount,
        session: ChatSession,
        model_input: str,
        request_id: str | None = None,
        lease: RuntimeLease | None = None,
    ) -> AgentRunResult:
        """Synchronous compatibility entry point for CLI harnesses and tests."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(user, session, model_input, request_id, lease=lease))
        raise RuntimeError("AgentRuntimeService.run() cannot run inside an event loop; await run_async()")

    async def run_async(
        self,
        user: UserAccount,
        session: ChatSession,
        model_input: str,
        request_id: str | None = None,
        lease: RuntimeLease | None = None,
    ) -> AgentRunResult:
        services = AgentRuntimeServices(
            db=self.db,
            settings=self.settings,
            user=user,
            session=session,
            model_registry=self.model_registry,
            memory=self.memory,
        )
        agents = [
            StatefulUnderstandingAgent(services),
            StatefulSafetyAgent(services),
            StatefulContextAgent(services),
            StatefulResponseAgent(services),
        ]
        store = (
            SqlAlchemyRuntimeStore(
                self.db,
                persistence_required=getattr(self.settings, "agent_runtime_persistence_required", True),
                lease=lease if getattr(self.settings, "agent_runtime_lease_enabled", True) else None,
            )
            if getattr(self.settings, "agent_runtime_persistence_enabled", True)
            else NullRuntimeStore()
        )
        state = store.load(request_id) if request_id else None
        resume = state is not None
        if state is not None:
            self._validate_resume(state, user, session, model_input)
            if state.flow.current_stage == FlowStage.GENERATING and state.flow.active_batch is None:
                state = GenerationLifecycle(
                    BlackboardCoordinator(self.settings),
                    store,
                ).failed(
                    state,
                    "检测到上次 SSE 生成未正常结束，已回到可重试状态",
                )
        else:
            security = PromptSecurityService().scan(model_input)
            state = BlackboardState.create(
                model_input=model_input,
                user_id=user.id,
                session_id=session.public_id,
                request_id=request_id,
                prompt_injection_signals=security.signals,
                workflow_version="workflow-v2",
            )
        runtime_class = WorkflowRuntime if state.workflow_version == "workflow-v2" else BlackboardEventRuntime
        coordinator_class = WorkflowCoordinator if state.workflow_version == "workflow-v2" else BlackboardCoordinator
        runtime = runtime_class(
            coordinator=coordinator_class(self.settings),
            dispatcher=AgentDispatcher(agents, self.settings),
            settings=self.settings,
            store=store,
        )
        execution = await self._execute_with_lease(runtime, state, resume, lease)
        return self._to_result(execution, user)

    async def _execute_with_lease(self, runtime, state, resume, lease):
        if lease is None:
            return await runtime.run(state, resume=resume)
        task = asyncio.create_task(runtime.run(state, resume=resume))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=max(1.0, lease.ttl_seconds / 3))
                if done:
                    return task.result()
                if not RuntimeLeaseManager(self.db, self.settings).renew(lease):
                    raise RuntimeLeaseLostError("Agent 执行期间租约丢失")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def resume_state_async(
        self,
        user: UserAccount,
        session: ChatSession,
        state: BlackboardState,
        lease: RuntimeLease | None = None,
    ) -> AgentRunResult:
        """Resume a state loaded by the startup recovery scanner."""

        return await self.run_async(
            user,
            session,
            state.request.model_input,
            request_id=state.request.request_id,
            lease=lease,
        )

    @staticmethod
    def _validate_resume(
        state: BlackboardState,
        user: UserAccount,
        session: ChatSession,
        model_input: str,
    ) -> None:
        if state.request.user_id != user.id or state.request.session_id != session.public_id:
            raise ValueError("requestId 不属于当前用户或会话")
        if state.request.model_input != model_input:
            raise ValueError("同一个 requestId 不能用于不同的输入")

    def _to_result(self, execution: RuntimeExecution, user: UserAccount) -> AgentRunResult:
        state = execution.state
        intent = state.flow.route or (state.understanding.intent if state.understanding else IntentType.CHAT)
        risk = state.safety.risk_level if state.safety else RiskLevel.HIGH
        review = state.safety.prompt_review if state.safety else None
        response_is_approved = bool(
            state.flow.current_stage in {
                FlowStage.READY_FOR_GENERATION,
                FlowStage.GENERATING,
                FlowStage.FINALIZING_RESPONSE,
                FlowStage.COMPLETED,
            }
            and state.response
            and state.response.generation_status == ResponseStatus.READY_FOR_GENERATION
            and review
            and review.approved
            and review.prompt_version == state.response.prompt_version
        )
        response_messages = list(state.response.messages) if response_is_approved and state.response else []
        if not response_messages:
            response_messages = self._fallback_messages(intent, risk, user.display_name, state.request.model_input)
        assessment = None
        if state.safety:
            assessment = PsychologyAssessment(
                emotion=state.safety.emotion,
                emotion_score=state.safety.emotion_score,
                risk=state.safety.risk_level,
                summary=state.safety.summary,
            )
        retrieved = []
        memory_brief = "无相关历史记忆。"
        if state.context:
            memory_brief = state.context.memory_brief
            retrieved = [
                SearchResult(item.chunk_id, item.source, item.content, item.score)
                for item in state.context.retrieved_knowledge
            ]
        return AgentRunResult(
            intent=intent,
            risk_level=risk,
            assessment=assessment,
            retrieved_knowledge=retrieved,
            response_messages=response_messages,
            memory_brief=memory_brief,
            collaboration_events=list(execution.events),
            runtime_state=state,
            request_id=state.request.request_id,
            replayed_response=state.response.final_response if state.response else None,
        )

    def _fallback_messages(self, intent: IntentType, risk: RiskLevel, display_name: str, model_input: str) -> list[AiMessage]:
        return [
            PromptTemplates.answer_system_prompt(intent, risk, "", display_name),
            AiMessage(role="user", content=model_input),
        ]


# Historical imports remain valid; engine selection is always checkpoint-versioned.
EventDrivenAgentRuntimeService = AgentRuntimeService
