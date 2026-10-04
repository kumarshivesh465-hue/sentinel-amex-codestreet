"""Tests for the agent layer. Standard library only, so no installs needed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agents


class IntentClassificationTests(unittest.TestCase):
    def test_lost_stolen(self):
        for text in ("I lost my card", "my card was stolen", "card is missing"):
            self.assertEqual(agents.classify_intent(text)["intent"], "LOST_STOLEN", text)

    def test_dispute(self):
        for text in (
            "there's a charge I don't recognize",
            "unauthorized transaction on my account",
            "I didn't make this purchase",
        ):
            self.assertEqual(agents.classify_intent(text)["intent"], "DISPUTE", text)

    def test_fee_reversal(self):
        for text in ("please waive my annual fee", "reverse the late fee", "I was overcharged"):
            self.assertEqual(agents.classify_intent(text)["intent"], "FEE_REVERSAL", text)

    def test_credit_limit(self):
        for text in ("I want a credit limit increase", "can you raise my limit"):
            self.assertEqual(agents.classify_intent(text)["intent"], "CREDIT_LIMIT", text)

    def test_explicit_human_request(self):
        for text in (
            "I want to speak to a human",
            "connect me with a representative",
            "let me talk to a real person",
        ):
            self.assertEqual(agents.classify_intent(text)["intent"], "HUMAN_REQUEST", text)

    def test_bare_nouns_do_not_mean_human_request(self):
        """Regression: 'the agent charged me a fee' is a fee complaint."""
        result = agents.classify_intent("The agent on the phone charged me a fee")
        self.assertEqual(result["intent"], "FEE_REVERSAL")

    def test_unknown(self):
        self.assertEqual(agents.classify_intent("hello there")["intent"], "UNKNOWN")

    def test_ambiguous_message_is_flagged(self):
        result = agents.classify_intent("I lost my card and also want a credit limit increase")
        self.assertTrue(result["ambiguous"])
        self.assertTrue(result["secondary_intents"])

    def test_ambiguous_lowers_confidence(self):
        single = agents.classify_intent("I lost my card")
        double = agents.classify_intent("I lost my card and want a credit limit increase")
        self.assertLess(double["confidence"], single["confidence"])


class AmountExtractionTests(unittest.TestCase):
    def test_dollar_amount(self):
        self.assertEqual(agents.extract_amount("reverse the $95.50 fee"), 95.50)

    def test_thousands_separator(self):
        self.assertEqual(agents.extract_amount("a $1,250.00 charge"), 1250.00)

    def test_context_wording(self):
        self.assertEqual(agents.extract_amount("I was charged 240 for this"), 240.0)

    def test_decimal_without_symbol(self):
        self.assertEqual(agents.extract_amount("the fee was 89.99"), 89.99)

    def test_card_number_is_not_an_amount(self):
        """Regression: a bare integer used to parse as money."""
        self.assertIsNone(agents.extract_amount("my card ending 1234 was lost"))

    def test_year_is_not_an_amount(self):
        self.assertIsNone(agents.extract_amount("I have been a customer since 2019"))

    def test_no_amount(self):
        self.assertIsNone(agents.extract_amount("I lost my card"))


class SentimentTests(unittest.TestCase):
    def test_neutral(self):
        self.assertEqual(agents.detect_sentiment("I lost my card")["label"], "neutral")

    def test_high_frustration(self):
        result = agents.detect_sentiment("This is absolutely unacceptable and I am furious")
        self.assertEqual(result["label"], "high")
        self.assertGreaterEqual(result["score"], 0.75)

    def test_shouting_detected(self):
        result = agents.detect_sentiment("THIS IS RIDICULOUS AND UNACCEPTABLE")
        self.assertIn("shouting", result["signals"])

    def test_legal_threat(self):
        self.assertGreaterEqual(agents.detect_sentiment("I will take legal action")["score"], 0.9)


class DeterminismTests(unittest.TestCase):
    def test_stable_int_in_range(self):
        for seed in ("SES-1", "SES-2", "another"):
            value = agents.stable_int(seed, 15, 55)
            self.assertGreaterEqual(value, 15)
            self.assertLessEqual(value, 55)

    def test_stable_int_is_repeatable(self):
        first = agents.stable_int("SES-ABC", 15, 55)
        second = agents.stable_int("SES-ABC", 15, 55)
        self.assertEqual(first, second)

    def test_credit_resolution_is_repeatable(self):
        """Regression: random.randint made the same case show new numbers."""
        first = agents.resolve_credit_limit("SES-ABC")
        second = agents.resolve_credit_limit("SES-ABC")
        self.assertEqual(first["payload"], second["payload"])


class ResolverTests(unittest.TestCase):
    def test_lost_stolen_is_autonomous(self):
        self.assertTrue(agents.resolve_lost_stolen()["autonomous"])

    def test_fee_under_limit(self):
        result = agents.resolve_fee_reversal(50.0)
        self.assertTrue(result["autonomous"])
        self.assertFalse(result["payload"]["amount_assumed"])

    def test_fee_over_limit(self):
        self.assertFalse(agents.resolve_fee_reversal(500.0)["autonomous"])

    def test_assumed_amount_is_flagged_and_lower_confidence(self):
        assumed = agents.resolve_fee_reversal(None)
        explicit = agents.resolve_fee_reversal(95.0)
        self.assertTrue(assumed["payload"]["amount_assumed"])
        self.assertLess(assumed["confidence"], explicit["confidence"])

    def test_dispute_over_limit(self):
        self.assertFalse(agents.resolve_dispute(5000.0)["autonomous"])


class PolicyGatewayTests(unittest.TestCase):
    def test_fee_within_limit_passes(self):
        self.assertTrue(agents.policy_gate("FEE_REVERSAL", {"amount": 100.0})["passed"])

    def test_fee_over_limit_blocked(self):
        self.assertFalse(agents.policy_gate("FEE_REVERSAL", {"amount": 200.0})["passed"])

    def test_exactly_at_limit_passes(self):
        self.assertTrue(
            agents.policy_gate("FEE_REVERSAL", {"amount": agents.FEE_WAIVER_LIMIT})["passed"]
        )

    def test_dispute_over_limit_blocked(self):
        self.assertFalse(
            agents.policy_gate("DISPUTE_PROVISIONAL_CREDIT", {"amount": 500.0})["passed"]
        )

    def test_unrelated_action_passes(self):
        self.assertTrue(agents.policy_gate("CARD_REISSUE", {})["passed"])


class ExplainabilityTests(unittest.TestCase):
    def test_every_action_has_a_rationale(self):
        for action in agents.RATIONALE_TEMPLATES:
            text = agents.explain(action)
            self.assertNotIn("No rationale template", text, action)

    def test_template_fills_values(self):
        text = agents.explain(
            "FEE_REVERSAL", amount=50.0, limit=150.0, policy="FEE-WAIVER-02"
        )
        self.assertIn("$50.00", text)
        self.assertIn("$150.00", text)

    def test_missing_value_does_not_raise(self):
        self.assertIsInstance(agents.explain("FEE_REVERSAL"), str)


class QualityScoreTests(unittest.TestCase):
    def test_high_confidence_scores_well(self):
        result = agents.quality_score(0.97, policy_passed=True, sentiment={"score": 0.0})
        self.assertGreaterEqual(result["score"], 85)
        self.assertEqual(result["band"], "excellent")

    def test_policy_failure_penalised(self):
        passed = agents.quality_score(0.9, policy_passed=True)
        failed = agents.quality_score(0.9, policy_passed=False)
        self.assertGreater(passed["score"], failed["score"])

    def test_assumed_amount_penalised(self):
        clean = agents.quality_score(0.9)
        assumed = agents.quality_score(0.9, assumed_amount=True)
        self.assertLess(assumed["score"], clean["score"])

    def test_score_bounded(self):
        result = agents.quality_score(5.0, ambiguous=True, assumed_amount=True)
        self.assertLessEqual(result["score"], 100.0)
        self.assertGreaterEqual(result["score"], 0.0)


class CsatPredictionTests(unittest.TestCase):
    def test_prediction_is_bounded(self):
        for confidence in (0.0, 0.5, 1.0):
            predicted = agents.predict_csat(confidence)["predicted"]
            self.assertGreaterEqual(predicted, 1.0)
            self.assertLessEqual(predicted, 5.0)

    def test_escalation_lowers_prediction(self):
        base = agents.predict_csat(0.9, resolved=True)["predicted"]
        escalated = agents.predict_csat(0.9, escalated=True)["predicted"]
        self.assertLess(escalated, base)

    def test_high_frustration_lowers_prediction(self):
        calm = agents.predict_csat(0.9, sentiment={"score": 0.0})["predicted"]
        angry = agents.predict_csat(0.9, sentiment={"score": 0.9})["predicted"]
        self.assertLess(angry, calm)

    def test_bands(self):
        self.assertEqual(agents.predict_csat(0.97, resolved=True)["band"], "likely_promoter")
        at_risk = agents.predict_csat(0.2, sentiment={"score": 1.0}, escalated=True)
        self.assertEqual(at_risk["band"], "at_risk")

    def test_drivers_are_reported(self):
        self.assertTrue(agents.predict_csat(0.9)["drivers"])

    def test_deterministic(self):
        first = agents.predict_csat(0.8, sentiment={"score": 0.2}, resolved=True)
        second = agents.predict_csat(0.8, sentiment={"score": 0.2}, resolved=True)
        self.assertEqual(first, second)


class MoneyTests(unittest.TestCase):
    def test_to_cents_rounds_half_up(self):
        from backend import money

        self.assertEqual(money.to_cents(150.0), 15000)
        self.assertEqual(money.to_cents(95.555), 9556)
        self.assertEqual(money.to_cents("10.10"), 1010)

    def test_limit_boundary_is_exact(self):
        # $150.00 must pass and $150.01 must not; float comparison made this risky.
        self.assertTrue(agents.policy_gate("FEE_REVERSAL", {"amount": 150.00})["passed"])
        self.assertFalse(agents.policy_gate("FEE_REVERSAL", {"amount": 150.01})["passed"])

    def test_cents_payload_is_respected(self):
        self.assertFalse(agents.policy_gate("FEE_REVERSAL", {"amount_cents": 20000})["passed"])
        self.assertTrue(agents.policy_gate("FEE_REVERSAL", {"amount_cents": 15000})["passed"])

    def test_resolver_payload_carries_cents(self):
        self.assertEqual(agents.resolve_fee_reversal(50.0)["payload"]["amount_cents"], 5000)
        self.assertEqual(agents.resolve_dispute(None)["payload"]["amount_cents"], 8400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
