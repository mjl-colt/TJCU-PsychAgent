from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.orm import Session

from app.agents.blackboard import BlackboardState, RuntimeEvent
from app.agents.event_projection import (
    EventProjectionError,
    RuntimeEventProjector,
    attach_state_projection,
    strip_state_projection,
)
from app.models.entities import AgentRuntimeCheckpoint, AgentRuntimeEventRecord


logger = logging.getLogger(__name__)


class RuntimePersistenceError(RuntimeError):
    """Raised when a required durable transition cannot be committed."""


class RuntimeStore(Protocol):
    def save(self, state: BlackboardState, event: RuntimeEvent) -> None:
        ...

    def save_many(self, state: BlackboardState, events: Sequence[RuntimeEvent]) -> None:
        ...

    def load(self, request_id: str) -> BlackboardState | None:
        ...

    def load_events(self, request_id: str) -> list[RuntimeEvent]:
        ...

    def load_incomplete(self, limit: int = 100) -> list[BlackboardState]:
        ...


class NullRuntimeStore:
    def save(self, state: BlackboardState, event: RuntimeEvent) -> None:
        return None

    def save_many(self, state: BlackboardState, events: Sequence[RuntimeEvent]) -> None:
        return None

    def load(self, request_id: str) -> BlackboardState | None:
        return None

    def load_events(self, request_id: str) -> list[RuntimeEvent]:
        return []

    def load_incomplete(self, limit: int = 100) -> list[BlackboardState]:
        return []


class SqlAlchemyRuntimeStore:
    """Durable event journal and latest-state checkpoint.

    Production mode is fail-closed by default: the runtime must not acknowledge
    a transition that cannot be recovered after a process crash.
    """

    def __init__(self, db: Session, persistence_required: bool = True):
        self.db = db
        self.persistence_required = persistence_required

    def save(self, state: BlackboardState, event: RuntimeEvent) -> None:
        self.save_many(state, (event,))

    def save_many(self, state: BlackboardState, events: Sequence[RuntimeEvent]) -> None:
        """Persist one state transition and all its audit events in one transaction."""

        try:
            persisted_state = state
            checkpoint = (
                self.db.query(AgentRuntimeCheckpoint)
                .filter(AgentRuntimeCheckpoint.request_id == state.request.request_id)
                .first()
            )
            revision_advanced = checkpoint is None or state.revision > checkpoint.state_version
            persisted_events = (
                attach_state_projection(persisted_state, events)
                if revision_advanced
                else strip_state_projection(events)
            )
            event_ids = [event.event_id for event in persisted_events]
            existing_ids: set[str] = set()
            if event_ids:
                existing_ids = {
                    row[0]
                    for row in self.db.query(AgentRuntimeEventRecord.event_id)
                    .filter(AgentRuntimeEventRecord.event_id.in_(event_ids))
                    .all()
                }
            for event in persisted_events:
                if event.event_id in existing_ids:
                    continue
                self.db.add(
                    AgentRuntimeEventRecord(
                        event_id=event.event_id,
                        request_id=event.request_id,
                        event_type=event.type.value,
                        actor=event.actor,
                        batch_id=event.batch_id,
                        command_id=event.command_id,
                        payload_json=event.model_dump_json(),
                    )
                )
            if checkpoint is None:
                checkpoint = AgentRuntimeCheckpoint(
                    request_id=state.request.request_id,
                    session_public_id=state.request.session_id,
                    state_version=state.revision,
                    stage=state.flow.current_stage.value,
                    completed=_checkpoint_is_closed(state),
                    state_json=persisted_state.model_dump_json(),
                )
                self.db.add(checkpoint)
            elif state.revision >= checkpoint.state_version:
                checkpoint.state_version = state.revision
                checkpoint.stage = state.flow.current_stage.value
                checkpoint.completed = _checkpoint_is_closed(state)
                checkpoint.state_json = persisted_state.model_dump_json()
                from app.models.entities import now

                checkpoint.updated_at = now()
                self.db.add(checkpoint)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.warning("Agent runtime checkpoint unavailable: %s", exc)
            if self.persistence_required:
                raise RuntimePersistenceError("无法持久化 Agent Runtime 状态") from exc

    def load(self, request_id: str) -> BlackboardState | None:
        try:
            checkpoint = (
                self.db.query(AgentRuntimeCheckpoint)
                .filter(AgentRuntimeCheckpoint.request_id == request_id)
                .first()
            )
            checkpoint_state = BlackboardState.model_validate_json(checkpoint.state_json) if checkpoint else None
            events = self._load_events(request_id)
            try:
                projection = RuntimeEventProjector().rebuild(events)
            except EventProjectionError as exc:
                logger.error("Runtime event projection invalid request_id=%s: %s", request_id, exc)
                projection = None
            if projection is not None and (
                checkpoint_state is None or projection.state.revision >= checkpoint_state.revision
            ):
                return projection.state
            return checkpoint_state
        except Exception as exc:
            self.db.rollback()
            logger.warning("Agent runtime checkpoint load unavailable: %s", exc)
            return None

    def load_events(self, request_id: str) -> list[RuntimeEvent]:
        try:
            return self._load_events(request_id)
        except Exception as exc:
            self.db.rollback()
            logger.warning("Agent runtime event replay unavailable: %s", exc)
            return []

    def _load_events(self, request_id: str) -> list[RuntimeEvent]:
        rows = (
            self.db.query(AgentRuntimeEventRecord)
            .filter(AgentRuntimeEventRecord.request_id == request_id)
            .order_by(AgentRuntimeEventRecord.id.asc())
            .all()
        )
        events: list[RuntimeEvent] = []
        for row in rows:
            payload = json.loads(row.payload_json)
            # Checkpoints created before 2026-09 stored a duplicate Inbox
            # lifecycle. It never carried dispatch ownership, so it can be
            # ignored while preserving every business and recovery event.
            if payload.get("type") in {
                "INBOX_INSERTED",
                "INBOX_CLAIMED",
                "INBOX_COMPLETED",
                "INBOX_DISCARDED",
            }:
                continue
            events.append(RuntimeEvent.model_validate(payload))
        return events

    def load_incomplete(self, limit: int = 100) -> list[BlackboardState]:
        try:
            rows = (
                self.db.query(AgentRuntimeCheckpoint)
                .filter(AgentRuntimeCheckpoint.completed.is_(False))
                .order_by(AgentRuntimeCheckpoint.updated_at.asc())
                .limit(max(1, limit))
                .all()
            )
            states = []
            for row in rows:
                try:
                    states.append(BlackboardState.model_validate_json(row.state_json))
                except Exception as exc:
                    logger.warning("Skipping invalid runtime checkpoint request_id=%s: %s", row.request_id, exc)
            return states
        except Exception as exc:
            self.db.rollback()
            logger.warning("Incomplete runtime checkpoint scan unavailable: %s", exc)
            return []


def _checkpoint_is_closed(state: BlackboardState) -> bool:
    """The indexed database flag is a projection of the canonical flow stage."""

    return state.flow.current_stage.value in {"COMPLETED", "FAILED"}
