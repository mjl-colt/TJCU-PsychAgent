from __future__ import annotations

import logging
import uuid

from app.agents.blackboard import FlowStage
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.agents.runtime_store import SqlAlchemyRuntimeStore
from app.agents.runtime_lease import RuntimeLeaseManager
from app.models.entities import ChatSession, UserAccount


logger = logging.getLogger(__name__)


async def recover_incomplete_runtime_runs(settings) -> dict[str, int]:
    """Resume non-terminal checkpoints once when an application process starts.

    Recovery is deliberately bounded.  The same request and command ids are
    reused, so another process or a later restart can safely attempt the same
    work again under the runtime's at-least-once contract.
    """

    if not getattr(settings, "agent_runtime_recovery_enabled", True) or not getattr(
        settings,
        "agent_runtime_persistence_enabled",
        True,
    ):
        return {"scanned": 0, "recovered": 0, "awaiting_generation": 0, "failed": 0, "busy": 0}

    # Import the module rather than copying SessionLocal at import time.  This
    # also lets the engineering harness replace the session factory safely.
    from app.core import database

    limit = max(1, int(getattr(settings, "agent_runtime_recovery_scan_limit", 100)))
    db = database.SessionLocal()
    recovered = 0
    failed = 0
    busy = 0
    awaiting_generation = 0
    try:
        lease_manager = RuntimeLeaseManager(db, settings)
        lease_manager.prune_expired()
        states = SqlAlchemyRuntimeStore(
            db,
            persistence_required=getattr(settings, "agent_runtime_persistence_required", True),
        ).load_incomplete(limit)
        for state in states:
            # A READY checkpoint is intentionally incomplete: it has an
            # approved prompt but no final text.  Startup cannot push an SSE
            # response to a disconnected browser, so the same requestId must
            # resume it when the client reconnects.
            if state.flow.current_stage == FlowStage.READY_FOR_GENERATION:
                awaiting_generation += 1
                continue
            lease = lease_manager.acquire(
                state.request.request_id,
                f"recovery-{uuid.uuid4().hex}",
            )
            if lease is None:
                busy += 1
                continue
            try:
                user = db.get(UserAccount, state.request.user_id) if state.request.user_id is not None else None
                session = (
                    db.query(ChatSession)
                    .filter(ChatSession.public_id == state.request.session_id)
                    .first()
                )
                if user is None or session is None or session.user_id != user.id:
                    failed += 1
                    logger.warning(
                        "Cannot recover runtime request_id=%s: owner or session is missing",
                        state.request.request_id,
                    )
                    continue
                result = await EventDrivenAgentRuntimeService(db, settings).resume_state_async(
                    user,
                    session,
                    state,
                )
                if result.runtime_state and result.runtime_state.flow.current_stage in {
                    FlowStage.READY_FOR_GENERATION,
                    FlowStage.COMPLETED,
                }:
                    recovered += 1
                else:
                    failed += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                logger.exception(
                    "Runtime recovery failed request_id=%s: %s",
                    state.request.request_id,
                    exc,
                )
            finally:
                lease_manager.release(lease)
        return {
            "scanned": len(states),
            "recovered": recovered,
            "awaiting_generation": awaiting_generation,
            "failed": failed,
            "busy": busy,
        }
    finally:
        db.close()
