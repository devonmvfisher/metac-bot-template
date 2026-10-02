"""v1.1: a Metaculus 429 through the real send hook (main.install_post_gate) with mocked HTTP.

The bot's own code adds no retry, wait or Retry-After handling (SEXTANT-POST429 adds no request,
retry loop or sleep). The SDK's bounded retry decides when to try again; sdk_post below stands in
for forecasting-tools 0.2.92 retry_with_exponential_backoff (3 retries; only HTTP errors retry).
"""
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from fbot import SkipQuestion, config, ledger
from fbot.postgate import Gate
from fbot.state import RunState
from fbot.types import Result
from .adapter_fakes import load_adapter
from .fakes import FakeClock, fixture

URL = "https://www.metaculus.com/api/questions/forecast/"
SDK_RETRIES = 3


class HTTPError(Exception):
    pass


class Harness:
    """The real hook from main.py around a fake requests.Session that answers from a script."""

    def __init__(self, replies, readbacks=None, book=None, clock=None):
        self.clock = clock or FakeClock("2026-10-05T01:00:00Z")
        self.book = book or ledger.Ledger(available=True)
        self.state = RunState(self.clock)
        self.state.attach_book(self.book)
        self.replies, self.sent, self.reads = list(replies), [], []
        answers = list(readbacks) if readbacks is not None else None

        def readback(*args):
            self.reads.append(args)
            return answers.pop(0) if answers is not None else False

        self.gate = Gate(self.state, readback=readback)
        self.question, _ = fixture("binary_long")
        harness = self
        requests = ModuleType("requests")

        class Session:
            def send(self, request, **kwargs):
                harness.sent.append((request.method, request.url, json.loads(request.body)))
                status, headers = harness.replies.pop(0)
                return SimpleNamespace(status_code=status, headers=dict(headers))

        requests.Session = Session
        httpx = ModuleType("httpx")

        class Client:
            def send(self, request, **kwargs):
                raise AssertionError("httpx is not used for the forecast POST in these tests")

        class AsyncClient:
            async def send(self, request, **kwargs):
                raise AssertionError("httpx is not used for the forecast POST in these tests")

        httpx.Client, httpx.AsyncClient = Client, AsyncClient
        module, _ = load_adapter()
        with patch.dict(sys.modules, requests=requests, httpx=httpx):
            module.install_post_gate(self.gate)
        self.requests = requests
        self.delays = []

    def register(self):
        self.gate.register(self.question, Result(.37, "FBOT v1\nFINAL fixture", [], models=[config.SOL[0]]))

    def sdk_post(self):
        """One SDK forecast publish: retry HTTP errors up to SDK_RETRIES times, never anything else."""
        delay = 2.5
        for attempt in range(SDK_RETRIES + 1):
            request = SimpleNamespace(method="POST", url=URL, headers={},
                                      body=json.dumps([{"question": self.question.qid, "source": "api",
                                                        "probability_yes": .37}]).encode())
            try:
                response = self.requests.Session().send(request, timeout=30)
                if response.status_code >= 400:
                    raise HTTPError(response.status_code)
                return response
            except HTTPError:
                if attempt == SDK_RETRIES:
                    raise
                self.delays.append(min(delay, 75.0))
                delay *= 3


