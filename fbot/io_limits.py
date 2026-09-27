"""Per-question SDK socket budgets; context stays separate across worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
from . import SkipQuestion

_deadline = ContextVar("fbot_sdk_deadline", default=None)


@contextmanager
def call_scope(deadline):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def bounded_timeout(existing, clock):
    deadline = _deadline.get()
    remaining = 60 if deadline is None else min(480, deadline - clock.monotonic())
    if remaining <= 0:
        raise SkipQuestion("TOO_LATE")
    if isinstance(existing, (tuple, list)):
        return tuple(min(float(v), remaining) if v is not None else remaining for v in existing)
    return min(float(existing), remaining) if isinstance(existing, (float, int)) else remaining
