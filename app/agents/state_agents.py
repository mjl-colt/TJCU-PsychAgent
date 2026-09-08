from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.agents.blackboard import (
    AgentAction,
    AgentCommand,
    AgentName,
    AgentStateUpdate,
    BlackboardSection,
    BlackboardState,
    ContextCompactionState,
    ContextState,
    KnowledgeEvidence,
    PromptReviewState,
    ResponsePolicyContract,
    ResponseState,
    ResponseStatus,
    SafetyState,
    UnderstandingState,
)
from app.agents.runtime_services import AgentRuntimeServices
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import (
    PromptTemplates,
)
from app.services.assessment import PsychologicalAssessmentService
from app.services.knowledge import KnowledgeService, has_domain_query_signal
from app.services.memory import compact_history_with_trace
from app.services.prompt_catalog import prompt_catalog
from app.services.skills import MindBridgeSkillLibrary
from app.services.prompt_security import PromptSecurityService
from app.services.safety_policy import SAFETY_POLICY_VERSION, detect_safety_signals, has_high_risk_signal


class IntentClassificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: IntentType
    reason: str = Field(min_length=1, max_length=240)


class StatefulAgent(Protocol):
    name: AgentName

    async def run(self, command: AgentCommand, state: BlackboardState) -> AgentStateUpdate:
        ...


class BaseStatefulAgent:
    name: AgentName

    def __init__(self, services: AgentRuntimeServices):
        self.services = services

    def client(self):
        return self.services.model_registry.client_for(self.name.value)


class StatefulUnderstandingAgent(BaseStatefulAgent):
    name = AgentName.UNDERSTANDING

    async def run(self, command: AgentCommand, state: BlackboardState) -> AgentStateUpdate:
        if command.action != AgentAction.UNDERSTAND:
            raise ValueError(f"UnderstandingAgent does not support {command.action.value}")
        text = state.request.model_input
        intent, reason = await self._classify(text)
        topic = _topic(intent)
        result = UnderstandingState(
            intent=intent,
            topic=topic,
            reason=reason,
            prompt_template_version=prompt_catalog().get_required("intent-classifier").prompt_id,
        )
        return AgentStateUpdate(section=BlackboardSection.UNDERSTANDING, data=result)

    async def _classify(self, text: str) -> tuple[IntentType, str]:
        if has_high_risk_signal(text):
            return IntentType.RISK, "high-risk deterministic rule"
        try:
            messages = PromptTemplates.intent_prompt([], text)
            label, reason = _parse_intent_result(await self.client().complete_async(messages))
            if label is not None:
                return label, reason
        except Exception:
            pass
        return IntentType.CONSULT, "classifier unavailable; conservative support fallback"


