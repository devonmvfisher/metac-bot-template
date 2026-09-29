"""Seeded properties for boundaries, cancellation, privacy and module hand-offs."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from fbot import SkipQuestion, coverage, hardening, ledger, misread, probe, prompts_v1, schedule
from fbot.targets import utc
from fbot.types import Question, Research
from .fakes import FakeClock
from .vclock import make_post, sub_question

NOW = utc("2026-10-20T12:00:00Z")


class ExtraProperties(unittest.TestCase):
    def test_budget_boundaries_after_queue_wait(self):
        for wait in (0, 1, 120, 300, 719, 1500, 3000, 3300):
            for closing in (239, 240, 719, 720, 721, 3600, None):
                clock = FakeClock("2026-10-20T12:00:00Z")
                close = None if closing is None else NOW + timedelta(seconds=closing)
                question = Question(1, 2, "binary", "Synthetic boundary", close_time=close)
                clock.sleep(wait)
                remaining = None if close is None else (close - clock.now()).total_seconds()
                if wait >= 3300 or (remaining is not None and remaining < 240):
                    with self.assertRaises(SkipQuestion):
                        schedule.budget(question, clock, NOW)
                else:
                    result = schedule.budget(question, clock, NOW)
                    self.assertGreater(result.seconds, 0)
                    self.assertLessEqual(result.deadline, 3300)
                    self.assertLessEqual(result.seconds, 1500)
                    if remaining is not None:
                        self.assertLess(result.seconds, remaining)
                        self.assertEqual(result.fast, remaining < 720)

    def test_limiter_granted_cancellation_returns_capacity(self):
        async def scenario(limit):
            limiter = schedule.PriorityLimiter(limit)
            task = asyncio.create_task(limiter.acquire((1,)))
            await asyncio.sleep(0)
            asyncio.get_running_loop().call_soon(task.cancel)
            result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertEqual((limiter.active, limiter.waiting), (0, 0))
            self.assertEqual(len(limiter.grants), 1)
            async with limiter.slot((2,)):
                self.assertEqual(limiter.active, 1)
            self.assertEqual(limiter.active, 0)
        for limit in (3, 4, 5):
            asyncio.run(scenario(limit))

    def test_limiter_equal_key_fifo_and_read_only_history(self):
        rng = random.Random(9201)
        async def scenario(limit, count):
            limiter, order = schedule.PriorityLimiter(limit), []
            async def work(index):
                async with limiter.slot((1,)):
                    order.append(index)
                    await asyncio.sleep(0)
            await asyncio.gather(*(work(index) for index in range(count)))
            self.assertEqual(order, list(range(count)))
            self.assertLessEqual(limiter.peak, limit)
            self.assertEqual((limiter.active, limiter.waiting), (0, 0))
            history = limiter.grants
            history.clear()
            self.assertEqual(len(limiter.grants), count)
        for _ in range(20):
            asyncio.run(scenario(rng.randint(3, 5), rng.randint(6, 40)))

    def test_coverage_permutation_and_duplicate_invariance(self):
        rng = random.Random(9202)
        for count in range(1, 45):
            posts = [make_post(i, [sub_question(i, NOW - timedelta(hours=i),
                                               ("forecast", "none", "absent")[i % 3])]) for i in range(1, count + 1)]
            expected = coverage.count_recent(posts, NOW)
            repeated = posts + rng.choices(posts, k=count)
            rng.shuffle(repeated)
            self.assertEqual(coverage.count_recent(repeated, NOW), expected)
            self.assertEqual(expected["closed"], count)
            self.assertEqual(expected["closed"], sum(expected[key] for key in ("forecasted", "forfeited", "unknown")))
            self.assertEqual(len(set(expected["forfeited_ids"])), expected["forfeited"])
            self.assertEqual(len(set(expected["unknown_ids"])), expected["unknown"])

    def test_probe_duplicate_ids_are_called_once_and_output_is_private(self):
        rng = random.Random(9203)
        for count in range(1, 25):
            models = [f"synthetic/model-{i}" for i in range(count)]
            slots = {"OPUS": tuple(models), "SOL": tuple(rng.choices(models, k=count))}
            calls = []
            def call(model):
                calls.append(model)
                if model.endswith("-0"):
                    raise RuntimeError("FAKEKEY123")
                return 200
            with self.assertLogs("fbot", level="INFO") as logs:
                result = probe.run(call, slots, FakeClock())
            self.assertEqual(calls, models)
            self.assertEqual(result.probed, count)
            self.assertEqual(len(result.results), count * 2)
            self.assertNotIn("FAKEKEY123", "\n".join(logs.output))
            by_id = {}
            for _, model, status in result.results:
                self.assertEqual(by_id.setdefault(model, status), status)

    def test_ledger_invalid_records_are_atomic_and_json_roundtrips(self):
        rng = random.Random(9204)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "counts.json"
            book = ledger.empty()
            for qid in range(1, 80):
                now = NOW - timedelta(days=rng.randint(0, 6))
                self.assertTrue(book.record_question(qid, "season", now))
                self.assertTrue(book.record_spend("M", rng.randint(1, 99) / 100, now))
            before = json.dumps(book.data, sort_keys=True)
            for invalid in (True, False, -1, 0, "FAKEKEY123", None, [], {}):
                self.assertFalse(book.record_question(invalid, "season", NOW))
                self.assertFalse(book.record_failure(invalid, 1, NOW))
                self.assertEqual(json.dumps(book.data, sort_keys=True), before)
            with self.assertLogs("fbot", level="INFO") as logs:
                self.assertTrue(ledger.save(book, path, NOW))
                again = ledger.load(path)
            self.assertEqual(book.data, again.data)
            self.assertEqual(again.spend_7d(NOW)["M"][0], 79)
            self.assertNotIn(b"FAKEKEY123", path.read_bytes())
            self.assertNotIn("FAKEKEY123", "\n".join(logs.output))
            self.assertEqual(ledger.sanitize(json.loads(path.read_text())), book.data)

    def test_prompts_are_pure_and_preserve_block_order(self):
        rng = random.Random(9205)
        for count in range(1, 31):
            question = Question(count, 1, "multiple_choice", "Synthetic options",
                                options=tuple(f"Choice {i}" for i in range(count)))
            blocks = [f"SYNTHETIC BLOCK {i}\ntext {i}\nEND BLOCK {i}" for i in range(6)]
            rng.shuffle(blocks)
            prompt = prompts_v1.build(question, Research(), NOW, evidence=blocks)
            self.assertEqual(prompt, prompts_v1.build(question, Research(), NOW, evidence=blocks))
            self.assertEqual(prompt.splitlines()[-count:], [label + ": NN%" for label in question.options])
            positions = [prompt.index(block) for block in blocks[:4]]
            self.assertEqual(positions, sorted(positions))
            self.assertTrue(all(block not in prompt for block in blocks[4:]))
            self.assertEqual(misread.matches(prompt), [])

    def test_probe_credit_stop_covers_repeated_ids_too(self):
        for model_count in range(1, 10):
            models = tuple(f"synthetic/model-{i}" for i in range(model_count))
            calls = []
            def call(model):
                calls.append(model)
                return 402 if model == models[-1] else 200
            with self.assertLogs("fbot", level="INFO"):
                report = probe.run(call, {"SOL": models, "FLASH": models}, FakeClock())
            self.assertTrue(report.credit_exhausted)
            self.assertEqual(calls, list(models))
            self.assertEqual(report.results[model_count:], [("FLASH", model, None) for model in models])
            self.assertEqual((report.alert_slots, report.flaky_slots), ((), ()))

    def test_loop_end_shift_invariance_and_grace_edges(self):
        rng = random.Random(9211)
        for _ in range(200):
            install = rng.randint(0, 3600)
            duration = rng.randint(1, 45)
            shift = timedelta(days=rng.randint(-60, 60), seconds=rng.randint(-300, 300))
            process = NOW + timedelta(seconds=install)
            end = schedule.loop_end(process, NOW, duration)
            self.assertLessEqual(end, process + timedelta(minutes=duration))
            self.assertLessEqual(end, NOW + timedelta(minutes=duration, seconds=300))
            self.assertEqual(schedule.loop_end(process + shift, NOW + shift, duration), end + shift)
            self.assertEqual(schedule.loop_end(process.isoformat(), NOW.isoformat(), duration), end)

    def test_limiter_cancelling_all_waiters_leaves_holders_intact(self):
        async def scenario(limit):
            limiter = schedule.PriorityLimiter(limit)
            for index in range(limit):
                await limiter.acquire((index,))
            waiting = [asyncio.create_task(limiter.acquire((index,))) for index in range(20)]
            for _ in range(3):
                await asyncio.sleep(0)
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
            self.assertEqual((limiter.active, limiter.waiting), (limit, 0))
            for _ in range(limit):
                limiter.release()
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertEqual((limiter.active, limiter.waiting), (0, 0))
            self.assertEqual(len(limiter.grants), limit)
            async with limiter.slot((99,)):
                self.assertEqual(limiter.active, 1)
        for limit in (3, 4, 5):
            asyncio.run(scenario(limit))

    def test_ledger_prune_boundaries_and_failed_replace_preserve_file(self):
        for shift in range(5):
            now = NOW + timedelta(days=shift)
            book = ledger.empty()
            for age in (6, 7, 8, 9):
                book.record_spend("C", 1, now - timedelta(days=age))
            book.record_question(1, "season", now - timedelta(days=14))
            book.record_question(2, "season", now - timedelta(days=15))
            book.record_failure(3, 1, now - timedelta(days=1))
            book.record_failure(4, 1, now - timedelta(days=2))
            book.prune(now)
            self.assertEqual(book.spend_7d(now), {"C": (1, 1.0)})
            self.assertEqual(len(book.data["spend"]), 3)
            self.assertIsNotNone(book.seen(1))
            self.assertIsNone(book.seen(2))
            self.assertEqual(book.failure_count(3, now), 1)
            self.assertEqual(book.failure_count(4, now), 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "counts.json"
            self.assertTrue(ledger.save(book, path, NOW))
            original = path.read_bytes()
            book.record_posted("season")
            with patch.object(ledger.os, "replace", side_effect=OSError("FAKEKEY123")):
                with self.assertLogs("fbot", level="INFO") as logs:
                    self.assertFalse(ledger.save(book, path, NOW))
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn("FAKEKEY123", "\n".join(logs.output))
            self.assertNotIn(b"FAKEKEY123", Path(str(path) + ".tmp").read_bytes())

    def test_hardening_random_rationale_lengths_and_season_boundary(self):
        rng = random.Random(9206)
        for _ in range(150):
            summary = "S" * rng.randint(0, 10000)
            texts = ["r" * rng.randint(0, 6000) for _ in range(rng.randint(0, 20))]
            out = hardening.cap_rationales(summary, texts)
            self.assertLessEqual(len(summary) + sum(len(text) + 2 for text in out), 10000)
            for i, text in enumerate(out, 1):
                self.assertTrue(text.startswith(f"RUN {i}\n"))
                self.assertGreater(len(text), len(f"RUN {i}\n"))
                self.assertLessEqual(len(text), 1500 if len(texts) >= 4 else 2500)
        for days in range(30):
            end = NOW + timedelta(days=days)
            env = {"SEASON_END_UTC": end.isoformat()}
            self.assertTrue(hardening.season_open(end - timedelta(microseconds=1), env))
            self.assertFalse(hardening.season_open(end, env))
            self.assertFalse(hardening.season_over_actions(end, env, ["[BOT ALERT] SEASON_OVER"])["post_season_over"])

    def test_misread_negation_window_and_clause_properties(self):
        question = Question(1, 2, "binary", "Synthetic")
        for negation in sorted(misread.NEGATIONS) + ["isn't", "isn\u2019t"]:
            for distance in range(7):
                phrase = negation + " filler" * distance + " outcome is known"
                self.assertEqual(bool(misread.matches(phrase)), distance >= misread.WINDOW)
                self.assertFalse(misread.drop(question, phrase, 0.2))
                self.assertEqual(misread.drop(question, phrase, 0.99), distance >= misread.WINDOW)
                for boundary in (".", ";", ":", "!", "?", "\n"):
                    self.assertTrue(misread.drop(question, phrase.split(" outcome")[0] + boundary + "outcome is known", 0.99))


if __name__ == "__main__":
    unittest.main()
