"""Small injectable HTTP client; HTTP bodies and keys never enter exceptions."""
import json
import logging
import math
import threading
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler
from . import CreditExhausted, ModelFailure

logger = logging.getLogger("fbot")
ROUTER = "https://openrouter.ai/api/v1/chat/completions"
DIRECT = "https://api.openai.com/v1/chat/completions"
KEY_URL = "https://openrouter.ai/api/v1/key"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(method, url, headers, body, timeout, total=None):
    from urllib.request import build_opener
    limit = time.monotonic() + (timeout if total is None else total)
    headers = {"User-Agent": "Sextant/0.2 (+https://github.com/devonmvfisher/metac-bot-template)", **(headers or {})}
    request = Request(url, data=None if body is None else json.dumps(body).encode(),
                      headers=headers, method=method)
    def read_body(response):
        chunks = []
        # read1 returns available bytes without waiting to fill a 64 KiB buffer.
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() >= limit:
                raise ModelFailure(0)
            chunk = reader(64 * 1024)
            if time.monotonic() >= limit:
                raise ModelFailure(0)
            if not chunk:
                break
            chunks.append(chunk)
        return json.loads(b"".join(chunks))
    try:
        try:
            with build_opener(NoRedirect()).open(request, timeout=min(timeout, max(0.001, limit - time.monotonic()))) as response:
                return response.status, read_body(response)
        except HTTPError as error:
            with error:
                try:
                    data = read_body(error)
                except ValueError:
                    data = {}
                return error.code, data
    except (TimeoutError, URLError, OSError, ValueError, HTTPException):
        raise ModelFailure(0) from None


class Client:
    def __init__(self, env, clock, events, send=transport):
        self.env, self.clock, self.events, self.send = env, clock, events, send
        self.lock = threading.Lock()
        self.exhausted = set()
        self.unavailable = set()

    def remaining(self, deadline, maximum):
        if deadline is None:
            return maximum
        value = min(maximum, deadline - self.clock.monotonic())
        if value <= 0:
            raise ModelFailure(0)
        return value

    def quota(self, provider, status, data):
        error = data.get("error", {}) if isinstance(data, dict) else {}
        error = error if isinstance(error, dict) else {}
        if status == 402 or (status == 403 and "key limit exceeded" in str(error.get("message", "")).lower()) or (provider == "bridge" and status == 429 and
                             error.get("code") == "insufficient_quota"):
            with self.lock:
                self.exhausted.add(provider)
            source = error.get("metadata", {})
            source = source.get("limit_source") if isinstance(source, dict) else None
            # No provider-controlled strings in logs, including seemingly harmless metadata.
            if source not in ("key", "account", "organization", "user", "credits"):
                source = "unknown"
            logger.info("CREDIT status=402 limit_source=%s", source)
            self.events("CREDITS_EXHAUSTED")
            raise CreditExhausted(provider)

    def one(self, model, prompt, bridge=False, deadline=None, timeout=480, probe=False):
        provider = "bridge" if bridge else "router"
        key = self.env.get("OPENAI_API_KEY" if bridge else "OPENROUTER_API_KEY", "")
        if not key:
            raise ModelFailure(401)
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if bridge:
            body.update(reasoning_effort="high", max_completion_tokens=4096 if probe else 32000)
        else:
            body.update(reasoning={"effort": "high"}, max_tokens=4096 if probe else 32000)
        headers = {"Authorization": 'Bearer ' + key, "Content-Type": "application/json"}
        for attempt in range(2):
            with self.lock:
                if provider in self.exhausted:
                    raise CreditExhausted(provider)
            try:
                status, data = self.send("POST", DIRECT if bridge else ROUTER, headers, body,
                                         self.remaining(deadline, timeout), total=self.remaining(deadline, timeout))
            except (TimeoutError, ModelFailure, OSError):
                status, data = 0, {}
            self.quota(provider, status, data)
            if 200 <= status < 300:
                try:
                    content = data["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError()
                    return content
                except (KeyError, IndexError, TypeError, ValueError):
                    raise ModelFailure(status) from None
            if status in (400, 404):
                with self.lock:
                    if model not in self.unavailable:
                        self.unavailable.add(model)
                        self.events("MODEL_UNAVAILABLE")
                        logger.info("MODEL_UNAVAILABLE model=%s", model)
            if attempt == 0 and (status == 0 or 500 <= status <= 599):
                if deadline is not None and deadline - self.clock.monotonic() <= 20:
                    raise ModelFailure(status)
                self.clock.sleep(20)
                continue
            raise ModelFailure(status)
        raise ModelFailure(0)

    def slot(self, models, prompt, **kwargs):
        last = ModelFailure(0)
        for model in models:
            try:
                return self.one(model, prompt, **kwargs), model
            except ModelFailure as failure:
                last = failure
                if failure.status in (401, 403):
                    break
        raise last

    def key(self):
        if not self.env.get("OPENROUTER_API_KEY"):
            return None, None
        with self.lock:
            if "router" in self.exhausted:
                raise CreditExhausted("router")
        try:
            status, body = self.send("GET", KEY_URL,
                                     {"Authorization": 'Bearer ' + self.env["OPENROUTER_API_KEY"]},
                                     None, 30)
            self.quota("router", status, body)
            data = body.get("data", {}) if status == 200 else {}
            remaining, limit = data.get("limit_remaining"), data.get("limit")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not math.isfinite(remaining):
                remaining = None
            if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not math.isfinite(limit):
                limit = None
            return remaining, limit
        except (ModelFailure, TimeoutError, OSError, AttributeError, TypeError):
            return None, None
