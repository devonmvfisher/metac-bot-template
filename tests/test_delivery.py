import asyncio
import json
from pathlib import Path
import threading
import time
import unittest
from fbot.config import SOL
from fbot.pipeline import forecast, forecast_async
from fbot.types import Research
from fbot import ops
from .fakes import FakeClock, FakeGit, FakeGitHub, FakeMetaculus, deps_for, fixture


class DeliveryTests(unittest.TestCase):
    def test_failed_test_run_never_says_ok(self):
        clock = FakeClock()
        github = FakeGitHub(clock.now())
        code = ops.run({}, {}, clock, github, FakeGit(clock.now()), test_failed=True)
        self.assertEqual(code, 1)
        self.assertIn("test run failed", json.dumps(github.writes))
        self.assertNotIn("test run ok", json.dumps(github.writes))
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/test_bot.yaml").read_text()
        self.assertIn("steps.install.outcome", workflow)
        self.assertIn("steps.run.outcome", workflow)
        self.assertIn("python3 -m fbot.ops --test-failed", workflow)
        self.assertIn("exit 1", workflow)

    def test_comment_retry_never_reposts_forecast(self):
        q, data = fixture("binary_long")
        deps = deps_for({SOL[0]: data["model_text"]})
        result = forecast(q, Research(), "C", deps)
        api = FakeMetaculus(deps.state)
        api.submit(q, result, comment_status=503)
        calls = []
        def send(method, url, headers, body, timeout):
            calls.append((method, url, body))
            return 200, {}
        api.gate.retry_comments(send, {"METACULUS_TOKEN": "FAKEKEY123"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("comments/create/"))
        self.assertTrue(calls[0][2]["is_private"])
        self.assertEqual(calls[0][2]["text"], result.comment)
        self.assertFalse(api.gate.missing_comments())
        self.assertEqual(len(deps.state.posted), 1)
        api.gate.retry_comments(send, {})
        self.assertEqual(len(calls), 1)

    def test_at_most_five_active_forecasts(self):
        q, data = fixture("binary_long")
        deps = deps_for({SOL[0]: data["model_text"]})
        lock, counts = threading.Lock(), {"active": 0, "peak": 0}
        original = deps.client.slot
        def tracked(*args, **kwargs):
            with lock:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            try:
                time.sleep(.04)
                return original(*args, **kwargs)
            finally:
                with lock:
                    counts["active"] -= 1
        deps.client.slot = tracked
        async def group():
            return await asyncio.gather(*(forecast_async(q, Research(), "C", deps) for _ in range(12)))
        self.assertEqual(len(asyncio.run(group())), 12)
        self.assertLessEqual(counts["peak"], 5)

    def test_bridge_parser_receives_provider_tier(self):
        from fbot.config import BRIDGE
        q, _ = fixture("binary_long")
        deps = deps_for({BRIDGE[0]: "malformed"})
        calls = []
        deps.parser = lambda question, text, tier: calls.append(tier) or .37
        result = forecast(q, Research(), "BRIDGE", deps)
        self.assertEqual(calls, ["BRIDGE"])
        self.assertAlmostEqual(result.value, .37)

    def test_observed_spend_is_available_in_counts_log(self):
        deps = deps_for({})
        deps.state.credit_before = 100
        deps.state.credit_after = 97
        deps.state.counts["attempted"] = 2
        self.assertEqual(deps.state.snapshot()["spend_per_question"], 1.5)
        text = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        self.assertIn("spend_per_question=%s", text)
