from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.core.enums import IntentType, RiskLevel
from app.models.entities import PsychologicalReport, UserAccount


class SkillLoadError(RuntimeError):
    pass


# Skill selection is deliberately a complete table instead of scattered topic
# keyword branches.  Every possible Understanding intent / Safety risk pair has
# one reviewable result, so adding a new enum value cannot silently inherit an
# unrelated response strategy.
_RESPONSE_SKILL_MATRIX: dict[tuple[IntentType, RiskLevel], tuple[str, ...]] = {
    (IntentType.CHAT, RiskLevel.LOW): (),
    (IntentType.CHAT, RiskLevel.MEDIUM): ("supportive_response_baseline", "campus_support_toolkit"),
    (IntentType.CHAT, RiskLevel.HIGH): ("supportive_response_baseline", "high_risk_safety_plan"),
    (IntentType.CONSULT, RiskLevel.LOW): ("supportive_response_baseline", "campus_support_toolkit"),
    (IntentType.CONSULT, RiskLevel.MEDIUM): ("supportive_response_baseline", "campus_support_toolkit"),
    (IntentType.CONSULT, RiskLevel.HIGH): ("supportive_response_baseline", "high_risk_safety_plan"),
    (IntentType.RISK, RiskLevel.LOW): ("supportive_response_baseline", "high_risk_safety_plan"),
    (IntentType.RISK, RiskLevel.MEDIUM): ("supportive_response_baseline", "high_risk_safety_plan"),
    (IntentType.RISK, RiskLevel.HIGH): ("supportive_response_baseline", "high_risk_safety_plan"),
}


@dataclass(frozen=True)
class SkillValidationIssue:
    level: str
    message: str


@dataclass(frozen=True)
class MindBridgeSkill:
    name: str
    description: str
    body: str
    path: Path
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        configured = self.metadata.get("version", "").strip()
        return configured or hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:12]

    def prompt_context(self) -> str:
        # SKILL.md can be detailed enough for people to review, while only the
        # bounded operational rules are injected into the model prompt.
        match = re.search(r"## Prompt 规则\s*\n(?P<rules>.*?)(?=\n## |\Z)", self.body, re.DOTALL)
        rules = match.group("rules").strip() if match else self.body.strip()
        return f"应用技能：{self.name}（版本 {self.version}）\n{rules}"

    def validation_issues(self) -> list[SkillValidationIssue]:
        issues: list[SkillValidationIssue] = []
        if self.path.parent.name != self.name:
            issues.append(SkillValidationIssue("WARN", f"目录名 {self.path.parent.name} 与 skill name {self.name} 不一致"))
        if "## Workflow" not in self.body and "## 工作流" not in self.body:
            issues.append(SkillValidationIssue("WARN", "建议包含 ## 工作流 小节，便于人工审阅和模型稳定加载"))
        if len(self.description) < 20:
            issues.append(SkillValidationIssue("WARN", "description 太短，可能无法准确表达触发场景"))
        if self.name == "counselor_handoff_summary" and "```text" not in self.body:
            issues.append(SkillValidationIssue("ERROR", "counselor_handoff_summary 必须包含 text 模板"))
        return issues


class MindBridgeSkillRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[2] / "skills"

    def list_skills(self) -> list[MindBridgeSkill]:
        if not self.root.exists():
            return []
        skills = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            skills.append(self._load_skill_file(skill_file))
        return skills

    def status_items(self) -> list[dict]:
        if not self.root.exists():
            return []
        items = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            try:
                skill = self._load_skill_file(skill_file)
                issues = skill.validation_issues()
            except SkillLoadError as exc:
                items.append(
                    {
                        "name": skill_file.parent.name,
                        "status": "FAILED",
                        "description": str(exc),
                        "path": skill_file.relative_to(self.root.parent).as_posix(),
                        "issues": [{"level": "ERROR", "message": str(exc)}],
                    }
                )
                continue
            has_error = any(issue.level == "ERROR" for issue in issues)
            items.append(
                {
                    "name": skill.name,
                    "status": "FAILED" if has_error else "READY" if not issues else "WARN",
                    "description": skill.description,
                    "path": skill.path.relative_to(self.root.parent).as_posix(),
                    "issues": [{"level": issue.level, "message": issue.message} for issue in issues],
                    "metadata": skill.metadata,
                    "documentChars": len(skill.body),
                    "runtimePromptChars": len(skill.prompt_context()),
                }
            )
        return items

    def get_required(self, name: str) -> MindBridgeSkill:
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        raise SkillLoadError(f"required standard skill not found: {name}")

    def template_for(self, name: str) -> str:
        skill = self.get_required(name)
        match = re.search(r"```text\s*\n(?P<template>.*?)\n```", skill.body, re.DOTALL)
        if match is None:
            raise SkillLoadError(f"standard skill {name} does not define a text template")
        return match.group("template").strip()

    def _load_skill_file(self, path: Path) -> MindBridgeSkill:
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text, path)
        name = metadata.get("name") or path.parent.name
        description = metadata.get("description", "")
        if not name.strip():
            raise SkillLoadError(f"{path} is missing frontmatter name")
        if not description.strip():
            raise SkillLoadError(f"{path} is missing frontmatter description")
        if not body.strip():
            raise SkillLoadError(f"{path} is missing skill body")
        return MindBridgeSkill(name=name.strip(), description=description.strip(), body=body.strip(), path=path, metadata=metadata)


class MindBridgeSkillLibrary:
    @staticmethod
    def registry() -> MindBridgeSkillRegistry:
        return MindBridgeSkillRegistry()

    @staticmethod
    def list_skills() -> list[MindBridgeSkill]:
        return MindBridgeSkillLibrary.registry().list_skills()

    @staticmethod
    def status_items() -> list[dict]:
        return MindBridgeSkillLibrary.registry().status_items()

    @staticmethod
    def response_skill_context(intent: IntentType, risk: RiskLevel) -> str:
        names = MindBridgeSkillLibrary.response_skill_names(intent, risk)
        registry = MindBridgeSkillLibrary.registry()
        return "\n\n".join(registry.get_required(name).prompt_context() for name in names)

    @staticmethod
    def response_skill_names(intent: IntentType, risk: RiskLevel) -> list[str]:
        """Select response skills from the complete intent/risk decision matrix."""
        try:
            return list(_RESPONSE_SKILL_MATRIX[(intent, risk)])
        except KeyError as exc:
            raise SkillLoadError(f"unsupported intent/risk skill path: {intent!r}/{risk!r}") from exc

    @staticmethod
    def counselor_handoff_summary(report: PsychologicalReport, user: UserAccount | None) -> str:
        template = MindBridgeSkillLibrary.registry().template_for("counselor_handoff_summary")
        student = _student_label(user, report.user_id)
        urgency = "立即跟进" if report.risk_level == RiskLevel.HIGH.value else "尽快跟进"
        next_steps = [
            f"{urgency}，确认学生当前位置、身边是否有人陪伴，以及当前是否安全。",
            "联系学生本人或其可用的现实支持人，并记录已采取的联系方式。",
            "必要时联系校园保卫、心理中心值班老师或当地紧急救助。",
            "将后续安排、接手人和下一次复访时间写入个案备注。",
        ]
        return _render_template(
            template,
            {
                "report_id": str(report.id),
                "student": student,
                "risk_level": report.risk_level,
                "emotion": report.emotion,
                "summary": report.summary,
                "next_steps": "\n".join(f"- {step}" for step in next_steps),
                "content_excerpt": _truncate(report.content, 700),
            },
        )


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillLoadError(f"{path} is missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise SkillLoadError(f"{path} has unterminated YAML frontmatter")
    metadata = {}
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise SkillLoadError(f"{path} has invalid frontmatter line: {line}")
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[end + len("\n---") :].strip()


def _render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _student_label(user: UserAccount | None, user_id: int) -> str:
    if user is None:
        return f"userId={user_id}"
    if user.display_name:
        return f"{user.display_name} ({user.username})"
    return user.username


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 3]}..."
