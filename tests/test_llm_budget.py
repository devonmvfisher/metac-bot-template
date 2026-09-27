import json
import unittest
from fbot import CreditExhausted, ModelFailure, SkipQuestion
from fbot.budget import Pacer, choose, remaining_season
from fbot.config import OPUS, SOL, FLASH, CHEAP, BRIDGE
from fbot.llm import Client, ROUTER, DIRECT
from fbot.state import RunState
from fbot.targets import utc
from .fakes import FakeClock, FakeLLM, FakeKey, fixture


class ClientBudgetTests(unittest.TestCase):
    def setup_client(self, fake, env=None):
        self.clock = FakeClock()
        self.events = []
        return Client(env or {"OPENROUTER_API_KEY": "FAKEKEY123", "OPENAI_API_KEY": "FAKEKEY123"},
                      self.clock, self.events.append, fake)

    def test_body_headers_and_bridge(self):
        fake = FakeLLM()
        client = self.setup_client(fake)
        client.one(SOL[0], "question")
        method, url, headers, body, timeout = fake.calls[-1]
        self.assertEqual((method, url, timeout), ("POST", ROUTER, 480))
        key = "FAKEKEY123"
        self.assertEqual(headers["Authorization"], 'Bearer ' + key)
        self.assertEqual(body, {"model": SOL[0], "messages": [{"role": "user", "content": "question"}],
                                "reasoning": {"effort": "high"}, "max_tokens": 32000})
        client.one(BRIDGE[0], "question", bridge=True)
        self.assertEqual(fake.calls[-1][1], DIRECT)
        self.assertEqual(fake.calls[-1][3]["max_completion_tokens"], 32000)
        self.assertNotIn("max_tokens", fake.calls[-1][3])
        self.assertEqual(fake.calls[-1][3]["reasoning_effort"], "high")
        client.one(OPUS[0], "Reply OK", probe=True)
        self.assertGreaterEqual(fake.calls[-1][3]["max_tokens"], 4096)

    def test_model_fallback_and_once_events(self):
        for status in (400, 404):
            fake = FakeLLM({CHEAP[0]: [(status, {})] * 2})
            client = self.setup_client(fake)
            for _ in range(2):
                with self.assertLogs("fbot", level="INFO") as logs:
                    if _ == 1:
                        # No duplicate event is expected on the second call.
                        import logging
                        logging.getLogger("fbot").info("CHECK")
                    text, model = client.slot(CHEAP, "question")
                self.assertNotIn("FAKEKEY123", "\n".join(logs.output))
                self.assertEqual(model, CHEAP[1])
            self.assertEqual(self.events.count("MODEL_UNAVAILABLE"), 1)
            self.assertEqual(len(fake.calls), 4)

    def test_429_fallback_and_no_retry_auth(self):
        fake = FakeLLM({SOL[0]: [(429, {})]})
        client = self.setup_client(fake)
        self.assertEqual(client.slot(SOL + FLASH, "q")[1], FLASH[0])
        for code in (401, 403):
            fake = FakeLLM({SOL[0]: [(code, {})]})
            client = self.setup_client(fake)
            with self.assertRaises(ModelFailure):
                client.slot(SOL + FLASH, "q")
            self.assertEqual(len(fake.calls), 1)

    def test_credit_exhaustion_stops_future_calls(self):
        fake = FakeLLM({SOL[0]: [(402, {"error": {"message": "FAKEKEY123", "metadata": {"limit_source": "account"}}})]})
        client = self.setup_client(fake)
        with self.assertLogs("fbot", level="INFO") as logs:
            with self.assertRaises(CreditExhausted) as failure:
                client.one(SOL[0], "q")
        self.assertIn("limit_source=account", "\n".join(logs.output))
        self.assertNotIn("FAKEKEY123", "\n".join(logs.output) + str(failure.exception))
        with self.assertRaises(CreditExhausted):
            client.one(FLASH[0], "q")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("CREDITS_EXHAUSTED", self.events)

    def test_direct_insufficient_quota(self):
        fake = FakeLLM({BRIDGE[0]: [(429, {"error": {"code": "insufficient_quota"}})]})
        client = self.setup_client(fake)
        with self.assertRaises(CreditExhausted):
            client.one(BRIDGE[0], "q", bridge=True)
        self.assertEqual(len(fake.calls), 1)

    def test_retry_timeout_5xx_and_deadline(self):
        for error in (TimeoutError(), (500, {}), (503, {})):
            fake = FakeLLM({SOL[0]: [error]})
            client = self.setup_client(fake)
            self.assertEqual(client.one(SOL[0], "q"), "Probability: 37%")
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(self.clock.sleeps, [20])
        fake = FakeLLM({SOL[0]: [(503, {})]})
        client = self.setup_client(fake)
        with self.assertRaises(ModelFailure):
            client.one(SOL[0], "q", deadline=19)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][-1], 19)

    def test_empty_length_is_failure(self):
        fake = FakeLLM({SOL[0]: [(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})]})
        client = self.setup_client(fake)
        with self.assertRaises(ModelFailure):
            client.one(SOL[0], "q")
        self.assertEqual(len(fake.calls), 1)

    def test_key_reads_nested_remaining(self):
        for value in (100, 5, 1.5, None):
            client = self.setup_client(FakeKey(value))
            self.assertEqual(client.key(), (value, 100))
        fake = FakeLLM({"key": [(200, {"limit_remaining": 100, "data": {"usage": 4}})]})
        self.assertEqual(self.setup_client(fake).key(), (None, None))
        fake = FakeLLM({"key": [(402, {"error": {"metadata": {"limit_source": "FAKEKEY123"}}})]})
        with self.assertLogs("fbot", level="INFO") as logs:
            with self.assertRaises(CreditExhausted):
                self.setup_client(fake).key()
        self.assertNotIn("FAKEKEY123", "\n".join(logs.output))

    def test_scenario_table(self):
        cases = [(100, "2026-10-01", "always", "C", "C"),
                 (100, "2026-10-01", "slack", "C", None),
                 (400, "2026-10-01", "slack", "B", "C"),
                 (1000, "2026-10-01", "slack", "A", "C"),
                 (100, "2026-10-01", "off", "C", None),
                 (1.5, "2026-10-01", "always", None, None),
                 (100, "2026-12-20", "slack", "A", "C"),
                 (None, "2026-10-01", "slack", "C", None),
                 (None, "2026-10-01", "always", "C", "C")]
        for credit, day, mode, season, mini in cases:
            for target, expected in (("season", season), ("minibench", mini)):
                with self.subTest(credit=credit, target=target, mode=mode):
                    if expected is None:
                        with self.assertRaises(SkipQuestion):
                            choose(utc(day), credit, target, mode)
                    else:
                        self.assertEqual(choose(utc(day), credit, target, mode), expected)
        self.assertAlmostEqual(remaining_season(utc("2026-10-01")), 347.375)

    def test_pacer_low_unknown_exhaustion_and_reservations(self):
        q, _ = fixture("binary_long")
        for credit, limit, event in ((5, 100, "CREDITS_LOW"), (24, 100, "CREDITS_LOW"), (None, 100, "CREDIT_UNKNOWN")):
            client = self.setup_client(FakeKey(credit, limit))
            state = RunState(self.clock)
            pacer = Pacer(client.env, client, state, self.clock)
            pacer.refresh()
            self.assertIn(event, state.alerts)
        client = self.setup_client(FakeKey(2.70))
        state = RunState(self.clock)
        pacer = Pacer(client.env, client, state, self.clock)
        pacer.refresh()
        self.assertEqual(pacer.tier(q), "C")
        with self.assertRaises(SkipQuestion):
            pacer.tier(q)
        self.assertIn("CREDITS_EXHAUSTED", state.alerts)

    def test_bridge_rules(self):
        from dataclasses import replace
        q, _ = fixture("binary_long")
        clock = FakeClock()
        for env, tier in (({}, None), ({"OPENAI_API_KEY": "FAKEKEY123"}, None),
                          ({"OPENAI_API_KEY": "FAKEKEY123", "USE_OPENAI_BRIDGE": "true"}, "BRIDGE")):
            state = RunState(clock)
            client = Client(env, clock, state.alert, FakeLLM())
            pacer = Pacer(env, client, state, clock)
            if tier is None:
                with self.assertRaisesRegex(SkipQuestion, "NO_LLM_KEY"):
                    pacer.tier(q)
            else:
                self.assertEqual(pacer.tier(q), tier)
                with self.assertRaisesRegex(SkipQuestion, "BUDGET_MINIBENCH"):
                    pacer.tier(replace(q, target="minibench"))
                client.exhausted.add("bridge")
                with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
                    pacer.tier(q)
