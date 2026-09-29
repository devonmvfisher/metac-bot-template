"""Counts-only coverage, bounded closed-post paging and weekly status rows."""
from datetime import timedelta
import logging
import math
from urllib.parse import urlencode
from . import config
from .targets import utc
from .llm import transport

logger = logging.getLogger("fbot")
RECENT_DAYS = 7
PAGE_SIZE = 100
MAX_PAGES = 100
SUPPORTED_TYPES = ("binary", "multiple_choice", "numeric", "discrete")
WEEKLY_TITLE = "Bot weekly status"
_TIERS = ("S", "A", "B", "C", "M", "BRIDGE")
_SOURCES = ("asknews", "web", "pages", "markets")
_TARGETS = ("season", "minibench")
# Season start and go-live: a question that closed earlier could never have been forecast.
SEASON_START = "2026-09-28T00:00:00Z"


def _count(value):
    return type(value) is int and value >= 0


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except Exception:
        return False


def questions_of(post):
    try:
        if not isinstance(post, dict):
            return []
        if isinstance(post.get("question"), dict):
            items = [post["question"]]
        else:
            group = post.get("group_of_questions")
            items = group.get("questions", []) if isinstance(group, dict) else []
        if not isinstance(items, (list, tuple)):
            return []
        return [item for item in items if isinstance(item, dict)
                and ("type" not in item or item["type"] in SUPPORTED_TYPES)]
    except Exception:
        return []


def closed_at(question):
    for key in ("actual_close_time", "scheduled_close_time"):
        try:
            return utc(question.get(key))
        except Exception:
            pass
    return None


def forecasted(question):
    try:
        forecasts = question.get("my_forecasts")
        if not isinstance(forecasts, dict):
            return None
        latest = forecasts.get("latest")
        return isinstance(latest, dict) and bool(latest)
    except Exception:
        return None


def count_recent(posts, now, days=RECENT_DAYS):
    result = {"closed": 0, "forecasted": 0, "forfeited": 0, "unknown": 0,
              "coverage": None, "forfeited_ids": [], "unknown_ids": [], "missed": []}
    seen = set()
    try:
        now = utc(now)
        cutoff = max(now - timedelta(days=days), utc(SEASON_START))
        for post in posts:
            for question in questions_of(post):
                if question.get("status") in ("upcoming", "open"):  # not closed yet; a closed group can hold these
                    continue
                qid = question.get("id")
                closed = closed_at(question)
                if type(qid) is not int or qid in seen or closed is None or not cutoff <= closed <= now:
                    continue
                seen.add(qid)
                result["closed"] += 1
                status = forecasted(question)
                name = "forecasted" if status is True else "forfeited" if status is False else "unknown"
                result[name] += 1
                if name != "forecasted":
                    result[name + "_ids"].append(qid)
    except Exception:
        pass
    denominator = result["forecasted"] + result["forfeited"]
    result["coverage"] = result["forecasted"] / denominator * 100 if denominator else None
    result["forfeited_ids"].sort()
    result["unknown_ids"].sort()
    result["missed"] = list(result["forfeited_ids"])
    return result


def count_open(posts):
    try:
        return sum(question.get("status") == "open" for post in posts for question in questions_of(post))
    except Exception:
        return 0


