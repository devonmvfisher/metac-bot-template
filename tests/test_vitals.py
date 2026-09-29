"""Counts-only vitals contract, with real code and offline boundary fakes."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import CreditExhausted, ModelFailure, REASONS, SkipQuestion, config, ops
from fbot.budget import Pacer
from fbot.llm import Client
from fbot.state import ALERTS, RunState
from .adapter_fakes import load_adapter, bot_for, raw_question
from .fakes import FakeClock, FakeGit, FakeGitHub, FakeLLM, fixture


class VitalsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock('2026-10-01T08:46:01Z')
        self.root = Path(tempfile.mkdtemp(prefix='vitals-'))
        self.addCleanup(__import__('shutil').rmtree, self.root)
        self.env = {'BOT_ENABLED':'true', 'CHAIN_ENABLED':'true',
                    'GITHUB_EVENT_NAME':'workflow_dispatch', 'JOB_START':'2026-10-01T08:00:48Z',
                    'GITHUB_RUN_NUMBER':'57', 'GITHUB_RUN_ATTEMPT':'1',
                    'INSTALL_OUTCOME':'success', 'RUN_OUTCOME':'success'}
        self.state = RunState(self.clock)
        self.state.loop_min, self.state.poll_min = 45, 10
        self.state.open = {'season':12, 'minibench':'unknown:http_403'}
        self.state.seen['season'].update((987654321,987654322))
        self.state.posted.add(987654321)
        self.state.commented.add(987654321)
        self.state.attempted_by_target['season'] = 1
        self.state.counts['attempted'] = 1
        self.state.tiers_by_target['season']['A'] = 1
        self.state.skips_by_target['season']['ALREADY_FORECAST'] = 1
        self.state.model_preset = 'A'
        self.state.record_model(config.SOL[0], True, {'prompt_tokens':800, 'completion_tokens':150, 'cost':1.126})
        self.state.parser_calls.update(calls=1,err=0)
        self.state.record_research(False, 'TimeoutError')
        self.state.research_calls['none'] = 1
        self.state.credit_source = 'cap_minus_usage'
        self.state.credit_before, self.state.credit_after, self.state.limit = 76, 73.5, 80
        self.data = self.state.snapshot()

    def record(self, data=None, **kwargs):
        return ops.vitals(self.data if data is None else data, self.env,
                          {'dispatched':1,'via':'rest','error':'-'}, {}, now=self.clock.now(), **kwargs)

    def execute(self, data=None, github=None, **kwargs):
        output = []
        github = github or FakeGitHub(self.clock.now())
        env = dict(self.env, JOB_START=str(self.clock.now().timestamp()-3000))
        code = ops.run(env, self.data if data is None else data, self.clock, github,
                       FakeGit(self.clock.now()), self.root, emit=output.append, **kwargs)
        return code, output

    def test_V01_whitelist_schema(self):
        record = self.record(alerts={'SKIPS','RUN_FAILED','SECRET-TITLE-XYZ'})
        self.assertEqual(set(record), {'v','rel','run','att','ev','t0','t1','dur_s','loop_min','poll_min',
            'polls','poll_err','open','q','tiers','preset','models','parser','research','credit',
            'gate_blocks','memo_hits','cov','alerts','next','outcome'})
        self.assertEqual((record['v'],record['rel'],record['run'],record['att']), (1,config.VERSION,57,1))
        self.assertEqual((record['t0'],record['t1'],record['dur_s']),
                         ('2026-10-01T08:00:48Z','2026-10-01T08:46:01Z',2713))
        for name in ('polls','poll_err','open','q','tiers'):
            self.assertEqual(set(record[name]), {'season','minibench'})
        self.assertEqual(record['q']['season'], {'seen':2,'attempted':1,'posted':1,'commented':1,
                                                'skip':{'ALREADY_FORECAST':1}})
        self.assertEqual(record['q']['minibench'], {'seen':0,'attempted':0,'posted':0,'commented':0,'skip':{}})
        self.assertEqual(record['models'], {config.SOL[0]:dict(calls=1,ok=1,err=0,in_tok=800,out_tok=150,usd=1.13)})
        self.assertEqual(record['parser'], {'calls':1,'err':0})
        self.assertEqual(record['research'], {'calls':1,'ok':0,'none':1,'err':{'TimeoutError':1}})
        self.assertEqual(record['credit'], {'src':'cap_minus_usage','before':76,'after':73.5,'limit':80,'spend':2.5,'per_attempt':2.5})
        self.assertEqual(record['next'], {'dispatched':1,'via':'rest','err':'-'})
        self.assertEqual(record['outcome'], {'install':'success','run':'success'})
        self.assertEqual(record['alerts'], ['RUN_FAILED','SKIPS'])
        self.assertEqual(record['cov'], {})
        allowed = set(config.PROBES + config.BRIDGE) | set(REASONS) | set(ALERTS) | set(config.TIERS)
        allowed.update(('v1','auto','A','B','C','schedule','workflow_dispatch','other','cap_minus_usage',
                        'key_limit','unknown','gh','rest','none','-','success','failure','cancelled','skipped'))
        def leaves(value):
            if isinstance(value, dict):
                for child in value.values(): leaves(child)
            elif isinstance(value, list):
                for child in value: leaves(child)
            elif isinstance(value, str):
                self.assertTrue(value in allowed or re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+Z|unknown:(http_\d{3}|shape|cap|error)', value))
            else:
                self.assertTrue(value is None or isinstance(value, (int,float)))
        leaves(record)

    def test_V02_privacy_question_forecast_comment_and_ids(self):
        self.data.update(question={'title':'SECRET-TITLE-XYZ','qid':987654321},
                         forecast=0.37, comment='PRIVATE-COMMENT-XYZ', post=987654322,
                         comment_failed=[987654321,987654322])
        coverage = {'season':{'closed':2,'forecasted':1,'missed':[987654321,987654322]}}
        self.env['GITHUB_STEP_SUMMARY'] = str(self.root/'summary.md')
        code, output = self.execute(coverage_reader=lambda: coverage)
        self.assertEqual(code, 0)
        vitals = '\n'.join(line for line in output if line.startswith(('VITALS ', '::notice')))
        vitals += (self.root/'summary.md').read_text()
        for forbidden in ('SECRET-TITLE-XYZ','987654321','987654322','0.37','PRIVATE-COMMENT-XYZ','missed'):
            self.assertNotIn(forbidden, vitals)
        record = json.loads(output[-1][7:])
        self.assertEqual(record['cov'], {'season':{'closed':2,'forecasted':1}})
        self.assertEqual(record['q']['season']['posted'], 1)

    def test_V03_escaping_and_emitter(self):
        value = '{"test":"100%\r\n"}'
        expected = '{"test":"100%25%0D%0A"}'
        self.assertEqual(ops.escape_annotation(value), expected)
        output = []
        with patch.object(ops.json, 'dumps', return_value=value):
            ops.emit_vitals({}, {}, {}, {}, self.clock, set(), output.append)
        self.assertEqual(output, ['::notice title=SEXTANT_VITALS v1::'+expected, 'VITALS '+value])

    def test_V04_size_and_unknown_models_collapse(self):
        self.data['model_calls'] = {'unapproved-model-'+str(i):dict(calls=1,ok=1,err=0,in_tok=100,out_tok=20,usd=.1) for i in range(40)}
        self.data['skips_by_target'] = {target:{**{str(i):1 for i in range(40)}, **{r:1 for r in REASONS}} for target in ('season','minibench')}
        self.data['research_calls']['err'] = {'ExampleError'+chr(65+i//26)+chr(65+i%26):1 for i in range(40)}
        record = self.record()
        compact = json.dumps(record,separators=(',',':'))
        self.assertLess(len(compact.encode()), 4096)
        self.assertEqual(set(record['models']), {'other'})
        self.assertEqual(record['models']['other']['calls'], 40)
        self.assertEqual(record['models']['other']['usd'], 4.0)
        self.assertNotIn('unapproved-model-', compact)
        self.assertEqual(set(record['q']['season']['skip']), set(REASONS))

    def test_V05_failure_preserves_exit_codes(self):
        for test_failed, expected in ((False,0),(True,1)):
            with patch.object(ops, 'vitals', side_effect=ValueError('PRIVATE-COMMENT-XYZ')):
                code, output = self.execute(test=True,test_failed=test_failed)
            self.assertEqual(code, expected)
            self.assertEqual(output[-1], 'VITALS UNAVAILABLE')
            self.assertNotIn('PRIVATE-COMMENT-XYZ','\n'.join(output))
        self.env['GITHUB_STEP_SUMMARY'] = str(self.root/'missing'/'summary.md')
        code, output = self.execute(test=True)
        self.assertEqual(code, 0)
        self.assertEqual(output[-1], 'VITALS UNAVAILABLE')
        with patch.object(ops, 'vitals', side_effect=ValueError()):
            ops.emit_vitals({}, {}, {}, {}, self.clock, set(), lambda value: (_ for _ in ()).throw(OSError()))

    def test_V06_next_matches_dispatch_result(self):
        for route in ('gh','rest','error','off'):
            github = FakeGitHub(self.clock.now())
            if route == 'error':
                github.dispatch = lambda branch: (_ for _ in ()).throw(TimeoutError('SECRET-TITLE-XYZ'))
            else:
                github.dispatch = lambda branch: route
            self.env['CHAIN_ENABLED'] = 'false' if route == 'off' else 'true'
            code, output = self.execute(github=github)
            record = json.loads(output[-1][7:])
            expected = {'dispatched':int(route in ('gh','rest')), 'via':route if route in ('gh','rest') else 'none',
                        'err':'TimeoutError' if route == 'error' else '-'}
            self.assertEqual(record['next'], expected)
            self.assertIn('CHAIN dispatched={dispatched} via={via} error={err}'.format(**expected), output)
            self.assertEqual(code, 0)

    def test_V07_exactly_one_notice_at_every_return_path(self):
        for path in ('test_ok','test_failed','test_issues_disabled','read_issues_disabled','write_issues_disabled','normal'):
            github = FakeGitHub(self.clock.now())
            if path in ('test_issues_disabled','read_issues_disabled'):
                github.disabled = True
            if path == 'write_issues_disabled':
                github.create = lambda *args: (_ for _ in ()).throw(ops.ApiError(410))
            code, output = self.execute(github=github, test=path.startswith('test'), test_failed=path=='test_failed')
            notices = [line for line in output if line.startswith('::notice title=SEXTANT_VITALS v1::')]
            plain = [line for line in output if line.startswith('VITALS {')]
            self.assertEqual((len(notices),len(plain)), (1,1), path)
            self.assertEqual(output[-2:], notices+plain, path)
            self.assertEqual(notices[0].split('::',2)[2], ops.escape_annotation(plain[0][7:]))
            self.assertEqual(code, int(path not in ('test_ok','normal')), path)

    def test_V08_unknown_snapshot_keys_never_forwarded(self):
        self.data.update(forecast=0.37, title='SECRET-TITLE-XYZ', missed=[987654321])
        self.data['skips_by_target']['season']['SECRET-TITLE-XYZ'] = 1
        self.data['model_calls']['SECRET-TITLE-XYZ'] = {'calls':1,'ok':0,'err':1,'text':'PRIVATE-COMMENT-XYZ'}
        self.data['research_calls']['err']['raw:SECRET-TITLE-XYZ'] = 1
        self.env.update(GITHUB_RUN_NUMBER='987654321.5',GITHUB_RUN_ATTEMPT='no',GITHUB_EVENT_NAME='private_event',
                        INSTALL_OUTCOME='SECRET-TITLE-XYZ',RUN_OUTCOME='PRIVATE-COMMENT-XYZ',JOB_START='bad')
        record = self.record()
        output = json.dumps(record)
        for forbidden in ('SECRET-TITLE-XYZ','PRIVATE-COMMENT-XYZ','0.37','987654321','forecast"','title','missed','private_event'):
            self.assertNotIn(forbidden, output)
        self.assertEqual((record['run'],record['att'],record['t0'],record['dur_s']), (None,None,None,None))
        self.assertEqual(record['ev'], 'other')
        self.assertEqual(record['outcome'], {'install':'unknown','run':'unknown'})
        self.assertEqual(record['models']['other']['calls'], 1)

    def test_V09_usage_body_sponsored_own_and_bridge(self):
        self.state = RunState(self.clock)
        for provider in ('router','own','bridge'):
            fake = FakeLLM()
            env = {'OPENROUTER_API_KEY':'FAKEKEY123','OPENROUTER_API_KEY_OWN':'FAKEKEY123',
                   'OPENAI_API_KEY':'FAKEKEY123','OWN_KEY_MODE':'primary' if provider=='own' else 'off'}
            client = Client(env, self.clock, self.state.alert, fake)
            client.balances.update(router=80,own=80)
            client.one(config.BRIDGE[0] if provider=='bridge' else config.SOL[0], 'SYNTHETIC', bridge=provider=='bridge')
            body = fake.calls[-1][3]
            if provider == 'bridge':
                self.assertNotIn('usage', body)
            else:
                self.assertEqual(body['usage'], {'include':True})
            self.assertEqual(sum(row['calls'] for row in self.state.model_calls.values()), ('router','own','bridge').index(provider)+1)

    def test_V10_http_attempts_and_numeric_usage_only(self):
        good = {'choices':[{'message':{'content':'OK'}}], 'usage':{'prompt_tokens':120,'completion_tokens':30,'cost':.125}}
        fake = FakeLLM({config.SOL[0]:[(500,{}),(200,good)]})
        state = RunState(self.clock)
        client = Client({'OPENROUTER_API_KEY':'FAKEKEY123'}, self.clock, state.alert, fake)
        client.one(config.SOL[0], 'SYNTHETIC')
        self.assertEqual(state.model_calls[config.SOL[0]], dict(calls=2,ok=1,err=1,in_tok=120,out_tok=30,usd=.125))
        for value in ('SECRET-TITLE-XYZ',True,-1,float('nan'),float('inf'),None,10**400):
            state.record_model('SECRET-TITLE-XYZ', True, dict(prompt_tokens=value,completion_tokens=value,cost=value))
        self.assertEqual(state.model_calls['other'], dict(calls=7,ok=7,err=0,in_tok=0,out_tok=0,usd=0))
        before = sum(row['calls'] for row in state.model_calls.values())
        with self.assertRaises(ModelFailure):
            client.one(config.SOL[0], 'SYNTHETIC', deadline=self.clock.monotonic())
        self.assertEqual(sum(row['calls'] for row in state.model_calls.values()), before)
        fake.scripts[config.SOL[0]] = [(402,{})]
        with self.assertRaises(CreditExhausted):
            client.one(config.SOL[0], 'SYNTHETIC')
        self.assertEqual(state.model_calls[config.SOL[0]]['err'], 2)
        fake.scripts[config.FLASH[0]] = [(200,{'choices':[]})]
        client.exhausted.clear()
        with self.assertRaises(ModelFailure):
            client.one(config.FLASH[0], 'SYNTHETIC')
        self.assertEqual(state.model_calls[config.FLASH[0]]['err'], 1)

    def test_V11_state_snapshot_counts_and_target_tiers(self):
        q, _ = fixture('binary_long')
        q = replace(q, qid=987654321, target='season')
        state = RunState(self.clock)
        for _ in range(2): state.mark_seen(q)
        state.skip(q,'ALREADY_FORECAST')
        state.posted.add(q.qid)
        state.commented.add(q.qid)
        client = SimpleNamespace(key=lambda: (80,80), exhausted=set())
        pacer = Pacer({'MODEL_PRESET':'A','OPENROUTER_API_KEY':'FAKEKEY123'},client,state,self.clock)
        pacer.refresh()
        tier = pacer.tier(q)
        snapshot = state.snapshot()
        self.assertEqual(snapshot['seen']['season'], 1)
        self.assertEqual(snapshot['skips_by_target']['season'], {'ALREADY_FORECAST':1})
        self.assertEqual(snapshot['tiers_by_target']['season'], {tier:1})
        self.assertEqual(snapshot['posted_by_target']['season'], 1)
        self.assertEqual(snapshot['commented_by_target']['season'], 1)
        self.assertNotIn('987654321', json.dumps(snapshot))
        state.record_research(False,'TimeoutError:429')
        state.record_research(True)
        self.assertEqual(state.snapshot()['research_calls'], {'calls':2,'ok':1,'none':0,'err':{'http_429':1}})

    def test_V12_main_parser_calls_and_errors(self):
        module,sdk = load_adapter()
        bot = bot_for(module)
        bot.f_env['OPENROUTER_API_KEY'] = 'FAKEKEY123'
        bot.f_client = Client(bot.f_env,bot.f_clock,bot.f_state.alert,FakeLLM())
        q = bot.question(raw_question(sdk))
        async def fail(*args,**kwargs): raise ValueError('PRIVATE-COMMENT-XYZ')
        with patch.object(module,'structure_output',fail):
            with self.assertRaises(SkipQuestion):
                asyncio.run(bot.structure(q,'SYNTHETIC',object))
        self.assertEqual(bot.f_state.parser_calls, {'calls':2,'err':2})
        async def succeed(*args,**kwargs): return 'OK'
        with patch.object(module,'structure_output',succeed):
            self.assertEqual(asyncio.run(bot.structure(q,'SYNTHETIC',object)), 'OK')
        self.assertEqual(bot.f_state.parser_calls, {'calls':3,'err':2})

    def test_V13_main_seen_and_no_research(self):
        module,sdk = load_adapter()
        bot = bot_for(module)
        raw = raw_question(sdk, id_of_question=987654321)
        with patch.object(module,'already_forecast',return_value=False):
            asyncio.run(bot.run_research(raw))
        self.assertEqual(bot.f_state.snapshot()['seen']['season'], 1)
        self.assertEqual(bot.f_state.research_calls['none'], 1)
        with patch.object(module,'already_forecast',return_value=True):
            with self.assertRaises(SkipQuestion):
                asyncio.run(bot.run_research(raw))
        self.assertEqual(bot.f_state.snapshot()['seen']['season'], 1)
        self.assertEqual(bot.f_state.skips_by_target['season']['ALREADY_FORECAST'], 1)

    def test_V14_execute_research_attempts_and_snapshot_settings(self):
        module, _ = load_adapter()
        clock, state = FakeClock(), RunState(FakeClock())
        captured = []
        original_service = module.Service
        def service(env, state, fetch, **kwargs):
            captured.append(fetch)
            return original_service(env,state,fetch,**kwargs)
        def opened(target, env, details):
            details['reason'] = 'http_403' if target == config.MINIBENCH_ID else 'ok'
            return 'unknown' if target == config.MINIBENCH_ID else 7
        failure = TimeoutError('SECRET-TITLE-XYZ')
        failure.status_code = 429
        async def run(*args, **kwargs):
            with self.assertRaises(TimeoutError): captured[0]('SYNTHETIC',{})
            self.assertEqual(captured[0]('SYNTHETIC',{}), ('',0))
            self.assertEqual(captured[0]('SYNTHETIC',{}), ('SYNTHETIC brief',1))
        client = SimpleNamespace(key=lambda: (None,None),exhausted=set())
        with patch.object(module,'Client',return_value=client), patch.object(module,'Service',side_effect=service), patch.object(module,'ask_news',side_effect=[failure,('',0),('SYNTHETIC brief',1)]), patch.object(module,'open_count',side_effect=opened), patch.object(module,'install_post_gate'), patch.object(module.schedule,'run_targets',side_effect=run), patch.object(state,'write'):
            asyncio.run(module.execute(SimpleNamespace(mode='tournament',loop_minutes=45,poll_minutes=10), {'BOT_ENABLED':'true','DAILY_PROBE':'false'}, clock, state))
        snapshot = state.snapshot()
        self.assertEqual((snapshot['loop_min'],snapshot['poll_min']), (45,10))
        self.assertEqual(snapshot['open'], {'season':7,'minibench':'unknown:http_403'})
        self.assertEqual(snapshot['research_calls'], {'calls':3,'ok':1,'none':0,'err':{'http_429':1}})
        self.assertNotIn('SECRET-TITLE-XYZ',json.dumps(snapshot))
