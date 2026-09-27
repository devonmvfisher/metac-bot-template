"""Run deliberately broken copies serially under the explicitly supplied temp root."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

MUTATIONS = [
    ("binary_cap", "fbot/config.py", "BINARY_CAP = 0.02", "BINARY_CAP = 0.01", "tests.test_core"),
    ("cdf_min_step", "fbot/validate.py", "    minimum_step = 0.01 / n\n", "    minimum_step = 0.01 / n * 0.5\n", "tests.test_review"),
    ("mc_floor", "fbot/config.py", "MC_FLOOR = 0.01", "MC_FLOOR = 0", "tests.test_core"),
    ("season_target", "fbot/config.py", "SEASON_ID = 33121", "SEASON_ID = 33122", "tests.test_release"),
    ("misread_removed", "fbot/guards.py", "def misread(text):", "MISREAD_PATTERNS = ()\n\ndef misread(text):", "tests.test_core"),
    ("quota_402_removed", "fbot/llm.py", "if status == 402 or", "if False or", "tests.test_llm_budget"),
    ("partial_after_exhaustion", "fbot/pipeline.py", "    if exhausted:\n", "    if exhausted and not values:\n", "tests.test_pipeline"),
    ("urgent_tier_removed", "fbot/runloop.py", "urgent = until_close < 12 * 60", "urgent = False", "tests.test_runloop"),
    ("heartbeat_delayed", "fbot/ops.py", "<= 25 * 86400", "<= 250 * 86400", "tests.test_ops"),
    ("comment_public", "fbot/postgate.py", "is_private=True", "is_private=False", "tests.test_integrity"),
    ("test_false_green", "fbot/ops.py", "return int(test_failed)", "return 0", "tests.test_delivery"),
    ("concurrency_uncapped", "fbot/pipeline.py", "PriorityLimiter(5)", "PriorityLimiter(50)", "tests.test_delivery"),

    ("m13_R01_step_tolerance", "fbot/validate.py", "    minimum_step = 0.01 / n\n", "    minimum_step = 0.01 / n * 0.9\n", "tests.test_review"),
    ("m14_R02_exact_comparison", "fbot/postgate.py", "    def matches(question, actual, wanted):", "    def matches(question, actual, wanted):\n        if actual != wanted:\n            return False", "tests.test_review"),
    ("m15_R03_unrecorded_gate", "fbot/postgate.py", "            self.state.skip(registered[0], reason)", "            pass", "tests.test_review"),
    ("m16_R04_retry_memo_removed", "main.py", "if self.f_state.failures[question.qid] >= 2:", "if False:", "tests.test_review"),
    ("m17_R05_gate_reread_removed", "fbot/postgate.py", "found = self.read(question)", "found = False", "tests.test_review"),
    ("m18_R06_403_removed", "fbot/llm.py", '(status == 403 and "key limit exceeded" in str(error.get("message", "")).lower())', "False", "tests.test_review"),
    ("m19_R07_old_model", "fbot/config.py", '"C": (SOL + FLASH,)', '"C": (SOL + ("google/gemini-3.1-pro-preview",),)', "tests.test_review"),
    ("m20_R09_run_failure_as_skips", "fbot/ops.py", 'alerts.add("RUN_FAILED")', 'alerts.add("SKIPS")', "tests.test_review"),
    ("m21_R10_comment_alert_removed", "fbot/postgate.py", 'self.state.alert("COMMENT_FAILED")', "pass", "tests.test_review"),
    ("m22_R12_display_raise", "fbot/comment.py", "    remaining = entries[len(first):]", '    if len(first) < len(entries):\n        raise SkipQuestion("INVALID_OUTPUT")\n    remaining = entries[len(first):]', "tests.test_review"),
    ("m23_R14_old_path_pattern", "fbot/comment.py", r'(?<![A-Za-z])[A-Za-z]:[\\/](?!/)[^\s]*|(?<![\w.:/-])/(?:home|Users)/[^\s]*', r'(?i)(?:[a-z]:[\\/]|/home/|/Users/)[^\s]+', "tests.test_review"),
    ("m24_R21_FIFO_limiter", "fbot/priority.py", "key = (close, question.qid)", "key = (0, 0)", "tests.test_review_v02"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    temp = Path(args.tmp).resolve()
    if not temp.is_dir():
        raise SystemExit("Create the job temp folder first.")
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1")
    records = []
    for name, file, old, new, suite in MUTATIONS:
        container = Path(tempfile.mkdtemp(prefix="fbot-mut-" + name + "-", dir=temp)).resolve()
        if not container.is_relative_to(temp):
            raise SystemExit("Mutation path escaped temp root.")
        fork = container / "fork"
        shutil.copytree(root, fork, ignore=shutil.ignore_patterns("__pycache__"))
        path = fork / file
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit("Mutation anchor is not unique: " + name)
        changed = text.replace(old, new, 1)
        ast.parse(changed, feature_version=(3, 11))
        path.write_text(changed, encoding="utf-8", newline="\n")
        completed = subprocess.run([sys.executable, "-B", "tests/run_offline.py", suite], cwd=fork,
                                   env=env, capture_output=True, text=True, timeout=50)
        output = completed.stdout + completed.stderr
        failed = re.findall(r"^(?:FAIL|ERROR): ([^\n]+)", output, re.M)
        killed = completed.returncode != 0 and bool(failed)
        records.append({"mutation": name, "file": file, "suite": suite, "killed": killed,
                        "returncode": completed.returncode, "failed_checks": failed})
        print(name + ": " + ("KILLED" if killed else "SURVIVED"), flush=True)
    Path(args.out).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Mutations killed: {sum(r['killed'] for r in records)}/{len(records)}")
    raise SystemExit(not all(record["killed"] for record in records))


if __name__ == "__main__":
    main()
