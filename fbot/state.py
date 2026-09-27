"""Counts-only, thread-safe run state. Never serialize model text or secrets."""
from collections import Counter
import json
import logging
from pathlib import Path
import threading

logger = logging.getLogger("fbot")
ALERTS = frozenset({"CREDITS_EXHAUSTED", "NO_LLM_KEY", "NO_FALL_QUESTIONS",
                   "INSTALL_FAILING", "HEARTBEAT_FAILED", "API_REJECTED", "SEASON_OVER",
                   "CREDITS_LOW", "CREDIT_UNKNOWN", "MODEL_UNAVAILABLE",
                   "RESEARCH_UNAVAILABLE", "SKIPS", "SCHEDULER_GAPS",
                   "GATE_BLOCKED", "RUN_FAILED", "POLL_FAILING", "COMMENT_FAILED"})


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
        self.tiers = Counter()

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
        logger.info("SKIP qid=%s target=%s reason=%s", question.qid, question.target, reason)

    def failure(self, qid, reason):
        weight = {"INVALID_OUTPUT": 2, "ALL_MODELS_FAILED": 1, "MISREAD_ALL": 1}.get(reason, 0)
        with self.lock:
            self.failures[qid] += weight

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
