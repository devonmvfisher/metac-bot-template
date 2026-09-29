"""Acceptance tests for fbot.numeric (NUMERIC_V1). Do not edit: this file's sha256 is checked.

Written by Claude Opus 5.5, 2026-09-27.
They exercise the real fbot.numeric module. The API oracle below is test code that
restates the Metaculus server rule (questions/serializers/common.py, continuous_validation,
commit 2009b488, read 2026-09-27).
"""
import ast
import json
import random
import statistics
import time
import unittest
from pathlib import Path
from fbot import numeric, validate
from fbot.guards import nominal
from fbot.types import Question

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "numeric_v1"
LEVELS = (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99)
REASONS = {"parse", "input", "order", "domain", "scale", "units", "degenerate", "cdf"}
NOTE_WORDS = {"snap", "tie-spread", "tail-cap", "fold-low", "fold-high", "flat-interior", "maxstep-pack"}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


QUESTIONS = load("questions.json")
BUILD = load("build_cases.json")["cases"]
PARSE = load("parse_cases.json")["cases"]
GOLDEN = load("golden.json")


def question(name):
    return Question(**QUESTIONS[name]["question"])


def declared(case):
    return {level: value for level, value in zip(LEVELS, case["declared"])}


def api_ok(q, cdf):
    n = q.inbound_outcome_count
    values = [round(v, 10) for v in cdf]
    if len(values) != n + 1:
        return False
    pmf = [round(b - a, 9) for a, b in zip(values, values[1:])]
    if not all(p >= round(0.01 / n, 9) for p in pmf):
        return False
    if not all(p <= 0.2 * 200 / n for p in pmf):
        return False
    if q.open_lower:
        if not values[0] >= 0.001:
            return False
    elif values[0] != 0.0:
        return False
    if q.open_upper:
        if not values[-1] <= 0.999:
            return False
    elif values[-1] != 1.0:
        return False
    return True


def strict_v0(q, cdf):
    check = getattr(validate, "cdf_strict", None) or getattr(validate, "cdf")
    return check(q, list(cdf))


def steps(cdf):
    return [b - a for a, b in zip(cdf, cdf[1:])]


def shaped(n, small):
    big = (1 - small * (n - 10)) / 10
    head = (n - 10) // 2
    pmf = [small] * head + [big] * 10 + [small] * (n - 10 - head)
    cdf = [0.0]
    for mass in pmf:
        cdf.append(round(cdf[-1] + mass, 10))
    cdf[-1] = 1.0
    return cdf


def mix_weight(notes):
    found = [note for note in notes if note.startswith("mix-")]
    return float(found[0][len("mix-"):]) if len(found) == 1 else None


