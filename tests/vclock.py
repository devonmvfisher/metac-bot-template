"""Virtual-time and fake-API helpers for the scheduling, coverage and fuzz tests. Test code only.

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT; reviewed 15:20 CT).
No network, no threads, no real sleeping: virtual time moves only when every task is waiting.
"""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import heapq
import itertools
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from fbot import SkipQuestion
from fbot.state import RunState
from fbot.targets import utc
from fbot.types import Question

FIXTURES = Path(__file__).parent / "fixtures" / "ops"


class VirtualClock:
    """now(), monotonic() and asleep() on a virtual timeline. run() drives one coroutine to the end."""

    def __init__(self, date="2026-10-05T00:00:00Z"):
        self.origin = utc(date)
        self.seconds = 0.0
        self.sleeps = []
        self._heap = []
        self._order = itertools.count()

    def now(self):
        return self.origin + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        # Synchronous sleeps (for example a model-call retry) simply move time forward.
        self.sleeps.append(seconds)
        self.seconds += max(0.0, seconds)

    async def asleep(self, seconds):
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.seconds + seconds, next(self._order), future))
        await future

    def run(self, coroutine, limit_hours=48):
        async def driver():
            main = asyncio.ensure_future(coroutine)
            while True:
                for _ in range(200):
                    if main.done():
                        return main.result()
                    await asyncio.sleep(0)
                while self._heap and self._heap[0][2].done():
                    heapq.heappop(self._heap)
                if not self._heap:
                    raise AssertionError("virtual deadlock: nothing sleeps and the run has not finished")
                wake = self._heap[0][0]
                if wake > limit_hours * 3600:
                    raise AssertionError("virtual time limit passed")
                self.seconds = max(self.seconds, wake)
                while self._heap and self._heap[0][0] <= self.seconds:
                    future = heapq.heappop(self._heap)[2]
                    if not future.done():
                        future.set_result(None)
        return asyncio.run(driver())


