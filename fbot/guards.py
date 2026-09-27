import math
import re

MISREAD_PATTERNS = (
    r"has already resolved", r"already resolved", r"question has resolved",
    r"resolved\s+(yes|no)", r"has already happened", r"this already occurred",
    r"outcome is known",
)


def misread(text):
    return any(re.search(pattern, text, re.I) for pattern in MISREAD_PATTERNS)


def scaled(question, value):
    if question.zero_point is None:
        return (value - question.lower) / (question.upper - question.lower)
    ratio = (question.upper - question.zero_point) / (question.lower - question.zero_point)
    # Algebraic inverse of the clone's _cdf_location_to_nominal_location.
    return math.log(1 + (value - question.lower) / (question.upper - question.lower) *
                    (ratio - 1)) / math.log(ratio)


def nominal(question, location):
    if question.zero_point is None:
        return question.lower + (question.upper - question.lower) * location
    ratio = (question.upper - question.zero_point) / (question.lower - question.zero_point)
    return question.lower + (question.upper - question.lower) * (
        ratio ** location - 1) / (ratio - 1)


def units_ok(question, percentiles):
    median = (percentiles[40] + percentiles[60]) / 2
    try:
        location = scaled(question, median)
        return math.isfinite(location) and -2 <= location <= 3
    except (ValueError, ZeroDivisionError, OverflowError):
        return False
