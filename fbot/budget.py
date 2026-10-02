"""Credit pacing with locked conservative reservations between key refreshes."""
import threading
import logging
import math
from . import SkipQuestion
from .config import COSTS, TIERS, FLOOR_CREDIT, enabled
from . import keys
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
    if mode not in ("always", "slack", "off", "skip"):
        with _mode_lock:
            if not _mode_warned:
                logging.getLogger("fbot").warning("CONFIG MINIBENCH_MODE invalid; using always")
                _mode_warned = True
        mode = "always"
    return mode


def choose(now, remaining, target, mode="always", costs=None):
    costs = COSTS if costs is None else costs
    mode = normalize_mode(mode)
    if target == "minibench" and mode == "skip":
        raise SkipQuestion("BUDGET_MINIBENCH")
    season = remaining_season(now)
    if remaining is None:
        if target == "minibench" and mode != "always":
            raise SkipQuestion("BUDGET_MINIBENCH")
        return "C"
    spendable = remaining - FLOOR_CREDIT
    if spendable < costs["C"] - 1e-9:
        if spendable >= costs["M"] - 1e-9:
            return "M"
        raise SkipQuestion("EXHAUSTED")
    allowed = mode == "always" or (mode == "slack" and spendable >= costs["B"] * season + costs["C"] * 60)
    if target == "minibench":
        if not allowed:
            return "M"
        return "C"
    allowance = (spendable - (costs["C"] * 60 if allowed else 0)) / max(season, 1)
    return next((tier for tier, spec in TIERS.items() if not spec.get("fast") and not spec.get("bridge") and costs[tier] <= allowance), "C")


def configure_preset(env, state):
    """Read repository settings once; never echo untrusted configuration text."""
    with state.lock:
        if state.model_preset is not None:
            return
        preset = (env.get("MODEL_PRESET") or "auto").strip().upper() or "AUTO"
        if preset not in ("AUTO", "A", "B", "C"):
            preset = "AUTO"
            state.counts["preset_invalid"] += 1
            state.alert("PRESET_CONFIG_INVALID")
        state.model_preset = "auto" if preset == "AUTO" else preset
        try:
            reserve = float((env.get("PRESET_RESERVE_USD") or "10").strip() or "10")
            if not math.isfinite(reserve) or reserve < 0:
                raise ValueError()
        except (TypeError, ValueError):
            reserve = 10.0
            state.counts["preset_reserve_invalid"] += 1
            state.alert("PRESET_CONFIG_INVALID")
        state.preset_reserve_usd = reserve
        # MiniBench dial: blank values keep the auto MiniBench rule (and MINIBENCH_MODE) unchanged.
        mini = (env.get("MINIBENCH_PRESET") or "auto").strip().upper() or "AUTO"
        if mini not in ("AUTO", "A", "B", "C"):
            mini = "AUTO"
            state.counts["minibench_preset_invalid"] += 1
            logging.getLogger("fbot").warning("CONFIG MINIBENCH_PRESET invalid; using auto")
            state.alert("PRESET_CONFIG_INVALID")
        state.minibench_preset = None if mini == "AUTO" else mini
        raw = (env.get("MINIBENCH_FLOOR_USD") or "").strip()
        floor = None
        if raw:
            try:
                floor = float(raw)
                if not math.isfinite(floor) or floor < 0:
                    raise ValueError()
            except (TypeError, ValueError):
                floor = None
                state.counts["minibench_floor_invalid"] += 1
                logging.getLogger("fbot").warning("CONFIG MINIBENCH_FLOOR_USD invalid; using no floor")
                state.alert("PRESET_CONFIG_INVALID")
        state.minibench_floor_usd = floor
        raw_mode = (env.get("MINIBENCH_MODE") or "").strip().lower() or "always"
        if normalize_mode(raw_mode) != raw_mode:  # R2F1: a mistyped brake must not run silently as always
            state.counts["minibench_mode_invalid"] += 1
            state.alert("PRESET_CONFIG_INVALID")  # R2F1


def log_preset(state):
    """One end-of-run line; list season tiers selected, or none if absent."""
    with state.lock:
        tiers = ",".join(sorted(state.preset_tiers)) or "none"
        logging.getLogger("fbot").info("PRESET %s tier=%s", state.model_preset or "auto", tiers)


