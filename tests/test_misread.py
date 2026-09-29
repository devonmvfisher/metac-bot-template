"""Acceptance tests for fbot.misread: drop a run only for a real misread (v1 item 7; review L03).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT).
Contract: INTERFACE.md section 7. The three benign sentences are the review's own probe inputs.
"""
import unittest
from fbot import guards, misread
from fbot.types import Question
from .vclock import ops_data

BINARY = Question(1, 2, "binary", "Synthetic")
MC = Question(3, 4, "multiple_choice", "Synthetic", options=("A", "B", "C"))
NUMERIC = Question(5, 6, "numeric", "Synthetic")


class MisreadTests(unittest.TestCase):
    def setUp(self):
        self.probes = ops_data("misread_probes")

    def test_patterns_are_the_spec_list(self):
        self.assertEqual(misread.PATTERNS, guards.MISREAD_PATTERNS)
        self.assertEqual(misread.WINDOW, 4)

    def test_three_benign_probe_sentences_no_longer_drop(self):
        self.assertEqual(len(self.probes["benign"]), 3)
        for sentence in self.probes["benign"]:
            self.assertTrue(guards.misread(sentence), sentence)  # v0 dropped every one of these
            self.assertFalse(misread.drop(BINARY, sentence, 0.20), sentence)
            self.assertFalse(misread.drop(MC, sentence, {"A": 0.5, "B": 0.3, "C": 0.2}), sentence)
        # The negated sentence is not a match at all, whatever the forecast.
        self.assertEqual(misread.matches(self.probes["benign"][1]), [])
        self.assertFalse(misread.drop(BINARY, self.probes["benign"][1], 0.99))

    def test_true_misread_still_drops(self):
        for sentence in self.probes["misread"]:
            self.assertTrue(misread.matches(sentence), sentence)
            self.assertTrue(misread.drop(BINARY, sentence, 0.99), sentence)
            self.assertTrue(misread.drop(BINARY, sentence, 0.01), sentence)
            self.assertTrue(misread.drop(MC, sentence, {"A": 0.97, "B": 0.02, "C": 0.01}), sentence)
            self.assertFalse(misread.drop(MC, sentence, {"A": 0.60, "B": 0.30, "C": 0.10}), sentence)
            self.assertFalse(misread.drop(NUMERIC, sentence, {10: 1.0, 90: 2.0}), sentence)

    def test_extreme_boundaries(self):
        self.assertFalse(misread.extreme("binary", 0.05))
        self.assertTrue(misread.extreme("binary", 0.0499))
        self.assertFalse(misread.extreme("binary", 0.95))
        self.assertTrue(misread.extreme("binary", 0.9501))
        self.assertTrue(misread.extreme("multiple_choice", {"A": 0.951, "B": 0.049}))
        self.assertFalse(misread.extreme("multiple_choice", {"A": 0.95, "B": 0.05}))
        self.assertFalse(misread.extreme("numeric", {10: 1.0}))
        self.assertFalse(misread.extreme("discrete", {10: 1.0}))
        for junk in ("0.99", float("nan"), None, True, {"A": "0.99"}):
            self.assertFalse(misread.extreme("binary", junk))
            self.assertFalse(misread.extreme("multiple_choice", junk))

    def test_negation_window_and_clauses(self):
        for text in ("The question has not already resolved.",
                     "It is unclear whether the outcome is known.",
                     "There is no sign that it has already happened.",
                     "The vote hasn't already resolved anything.",
                     "Do not assume the outcome is known."):
            self.assertEqual(misread.matches(text), [], text)
        for text in ("Reports say the event has already happened.",
                     "No. The question has already resolved YES.",
                     "Not that anyone doubts the recall has already happened."):
            self.assertTrue(misread.matches(text), text)
        self.assertEqual(misread.matches("This question has already resolved YES."),
                         ["has already resolved", "already resolved", r"resolved\s+(yes|no)"])


if __name__ == "__main__":
    unittest.main()
