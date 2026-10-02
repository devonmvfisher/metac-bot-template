"""Prototype (Claude's scratch, not shipped): a Metaculus 429 on a POST is an outage, not a bad payload."""
import asyncio
import unittest
from unittest.mock import patch
import fbot
from fbot import SkipQuestion, ledger, ops
from fbot.postgate import Gate
from fbot.state import RunState
from fbot.types import Result
from fbot import config
from .adapter_fakes import load_adapter, raw_question, bot_for
from .fakes import FakeClock, FakeGit, FakeGitHub, fixture

URL = "https://www.metaculus.com/api/questions/forecast/"
COMMENT = "https://www.metaculus.com/api/comments/create/"
RUN_GAP = 20 * 60
WINDOW = 3 * 3600


def result_for(question):
    return Result(.37, "FBOT v1\nFINAL fixture", [], models=[config.SOL[0]])


class Run:
    def __init__(self, book, clock):
        self.state = RunState(clock)
        self.state.attach_book(book)
        self.gate = Gate(self.state, readback=lambda *a: False)

    def poll(self, question, statuses):
        self.gate.register(question, result_for(question))
        for status in statuses:
            _, ticket = self.gate.before("POST", URL, [{"question": question.qid, "source": "api", "probability_yes": .37}])
            self.gate.after(ticket, status)
        self.gate.finish_poll()


