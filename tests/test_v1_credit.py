"""Items 1-2: real key routing, tier policy, reservations and completed runs."""
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import patch
from fbot import CreditExhausted, ModelFailure, SkipQuestion, aggregate, config, keys, ledger, pipeline
from fbot.budget import Pacer
from fbot.llm import Client
from fbot.state import RunState
from fbot.types import Research
from .fakes import FakeClock, FakeLLM, FakeMetaculus, deps_for, fixture, narration


def partial_forecast():
    """Finish Sol and one Flash while Opus remains pending; one Flash fails."""
    q, _ = fixture('binary_long')
    release, lock, flash_calls = threading.Event(), threading.Lock(), [0]
    def slow(prompt):
        release.wait(1)
        return narration('Probability: 90%')
    def flash(prompt):
        with lock:
            flash_calls[0] += 1
            first = flash_calls[0] == 1
        if first:
            raise ModelFailure(404)
        return narration('Probability: 37%')
    deps = deps_for({config.OPUS[0]: slow, config.SOL[0]: narration('Probability: 37%'), config.FLASH[0]: flash})
    deps.deadline = 120.05
    try:
        result = pipeline.forecast(q, Research(), 'B', deps)
        api = FakeMetaculus(deps.state)
        api.submit(q, result)
        return q, result, api
    finally:
        release.set()


