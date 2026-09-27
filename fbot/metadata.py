"""Read-only count/read-back metadata using the cloned endpoint shapes."""
from urllib.parse import urlencode
from .llm import transport


def open_count(target, env, send=transport):
    count = 0
    try:
        for offset in range(0, 10000, 50):
            params = urlencode({"tournaments": target, "statuses": "open", "limit": 50,
                                "offset": offset, "include_description": "false",
                                "forecast_type": "binary,multiple_choice,numeric,discrete"})
            status, data = send("GET", "https://www.metaculus.com/api/posts/?" + params,
                                {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 30)
            if status != 200 or not isinstance(data, dict) or not isinstance(data.get("results"), list):
                return "unknown"
            for post in data["results"]:
                question = post.get("question")
                if isinstance(question, dict) and question.get("status") == "open":
                    count += 1
            if not data.get("next"):
                return count
    except Exception:
        return "unknown"
    return "unknown"


def already_forecast(post_id, qid, env, send=transport):
    """Unknown metadata fails closed; a nonempty latest object means found."""
    try:
        status, post = send("GET", f"https://www.metaculus.com/api/posts/{post_id}/",
                            {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 15)
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
