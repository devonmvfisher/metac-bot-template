"""Clock-injected polling and deadlines, with SDK calls supplied by the adapter."""
import asyncio
import logging
import math
from . import SkipQuestion
from .config import SEASON_ID, MINIBENCH_ID, MAX_QUESTIONS, enabled
from .targets import active, utc


def deadline(question, clock, started):
    seconds = min(25 * 60, 55 * 60 - (clock.now() - started).total_seconds())
    urgent = False
    if question.close_time is not None:
        until_close = (utc(question.close_time) - clock.now()).total_seconds()
        if until_close < 4 * 60:
            raise SkipQuestion("TOO_LATE")
        seconds = min(seconds, until_close - 5 * 60)
        urgent = until_close < 12 * 60
    if seconds <= 0:
        raise SkipQuestion("TOO_LATE")
    return clock.monotonic() + seconds, urgent


def can_start(clock, started, loop_minutes=45):
    return (clock.now() - started).total_seconds() < min(loop_minutes, 45) * 60


async def bounded_questions(questions, callback, state):
    semaphore = asyncio.Semaphore(MAX_QUESTIONS)

    async def one(question):
        async with semaphore:
            try:
                return await callback(question)
            except SkipQuestion as skip:
                state.skip(question, skip.reason)
            except Exception:
                state.skip(question, "INVALID_OUTPUT")
    return await asyncio.gather(*(one(question) for question in questions), return_exceptions=True)


async def run(forecast_on_tournament, state, clock, loop_minutes=45, poll_minutes=10, sleep=None):
    if not 0 < loop_minutes <= 45 or not 0 < poll_minutes <= 10:
        raise ValueError("invalid poll limits")
    sleep = sleep or asyncio.sleep
    start = state.started
    next_poll = 0
    while can_start(clock, start, loop_minutes):
        if not active(clock.now()):
            state.alert("SEASON_OVER")
            break
        for target in (SEASON_ID, MINIBENCH_ID):
            if not can_start(clock, start, loop_minutes) or not active(clock.now()):
                break
            label = "season" if target == SEASON_ID else "minibench"
            state.polls[label] += 1
            try:
                await forecast_on_tournament(target, return_exceptions=True)
            except Exception as error:
                state.poll_errors[label] += 1
                state.counts["poll_errors"] += 1
                logging.getLogger("fbot").warning("POLL_ERROR target=%s error=%s", label, type(error).__name__)
        elapsed = (clock.now() - start).total_seconds()
        next_poll = max(next_poll + poll_minutes * 60, (int(elapsed // (poll_minutes * 60)) + 1) * poll_minutes * 60)
        remaining = min(next_poll, loop_minutes * 60) - elapsed
        if remaining > 0:
            await sleep(remaining)
    state.check_polls()
    return state.snapshot()


def should_dispatch(env, now):
    return enabled(env, "BOT_ENABLED") and enabled(env, "CHAIN_ENABLED") and bool(active(now))


def dispatch_wait(job_start_epoch, now):
    return min(15 * 60, max(0, 15 * 60 - (now.timestamp() - job_start_epoch)))


def minutes(value, default, name):
    try:
        result = float(value)
        if not math.isfinite(result) or not 0 < result <= default:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        logging.getLogger("fbot").warning("CONFIG %s invalid; using %s", name, default)
        return default
