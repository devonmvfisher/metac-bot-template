import math
import statistics
from .config import PERCENTILES


def combine(kind, values, options=()):
    if not values:
        raise ValueError("no forecasts")
    if kind == "binary":
        # Finite limits only for taking logarithms; output caps happen separately.
        logits = [math.log(max(1e-12, min(1 - 1e-12, p)) /
                           (1 - max(1e-12, min(1 - 1e-12, p)))) for p in values]
        median = statistics.median(logits)
        return 1 / (1 + math.exp(-median))
    if kind == "multiple_choice":
        return {option: statistics.mean(value[option] for value in values)
                for option in options}
    means = sorted(statistics.mean(value[p] for value in values) for p in PERCENTILES)
    return dict(zip(PERCENTILES, means))
