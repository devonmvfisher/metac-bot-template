"""Whole-adapter acceptance using SDK, HTTP and scheduler fakes only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import config, coverage, evidence, ledger, live, markets, misread, numeric, probe, research2, schedule, sources, validate, webread
from fbot.budget import Pacer
from fbot.llm import Client
from fbot.pipeline import forecast, forecast_async
from fbot.state import RunState
from fbot.types import Question, Research
from .adapter_fakes import load_adapter, bot_for, raw_question
from .fakes import FakeClock, FakeMetaculus, deps_for, fixture, fixture_prepare, narration


class AcceptanceTests(unittest.TestCase):
    def test_L3_unknown_single_fails_even_when_other_question_is_found(self):
        clock = FakeClock()
        state = RunState(clock)
        questions = {n: Question(n, n + 20, 'binary', 'fixture', target='test') for n in (1, 2)}
        state.http_status = {1: 201, 2: 201}
        state.commented = {1, 2}
        def read(post, ids, env, **kwargs):
            return {qid: ('found', 'ok') if qid == 1 else ('unknown', 'shape') for qid in ids}
        self.assertFalse(live.check_test_posts(questions, state, {}, clock, read))
        self.assertEqual(clock.sleeps, [5])

    def test_A6_fake_test_bot_all_presets_and_group_pair(self):
        for preset in ('auto', 'A', 'B', 'C'):
            module, sdk = load_adapter()
            clock = FakeClock()
            state = RunState(clock)
            state.attach_book(ledger.Ledger(available=True))
            env = {'OPENROUTER_API_KEY': 'FAKEKEY', 'TEST_PRESET': preset}
            def send(method, url, headers, body, timeout, **kwargs):
                if method == 'GET':
                    return 200, {'data': {'limit_remaining': 1000, 'limit': 1000}}
                prompt = body['messages'][0]['content']
                if 'Percentile 1:' in prompt:
                    maximum = 9 if 'discrete fixture' in prompt else 100
                    text = '\n'.join(f'Percentile {p:g}: {maximum * p / 100}' for p in numeric.LEVELS)
                else:
                    text = 'Probability: 37%'
                return 200, {'choices': [{'message': {'content': narration(text)}}]}
            client = Client(env, clock, state.alert, send)
            gates, tiers, reads = [], [], []
            def install(gate):
                gate.readback = lambda *a: False
                gates.append(gate)
            def read(post, ids, env, **kwargs):
                reads.append(post)
                return {qid: ('unknown', 'field_missing') if post == 9200 else ('found', 'ok') for qid in ids}
            async def poll(bot, target, **kwargs):
                self.assertEqual(target, config.TEST_ID)
                rows = [raw_question(sdk, id_of_question=8101, id_of_post=9101),
                        raw_question(sdk, 'NumericQuestion', id_of_question=8102, id_of_post=9102),
                        raw_question(sdk, 'DiscreteQuestion', id_of_question=8103, id_of_post=9103, question_text='discrete fixture', upper_bound=9, inbound_outcome_count=9, cdf=[i/9 for i in range(10)]),
                        raw_question(sdk, id_of_question=8104, id_of_post=9200),
                        raw_question(sdk, id_of_question=8105, id_of_post=9200)]
                for raw in rows:
                    await bot.run_research(raw)
                    await bot.prediction(raw)
                    gate = gates[0]
                    question, result = gate.results[raw.id_of_question]
                    tiers.append(result.tier)
                    body = {'question': question.qid, **validate.payload(question, result)}
                    _, ticket = gate.before('POST', 'https://www.metaculus.com/api/questions/forecast/', [body])
                    gate.after(ticket, 201)
                    _, ticket = gate.before('POST', 'https://www.metaculus.com/api/comments/create/', {'on_post': question.post_id})
                    gate.after(ticket, 201)
                return []
            with patch.object(module, 'Client', return_value=client), patch.object(module, 'already_forecast', return_value=False), patch.object(module, 'readback', side_effect=read), patch.object(module, 'install_post_gate', side_effect=install), patch.object(module.FBot, 'forecast_on_tournament', new=poll, create=True), patch.object(state, 'write'), self.assertLogs('fbot', level='INFO') as logs:
                asyncio.run(module.execute(SimpleNamespace(mode='test_questions'), env, clock, state))
            self.assertEqual(tiers, ['A' if preset == 'auto' else preset] * 5)
            self.assertEqual(len(state.posted), 5)
            self.assertEqual(state.posted, state.commented)
            self.assertEqual(reads.count(9200), 2)
            output = '\n'.join(logs.output)
            for label in ('PROBE model=', 'COST model=', 'READBACK qid=', 'POSTED qid=', 'readback=unverified_group', 'NUMERIC qid=', 'PAGES qid=', 'RESEARCH2 qid=', 'MARKETS qid='):
                self.assertIn(label, output)
            self.assertNotIn('GATE_BLOCK', output)
            self.assertNotIn('FAKEKEY', output + json.dumps(state.snapshot()))

    def test_A5_numeric_misread_schedule_failures_still_post_labelled(self):
        for module, attr, name, fixture_name in ((numeric, 'build_cdf', 'numeric', 'numeric_closed'), (misread, 'drop', 'misread', 'binary_long'), (schedule, 'budget', 'schedule', 'binary_long')):
            q, data = fixture(fixture_name)
            text = data['model_text']
            if name == 'numeric':
                text = narration('\n'.join(f'Percentile {p:g}: {p:g}' for p in numeric.LEVELS))
            deps = deps_for({config.SOL[0]: text}, fixture_prepare(data))
            deps.env['NUMERIC_V1'] = 'true'
            with patch.object(module, attr, side_effect=RuntimeError('private')):
                result = asyncio.run(forecast_async(q, Research(), 'C', deps))
            api = FakeMetaculus(deps.state)
            api.submit(q, result)
            self.assertEqual(len(api.requests), 2)
            self.assertIn('without-' + name, result.summary)

    def test_A5_ledger_probe_coverage_faults_do_not_block_post(self):
        q, data = fixture('binary_long')
        for name, module, attribute, fallback, args in (
            ('ledger', ledger, 'load', ledger.empty(), ()),
            ('probe', probe, 'due', False, (ledger.empty(), FakeClock().now())),
            ('coverage', coverage, 'read_recent', {}, ({}, FakeClock().now()))):
            deps = deps_for({config.SOL[0]: data['model_text']})
            with patch.object(module, attribute, side_effect=RuntimeError('private')):
                evidence.call(name, getattr(module, attribute), fallback, deps.state, *args)
            result = forecast(q, Research(), 'C', deps)
            api = FakeMetaculus(deps.state)
            api.submit(q, result)
            self.assertEqual(len(api.requests), 2)
            self.assertIn('without-' + name, result.summary)
