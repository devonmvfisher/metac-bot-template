import threading
import time
from . import SkipQuestion
from .config import enabled
from .types import Research


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
    def __init__(self, env, state, fetch=None):
        self.env, self.state, self.fetch = env, state, fetch
        self.cache, self.locks = {}, {}
        self.lock = threading.Lock()
        self.total = self.missing = 0
        self.errors = {}
        self.sleep = time.sleep

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
                    for attempt in range(4):
                        timeout = 60 if deadline is None else min(60, deadline - self.state.clock.monotonic())
                        if timeout <= 0:
                            self.errors[question.qid] = "NO_TIME"
                            break
                        try:
                            response = self.fetch(question.title, auth, timeout=timeout,
                                                  strategy="latest news", n_articles=6)
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
                                    self.sleep(5 * (attempt + 1))
                                    continue
                                break
                            if attempt >= 1:
                                break
                            # One logical query; only a failed request gets one retry.
                            continue
                self.cache[question.qid] = value
                with self.lock:
                    self.total += 1
                    self.missing += int(not value.available)
                    if self.missing / self.total > 0.20 or self.total >= 3 and self.missing == self.total:
                        self.state.alert("RESEARCH_UNAVAILABLE")
            if not value.available and enabled(self.env, "SKIP_WITHOUT_RESEARCH"):
                raise SkipQuestion("NO_RESEARCH")
            return value
