"""v1.1 MiniBench dial (MINIBENCH_MODE=skip, MINIBENCH_PRESET, MINIBENCH_FLOOR_USD) on the production pacer."""
from dataclasses import replace
from pathlib import Path
import unittest
from fbot import SkipQuestion
from fbot.budget import Pacer, normalize_mode
from fbot.config import COSTS, FLOOR_CREDIT
from fbot.llm import Client
from fbot.state import RunState
from .fakes import FakeClock, FakeKey, fixture

ROOT = Path(__file__).resolve().parents[1]
CREDITS = (None, 0, 1, 3, 13, 14, 40, 73.45, 100, 400, 1000)


def make(credit, **settings):
    clock = FakeClock()
    state = RunState(clock)
    env = {"OPENROUTER_API_KEY": "FAKEKEY123", **{k: v for k, v in settings.items() if v is not None}}
    pacer = Pacer(env, Client(env, clock, state.alert, FakeKey(credit)), state, clock)
    pacer.refresh()
    return pacer, state


def outcome(pacer, question, fast=False):
    try:
        return pacer.tier(question, fast=fast), round(pacer.reserved, 9)
    except SkipQuestion as skip:
        return skip.reason, round(pacer.reserved, 9)


class DialTests(unittest.TestCase):
    def setUp(self):
        self.season, _ = fixture("binary_long")
        self.mini = replace(self.season, target="minibench")
        self.test_q = replace(self.season, target="test")

    def test_D01_blank_dial_changes_nothing(self):
        self.assertEqual(make(73.45)[0].tier(self.mini), "C")
        self.assertEqual(make(73.45, MINIBENCH_MODE="off")[0].tier(self.mini), "M")
        for mode in (None, "always", "slack", "off"):
            for preset in (None, "A"):
                for credit in CREDITS:
                    for fast in (False, True):
                        with self.subTest(mode=mode, preset=preset, credit=credit, fast=fast):
                            base = outcome(make(credit, MINIBENCH_MODE=mode, MODEL_PRESET=preset)[0], self.mini, fast)
                            for blank in ({"MINIBENCH_PRESET": "", "MINIBENCH_FLOOR_USD": ""},
                                          {"MINIBENCH_PRESET": " auto ", "MINIBENCH_FLOOR_USD": "  "},
                                          {"MINIBENCH_PRESET": "AUTO"}):
                                pacer, state = make(credit, MINIBENCH_MODE=mode, MODEL_PRESET=preset, **blank)
                                self.assertEqual(outcome(pacer, self.mini, fast), base)
                                self.assertIsNone(state.minibench_preset)
                                self.assertIsNone(state.minibench_floor_usd)
                                self.assertNotIn("PRESET_CONFIG_INVALID", state.alerts)

    def test_D02_skip_skips_minibench_at_every_credit_level(self):
        self.assertEqual(normalize_mode(" Skip "), "skip")
        for spelling in ("skip", " SKIP ", "Skip"):
            for credit in CREDITS:
                for extra in ({}, {"MINIBENCH_PRESET": "A", "MINIBENCH_FLOOR_USD": "0"}):
                    with self.subTest(spelling=spelling, credit=credit, extra=extra):
                        pacer, state = make(credit, MINIBENCH_MODE=spelling, MODEL_PRESET="A", **extra)
                        with self.assertRaises(SkipQuestion) as caught:
                            pacer.tier(self.mini)
                        if credit is None or credit >= FLOOR_CREDIT + COSTS["M"]:
                            self.assertEqual(caught.exception.reason, "BUDGET_MINIBENCH")
                        self.assertEqual(pacer.reserved, 0)
                        self.assertEqual(sum(state.tiers.values()), 0)
                        self.assertEqual(state.failures[self.mini.qid], 0)

    def test_D03_skip_treats_the_season_exactly_like_off(self):
        always = outcome(make(420, MINIBENCH_MODE="always")[0], self.season)
        self.assertNotEqual(outcome(make(420, MINIBENCH_MODE="off")[0], self.season), always)
        for preset in (None, "A", "C"):
            for credit in CREDITS + (420,):
                with self.subTest(preset=preset, credit=credit):
                    off = outcome(make(credit, MINIBENCH_MODE="off", MODEL_PRESET=preset)[0], self.season)
                    skip = outcome(make(credit, MINIBENCH_MODE="skip", MODEL_PRESET=preset)[0], self.season)
                    self.assertEqual(skip, off)
        self.assertEqual(make(73.45, MINIBENCH_MODE="skip", MODEL_PRESET="A")[0].tier(self.season), "A")

    def test_D04_preset_needs_the_reserve_plus_the_tier_cost(self):
        for mini in ("A", "B", "C"):
            for reserve in (None, "0", "20"):
                with self.subTest(mini=mini, reserve=reserve):
                    amount = 10.0 if reserve is None else float(reserve)
                    exact = FLOOR_CREDIT + amount + COSTS[mini]
                    pacer, state = make(exact + .01, MINIBENCH_PRESET=mini, PRESET_RESERVE_USD=reserve)
                    self.assertEqual(pacer.tier(self.mini), mini)
                    self.assertAlmostEqual(pacer.reserved, COSTS[mini])
                    self.assertEqual(state.tiers_by_target["minibench"][mini], 1)
                    below = outcome(make(exact - .01, MINIBENCH_PRESET=mini, PRESET_RESERVE_USD=reserve)[0], self.mini)
                    self.assertEqual(below, outcome(make(exact - .01, PRESET_RESERVE_USD=reserve)[0], self.mini))
        pacer, state = make(14, MINIBENCH_PRESET="A")
        self.assertEqual([pacer.tier(self.mini), pacer.tier(self.mini)], ["A", "C"])
        self.assertAlmostEqual(pacer.reserved, COSTS["A"] + COSTS["C"])
        self.assertEqual(make(1000, MINIBENCH_PRESET="a")[0].tier(self.mini), "A")
        pacer, state = make(1000, MINIBENCH_MODE="off", MINIBENCH_PRESET="B")
        self.assertEqual(pacer.tier(self.mini), "B")
        self.assertEqual(state.preset_tiers, set())
        self.assertEqual(make(None, MINIBENCH_PRESET="A")[0].tier(self.mini), "C")

    def test_D05_floor_skips_below_and_counts_pending_reservations(self):
        for preset, above in ((None, "C"), ("A", "A")):
            with self.subTest(preset=preset):
                self.assertEqual(outcome(make(39.99, MINIBENCH_FLOOR_USD="40", MINIBENCH_PRESET=preset)[0], self.mini),
                                 ("BUDGET_MINIBENCH", 0))
                self.assertEqual(make(40, MINIBENCH_FLOOR_USD="40", MINIBENCH_PRESET=preset)[0].tier(self.mini), above)
                self.assertEqual(make(41, MINIBENCH_FLOOR_USD=" 40 ", MINIBENCH_PRESET=preset)[0].tier(self.mini), above)
        self.assertEqual(outcome(make(None, MINIBENCH_FLOOR_USD="40")[0], self.mini), ("BUDGET_MINIBENCH", 0))
        self.assertEqual(make(None)[0].tier(self.mini), "C")
        self.assertEqual(make(3, MINIBENCH_FLOOR_USD="0")[0].tier(self.mini), "C")
        pacer, state = make(41, MINIBENCH_FLOOR_USD="40")
        self.assertEqual([outcome(pacer, self.mini)[0] for _ in range(3)], ["C", "C", "BUDGET_MINIBENCH"])
        self.assertEqual(state.minibench_floor_usd, 40.0)

    def test_D06_no_dial_value_changes_a_season_or_test_question(self):
        dials = ({"MINIBENCH_PRESET": "A"}, {"MINIBENCH_PRESET": "C"}, {"MINIBENCH_FLOOR_USD": "0"},
                 {"MINIBENCH_FLOOR_USD": "200"}, {"MINIBENCH_PRESET": "B", "MINIBENCH_FLOOR_USD": "1000"},
                 {"MINIBENCH_PRESET": "Z", "MINIBENCH_FLOOR_USD": "-5"})
        for preset in (None, "A", "C"):
            for credit in CREDITS:
                for question in (self.season, self.test_q):
                    for fast in (False, True):
                        base = outcome(make(credit, MODEL_PRESET=preset)[0], question, fast)
                        for dial in dials:
                            with self.subTest(preset=preset, credit=credit, target=question.target, fast=fast, dial=dial):
                                self.assertEqual(outcome(make(credit, MODEL_PRESET=preset, **dial)[0], question, fast), base)

    def test_D07_bad_values_alert_and_fall_back_without_echoing(self):
        for value in ("Z", "AA", "1", "auto-A"):
            with self.subTest(preset=value):
                with self.assertLogs("fbot", "WARNING") as logs:
                    pacer, state = make(73.45, MINIBENCH_PRESET=value)
                self.assertIn("PRESET_CONFIG_INVALID", state.alerts)
                self.assertEqual(state.counts["minibench_preset_invalid"], 1)
                self.assertIsNone(state.minibench_preset)
                self.assertEqual(pacer.tier(self.mini), "C")
                self.assertIn("CONFIG MINIBENCH_PRESET invalid; using auto", "\n".join(logs.output))
                self.assertNotIn(value, "\n".join(logs.output).replace("MINIBENCH_PRESET", "").replace("auto", ""))
        for value in ("-1", "abc", "nan", "inf", "1e999", "40usd"):
            with self.subTest(floor=value):
                with self.assertLogs("fbot", "WARNING") as logs:
                    pacer, state = make(39, MINIBENCH_FLOOR_USD=value)
                self.assertIn("PRESET_CONFIG_INVALID", state.alerts)
                self.assertEqual(state.counts["minibench_floor_invalid"], 1)
                self.assertIsNone(state.minibench_floor_usd)
                self.assertEqual(pacer.tier(self.mini), "C")
                self.assertIn("CONFIG MINIBENCH_FLOOR_USD invalid; using no floor", "\n".join(logs.output))
                self.assertNotIn(value, "\n".join(logs.output))
        pacer, state = make(73.45, MINIBENCH_PRESET="A", MINIBENCH_FLOOR_USD="45")
        self.assertNotIn("PRESET_CONFIG_INVALID", state.alerts)
        self.assertEqual((state.minibench_preset, state.minibench_floor_usd), ("A", 45.0))

    def test_D08_floor_and_skip_beat_the_fast_path(self):
        self.assertEqual(make(73.45)[0].tier(self.mini, fast=True), "M")
        self.assertEqual(make(1000, MINIBENCH_PRESET="A")[0].tier(self.mini, fast=True), "M")
        for settings in ({"MINIBENCH_MODE": "skip"}, {"MINIBENCH_FLOOR_USD": "100"},
                         {"MINIBENCH_MODE": "skip", "MINIBENCH_PRESET": "A"}):
            with self.subTest(settings=settings):
                self.assertEqual(outcome(make(73.45, **settings)[0], self.mini, fast=True), ("BUDGET_MINIBENCH", 0))

    def test_D09_dial_variables_are_wired_and_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
        steps = (ROOT / "DEVON-GITHUB-STEPS.md").read_text(encoding="utf-8")
        for workflow in ("run_bot_on_tournament.yaml", "run_bot_on_timer.yaml"):
            text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
            run = text.split("      - name: Run bot", 1)[1].split("      - name: Ops", 1)[0]
            for name in ("MINIBENCH_MODE", "MINIBENCH_PRESET", "MINIBENCH_FLOOR_USD"):
                self.assertIn(name + ": ${{ vars." + name + " }}", run, workflow)
                self.assertNotIn("secrets." + name, text, workflow)
        for name in ("MINIBENCH_PRESET", "MINIBENCH_FLOOR_USD"):
            self.assertIn("| " + name + " |", runbook)
            self.assertIn("- " + name + " = blank.", steps)
            self.assertIn(name, readme)
        mode_row = next(line for line in runbook.splitlines() if line.startswith("| MINIBENCH_MODE |"))
        self.assertIn("skip", mode_row)
        alert_row = next(line for line in runbook.splitlines() if line.startswith("| PRESET_CONFIG_INVALID |"))
        self.assertIn("MINIBENCH_PRESET", alert_row)
        self.assertIn("MINIBENCH_FLOOR_USD", alert_row)


if __name__ == "__main__":
    unittest.main()
