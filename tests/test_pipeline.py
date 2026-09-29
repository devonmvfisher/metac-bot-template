import asyncio
import io
import json
import logging
import random
import re
import unittest
from dataclasses import replace
from pathlib import Path
from fbot import CreditExhausted, ModelFailure, SkipQuestion
from fbot import aggregate, comment, parse, prompts, validate
from fbot.config import OPUS, SOL, FLASH, BRIDGE, PERCENTILES, researcher_slot
from fbot.pipeline import forecast, forecast_async
from fbot.research import Service, credentials
from fbot.types import Research
from .fakes import FakeAskNews, FakeMetaculus, deps_for, fixture, fixture_prepare, narration


class PipelineTests(unittest.TestCase):
    def test_all_fail_no_post_or_comment(self):
        # v1-r3 F09: no reply at all (status 0, the fake default) is MODEL_TRANSIENT; a 404 on every slot is not.
        for scripts, reason in (({}, "MODEL_TRANSIENT"),
                                ({slot[0]: ModelFailure(404) for slot in (OPUS, SOL, FLASH)}, "ALL_MODELS_FAILED")):
            q, _ = fixture("binary_long")
            deps = deps_for(scripts)
            api = FakeMetaculus(deps.state)
            with self.assertRaisesRegex(SkipQuestion, reason):
                api.submit(q, forecast(q, Research(), "A", deps))
            self.assertEqual(api.requests, [])

    def test_two_of_three(self):
        q, _ = fixture("binary_long")
        deps = deps_for({OPUS[0]: narration("Probability: 20%"), SOL[0]: ModelFailure(), FLASH[0]: narration("Probability: 90%")})
        result = forecast(q, Research(), "A", deps)
        self.assertAlmostEqual(result.value, .2)
        self.assertIn("3/5 ok", result.summary)
        self.assertIn("run3:failed", result.summary)

    def test_500_seeded_mixed_trials(self):
        rng = random.Random(50519)
        names = ("binary_long", "mc_three", "numeric_closed")
        for trial in range(500):
            q, data = fixture(names[trial % 3])
            scripts, valid, weights = {}, [], []
            for slot in (OPUS, SOL, FLASH):
                kind = rng.randrange(3)
                if kind == 0:
                    scripts[slot[0]] = ModelFailure()
                elif kind == 1:
                    scripts[slot[0]] = "unparseable output"
                else:
                    if q.kind == "binary":
                        amount = rng.uniform(.1, .9)
                        text = f"Probability: {amount}"
                    elif q.kind == "multiple_choice":
                        amount = rng.uniform(.05, .8)
                        text = f"Red: {amount}\nGreen: {(1-amount)/3}\nBlue: {(1-amount)*2/3}"
                    else:
                        shift = rng.uniform(-2, 2)
                        text = "\n".join(f"Percentile {p}: {p + shift}" for p in PERCENTILES)
                    scripts[slot[0]] = narration(text)
                    repetitions, weight = (1, 1) if slot == FLASH else (2, 2)
                    valid.extend([parse.parse(q, text)] * repetitions)
                    weights.extend([weight] * repetitions)
            deps = deps_for(scripts, fixture_prepare(data))
            api = FakeMetaculus(deps.state)
            if not valid:
                with self.assertRaises(SkipQuestion):
                    api.submit(q, forecast(q, Research(), "A", deps))
                self.assertFalse(api.requests)
            else:
                result = forecast(q, Research(), "A", deps)
                expected = aggregate.combine(q.kind, valid, q.options, weights)
                if q.kind == "multiple_choice":
                    expected = validate.repair(expected)
                self.assertEqual(result.value, expected)
                api.submit(q, result)
                self.assertTrue(api.requests[1][1]["is_private"])

    def test_fixtures_final_equals_exact_payload_and_private_comment(self):
        for path in sorted((Path(__file__).parent / "fixtures").glob("*.json")):
            q, data = fixture(path.stem)
            with self.subTest(name=path.stem):
                self.assertNotEqual(q.qid, q.post_id)
                deps = deps_for({SOL[0]: data["model_text"]}, fixture_prepare(data))
                result = forecast(q, Research(), "C", deps)
                api = FakeMetaculus(deps.state)
                api.submit(q, result)
                posted = api.requests[0][1][0]
                body = api.requests[1][1]
                self.assertEqual(posted["question"], q.qid)
                self.assertEqual(body["on_post"], q.post_id)
                self.assertTrue(body["is_private"])
                self.assertTrue(body["included_forecast"])
                self.assertEqual(body["text"], result.comment)
                prefix = "\n".join(result.comment.splitlines()[:2])
                self.assertLessEqual(len(prefix), 200)
                self.assertTrue(prefix.startswith("FBOT "))
                self.assertTrue(prefix.splitlines()[1].startswith("FINAL "))
                self.assertNotIn("see line", prefix)
                self.assertLessEqual(len(result.summary), 1000)
                self.assertLessEqual(len(result.comment), 10000)
                if q.kind == "binary":
                    printed = float(prefix.splitlines()[1][6:-1])
                    self.assertEqual(printed, round(posted["probability_yes"] * 100, 1))
                elif q.kind == "multiple_choice":
                    lines = "\n".join(line for line in result.summary.splitlines() if line.startswith("FINAL"))
                    values = [float(v) for v in re.findall(r"=([\d.]+)%", lines)]
                    self.assertEqual(values, [round(v * 100, 1) for v in posted["probability_yes_per_category"].values()])
                else:
                    for p in (10, 50, 90):
                        printed = re.search(f"p{p}=([^, ]+)", prefix)[1]
                        self.assertEqual(printed, f"{comment.quantile(q, posted['continuous_cdf'], p/100):.6g}")

    def test_gate_rejects_changed_value_and_wrong_ids(self):
        q, data = fixture("binary_long")
        deps = deps_for({SOL[0]: data["model_text"]})
        result = forecast(q, Research(), "C", deps)
        api = FakeMetaculus(deps.state)
        api.gate.register(q, result)
        for qid, value in ((q.post_id, result.value), (q.qid, .75)):
            with self.assertRaises(SkipQuestion):
                api.send("/api/questions/forecast/", [{"question": qid, "probability_yes": value}])
        self.assertEqual(api.requests, [])
        api.submit(q, result, comment_status=400)
        self.assertIn("API_REJECTED", deps.state.alerts)
        self.assertEqual(len(api.gate.missing_comments()), 1)
        with self.assertRaises(SkipQuestion):
            api.submit(q, result)

    def test_misread_retry_once_and_warning(self):
        q, _ = fixture("binary_long")
        bad = narration("Probability: 99%") + "\nOutcome is known"
        deps = deps_for({OPUS[0]: bad, SOL[0]: bad, FLASH[0]: bad})
        with self.assertRaisesRegex(SkipQuestion, "MISREAD_ALL"):
            forecast(q, Research(), "A", deps)
        self.assertEqual(len(deps.client.calls), 8)
        self.assertIn("WARNING", deps.client.calls[-1][1])
        calls = [0]
        def second_good(prompt):
            calls[0] += 1
            return bad if calls[0] <= 2 else narration("Probability: 37%")
        deps = deps_for({OPUS[0]: bad, SOL[0]: second_good, FLASH[0]: bad})
        result = forecast(q, Research(), "A", deps)
        self.assertIn("RETRY", result.summary)
        self.assertEqual(result.value, .37)

    def test_credit_during_ensemble_must_skip_partial_results(self):
        q, _ = fixture("binary_long")
        deps = deps_for({OPUS[0]: CreditExhausted(), SOL[0]: narration("Probability: 37%"), FLASH[0]: ModelFailure()})
        with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
            forecast(q, Research(), "A", deps)

    def test_bridge_fallback(self):
        q, _ = fixture("binary_long")
        deps = deps_for({SOL[0]: CreditExhausted(), BRIDGE[0]: narration("Probability: 37%")})
        deps.env = {"USE_OPENAI_BRIDGE": "true", "OPENAI_API_KEY": "FAKEKEY123"}
        result = forecast(q, Research(), "C", deps)
        self.assertEqual(result.tier, "BRIDGE")
        self.assertIn("FALLBACK", result.summary)
        with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
            forecast(replace(q, target="minibench"), Research(), "C", deps)

    def test_invalid_numeric_no_post(self):
        q, data = fixture("numeric_closed")
        broken = dict(data, cdf=[0, 1])
        deps = deps_for({SOL[0]: data["model_text"]}, fixture_prepare(broken))
        api = FakeMetaculus(deps.state)
        with self.assertRaisesRegex(SkipQuestion, "INVALID_OUTPUT"):
            api.submit(q, forecast(q, Research(), "C", deps))
        self.assertFalse(api.requests)

    def test_regex_first_parser_only_when_needed(self):
        q, _ = fixture("binary_long")
        deps = deps_for({SOL[0]: "Probability: 37%"})
        calls = []
        deps.parser = lambda q, text, tier: calls.append(text) or .42
        self.assertEqual(forecast(q, Research(), "C", deps).value, .37)
        self.assertFalse(calls)
        deps.client.values[SOL[0]] = "malformed"
        self.assertAlmostEqual(forecast(q, Research(), "C", deps).value, .42)
        self.assertEqual(calls, ["malformed", "malformed"])

    def test_prompt_sections_no_blend_and_hygiene(self):
        q, data = fixture("binary_long")
        deps = deps_for({SOL[0]: data["model_text"]})
        prompt = prompts.build(q, Research("x" * 14000, 6, True), deps.clock.now())
        self.assertIn("2026-10-01", prompt)
        self.assertIn("OPEN", prompt)
        self.assertLess(prompt.index("OUTSIDE VIEW:"), prompt.index("INSIDE VIEW:"))
        self.assertLess(prompt.index("INSIDE VIEW:"), prompt.index("FINAL:"))
        self.assertNotIn("bayes", prompt.lower())
        self.assertNotIn("x" * 25001, prompt)
        self.assertIn("x" * 14000, prompt)
        values = []
        for base in ("1%", "99%"):
            deps.client.values[SOL[0]] = data["model_text"].replace("20%", base)
            values.append(forecast(q, Research(), "C", deps).value)
        self.assertEqual(values, [.37, .37])

    def test_no_secret_in_comment_or_log(self):
        q, _ = fixture("binary_long")
        marker = "PRIVATE_RATIONALE_MARKER"
        text = narration("Probability: 37%").replace("One fact", marker + " FAKEKEY123 " + "A" * 12000)
        deps = deps_for({SOL[0]: text})
        deps.env = {"OPENROUTER_API_KEY": "FAKEKEY123"}
        stream, handler = io.StringIO(), None
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            result = forecast(q, Research(), "C", deps)
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertNotIn("FAKEKEY123", result.comment)
        self.assertLessEqual(len(result.comment), 10000)
        for forbidden in ("FAKEKEY123", marker, "0.37", "37.0%"):
            self.assertNotIn(forbidden, stream.getvalue())

    def test_research_choice_cache_retry_and_alert(self):
        q, _ = fixture("binary_long")
        env = {"ASKNEWS_API_KEY": "FAKEKEY123", "ASKNEWS_CLIENT_ID": "client", "ASKNEWS_SECRET": "pair"}
        self.assertEqual(credentials(env), {"api_key": "FAKEKEY123"})
        self.assertEqual(credentials({"ASKNEWS_CLIENT_ID": "client", "ASKNEWS_SECRET": "pair"}),
                         {"client_id": "client", "client_secret": "pair"})
        self.assertIsNone(credentials({"ASKNEWS_CLIENT_ID": "client"}))
        self.assertEqual(researcher_slot(env), "asknews/news-summaries")
        self.assertEqual(researcher_slot({}), "no_research")
        deps = deps_for({})
        fetch = FakeAskNews()
        service = Service(env, deps.state, fetch)
        value = service.get(q)
        self.assertEqual(len(value.text), 12000)
        service.get(q)
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(fetch.calls[0][2], {"timeout": 60, "strategy": "latest news", "n_articles": 6})
        deps.state.posted.add(q.qid)
        service.get(q)
        self.assertEqual(len(fetch.calls), 1)
        failed = FakeAskNews(True)
        deps = deps_for({})
        service = Service(env, deps.state, failed)
        self.assertFalse(service.get(q).available)
        self.assertEqual(len(failed.calls), 2)
        self.assertNotIn("RESEARCH_UNAVAILABLE", deps.state.alerts)
        skipped = Service({"SKIP_WITHOUT_RESEARCH": "true"}, deps.state)
        self.assertFalse(skipped.get(q).available)

    def test_research_threshold(self):
        q, _ = fixture("binary_long")
        deps = deps_for({})
        fetch = FakeAskNews()
        service = Service({"ASKNEWS_API_KEY": "FAKEKEY123"}, deps.state, fetch)
        for i in range(4):
            service.get(replace(q, qid=q.qid+i))
        fetch.fail = True
        service.get(replace(q, qid=q.qid+4))
        self.assertNotIn("RESEARCH_UNAVAILABLE", deps.state.alerts)
        service.get(replace(q, qid=q.qid+5))
        self.assertIn("RESEARCH_UNAVAILABLE", deps.state.alerts)
