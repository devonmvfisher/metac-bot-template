"""Acceptance tests for the dispatch-only workflow the outside timer starts (v1 item 3; review L16).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT).
Contract: INTERFACE.md section 2. Text checks only; no YAML library is needed.
"""
from pathlib import Path
import unittest
from fbot import config, schedule

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
HEADER = (
    "name: Forecast on timer (outside dispatch)\n"
    "on:\n"
    "  workflow_dispatch:\n"
    "concurrency:\n"
    "  group: fbot-post-tournament\n"
    "  cancel-in-progress: false\n"
    "permissions:\n"
    "  contents: write\n"
    "  actions: write\n"
    "  issues: write\n"
    "env:\n"
    "  FBOT_TRIGGER: timer\n"
)


def split(text):
    head, found, jobs = text.partition("\njobs:\n")
    return head + "\n", (jobs if found else None)


class TimerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.raw = (WORKFLOWS / schedule.TIMER_WORKFLOW).read_bytes()
        self.timer = self.raw.decode("utf-8")
        self.tournament = (WORKFLOWS / schedule.TOURNAMENT_WORKFLOW).read_text(encoding="utf-8")

    def test_header_is_exact(self):
        head, jobs = split(self.timer)
        self.assertEqual(head, HEADER)
        self.assertIsNotNone(jobs)

    def test_dispatch_only(self):
        for word in ("schedule:", "cron:", "push:", "pull_request", "workflow_run", "repository_dispatch",
                     "workflow_call"):
            self.assertNotIn(word, self.timer)

    def test_jobs_block_identical_to_tournament(self):
        # The jobs block is re-copied from the tournament workflow whenever that file changes; this keeps them in step.
        self.assertEqual(split(self.timer)[1], split(self.tournament)[1])

    def test_lf_no_tabs_trailing_newline(self):
        self.assertNotIn(b"\r", self.raw)
        self.assertNotIn(b"\t", self.raw)
        self.assertTrue(self.raw.endswith(b"\n"))

    def test_no_forbidden_targets(self):
        for value in config.FORBIDDEN_TARGETS:
            self.assertNotIn(str(value), self.timer)

    @unittest.skipIf(config.VERSION == "v0", "review R23 sets the tournament's literal group in v0.1; checked at integration")
    def test_both_posting_workflows_share_the_literal_group(self):
        head = split(self.tournament)[0]
        self.assertIn("  group: fbot-post-tournament\n", head)
        self.assertNotIn("github.workflow", head)


if __name__ == "__main__":
    unittest.main()
