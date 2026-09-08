"""Deterministic dispatcher benchmark; it measures scheduling, not model inference."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.blackboard import (
    AgentAction,
    AgentCommand,
    AgentName,
    AgentStateUpdate,
    BlackboardSection,
    BlackboardState,
    SafetyState,
    UnderstandingState,
)
from app.agents.dispatcher import AgentDispatcher
from app.core.enums import IntentType, RiskLevel


class DelayedUnderstanding:
    name = AgentName.UNDERSTANDING

    async def run(self, command, state):
        await asyncio.sleep(0.08)
        return AgentStateUpdate(
            section=BlackboardSection.UNDERSTANDING,
            data=UnderstandingState(intent=IntentType.CONSULT, topic="benchmark"),
        )


class DelayedSafety:
    name = AgentName.SAFETY

    async def run(self, command, state):
        await asyncio.sleep(0.12)
        return AgentStateUpdate(
            section=BlackboardSection.SAFETY,
            data=SafetyState(risk_level=RiskLevel.LOW, assessment_method="BENCHMARK"),
        )


async def timed(run):
    started = time.perf_counter()
    await run()
    return (time.perf_counter() - started) * 1000


async def main(rounds: int = 10):
    settings = SimpleNamespace(
        agent_runtime_max_concurrency=4,
        agent_runtime_timeout_seconds=1.0,
        agent_runtime_safety_timeout_seconds=1.0,
        agent_runtime_max_retries=0,
        agent_runtime_retry_backoff_seconds=0.0,
    )
    dispatcher = AgentDispatcher([DelayedUnderstanding(), DelayedSafety()], settings)
    state = BlackboardState.create("benchmark")
    commands = (
        AgentCommand(batch_id="benchmark", agent=AgentName.UNDERSTANDING, action=AgentAction.UNDERSTAND, state_revision=0),
        AgentCommand(batch_id="benchmark", agent=AgentName.SAFETY, action=AgentAction.ASSESS_RISK, state_revision=0),
    )
    serial = []
    parallel = []
    for _ in range(rounds):
        serial.append(
            await timed(
                lambda: _serial(dispatcher, commands, state)
            )
        )
        parallel.append(await timed(lambda: dispatcher.dispatch(commands, state)))
    serial_p50 = statistics.median(serial)
    parallel_p50 = statistics.median(parallel)
    print(
        json.dumps(
            {
                "rounds": rounds,
                "workload": {"understandingMs": 80, "safetyMs": 120},
                "serialP50Ms": round(serial_p50, 2),
                "parallelP50Ms": round(parallel_p50, 2),
                "stageLatencyReductionPct": round((serial_p50 - parallel_p50) / serial_p50 * 100, 2),
                "speedup": round(serial_p50 / parallel_p50, 2),
                "note": "synthetic async I/O benchmark; run a separate Ollama/GPU load test",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def _serial(dispatcher, commands, state):
    for command in commands:
        await dispatcher.dispatch((command,), state)


if __name__ == "__main__":
    asyncio.run(main())
