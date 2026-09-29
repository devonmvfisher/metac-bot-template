"""Offline fakes for the A5-R2 modules: web pages, DNS, market APIs and web-search replies.

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT).
Part of the A5-R2 acceptance tests. Do not weaken; add new fakes in your own file if you need more.
Reviewed and changed by Claude Opus 5.5 (workflow a5-r2-review, Sun Sep 27 2026): the offline guard below.
"""
import base64
from contextlib import contextmanager
import ipaddress
import json
import logging
from pathlib import Path
import socket
import sys
from fbot.types import Question

FIXTURES = Path(__file__).parent / "fixtures" / "r2"
PUBLIC_IP = "93.184.216.34"
SENTINEL = "FAKEKEY-R2-SENTINEL-7Q"


class OfflineViolation(BaseException):
    """A DNS lookup or an outside connection during the r2 tests.

    It derives from BaseException on purpose: the modules fail soft with `except Exception`, so they cannot
    swallow it. Any real lookup or connection therefore ends the test with an ERROR, before a packet leaves."""


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _offline_audit(event, args):
    if event in ("socket.getaddrinfo", "socket.gethostbyname") and args and not _loopback(args[0]):
        raise OfflineViolation("DNS lookup during the r2 tests")


def _no_connection(*args, **kwargs):
    raise OfflineViolation("outside connection during the r2 tests")


_GUARD = []


def install_offline_guard():
    """Deny DNS lookups (audit events, so a name imported early is covered too) and socket.create_connection
    (what urllib and http.client use). tests/run_offline.py already denies raw connections. Idempotent."""
    if not _GUARD:
        sys.addaudithook(_offline_audit)
        socket.create_connection = _no_connection
        _GUARD.append(True)


install_offline_guard()


def load(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def body_of(entry):
    if "body_b64" in entry:
        return base64.b64decode(entry["body_b64"])
    return entry.get("body", "").encode("utf-8")


class Headers:
    """Case-insensitive header lookup, like email.message.Message.get."""

    def __init__(self, values):
        self.values = {str(k).lower(): str(v) for k, v in (values or {}).items()}

    def get(self, name, default=None):
        return self.values.get(str(name).lower(), default)


class FakeResponse:
    def __init__(self, status, headers, body, clock=None, seconds_per_read=0, chunk_limit=None):
        self.status = status
        self.headers = Headers(headers)
        self.body = body
        self.position = 0
        self.clock = clock
        self.seconds_per_read = seconds_per_read
        self.chunk_limit = chunk_limit
        self.reads = 0
        self.bytes_served = 0
        self.closed = False

    def read(self, size=-1):
        self.reads += 1
        if self.clock is not None and self.seconds_per_read:
            self.clock.sleep(self.seconds_per_read)
        if size is None or size < 0:
            size = len(self.body) - self.position
        if self.chunk_limit:
            size = min(size, self.chunk_limit)
        data = self.body[self.position:self.position + size]
        self.position += len(data)
        self.bytes_served += len(data)
        return data

    def close(self):
        self.closed = True


class FakeWeb:
    """The open_url fake. routes maps an exact URL to a dict with any of:
    status, headers, body (str or bytes), body_b64, raise (an exception instance), delay (seconds on the
    fake clock before replying), seconds_per_read, chunk_limit. Unknown URLs get 404."""

    def __init__(self, clock, routes=None):
        self.clock = clock
        self.routes = dict(routes or {})
        self.calls = []
        self.responses = []

    def add(self, url, **route):
        self.routes[url] = route

    def urls(self):
        return [call[0] for call in self.calls]

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        route = self.routes.get(url)
        if route is None:
            response = FakeResponse(404, {"Content-Type": "text/html"}, b"<html>not found</html>", self.clock)
            self.responses.append(response)
            return response
        if route.get("delay"):
            self.clock.sleep(route["delay"])
        if route.get("raise") is not None:
            raise route["raise"]
        body = body_of(route) if "body_b64" in route else route.get("body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        response = FakeResponse(route.get("status", 200), route.get("headers", {}), body, self.clock,
                                route.get("seconds_per_read", 0), route.get("chunk_limit"))
        self.responses.append(response)
        return response


def page_route(name):
    """(url, route) for a REAL page fixture."""
    entry = load(name)
    headers = dict(entry.get("headers") or {})
    if entry.get("declared_length") is not None:
        headers["Content-Length"] = str(entry["declared_length"])
    return entry["source_url"], {"status": entry["status"], "headers": headers, "body": body_of(entry)}


def synthetic_route(key):
    entry = load("synthetic_pages")["pages"][key]
    return entry["url"], {"status": entry["status"], "headers": entry.get("headers") or {}, "body": body_of(entry)}


class FakeResolver:
    """resolve(host, port) -> list of IP strings. Unknown hosts resolve to PUBLIC_IP unless strict."""

    def __init__(self, table=None, strict=False):
        self.table = {k.lower(): (v if isinstance(v, Exception) else list(v)) for k, v in (table or {}).items()}
        self.strict = strict
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        host = host.lower()
        if host in self.table:
            value = self.table[host]
            if isinstance(value, Exception):
                raise value
            return value
        if self.strict:
            raise OSError("no such host")
        return [PUBLIC_IP]


class FakeSend:
    """The research2 send fake: send(method, url, headers, body, timeout) -> (status, data).
    Each scripted item is a (status, data) tuple or an exception instance. seconds moves the fake clock."""

    def __init__(self, clock, script=(), seconds=0):
        self.clock = clock
        self.script = list(script)
        self.seconds = seconds
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), json.loads(json.dumps(body)), timeout))
        if self.seconds:
            self.clock.sleep(self.seconds)
        item = self.script.pop(0) if self.script else (500, {})
        if isinstance(item, Exception):
            raise item
        return item


def reply(name):
    entry = load("openrouter_online_replies")["replies"][name]
    return entry["status"], entry["data"]


class FakeJSON:
    """The markets get_json fake: get_json(url, timeout) -> (status, data).
    routes maps a URL prefix (scheme+host+path) to (status, data) or an exception; seconds moves the fake clock."""

    def __init__(self, clock, routes=None, seconds=0):
        self.clock = clock
        self.routes = dict(routes or {})
        self.seconds = seconds
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.seconds:
            self.clock.sleep(self.seconds)
        for prefix, value in self.routes.items():
            if url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        return 404, None


def market_routes():
    return {
        "https://gamma-api.polymarket.com/public-search": (200, load("markets_polymarket_search")["data"]),
        "https://api.manifold.markets/v0/search-markets": (200, load("markets_manifold_search")["data"]),
    }


def question(qid=501, kind="binary", title="Will the synthetic index exceed 50 by 2026-12-31?", **fields):
    fields.setdefault("resolution", "Resolves using the fictional published index.")
    fields.setdefault("fine_print", "")
    return Question(qid, qid + 1000, kind, title, **fields)


class Captured:
    def __init__(self):
        self.lines = []

    def text(self):
        return "\n".join(self.lines)


@contextmanager
def captured_logs():
    """Collect every fbot log line at INFO, restoring the logger afterwards."""
    log = logging.getLogger("fbot")
    store = Captured()

    class Handler(logging.Handler):
        def emit(self, record):
            store.lines.append(record.getMessage())

    handler = Handler(logging.INFO)
    old_level = log.level
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        yield store
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
