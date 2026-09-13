from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user, require_admin
from app.models.entities import UserAccount
from app.schemas.dtos import KnowledgeIngestRequest, KnowledgeIngestResponse, ChatRequest, authority
from app.services.chat import ChatService
from app.services.knowledge import KnowledgeSecurityError, KnowledgeService
from app.services.health import readiness_status
from app.services.model_assets import finetuned_model_status
from app.services.prompt_catalog import prompt_catalog
from app.services.rate_limit import get_rate_limiter
from app.services.report import ReportService
from app.services.skills import MindBridgeSkillLibrary

router = APIRouter()


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.get("/actuator/ready")
def ready(db: Annotated[Session, Depends(get_db)]):
    status_code, body = readiness_status(db, get_settings())
    return JSONResponse(status_code=status_code, content=body)


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    settings = get_settings()
    if not get_rate_limiter(settings).allow(
        "chat",
        str(user.id),
        settings.chat_rate_limit_per_minute,
    ):
        raise HTTPException(429, "对话请求过于频繁，请稍后重试", headers={"Retry-After": "60"})
    service = ChatService(db, settings)
    try:
        outcome = await service.prepare_chat(user, request)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return StreamingResponse(service.stream_outcome(user, outcome), media_type="text/event-stream")


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": {"active": "checkpoint_workflow", "workflowVersion": "workflow-v2", "legacyRecovery": "event-v1"},
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "CoordinatorAgent", "status": "READY", "description": "按显式步骤和业务结果选择下一节点，维护安全门禁；不消费事件队列"},
            {"name": "UnderstandingAgent", "status": "READY", "description": "独立理解用户输入，只返回 understanding 状态更新"},
            {"name": "SafetyAgent", "status": "READY", "description": "独立风险评估、硬规则优先和候选 Prompt 安全审查"},
            {"name": "ContextAgent", "status": "READY", "description": "独立记忆视图、RAG 检索和 skill 上下文聚合"},
            {"name": "ResponseAgent", "status": "READY", "description": "根据 Blackboard 分区组装带版本的候选 Prompt"},
        ],
        "prompts": prompt_catalog().status_items(),
        "skills": MindBridgeSkillLibrary.status_items(),
        "runtimeHarness": {
        "name": "心理ai Agent Harness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "checkpoint-workflow",
            "maxSteps": settings.agent_workflow_max_steps,
            "scheduler": "explicit-step-dispatcher",
        },
        "collaboration": {
            "scheduler": "explicit-workflow-transitions",
            "state": "typed-versioned-blackboard",
            "messageBus": "audit-only MySQL journal; no event queue in workflow-v2",
            "fixedWorkflow": True,
            "parallelism": "bounded tasks with independently persisted outcomes",
            "checkpoint": "MySQL state, current step and task receipts; event-v1 recovery retained",
            "requestOwnership": "expiring database lease with SSE heartbeat",
            "security": {
                "inputTrust": "all user, memory, RAG and skill content treated as untrusted data",
                "promptInjection": "direct, multilingual and encoded signal detection with prompt isolation",
                "outputGuardrail": "deterministic prohibition/citation gate plus fail-closed HIGH-risk semantic review",
                "toolAuthority": "code-owned allowlist and policy checks; prompts cannot grant permissions",
            },
            "agentIsolation": {
                "prompt": "per-agent system prompt",
                "memory": "no duplicated private store; each agent receives a bounded role-specific view of session memory",
                "model": "per-agent model profile",
                "tools": "per-agent tool permissions",
                "stateWrite": "validated per-agent Blackboard section",
            },
        },
    }


@router.get("/api/reports/me")
def my_reports(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports(user.id)


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).alert_records()


@router.get("/api/admin/cases")
def admin_cases(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).risk_cases()


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(case_id: int, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).case_notes(case_id)


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).dead_letters()


@router.get("/api/admin/agent-traces")
def admin_agent_traces(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).agent_run_traces()


@router.get("/api/admin/runtime-checkpoints")
def admin_runtime_checkpoints(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).runtime_checkpoints()


@router.get("/api/admin/runtime-events")
def admin_runtime_events(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    request_id: str | None = None,
):
    return ReportService(db).runtime_events(request_id)


@router.get("/api/admin/runtime-metrics")
def admin_runtime_metrics(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    return ReportService(db).runtime_metrics()


@router.get("/api/admin/tool-audits")
def admin_tool_audits(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_audits()


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(session_id: str, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        return ReportService(db).conversation(session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    except (KnowledgeSecurityError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    settings = get_settings()
    max_bytes = max(1, settings.knowledge_max_file_bytes)
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(422, f"知识文件不能超过 {max_bytes} 字节")
    try:
        chunks = KnowledgeService(db, settings).ingest_file(file.filename or "uploaded-file", data)
    except (KnowledgeSecurityError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return KnowledgeIngestResponse(source=file.filename or "uploaded-file", chunks=chunks)
