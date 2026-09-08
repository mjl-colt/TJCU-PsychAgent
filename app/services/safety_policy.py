from __future__ import annotations

import re
from dataclasses import dataclass


SAFETY_POLICY_VERSION = "safety-signals-v3"


@dataclass(frozen=True)
class SafetySignal:
    rule_id: str
    category: str
    description: str


@dataclass(frozen=True)
class SafetyDetection:
    hard_high: bool
    signals: tuple[SafetySignal, ...]
    policy_version: str = SAFETY_POLICY_VERSION

    @property
    def signal_ids(self) -> tuple[str, ...]:
        return tuple(item.rule_id for item in self.signals)

    @property
    def reason(self) -> str:
        if not self.signals:
            return "未命中确定性即时危险规则"
        return "；".join(item.description for item in self.signals)


# These are policy rules, not a diagnosis model. They intentionally cover only
# explicit, high-precision crisis language. Ambiguous distress is assessed by
# SafetyAgent's model and fails closed to MEDIUM when that model is unavailable.
_DIRECT_SELF_HARM = re.compile(
    r"(?:我|本人).{0,8}(?:想|要|准备|打算|决定|计划|马上|今晚).{0,12}"
    r"(?:自杀|自残|轻生|结束.{0,3}生命|伤害自己)|"
    r"(?:我)?(?:不想活(?:了|下去)?|活不下去(?:了)?|想死|不如死了|想结束生命)|"
    r"\bi\s+(?:want|plan|intend|am going)\s+to\s+(?:die|kill myself|end my life|harm myself)\b|"
    r"\b(?:kill myself|end my life)\b",
    re.IGNORECASE,
)

_ACTIVE_ATTEMPT = re.compile(
    r"(?:我|本人)?.{0,5}(?:已经|正在|刚刚|此刻).{0,10}(?:开始实施|伤害自己|自残|轻生)|"
    r"\bi\s+(?:am|have)\s+(?:already\s+)?(?:harming myself|attempting suicide)\b",
    re.IGNORECASE,
)

_DIRECT_HARM_TO_OTHERS = re.compile(
    r"(?:我|本人).{0,8}(?:想|要|准备|打算|决定|计划|马上|今晚).{0,12}"
    r"(?:杀(?:了|掉)?(?:他|她|他们|别人|某人)|伤害(?:他|她|他们|别人|某人)|报复.{0,5}(?:他|她|他们|别人|某人))|"
    r"\bi\s+(?:want|plan|intend|am going)\s+to\s+(?:kill|hurt|harm)\s+(?:him|her|them|someone)\b",
    re.IGNORECASE,
)

_PLAN_MARKERS = re.compile(r"计划|准备好了|具体时间|今晚|马上|现在就|工具|地点|告别|遗书|plan|tonight|right now", re.IGNORECASE)
_RISK_REFERENCES = re.compile(r"自杀|自残|轻生|伤害自己|结束生命|kill myself|suicide|self[- ]?harm", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"我|本人|自己|\bi\b|\bmyself\b", re.IGNORECASE)

_EXPLICIT_NEGATION = re.compile(
    r"(?:我|本人).{0,3}(?:从来)?(?:没有|没|不会|并不|不曾).{0,5}"
    r"(?:想|打算|准备|计划|决定)?(?:自杀|自残|轻生|伤害自己|结束生命)|"
    r"(?:我|本人).{0,3}不想(?:自杀|自残|轻生|伤害自己)|"
    r"\bi\s+(?:do not|don't|never)\s+(?:want|plan|intend)\s+to\s+(?:die|kill myself|harm myself)\b",
    re.IGNORECASE,
)

_EDUCATIONAL_CONTEXT = re.compile(
    r"(?:论文|作业|报告|研究|课题|新闻|定义|含义|科普|预防|统计|怎么翻译|英文怎么说).{0,16}"
    r"(?:自杀|自残|轻生|suicide|self[- ]?harm)|"
    r"(?:自杀|自残|轻生|suicide|self[- ]?harm).{0,16}(?:论文|作业|报告|研究|课题|定义|科普|预防|统计)",
    re.IGNORECASE,
)

_NON_PERSONAL_INQUIRY = re.compile(
    r"(?:我|本人).{0,4}(?:想|要)(?:了解|研究|讨论|写|询问|知道|查询|翻译).{0,10}"
    r"(?:自杀|自残|轻生|suicide|self[- ]?harm)",
    re.IGNORECASE,
)


def detect_safety_signals(text: str) -> SafetyDetection:
    normalized = " ".join((text or "").split())
    if not normalized:
        return SafetyDetection(False, ())

    signals: list[SafetySignal] = []

    # Negation applies only to an otherwise simple statement. A separate active
    # attempt or direct harm-to-others rule must never be suppressed by it.
    negated_self_harm = bool(_EXPLICIT_NEGATION.search(normalized))
    educational_only = (
        bool(_EDUCATIONAL_CONTEXT.search(normalized)) and not _FIRST_PERSON.search(normalized)
    ) or bool(_NON_PERSONAL_INQUIRY.search(normalized))

    if _ACTIVE_ATTEMPT.search(normalized):
        signals.append(SafetySignal("CRISIS_ACTIVE_ATTEMPT", "SELF_HARM", "表达正在或已经实施伤害"))

    if _DIRECT_HARM_TO_OTHERS.search(normalized):
        signals.append(SafetySignal("CRISIS_DIRECT_HARM_TO_OTHERS", "HARM_TO_OTHERS", "表达直接伤人意图"))

    if not educational_only and not negated_self_harm and _DIRECT_SELF_HARM.search(normalized):
        signals.append(SafetySignal("CRISIS_DIRECT_SELF_HARM", "SELF_HARM", "表达直接自伤或轻生意图"))

    if (
        not educational_only
        and not negated_self_harm
        and _RISK_REFERENCES.search(normalized)
        and _FIRST_PERSON.search(normalized)
        and _PLAN_MARKERS.search(normalized)
    ):
        signals.append(SafetySignal("CRISIS_PLAN_OR_IMMEDIACY", "IMMEDIATE_DANGER", "同时出现危险意图与计划或即时性信号"))

    deduped = tuple({item.rule_id: item for item in signals}.values())
    return SafetyDetection(bool(deduped), deduped)


def has_high_risk_signal(text: str) -> bool:
    """Compatibility wrapper used at deterministic safety boundaries."""
    return detect_safety_signals(text).hard_high
