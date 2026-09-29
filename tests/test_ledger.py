"""Acceptance tests for fbot.ledger: the counts-only memory kept across runs (v1 H1).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT; reviewed 15:20 CT).
Contract: INTERFACE.md section 5. The ledger holds counts, ids, dates, tiers, reasons and spend only.
"""
from datetime import timedelta
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from fbot import ledger
from fbot.targets import utc

NOW = utc("2026-10-10T12:00:00Z")
TODAY = "2026-10-10"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fbot-ledger-"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = self.dir / "fbot-ledger.json"

    def test_empty_shape(self):
        book = ledger.empty()
        self.assertFalse(book.available)
        self.assertEqual(book.data, {"version": 1, "updated": None, "spend": {}, "posted": {}, "failures": {},
                                     "transient": {}, "comment_failed": [], "seen": {}, "free_tokens": {},
                                     "last_probe": None, "asknews": {}})
        self.assertEqual(set(book.data), ledger.KEYS)
        self.assertEqual(ledger.LEDGER_PATH, "fbot-ledger.json")

    def test_missing_and_corrupt_fail_soft(self):
        self.assertFalse(ledger.load(self.path).available)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("fbot", level="INFO") as logs:
            book = ledger.load(self.path)
        self.assertFalse(book.available)
        self.assertEqual(book.data, ledger.empty().data)
        self.assertIn("LEDGER status=corrupt", "\n".join(logs.output))
        self.path.write_text(json.dumps({"version": 99, "posted": {"season": 4}}), encoding="utf-8")
        book = ledger.load(self.path)
        self.assertFalse(book.available)
        self.assertEqual(book.data["posted"], {})
        self.assertFalse(ledger.save(book, self.dir / "missing-folder" / "x.json", NOW))

    def test_roundtrip_atomic_lf(self):
        book = ledger.empty()
        self.assertTrue(book.record_question(101, "season", NOW, reason="TOO_LATE"))
        self.assertTrue(book.record_spend("C", 0.31, NOW))
        book.record_posted("season")
        book.record_comment_failed(101)
        book.record_failure(101, 2, NOW)
        book.add_free_tokens(NOW, 1200)
        book.record_probe(NOW)
        self.assertTrue(ledger.save(book, self.path, NOW))
        self.assertEqual(book.data["updated"], "2026-10-10T12:00:00Z")
        self.assertTrue(ledger.save(book, str(self.path), NOW))  # a str path works as well as a Path
        self.assertEqual([p.name for p in self.dir.iterdir()], ["fbot-ledger.json"])
        raw = self.path.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))
        again = ledger.load(self.path)
        self.assertTrue(again.available)
        self.assertEqual(again.data, book.data)
        self.assertEqual(again.seen(101), ("season", TODAY, "TOO_LATE"))
        self.assertEqual(again.failure_count(101, NOW), 2)
        self.assertEqual(again.free_tokens(NOW), 1200)
        self.assertEqual(again.data["comment_failed"], [101])
        self.assertEqual(again.data["posted"], {"season": 1})
        self.assertEqual(again.data["last_probe"], TODAY)

    def test_sanitize_drops_text_values_and_junk(self):
        raw = {"version": 1, "rationale": "PRIVATE_REASONING", "probability": 0.376543,
               "spend": {TODAY: {"C": [3, 0.9], "Z": [1, 1.0], "B": [-1, 2.0], "A": [2, float("nan")],
                                 "M": [True, 0.1]},
                         "not-a-date": {"C": [1, 0.1]}, "2026-13-45": {"C": [1, 0.1]}},
               "seen": {"101": ["season", TODAY, "TOO_LATE"], "abc": ["season", TODAY, None],
                        "102": ["season", TODAY, "tok-FAKEKEY123"], "103": ["elsewhere", TODAY, None],
                        "0104": ["season", TODAY, None]},
               "failures": {"101": [2, TODAY], "104": ["2", TODAY], "105": [0, TODAY]},
               "transient": {"101": [1, TODAY], "abc": [1, TODAY], "106": [0, TODAY], "107": [1, "soon"]},
               "comment_failed": [5, "6", -7, 5, True],
               "free_tokens": {TODAY: 100, "2026-10-11": -5},
               "posted": {"season": 3, "minibench": "4", "FAKEKEY123": 1},
               "last_probe": "yesterday", "updated": "FAKEKEY123"}
        clean = ledger.sanitize(raw)
        self.assertEqual(clean, {"version": 1, "updated": None,
                                 "spend": {TODAY: {"C": [3, 0.9]}},
                                 "posted": {"season": 3},
                                 "failures": {"101": [2, TODAY]},
                                 "transient": {"101": [1, TODAY]},
                                 "comment_failed": [5],
                                 "seen": {"101": ["season", TODAY, "TOO_LATE"]},
                                 "free_tokens": {TODAY: 100},
                                 "last_probe": None, "asknews": {}})
        text = json.dumps(clean)
        for bad in ("PRIVATE_REASONING", "0.376543", "FAKEKEY123", "tok-", '"Z"', "not-a-date", '"abc"', "elsewhere"):
            self.assertNotIn(bad, text)
        self.assertEqual(ledger.sanitize(clean), clean)
        self.assertEqual(ledger.sanitize("junk"), ledger.empty().data)

    def test_rejects_bad_records(self):
        book = ledger.empty()
        self.assertFalse(book.record_question("FAKEKEY123", "season", NOW))
        self.assertFalse(book.record_question(7, "elsewhere", NOW))
        self.assertFalse(book.record_question(7, "season", NOW, reason="made up"))
        self.assertFalse(book.record_spend("C", float("nan"), NOW))
        self.assertFalse(book.record_spend("C", -1, NOW))
        self.assertFalse(book.record_spend("Q", 1, NOW))
        self.assertFalse(book.record_spend("C", True, NOW))
        self.assertTrue(ledger.save(book, self.path, NOW))
        self.assertNotIn(b"FAKEKEY123", self.path.read_bytes())
        self.assertEqual(book.data["seen"], {})
        self.assertEqual(book.data["spend"], {})

    def test_measured_cost_needs_twenty_questions_in_seven_days(self):
        book = ledger.empty()
        for _ in range(30):
            book.record_spend("C", 1.0, NOW - timedelta(days=8))  # outside the 7-day window
        for _ in range(19):
            book.record_spend("C", 0.30, NOW)
        self.assertIsNone(book.measured_cost("C", NOW))
        self.assertEqual(book.pacer_cost("C", 0.435, NOW), 0.435)
        book.record_spend("C", 0.30, NOW - timedelta(days=6))
        self.assertAlmostEqual(book.measured_cost("C", NOW), 0.30)
        self.assertAlmostEqual(book.pacer_cost("C", 0.435, NOW), max(0.435 / 2, 1.2 * 0.30))
        self.assertAlmostEqual(book.pacer_cost("C", 1.0, NOW), 1.0 / 2)
        self.assertIsNone(book.measured_cost("M", NOW))
        questions, usd = book.spend_7d(NOW)["C"]
        self.assertEqual(questions, 20)
        self.assertAlmostEqual(usd, 6.0)
        self.assertNotIn("M", book.spend_7d(NOW))

    def test_failure_memo_across_runs(self):
        book = ledger.empty()
        book.record_failure(7, 1, NOW)
        book.record_failure(7, 1, NOW)
        self.assertEqual(book.failure_count(7, NOW), 2)
        self.assertEqual(book.failure_count(7, NOW + timedelta(days=1)), 2)
        self.assertEqual(book.failure_count(7, NOW + timedelta(days=2)), 0)
        self.assertEqual(book.failure_count(8, NOW), 0)
        for _ in range(20):
            book.record_failure(9, 2, NOW)
        self.assertEqual(book.failure_count(9, NOW), 9)
        book.record_failure(7, 1, NOW + timedelta(days=3))  # an expired memo starts again
        self.assertEqual(book.failure_count(7, NOW + timedelta(days=3)), 1)
        book.record_failure(11, 2, NOW)
        book.record_failure(11, 1, NOW + timedelta(days=2))  # exactly FAILURE_DAYS old: also starts again
        self.assertEqual(book.failure_count(11, NOW + timedelta(days=2)), 1)

    def test_prune_windows(self):
        book = ledger.empty()
        book.record_spend("C", 0.3, NOW - timedelta(days=9))
        book.record_spend("C", 0.3, NOW - timedelta(days=7))
        book.record_question(1, "season", NOW - timedelta(days=15))
        book.record_question(2, "season", NOW - timedelta(days=13))
        book.record_failure(3, 1, NOW - timedelta(days=3))
        book.add_free_tokens(NOW - timedelta(days=9), 10)
        for qid in range(300):
            book.record_comment_failed(qid + 1)
        book.prune(NOW)
        self.assertEqual(sorted(book.data["spend"]), [(NOW - timedelta(days=7)).date().isoformat()])
        self.assertIsNone(book.seen(1))
        self.assertIsNotNone(book.seen(2))
        self.assertEqual(book.data["failures"], {})
        self.assertEqual(book.data["free_tokens"], {})
        self.assertEqual(len(book.data["comment_failed"]), 200)
        self.assertEqual(book.data["comment_failed"][-1], 300)

    def test_threads_count_exactly(self):
        book = ledger.empty()

        def work(base):
            for i in range(100):
                book.record_spend("M", 0.01, NOW)
                book.record_question(base * 1000 + i + 1, "minibench", NOW)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(book.data["spend"][TODAY]["M"][0], 800)
        self.assertEqual(len(book.data["seen"]), 800)


if __name__ == "__main__":
    unittest.main()
