import math
import random
import unittest
from dataclasses import replace
from fbot import SkipQuestion
from fbot import aggregate, config, guards, parse, targets, validate
from fbot.types import Question
from .fakes import FakeClock, fixture


class CoreTests(unittest.TestCase):
    def test_target_constants_and_end(self):
        self.assertEqual(config.SEASON_ID, 33121)
        self.assertEqual(config.SEASON_SLUG, "fall-futureeval-2026")
        self.assertEqual(config.MINIBENCH_ID, "minibench")
        self.assertEqual(config.TEST_ID, "bot-testing-area")
        self.assertEqual(targets.active(targets.utc("2027-01-06T23:59:59Z")), [33121, "minibench"])
        self.assertEqual(targets.active(targets.utc("2027-01-07T00:00:00Z")), [])
        self.assertEqual(targets.startup(3, 2), ["TARGET season=33121 slug=fall-futureeval-2026 open=3", "TARGET minibench open=2"])

    def test_binary_parser(self):
        q, _ = fixture("binary_long")
        for text in ("Probability: 37%", "37 %", "0.37"):
            self.assertEqual(parse.parse(q, text), 0.37)
        for text in ("37", "nan", "101%", "-1%", "0.2\n0.3"):
            with self.assertRaises(ValueError):
                parse.parse(q, text)

    def test_units_parser(self):
        for text in ("1,200,000", "1.2 million", "1.2M"):
            self.assertEqual(parse.number(text), 1200000)
        self.assertEqual(parse.number("$3.4B"), 3400000000)
        self.assertEqual(parse.number("$3.4B", "million dollars"), 3400)
        for text in ("nan", "infinity", "1e9", "a million"):
            with self.assertRaises(ValueError):
                parse.number(text)

    def test_mc_labels_missing_unknown_and_retired(self):
        q, _ = fixture("mc_three")
        parsed = parse.parse(q, " red : 20%\nGREEN: 30%\n Blue : 50%")
        self.assertEqual(set(parsed), set(q.options))
        for text in ("Red: 20%\nGreen: 80%", "Red: 20%\nGreen: 30%\nOther: 50%",
                     "Red: 20%\nGreen: 30%\nBlue: 50%\nRed: 0%"):
            with self.assertRaises(ValueError):
                parse.parse(q, text)
        retired, _ = fixture("mc_retired")
        value = {"Alpha": .2, "Beta": .3, "Gamma": .5}
        self.assertTrue(validate.multiple_choice(retired, value))
        self.assertTrue(validate.multiple_choice(retired, {**value, "Retired option": None}))
        self.assertFalse(validate.multiple_choice(retired, {**value, "Retired option": .01}))
        self.assertFalse(validate.multiple_choice(retired, {**value, "Unknown": None}))

    def test_aggregation(self):
        self.assertAlmostEqual(aggregate.combine("binary", [.2, .5, .9]), .5)
        expected = 1 / (1 + math.exp(-(math.log(.2 / .8) + math.log(.9 / .1)) / 2))
        self.assertAlmostEqual(aggregate.combine("binary", [.2, .9]), expected)
        self.assertAlmostEqual(aggregate.combine("multiple_choice", [{"a": .2}, {"a": .4}], ("a",))["a"], .3)
        runs = [{p: 100 - p for p in config.PERCENTILES}, {p: 110 - p for p in config.PERCENTILES}]
        out = aggregate.combine("numeric", runs)
        self.assertEqual(list(out.values()), sorted(out.values()))

    def test_binary_caps_and_extreme_agreement(self):
        self.assertEqual(validate.cap_binary(.001, [.001, .001])[0], .02)
        self.assertEqual(validate.cap_binary(.999, [.999, .999])[0], .98)
        self.assertEqual(validate.cap_binary(.001, [.001])[0], .05)
        self.assertEqual(validate.cap_binary(.999, [.999])[0], .95)
        self.assertEqual(validate.cap_binary(.97, [.99, .89])[0], .95)
        self.assertEqual(validate.cap_binary(.03, [.001, .11])[0], .05)
        self.assertEqual(validate.cap_binary(.96, [.96, .97])[0], .96)
        for value in (-.1, 0, 1, float("nan"), float("inf"), True):
            self.assertFalse(validate.binary(value))

    def test_mc_floor_1000_vectors(self):
        rng = random.Random(4219)
        for _ in range(1000):
            count = rng.randint(2, 30)
            values = {str(i): rng.random() ** 8 for i in range(count)}
            q = Question(1, 2, "multiple_choice", "Fixture", options=tuple(values))
            filled = validate.repair(values)
            self.assertTrue(validate.multiple_choice(q, filled))
            self.assertGreaterEqual(min(filled.values()), .01 - 1e-9)
            self.assertLessEqual(max(filled.values()), .99)
            self.assertLessEqual(abs(sum(filled.values()) - 1), 1e-6)
        self.assertEqual(validate.repair({"a": 0, "b": 1}), {"a": .01, "b": .99})

    def test_mc_planted_invalid(self):
        q, _ = fixture("mc_three")
        for value in ({"Red": .5, "Green": .5}, {"Red": .001, "Green": .499, "Blue": .5},
                      {"Red": .2, "Green": .2, "Blue": .2}, {"Red": float("nan"), "Green": .5, "Blue": .5}):
            self.assertFalse(validate.multiple_choice(q, value))
        for bad in ({"a": 0, "b": 0}, {"a": -1, "b": 2}, {"a": float("nan"), "b": 1}):
            with self.assertRaises(SkipQuestion):
                validate.repair(bad)

    def test_all_cdf_shapes(self):
        for name in ("numeric_closed", "numeric_open", "numeric_lower_open", "numeric_upper_open",
                     "numeric_log", "numeric_billions", "discrete_ten", "discrete_fifty"):
            with self.subTest(name=name):
                q, data = fixture(name)
                self.assertEqual(len(data["cdf"]), q.inbound_outcome_count + 1)
                self.assertTrue(validate.cdf_strict(q, data["cdf"]))

    def test_cdf_planted_rules(self):
        q, data = fixture("numeric_closed")
        original = data["cdf"]
        invalid = [original[:-1]]
        for index, value in ((0, .001), (-1, .999), (12, float("nan")), (12, -1), (12, 2),
                             (1, .00004), (50, original[49]), (5, .9), (11, original[11] + .000000000011)):
            changed = list(original)
            changed[index] = value
            invalid.append(changed)
        for values in invalid:
            self.assertFalse(validate.cdf_strict(q, values))
        open_q, opened = fixture("numeric_open")
        for index, value in ((0, .001), (-1, .999)):
            changed = list(opened["cdf"])
            changed[index] = value
            self.assertFalse(validate.cdf_strict(open_q, changed))

    def test_scale_inverse_and_units(self):
        q, _ = fixture("numeric_log")
        for t in (0, .1, .4, .8, 1):
            self.assertAlmostEqual(guards.scaled(q, guards.nominal(q, t)), t)
        ordinary, _ = fixture("numeric_billions")
        values = {p: 3.4e9 for p in config.PERCENTILES}
        self.assertFalse(guards.units_ok(ordinary, values))
        self.assertTrue(guards.units_ok(ordinary, {p: 3400 for p in config.PERCENTILES}))

    def test_misread_patterns(self):
        for text in ("HAS ALREADY RESOLVED", "already resolved", "question has resolved", "resolved yes",
                     "resolved No", "has already happened", "this already occurred", "outcome is known"):
            self.assertTrue(guards.misread(text))
        self.assertFalse(guards.misread("It remains open and the outcome is uncertain."))


if __name__ == "__main__":
    unittest.main()
