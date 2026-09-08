from __future__ import annotations

from app.agents.blackboard import BlackboardState
from app.agents.event_projection import attach_state_projection
from app.agents.runtime_guard import annotate_runtime_events, validate_runtime_transition
from app.agents.runtime_store import RuntimeStore
from app.agents.state_coordinator import BlackboardCoordinator


class GenerationLifecycle:
    """Persist the SSE generation phase as Blackboard state transitions.

    Token transport remains in ``ChatService``.  This adapter makes start,
    completion and interruption durable so ``COMPLETED`` now means that the
    final response text exists, rather than merely that its prompt is ready.
    """

    def __init__(
        self,
        coordinator: BlackboardCoordinator,
        store: RuntimeStore,
    ):
        self.coordinator = coordinator
        self.store = store

    def started(self, state: BlackboardState) -> BlackboardState:
        decision = self.coordinator.generation_started(state)
        self._persist(state, decision.state, decision.events)
        return decision.state

    def completed(
        self,
        state: BlackboardState,
        final_response: str,
        *,
        guardrail_replaced: bool = False,
        guardrail_issues: tuple[str, ...] = (),
        cited_ids: tuple[str, ...] = (),
    ) -> BlackboardState:
        decision = self.coordinator.generation_completed(
            state,
            final_response,
            guardrail_replaced=guardrail_replaced,
            guardrail_issues=guardrail_issues,
            cited_ids=cited_ids,
        )
        self._persist(state, decision.state, decision.events)
        return decision.state

    def failed(self, state: BlackboardState, message: str) -> BlackboardState:
        decision = self.coordinator.generation_failed(state, message)
        self._persist(state, decision.state, decision.events)
        return decision.state

    def _persist(self, previous: BlackboardState, current: BlackboardState, events) -> None:
        validate_runtime_transition(previous, current)
        annotated = annotate_runtime_events(current, events)
        self.store.save_many(current, attach_state_projection(current, annotated))
