from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.schemas.dtos import AiMessage
from app.services.prompt_catalog import prompt_catalog
from app.services.prompt_security import PromptSecurityService


SEMANTIC_REVIEW_PROMPT_ID = "semantic-output-safety-review:v1"


class SemanticSafetyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledges_distress: bool
    addresses_immediate_safety: bool
    connects_human_support: bool
    recommends_emergency_escalation: bool
    contains_diagnosis: bool
    contains_medication_instruction: bool
    contains_dangerous_details: bool


@dataclass(frozen=True)
class SemanticSafetyReviewResult:
    allowed: bool
    issues: tuple[str, ...] = ()
    prompt_id: str = SEMANTIC_REVIEW_PROMPT_ID


class SemanticResponseSafetyReviewer:
    """Fail-closed semantic review for buffered high-risk final responses."""

    def __init__(self, ai):
        self.ai = ai

    async def review_async(self, content: str) -> SemanticSafetyReviewResult:
        security = PromptSecurityService()
        review_prompt = prompt_catalog().render("semantic-output-safety-review")
        messages = [
            AiMessage(
                role="system",
                content=review_prompt,
            ),
            AiMessage(
                role="user",
                content=security.wrap_untrusted(content, "candidate_response"),
            ),
        ]
        try:
            raw = await self.ai.complete_async(messages)
            payload = _parse_payload(raw)
        except Exception as exc:
            return SemanticSafetyReviewResult(
                False,
                (f"SEMANTIC_REVIEW_UNAVAILABLE:{type(exc).__name__}",),
            )

        issues = []
        if not payload.acknowledges_distress:
            issues.append("MISSING_DISTRESS_ACKNOWLEDGEMENT")
        if not payload.addresses_immediate_safety:
            issues.append("MISSING_IMMEDIATE_SAFETY_ACTION")
        if not payload.connects_human_support:
            issues.append("MISSING_HUMAN_SUPPORT")
        if not payload.recommends_emergency_escalation:
            issues.append("MISSING_EMERGENCY_ESCALATION")
        if payload.contains_diagnosis:
            issues.append("CONTAINS_DIAGNOSIS")
        if payload.contains_medication_instruction:
            issues.append("CONTAINS_MEDICATION_INSTRUCTION")
        if payload.contains_dangerous_details:
            issues.append("CONTAINS_DANGEROUS_DETAILS")
        return SemanticSafetyReviewResult(not issues, tuple(issues))


def _parse_payload(raw: str) -> SemanticSafetyPayload:
    text = (raw or "").strip().strip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1] if start >= 0 and end > start else text)
    return SemanticSafetyPayload.model_validate(data)