class StatefulSafetyAgent(BaseStatefulAgent):
    name = AgentName.SAFETY

    async def run(self, command: AgentCommand, state: BlackboardState) -> AgentStateUpdate:
        if command.action == AgentAction.ASSESS_RISK:
            return await self._assess(state)
        if command.action == AgentAction.REVIEW_RESPONSE:
            return await self._review(state)
        raise ValueError(f"SafetyAgent does not support {command.action.value}")

    async def _assess(self, state: BlackboardState) -> AgentStateUpdate:
        text = state.request.model_input
        detection = detect_safety_signals(text)
        history = list(state.context.model_history) if state.context else await asyncio.to_thread(
            self.services.memory.load_recent,
            self.services.session.public_id,
        )
        history = self._safe_assessment_history(history, text)
        assessment = await PsychologicalAssessmentService(self.client()).assess_async(text, history)
        hard_risk = detection.hard_high
        constraints = ["不进行医学诊断", "不提供药物剂量建议", "不暴露后台风险标签"]
        if assessment.risk == RiskLevel.HIGH:
            constraints.extend(
                [
                    "优先确认用户当前是否安全",
                    "建议立即联系现实中的可信任人员",
                    "提供学校或当地紧急求助渠道",
                    "不得输出可能加重风险的危险细节",
                ]
            )
        result = SafetyState(
            risk_level=assessment.risk,
            risk_type="SELF_HARM_OR_IMMEDIATE_DANGER" if assessment.risk == RiskLevel.HIGH else None,
            risk_signals=detection.signal_ids,
            assessment_method=(
                f"HARD_RULE:{SAFETY_POLICY_VERSION}"
                if hard_risk
                else f"MODEL_WITH_POLICY_FALLBACK:{SAFETY_POLICY_VERSION}"
            ),
            response_constraints=tuple(constraints),
            emotion=assessment.emotion,
            emotion_score=assessment.emotion_score,
            summary=assessment.summary,
            prompt_template_version=(
                f"{prompt_catalog().get_required('psychology-assessment').prompt_id}+{SAFETY_POLICY_VERSION}"
            ),
        )
        return AgentStateUpdate(section=BlackboardSection.SAFETY, data=result)

    def _safe_assessment_history(self, history: list[AiMessage], text: str) -> list[AiMessage]:
        bounded = [
            AiMessage(role=item.role, content=_clip(item.content, 600))
            for item in history[-8:]
            if item.role in {"user", "assistant"}
        ]
        if not bounded or bounded[-1].role != "user" or bounded[-1].content != text:
            bounded.append(AiMessage(role="user", content=text))
        return bounded

    async def _review(self, state: BlackboardState) -> AgentStateUpdate:
        if state.safety is None or state.response is None:
            raise ValueError("Safety review requires safety assessment and response proposal")
        response = state.response
        combined = _render_prompt(response.messages)
        issues: list[str] = []
        expected_contract = _build_response_policy_contract(
            state,
            response.mode,
            self.services.settings,
            include_evidence=not response.safe_fallback,
        )
        if response.policy_contract != expected_contract:
            issues.append("候选 Prompt 的强类型安全契约与当前风险、证据不一致")
        if _policy_contract_marker(response.policy_contract) not in combined:
            issues.append("候选 Prompt 没有携带可校验的安全契约指纹")
        if state.context and state.context.retrieved_knowledge:
            expected_citations = tuple(f"[{item}]" for item in expected_contract.allowed_citation_ids)
            if not all(citation in combined for citation in expected_citations):
                issues.append("候选 Prompt 没有完整携带 RAG 证据标签")
        if _contains_unsafe_response_instruction(combined):
            issues.append("候选 Prompt 包含诊断、擅自用药或危险操作指令")
        if len(combined) > self.services.settings.agent_max_prompt_chars:
            issues.append("候选 Prompt 超过生产长度上限")
        review = PromptReviewState(
            prompt_version=response.prompt_version,
            approved=not issues,
            issues=tuple(issues),
            reason="候选 Prompt 满足当前安全约束" if not issues else "；".join(issues),
        )
        result = state.safety.model_copy(update={"prompt_review": review})
        return AgentStateUpdate(section=BlackboardSection.SAFETY, data=result)


