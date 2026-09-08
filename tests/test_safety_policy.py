import unittest

from app.services.safety_policy import SAFETY_POLICY_VERSION, detect_safety_signals, has_high_risk_signal


class SafetyPolicyTests(unittest.TestCase):
    def test_direct_self_harm_intent_is_hard_high(self):
        result = detect_safety_signals("我今晚想结束自己的生命")

        self.assertTrue(result.hard_high)
        self.assertIn("CRISIS_DIRECT_SELF_HARM", result.signal_ids)
        self.assertEqual(result.policy_version, SAFETY_POLICY_VERSION)

    def test_active_attempt_is_hard_high(self):
        result = detect_safety_signals("我已经开始伤害自己了")

        self.assertTrue(result.hard_high)
        self.assertIn("CRISIS_ACTIVE_ATTEMPT", result.signal_ids)

    def test_direct_harm_to_others_is_hard_high(self):
        result = detect_safety_signals("我今晚准备伤害别人")

        self.assertTrue(result.hard_high)
        self.assertIn("CRISIS_DIRECT_HARM_TO_OTHERS", result.signal_ids)

    def test_explicit_negation_does_not_trigger_hard_rule(self):
        self.assertFalse(has_high_risk_signal("我没有想自杀，只是在了解相关知识"))
        self.assertFalse(has_high_risk_signal("我不想自杀，但最近压力很大"))

    def test_educational_reference_does_not_trigger_hard_rule(self):
        self.assertFalse(has_high_risk_signal("论文要讨论自杀预防的校园机制"))
        self.assertFalse(has_high_risk_signal("我想了解自杀预防的相关知识"))

    def test_ambiguous_distress_is_left_to_safety_model(self):
        result = detect_safety_signals("最近真的快撑不住了")

        self.assertFalse(result.hard_high)
        self.assertEqual(result.signal_ids, ())


if __name__ == "__main__":
    unittest.main()
