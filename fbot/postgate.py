"""Verify SDK drift, then send the registered forecast and private comment."""
import json
import logging
import threading
from urllib.parse import urlsplit
from . import SkipQuestion, validate
from .metadata import already_forecast
from .targets import active

logger = logging.getLogger("fbot")


class Gate:
    def __init__(self, state, env=None, readback=already_forecast):
        self.state, self.env, self.readback = state, env or {}, readback
        self.lock = threading.RLock()
        self.results = {}
        self.posts = {}
        self.posted_order = []
        self.pending = set()
        self.unfinished = {}
        self.rate_limited = set()

    def register(self, question, result):
        validate.require(question, result.value, result.cdf, result.numeric_v1)
        if not result.comment.startswith("FBOT ") or len(result.comment) > 10000:
            raise SkipQuestion("INVALID_OUTPUT")
        with self.lock:
            self.results[question.qid] = (question, result)
            self.unfinished[question.qid] = self.state.failures[question.qid]
            members = self.posts.setdefault(question.post_id, [])
            if question.qid not in members:
                members.append(question.qid)

    def block(self, qid, kind, rule, reason="INVALID_OUTPUT"):
        registered = self.results.get(qid)
        if registered:
            self.state.skip(registered[0], reason)
            self.state.failure(qid, reason)
        with self.state.lock:
            self.state.counts["gate_blocks"] += 1
        logger.warning("GATE_BLOCK qid=%s kind=%s rule=%s", qid if isinstance(qid, int) else "unknown", kind, rule)
        self.state.alert("GATE_BLOCKED")
        raise SkipQuestion(reason)

    def read(self, question):
        details = {}
        try:
            if self.readback is already_forecast:
                found = self.readback(question.post_id, question.qid, self.env, details=details)
            else:
                found = self.readback(question.post_id, question.qid, self.env)
        except Exception:
            found = None
        question.is_group = bool(details.get('group', question.is_group))
        logger.info("READBACK qid=%s state=%s why=%s group=%s", question.qid,
                    "found" if found is True else "none" if found is False else "unknown",
                    details.get('why', 'shape' if found is None else 'ok'), int(question.is_group))
        if found is None and question.is_group and question.target != 'test':
            self.state.counts['readback_unknown_group'] += 1
        return found

    @staticmethod
    def matches(question, actual, wanted):
        if question.kind == "binary":
            return validate.finite(actual) and abs(actual - wanted) <= 1e-3
        if question.kind == "multiple_choice":
            if not isinstance(actual, dict) or set(actual) - set(question.options) - set(question.retired):
                return False
            if any(actual.get(key) is not None for key in question.retired):
                return False
            return all(validate.finite(actual.get(key)) and abs(actual[key] - wanted[key]) <= 1e-3
                       for key in question.options)
        return (isinstance(actual, (list, tuple)) and len(actual) == len(wanted)
                and all(validate.finite(a) and abs(round(a, 10) - round(b, 10)) <= 1e-3
                        for a, b in zip(actual, wanted)))

    def before(self, method, url, raw, qid=None):
        parsed = urlsplit(str(url))
        host = parsed.hostname or ""
        is_metaculus = host == "metaculus.com" or host.endswith(".metaculus.com")
        if method.upper() != "POST" or not is_metaculus:
            return raw, None
        forecast = parsed.path.rstrip("/") == "/api/questions/forecast"
        comment = parsed.path.rstrip("/") == "/api/comments/create"
        kind = "comment" if comment else "forecast"
        if not forecast and not comment:
            self.block(qid, kind, "endpoint")
        try:
            data = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
            # Network reads happen before acquiring the serialization lock.
            if forecast:
                if not isinstance(data, list) or not data:
                    self.block(qid, kind, "shape")
                data = [dict(entry) for entry in data]
                for entry in data:
                    qid = entry.get("question")
                    if not isinstance(qid, int) or isinstance(qid, bool):
                        self.block(None, kind, "shape")
                    if qid not in self.results:
                        self.block(qid, kind, "unregistered")
                    question, _ = self.results[qid]
                    found = self.read(question)
                    if question.target != "test":
                        if found is True:
                            self.block(qid, kind, "duplicate")
                        if found is None:
                            if qid in self.rate_limited:  # P429E
                                self.block(qid, kind, "readback", "POST_RATE_LIMITED")
                            self.block(qid, kind, "readback")
            with self.lock:
                if forecast:
                    ids = []
                    for entry in data:
                        qid = entry["question"]
                        question, result = self.results[qid]
                        if qid in self.state.posted or qid in self.pending or qid in ids:
                            self.block(qid, kind, "duplicate")
                        if result.deadline is not None and self.state.clock.monotonic() >= result.deadline:
                            self.block(qid, kind, "shape", "POST_RATE_LIMITED" if qid in self.rate_limited else "TOO_LATE")  # P429G
                        if question.target != "test" and not active(self.state.clock.now(), self.env):
                            self.block(qid, kind, "shape", "AFTER_SEASON")
                        if question.target == "season":
                            from .targets import season
                            if not season(self.env).valid:
                                self.state.alert("TARGET_MISMATCH")
                                self.block(qid, kind, "target")
                        if result.numeric_v1:
                            incoming = entry.get('continuous_cdf')
                            if (not isinstance(incoming, list) or len(incoming) != question.inbound_outcome_count + 1
                                    or not all(validate.finite(value) for value in incoming)):
                                self.block(qid, kind, 'shape')
                            entry['continuous_cdf'] = list(result.cdf)
                        wanted = validate.payload(question, result)
                        for name, value in wanted.items():
                            if value is None:
                                if entry.get(name) is not None:
                                    self.block(qid, kind, "mismatch")
                            elif not self.matches(question, entry.get(name), value):
                                self.block(qid, kind, "mismatch")
                            else:
                                entry[name] = value
                        ids.append(qid)
                    self.pending.update(ids)
                    return data, ("forecast", ids)
                members = self.posts.get(data["on_post"], [])
                if qid is None:
                    qid = next((item for item in reversed(self.posted_order)
                                if item in members and item not in self.state.commented), None)
                if qid not in members or qid not in self.state.posted or qid in self.state.commented:
                    self.block(qid, kind, "unregistered")
                question, result = self.results[qid]
                data = dict(data, text=result.comment, is_private=True, included_forecast=True,
                            parent=None, on_post=question.post_id)
                return data, ("comment", [qid])
        except (KeyError, ValueError, TypeError, AttributeError):
            self.block(qid if isinstance(qid, int) else None, kind, "shape")

    def after(self, ticket, status):
        if ticket is None:
            return
        kind, ids = ticket
        with self.lock, self.state.lock:
            limited = status == 429  # P429B
            if 400 <= status <= 499 and not limited:
                self.state.alert("API_REJECTED")
            if limited:
                for qid in ids:
                    self.state.counts["post_rate_limited"] += 1
                    logger.info("POST_RATE_LIMITED qid=%s kind=%s", qid, kind)
            for qid in ids:
                question, _ = self.results[qid]
                if kind == "forecast":
                    self.pending.discard(qid)
                    self.state.http_status[qid] = status
                    if limited:
                        self.rate_limited.add(qid)  # P429C
                    elif 400 <= status <= 499:
                        self.state.failure(qid, "INVALID_OUTPUT")
                if 200 <= status < 300:
                    if kind == "forecast":
                        if qid not in self.state.posted:
                            self.state.counts[question.target] += 1
                            self.posted_order.append(qid)
                            self.state.remember('record_posted', question.target)
                            self.state.remember('record_question', qid, question.target, self.state.clock.now())
                        self.state.posted.add(qid)
                    else:
                        self.state.commented.add(qid)
                        self.state.comment_failed.discard(qid)
                        self.state.remember("clear_comment_failed", qid)
            if not self.state.comment_failed:
                self.state.alerts.discard("COMMENT_FAILED")

    def retry_comments(self, send, env):
        for question, result in self.missing_comments():
            for attempt in range(3):
                if attempt:
                    self.state.clock.sleep(30)
                try:
                    body, ticket = self.before("POST", "https://www.metaculus.com/api/comments/create/",
                                               {"on_post": question.post_id}, qid=question.qid)
                    status, _ = send("POST", "https://www.metaculus.com/api/comments/create/",
                                     {"Authorization": "Token " + env.get("METACULUS_TOKEN", ""),
                                      "Content-Type": "application/json"}, body, 30)
                    self.after(ticket, status)
                    if 200 <= status < 300:
                        break
                except Exception:
                    continue
            if question.qid not in self.state.commented:
                with self.state.lock:
                    self.state.comment_failed.add(question.qid)
                    self.state.remember('record_comment_failed', question.qid)
                self.state.alert("COMMENT_FAILED")

    def finish_poll(self, qids=None):
        """Account once per registered attempt, including SDK publish exceptions."""
        with self.lock:
            for qid, previous_failures in list(self.unfinished.items()):
                if qids is not None and qid not in qids:
                    continue
                if qid in self.pending:
                    continue
                if qid not in self.state.posted and self.state.failures[qid] == previous_failures:
                    question, _ = self.results[qid]
                    if qid in self.rate_limited:  # P429D
                        self.state.failure(qid, 'POST_RATE_LIMITED')
                        self.state.skip(question, 'POST_RATE_LIMITED')
                    else:
                        self.state.failure(qid, 'POST_FAILED')
                        self.state.skip(question, 'POST_FAILED')
                self.rate_limited.discard(qid)
                del self.unfinished[qid]

    def missing_comments(self):
        with self.lock:
            return [(question, result) for qid, (question, result) in self.results.items()
                    if qid in self.state.posted and qid not in self.state.commented]
