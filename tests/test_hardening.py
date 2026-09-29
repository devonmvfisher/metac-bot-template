"""Acceptance tests for fbot.hardening: the rationale cap (v1 H6) and season-end decisions (v1 H7; review L08).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT; reviewed 15:20 CT).
Contract: INTERFACE.md section 8. These are SHOULD items: if they are not built, this file fails
with ImportError, and the build notes must say so plainly.
"""
from contextlib import redirect_stdout
import io
import unittest
from fbot import hardening
from fbot.targets import utc

BEFORE = utc("2027-01-06T23:59:59Z")
AFTER = utc("2027-01-07T00:00:00Z")


class HardeningTests(unittest.TestCase):
    def test_rationale_caps_h6(self):
        summary = "S" * 900
        five = hardening.cap_rationales(summary, ["r" * 4000] * 5)
        self.assertEqual(len(five), 5)
        self.assertTrue(all(len(text) <= 1500 for text in five))
        self.assertEqual([text[:6] for text in five], [f"RUN {i}\n" for i in range(1, 6)])
        self.assertLessEqual(len(summary) + sum(len(text) + 2 for text in five), 10000)
        three = hardening.cap_rationales(summary, ["r" * 4000] * 3)
        self.assertEqual([len(text) for text in three], [2500, 2500, 2500])
        many = hardening.cap_rationales("S" * 1000, ["r" * 4000] * 9)
        # Runs that no longer fit are left out: never an empty or header-only RUN block.
        self.assertEqual([len(text) for text in many], [1500] * 5 + [1488])
        self.assertTrue(all(text.startswith("RUN ") and len(text) > 6 for text in many))
        self.assertLessEqual(1000 + sum(len(text) + 2 for text in many), 10000)
        self.assertEqual(hardening.cap_rationales(summary, []), [])
        short = hardening.cap_rationales(summary, ["tiny"] * 4)
        self.assertEqual(short[0], "RUN 1\ntiny")

    def test_season_end_actions_h7(self):
        self.assertEqual(hardening.season_over_actions(BEFORE, {}, []),
                         {"forecast": True, "heartbeat": True, "post_season_over": False})
        self.assertEqual(hardening.season_over_actions(AFTER, {}, []),
                         {"forecast": False, "heartbeat": False, "post_season_over": True})
        # An existing issue, open or closed, means SEASON_OVER was already posted once.
        self.assertEqual(hardening.season_over_actions(AFTER, {}, ["Bot weekly status", "[BOT ALERT] SEASON_OVER"]),
                         {"forecast": False, "heartbeat": False, "post_season_over": False})

    def test_season_end_variable(self):
        custom = {"SEASON_END_UTC": "2026-12-01T00:00:00Z"}
        self.assertFalse(hardening.season_open(utc("2026-12-02T00:00:00Z"), custom))
        self.assertTrue(hardening.season_open(utc("2026-11-30T00:00:00Z"), custom))
        self.assertTrue(hardening.season_open(utc("2026-12-02T00:00:00Z"), {"SEASON_END_UTC": "garbage"}))
        self.assertTrue(hardening.season_open(BEFORE, {}))
        self.assertFalse(hardening.season_open(AFTER, {}))

    def test_cli_prints_a_github_output_line(self):
        for now, expected in ((BEFORE, "season_open=true\n"), (AFTER, "season_open=false\n")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = hardening.main(["--season-open"], env={}, now=now)
            self.assertEqual((code, out.getvalue()), (0, expected))

    def test_cli_fails_open(self):
        # The workflow skips the forecast step on "false", so any error must say "true", never stop the bot.
        for env, now in (({"SEASON_END_UTC": "garbage"}, BEFORE), ({}, "not a time")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = hardening.main(["--season-open"], env=env, now=now)
            self.assertEqual((code, out.getvalue()), (0, "season_open=true\n"))


if __name__ == "__main__":
    unittest.main()
