import math
import statistics
from .config import PERCENTILES


def combine(kind, values, options=(), weights=None):
    if not values:
        raise ValueError("no forecasts")
    if kind == "binary":
        # Finite limits only for taking logarithms; output caps happen separately.
        logits = [math.log(max(1e-12, min(1 - 1e-12, p)) /
                           (1 - max(1e-12, min(1 - 1e-12, p)))) for p in values]
        median = weighted_median(logits, weights)
        return 1 / (1 + math.exp(-median))
    if kind == "multiple_choice":
        weights = weights or [1] * len(values)
        return {option: sum(value[option] * weight for value, weight in zip(values, weights)) / sum(weights)
                for option in options}
    means = sorted(statistics.mean(value[p] for value in values) for p in PERCENTILES)
    return dict(zip(PERCENTILES, means))


def weighted_median(values, weights=None):
    weights = weights or [1] * len(values)
    pairs = sorted(zip(values, weights))
    half, cumulative = sum(weights) / 2, 0
    for index, (value, weight) in enumerate(pairs):
        cumulative += weight
        if cumulative > half:
            return value
        if cumulative == half:
            return (value + pairs[index + 1][0]) / 2
    raise ValueError("invalid weights")
