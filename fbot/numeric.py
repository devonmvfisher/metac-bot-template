"""Pure percentile parsing, monotone interpolation, and strict CDF assembly."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
import re

from .guards import nominal, scaled
from .parse import number

LEVELS = (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99)
LEGACY = (10, 20, 40, 60, 80, 90)
METHOD = "cdf median, pchip 13"
MIX_MIN = 0.01
MIX_MAX = 0.05
TAIL_K = 4 / 3
OPEN_TAIL_MIN = 0.002
TAIL_TOTAL_MAX = 0.7
FLOOR_FACTOR = 1.1
CAP_FACTOR = 0.9
STRICT_MIN = 1.05
STRICT_MAX = 0.95
REASONS = frozenset({"parse", "input", "order", "domain", "scale", "units", "degenerate", "cdf"})
LINE = (r"\s*(?:[-+\N{BULLET}]\s*)?(?:percentile|p)\s*(\d{1,2}(?:\.\d+)?)"
        r"\s*(?:st|nd|rd|th)?\s*(?:percentile)?\s*(?:\([^)]*\))?\s*[:=]\s*(.+?)\s*")


class NumericError(ValueError):
    def __init__(self, reason):
        if not isinstance(reason, str) or reason not in REASONS:
            raise ValueError("unknown numeric reason")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Built:
    cdf: tuple
    notes: tuple = ()


def _switch(env):
    value = env.get("NUMERIC_V1")
    return "" if value is None else str(value).strip().lower()


def enabled(env):
    return _switch(env) not in ("false", "0", "off", "no")


def config_line(env):
    if _switch(env) in ("", "true", "1", "yes", "on", "false", "0", "off", "no"):
        return None
    return "CONFIG NUMERIC_V1 invalid; using true"


def is_percent_unit(unit):
    unit = (unit or "").strip().lower()
    return unit in ("%", "pp") or "%" in unit or "percent" in unit


def is_fraction_scale(question):
    return (question.zero_point is None and question.lower >= 0 and question.upper <= 1
            and not is_percent_unit(question.unit))


def _lines(text):
    # Collapse whitespace runs and skip very long lines: the LINE pattern backtracks
    # polynomially on long whitespace runs, and re holds the GIL while it runs.
    lines = (" ".join(line.split()) for line in text.splitlines())
    return [line for line in lines if len(line) <= 300]


def parse_percentiles(question, text):
    if not isinstance(text, str):
        raise NumericError("parse")
    text = text.translate(str.maketrans("", "", "*_#`"))
    parts = re.split(r"(?im)^[ \t]*FINAL\b[ \t]*:?", text)
    candidates = [part for part in parts
                  if any(re.fullmatch(LINE, line, re.I) for line in _lines(part))]
    chosen = candidates[-1] if candidates else parts[-1]
    result = {}
    unit = (question.unit or "").strip()
    for line in _lines(chosen):
        match = re.fullmatch(LINE, line, re.I)
        if not match:
            continue
        level = float(match[1])
        if level not in LEVELS:
            continue
        value_text = match[2].strip()
        if unit and len(value_text) > len(unit) and value_text.lower().endswith(unit.lower()):
            value_text = value_text[:-len(unit)].strip()
        if value_text.endswith("%"):
            if not is_percent_unit(question.unit):
                continue
            value_text = value_text[:-1].strip()
        try:
            value = number(value_text, question.unit or "")
        except ValueError:
            continue
        if level in result:
            raise NumericError("parse")
        result[level] = value
    if set(result) != set(LEVELS):
        raise NumericError("parse")
    return {level: result[level] for level in LEVELS}


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _in_range(question, value, low, high):
    try:
        return low <= scaled(question, value) <= high
    except (ValueError, ZeroDivisionError, OverflowError, TypeError):
        return False


def check_values(question, values):
    if not isinstance(values, dict) or set(values) != set(LEVELS):
        raise NumericError("input")
    ordered = [values[level] for level in LEVELS]
    if not all(_finite(value) for value in ordered):
        raise NumericError("input")
    if any(b < a for a, b in zip(ordered, ordered[1:])):
        raise NumericError("order")
    try:
        locations = [scaled(question, value) for value in ordered]
        if not all(math.isfinite(value) for value in locations):
            raise ValueError("nonfinite location")
    except (ValueError, ZeroDivisionError, OverflowError, TypeError) as error:
        raise NumericError("domain") from error
    v50, t50 = values[50], locations[LEVELS.index(50)]
    wrong_fraction = (is_fraction_scale(question) and v50 > 1
                      and _in_range(question, v50 / 100, 0, 1))
    wrong_percent = (is_percent_unit(question.unit) and question.upper > 1
                     and max(ordered) <= 1 and t50 < 0.01
                     and _in_range(question, v50 * 100, 0.02, 0.98))
    if wrong_fraction or wrong_percent:
        raise NumericError("scale")
    if not -2 <= t50 <= 3:
        raise NumericError("units")
    if question.kind != "discrete" and ordered[0] == ordered[-1]:
        raise NumericError("degenerate")
    return locations


def _edge(h0, h1, m0, m1):
    slope = ((2 * h0 + h1) * m0 - h0 * m1) / (h0 + h1)
    if (slope > 0) - (slope < 0) != (m0 > 0) - (m0 < 0):
        return 0.0
    if (m0 > 0) - (m0 < 0) != (m1 > 0) - (m1 < 0) and abs(slope) > 3 * abs(m0):
        return 3 * m0
    return slope


def pchip(xs, ys, points):
    """Shape-preserving Hermite interpolation with constant exterior values."""
    if (not isinstance(xs, (list, tuple)) or not isinstance(ys, (list, tuple))
            or len(xs) != len(ys) or len(xs) < 2
            or not all(_finite(v) for v in (*xs, *ys))
            or any(b <= a for a, b in zip(xs, xs[1:]))):
        raise NumericError("input")
    try:
        points = iter(points)
    except TypeError as error:
        raise NumericError("input") from error
    h = [b - a for a, b in zip(xs, xs[1:])]
    m = [(ys[i + 1] - ys[i]) / width for i, width in enumerate(h)]
    slopes = [m[0]] * len(xs)
    if len(xs) > 2:
        slopes[0] = _edge(h[0], h[1], m[0], m[1])
        slopes[-1] = _edge(h[-1], h[-2], m[-1], m[-2])
        for k in range(1, len(xs) - 1):
            before, after = m[k - 1], m[k]
            if before == 0 or after == 0 or (before > 0) != (after > 0):
                slopes[k] = 0.0
            else:
                w1, w2 = 2 * h[k] + h[k - 1], h[k] + 2 * h[k - 1]
                slopes[k] = (w1 + w2) / (w1 / before + w2 / after)
    result = []
    for t in points:
        if not _finite(t):
            raise NumericError("input")
        if t <= xs[0]:
            result.append(ys[0])
        elif t >= xs[-1]:
            result.append(ys[-1])
        else:
            i = bisect_right(xs, t) - 1
            s = (t - xs[i]) / h[i]
            result.append(ys[i] * (2 * s**3 - 3 * s**2 + 1)
                          + h[i] * slopes[i] * (s**3 - 2 * s**2 + s)
                          + ys[i + 1] * (3 * s**2 - 2 * s**3)
                          + h[i] * slopes[i + 1] * (s**3 - s**2))
    return result


def _valid_count(n):
    return isinstance(n, int) and not isinstance(n, bool) and n >= 1


def check_strict(question, cdf):
    n = question.inbound_outcome_count
    if not _valid_count(n) or not isinstance(cdf, (list, tuple)) or len(cdf) != n + 1:
        return False
    if any(not _finite(v) or not 0 <= v <= 1 or abs(v - round(v, 10)) > 1e-12 for v in cdf):
        return False
    if question.open_lower:
        if cdf[0] < 0.001 + 1e-6:
            return False
    elif cdf[0] != 0:
        return False
    if question.open_upper:
        if cdf[-1] > 0.999 - 1e-6:
            return False
    elif cdf[-1] != 1:
        return False
    minimum = STRICT_MIN * 0.01 / n
    maximum = STRICT_MAX * 0.2 * 200 / n
    return all(minimum - 1e-10 <= b - a <= maximum + 1e-10 for a, b in zip(cdf, cdf[1:]))


def _finish(question, cdf, notes):
    cdf = [round(value, 10) for value in cdf]
    if not question.open_lower:
        cdf[0] = 0.0
    if not question.open_upper:
        cdf[-1] = 1.0
    if not check_strict(question, cdf):
        raise NumericError("cdf")
    return Built(tuple(cdf), tuple(notes))


def build_cdf(question, values):
    """Apply snapping, tie spreading, tails, uniform mixing and nearest packing."""
    n = question.inbound_outcome_count
    if not _valid_count(n):
        raise NumericError("input")
    locations = check_values(question, values)
    notes = []
    width = 1 / n
    if question.kind == "discrete":
        snapped = [(2 * min(n - 1, max(0, math.floor(x * n))) + 1) / (2 * n)
                   if 0 <= x <= 1 else x for x in locations]
        if any(abs(a - b) > 1e-12 for a, b in zip(locations, snapped)):
            notes.append("snap")
        locations = snapped
    start, spread = 0, False
    while start < len(locations):
        stop = start + 1
        centre = locations[start]
        while stop < len(locations) and abs(locations[stop] - centre) <= 1e-12:
            stop += 1
        count = stop - start
        if count >= 2:
            locations[start:stop] = [centre + width * (2 * i - (count - 1)) / (2 * count)
                                     for i in range(count)]
            spread = True
        start = stop
    if spread:
        notes.append("tie-spread")
    for i in range(1, len(locations)):
        locations[i] = max(locations[i], locations[i - 1] + width / 1000)
    xs = ([locations[0] - TAIL_K * (locations[1] - locations[0])]
          + locations + [locations[-1] + TAIL_K * (locations[-1] - locations[-2])])
    ys = [0.0] + [level / 100 for level in LEVELS] + [1.0]
    raw = [min(1.0, max(0.0, g)) for g in pchip(xs, ys, [k / n for k in range(n + 1)])]
    low_raw, high_raw = raw[0], 1 - raw[-1]
    low = min(max(low_raw, OPEN_TAIL_MIN), TAIL_TOTAL_MAX) if question.open_lower else 0.0
    high = min(max(high_raw, OPEN_TAIL_MIN), TAIL_TOTAL_MAX) if question.open_upper else 0.0
    capped = ((question.open_lower and low_raw > TAIL_TOTAL_MAX)
              or (question.open_upper and high_raw > TAIL_TOTAL_MAX))
    if low + high > TAIL_TOTAL_MAX:
        factor = TAIL_TOTAL_MAX / (low + high)
        low, high = low * factor, high * factor
        capped = True
    if capped:
        notes.append("tail-cap")
    mass = 1 - low - high
    bins = [max(0.0, b - a) for a, b in zip(raw, raw[1:])]
    if not question.open_lower:
        bins[0] += low_raw
        if low_raw >= 0.001:
            notes.append("fold-low")
    if not question.open_upper:
        bins[-1] += high_raw
        if high_raw >= 0.001:
            notes.append("fold-high")
    total = sum(bins)
    if total <= 1e-12:
        shape = [width] * n
        notes.append("flat-interior")
    else:
        shape = [p / total for p in bins]
    floor = FLOOR_FACTOR * (0.01 / n) / mass
    need = max(((floor - q) / (width - q) for q in shape if q < floor), default=0.0)
    weight = max(MIX_MIN, need)
    if weight > MIX_MAX:
        raise NumericError("cdf")
    notes.insert(0, f"mix-{weight:.3f}")
    bins = [mass * ((1 - weight) * q + weight / n) for q in shape]
    cap = CAP_FACTOR * (0.2 * 200 / n)
    if any(p > cap for p in bins):
        notes.append("maxstep-pack")
        for k in range(n):
            if bins[k] <= cap:
                continue
            excess = bins[k] - cap
            bins[k] = cap
            for distance in range(1, n):
                ring = [j for j in (k - distance, k + distance) if 0 <= j < n]
                rooms = [max(0.0, cap - bins[j]) for j in ring]
                room = sum(rooms)
                if room >= excess:
                    for j, available in zip(ring, rooms):
                        bins[j] += excess * available / room
                    excess = 0.0
                    break
                for j, available in zip(ring, rooms):
                    bins[j] += available
                excess -= room
            if excess > 0:
                raise NumericError("cdf")
    cdf = [low]
    for p in bins:
        cdf.append(cdf[-1] + p)
    return _finish(question, cdf, notes)


def _median(pairs):
    pairs = sorted(pairs)
    halfway = sum(weight for _, weight in pairs) / 2
    running = 0.0
    for i, (value, weight) in enumerate(pairs):
        running += weight
        equal = math.isclose(running, halfway, rel_tol=1e-12, abs_tol=0.0)
        if running >= halfway or equal:
            if equal and i + 1 < len(pairs):
                return (value + pairs[i + 1][0]) / 2
            return value
    raise NumericError("cdf")


def combine(question, cdfs, weights=None):
    if not isinstance(cdfs, (list, tuple)) or not cdfs:
        raise NumericError("input")
    if any(not check_strict(question, cdf) for cdf in cdfs):
        raise NumericError("input")
    if weights is None:
        weights = [1] * len(cdfs)
    if (not isinstance(weights, (list, tuple)) or len(weights) != len(cdfs)
            or any(not _finite(w) or w <= 0 for w in weights)):
        raise NumericError("input")
    if len(cdfs) == 1:
        return Built(tuple(cdfs[0]))
    # Rescaling keeps the same relative median and avoids overflow in the sum.
    largest = max(weights)
    weights = [w / largest for w in weights]
    cdf = [_median(list(zip(column, weights))) for column in zip(*cdfs)]
    return _finish(question, cdf, (f"cdf-median-{len(cdfs)}",))


def sdk_percentiles(question, cdf):
    if not check_strict(question, cdf):
        raise NumericError("cdf")
    result = []
    n = question.inbound_outcome_count
    for level in LEVELS:
        fraction = level / 100
        if cdf[0] < fraction < cdf[-1]:
            k = bisect_left(cdf, fraction, 1)
            location = (k - 1 + (fraction - cdf[k - 1]) / (cdf[k] - cdf[k - 1])) / n
            try:
                value = nominal(question, location)
            except (ValueError, ZeroDivisionError, OverflowError, TypeError) as error:
                raise NumericError("cdf") from error
            if not _finite(value) or (result and value <= result[-1][1]):
                raise NumericError("cdf")
            result.append((fraction, value))
    if len(result) < 2:
        raise NumericError("cdf")
    return result


def legacy(values):
    try:
        return {level: values[level] for level in LEGACY}
    except (KeyError, TypeError, IndexError) as error:
        raise NumericError("input") from error


def display_bounds(question):
    if question.kind == "discrete":
        n = question.inbound_outcome_count
        if not _valid_count(n):
            raise NumericError("input")
        if question.zero_point is None:
            step = (question.upper - question.lower) / n
            return question.lower + step / 2, question.upper - step / 2
        return nominal(question, 1 / (2 * n)), nominal(question, 1 - 1 / (2 * n))
    return question.lower, question.upper


def plain(x):
    if not _finite(x):
        raise NumericError("input")
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    text = format(x, ",.6f") if abs(x) >= 1 else format(x, ".12f")
    return text.rstrip("0").rstrip(".")


def final_format(question):
    return "\n".join(f"Percentile {level:g}: value" for level in LEVELS)


def percentile_guidance(question):
    lines = ["Give your forecast as 13 percentiles: 1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5 and 99.",
             "Percentile 10: X means there is a 10% chance the true value is below X."]
    if question.kind == "discrete":
        step = (question.upper - question.lower) / question.inbound_outcome_count
        lines.append(f"Use only possible outcomes, in steps of {plain(step)}; a value may repeat across percentiles when you are confident of it.")
    else:
        lines.append("Values should increase from percentile 1 to percentile 99.")
    return "\n".join(lines)


def guidance(question):
    lo, hi = (plain(v) for v in display_bounds(question))
    unit = question.unit or "as stated in the question"
    lines = [percentile_guidance(question),
             f"Units: {unit}. Write plain numbers in these units, with no scientific notation."]
    if is_percent_unit(question.unit):
        lines.append("This question is in percent: write 45 or 45% for forty-five percent, never 0.45.")
    elif is_fraction_scale(question):
        lines.append("This question uses a 0 to 1 scale: write 0.45 for forty-five percent, never 45 or 45%.")
    lines.append(f"The range shown is {lo} to {hi}.")
    lines.append(f"The range is open below: the outcome can be below {lo}; if you think that is likely, put your low percentiles below {lo}."
                 if question.open_lower else f"The outcome cannot be below {lo}.")
    lines.append(f"The range is open above: the outcome can be above {hi}; if you think that is likely, put your high percentiles above {hi}."
                 if question.open_upper else f"The outcome cannot be above {hi}.")
    return "\n".join(lines)