class StatefulContextAgent(BaseStatefulAgent):
    name = AgentName.CONTEXT

    async def run(self, command: AgentCommand, state: BlackboardState) -> AgentStateUpdate:
        if command.action != AgentAction.GATHER_CONTEXT:
            raise ValueError(f"ContextAgent does not support {command.action.value}")
        history = await self._load_history()
        compaction = compact_history_with_trace(
            history,
            self.services.settings,
            state.request.model_input,
            compaction_id=command.command_id,
        )
        compacted = list(compaction.messages)
        deterministic_brief = compaction.summary
        memory_brief = await self._summarize_memory(history, state.request.model_input, deterministic_brief)
        model_history = self._bounded_history([*compacted, AiMessage(role="user", content=state.request.model_input)])
        intent = state.understanding.intent if state.understanding else (state.flow.route or IntentType.CHAT)
        risk = state.safety.risk_level if state.safety else RiskLevel.MEDIUM

        query = await self._rewrite_query(memory_brief, state.request.model_input)
        retrieved = await asyncio.to_thread(self._retrieve_with_isolated_session, query)
        skill_names = MindBridgeSkillLibrary.response_skill_names(intent, risk)
        registry = MindBridgeSkillLibrary.registry()
        selected_skills = [registry.get_required(name) for name in skill_names]
        skill_context = "\n\n".join(skill.prompt_context() for skill in selected_skills)
        evidence = tuple(
            KnowledgeEvidence(chunk_id=item.chunk_id, source=item.source, content=_sanitize_retrieved_content(item.content), score=item.score)
            for item in retrieved
        )
        result = ContextState(
            memory_brief=memory_brief,
            model_history=tuple(model_history),
            rewritten_query=query,
            retrieved_knowledge=evidence,
            skill_context=_clip(skill_context, self.services.settings.agent_max_prompt_chars // 4),
            skill_versions=tuple(f"{skill.name}:{skill.version}" for skill in selected_skills),
            compaction=ContextCompactionState(
                compaction_id=compaction.compaction_id,
                source_message_count=compaction.source_message_count,
                retained_message_count=compaction.retained_message_count,
                summary_chars=len(compaction.summary),
                compacted=compaction.compacted,
                summary_hash=compaction.summary_hash,
            ),
            prompt_template_version=prompt_catalog().bundle_version(
                "context-query-rewrite",
                "context-memory-summary",
            ),
        )
        return AgentStateUpdate(section=BlackboardSection.CONTEXT, data=result)

    async def _load_history(self) -> list[AiMessage]:
        history = await asyncio.to_thread(self.services.memory.load_recent, self.services.session.public_id)
        if history:
            return history
        return await asyncio.to_thread(self._load_history_from_database)

    def _load_history_from_database(self) -> list[AiMessage]:
        from app.models.entities import ChatMessage

        bind = self.services.db.get_bind()
        with Session(bind=bind) as db:
            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == self.services.session.id)
                .order_by(ChatMessage.created_at.desc())
                .limit(self.services.settings.redis_memory_max_messages)
                .all()
            )
            rows.reverse()
            history = self.services.memory.messages_from_rows(rows)
        if history:
            self.services.memory.replace(self.services.session.public_id, history)
        return history

    def _retrieve_with_isolated_session(self, query: str):
        bind = self.services.db.get_bind()
        with Session(bind=bind) as db:
            return KnowledgeService(db, self.services.settings).retrieve(
                query,
                self.services.settings.knowledge_top_k,
            )

    async def _rewrite_query(self, memory_brief: str, model_input: str) -> str:
        security = PromptSecurityService()
        try:
            query = await self.client().complete_async(
                [
                    AiMessage(
                        role="system",
                        content=prompt_catalog().render("context-query-rewrite"),
                    ),
                    AiMessage(
                        role="user",
                        content=(
                            f"记忆摘要：\n{security.wrap_untrusted(memory_brief, 'memory_brief')}"
                            f"\n\n当前输入：\n{security.wrap_untrusted(model_input)}"
                        ),
                    ),
                ]
            )
            rewritten = (query.strip() or model_input)[:60]
            if has_domain_query_signal(model_input) and not has_domain_query_signal(rewritten):
                return model_input[:60]
            return rewritten
        except Exception:
            return model_input[:60]

    async def _summarize_memory(self, history: list[AiMessage], current_input: str, fallback: str) -> str:
        if not history:
            return "无相关历史记忆。"
        max_chars = max(120, self.services.settings.memory_summary_max_chars)
        try:
            security = PromptSecurityService()
            summary = await self.client().complete_async(
                [
                    AiMessage(role="system", content=prompt_catalog().render("context-memory-summary")),
                    AiMessage(
                        role="user",
                        content=(
                            f"当前输入：\n{security.wrap_untrusted(current_input)}"
                            f"\n\n最近历史：\n{security.wrap_untrusted(str(history[-12:]), 'recent_history')}"
                        ),
                    ),
                ]
            )
            return summary.strip()[:max_chars] or fallback
        except Exception:
            return fallback or "无相关历史记忆。"

    def _bounded_history(self, history: list[AiMessage]) -> list[AiMessage]:
        limit = max(2, self.services.settings.chat_history_limit * 2)
        if len(history) <= limit:
            return history
        if history[0].role == "system":
            return [history[0], *history[-(limit - 1):]]
        return history[-limit:]


