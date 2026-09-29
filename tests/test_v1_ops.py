"""Reviewed OPS fixes and integration boundaries, with fake external services."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import coverage, hardening, ledger, ops, parse, prompts_v1, schedule
from fbot.pipeline import forecast_async
from fbot.state import RunState
from .adapter_fakes import load_adapter, bot_for, raw_question
from .fakes import FakeClock, FakeGit, FakeGitHub, deps_for, fixture, narration


class OpsIntegrationTests(unittest.TestCase):
    def test_M1_all_pollers_failing_raise_POLL_FAILING(self):
        clock = FakeClock()
        state = RunState(clock)
        async def failed(name):
            raise RuntimeError('private text')
        async def sleep(seconds):
            clock.sleep(seconds)
        asyncio.run(schedule.run_targets({'season': failed, 'minibench': failed}, state, clock,
                    clock.now()+timedelta(minutes=2), 1, sleep=sleep, concurrent=False))
        self.assertIn('POLL_FAILING', state.alerts)
        self.assertEqual(sum(state.polls.values()), sum(state.poll_errors.values()))
        self.assertGreaterEqual(sum(state.polls.values()), 2)

    def test_M1_unknown_forfeits_are_reported(self):
        rows = coverage.weekly_rows({'season': {'forfeited': 0, 'unknown': 5}, 'minibench': {'unknown': 2}})
        self.assertIn('forfeits_status_unknown season=5 minibench=2', rows)

    def test_M1_empty_season_end_cli_uses_default(self):
        output = io.StringIO()
        with redirect_stdout(output):
            hardening.main(['--season-open'], {'SEASON_END_UTC': ''}, FakeClock('2027-02-01T00:00:00Z').now())
        self.assertEqual(output.getvalue().strip(), 'season_open=false')

    def test_L5_H1_failure_memo_survives_ledger_reload(self):
        clock = FakeClock()
        original = RunState(clock)
        book = ledger.empty()
        original.attach_book(book)
        original.failure(8101, 'POST_FAILED')
        original.failure(8101, 'POST_FAILED')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.json'
            self.assertTrue(ledger.save(book, path, now=clock.now()))
            restored = RunState(clock)
            restored.attach_book(ledger.load(path))
            module, sdk = load_adapter()
            bot = bot_for(module)
            bot.f_state = restored
            with self.assertRaisesRegex(Exception, 'RETRY_CAPPED'):
                asyncio.run(bot.run_research(raw_question(sdk)))
            self.assertEqual(bot.f_client.calls, [])

    def test_M1_granted_deadline_reaches_adapter_callback(self):
        from fbot.config import SOL
        q, _ = fixture('binary_long')
        deps = deps_for({SOL[0]: narration('Probability: 37%')})
        seen = []
        deps.granted = seen.append
        result = asyncio.run(forecast_async(q, SimpleNamespace(text='', available=False, articles=0), 'C', deps))
        self.assertEqual(seen, [1500])
        self.assertEqual(result.deadline, 3300)

    def test_M1_scheduler_budget_failure_keeps_question(self):
        from fbot.config import SOL
        q, _ = fixture('binary_long')
        deps = deps_for({SOL[0]: narration('Probability: 37%')})
        with patch.object(schedule, 'budget', side_effect=RuntimeError('private text')):
            result = asyncio.run(forecast_async(q, SimpleNamespace(text='', available=False, articles=0), 'C', deps))
        self.assertIn('without-schedule', result.caps)
        self.assertAlmostEqual(result.value, .37)

    def test_H6_H8_rationales_plain_bounds_and_bulleted_options(self):
        text = hardening.cap_rationales('summary', ['a'*3000]*5)
        self.assertEqual(len(text), 5)
        self.assertTrue(all(len(t) <= 1500 for t in text))
        q, _ = fixture('numeric_billions')
        prompt = prompts_v1.build(replace(q, lower=2500000, upper=3000000), SimpleNamespace(available=False), FakeClock().now())
        self.assertIn('2500000', prompt)
        self.assertNotIn('2.5e+06', prompt)
        q, _ = fixture('mc_three')
        lines = '\n'.join('- **' + name + '**: ' + str(value) + '%' for name, value in zip(q.options, (20, 30, 50)))
        self.assertEqual(parse.parse(q, 'FINAL\n' + lines), dict(zip(q.options, (.2, .3, .5))))

    def test_L1_H5_schedule_events_and_unknown_gap(self):
        clock = FakeClock()
        def run(qid, event):
            return {'id': qid, 'event': event, 'run_started_at': clock.now().isoformat(),
                    'updated_at': clock.now().isoformat(), 'conclusion': 'success'}
        gap, row = ops.schedule_status([[run(1, 'schedule')], [run(2, 'workflow_dispatch'), run(1, 'schedule')]], clock.now())
        self.assertIn('schedule=1 dispatch=1', row)
        self.assertEqual(gap, 0)
        self.assertIsNone(schedule.gap_minutes([None], clock.now()))

    def test_L2_weekly_coverage_not_read_between_statuses(self):
        clock = FakeClock()
        github, calls = FakeGitHub(clock.now()), []
        def read():
            calls.append(1)
            return {}
        for _ in range(2):
            ops.run({'BOT_ENABLED': 'true'}, {'counts': {}}, clock, github, FakeGit(clock.now()), coverage_reader=read,
                    emit=lambda line: None)
        self.assertEqual(calls, [1])

    def test_H7_closed_season_issue_not_reopened_or_repeated(self):
        clock = FakeClock('2027-01-07T00:00:00Z')
        github, queries = FakeGitHub(clock.now()), []
        def lookup(title, state):
            queries.append((title, state))
            return [{'title': title, 'state': 'closed'}]
        github.issues_for_title = lookup
        git = FakeGit(clock.now())
        ops.run({}, {'counts': {}}, clock, github, git, emit=lambda line: None)
        self.assertEqual(queries, [('[BOT ALERT] SEASON_OVER', 'all')])
        self.assertEqual(git.calls, [])
        self.assertFalse(any('[BOT ALERT] SEASON_OVER' in str(row) for row in github.writes))
