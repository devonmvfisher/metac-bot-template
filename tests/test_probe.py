"""Acceptance tests for fbot.probe: the daily "does every model still answer" check (v1 item 3).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT; reviewed 15:20 CT).
Contract: INTERFACE.md section 4. The probe never talks to Metaculus and never posts a forecast.
"""
from datetime import timedelta
import unittest
from fbot import config, ledger, probe
from fbot.llm import Client
from fbot.state import RunState
from fbot.targets import utc
from .fakes import FakeClock, FakeLLM

SLOTS = {"OPUS": ("anthropic/claude-opus-5.5",), "SOL": ("openai/gpt-6-sol",),
         "FLASH": ("google/gemini-3.8-flash", "google/gemini-3.6-flash"), "CHEAP": ("openai/gpt-6-luna",)}
ROUTER = "https://openrouter.ai/api/v1/chat/completions"


def rig(scripts=None):
    clock = FakeClock("2026-10-05T12:00:00Z")
    state = RunState(clock)
    llm = FakeLLM(scripts or {}, default="OK")
    client = Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, clock, state.alert, send=llm)
    return client, llm, state, clock


class ProbeTests(unittest.TestCase):
    def test_all_answer_no_alert_once_a_day_and_zero_metaculus_calls(self):
        client, llm, state, clock = rig()
        book = ledger.empty()
        self.assertTrue(probe.due(book, clock.now()))
        self.assertTrue(probe.due(None, clock.now()))
        with self.assertLogs("fbot", level="INFO") as logs:
            report = probe.run(probe.make_call(client), SLOTS, clock)
            probe.apply(report, state, book, clock.now())
        self.assertEqual(report.results, [("OPUS", "anthropic/claude-opus-5.5", 200),
                                          ("SOL", "openai/gpt-6-sol", 200),
                                          ("FLASH", "google/gemini-3.8-flash", 200),
                                          ("FLASH", "google/gemini-3.6-flash", 200),
                                          ("CHEAP", "openai/gpt-6-luna", 200)])
        self.assertEqual((report.alert_slots, report.flaky_slots, report.credit_exhausted, report.probed),
                         ((), (), False, 5))
        self.assertNotIn("MODEL_UNAVAILABLE", state.alerts)
        self.assertFalse(probe.due(book, clock.now()))
        self.assertFalse(probe.due(book, clock.now() + timedelta(hours=11)))
        self.assertTrue(probe.due(book, clock.now() + timedelta(hours=12, seconds=1)))
        text = "\n".join(logs.output)
        self.assertIn("PROBE_DAILY slot=FLASH model=google/gemini-3.6-flash status=200", text)
        self.assertIn("PROBE_DAILY alert=none flaky=0 credit_exhausted=false", text)
        self.assertNotIn("FAKEKEY123", text)
        self.assertEqual(len(llm.calls), 5)
        for method, url, headers, body, timeout in llm.calls:
            self.assertEqual((method, url), ("POST", ROUTER))
            self.assertNotIn("metaculus", url)
            # Each call is bounded: a slow model can never hold the run for v0's 480-second default.
            self.assertLessEqual(timeout, probe.CALL_SECONDS)

    def test_due_without_a_saved_date_only_in_the_probe_hour(self):
        # A lost ledger (cache miss) must not turn the daily probe into a probe on every run.
        self.assertEqual(probe.PROBE_HOUR_UTC, 12)
        in_hour, later = utc("2026-10-05T12:40:00Z"), utc("2026-10-05T13:00:00Z")
        for book in (None, ledger.empty()):
            self.assertTrue(probe.due(book, in_hour))
            self.assertFalse(probe.due(book, later))
        book = ledger.empty()
        book.record_probe(utc("2026-10-04T12:10:00Z"))
        self.assertTrue(probe.due(book, later))

    def test_gone_model_names_its_slot(self):
        client, llm, state, clock = rig({"google/gemini-3.8-flash": [(404, {})]})
        report = probe.run(probe.make_call(client), SLOTS, clock)
        probe.apply(report, state, ledger.empty(), clock.now())
        self.assertEqual(report.alert_slots, ("FLASH",))
        self.assertIn("MODEL_UNAVAILABLE", state.alerts)
        self.assertEqual(probe.alert_rows(state.snapshot()), ["slots=FLASH"])

    def test_flaky_slot_counted_not_alerted_and_dead_slot_alerts(self):
        client, llm, state, clock = rig({"google/gemini-3.8-flash": [(429, {})],
                                         "google/gemini-3.6-flash": [(503, {}), (503, {})],
                                         "openai/gpt-6-sol": [(401, {})]})
        report = probe.run(probe.make_call(client), SLOTS, clock)
        probe.apply(report, state, None, clock.now())
        self.assertEqual(report.alert_slots, ("SOL",))
        self.assertEqual(report.flaky_slots, ("FLASH",))
        self.assertEqual(state.counts["probe_flaky"], 1)
        self.assertEqual(state.counts["probe_unavailable_SOL"], 1)
        self.assertEqual(probe.alert_rows(state.snapshot()), ["slots=SOL"])

    def test_credit_exhausted_stops_probing_without_model_alert(self):
        client, llm, state, clock = rig({"anthropic/claude-opus-5.5": [(402, {"error": {"message": "Insufficient credits"}})]})
        report = probe.run(probe.make_call(client), SLOTS, clock)
        self.assertTrue(report.credit_exhausted)
        self.assertEqual(report.alert_slots, ())
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual([status for _, _, status in report.results], [402, None, None, None, None])

    def test_time_budget_leaves_the_rest_unprobed(self):
        clock = FakeClock()
        calls = []

        def call(model):
            calls.append(model)
            clock.sleep(70)
            return 200

        report = probe.run(call, SLOTS, clock, budget_seconds=120)
        self.assertEqual(len(calls), 2)
        self.assertEqual([status for _, _, status in report.results], [200, 200, None, None, None])
        self.assertEqual((report.alert_slots, report.probed), ((), 2))

    def test_call_exception_is_status_zero_and_private(self):
        def call(model):
            raise RuntimeError("provider text FAKEKEY123")

        with self.assertLogs("fbot", level="INFO") as logs:
            report = probe.run(call, {"SOL": ("openai/gpt-6-sol",)}, FakeClock())
        self.assertEqual(report.results, [("SOL", "openai/gpt-6-sol", 0)])
        self.assertEqual((report.alert_slots, report.flaky_slots), ((), ("SOL",)))
        self.assertNotIn("FAKEKEY123", "\n".join(logs.output))

    def test_shared_id_probed_once(self):
        client, llm, state, clock = rig()
        slots = {"FLASH": ("google/gemini-3.8-flash",), "CHEAP": ("google/gemini-3.8-flash", "openai/gpt-6-luna")}
        report = probe.run(probe.make_call(client), slots, clock)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(len(report.results), 3)
        self.assertEqual(report.probed, 2)

    def test_slots_from_config(self):
        slots = probe.slots_from_config(config)
        self.assertEqual(list(slots)[:2], ["OPUS", "SOL"])
        self.assertTrue(set(slots) <= set(probe.SLOT_NAMES))
        self.assertNotIn("BRIDGE", slots)
        ids = [model for models in slots.values() for model in models]
        self.assertTrue(ids and all(isinstance(model, str) and "/" in model for model in ids))
        self.assertNotIn("google/gemini-3.1-pro-preview", ids)

    def test_alert_rows_whitelist(self):
        data = {"counts": {"probe_unavailable_FLASH": 1, "probe_unavailable_EVIL": 1, "probe_unavailable_SOL": 0,
                           "probe_unavailable_OPUS": 1}}
        self.assertEqual(probe.alert_rows(data), ["slots=FLASH,OPUS"])
        self.assertEqual(probe.alert_rows({}), [])
        self.assertEqual(probe.alert_rows({"counts": "junk"}), [])


if __name__ == "__main__":
    unittest.main()
