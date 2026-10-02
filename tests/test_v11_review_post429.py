"""v1.1 review fixes for POST429 (REVIEW-1 and REVIEW-2, Oct 1 2026).

RF01/RF02: a post deadline that passes while the SDK waits after a 429 is one POST_RATE_LIMITED skip
(it was TOO_LATE plus POST_RATE_LIMITED); with no 429 the TOO_LATE path is unchanged.
RF03: the panel's T9 burn bound. A throttle on forecast posts that lasts all day, while read-backs work,
re-runs the models at each allowed poll. The number of paid tries is pinned so nobody loosens it unseen.
RF04: the runbook says when a GATE_BLOCKED or COMMENT_FAILED is a throttle, and gives the real timing.
"""
from pathlib import Path
import unittest
from fbot import SkipQuestion, config, ledger
from fbot.postgate import Gate
from fbot.state import RunState
from fbot.types import Result
from .fakes import FakeClock, fixture

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.metaculus.com/api/questions/forecast/"
SDK_TRIES = 4          # forecasting-tools 0.2.92: 1 try + max_retries=3
RUN_MINUTES = 46       # one chain run, start to start (vitals: 45 min 37 s to 46 min 53 s)
POLLS = (0, 10, 20, 30, 40)  # LOOP_MINUTES=45, POLL_MINUTES=10


def result(deadline=None):
    return Result(.37, "FBOT v1\nFINAL fixture", [], models=[config.SOL[0]], deadline=deadline)


def body(qid):
    return [{"question": qid, "source": "api", "probability_yes": .37}]


def paid_tries(statuses, hours=24.0, open_minutes=None, offset_minutes=0):
    """Paid model runs for one question under a sustained post answer, as main.py does it.

    Each run attaches the shared ledger; each poll skips the question when failures >= 2 (main.py
    RETRY_CAPPED), otherwise the models run (paid), the gate registers the result and the SDK sends
    up to 4 posts. open_minutes limits the question's open window (a MiniBench question), starting
    offset_minutes after the first run starts.
    """
    clock = FakeClock("2026-10-05T01:00:00Z")
    book = ledger.Ledger(available=True)
    q, _ = fixture("binary_long")
    opens = offset_minutes * 60
    closes = None if open_minutes is None else opens + open_minutes * 60
    paid, posted = 0, False
    while clock.seconds < hours * 3600:
        start = clock.seconds
        state = RunState(clock)
        state.attach_book(book)
        gate = Gate(state, readback=lambda *a: False)
        for minute in POLLS:
            clock.seconds = start + minute * 60
            if clock.seconds < opens or (closes is not None and clock.seconds >= closes):
                continue
            if posted or state.failures[q.qid] >= 2:   # ALREADY_FORECAST, RETRY_CAPPED
                continue
            paid += 1
            gate.register(q, result())
            for status in statuses:
                _, ticket = gate.before("POST", URL, body(q.qid))
                gate.after(ticket, status)
                if 200 <= status < 300:
                    posted = True
                    break
            gate.finish_poll()
        clock.seconds = start + RUN_MINUTES * 60
    return paid


