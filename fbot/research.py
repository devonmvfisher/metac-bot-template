import threading
import time
from contextlib import contextmanager
import math
from . import SkipQuestion
from .config import enabled
from .types import Research
from . import schedule
import logging


def credentials(env):
    if env.get("ASKNEWS_API_KEY"):
        return {"api_key": env["ASKNEWS_API_KEY"]}
    if env.get("ASKNEWS_CLIENT_ID") and env.get("ASKNEWS_SECRET"):
        return {"client_id": env["ASKNEWS_CLIENT_ID"], "client_secret": env["ASKNEWS_SECRET"]}
    return None


def error_code(exc):
    """Exception class name plus HTTP status when one exists; never provider text."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = "".join(ch for ch in type(exc).__name__ if ch.isalnum() or ch == "_")[:40] or "Error"
    return f"{name}:{status}" if isinstance(status, int) else name


class Service:
    def __init__(self, env, state, fetch=None, limiter=None, tally=None):
        self.env, self.state, self.fetch = env, state, fetch
        self.cache, self.locks = {}, {}
        self.lock = threading.Lock()
        self.total = self.missing = 0
        self.errors = {}
        self.sleep = time.sleep
        self.limiter = limiter or AskNewsLimiter(state.clock)
        from .webread import Tally
        self.tally = tally or Tally()
        self.parity = schedule.switch(env, "ASKNEWS_PARITY", default=False)
        self.archive = schedule.switch(env, "ASKNEWS_ARCHIVE", default=False)
        self.archive_warned = False

    def get(self, question, deadline=None):
        with self.lock:
            guard = self.locks.setdefault(question.qid, threading.Lock())
        with guard:
            if question.qid in self.state.posted:
                self.errors[question.qid] = "POSTED"
                return Research()
            if question.qid in self.cache:
                value = self.cache[question.qid]
            else:
                value = Research()
                auth = credentials(self.env)
                if auth is None:
                    self.errors[question.qid] = "NO_KEY"
                elif self.fetch is None:
                    self.errors[question.qid] = "NO_FETCH"
                if auth is not None and self.fetch is not None:
                    value, asked = self._search(question, auth, deadline)
                    book = self.state.book
                    loaded = book is not None and book.available
                    if self.parity and self.archive and loaded and question.target == "season":
                        older, _ = self._search(question, auth, deadline, archive=True)
                        if older.available:
                            text = "\n\n".join(part for part in (value.text, older.text) if part)
                            value = Research(text[:12000], value.articles + older.articles, True)
                    elif self.parity and question.target == "season":
                        with self.lock:
                            if not self.archive_warned:
                                logging.getLogger("fbot").info("CONFIG ASKNEWS_ARCHIVE inactive; requires switch and loaded ledger")
                                self.archive_warned = True
                    if asked:
                        self.tally.record('asknews', value.available)
                        self.tally.raise_alerts(self.state)
                self.cache[question.qid] = value
                with self.lock:
                    self.total += 1
                    self.missing += int(not value.available)
            return value


    def _search(self, question, auth, deadline, archive=False):
        value = Research()
        asked = False
        for attempt in range(4):
            timeout = 60 if deadline is None else min(60, deadline - self.state.clock.monotonic())
            if timeout <= 0:
                self.errors[question.qid] = "NO_TIME"
                break
            try:
                with self.limiter.slot(deadline):
                    timeout = 60 if deadline is None else min(60, deadline - self.state.clock.monotonic())
                    if timeout <= 0:
                        raise TimeoutError()
                    if archive:
                        if not self.state.remember("asknews_charge", self.state.clock.now(), True):
                            break
                    else:
                        self.state.remember("asknews_charge", self.state.clock.now())
                    asked = True
                    kwargs = {"parity": True, "now": self.state.clock.now()} if self.parity else {}
                    response = self.fetch(question.title, auth, timeout=timeout,
                                          strategy="news knowledge" if archive else "latest news",
                                          n_articles=10 if archive else 6, **kwargs)
                text, count = response
                if not isinstance(text, str) or not isinstance(count, int):
                    raise ValueError()
                value = Research(text[:12000], count, bool(text.strip()))
                self.errors[question.qid] = "OK" if value.available else "EMPTY"
                break
            except Exception as exc:
                self.errors[question.qid] = error_code(exc)
                # A rate limit (HTTP 429) waits and tries again, up to 3 retries; any other
                # failure keeps the original single retry.
                if self.errors[question.qid].endswith(":429"):
                    if attempt < 3:
                        delay = 5 * (attempt + 1)
                        retry_after = getattr(exc, 'retry_after', None)
                        if isinstance(retry_after, (int, float)) and math.isfinite(retry_after):
                            delay = max(delay, retry_after)
                        remaining = delay if deadline is None else max(0, deadline - self.state.clock.monotonic())
                        self.sleep(min(delay, remaining))
                        if remaining <= delay and deadline is not None:
                            break
                        continue
                    break
                if attempt >= 1:
                    break
                # One logical query; only a failed request gets one retry.
                continue
        return value, asked


class AskNewsLimiter:
    """One instance shared by every question in the process."""
    def __init__(self, clock):
        self.clock = clock
        self.semaphore = threading.BoundedSemaphore(2)
        self.start_lock = threading.Lock()
        self.last_start = None

    @contextmanager
    def slot(self, deadline=None):
        remaining = None if deadline is None else max(0, deadline - self.clock.monotonic())
        if not self.semaphore.acquire(timeout=remaining):
            raise TimeoutError()
        try:
            with self.start_lock:
                delay = 0 if self.last_start is None else max(0, self.last_start + 2 - self.clock.monotonic())
                if deadline is not None and self.clock.monotonic() + delay >= deadline:
                    raise TimeoutError()
                if delay:
                    self.clock.sleep(delay)
                self.last_start = self.clock.monotonic()
            yield
        finally:
            self.semaphore.release()
