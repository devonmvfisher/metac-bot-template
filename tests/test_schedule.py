"""Acceptance tests for fbot.schedule: 15-minute runs, closest-closing first, the fast path,
concurrent targets and the outside-timer helpers (v1 item 3; review L05, L06, L16).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT).
Contract: INTERFACE.md section 1. Timing tests run on a virtual clock; none sleeps for real.
"""
import asyncio
from datetime import timedelta
import logging
import unittest
from fbot import SkipQuestion
from fbot import ops, schedule
from fbot.state import RunState
from fbot.targets import utc
from fbot.types import Question
from .fakes import FakeClock
from .vclock import FakeTournament, VirtualClock, ops_data, timer_runs


def burst(origin, open_at, count=5, first_qid=100, target="season", window=5400):
    return [(open_at, Question(first_qid + i, first_qid + i + 5000, "binary", "Synthetic burst",
                               target=target, close_time=origin + timedelta(seconds=open_at + window)))
            for i in range(count)]


def work(normal):
    return lambda question, budget: 120 if budget.fast else normal


def question_closing(clock, minutes, qid=1):
    close = None if minutes is None else clock.now() + timedelta(minutes=minutes)
    return Question(qid, qid + 1000, "binary", "Synthetic", close_time=close)


class ScheduleTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((schedule.MIN_CONCURRENT, schedule.MAX_CONCURRENT), (3, 5))
        self.assertEqual(schedule.FAST_PATH_SECONDS, 720)
        self.assertEqual(schedule.TOO_LATE_SECONDS, 240)
        self.assertEqual(schedule.CLOSE_MARGIN_SECONDS, 300)
        self.assertEqual(schedule.FAST_CLOSE_MARGIN_SECONDS, 120)
        self.assertEqual(schedule.QUESTION_CAP_SECONDS, 1500)
        self.assertEqual(schedule.JOB_CAP_SECONDS, 3300)
        self.assertEqual(schedule.JOB_GRACE_SECONDS, 300)
        self.assertEqual((schedule.NORMAL_TIMEOUT_SECONDS, schedule.FAST_TIMEOUT_SECONDS), (480, 300))
        self.assertEqual(schedule.CONCURRENCY_GROUP, "fbot-post-tournament")
        self.assertEqual(schedule.TIMER_WORKFLOW, "run_bot_on_timer.yaml")
        self.assertEqual(schedule.TOURNAMENT_WORKFLOW, "run_bot_on_tournament.yaml")
        self.assertEqual(schedule.SWITCH_DEFAULTS,
                         {"PROMPTS_V1": True, "DAILY_PROBE": True, "CONCURRENT_TARGETS": True})

    def test_loop_end_uses_job_start_l05(self):
        job = utc("2026-10-05T12:00:00Z")
        # A slow install: the process starts 12 minutes into the job, so JOB_START + 15 + 5 ends first.
        self.assertEqual(schedule.loop_end(job + timedelta(minutes=12), job, 15), job + timedelta(minutes=20))
        # A quick install: the process's own 15 minutes end first.
        self.assertEqual(schedule.loop_end(job + timedelta(minutes=2), job, 15), job + timedelta(minutes=17))
        self.assertEqual(schedule.loop_end(job, None, 45), job + timedelta(minutes=45))
        self.assertEqual(schedule.loop_end("2026-10-05T12:02:00Z", "2026-10-05T12:00:00Z", 15),
                         job + timedelta(minutes=17))
        for bad in (0, -1, 46, float("nan"), float("inf"), True, "15", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                schedule.loop_end(job, job, bad)

    def test_budget_fast_path_and_margins(self):
        clock = FakeClock("2026-10-05T12:00:00Z")
        job = clock.now()
        fast = schedule.budget(question_closing(clock, 11), clock, job)
        self.assertTrue(fast.fast)
        self.assertEqual((fast.tier_hint, fast.timeout), ("M", 300))
        self.assertAlmostEqual(fast.seconds, 11 * 60 - 120)
        self.assertAlmostEqual(fast.deadline, clock.monotonic() + fast.seconds)
        normal = schedule.budget(question_closing(clock, 12), clock, job)
        self.assertFalse(normal.fast)
        self.assertEqual((normal.tier_hint, normal.timeout), (None, 480))
        self.assertAlmostEqual(normal.seconds, 12 * 60 - 300)
        self.assertAlmostEqual(schedule.budget(question_closing(clock, 4), clock, job).seconds, 120)
        with self.assertRaisesRegex(SkipQuestion, "TOO_LATE"):
            schedule.budget(question_closing(clock, 3.99), clock, job)
        self.assertAlmostEqual(schedule.budget(question_closing(clock, 600), clock, job).seconds, 1500)
        self.assertAlmostEqual(schedule.budget(question_closing(clock, None), clock, job).seconds, 1500)
        self.assertFalse(schedule.budget(question_closing(clock, None), clock, job).fast)
        clock.sleep(50 * 60)
        self.assertAlmostEqual(schedule.budget(question_closing(clock, None), clock, job).seconds, 300)
        self.assertAlmostEqual(schedule.budget(question_closing(clock, 600), clock, job).deadline,
                               clock.monotonic() + 300)
        clock.sleep(5 * 60)
        with self.assertRaisesRegex(SkipQuestion, "TOO_LATE"):
            schedule.budget(question_closing(clock, None), clock, job)

    def test_priority_key(self):
        close = utc("2026-10-05T13:00:00Z")
        self.assertEqual(schedule.priority_key(Question(7, 8, "binary", "x", close_time=close)), (close, 7))
        self.assertEqual(schedule.priority_key(Question(7, 8, "binary", "x")), (schedule.FAR_FUTURE, 7))
        self.assertLess(schedule.priority_key(Question(9, 8, "binary", "x", close_time=close)),
                        schedule.priority_key(Question(1, 8, "binary", "x")))
        self.assertLess(schedule.priority_key(Question(1, 8, "binary", "x", close_time=close)),
                        schedule.priority_key(Question(2, 8, "binary", "x", close_time=close)))

    def test_limiter_bounds(self):
        for bad in (2, 6, 0, 3.0, True, "5", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                schedule.PriorityLimiter(bad)
        self.assertEqual(schedule.PriorityLimiter().limit, 5)
        self.assertEqual(schedule.PriorityLimiter(3).limit, 3)

    def test_limiter_grants_closest_first_in_one_turn(self):
        async def scenario():
            limiter = schedule.PriorityLimiter(5)
            order, release = [], asyncio.Event()

            async def one(key):
                async with limiter.slot(key):
                    order.append(key)
                    await release.wait()

            tasks = [asyncio.ensure_future(one((k, k))) for k in (8, 3, 7, 1, 6, 2, 5, 4)]
            for _ in range(20):
                await asyncio.sleep(0)
            first, busy = list(order), (limiter.active, limiter.waiting, limiter.peak)
            release.set()
            await asyncio.gather(*tasks)
            return first, busy, order, limiter

        first, busy, order, limiter = asyncio.run(scenario())
        # Every request made in the same event-loop turn competes by key, not by arrival.
        self.assertEqual(first, [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
        self.assertEqual(busy, (5, 3, 5))
        self.assertEqual(order, sorted(order))
        self.assertEqual((limiter.active, limiter.waiting), (0, 0))
        self.assertEqual(len(limiter.grants), 8)
        for key, next_waiting in limiter.grants:
            if next_waiting is not None:
                self.assertLessEqual(key, next_waiting)
        self.assertEqual(limiter.grants[-1], ((8, 8), None))

    def test_cancelled_waiter_never_takes_a_slot(self):
        async def scenario():
            limiter = schedule.PriorityLimiter(3)
            gate = asyncio.Event()

            async def hold(key):
                async with limiter.slot(key):
                    await gate.wait()

            holders = [asyncio.ensure_future(hold((i,))) for i in range(3)]
            for _ in range(10):
                await asyncio.sleep(0)
            waiter = asyncio.ensure_future(hold((-1,)))
            late = asyncio.ensure_future(hold((9,)))
            for _ in range(10):
                await asyncio.sleep(0)
            waiter.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(*holders, late)
            return limiter

        limiter = asyncio.run(scenario())
        self.assertEqual((limiter.active, limiter.waiting), (0, 0))
        self.assertNotIn((-1,), [key for key, _ in limiter.grants])
        self.assertIn((9,), [key for key, _ in limiter.grants])

    def test_release_without_acquire_raises(self):
        async def scenario():
            limiter = schedule.PriorityLimiter(3)
            with self.assertRaises(RuntimeError):
                limiter.release()
        asyncio.run(scenario())

    def test_run_targets_counts_isolates_errors_and_stops_at_end(self):
        clock = VirtualClock()
        state = RunState(clock)
        calls = []

        async def season(name):
            calls.append((name, clock.monotonic()))
            raise ValueError("private error text")

        async def minibench(name):
            calls.append((name, clock.monotonic()))
            await clock.asleep(30)

        end = clock.now() + timedelta(minutes=15)
        with self.assertLogs("fbot", level="INFO") as logs:
            logging.getLogger("fbot").info("test start")
            snapshot = clock.run(schedule.run_targets({"season": season, "minibench": minibench},
                                                      state, clock, end, 5, sleep=clock.asleep))
        self.assertEqual([t for n, t in calls if n == "season"], [0, 300, 600])
        self.assertEqual([t for n, t in calls if n == "minibench"], [0, 300, 600])
        self.assertEqual(state.counts["polls_season"], 3)
        self.assertEqual(state.counts["poll_errors_season"], 3)
        self.assertEqual(state.counts["poll_errors"], 3)
        self.assertEqual(state.counts["polls_minibench"], 3)
        self.assertEqual(state.counts["poll_errors_minibench"], 0)
        self.assertEqual(clock.monotonic(), 900)
        self.assertEqual(snapshot["counts"]["poll_errors"], 3)
        text = "\n".join(logs.output)
        self.assertIn("POLL_ERROR target=season error=ValueError", text)
        self.assertNotIn("private error text", text)

    def test_minibench_not_blocked_by_slow_season_poll_l06(self):
        for concurrent in (True, False):
            clock = VirtualClock()
            state = RunState(clock)
            started = {}

            async def season(name):
                await clock.asleep(20 * 60)  # a slow season question holds this poll open

            async def minibench(name):
                if clock.monotonic() >= 60 and "mb" not in started:
                    started["mb"] = clock.monotonic()

            end = clock.now() + timedelta(minutes=45)
            clock.run(schedule.run_targets({"season": season, "minibench": minibench}, state, clock, end, 5,
                                           sleep=clock.asleep, concurrent=concurrent))
            latency = started["mb"] - 60
            if concurrent:
                self.assertLessEqual(latency, 5 * 60)
            else:
                self.assertGreaterEqual(latency, 15 * 60)

    def test_after_season_no_poll_and_alert(self):
        clock = VirtualClock("2027-01-07T00:00:00Z")
        state, calls = RunState(clock), []

        async def poll(name):
            calls.append(name)

        clock.run(schedule.run_targets({"season": poll, "minibench": poll}, state, clock,
                                       clock.now() + timedelta(minutes=15), 5, sleep=clock.asleep))
        self.assertEqual(calls, [])
        self.assertIn("SEASON_OVER", state.alerts)

    def test_run_targets_rejects_bad_arguments(self):
        clock = VirtualClock()
        state = RunState(clock)

        async def poll(name):
            return None

        end = clock.now() + timedelta(minutes=15)
        for minutes in (0, 11, -5):
            with self.assertRaises(ValueError):
                clock.run(schedule.run_targets({"season": poll}, state, clock, end, poll_minutes=minutes,
                                               sleep=clock.asleep))
        for pollers in ({"other": poll}, {}):
            with self.assertRaises(ValueError):
                clock.run(schedule.run_targets(pollers, state, clock, end, 5, sleep=clock.asleep))

    def test_burst_of_five_inside_60_minutes(self):
        for limit in (3, 5):
            clock = VirtualClock()
            # Worst case: the burst opens just after run 1's last poll (run 1 polls at 210, 510 and 810 s).
            entries = burst(clock.now(), 820)
            tour = FakeTournament(clock, entries)
            records = timer_runs(clock, tour, schedule, runs=3, work=work(840), limit=limit)
            self.assertEqual(set(tour.posted), {q.qid for _, q in entries}, limit)
            self.assertEqual(tour.skipped, {}, limit)
            for at, question in entries:
                self.assertLessEqual(tour.posted[question.qid] - at, 3600, (limit, question.qid))
            self.assertLessEqual(max(r.limiter.peak for r in records), limit)
            self.assertGreaterEqual(max(r.limiter.peak for r in records), 3)

    def test_minibench_start_twenty_in_first_hour(self):
        clock = VirtualClock()
        origin = clock.now()
        entries, qid = [], 3001
        for wave in range(4):  # released up to 5 at a time: minutes 0, 15, 30 and 45
            for _ in range(5):
                entries.append((wave * 900, Question(qid, qid + 5000, "binary", "Synthetic MiniBench",
                                                     target="minibench",
                                                     close_time=origin + timedelta(seconds=wave * 900 + 5400))))
                qid += 1
        entries += burst(origin, 1200, first_qid=4001)  # season questions arrive in the same hour
        tour = FakeTournament(clock, entries)
        records = timer_runs(clock, tour, schedule, runs=6, work=work(600))
        minibench = {q.qid for _, q in entries if q.target == "minibench"}
        self.assertEqual(len(minibench), 20)
        self.assertTrue(minibench <= set(tour.started))
        self.assertTrue(minibench <= set(tour.posted))
        self.assertEqual(tour.skipped, {})
        for record in records:
            self.assertLessEqual(record.limiter.peak, schedule.MAX_CONCURRENT)
            for key, next_waiting in record.limiter.grants:
                if next_waiting is not None:
                    self.assertLessEqual(key, next_waiting)

    def test_closest_closing_first_saves_tight_questions(self):
        clock = VirtualClock()
        origin = clock.now()
        first_poll = 210  # queue 30 s plus install 180 s
        loose = [(0, Question(5010 + i, 9010 + i, "binary", "Synthetic loose", target="minibench",
                              close_time=origin + timedelta(minutes=120))) for i in range(5)]
        tight = [(0, Question(5001 + i, 9001 + i, "binary", "Synthetic tight", target="minibench",
                              close_time=origin + timedelta(seconds=first_poll + 25 * 60))) for i in range(5)]
        tour = FakeTournament(clock, loose + tight)  # the listing puts the loose ones first
        timer_runs(clock, tour, schedule, runs=1, work=work(720))
        self.assertEqual(set(tour.posted), {q.qid for _, q in loose + tight})
        self.assertEqual(tour.skipped, {})
        self.assertEqual(sorted(tour.started[:5]), [q.qid for _, q in tight])

    def test_fast_path_takes_questions_closing_within_12_minutes(self):
        clock = VirtualClock()
        entries = [(0, Question(701, 7701, "binary", "Synthetic urgent", target="minibench",
                                close_time=clock.now() + timedelta(seconds=210 + 600)))]
        tour = FakeTournament(clock, entries)
        timer_runs(clock, tour, schedule, runs=1, work=work(840))
        self.assertIn(701, tour.posted)
        self.assertTrue(tour.budgets[701].fast)
        self.assertEqual(tour.budgets[701].tier_hint, "M")

    def test_automatic_run(self):
        self.assertTrue(schedule.automatic_run({"GITHUB_EVENT_NAME": "schedule"}))
        self.assertTrue(schedule.automatic_run({"GITHUB_EVENT_NAME": "workflow_dispatch", "FBOT_TRIGGER": "timer"}))
        self.assertTrue(schedule.automatic_run({"FBOT_TRIGGER": " Timer "}))
        self.assertFalse(schedule.automatic_run({"GITHUB_EVENT_NAME": "workflow_dispatch"}))
        self.assertFalse(schedule.automatic_run({"FBOT_TRIGGER": "manual"}))
        self.assertFalse(schedule.automatic_run({}))

    def test_gap_minutes_merges_both_workflows_real_shape(self):
        runs = ops_data("github_runs_real")["workflow_runs"]
        now = utc("2026-05-14T21:00:00Z")
        self.assertAlmostEqual(ops.longest_gap(runs, now), 26.4)
        timer = [{"id": 1, "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
                  "run_started_at": "2026-05-14T19:55:00Z", "updated_at": "2026-05-14T20:05:00Z"},
                 {"id": 2, "event": "workflow_dispatch", "status": "completed", "conclusion": "cancelled",
                  "run_started_at": "2026-05-14T20:15:00Z", "updated_at": "2026-05-14T20:35:00Z"}]
        # The timer run fills the first gap; the cancelled one is ignored; a repeated list changes nothing.
        self.assertAlmostEqual(schedule.gap_minutes([runs, timer, runs], now), 26.0)
        self.assertEqual(schedule.gap_minutes([], now), 0)

    def test_switch_parsing(self):
        env = {"S_A": "true", "S_B": " FALSE ", "S_C": "0", "S_D": "on", "S_E": "", "ZZ_TEST_INVALID": "maybe"}
        self.assertTrue(schedule.switch(env, "S_A", default=False))
        self.assertFalse(schedule.switch(env, "S_B"))
        self.assertFalse(schedule.switch(env, "S_C"))
        self.assertTrue(schedule.switch(env, "S_D", default=False))
        self.assertTrue(schedule.switch(env, "S_E"))
        self.assertFalse(schedule.switch(env, "S_E", default=False))
        self.assertTrue(schedule.switch(env, "MISSING_NAME"))
        self.assertTrue(schedule.switch({}, "PROMPTS_V1"))
        with self.assertLogs("fbot", level="INFO") as logs:
            self.assertTrue(schedule.switch(env, "ZZ_TEST_INVALID"))
            self.assertTrue(schedule.switch(env, "ZZ_TEST_INVALID"))
        self.assertEqual(sum("CONFIG ZZ_TEST_INVALID invalid; using true" in line for line in logs.output), 1)
        with self.assertRaises(ValueError):
            schedule.switch(env, "lower-case")


if __name__ == "__main__":
    unittest.main()
