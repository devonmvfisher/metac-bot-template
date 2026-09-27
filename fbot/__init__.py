"""Offline-testable forecasting decisions; external adapters are injected."""

REASONS = frozenset({
    "EXHAUSTED", "NO_LLM_KEY", "BUDGET_MINIBENCH", "ALL_MODELS_FAILED",
    "INVALID_OUTPUT", "MISREAD_ALL", "TOO_LATE", "UNHANDLED_TYPE",
    "NO_RESEARCH", "AFTER_SEASON", "DISABLED",
    "RETRY_CAPPED", "ALREADY_FORECAST", "READBACK_UNKNOWN",
})


class SkipQuestion(Exception):
    def __init__(self, reason):
        if reason not in REASONS:
            raise ValueError("unknown skip reason")
        self.reason = reason
        super().__init__(reason)


class CreditExhausted(Exception):
    def __init__(self, provider="router"):
        self.provider = provider
        super().__init__("EXHAUSTED")


class ModelFailure(Exception):
    def __init__(self, status=0):
        self.status = status
        super().__init__("MODEL_FAILED")