class ReviewPost429(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-10-05T01:00:00Z")
        self.book = ledger.Ledger(available=True)
        self.state = RunState(self.clock)
        self.state.attach_book(self.book)
        self.q, _ = fixture("binary_long")

    def late_publish(self, first_status):
        gate = Gate(self.state, readback=lambda *a: False)
        gate.register(self.q, result(deadline=self.clock.monotonic() + 30))
        with self.assertLogs("fbot", "INFO") as logs:
            _, ticket = gate.before("POST", URL, body(self.q.qid))
            gate.after(ticket, first_status)
            self.clock.sleep(60)   # SDK retry 2 waits min(7.5 x U(1,8), 75) s: up to 60 s
            with self.assertRaises(SkipQuestion) as caught:
                gate.before("POST", URL, body(self.q.qid))
            gate.finish_poll()
        return gate, caught.exception.reason, "\n".join(logs.output)

    def test_RF01_deadline_passing_after_a_429_is_one_throttle_skip(self):
        gate, reason, logs = self.late_publish(429)
        now = self.clock.now()
        self.assertEqual(reason, "POST_RATE_LIMITED")
        self.assertEqual(dict(self.state.skips), {"POST_RATE_LIMITED": 1})
        self.assertEqual(self.state.failures[self.q.qid], 1)
        self.assertEqual(self.book.failure_count(self.q.qid, now), 0)
        self.assertEqual(self.book.transient_runs(self.q.qid, now), 1)
        self.assertIn("GATE_BLOCKED", self.state.alerts)   # still fails closed and visible
        self.assertNotIn("API_REJECTED", self.state.alerts)
        self.assertIn(f"GATE_BLOCK qid={self.q.qid} kind=forecast rule=shape", logs)
        self.assertIn(f"POST_RATE_LIMITED qid={self.q.qid} kind=forecast", logs)
        self.assertEqual(gate.rate_limited, set())
        self.assertEqual(gate.pending, set())

    def test_RF02_deadline_passing_without_a_429_is_unchanged(self):
        gate, reason, logs = self.late_publish(503)
        now = self.clock.now()
        self.assertEqual(reason, "TOO_LATE")
        # v1-r3 behaviour, byte for byte: TOO_LATE has weight 0, so finish_poll books POST_FAILED.
        self.assertEqual(dict(self.state.skips), {"TOO_LATE": 1, "POST_FAILED": 1})
        self.assertEqual(self.state.failures[self.q.qid], 1)
        self.assertEqual(self.book.failure_count(self.q.qid, now), 1)
        self.assertEqual(self.book.transient_runs(self.q.qid, now), 0)
        self.assertIn(f"GATE_BLOCK qid={self.q.qid} kind=forecast rule=shape", logs)
        self.assertNotIn("POST_RATE_LIMITED", logs)

    def test_RF03_burn_bound_pinned(self):
        throttled = paid_tries([429] * SDK_TRIES)
        rejected = paid_tries([400] * SDK_TRIES)
        posted = paid_tries([200])
        window = max(paid_tries([429] * SDK_TRIES, hours=4, open_minutes=90, offset_minutes=offset)
                     for offset in range(0, RUN_MINUTES, 2))
        print(f"\nRF03 paid tries in 24 h, one season question: sustained 429 = {throttled}, sustained 400 = {rejected},"
              f" posted = {posted}; one MiniBench question open 90 min under a sustained 429 = {window} at most")
        self.assertLessEqual(throttled, 34)   # at Tier A (measured US$0.27) about US$9 a day per stuck question
        self.assertGreaterEqual(throttled, 2)  # it is retried: a short throttle no longer forfeits the question
        self.assertEqual(rejected, 1)          # a real rejection still parks the question for 2 days
        self.assertEqual(posted, 1)
        self.assertLessEqual(window, 6)

    def test_RF04_runbook_throttle_rules_and_timing(self):
        runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
        gate = next(line for line in runbook.splitlines() if line.startswith("| GATE_BLOCKED |"))
        for words in ("rule=readback", "rule=shape", "why=rate_limited", "q.season.skip.POST_RATE_LIMITED",
                      "q.minibench.skip.POST_RATE_LIMITED", "READBACK_UNKNOWN", "no POST_RATE_LIMITED skips follows the first rule"):
            self.assertIn(words, gate)
        comment = next(line for line in runbook.splitlines() if line.startswith("| COMMENT_FAILED |"))
        self.assertIn("kind=comment", comment)
        self.assertIn("not a rollback trigger", comment)
        self.assertNotIn("within about 2.5 minutes", runbook)
        self.assertIn("about 2 to 3 minutes of waiting", runbook)
        self.assertIn("the whole bot waits meanwhile", runbook)
        self.assertIn("about 34 paid tries a day", runbook)
        self.assertIn("stops MiniBench spend and posts, not reads", runbook)
        self.assertIn("never roll back for it", runbook)


if __name__ == "__main__":
    unittest.main()
