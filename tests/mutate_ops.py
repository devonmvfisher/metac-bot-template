"""Run one deliberately broken copy at a time under an explicit temporary root."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

MUTATIONS = [{'id': 'P01',
  'file': 'fbot/schedule.py',
  'old': 'heapq.heappush(self._queue, (key, sequence, key, future))',
  'new': 'heapq.heappush(self._queue, (sequence, sequence, key, future))',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P02',
  'file': 'fbot/schedule.py',
  'old': 'self._limit = limit',
  'new': 'self._limit = 1',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P03',
  'file': 'fbot/schedule.py',
  'old': 'while self._queue and self._active < self._limit:',
  'new': 'while self._queue:',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P04',
  'file': 'fbot/schedule.py',
  'old': '            if future.cancelled():\n                continue\n',
  'new': '            if future.cancelled():\n                pass\n',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P05',
  'file': 'fbot/schedule.py',
  'old': 'if job_start is not None:',
  'new': 'if False:',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P06',
  'file': 'fbot/schedule.py',
  'old': '    if concurrent:\n',
  'new': '    if False:\n',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P07',
  'file': 'fbot/schedule.py',
  'old': 'FAST_PATH_SECONDS = 720',
  'new': 'FAST_PATH_SECONDS = 240',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P08',
  'file': 'fbot/schedule.py',
  'old': 'seconds = min(QUESTION_CAP_SECONDS, JOB_CAP_SECONDS - (now - targets.utc(job_started)).total_seconds())',
  'new': 'seconds = QUESTION_CAP_SECONDS',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P09',
  'file': 'fbot/schedule.py',
  'old': '                except Exception as error:\n',
  'new': '                except Exception as error:\n                    raise\n',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P10',
  'file': 'fbot/schedule.py',
  'old': 'env.get("GITHUB_EVENT_NAME") == "schedule" or env.get("FBOT_TRIGGER", "").strip().lower() == "timer"',
  'new': 'env.get("GITHUB_EVENT_NAME") == "schedule"',
  'suite': 'tests.test_schedule',
  'should': False},
 {'id': 'P11',
  'file': '.github/workflows/run_bot_on_timer.yaml',
  'old': '  group: fbot-post-tournament\n',
  'new': '  group: ${{ github.workflow }}\n',
  'suite': 'tests.test_timer_workflow',
  'should': False},
 {'id': 'P12',
  'file': '.github/workflows/run_bot_on_timer.yaml',
  'old': '  workflow_dispatch:\n',
  'new': '  workflow_dispatch:\n  schedule:\n    - cron: "*/15 * * * *"\n',
  'suite': 'tests.test_timer_workflow',
  'should': False},
 {'id': 'P13',
  'file': 'fbot/coverage.py',
  'old': '("order_by", "-scheduled_close_time")',
  'new': '("unused_order", "-scheduled_close_time")',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P14',
  'file': 'fbot/coverage.py',
  'old': 'if previous is not None and closed > previous:',
  'new': 'if False:',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P15',
  'file': 'fbot/coverage.py',
  'old': 'group = post.get("group_of_questions")',
  'new': 'group = None',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P16',
  'file': 'fbot/coverage.py',
  'old': '        if not isinstance(forecasts, dict):\n            return None',
  'new': '        if not isinstance(forecasts, dict):\n            return False',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P17',
  'file': 'fbot/coverage.py',
  'old': '        for name in _SOURCES:\n',
  'new': '        for name in research:\n',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P18',
  'file': 'fbot/probe.py',
  'old': 'FLAKY = frozenset({0, 408, 429, 500, 502, 503, 504})',
  'new': 'FLAKY = frozenset({0, 408})',
  'suite': 'tests.test_probe',
  'should': False},
 {'id': 'P19',
  'file': 'fbot/probe.py',
  'old': '                    if status == 402:\n',
  'new': '                    if False:\n',
  'suite': 'tests.test_probe',
  'should': False},
 {'id': 'P20',
  'file': 'fbot/probe.py',
  'old': 'return saved != now.date().isoformat() if saved else now.hour == PROBE_HOUR_UTC',
  'new': 'return True if saved else now.hour == PROBE_HOUR_UTC',
  'suite': 'tests.test_probe',
  'should': False},
 {'id': 'P21',
  'file': 'fbot/ledger.py',
  'old': '    return clean\n',
  'new': '    clean.update({key: value for key, value in raw.items() if key not in KEYS})\n    return clean\n',
  'suite': 'tests.test_ledger',
  'should': False},
 {'id': 'P22',
  'file': 'fbot/ledger.py',
  'old': 'count >= min_questions else None',
  'new': 'count >= 1 else None',
  'suite': 'tests.test_ledger',
  'should': False},
 {'id': 'P23',
  'file': 'fbot/ledger.py',
  'old': '    except Exception:\n        logger.info("LEDGER status=corrupt")',
  'new': '    except Exception:\n        raise',
  'suite': 'tests.test_ledger',
  'should': False},
 {'id': 'P24',
  'file': 'fbot/prompts_v1.py',
  'old': '                  NOT_YET, BEFORE_DATE))',
  'new': '                  NOT_YET))',
  'suite': 'tests.test_prompts_v1',
  'should': False},
 {'id': 'P25',
  'file': 'fbot/prompts_v1.py',
  'old': 'parts.extend("- " + option for option in options)',
  'new': 'parts.extend("- " + option for option in options[:-1])',
  'suite': 'tests.test_prompts_v1',
  'should': False},
 {'id': 'P26',
  'file': 'fbot/prompts_v1.py',
  'old': '            parts.append(SCALE_0_1.format(**bounds))\n'
         '        elif _percent(unit) and 0 <= lower and 1 < upper <= 100:\n'
         '            parts.append(SCALE_0_100.format(**bounds))',
  'new': '            parts.append(SCALE_0_100.format(**bounds))\n'
         '        elif _percent(unit) and 0 <= lower and 1 < upper <= 100:\n'
         '            parts.append(SCALE_0_1.format(**bounds))',
  'suite': 'tests.test_prompts_v1',
  'should': False},
 {'id': 'P27',
  'file': 'fbot/misread.py',
  'old': 'not negated(text, match.start())',
  'new': 'True',
  'suite': 'tests.test_misread',
  'should': False},
 {'id': 'P28',
  'file': 'fbot/misread.py',
  'old': 'return bool(matches(text)) and extreme(question.kind, value)',
  'new': 'return bool(matches(text))',
  'suite': 'tests.test_misread',
  'should': False},
 {'id': 'P29',
  'file': 'fbot/hardening.py',
  'old': 'per_run = 1500 if len(rationales) >= 4 else 2500',
  'new': 'per_run = 2500',
  'suite': 'tests.test_hardening',
  'should': True},
 {'id': 'P30',
  'file': 'fbot/hardening.py',
  'old': '"[BOT ALERT] SEASON_OVER" not in issue_titles',
  'new': 'True',
  'suite': 'tests.test_hardening',
  'should': True},
 {'id': 'P31',
  'file': 'fbot/coverage.py',
  'old': '("statuses", "closed"), ("statuses", "resolved")',
  'new': '("statuses", "closed,resolved")',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P32',
  'file': 'fbot/probe.py',
  'old': '            client.one(model_id, "Reply OK", probe=True,\n'
         '                       deadline=client.clock.monotonic() + seconds, timeout=seconds)',
  'new': '            client.one(model_id, "Reply OK", probe=True)',
  'suite': 'tests.test_probe',
  'should': False},
 {'id': 'P33',
  'file': 'fbot/probe.py',
  'old': 'return saved != now.date().isoformat() if saved else now.hour == PROBE_HOUR_UTC',
  'new': 'return saved != now.date().isoformat() if saved else True',
  'suite': 'tests.test_probe',
  'should': False},
 {'id': 'P34',
  'file': 'fbot/prompts_v1.py',
  'old': 'if research_blocks is None:',
  'new': 'if True:',
  'suite': 'tests.test_prompts_v1',
  'should': False},
 {'id': 'P35',
  'file': 'fbot/coverage.py',
  'old': '                    if not page:\n                        break',
  'new': '                    if not page:\n                        continue',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'P03-fuzz',
  'file': 'fbot/schedule.py',
  'old': 'while self._queue and self._active < self._limit:',
  'new': 'while self._queue:',
  'suite': 'tests.test_fuzz',
  'should': False},
 {'id': 'P16-fuzz',
  'file': 'fbot/coverage.py',
  'old': '        if not isinstance(forecasts, dict):\n            return None',
  'new': '        if not isinstance(forecasts, dict):\n            return False',
  'suite': 'tests.test_fuzz',
  'should': False},
 {'id': 'E01',
  'file': 'fbot/schedule.py',
  'old': '            if not future.cancelled():\n                self._active -= 1',
  'new': '            if not future.cancelled():\n                self._active -= 0',
  'suite': 'tests.test_ops_extra',
  'should': False},
 {'id': 'E02',
  'file': 'fbot/probe.py',
  'old': '                if exhausted:\n                    status = None',
  'new': '                if exhausted:\n                    status = statuses.get(model)',
  'suite': 'tests.test_ops_extra',
  'should': False},
 {'id': 'REV_POLL_P0',
  'file': 'fbot/schedule.py',
  'old': '        check()',
  'new': '        pass',
  'suite': 'tests.test_v1_ops',
  'should': False},
 {'id': 'REV_FORFEITS_UNKNOWN',
  'file': 'fbot/coverage.py',
  'old': '        rows.append(f"forfeits_status_unknown season={unknown[\'season\']} minibench={unknown[\'minibench\']}")',
  'new': '        pass',
  'suite': 'tests.test_v1_ops',
  'should': False},
 {'id': 'REV_EMPTY_END',
  'file': 'fbot/hardening.py',
  'old': 'if str(env.get("SEASON_END_UTC") or "").strip():',
  'new': 'if "SEASON_END_UTC" in env:',
  'suite': 'tests.test_v1_ops',
  'should': False},
 {'id': 'R31_WITH_CP',
  'file': 'fbot/coverage.py',
  'old': '("with_cp", "true"), ',
  'new': '',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'R31_OPEN_SKIP',
  'file': 'fbot/coverage.py',
  'old': 'if question.get("status") in ("upcoming", "open"):',
  'new': 'if False:',
  'suite': 'tests.test_coverage_v1',
  'should': False},
 {'id': 'R31_SEASON_START',
  'file': 'fbot/coverage.py',
  'old': 'cutoff = max(now - timedelta(days=days), utc(SEASON_START))',
  'new': 'cutoff = now - timedelta(days=days)',
  'suite': 'tests.test_coverage_v1',
  'should': False}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    temporary = Path(args.tmp).resolve()
    if not temporary.is_dir() or Path(args.out).exists():
        raise SystemExit("Need an existing temporary folder and a fresh report path")
    env = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    selected = [row for row in MUTATIONS if args.only is None or row["id"] in args.only]
    if args.only and set(args.only) != {row["id"] for row in selected}:
        raise SystemExit("Unknown mutation id")
    results = []
    for row in selected:
        record = dict(row)
        source = root / row["file"]
        if not source.exists() and row["should"]:
            record.update(outcome="NOT BUILT", failed_checks=[])
        else:
            text = source.read_text(encoding="utf-8")
            if text.count(row["old"]) != 1:
                raise SystemExit("Nonunique mutation anchor: " + row["id"])
            changed = text.replace(row["old"], row["new"], 1)
            if source.suffix == ".py":
                ast.parse(changed, feature_version=(3, 11))
            container = Path(tempfile.mkdtemp(prefix="ops-mut-" + row["id"] + "-", dir=temporary)).resolve()
            if not container.is_relative_to(temporary):
                raise SystemExit("Temporary path escaped root")
            fork = container / "fork"
            shutil.copytree(root, fork, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (fork / row["file"]).write_text(changed, encoding="utf-8", newline="\n")
            started = time.perf_counter()
            try:
                run = subprocess.run([sys.executable, "-B", "tests/run_offline.py", row["suite"]],
                                     cwd=fork, env=env, capture_output=True, text=True, encoding="utf-8", timeout=50)
                output = run.stdout + run.stderr
                failed = re.findall(r"^(?:FAIL|ERROR): ([^\n]+)", output, re.M)
                genuine = failed and not any("_FailedTest" in failure for failure in failed)
                record.update(outcome="KILLED" if run.returncode != 0 and genuine else "SURVIVED",
                              returncode=run.returncode, failed_checks=failed)
            except subprocess.TimeoutExpired:
                record.update(outcome="TIMEOUT", returncode=None, failed_checks=[])
            record.update(seconds=round(time.perf_counter() - started, 4),
                          original_sha256=hashlib.sha256(text.encode()).hexdigest(),
                          mutant_sha256=hashlib.sha256(changed.encode()).hexdigest())
        results.append(record)
        print(row["id"] + ": " + record["outcome"], flush=True)
    with Path(args.out).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(results, stream, indent=2, allow_nan=False)
        stream.write("\n")
    killed = sum(row["outcome"] == "KILLED" for row in results)
    print(f"Mutations killed: {killed}/{len(results)}")
    return 0 if all(row["outcome"] in ("KILLED", "NOT BUILT") for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
