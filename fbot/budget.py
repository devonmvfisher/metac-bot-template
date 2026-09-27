"""Credit pacing with locked conservative reservations between key refreshes."""
import threading
import logging
from . import SkipQuestion
from .config import COSTS, FLOOR_CREDIT, enabled
from .targets import utc


def remaining_season(now):
    now = utc(now)
    start, shoulder, end = map(utc, ("2026-09-28T00:00:00Z", "2026-10-12T00:00:00Z",
                                    "2026-12-15T00:00:00Z"))
    if now <= start:
        opened = 0
    elif now <= shoulder:
        opened = 0.035 * (now - start).total_seconds() / (shoulder - start).total_seconds()
    elif now < end:
        opened = 0.035 + 0.965 * (now - shoulder).total_seconds() / (end - shoulder).total_seconds()
    else:
        opened = 1
    return 350 * (1 - opened)


_mode_warned = False
_mode_lock = threading.Lock()


def normalize_mode(mode):
    global _mode_warned
    mode = (mode or "always").strip().lower()
    if mode not in ("always", "slack", "off"):
        with _mode_lock:
            if not _mode_warned:
                logging.getLogger("fbot").warning("CONFIG MINIBENCH_MODE invalid; using always")
                _mode_warned = True
        mode = "always"
    return mode


def choose(now, remaining, target, mode="always"):
    mode = normalize_mode(mode)
    season = remaining_season(now)
    if remaining is None:
        if target == "minibench" and mode != "always":
            raise SkipQuestion("BUDGET_MINIBENCH")
        return "C"
    spendable = remaining - FLOOR_CREDIT
    if spendable < COSTS["C"] - 1e-9:
        raise SkipQuestion("EXHAUSTED")
    allowed = mode == "always" or (mode == "slack" and spendable >= COSTS["B"] * season + COSTS["C"] * 60)
    if target == "minibench":
        if not allowed:
            raise SkipQuestion("BUDGET_MINIBENCH")
        return "C"
    allowance = (spendable - (COSTS["C"] * 60 if allowed else 0)) / max(season, 1)
    return next((tier for tier in ("A", "B", "C") if COSTS[tier] <= allowance), "C")


class Pacer:
    def __init__(self, env, client, state, clock):
        self.env, self.client, self.state, self.clock = env, client, state, clock
        self.lock = threading.RLock()
        self.remaining = None
        self.reserved = 0
        self.refreshed = False

    def refresh(self):
        from . import CreditExhausted
        with self.lock:
            try:
                remaining, limit = self.client.key()
            except CreditExhausted:
                remaining, limit = 0, None
            self.remaining = remaining
            self.reserved = 0
            if not self.refreshed:
                self.state.credit_before = remaining
            self.refreshed = True
            self.state.credit_after, self.state.limit = remaining, limit
            if self.env.get("OPENROUTER_API_KEY"):
                if remaining is None:
                    self.state.alert("CREDIT_UNKNOWN")
                elif remaining < 10 or (limit is not None and remaining < 0.25 * limit):
                    self.state.alert("CREDITS_LOW")
            return remaining, limit

    def tier(self, question, forced_c=False):
        with self.lock:
            router = bool(self.env.get("OPENROUTER_API_KEY")) and "router" not in self.client.exhausted
            if not router:
                bridge = enabled(self.env, "USE_OPENAI_BRIDGE") and self.env.get("OPENAI_API_KEY")
                if bridge and "bridge" not in self.client.exhausted:
                    if question.target == "minibench":
                        raise SkipQuestion("BUDGET_MINIBENCH")
                    return "BRIDGE"
                exhausted = bool(self.client.exhausted)
                self.state.alert("CREDITS_EXHAUSTED" if exhausted else "NO_LLM_KEY")
                raise SkipQuestion("EXHAUSTED" if exhausted else "NO_LLM_KEY")
            credit = None if self.remaining is None else self.remaining - self.reserved
            try:
                tier = choose(self.clock.now(), credit, question.target,
                              self.env.get("MINIBENCH_MODE") or "always")
            except SkipQuestion as skip:
                if skip.reason == "EXHAUSTED":
                    self.state.alert("CREDITS_EXHAUSTED")
                raise
            if forced_c:
                tier = "C"
            self.reserved += COSTS[tier]
            self.state.tiers[tier] += 1
            return tier
