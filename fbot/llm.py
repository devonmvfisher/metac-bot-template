"""Small injectable HTTP client; HTTP bodies and keys never enter exceptions."""
import json
import logging
import math
import threading
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler
from . import CreditExhausted, ModelFailure, keys
from .webread import USER_AGENT

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
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
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
        raw = b"".join(chunks)
        return {} if response.status == 204 and not raw else json.loads(raw)
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
        self.own_mode = keys.mode(env, events)
        self.balances = {}
        self.state = getattr(events, '__self__', None)
        self.credit_source, self.key_usage = 'unknown', None
        self.credit_sources = {'router':'unknown', 'own':'unknown'}
        self.caps = {'router':self.parse_cap('BUDGET_CAP_USD'),
                     'own':self.parse_cap('BUDGET_CAP_OWN_USD')}

    def parse_cap(self, name):
        raw = self.env.get(name)
        if raw is None or isinstance(raw, str) and not raw.strip():
            return None
        try:
            value = float(raw)
            if isinstance(raw, bool) or not math.isfinite(value) or not 0 < value <= 10000:
                raise ValueError()
            return value
        except (ValueError, TypeError, OverflowError):
            if hasattr(self.state, 'counts'):
                with self.state.lock:
                    self.state.counts['budget_cap_invalid'] += 1
            logger.info('CONFIG %s invalid; ignored', name)
            self.events('BUDGET_CAP_INVALID')
            return None

    def credit(self, provider, remaining, limit, source, usage):
        self.balances[provider] = remaining
        self.credit_sources[provider] = source
        if provider == 'router':
            self.credit_source, self.key_usage = source, usage
            if hasattr(self.state, 'credit_source'):
                self.state.credit_source = source
        return remaining, limit

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
            if keys.enabled_keys(self.env, self.own_mode) <= self.exhausted:
                self.events("CREDITS_EXHAUSTED")
            raise CreditExhausted(provider)

    def key_order(self, target="season", minimum=None):
        from .config import COSTS
        costs = getattr(self, 'credit_costs', COSTS)
        if isinstance(minimum, str):
            minimum = costs[minimum]
        return keys.order(self.env, self.own_mode, self.exhausted, target, self.balances, minimum, costs['C'])

    def one(self, model, prompt, bridge=False, deadline=None, timeout=480, probe=False,
            target="season", minimum=None):
        attempted = set()
        while True:
            providers = ["bridge"] if bridge else self.key_order(target, minimum)
            provider = next((p for p in providers if p not in attempted), None)
            if provider is None:
                if attempted or self.exhausted:
                    raise CreditExhausted("router")
                raise ModelFailure(401)
            attempted.add(provider)
            try:
                answer = self._one(model, prompt, provider, deadline, timeout, probe)
                if provider == "own":
                    self.events("USING_OWN_KEY")
                return answer
            except CreditExhausted:
                if bridge:
                    raise

    def _one(self, model, prompt, provider, deadline, timeout, probe):
        bridge = provider == "bridge"
        key = self.env.get(keys.NAMES[provider], "")
        if not key:
            raise ModelFailure(401)
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if bridge:
            body.update(reasoning_effort="high", max_completion_tokens=4096 if probe else 32000)
        else:
            body.update(reasoning={"effort": "high"}, max_tokens=4096 if probe else 32000)
            body['usage'] = {'include': True}
        headers = {"Authorization": 'Bearer ' + key, "Content-Type": "application/json"}
        for attempt in range(2):
            with self.lock:
                if provider in self.exhausted:
                    raise CreditExhausted(provider)
            seconds = self.remaining(deadline, timeout)
            total = self.remaining(deadline, timeout)
            try:
                status, data = self.send("POST", DIRECT if bridge else ROUTER, headers, body,
                                         seconds, total=total)
            except (TimeoutError, ModelFailure, OSError):
                status, data = 0, {}
            except Exception:
                self.record_model(model, False)
                raise
            if not 200 <= status < 300:
                self.record_model(model, False)
            self.quota(provider, status, data)
            if 200 <= status < 300:
                try:
                    content = data["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError()
                    usage = data.get('usage')
                    self.record_model(model, True, usage)
                    if probe:
                        from .state import model_name, usage_number
                        usage = usage if isinstance(usage, dict) else {}
                        logger.info('USAGE model=%s in=%s out=%s cost=%s', model_name(model),
                                    usage_number(usage.get('prompt_tokens')), usage_number(usage.get('completion_tokens')), usage_number(usage.get('cost')))
                    return content
                except (KeyError, IndexError, TypeError, ValueError):
                    self.record_model(model, False)
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

    def record_model(self, model, ok, usage=None):
        record = getattr(self.state, 'record_model', None)
        if callable(record):
            record(model, ok, usage)

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

    def key(self, provider="router"):
        if not self.env.get(keys.NAMES[provider]) or (provider == "own" and self.own_mode == "off"):
            return None, None
        with self.lock:
            if provider in self.exhausted:
                raise CreditExhausted(provider)
        try:
            status, body = self.send("GET", KEY_URL,
                                     {"Authorization": 'Bearer ' + self.env[keys.NAMES[provider]]},
                                     None, 30)
            self.quota(provider, status, body)
            data = body.get("data", {}) if status == 200 else {}
            remaining, limit = data.get("limit_remaining"), data.get("limit")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not math.isfinite(remaining):
                remaining = None
            if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not math.isfinite(limit):
                limit = None
            usage = data.get('usage')
            if not isinstance(usage, (int, float)) or isinstance(usage, bool) or not math.isfinite(usage) or usage < 0:
                usage = None
            cap = self.caps.get(provider)
            capped = cap - usage if cap is not None and usage is not None else None
            source = 'key_limit' if remaining is not None else 'unknown'
            if remaining is not None:
                if capped is not None and capped < remaining:
                    remaining, limit, source = capped, cap if limit is None else min(limit, cap), 'cap_minus_usage'
            elif capped is not None:
                remaining, limit, source = capped, cap, 'cap_minus_usage'
            else:
                remaining, limit = None, None
            return self.credit(provider, remaining, limit, source, usage)
        except (ModelFailure, TimeoutError, OSError, AttributeError, TypeError):
            self.credit(provider, self.balances.get(provider), None, 'unknown', None)
            return None, None
