"""Outside market snapshots presented only as labelled evidence."""
import json
import logging
import math
import re
import threading
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urlencode

from . import webread

logger = logging.getLogger("fbot")
HEADING = "OUTSIDE MARKETS (evidence only; may not match this question exactly)"
END = "END OUTSIDE MARKETS"
NOTE = "These are prices other people trade on questions that may differ from this one. Treat them as evidence only."
BUDGET_SECONDS = 15
CALL_SECONDS = 6
MAX_BYTES = 2000000
MAX_MATCHES = 3
MAX_PER_PLATFORM = 2
MIN_SCORE = 2  # PB15
PLATFORMS = ("Polymarket", "Manifold")
# Off unless set to true: both terms checks left a doubt (Polymarket terms unverified or broad on bots;
# Manifold terms s8 on revenue-generating use). MARKETS_ENABLED stays the master switch.
SWITCHES = {"Polymarket": "POLYMARKET_ENABLED", "Manifold": "MANIFOLD_ENABLED"}
POLYMARKET_URL = "https://gamma-api.polymarket.com/public-search"
MANIFOLD_URL = "https://api.manifold.markets/v0/search-markets"
MONTHS = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june",
          "jul": "july", "aug": "august", "sep": "september", "sept": "september", "oct": "october",
          "nov": "november", "dec": "december"}
STOPWORDS = frozenset("""
a an and are as at be been before between by can could did do does during end ever for from had has have
how if in into is it its least less many more most much next no not of on or over per same says than that
the their then there these this those to under until was were what when which who whom why will with would
yes above below after any according reported report reports first question resolve resolves value total
""".split())


class Market(NamedTuple):
    platform: str
    title: str
    yes: float | None
    volume: float | None
    unit: str
    close: str | None
    url: str


class MarketsBrief(NamedTuple):
    text: str = ""
    found: int = 0
    platforms_ok: int = 0
    platforms_tried: int = 0
    reasons: tuple = ()

    @property
    def available(self):
        return bool(self.text)

    def label(self):
        return f"markets {self.found}"


def terms(text):
    found = []
    for raw in re.findall(r"[a-z0-9][a-z0-9.%-]*", text.lower()):
        token = raw.strip(".-")
        if re.fullmatch(r"(?:[a-z]\.)+[a-z]", token):
            token = token.replace(".", "")
        token = MONTHS.get(token.rstrip("%"), token.rstrip("%"))
        if not token or token in STOPWORDS or (not any(c.isdigit() for c in token) and len(token) < 3):
            continue
        if token not in found:
            found.append(token)
    return found


def search_query(words):
    return " ".join([word for word in words if not any(char.isdigit() for char in word)][:4])


def polymarket_url(q):
    return POLYMARKET_URL + "?" + urlencode({"q": q, "limit_per_type": 5, "events_status": "active"})


def manifold_url(q):
    return MANIFOLD_URL + "?" + urlencode({"term": q, "limit": 10, "filter": "open", "sort": "score"})


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, OverflowError):
        return None


def _price(value):
    number = _number(value)
    return number if number is not None and number <= 1 else None


