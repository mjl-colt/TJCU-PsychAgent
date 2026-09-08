from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import AgentRuntimeLease, now


logger = logging.getLogger(__name__)


class RuntimeLeaseBusyError(ValueError):
    """Raised when another process still owns the same request."""


class RuntimeLeaseLostError(RuntimeError):
    """Raised when a long-running request can no longer renew its ownership."""


@dataclass(frozen=True)
class RuntimeLease:
    request_id: str
    owner_id: str
    ttl_seconds: float


class RuntimeLeaseManager:
    """Database-backed lease used to serialize one request across processes."""

    def __init__(self, db: Session, settings):
        self.db = db
        self.enabled = bool(
            getattr(settings, "agent_runtime_persistence_enabled", True)
            and getattr(settings, "agent_runtime_lease_enabled", True)
        )
        self.ttl_seconds = max(15.0, float(getattr(settings, "agent_runtime_lease_ttl_seconds", 120.0)))

    def acquire(self, request_id: str, owner_id: str) -> RuntimeLease | None:
        if not self.enabled:
            return RuntimeLease(request_id, owner_id, self.ttl_seconds)
        current = now()
        lease_until = current + timedelta(seconds=self.ttl_seconds)
        updated = (
            self.db.query(AgentRuntimeLease)
            .filter(
                AgentRuntimeLease.request_id == request_id,
                or_(AgentRuntimeLease.lease_until <= current, AgentRuntimeLease.owner_id == owner_id),
            )
            .update(
                {
                    AgentRuntimeLease.owner_id: owner_id,
                    AgentRuntimeLease.lease_until: lease_until,
                    AgentRuntimeLease.updated_at: current,
                },
                synchronize_session=False,
            )
        )
        if updated:
            self.db.commit()
            return RuntimeLease(request_id, owner_id, self.ttl_seconds)
        try:
            self.db.add(
                AgentRuntimeLease(
                    request_id=request_id,
                    owner_id=owner_id,
                    lease_until=lease_until,
                )
            )
            self.db.commit()
            return RuntimeLease(request_id, owner_id, self.ttl_seconds)
        except IntegrityError:
            self.db.rollback()
            return None

    def renew(self, lease: RuntimeLease) -> bool:
        if not self.enabled:
            return True
        current = now()
        updated = (
            self.db.query(AgentRuntimeLease)
            .filter(
                AgentRuntimeLease.request_id == lease.request_id,
                AgentRuntimeLease.owner_id == lease.owner_id,
            )
            .update(
                {
                    AgentRuntimeLease.lease_until: current + timedelta(seconds=lease.ttl_seconds),
                    AgentRuntimeLease.updated_at: current,
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return updated == 1

    def release(self, lease: RuntimeLease) -> None:
        if not self.enabled:
            return
        try:
            (
                self.db.query(AgentRuntimeLease)
                .filter(
                    AgentRuntimeLease.request_id == lease.request_id,
                    AgentRuntimeLease.owner_id == lease.owner_id,
                )
                .delete(synchronize_session=False)
            )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.warning("Runtime lease release failed request_id=%s: %s", lease.request_id, exc)

    def prune_expired(self) -> int:
        if not self.enabled:
            return 0
        try:
            deleted = (
                self.db.query(AgentRuntimeLease)
                .filter(AgentRuntimeLease.lease_until <= now())
                .delete(synchronize_session=False)
            )
            self.db.commit()
            return deleted
        except Exception as exc:
            self.db.rollback()
            logger.warning("Expired runtime lease cleanup failed: %s", exc)
            return 0
