from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.agent_models import AgentModelRegistry

if TYPE_CHECKING:
    from app.models.entities import ChatSession, UserAccount
    from app.services.memory import RedisShortTermMemoryStore


@dataclass
class AgentRuntimeServices:
    db: Session
    settings: Settings
    user: UserAccount
    session: ChatSession
    model_registry: AgentModelRegistry
    memory: RedisShortTermMemoryStore