def iso(value):
    return utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def ops_data(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def ops_question(name):
    fields = dict(ops_data(name)["question"])
    for key in ("options", "retired"):
        if key in fields:
            fields[key] = tuple(fields[key])
    for key in ("close_time", "resolve_time"):
        if fields.get(key):
            fields[key] = utc(fields[key])
    return Question(**fields)


@dataclass
class RunRecord:
    job_started: object
    end: object
    limiter: object
    snapshot: dict
    state: object


class FakeTournament:
    """Questions that open at set virtual seconds. A poll lists the open ones not yet posted, skipped
    or in flight, highest qid first, so list order is never the close order."""

    def __init__(self, clock, entries):
        self.clock = clock
        self.entries = list(entries)
        self.opened = {question.qid: at for at, question in self.entries}
        self.posted, self.skipped, self.started_at, self.listed_at, self.budgets = {}, {}, {}, {}, {}
        self.started, self.inflight = [], set()

    def listing(self, target):
        now = self.clock.monotonic()
        found = []
        for at, question in self.entries:
            if question.target != target or at > now:
                continue
            if question.qid in self.posted or question.qid in self.skipped or question.qid in self.inflight:
                continue
            if question.close_time is not None and utc(question.close_time) <= self.clock.now():
                continue
            self.listed_at.setdefault(question.qid, now)
            found.append(question)
        return sorted(found, key=lambda question: question.qid, reverse=True)


def make_poller(tour, limiter, schedule, job_started, work):
    """The SDK's forecast_on_tournament reduced to scheduling: list, then run every listed question at once.
    work(question, budget) returns the virtual seconds the question's model runs would take."""

    async def one(question):
        tour.inflight.add(question.qid)
        try:
            async with limiter.slot(schedule.priority_key(question)):
                tour.started.append(question.qid)
                tour.started_at[question.qid] = tour.clock.monotonic()
                try:
                    budget = schedule.budget(question, tour.clock, job_started)
                except SkipQuestion as skip:
                    tour.skipped[question.qid] = skip.reason
                    return
                tour.budgets[question.qid] = budget
                need = work(question, budget)
                if need > budget.seconds:
                    await tour.clock.asleep(budget.seconds)
                    tour.skipped[question.qid] = "TOO_LATE"
                    return
                await tour.clock.asleep(need)
                tour.posted[question.qid] = tour.clock.monotonic()
        finally:
            tour.inflight.discard(question.qid)

    async def poll(target):
        await asyncio.gather(*(one(question) for question in tour.listing(target)))

    return poll


def timer_runs(clock, tour, schedule, runs, work, limit=None, loop_minutes=15, poll_minutes=5,
               install_seconds=180, queue_seconds=30, concurrent=True):
    """Back-to-back runs, as the outside timer plus the shared concurrency group give them:
    each run starts when the previous one ends, after a short queue and an install."""
    records = []

    async def main():
        for _ in range(runs):
            await clock.asleep(queue_seconds)
            job_started = clock.now()
            await clock.asleep(install_seconds)
            state = RunState(clock)
            state.job_started = job_started
            limiter = schedule.PriorityLimiter(limit or schedule.MAX_CONCURRENT)
            end = schedule.loop_end(state.started, job_started, loop_minutes)
            pollers = {name: make_poller(tour, limiter, schedule, job_started, work)
                       for name in ("season", "minibench")}
            snapshot = await schedule.run_targets(pollers, state, clock, end, poll_minutes,
                                                  sleep=clock.asleep, concurrent=concurrent)
            records.append(RunRecord(job_started, end, limiter, snapshot, state))
        return records

    return clock.run(main())


def sub_question(qid, closed_at, state="forecast", kind="binary", status="closed"):
    """One question dict shaped like Metaculus's serialize_question output (field names only; values invented).
    state: forecast (my_forecasts.latest set), none (latest null) or absent (no my_forecasts key)."""
    question = {"id": qid, "title": "Synthetic", "type": kind, "status": status,
                "scheduled_close_time": iso(closed_at), "actual_close_time": iso(closed_at)}
    if state != "absent":
        latest = None
        if state == "forecast":
            latest = {"start_time": 1760000000.0, "end_time": None, "forecast_values": [0.6, 0.4]}
        question["my_forecasts"] = {"history": [], "latest": latest, "score_data": {}}
    return question


def make_post(pid, questions, tournament=33121, group=False):
    post = {"_tournament": tournament, "id": pid, "title": "Synthetic",
            "scheduled_close_time": max(q["scheduled_close_time"] for q in questions)}
    if group:
        post["group_of_questions"] = {"id": pid, "questions": list(questions)}
    else:
        post["question"] = questions[0]
    return post


def without_forecasts(post, keep):
    """A copy of one post; its questions lose my_forecasts unless keep is true."""
    if keep:
        return post
    post = dict(post)
    if isinstance(post.get("question"), dict):
        post["question"] = {key: value for key, value in post["question"].items() if key != "my_forecasts"}
    group = post.get("group_of_questions")
    if isinstance(group, dict):
        post["group_of_questions"] = dict(group, questions=[
            {key: value for key, value in question.items() if key != "my_forecasts"}
            for question in group.get("questions", [])])
    return post


class FakePostsAPI:
    """Serves GET /api/posts/ pages from a list of posts with limit/offset paging, like the site.
    Like the site, limit is capped at 100 and "next" is never null: the site's paginator reports an
    infinite count (CountlessLimitOffsetPagination on Metaculus main), so a reader must stop on an
    empty page or on its own date rule. honour_order=False models the site silently ignoring an
    unknown order_by value. Like the site, my_forecasts is sent only when the query has with_cp=true
    (posts_list_api_view passes with_cp to serialize_post_many, which loads the user's forecasts only then)."""

    def __init__(self, posts, honour_order=True, status=200):
        self.posts, self.honour_order, self.status = list(posts), honour_order, status
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, dict(headers)))
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if method != "GET" or parts.netloc != "www.metaculus.com" or parts.path != "/api/posts/":
            return 404, {}
        if self.status != 200:
            return self.status, {}
        wanted = query.get("tournaments", [""])[0]
        posts = [post for post in self.posts if str(post.get("_tournament")) == wanted]
        if self.honour_order and query.get("order_by") == ["-scheduled_close_time"]:
            posts = sorted(posts, key=lambda post: post["scheduled_close_time"], reverse=True)
        limit = min(100, int(query.get("limit", ["20"])[0]))
        offset = int(query.get("offset", ["0"])[0])
        with_cp = query.get("with_cp") == ["true"]
        page = [without_forecasts({key: value for key, value in post.items() if not key.startswith("_")}, with_cp)
                for post in posts[offset:offset + limit]]
        following = f"https://www.metaculus.com/api/posts/?limit={limit}&offset={offset + limit}"
        return 200, {"results": page, "next": following, "previous": None}
