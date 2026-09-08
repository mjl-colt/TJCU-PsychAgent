from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import EmotionLabel, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates
from app.services.safety_policy import has_high_risk_signal


@dataclass
class PsychologyAssessment:
    emotion: EmotionLabel
    emotion_score: float
    risk: RiskLevel
    summary: str


class AssessmentPayload(BaseModel):
    """Strict boundary for nondeterministic model output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    emotion: EmotionLabel
    emotion_score: float = Field(alias="emotionScore", ge=0.0, le=4.0)
    risk: RiskLevel
    summary: str = Field(min_length=1, max_length=240)


class PsychologicalAssessmentService:
    def __init__(self, ai: AiClient):
        self.ai = ai

    def assess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        if has_high_risk_signal(text):
            return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, "检测到明确高风险表达")
        try:
            raw = self.ai.complete(PromptTemplates.psychology_prompt(history or [], text))
            return parse_assessment(raw)
        except Exception:
            return heuristic(text)

    async def assess_async(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        if has_high_risk_signal(text):
            return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, "检测到明确高风险表达")
        # Let runtime errors propagate to Dispatcher. It owns retry and the
        # fail-closed Safety fallback (MEDIUM unless a hard rule requires HIGH).
        # Swallowing the error here could incorrectly turn an outage into LOW.
        raw = await self.ai.complete_async(PromptTemplates.psychology_prompt(history or [], text))
        return parse_assessment(raw)


def parse_assessment(raw: str) -> PsychologyAssessment:
    start = raw.find("{")
    end = raw.rfind("}")
    data = json.loads(raw[start:end + 1] if start >= 0 and end > start else raw)
    payload = AssessmentPayload.model_validate(data)
    emotion = payload.emotion
    score = payload.emotion_score
    risk = payload.risk
    score_risk = risk_from_score(score)
    if risk_order(score_risk) > risk_order(risk):
        risk = score_risk
    if emotion == EmotionLabel.HIGH_RISK:
        risk = RiskLevel.HIGH
    return PsychologyAssessment(emotion, score, risk, payload.summary)


def heuristic(text: str) -> PsychologyAssessment:
    # This compatibility path is used only by synchronous callers. When the
    # model is unavailable, do not guess LOW/CHAT from a language-specific
    # word list: use a conservative, explicitly degraded result.
    return PsychologyAssessment(
        EmotionLabel.ANXIETY,
        3.0,
        RiskLevel.MEDIUM,
        "风险模型不可用，已采用保守降级",
    )


def score_for_emotion(emotion: EmotionLabel) -> float:
    return {
        EmotionLabel.HIGH_RISK: 4.0,
        EmotionLabel.DEPRESSED: 3.0,
        EmotionLabel.ANXIETY: 2.0,
        EmotionLabel.NORMAL: 0.0,
    }[emotion]


def risk_from_score(score: float) -> RiskLevel:
    if score >= 4:
        return RiskLevel.HIGH
    if score >= 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def risk_order(risk: RiskLevel) -> int:
    return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[risk]
