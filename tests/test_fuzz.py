"""Property ("fuzz") tests over the new modules and v0's validators: multiple choice with 10-12 and
30-60 options, budgets, the limiter, the ledger, coverage counting and the misread guard.

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT; reviewed 15:20 CT).
Contract: INTERFACE.md sections 1 to 7. Fixed seeds; the whole file runs in a few seconds.
"""
import asyncio
from datetime import timedelta
import json
import random
import unittest
from fbot import SkipQuestion
from fbot import config, coverage, ledger, misread, parse, prompts_v1, schedule, validate
from fbot.targets import utc
from fbot.types import Question, Research, Result
from .fakes import FakeClock
from .vclock import VirtualClock, make_post, sub_question

NOW = utc("2026-10-20T12:00:00Z")
WORDS = ("north", "river", "delta", "summit", "harbor", "copper", "maple", "orbit", "signal", "cedar",
         "falcon", "prairie", "quartz", "lantern", "meadow", "pioneer", "granite", "vector", "willow", "zenith")
ENDINGS = ("", " 12", " 305", " (other)", ": reserve", " / joint", " 10-20%", " or more")


def labels(rng, count):
    seen, out = set(), []
    while len(out) < count:
        words = [word.capitalize() if rng.random() < 0.5 else word for word in rng.sample(WORDS, rng.randint(1, 3))]
        label = " ".join(words) + rng.choice(ENDINGS)
        key = " ".join(label.split()).casefold()
        if key not in seen:
            seen.add(key)
            out.append(label)
    return out


def composition(rng, count, total=100):
    cuts = sorted(rng.sample(range(1, total), count - 1))
    return [b - a for a, b in zip([0] + cuts, cuts + [total])]


