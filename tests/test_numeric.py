"""Additional fixture, parser, weighted-median, and seeded build checks."""
import copy
from dataclasses import FrozenInstanceError, replace
import json
import math
from pathlib import Path
import random
import statistics
import time
import unittest

from fbot import numeric, validate
from fbot.guards import nominal
from fbot.types import Question

FIXTURES = Path(__file__).parent / "fixtures" / "numeric_v1"
LEVELS = (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99)
QUESTIONS = json.loads((FIXTURES / "questions.json").read_text(encoding="utf-8"))
BUILD = json.loads((FIXTURES / "build_cases.json").read_text(encoding="utf-8"))["cases"]
CASES = json.loads((FIXTURES / "lane_cases.json").read_text(encoding="utf-8"))["cases"]


def question(name):
    return Question(**QUESTIONS[name]["question"])


def values(case):
    if "lines" in case:
        return numeric.parse_percentiles(question(case["question"]), "\n".join(case["lines"]))
    return dict(zip(LEVELS, case["declared"]))


def server_rule(q, cdf):
    """Independent restatement of the packet's server rule, including rounding."""
    n = q.inbound_outcome_count
    if len(cdf) != n + 1 or not all(math.isfinite(v) and 0 <= v <= 1 for v in cdf):
        return False
    rounded = [round(v, 10) for v in cdf]
    pmf = [round(rounded[i + 1] - rounded[i], 9) for i in range(n)]
    return (all(round(0.01 / n, 9) <= p <= 0.2 * 200 / n for p in pmf)
            and (rounded[0] >= 0.001 if q.open_lower else rounded[0] == 0)
            and (rounded[-1] <= 0.999 if q.open_upper else rounded[-1] == 1))


