"""Numeric v1 routing through production parsing, preparation and the post gate."""
import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import SkipQuestion, numeric, parse, prompts, prompts_v1
from fbot.config import SOL, PERCENTILES
from fbot.pipeline import forecast
from fbot.postgate import Gate
from fbot.types import Question, Research
from .adapter_fakes import load_adapter, bot_for, raw_question
from .fakes import deps_for, narration


class NumericIntegrationTests(unittest.TestCase):
    def build(self, *, new=True, known=True, text=None):
        q = Question(8801, 9901, 'numeric', 'Synthetic quantity', lower=0, upper=100, size_known=known)
        text = text or narration('\n'.join(f'Percentile {p}: {p}' for p in (numeric.LEVELS if new and known else PERCENTILES)))
        deps = deps_for({SOL[0]: text})
        deps.env['NUMERIC_V1'] = str(new).lower()
        module, sdk = load_adapter()
        bot = bot_for(module)
        bot.f_state, bot.f_env = deps.state, deps.env
        raw = raw_question(sdk, 'NumericQuestion')
        deps.prepare = lambda question, result: bot.prepare(raw, question, result)
        return q, deps, bot, raw

    def test_M2_default_numeric_pipeline_preserves_validated_curve(self):
        q, deps, bot, raw = self.build()
        deps.env = {}
        bot.f_env = deps.env
        result = forecast(q, Research(), 'C', deps)
        self.assertTrue(result.numeric_v1)
        self.assertTrue(numeric.check_strict(q, result.cdf))
        self.assertIn(numeric.METHOD, result.summary)
        self.assertEqual(len(raw.seen_percentiles), len(numeric.sdk_percentiles(q, result.cdf)))
        self.assertEqual(deps.state.counts['numeric_v1'], 1)

    def test_M2_gate_replaces_sdk_drift_only_with_provenance(self):
        q, deps, bot, raw = self.build()
        result = forecast(q, Research(), 'C', deps)
        gate = Gate(deps.state, readback=lambda *a: False)
        gate.register(q, result)
        curve = [value + .01 for value in result.cdf]
        payload = [{'question': q.qid, 'continuous_cdf': curve}]
        sent, ticket = gate.before('POST', 'https://www.metaculus.com/api/questions/forecast/', payload)
        self.assertEqual(sent[0]['continuous_cdf'], result.cdf)
        self.assertEqual(payload[0]['continuous_cdf'], curve)
        gate.after(ticket, 503)
        result.numeric_v1 = False
        with self.assertRaises(SkipQuestion):
            gate.before('POST', 'https://www.metaculus.com/api/questions/forecast/', payload)
        result.numeric_v1 = True
        for curve in ([0.1], [float('nan')]*201):
            with self.assertRaises(SkipQuestion):
                gate.before('POST', 'https://www.metaculus.com/api/questions/forecast/', [{'question': q.qid, 'continuous_cdf': curve}])

    def test_M2_numeric_faults_fall_back_but_content_faults_do_not(self):
        for fault in (RuntimeError('private text'), numeric.NumericError('cdf')):
            q, deps, bot, raw = self.build()
            with patch.object(numeric, 'build_cdf', side_effect=fault):
                result = forecast(q, Research(), 'C', deps)
            self.assertFalse(result.numeric_v1)
            self.assertIn('NUMERIC_FALLBACK', deps.state.alerts)
            self.assertEqual(len(result.cdf), 201)
        for reason in ('scale', 'degenerate', 'order', 'domain', 'units', 'input'):
            q, deps, bot, raw = self.build()
            with patch.object(numeric, 'build_cdf', side_effect=numeric.NumericError(reason)):
                with self.assertRaisesRegex(SkipQuestion, 'ALL_MODELS_FAILED'):
                    forecast(q, Research(), 'C', deps)
            self.assertEqual(deps.state.counts['numeric_drop_' + reason], 2)

    def test_M2_unknown_size_and_sdk_preflight_fallback(self):
        q, deps, bot, raw = self.build(known=False)
        with patch.object(numeric, 'build_cdf', side_effect=AssertionError('must not build a guessed CDF')):
            result = forecast(q, Research(), 'C', deps)
        self.assertFalse(result.numeric_v1)
        self.assertEqual(deps.state.counts['numeric_fallback'], 1)
        q, deps, bot, raw = self.build()
        with patch.object(numeric, 'sdk_percentiles', side_effect=RuntimeError('fixture')):
            result = forecast(q, Research(), 'C', deps)
        self.assertFalse(result.numeric_v1)
        self.assertIn('NUMERIC_FALLBACK', deps.state.alerts)

    def test_M2_fractional_sdk_levels_are_not_rounded(self):
        q, deps, bot, raw = self.build()
        async def structure(*args, **kwargs):
            self.assertIn('2.5', kwargs['instructions'])
            return [SimpleNamespace(percentile=p/100, value=p) for p in numeric.LEVELS]
        bot.structure = structure
        bot.on_main_loop = lambda coroutine, question: asyncio.run(coroutine)
        values = bot.parser_fallback(q, 'fixture', 'C')
        self.assertEqual(set(values), set(numeric.LEVELS))
        self.assertEqual((values[2.5], values[97.5]), (2.5, 97.5))

    def test_M2_prompt_routes_and_long_blank_final_are_bounded(self):
        q, deps, bot, raw = self.build()
        for modern in (False, True):
            deps.env['PROMPTS_V1'] = str(modern).lower()
            result = forecast(q, Research(), 'C', deps)
            prompt = deps.client.calls[-1][1]
            self.assertIn('Percentile 2.5:', prompt)
            self.assertTrue(result.numeric_v1)
        legacy = prompts.build(q, Research(), deps.clock.now(), use_numeric=True)
        self.assertEqual(legacy.count('Percentile 10: X means'), 1)
        text = '\n'*40000 + 'FINAL\nProbability: 37%'
        start = time.perf_counter()
        self.assertEqual(parse.final_text(text), 'Probability: 37%')
        self.assertLess(time.perf_counter()-start, 1)