class FuzzTests(unittest.TestCase):
    def test_mc_prompt_parse_repair_roundtrip(self):
        rng = random.Random(9101)
        for count in (10, 11, 12, 30, 35, 40, 45, 50, 55, 60):
            for trial in range(6):
                options = labels(rng, count)
                question = Question(count * 100 + trial, 1, "multiple_choice", "Synthetic fuzz question",
                                    options=tuple(options), close_time=NOW + timedelta(hours=2))
                prompt = prompts_v1.build(question, Research(), NOW)
                lines = prompt.splitlines()
                start = lines.index(prompts_v1.OPTIONS_HEADER) + 1
                self.assertEqual(lines[start:start + count], ["- " + option for option in options])
                self.assertEqual(lines[-count:], [option + ": NN%" for option in options])
                self.assertEqual(misread.matches(prompt), [])
                self.assertNotIn("bayes", prompt.lower())
                reply = ("OUTSIDE VIEW: a\nINSIDE VIEW: b\nFINAL\n" +
                         "\n".join(f"{option}: {share}%" for option, share in zip(options, composition(rng, count))))
                parsed = parse.parse(question, reply)
                self.assertAlmostEqual(sum(parsed.values()), 1)
                repaired = validate.repair(parsed)
                self.assertTrue(validate.multiple_choice(question, repaired))
                self.assertFalse(misread.drop(question, reply, parsed))

    def test_budget_invariants(self):
        rng = random.Random(9102)
        for _ in range(600):
            clock = FakeClock("2026-10-05T12:00:00Z")
            job_age = rng.uniform(0, 60 * 60)
            until = None if rng.random() < 0.2 else rng.uniform(-60, 120 * 60)
            close = None if until is None else clock.now() + timedelta(seconds=until)
            question = Question(1, 2, "binary", "Synthetic", close_time=close)
            try:
                budget = schedule.budget(question, clock, clock.now() - timedelta(seconds=job_age))
            except SkipQuestion as skip:
                self.assertEqual(skip.reason, "TOO_LATE")
                continue
            self.assertGreater(budget.seconds, 0)
            self.assertLessEqual(budget.seconds, 1500)
            self.assertLessEqual(job_age + budget.seconds, 3300 + 1e-6)
            self.assertAlmostEqual(budget.deadline, clock.monotonic() + budget.seconds)
            if until is None:
                self.assertFalse(budget.fast)
            else:
                self.assertGreaterEqual(until, 240)
                self.assertEqual(budget.fast, until < 720)
                self.assertLessEqual(budget.seconds, until - (120 if budget.fast else 300) + 1e-6)
                self.assertEqual(budget.tier_hint, "M" if budget.fast else None)

    def test_limiter_random_holds(self):
        rng = random.Random(9103)
        for _ in range(25):
            clock = VirtualClock()
            limit = rng.randint(schedule.MIN_CONCURRENT, schedule.MAX_CONCURRENT)
            plan = [((rng.randint(0, 5), index), rng.choice([0, 0, 30, 60]), rng.randint(1, 900))
                    for index in range(rng.randint(1, 24))]
            box = {}

            async def main():
                limiter = schedule.PriorityLimiter(limit)
                box["limiter"], finished = limiter, []

                async def one(key, start, hold):
                    await clock.asleep(start)
                    async with limiter.slot(key):
                        await clock.asleep(hold)
                    finished.append(key)

                await asyncio.gather(*(one(*item) for item in plan))
                return finished

            finished = clock.run(main())
            limiter = box["limiter"]
            self.assertEqual(sorted(finished), sorted(item[0] for item in plan))
            self.assertLessEqual(limiter.peak, limit)
            self.assertEqual((limiter.active, limiter.waiting), (0, 0))
            self.assertEqual(len(limiter.grants), len(plan))
            for key, next_waiting in limiter.grants:
                if next_waiting is not None:
                    self.assertLessEqual(key, next_waiting)

    def test_ledger_sanitize_random_junk(self):
        rng = random.Random(9104)
        atoms = ["FAKEKEY123", "tok-FAKEKEY123", "2026-10-10", "C", "M", "season", "TOO_LATE", "101", "-3", "",
                 1, -1, 0, 7, 2.5, float("nan"), float("inf"), True, None]

        def junk(depth):
            roll = rng.random()
            if depth <= 0 or roll < 0.4:
                return rng.choice(atoms)
            if roll < 0.7:
                return [junk(depth - 1) for _ in range(rng.randint(0, 4))]
            return {str(rng.choice(atoms)): junk(depth - 1) for _ in range(rng.randint(0, 4))}

        names = sorted(ledger.KEYS) + ["rationale", "probability", "extra"]
        for _ in range(300):
            raw = {name: junk(3) for name in rng.sample(names, 6)}
            clean = ledger.sanitize(raw)
            text = json.dumps(clean, allow_nan=False)  # raises if NaN or infinity survived
            self.assertNotIn("FAKEKEY123", text)
            self.assertNotIn("tok-", text)
            self.assertEqual(set(clean), ledger.KEYS)
            self.assertEqual(ledger.sanitize(clean), clean)
            self.assertEqual(ledger.sanitize(json.loads(text)), clean)

    def test_coverage_counts_random_posts(self):
        rng = random.Random(9105)
        for _ in range(150):
            posts, expect, qid = [], {"forecast": set(), "none": set(), "absent": set()}, 1
            for pid in range(rng.randint(0, 25)):
                subs = []
                for _ in range(rng.choice([1, 1, 1, 2, 3, 4])):
                    closed = NOW - timedelta(seconds=rng.randint(-86400, 10 * 86400))
                    state = rng.choice(["forecast", "none", "absent"])
                    kind = rng.choice(["binary", "multiple_choice", "numeric", "discrete", "date"])
                    subs.append(sub_question(qid, closed, state, kind))
                    if kind != "date" and NOW - timedelta(days=7) <= closed <= NOW:
                        expect[state].add(qid)
                    qid += 1
                posts.append(make_post(pid + 1, subs, group=len(subs) > 1))
            result = coverage.count_recent(posts, NOW)
            self.assertEqual(result["forecasted"], len(expect["forecast"]))
            self.assertEqual(result["forfeited_ids"], sorted(expect["none"]))
            self.assertEqual(result["unknown_ids"], sorted(expect["absent"]))
            self.assertEqual(result["closed"], sum(len(ids) for ids in expect.values()))
            self.assertEqual(result["missed"], result["forfeited_ids"])

    def test_misread_moderate_runs_never_drop(self):
        rng = random.Random(9106)
        mc = Question(3, 4, "multiple_choice", "Synthetic", options=("A", "B", "C"))
        binary = Question(1, 2, "binary", "Synthetic")
        phrases = ("has already happened", "already resolved", "resolved yes", "resolved no", "outcome is known",
                   "this already occurred", "question has resolved", "has already resolved")
        for _ in range(400):
            sentence = " ".join((rng.choice(("Analysts said", "In 2019", "Historically", "A similar case")),
                                 rng.choice(phrases), rng.choice(("twice.", "in one country.", "before the vote."))))
            self.assertTrue(misread.matches(sentence), sentence)
            self.assertFalse(misread.drop(binary, sentence, rng.uniform(0.05, 0.95)), sentence)
            a = rng.uniform(0.05, 0.9)
            b = rng.uniform(0, 1 - a)
            self.assertFalse(misread.drop(mc, sentence, {"A": a, "B": b, "C": 1 - a - b}), sentence)
            extreme = rng.choice((rng.uniform(0.0, 0.0499), rng.uniform(0.9501, 1.0)))
            self.assertTrue(misread.drop(binary, sentence, extreme), sentence)

    @unittest.skipIf(config.VERSION == "v0", "needs the v0.1 comment builder (review R12); runs at integration")
    def test_comment_builds_for_many_options(self):
        from fbot import comment
        rng = random.Random(9107)
        for count in (12, 15, 30, 60):
            options = labels(rng, count)
            question = Question(count, 1, "multiple_choice", "Synthetic fuzz question", options=tuple(options))
            value = validate.repair({o: s / 100 for o, s in zip(options, composition(rng, count))})
            result = Result(value, "", ["OUTSIDE VIEW: a; base rate 20%\nINSIDE VIEW: b\nFINAL\nsee values"],
                            models=[config.SOL[0]], tier="C")
            built = comment.build(question, result, 1, Research(), {})
            self.assertTrue(built.summary.startswith("FBOT "))
            self.assertLessEqual(built.summary.index("FINAL"), 200)
            self.assertLessEqual(len(built.summary), 1000)


if __name__ == "__main__":
    unittest.main()