def _space(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _date(value):
    return _space(value)[:10] or None


def _list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return value if isinstance(value, list) else []


def parse_polymarket(data):
    found = []
    try:
        events = data.get("events", []) if isinstance(data, dict) else []
        for event in _list(events):
            if not isinstance(event, dict) or event.get("closed") or event.get("active") is False:
                continue
            for market in _list(event.get("markets")):
                if not isinstance(market, dict):
                    continue
                if market.get("closed") or market.get("active") is False:  # PB16
                    continue
                outcomes = _list(market.get("outcomes"))
                prices = _list(market.get("outcomePrices"))
                if "Yes" not in outcomes:
                    continue
                index = outcomes.index("Yes")
                yes = _price(prices[index]) if index < len(prices) else None
                volume = _number(market.get("volumeNum"))
                if volume is None:
                    volume = _number(market.get("volume"))
                found.append(Market("Polymarket", _space(market.get("question")), yes, volume, "USD",
                                    _date(market.get("endDate")),
                                    "https://polymarket.com/event/" + _space(event.get("slug"))))
    except Exception:
        pass
    return found


def parse_manifold(data):
    found = []
    try:
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or item.get("isResolved"):  # PB27
                continue
            url = _space(item.get("url"))
            if not url.startswith("https://manifold.markets/"):
                continue
            yes = _price(item.get("probability")) if item.get("outcomeType") == "BINARY" else None
            close = None
            milliseconds = _number(item.get("closeTime"))
            if milliseconds is not None:
                try:
                    close = datetime.fromtimestamp(milliseconds / 1000, timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, OverflowError, OSError):
                    pass
            found.append(Market("Manifold", _space(item.get("question")), yes, _number(item.get("volume")),
                                "mana", close, url))
    except Exception:
        pass
    return found


def score(words, title):
    title_words = set(terms(title))
    shared = [word for word in words if word in title_words]
    return len(shared) if any(not any(c.isdigit() for c in word) for word in shared) else 0


def select(words, candidates):
    ranked = [(score(words, market.title), market) for market in candidates]
    ranked = [(points, market) for points, market in ranked if points >= MIN_SCORE]
    ranked.sort(key=lambda pair: (-pair[0], PLATFORMS.index(pair[1].platform),
                                  -(pair[1].volume or 0), pair[1].title))
    chosen = []
    counts = {}
    for points, market in ranked:
        if counts.get(market.platform, 0) >= MAX_PER_PLATFORM:
            continue
        chosen.append(market)
        counts[market.platform] = counts.get(market.platform, 0) + 1
        if len(chosen) >= MAX_MATCHES:
            break
    return chosen


def line(m):
    title = _space(m.title).replace('"', "'")[:160]
    price = f"Yes {m.yes * 100:.1f}%" if m.yes is not None else "multi-outcome, see link"
    volume = "unknown"
    if m.volume is not None:
        volume = "US$" + f"{m.volume:,.0f}" if m.unit == "USD" else f"{m.volume:,.0f} mana"
    return (f'- {m.platform}: "{title}" | {price} | volume {volume} | closes {_space(m.close) or "unknown"}'
            f" | {_space(m.url)[:200]}")  # PB14


def block_text(chosen):
    return "\n".join([line(market) for market in chosen] + [NOTE])


def prompt_block(brief):
    return webread.fence(HEADING, brief.text, END) if brief.available else ""


def evidence(brief):
    return webread.section(HEADING, brief.text) if brief.available else None


def default_get_json(clock):
    def fetch(url, timeout):
        reply = webread.get(url, clock=clock, deadline=clock.monotonic() + timeout,
                            accept=webread.JSON_TYPES, max_bytes=MAX_BYTES)
        if reply.reason != "ok" or reply.truncated:
            return reply.status, None
        try:
            return reply.status, json.loads(webread.decode(reply.body, reply.charset))
        except (ValueError, TypeError):
            return reply.status, None
    return fetch


class Markets:
    def __init__(self, env, clock, get_json=None, tally=None):
        self.env = env
        self.clock = clock
        self.get_json = get_json if get_json is not None else default_get_json(clock)
        self.tally = tally
        self._lock = threading.Lock()
        self._qid_locks = {}
        self._cache = {}

    def _snapshot(self, question, deadline):
        tried = 0
        ok = 0
        reasons = []
        chosen = []
        text = ""
        try:
            words = terms(question.title)
            query = search_query(words)
            calls = []
            if query and webread.switch(self.env, SWITCHES["Polymarket"], False):  # R3P
                calls.append((polymarket_url(query), parse_polymarket))
            if query and webread.switch(self.env, SWITCHES["Manifold"], False):  # R3M
                calls.append((manifold_url(query), parse_manifold))
            end = self.clock.monotonic() + BUDGET_SECONDS
            if deadline is not None:
                end = min(end, deadline)
            candidates = []
            for url, parser in calls:
                tried += 1
                remaining = end - self.clock.monotonic()
                if remaining <= 0:  # PB17
                    reasons.append("timeout")
                    continue
                try:
                    status, data = self.get_json(url, min(CALL_SECONDS, remaining))
                except TimeoutError:
                    reasons.append("timeout")
                    continue
                except Exception:
                    reasons.append("error")
                    continue
                if self.clock.monotonic() > end:
                    reasons.append("timeout")
                elif status != 200 or data is None:
                    reasons.append("http")
                else:
                    candidates.extend(parser(data))
                    ok += 1
                    reasons.append("ok")
            chosen = select(words, candidates)
            text = block_text(chosen) if chosen else ""
        except Exception:
            reasons.append("error")
            text = ""
            chosen = []
        result = MarketsBrief(text, len(chosen), ok, tried, tuple(reasons))
        if tried and self.tally is not None:
            self.tally.record("markets", result.found > 0)
        logger.info("MARKETS qid=%s found=%d platforms_ok=%d/%d",
                    getattr(question, "qid", 0), result.found, ok, tried)
        return result

    def snapshot(self, question, deadline=None):
        try:
            if not webread.switch(self.env, "MARKETS_ENABLED", True):
                return MarketsBrief(reasons=("disabled",))
            with self._lock:
                qid_lock = self._qid_locks.setdefault(question.qid, threading.Lock())
            with qid_lock:
                with self._lock:
                    if question.qid in self._cache:
                        return self._cache[question.qid]
                result = self._snapshot(question, deadline)
                with self._lock:
                    self._cache[question.qid] = result
                return result
        except Exception:
            return MarketsBrief(reasons=("error",))
