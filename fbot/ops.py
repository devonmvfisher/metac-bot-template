"""Stdlib operations; dispatch, heartbeat, then deduplicated issues, then exit."""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode
from .config import SEASON_ID, MINIBENCH_ID, enabled
from .llm import transport
from .runloop import should_dispatch, dispatch_wait
from .state import ALERTS
from .targets import active, utc
from .types import Clock

P0 = frozenset({"CREDITS_EXHAUSTED", "NO_LLM_KEY", "NO_FALL_QUESTIONS", "INSTALL_FAILING",
                "HEARTBEAT_FAILED", "API_REJECTED", "SEASON_OVER",
                "GATE_BLOCKED", "RUN_FAILED", "POLL_FAILING", "COMMENT_FAILED"})
STEPS = {
    "CREDITS_EXHAUSTED": "Assume no more credit is coming; submit another credit form yourself, in your own words; check the configured bridge or leave the bot stopped.",
    "GATE_BLOCKED": "The bot refused to send a forecast it could not verify. If it repeats and no helper is available, set BOT_ENABLED=false.",
    "RUN_FAILED": "If it repeats and no helper is available, set BOT_ENABLED=false.",
    "POLL_FAILING": "If it repeats and no helper is available, set BOT_ENABLED=false.",
    "COMMENT_FAILED": "The forecast is posted but its private comment is missing. Ask a helper. Never re-run the forecast.",
    "NO_LLM_KEY": "Check the secret names and run Test Bot.",
    "NO_FALL_QUESTIONS": "Ask a helper to check the season target and coverage reader.",
    "INSTALL_FAILING": "Run Test Bot and ask a helper to inspect the install step.",
    "HEARTBEAT_FAILED": "Edit .github/heartbeat.txt in the browser and check Actions permissions.",
    "API_REJECTED": "Check Test Bot on the test branch; ask a helper to inspect the rejected payload path.",
    "SEASON_OVER": "Leave the fork, secrets and Test Bot working until any prize is paid.",
    "CREDITS_LOW": "Check the remaining credit and the MiniBench setting.",
    "CREDIT_UNKNOWN": "Check the key status in Test Bot; forecasting uses Tier C.",
    "MODEL_UNAVAILABLE": "Check the PROBE lines in Test Bot and request a reviewed model fix.",
    "RESEARCH_UNAVAILABLE": "Check the AskNews secret names; see the resources link in RUNBOOK.md.",
    "SKIPS": "Read the skip counts; ask a helper about recurring misses.",
    "SCHEDULER_GAPS": "If this repeats on 3 days, consider CHAIN_ENABLED=true; that is your call under GitHub's terms.",
}


class ApiError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__("API_ERROR")


class Git:
    def run(self, *args):
        completed = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
        if completed.returncode:
            raise RuntimeError("GIT_FAILED")
        return completed.stdout.strip()


class GitHub:
    def __init__(self, env, send=transport):
        self.env, self.send = env, send
        self.repo = env.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo):
            raise ValueError("repository required")

    def request(self, method, path, data=None):
        status, body = self.send(method, "https://api.github.com" + path,
                                 {"Authorization": 'Bearer ' + self.env.get("GH_TOKEN", ""),
                                  "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28",
                                  "Content-Type": "application/json"}, data, 30)
        if not 200 <= status < 300:
            raise ApiError(status)
        return body

    def dispatch(self, branch):
        # The CLI uses the built-in GH_TOKEN, never a personal token.
        subprocess.run(["gh", "workflow", "run", "run_bot_on_tournament.yaml", "--ref", branch],
                       capture_output=True, check=True, timeout=45)

    def default_branch(self):
        return self.request("GET", f"/repos/{self.repo}")["default_branch"]

    def issues(self):
        result = []
        for page in range(1, 11):
            entries = self.request("GET", f"/repos/{self.repo}/issues?state=open&per_page=100&page={page}")
            result.extend(entry for entry in entries if "pull_request" not in entry)
            if len(entries) < 100:
                break
        return result

    def latest(self, issue):
        # updated_at alone changes on labels/edits; inspect the last actual comment.
        count = int(issue.get("comments", 0))
        if count:
            page = (count + 99) // 100
            comments = self.request("GET", f"/repos/{self.repo}/issues/{issue['number']}/comments?per_page=100&page={page}")
            if comments:
                return utc(comments[-1]["created_at"])
        return utc(issue["created_at"])

    def create(self, title, body, label):
        return self.request("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": [label]})

    def comment(self, number, body):
        return self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def runs(self):
        data = self.request("GET", f"/repos/{self.repo}/actions/workflows/run_bot_on_tournament.yaml/runs?per_page=100")
        return data["workflow_runs"]


