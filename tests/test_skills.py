import tempfile
import unittest
from pathlib import Path

from app.core.enums import IntentType, RiskLevel
from app.services.skills import MindBridgeSkillLibrary, MindBridgeSkillRegistry, SkillLoadError


def write_skill(root: Path, name: str, text: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


class SkillRegistryTests(unittest.TestCase):
    def test_skill_registry_loads_valid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: Use for a clear and sufficiently described demo scenario.\n---\n\n# Demo\n\n## Workflow\n\n- Do one thing.\n""",
            )

            skill = MindBridgeSkillRegistry(root).get_required("demo_skill")

            self.assertEqual(skill.name, "demo_skill")
            self.assertEqual(skill.validation_issues(), [])

    def test_skill_registry_accepts_chinese_workflow_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: 当需要验证中文技能工作流标题能够被正确识别和加载时使用。\n---\n\n# 中文示例\n\n## 工作流\n\n- 执行一个明确的步骤。\n""",
            )

            skill = MindBridgeSkillRegistry(root).get_required("demo_skill")

            self.assertEqual(skill.validation_issues(), [])

    def test_skill_status_reports_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: short\n---\n\n# Demo\n""",
            )

            status = MindBridgeSkillRegistry(root).status_items()[0]

            self.assertEqual(status["status"], "WARN")
            self.assertTrue(status["issues"])

    def test_skill_requires_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "bad", "# Missing metadata")

            with self.assertRaises(SkillLoadError):
                MindBridgeSkillRegistry(root).get_required("bad")

    def test_production_skill_catalog_is_valid_and_bounded_for_prompt_use(self):
        statuses = MindBridgeSkillLibrary.status_items()

        self.assertEqual(len(statuses), 4)
        self.assertTrue(all(item["status"] == "READY" for item in statuses))
        self.assertTrue(all(item["runtimePromptChars"] < item["documentChars"] for item in statuses))

    def test_all_nine_intent_risk_paths_have_explicit_skill_sets(self):
        support = ["supportive_response_baseline", "campus_support_toolkit"]
        crisis = ["supportive_response_baseline", "high_risk_safety_plan"]
        expected = {
            (IntentType.CHAT, RiskLevel.LOW): [],
            (IntentType.CHAT, RiskLevel.MEDIUM): support,
            (IntentType.CHAT, RiskLevel.HIGH): crisis,
            (IntentType.CONSULT, RiskLevel.LOW): support,
            (IntentType.CONSULT, RiskLevel.MEDIUM): support,
            (IntentType.CONSULT, RiskLevel.HIGH): crisis,
            (IntentType.RISK, RiskLevel.LOW): crisis,
            (IntentType.RISK, RiskLevel.MEDIUM): crisis,
            (IntentType.RISK, RiskLevel.HIGH): crisis,
        }
        self.assertEqual(
            set(expected),
            {(intent, risk) for intent in IntentType for risk in RiskLevel},
            "新增 intent/risk 枚举时必须先明确它应加载哪些 Skill",
        )
        for path, names in expected.items():
            with self.subTest(intent=path[0].value, risk=path[1].value):
                self.assertEqual(MindBridgeSkillLibrary.response_skill_names(*path), names)

    def test_high_risk_uses_only_baseline_and_crisis_skill(self):
        names = MindBridgeSkillLibrary.response_skill_names(
            IntentType.RISK,
            RiskLevel.HIGH,
        )

        self.assertEqual(names, ["supportive_response_baseline", "high_risk_safety_plan"])


if __name__ == "__main__":
    unittest.main()
