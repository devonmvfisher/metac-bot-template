"""v1.1 workflow pins: every job runs on ubuntu-24.04, and the MiniBench dial variables reach the bot."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ALL = ("review_bot.yaml", "run_bot_on_metaculus_cup.yaml", "run_bot_on_timer.yaml",
       "run_bot_on_tournament.yaml", "test_bot.yaml")
POSTING = ("run_bot_on_tournament.yaml", "run_bot_on_timer.yaml")
DIAL = ("MINIBENCH_MODE", "MINIBENCH_PRESET", "MINIBENCH_FLOOR_USD")


def run_step(text):
    return text.split("      - name: Run bot", 1)[1].split("      - name:", 1)[0]


class WorkflowPinTests(unittest.TestCase):
    def test_W01_every_workflow_pins_ubuntu_24_04(self):
        self.assertEqual(sorted(path.name for path in WORKFLOWS.iterdir()), sorted(ALL))
        for name in ALL:
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            labels = re.findall(r"^\s*runs-on:\s*(.+?)\s*$", text, re.M)
            self.assertEqual(len(labels), text.count("runs-on:"), name)
            self.assertTrue(labels, name)
            self.assertEqual(set(labels), {"ubuntu-24.04"}, name)
            self.assertNotIn("ubuntu-latest", text, name)
            self.assertNotIn("ubuntu-26", text, name)

    def test_W02_posting_workflows_pass_the_dial_variables_once(self):
        for name in POSTING:
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            run = run_step(text)
            for variable in DIAL:
                self.assertEqual(text.count(variable + ":"), 1, name + " " + variable)
                self.assertIn(variable + ": ${{ vars." + variable + " }}", run, name + " " + variable)
                self.assertNotIn("secrets." + variable, text, name + " " + variable)

    def test_W03_test_bot_does_not_read_minibench_settings(self):
        text = (WORKFLOWS / "test_bot.yaml").read_text(encoding="utf-8")
        for variable in DIAL:
            self.assertNotIn(variable, text)


if __name__ == "__main__":
    unittest.main()
