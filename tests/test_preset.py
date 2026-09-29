"""Strong-start settings against the production pacer, run log and ops path."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from fbot import SkipQuestion, ops
from fbot.budget import Pacer, choose, configure_preset, log_preset
from fbot.config import COSTS, FLOOR_CREDIT
from fbot.llm import Client
from fbot.state import RunState
from .adapter_fakes import load_adapter
from .fakes import FakeClock, FakeKey, FakeGit, FakeGitHub, fixture

ROOT = Path(__file__).resolve().parents[1]


class PresetTests(unittest.TestCase):
    def pacer(self, remaining=100, preset=None, reserve=None, mode="always"):
        clock = FakeClock()
        state = RunState(clock)
        env = {"OPENROUTER_API_KEY": "FAKEKEY123", "MINIBENCH_MODE": mode}
        if preset is not None:
            env["MODEL_PRESET"] = preset
        if reserve is not None:
            env["PRESET_RESERVE_USD"] = reserve
        client = Client(env, clock, state.alert, FakeKey(remaining))
        pacer = Pacer(env, client, state, clock)
        pacer.refresh()
        return pacer, state

    def test_P01_auto_default_and_explicit_match_existing_pacer(self):
        question, _ = fixture("binary_long")
        for preset in (None, "", "auto", " AUTO "):
            for credit in (None, 1, 2.7, 14, 100, 400, 1000):
                for forced in (False, True):
                    with self.subTest(preset=preset, credit=credit, forced=forced):
                        pacer, state = self.pacer(credit, preset)
                        try:
                            expected = choose(pacer.clock.now(), credit, "season")
                        except SkipQuestion as error:
                            with self.assertRaisesRegex(SkipQuestion, error.reason):
                                pacer.tier(question, forced_c=forced)
                        else:
                            expected = "C" if forced else expected
                            self.assertEqual(pacer.tier(question, forced_c=forced), expected)
                            self.assertEqual(pacer.reserved, COSTS[expected])
                        self.assertEqual(state.model_preset, "auto")
                        self.assertNotIn("PRESET_CONFIG_INVALID", state.alerts)

    def test_P02_A_B_C_used_with_sufficient_credit(self):
        question, _ = fixture("binary_long")
        for preset in ("A", "B", "C"):
            for forced in (False, True):
                pacer, state = self.pacer(1000, preset)
                self.assertEqual(pacer.tier(question, forced_c=forced), preset)
                self.assertEqual(pacer.reserved, COSTS[preset])
                self.assertEqual(state.tiers[preset], 1)
                self.assertEqual(state.preset_tiers, {preset})

    def test_P03_reserve_plus_cost_exact_and_below_falls_back(self):
        question, _ = fixture("binary_long")
        for preset in ("A", "B", "C"):
            exact = FLOOR_CREDIT + 10 + COSTS[preset]
            pacer, _ = self.pacer(exact, preset)
            self.assertEqual(pacer.tier(question), preset)
            below = exact - .01
            pacer, _ = self.pacer(below, preset)
            self.assertEqual(pacer.tier(question), choose(pacer.clock.now(), below, "season"))
            self.assertEqual(pacer.tier(question), "C")

    def test_P03_custom_reserve_and_pending_reservations(self):
        question, _ = fixture("binary_long")
        for reserve in ("0", "5", "20"):
            exact = FLOOR_CREDIT + float(reserve) + COSTS["A"]
            pacer, _ = self.pacer(exact, "A", reserve)
            self.assertEqual(pacer.tier(question), "A")
            below, _ = self.pacer(exact-.01, "A", reserve)
            self.assertEqual(below.tier(question), "C")
        pacer, state = self.pacer(14, "A")
        self.assertEqual(pacer.tier(question), "A")
        self.assertEqual(pacer.tier(question), "C")
        self.assertAlmostEqual(pacer.reserved, COSTS["A"] + COSTS["C"])
        self.assertEqual(state.preset_tiers, {"A", "C"})

    def test_P03_concurrent_questions_share_reservations(self):
        question, _ = fixture("binary_long")
        pacer, state = self.pacer(14, "A")
        with ThreadPoolExecutor(max_workers=5) as pool:
            tiers = list(pool.map(lambda _: pacer.tier(question), range(5)))
        self.assertEqual(tiers.count("A"), 1)
        self.assertEqual(tiers.count("C"), 4)
        self.assertAlmostEqual(pacer.reserved, COSTS["A"] + 4*COSTS["C"])

    def test_P03_unknown_credit_and_true_exhaustion_unchanged(self):
        question, _ = fixture("binary_long")
        pacer, state = self.pacer(None, "A")
        self.assertEqual(pacer.tier(question), "C")
        self.assertIn("CREDIT_UNKNOWN", state.alerts)
        pacer, state = self.pacer(1, "A")
        with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
            pacer.tier(question)
        self.assertIn("CREDITS_EXHAUSTED", state.alerts)

    def test_P04_MiniBench_keeps_every_auto_rule(self):
        question, _ = fixture("binary_long")
        question = replace(question, target="minibench")
        for preset in ("A", "B", "C"):
            for mode in ("always", "slack", "off"):
                for credit in (None, 1, 14, 100, 1000):
                    with self.subTest(preset=preset, mode=mode, credit=credit):
                        pacer, state = self.pacer(credit, preset, "0", mode)
                        try:
                            expected = choose(pacer.clock.now(), credit, "minibench", mode)
                        except SkipQuestion as error:
                            with self.assertRaisesRegex(SkipQuestion, error.reason):
                                pacer.tier(question)
                        else:
                            self.assertEqual(pacer.tier(question), expected)
                        self.assertFalse(state.preset_tiers)

    def test_P04_Test_Bot_and_bridge_keep_auto_rules(self):
        question, _ = fixture("binary_long")
        pacer, state = self.pacer(100, "A")
        self.assertEqual(pacer.tier(replace(question, target="test"), forced_c=True), "A")
        self.assertFalse(state.preset_tiers)
        clock = FakeClock()
        state = RunState(clock)
        env = {"MODEL_PRESET": "A", "USE_OPENAI_BRIDGE": "true", "OPENAI_API_KEY": "FAKEKEY123"}
        client = Client(env, clock, state.alert, FakeKey(None))
        pacer = Pacer(env, client, state, clock)
        self.assertEqual(pacer.tier(question), "BRIDGE")
        self.assertEqual(state.preset_tiers, {"BRIDGE"})
        with self.assertRaisesRegex(SkipQuestion, "BUDGET_MINIBENCH"):
            pacer.tier(replace(question, target="minibench"))

    def test_P05_bad_preset_uses_auto_and_sanitized_P1_alert(self):
        question, _ = fixture("binary_long")
        bad = "UNTRUSTED_VALUE\nforecast=secret"
        pacer, state = self.pacer(100, bad)
        self.assertEqual(pacer.tier(question), "C")
        configure_preset(pacer.env, state)
        self.assertEqual(state.counts["preset_invalid"], 1)
        self.assertEqual(ops.safe_counts(state.snapshot())["counts"]["preset_invalid"], 1)
        self.assertIn("PRESET_CONFIG_INVALID", state.alerts)
        self.assertNotIn("PRESET_CONFIG_INVALID", ops.P0)
        with self.assertLogs("fbot", level="INFO") as logs:
            log_preset(state)
        self.assertEqual(logs.output, ["INFO:fbot:PRESET auto tier=C"])
        github = FakeGitHub(state.clock.now())
        code = ops.run({"BOT_ENABLED": "true", "GITHUB_EVENT_NAME": "schedule"}, state.snapshot(), state.clock,
                       github, FakeGit(state.clock.now()))
        self.assertEqual(code, 0)
        self.assertTrue(any(item[0] == "create" and item[1] == "[BOT ALERT] PRESET_CONFIG_INVALID" for item in github.writes))
        public = json.dumps(state.snapshot()) + json.dumps(github.writes) + "\n".join(logs.output)
        self.assertNotIn("UNTRUSTED_VALUE", public)
        self.assertNotIn("forecast=secret", public)

    def test_P05_bad_reserve_uses_default_and_sanitized_alert(self):
        question, _ = fixture("binary_long")
        for value in ("bad", "-1", "NaN", "inf"):
            pacer, state = self.pacer(13, "A", value)
            self.assertEqual(state.preset_reserve_usd, 10)
            self.assertEqual(pacer.tier(question), "C")
            self.assertEqual(state.counts["preset_reserve_invalid"], 1)
            self.assertIn("PRESET_CONFIG_INVALID", state.alerts)
        for value in (None, "", " "):
            pacer, state = self.pacer(14, "A", value)
            self.assertEqual(state.preset_reserve_usd, 10)
            self.assertEqual(pacer.tier(question), "A")
            self.assertNotIn("PRESET_CONFIG_INVALID", state.alerts)

    def test_P06_one_run_log_covers_override_and_auto_fallback(self):
        module, _ = load_adapter()
        question, _ = fixture("binary_long")
        async def execute(args, env, clock, state):
            client = Client(env, clock, state.alert, FakeKey(14))
            pacer = Pacer(env, client, state, clock)
            pacer.refresh()
            self.assertEqual(pacer.tier(question), "A")
            self.assertEqual(pacer.tier(question), "C")
        env = {"OPENROUTER_API_KEY": "FAKEKEY123", "MODEL_PRESET": "A"}
        with patch.object(module, "setup_logs"), patch.object(module, "execute", side_effect=execute), \
             patch.object(module.os, "environ", env), patch.object(module.RunState, "write"), patch.object(module.ledger, "save"), \
             patch.object(sys, "argv", ["main.py"]), self.assertLogs("fbot", level="INFO") as logs:
            with self.assertRaises(SystemExit) as exited:
                module.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual([line for line in logs.output if "PRESET " in line], ["INFO:fbot:PRESET A tier=A,C"])

    def test_P06_no_questions_and_failure_still_log_once(self):
        for failed in (False, True):
            module, _ = load_adapter()
            async def execute(*args):
                if failed:
                    raise RuntimeError("not echoed")
            with patch.object(module, "setup_logs"), patch.object(module, "execute", side_effect=execute), \
                 patch.object(module.os, "environ", {"MODEL_PRESET": "B"}), patch.object(module.RunState, "write"), patch.object(module.ledger, "save"), \
                 patch.object(sys, "argv", ["main.py"]), self.assertLogs("fbot", level="INFO") as logs:
                with self.assertRaises(SystemExit) as exited:
                    module.main()
            self.assertEqual(exited.exception.code, int(failed))
            self.assertEqual([line for line in logs.output if "PRESET " in line], ["INFO:fbot:PRESET B tier=none"])

    def test_P07_repository_variables_and_docs(self):
        workflow = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        run = workflow.split("      - name: Run bot", 1)[1].split("      - name: Ops", 1)[0]
        for name in ("MODEL_PRESET", "PRESET_RESERVE_USD"):
            self.assertIn(name + ": ${{ vars." + name + " }}", run)
            self.assertNotIn("secrets." + name, workflow)
            for file in ("README.md", "RUNBOOK.md"):
                self.assertIn(name, (ROOT / file).read_text(encoding="utf-8"))
        self.assertIn("PRESET_CONFIG_INVALID", (ROOT / "RUNBOOK.md").read_text())
        self.assertIn("PRESET_RESERVE_USD", ops.STEPS["PRESET_CONFIG_INVALID"])
