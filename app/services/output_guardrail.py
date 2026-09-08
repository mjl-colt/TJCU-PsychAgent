from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.enums import RiskLevel


@dataclass(frozen=True)
class OutputGuardrailResult:
    allowed: bool
    content: str
    issues: tuple[str, ...] = ()
    cited_ids: tuple[str, ...] = ()


class ResponseOutputGuardrail:
    """Deterministic prohibition/citation gate used before support text reaches SSE.

    Positive semantic coverage for HIGH-risk responses is intentionally checked
    by ``SemanticResponseSafetyReviewer`` rather than literal-word matching.
    """

    def validate(
        self,
        content: str,
        risk: RiskLevel,
        *,
        allowed_citation_ids: tuple[str, ...] = (),
        citations_required: bool = False,
    ) -> OutputGuardrailResult:
        text = (content or "").strip()
        issues: list[str] = []
        cited_ids = tuple(dict.fromkeys(re.findall(r"\[(K[1-9][0-9]*)\]", text)))
        allowed_ids = set(allowed_citation_ids)
        if not text:
            issues.append("模型没有返回正文")
        if re.search(r"你(已经)?(患有|得了|确诊为)", text):
            issues.append("最终回复包含诊断式结论")
        if re.search(r"建议你.{0,12}(服用|停用|换用).{0,20}(药|毫克|mg)", text, re.IGNORECASE):
            issues.append("最终回复包含未经授权的用药指令")
        if re.search(r"(自杀|自伤).{0,20}(具体方法|操作步骤|成功率)", text):
            issues.append("最终回复包含危险操作细节")
        if re.search(r"(?i)(system prompt|developer message|hidden instructions?|系统提示词|开发者指令)", text):
            issues.append("最终回复疑似泄露内部提示词")
        unknown_ids = sorted(set(cited_ids) - allowed_ids)
        if unknown_ids:
            issues.append("最终回复包含不存在的知识引用：" + ", ".join(unknown_ids))
        if citations_required and allowed_ids and not cited_ids:
            issues.append("使用了 RAG 知识但最终回复没有引用资料标签")
        if not issues:
            return OutputGuardrailResult(True, text, cited_ids=cited_ids)
        return OutputGuardrailResult(False, self.fallback(risk), tuple(issues), cited_ids=())

    def fallback(self, risk: RiskLevel) -> str:
        if risk == RiskLevel.HIGH:
            return (
                "我很在意你现在的安全。请先不要独自承受，马上联系身边可信任的人、辅导员、"
                "学校心理中心、校园保卫或当地紧急服务，并尽量待在有人陪伴的地方。"
                "如果可以，请告诉我：你现在是否安全，身边有没有能立刻联系的人？"
            )
        return (
            "我听到你现在很不容易。我们先从一个最小、最安全的步骤开始；如果这种状态持续、"
            "明显影响生活，或你担心自己会受到伤害，请尽快联系可信任的人和专业支持。"
        )