class StatefulResponseAgent(BaseStatefulAgent):
    name = AgentName.RESPONSE

    async def run(self, command: AgentCommand, state: BlackboardState) -> AgentStateUpdate:
        if command.action in {AgentAction.PREPARE_RESPONSE, AgentAction.REVISE_RESPONSE}:
            return await self._prepare(state, revision=command.action == AgentAction.REVISE_RESPONSE)
        if command.action == AgentAction.FINALIZE_RESPONSE:
            if state.response is None:
                raise ValueError("response finalization requires a proposal")
            ready = state.response.model_copy(update={"generation_status": ResponseStatus.READY_FOR_GENERATION})
            return AgentStateUpdate(section=BlackboardSection.RESPONSE, data=ready)
        raise ValueError(f"ResponseAgent does not support {command.action.value}")

    async def _prepare(self, state: BlackboardState, revision: bool) -> AgentStateUpdate:
        intent = state.flow.route or (state.understanding.intent if state.understanding else IntentType.CHAT)
        risk = state.safety.risk_level if state.safety else RiskLevel.MEDIUM
        context = state.context
        model_history = list(context.model_history) if context and context.model_history else [
            AiMessage(role="user", content=state.request.model_input)
        ]
        memory_brief = context.memory_brief if context else "无相关历史记忆。"
        final_context = (
            _clip(
                "\n\n".join(
                    f"- [K{index}] 来源={_safe_source_label(item.source)}；内容={item.content}"
                    for index, item in enumerate(context.retrieved_knowledge, start=1)
                ),
                self.services.settings.agent_max_prompt_chars // 3,
            )
            if context
            else ""
        )
        skill_context = context.skill_context if context else ""
        constraints = "\n".join(f"- {item}" for item in (state.safety.response_constraints if state.safety else ()))
        revision_issue = ""
        if revision and state.safety and state.safety.prompt_review:
            revision_issue = "\n必须修正上一版本问题：\n" + "\n".join(
                f"- {item}" for item in state.safety.prompt_review.issues
            )
        mode = "normal_chat" if intent == IntentType.CHAT and risk == RiskLevel.LOW else "support"
        policy_contract = _build_response_policy_contract(state, mode, self.services.settings)
        system_prompt = PromptTemplates.answer_system_prompt(
            IntentType.CHAT if mode == "normal_chat" else intent,
            risk,
            final_context,
            self.services.user.display_name,
            skill_context,
        )
        max_prompt_chars = self.services.settings.agent_max_prompt_chars
        response_control = AiMessage(
            role="system",
            content=prompt_catalog().render(
                "response-controller",
                mode=mode,
                memory_brief=PromptSecurityService().wrap_untrusted(memory_brief, "memory_brief"),
                policy_contract=_policy_contract_block(policy_contract),
                constraints=constraints or "- 使用安全、非诊断表达",
                revision_issue=revision_issue,
            ),
        )
        messages = [
            AiMessage(role=system_prompt.role, content=_clip(system_prompt.content, int(max_prompt_chars * 0.55))),
            AiMessage(role=response_control.role, content=_clip(response_control.content, int(max_prompt_chars * 0.20))),
            *_fit_history_to_budget(model_history, int(max_prompt_chars * 0.25)),
        ]
        version = (state.response.prompt_version + 1) if state.response else 1
        rendered_prompt = _render_prompt(messages)
        result = ResponseState(
            prompt_version=version,
            messages=tuple(messages),
            mode=mode,
            intent=intent,
            risk_level=risk,
            policy_contract=policy_contract,
            prompt_template_version=prompt_catalog().bundle_version(
                "chat-response" if mode == "normal_chat" else "support-response",
                "response-controller",
            ),
            prompt_hash=hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest(),
        )
        return AgentStateUpdate(section=BlackboardSection.RESPONSE, data=result)


