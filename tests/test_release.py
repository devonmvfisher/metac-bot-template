import ast
import hashlib
import json
from pathlib import Path
import re
import unittest
from fbot import config

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_python311_syntax_and_lf(self):
        for path in [ROOT / "main.py", *sorted((ROOT / "fbot").glob("*.py"))]:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
        for path in ROOT.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertNotIn(b"\r\n", path.read_bytes(), str(path.relative_to(ROOT)))

    def test_dependency_hashes_and_git_blobs(self):
        pins = json.loads((ROOT / "tests/MANIFEST.json").read_text())
        for name, pin in pins.items():
            data = (ROOT / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), pin["sha256"])
            self.assertEqual(hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest(), pin["git_blob"])
            self.assertEqual(len(data), pin["bytes"])

    def test_targets_and_forbidden_static(self):
        self.assertEqual(config.SEASON_ID, 33121)
        for path in [ROOT / "main.py", *(ROOT / "fbot").glob("*.py")]:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, ("CURRENT_AI_COMPETITION_ID", "CURRENT_MINIBENCH_ID"))
            if path.name == "config.py":
                tree.body = [node for node in tree.body if not isinstance(node, ast.Assign) or
                             not any(isinstance(t, ast.Name) and t.id == "FORBIDDEN_TARGETS" for t in node.targets)]
            constants = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)]
            self.assertFalse(any(value in constants for value in config.FORBIDDEN_TARGETS), str(path))
        for path in (ROOT / ".github/workflows").glob("*"):
            content = path.read_text()
            for value in config.FORBIDDEN_TARGETS:
                self.assertNotIn(str(value), content)

    def test_no_constant_fallback_ast(self):
        for path in [ROOT / "main.py", *(ROOT / "fbot").glob("*.py")]:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    self.assertNotEqual(node.value, .5, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                    literal_one = isinstance(node.left, ast.Constant) and node.left.value == 1
                    denominator_len = isinstance(node.right, ast.Call) and isinstance(node.right.func, ast.Name) and node.right.func.id == "len"
                    self.assertFalse(literal_one and denominator_len)
            executable = ast.unparse(tree)
            for forbidden in ("gpt-4o", "search-preview", "x-ai/", "gemini-3.1-pro"):
                self.assertNotIn(forbidden, executable)

    def test_workflow_structure_and_secrets(self):
        paths = {p.name: p.read_text() for p in (ROOT / ".github/workflows").glob("*")}
        self.assertEqual(set(paths), {"run_bot_on_tournament.yaml", "test_bot.yaml", "run_bot_on_metaculus_cup.yaml", "review_bot.yaml"})
        for name, text in paths.items():
            self.assertIn("workflow_dispatch", text)
            self.assertIn("cancel-in-progress: false", text)
            if name not in ("test_bot.yaml", "run_bot_on_tournament.yaml"):
                self.assertNotIn("schedule:", text)
                self.assertIn('echo "disabled"', text)
                self.assertNotIn("secrets.", text)
                continue
            for required in ("actions/checkout@v5", "persist-credentials: false", "actions/setup-python@v6",
                             "pipx install poetry==2.4.1", "for i in 1 2 3", "timeout-minutes: 58", "timeout-minutes: 70"):
                self.assertIn(required, text)
            for section in text.split("      - name:"):
                if "id: install" in section or "id: run" in section or "id: poetry" in section:
                    self.assertIn("continue-on-error: true", section)
            for forbidden in ("checkout@v4", "setup-python@v5", "upload-artifact@v4", "snok/install-poetry@v1"):
                self.assertNotIn(forbidden, text)
            names = set(re.findall(r"secrets\.([A-Z_]+)", text))
            self.assertEqual(names, {"METACULUS_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ASKNEWS_CLIENT_ID", "ASKNEWS_SECRET", "ASKNEWS_API_KEY"})
        tournament, test = paths["run_bot_on_tournament.yaml"], paths["test_bot.yaml"]
        self.assertIn('cron: "11,31,51 * * * *"', tournament)
        self.assertNotIn("schedule:", test)
        self.assertIn("contents: write\n  actions: write\n  issues: write", tournament)
        self.assertIn("vars.BOT_ENABLED == 'true'", tournament)
        self.assertIn("export BOT_ENABLED=true", tournament)
        self.assertNotIn("BOT_ENABLED", test)

    def test_glue_marked_and_async_wrappers(self):
        text = (ROOT / "main.py").read_text()
        for index, line in enumerate(text.splitlines(), 1):
            if line.strip():
                self.assertIn("# GLUE (NOT EXECUTED offline)", line, str(index))
        self.assertIn("await forecast_async", text)
        self.assertIn("await asyncio.to_thread", text)
        self.assertNotIn("log_report_summary", text)
        self.assertNotIn("_run_forecast_on_discrete", text)
        self.assertIn("predictions_per_research_report=1", text)
        self.assertIn("publish_reports_to_metaculus=True", text)

    def test_hygiene_and_fixtures_count(self):
        self.assertGreaterEqual(len(list((ROOT / "tests/fixtures").glob("*.json"))), 12)
        ignored = (ROOT / ".gitignore").read_text()
        self.assertIn("fbot-run.json", ignored)
        self.assertIn(".venv/", ignored)
        self.assertTrue((ROOT / ".github/heartbeat.txt").exists())
