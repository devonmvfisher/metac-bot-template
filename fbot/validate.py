"""Validate the exact outbound values. Never repair a CDF in v0."""
import math
from . import SkipQuestion
from .config import BINARY_CAP, SINGLE_CAP, MC_FLOOR


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def binary(value):
    return finite(value) and 0.001 <= value <= 0.999


def cap_binary(value, runs):
    cap = BINARY_CAP
    reasons = []
    if len(runs) == 1:
        cap = SINGLE_CAP
        reasons.append("single-run")
    elif value < SINGLE_CAP and not all(p < 0.10 for p in runs):
        cap = SINGLE_CAP
        reasons.append("weak-extreme-agreement")
    elif value > 1 - SINGLE_CAP and not all(p > 0.90 for p in runs):
        cap = SINGLE_CAP
        reasons.append("weak-extreme-agreement")
    capped = min(1 - cap, max(cap, value))
    if capped != value:
        reasons.append(f"binary-cap-{cap:g}")
    return capped, reasons


def repair(values, floor=MC_FLOOR):
    """Water-fill active categories, then put rounding remainder on the largest."""
    if len(values) < 2 or len(values) * floor > 1 or any(
            not finite(v) or v < 0 for v in values.values()) or sum(values.values()) <= 0:
        raise SkipQuestion("INVALID_OUTPUT")
    result = {}
    active = dict(values)
    while active:
        mass = 1 - sum(result.values())
        weight = sum(active.values())
        if weight <= 0:
            raise SkipQuestion("INVALID_OUTPUT")
        scaled_values = {k: mass * v / weight for k, v in active.items()}
        below = [k for k, v in scaled_values.items() if v < floor]
        if not below:
            result.update(scaled_values)
            break
        for key in below:
            result[key] = floor
            del active[key]
    result = {k: round(result[k], 6) for k in values}
    largest = max(result, key=result.get)
    result[largest] = round(result[largest] + 1 - sum(result.values()), 6)
    return result


def multiple_choice(question, values):
    if not isinstance(values, dict):
        return False
    live = set(question.options)
    if not live.issubset(values) or set(values) - live - set(question.retired):
        return False
    if any(values.get(name) is not None for name in question.retired):
        return False
    probabilities = [values[k] for k in question.options]
    return (all(finite(v) and MC_FLOOR - 1e-9 <= v <= 0.99 for v in probabilities)
            and abs(sum(probabilities) - 1) <= 1e-6)


def cdf_strict(question, values):
    n = question.inbound_outcome_count
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return False
    if not isinstance(values, (list, tuple)) or len(values) != n + 1:
        return False
    if any(not finite(v) or not 0 <= v <= 1 or abs(v - round(v, 10)) > 1e-12 for v in values):
        return False
    if question.open_lower:
        if values[0] < 0.001 + 1e-6:
            return False
    elif values[0] != 0:
        return False
    if question.open_upper:
        if values[-1] > 0.999 - 1e-6:
            return False
    elif values[-1] != 1:
        return False
    minimum_step = 0.01 / n * 1.05
    maximum_step = 0.2 * 200 / n * 0.95
    return all(minimum_step - 1e-10 <= b - a <= maximum_step + 1e-10
               for a, b in zip(values, values[1:]))


def cdf_api(question, values, tol=1e-9):
    n = question.inbound_outcome_count
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return None
    if not isinstance(values, (list, tuple)) or len(values) != n + 1:
        return None
    if any(not finite(v) for v in values):
        return None
    r = [round(v, 10) for v in values]
    if any(not 0 <= v <= 1 for v in r):
        return None
    if (r[0] < 0.001 - tol if question.open_lower else r[0] != 0.0):
        return None
    if (r[-1] > 0.999 + tol if question.open_upper else r[-1] != 1.0):
        return None
    minimum_step = 0.01 / n
    maximum_step = 0.2 * 200 / n
    if not all(minimum_step - tol <= b - a <= maximum_step + tol for a, b in zip(r, r[1:])):
        return None
    return r


def require(question, value, outbound_cdf=None, numeric_v1=False):
    valid = (binary(value) if question.kind == "binary" else
             multiple_choice(question, value) if question.kind == "multiple_choice" else
             cdf_api(question, outbound_cdf) is not None if question.kind in ("numeric", "discrete") else False)
    if valid and numeric_v1:
        from . import numeric
        valid = numeric.check_strict(question, outbound_cdf)
    if not valid:
        raise SkipQuestion("INVALID_OUTPUT")


def payload(question, result):
    require(question, result.value, result.cdf, result.numeric_v1)
    return {"probability_yes": result.value if question.kind == "binary" else None,
            "probability_yes_per_category": result.value if question.kind == "multiple_choice" else None,
            "continuous_cdf": result.cdf if question.kind in ("numeric", "discrete") else None}