class AgentFallbackPolicy:
    """Deterministic fail-safe results used after retry exhaustion."""

    def create(self, command: AgentCommand, state: BlackboardState, error: Exception) -> AgentStateUpdate | None:
        text = state.request.model_input
        if command.action == AgentAction.UNDERSTAND:
            intent = IntentType.RISK if has_high_risk_signal(text) else IntentType.CONSULT
            return AgentStateUpdate(
                section=BlackboardSection.UNDERSTANDING,
                data=UnderstandingState(
                    intent=intent,
                    topic=_topic(intent),
                    reason=f"deterministic fallback after {type(error).__name__}",
                    prompt_template_version=prompt_catalog().get_required("intent-classifier").prompt_id,
                ),
            )
        if command.action == AgentAction.ASSESS_RISK:
            detection = detect_safety_signals(text)
            hard = detection.hard_high
            risk = RiskLevel.HIGH if hard else RiskLevel.MEDIUM
            return AgentStateUpdate(
                section=BlackboardSection.SAFETY,
                data=SafetyState(
                    risk_level=risk,
                    risk_type="SELF_HARM_OR_IMMEDIATE_DANGER" if hard else "ASSESSMENT_UNAVAILABLE",
                    risk_signals=detection.signal_ids,
                    assessment_method=f"FAIL_CLOSED_FALLBACK:{SAFETY_POLICY_VERSION}",
                    response_constraints=(
                        "不进行医学诊断",
                        "使用保守、安全的支持性表达",
                        "风险评估不可用时建议寻求现实支持",
                    ),
                    emotion=EmotionLabel.HIGH_RISK if hard else EmotionLabel.ANXIETY,
                    emotion_score=4.0 if hard else 3.0,
                    summary="安全模型不可用，已采用保守兜底",
                    prompt_template_version=(
                        f"{prompt_catalog().get_required('psychology-assessment').prompt_id}+{SAFETY_POLICY_VERSION}"
                    ),
                ),
            )
        if command.action == AgentAction.GATHER_CONTEXT:
            return AgentStateUpdate(
                section=BlackboardSection.CONTEXT,
                data=ContextState(
                    model_history=(AiMessage(role="user", content=text),),
                    memory_brief="上下文服务不可用，使用当前输入。",
                    prompt_template_version=prompt_catalog().bundle_version(
                        "context-query-rewrite",
                        "context-memory-summary",
                    ),
                ),
            )
        if command.action in {AgentAction.PREPARE_RESPONSE, AgentAction.REVISE_RESPONSE}:
            intent = state.flow.route or IntentType.CONSULT
            risk = state.safety.risk_level if state.safety else RiskLevel.MEDIUM
            mode = "safe_fallback"
            contract = _build_response_policy_contract(
                state,
                mode,
                self.services.settings,
                include_evidence=False,
            )
            messages = (
                PromptTemplates.answer_system_prompt(intent, risk, "", "同学"),
                AiMessage(
                    role="system",
                    content=(
                        "这是系统内置安全兜底 Prompt；不得诊断、不得提供危险细节。\n"
                        + _policy_contract_block(contract)
                    ),
                ),
                AiMessage(role="user", content=text),
            )
            version = state.response.prompt_version + 1 if state.response else 1
            return AgentStateUpdate(
                section=BlackboardSection.RESPONSE,
                data=ResponseState(
                    prompt_version=version,
                    messages=messages,
                    mode=mode,
                    intent=intent,
                    risk_level=risk,
                    safe_fallback=True,
                    policy_contract=contract,
                    prompt_template_version=prompt_catalog().bundle_version(
                        "support-response",
                        "response-controller",
                    ),
                    prompt_hash=hashlib.sha256(_render_prompt(messages).encode("utf-8")).hexdigest(),
                ),
            )
        if command.action == AgentAction.REVIEW_RESPONSE and state.safety and state.response:
            review = PromptReviewState(
                prompt_version=state.response.prompt_version,
                approved=state.response.safe_fallback,
                issues=() if state.response.safe_fallback else ("安全审查服务不可用",),
                reason="仅批准系统内置安全兜底 Prompt" if state.response.safe_fallback else "审查失败，禁止自动通过",
            )
            return AgentStateUpdate(
                section=BlackboardSection.SAFETY,
                data=state.safety.model_copy(update={"prompt_review": review}),
            )
        if command.action == AgentAction.FINALIZE_RESPONSE and state.response:
            return AgentStateUpdate(
                section=BlackboardSection.RESPONSE,
                data=state.response.model_copy(update={"generation_status": ResponseStatus.READY_FOR_GENERATION}),
            )
        return None


