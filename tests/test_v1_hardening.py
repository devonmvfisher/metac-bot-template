"""H1-H9: season selection, durable counts and repository settings."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import config, ledger, ops, schedule, targets
from fbot.state import RunState
from .adapter_fakes import load_adapter
from .fakes import FakeClock, FakeGit, FakeGitHub

ROOT = Path(__file__).resolve().parents[1]
VARIABLES = "BUDGET_CAP_USD BUDGET_CAP_OWN_USD OWN_KEY_MODE MODEL_PRESET PRESET_RESERVE_USD RESEARCH2_ENABLED MARKETS_ENABLED POLYMARKET_ENABLED MANIFOLD_ENABLED SOURCES_ENABLED NUMERIC_V1 PROMPTS_V1 DAILY_PROBE CONCURRENT_TARGETS CHAIN_ENABLED ASKNEWS_PARITY ASKNEWS_ARCHIVE DEADLINE_SHIFT SEASON_ID SEASON_SLUG SEASON_END_UTC ALLOW_NEW_SEASON".split()


class HardeningIntegrationTests(unittest.TestCase):
    def test_H1_failures_live_full_48_hours(self):
        clock = FakeClock("2026-10-01T23:59:00Z")
        book = ledger.Ledger(available=True)
        book.record_failure(91, 2, clock.now())
        clock.sleep(48 * 3600 - 1)
        book.prune(clock.now())
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "ledger.json"
            self.assertTrue(ledger.save(book, file, clock.now()))
            restored = ledger.load(file)
            self.assertEqual(restored.failure_count(91, clock.now()), 2)
            clock.sleep(1)
            restored.prune(clock.now())
            self.assertEqual(restored.failure_count(91, clock.now()), 0)

    def test_H1_monthly_counts_and_sanitization(self):
        book = ledger.Ledger({"asknews": {"2026-10": 899, "secret": "FAKEKEY"}}, available=True)
        now = FakeClock().now()
        self.assertFalse(book.asknews_charge(now, archive=True))
        self.assertTrue(book.asknews_charge(now))
        self.assertTrue(book.asknews_charge(now))
        self.assertEqual(book.data["asknews"], {"2026-10": 901})
        self.assertTrue(book.asknews_charge(now.replace(month=11), archive=True))
        self.assertNotIn("FAKEKEY", json.dumps(book.data))

    def test_H2_defaults_empty_known_and_allow_new(self):
        now = FakeClock().now()
        default = targets.season()
        self.assertEqual((default.id, default.slug, default.end, default.valid), (33121, "fall-futureeval-2026", "2027-01-07T00:00:00Z", True))
        self.assertEqual(targets.season({name: " " for name in ("SEASON_ID", "SEASON_SLUG", "SEASON_END_UTC")}), default)
        for env in ({"SEASON_ID": "9"}, {"SEASON_SLUG": "bad"}, {"SEASON_END_UTC": "2027-02-01Z"}, {"SEASON_ID": "invalid"}):
            self.assertFalse(targets.season(env).valid)
            self.assertEqual(targets.active(now, env), ["minibench"])
        env = {"SEASON_ID": "99991", "SEASON_SLUG": "future-season", "SEASON_END_UTC": "2028-01-01T00:00:00Z", "ALLOW_NEW_SEASON": "true"}
        self.assertTrue(targets.season(env).valid)
        self.assertEqual(targets.active(now, env), [99991, "minibench"])
        self.assertEqual(targets.active(targets.utc("2028-01-01Z"), env), [])
        env["SEASON_ID"] = "33121"
        self.assertFalse(targets.season(env).valid)

    def test_H2_invalid_season_schedules_only_minibench_and_P0(self):
        module, sdk = load_adapter()
        clock, state, names = FakeClock(), RunState(FakeClock()), []
        async def run(pollers, *args, **kwargs):
            names.extend(pollers)
        env = {"BOT_ENABLED": "true", "SEASON_ID": "bad", "DAILY_PROBE": "false"}
        client = SimpleNamespace(key=lambda: (None, None), exhausted=set())
        with patch.object(module, "Client", return_value=client), patch.object(module, "open_count", return_value=0), patch.object(module, "install_post_gate"), patch.object(module.schedule, "run_targets", side_effect=run), patch.object(state, "write"):
            asyncio.run(module.execute(SimpleNamespace(mode="tournament", loop_minutes=45, poll_minutes=10), env, clock, state))
        self.assertEqual(names, ["minibench"])
        self.assertIn("TARGET_MISMATCH", state.alerts)
        self.assertIn("TARGET_MISMATCH", ops.P0)

    def test_H4_variables_passed_and_test_preset_input(self):
        for name in ("run_bot_on_tournament.yaml", "run_bot_on_timer.yaml", "test_bot.yaml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            run = text.split("      - name: Run bot", 1)[1].split("      - name:", 1)[0]
            for variable in VARIABLES:
                self.assertIn(variable + ": ${{ vars." + variable + " }}", run)
            self.assertIn("OPENROUTER_API_KEY_OWN: ${{ secrets.OPENROUTER_API_KEY_OWN }}", run)
        test = (ROOT / ".github/workflows/test_bot.yaml").read_text()
        self.assertIn("default: C", test)
        self.assertIn("options: [auto, A, B, C]", test)
        self.assertIn("TEST_PRESET: ${{ inputs.preset }}", test)

    def test_H4_invalid_switches_use_default_once(self):
        for name, default in (("NUMERIC_V1", True), ("ASKNEWS_ARCHIVE", False)):
            schedule._warned.discard(name)
            with self.assertLogs("fbot", level="INFO") as logs:
                for _ in range(2):
                    self.assertIs(schedule.switch({name: "FAKEKEY"}, name, default=default), default)
            self.assertEqual(len(logs.output), 1)
            self.assertNotIn("FAKEKEY", logs.output[0])

    def test_H5_own_key_alert_is_weekly_and_both_balances_visible(self):
        clock = FakeClock()
        github = FakeGitHub(clock.now())
        state = RunState(clock)
        state.alert("USING_OWN_KEY")
        state.credit_after, state.credit_own = 13, 51
        env = {"BOT_ENABLED": "true"}
        ops.run(env, state.snapshot(), clock, github, FakeGit(clock.now()))
        first = len([entry for entry in github.writes if "USING_OWN_KEY" in json.dumps(entry)])
        clock.sleep(6 * 86400)
        ops.run(env, state.snapshot(), clock, github, FakeGit(clock.now()))
        self.assertEqual(len([entry for entry in github.writes if "USING_OWN_KEY" in json.dumps(entry)]), first)
        body = ops.weekly_body(state.snapshot(), {}, 0, ".", True, env)
        self.assertIn("credit_after=13", body)
        self.assertIn("credit_own=51", body)

    def test_H8_ascii_and_H9_version(self):
        for path in (ROOT / "fbot").glob("*.py"):
            path.read_bytes().decode("ascii")
        self.assertEqual(config.VERSION, "v1")