def read_recent(env, now, send=transport, targets=None, due=True, days=RECENT_DAYS, max_pages=MAX_PAGES):
    if not due:
        return None
    if targets is None:
        from . import targets as configured_targets
        season = configured_targets.season(env)
        targets = (("season", season.id), ("minibench", config.MINIBENCH_ID)) if season.valid else (("minibench", config.MINIBENCH_ID),)
    result = {}
    try:
        selected = [(name, target) for name, target in targets if name in _TARGETS]
    except Exception:
        return result
    for name, target in selected:
        reason, posts, pages = None, [], 0
        result[name] = None
        try:
            cutoff = utc(now) - timedelta(days=days)
            previous, offset = None, 0
            if type(max_pages) is not int or max_pages < 1:
                reason = "cap"
            else:
                for _ in range(max_pages):
                    params = [("tournaments", target), ("statuses", "closed"), ("statuses", "resolved"),
                              ("limit", PAGE_SIZE), ("offset", offset), ("include_descriptions", "false"),
                              # The site sends my_forecasts only with with_cp=true.
                              ("with_cp", "true"), ("order_by", "-scheduled_close_time")]
                    status, body = send("GET", "https://www.metaculus.com/api/posts/?" + urlencode(params),
                                        {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 30)
                    pages += 1
                    if status != 200:
                        reason = "http"
                        break
                    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                        reason = "shape"
                        break
                    page = body["results"]
                    if not page:
                        break
                    for post in page:
                        try:
                            closed = utc(post.get("scheduled_close_time"))
                        except Exception:
                            reason = "shape"
                            break
                        if previous is not None and closed > previous:
                            reason = "order"
                            break
                        previous = closed
                    if reason:
                        break
                    posts.extend(page)
                    offset += len(page)
                    if previous < cutoff or not body.get("next"):
                        break
                else:
                    reason = "cap"
        except Exception:
            reason = "error"
        if reason:
            logger.info("COVERAGE target=%s status=unknown reason=%s", name, reason)
        else:
            counts = count_recent(posts, now, days)
            result[name] = counts
            logger.info("COVERAGE target=%s pages=%s closed=%s forecasted=%s forfeited=%s unknown=%s",
                        name, pages, counts["closed"], counts["forecasted"], counts["forfeited"], counts["unknown"])
    return result


def weekly_due(issues, latest, now, days=6.5):
    try:
        dates = [utc(latest(issue)) for issue in issues if issue.get("title") == WEEKLY_TITLE]
        return not dates or utc(now) - max(dates) > timedelta(days=days)
    except Exception:
        return True


def forfeit_split(forfeited_ids, ledger):
    if ledger is None:
        return None
    try:
        ids = list(forfeited_ids)
        seen = sum(ledger.seen(qid) is not None for qid in ids)
        return {"seen": seen, "never_seen": len(ids) - seen}
    except Exception:
        return None


def _money(value, allow_off=False):
    if allow_off and value == "off":
        return "off"
    return f"{value:.2f}" if _number(value) else "unknown"


def weekly_rows(coverage_data, ledger=None, now=None, credit=None, comment_failed=0, preset=None, research=None):
    coverage_data = coverage_data if isinstance(coverage_data, dict) else {}
    rows, counts = [], {}
    for target in _TARGETS:
        value = coverage_data.get(target)
        counts[target] = value if isinstance(value, dict) else {}
    forfeits = {name: data.get("forfeited") if _count(data.get("forfeited")) else "unknown"
                for name, data in counts.items()}
    rows.append(f"forfeits season={forfeits['season']} minibench={forfeits['minibench']}")
    unknown = {name: data.get("unknown") if _count(data.get("unknown")) else 0 for name, data in counts.items()}
    if any(unknown.values()):
        rows.append(f"forfeits_status_unknown season={unknown['season']} minibench={unknown['minibench']}")
    for name, data in counts.items():
        if data:
            split = forfeit_split(data.get("forfeited_ids", []), ledger)
            if split is not None:
                rows.append(f"forfeit_split {name} seen={split['seen']} never_seen={split['never_seen']}")
    spend = None
    if ledger is not None and now is not None:
        try:
            spend = ledger.spend_7d(now)
        except Exception:
            pass
    if not isinstance(spend, dict):
        rows.append("spend_7d unknown")
    else:
        spending = []
        for tier in _TIERS:
            entry = spend.get(tier)
            if (isinstance(entry, (list, tuple)) and len(entry) == 2 and _count(entry[0])
                    and entry[0] > 0 and _number(entry[1]) and entry[1] >= 0):
                questions, usd = entry
                spending.append(f"spend_7d tier={tier} questions={questions} usd={usd:.2f} per_q={usd / questions:.2f}")
        rows.extend(spending or ["spend_7d none"])
    credit = credit if isinstance(credit, dict) else {}
    rows.append(f"limit_remaining metaculus={_money(credit.get('metaculus'))} own={_money(credit.get('own'), True)}")
    if isinstance(research, dict):
        rates = []
        for name in _SOURCES:
            entry = research.get(name)
            rate = "n/a"
            if (isinstance(entry, (tuple, list)) and len(entry) == 2
                    and all(_count(number) for number in entry) and entry[1]):
                try:
                    rate = f"{round(entry[0] / entry[1] * 100)}%"
                except Exception:
                    pass
            rates.append(f"{'markets_found' if name == 'markets' else name}={rate}")
        rows.append("research " + " ".join(rates))
    else:
        rows.append("research unknown")
    rows.append(f"comment_failed={comment_failed if _count(comment_failed) else 0}")
    rows.append(f"preset={preset if preset in ('auto', 'A', 'B', 'C') else 'unknown'}")
    return rows


def research_counts(counts):
    if not isinstance(counts, dict) or not any(name + suffix in counts for name in _SOURCES for suffix in ("_ok", "_tried")):
        return None
    return {name: tuple(counts.get(name + suffix) if _count(counts.get(name + suffix)) else 0
                        for suffix in ("_ok", "_tried")) for name in _SOURCES}
