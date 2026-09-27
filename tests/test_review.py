import ast
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fbot import CreditExhausted, ModelFailure, SkipQuestion, comment, config, metadata, ops, runloop, validate
from fbot.llm import Client
from fbot.postgate import Gate
from fbot.state import RunState
from fbot.types import Question, Research, Result
from .adapter_fakes import load_adapter, raw_question, bot_for
from .fakes import FakeClock, FakeLLM, FakeGit, FakeGitHub, fixture

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.metaculus.com/api/questions/forecast/"


def result_for(question, value=None, cdf=None):
    if value is None:
        value = {p: p for p in config.PERCENTILES} if question.kind in ("numeric", "discrete") else .37
    return Result(value, "FBOT v0.1\nFINAL fixture", [], models=[config.SOL[0]], cdf=cdf)


def gate_for(question, result, readback=lambda *a: False):
    gate = Gate(RunState(FakeClock()), readback=readback)
    gate.register(question, result)
    return gate


def assert_p0(case, state, key, outcome=None):
    env = {"BOT_ENABLED": "true", "GITHUB_EVENT_NAME": "schedule"}
    if outcome:
        env["RUN_OUTCOME"] = outcome
    github = FakeGitHub(state.clock.now())
    code = ops.run(env, state.snapshot(), state.clock, github, FakeGit(state.clock.now()))
    case.assertEqual(code, 1)
    case.assertTrue(any(w[0] == "create" and w[1] == "[BOT ALERT] " + key for w in github.writes))
    case.assertIn(key, ops.P0)


