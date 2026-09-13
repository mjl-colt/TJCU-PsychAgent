from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from app.agents.blackboard import (
    AgentCommand,
    AgentExecutionOutcome,
    AgentName,
    BlackboardState,
)
from app.agents.state_agents import AgentFallbackPolicy, StatefulAgent


class AgentDispatcher:
    """Execute a coordinator-issued batch with bounded parallelism."""

    def __init__(self, agents: list[StatefulAgent], settings, fallback_policy: AgentFallbackPolicy | None = None):
        self._agents = {agent.name: agent for agent in agents}
        self._timeout_seconds = float(getattr(settings, "agent_runtime_timeout_seconds", 12.0))
        self._safety_timeout_seconds = float(getattr(settings, "agent_runtime_safety_timeout_seconds", 6.0))
        self._max_retries = max(0, int(getattr(settings, "agent_runtime_max_retries", 1)))
        self._retry_backoff = max(0.0, float(getattr(settings, "agent_runtime_retry_backoff_seconds", 0.15)))
        self._semaphore = asyncio.Semaphore(max(1, int(getattr(settings, "agent_runtime_max_concurrency", 4))))
        self._fallback = fallback_policy or AgentFallbackPolicy()

    async def dispatch(self, commands: tuple[AgentCommand, ...], state: BlackboardState) -> list[AgentExecutionOutcome]:
        if not commands:
            return []
        return list(
            await asyncio.gather(
                *(self._execute(command, state.model_copy(deep=True)) for command in commands)
            )
        )

    async def iter_outcomes(
        self, commands: tuple[AgentCommand, ...], state: BlackboardState,
    ) -> AsyncIterator[AgentExecutionOutcome]:
        """Deliver each result immediately; the caller serializes durable writes.

        Closing/cancelling the iterator cancels and joins unfinished work, so a
        failed checkpoint cannot leave detached agents running in the background.
        """
        tasks = [asyncio.create_task(self._execute(command, state.model_copy(deep=True))) for command in commands]
        try:
            for task in asyncio.as_completed(tasks):
                yield await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, command: AgentCommand, snapshot: BlackboardState) -> AgentExecutionOutcome:
        started = time.perf_counter()
        agent = self._agents.get(command.agent)
        if agent is None:
            error = LookupError(f"agent not registered: {command.agent.value}")
            return self._fallback_outcome(command, snapshot, error, 1, started)

        timeout = self._safety_timeout_seconds if command.agent == AgentName.SAFETY else self._timeout_seconds
        last_error: Exception = RuntimeError("agent execution failed")
        attempts = 0
        async with self._semaphore:
            for attempts in range(1, self._max_retries + 2):
                try:
                    update = await asyncio.wait_for(agent.run(command, snapshot), timeout=timeout)
                    return AgentExecutionOutcome(
                        command=command,
                        update=update,
                        success=True,
                        attempts=attempts,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                except Exception as exc:
                    last_error = exc
                    if attempts <= self._max_retries and self._retry_backoff:
                        await asyncio.sleep(self._retry_backoff * (2 ** (attempts - 1)))
        return self._fallback_outcome(command, snapshot, last_error, attempts, started)

    def _fallback_outcome(
        self,
        command: AgentCommand,
        snapshot: BlackboardState,
        error: Exception,
        attempts: int,
        started: float,
    ) -> AgentExecutionOutcome:
        try:
            update = self._fallback.create(command, snapshot, error)
        except Exception as fallback_error:
            return AgentExecutionOutcome(
                command=command,
                success=False,
                attempts=max(1, attempts),
                duration_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(error).__name__}: {error}; fallback failed: {fallback_error}",
            )
        return AgentExecutionOutcome(
            command=command,
            update=update,
            success=update is not None,
            degraded=update is not None,
            attempts=max(1, attempts),
            duration_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(error).__name__}: {error}",
        )