def heartbeat(git, clock, root):
    last = float(git.run("git", "log", "-1", "--format=%ct"))
    if clock.now().timestamp() - last <= 25 * 86400:
        return False
    path = Path(root) / ".github" / "heartbeat.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(clock.now().date().isoformat() + "\n", encoding="utf-8", newline="\n")
    git.run("gh", "auth", "setup-git")
    git.run("git", "add", ".github/heartbeat.txt")
    git.run("git", "-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", "heartbeat: keep schedule alive")
    git.run("git", "pull", "--rebase")
    git.run("git", "push")
    return True


def longest_gap(runs, now):
    window = now - timedelta(hours=24)
    intervals = []
    for run in runs:
        if run.get("conclusion") == "cancelled" or not run.get("run_started_at"):
            continue
        try:
            start, end = utc(run["run_started_at"]), utc(run["updated_at"])
        except (ValueError, TypeError, KeyError):
            continue
        if end >= window and start <= now:
            intervals.append((start, max(start, end)))
    intervals.sort()
    maximum, previous_end = 0, None
    for start, end in intervals:
        if previous_end is not None:
            maximum = max(maximum, (start - previous_end).total_seconds())
        previous_end = max(previous_end, end) if previous_end else end
    return maximum / 60


def coverage(posts, now):
    total, forecasted, missed = 0, 0, []
    for post in posts:
        question = post.get("question")
        if not isinstance(question, dict):
            continue
        close = question.get("actual_close_time") or question.get("scheduled_close_time")
        if not close:
            raise ValueError("coverage unknown")
        closed = utc(close)
        if not now - timedelta(days=7) <= closed <= now:
            continue
        total += 1
        latest = ((question.get("my_forecasts") or {}).get("latest") or {})
        if latest.get("forecast_values") is not None:
            forecasted += 1
        else:
            missed.append(int(question["id"]))
    return {"closed": total, "forecasted": forecasted,
            "coverage": forecasted / total * 100 if total else None, "missed": missed}


def read_coverage(env, now, send=transport):
    results = {}
    for target, target_id in (("season", SEASON_ID), ("minibench", MINIBENCH_ID)):
        posts = []
        try:
            for offset in range(0, 10000, 100):
                params = urlencode({"tournaments": target_id, "statuses": "closed,resolved",
                                    "limit": 100, "offset": offset, "include_description": "false"})
                status, data = send("GET", "https://www.metaculus.com/api/posts/?" + params,
                                     {"Authorization": "Token " + env.get("METACULUS_TOKEN", "")}, None, 30)
                if status != 200 or not isinstance(data.get("results"), list):
                    raise ValueError()
                posts.extend(data["results"])
                if not data.get("next"):
                    break
            else:
                raise ValueError()
            results[target] = coverage(posts, now)
        except Exception:
            results[target] = None
    return results


def issue_once(github, title, body, label, now, hours, issues=None):
    issues = github.issues() if issues is None else issues
    found = next((issue for issue in issues if issue.get("title") == title), None)
    if found:
        if (now - github.latest(found)).total_seconds() <= hours * 3600:
            return False
        github.comment(found["number"], body)
    else:
        github.create(title, body, label)
    return True


def safe_counts(data):
    from . import REASONS
    def subset(key, names):
        return {name: max(0, int(value)) for name, value in data.get(key, {}).items()
                if name in names and isinstance(value, (int, float))}
    return {"counts": subset("counts", {"season", "minibench", "test", "poll_errors", "gate_blocks", "memo_hits"}),
            "skips": subset("skips", REASONS - {"BUDGET_MINIBENCH", "DISABLED", "ALREADY_FORECAST"})}


def mention(env):
    value = env.get("ALERT_MENTION", "")
    return value if re.fullmatch(r"@[A-Za-z0-9-]{1,39}", value) else ""


def weekly_body(data, coverage_data, gap, root, enabled_bot, env):
    if not enabled_bot:
        return "Bot status: disabled. " + mention(env)
    rows = ["Bot weekly status", json.dumps(safe_counts(data), sort_keys=True)]
    for target in ("season", "minibench"):
        item = coverage_data.get(target)
        rows.append(target + ": coverage unknown" if item is None else
                    f"{target}: closed={item['closed']} forecasted={item['forecasted']} "
                    f"coverage={item['coverage']}% missed={item['missed']}")
    for name in ("credit_after", "limit"):
        value = data.get(name)
        rows.append(f"{name}={value if isinstance(value, (int, float)) else 'unknown'}")
    tiers = [tier for tier in data.get("tiers", {}) if tier in ("A", "B", "C", "BRIDGE")]
    rows.append("tiers=" + ",".join(tiers))
    try:
        date = (Path(root) / ".github" / "heartbeat.txt").read_text().strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = "unknown"
    except OSError:
        date = "unknown"
    rows += ["heartbeat=" + date, f"longest_run_gap_minutes={gap}", mention(env)]
    return "\n".join(rows)


