from dataclasses import replace
from datetime import timedelta
import json
import unittest
from fbot import CreditExhausted, ModelFailure, SkipQuestion
from fbot import comment, metadata
from fbot.budget import choose, Pacer
from fbot.config import SOL, OPUS, FLASH
from fbot.io_limits import call_scope, bounded_timeout
from fbot.llm import Client
from fbot.pipeline import forecast
from fbot.types import Research
from .fakes import FakeClock, FakeLLM, FakeMetaculus, deps_for, fixture, fixture_prepare, narration


class IntegrityTests(unittest.TestCase):
    def test_send_point_deadline_and_season(self):
        q, data = fixture("binary_long")
        deps = deps_for({SOL[0]: data["model_text"]})
        deps.deadline = 10
        result = forecast(q, Research(), "C", deps)
        api = FakeMetaculus(deps.state)
        api.gate.register(q, result)
        deps.clock.sleep(result.deadline + 1)
        with self.assertRaisesRegex(SkipQuestion, "TOO_LATE"):
            api.send("/api/questions/forecast/", [{"question": q.qid, "probability_yes": result.value}])
        self.assertFalse(api.requests)
        result.deadline = None
        deps.clock.date = FakeClock("2027-01-07T00:00:00Z").date
        with self.assertRaisesRegex(SkipQuestion, "AFTER_SEASON"):
            api.send("/api/questions/forecast/", [{"question": q.qid, "probability_yes": result.value}])

    def test_endpoint_change_cannot_bypass_gate(self):
        deps = deps_for({})
        api = FakeMetaculus(deps.state)
        for url in ("https://www.metaculus.com/api2/questions/forecast/",
                    "https://api.metaculus.com/unknown"):
            with self.assertRaises(SkipQuestion):
                api.gate.before("POST", url, {})

    def test_private_comment_overrides_framework_wrapper(self):
        q, data = fixture("binary_long")
        deps = deps_for({SOL[0]: data["model_text"]})
        result = forecast(q, Research(), "C", deps)
        api = FakeMetaculus(deps.state)
        api.submit(q, result)
        self.assertTrue(api.requests[-1][1]["is_private"])
        self.assertTrue(api.requests[-1][1]["text"].startswith("FBOT "))

    def test_sdk_timeout_scope(self):
        clock = FakeClock()
        self.assertEqual(bounded_timeout(None, clock), 60)
        with call_scope(200):
            self.assertEqual(bounded_timeout(None, clock), 200)
            self.assertEqual(bounded_timeout((20, None), clock), (20, 200))
            clock.sleep(201)
            with self.assertRaisesRegex(SkipQuestion, "TOO_LATE"):
                bounded_timeout(999, clock)
        self.assertEqual(bounded_timeout(None, clock), 60)

    def test_unknown_initial_credit_does_not_invent_spend(self):
        deps = deps_for({})
        env = {"OPENROUTER_API_KEY": "FAKEKEY123"}
        fake = FakeLLM({"key": [(200, {"data": {"limit_remaining": None}}),
                                 (200, {"data": {"limit_remaining": 90}})]})
        client = Client(env, deps.clock, deps.state.alert, fake)
        pacer = Pacer(env, client, deps.state, deps.clock)
        pacer.refresh()
        pacer.refresh()
        self.assertIsNone(deps.state.spend())
        self.assertEqual(choose(deps.clock.now(), 2.54, "season"), "C")
        self.assertEqual(choose(deps.clock.now(), 2.46, "season"), "M")

    def test_misread_retry_failure_stays_misread_all(self):
        q, _ = fixture("binary_long")
        calls = [0]
        def value(prompt):
            calls[0] += 1
            return "already resolved\nProbability: 99%" if calls[0] <= 3 else "unparseable"
        deps = deps_for({SOL[0]: value, FLASH[0]: value})
        with self.assertRaisesRegex(SkipQuestion, "MISREAD_ALL"):
            forecast(q, Research(), "C", deps)

    def test_parser_credit_exception_is_not_parse_failure(self):
        q, _ = fixture("binary_long")
        deps = deps_for({SOL[0]: "malformed"})
        def parser(*args):
            raise CreditExhausted()
        deps.parser = parser
        with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
            forecast(q, Research(), "C", deps)

    def test_tail_summary_uses_bound_not_invented_quantile(self):
        q, _ = fixture("numeric_open")
        values = [.15 + .70 * i / 200 for i in range(201)]
        self.assertEqual(comment.quantile_label(q, values, .1), "<0")
        self.assertEqual(comment.quantile_label(q, values, .9), ">100")

    def test_open_counts_use_question_status_and_pagination(self):
        calls = []
        def send(*args):
            calls.append(args)
            if len(calls) == 1:
                return 200, {"results": [{"question": {"status": "open"}}, {"question": {"status": "closed"}}], "next": "next"}
            return 200, {"results": [{"question": {"status": "open"}}, {"group_of_questions": []}], "next": None}
        self.assertEqual(metadata.open_count(33121, {}, send), 2)
        self.assertIn("offset=2", calls[1][1])
        self.assertEqual(metadata.open_count(33121, {}, lambda *args: (403, {})), "unknown")
