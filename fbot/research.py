import threading
from . import SkipQuestion
from .config import enabled
from .types import Research


def credentials(env):
    if env.get("ASKNEWS_API_KEY"):
        return {"api_key": env["ASKNEWS_API_KEY"]}
    if env.get("ASKNEWS_CLIENT_ID") and env.get("ASKNEWS_SECRET"):
        return {"client_id": env["ASKNEWS_CLIENT_ID"], "client_secret": env["ASKNEWS_SECRET"]}
    return None


class Service:
    def __init__(self, env, state, fetch=None):
        self.env, self.state, self.fetch = env, state, fetch
        self.cache, self.locks = {}, {}
        self.lock = threading.Lock()
        self.total = self.missing = 0

    def get(self, question, deadline=None):
        with self.lock:
            guard = self.locks.setdefault(question.qid, threading.Lock())
        with guard:
            if question.qid in self.state.posted:
                return Research()
            if question.qid in self.cache:
                value = self.cache[question.qid]
            else:
                value = Research()
                auth = credentials(self.env)
                if auth is not None and self.fetch is not None:
                    for attempt in range(2):
                        timeout = 60 if deadline is None else min(60, deadline - self.state.clock.monotonic())
                        if timeout <= 0:
                            break
                        try:
                            response = self.fetch(question.title, auth, timeout=timeout,
                                                  strategy="latest news", n_articles=6)
                            text, count = response
                            if not isinstance(text, str) or not isinstance(count, int):
                                raise ValueError()
                            value = Research(text[:12000], count, bool(text.strip()))
                            break
                        except Exception:
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
