"""Dedicated production process for Redis Stream -> MCP tool execution."""
from __future__ import annotations

import signal
import threading

from app.core.bootstrap import create_schema
from app.core.config import get_settings
from app.services.tool_queue import get_tool_queue_worker


def main() -> None:
    create_schema()
    settings = get_settings()
    worker = get_tool_queue_worker(settings)
    worker.start()
    stop = threading.Event()

    def request_stop(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if worker.dispatcher is None:
            raise RuntimeError("Tool queue worker is disabled; set TOOL_QUEUE_ENABLED=true")
        while not stop.wait(5):
            if not worker.dispatcher.is_alive():
                raise RuntimeError("Tool queue dispatcher stopped unexpectedly")
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
