"""Counts-only, thread-safe run state. Never serialize model text or secrets."""
from collections import Counter
import json
import logging
import math
import re
from pathlib import Path
import threading

logger = logging.getLogger("fbot")
ALERTS = frozenset({"CREDITS_EXHAUSTED", "NO_LLM_KEY", "NO_FALL_QUESTIONS",
                   "INSTALL_FAILING", "HEARTBEAT_FAILED", "API_REJECTED", "SEASON_OVER",
                   "CREDITS_LOW", "CREDIT_UNKNOWN", "MODEL_UNAVAILABLE",
                   "RESEARCH_UNAVAILABLE", "SKIPS", "SCHEDULER_GAPS",
                   "GATE_BLOCKED", "RUN_FAILED", "POLL_FAILING", "COMMENT_FAILED",
                   "PRESET_CONFIG_INVALID", "NUMERIC_FALLBACK", "OWN_KEY_CONFIG_INVALID", "USING_OWN_KEY", "TARGET_MISMATCH", "BUDGET_CAP_INVALID"})
# A question whose model calls failed for transient reasons in this many runs (inside the ledger window)
# sits out until the window passes: real protection against a provider that keeps failing.
TRANSIENT_RUNS = 3


def model_name(value):
    from .config import PROBES, BRIDGE
    return value if isinstance(value, str) and value in PROBES + BRIDGE else 'other'


def usage_number(value):
    try:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def research_error(value):
    if not isinstance(value, str):
        return None
    status = re.search(r':(\d{3})$', value)
    if status:
        return 'http_' + status[1]
    return value if re.fullmatch(r'http_\d{3}|[A-Za-z]{1,40}', value) else None


