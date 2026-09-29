from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from fbot import ops
from .fakes import FakeClock, FakeGit, FakeGitHub


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-10-20T12:00:00Z")
        self.root = Path(tempfile.mkdtemp(prefix="fbot-ops-"))
        self.addCleanup(__import__("shutil").rmtree, self.root)
        self.events = []
        self.git = FakeGit(self.clock.now(), events=self.events)
        self.github = FakeGitHub(self.clock.now(), events=self.events)
        self.env = {"BOT_ENABLED": "true", "CHAIN_ENABLED": "true", "GITHUB_EVENT_NAME": "schedule",
                    "JOB_START": str(self.clock.now().timestamp()-3000), "ALERT_MENTION": "@example"}

    def run_ops(self, data=None, coverage=None, **kwargs):
        return ops.run(self.env, data or {}, self.clock, self.github, self.git, self.root,
                       coverage_reader=lambda: coverage or {}, **kwargs)

    def test_heartbeat_24_vs_26_exact_commands(self):
        self.assertFalse(ops.heartbeat(self.git, self.clock, self.root))
        self.assertEqual(len(self.git.calls), 1)
        self.git = FakeGit(self.clock.now(), age=26)
        self.assertTrue(ops.heartbeat(self.git, self.clock, self.root))
        commands = self.git.calls
        self.assertEqual(commands[1], ("gh", "auth", "setup-git"))
        self.assertEqual(commands[2], ("git", "add", ".github/heartbeat.txt"))
        self.assertEqual(commands[3][-3:], ("commit", "-m", "heartbeat: keep schedule alive"))
        self.assertEqual(commands[4:], [("git", "pull", "--rebase"), ("git", "push")])
        self.assertEqual((self.root / ".github/heartbeat.txt").read_text().strip(), "2026-10-20")
        for command in commands:
            self.assertNotIn("-A", command)
            self.assertNotIn("-a", command)
            self.assertNotEqual(command, ("git", "add", "."))

    def test_alert_dedupe(self):
        title = "[BOT ALERT] CREDITS_LOW"
        self.assertTrue(ops.issue_once(self.github, title, "counts", "bot-alert", self.clock.now(), 24))
        self.assertFalse(ops.issue_once(self.github, title, "counts", "bot-alert", self.clock.now(), 24))
        self.clock.sleep(24 * 3600 + 1)
        self.assertTrue(ops.issue_once(self.github, title, "counts", "bot-alert", self.clock.now(), 24))
        self.assertEqual([w[0] for w in self.github.writes], ["create", "comment"])

    def test_410_exits_after_dispatch_and_heartbeat(self):
        self.github.disabled = True
        output = []
        self.env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        code = self.run_ops(emit=output.append)
        self.assertEqual(code, 1)
        self.assertEqual(self.events[:3], ["dispatch", "heartbeat", "issues"])
        self.assertIn("ISSUES DISABLED", output)

    def test_schedule_only_failure_after_ops_and_no_duplicate_failure(self):
        data = {"alerts": ["CREDITS_EXHAUSTED"]}
        self.assertEqual(self.run_ops(data), 1)
        self.assertEqual(self.events[:3], ["dispatch", "heartbeat", "issues"])
        self.assertEqual(self.run_ops(data), 0)
        self.github = FakeGitHub(self.clock.now())
        self.env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.assertEqual(self.run_ops(data), 0)

    def test_body_whitelist_rejects_values_and_fake_secret(self):
        data = {"alerts": ["API_REJECTED", "FAKEKEY123"], "probability": .376543,
                "rationale": "PRIVATE_REASONING", "counts": {"season": 2, "FAKEKEY123": 33},
                "skips": {"INVALID_OUTPUT": 1, "FAKEKEY123": 3}, "credit_after": 5}
        self.run_ops(data)
        body = json.dumps(self.github.writes)
        for text in ("FAKEKEY123", "PRIVATE_REASONING", "0.376543"):
            self.assertNotIn(text, body)
        self.assertIn("@example", body)

    def test_weekly_6_5_days_and_coverage_alert(self):
        coverage = {"season": {"closed": 20, "forecasted": 18, "coverage": 90, "missed": [31, 32]}}
        self.run_ops(coverage=coverage)
        initial = [w for w in self.github.writes if w[0] == "create"]
        self.assertTrue(any(w[1] == "[BOT ALERT] SKIPS" for w in initial))
        weekly = [w for w in initial if w[1] == "Bot weekly status"]
        self.assertEqual(len(weekly), 1)
        self.assertIn("coverage=90%", weekly[0][2])
        self.clock.sleep(6 * 86400)
        self.run_ops(coverage=coverage)
        number = next(x["number"] for x in self.github.entries if x["title"] == "Bot weekly status")
        self.assertFalse(any(w[0] == "comment" and w[1] == number for w in self.github.writes))
        self.clock.sleep(86400)
        self.run_ops(coverage=coverage)
        self.assertTrue(any(w[0] == "comment" and w[1] == number for w in self.github.writes))

    def test_coverage_math_and_unknown(self):
        posts = [{"id": 201+i, "question": {"id": 101+i, "scheduled_close_time": "2026-10-19T00:00:00Z",
                 "my_forecasts": {"latest": {"forecast_values": [.2, .8]}} if i != 3 else {}}} for i in range(5)]
        result = ops.coverage(posts, self.clock.now())
        self.assertEqual(result, {"closed": 5, "forecasted": 4, "coverage": 80, "missed": [104]})
        data = ops.read_coverage({"METACULUS_TOKEN": "FAKEKEY123"}, self.clock.now(),
                                 send=lambda *args: (500, {}))
        self.assertEqual(data, {"season": None, "minibench": None})
        self.run_ops(coverage=data)
        body = json.dumps(self.github.writes)
        self.assertIn("coverage unknown", body)
        self.assertNotIn("NO_FALL_QUESTIONS", body)

    def test_gaps_ignore_cancelled_and_overlapping(self):
        now = self.clock.now()
        def run(start, end, conclusion="success"):
            return {"run_started_at": (now-timedelta(minutes=start)).isoformat(),
                    "updated_at": (now-timedelta(minutes=end)).isoformat(), "conclusion": conclusion}
        runs = [run(240, 200), run(180, 175, "cancelled"), run(110, 70), run(100, 60), run(20, 0)]
        self.assertEqual(ops.longest_gap(runs, now), 90)
        self.github.run_entries = runs
        self.run_ops()
        self.assertTrue(any(w[0] == "create" and w[1] == "[BOT ALERT] SCHEDULER_GAPS" for w in self.github.writes))

    def test_zero_closed_after_oct_12_only(self):
        empty = {"season": {"closed": 0, "forecasted": 0, "coverage": None, "missed": []}}
        self.clock = FakeClock("2026-10-12T12:00:00Z")
        self.run_ops(coverage=empty)
        self.assertNotIn("NO_FALL_QUESTIONS", json.dumps(self.github.writes))
        self.clock = FakeClock("2026-10-13T00:00:00Z")
        self.github = FakeGitHub(self.clock.now())  # L2: this independent case must be due for weekly coverage.
        self.run_ops(coverage=empty)
        self.assertIn("NO_FALL_QUESTIONS", json.dumps(self.github.writes))

    def test_disabled_heartbeat_season_over_only(self):
        self.env = {}
        self.clock = FakeClock("2027-01-07T00:00:00Z")
        messages = []
        self.run_ops({"alerts": ["CREDITS_EXHAUSTED", "MODEL_UNAVAILABLE"], "skips": {"TOO_LATE": 4}}, emit=messages.append)
        self.assertIn("DISABLED", messages)
        self.assertNotIn("dispatch", self.events)
        text = json.dumps(self.github.writes)
        self.assertIn("SEASON_OVER", text)
        self.assertIn("disabled", text)
        self.assertNotIn("CREDITS_EXHAUSTED", text)
        self.assertNotIn("MODEL_UNAVAILABLE", text)

    def test_test_issue_notification(self):
        self.assertEqual(self.run_ops(test=True), 0)
        self.assertIn("test run ok @example", json.dumps(self.github.writes))
        self.assertNotIn("dispatch", self.events)
        self.assertNotIn("heartbeat", self.events)

    def test_install_failure_and_heartbeat_failure(self):
        self.env["INSTALL_OUTCOME"] = "failure"
        self.git = FakeGit(self.clock.now(), age=26, fail=True, events=self.events)
        self.assertEqual(self.run_ops(), 1)
        text = json.dumps(self.github.writes)
        self.assertIn("INSTALL_FAILING", text)
        self.assertIn("HEARTBEAT_FAILED", text)
