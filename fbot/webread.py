"""Shared bounded readers, evidence fences, and run-level accounting."""
import ipaddress
import logging
import math
import re
import socket
import threading
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

logger = logging.getLogger("fbot")
USER_AGENT = "SextantBot/1.0 (+https://github.com/devonmvfisher/metac-bot-template)"
# Hosts whose terms bar this bot outright (Kalshi Developer Agreement s3 and Data Terms II; FRED terms
# summary II and (p); unhcr.org terms s1). Checked on every hop by get(), whatever module calls it.
DENIED_HOSTS = ("kalshi.com", "stlouisfed.org", "unhcr.org")
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
JSON_TYPES = ("application/json",)
REASONS = ("ok", "scheme", "private", "error", "timeout", "http", "type", "size", "redirects", "denied")
SOURCES = ("asknews", "web", "pages", "markets")
REMOVED = "[marker removed]"
SECTION_CHARS = 6000
_once_lock = threading.Lock()
_once_keys = set()


class Reply(NamedTuple):
    reason: str
    status: int = 0
    content_type: str = ""
    charset: str = ""
    body: bytes = b""
    truncated: bool = False
    final_url: str = ""


def once(key):
    with _once_lock:
        if key in _once_keys:
            return False
        _once_keys.add(key)
        return True


def reset_once():
    with _once_lock:
        _once_keys.clear()


def switch(env, name, default=True):
    value = str(env.get(name, "")).strip().lower()
    if not value:
        return default  # PB20
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    if once(("switch", name)):
        logger.info("CONFIG %s invalid; using %s", name, str(bool(default)).lower())
    return default


def host_in(host, names):
    host = (host or "").lower().rstrip(".")
    return any(host == name or host.endswith("." + name) for name in names)


def denied(url, allow=None):
    try:
        host = urlsplit(url).hostname
    except Exception:
        return True
    return host_in(host, DENIED_HOSTS) or (allow is not None and not host_in(host, allow))  # R3D


def default_resolve(host, port):
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def public_address(text):
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_global and not address.is_multicast  # PB01
    except (ValueError, TypeError, AttributeError):
        return False


def check_url(url, resolve=None):
    try:
        parts = urlsplit(url)
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443)):
            return "scheme"
        host = parts.hostname
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except Exception:
        return "scheme"
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            addresses = (resolve or default_resolve)(host, port)
            if not addresses:
                return "error"
            return None if all(public_address(item) for item in addresses) else "private"
        except Exception:
            return "error"
    return None if public_address(host) else "private"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_open(url, headers, timeout):
    request = Request(url, headers=headers, method="GET")
    try:
        return build_opener(_NoRedirect()).open(request, timeout=timeout)
    except HTTPError as response:
        return response


def _media(headers):
    value = headers.get("Content-Type", "")
    pieces = value.split(";")
    content_type = pieces[0].strip().lower()
    charset = ""
    for piece in pieces[1:]:
        name, separator, value = piece.partition("=")
        if separator and name.strip().lower() == "charset":
            charset = value.strip().strip("\"'").lower()
    return content_type, charset


def get(url, *, clock, deadline=None, accept=TEXT_TYPES, max_bytes=1000000,
        open_url=None, resolve=None, max_redirects=3, chunk=65536, allow=None):
    current = url
    try:
        for hop in range(max_redirects + 1):
            if deadline is not None and clock.monotonic() >= deadline:
                return Reply("timeout")
            if denied(current):  # R3H
                return Reply("denied")
            reason = check_url(current, resolve)  # PB02
            if reason:
                return Reply(reason)
            if allow is not None and denied(current, allow):  # R3L
                return Reply("denied")
            remaining = None if deadline is None else deadline - clock.monotonic()
            if remaining is not None and remaining <= 0:
                return Reply("timeout")
            timeout = 10 if remaining is None else max(0.1, min(10, remaining))
            headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(accept)}  # PB03
            response = None
            try:
                response = (open_url or urllib_open)(current, headers=headers, timeout=timeout)
                status = int(response.status)
                content_type, charset = _media(response.headers)
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    if not location:
                        return Reply("http", status)
                    if hop >= max_redirects:
                        return Reply("redirects", status)
                    current = urljoin(current, location)
                    continue
                if not 200 <= status < 300:  # PB08
                    return Reply("http", status)
                if content_type not in accept:  # PB07
                    return Reply("type", status, content_type, charset)
                try:
                    declared = int(response.headers.get("Content-Length", ""))
                except (TypeError, ValueError):
                    declared = 0
                if declared > max_bytes:  # PB04
                    return Reply("size", status, content_type, charset)
                data = bytearray()
                reader = getattr(response, "read1", None) or response.read
                while True:
                    part = reader(min(chunk, max_bytes + 1 - len(data)))
                    if deadline is not None and clock.monotonic() > deadline:  # PB06
                        return Reply("timeout", status, content_type, charset)
                    if not part:
                        break
                    data.extend(part)
                    if len(data) > max_bytes:  # PB05
                        return Reply("ok", status, content_type, charset, bytes(data[:max_bytes]), True, current)
                return Reply("ok", status, content_type, charset, bytes(data), False, current)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
        return Reply("redirects")
    except (TimeoutError, socket.timeout):
        return Reply("timeout")
    except Exception:
        return Reply("error")