class P429Tests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-10-05T01:00:00Z")
        self.book = ledger.Ledger(available=True)
        self.q, _ = fixture("binary_long")

    def test_P01_forecast_429_is_not_a_rejection(self):
        run = Run(self.book, self.clock)
        run.gate.register(self.q, result_for(self.q))
        _, ticket = run.gate.before("POST", URL, [{"question": self.q.qid, "probability_yes": .37}])
        with self.assertLogs("fbot", "INFO") as logs:
            run.gate.after(ticket, 429)
        self.assertNotIn("API_REJECTED", run.state.alerts)
        self.assertEqual(run.state.failures[self.q.qid], 0)
        self.assertEqual(run.state.http_status[self.q.qid], 429)
        self.assertEqual(run.state.counts["post_rate_limited"], 1)
        self.assertIn(self.q.qid, run.gate.rate_limited)
        self.assertNotIn(self.q.qid, run.gate.pending)
        self.assertIn(f"POST_RATE_LIMITED qid={self.q.qid} kind=forecast", "\n".join(logs.output))

    def test_P02_every_other_4xx_is_unchanged(self):
        for status in (400, 401, 403, 404, 408, 409, 422, 499):
            with self.subTest(status=status):
                run = Run(ledger.Ledger(available=True), FakeClock())
                run.poll(self.q, [status])
                self.assertIn("API_REJECTED", run.state.alerts)
                self.assertEqual(run.state.failures[self.q.qid], 2)
                self.assertEqual(run.gate.rate_limited, set())

    def test_P03_sdk_retry_after_429_posts_once_and_leaves_no_mark(self):
        run = Run(self.book, self.clock)
        run.poll(self.q, [429, 200])
        self.assertIn(self.q.qid, run.state.posted)
        self.assertEqual(run.state.counts[self.q.target], 1)
        self.assertEqual(run.state.failures[self.q.qid], 0)
        self.assertEqual(dict(run.state.skips), {})
        self.assertNotIn("API_REJECTED", run.state.alerts)
        self.assertEqual(self.book.failure_count(self.q.qid, self.clock.now()), 0)
        self.assertEqual(self.book.transient_runs(self.q.qid, self.clock.now()), 0)
        self.assertEqual(run.gate.rate_limited, set())

    def test_P04_throttled_to_the_end_of_the_poll_is_transient(self):
        run = Run(self.book, self.clock)
        with self.assertLogs("fbot", "INFO") as logs:
            run.poll(self.q, [429, 429, 429, 429])
        self.assertEqual(run.state.failures[self.q.qid], 1)
        self.assertEqual(run.state.skips["POST_RATE_LIMITED"], 1)
        self.assertEqual(run.state.skips["POST_FAILED"], 0)
        self.assertEqual(self.book.failure_count(self.q.qid, self.clock.now()), 0)
        self.assertEqual(self.book.transient_runs(self.q.qid, self.clock.now()), 1)
        self.assertEqual(run.state.counts["post_rate_limited"], 4)
        self.assertIn(f"SKIP qid={self.q.qid} target={self.q.target} reason=POST_RATE_LIMITED", "\n".join(logs.output))
        self.assertEqual(run.gate.rate_limited, set())

    def test_P05_two_throttled_polls_cap_this_run_only(self):
        run = Run(self.book, self.clock)
        run.poll(self.q, [429])
        run.poll(self.q, [429])
        self.assertEqual(run.state.failures[self.q.qid], 2)
        self.assertEqual(self.book.transient_runs(self.q.qid, self.clock.now()), 1)
        self.clock.sleep(RUN_GAP)
        self.assertEqual(Run(self.book, self.clock).state.failures[self.q.qid], 0)

    def test_P06_three_throttled_runs_sit_out_then_retry(self):
        for _ in range(3):
            Run(self.book, self.clock).poll(self.q, [429])
            self.clock.sleep(RUN_GAP)
        self.assertGreaterEqual(Run(self.book, self.clock).state.failures[self.q.qid], 2)
        self.clock.sleep(WINDOW - 2 * RUN_GAP)
        self.assertGreaterEqual(Run(self.book, self.clock).state.failures[self.q.qid], 2)
        self.clock.sleep(2 * RUN_GAP)
        self.assertEqual(Run(self.book, self.clock).state.failures[self.q.qid], 0)

    def test_P07_a_real_reject_after_a_429_keeps_the_two_day_memory(self):
        run = Run(self.book, self.clock)
        run.poll(self.q, [429, 400])
        self.assertIn("API_REJECTED", run.state.alerts)
        self.assertEqual(run.state.failures[self.q.qid], 2)
        self.assertEqual(dict(run.state.skips), {})
        self.assertEqual(self.book.failure_count(self.q.qid, self.clock.now()), 2)
        self.assertEqual(self.book.transient_runs(self.q.qid, self.clock.now()), 0)

    def test_P08_the_mark_lasts_one_poll(self):
        run = Run(self.book, self.clock)
        run.poll(self.q, [429])
        run.poll(self.q, [503])
        self.assertEqual(run.state.skips["POST_RATE_LIMITED"], 1)
        self.assertEqual(run.state.skips["POST_FAILED"], 1)
        self.assertEqual(self.book.failure_count(self.q.qid, self.clock.now()), 1)

    def test_P09_comment_429_is_retried_not_rejected(self):
        run = Run(self.book, self.clock)
        run.poll(self.q, [200])
        _, ticket = run.gate.before("POST", COMMENT, {"on_post": self.q.post_id})
        run.gate.after(ticket, 429)
        self.assertNotIn("API_REJECTED", run.state.alerts)
        self.assertNotIn(self.q.qid, run.state.commented)
        replies = [429, 200]
        run.gate.retry_comments(lambda *a: (replies.pop(0), {}), {})
        self.assertIn(self.q.qid, run.state.commented)
        self.assertEqual(self.clock.sleeps, [30])
        self.assertNotIn("COMMENT_FAILED", run.state.alerts)
        self.assertNotIn("API_REJECTED", run.state.alerts)

    def test_P10_ops_posts_no_p0_for_a_throttled_run(self):
        env = {"BOT_ENABLED": "true", "GITHUB_EVENT_NAME": "schedule"}
        run = Run(self.book, self.clock)
        run.poll(self.q, [429, 429, 429, 429])
        github = FakeGitHub(self.clock.now())
        code = ops.run(env, run.state.snapshot(), self.clock, github, FakeGit(self.clock.now()), emit=lambda *a: None)
        self.assertEqual(code, 0)
        self.assertFalse(any(w[0] == "create" and "API_REJECTED" in w[1] for w in github.writes))
        control = Run(ledger.Ledger(available=True), FakeClock())
        control.poll(self.q, [400])
        github = FakeGitHub(self.clock.now())
        code = ops.run(env, control.state.snapshot(), self.clock, github, FakeGit(self.clock.now()), emit=lambda *a: None)
        self.assertEqual(code, 1)
        self.assertTrue(any(w[0] == "create" and w[1] == "[BOT ALERT] API_REJECTED" for w in github.writes))

    def test_P11_group_post_marks_every_member(self):
        run = Run(self.book, self.clock)
        second = type(self.q)(**{**self.q.__dict__, "qid": self.q.qid + 1}) if hasattr(self.q, "__dict__") else None
        run.gate.register(self.q, result_for(self.q))
        run.gate.register(second, result_for(second))
        _, ticket = run.gate.before("POST", URL, [{"question": self.q.qid, "probability_yes": .37},
                                                  {"question": second.qid, "probability_yes": .37}])
        run.gate.after(ticket, 429)
        run.gate.finish_poll()
        self.assertEqual(run.state.skips["POST_RATE_LIMITED"], 2)
        self.assertEqual(run.state.counts["post_rate_limited"], 2)
        self.assertNotIn("API_REJECTED", run.state.alerts)

    def test_P12_reason_is_known_everywhere(self):
        self.assertIn("POST_RATE_LIMITED", fbot.REASONS)
        self.assertEqual(SkipQuestion("POST_RATE_LIMITED").reason, "POST_RATE_LIMITED")
        run = Run(self.book, self.clock)
        run.poll(self.q, [429])
        vitals = []
        ops.run({"BOT_ENABLED": "true", "GITHUB_EVENT_NAME": "schedule"}, run.state.snapshot(), self.clock,
                FakeGitHub(self.clock.now()), FakeGit(self.clock.now()), emit=vitals.append)
        self.assertTrue(any("POST_RATE_LIMITED" in line for line in vitals))

    def test_P13_no_429_no_trace(self):
        run = Run(self.book, self.clock)
        run.poll(self.q, [200])
        self.assertNotIn("post_rate_limited", run.state.snapshot()["counts"])
        self.assertEqual(run.gate.rate_limited, set())

    def test_P14_adapter_path_two_throttled_polls_then_capped(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk)
        with patch.object(module, 'already_forecast', return_value=False):
            for attempt in range(2):
                asyncio.run(bot.run_research(raw))
                asyncio.run(bot.prediction(raw))
                _, ticket = bot.f_gate.before('POST', URL, [{'question': raw.id_of_question, 'probability_yes': .37}])
                bot.f_gate.after(ticket, 429)
                bot.f_gate.finish_poll()
                self.assertEqual(bot.f_state.failures[raw.id_of_question], attempt + 1)
            before = len(bot.f_client.calls)
            with self.assertRaisesRegex(SkipQuestion, 'RETRY_CAPPED'):
                asyncio.run(bot.run_research(raw))
            self.assertEqual(len(bot.f_client.calls), before)
        self.assertEqual(bot.f_state.skips['POST_RATE_LIMITED'], 2)
        self.assertEqual(bot.f_state.skips['POST_FAILED'], 0)
        self.assertNotIn("API_REJECTED", bot.f_state.alerts)

    def test_P15_throttled_readback_on_the_sdk_retry_is_transient(self):
        state = RunState(self.clock)
        state.attach_book(self.book)
        answers = [False, None]
        gate = Gate(state, readback=lambda *a: answers.pop(0))
        gate.register(self.q, result_for(self.q))
        _, ticket = gate.before("POST", URL, [{"question": self.q.qid, "probability_yes": .37}])
        gate.after(ticket, 429)
        with self.assertRaises(SkipQuestion) as caught:
            gate.before("POST", URL, [{"question": self.q.qid, "probability_yes": .37}])
        gate.finish_poll()
        self.assertEqual(caught.exception.reason, "POST_RATE_LIMITED")
        self.assertEqual(state.failures[self.q.qid], 1)
        self.assertEqual(dict(state.skips), {"POST_RATE_LIMITED": 1})
        self.assertEqual(self.book.failure_count(self.q.qid, self.clock.now()), 0)
        self.assertEqual(self.book.transient_runs(self.q.qid, self.clock.now()), 1)
        self.assertIn("GATE_BLOCKED", state.alerts)
        self.assertNotIn("API_REJECTED", state.alerts)
        self.assertEqual(gate.rate_limited, set())

    def test_P16_an_unknown_readback_without_a_429_this_poll_is_unchanged(self):
        for earlier in ([], [429]):
            with self.subTest(earlier=earlier):
                clock = FakeClock("2026-10-05T01:00:00Z")
                book = ledger.Ledger(available=True)
                state = RunState(clock)
                state.attach_book(book)
                answers = [False] * len(earlier) + [None]
                gate = Gate(state, readback=lambda *a: answers.pop(0))
                for status in earlier:
                    gate.register(self.q, result_for(self.q))
                    _, ticket = gate.before("POST", URL, [{"question": self.q.qid, "probability_yes": .37}])
                    gate.after(ticket, status)
                    gate.finish_poll()
                gate.register(self.q, result_for(self.q))
                with self.assertRaises(SkipQuestion) as caught:
                    gate.before("POST", URL, [{"question": self.q.qid, "probability_yes": .37}])
                gate.finish_poll()
                self.assertEqual(caught.exception.reason, "INVALID_OUTPUT")
                self.assertIn("GATE_BLOCKED", state.alerts)
                self.assertEqual(book.failure_count(self.q.qid, clock.now()), 2)
                self.assertEqual(state.skips["INVALID_OUTPUT"], 1)
                self.assertEqual(gate.rate_limited, set())


if __name__ == "__main__":
    unittest.main()
