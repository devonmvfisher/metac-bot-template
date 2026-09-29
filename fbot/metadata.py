"""Read-only count/read-back metadata using the cloned endpoint shapes."""
import time
from urllib.parse import urlencode
from .llm import transport
from .coverage import count_open

RETRY_STATUS = (429, 502, 503, 504)


def open_count(target, env, send=transport, details=None):
    count, offset = 0, 0
    def done(value, reason):
        if details is not None:
            details['reason'] = reason
        return value
    try:
        for _ in range(20):
            params = urlencode({"tournaments": target, "statuses": "open", "limit": 100,
                                "offset": offset, "include_descriptions": "false"})
            status, data = send("GET", "https://www.metaculus.com/api/posts/?" + params,
                                {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 30)
            if status != 200:
                return done("unknown", f"http_{status}")
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                return done("unknown", "shape")
            if not data["results"]:
                return done(count, "ok")
            count += count_open(data["results"])
            offset += len(data["results"])
            if not data.get("next"):
                return done(count, "ok")
    except Exception:
        return done("unknown", "error")
    return done("unknown", "cap")


def readback(post_id, qids, env, send=transport, sleep=time.sleep, tries=3, details=None):
    """Read all requested members of one post, retaining R27's 2/4 second retry."""
    qids = tuple(qids)
    def unknown(why):
        return {qid: ("unknown", why) for qid in qids}
    status, post = None, None
    for attempt in range(tries):
        try:
            status, post = send("GET", f"https://www.metaculus.com/api/posts/{post_id}/",
                                {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 15)
        except Exception:
            status, post = None, None
        if status in (None, 0) or status in RETRY_STATUS:
            if attempt + 1 < tries:
                sleep(2 * (attempt + 1))
                continue
            return unknown("dropped" if status in (None, 0) else "rate_limited" if status == 429 else f"http_{status}")
        break
    try:
        if status == 404:
            return unknown("no_post")
        if status != 200:
            return unknown(f"http_{status}")
        if not isinstance(post, dict):
            return unknown("shape")
        questions = [post.get("question")]
        group = post.get("group_of_questions")
        if details is not None:
            details['group'] = isinstance(group, dict)
        if isinstance(group, dict):
            if not isinstance(group.get("questions"), list):
                return unknown("shape")
            questions.extend(group['questions'])
        results = {}
        for qid in qids:
            question = next((q for q in questions if isinstance(q, dict) and q.get("id") == qid), None)
            if question is None:
                results[qid] = ("unknown", "not_in_post")
            elif "my_forecasts" not in question:
                results[qid] = ("unknown", "field_missing")
            else:
                own = question['my_forecasts']
                latest = own.get('latest') if isinstance(own, dict) else None
                results[qid] = ("found" if isinstance(latest, dict) and bool(latest) else "none", "ok")
        return results
    except Exception:
        return unknown("shape")


def already_forecast(post_id, qid, env, send=transport, sleep=time.sleep, tries=3, details=None):
    state, why = readback(post_id, [qid], env, send, sleep, tries, details)[qid]
    if details is not None:
        details.update(state=state, why=why)
    return True if state == 'found' else False if state == 'none' else None
