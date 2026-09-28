"""Read-only count/read-back metadata using the cloned endpoint shapes."""
import re
import time
from urllib.parse import urlencode
from .llm import transport

RETRY_STATUS = (429, 502, 503, 504)
# Repeated keys, as the SDK sends them (the site also splits one comma value); group posts
# are unpacked into their sub-questions.
FORECAST_TYPES = ("binary", "multiple_choice", "numeric", "discrete", "group_of_questions")


def reason(value):
    """An HTTP status, "pages" or an exception class name; never a message or reply text."""
    if isinstance(value, BaseException):
        value = type(value).__name__
    elif type(value) is not int and value != "pages":
        value = "status"
    return re.sub(r"[^A-Za-z0-9]", "", str(value))[:40] or "Exception"


def questions_of(post):
    group = post.get("group_of_questions")
    return [post.get("question"), *((group.get("questions") or []) if isinstance(group, dict) else [])]


def open_count(target, env, send=transport):
    count = 0
    try:
        for offset in range(0, 10000, 50):
            params = urlencode([("tournaments", target), ("statuses", "open"), ("limit", 50),
                                ("offset", offset), ("include_descriptions", "false"),
                                *(("forecast_type", kind) for kind in FORECAST_TYPES)])
            status, data = send("GET", "https://www.metaculus.com/api/posts/?" + params,
                                {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 30)
            if status != 200 or not isinstance(data, dict) or not isinstance(data.get("results"), list):
                return "unknown:" + reason(status)
            for post in data["results"]:
                count += sum(isinstance(question, dict) and question.get("status") == "open"
                             for question in questions_of(post))
            # The site's list has no count, so "next" is never null: an empty or short page is the last.
            if not data.get("next") or len(data["results"]) < 50:
                return count
    except Exception as error:
        return "unknown:" + reason(error)
    return "unknown:pages"


def already_forecast(post_id, qid, env, send=transport, sleep=time.sleep, tries=3):
    """Unknown metadata fails closed; a nonempty latest object means found.

    A rate limit or a dropped reply is retried twice (2 s, then 4 s) before it counts as unknown.
    """
    status, post = None, None
    for attempt in range(tries):
        try:
            status, post = send("GET", f"https://www.metaculus.com/api/posts/{post_id}/",
                                {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 15)
        except Exception:
            status, post = None, None
        if status is None or status in RETRY_STATUS:
            if attempt + 1 < tries:
                sleep(2 * (attempt + 1))
                continue
            return None
        break
    try:
        if status != 200 or not isinstance(post, dict):
            return None
        questions = [post.get("question")]
        group = post.get("group_of_questions")
        if isinstance(group, dict):
            questions.extend(group.get("questions") or [])
        question = next((q for q in questions if isinstance(q, dict) and q.get("id") == qid), None)
        if question is None or "my_forecasts" not in question:
            return None
        own = question["my_forecasts"]
        latest = own.get("latest") if isinstance(own, dict) else None
        return isinstance(latest, dict) and bool(latest)
    except Exception:
        return None
