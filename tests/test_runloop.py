import asyncio
from datetime import timedelta
from dataclasses import replace
import time
import unittest
from fbot import SkipQuestion
from fbot import runloop
from fbot.config import SOL, SEASON_ID
from fbot.pipeline import forecast_async
from fbot.state import RunState
from fbot.types import Research
from .fakes import FakeClock, FakeMetaculus, deps_for, fixture, narration


class RunloopTests(unittest.TestCase):
    def test_open_at_3_17_44_and_lag(self):
        clock, q = FakeClock(), fixture("binary_long")[0]
        state = RunState(clock)
        api = FakeMetaculus(state)
        listed = []
        async def poll(target, return_exceptions):
            self.assertTrue(return_exceptions)
            listed.append(target)
            if target != SEASON_ID:
                return []
            for i, minute in enumerate((3, 17, 44)):
                item = replace(q, qid=100+i, post_id=200+i)
                # Deliberately keep listing posted questions, modelling delayed API state.
                if clock.seconds >= minute * 60 and item.qid not in state.posted:
                    deps = deps_for({SOL[0]: narration("Probability: 37%")}, clock=clock)
                    result = await forecast_async(item, Research(), "C", deps)
                    api.submit(item, result)
        asyncio.run(runloop.run(poll, state, clock, sleep=clock.asleep))
        self.assertEqual(clock.seconds, 45 * 60)
        self.assertEqual(state.posted, {100, 101})
        self.assertEqual(listed[:2], [SEASON_ID, "minibench"])
        state.started = clock.now()
        asyncio.run(poll(SEASON_ID, True))
        self.assertEqual(state.posted, {100, 101, 102})
        self.assertEqual(sum(path.endswith("forecast/") for path, _ in api.requests), 3)

    def test_five_questions_second_error_isolated(self):
        clock, q = FakeClock(), fixture("binary_long")[0]
        state, processed = RunState(clock), []
        questions = [replace(q, qid=i+1) for i in range(5)]
        async def one(question):
            if question.qid == 2:
                raise ValueError("private error text")
            processed.append(question.qid)
        done = [False]
        async def poll(target, return_exceptions):
            if target == SEASON_ID and not done[0]:
                done[0] = True
                await runloop.bounded_questions(questions, one, state)
        with self.assertLogs("fbot", level="INFO") as logs:
            asyncio.run(runloop.run(poll, state, clock, sleep=clock.asleep))
        self.assertEqual(processed, [1, 3, 4, 5])
        self.assertEqual(sum(state.skips.values()), 1)
        self.assertEqual(clock.seconds, 2700)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private error text", "\n".join(logs.output))

    def test_deadline_and_end_caps(self):
        clock, q = FakeClock(), fixture("binary_urgent")[0]
        started = clock.now()
        end, urgent = runloop.deadline(q, clock, started)
        self.assertTrue(urgent)
        self.assertEqual(end, 300)
        with self.assertRaisesRegex(SkipQuestion, "TOO_LATE"):
            runloop.deadline(replace(q, close_time=clock.now()+timedelta(minutes=3)), clock, started)
        clock.sleep(44 * 60)
        end, urgent = runloop.deadline(replace(q, close_time=None), clock, started)
        self.assertEqual(end, 55 * 60)
        clock.sleep(60)
        self.assertFalse(runloop.can_start(clock, started))
        # Four-to-five-minute closes also cannot meet the five-minute safety margin.
        with self.assertRaises(SkipQuestion):
            runloop.deadline(replace(q, close_time=clock.now()+timedelta(seconds=270)), clock, started)

    def test_dispatch_truth_table_and_minimum_interval(self):
        clock = FakeClock()
        for bot in ("", "false", "true"):
            for chain in ("", "false", "true"):
                self.assertEqual(runloop.should_dispatch({"BOT_ENABLED": bot, "CHAIN_ENABLED": chain}, clock.now()),
                                 bot == chain == "true")
        self.assertEqual(runloop.dispatch_wait(clock.now().timestamp(), clock.now()), 900)
        self.assertEqual(runloop.dispatch_wait(clock.now().timestamp()-1000, clock.now()), 0)
        late = FakeClock("2027-01-07T00:00:00Z")
        self.assertFalse(runloop.should_dispatch({"BOT_ENABLED": "true", "CHAIN_ENABLED": "true"}, late.now()))

    def test_five_forecasts_concurrent(self):
        q, _ = fixture("binary_long")
        deps = deps_for({SOL[0]: narration("Probability: 37%")}, delay=.5)
        async def once(count):
            return await asyncio.gather(*(forecast_async(replace(q, qid=i), Research(), "C", deps) for i in range(count)))
        start = time.monotonic()
        asyncio.run(once(1))
        one = time.monotonic() - start
        start = time.monotonic()
        result = asyncio.run(once(5))
        many = time.monotonic() - start
        self.assertEqual(len(result), 5)
        self.assertLess(many, 2 * one)

    def test_after_season_no_forecast_one_alert(self):
        clock = FakeClock("2027-01-07T00:00:00Z")
        state, calls = RunState(clock), []
        async def poll(*args, **kwargs):
            calls.append(args)
        asyncio.run(runloop.run(poll, state, clock, sleep=clock.asleep))
        self.assertFalse(calls)
        self.assertEqual(state.alerts, {"SEASON_OVER"})
