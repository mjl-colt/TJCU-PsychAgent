"""Reliable asynchronous tool execution: transactional outbox -> Redis Stream -> MCP.

``tool_jobs`` remains the business source of truth. Redis only transports a
job id and is intentionally at-least-once; an atomic state claim in MySQL makes
duplicate stream deliveries harmless. The outbox closes the database/Redis
dual-write gap: a process crash before ``XADD`` leaves a durable PENDING row
that a worker publishes after restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
import uuid
from collections import deque
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import (
    DeadLetterRecord,
    ExcelRecord,
    PsychologicalReport,
    RiskCase,
    ToolJob,
    ToolOutbox,
    now,
)
from app.services.mcp_client import McpToolError, MindBridgeMcpToolClient
from app.services.tool_governance import ToolGovernanceService
from app.services.tools import ToolOrchestrationService


logger = logging.getLogger(__name__)

OUTBOX_PENDING = "PENDING"
OUTBOX_PUBLISHING = "PUBLISHING"
OUTBOX_PUBLISHED = "PUBLISHED"


class NonRetryableToolError(RuntimeError):
    """A policy or payload failure that must go directly to the dead letter."""


def _schedule_outbox(db: Session, job: ToolJob, *, available_at=None, reason: str = "") -> ToolOutbox:
    """Create or re-arm the one outbox record belonging to a ToolJob.

    Re-arming a previously published record is how delayed retries are put back
    onto the stream. A unique job_id is the durable deduplication key.
    """
    if job.id is None:
        db.flush()
    due_at = available_at or job.run_after or now()
    outbox = db.query(ToolOutbox).filter(ToolOutbox.job_id == job.id).first()
    if outbox is None:
        outbox = ToolOutbox(job_id=job.id, status=OUTBOX_PENDING, available_at=due_at, last_error=reason)
        db.add(outbox)
        return outbox
    outbox.status = OUTBOX_PENDING
    outbox.stream_message_id = ""
    outbox.available_at = due_at
    outbox.published_at = None
    outbox.last_error = reason
    outbox.updated_at = now()
    db.add(outbox)
    return outbox


class ToolQueueService:
    """Creates business ToolJobs and their outbox records in one DB transaction."""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def enqueue_report(self, report_id: int, risk_level: str | None, *, commit: bool = True) -> list[ToolJob]:
        excel_job = self._find_or_create(ToolJobKind.EXCEL_REPORT.value, report_id)
        jobs = [excel_job]
        case_job = None
        if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
            case_job = self._find_or_create(ToolJobKind.CASE_CREATE.value, report_id)
            jobs.append(case_job)
        if risk_level == RiskLevel.HIGH.value:
            jobs.append(self._find_or_create(ToolJobKind.ALERT_SEND.value, report_id, case_job.id if case_job else None))
        for job in jobs:
            if job.status == ToolJobStatus.PENDING.value:
                _schedule_outbox(self.db, job)
        if commit:
            self.db.commit()
        return jobs

    def _find_or_create(self, kind: str, report_id: int, depends_on_job_id: int | None = None) -> ToolJob:
        existing = (
            self.db.query(ToolJob)
            .filter(ToolJob.report_id == report_id, ToolJob.kind == kind)
            .filter(ToolJob.status.in_([ToolJobStatus.PENDING.value, ToolJobStatus.RUNNING.value, ToolJobStatus.SUCCESS.value]))
            .first()
        )
        if existing is not None:
            if (depends_on_job_id is not None
                    and existing.status == ToolJobStatus.PENDING.value
                    and existing.depends_on_job_id != depends_on_job_id):
                # A manual re-dispatch may replace a dead predecessor. Keep a
                # still-pending child attached to the new dependency.
                existing.depends_on_job_id = depends_on_job_id
                existing.updated_at = now()
                self.db.add(existing)
            return existing
        job = ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self.settings.tool_queue_max_attempts,
            depends_on_job_id=depends_on_job_id,
            run_after=now(),
            last_error="",
        )
        self.db.add(job)
        self.db.flush()
        return job


class RateLimiter:
    """Bounded local limiter for alert sends; the broker remains durable."""

    def __init__(self, limit_per_minute: int):
        self.limit = max(0, limit_per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        current = time.monotonic()
        with self.lock:
            while self.events and current - self.events[0] >= 60.0:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(current)
                return True, 0.0
            return False, max(1.0, 60.0 - (current - self.events[0]))


class ToolQueueWorker:
    """Redis Stream consumer with MySQL-backed state, retry, audit and idempotency."""

    def __init__(self, settings: Settings, *, redis_client=None):
        self.settings = settings
        self.stop_event = threading.Event()
        self.dispatcher: threading.Thread | None = None
        self._redis_client = redis_client
        self._group_ready = False
        configured_name = str(getattr(settings, "tool_queue_consumer_name", "")).strip()
        self.consumer_name = configured_name or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.stream = str(getattr(settings, "tool_queue_stream", "mindbridge:tool-jobs"))
        self.group = str(getattr(settings, "tool_queue_consumer_group", "mindbridge-tool-workers"))
        self.alert_limiter = RateLimiter(getattr(settings, "alert_email_rate_limit_per_minute", 30))
        self._last_maintenance_at = 0.0

    def start(self) -> None:
        if (not getattr(self.settings, "tool_queue_enabled", True)
                or not getattr(self.settings, "tool_queue_worker_enabled", True)
                or self.dispatcher is not None):
            return
        if str(getattr(self.settings, "tool_queue_backend", "redis_stream")).lower() != "redis_stream":
            raise RuntimeError("生产工具 Worker 仅支持 tool_queue_backend=redis_stream")
        self._recover_running_jobs()
        self._backfill_pending_outbox()
        self.stop_event.clear()
        self.dispatcher = threading.Thread(target=self._loop, name="mindbridge-tool-stream-worker", daemon=True)
        self.dispatcher.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.dispatcher is not None:
            self.dispatcher.join(timeout=5)
            self.dispatcher = None

    def _loop(self) -> None:
        retry_delay = max(0.2, float(getattr(self.settings, "tool_queue_poll_interval_seconds", 1.0)))
        while not self.stop_event.is_set():
            try:
                self._run_maintenance_if_due()
                self._publish_pending_outbox()
                self._claim_stale_messages()
                self._consume_once()
            except Exception as exc:
                if "NOGROUP" in str(exc).upper():
                    # Redis may have restarted from a snapshot that predates
                    # the consumer group. Recreate it on the next iteration.
                    self._group_ready = False
                logger.warning("Tool Stream worker unavailable; outbox will retry: %s", exc, exc_info=True)
                self.stop_event.wait(retry_delay)

    def _run_maintenance_if_due(self) -> None:
        """Run expensive recovery scans at a bounded frequency."""
        current = time.monotonic()
        interval = max(1.0, float(getattr(
            self.settings, "tool_queue_reconcile_interval_seconds", 30.0,
        )))
        if current - self._last_maintenance_at < interval:
            return
        # Throttle scans even if Redis is currently unavailable.
        self._last_maintenance_at = current
        self._recover_running_jobs()
        self._recover_stuck_outbox()
        self._backfill_pending_outbox()
        self._reconcile_unconfirmed_outbox()

    def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - requirements pins redis
            raise RuntimeError("缺少 redis 依赖，无法消费工具 Stream") from exc
        self._redis_client = redis.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_timeout=max(1.0, float(getattr(self.settings, "redis_socket_timeout_seconds", 2.0))),
            socket_connect_timeout=max(1.0, float(getattr(self.settings, "redis_socket_timeout_seconds", 2.0))),
        )
        return self._redis_client

    def _ensure_group(self, client) -> None:
        if self._group_ready:
            return
        try:
            client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc).upper():
                raise
        self._group_ready = True

    def _publish_pending_outbox(self) -> int:
        """Publish claimed outbox rows. A crash may duplicate XADD, never lose it."""
        client = self._redis()
        published = 0
        db = SessionLocal()
        try:
            current = now()
            lease_until = current + timedelta(seconds=max(
                5.0, float(getattr(self.settings, "tool_queue_outbox_publish_lease_seconds", 30.0)),
            ))
            candidates = (
                db.query(ToolOutbox.id)
                .filter(ToolOutbox.status == OUTBOX_PENDING, ToolOutbox.available_at <= current)
                .order_by(ToolOutbox.id.asc())
                .limit(max(1, int(getattr(self.settings, "tool_queue_outbox_batch_size", 100))))
                .all()
            )
            for (outbox_id,) in candidates:
                claimed = (
                    db.query(ToolOutbox)
                    .filter(ToolOutbox.id == outbox_id, ToolOutbox.status == OUTBOX_PENDING, ToolOutbox.available_at <= current)
                    .update({
                        ToolOutbox.status: OUTBOX_PUBLISHING,
                        ToolOutbox.available_at: lease_until,
                        ToolOutbox.updated_at: current,
                    }, synchronize_session=False)
                )
                db.commit()
                if claimed != 1:
                    continue
                outbox = db.get(ToolOutbox, outbox_id)
                if outbox is None:
                    continue
                try:
                    message_id = client.xadd(
                        self.stream,
                        {
                            "schema": "tool-job-v1",
                            "job_id": str(outbox.job_id),
                            "outbox_id": str(outbox.id),
                        },
                        maxlen=max(1_000, int(getattr(self.settings, "tool_queue_stream_maxlen", 100_000))),
                        approximate=True,
                    )
                except Exception as exc:
                    outbox.status = OUTBOX_PENDING
                    outbox.attempts += 1
                    outbox.available_at = now() + timedelta(seconds=1)
                    outbox.last_error = f"Redis XADD failed: {type(exc).__name__}: {exc}"
                    outbox.updated_at = now()
                    db.add(outbox)
                    db.commit()
                    raise
                outbox.status = OUTBOX_PUBLISHED
                outbox.stream_message_id = _text(message_id)
                outbox.published_at = now()
                outbox.last_error = ""
                outbox.updated_at = now()
                db.add(outbox)
                db.commit()
                published += 1
            return published
        finally:
            db.close()

    def _consume_once(self) -> int:
        client = self._redis()
        self._ensure_group(client)
        records = client.xreadgroup(
            self.group,
            self.consumer_name,
            {self.stream: ">"},
            count=max(1, int(getattr(self.settings, "tool_queue_batch_size", 10))),
            block=max(1, int(getattr(self.settings, "tool_queue_stream_block_ms", 1000))),
        )
        processed = 0
        for _, messages in records or []:
            for message_id, payload in messages:
                self._process_stream_message(client, message_id, payload)
                processed += 1
        return processed

    def _claim_stale_messages(self) -> int:
        client = self._redis()
        self._ensure_group(client)
        try:
            claimed = client.xautoclaim(
                self.stream,
                self.group,
                self.consumer_name,
                min_idle_time=max(1_000, int(float(getattr(self.settings, "tool_queue_claim_idle_seconds", 60.0)) * 1000)),
                start_id="0-0",
                count=max(1, int(getattr(self.settings, "tool_queue_batch_size", 10))),
            )
        except AttributeError:  # lightweight test clients or older Redis clients
            return 0
        messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) >= 2 else []
        for message_id, payload in messages or []:
            self._process_stream_message(client, message_id, payload)
        return len(messages or [])

    def _process_stream_message(self, client, message_id, payload: dict) -> None:
        raw_job_id = _text(payload.get("job_id") if isinstance(payload, dict) else None)
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            logger.error("Ignoring malformed tool stream message id=%s payload=%s", message_id, payload)
            self._ack(client, message_id)
            return

        db = SessionLocal()
        try:
            job = self._claim_job(db, job_id)
            if job is None:
                # Duplicate delivery, already-completed job, or a delayed stale
                # stream message. The DB state is authoritative.
                self._ack(client, message_id)
                return
            dependency_error = self._dependency_terminal_error(db, job)
            if dependency_error:
                self._dead_letter(db, job, NonRetryableToolError(dependency_error))
                self._ack(client, message_id)
                return
            if not self._dependency_ready(db, job):
                self._requeue(db, job, self._dependency_wait_reason(job), 2.0)
                self._ack(client, message_id)
                return
            if job.kind in {ToolJobKind.RISK_ALERT.value, ToolJobKind.ALERT_SEND.value}:
                allowed, retry_after = self.alert_limiter.allow()
                if not allowed:
                    self._requeue(db, job, "邮件预警限流中，稍后重试", retry_after)
                    self._ack(client, message_id)
                    return
            self._start_attempt(db, job)
            try:
                self._execute(db, job)
            except NonRetryableToolError as exc:
                self._dead_letter(db, job, exc)
            except Exception as exc:
                self._fail_or_dead_letter(db, job.id, exc)
            else:
                persisted = db.get(ToolJob, job.id)
                if persisted is not None:
                    persisted.status = ToolJobStatus.SUCCESS.value
                    persisted.last_error = ""
                    persisted.updated_at = now()
                    db.add(persisted)
                    db.commit()
            self._ack(client, message_id)
        finally:
            db.close()

    def _claim_job(self, db: Session, job_id: int) -> ToolJob | None:
        current = now()
        claimed = (
            db.query(ToolJob)
            .filter(ToolJob.id == job_id, ToolJob.status == ToolJobStatus.PENDING.value, ToolJob.run_after <= current)
            .update({
                ToolJob.status: ToolJobStatus.RUNNING.value,
                ToolJob.updated_at: current,
            }, synchronize_session=False)
        )
        db.commit()
        return db.get(ToolJob, job_id) if claimed == 1 else None

    @staticmethod
    def _start_attempt(db: Session, job: ToolJob) -> None:
        """Count only a real tool invocation, not dependency or rate-limit waits."""
        job.attempts += 1
        job.updated_at = now()
        db.add(job)
        db.commit()

    def _execute(self, db: Session, job: ToolJob) -> None:
        report = db.get(PsychologicalReport, job.report_id)
        if report is None:
            raise NonRetryableToolError(f"report {job.report_id} not found")
        governance = ToolGovernanceService(db)
        audit = governance.start_job(job, report)
        if not audit.allowed:
            raise NonRetryableToolError(audit.reason)
        try:
            if bool(getattr(self.settings, "tool_queue_mcp_enabled", True)):
                result = self._execute_via_mcp(db, job)
            else:
                result = self._execute_internal(db, job, report)
        except Exception as exc:
            governance.finish(audit, "FAILED", f"{type(exc).__name__}: {exc}")
            raise
        governance.finish(audit, "SUCCESS", payload={
            "result": result,
            "transport": "mcp" if bool(getattr(self.settings, "tool_queue_mcp_enabled", True)) else "internal",
        })

    def _execute_via_mcp(self, db: Session, job: ToolJob) -> str:
        case_id = None
        if job.kind == ToolJobKind.ALERT_SEND.value:
            case = db.query(RiskCase).filter(RiskCase.report_id == job.report_id).first()
            if case is None:
                raise RuntimeError("ALERT_SEND 等待的风险个案不存在")
            case_id = case.id
        try:
            return asyncio.run(MindBridgeMcpToolClient(self.settings).execute_job(
                job.kind, job.report_id, case_id=case_id,
            ))
        except McpToolError:
            raise
        except Exception as exc:
            raise McpToolError(f"MCP worker 调用失败：{type(exc).__name__}: {exc}") from exc

    def _execute_internal(self, db: Session, job: ToolJob, report: PsychologicalReport) -> str:
        """Explicit development fallback; production defaults to the MCP path."""
        tools = ToolOrchestrationService(db, self.settings)
        if job.kind == ToolJobKind.EXCEL_REPORT.value:
            record = tools.write_excel(report)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return record.message
        if job.kind == ToolJobKind.CASE_CREATE.value:
            return f"caseId={tools.create_case(report).id}"
        if job.kind == ToolJobKind.ALERT_SEND.value:
            record = tools.send_case_alert(tools.create_case(report))
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return record.message
        if job.kind == ToolJobKind.RISK_ALERT.value:
            record = tools.notify(report)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return record.message
        raise NonRetryableToolError(f"unknown tool job kind: {job.kind}")

    def _dependency_ready(self, db: Session, job: ToolJob) -> bool:
        if job.kind not in {ToolJobKind.RISK_ALERT.value, ToolJobKind.ALERT_SEND.value}:
            return True
        if job.depends_on_job_id:
            dependency = db.get(ToolJob, job.depends_on_job_id)
            return dependency is not None and dependency.status == ToolJobStatus.SUCCESS.value
        if job.kind == ToolJobKind.ALERT_SEND.value:
            return db.query(RiskCase).filter(RiskCase.report_id == job.report_id).first() is not None
        return (
            db.query(ExcelRecord)
            .filter(ExcelRecord.report_id == job.report_id, ExcelRecord.status == ToolStatus.SUCCESS.value)
            .first() is not None
        )

    @staticmethod
    def _dependency_terminal_error(db: Session, job: ToolJob) -> str | None:
        if not job.depends_on_job_id:
            return None
        dependency = db.get(ToolJob, job.depends_on_job_id)
        if dependency is None:
            return f"依赖任务 {job.depends_on_job_id} 不存在"
        if dependency.status == ToolJobStatus.DEAD.value:
            return f"依赖任务 {dependency.id} 已进入死信"
        return None

    @staticmethod
    def _dependency_wait_reason(job: ToolJob) -> str:
        return "等待风险个案创建成功后再发送预警" if job.kind == ToolJobKind.ALERT_SEND.value else "等待 Excel 台账写入成功后再发送预警"

    def _requeue(self, db: Session, job: ToolJob, reason: str, delay_seconds: float) -> None:
        job.status = ToolJobStatus.PENDING.value
        job.last_error = reason
        job.run_after = now() + timedelta(seconds=max(1.0, delay_seconds))
        job.updated_at = now()
        db.add(job)
        _schedule_outbox(db, job, available_at=job.run_after, reason=reason)
        db.commit()

    def _fail_or_dead_letter(self, db: Session, job_id: int, exc: Exception) -> None:
        # A failed audit/tool commit can leave the Session in a failed
        # transaction. Roll it back before recording retry state.
        db.rollback()
        job = db.get(ToolJob, job_id)
        if job is None:
            return
        message = f"{type(exc).__name__}: {exc}"
        if job.attempts >= job.max_attempts:
            self._dead_letter(db, job, exc)
            return
        job.status = ToolJobStatus.PENDING.value
        job.last_error = message
        job.run_after = now() + timedelta(seconds=float(getattr(
            self.settings, "tool_queue_retry_delay_seconds", 15.0,
        )) * max(1, job.attempts))
        job.updated_at = now()
        db.add(job)
        _schedule_outbox(db, job, available_at=job.run_after, reason=message)
        db.commit()

    @staticmethod
    def _dead_letter(db: Session, job: ToolJob, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        job.status = ToolJobStatus.DEAD.value
        job.last_error = message
        job.updated_at = now()
        db.add(job)
        db.add(DeadLetterRecord(
            job_id=job.id,
            report_id=job.report_id,
            kind=job.kind,
            reason=message,
            payload=json.dumps({"reportId": job.report_id, "kind": job.kind, "attempts": job.attempts}, ensure_ascii=False),
        ))
        db.commit()

    def _recover_running_jobs(self) -> None:
        db = SessionLocal()
        try:
            timeout = max(30.0, float(getattr(
                self.settings, "tool_queue_running_timeout_seconds", 120.0,
            )))
            stale_before = now() - timedelta(seconds=timeout)
            rows = db.query(ToolJob).filter(
                ToolJob.status == ToolJobStatus.RUNNING.value,
                ToolJob.updated_at <= stale_before,
            ).all()
            for job in rows:
                job.status = ToolJobStatus.PENDING.value
                job.last_error = "Worker 执行租约超时，恢复未完成任务"
                job.run_after = now()
                job.updated_at = now()
                db.add(job)
                _schedule_outbox(db, job, available_at=job.run_after, reason=job.last_error)
            db.commit()
        finally:
            db.close()

    def _backfill_pending_outbox(self) -> None:
        db = SessionLocal()
        try:
            for job in db.query(ToolJob).filter(ToolJob.status == ToolJobStatus.PENDING.value).all():
                outbox = db.query(ToolOutbox).filter(ToolOutbox.job_id == job.id).first()
                if outbox is None:
                    _schedule_outbox(db, job, available_at=job.run_after, reason="startup backfill")
            db.commit()
        finally:
            db.close()

    def _recover_stuck_outbox(self) -> None:
        db = SessionLocal()
        try:
            current = now()
            db.query(ToolOutbox).filter(
                ToolOutbox.status == OUTBOX_PUBLISHING,
                ToolOutbox.available_at <= current,
            ).update({
                ToolOutbox.status: OUTBOX_PENDING,
                ToolOutbox.last_error: "outbox publish lease expired; retrying",
                ToolOutbox.updated_at: current,
            }, synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def _reconcile_unconfirmed_outbox(self) -> None:
        """Re-publish jobs that were marked published before Redis lost data.

        This is intentionally conservative: only a still-PENDING ToolJob is
        re-armed, and the DB claim makes any duplicate Stream message harmless.
        """
        client = self._redis()
        db = SessionLocal()
        try:
            threshold = now() - timedelta(seconds=max(
                10.0, float(getattr(self.settings, "tool_queue_claim_idle_seconds", 60.0)),
            ))
            rows = (
                db.query(ToolOutbox)
                .join(ToolJob, ToolJob.id == ToolOutbox.job_id)
                .filter(
                    ToolOutbox.status == OUTBOX_PUBLISHED,
                    ToolOutbox.published_at <= threshold,
                    ToolJob.status == ToolJobStatus.PENDING.value,
                    ToolJob.run_after <= now(),
                )
                .all()
            )
            for outbox in rows:
                # A slow or temporarily offline consumer is not message loss.
                # Re-publish only when the exact message id has disappeared
                # from the Stream (for example after Redis restore/trim).
                existing = client.xrange(
                    self.stream,
                    min=outbox.stream_message_id,
                    max=outbox.stream_message_id,
                    count=1,
                ) if outbox.stream_message_id else []
                if existing and _text(existing[0][0]) == outbox.stream_message_id:
                    continue
                outbox.status = OUTBOX_PENDING
                outbox.stream_message_id = ""
                outbox.available_at = now()
                outbox.last_error = "job remains pending; reconciling broker delivery"
                outbox.updated_at = now()
                db.add(outbox)
            db.commit()
        finally:
            db.close()

    def _ack(self, client, message_id) -> None:
        try:
            client.xack(self.stream, self.group, message_id)
        except Exception:
            # Keep it pending for XAUTOCLAIM. The DB claim and tool idempotency
            # make the subsequent delivery safe.
            logger.warning("Redis XACK failed for tool message=%s", message_id, exc_info=True)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value or "")


_worker: ToolQueueWorker | None = None


def get_tool_queue_worker(settings: Settings) -> ToolQueueWorker:
    global _worker
    if _worker is None:
        _worker = ToolQueueWorker(settings)
    return _worker