def _topic(intent: IntentType) -> str:
    if intent == IntentType.RISK:
        return "safety"
    if intent == IntentType.CONSULT:
        return "mental_health_support"
    return "general_conversation"


def _build_response_policy_contract(
    state: BlackboardState,
    mode: str,
    settings,
    *,
    include_evidence: bool = True,
) -> ResponsePolicyContract:
    risk = state.safety.risk_level if state.safety else RiskLevel.MEDIUM
    evidence_count = len(state.context.retrieved_knowledge) if state.context and include_evidence else 0
    high_risk = risk == RiskLevel.HIGH
    return ResponsePolicyContract(
        requires_non_diagnostic=mode != "normal_chat",
        requires_immediate_safety_check=high_risk,
        requires_human_support=high_risk,
        requires_emergency_escalation=high_risk,
        requires_rag_citations=bool(
            getattr(settings, "agent_rag_citations_required", True)
            and evidence_count
            and not high_risk
        ),
        allowed_citation_ids=tuple(f"K{index}" for index in range(1, evidence_count + 1)),
    )


def _policy_contract_payload(contract: ResponsePolicyContract) -> str:
    return json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _policy_contract_marker(contract: ResponsePolicyContract) -> str:
    digest = hashlib.sha256(_policy_contract_payload(contract).encode("utf-8")).hexdigest()
    return f"POLICY_CONTRACT_SHA256={digest}"


def _policy_contract_block(contract: ResponsePolicyContract) -> str:
    return (
        f"{_policy_contract_marker(contract)}\n"
        "POLICY_CONTRACT_JSON=" + _policy_contract_payload(contract)
    )


def _render_prompt(messages) -> str:
    return "\n\n".join(f"{message.role}: {message.content}" for message in messages)


def _parse_intent_result(raw: str) -> tuple[IntentType | None, str]:
    """Parse a strict JSON classifier result while retaining legacy labels."""
    text = (raw or "").strip().strip("`").strip()
    try:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
            payload = IntentClassificationPayload.model_validate(data)
            return payload.intent, payload.reason
    except (ValueError, TypeError, json.JSONDecodeError, ValidationError):
        pass
    token = text.upper().splitlines()[0].strip() if text else ""
    try:
        return IntentType(token), "legacy exact-label classifier"
    except ValueError:
        return None, "invalid classifier output"


def _sanitize_retrieved_content(content: str) -> str:
    """Mark retrieved text as data and neutralize common prompt-injection verbs."""
    text, _ = PromptSecurityService().neutralize_untrusted(content)
    return text


def _contains_unsafe_response_instruction(prompt: str) -> bool:
    patterns = (
        r"你(已经)?(患有|得了|确诊为)",
        r"建议你.{0,12}(服用|停用|换用).{0,20}(药|毫克|mg)",
        r"每天.{0,12}\d+\s*(毫克|mg)",
        r"告诉用户.{0,20}(自伤|自杀).{0,20}(方法|步骤|技巧)",
    )
    return any(re.search(pattern, prompt, re.IGNORECASE) for pattern in patterns)


def _safe_source_label(value: str) -> str:
    return re.sub(r"[\[\]<>\r\n]", "_", value or "unknown")[:128]


def _clip(value: str, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "\n[内容已按生产限制截断]"


def _fit_history_to_budget(history: list[AiMessage], budget: int) -> list[AiMessage]:
    """Keep the newest messages while enforcing a character budget."""
    selected: list[AiMessage] = []
    remaining = max(200, budget)
    for message in reversed(history):
        if remaining <= 0:
            break
        content = _clip(message.content, remaining)
        selected.append(AiMessage(role=message.role, content=content))
        remaining -= len(content)
    selected.reverse()
    return selected