def run(env, data, clock, github, git, root=".", coverage_reader=None, emit=print, test=False, test_failed=False):
    if test or test_failed:
        try:
            message = "test run failed; inspect install/run steps " if test_failed else "test run ok "
            issue_once(github, "Bot weekly status", message + mention(env),
                       "bot-status", clock.now(), -1)
            return int(test_failed)
        except ApiError as error:
            if error.status == 410:
                emit("ISSUES DISABLED")
            return 1
    missing_state = not isinstance(data, dict) or not data
    data = data if isinstance(data, dict) else {}
    alerts = set()
    enabled_bot = enabled(env, "BOT_ENABLED")
    if should_dispatch(env, clock.now()):
        try:
            wait = dispatch_wait(float(env.get("JOB_START", clock.now().timestamp())), clock.now())
            if wait:
                clock.sleep(wait)
            if should_dispatch(env, clock.now()):
                github.dispatch(github.default_branch())
        except Exception:
            alerts.add("SCHEDULER_GAPS")
    try:
        heartbeat(git, clock, root)
    except Exception:
        alerts.add("HEARTBEAT_FAILED")
    if not active(clock.now()):
        alerts.add("SEASON_OVER")
    coverage_data, gap = {}, None
    if enabled_bot:
        alerts.update(key for key in data.get("alerts", []) if key in ALERTS)
        if env.get("INSTALL_OUTCOME") == "failure":
            alerts.add("INSTALL_FAILING")
        if env.get("RUN_OUTCOME") in ("failure", "cancelled") or (env.get("RUN_OUTCOME") == "success" and missing_state):
            alerts.add("RUN_FAILED")
        if sum(safe_counts(data)["skips"].values()):
            alerts.add("SKIPS")
        try:
            gap = longest_gap(github.runs(), clock.now())
            if gap > 75:
                alerts.add("SCHEDULER_GAPS")
        except Exception:
            gap = None
        if coverage_reader:
            try:
                coverage_data = coverage_reader()
            except Exception:
                coverage_data = {}
        for target, item in coverage_data.items():
            if item is not None:
                if item.get("coverage") is not None and item["coverage"] < 95:
                    alerts.add("SKIPS")
                if target == "season" and item.get("closed") == 0 and clock.now() > utc("2026-10-12T23:59:59Z"):
                    alerts.add("NO_FALL_QUESTIONS")
    else:
        emit("DISABLED")
        alerts.intersection_update({"HEARTBEAT_FAILED", "SEASON_OVER"})
    posted_p0 = False
    try:
        issues = github.issues()
        for key in sorted(alerts):
            body = f"{key}\n{json.dumps(safe_counts(data), sort_keys=True)}\nRUNBOOK.md: {STEPS[key]}\n{mention(env)}"
            sent = issue_once(github, "[BOT ALERT] " + key, body, "bot-alert", clock.now(), 24, issues)
            posted_p0 = posted_p0 or (sent and key in P0)
        issue_once(github, "Bot weekly status",
                   weekly_body(data, coverage_data, gap, root, enabled_bot, env),
                   "bot-status", clock.now(), 6.5 * 24, issues)
    except ApiError as error:
        if error.status == 410:
            emit("ISSUES DISABLED")
            return 1
        emit("ISSUES UNAVAILABLE")
    except Exception:
        emit("ISSUES UNAVAILABLE")
    return int(posted_p0 and env.get("GITHUB_EVENT_NAME") == "schedule")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--test-failed", action="store_true")
    args = parser.parse_args()
    try:
        data = json.loads(Path("fbot-run.json").read_text())
    except (ValueError, OSError):
        data = {}
    env = dict(os.environ)
    clock = Clock()
    try:
        github = GitHub(env)
        code = run(env, data, clock, github, Git(), coverage_reader=lambda: read_coverage(env, clock.now()), test=args.test, test_failed=args.test_failed)
    except Exception:
        print("OPS UNAVAILABLE")
        code = 1 if args.test or args.test_failed or env.get("GITHUB_EVENT_NAME") == "schedule" else 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