class RunState:
    def __init__(self, clock):
        self.clock = clock
        self.started = clock.now()
        self.job_started = self.started
        self.lock = threading.RLock()
        self.posted = set()
        self.commented = set()
        self.comment_failed = set()
        self.http_status = {}
        self.failures = Counter()
        self.polls = Counter()
        self.poll_errors = Counter()
        self.inflight = set()
        self.skips = Counter()
        self.counts = Counter()
        self.alerts = set()
        self.credit_before = None
        self.credit_after = None
        self.limit = None
        self.credit_own = None
        self.credit_source = 'unknown'
        self.tiers = Counter()
        self.model_preset = None
        self.preset_reserve_usd = 10.0
        self.preset_tiers = set()
        self.minibench_preset = None
        self.minibench_floor_usd = None
        self.book = None
        self.without_modules = set()
        self.numeric_enabled = None
        self.research2_usd = 0.0
        self.seen = {target:set() for target in ('season','minibench','test')}
        self.skips_by_target = {target:Counter() for target in self.seen}
        self.attempted_by_target = Counter()
        self.tiers_by_target = {target:Counter() for target in self.seen}
        self.model_calls = {}
        self.parser_calls = Counter()
        self.research_calls = {'calls':0,'ok':0,'none':0,'err':Counter()}
        self.loop_min = self.poll_min = None
        self.open = {}
        self.transient_seen = set()

    def mark_seen(self, question):
        with self.lock:
            if question.target in self.seen:
                self.seen[question.target].add(question.qid)

    def record_model(self, model, ok, usage=None):
        with self.lock:
            row = self.model_calls.setdefault(model_name(model), dict(calls=0,ok=0,err=0,in_tok=0,out_tok=0,usd=0))
            row['calls'] += 1
            row['ok' if ok else 'err'] += 1
            if ok and isinstance(usage, dict):
                for source, target in (('prompt_tokens','in_tok'),('completion_tokens','out_tok'),('cost','usd')):
                    value = usage_number(usage.get(source))
                    if value is not None and usage_number(row[target] + value) is not None:
                        row[target] += value

    def record_research(self, ok, error=None):
        with self.lock:
            self.research_calls['calls'] += 1
            self.research_calls['ok'] += int(ok)
            code = research_error(error)
            if code:
                self.research_calls['err'][code] += 1

    def attach_book(self, book):
        self.book = book
        try:
            book.prune(self.clock.now())
            for qid in book.data['failures']:
                self.failures[int(qid)] = book.failure_count(int(qid), self.clock.now())
            for qid in book.data['transient']:
                if book.transient_runs(int(qid), self.clock.now()) >= TRANSIENT_RUNS:  # R3K
                    self.failures[int(qid)] = max(self.failures[int(qid)], 2)
            self.comment_failed.update(book.data['comment_failed'])
        except Exception:
            self.without_modules.add('ledger')

    def remember(self, method, *args):
        if self.book is not None:
            try:
                return getattr(self.book, method)(*args)
            except Exception:
                self.without_modules.add('ledger')

    def alert(self, key):
        if key in ALERTS:
            with self.lock:
                self.alerts.add(key)

    def skip(self, question, reason):
        from . import REASONS
        if reason not in REASONS:
            reason = "INVALID_OUTPUT"
        with self.lock:
            self.skips[reason] += 1
            if question.target in self.skips_by_target:
                self.skips_by_target[question.target][reason] += 1
        logger.info("SKIP qid=%s target=%s reason=%s", question.qid, question.target, reason)
        self.remember('record_question', question.qid, question.target, self.clock.now(), reason)

    def failure(self, qid, reason):
        if reason == "POST_RATE_LIMITED":  # P429A
            reason = "MODEL_TRANSIENT"  # a Metaculus 429 is an outage, not the question: the F09 rule applies
        weight = {"INVALID_OUTPUT": 2, "ALL_MODELS_FAILED": 1, "MISREAD_ALL": 1, "POST_FAILED": 1,
                  "MODEL_TRANSIENT": 1}.get(reason, 0)
        with self.lock:
            self.failures[qid] += weight
            if reason == "MODEL_TRANSIENT":  # R3S
                if qid not in self.transient_seen:  # R3O
                    self.transient_seen.add(qid)
                    self.remember('record_transient', qid, self.clock.now())
            elif weight:
                self.remember('record_failure', qid, weight, self.clock.now())

    def check_polls(self):
        with self.lock:
            calls, errors = sum(self.polls.values()), sum(self.poll_errors.values())
            if calls >= 2 and calls == errors:
                self.alert("POLL_FAILING")

    def snapshot(self):
        with self.lock:
            return {"counts": dict(self.counts), "skips": dict(self.skips),
                    "posted": len(self.posted), "comments": len(self.commented),
                    "credit_before": self.credit_before, "credit_after": self.credit_after,
                    "limit": self.limit, "tiers": dict(self.tiers),
                    "credit_own": self.credit_own,
                    "credit_source": self.credit_source,
                    "seen": {target:len(values) for target,values in self.seen.items()},
                    "skips_by_target": {target:dict(values) for target,values in self.skips_by_target.items()},
                    "attempted_by_target": dict(self.attempted_by_target),
                    "posted_by_target": {target:len(values & self.posted) for target,values in self.seen.items()},
                    "commented_by_target": {target:len(values & self.commented) for target,values in self.seen.items()},
                    "tiers_by_target": {target:dict(values) for target,values in self.tiers_by_target.items()},
                    "model_calls": {name:dict(values) for name,values in self.model_calls.items()},
                    "parser_calls": dict(self.parser_calls),
                    "research_calls": {**self.research_calls,'err':dict(self.research_calls['err'])},
                    "loop_min": self.loop_min, "poll_min": self.poll_min, "open": dict(self.open),
                    "research2_usd": self.research2_usd,
                    "model_preset": self.model_preset or 'auto',
                    "alerts": sorted(self.alerts),
                    "comment_failed": sorted(self.comment_failed),
                    "polls": dict(self.polls), "poll_errors": dict(self.poll_errors),
                    "observed_spend": self.spend(),
                    "spend_per_question": self.spend() / self.counts["attempted"] if self.counts["attempted"] and self.spend() is not None else None}

    def spend(self):
        if self.credit_before is None or self.credit_after is None:
            return None
        return self.credit_before - self.credit_after

    def write(self, path="fbot-run.json"):
        Path(path).write_text(json.dumps(self.snapshot(), sort_keys=True) + "\n",
                              encoding="utf-8", newline="\n")
