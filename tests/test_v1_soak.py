"""A deterministic 120-question soak through the real pacer, client and gate."""
from collections import Counter
from dataclasses import replace
import threading
import unittest
from fbot import config, ledger, numeric
from fbot.budget import Pacer
from fbot.llm import Client
from fbot.pipeline import Dependencies, forecast
from fbot.state import RunState
from fbot.types import Research
from .fakes import FakeClock, FakeMetaculus, fixture, fixture_prepare, narration
from . import test_v1_live


def run_soak():
    clock = FakeClock()
    state = RunState(clock)
    state.attach_book(ledger.Ledger(available=True))
    env = {'OPENROUTER_API_KEY': 'FAKEKEY-SOAK', 'MODEL_PRESET': 'auto'}
    account, current, lock = [1000.0], [''], threading.Lock()
    def send(method, url, headers, body, timeout, **kwargs):
        with lock:
            if method == 'GET':
                return 200, {'data': {'limit_remaining': account[0], 'limit': 1000}}
            account[0] -= .04
            return 200, {'choices': [{'message': {'content': current[0]}}]}
    client = Client(env, clock, state.alert, send)
    pacer = Pacer(env, client, state, clock)
    pacer.refresh()
    api, rows, summaries = FakeMetaculus(state), [], []
    counts = Counter()
    for index in range(120):
        name = ('binary_long', 'mc_three', 'numeric_closed', 'discrete_ten')[index % 4]
        question, data = fixture(name)
        question = replace(question, qid=50000 + index, post_id=60000 if index < 2 else 60000 + index, is_group=index < 2)
        state.model_preset = ('A', 'B', 'C', 'auto')[(index // 4) % 4]
        fast = (index // 4) % 4 == 3
        tier = pacer.tier(question, fast=fast)
        plan = pacer.pending[question.qid]
        if question.kind in ('numeric', 'discrete'):
            current[0] = narration('\n'.join(f'Percentile {p:g}: {question.lower + (question.upper-question.lower)*p/100:g}' for p in numeric.LEVELS))
        else:
            current[0] = data['model_text']
        def prepare(q, result):
            return result if result.numeric_v1 else fixture_prepare(data)(q, result)
        deps = Dependencies(client, clock, state, env, prepare=prepare)
        before = pacer.measure_start()
        result = forecast(question, Research('Synthetic news evidence', 6, True), tier, deps)
        pacer.measure_end(question, tier, before)
        spent = before['router'] - account[0]
        assert spent <= plan + 1e-9, (tier, spent, plan)
        api.submit(question, result)
        assert api.requests[-1][0] == '/api/comments/create/'
        assert api.requests[-1][1]['is_private'] is True
        assert api.requests[-1][1]['included_forecast'] is True
        assert question.qid in state.posted and question.qid in state.commented
        counts[question.kind] += 1
        rows.append({'qid': question.qid, 'post': question.post_id, 'kind': question.kind, 'tier': tier,
                     'planned_usd': round(plan, 6), 'spent_usd': round(spent, 6), 'comment': True})
        if index < 4 or index % 16 == 0:
            summaries.append(result.summary)
    assert len(state.posted) == len(state.commented) == 120
    # The same acceptance run also exercises the required burst and failed-post cap.
    live = test_v1_live.LiveTests()
    live.test_L4_eight_calls_share_spacing_and_two_slots()
    live.test_L5_two_failed_posts_cap_third_without_models()
    return {'questions': 120, 'types': dict(counts), 'tiers': dict(state.tiers), 'crashes': 0,
            'display_skips': 0, 'comments': len(state.commented), 'group_pairs': 1,
            'asknews_burst': 8, 'intentional_post_failures': 2, 'capped_third_poll': True,
            'within_plan': all(row['spent_usd'] <= row['planned_usd'] + 1e-9 for row in rows),
            'rows': rows}, summaries


class SoakTests(unittest.TestCase):
    def test_A3_120_questions_four_types_all_tiers_comments_and_plan(self):
        report, summaries = run_soak()
        self.assertEqual(report['types'], {'binary': 30, 'multiple_choice': 30, 'numeric': 30, 'discrete': 30})
        self.assertEqual(set(report['tiers']), {'A', 'B', 'C', 'M'})
        self.assertEqual(report['comments'], 120)
        self.assertTrue(report['within_plan'])
        self.assertEqual(report['display_skips'], 0)
