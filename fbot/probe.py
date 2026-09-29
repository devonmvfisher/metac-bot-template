"""Bounded once-daily model availability checks; no forecast API calls."""
from dataclasses import dataclass
import logging
from . import CreditExhausted, ModelFailure
from .targets import utc

logger = logging.getLogger("fbot")
SLOT_NAMES = ("OPUS", "SOL", "FLASH", "CHEAP", "BRIDGE", "OWN")
BUDGET_SECONDS = 120
CALL_SECONDS = 45
PROBE_HOUR_UTC = 12
FLAKY = frozenset({0, 408, 429, 500, 502, 503, 504})


def slots_from_config(config):
    slots = {}
    for name in ("OPUS", "SOL", "FLASH", "CHEAP"):
        try:
            models = getattr(config, name, None)
            if isinstance(models, tuple) and models and all(isinstance(model, str) and model for model in models):
                slots[name] = models
        except Exception:
            pass
    return slots


def due(ledger, now):
    try:
        now = utc(now)
        saved = ledger.data.get("last_probe") if ledger is not None else None
        return saved != now.date().isoformat() if saved else now.hour == PROBE_HOUR_UTC
    except Exception:
        return False


def make_call(client, seconds=CALL_SECONDS):
    def call(model_id):
        try:
            client.one(model_id, "Reply OK", probe=True,
                       deadline=client.clock.monotonic() + seconds, timeout=seconds)
            return 200
        except CreditExhausted:
            return 402
        except ModelFailure as error:
            return error.status
        except Exception:
            return 0
    return call


@dataclass
class ProbeReport:
    results: list
    alert_slots: tuple
    flaky_slots: tuple
    credit_exhausted: bool
    probed: int


def run(call, slots, clock, budget_seconds=BUDGET_SECONDS):
    results, statuses, by_slot = [], {}, {}
    exhausted = False
    try:
        start = clock.monotonic()
        for slot, models in slots.items():
            if slot not in SLOT_NAMES or not isinstance(models, (tuple, list)):
                continue
            for model in models:
                if not isinstance(model, str) or not model:
                    continue
                if exhausted:
                    status = None
                elif model in statuses:
                    status = statuses[model]
                elif clock.monotonic() - start >= budget_seconds:
                    status = None
                else:
                    try:
                        status = call(model)
                        if type(status) is not int or not 0 <= status <= 599:
                            status = 0
                    except Exception:
                        status = 0
                    statuses[model] = status
                    if status == 402:
                        exhausted = True
                results.append((slot, model, status))
                if status is not None:
                    by_slot.setdefault(slot, []).append(status)
                logger.info("PROBE_DAILY slot=%s model=%s status=%s", slot, model, "skipped" if status is None else status)
    except Exception:
        pass
    alerts, flaky = [], []
    if not exhausted:
        for slot, values in by_slot.items():
            if any(status in (400, 404) for status in values) or (200 not in values and any(status not in FLAKY for status in values)):
                alerts.append(slot)
            elif 200 not in values and all(status in FLAKY for status in values):
                flaky.append(slot)
    report = ProbeReport(results, tuple(sorted(alerts)), tuple(sorted(flaky)), exhausted, len(statuses))
    logger.info("PROBE_DAILY alert=%s flaky=%s credit_exhausted=%s",
                ",".join(report.alert_slots) or "none", len(report.flaky_slots), str(exhausted).lower())
    return report


def apply(report, state, ledger=None, now=None):
    try:
        for slot in report.alert_slots:
            if slot in SLOT_NAMES:
                state.counts["probe_unavailable_" + slot] = 1
        if report.alert_slots:
            state.alert("MODEL_UNAVAILABLE")
        state.counts["probe_flaky"] = len(report.flaky_slots)
        state.counts["probe_models"] = report.probed
        if ledger is not None and now is not None:
            ledger.record_probe(now)
    except Exception:
        pass


def alert_rows(data):
    try:
        counts = data.get("counts")
        if not isinstance(counts, dict):
            return []
        slots = sorted(slot for slot in SLOT_NAMES
                       if type(counts.get("probe_unavailable_" + slot)) is int
                       and counts["probe_unavailable_" + slot] >= 1)
        return ["slots=" + ",".join(slots)] if slots else []
    except Exception:
        return []
