import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.tool_queue import get_tool_queue_worker
from app.agents.recovery import recover_incomplete_runtime_runs


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="心理ai", version="0.1.0")

    @app.middleware("http")
    async def no_cache_frontend_assets(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.on_event("startup")
    async def startup() -> None:
        create_schema()
        db = SessionLocal()
        try:
            seed_data(db)
        finally:
            db.close()
        worker = get_tool_queue_worker(get_settings())
        worker.start()
        app.state.tool_queue_worker = worker
        recovery_task = asyncio.create_task(
            recover_incomplete_runtime_runs(get_settings()),
            name="mindbridge-runtime-recovery",
        )
        recovery_task.add_done_callback(_log_recovery_result)
        app.state.runtime_recovery_task = recovery_task

    @app.on_event("shutdown")
    async def shutdown() -> None:
        recovery_task = getattr(app.state, "runtime_recovery_task", None)
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovery_task
        worker = getattr(app.state, "tool_queue_worker", None)
        if worker is not None:
            worker.stop()

    app.include_router(router)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()


def _log_recovery_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        result = task.result()
    except Exception:
        logger.exception("Runtime startup recovery crashed")
        return
    if result["scanned"]:
        logger.info("Runtime startup recovery result: %s", result)