class Post429HttpTests(unittest.TestCase):
    def test_H01_429_then_success_posts_once_through_the_real_hook(self):
        run = Harness([(429, {}), (200, {})])
        run.register()
        response = run.sdk_post()
        run.gate.finish_poll()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(run.sent), 2)
        self.assertEqual(len(run.reads), 2)
        self.assertEqual([body[0]["question"] for _, _, body in run.sent], [run.question.qid] * 2)
        self.assertIn(run.question.qid, run.state.posted)
        self.assertEqual(run.state.counts[run.question.target], 1)
        self.assertEqual(run.state.counts["post_rate_limited"], 1)
        self.assertEqual(run.state.failures[run.question.qid], 0)
        self.assertEqual(dict(run.state.skips), {})
        self.assertNotIn("API_REJECTED", run.state.alerts)
        self.assertEqual(run.book.failure_count(run.question.qid, run.clock.now()), 0)
        self.assertEqual(run.book.transient_runs(run.question.qid, run.clock.now()), 0)

    def test_H02_a_retry_after_header_changes_nothing_and_the_bot_never_waits(self):
        snapshots = []
        for headers in ({}, {"Retry-After": "120"}, {"Retry-After": "Wed, 07 Oct 2026 01:00:00 GMT"}):
            with self.subTest(headers=headers):
                run = Harness([(429, headers), (200, {})])
                run.register()
                run.sdk_post()
                run.gate.finish_poll()
                self.assertEqual(run.clock.sleeps, [])
                self.assertEqual(len(run.sent), 2)
                self.assertIn(run.question.qid, run.state.posted)
                snapshot = run.state.snapshot()
                snapshots.append((dict(snapshot["counts"]), dict(snapshot["skips"]), sorted(snapshot["alerts"])))
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0], snapshots[2])

    def test_H03_throttled_past_the_sdk_limit_gives_up_once_with_the_outage_rule(self):
        run = Harness([(429, {"Retry-After": "30"})] * (SDK_RETRIES + 1))
        run.register()
        with self.assertRaises(HTTPError):
            run.sdk_post()
        run.gate.finish_poll()
        self.assertEqual(len(run.sent), SDK_RETRIES + 1)
        self.assertEqual(run.replies, [])
        self.assertEqual(run.delays, [2.5, 7.5, 22.5])
        self.assertEqual(run.clock.sleeps, [])
        self.assertEqual(run.state.counts["post_rate_limited"], SDK_RETRIES + 1)
        self.assertEqual(dict(run.state.skips), {"POST_RATE_LIMITED": 1})
        self.assertEqual(run.state.failures[run.question.qid], 1)
        self.assertNotIn(run.question.qid, run.state.posted)
        self.assertNotIn("API_REJECTED", run.state.alerts)
        self.assertEqual(run.book.failure_count(run.question.qid, run.clock.now()), 0)
        self.assertEqual(run.book.transient_runs(run.question.qid, run.clock.now()), 1)

    def test_H04_a_retry_whose_readback_finds_the_forecast_is_never_sent(self):
        run = Harness([(429, {})], readbacks=[False, True])
        run.register()
        with self.assertRaises(SkipQuestion):
            run.sdk_post()
        run.gate.finish_poll()
        self.assertEqual(len(run.sent), 1)
        self.assertEqual(len(run.reads), 2)
        self.assertIn("GATE_BLOCKED", run.state.alerts)
        self.assertNotIn("API_REJECTED", run.state.alerts)
        self.assertNotIn(run.question.qid, run.state.posted)

    def test_H05_after_a_2xx_no_second_post_of_the_question_is_sent(self):
        run = Harness([(429, {}), (200, {})])
        run.register()
        run.sdk_post()
        with self.assertRaises(SkipQuestion):
            run.sdk_post()
        run.gate.finish_poll()
        self.assertEqual(len(run.sent), 2)
        self.assertEqual(run.state.counts[run.question.target], 1)
        self.assertIn(run.question.qid, run.state.posted)

    def test_H06_a_real_reject_through_the_hook_still_alerts_and_keeps_the_two_day_memory(self):
        run = Harness([(400, {})] * (SDK_RETRIES + 1))
        run.register()
        with self.assertRaises(HTTPError):
            run.sdk_post()
        run.gate.finish_poll()
        self.assertEqual(len(run.sent), SDK_RETRIES + 1)
        self.assertIn("API_REJECTED", run.state.alerts)
        self.assertGreaterEqual(run.state.failures[run.question.qid], 2)
        self.assertGreaterEqual(run.book.failure_count(run.question.qid, run.clock.now()), 2)
        self.assertEqual(run.book.transient_runs(run.question.qid, run.clock.now()), 0)
        self.assertNotIn("post_rate_limited", run.state.snapshot()["counts"])


if __name__ == "__main__":
    unittest.main()
