from __future__ import annotations

import asyncio

from app.agents.blackboard import RuntimeEvent


class InMemoryEventBus:
    """Per-turn event mailbox.

    The durable copy lives in ``RuntimeStore``.  A per-turn mailbox avoids
    cross-request event stealing and lets the HTTP request await its result
    without polling Blackboard state.
    """

    def __init__(self):
        self._queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue()

    async def publish(self, event: RuntimeEvent) -> None:
        await self._queue.put(event)

    async def consume(self) -> RuntimeEvent:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()
