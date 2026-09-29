"""Match outcome misreads only outside negation, then check parsed extremes."""
import math
from numbers import Real
import re
from . import guards

PATTERNS = guards.MISREAD_PATTERNS
WINDOW = 4
NEGATIONS = frozenset({"not", "never", "no", "nor", "cannot", "without", "whether", "if", "unless"})


def negated(text, start):
    try:
        clause = re.split(r"[.;:!?\n]", text[:start])[-1]
        words = re.findall(r"[A-Za-z'\u2019]+", clause.lower())[-WINDOW:]
        return any(word in NEGATIONS or word.endswith(("n't", "n\u2019t")) for word in words)
    except Exception:
        return False


def matches(text):
    if not isinstance(text, str):
        return []
    return [pattern for pattern in PATTERNS
            if any(not negated(text, match.start()) for match in re.finditer(pattern, text, re.I))]


def _finite(value):
    try:
        return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)
    except Exception:
        return False


def extreme(kind, value):
    if kind == "binary":
        return _finite(value) and (value < 0.05 or value > 0.95)
    if kind == "multiple_choice" and isinstance(value, dict):
        return any(_finite(number) and number > 0.95 for number in value.values())
    return False


def drop(question, text, value):
    try:
        return bool(matches(text)) and extreme(question.kind, value)
    except Exception:
        return False
