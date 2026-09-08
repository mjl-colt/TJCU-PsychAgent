from __future__ import annotations

import json
from typing import Iterable

import httpx

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.prompt_catalog import prompt_catalog
from app.services.prompt_security import PromptSecurityService
from app.services.safety_policy import has_high_risk_signal


class PromptTemplates:
    @staticmethod
    def intent_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        security = PromptSecurityService()
        catalog = prompt_catalog()
        return [
            AiMessage(role="system", content=catalog.render("intent-classifier")),
            AiMessage(
                role="user",
                content=(
                    f"最近上下文：\n{security.wrap_untrusted(format_history(history), 'recent_history')}"
                    f"\n\n当前输入：\n{security.wrap_untrusted(user_input)}"
                ),
            ),
        ]

    @staticmethod
    def psychology_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        security = PromptSecurityService()
        catalog = prompt_catalog()
        return [
            AiMessage(role="system", content=catalog.render("psychology-assessment")),
            AiMessage(
                role="user",
                content=(
                    f"最近上下文：\n{security.wrap_untrusted(format_history(history), 'recent_history')}"
                    f"\n\n当前输入：\n{security.wrap_untrusted(user_input)}"
                ),
            ),
        ]

    @staticmethod
    def answer_system_prompt(intent: IntentType, risk: RiskLevel, context: str, display_name: str, skill_context: str = "") -> AiMessage:
        catalog = prompt_catalog()
        if intent == IntentType.CHAT:
            content = catalog.render("chat-response", display_name=display_name)
            return AiMessage(role="system", content=content)
        crisis_rules = catalog.render("crisis-response-rules") if risk == RiskLevel.HIGH else ""
        grounding_rules = catalog.render("rag-grounded-rules" if context.strip() else "rag-insufficient-rules")
        content = catalog.render(
            "support-response",
            display_name=display_name,
            context=context,
            skill_context=skill_context or "无",
            crisis_rules=crisis_rules,
            grounding_rules=grounding_rules,
        )
        return AiMessage(role="system", content=content)


class AiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, messages: list[AiMessage]) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return self._ollama(messages, stream=False)
        if provider == "openai":
            return self._openai(messages, stream=False)
        return self._mock(messages)

    async def complete_async(self, messages: list[AiMessage]) -> str:
        """Non-blocking completion used by concurrently scheduled agents.

        The synchronous ``complete`` method remains for command-line and legacy
        callers.  Keeping the HTTP implementation genuinely asynchronous is
        important: wrapping a blocking ``httpx.post`` in ``asyncio.gather``
        would still execute the requests serially on the event-loop thread.
        """

        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return await self._ollama_async(messages)
        if provider == "openai":
            return await self._openai_async(messages)
        return self._mock(messages)

    async def stream(self, messages: list[AiMessage]):
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            async for token in self._ollama_stream(messages):
                yield token
            return
        if provider == "openai":
            async for token in self._openai_stream(messages):
                yield token
            return
        text = self._mock(messages)
        for chunk in split_text(text, 12):
            yield chunk

    def _ollama(self, messages: list[AiMessage], stream: bool) -> str:
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        response = httpx.post(f"{self.settings.ollama_base_url}/api/chat", json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["message"]["content"]

    async def _ollama_async(self, messages: list[AiMessage]) -> str:
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.settings.ollama_base_url}/api/chat", json=payload)
        response.raise_for_status()
        return response.json()["message"]["content"]

    async def _ollama_stream(self, messages: list[AiMessage]):
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": True,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", f"{self.settings.ollama_base_url}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token

    def _openai(self, messages: list[AiMessage], stream: bool) -> str:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": stream,
        }
        response = httpx.post(f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _openai_async(self, messages: list[AiMessage]) -> str:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.settings.openai_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _openai_stream(self, messages: list[AiMessage]):
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ").strip()
                    if raw == "[DONE]":
                        break
                    data = json.loads(raw)
                    token = data["choices"][0].get("delta", {}).get("content", "")
                    if token:
                        yield token

    def _mock(self, messages: list[AiMessage]) -> str:
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        system = " ".join(m.content for m in messages if m.role == "system")
        if "PROMPT_ID=semantic-output-safety-review:v1" in system:
            lowered = last.lower()
            return json.dumps(
                {
                    "acknowledges_distress": any(term in lowered for term in ("痛苦", "不容易", "担心", "hear", "sorry")),
                    "addresses_immediate_safety": any(term in lowered for term in ("安全", "safe", "危险")),
                    "connects_human_support": any(term in lowered for term in ("可信任", "身边", "室友", "家人", "辅导员", "trusted", "someone")),
                    "recommends_emergency_escalation": any(term in lowered for term in ("紧急", "校园保卫", "emergency", "心理中心")),
                    "contains_diagnosis": any(term in lowered for term in ("确诊", "患有", "diagnosed")),
                    "contains_medication_instruction": any(term in lowered for term in ("毫克", "服用", "mg")),
                    "contains_dangerous_details": "具体方法" in lowered or "操作步骤" in lowered,
                },
                ensure_ascii=False,
            )
        if "PROMPT_ID=psychology-assessment:" in system:
            if has_high_risk_signal(last):
                return '{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","summary":"检测到明确高风险表达"}'
            if _mock_has_consult_signal(last):
                return '{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","summary":"检测到压力或情绪求助表达"}'
            return '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","summary":"未检测到明显风险信号"}'
        if "PROMPT_ID=intent-classifier:" in system:
            if has_high_risk_signal(last):
                return "RISK"
            if _mock_has_consult_signal(last):
                return "CONSULT"
            return "CHAT"
        if "high_risk_safety_plan" in system and has_high_risk_signal(last):
            return "我听到你现在已经痛苦到觉得撑不下去了。现在最重要的是先让你不要一个人扛：请马上联系身边可信任的人，或者直接联系辅导员、学校心理中心、校园保卫/当地紧急服务。接下来 10 分钟，请先把自己移到有人在的地方，并把可能伤害自己的东西放远一点。如果可以，回我一句：你现在身边有没有可以马上联系或走过去找的人？"
        if "PROMPT_ID=support-response:" in system or "模式：support" in system:
            citation = " [K1]" if "[K1]" in system else ""
            return "我听到你最近压力很大，还影响到了睡眠，这种状态确实会让人很消耗。你可以先做两件小事：今晚把最担心的事情写成清单，先只选一个最小步骤处理；睡前 30 分钟把手机和学习任务放远一点，用缓慢呼吸或热水澡帮身体降下来。" + citation + " 如果这种失眠持续一周以上，建议联系学校心理中心或辅导员一起看一看。"
        if "PROMPT_ID=chat-response:" in system:
            return "我在。这个问题可以直接拆开来看，我们先从你最想解决的那一部分开始。"
        if "PROMPT_ID=context-memory-summary:" in system:
            return last[:40] or "无相关历史记忆。"
        if "PROMPT_ID=context-query-rewrite:" in system:
            return last[:40] or "校园心理支持"
        return "我在。先把你现在最具体的困扰说出来，我们可以一步一步拆开。如果情况已经影响安全，请马上联系身边可信任的人或学校心理中心。"


def format_history(history: list[AiMessage]) -> str:
    if not history:
        return "无"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-20:])


_MOCK_CONSULT_WORDS = ("焦虑", "抑郁", "压力", "失眠", "难过", "崩溃", "痛苦", "无助", "心理", "咨询", "anxious", "depress", "stress")


def _mock_has_consult_signal(text: str) -> bool:
    """Deterministic fixture behavior for AI_PROVIDER=mock; never used for production routing."""
    normalized = text.lower()
    return any(word in normalized for word in _MOCK_CONSULT_WORDS)


def split_text(text: str, size: int) -> Iterable[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]
