"""Stdlib operations; dispatch, heartbeat, then deduplicated issues, then exit."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode
from .config import SEASON_ID, MINIBENCH_ID, enabled
from .llm import transport
from .runloop import should_dispatch, dispatch_wait
from .state import ALERTS, model_name, usage_number
from .targets import active, utc
from .types import Clock
from . import REASONS, config, schedule, coverage as coverage_v1, ledger, probe, hardening

P0 = frozenset({"CREDITS_EXHAUSTED", "NO_LLM_KEY", "NO_FALL_QUESTIONS", "INSTALL_FAILING",
                "HEARTBEAT_FAILED", "API_REJECTED", "SEASON_OVER",
                "GATE_BLOCKED", "RUN_FAILED", "POLL_FAILING", "COMMENT_FAILED", "TARGET_MISMATCH"})
STEPS = {
    "BUDGET_CAP_INVALID": 'Set BUDGET_CAP_USD (or BUDGET_CAP_OWN_USD for the own key) to the total dollars that key may spend, for example 80, or leave it blank.',
    "OWN_KEY_CONFIG_INVALID": "Set OWN_KEY_MODE to off, insurance or primary; default off.",
    "USING_OWN_KEY": "Season forecasts are using own credit. Check both remaining balances in the weekly status.",
    "TARGET_MISMATCH": "Check SEASON_ID, SEASON_SLUG and SEASON_END_UTC; MiniBench continues while the season is blocked.",
    "NUMERIC_FALLBACK": "Set NUMERIC_V1=false (2 min) and ask a helper to inspect Test Bot's numeric path.",
    "PRESET_CONFIG_INVALID": "Set MODEL_PRESET to auto, A, B or C and PRESET_RESERVE_USD to a nonnegative number (default 10). Invalid preset uses auto; invalid reserve uses 10.",
    "CREDITS_EXHAUSTED": "Assume no more credit is coming; submit another credit form yourself, in your own words; check the configured bridge or leave the bot stopped, or raise BUDGET_CAP_USD after a top-up.",
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
    "RESEARCH_UNAVAILABLE": "Check the AskNews secret names; see the resources link in RUNBOOK.md. If only web search fails, set RESEARCH2_ENABLED=false.",
    "SKIPS": "Read the skip counts; ask a helper about recurring misses.",
    "SCHEDULER_GAPS": "Check that a run started in the last 75 minutes. If none did, set CHAIN_ENABLED=true or check the outside timer's log, and start one run by hand now (Actions > Forecast on new AI tournament questions > Run workflow).",
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
        try:
            subprocess.run(["gh", "workflow", "run", "run_bot_on_tournament.yaml", "--ref", branch],
                           capture_output=True, check=True, timeout=45)
            return 'gh'
        except Exception:
            self.request('POST', f'/repos/{self.repo}/actions/workflows/run_bot_on_tournament.yaml/dispatches',
                         {'ref': branch})
            return 'rest'

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

    def runs(self, workflow=schedule.TOURNAMENT_WORKFLOW):
        data = self.request("GET", f"/repos/{self.repo}/actions/workflows/{workflow}/runs?per_page=100")
        return data["workflow_runs"]

    def issues_for_title(self, title, state='all'):
        query = f'repo:{self.repo} is:issue in:title "{title}"'
        if state != 'all':
            query += ' is:open'
        data = self.request('GET', '/search/issues?' + urlencode({'q': query, 'per_page': 100}))
        return [item for item in data.get('items', []) if item.get('title') == title]


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
    return coverage_v1.read_recent(env, now, send=send) or {}


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
    return {"counts": subset("counts", {"season", "minibench", "test", "poll_errors", "gate_blocks", "memo_hits", "preset_invalid", "budget_cap_invalid", "preset_reserve_invalid", "readback_unknown_group", "polls_season", "polls_minibench", "poll_errors_season", "poll_errors_minibench", "probe_flaky", "probe_models", "probe_unavailable_OPUS", "probe_unavailable_SOL", "probe_unavailable_FLASH", "probe_unavailable_CHEAP", "asknews_tried", "asknews_ok", "web_tried", "web_ok", "pages_tried", "pages_ok", "markets_tried", "markets_ok"}),
            "skips": subset("skips", REASONS - {"BUDGET_MINIBENCH", "DISABLED", "ALREADY_FORECAST"})}


def mention(env):
    value = env.get("ALERT_MENTION", "")
    return value if re.fullmatch(r"@[A-Za-z0-9-]{1,39}", value) else ""


def weekly_body(data, coverage_data, gap, root, enabled_bot, env, book=None, now=None, schedule_row=None):
    if not enabled_bot:
        return "Bot status: disabled. " + mention(env)
    rows = ["Bot weekly status", json.dumps(safe_counts(data), sort_keys=True)]
    for target in ("season", "minibench"):
        item = coverage_data.get(target)
        rows.append(target + ": coverage unknown" if item is None else
                    f"{target}: closed={item['closed']} forecasted={item['forecasted']} "
                    f"coverage={item['coverage']}% missed={item['missed']}")
    for name in ("credit_after", "credit_own", "limit", "research2_usd"):
        value = data.get(name)
        rows.append(f"{name}={value if isinstance(value, (int, float)) else 'unknown'}")
    tiers = [tier for tier in data.get("tiers", {}) if tier in ("A", "B", "C", "M", "BRIDGE")]
    rows.append("tiers=" + ",".join(tiers))
    try:
        date = (Path(root) / ".github" / "heartbeat.txt").read_text().strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = "unknown"
    except OSError:
        date = "unknown"
    rows += ["heartbeat=" + date, f"longest_run_gap_minutes={gap}", mention(env)]
    rows.extend(coverage_v1.weekly_rows(coverage_data, ledger=book, now=now,
                credit={'metaculus': data.get('credit_after'), 'own': data.get('credit_own')},
                comment_failed=len(data.get('comment_failed', [])), preset=data.get('model_preset'),
                research=coverage_v1.research_counts(data.get('counts'))))
    if schedule_row:
        rows.append(schedule_row)
    return "\n".join(rows)


def schedule_status(run_lists, now):
    counts, seen = {'schedule': 0, 'dispatch': 0}, set()
    for run_index, runs in enumerate(run_lists):
        for item_index, run in enumerate(runs):
            key = run.get('id', (run_index, item_index))
            if key in seen:
                continue
            seen.add(key)
            try:
                started = utc(run.get('run_started_at') or run.get('created_at'))
                if not now - timedelta(hours=24) <= started <= now:
                    continue
                event = run.get('event')
                if event == 'schedule':
                    counts['schedule'] += 1
                elif event == 'workflow_dispatch':
                    counts['dispatch'] += 1
            except Exception:
                pass
    gap = schedule.gap_minutes(run_lists, now)
    row = f"SCHEDULE runs_24h schedule={counts['schedule']} dispatch={counts['dispatch']} gap_max_min={gap if gap is not None else 'unknown'}"
    return gap, row


def vitals(data, env, dispatch_result, coverage_data, now=None, alerts=None):
    """Build a new record; never forward arbitrary snapshot or environment keys."""
    def mapping(value):
        return value if isinstance(value, dict) else {}
    def count(value):
        number = usage_number(value)
        return int(number) if number is not None and number == int(number) else 0
    def money(value):
        return round(value, 2) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    def whole(value):
        return int(value) if isinstance(value, str) and re.fullmatch(r'[0-9]+', value.strip()) else None
    def named(value, names, default):
        return value if isinstance(value, str) and value in names else default
    def code(value):
        return value if isinstance(value, str) and re.fullmatch(r'http_[0-9]{3}|[A-Za-z]{1,40}', value) else None
    def target_counts(key):
        source = mapping(data.get(key))
        return {target:count(source.get(target)) for target in targets}
    data = mapping(data)
    targets = ('season', 'minibench')
    end = utc(now if now is not None else Clock().now())
    try:
        raw_start = env.get('JOB_START')
        start = datetime.fromtimestamp(float(raw_start), timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            start = utc(raw_start) if isinstance(raw_start, str) else None
        except (ValueError, TypeError, AttributeError):
            start = None
    q = {}
    for target in targets:
        q[target] = {field:target_counts(source)[target] for field,source in (
            ('seen','seen'), ('attempted','attempted_by_target'),
            ('posted','posted_by_target'), ('commented','commented_by_target'))}
        skips = mapping(mapping(data.get('skips_by_target')).get(target))
        q[target]['skip'] = {key:count(skips[key]) for key in sorted(REASONS) if key in skips}
    models = {}
    for raw_name, values in mapping(data.get('model_calls')).items():
        name = model_name(raw_name)
        row = models.setdefault(name, dict(calls=0,ok=0,err=0,in_tok=0,out_tok=0,usd=0))
        for field in row:
            value = mapping(values).get(field)
            row[field] += (usage_number(value) or 0) if field in ('in_tok','out_tok','usd') else count(value)
    for row in models.values():
        row['usd'] = money(row['usd'])
    research = mapping(data.get('research_calls'))
    errors = mapping(research.get('err'))
    parser = mapping(data.get('parser_calls'))
    opened = mapping(data.get('open'))
    open_values = {}
    for target in targets:
        value = opened.get(target)
        if usage_number(value) is not None:
            open_values[target] = value
        elif isinstance(value, str) and re.fullmatch(r'unknown:(?:http_[0-9]{3}|shape|cap|error)', value):
            open_values[target] = value
        else:
            open_values[target] = 'unknown:error'
    tiers = mapping(data.get('tiers_by_target'))
    counts = mapping(data.get('counts'))
    coverage_data = mapping(coverage_data)
    chain = mapping(dispatch_result)
    record = {
        'v':1, 'rel':config.VERSION,
        'run':whole(env.get('GITHUB_RUN_NUMBER')), 'att':whole(env.get('GITHUB_RUN_ATTEMPT')),
        'ev':named(env.get('GITHUB_EVENT_NAME'), ('schedule','workflow_dispatch'), 'other'),
        't0':start.isoformat().replace('+00:00','Z') if start else None,
        't1':end.isoformat().replace('+00:00','Z'),
        'dur_s':round((end-start).total_seconds(), 2) if start else None,
        'loop_min':usage_number(data.get('loop_min')), 'poll_min':usage_number(data.get('poll_min')),
        'polls':target_counts('polls'), 'poll_err':target_counts('poll_errors'), 'open':open_values,
        'q':q,
        'tiers':{target:{key:count(mapping(tiers.get(target))[key]) for key in config.TIERS if key in mapping(tiers.get(target))} for target in targets},
        'preset':named(data.get('model_preset'), ('auto','A','B','C'), 'auto'),
        'models':dict(sorted(models.items())),
        'parser':{key:count(parser.get(key)) for key in ('calls','err')},
        'research':{**{key:count(research.get(key)) for key in ('calls','ok','none')},
                    'err':{key:count(errors[key]) for key in sorted(errors) if code(key)}},
        'credit':{'src':named(data.get('credit_source'), ('key_limit','cap_minus_usage'), 'unknown'),
                  **{key:money(data.get(source)) for key,source in (
                      ('before','credit_before'), ('after','credit_after'), ('limit','limit'),
                      ('spend','observed_spend'), ('per_attempt','spend_per_question'))}},
        'gate_blocks':count(counts.get('gate_blocks')), 'memo_hits':count(counts.get('memo_hits')),
        'cov':{target:{key:money(mapping(coverage_data[target]).get(key)) for key in ('closed','forecasted')} for target in targets if target in coverage_data},
        'alerts':sorted(key for key in (data.get('alerts', []) if alerts is None else alerts) if key in ALERTS),
        'next':{'dispatched':int(chain.get('dispatched') == 1),
                'via':named(chain.get('via'), ('gh','rest'), 'none'),
                'err':chain['error'] if isinstance(chain.get('error'), str) and re.fullmatch(r'[A-Za-z]{1,40}', chain['error']) else '-'},
        'outcome':{key:named(env.get(source), ('success','failure','cancelled','skipped'), 'unknown')
                   for key,source in (('install','INSTALL_OUTCOME'), ('run','RUN_OUTCOME'))},
    }
    return record  # VITALS_WHITELIST


def escape_annotation(value):
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def emit_vitals(data, env, dispatch_result, coverage_data, clock, alerts, emit):
    try:
        record = vitals(data, env, dispatch_result, coverage_data, now=clock.now(), alerts=alerts)
        compact = json.dumps(record, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        summary = env.get('GITHUB_STEP_SUMMARY')
        if summary:
            table = '\n| Sextant vitals | Count |\n|---|---:|\n'
            for target, values in record['q'].items():
                table += f"| {target} posted / attempted | {values['posted']} / {values['attempted']} |\n"
            table += f"| Model HTTP calls | {sum(row['calls'] for row in record['models'].values())} |\n"
            table += f"| Spend USD | {record['credit']['spend']} |\n"
            table += f"| Next dispatched | {record['next']['dispatched']} |\n"
            with Path(summary).open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(table)
        emit('::notice title=SEXTANT_VITALS v1::' + escape_annotation(compact))
        emit('VITALS ' + compact)
    except Exception:
        try:
            emit('VITALS UNAVAILABLE')
        except Exception:
            pass


def run(env, data, clock, github, git, root=".", coverage_reader=None, emit=print, test=False, test_failed=False, book=None):
    missing_state = not isinstance(data, dict) or not data
    data = data if isinstance(data, dict) else {}
    alerts = set()
    dispatched, route, error_class = 0, 'none', '-'
    coverage_data = {}
    def finish(code):
        emit_vitals(data, env, {'dispatched':dispatched,'via':route,'error':error_class}, coverage_data, clock, alerts, emit)
        return code
    if test or test_failed:
        emit('CHAIN dispatched=0 via=none error=-')
        try:
            message = "test run failed; inspect install/run steps " if test_failed else "test run ok "
            issue_once(github, "Bot weekly status", message + mention(env),
                       "bot-status", clock.now(), -1)
            return finish(int(test_failed))
        except ApiError as error:
            if error.status == 410:
                emit("ISSUES DISABLED")
            return finish(1)
    enabled_bot = enabled(env, "BOT_ENABLED")
    if should_dispatch(env, clock.now()):
        try:
            wait = dispatch_wait(float(env.get("JOB_START", clock.now().timestamp())), clock.now())
            if wait:
                clock.sleep(wait)
            if should_dispatch(env, clock.now()):
                route = github.dispatch(github.default_branch()) or 'gh'
                dispatched = 1
        except Exception as error:
            error_class = type(error).__name__
            alerts.add("SCHEDULER_GAPS")
    emit(f'CHAIN dispatched={dispatched} via={route} error={error_class}')
    try:
        if hardening.season_open(clock.now(), env):
            heartbeat(git, clock, root)
    except Exception:
        alerts.add("HEARTBEAT_FAILED")
    if not hardening.season_open(clock.now(), env):
        alerts.add("SEASON_OVER")
    coverage_data, gap = {}, None
    schedule_row = 'SCHEDULE runs_24h schedule=0 dispatch=0 gap_max_min=unknown'
    try:
        issues = github.issues()
    except ApiError as error:
        if error.status == 410:
            emit('ISSUES DISABLED')
            return finish(1)
        issues = []
    except Exception:
        issues = []
    due = coverage_v1.weekly_due(issues, github.latest, clock.now())
    if enabled_bot:
        alerts.update(key for key in data.get("alerts", []) if key in ALERTS)
        if env.get("INSTALL_OUTCOME") == "failure":
            alerts.add("INSTALL_FAILING")
        if env.get("RUN_OUTCOME") in ("failure", "cancelled") or (env.get("RUN_OUTCOME") == "success" and missing_state):
            alerts.add("RUN_FAILED")
        if sum(safe_counts(data)["skips"].values()):
            alerts.add("SKIPS")
        try:
            tournament_runs = github.runs(schedule.TOURNAMENT_WORKFLOW)
            try:
                timer_runs = github.runs(schedule.TIMER_WORKFLOW)
            except ApiError as error:
                if error.status != 404:
                    raise
                timer_runs = []
            gap, schedule_row = schedule_status([tournament_runs, timer_runs], clock.now())
            if gap is not None and gap > 75:
                alerts.add("SCHEDULER_GAPS")
        except Exception:
            gap = None
        try:
            if due and coverage_reader:
                coverage_data = coverage_reader()
        except Exception:
            coverage_data = {}
            emit('MODULE without=coverage')
        for target, item in coverage_data.items():
            if item is not None:
                if item.get("coverage") is not None and item["coverage"] < 95:
                    alerts.add("SKIPS")
                if target == "season" and item.get("closed") == 0 and clock.now() > utc("2026-10-12T23:59:59Z"):
                    alerts.add("NO_FALL_QUESTIONS")
    else:
        emit("DISABLED")
        alerts.intersection_update({"HEARTBEAT_FAILED", "SEASON_OVER"})
    emit(schedule_row)
    posted_p0 = False
    try:
        for key in sorted(alerts):
            body = f"{key}\n{json.dumps(safe_counts(data), sort_keys=True)}\nRUNBOOK.md: {STEPS[key]}\n{mention(env)}"
            if key == 'MODEL_UNAVAILABLE':
                body += '\n' + '\n'.join(probe.alert_rows(data))
            if key == 'SEASON_OVER':
                title = '[BOT ALERT] SEASON_OVER'
                lookup = getattr(github, 'issues_for_title', None)
                previous = lookup(title, state='all') if lookup else [i for i in issues if i.get('title') == title]
                sent = False
                if not previous:
                    github.create(title, body, 'bot-alert')
                    sent = True
            else:
                prior = issues
                if key == 'USING_OWN_KEY':
                    lookup = getattr(github, 'issues_for_title', None)
                    prior = lookup('[BOT ALERT] ' + key, state='all') if lookup else issues
                sent = issue_once(github, "[BOT ALERT] " + key, body, "bot-alert", clock.now(), 168 if key == 'USING_OWN_KEY' else 24, prior)
            posted_p0 = posted_p0 or (sent and key in P0)
        issue_once(github, "Bot weekly status",
                   weekly_body(data, coverage_data, gap, root, enabled_bot, env, book, clock.now(), schedule_row),
                   "bot-status", clock.now(), 6.5 * 24, issues)
    except ApiError as error:
        if error.status == 410:
            emit("ISSUES DISABLED")
            return finish(1)
        emit("ISSUES UNAVAILABLE")
    except Exception:
        emit("ISSUES UNAVAILABLE")
    return finish(int(posted_p0 and schedule.automatic_run(env)))


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
        code = run(env, data, clock, github, Git(), coverage_reader=lambda: coverage_v1.read_recent(env, clock.now()),
                   test=args.test, test_failed=args.test_failed, book=ledger.load())
    except Exception:
        print("OPS UNAVAILABLE")
        code = 1 if args.test or args.test_failed or schedule.automatic_run(env) else 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