class CreditTierTests(unittest.TestCase):
    def pacer(self, mode="off", sponsored=100, own=1000, present=True):
        env = {"OPENROUTER_API_KEY": "FAKEKEY-sponsored", "OWN_KEY_MODE": mode}
        if present:
            env["OPENROUTER_API_KEY_OWN"] = "FAKEKEY-own"
        clock, balances, calls = FakeClock(), {"router": sponsored, "own": own}, []
        state = RunState(clock)
        def send(method, url, headers, body, timeout, **kwargs):
            provider = "own" if headers.get("Authorization", "").endswith("-own") else "router"
            calls.append((provider, method))
            if balances[provider] in (402, 403):
                return balances[provider], {"error": {"message": "key limit exceeded"}}
            if method == "GET":
                return 200, {"data": {"limit_remaining": balances[provider], "limit": 2000}}
            return 200, {"choices": [{"message": {"content": narration("Probability: 37%")}}]}
        client = Client(env, clock, state.alert, send)
        pacer = Pacer(env, client, state, clock)
        pacer.refresh()
        return pacer, client, state, balances, calls

    def test_I1_mode_matrix_credit_and_minibench_isolation(self):
        q, _ = fixture("binary_long")
        for mode in ("off", "insurance", "primary"):
            for present in (False, True):
                for sponsored in (100, 402, 403, 2.4):
                    for own in (1000, 402, 403):
                        with self.subTest(mode=mode, present=present, sponsored=sponsored, own=own):
                            pacer, client, state, balances, calls = self.pacer(mode, sponsored, own, present)
                            can_own = mode != "off" and present and own == 1000
                            if sponsored == 100 or sponsored == 2.4 or can_own:
                                self.assertIn(pacer.tier(q), ("A", "B", "C", "M"))
                            else:
                                with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
                                    pacer.tier(q)
                            before = len(calls)
                            try:
                                client.one(config.SOL[0], "synthetic", target="minibench", minimum=config.COSTS["M"])
                            except (CreditExhausted, ModelFailure):
                                pass
                            self.assertFalse(any(provider == "own" for provider, method in calls[before:]))
                            if mode == "off" or not present:
                                self.assertFalse(any(provider == "own" for provider, method in calls))

    def test_I1_insurance_and_primary_key_order(self):
        for mode in ("insurance", "primary"):
            pacer, client, state, balances, calls = self.pacer(mode)
            q, _ = fixture("binary_long")
            self.assertEqual(pacer.tier(q), "A" if mode == "primary" else "C")
            client.one(config.SOL[0], "synthetic")
            self.assertEqual(calls[-1], ("router", "POST"))
            for status in (402, 403):
                pacer, client, state, balances, calls = self.pacer(mode)
                balances["router"] = status
                with self.assertLogs("fbot", level="INFO") as logs:
                    client.one(config.SOL[0], "synthetic")
                self.assertEqual(calls[-2:], [("router", "POST"), ("own", "POST")])
                self.assertIn("USING_OWN_KEY", state.alerts)
                self.assertNotIn("CREDITS_EXHAUSTED", state.alerts)
                self.assertNotIn("FAKEKEY", json.dumps(state.snapshot()) + "\n".join(logs.output))

    def test_I1_low_sponsored_uses_own_only_below_C(self):
        for remaining, expected in ((2 + config.COSTS["C"], "router"), (2 + config.COSTS["C"] - .01, "own")):
            pacer, client, state, balances, calls = self.pacer("insurance", remaining)
            client.one(config.SOL[0], "synthetic")
            self.assertEqual(calls[-1], (expected, "POST"))

    def test_I1_bad_mode_once_counts_only(self):
        with self.assertLogs("fbot", level="INFO") as logs:
            pacer, client, state, balances, calls = self.pacer("FAKEKEY-invalid")
            pacer.refresh()
        self.assertEqual(sum("CONFIG OWN_KEY_MODE" in line for line in logs.output), 1)
        self.assertNotIn("FAKEKEY", "\n".join(logs.output))
        self.assertEqual(client.own_mode, "off")
        self.assertIn("OWN_KEY_CONFIG_INVALID", state.alerts)

    def test_I1_exhaustion_only_all_enabled_keys(self):
        pacer, client, state, balances, calls = self.pacer("primary", 402, 1000)
        self.assertNotIn("CREDITS_EXHAUSTED", state.alerts)
        balances["own"] = 403
        pacer.refresh()
        self.assertIn("CREDITS_EXHAUSTED", state.alerts)

    def test_I1_flash_floor_and_no_own_on_minibench(self):
        q, _ = fixture("binary_long")
        for target in ("season", "minibench"):
            pacer, client, state, balances, calls = self.pacer("off", 2.4)
            self.assertEqual(pacer.tier(replace(q, target=target)), "M")
        pacer, client, state, balances, calls = self.pacer("primary", 402)
        with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
            pacer.tier(replace(q, target="minibench"))
        self.assertNotIn("CREDITS_EXHAUSTED", state.alerts)

    def test_I2_table_and_weighted_median(self):
        self.assertEqual({tier: len(config.SLOTS[tier]) for tier in ("A", "B", "C", "M")}, {"A": 5, "B": 4, "C": 3, "M": 3})
        self.assertEqual(config.SLOTS["A"], (config.OPUS, config.OPUS, config.SOL, config.SOL, config.FLASH))
        self.assertEqual(config.SLOTS["B"], (config.OPUS, config.SOL, config.FLASH, config.FLASH))
        self.assertEqual(config.FLASH[-1], config.SOL[0])
        self.assertNotIn("S", config.TIERS)
        for tier, base in (("A", 1.2), ("B", .77), ("C", .36), ("M", .24)):
            self.assertAlmostEqual(config.COSTS[tier], base * 1.5)
        self.assertAlmostEqual(aggregate.combine("binary", [.1, .8, .9], weights=[2, 2, 1]), .8)
        result = aggregate.combine("multiple_choice", [{"x": .1}, {"x": .8}, {"x": .9}], ("x",), [2, 2, 1])
        self.assertAlmostEqual(result["x"], .54)

    def test_I2_flash_down_is_exactly_two_sol(self):
        q, _ = fixture("binary_long")
        deps = deps_for({config.SOL[0]: narration("Probability: 99%")})
        result = pipeline.forecast(q, Research(), "C", deps)
        self.assertEqual(result.models, [config.SOL[0], config.SOL[0]])
        self.assertEqual(result.value, .98)
        self.assertNotIn("single-run", result.caps)

    def test_I2_flash_outage_plan_is_read_from_the_tier_table(self):
        q, _ = fixture("binary_long")
        deps = deps_for({config.SOL[0]: narration("Probability: 37%")})
        with patch.dict(config.TIERS['C'], flash_down=((config.SOL, 2),) * 3):
            result = pipeline.forecast(q, Research(), "C", deps)
        self.assertEqual(result.models, [config.SOL[0]] * 3)

    def test_I2_partial_deadline_posts_finished_runs(self):
        q, _ = fixture("binary_long")
        release = threading.Event()
        def slow(prompt):
            release.wait(1)
            return narration("Probability: 90%")
        deps = deps_for({config.OPUS[0]: slow, config.SOL[0]: narration("Probability: 37%"), config.FLASH[0]: narration("Probability: 37%")})
        deps.deadline = 120.05
        try:
            result = pipeline.forecast(q, Research(), "B", deps)
            self.assertIn("run1:deadline", result.dropped)
            self.assertEqual(len(result.models), 3)
            api = FakeMetaculus(deps.state)
            api.submit(q, result)
            self.assertEqual(len(api.requests), 2)
            self.assertEqual(result.deadline, 3300)
        finally:
            release.set()

    def test_I2_two_finished_runs_post_while_opus_is_pending(self):
        q, result, api = partial_forecast()
        self.assertEqual(result.models, [config.SOL[0], config.FLASH[0]])
        self.assertIn('run1:deadline', result.dropped)
        self.assertEqual(len(api.requests), 2)
        self.assertTrue(api.requests[-1][1]['is_private'])
        self.assertIn(q.qid, api.gate.state.posted)

    def test_I2_measured_cost_twentieth_question_and_missing_ledger(self):
        pacer, client, state, balances, calls = self.pacer("off", 1000)
        state.attach_book(ledger.Ledger(available=True))
        q, _ = fixture("binary_long")
        for i in range(20):
            before = pacer.measure_start()
            balances["router"] -= 1
            pacer.measure_end(replace(q, qid=q.qid + i), "C", before)
            self.assertAlmostEqual(pacer.costs()["C"], 1.2 if i == 19 else config.COSTS["C"])
        state.book = ledger.empty()
        self.assertEqual(pacer.costs(), config.COSTS)

    def test_I2_refresh_keeps_pending_reservations(self):
        pacer, client, state, balances, calls = self.pacer("off", 2.7)
        q, _ = fixture("binary_long")
        pacer.tier(q)
        pacer.refresh()
        with self.assertRaisesRegex(SkipQuestion, "EXHAUSTED"):
            pacer.tier(replace(q, qid=q.qid + 1))

    def test_H3_fast_wins_and_test_presets(self):
        q, _ = fixture("binary_long")
        for preset in ("A", "B", "C"):
            pacer, client, state, balances, calls = self.pacer("off", 1000)
            state.model_preset = preset
            self.assertEqual(pacer.tier(q, fast=True), "M")
            self.assertEqual(pacer.tier(replace(q, target="test")), preset)

    def test_H3_measured_cost_applies_to_preset_reserve(self):
        pacer, client, state, balances, calls = self.pacer("off", 14)
        state.model_preset = "A"
        state.attach_book(ledger.Ledger(available=True))
        for _ in range(20):
            state.book.record_spend("A", 2, state.clock.now())
        q, _ = fixture("binary_long")
        self.assertEqual(pacer.tier(q), "C")

    def test_I1_own_reservations_do_not_consume_minibench_credit(self):
        q, _ = fixture("binary_long")
        pacer, client, state, balances, calls = self.pacer("primary", 2.4, 1000)
        self.assertEqual(pacer.tier(q), "A")
        self.assertEqual(pacer.tier(replace(q, qid=q.qid + 1, target="minibench")), "M")
        self.assertAlmostEqual(pacer.reservations['router'], config.COSTS['M'])
        pacer.release(q.qid)
        self.assertAlmostEqual(pacer.reserved, config.COSTS['M'])

    def test_I2_measured_low_cost_reaches_client_and_flash_floor_per_key(self):
        q, _ = fixture("binary_long")
        pacer, client, state, balances, calls = self.pacer("off", 2.4)
        state.attach_book(ledger.Ledger(available=True))
        for _ in range(20):
            state.book.record_spend("C", .1, state.clock.now())
        self.assertEqual(pacer.tier(q), "C")
        client.one(config.SOL[0], 'synthetic')
        self.assertEqual(calls[-1], ('router', 'POST'))
        pacer, client, state, balances, calls = self.pacer("primary", 2.4, 2.4)
        self.assertEqual(pacer.tier(q), 'M')