class NumericAcceptTests(unittest.TestCase):
    def test_a01_constants(self):
        self.assertEqual(tuple(numeric.LEVELS), LEVELS)
        self.assertEqual(tuple(numeric.LEGACY), (10, 20, 40, 60, 80, 90))
        self.assertEqual(set(numeric.REASONS), REASONS)
        self.assertLessEqual(numeric.MIX_MAX, 0.05)
        self.assertEqual(numeric.MIX_MIN, 0.01)
        self.assertIsInstance(numeric.METHOD, str)
        self.assertTrue(issubclass(numeric.NumericError, ValueError))
        self.assertEqual(numeric.NumericError("units").reason, "units")
        with self.assertRaises(ValueError):
            numeric.NumericError("not-a-reason")

    def test_a02_switch(self):
        for env in ({}, {"NUMERIC_V1": ""}, {"NUMERIC_V1": "true"}, {"NUMERIC_V1": " TRUE "}, {"NUMERIC_V1": "1"}):
            self.assertTrue(numeric.enabled(env), env)
            self.assertIsNone(numeric.config_line(env), env)
        for value in ("false", " FALSE ", "False", "0", "off", "no"):
            self.assertFalse(numeric.enabled({"NUMERIC_V1": value}), value)
            self.assertIsNone(numeric.config_line({"NUMERIC_V1": value}), value)
        self.assertTrue(numeric.enabled({"NUMERIC_V1": "maybe"}))
        self.assertEqual(numeric.config_line({"NUMERIC_V1": "maybe"}), "CONFIG NUMERIC_V1 invalid; using true")

    def test_a03_fixture_labels(self):
        for name, entry in QUESTIONS.items():
            if name == "about":
                continue
            self.assertTrue(entry["label"].startswith("SYNTHETIC"), name)
            q = question(name)
            self.assertNotEqual(q.qid, q.post_id)

    def test_a04_parse_cases(self):
        for case in PARSE:
            with self.subTest(case=case["name"]):
                q = question(case["question"])
                text = "\n".join(case["lines"])
                if case["expect"] == "parse":
                    with self.assertRaises(numeric.NumericError) as caught:
                        numeric.parse_percentiles(q, text)
                    self.assertEqual(caught.exception.reason, "parse")
                else:
                    got = numeric.parse_percentiles(q, text)
                    self.assertEqual(list(got), list(LEVELS))
                    for level, value in zip(LEVELS, case["expect"]):
                        self.assertAlmostEqual(got[level], value, places=9)

    def test_a05_build_ok_cases(self):
        for name, case in BUILD.items():
            if case["expect"] != "ok":
                continue
            with self.subTest(case=name):
                q = question(case["question"])
                built = numeric.build_cdf(q, declared(case))
                self.assertIsInstance(built.cdf, tuple)
                self.assertEqual(len(built.cdf), q.inbound_outcome_count + 1)
                self.assertTrue(all(isinstance(v, float) for v in built.cdf))
                self.assertTrue(all(abs(v - round(v, 10)) <= 1e-12 for v in built.cdf))
                self.assertTrue(numeric.check_strict(q, built.cdf))
                self.assertTrue(strict_v0(q, built.cdf))
                self.assertTrue(api_ok(q, built.cdf))
                notes = set(built.notes)
                self.assertTrue(set(case["notes_include"]) <= notes, built.notes)
                self.assertFalse(set(case["notes_exclude"]) & notes, built.notes)
                weight = mix_weight(built.notes)
                self.assertIsNotNone(weight, built.notes)
                self.assertTrue(0.01 <= weight <= 0.05, weight)
                self.assertTrue(notes - {n for n in notes if n.startswith("mix-")} <= NOTE_WORDS, built.notes)

    def test_a06_build_error_cases(self):
        for name, case in BUILD.items():
            if case["expect"] == "ok":
                continue
            with self.subTest(case=name):
                q = question(case["question"])
                values = declared(case)
                with self.assertRaises(numeric.NumericError) as caught:
                    numeric.build_cdf(q, values)
                self.assertEqual(caught.exception.reason, case["expect"])
                with self.assertRaises(numeric.NumericError) as again:
                    numeric.check_values(q, values)
                self.assertEqual(again.exception.reason, case["expect"])

    def test_a07_pchip_golden(self):
        for entry in GOLDEN["pchip"]:
            with self.subTest(case=entry["name"]):
                got = numeric.pchip(entry["xs"], entry["ys"], entry["points"])
                self.assertEqual(len(got), len(entry["values"]))
                for a, b in zip(got, entry["values"]):
                    self.assertAlmostEqual(a, b, delta=1e-12)

    def test_a08_pchip_properties(self):
        rng = random.Random(28091)
        for _ in range(200):
            count = rng.randint(3, 15)
            xs = sorted(rng.uniform(-2, 3) for _ in range(count))
            if any(b - a < 1e-6 for a, b in zip(xs, xs[1:])):
                continue
            ys = sorted(rng.random() for _ in range(count))
            grid = [xs[0] + (xs[-1] - xs[0]) * k / 400 for k in range(401)]
            values = numeric.pchip(xs, ys, grid)
            self.assertTrue(all(b >= a - 1e-12 for a, b in zip(values, values[1:])))
            self.assertTrue(all(ys[0] - 1e-12 <= v <= ys[-1] + 1e-12 for v in values))
            at_knots = numeric.pchip(xs, ys, xs)
            for a, b in zip(at_knots, ys):
                self.assertAlmostEqual(a, b, delta=1e-12)
        line = numeric.pchip([0.0, 1.0, 3.0, 4.0], [0.0, 0.25, 0.75, 1.0], [0.5, 2.0, 3.5])
        for a, b in zip(line, [0.125, 0.5, 0.875]):
            self.assertAlmostEqual(a, b, delta=1e-12)
        with self.assertRaises(numeric.NumericError):
            numeric.pchip([0.0, 0.0, 1.0], [0.0, 0.5, 1.0], [0.5])

    def test_a09_cdf_golden(self):
        for entry in GOLDEN["cdf"]:
            with self.subTest(case=entry["case"]):
                case = BUILD[entry["case"]]
                built = numeric.build_cdf(question(case["question"]), declared(case))
                for index, value in entry["points"].items():
                    self.assertAlmostEqual(built.cdf[int(index)], value, delta=1e-9, msg=index)
                self.assertEqual(set(built.notes), set(entry["notes"]))

    def test_a10_strict_margin_versus_api(self):
        q = question("closed_linear")
        n = q.inbound_outcome_count
        edge = shaped(n, 0.01 / n)
        self.assertTrue(api_ok(q, edge))
        self.assertFalse(numeric.check_strict(q, edge))
        self.assertFalse(strict_v0(q, edge))
        roomy = shaped(n, 1.06 * 0.01 / n)
        self.assertTrue(api_ok(q, roomy))
        self.assertTrue(numeric.check_strict(q, roomy))
        open_q = question("open_both")
        base = numeric.build_cdf(open_q, declared(BUILD["open_both"])).cdf
        for index, value in ((0, 0.001), (-1, 0.999), (0, 0.0010005), (-1, 0.9989995)):
            changed = list(base)
            changed[index] = value
            self.assertTrue(api_ok(open_q, changed))
            self.assertFalse(numeric.check_strict(open_q, changed))
        built = numeric.build_cdf(q, declared(BUILD["closed_linear"])).cdf
        planted = [built[:-1], [0.001] + list(built[1:]), list(built[:-1]) + [0.999],
                   [built[0], built[2], built[1]] + list(built[3:]), list(built[:5]) + [float("nan")] + list(built[6:]),
                   [built[0], built[1] + 1e-11] + list(built[2:])]
        for values in planted:
            self.assertFalse(numeric.check_strict(q, values))
            self.assertEqual(numeric.check_strict(q, values), strict_v0(q, values))
        self.assertTrue(numeric.check_strict(q, list(built)))

    def test_a11_min_and_max_step_targets(self):
        for name in ("closed_linear", "ties_rate", "shape_discrete_900510", "disc_fifty", "log_open_big"):
            case = BUILD[name]
            q = question(case["question"])
            n = q.inbound_outcome_count
            built = numeric.build_cdf(q, declared(case)).cdf
            self.assertGreaterEqual(min(steps(built)), 1.1 * 0.01 / n - 2e-10, name)
            self.assertLessEqual(max(steps(built)), 0.9 * 0.2 * 200 / n + 2e-10, name)

    def test_a12_twins_fraction_and_percent(self):
        fraction = numeric.build_cdf(question("twin_fraction"), declared(BUILD["twin_fraction"])).cdf
        percent = numeric.build_cdf(question("twin_percent"), declared(BUILD["twin_percent"])).cdf
        for a, b in zip(fraction, percent):
            self.assertAlmostEqual(a, b, delta=1e-9)
        self.assertIn("This question uses a 0 to 1 scale: write 0.45 for forty-five percent, never 45 or 45%.",
                      numeric.guidance(question("twin_fraction")))
        self.assertIn("This question is in percent: write 45 or 45% for forty-five percent, never 0.45.",
                      numeric.guidance(question("twin_percent")))
        self.assertNotIn("0 to 1 scale", numeric.guidance(question("twin_percent")))

    def test_a13_whole_number_twin(self):
        disc_q, num_q = question("twin_whole_disc"), question("twin_whole_num")
        disc = numeric.build_cdf(disc_q, declared(BUILD["twin_whole_disc"])).cdf
        num = numeric.build_cdf(num_q, declared(BUILD["twin_whole_num"])).cdf
        for k in range(50):
            self.assertAlmostEqual(disc[k + 1], num[4 * k + 2], delta=0.005, msg=k)
        disc_p = dict(numeric.sdk_percentiles(disc_q, disc))
        num_p = dict(numeric.sdk_percentiles(num_q, num))
        self.assertAlmostEqual(disc_p[0.5], num_p[0.5], delta=1.0)

    def test_a14_discrete_snapping(self):
        q = question("disc_ten")
        snapped = numeric.build_cdf(q, declared(BUILD["disc_ten_snap"]))
        whole = numeric.build_cdf(q, declared(BUILD["disc_ten_ties"]))
        self.assertEqual(snapped.cdf, whole.cdf)
        self.assertIn("snap", snapped.notes)
        self.assertNotIn("snap", whole.notes)
        point = numeric.build_cdf(question("disc_three"), declared(BUILD["disc_three_point_mass"])).cdf
        self.assertGreaterEqual(point[2] - point[1], 0.9)
        self.assertEqual((point[0], point[-1]), (0.0, 1.0))
        ties = whole.cdf
        self.assertGreaterEqual(ties[4] - ties[3], 0.1)
        for name, bounds in (("shape_discrete_900510", (0, 120)), ("disc_fifty", (1, 50)), ("closed_linear", (0, 100))):
            for got, want in zip(numeric.display_bounds(question(name)), bounds):
                self.assertAlmostEqual(got, want, delta=1e-9, msg=name)

    def test_a15_tails_and_folds(self):
        for name in ("open_both", "open_lower", "open_upper", "shape_percent_900511", "disc_fifty", "log_open_big"):
            case = BUILD[name]
            q = question(case["question"])
            built = numeric.build_cdf(q, declared(case)).cdf
            if q.open_lower:
                self.assertGreaterEqual(built[0], 0.002 - 1e-12, name)
            else:
                self.assertEqual(built[0], 0.0, name)
            if q.open_upper:
                self.assertLessEqual(built[-1], 0.998 + 1e-12, name)
            else:
                self.assertEqual(built[-1], 1.0, name)
        heavy = numeric.build_cdf(question("open_lower"), declared(BUILD["heavy_open_tail"]))
        self.assertAlmostEqual(heavy.cdf[0], 0.7, delta=1e-9)
        fold = numeric.build_cdf(question("closed_linear"), declared(BUILD["closed_fold"])).cdf
        self.assertEqual(fold[0], 0.0)
        self.assertGreaterEqual(fold[1], 0.02)

    def test_a16_combine_median(self):
        q = question("closed_linear")
        runs = [numeric.build_cdf(q, declared(BUILD[name])).cdf for name in ("closed_linear", "closed_fold")]
        runs.append(numeric.build_cdf(question("twin_percent"), declared(BUILD["twin_percent"])).cdf)
        combined = numeric.combine(q, runs)
        for k in range(len(runs[0])):
            self.assertAlmostEqual(combined.cdf[k], round(statistics.median(r[k] for r in runs), 10), delta=1e-10)
        self.assertTrue(numeric.check_strict(q, combined.cdf))
        self.assertTrue(api_ok(q, combined.cdf))
        self.assertIn("cdf-median-3", combined.notes)
        single = numeric.combine(q, runs[:1])
        self.assertEqual(single.cdf, tuple(runs[0]))
        self.assertEqual(single.notes, ())
        pair = numeric.combine(q, runs[:2])
        self.assertAlmostEqual(pair.cdf[100], round((runs[0][100] + runs[1][100]) / 2, 10), delta=1e-10)
        weighted = numeric.combine(q, runs, weights=[2, 2, 1])
        for k in (1, 50, 100, 150, 199):
            running, expected = 0.0, None
            for value, weight in sorted(zip((r[k] for r in runs), (2, 2, 1))):
                running += weight
                if running >= 2.5:
                    expected = value
                    break
            self.assertAlmostEqual(weighted.cdf[k], round(expected, 10), delta=1e-10, msg=k)
        for bad_runs, bad_weights in (([], None), ([runs[0][:-1]], None), ([[0.0] * 201], None),
                                      (runs, [1, 1]), (runs, [1, 0, 1]), (runs, [1, float("nan"), 1])):
            with self.assertRaises(numeric.NumericError) as caught:
                numeric.combine(q, bad_runs, weights=bad_weights)
            self.assertEqual(caught.exception.reason, "input")

    def test_a17_combine_keeps_rules_fuzz(self):
        rng = random.Random(40117)
        for name in ("closed_linear", "open_both", "shape_discrete_900510", "disc_ten", "log_open_big"):
            q = question(name)
            for _ in range(20):
                runs = []
                for _ in range(rng.randint(2, 5)):
                    locations = sorted(rng.uniform(-0.3, 1.3) for _ in LEVELS)
                    values = [nominal(q, max(t, -0.5)) for t in locations]
                    runs.append(numeric.build_cdf(q, dict(zip(LEVELS, values))).cdf)
                combined = numeric.combine(q, runs)
                self.assertTrue(numeric.check_strict(q, combined.cdf), name)
                self.assertTrue(api_ok(q, combined.cdf), name)

    def test_a18_build_fuzz(self):
        rng = random.Random(20260927)
        names = ("closed_linear", "open_both", "open_lower", "open_upper", "log_closed", "log_open_big",
                 "disc_ten", "disc_fifty", "disc_three", "shape_discrete_900510", "shape_percent_900511")
        for trial in range(1100):
            q = question(names[trial % len(names)])
            mode = trial % 4
            if mode == 0:
                locations = sorted(rng.uniform(0, 1) for _ in LEVELS)
            elif mode == 1:
                locations = sorted(rng.uniform(-0.6, 1.6) for _ in LEVELS)
            elif mode == 2:
                centre, width = rng.uniform(0, 1), 10 ** rng.uniform(-6, -1)
                locations = sorted(centre + rng.uniform(-width, width) for _ in LEVELS)
            else:
                picks = sorted(rng.uniform(0, 1) for _ in range(5))
                locations = sorted(rng.choice(picks) for _ in LEVELS)
            values = sorted(nominal(q, max(t, -0.5)) for t in locations)
            try:
                built = numeric.build_cdf(q, dict(zip(LEVELS, values)))
            except numeric.NumericError as error:
                self.assertIn(error.reason, REASONS)
                self.assertNotEqual(mode, 0, (trial, error.reason))
                with self.assertRaises(numeric.NumericError, msg=(trial, error.reason)):
                    numeric.check_values(q, dict(zip(LEVELS, values)))
                continue
            self.assertTrue(numeric.check_strict(q, built.cdf), trial)
            self.assertTrue(api_ok(q, built.cdf), trial)

    def test_a19_sdk_percentiles_and_legacy(self):
        for name in ("closed_linear", "open_both", "disc_ten_ties", "ties_rate", "log_open_big"):
            case = BUILD[name]
            q = question(case["question"])
            built = numeric.build_cdf(q, declared(case)).cdf
            pairs = numeric.sdk_percentiles(q, built)
            self.assertGreaterEqual(len(pairs), 6, name)
            fractions = [f for f, _ in pairs]
            values = [v for _, v in pairs]
            self.assertTrue(set(fractions) <= {level / 100 for level in LEVELS})
            self.assertTrue(all(built[0] < f < built[-1] for f in fractions))
            self.assertTrue(all(b > a for a, b in zip(fractions, fractions[1:])))
            self.assertTrue(all(b > a for a, b in zip(values, values[1:])), name)
        pairs = dict(numeric.sdk_percentiles(question("closed_linear"),
                                             numeric.build_cdf(question("closed_linear"), declared(BUILD["closed_linear"])).cdf))
        self.assertAlmostEqual(pairs[0.5], 49, delta=1.0)
        self.assertAlmostEqual(pairs[0.1], 29, delta=1.5)
        values = declared(BUILD["closed_linear"])
        self.assertEqual(numeric.legacy(values), {10: 29, 20: 36, 40: 45, 60: 53, 80: 61, 90: 67})
        thin = [round(0.2 + 0.3 * k / 200, 10) for k in range(201)]
        with self.assertRaises(numeric.NumericError) as caught:
            numeric.sdk_percentiles(question("open_both"), thin)
        self.assertEqual(caught.exception.reason, "cdf")

    def test_a20_prompt_text(self):
        self.assertEqual(numeric.final_format(question("closed_linear")).splitlines(),
                         [f"Percentile {level:g}: value" for level in LEVELS])
        for name, entry in QUESTIONS.items():
            if name == "about":
                continue
            q = question(name)
            text = numeric.guidance(q) + "\n" + numeric.final_format(q)
            self.assertIn("Give your forecast as 13 percentiles: 1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5 and 99.", text)
            self.assertIn("Percentile 10: X means there is a 10% chance the true value is below X.", text)
            self.assertNotIn("bayes", text.lower())
            self.assertNotIn("e+", text)
            self.assertNotIn("e-0", text)
            self.assertIn("Units: ", text)
            core = numeric.percentile_guidance(q)
            self.assertEqual(len(core.splitlines()), 3, name)
            self.assertTrue(numeric.guidance(q).startswith(core + "\n"), name)
            self.assertNotIn("Units:", core)
            self.assertNotIn("range", core.lower())
        big = numeric.guidance(question("log_open_big"))
        self.assertIn("The range shown is 1,000,000,000 to 1,000,000,000,000.", big)
        self.assertIn("The outcome cannot be below 1,000,000,000.", big)
        self.assertIn("The range is open above", big)
        self.assertIn("The range shown is 0.0025 to 0.05.", numeric.guidance(question("small_rate")))
        self.assertIn("The range shown is 0 to 120.", numeric.guidance(question("shape_discrete_900510")))
        self.assertIn("steps of 1", numeric.guidance(question("disc_ten")))
        self.assertIn("The range is open below", numeric.guidance(question("open_lower")))
        self.assertIn("The outcome cannot be above 100.", numeric.guidance(question("open_lower")))

    def test_a21_units_boundaries(self):
        q = question("closed_linear")
        base = BUILD["closed_linear"]["declared"]
        for shift, ok in ((-248, True), (-250, False), (250, True), (252, False)):
            values = dict(zip(LEVELS, [v + shift for v in base]))
            if ok:
                locations = numeric.check_values(q, values)
                self.assertEqual(len(locations), 13)
            else:
                with self.assertRaises(numeric.NumericError) as caught:
                    numeric.check_values(q, values)
                self.assertEqual(caught.exception.reason, "units")

    def test_a22_inputs_untouched_and_deterministic(self):
        q = question(BUILD["ties_rate"]["question"])
        values = declared(BUILD["ties_rate"])
        before = dict(values)
        first = numeric.build_cdf(q, values)
        second = numeric.build_cdf(q, values)
        self.assertEqual(values, before)
        self.assertEqual(first, second)
        runs = [list(first.cdf), list(second.cdf)]
        copies = [list(r) for r in runs]
        numeric.combine(q, runs)
        self.assertEqual(runs, copies)

    def test_a23_parse_then_build(self):
        case = next(c for c in PARSE if c["name"] == "emphasis_and_bullets")
        q = question(case["question"])
        built = numeric.build_cdf(q, numeric.parse_percentiles(q, "\n".join(case["lines"])))
        self.assertTrue(numeric.check_strict(q, built.cdf))

    def test_a24_speed(self):
        q = question("shape_percent_900511")
        started = time.perf_counter()
        runs = [numeric.build_cdf(q, declared(BUILD[name])).cdf for name in ("shape_percent_900511", "ties_rate")] * 3
        numeric.combine(q, runs[:5])
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_a25_module_hygiene(self):
        path = ROOT / "fbot" / "numeric.py"
        data = path.read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.isascii(), "numeric.py must be ASCII only")
        text = data.decode("utf-8")
        tree = ast.parse(text, feature_version=(3, 11))
        allowed = {"bisect", "dataclasses", "math", "re", "statistics", "fbot", "guards", "parse", "types", ""}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed, alias.name)
            if isinstance(node, ast.ImportFrom):
                name = node.module or ""
                root = name.split(".")[-1] if node.level or name.startswith("fbot") else name.split(".")[0]
                self.assertIn(root, allowed, name)
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.assertNotEqual(node.value, .5, f"float literal at line {node.lineno}")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                literal_one = isinstance(node.left, ast.Constant) and node.left.value == 1
                by_len = isinstance(node.right, ast.Call) and isinstance(node.right.func, ast.Name) and node.right.func.id == "len"
                self.assertFalse(literal_one and by_len, node.lineno)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                both = all(isinstance(side, ast.Constant) and isinstance(side.value, str) for side in (node.left, node.right))
                self.assertFalse(both, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("print", "open", "eval", "exec"), node.lineno)
        lowered = text.lower()
        for word in ("bayes", "os.environ"):
            self.assertNotIn(word, lowered)


if __name__ == "__main__":
    unittest.main()
