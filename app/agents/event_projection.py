from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

from app.agents.blackboard import BlackboardState, RuntimeEvent, RuntimeEventType


class EventProjectionError(RuntimeError):
    """Raised when durable events cannot produce a trustworthy Blackboard view."""


@dataclass(frozen=True)
class ProjectionResult:
    state: BlackboardState
    event_id: str
    projected_event_count: int


def state_projection_payload(state: BlackboardState) -> dict:
    """Build the privacy-safe projection stored beside a runtime event."""

    return state.model_dump(mode="json")


def state_projection_hash(payload: dict) -> str:
    state = BlackboardState.model_validate(payload)
    return hashlib.sha256(state.model_dump_json().encode("utf-8")).hexdigest()


def attach_state_projection(state: BlackboardState, events: Iterable[RuntimeEvent]) -> tuple[RuntimeEvent, ...]:
    """Attach one current-state projection to the final event in a transition."""

    values = list(events)
    if not values:
        return ()
    payload = state_projection_payload(state)
    metadata = dict(values[-1].metadata)
    metadata.update(
        {
            "stateRevision": state.revision,
            "projectionVersion": 1,
            "stateProjectionHash": state_projection_hash(payload),
        }
    )
    values[-1] = values[-1].model_copy(update={"metadata": metadata, "state_projection": payload})
    return tuple(values)


def strip_state_projection(events: Iterable[RuntimeEvent]) -> tuple[RuntimeEvent, ...]:
    """Remove a caller-attached projection when the revision did not advance."""

    values = []
    for event in events:
        metadata = dict(event.metadata)
        for key in ("stateRevision", "projectionVersion", "stateProjectionHash"):
            metadata.pop(key, None)
        values.append(event.model_copy(update={"metadata": metadata, "state_projection": None}))
    return tuple(values)


class RuntimeEventProjector:
    """Rebuild the latest Blackboard read model from append-only events."""

    def rebuild(self, events: Iterable[RuntimeEvent]) -> ProjectionResult | None:
        latest: BlackboardState | None = None
        latest_event_id = ""
        projected_count = 0
        request_id: str | None = None
        for event in events:
            if request_id is None:
                request_id = event.request_id
            elif event.request_id != request_id:
                raise EventProjectionError("事件流混入了其他 requestId")
            if event.state_projection is None:
                continue
            flow_payload = event.state_projection.get("flow", {})
            if isinstance(flow_payload, dict) and "inbox" in flow_payload:
                # Inbox was removed because it duplicated flow and lease
                # state. Its historical hash was calculated with that field,
                # so use the matching transactional checkpoint instead of
                # misreporting a schema migration as event tampering.
                continue
            projected = BlackboardState.model_validate(event.state_projection)
            if projected.request.request_id != event.request_id:
                raise EventProjectionError("事件和 Blackboard 投影的 requestId 不一致")
            if latest is not None and projected.revision < latest.revision:
                raise EventProjectionError("Blackboard 投影 revision 发生回退")
            expected_hash = str(event.metadata.get("stateProjectionHash", ""))
            if expected_hash and expected_hash != state_projection_hash(event.state_projection):
                raise EventProjectionError("Blackboard 投影 hash 校验失败")
            latest = projected
            latest_event_id = event.event_id
            projected_count += 1
        if latest is None:
            return None
        return ProjectionResult(latest, latest_event_id, projected_count)


def interrupted_compaction_ids(events: Iterable[RuntimeEvent]) -> tuple[str, ...]:
    """Return compactions that have a durable start without a terminal event."""

    opened: dict[str, None] = {}
    for event in events:
        compaction_id = str(event.metadata.get("compactionId", "") or event.command_id)
        if not compaction_id:
            continue
        if event.type == RuntimeEventType.CONTEXT_COMPACTION_STARTED:
            opened[compaction_id] = None
        elif event.type in {
            RuntimeEventType.CONTEXT_COMPACTION_COMPLETED,
            RuntimeEventType.CONTEXT_COMPACTION_FAILED,
        }:
            opened.pop(compaction_id, None)
    return tuple(opened)
