"""Sanitised counts-only memory with locked records and atomic persistence."""
from datetime import date, datetime, timezone
from functools import wraps
import json
import logging
import math
import os
import re
import threading
import fbot
from .targets import utc

logger = logging.getLogger("fbot")
VERSION = 1
LEDGER_PATH = "fbot-ledger.json"
KEYS = frozenset({"version", "updated", "spend", "posted", "failures", "transient", "comment_failed", "seen", "free_tokens", "last_probe", "asknews"})
from .config import TIERS as FORECAST_TIERS
TIERS = tuple(FORECAST_TIERS) + ('RESEARCH2',)
TARGETS = ("season", "minibench", "test")
FAILURE_DAYS = 2
TRANSIENT_HOURS = 3
SPEND_KEEP_DAYS = 8
SEEN_KEEP_DAYS = 14
COMMENT_FAILED_KEEP = 200
FAILURE_CAP = 9


def _schema():
    return {"version": VERSION, "updated": None, "spend": {}, "posted": {}, "failures": {}, "transient": {},
            "comment_failed": [], "seen": {}, "free_tokens": {}, "last_probe": None, "asknews": {}}


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _money(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and math.isfinite(value)
    except Exception:
        return False


def _date(value):
    try:
        return isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None and date.fromisoformat(value).isoformat() == value
    except Exception:
        return False


def _qid(value):
    try:
        return isinstance(value, str) and int(value) > 0 and str(int(value)) == value
    except Exception:
        return False


def _failure_time(value):
    try:
        return isinstance(value, str) and len(value) <= 32 and utc(value) is not None
    except Exception:
        return False


def _stamp(now):
    return utc(now).isoformat(timespec="seconds").replace("+00:00", "Z")


def _today(now):
    return utc(now).date().isoformat()


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _entry(value, length):
    return isinstance(value, (list, tuple)) and len(value) == length


def _reason(value):
    try:
        return value is None or value in fbot.REASONS
    except Exception:
        return False


def sanitize(raw):
    clean = _schema()
    raw = _mapping(raw)
    try:
        if isinstance(raw.get("updated"), str):
            try:
                clean["updated"] = _stamp(raw["updated"])
            except Exception:
                pass
        for day, tiers in _mapping(raw.get("spend")).items():
            if not _date(day):
                continue
            keep = {tier: [entry[0], float(entry[1])] for tier, entry in _mapping(tiers).items()
                    if tier in TIERS and _entry(entry, 2) and _integer(entry[0]) and _money(entry[1])}
            if keep:
                clean["spend"][day] = keep
        clean["posted"] = {target: count for target, count in _mapping(raw.get("posted")).items()
                           if target in TARGETS and _integer(count)}
        clean["failures"] = {qid: list(entry) for qid, entry in _mapping(raw.get("failures")).items()
                             if _qid(qid) and _entry(entry, 2) and _integer(entry[0], 1)
                             and entry[0] <= FAILURE_CAP and _failure_time(entry[1])}
        clean["transient"] = {qid: list(entry) for qid, entry in _mapping(raw.get("transient")).items()
                              if _qid(qid) and _entry(entry, 2) and _integer(entry[0], 1)
                              and entry[0] <= FAILURE_CAP and _failure_time(entry[1])}
        comments = raw.get("comment_failed", [])
        if isinstance(comments, (list, tuple)):
            clean["comment_failed"] = list(dict.fromkeys(qid for qid in comments if _integer(qid, 1)))
        clean["seen"] = {qid: list(entry) for qid, entry in _mapping(raw.get("seen")).items()
                         if _qid(qid) and _entry(entry, 3) and entry[0] in TARGETS
                         and _date(entry[1]) and _reason(entry[2])}
        clean["free_tokens"] = {day: count for day, count in _mapping(raw.get("free_tokens")).items()
                               if _date(day) and _integer(count)}
        if _date(raw.get("last_probe")):
            clean["last_probe"] = raw["last_probe"]
        clean["asknews"] = {month: value for month, value in _mapping(raw.get("asknews")).items()
                            if isinstance(month, str) and re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month) and _integer(value)}
    except Exception:
        pass
    return clean


def _locked(default=False):
    def decorate(method):
        @wraps(method)
        def guarded(self, *args, **kwargs):
            with self._lock:
                try:
                    return method(self, *args, **kwargs)
                except Exception:
                    return default() if callable(default) else default
        return guarded
    return decorate