def run_fuzz(check, count=2400):
    rng = random.Random(78123409)
    names = sorted(name for name in QUESTIONS if name != "about")
    seen, errors, modes = set(), {}, set()
    for trial in range(count):
        name = names[trial % len(names)]
        q = question(name)
        mode = (trial // len(names)) % 12
        seen.add(name)
        modes.add(mode)
        if mode == 0:
            locations = sorted(rng.uniform(0, 1) for _ in LEVELS)
        elif mode == 1:
            locations = sorted(rng.uniform(-0.7, 1.7) for _ in LEVELS)
        elif mode == 2:
            centre = rng.uniform(0.1, 0.9)
            locations = sorted(centre + rng.uniform(-1e-8, 1e-8) for _ in LEVELS)
        elif mode == 3:
            picks = [rng.uniform(0.1, 0.9) for _ in range(4)]
            locations = sorted(rng.choice(picks) for _ in LEVELS)
        elif mode == 4:
            locations = [0.45] * 13
        elif mode == 5:
            locations = [0.1, 0.2, 0.3, 0.4, 0.4 + 1e-13, 0.4 + 2e-13,
                         0.4 + 1e-8, 0.55, 0.65, 0.75, 0.85, 0.9, 0.95]
        elif mode == 6:
            locations = sorted(rng.uniform(-0.7, -0.4) for _ in LEVELS)
        elif mode == 7:
            locations = sorted(rng.uniform(1.1, 1.8) for _ in LEVELS)
        else:
            locations = sorted(rng.uniform(0.1, 0.9) for _ in LEVELS)
        declared = dict(zip(LEVELS, sorted(nominal(q, t) for t in locations)))
        if mode == 8:
            declared[50] = True
        elif mode == 9:
            declared[99] = declared[1] - 1
        elif mode == 10:
            if q.zero_point is not None:
                declared[1] = q.zero_point
            else:
                declared = dict(zip(LEVELS, (nominal(q, 4 + i / 100) for i in range(13))))
        elif mode == 11:
            del declared[97.5]
        before = copy.deepcopy(declared)
        try:
            built = numeric.build_cdf(q, declared)
        except numeric.NumericError as error:
            with check.assertRaises(numeric.NumericError, msg=(trial, name, mode)) as caught:
                numeric.check_values(q, declared)
            check.assertEqual(error.reason, caught.exception.reason, (trial, name, mode))
            errors[error.reason] = errors.get(error.reason, 0) + 1
        else:
            numeric.check_values(q, declared)
            check.assertTrue(numeric.check_strict(q, built.cdf), (trial, name, mode))
            check.assertTrue(server_rule(q, built.cdf), (trial, name, mode))
            check.assertTrue((getattr(validate, "cdf_strict", None) or validate.cdf)(q, list(built.cdf)), (trial, name, mode))
        check.assertEqual(declared, before)
    check.assertEqual(seen, set(names))
    check.assertEqual(modes, set(range(12)))
    return {"seed": 78123409, "builds": count, "shapes": len(seen), "modes": len(modes),
            "valid": count - sum(errors.values()), "errors": errors, "invalid_cdfs": 0,
            "unmatched_errors": 0}


class NumericTests(unittest.TestCase):
    def reason(self, expected, function, *args, **kwargs):
        with self.assertRaises(numeric.NumericError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.reason, expected)
        self.assertEqual(str(caught.exception), expected)

    def test_fixture_cases(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                q, declared = question(case["question"]), values(case)
                if case["expect"] != "ok":
                    self.reason(case["expect"], numeric.build_cdf, q, declared)
                    self.reason(case["expect"], numeric.check_values, q, declared)
                else:
                    built = numeric.build_cdf(q, declared)
                    self.assertTrue(numeric.check_strict(q, built.cdf))
                    self.assertTrue(server_rule(q, built.cdf))
                    self.assertLessEqual(set(case["notes_include"]), set(built.notes))
                    self.assertFalse(set(case["notes_exclude"]) & set(built.notes))

    def test_seeded_build_fuzz(self):
        report = run_fuzz(self)
        self.assertGreaterEqual(report["builds"], 2000)
        self.assertGreater(report["valid"], 1000)
        print("FUZZ " + json.dumps(report, sort_keys=True))

    def test_combine_two_to_seven_runs_weighted_and_unweighted(self):
        rng = random.Random(512091)
        for name in sorted(name for name in QUESTIONS if name != "about"):
            q = question(name)
            runs = []
            for _ in range(7):
                data = dict(zip(LEVELS, sorted(nominal(q, rng.uniform(0.05, 0.95)) for _ in LEVELS)))
                runs.append(numeric.build_cdf(q, data).cdf)
            for count in range(2, 8):
                for weights in (None, [rng.randint(1, 9) for _ in range(count)]):
                    selected = runs[:count]
                    combined = numeric.combine(q, selected, weights)
                    integer_weights = weights or [1] * count
                    expected = [round(statistics.median([run[k] for run, w in zip(selected, integer_weights)
                                                        for _ in range(w)]), 10)
                                for k in range(q.inbound_outcome_count + 1)]
                    self.assertEqual(combined.cdf, tuple(expected), (name, count, weights))
                    self.assertTrue(numeric.check_strict(q, combined.cdf))
                    self.assertTrue(server_rule(q, combined.cdf))
                    self.assertEqual(combined.notes, (f"cdf-median-{count}",))

    def test_weighted_half_and_relative_tolerance(self):
        q = question("closed_linear")
        runs = [numeric.build_cdf(q, values(BUILD[name])).cdf for name in ("closed_linear", "closed_fold")]
        midpoint = tuple(round((a + b) / 2, 10) for a, b in zip(*runs))
        for weights in ([1, 1], [1e-300, 1e-300], [1e308, 1e308], [1, 1 + 1e-13]):
            self.assertEqual(numeric.combine(q, runs, weights).cdf, midpoint)
        self.assertEqual(numeric.combine(q, runs, [1, 1 + 1e-8]).cdf, runs[1])
        self.assertEqual(numeric.combine(q, runs, [1e-300, 1e308]).cdf, runs[1])

    def test_invalid_error_reason_is_value_error(self):
        for reason in (None, 0, True, [], {}, {"input"}, "unknown"):
            with self.subTest(reason=repr(reason)):
                with self.assertRaises(ValueError):
                    numeric.NumericError(reason)

    def test_pchip_noniterable_points_are_input(self):
        for points in (None, 7, True):
            with self.subTest(points=points):
                self.reason("input", numeric.pchip, [0, 1], [0, 1], points)

    def test_legacy_short_sequence_is_input(self):
        for data in ([], (), [1], "", None):
            with self.subTest(data=data):
                self.reason("input", numeric.legacy, data)

    def test_all_error_reasons(self):
        reached = set()
        for case in list(BUILD.values()) + list(CASES.values()):
            if case["expect"] != "ok":
                self.reason(case["expect"], numeric.build_cdf, question(case["question"]), values(case))
                reached.add(case["expect"])
        q = question("closed_linear")
        self.reason("parse", numeric.parse_percentiles, q, "FINAL\nNo numbers")
        self.reason("cdf", numeric.sdk_percentiles, q, [0] * 201)
        reached.update(("parse", "cdf"))
        self.assertEqual(reached, numeric.REASONS)

    def test_validation_order(self):
        q = question("log_closed")
        data = values(BUILD["log_closed"])
        data[1], data[50] = -3, None
        self.reason("input", numeric.check_values, q, data)
        data[50] = -4
        self.reason("order", numeric.check_values, q, data)
        data[50] = 24
        self.reason("domain", numeric.check_values, q, data)
        tied = dict.fromkeys(LEVELS, 400)
        self.reason("scale", numeric.check_values, question("small_rate"), dict.fromkeys(LEVELS, 4))
        self.reason("units", numeric.check_values, question("closed_linear"), tied)
        self.reason("degenerate", numeric.check_values, question("closed_linear"), dict.fromkeys(LEVELS, 50))

    def test_bad_values_and_grid_sizes(self):
        q = question("closed_linear")
        good = values(BUILD["closed_linear"])
        for bad in (None, [], "bad", {**good, 3: 10}):
            self.reason("input", numeric.check_values, q, bad)
        for bad in (True, None, "1", float("inf"), float("nan"), 10**1000):
            self.reason("input", numeric.check_values, q, {**good, 50: bad})
        for size in (True, False, 0, -1, 200.0, None):
            altered = replace(q, inbound_outcome_count=size)
            self.reason("input", numeric.build_cdf, altered, good)
            self.assertFalse(numeric.check_strict(altered, [0, 1]))
        for size in (1, 2, 3, 30, 36, 37, 199, 200, 201, 500):
            altered = replace(q, inbound_outcome_count=size)
            built = numeric.build_cdf(altered, good)
            self.assertTrue(numeric.check_strict(altered, built.cdf))
            self.assertTrue(server_rule(altered, built.cdf))

    def test_bad_cdfs_and_weights(self):
        q = question("closed_linear")
        run = numeric.build_cdf(q, values(BUILD["closed_linear"])).cdf
        for bad in (None, [], "bad", [run[:-1]], [[0] * 201]):
            self.reason("input", numeric.combine, q, bad)
        for bad in ([1], [1, 0], [1, -1], [True, 1], [1, "1"], [1, math.inf], [1, math.nan]):
            self.reason("input", numeric.combine, q, [run, run], bad)
        for bad in (None, "bad", [0], list(run[:5]) + [True] + list(run[6:])):
            self.assertFalse(numeric.check_strict(q, bad))

    def test_parse_last_valid_block(self):
        q = question("no_unit")
        old = "\n".join(f"P{p:g}: {i}" for i, p in enumerate(LEVELS))
        new = "\n".join(f"P{p:g}: {i + 20}" for i, p in enumerate(LEVELS))
        text = f"FINAL\n{old}\nFINAL:\n{new}\nFinal note: done"
        self.assertEqual(list(numeric.parse_percentiles(q, text).values()), list(range(20, 33)))

    def test_parse_emphasis_echo_extra_and_units(self):
        q = question("closed_linear")
        text = "# FINAL\nPercentile 10: X means there is a 10% chance\nP33: ignore"
        text += "\n" + "\n".join(f"\u2022 **P{p:g}** = `{i} INDEX UNITS`" for i, p in enumerate(LEVELS))
        self.assertEqual(list(numeric.parse_percentiles(q, text).values()), list(range(13)))

    def test_parse_finallly_and_no_header(self):
        q = question("no_unit")
        text = "\n".join(f"+ Percentile {p:g}th (label): {i}" for i, p in enumerate(LEVELS))
        for source in (text, "FINALLY this is a conclusion\n" + text):
            self.assertEqual(list(numeric.parse_percentiles(q, source).values()), list(range(13)))

    def test_parse_percent_fraction_and_bad_suffix(self):
        text = "\n".join(f"P{p:g}: {i + 1}%" for i, p in enumerate(LEVELS))
        self.assertEqual(list(numeric.parse_percentiles(question("twin_percent"), text).values()), list(range(1, 14)))
        self.reason("parse", numeric.parse_percentiles, question("twin_fraction"), text)
        self.reason("parse", numeric.parse_percentiles, question("no_unit"), text)

    def test_parse_duplicate_unreadable_and_partial_final(self):
        q = question("no_unit")
        text = "\n".join(f"P{p:g}: {i}" for i, p in enumerate(LEVELS))
        self.reason("parse", numeric.parse_percentiles, q, text + "\nP10: 3")
        self.assertEqual(len(numeric.parse_percentiles(q, text + "\nP10: 3e2")), 13)
        self.reason("parse", numeric.parse_percentiles, q, text + "\nFINAL\nP10: 2")
        self.reason("parse", numeric.parse_percentiles, q, None)

    def test_parse_long_whitespace_runs_are_fast(self):
        q = question("no_unit")
        start = time.perf_counter()
        for pad in (" " * 250, "	" * 250):
            self.reason("parse", numeric.parse_percentiles, q, "P10" + pad + "x")
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_parse_scaled_suffixes(self):
        q = question("millions")
        text = "\n".join(f"P{p:g}: ${i + 1}B million dollars" for i, p in enumerate(LEVELS))
        self.assertEqual(list(numeric.parse_percentiles(q, text).values()), [(i + 1) * 1000 for i in range(13)])

    def test_immutability_and_determinism(self):
        q = question("closed_linear")
        data = values(BUILD["closed_linear"])
        before = copy.deepcopy((q, data))
        built = numeric.build_cdf(q, data)
        self.assertEqual((q, data), before)
        self.assertEqual(built, numeric.build_cdf(q, data))
        runs = [list(built.cdf), list(numeric.build_cdf(q, values(BUILD["closed_fold"])).cdf)]
        weights = [2, 1]
        before = copy.deepcopy((q, runs, weights))
        numeric.combine(q, runs, weights)
        self.assertEqual((q, runs, weights), before)
        with self.assertRaises(FrozenInstanceError):
            built.notes = ("changed",)

    def test_pchip_two_knots_flats_and_turns(self):
        self.assertEqual(numeric.pchip([2, 4], [7, 3], [0, 2, 3, 4, 6]), [7, 7, 5, 3, 3])
        self.assertEqual(numeric.pchip([0, 1, 2], [4, 4, 4], [-1, 0, .5, 1.5, 3]), [4] * 5)
        xs, ys = [0, 1, 2, 3], [0, 1, 0, 1]
        self.assertEqual(numeric.pchip(xs, ys, xs), ys)
        result = numeric.pchip(xs, ys, [i / 100 for i in range(301)])
        self.assertTrue(all(0 <= value <= 1 for value in result))
        for x, y in (([], []), ([0], [0]), ([0, 1], [0]), ([1, 0], [0, 1]), ([0, math.nan], [0, 1])):
            self.reason("input", numeric.pchip, x, y, [0])

    def test_upper_fold_mass(self):
        q = question("closed_linear")
        data = dict(zip(LEVELS, [40, 50, 60, 70, 80, 90, 96, 100, 104, 110, 120, 130, 150]))
        built = numeric.build_cdf(q, data)
        self.assertIn("fold-high", built.notes)
        self.assertAlmostEqual(built.cdf[-1] - built.cdf[-2], 0.18, places=9)
        self.assertTrue(server_rule(q, built.cdf))

    def test_strict_maximum_margin(self):
        q = question("closed_linear")
        for large, expected in ((.189, True), (.195, False)):
            masses = [large] + [(1 - large) / 199] * 199
            cdf = [0.0]
            for mass in masses:
                cdf.append(round(cdf[-1] + mass, 10))
            cdf[-1] = 1.0
            self.assertTrue(server_rule(q, cdf))
            self.assertEqual(numeric.check_strict(q, cdf), expected)
            self.assertEqual((getattr(validate, "cdf_strict", None) or validate.cdf)(q, cdf), expected)

    def test_prompt_split_and_display(self):
        for name in ("disc_thirty", "log_open_lower", "no_unit", "twin_fraction", "twin_percent"):
            q = question(name)
            core = numeric.percentile_guidance(q)
            self.assertEqual(len(core.splitlines()), 3)
            self.assertNotIn("Units:", core)
            self.assertTrue(numeric.guidance(q).startswith(core + "\n"))
            self.assertEqual(len(numeric.final_format(q).splitlines()), 13)
        log_disc = replace(question("log_closed"), kind="discrete", inbound_outcome_count=10)
        lo, hi = numeric.display_bounds(log_disc)
        self.assertAlmostEqual(lo, nominal(log_disc, .05))
        self.assertAlmostEqual(hi, nominal(log_disc, .95))
        self.assertEqual(numeric.display_bounds(question("disc_thirty")), (0, 29))
        for value, text in ((1e12, "1,000,000,000,000"), (.0025, "0.0025"), (1e-8, "0.00000001"), (-3.25, "-3.25")):
            self.assertEqual(numeric.plain(value), text)

    def test_switch_values_and_units(self):
        for value in (None, "", True, 1, "on", "yes", "nonsense"):
            self.assertTrue(numeric.enabled({"NUMERIC_V1": value}))
        for value in (False, 0, " false ", "no", "off"):
            self.assertFalse(numeric.enabled({"NUMERIC_V1": value}))
        for unit in ("pp", " Percent points ", "% annual"):
            self.assertTrue(numeric.is_percent_unit(unit))
        for unit in (None, "", "count"):
            self.assertFalse(numeric.is_percent_unit(unit))

    def test_sdk_inverse_and_endpoint_exclusion(self):
        for name in ("closed_linear", "log_closed", "disc_thirty"):
            q = question(name)
            cdf = [round(k / q.inbound_outcome_count, 10) for k in range(q.inbound_outcome_count + 1)]
            pairs = numeric.sdk_percentiles(q, cdf)
            self.assertEqual([p for p, _ in pairs], [level / 100 for level in LEVELS])
            for fraction, value in pairs:
                self.assertAlmostEqual(value, nominal(q, fraction), delta=max(1e-8, abs(value) * 1e-8))
        q = question("open_both")
        cdf = [round(.2 + .3 * k / 200, 10) for k in range(201)]
        self.reason("cdf", numeric.sdk_percentiles, q, cdf)


if __name__ == "__main__":
    unittest.main()
