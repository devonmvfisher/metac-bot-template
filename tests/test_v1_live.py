"""L1-L5 against the actual metadata, adapter, transport and post gate."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from fbot import SkipQuestion, live, llm, metadata, ops
from fbot.research import AskNewsLimiter, Service
from fbot.state import RunState
from .adapter_fakes import load_adapter, raw_question, bot_for
from .fakes import FakeClock, FakeGit, FakeGitHub, fixture


class LiveTests(unittest.TestCase):
    def test_L1_chain_events_and_exclusions(self):
        for event in ('schedule', 'workflow_dispatch', 'timer'):
            clock = FakeClock()
            github = FakeGitHub(clock.now())
            env = {'BOT_ENABLED': 'true', 'CHAIN_ENABLED': 'true', 'GITHUB_EVENT_NAME': event,
                   'JOB_START': str(clock.now().timestamp() - 3000)}
            output = []
            ops.run(env, {'counts': {}}, clock, github, FakeGit(clock.now()), emit=output.append)
            self.assertEqual([w for w in github.writes if w[0] == 'dispatch'], [('dispatch', 'main')])
            self.assertEqual([s for s in output if s.startswith('CHAIN ')], ['CHAIN dispatched=1 via=gh error=-'])
        for bot, chain, test, date in ((False, True, False, '2026-10-01T00:00:00Z'),
                                     (True, False, False, '2026-10-01T00:00:00Z'),
                                     (True, True, True, '2026-10-01T00:00:00Z'),
                                     (True, True, False, '2027-01-07T00:00:00Z')):
            clock = FakeClock(date)
            github = FakeGitHub(clock.now())
            output = []
            ops.run({'BOT_ENABLED': str(bot).lower(), 'CHAIN_ENABLED': str(chain).lower()},
                    {'counts': {}}, clock, github, FakeGit(clock.now()), emit=output.append, test=test)
            self.assertFalse(any(w[0] == 'dispatch' for w in github.writes))
            self.assertEqual([s for s in output if s.startswith('CHAIN ')], ['CHAIN dispatched=0 via=none error=-'])

    def test_L1_rest_fallback_and_both_fail_softly(self):
        calls = []
        env = {'GITHUB_REPOSITORY': 'fixture/repository', 'GH_TOKEN': 'FAKEKEY123',
               'BOT_ENABLED': 'true', 'CHAIN_ENABLED': 'true'}
        def send(*args):
            calls.append(args)
            return 204, {}
        github = ops.GitHub(env, send)
        with patch.object(ops.subprocess, 'run', side_effect=OSError('private text')):
            self.assertEqual(github.dispatch('main'), 'rest')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith('/actions/workflows/run_bot_on_tournament.yaml/dispatches'))
        self.assertEqual(calls[0][3], {'ref': 'main'})
        clock = FakeClock()
        fake = FakeGitHub(clock.now())
        github.send = lambda *a: (503, {})
        fake.dispatch = github.dispatch
        output = []
        with patch.object(ops.subprocess, 'run', side_effect=OSError('private text')):
            code = ops.run(env, {'counts': {}}, clock, fake, FakeGit(clock.now()), emit=output.append)
        self.assertEqual(code, 0)
        self.assertTrue(any('SCHEDULER_GAPS' in str(row) for row in fake.writes))
        self.assertIn('CHAIN dispatched=0 via=none error=ApiError', output)
        self.assertNotIn('private text', str(output))
        self.assertNotIn('FAKEKEY123', str(output))

    def test_L1_empty_204_transport_is_success(self):
        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b''
        opener = SimpleNamespace(open=lambda *a, **k: Response())
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(llm.transport('POST', 'https://example.invalid/dispatch', {}, {}, 30), (204, {}))

    def test_L2_empty_page_and_exact_parameters(self):
        calls, details = [], {}
        def send(method, url, *args):
            calls.append(parse_qs(urlsplit(url).query))
            return 200, {'results': [], 'next': 'still-present'}
        self.assertEqual(metadata.open_count(33121, {}, send, details), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {'tournaments': ['33121'], 'statuses': ['open'], 'limit': ['100'],
                                   'offset': ['0'], 'include_descriptions': ['false']})
        self.assertEqual(details, {'reason': 'ok'})

    def test_L2_groups_offsets_and_bounded_unknown(self):
        page = [{'group_of_questions': {'questions': [{'id': 1, 'status': 'open'}, {'id': 2, 'status': 'open'},
                                                       {'id': 3, 'status': 'closed'}]}}]
        calls = []
        def send(method, url, *args):
            calls.append(url)
            return 200, {'results': page if len(calls) == 1 else [], 'next': 'never-empty'}
        self.assertEqual(metadata.open_count(33121, {}, send), 2)
        self.assertEqual(len(calls), 2)
        self.assertIn('offset=1', calls[1])
        details, calls = {}, []
        def capped(*args):
            calls.append(1)
            return 200, {'results': page, 'next': 'next'}
        self.assertEqual(metadata.open_count(33121, {}, capped, details), 'unknown')
        self.assertEqual((len(calls), details), (20, {'reason': 'cap'}))
        self.assertFalse(live.startup_counts(['unknown', 'unknown'], ['cap', 'cap']))
        self.assertFalse(live.startup_counts(['unknown', 'unknown'], ['http_429', 'cap']))
        self.assertTrue(live.startup_counts(['unknown', 'unknown'], ['http_429', 'shape']))

    def test_L3_batch_readback_and_reasons(self):
        calls = []
        body = {'group_of_questions': {'questions': [{'id': 1, 'my_forecasts': {'latest': {'id': 7}}},
                                                     {'id': 2, 'my_forecasts': {'latest': {}}}, {'id': 3}]}}
        def send(*args):
            calls.append(1)
            return 200, body
        self.assertEqual(metadata.readback(20, [1, 2, 3, 4], {}, send),
                         {1: ('found', 'ok'), 2: ('none', 'ok'), 3: ('unknown', 'field_missing'), 4: ('unknown', 'not_in_post')})
        self.assertEqual(len(calls), 1)
        for status, why in ((404, 'no_post'), (500, 'http_500'), (429, 'rate_limited'), (0, 'dropped')):
            naps = []
            self.assertEqual(metadata.readback(20, [1], {}, lambda *a: (status, {}), naps.append), {1: ('unknown', why)})
            self.assertEqual(naps, [2, 4] if status in (429, 0) else [])

    def test_L3_test_group_retry_and_tolerance_only_with_one_found(self):
        q, _ = fixture('binary_long')
        questions = {1: replace(q, qid=1, post_id=10), 2: replace(q, qid=2, post_id=20), 3: replace(q, qid=3, post_id=20)}
        for retry_found in (False, True):
            clock = FakeClock()
            state = RunState(clock)
            state.http_status = {1: 201, 2: 201, 3: 201}
            state.commented = {1, 2, 3}
            calls = []
            def read(post, ids, *args, **kwargs):
                calls.append((post, tuple(ids)))
                status = 'found' if post == 10 or retry_found and calls.count((20, (2, 3))) == 2 else 'unknown'
                return {qid: (status, 'field_missing' if status == 'unknown' else 'ok') for qid in ids}
            with self.assertLogs('fbot', level='INFO') as logs:
                self.assertTrue(live.check_test_posts(questions, state, {}, clock, read))
            self.assertEqual(calls, [(10, (1,)), (20, (2, 3)), (20, (2, 3))])
            self.assertEqual(clock.monotonic(), 5)
            self.assertEqual(any('unverified_group' in line for line in logs.output), not retry_found)
        state.commented.discard(3)
        self.assertFalse(live.check_test_posts(questions, state, {}, clock, read))
        state.commented.add(3)
        self.assertFalse(live.check_test_posts(questions, state, {}, clock,
                                             lambda post, ids, *a, **k: {i: ('unknown', 'shape') for i in ids}))

    def test_L3_single_unknown_and_missing_fail(self):
        q, _ = fixture('binary_long')
        state = RunState(FakeClock())
        state.http_status = {q.qid: 201}
        state.commented = {q.qid}
        for status in ('unknown', 'none'):
            self.assertFalse(live.check_test_posts({q.qid: q}, state, {}, state.clock,
                                                 lambda *a, **k: {q.qid: (status, 'shape')}))

    def test_L3_tournament_group_unknown_still_blocks_before_models(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk)
        def read(*args, **kwargs):
            kwargs['details'].update(group=True, why='field_missing')
            return None
        with patch.object(module, 'already_forecast', side_effect=read):
            with self.assertRaisesRegex(SkipQuestion, 'READBACK_UNKNOWN'):
                asyncio.run(bot.run_research(raw))
        self.assertIn('GATE_BLOCKED', bot.f_state.alerts)
        self.assertEqual(bot.f_state.counts['readback_unknown_group'], 1)
        self.assertEqual(bot.f_client.calls, [])

    def test_L4_eight_calls_share_spacing_and_two_slots(self):
        clock = FakeClock()
        state = RunState(clock)
        starts, peaks = [], []
        lock, barrier = threading.Lock(), threading.Barrier(2)
        inflight = 0
        def fetch(*args, **kwargs):
            nonlocal inflight
            with lock:
                starts.append(clock.monotonic())
                inflight += 1
                peaks.append(inflight)
            barrier.wait(timeout=3)
            with lock:
                inflight -= 1
            return 'fixture news', 1
        service = Service({'ASKNEWS_API_KEY': 'FAKEKEY123'}, state, fetch)
        q, _ = fixture('binary_long')
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(service.get, [replace(q, qid=q.qid+i) for i in range(8)]))
        self.assertTrue(all(v.available for v in values))
        self.assertEqual(len(starts), 8)
        self.assertTrue(all(b-a >= 2 for a, b in zip(starts, starts[1:])))
        self.assertEqual(max(peaks), 2)

    def test_L4_retry_after_and_deadline_bound(self):
        class Limited(Exception):
            status_code, retry_after = 429, 9
        for deadline, expected in ((30, True), (4, False)):
            clock = FakeClock()
            calls = []
            def fetch(*a, **k):
                calls.append(clock.monotonic())
                if len(calls) == 1:
                    raise Limited('secret')
                return 'news', 1
            service = Service({'ASKNEWS_API_KEY': 'FAKEKEY123'}, RunState(clock), fetch)
            service.sleep = clock.sleep
            q, _ = fixture('binary_long')
            self.assertEqual(service.get(q, deadline).available, expected)
            self.assertEqual(calls, [0, 9] if expected else [0])
            self.assertLessEqual(clock.monotonic(), deadline)

    def test_L5_two_failed_posts_cap_third_without_models(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk)
        with patch.object(module, 'already_forecast', return_value=False):
            for attempt in range(2):
                asyncio.run(bot.run_research(raw))
                asyncio.run(bot.prediction(raw))
                _, ticket = bot.f_gate.before('POST', 'https://www.metaculus.com/api/questions/forecast/',
                                             [{'question': raw.id_of_question, 'probability_yes': .37}])
                bot.f_gate.after(ticket, 503)
                bot.f_gate.finish_poll()
                bot.f_gate.finish_poll()
                self.assertEqual(bot.f_state.failures[raw.id_of_question], attempt+1)
            before = len(bot.f_client.calls)
            with self.assertRaisesRegex(SkipQuestion, 'RETRY_CAPPED'):
                asyncio.run(bot.run_research(raw))
            self.assertEqual(len(bot.f_client.calls), before)
        self.assertEqual(bot.f_state.skips['POST_FAILED'], 2)
