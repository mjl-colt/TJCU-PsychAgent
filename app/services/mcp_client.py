from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.core.enums import RiskLevel, ToolJobKind


class McpToolError(RuntimeError):
    pass


class MindBridgeMcpToolClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def handle_report(self, report_id: int, risk_level: str | None) -> list[str]:
        try:
            async with self._session() as session:
                results = [
                    await self._call_tool(session, "mindbridge_excel_report", {"report_id": report_id}),
                ]
                case_id = None
                if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
                    case_result = await self._call_tool(session, "mindbridge_case_create", {"report_id": report_id})
                    results.append(case_result)
                    case_id = self._extract_case_id(case_result)
                if risk_level == RiskLevel.HIGH.value:
                    if case_id is None:
                        raise McpToolError("高风险报告已创建，但无法解析 caseId，预警未发送")
                    results.append(await self._call_tool(session, "mindbridge_alert_send", {"case_id": case_id}))
                return results
        except McpToolError:
            raise
        except Exception as exc:
            raise McpToolError(f"MCP 工具调用异常：{type(exc).__name__}: {exc}") from exc

    async def execute_job(self, kind: str, report_id: int, *, case_id: int | None = None) -> str:
        """Execute one durable job through the MCP boundary.

        Queue consumers use one MCP call per ToolJob so acknowledgements and
        retry state remain aligned with the individual business side effect.
        """
        mapping = {
            ToolJobKind.EXCEL_REPORT.value: ("mindbridge_excel_report", {"report_id": report_id}),
            ToolJobKind.CASE_CREATE.value: ("mindbridge_case_create", {"report_id": report_id}),
            ToolJobKind.RISK_ALERT.value: ("mindbridge_alert_notify", {"report_id": report_id}),
        }
        if kind == ToolJobKind.ALERT_SEND.value:
            if case_id is None:
                raise McpToolError("ALERT_SEND 缺少已创建的 caseId")
            mapping[kind] = ("mindbridge_alert_send", {"case_id": case_id})
        tool = mapping.get(kind)
        if tool is None:
            raise McpToolError(f"未知 MCP 工具任务：{kind}")
        try:
            async with self._session() as session:
                return await self._call_tool(session, *tool)
        except McpToolError:
            raise
        except Exception as exc:
            raise McpToolError(f"MCP 工具调用异常：{type(exc).__name__}: {exc}") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpToolError("缺少 mcp 依赖，无法通过 MCP 调用心理ai工具") from exc

        project_root = self.settings.project_root
        env = os.environ.copy()
        python_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(project_root) if not python_path else f"{project_root}{os.pathsep}{python_path}"

        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_tools.server"],
            env=env,
            cwd=str(project_root),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def _call_tool(self, session: Any, name: str, arguments: dict[str, Any]) -> str:
        timeout = max(1.0, float(getattr(self.settings, "tool_queue_mcp_timeout_seconds", 30.0)))
        result = await asyncio.wait_for(session.call_tool(name, arguments=arguments), timeout=timeout)
        message = self._result_message(result)
        if getattr(result, "isError", False):
            raise McpToolError(f"{name} 调用失败：{message}")
        # Some built-in tools return domain failures as plain text rather than
        # setting the MCP error bit. Normalize those into retryable failures.
        normalized = message.strip().lower()
        if normalized.startswith("failed:") or " not found" in normalized:
            raise McpToolError(f"{name} 调用失败：{message}")
        return message

    def _result_message(self, result: Any) -> str:
        parts = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        if parts:
            return "\n".join(parts)
        structured = getattr(result, "structuredContent", None)
        return str(structured if structured is not None else result)

    def _extract_case_id(self, message: str) -> int | None:
        match = re.search(r"caseId=(\d+)", message)
        return int(match.group(1)) if match else None