class Pacer:
    def __init__(self, env, client, state, clock):
        self.env, self.client, self.state, self.clock = env, client, state, clock
        self.lock = threading.RLock()
        self.remaining = None
        self.reserved = 0
        self.refreshed = False
        self.balances = {}
        self.pending = {}
        self.reservations = {}
        self.allocations = {}
        self.own_mode = getattr(client, "own_mode", "off")
        configure_preset(env, state)

    def costs(self):
        book = self.state.book
        if book is None or not book.available:
            return COSTS.copy()
        try:
            return {tier: book.pacer_cost(tier, cost, self.clock.now()) or cost for tier, cost in COSTS.items()}
        except Exception:
            self.state.without_modules.add("ledger")
            return COSTS.copy()

    def refresh(self):
        from . import CreditExhausted
        with self.lock:
            try:
                remaining, limit = self.client.key()
            except CreditExhausted:
                remaining, limit = 0, None
            self.remaining = remaining
            self.state.credit_source = getattr(self.client, 'credit_source', 'unknown')
            self.balances["router"] = remaining
            if self.own_mode != "off" and self.env.get("OPENROUTER_API_KEY_OWN"):
                try:
                    self.balances["own"] = self.client.key("own")[0]
                except CreditExhausted:
                    self.balances["own"] = 0
                self.state.credit_own = self.balances["own"]
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

    def tier(self, question, forced_c=False, fast=False):
        with self.lock:
            costs = self.costs()
            self.client.credit_costs = costs
            choices = keys.order(self.env, self.own_mode, self.client.exhausted, question.target, self.balances, costs["M"], costs['C'])
            router = bool(choices)
            if not router:
                bridge = enabled(self.env, "USE_OPENAI_BRIDGE") and self.env.get("OPENAI_API_KEY")
                if bridge and "bridge" not in self.client.exhausted:
                    if question.target == "minibench":
                        raise SkipQuestion("BUDGET_MINIBENCH")
                    if question.target == "season":
                        with self.state.lock:
                            self.state.preset_tiers.add("BRIDGE")
                    return "BRIDGE"
                exhausted = bool(self.client.exhausted) or bool(keys.enabled_keys(self.env, self.own_mode))
                if exhausted:
                    self.exhaustion_alert(costs)
                else:
                    self.state.alert("NO_LLM_KEY")
                raise SkipQuestion("EXHAUSTED" if exhausted else "NO_LLM_KEY")
            eligible = keys.order(self.env, self.own_mode, self.client.exhausted, question.target, self.balances, costs["C"], costs['C'])
            available = {name: None if value is None else value - self.reservations.get(name, 0) for name, value in self.balances.items()}
            balances = [available.get(provider) for provider in (eligible or choices)]
            # Each key retains its own floor when the pacer combines credit.
            credit = None if any(value is None for value in balances) else sum(max(0, value - FLOOR_CREDIT) for value in balances) + FLOOR_CREDIT
            mini_floor = getattr(self.state, "minibench_floor_usd", None)
            if question.target == "minibench" and mini_floor is not None and (credit is None or credit < mini_floor):  # MBD1
                raise SkipQuestion("BUDGET_MINIBENCH")
            try:
                tier = choose(self.clock.now(), credit, question.target,
                              self.env.get("MINIBENCH_MODE") or "always", costs)
            except SkipQuestion as skip:
                if skip.reason == "EXHAUSTED":
                    self.exhaustion_alert(costs)
                raise
            if forced_c:
                tier = "C"
            if not eligible and not forced_c:
                tier = "M"
            preset = self.state.model_preset
            if (question.target == "season" and preset in ("A", "B", "C") and credit is not None
                    and credit - FLOOR_CREDIT >= self.state.preset_reserve_usd + costs[preset]):
                tier = preset
            mini = getattr(self.state, "minibench_preset", None)
            if (question.target == "minibench" and mini in ("A", "B", "C") and eligible and credit is not None
                    and credit >= FLOOR_CREDIT + self.state.preset_reserve_usd + costs[mini]):  # MBD2
                tier = mini
            if question.target == "test" and preset in ("A", "B", "C"):
                tier = preset
            if fast:
                tier = "M"
            self.reserve(question.qid, costs[tier], choices if tier == 'M' else eligible or choices)
            self.state.tiers[tier] += 1
            self.state.tiers_by_target[question.target][tier] += 1
            if question.target == "season":
                with self.state.lock:
                    self.state.preset_tiers.add(tier)
            return tier

    def exhaustion_alert(self, costs):
        enabled_keys = keys.enabled_keys(self.env, self.own_mode)
        spent = {name for name in enabled_keys if name in self.client.exhausted or
                 (self.balances.get(name) is not None and self.balances[name] - FLOOR_CREDIT - self.reserved < costs["M"] - 1e-9)}
        if enabled_keys and enabled_keys <= spent:
            self.state.alert("CREDITS_EXHAUSTED")

    def measure_start(self):
        self.refresh()
        return dict(self.balances)

    def release(self, qid):
        with self.lock:
            self.reserved = max(0, self.reserved - self.pending.pop(qid, 0))
            for provider, amount in self.allocations.pop(qid, {}).items():
                self.reservations[provider] = max(0, self.reservations.get(provider, 0) - amount)

    def reserve(self, qid, amount, providers):
        self.reserved += amount
        self.pending[qid] = self.pending.get(qid, 0) + amount
        allocations = self.allocations.setdefault(qid, {})
        for index, provider in enumerate(providers):
            balance = self.balances.get(provider)
            capacity = amount if balance is None or index == len(providers) - 1 else max(0, balance - FLOOR_CREDIT - self.reservations.get(provider, 0))
            part = min(amount, capacity)
            allocations[provider] = allocations.get(provider, 0) + part
            self.reservations[provider] = self.reservations.get(provider, 0) + part
            amount -= part

    def reserve_web(self, question, urgent=False):
        if question.target != 'season' or urgent:
            return False
        with self.lock:
            amount = self.state.remember('measured_cost', 'RESEARCH2', self.clock.now())
            amount = 0.05 if amount is None else amount
            remaining = self.balances.get('router')
            if ('router' in self.client.exhausted or remaining is None
                    or remaining - self.reserved - FLOOR_CREDIT < self.state.preset_reserve_usd + amount):
                return False
            self.reserve(question.qid, amount, ['router'])
            return True

    def measure_end(self, question, tier, before):
        with self.lock:
            self.refresh()
            deltas = [max(0, value - self.balances[name]) for name, value in before.items()
                      if value is not None and self.balances.get(name) is not None]
            if deltas and len(deltas) == len(before):
                spend = sum(deltas)
                self.state.remember("record_spend", tier, spend, self.clock.now())
                logging.getLogger("fbot").info("SPEND tier=%s per_q=%.6f", tier, spend)
            self.release(question.qid)