class ReviewTests(unittest.TestCase):
    def test_R01_four_standardised_CDF_shapes(self):
        for lower in (False, True):
            for upper in (False, True):
                with self.subTest(open_lower=lower, open_upper=upper):
                    q = Question(1, 2, "numeric", "fixture", open_lower=lower, open_upper=upper)
                    lo, hi = (.001 if lower else 0), (.999 if upper else 1)
                    values = [lo + .01 * i / 200 + (hi - lo - .01) * min(1, max(0, (i - 90) / 20)) for i in range(201)]
                    self.assertIsNotNone(validate.cdf_api(q, values))
                    self.assertFalse(validate.cdf_strict(q, values))

    def test_R01_round_17_digits_and_closed_upper(self):
        q, data = fixture("numeric_closed")
        values = [v + 1.234567e-12 if 0 < v < 1 else v for v in data["cdf"]]
        values[-1] = .9999999999999999
        self.assertEqual(validate.cdf_api(q, values), [round(v, 10) for v in values])
        self.assertEqual(validate.cdf_api(q, values)[-1], 1.0)

    def test_R01_reject_below_API_limits(self):
        q, data = fixture("numeric_closed")
        values = list(data["cdf"])
        values[1] = .01 / 200 - 1e-8
        self.assertIsNone(validate.cdf_api(q, values))
        values[1] = .01 / 200 * .94
        self.assertIsNone(validate.cdf_api(q, values))
        opened = replace(q, open_lower=True)
        values = list(data["cdf"])
        values[0] = .000999
        self.assertIsNone(validate.cdf_api(opened, values))

    def test_R02_direct_constructors_never_structure(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        for kind in ("MultipleChoiceQuestion", "NumericQuestion"):
            raw = raw_question(sdk, kind)
            question = bot.question(raw)
            values = {"red": .3, "blue": .7} if kind == "MultipleChoiceQuestion" else None
            result = bot.prepare(raw, question, result_for(question, values))
            self.assertIsNotNone(result.prediction)
            if kind == "NumericQuestion":
                self.assertEqual(result.cdf, raw.cdf)

    def test_R02_gate_accepts_drift_and_overwrites_exact(self):
        cases = [(Question(1, 2, "binary", "fixture"), .37, None, "probability_yes", .37 + 1e-12),
                 (Question(1, 2, "multiple_choice", "fixture", options=("a", "b"), retired=("old",)),
                  {"a": .3, "b": .7}, None, "probability_yes_per_category", {"a": .3000003, "b": .7, "old": None}),
                 (Question(1, 2, "numeric", "fixture"), None, [i / 200 for i in range(201)],
                  "continuous_cdf", [i / 200 + (5e-7 if i == 100 else 0) for i in range(201)])]
        for q, value, cdf, field, drift in cases:
            result = result_for(q, value, cdf)
            gate = gate_for(q, result)
            sent, _ = gate.before("POST", URL, [{"question": q.qid, field: drift}])
            self.assertEqual(sent[0][field], validate.payload(q, result)[field])

    def test_R02_constructor_absent_uses_tolerant_main_loop_fallback(self):
        module, sdk = load_adapter()
        del sdk.PredictedOption
        async def scenario():
            bot = bot_for(module, asyncio.get_running_loop())
            raw = raw_question(sdk, "MultipleChoiceQuestion")
            q = bot.question(raw)
            async def structure(*args, **kwargs):
                return SimpleNamespace(predicted_options=[SimpleNamespace(option_name="red", probability=.3000003),
                                                         SimpleNamespace(option_name="blue", probability=.6999997)])
            bot.structure = structure
            result = await asyncio.to_thread(bot.prepare, raw, q, result_for(q, {"red": .3, "blue": .7}))
            self.assertEqual(result.value, {"red": .3, "blue": .7})
        asyncio.run(scenario())

    def test_R02_blocks_wrong_qid_large_drift_and_extra_fields(self):
        q = Question(1, 2, "multiple_choice", "fixture", options=("a", "b"), retired=("old",))
        gate = gate_for(q, result_for(q, {"a": .3, "b": .7}))
        for body in ({"question": 99}, {"question": 1, "probability_yes_per_category": {"a": .302, "b": .698}},
                     {"question": 1, "probability_yes_per_category": {"a": .3, "b": .7}, "probability_yes": .3},
                     {"question": 1, "probability_yes_per_category": {"a": .3, "b": .7, "old": .1}}):
            with self.assertRaises(SkipQuestion):
                gate.before("POST", URL, [body])

    def test_R03_gate_block_records_skip_log_and_P0(self):
        q = Question(1, 2, "binary", "fixture")
        gate = gate_for(q, result_for(q))
        with self.assertLogs("fbot", level="INFO") as logs, self.assertRaises(SkipQuestion):
            gate.before("POST", URL, [{"question": 1, "probability_yes": .9}])
        self.assertEqual(gate.state.skips["INVALID_OUTPUT"], 1)
        self.assertEqual(gate.state.failures[1], 2)
        self.assertEqual(ops.safe_counts(gate.state.snapshot())["counts"]["gate_blocks"], 1)
        self.assertIn("GATE_BLOCKED", gate.state.snapshot()["alerts"])
        self.assertTrue(any("SKIP qid=1" in line for line in logs.output))
        self.assertTrue(any("kind=forecast rule=mismatch" in line for line in logs.output))
        assert_p0(self, gate.state, "GATE_BLOCKED")

    def test_R03_date_early_skip_both_methods(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk, "DateQuestion")
        for method in (bot.run_research, bot.prediction):
            with self.assertLogs("fbot", level="INFO") as logs, self.assertRaisesRegex(SkipQuestion, "UNHANDLED_TYPE"):
                asyncio.run(method(raw))
            self.assertTrue(any("reason=UNHANDLED_TYPE" in line for line in logs.output))

    def test_R03_memo_is_count_not_skip(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk)
        bot.f_state.posted.add(raw.id_of_question)
        with self.assertRaises(SkipQuestion):
            asyncio.run(bot.run_research(raw))
        self.assertEqual(bot.f_state.counts["memo_hits"], 1)
        self.assertFalse(bot.f_state.skips)

    def test_R04_five_polls_prepare_invalid_spends_once(self):
        self.retry_polls(SkipQuestion("INVALID_OUTPUT"), 1, prepare=True)

    def test_R04_five_polls_all_models_failed_spends_twice(self):
        self.retry_polls(ModelFailure(404), 2)

    def retry_polls(self, error, expected, prepare=False):
        module, sdk = load_adapter()
        async def scenario():
            bot = bot_for(module, asyncio.get_running_loop(), value=None if prepare else error)
            if prepare:
                bot.prepare = lambda *args: (_ for _ in ()).throw(error)
            with patch.object(module, "already_forecast", return_value=False), self.assertLogs("fbot", level="INFO") as logs:
                for _ in range(5):
                    try:
                        raw = raw_question(sdk)
                        await bot.run_research(raw)
                        await bot.prediction(raw)
                    except SkipQuestion:
                        pass
            self.assertEqual(len(bot.f_client.calls), expected)
            self.assertTrue(any("reason=RETRY_CAPPED" in line for line in logs.output))
        asyncio.run(scenario())

    def test_R04_4xx_caps_and_deadline_does_not(self):
        q = Question(1, 2, "binary", "fixture")
        gate = gate_for(q, result_for(q))
        gate.after(("forecast", [1]), 400)
        self.assertEqual(gate.state.failures[1], 2)
        fresh = RunState(FakeClock())
        for reason in ("TOO_LATE", "EXHAUSTED"):
            fresh.failure(1, reason)
        self.assertEqual(fresh.failures[1], 0)

    def test_R05_metadata_found_none_unknown_and_group(self):
        for own, expected in (({"latest": {"id": 9}}, True), ({"latest": {}}, False), (None, False)):
            question = {"id": 1, "my_forecasts": own}
            for body in ({"question": question}, {"group_of_questions": {"questions": [{"id": 99}, question]}}):
                calls = []
                def send(*args):
                    calls.append(args)
                    return 200, body
                self.assertIs(metadata.already_forecast(2, 1, {}, send), expected)
                self.assertEqual(calls[0][-1], 15)
        self.assertIsNone(metadata.already_forecast(2, 1, {}, lambda *a: (200, {"question": {"id": 1}})))
        self.assertIsNone(metadata.already_forecast(2, 1, {}, lambda *a: (500, {})))

    def test_R05_before_spend_found_or_unknown(self):
        module, sdk = load_adapter()
        for found, reason in ((True, "ALREADY_FORECAST"), (None, "READBACK_UNKNOWN")):
            bot = bot_for(module)
            with patch.object(module, "already_forecast", return_value=found), self.assertRaisesRegex(SkipQuestion, reason):
                asyncio.run(bot.run_research(raw_question(sdk)))
            self.assertFalse(bot.f_client.calls)
            if found is None:
                self.assertIn("GATE_BLOCKED", bot.f_state.alerts)
        self.assertNotIn("ALREADY_FORECAST", ops.safe_counts({"skips": {"ALREADY_FORECAST": 1}})["skips"])

    def test_R05_forecast_appears_before_post_and_read_outside_lock(self):
        q = Question(1, 2, "binary", "fixture")
        gate = gate_for(q, result_for(q))
        def read(*args):
            self.assertFalse(gate.lock._is_owned())
            return True
        gate.readback = read
        with self.assertLogs("fbot", level="INFO") as logs, self.assertRaises(SkipQuestion):
            gate.before("POST", URL, [{"question": 1, "probability_yes": .37}])
        self.assertTrue(any("rule=duplicate" in line for line in logs.output))

    def test_R05_test_mode_reads_but_does_not_block(self):
        for found in (True, False, None):
            q = Question(1, 2, "binary", "fixture", target="test")
            gate = gate_for(q, result_for(q), lambda *a: found)
            with self.assertLogs("fbot", level="INFO") as logs:
                _, ticket = gate.before("POST", URL, [{"question": 1, "probability_yes": .37}])
            self.assertEqual(ticket, ("forecast", [1]))
            self.assertTrue(any("READBACK qid=1 state=" in line for line in logs.output))

    def test_R06_403_key_limit_stops_without_retry(self):
        state = RunState(FakeClock())
        fake = FakeLLM({config.SOL[0]: [(403, {"error": {"message": "Key limit exceeded"}})]})
        client = Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, state.clock, state.alert, fake)
        for _ in range(2):
            with self.assertRaises(CreditExhausted):
                client.one(config.SOL[0], "fixture")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("CREDITS_EXHAUSTED", state.alerts)

    def test_R06_other_403_is_model_failure(self):
        fake = FakeLLM({config.SOL[0]: [(403, {"error": {"message": "Forbidden"}})]})
        client = Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, FakeClock(), lambda *a: None, fake)
        with self.assertRaises(ModelFailure) as failed:
            client.one(config.SOL[0], "fixture")
        self.assertEqual(failed.exception.status, 403)

    def test_R07_Tier_C_Sol_Flash38_Flash36_404_429(self):
        self.assertEqual(config.SLOTS["C"], ((config.SOL + config.FLASH),))
        self.assertEqual(config.FLASH, ("google/gemini-3.8-flash", "google/gemini-3.6-flash"))
        for status in (404, 429):
            fake = FakeLLM({model: [(status, {})] for model in config.SLOTS["C"][0][:-1]})
            client = Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, FakeClock(), lambda *a: None, fake)
            _, model = client.slot(config.SLOTS["C"][0], "fixture")
            self.assertEqual(model, config.FLASH[1])
            self.assertEqual([call[3]["model"] for call in fake.calls], list(config.SOL + config.FLASH))

    def test_R08_parser_coroutine_uses_main_loop(self):
        module, sdk = load_adapter()
        async def scenario():
            loop = asyncio.get_running_loop()
            bot = bot_for(module, loop)
            q = bot.question(raw_question(sdk))
            async def structure(*a, **k):
                self.assertIs(asyncio.get_running_loop(), loop)
                return SimpleNamespace(prediction_in_decimal=.37)
            bot.structure = structure
            with patch.object(asyncio, "run", side_effect=AssertionError("nested loop")):
                self.assertEqual(await asyncio.to_thread(bot.parser_fallback, q, "fixture"), .37)
        asyncio.run(scenario())

    def test_R08_timeout_cancels_future(self):
        module, sdk = load_adapter()
        async def scenario():
            bot = bot_for(module, asyncio.get_running_loop())
            q = bot.question(raw_question(sdk))
            bot.f_context[q.qid] = (q, Research(), "C", .01, False)
            cancelled = asyncio.Event()
            async def structure(*a, **k):
                try:
                    await asyncio.sleep(5)
                finally:
                    cancelled.set()
            bot.structure = structure
            with self.assertRaisesRegex(SkipQuestion, "TOO_LATE"):
                await asyncio.to_thread(bot.parser_fallback, q, "fixture")
            await asyncio.wait_for(cancelled.wait(), 1)
        asyncio.run(scenario())

    def test_R09_RUN_OUTCOME_failure_is_P0(self):
        assert_p0(self, RunState(FakeClock()), "RUN_FAILED", "failure")

    def test_R09_success_missing_unreadable_state_is_P0(self):
        clock = FakeClock()
        for data in ({}, None):
            github = FakeGitHub(clock.now())
            self.assertEqual(ops.run({"BOT_ENABLED": "true", "RUN_OUTCOME": "success", "GITHUB_EVENT_NAME": "schedule"},
                                    data, clock, github, FakeGit(clock.now())), 1)
            self.assertIn("RUN_FAILED", json.dumps(github.writes))

    def test_R09_all_polls_errored_counts_and_class_only_log(self):
        clock = FakeClock()
        state = RunState(clock)
        async def poll(*a, **k):
            raise ValueError("private message")
        with self.assertLogs("fbot", level="INFO") as logs:
            asyncio.run(runloop.run(poll, state, clock, loop_minutes=1, sleep=clock.asleep))
        self.assertEqual(state.polls, {"season": 1, "minibench": 1})
        self.assertEqual(state.poll_errors, state.polls)
        self.assertIn("POLL_FAILING", state.alerts)
        self.assertNotIn("private message", "\n".join(logs.output))
        self.assertIn("error=ValueError", "\n".join(logs.output))

    def test_R09_strict_environment_check(self):
        module, _ = load_adapter()
        with patch.object(module, "setup_logs"), patch.object(module.bot_helpers, "check_environment", side_effect=RuntimeError) as check:
            with self.assertRaises(RuntimeError):
                module.main()
        check.assert_called_once_with(strict=True)

    def test_R10_T7_6_three_comment_attempts_30_seconds_P0(self):
        q = Question(1, 2, "binary", "fixture")
        gate = gate_for(q, result_for(q))
        gate.after(("forecast", [1]), 201)
        times = []
        def send(*args):
            self.assertTrue(args[1].endswith("comments/create/"))
            times.append(gate.state.clock.monotonic())
            return 500, {}
        gate.retry_comments(send, {})
        self.assertEqual(times, [0, 30, 60])
        self.assertEqual(gate.state.snapshot()["comment_failed"], [1])
        self.assertIn("COMMENT_FAILED", gate.state.snapshot()["alerts"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fbot-run.json"
            gate.state.write(path)
            self.assertEqual(json.loads(path.read_text())["comment_failed"], [1])
        assert_p0(self, gate.state, "COMMENT_FAILED")
        gate.retry_comments(lambda *args: (201, {}), {})
        self.assertEqual(gate.state.posted, {1})
        self.assertFalse(gate.state.comment_failed)

    def test_R11_PROBE_list_matches_config(self):
        self.assertEqual(config.PROBES, config.OPUS + config.SOL + config.FLASH + (config.CHEAP[1],))
        source = (ROOT / "main.py").read_text()
        self.assertIn("models = BRIDGE if direct else PROBES", source)
        self.assertIn('http=%s readback=%s comment=%s', source)

    def execute_test(self, found):
        module, sdk = load_adapter()
        clock, state = FakeClock(), RunState(FakeClock())
        fake = FakeLLM()
        client = Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, clock, state.alert, fake)
        pacer = SimpleNamespace(refresh=lambda: (100, 100), tier=lambda *a, **k: "C")
        installed = []
        def install(gate):
            gate.readback = lambda *a: False
            installed.append(gate)
        async def poll(bot, target, **kwargs):
            raw = raw_question(sdk)
            await bot.run_research(raw)
            await bot.prediction(raw)
            gate = installed[0]
            _, ticket = gate.before("POST", URL, [{"question": raw.id_of_question, "probability_yes": .37}])
            gate.after(ticket, 201)
            _, ticket = gate.before("POST", "https://www.metaculus.com/api/comments/create/", {"on_post": raw.id_of_post})
            gate.after(ticket, 201)
            return []
        with patch.object(module, "Client", return_value=client), patch.object(module, "Pacer", return_value=pacer), \
             patch.object(module, "Service", return_value=SimpleNamespace(get=lambda *a: Research())), \
             patch.object(module, "install_post_gate", side_effect=install), patch.object(module, "already_forecast", return_value=found), \
             patch.object(module.FBot, "forecast_on_tournament", new=poll, create=True), patch.object(state, "write"), \
             self.assertLogs("fbot", level="INFO") as logs:
            operation = module.execute(SimpleNamespace(mode="test_questions"), {"OPENROUTER_API_KEY": "FAKEKEY123"}, clock, state)
            if found is True:
                asyncio.run(operation)
            else:
                with self.assertRaisesRegex(RuntimeError, "TEST_FAILED"):
                    asyncio.run(operation)
        probes = [line.split("model=", 1)[1].split(" status=")[0] for line in logs.output if "PROBE model=" in line]
        self.assertEqual(probes, list(config.PROBES))
        self.assertTrue(any("http=201 readback=" in line for line in logs.output))
        self.assertEqual(state.posted, {8101})

    def test_R11_Test_Bot_found_passes(self):
        self.execute_test(True)

    def test_R11_Test_Bot_missing_fails(self):
        self.execute_test(False)

    def test_R11_Test_Bot_unknown_fails(self):
        self.execute_test(None)

    def test_R12_12_15_30_60_options_and_10_long_labels(self):
        for count, width in ((12, 12), (15, 12), (30, 12), (60, 12), (10, 79)):
            with self.subTest(count=count, width=width):
                labels = tuple((f"label{i:02d}" + "x" * width)[:width] for i in range(count))
                q = Question(1, 2, "multiple_choice", "fixture", options=labels)
                value = validate.repair({key: i + 1 for i, key in enumerate(labels)})
                result = comment.build(q, result_for(q, value), 1, Research(), {})
                self.assertTrue(result.summary.startswith("FBOT "))
                self.assertLess(result.summary.index("FINAL "), 200)
                self.assertLessEqual(len(result.summary), 1000)
                final = "\n".join(line for line in result.summary.splitlines() if line.startswith("FINAL"))
                printed = re.findall(r"o(\d+)=([\d.]+)%", final)
                self.assertEqual(len(printed), count)
                for index, probability in printed:
                    self.assertEqual(float(probability), round(value[labels[int(index)-1]] * 100, 1))

    def test_R13_cdf_size_inbound_or_neither(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        for field in ("cdf_size", "inbound_outcome_count", None):
            raw = raw_question(sdk, "DiscreteQuestion", cdf=[i / 10 for i in range(11)])
            del raw.inbound_outcome_count
            if field:
                setattr(raw, field, 11 if field == "cdf_size" else 10)
            q = bot.question(raw)
            self.assertEqual(q.kind, "discrete")
            result = bot.prepare(raw, q, result_for(q))
            self.assertEqual(q.inbound_outcome_count, 10)
            self.assertEqual(len(result.cdf), 11)
            self.assertEqual(q.resolve_time, raw.scheduled_resolution_time)

    def test_R13_tied_percentiles_and_kind_specific_fallbacks(self):
        module, sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk, "DiscreteQuestion")
        q = bot.question(raw)
        result = bot.prepare(raw, q, result_for(q, {p: 20 for p in config.PERCENTILES}))
        values = [p.value for p in raw.seen_percentiles]
        self.assertTrue(all(a < b for a, b in zip(values, values[1:])))
        self.assertIn("tie-break", result.caps)
        module._fallbacks.clear()
        bot.question(raw_question(sdk))
        self.assertFalse(any(name.endswith(("lower_bound", "upper_bound", "options")) for name in module._fallbacks))

    def test_R14_reuters_url_passage_and_paths(self):
        self.assertEqual(comment.clean("https://www.reuters.com/world/x", {}), "https://www.reuters.com/world/x")
        self.assertEqual(comment.clean("passage of the bill", {}), "passage of the bill")
        self.assertEqual(comment.clean("C:\\Users\\x\\f.txt", {}), "[path]")
        self.assertEqual(comment.clean("/home/runner/work/x", {}), "[path]")

    def test_R14_no_literal_string_concatenations(self):
        for path in [ROOT / "main.py", *(ROOT / "fbot").glob("*.py")]:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    self.assertFalse(all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                                         for v in (node.left, node.right)), f"{path.name}:{node.lineno}")

    def test_R15_MIT_license_scope(self):
        text = (ROOT / "LICENSE").read_text()
        self.assertTrue(text.startswith("This license applies to the files in fbot/ and tests/. Files copied or adapted from Metaculus/metac-bot-template are not covered."))
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026", text)

    def test_R16_alert_rows_and_operating_docs(self):
        runbook = (ROOT / "RUNBOOK.md").read_text()
        for key in ("GATE_BLOCKED", "RUN_FAILED", "POLL_FAILING", "COMMENT_FAILED"):
            self.assertIn("| " + key + " |", runbook)
            self.assertIn(key, ops.STEPS)
        self.assertIn("Sextant", runbook)
        self.assertIn("Prize steps", runbook)
        self.assertIn("UNVERIFIED", runbook)
        self.assertIn("0.2.92", (ROOT / "CHANGELOG.md").read_text())
