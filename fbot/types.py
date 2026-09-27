from dataclasses import dataclass, field
from datetime import datetime, timezone
import time


@dataclass
class Question:
    qid: int
    post_id: int
    kind: str
    title: str
    target: str = "season"
    background: str = ""
    resolution: str = ""
    fine_print: str = ""
    options: tuple = ()
    retired: tuple = ()
    lower: float = 0
    upper: float = 100
    open_lower: bool = False
    open_upper: bool = False
    zero_point: float | None = None
    inbound_outcome_count: int = 200
    size_known: bool = True
    unit: str = ""
    close_time: datetime | None = None
    resolve_time: datetime | None = None


@dataclass(frozen=True)
class Research:
    text: str = ""
    articles: int = 0
    available: bool = False


@dataclass
class Result:
    value: object
    summary: str
    rationales: list
    models: list = field(default_factory=list)
    caps: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    tier: str = "C"
    cdf: list | None = None
    prediction: object = None
    deadline: float | None = None

    @property
    def comment(self):
        return self.summary + "\n\n" + "\n\n".join(self.rationales)


class Clock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)
