from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

from app.agents.blackboard import FlowStage
from app.agents.generation_lifecycle import GenerationLifecycle
from app.agents.runtime_store import NullRuntimeStore, SqlAlchemyRuntimeStore
from app.agents.state_coordinator import BlackboardCoordinator
from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.core.database import session_scope
from app.core.enums import IntentType, RiskLevel
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.services.ai import split_text
from app.services.agent_models import AgentModelRegistry
from app.services.output_guardrail import ResponseOutputGuardrail
from app.services.semantic_safety_review import SemanticResponseSafetyReviewer


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        # Final generation uses the same model gateway as ResponseAgent so
        # provider fallback and the circuit breaker cover the complete turn.
        self.ai = AgentModelRegistry(settings).client_for("ResponseAgent")
        self.output_guardrail = ResponseOutputGuardrail()
        self.semantic_safety_reviewer = SemanticResponseSafetyReviewer(
            AgentModelRegistry(settings).client_for("SafetyAgent")
        )
        self.agent_harness = MindBridgeAgentHarness(db, settings)

    async def prepare_chat(self, user: UserAccount, request: ChatRequest):
        return await self.agent_harness.run_async(user, request)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        outcome = await self.prepare_chat(user, request)
        async for event in self.stream_outcome(user, outcome):
            yield event

    async def stream_outcome(self, user: UserAccount, outcome):
        # A StreamingResponse can outlive the request-scoped DB dependency.
        # Give the stream its own session and use immutable identity snapshots
        # stored in the outcome; never carry request ORM objects across here.
        with session_scope() as stream_db:
            stream_service = ChatService(stream_db, self.settings)
            try:
                async for event in stream_service._stream_outcome_with_lease(outcome):
                    yield event
            except Exception as exc:
                logger.exception("SSE stream failed request_id=%s: %s", outcome.request_id, exc)
                yield sse(
                    "error",
                    ChatStreamEvent(
                        type="error",
                        sessionId=outcome.session_public_id,
                        requestId=outcome.request_id,
                        message="回复生成中断，请使用同一 requestId 重试",
                    ).model_dump(by_alias=True),
                )
            finally:
                stream_service.agent_harness.release_lease(outcome.lease)

    async def _stream_outcome_with_lease(self, outcome):
        last_lease_renewal = time.monotonic()
        heartbeat_interval = max(5.0, outcome.lease.ttl_seconds / 3) if outcome.lease else float("inf")
        yield sse(
            "meta",
            ChatStreamEvent(
                type="meta",
                sessionId=outcome.session_public_id,
                requestId=outcome.request_id,
            ).model_dump(by_alias=True),
        )
        store = (
            SqlAlchemyRuntimeStore(
                self.db,
                persistence_required=getattr(self.settings, "agent_runtime_persistence_required", True),
            )
            if getattr(self.settings, "agent_runtime_persistence_enabled", True)
            else NullRuntimeStore()
        )
        lifecycle = GenerationLifecycle(
            BlackboardCoordinator(self.settings),
            store,
        )
        runtime_state = outcome.runtime_state
        if outcome.replayed_response is not None:
            lifecycle_enabled = runtime_state.flow.current_stage == FlowStage.READY_FOR_GENERATION
            if lifecycle_enabled:
                runtime_state = lifecycle.started(runtime_state)
            yield sse(
                "token",
                ChatStreamEvent(
                    type="token",
                    sessionId=outcome.session_public_id,
                    requestId=outcome.request_id,
                    content=outcome.replayed_response,
                ).model_dump(),
            )
            await self._dispatch_tools(outcome)
            if lifecycle_enabled:
                lifecycle.completed(runtime_state, outcome.replayed_response)
            yield sse(
                "done",
                ChatStreamEvent(
                    type="done",
                    sessionId=outcome.session_public_id,
                    requestId=outcome.request_id,
                ).model_dump(),
            )
            return

        lifecycle_enabled = runtime_state.flow.current_stage == FlowStage.READY_FOR_GENERATION
        if lifecycle_enabled:
            runtime_state = lifecycle.started(runtime_state)
        assistant = []
        guardrail_replaced = False
        guardrail_issues: tuple[str, ...] = ()
        cited_ids: tuple[str, ...] = ()
        current_risk = runtime_state.safety.risk_level if runtime_state.safety else RiskLevel.MEDIUM
        buffered_support = requires_buffered_output(
            outcome.intent,
            current_risk,
            bool(getattr(self.settings, "agent_support_output_guardrail_enabled", True)),
        )
        try:
            async for token in self.ai.stream(outcome.response_messages):
                if time.monotonic() - last_lease_renewal >= heartbeat_interval:
                    self.agent_harness.renew_lease(outcome.lease)
                    last_lease_renewal = time.monotonic()
                assistant.append(token)
                if not buffered_support:
                    yield sse(
                        "token",
                        ChatStreamEvent(
                            type="token",
                            sessionId=outcome.session_public_id,
                            requestId=outcome.request_id,
                            content=token,
                        ).model_dump(),
                    )
        except BaseException as exc:
            if lifecycle_enabled:
                lifecycle.failed(runtime_state, f"SSE 生成中断：{type(exc).__name__}")
            raise
        if assistant:
            final_response = "".join(assistant)
            if buffered_support:
                evidence_count = len(runtime_state.context.retrieved_knowledge) if runtime_state.context else 0
                allowed_citation_ids = tuple(f"K{index}" for index in range(1, evidence_count + 1))
                guarded = self.output_guardrail.validate(
                    final_response,
                    current_risk,
                    allowed_citation_ids=allowed_citation_ids,
                    citations_required=bool(
                        getattr(self.settings, "agent_rag_citations_required", True)
                        and evidence_count
                        and (runtime_state.safety is None or runtime_state.safety.risk_level != RiskLevel.HIGH)
                    ),
                )
                final_response = guarded.content
                cited_ids = guarded.cited_ids
                if not guarded.allowed:
                    guardrail_replaced = True
                    guardrail_issues = guarded.issues
                    logger.warning(
                        "Final response guardrail replaced output request_id=%s issues=%s",
                        outcome.request_id,
                        guarded.issues,
                    )
                elif current_risk == RiskLevel.HIGH:
                    semantic_review = await self.semantic_safety_reviewer.review_async(final_response)
                    if not semantic_review.allowed:
                        final_response = self.output_guardrail.fallback(RiskLevel.HIGH)
                        cited_ids = ()
                        guardrail_replaced = True
                        guardrail_issues = semantic_review.issues
                        logger.warning(
                            "Semantic safety review replaced HIGH-risk output request_id=%s issues=%s prompt_id=%s",
                            outcome.request_id,
                            semantic_review.issues,
                            semantic_review.prompt_id,
                        )
            self.agent_harness.save_assistant_message(
                outcome.user_id,
                outcome.session_id,
                outcome.session_public_id,
                final_response,
                outcome.request_id,
            )
            await self._dispatch_tools(outcome)
            if lifecycle_enabled:
                runtime_state = lifecycle.completed(
                    runtime_state,
                    final_response,
                    guardrail_replaced=guardrail_replaced,
                    guardrail_issues=guardrail_issues,
                    cited_ids=cited_ids,
                )
            if buffered_support:
                for token in split_text(final_response, 12):
                    yield sse(
                        "token",
                        ChatStreamEvent(
                            type="token",
                            sessionId=outcome.session_public_id,
                            requestId=outcome.request_id,
                            content=token,
                        ).model_dump(),
                    )
        elif lifecycle_enabled:
            lifecycle.failed(runtime_state, "SSE 生成未返回任何内容")
        yield sse(
            "done",
            ChatStreamEvent(
                type="done",
                sessionId=outcome.session_public_id,
                requestId=outcome.request_id,
            ).model_dump(),
        )

    async def _dispatch_tools(self, outcome) -> None:
        self.agent_harness.renew_lease(outcome.lease)
        try:
            await self.agent_harness.dispatch_tools(outcome.tool_plan)
            if outcome.tool_plan.requires_tools:
                self.agent_harness.mark_tools_dispatched(outcome.request_id)
        except Exception as exc:
            logger.warning(
                "Post-response tool dispatch failed for session=%s report_id=%s: %s",
                outcome.session_public_id,
                outcome.report_id,
                exc,
                exc_info=True,
            )


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def requires_buffered_output(intent: IntentType, risk: RiskLevel, support_guard_enabled: bool) -> bool:
    """HIGH is a mandatory safety boundary; configuration cannot bypass it."""
    return risk == RiskLevel.HIGH or (support_guard_enabled and intent != IntentType.CHAT)