class Ledger:
    def __init__(self, data=None, available=False):
        self._lock = threading.RLock()
        with self._lock:
            self.data = sanitize(data)
            self.available = bool(available)

    @_locked()
    def record_question(self, qid, target, now, reason=None):
        if not _integer(qid, 1) or target not in TARGETS or not _reason(reason):
            return False
        entry = [target, _today(now), reason]
        self.data["seen"][str(qid)] = entry
        return True

    @_locked()
    def record_spend(self, tier, usd, now):
        if tier not in TIERS or not _money(usd):
            return False
        today = _today(now)
        count, spent = self.data["spend"].get(today, {}).get(tier, [0, 0.0])
        total = spent + usd
        if not _money(total):
            return False
        self.data["spend"].setdefault(today, {})[tier] = [count + 1, float(total)]
        return True

    @_locked()
    def record_posted(self, target):
        if target not in TARGETS:
            return False
        self.data["posted"][target] = self.data["posted"].get(target, 0) + 1
        return True

    @_locked()
    def record_failure(self, qid, weight, now):
        if not _integer(qid, 1) or not _integer(weight, 1):
            return False
        today = _today(now)
        count = self.failure_count(qid, now)
        self.data["failures"][str(qid)] = [min(FAILURE_CAP, count + weight), _stamp(now)]
        return True

    @_locked(0)
    def failure_count(self, qid, now):
        if not _integer(qid, 1):
            return 0
        entry = self.data["failures"].get(str(qid))
        if entry is None or (utc(now) - utc(entry[1])).total_seconds() >= FAILURE_DAYS * 86400:
            return 0
        return entry[0]

    @_locked()
    def record_transient(self, qid, now):
        if not _integer(qid, 1):
            return False
        count = self.transient_runs(qid, now)
        self.data["transient"][str(qid)] = [min(FAILURE_CAP, count + 1), _stamp(now)]
        return True

    @_locked(0)
    def transient_runs(self, qid, now):
        if not _integer(qid, 1):
            return 0
        entry = self.data["transient"].get(str(qid))
        if entry is None or (utc(now) - utc(entry[1])).total_seconds() >= TRANSIENT_HOURS * 3600:  # R3W
            return 0
        return entry[0]

    @_locked()
    def asknews_charge(self, now, archive=False):
        month = _today(now)[:7]
        used = self.data["asknews"].get(month, 0)
        charge = 5 if archive else 1
        if archive and used + charge > 900:
            return False
        self.data["asknews"][month] = used + charge
        return True

    @_locked()
    def clear_comment_failed(self, qid):
        self.data["comment_failed"] = [item for item in self.data["comment_failed"] if item != qid]
        return True

    @_locked()
    def record_comment_failed(self, qid):
        if not _integer(qid, 1):
            return False
        if qid not in self.data["comment_failed"]:
            self.data["comment_failed"].append(qid)
        return True

    @_locked()
    def record_probe(self, now):
        today = _today(now)
        self.data["last_probe"] = today
        return True

    @_locked()
    def add_free_tokens(self, now, count):
        if not _integer(count):
            return False
        today = _today(now)
        self.data["free_tokens"][today] = self.data["free_tokens"].get(today, 0) + count
        return True

    @_locked(0)
    def free_tokens(self, now):
        return self.data["free_tokens"].get(_today(now), 0)

    @_locked(None)
    def seen(self, qid):
        if not _integer(qid, 1):
            return None
        entry = self.data["seen"].get(str(qid))
        return tuple(entry) if entry is not None else None

    @_locked(dict)
    def spend_7d(self, now):
        today = utc(now).date()
        totals = {}
        for day, tiers in self.data["spend"].items():
            if 0 <= (today - date.fromisoformat(day)).days <= 6:
                for tier, (count, usd) in tiers.items():
                    old_count, old_usd = totals.get(tier, (0, 0.0))
                    if count:
                        totals[tier] = old_count + count, old_usd + usd
        return totals

    @_locked(None)
    def measured_cost(self, tier, now, min_questions=20):
        if tier not in TIERS or not _integer(min_questions, 1):
            return None
        count, usd = self.spend_7d(now).get(tier, (0, 0.0))
        return usd / count if count >= min_questions else None

    @_locked(None)
    def pacer_cost(self, tier, config_cost, now, min_questions=20):
        if not _money(config_cost):
            return None
        measured = self.measured_cost(tier, now, min_questions)
        return config_cost if measured is None else max(config_cost / 2, 1.2 * measured)

    @_locked()
    def prune(self, now):
        today = utc(now).date()
        spend = {day: tiers for day, tiers in self.data["spend"].items()
                 if (today - date.fromisoformat(day)).days <= SPEND_KEEP_DAYS}
        seen = {qid: entry for qid, entry in self.data["seen"].items()
                if (today - date.fromisoformat(entry[1])).days <= SEEN_KEEP_DAYS}
        failures = {qid: entry for qid, entry in self.data["failures"].items()
                    if (utc(now) - utc(entry[1])).total_seconds() < FAILURE_DAYS * 86400}
        transient = {qid: entry for qid, entry in self.data["transient"].items()
                     if (utc(now) - utc(entry[1])).total_seconds() < TRANSIENT_HOURS * 3600}
        free = {day: count for day, count in self.data["free_tokens"].items()
                if (today - date.fromisoformat(day)).days <= SPEND_KEEP_DAYS}
        self.data.update(spend=spend, seen=seen, failures=failures, transient=transient, free_tokens=free,
                         comment_failed=self.data["comment_failed"][-COMMENT_FAILED_KEEP:])
        return True


def empty():
    return Ledger(available=False)


def load(path=LEDGER_PATH):
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != VERSION:
            raise ValueError("invalid version")
        book = Ledger(raw, available=True)
        logger.info("LEDGER status=loaded")
        return book
    except FileNotFoundError:
        logger.info("LEDGER status=missing")
    except Exception:
        logger.info("LEDGER status=corrupt")
    return empty()


def save(ledger, path=LEDGER_PATH, now=None):
    try:
        with ledger._lock:
            stamp = _stamp(datetime.now(timezone.utc) if now is None else now)
            ledger.data = sanitize(ledger.data)
            ledger.data["updated"] = stamp
            text = json.dumps(ledger.data, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n"
            temporary = os.fspath(path) + ".tmp"
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(temporary, path)
        return True
    except Exception:
        logger.info("LEDGER status=save_failed")
        return False
