from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from app.core.config import Settings
from app.services.ai import AiClient


AGENT_MODEL_ALIASES = {
    "UnderstandingAgent": "understanding",
    "SafetyAgent": "safety",
    "ContextAgent": "context",
    "ResponseAgent": "response",
}


@dataclass(frozen=True)
class AgentModelProfile:
    provider: str
    model: str
    temperature: float
    max_tokens: int


class AgentModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings

    def profile_for(self, agent_name: str) -> AgentModelProfile:
        alias = AGENT_MODEL_ALIASES.get(agent_name, _snake(agent_name.removesuffix("Agent")))
        provider = self._setting(f"agent_model_{alias}_provider", self._default_provider())
        model = self._setting(f"agent_model_{alias}_model", self._default_model(provider))
        temperature = float(self._setting(f"agent_model_{alias}_temperature", getattr(self.settings, "ai_temperature", 0.35)))
        max_tokens = int(self._setting(f"agent_model_{alias}_max_tokens", getattr(self.settings, "ai_max_tokens", 512)))
        return AgentModelProfile(provider=provider, model=model, temperature=temperature, max_tokens=max_tokens)

    def client_for(self, agent_name: str) -> "AgentModelGatewayClient":
        profile = self.profile_for(agent_name)
        primary = self._client(profile)
        fallback_provider = str(getattr(self.settings, "agent_model_fallback_provider", "") or "").lower()
        fallback_model = str(getattr(self.settings, "agent_model_fallback_model", "") or "")
        fallback = None
        if fallback_provider and fallback_model and (fallback_provider, fallback_model) != (profile.provider, profile.model):
            fallback = self._client(
                AgentModelProfile(fallback_provider, fallback_model, profile.temperature, profile.max_tokens)
            )
        return AgentModelGatewayClient(
            primary,
            fallback,
            key=f"{agent_name}:{profile.provider}:{profile.model}",
            failure_threshold=max(1, int(getattr(self.settings, "agent_model_circuit_breaker_failures", 3))),
            reset_seconds=max(1.0, float(getattr(self.settings, "agent_model_circuit_breaker_reset_seconds", 30.0))),
        )

    def _client(self, profile: AgentModelProfile) -> AiClient:
        settings = copy.copy(self.settings)
        settings.ai_provider = profile.provider
        settings.ai_temperature = profile.temperature
        settings.ai_max_tokens = profile.max_tokens
        if profile.provider == "openai":
            settings.openai_model = profile.model
        else:
            settings.ollama_model = profile.model
        return AiClient(settings)

    def _setting(self, name: str, fallback: Any) -> Any:
        value = getattr(self.settings, name, None)
        if value in {None, ""}:
            return fallback
        return value

    def _default_provider(self) -> str:
        return self._setting("agent_model_default_provider", getattr(self.settings, "ai_provider", "mock")).lower()

    def _default_model(self, provider: str) -> str:
        configured = self._setting("agent_model_default_model", "")
        if configured:
            return configured
        if provider == "openai":
            return getattr(self.settings, "openai_model", "gpt-4o-mini")
        if provider == "ollama":
            return getattr(self.settings, "ollama_model", "mindbridge-qwen2.5-7b-ft:latest")
        return "mock"


def _snake(value: str) -> str:
    chars = []
    for index, char in enumerate(value):
        if char.isupper() and index > 0:
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


class AgentModelGatewayClient:
    """Small model gateway with provider fallback and a process-local circuit breaker."""

    _states: dict[str, tuple[int, float]] = {}
    _lock = Lock()

    def __init__(self, primary: AiClient, fallback: AiClient | None, *, key: str, failure_threshold: int, reset_seconds: float):
        self.primary = primary
        self.fallback = fallback
        self.key = key
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds

    async def complete_async(self, messages):
        if self._is_open():
            if self.fallback is None:
                raise RuntimeError(f"model circuit open: {self.key}")
            return await self.fallback.complete_async(messages)
        try:
            result = await self.primary.complete_async(messages)
            self._success()
            return result
        except Exception:
            self._failure()
            if self.fallback is None:
                raise
            return await self.fallback.complete_async(messages)

    async def stream(self, messages):
        """Stream through the gateway without ever splicing two model outputs.

        A fallback is safe only before the primary emits its first token.  Once
        output is visible, switching models could create a contradictory or
        duplicated answer, so the error is propagated and the durable
        generation lifecycle marks the turn retryable.
        """

        if self._is_open():
            if self.fallback is None:
                raise RuntimeError(f"model circuit open: {self.key}")
            async for token in self.fallback.stream(messages):
                yield token
            return
        emitted = False
        try:
            async for token in self.primary.stream(messages):
                emitted = True
                yield token
            self._success()
        except Exception:
            self._failure()
            if self.fallback is None or emitted:
                raise
            async for token in self.fallback.stream(messages):
                yield token

    def complete(self, messages):
        if self._is_open():
            if self.fallback is None:
                raise RuntimeError(f"model circuit open: {self.key}")
            return self.fallback.complete(messages)
        try:
            result = self.primary.complete(messages)
            self._success()
            return result
        except Exception:
            self._failure()
            if self.fallback is None:
                raise
            return self.fallback.complete(messages)

    def _is_open(self) -> bool:
        with self._lock:
            failures, opened_until = self._states.get(self.key, (0, 0.0))
            if opened_until and time.monotonic() >= opened_until:
                self._states[self.key] = (0, 0.0)
                return False
            return failures >= self.failure_threshold and opened_until > time.monotonic()

    def _success(self) -> None:
        with self._lock:
            self._states[self.key] = (0, 0.0)

    def _failure(self) -> None:
        with self._lock:
            failures, _ = self._states.get(self.key, (0, 0.0))
            failures += 1
            opened_until = time.monotonic() + self.reset_seconds if failures >= self.failure_threshold else 0.0
            self._states[self.key] = (failures, opened_until)
