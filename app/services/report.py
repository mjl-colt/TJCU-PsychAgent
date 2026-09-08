from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.entities import AlertRecord, AgentRunTrace, AgentRuntimeCheckpoint, AgentRuntimeEventRecord, AgentRuntimeLease, CaseNote, ChatMessage, ChatSession, DeadLetterRecord, ExcelRecord, PsychologicalReport, RiskCase, ToolAuditRecord, ToolJob, UserAccount, now
from app.schemas.dtos import AgentRunTraceResponse, CaseNoteResponse, ConversationMessageResponse, ConversationResponse, DeadLetterResponse, ReportResponse, RiskCaseResponse, ToolAuditResponse, ToolJobResponse, ToolRecordResponse


class ReportService:
    def __init__(self, db: Session):
        self.db = db

    def latest_reports(self, user_id: int | None = None) -> list[ReportResponse]:
        query = self.db.query(PsychologicalReport).order_by(PsychologicalReport.created_at.desc())
        if user_id is not None:
            query = query.filter(PsychologicalReport.user_id == user_id)
        return [self._report_response(item) for item in query.limit(100).all()]

    def excel_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(ExcelRecord).order_by(ExcelRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(id=row.id, reportId=row.report_id, status=row.status, message=row.message, createdAt=row.created_at, filePath=row.file_path)
            for row in rows
        ]

    def alert_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(AlertRecord).order_by(AlertRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(
                id=row.id,
                reportId=row.report_id,
                status=row.status,
                message=row.message,
                createdAt=row.created_at,
                channel=row.channel,
                recipient=row.recipient,
            )
            for row in rows
        ]

    def risk_cases(self) -> list[RiskCaseResponse]:
        rows = self.db.query(RiskCase).order_by(RiskCase.updated_at.desc()).limit(100).all()
        return [
            RiskCaseResponse(
                id=row.id,
                reportId=row.report_id,
                riskLevel=row.risk_level,
                status=row.status,
                owner=row.owner,
                summary=row.summary,
                handoffSummary=row.handoff_summary,
                acknowledgedBy=row.acknowledged_by,
                acknowledgedAt=row.acknowledged_at,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def case_notes(self, case_id: int) -> list[CaseNoteResponse]:
        rows = self.db.query(CaseNote).filter(CaseNote.case_id == case_id).order_by(CaseNote.created_at.asc()).all()
        return [
            CaseNoteResponse(id=row.id, caseId=row.case_id, actor=row.actor, note=row.note, createdAt=row.created_at)
            for row in rows
        ]

    def tool_jobs(self) -> list[ToolJobResponse]:
        rows = self.db.query(ToolJob).order_by(ToolJob.created_at.desc()).limit(100).all()
        return [
            ToolJobResponse(
                id=row.id,
                reportId=row.report_id,
                kind=row.kind,
                status=row.status,
                attempts=row.attempts,
                maxAttempts=row.max_attempts,
                dependsOnJobId=row.depends_on_job_id,
                runAfter=row.run_after,
                lastError=row.last_error,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def dead_letters(self) -> list[DeadLetterResponse]:
        rows = self.db.query(DeadLetterRecord).order_by(DeadLetterRecord.created_at.desc()).limit(100).all()
        return [
            DeadLetterResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                kind=row.kind,
                reason=row.reason,
                payload=row.payload,
                createdAt=row.created_at,
            )
            for row in rows
        ]

    def agent_run_traces(self) -> list[AgentRunTraceResponse]:
        rows = self.db.query(AgentRunTrace).order_by(AgentRunTrace.created_at.desc()).limit(100).all()
        responses = []
        for row in rows:
            user = self.db.get(UserAccount, row.user_id)
            session = self.db.get(ChatSession, row.session_id)
            responses.append(
                AgentRunTraceResponse(
                    id=row.id,
                    sessionId=session.public_id if session else "",
                    reportId=row.report_id,
                    username=user.username if user else "",
                    intent=row.intent,
                    riskLevel=row.risk_level,
                    originalInput=row.original_input,
                    sanitizedInput=row.sanitized_input,
                    memoryBrief=row.memory_brief,
                    agentSteps=_loads(row.agent_steps_json, []),
                    retrievedKnowledge=_loads(row.retrieved_knowledge_json, []),
                    responseMessages=_loads(row.response_messages_json, []),
                    assessment=_loads(row.assessment_json, {}),
                    createdAt=row.created_at,
                )
            )
        return responses

    def tool_audits(self) -> list[ToolAuditResponse]:
        rows = self.db.query(ToolAuditRecord).order_by(ToolAuditRecord.created_at.desc()).limit(100).all()
        return [
            ToolAuditResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                toolName=row.tool_name,
                policy=row.policy,
                allowed=row.allowed,
                status=row.status,
                reason=row.reason,
                payload=_loads(row.payload, {}),
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def runtime_checkpoints(self) -> list[dict]:
        rows = self.db.query(AgentRuntimeCheckpoint).order_by(AgentRuntimeCheckpoint.updated_at.desc()).limit(100).all()
        return [
            {
                "requestId": row.request_id,
                "sessionId": row.session_public_id,
                "stateVersion": row.state_version,
                "stage": row.stage,
                "completed": row.completed,
                "updatedAt": row.updated_at,
            }
            for row in rows
        ]

    def runtime_events(self, request_id: str | None = None) -> list[dict]:
        query = self.db.query(AgentRuntimeEventRecord).order_by(AgentRuntimeEventRecord.created_at.desc())
        if request_id:
            query = query.filter(AgentRuntimeEventRecord.request_id == request_id)
        items = []
        for row in query.limit(500).all():
            payload = _loads(row.payload_json, {})
            payload, projection_summary = _public_runtime_event_payload(payload)
            items.append({
                "eventId": row.event_id,
                "requestId": row.request_id,
                "type": row.event_type,
                "actor": row.actor,
                "batchId": row.batch_id,
                "commandId": row.command_id,
                "payload": payload,
                "projection": projection_summary,
                "createdAt": row.created_at,
            })
        return items

    def runtime_metrics(self, event_window: int = 5000) -> dict:
        """Aggregate low-cost operational signals from the durable journal."""

        stage_rows = (
            self.db.query(AgentRuntimeCheckpoint.stage, func.count(AgentRuntimeCheckpoint.id))
            .group_by(AgentRuntimeCheckpoint.stage)
            .all()
        )
        rows = (
            self.db.query(AgentRuntimeEventRecord)
            .order_by(AgentRuntimeEventRecord.id.desc())
            .limit(max(100, min(event_window, 20000)))
            .all()
        )
        type_counts: dict[str, int] = {}
        durations = []
        degraded = 0
        retried = 0
        prompt_injection_detections = 0
        output_guardrail_replacements = 0
        projected_events = 0
        for row in rows:
            type_counts[row.event_type] = type_counts.get(row.event_type, 0) + 1
            payload = _loads(row.payload_json, {})
            metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            duration = metadata.get("durationMs")
            if isinstance(duration, (int, float)):
                durations.append(float(duration))
            if metadata.get("degraded") is True:
                degraded += 1
            if isinstance(metadata.get("attempts"), int) and metadata["attempts"] > 1:
                retried += 1
            signals = metadata.get("promptInjectionSignals")
            if isinstance(signals, list) and signals:
                prompt_injection_detections += 1
            if metadata.get("guardrailReplaced") is True:
                output_guardrail_replacements += 1
            if metadata.get("projectionVersion") == 1:
                projected_events += 1
        durations.sort()
        completed_agents = type_counts.get("AGENT_COMPLETED", 0)
        failed_agents = type_counts.get("AGENT_FAILED", 0)
        outcome_total = completed_agents + failed_agents
        active_leases = (
            self.db.query(AgentRuntimeLease)
            .filter(AgentRuntimeLease.lease_until > now())
            .count()
        )
        expired_leases = (
            self.db.query(AgentRuntimeLease)
            .filter(AgentRuntimeLease.lease_until <= now())
            .count()
        )
        return {
            "checkpointStages": {stage: count for stage, count in stage_rows},
            "incompleteCheckpoints": (
                self.db.query(AgentRuntimeCheckpoint)
                .filter(AgentRuntimeCheckpoint.completed.is_(False))
                .count()
            ),
            "eventWindow": len(rows),
            "eventTypes": type_counts,
            "agentDurationMs": {
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
                "p99": _percentile(durations, 0.99),
            },
            "agentFailureRate": round(failed_agents / max(1, outcome_total), 6),
            "degradedOutcomes": degraded,
            "retriedOutcomes": retried,
            "recoveries": type_counts.get("TURN_RECOVERY_STARTED", 0),
            "generationFailures": type_counts.get("GENERATION_FAILED", 0),
            "promptInjectionDetections": prompt_injection_detections,
            "outputGuardrailReplacements": output_guardrail_replacements,
            "contextCompaction": {
                "started": type_counts.get("CONTEXT_COMPACTION_STARTED", 0),
                "completed": type_counts.get("CONTEXT_COMPACTION_COMPLETED", 0),
                "failed": type_counts.get("CONTEXT_COMPACTION_FAILED", 0),
            },
            "eventProjections": projected_events,
            "leases": {"active": active_leases, "expired": expired_leases},
        }

    def conversation(self, public_id: str) -> ConversationResponse:
        session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        if session is None:
            raise ValueError("Session not found")
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )

    def _report_response(self, report: PsychologicalReport) -> ReportResponse:
        user = self.db.get(UserAccount, report.user_id)
        session = self.db.get(ChatSession, report.session_id)
        return ReportResponse(
            id=report.id,
            sessionId=session.public_id if session else "",
            username=user.username if user else "",
            displayName=user.display_name if user else "",
            content=report.content,
            intent=report.intent,
            emotion=report.emotion,
            emotionScore=report.emotion_score,
            riskLevel=report.risk_level,
            summary=report.summary,
            createdAt=report.created_at,
        )


def _loads(raw: str, default):
    import json

    try:
        return json.loads(raw or "")
    except Exception:
        return default


def _public_runtime_event_payload(payload):
    """Remove prompt/history bodies while preserving operational diagnostics."""

    if not isinstance(payload, dict):
        return payload, None
    value = dict(payload)
    projection = value.pop("state_projection", None)
    projection_summary = None
    if isinstance(projection, dict):
        flow = projection.get("flow", {})
        projection_summary = {
            "revision": projection.get("revision"),
            "stage": flow.get("current_stage") if isinstance(flow, dict) else None,
            "hash": value.get("metadata", {}).get("stateProjectionHash"),
        }
    outcome = value.get("outcome")
    if isinstance(outcome, dict):
        safe_outcome = {
            key: outcome.get(key)
            for key in ("command", "success", "degraded", "attempts", "duration_ms", "error")
        }
        update = outcome.get("update")
        if isinstance(update, dict):
            data = update.get("data", {})
            safe_outcome["update"] = {
                "section": update.get("section"),
                "summary": _runtime_update_summary(update.get("section"), data),
            }
        value["outcome"] = safe_outcome
    return value, projection_summary


def _runtime_update_summary(section, data) -> dict:
    if not isinstance(data, dict):
        return {}
    allowed = {
        "understanding": ("intent", "topic", "prompt_template_version"),
        "safety": ("risk_level", "assessment_method", "prompt_template_version"),
        "context": ("skill_versions", "prompt_template_version", "compaction"),
        "response": ("prompt_version", "mode", "intent", "risk_level", "generation_status", "safe_fallback", "prompt_template_version", "prompt_hash"),
    }
    summary = {key: data.get(key) for key in allowed.get(str(section), ()) if key in data}
    if str(section) == "context":
        evidence = data.get("retrieved_knowledge", [])
        summary["retrievedKnowledgeCount"] = len(evidence) if isinstance(evidence, (list, tuple)) else 0
    return summary


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
    return round(values[index], 3)