def decode(body, charset=""):
    try:
        if body.startswith(b"\xef\xbb\xbf"):
            return body.decode("utf-8-sig", errors="replace")
        if not charset:
            match = re.search(br"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
                              body[:2048], re.IGNORECASE)
            charset = match.group(1).decode("ascii") if match else "utf-8"
        try:
            return body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            return body.decode("utf-8", errors="replace")
    except Exception:
        return ""


_SKIP = frozenset(("script", "style", "noscript", "svg", "template", "nav", "footer",
                   "form", "select", "button", "iframe", "object", "canvas"))  # PB29
_BLOCK = frozenset(("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                    "table", "section", "article", "header", "main", "ul", "ol", "dl",
                    "dt", "dd", "blockquote", "pre", "title", "thead", "tbody", "caption",
                    "hr", "body", "head", "html", "aside"))
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"))


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skipped = []
        self.line_text = False

    def handle_starttag(self, tag, attrs):
        if self.skipped or tag in _SKIP:
            if tag not in _VOID:
                self.skipped.append(tag)
            return
        if tag in _BLOCK:
            self.parts.append("\n")
            self.line_text = False
        elif tag in ("td", "th"):
            if self.line_text:
                self.parts.append(" | ")

    def handle_endtag(self, tag):
        if self.skipped:
            if tag in self.skipped:
                index = len(self.skipped) - 1 - self.skipped[::-1].index(tag)
                del self.skipped[index:]
            return
        if tag in _BLOCK:
            self.parts.append("\n")
            self.line_text = False

    def handle_startendtag(self, tag, attrs):
        if self.skipped or tag in _SKIP:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(re.sub(r"\s+", " ", data))  # OWN01
            if data.strip():
                self.line_text = True


def html_text(body, content_type="text/html", charset=""):
    text = decode(body, charset).replace("\ufeff", "")
    if content_type != "text/plain":
        parser = _TextParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            pass
        text = "".join(parser.parts)
    lines = [" ".join(line.split()).strip(" |") for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def clip(text, limit):
    if len(text) <= limit:
        return text
    newline = text.rfind("\n", 0, limit)
    return text[:newline if newline > limit // 2 else limit]


def neutralise(text, markers=()):
    protected = {marker.strip().upper() for marker in markers}
    lines = []
    for line in text.replace("\r", "").split("\n"):
        stripped = line.strip()
        is_end = stripped.startswith("END ") and stripped == stripped.upper()  # PB09
        if stripped.upper() in protected or is_end or re.match(r"^\s*FINAL\b", line):
            line = REMOVED
        lines.append(line)
    return "\n".join(lines)


def fence(heading, text, end):
    return f"{heading}\n{neutralise(text, (heading, end))}\n{end}"


def section(heading, text):
    return heading, clip(neutralise(text, (heading, "END SECTION")), SECTION_CHARS)  # PB34


def alert_due(tried, failed):
    return tried >= 5 and failed * 5 > tried  # PB23 PB24


class Tally:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {f"{source}_{suffix}": 0 for source in SOURCES for suffix in ("tried", "ok")}
        self._cost = 0.0

    def record(self, source, ok):
        if source not in SOURCES:
            raise ValueError("unknown research source")
        with self._lock:
            self._counts[f"{source}_tried"] += 1
            if ok:
                self._counts[f"{source}_ok"] += 1

    def add_cost(self, amount):
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(amount) or amount < 0):
            return
        with self._lock:
            self._cost += amount

    def counts(self):
        with self._lock:
            return dict(self._counts)

    def spend(self):
        with self._lock:
            return {"research2_usd": round(self._cost, 6)}

    def alert_due(self, source):
        counts = self.counts()
        tried = counts[f"{source}_tried"]
        return alert_due(tried, tried - counts[f"{source}_ok"])

    def raise_alerts(self, state):
        if self.alert_due("asknews") or self.alert_due("web"):
            state.alert("RESEARCH_UNAVAILABLE")
            return True
        return False
