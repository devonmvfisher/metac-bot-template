import asyncio
from collections import defaultdict
from datetime import timedelta
import json
from pathlib import Path
import threading
import time
from dataclasses import replace
from fbot import ModelFailure
from fbot.config import OPUS, SOL, FLASH
from fbot.pipeline import Dependencies
from fbot.postgate import Gate
from fbot.state import RunState
from fbot.targets import utc
from fbot.types import Question
from fbot.llm import Client
from fbot.ops import ApiError


class FakeClock:
    def __init__(self, date="2026-10-01T00:00:00Z"):
        self.date = utc(date)
        self.seconds = 0
        self.sleeps = []

    def now(self):
        return self.date + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds

    async def asleep(self, seconds):
        self.sleep(seconds)
        await asyncio.sleep(0)


class FakeLLM:
    def __init__(self, scripts=None, default="Probability: 37%"):
        self.scripts = {key: list(values) for key, values in (scripts or {}).items()}
        self.default, self.calls = default, []
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout, total=None):
        with self.lock:
            self.calls.append((method, url, dict(headers), body, timeout))
            key = "key" if method == "GET" else body["model"]
            queue = self.scripts.get(key, [])
            response = queue.pop(0) if queue else self.default
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, {"choices": [{"message": {"content": response}, "finish_reason": "stop"}]}


class FakeKey(FakeLLM):
    def __init__(self, remaining, limit=100):
        super().__init__({"key": [(200, {"data": {"limit_remaining": remaining, "limit": limit}})]})


class TextClient:
    def __init__(self, values, delay=0):
        self.values, self.delay, self.calls = values, delay, []
        self.exhausted = set()

    def slot(self, models, prompt, **kwargs):
        self.calls.append((models, prompt, kwargs))
        if self.delay:
            time.sleep(self.delay)
        value = self.values.get(models[0], ModelFailure())
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(prompt)
        return value, models[0]


class FakeMetaculus:
    def __init__(self, state):
        self.gate, self.requests = Gate(state, readback=lambda *args: False), []

    def submit(self, question, result, status=200, comment_status=200):
        from fbot.validate import payload
        self.gate.register(question, result)
        self.send("/api/questions/forecast/", [{"question": question.qid, "source": "api", **payload(question, result)}], status)
        if status == 200:
            self.send("/api/comments/create/", {"on_post": question.post_id, "text": "framework wrapper", "is_private": False}, comment_status)

    def send(self, path, body, status=200):
        outgoing, ticket = self.gate.before("POST", "https://www.metaculus.com" + path, body)
        self.requests.append((path, outgoing))
        self.gate.after(ticket, status)
        return outgoing


class FakeAskNews:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def __call__(self, query, auth, **kwargs):
        self.calls.append((query, auth, kwargs))
        if self.fail:
            raise TimeoutError("provider text must stay private")
        return "Synthetic latest news. " * 800, 6


class FakeGit:
    def __init__(self, now, age=24, events=None, fail=False):
        self.last = now.timestamp() - age * 86400
        self.calls, self.events, self.fail = [], events if events is not None else [], fail

    def run(self, *args):
        self.calls.append(args)
        if args[:2] == ("git", "log"):
            self.events.append("heartbeat")
            return str(self.last)
        if self.fail:
            raise RuntimeError("do not echo provider errors")
        return ""


class FakeGitHub:
    def __init__(self, now, issues=None, disabled=False, events=None, runs=None):
        self.now, self.entries = now, list(issues or [])
        self.disabled, self.events = disabled, events if events is not None else []
        self.writes, self.run_entries = [], list(runs or [])

    def default_branch(self):
        return "main"

    def dispatch(self, branch):
        self.events.append("dispatch")
        self.writes.append(("dispatch", branch))

    def issues(self):
        self.events.append("issues")
        if self.disabled:
            raise ApiError(410)
        return self.entries

    def latest(self, issue):
        return utc(issue["created_at"])

    def create(self, title, body, label):
        self.writes.append(("create", title, body, label))
        self.entries.append({"number": len(self.entries) + 1, "title": title,
                             "created_at": self.now.isoformat()})

    def comment(self, number, body):
        self.writes.append(("comment", number, body))
        for issue in self.entries:
            if issue["number"] == number:
                issue["created_at"] = self.now.isoformat()

    def runs(self, workflow=None):
        return self.run_entries


def fixture(name):
    data = json.loads((Path(__file__).parent / "fixtures" / (name + ".json")).read_text())
    fields = dict(data["question"])
    for key in ("options", "retired"):
        if key in fields:
            fields[key] = tuple(fields[key])
    for key in ("close_time", "resolve_time"):
        if fields.get(key):
            fields[key] = utc(fields[key])
    return Question(**fields), data


def deps_for(values, prepare=None, delay=0, clock=None):
    clock = clock or FakeClock()
    state = RunState(clock)
    deps = Dependencies(TextClient(values, delay), clock, state, {"NUMERIC_V1": "false"}, prepare=prepare)
    return deps


def fixture_prepare(data):
    # This is an injected SDK fixture, never a claim to test SDK interpolation.
    def prepare(question, result):
        result.cdf = data.get("cdf")
        result.prediction = result.value
        return result
    return prepare


def narration(value):
    return "OUTSIDE VIEW: Comparable cases; base rate 20%.\nINSIDE VIEW: One fact pushes upward.\nFINAL\n" + value
