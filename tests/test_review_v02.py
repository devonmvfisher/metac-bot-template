import asyncio
from datetime import timedelta
from http.client import IncompleteRead
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import ModelFailure, SkipQuestion, budget, config, llm, prompts, runloop
from fbot.pipeline import forecast
from fbot.postgate import Gate
from fbot.priority import PriorityLimiter
from fbot.state import RunState
from fbot.types import Question, Research
from .adapter_fakes import load_adapter, raw_question, bot_for
from .fakes import FakeClock, deps_for, fixture

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.metaculus.com/api/questions/forecast/"
COMMENT_URL = "https://www.metaculus.com/api/comments/create/"


class ReviewTests(unittest.TestCase):
    def test_R17_group_post_pair_comments_and_explicit_retry_A(self):
        pairs = []
        deps = deps_for({})
        gate = Gate(deps.state, readback=lambda *a: False)
        for name in ("group_1", "group_2"):
            q, data = fixture(name)
            deps.client.values[config.SOL[0]] = data["model_text"]
            result = forecast(q, Research(), "C", deps)
            gate.register(q, result)
            _, ticket = gate.before("POST", URL, [{"question": q.qid, "probability_yes": result.value}])
            gate.after(ticket, 200)
            pairs.append((q, result))
        a, b = pairs
        self.assertEqual(gate.posts[900], [a[0].qid, b[0].qid])
        self.assertEqual(gate.posted_order, [a[0].qid, b[0].qid])
        body, ticket = gate.before("POST", COMMENT_URL, {"on_post": 900})
        self.assertEqual(body["text"], b[1].comment)
        self.assertIn(f"| q{b[0].qid} |", body["text"].splitlines()[0])
        gate.after(ticket, 200)
        # A's first comment fails; the final retry must explicitly retain A.
        body, ticket = gate.before("POST", COMMENT_URL, {"on_post": 900})
        self.assertEqual(body["text"], a[1].comment)
        gate.after(ticket, 500)
        calls = []
        def send(method, url, headers, body, timeout):
            calls.append((url, body))
            return 200, {}
        gate.retry_comments(send, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["text"], a[1].comment)
        self.assertIn(f"| q{a[0].qid} |", calls[0][1]["text"])
        self.assertEqual(deps.state.commented, {a[0].qid, b[0].qid})
        with self.assertRaises(SkipQuestion):
            gate.before("POST", COMMENT_URL, {"on_post": 900})

    def test_R18_Always_slack_spaces_offf_default_and_log_once(self):
        budget._mode_warned = False
        self.assertEqual(budget.normalize_mode("Always"), "always")
        self.assertEqual(budget.normalize_mode(" slack "), "slack")
        with self.assertLogs("fbot", level="WARNING") as logs:
            for _ in range(2):
                self.assertEqual(budget.choose(FakeClock().now(), 100, "minibench", "offf"), "C")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("CONFIG MINIBENCH_MODE invalid; using always", logs.output[0])

    def test_R18_slack_reserves_B_cost_times_season(self):
        with patch.dict(config.COSTS, B=2), patch.object(budget, "remaining_season", return_value=10):
            threshold = config.FLOOR_CREDIT + 2 * 10 + config.COSTS["C"] * 60
            self.assertEqual(budget.choose(FakeClock().now(), threshold, "minibench", "slack"), "C")
            self.assertEqual(budget.choose(FakeClock().now(), threshold - 1, "minibench", "slack"), "M")

    def test_R19_trickling_response_total_limit(self):
        clock, reads = FakeClock(), []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                reads.append(size)
                clock.sleep(10)
                return b" "
        opener = SimpleNamespace(open=lambda *a, **k: Response())
        with patch("urllib.request.build_opener", return_value=opener), patch.object(llm.time, "monotonic", clock.monotonic):
            with self.assertRaises(ModelFailure) as failed:
                llm.transport("GET", "https://example.invalid/fixture", {}, None, 100, total=30)
        self.assertEqual(failed.exception.status, 0)
        self.assertEqual(reads, [64 * 1024] * 3)
        self.assertEqual(clock.monotonic(), 30)

    def test_R19_IncompleteRead_is_failure_and_key_unknown(self):
        opener = SimpleNamespace(open=lambda *a, **k: (_ for _ in ()).throw(IncompleteRead(b"x", 10)))
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ModelFailure) as failed:
                llm.transport("GET", "https://example.invalid/fixture", {}, None, 10)
            client = llm.Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, FakeClock(), lambda *a: None)
            self.assertEqual(client.key(), (None, None))
        self.assertEqual(failed.exception.status, 0)

    def test_R19_IncompleteRead_falls_through_to_Flash(self):
        calls = []
        class Response:
            status = 200
            def __init__(self):
                self.body = json.dumps({"choices": [{"message": {"content": "Probability: 37%"}}]}).encode()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                data, self.body = self.body, b""
                return data
        def opened(request, **kwargs):
            model = json.loads(request.data)["model"]
            calls.append(model)
            if model == config.SOL[0]:
                raise IncompleteRead(b"x", 10)
            return Response()
        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=opened)):
            client = llm.Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, FakeClock(), lambda *a: None)
            _, model = client.slot(config.SOL + config.FLASH, "fixture")
        self.assertEqual(model, config.FLASH[0])
        self.assertEqual(calls, [config.SOL[0], config.SOL[0], config.FLASH[0]])

    def test_R19_client_passes_remaining_total(self):
        calls = []
        def send(*args, **kwargs):
            calls.append((args[-1], kwargs["total"]))
            return 200, {"choices": [{"message": {"content": "Probability: 37%"}}]}
        clock = FakeClock()
        clock.sleep(3)
        client = llm.Client({"OPENROUTER_API_KEY": "FAKEKEY123"}, clock, lambda *a: None, send)
        client.one(config.SOL[0], "fixture", deadline=12, timeout=100)
        self.assertEqual(calls, [(9, 9)])

    def test_R20_T10_exact_header_and_forward_numeric_guards(self):
        q, _ = fixture("numeric_closed")
        now = FakeClock().now()
        text = prompts.build(q, Research(), now)
        header = (f"Today is {now.date().isoformat()} (UTC). This question is OPEN and has NOT resolved. "
                  f"It closes at {q.close_time or 'unknown'} and resolves by {q.resolve_time or 'unknown'}. "
                  "Do not assume the outcome is known.")
        self.assertEqual(text.splitlines()[0], header)
        self.assertIn("A question asking whether something happens by or before a date is forward-looking from today; if it has not happened yet, the status quo is that it has not.", text)
        self.assertIn("Percentile 10: X means there is a 10% chance the true value is below X.", text)
        self.assertNotIn("bayes", text.lower())

    def test_R21_eight_shuffled_closes_waiters_start_in_order(self):
        async def scenario():
            limiter = PriorityLimiter(5)
            now = FakeClock().now()
            offsets = [100, 80, 70, 110, 60, 90, 10, 50]
            release = [asyncio.Event() for _ in offsets]
            started = [asyncio.Event() for _ in offsets]
            order = []
            async def one(index):
                q = Question(index, 1000+index, "binary", "fixture", close_time=now+timedelta(minutes=offsets[index]))
                async with limiter.slot(q):
                    order.append(index)
                    started[index].set()
                    await release[index].wait()
            tasks = [asyncio.create_task(one(i)) for i in range(8)]
            try:
                await asyncio.gather(*(started[i].wait() for i in range(5)))
                self.assertEqual(limiter.active, 5)
                self.assertEqual(len(limiter.waiters), 3)
                release[0].set()
                for index in (6, 7, 5):
                    await asyncio.wait_for(started[index].wait(), 1)
                    release[index].set()
                self.assertEqual(order[5:], [6, 7, 5])
            finally:
                for event in release:
                    event.set()
                await asyncio.gather(*tasks)
            self.assertEqual(limiter.active, 0)
        asyncio.run(scenario())

    def test_R21_research_concurrency_never_above_two(self):
        module, sdk = load_adapter()
        lock, counts = threading.Lock(), {"active": 0, "peak": 0}
        def research(*args):
            with lock:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            try:
                time.sleep(.02)
                return Research()
            finally:
                with lock:
                    counts["active"] -= 1
        async def scenario():
            bot = bot_for(module, asyncio.get_running_loop())
            bot.f_research.get = research
            with patch.object(module, "already_forecast", return_value=False):
                await asyncio.gather(*(bot.run_research(raw_question(sdk, id_of_question=8500+i)) for i in range(8)))
            self.assertEqual(len(bot.f_context), 8)
        asyncio.run(scenario())
        self.assertEqual(counts["peak"], 2)

    def test_R21_cancelled_waiter_does_not_leak_permit(self):
        async def scenario():
            limiter = PriorityLimiter(1)
            q = Question(1, 2, "binary", "fixture")
            await limiter.acquire(q)
            waiting = asyncio.create_task(limiter.acquire(q))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            limiter.release()
            self.assertFalse(limiter.waiters)
            self.assertEqual(limiter.active, 0)
        asyncio.run(scenario())

    def test_R22_workflow_variables_and_fallbacks(self):
        text = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        for expected in ('LOOP_MINUTES: ${{ vars.LOOP_MINUTES }}', 'POLL_MINUTES: ${{ vars.POLL_MINUTES }}',
                         '--loop-minutes "${LOOP_MINUTES:-45}"', '--poll-minutes "${POLL_MINUTES:-10}"'):
            self.assertIn(expected, text)
        for default, name in ((45, "LOOP_MINUTES"), (10, "POLL_MINUTES")):
            with self.assertLogs("fbot", level="WARNING") as logs:
                for value in ("", "bad", "nan", "inf", "-1", "0", str(default+1), None):
                    self.assertEqual(runloop.minutes(value, default, name), default)
            self.assertTrue(all(f"CONFIG {name} invalid; using {default}" in row for row in logs.output))
            self.assertEqual(runloop.minutes(" 3.5 ", default, name), 3.5)

    def test_R22_main_parses_leniently_and_can_start_uses_setting(self):
        module, sdk = load_adapter()
        seen = []
        async def execute(args, *other):
            seen.append((args.loop_minutes, args.poll_minutes))
        with patch.object(module, "setup_logs"), patch.object(module, "execute", side_effect=execute), \
             patch.object(module.RunState, "write"), patch.object(module.ledger, "save"), patch.object(module.os, "environ", {}), \
             patch.object(sys, "argv", ["main.py", "--loop-minutes", "oops", "--poll-minutes", " 4 "]):
            with self.assertRaises(SystemExit) as exited:
                module.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(seen, [(45, 4)])
        bot = bot_for(module)
        bot.f_loop_minutes = 2
        bot.f_clock.sleep(121)
        with patch.object(module, "already_forecast", return_value=False):
            asyncio.run(bot.run_research(raw_question(sdk)))
        self.assertIn(8101, bot.f_context)

    def test_R23_literal_tournament_group_and_test_independent(self):
        tournament = (ROOT / ".github/workflows/run_bot_on_tournament.yaml").read_text()
        test = (ROOT / ".github/workflows/test_bot.yaml").read_text()
        self.assertIn("group: fbot-post-tournament", tournament)
        self.assertNotIn("github.workflow", tournament)
        self.assertNotIn("fbot-post-tournament", test)
        self.assertIn('cron: "11,31,51 * * * *"', tournament)

    def test_R27_readback_retries_rate_limits_then_fails_closed(self):
        from fbot import metadata
        replies = [(429, {}), (503, {}), (200, {"question": {"id": 1, "my_forecasts": {"latest": {"id": 3}}}})]
        naps = []
        self.assertIs(metadata.already_forecast(2, 1, {}, lambda *a: replies.pop(0), sleep=naps.append), True)
        self.assertEqual(naps, [2, 4])
        naps.clear()
        self.assertIsNone(metadata.already_forecast(2, 1, {}, lambda *a: (429, {}), sleep=naps.append))
        self.assertEqual(naps, [2, 4])
        def boom(*a):
            raise OSError("reset")
        naps.clear()
        self.assertIsNone(metadata.already_forecast(2, 1, {}, boom, sleep=naps.append))
        self.assertEqual(naps, [2, 4])
        naps.clear()
        self.assertIsNone(metadata.already_forecast(2, 1, {}, lambda *a: (500, {}), sleep=naps.append))
        self.assertEqual(naps, [])
        self.assertIs(metadata.already_forecast(2, 1, {}, lambda *a: (200, {"question": {"id": 1, "my_forecasts": {"latest": {}}}}), sleep=naps.append), False)
        self.assertEqual(naps, [])

    def test_R28_research_records_error_codes_without_text(self):
        from fbot.research import Service, error_code
        class Boom(Exception):
            status_code = 401
        class Plain(Exception):
            pass
        self.assertEqual(error_code(Boom("secret text")), "Boom:401")
        self.assertEqual(error_code(Plain("secret text")), "Plain")
        class Resp:
            status_code = 429
        wrapped = Plain("x")
        wrapped.response = Resp()
        self.assertEqual(error_code(wrapped), "Plain:429")
        from tests.fakes import deps_for, fixture
        q, _ = fixture("binary_long")
        deps = deps_for({})
        def fail(*a, **k):
            raise Boom("provider says secret")
        service = Service({"ASKNEWS_API_KEY": "FAKEKEY123"}, deps.state, fail)
        self.assertFalse(service.get(q).available)
        self.assertEqual(service.errors[q.qid], "Boom:401")
        none = Service({}, deps_for({}).state, fail)
        none.get(q)
        self.assertEqual(none.errors[q.qid], "NO_KEY")
        ok = Service({"ASKNEWS_API_KEY": "FAKEKEY123"}, deps_for({}).state, lambda *a, **k: ("news", 1))
        self.assertTrue(ok.get(q).available)
        self.assertEqual(ok.errors[q.qid], "OK")

    def test_R30_research_waits_on_rate_limit_then_succeeds(self):
        from fbot.research import Service
        from tests.fakes import deps_for, fixture
        q, _ = fixture("binary_long")
        class Limited(Exception):
            status_code = 429
        calls, naps = [], []
        def fetch(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise Limited("slow down")
            return ("news", 2)
        service = Service({"ASKNEWS_API_KEY": "FAKEKEY123"}, deps_for({}).state, fetch)
        service.sleep = naps.append
        self.assertTrue(service.get(q).available)
        self.assertEqual((len(calls), naps, service.errors[q.qid]), (3, [5, 10], "OK"))
        calls.clear(); naps.clear()
        def always(*a, **k):
            calls.append(1)
            raise Limited("slow down")
        capped = Service({"ASKNEWS_API_KEY": "FAKEKEY123"}, deps_for({}).state, always)
        capped.sleep = naps.append
        self.assertFalse(capped.get(q).available)
        self.assertEqual((len(calls), naps, capped.errors[q.qid]), (4, [5, 10, 15], "Limited:429"))
        calls.clear(); naps.clear()
        def other(*a, **k):
            calls.append(1)
            raise ValueError("x")
        plain = Service({"ASKNEWS_API_KEY": "FAKEKEY123"}, deps_for({}).state, other)
        plain.sleep = naps.append
        self.assertFalse(plain.get(q).available)
        self.assertEqual((len(calls), naps), (2, []))

    def test_R24_single_prediction_is_not_reaggregated(self):
        module, _ = load_adapter()
        bot = module.FBot()
        for value in (0.37, object(), [0.0, 0.5, 1.0]):
            self.assertIs(asyncio.run(bot._aggregate_predictions([value], object())), value)

    def test_R24_other_lengths_await_base_aggregator(self):
        module, sdk = load_adapter()
        bot = module.FBot()
        question, expected = object(), object()
        calls = []

        async def aggregate(instance, predictions, raw_question):
            calls.append((instance, predictions, raw_question))
            return expected

        with patch.object(sdk.ForecastBot, "_aggregate_predictions", new=aggregate, create=True):
            for predictions in ([], [object(), object()]):
                with self.subTest(length=len(predictions)):
                    self.assertIs(asyncio.run(bot._aggregate_predictions(predictions, question)), expected)
                    self.assertIs(calls[-1][0], bot)
                    self.assertIs(calls[-1][1], predictions)
                    self.assertIs(calls[-1][2], question)
        self.assertEqual(len(calls), 2)

    def test_R25_transport_user_agent_and_caller_headers(self):
        default = "SextantBot/1.0 (+https://github.com/devonmvfisher/metac-bot-template)"
        requests = []

        class Response:
            status = 200
            def __init__(self): self.body = b"{}"
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                data, self.body = self.body, b""
                return data

        def opened(request, **kwargs):
            requests.append(request)
            return Response()

        cases = (None, {}, {"Authorization": "Bearer FAKEKEY123"},
                 {"User-Agent": "FixtureClient/1.0", "Content-Type": "application/json"})
        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=opened)):
            for headers in cases:
                original = None if headers is None else dict(headers)
                with self.subTest(headers=original):
                    self.assertEqual(llm.transport("GET", "https://example.invalid/fixture", headers, None, 10), (200, {}))
                    request = requests[-1]
                    self.assertEqual(request.get_header("User-agent"), (headers or {}).get("User-Agent", default))
                    for name, value in (headers or {}).items():
                        self.assertEqual(request.get_header(name.capitalize()), value)
                    self.assertEqual(headers, original)
        self.assertEqual(len(requests), len(cases))

    def test_R26_execute_disables_research_summarization(self):
        module, _ = load_adapter()
        clock = FakeClock()
        state = RunState(clock)
        pacer = SimpleNamespace(refresh=lambda: (100, 100))

        async def no_polls(*args, **kwargs):
            pass

        with patch.object(module, "Client", return_value=object()), \
             patch.object(module, "Pacer", return_value=pacer), \
             patch.object(module, "Service", return_value=object()), \
             patch.object(module, "open_count", return_value=0), \
             patch.object(module, "install_post_gate"), patch.object(state, "write"), \
             patch.object(module.schedule, "run_targets", new=no_polls), \
             patch.object(module, "FBot", wraps=module.FBot) as constructor:
            args = SimpleNamespace(mode="tournament", loop_minutes=45, poll_minutes=10)
            asyncio.run(module.execute(args, {"BOT_ENABLED": "true"}, clock, state))
        self.assertEqual(constructor.call_count, 2)
        for call in constructor.call_args_list:
            self.assertIs(call.kwargs["enable_summarize_research"], False)
