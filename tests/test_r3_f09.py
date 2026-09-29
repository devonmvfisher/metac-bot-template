"""F09 (P0-LIVE-MAP): a short model-provider outage must not bench a question across runs, and a provider
that keeps failing still meets a real limit.

Written by Claude Opus 5.5 (v1-r3 build, Mon Sep 28 2026).
Rules: every slot failing with no reply, 408, 429 or 5xx is MODEL_TRANSIENT. It caps the question for the rest of
the run as before (two tries), is remembered once per run for ledger.TRANSIENT_HOURS, and after
state.TRANSIENT_RUNS such runs the question sits out until that window passes. Every other all-fail
(ALL_MODELS_FAILED, INVALID_OUTPUT, MISREAD_ALL, POST_FAILED) keeps the two-day memory.
"""
import json
from pathlib import Path
import tempfile
import unittest
import fbot
from fbot import ModelFailure, SkipQuestion, ledger
from fbot.config import FLASH, OPUS, SOL
from fbot.pipeline import forecast
from fbot.state import TRANSIENT_RUNS, RunState
from fbot.types import Research
from .fakes import FakeClock, deps_for, fixture

RUN_GAP = 20 * 60
WINDOW = 3 * 3600  # the decided window, written out so a changed constant is caught


def run(book, clock):
    state = RunState(clock)
    state.attach_book(book)
    return state


class ClassifyTests(unittest.TestCase):
    def reason(self, scripts):
        q, _ = fixture("binary_long")
        with self.assertRaises(SkipQuestion) as caught:
            forecast(q, Research(), "A", deps_for(scripts))
        return caught.exception.reason

    def every_slot(self, value):
        return {slot[0]: value for slot in (OPUS, SOL, FLASH)}

    def test_no_reply_rate_limit_and_server_errors_are_transient(self):
        self.assertEqual(self.reason({}), "MODEL_TRANSIENT")
        for status in (0, 408, 429, 500, 502, 503, 599):
            self.assertEqual(self.reason(self.every_slot(ModelFailure(status))), "MODEL_TRANSIENT", status)

    def test_question_and_setup_failures_are_not(self):
        for status in (400, 401, 403, 404, 422, 600):
            self.assertEqual(self.reason(self.every_slot(ModelFailure(status))), "ALL_MODELS_FAILED", status)
        mixed = {OPUS[0]: ModelFailure(503), SOL[0]: "unparseable output", FLASH[0]: ModelFailure(503)}
        self.assertEqual(self.reason(mixed), "ALL_MODELS_FAILED")
        bug = {OPUS[0]: RuntimeError("bug"), SOL[0]: ModelFailure(503), FLASH[0]: ModelFailure(503)}
        self.assertEqual(self.reason(bug), "ALL_MODELS_FAILED")

    def test_constants(self):
        self.assertIn("MODEL_TRANSIENT", fbot.REASONS)
        self.assertEqual((TRANSIENT_RUNS, ledger.TRANSIENT_HOURS, ledger.FAILURE_DAYS), (3, 3, 2))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-10-01T12:00:00Z")
        self.book = ledger.Ledger(available=True)

    def test_short_outage_caps_this_run_only(self):
        state = run(self.book, self.clock)
        state.failure(801, "MODEL_TRANSIENT")
        state.failure(801, "MODEL_TRANSIENT")
        self.assertEqual(state.failures[801], 2)
        self.assertEqual(self.book.failure_count(801, self.clock.now()), 0)
        self.assertEqual(self.book.transient_runs(801, self.clock.now()), 1)
        self.clock.sleep(RUN_GAP)
        self.assertEqual(run(self.book, self.clock).failures[801], 0)

    def test_an_outage_across_two_runs_still_does_not_bench(self):
        for _ in range(2):
            state = run(self.book, self.clock)
            state.failure(802, "MODEL_TRANSIENT")
            state.failure(802, "MODEL_TRANSIENT")
            self.clock.sleep(RUN_GAP)
        self.assertEqual(self.book.transient_runs(802, self.clock.now()), 2)
        self.assertEqual(run(self.book, self.clock).failures[802], 0)

    def test_a_provider_that_keeps_failing_benches_the_question_then_it_is_retried(self):
        for _ in range(3):
            run(self.book, self.clock).failure(803, "MODEL_TRANSIENT")
            self.clock.sleep(RUN_GAP)
        self.assertGreaterEqual(run(self.book, self.clock).failures[803], 2)
        self.clock.sleep(WINDOW - 2 * RUN_GAP)
        self.assertGreaterEqual(run(self.book, self.clock).failures[803], 2)
        self.clock.sleep(2 * RUN_GAP)
        self.assertEqual(run(self.book, self.clock).failures[803], 0)

    def test_question_failures_keep_the_two_day_memory(self):
        state = run(self.book, self.clock)
        state.failure(804, "ALL_MODELS_FAILED")
        state.failure(804, "ALL_MODELS_FAILED")
        self.assertEqual(self.book.transient_runs(804, self.clock.now()), 0)
        self.clock.sleep(47 * 3600)
        self.assertEqual(run(self.book, self.clock).failures[804], 2)
        self.clock.sleep(3600)
        self.assertEqual(run(self.book, self.clock).failures[804], 0)

    def test_ledger_round_trip_and_an_older_ledger(self):
        self.book.record_transient(805, self.clock.now())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            self.assertTrue(ledger.save(self.book, path, self.clock.now()))
            self.assertEqual(ledger.load(path).transient_runs(805, self.clock.now()), 1)
            older = json.loads(path.read_text(encoding="utf-8"))
            del older["transient"]
            path.write_text(json.dumps(older), encoding="utf-8")
            loaded = ledger.load(path)
            self.assertTrue(loaded.available)
            self.assertEqual(loaded.data["transient"], {})
        self.assertFalse(self.book.record_transient("805", self.clock.now()))
        self.assertFalse(self.book.record_transient(0, self.clock.now()))

    def test_prune_drops_marks_after_the_window(self):
        # v1-r3 build: prune keeps a transient mark for the window and drops it after.
        self.book.record_transient(806, self.clock.now())
        self.clock.sleep(WINDOW - 1)
        self.book.prune(self.clock.now())
        self.assertIn("806", self.book.data["transient"])
        self.clock.sleep(1)
        self.book.prune(self.clock.now())
        self.assertNotIn("806", self.book.data["transient"])

    def test_an_older_ledger_keeps_its_other_rows(self):
        # v1-r3 build: a ledger saved before v1-r3 has no "transient" key; every other row still loads.
        stamp = "2026-10-01T11:00:00Z"
        clean = ledger.sanitize({"version": 1, "failures": {"807": [2, stamp]}, "comment_failed": [808],
                                 "free_tokens": {"2026-10-01": 5}, "asknews": {"2026-10": 12}})
        self.assertEqual(clean["transient"], {})
        self.assertEqual((clean["failures"], clean["comment_failed"]), ({"807": [2, stamp]}, [808]))
        self.assertEqual((clean["free_tokens"], clean["asknews"]), ({"2026-10-01": 5}, {"2026-10": 12}))


if __name__ == "__main__":
    unittest.main()
