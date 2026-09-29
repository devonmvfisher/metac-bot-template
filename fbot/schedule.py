"""Bounded scheduling, priority slots and automatic-run helpers."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import heapq
import itertools
import logging
import math
import re
from . import SkipQuestion, targets

logger = logging.getLogger("fbot")
MIN_CONCURRENT = 3
MAX_CONCURRENT = 5
FAST_PATH_SECONDS = 720
TOO_LATE_SECONDS = 240
CLOSE_MARGIN_SECONDS = 300
FAST_CLOSE_MARGIN_SECONDS = 120
QUESTION_CAP_SECONDS = 1500
JOB_CAP_SECONDS = 3300
JOB_GRACE_SECONDS = 300
NORMAL_TIMEOUT_SECONDS = 480
FAST_TIMEOUT_SECONDS = 300
FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)
CONCURRENCY_GROUP = "fbot-post-tournament"
TIMER_WORKFLOW = "run_bot_on_timer.yaml"
TOURNAMENT_WORKFLOW = "run_bot_on_tournament.yaml"
SWITCH_DEFAULTS = {"PROMPTS_V1": True, "DAILY_PROBE": True, "CONCURRENT_TARGETS": True}
TARGET_NAMES = ("season", "minibench")
_warned = set()


def _tally(state, attribute, name):
    try:
        getattr(state, attribute)[name] += 1
    except Exception:
        pass


def _minutes(value, maximum):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 < value <= maximum)


def loop_end(process_start, job_start, loop_minutes):
    if not _minutes(loop_minutes, 45):
        raise ValueError("invalid loop minutes")
    try:
        end = targets.utc(process_start) + timedelta(minutes=loop_minutes)
        if job_start is not None:
            end = min(end, targets.utc(job_start) + timedelta(minutes=loop_minutes, seconds=JOB_GRACE_SECONDS))
        return end
    except Exception:
        raise ValueError("invalid start time") from None


@dataclass(frozen=True)
class Budget:
    deadline: float
    seconds: float
    fast: bool
    timeout: int
    tier_hint: str | None


def budget(question, clock, job_started):
    try:
        now = targets.utc(clock.now())
        until = None if question.close_time is None else (targets.utc(question.close_time) - now).total_seconds()
        if until is not None and until < TOO_LATE_SECONDS:
            raise SkipQuestion("TOO_LATE")
        fast = until is not None and until < FAST_PATH_SECONDS
        seconds = min(QUESTION_CAP_SECONDS, JOB_CAP_SECONDS - (now - targets.utc(job_started)).total_seconds())
        if until is not None:
            seconds = min(seconds, until - (FAST_CLOSE_MARGIN_SECONDS if fast else CLOSE_MARGIN_SECONDS))
        if seconds <= 0:
            raise SkipQuestion("TOO_LATE")
        return Budget(clock.monotonic() + seconds, seconds, fast,
                      FAST_TIMEOUT_SECONDS if fast else NORMAL_TIMEOUT_SECONDS, "M" if fast else None)
    except SkipQuestion:
        raise
    except Exception as error:
        logger.info('BUDGET error=%s', type(error).__name__)
        raise


def priority_key(question):
    try:
        close = FAR_FUTURE if question.close_time is None else targets.utc(question.close_time)
        return close, int(question.qid)
    except Exception:
        return FAR_FUTURE, 0


class PriorityLimiter:
    """One loop owns the heap. Each dispatch batches requests from one turn."""
    def __init__(self, limit=MAX_CONCURRENT):
        if type(limit) is not int or not MIN_CONCURRENT <= limit <= MAX_CONCURRENT:
            raise ValueError("invalid concurrency limit")
        self._limit = limit
        self._active = 0
        self._peak = 0
        self._queue = []
        self._grants = []
        self._order = itertools.count()
        self._scheduled = False

    @property
    def limit(self):
        return self._limit

    @property
    def active(self):
        return self._active

    @property
    def waiting(self):
        return sum(not entry[3].done() for entry in self._queue)

    @property
    def peak(self):
        return self._peak

    @property
    def grants(self):
        return list(self._grants)

    def _schedule(self):
        if not self._scheduled:
            self._scheduled = True
            asyncio.get_running_loop().call_soon(self._dispatch)

    def _dispatch(self):
        self._scheduled = False
        while self._queue and self._active < self._limit:
            _, _, key, future = heapq.heappop(self._queue)
            if future.cancelled():
                continue
            self._active += 1
            self._peak = max(self._peak, self._active)
            next_waiting = min((entry[2] for entry in self._queue if not entry[3].done()), default=None)
            self._grants.append((key, next_waiting))
            if not future.done():
                future.set_result(None)

    async def acquire(self, key):
        future = asyncio.get_running_loop().create_future()
        sequence = next(self._order)
        heapq.heappush(self._queue, (key, sequence, key, future))
        self._schedule()
        try:
            await future
        except asyncio.CancelledError:
            if not future.cancelled():
                self._active -= 1
            self._schedule()
            raise

    def release(self):
        if self._active <= 0:
            raise RuntimeError("no active slot")
        self._active -= 1
        self._schedule()

    @asynccontextmanager
    async def slot(self, key):
        await self.acquire(key)
        try:
            yield
        finally:
            self.release()


async def run_targets(pollers, state, clock, end, poll_minutes=10, sleep=None, concurrent=True):
    if not isinstance(pollers, dict) or not pollers or not set(pollers) <= set(TARGET_NAMES):
        raise ValueError("invalid targets")
    if not _minutes(poll_minutes, 10):
        raise ValueError("invalid poll minutes")
    sleep = asyncio.sleep if sleep is None else sleep
    period = poll_minutes * 60
    end = targets.utc(end)

    async def loop(names):
        origin = clock.now()
        k = 0
        while clock.now() < end:
            if not targets.active(clock.now(), getattr(state, "env", None)):
                state.alert("SEASON_OVER")
                break
            for name in names:
                if clock.now() >= end:
                    break
                try:
                    await pollers[name](name)
                except Exception as error:
                    state.counts["poll_errors"] += 1
                    state.counts["poll_errors_" + name] += 1
                    _tally(state, "poll_errors", name)
                    logger.info("POLL_ERROR target=%s error=%s", name, type(error).__name__)
                finally:
                    state.counts["polls_" + name] += 1
                    _tally(state, "polls", name)
            elapsed = (clock.now() - origin).total_seconds()
            k = max(k + 1, math.floor(elapsed / period) + 1)
            wake = min(origin + timedelta(seconds=k * period), end)
            delay = (wake - clock.now()).total_seconds()
            if delay > 0:
                await sleep(delay)

    if concurrent:
        await asyncio.gather(*(loop((name,)) for name in pollers))
    else:
        await loop(tuple(pollers))
    check = getattr(state, "check_polls", None)
    if callable(check):
        check()
    return state.snapshot()


def automatic_run(env):
    try:
        return env.get("GITHUB_EVENT_NAME") == "schedule" or env.get("FBOT_TRIGGER", "").strip().lower() == "timer"
    except Exception:
        return False


def gap_minutes(run_lists, now):
    from . import ops
    try:
        merged, seen = [], set()
        for run_index, runs in enumerate(run_lists):
            for item_index, run in enumerate(runs):
                key = run.get("id", (run_index, item_index))
                if key not in seen:
                    seen.add(key)
                    merged.append(run)
        return ops.longest_gap(merged, now)
    except Exception:
        return None


def switch(env, name, default=None):
    if not isinstance(name, str) or re.fullmatch(r"[A-Z0-9_]+", name) is None:
        raise ValueError("invalid switch name")
    fallback = SWITCH_DEFAULTS.get(name, True) if default is None else bool(default)
    try:
        value = env.get(name, "").strip().lower()
    except Exception:
        value = "invalid"
    if value in ("true", "1", "on", "yes"):
        return True
    if value in ("false", "0", "off", "no"):
        return False
    if value and name not in _warned:
        _warned.add(name)
        logger.info("CONFIG %s invalid; using %s", name, str(fallback).lower())
    return fallback
