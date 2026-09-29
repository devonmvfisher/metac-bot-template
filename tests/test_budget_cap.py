"""Null-limit cap regression checks through the real client and pacer."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from fbot import SkipQuestion, config, ops
from fbot.budget import Pacer
from fbot.llm import Client
from fbot.state import RunState
from .fakes import FakeClock, fixture


class BudgetCapTests(unittest.TestCase):
    def make(self, data=None, env=None):
        settings={'OPENROUTER_API_KEY':'FAKEKEY123','MODEL_PRESET':'A'}
        settings.update(env or {})
        clock=FakeClock()
        state=RunState(clock)
        records=[]
        response=data if data is not None else {'usage':4.5,'limit':None,'limit_remaining':None}
        def send(method,url,headers,body,timeout,**kwargs):
            records.append(method)
            return 200, {'data':dict(response)}
        client=Client(settings,clock,state.alert,send)
        return client,Pacer(settings,client,state,clock),state,response,records

    def test_CAP01_null_limit_restores_preset_A(self):
        client,pacer,state,_,_=self.make(env={'BUDGET_CAP_USD':'80'})
        self.assertEqual(client.key(),(75.5,80))
        self.assertEqual(client.credit_source,'cap_minus_usage')
        self.assertEqual(client.key_usage,4.5)
        pacer.refresh()
        self.assertNotIn('CREDIT_UNKNOWN',state.alerts)
        self.assertEqual(pacer.tier(fixture('binary_long')[0]),'A')
        self.assertEqual(state.snapshot()['credit_source'],'cap_minus_usage')

    def test_CAP02_unset_and_blank_remain_unknown(self):
        for env in ({},{'BUDGET_CAP_USD':''},{'BUDGET_CAP_USD':' '}):
            client,pacer,state,_,_=self.make(env=env)
            self.assertEqual(client.key(),(None,None))
            pacer.refresh()
            self.assertIn('CREDIT_UNKNOWN',state.alerts)
            self.assertEqual(pacer.tier(fixture('binary_long')[0]),'C')
            self.assertNotIn('BUDGET_CAP_INVALID',state.alerts)

    def test_CAP03_invalid_values_sanitized_once(self):
        for value in ('abc','-5','0','nan','inf','1e9','FAKEKEY123'):
            with self.assertLogs('fbot',level='INFO') as captured:
                client,pacer,state,_,_=self.make(env={'BUDGET_CAP_USD':value,'BUDGET_CAP_OWN_USD':value})
                client.key()
                client.key()
            self.assertEqual(state.counts['budget_cap_invalid'],2)
            self.assertEqual(len(captured.output),2)
            self.assertEqual(captured.output[0],'INFO:fbot:CONFIG BUDGET_CAP_USD invalid; ignored')
            self.assertEqual(captured.output[1],'INFO:fbot:CONFIG BUDGET_CAP_OWN_USD invalid; ignored')
            self.assertIn('BUDGET_CAP_INVALID',state.alerts)
            self.assertNotIn('BUDGET_CAP_INVALID',ops.P0)
            self.assertEqual(ops.safe_counts(state.snapshot())['counts']['budget_cap_invalid'],2)
            self.assertIsNone(client.key()[0])

    def test_CAP04_invalid_usage_never_creates_credit(self):
        for value in (None,'4',True,-1,float('nan'),float('inf')):
            client,pacer,state,_,_=self.make({'usage':value}, {'BUDGET_CAP_USD':'80'})
            self.assertEqual(client.key(),(None,None))
            pacer.refresh()
            self.assertIn('CREDIT_UNKNOWN',state.alerts)
        client,_,_,_,_=self.make({}, {'BUDGET_CAP_USD':'80'})
        self.assertEqual(client.key(),(None,None))

    def test_CAP05_lower_balance_and_lower_limit_win(self):
        client,pacer,state,_,_=self.make({'limit_remaining':20,'limit':60,'usage':70},{'BUDGET_CAP_USD':'80'})
        self.assertEqual(client.key(),(10,60))
        pacer.refresh()
        self.assertIn('CREDITS_LOW',state.alerts)
        self.assertEqual(client.credit_source,'cap_minus_usage')

    def test_CAP06_exhaustion_without_model_call_and_flash_floor(self):
        client,pacer,state,data,records=self.make(env={'BUDGET_CAP_USD':'5'})
        pacer.refresh()
        with self.assertRaisesRegex(SkipQuestion,'EXHAUSTED'):
            pacer.tier(fixture('binary_long')[0])
        self.assertIn('CREDITS_EXHAUSTED',state.alerts)
        self.assertNotIn('POST',records)
        client,pacer,state,_,_=self.make({'usage':77.6},{'BUDGET_CAP_USD':'80'})
        pacer.refresh()
        self.assertEqual(pacer.tier(fixture('binary_long')[0]),'M')

    def test_CAP07_refresh_spend_delta(self):
        client,pacer,state,data,_=self.make({'usage':4.0},{'BUDGET_CAP_USD':'80'})
        pacer.refresh()
        data['usage']=6.5
        pacer.refresh()
        self.assertEqual((state.credit_before,state.credit_after,state.spend()),(76,73.5,2.5))

    def test_CAP08_minibench_matches_key_limit_modes(self):
        q=replace(fixture('binary_long')[0],target='minibench')
        for mode in ('always','slack','off'):
            for remaining in (1000,400,100,40,2.4,1,None):
                results=[]
                for capped in (False,True):
                    env={'MINIBENCH_MODE':mode,'BUDGET_CAP_USD':'2000' if capped else ''}
                    data={'usage':None if remaining is None else 2000-remaining} if capped else {'limit_remaining':remaining,'limit':2000}
                    _,pacer,_,_,_=self.make(data,env)
                    pacer.refresh()
                    try:
                        results.append(pacer.tier(q))
                    except SkipQuestion as error:
                        results.append(error.reason)
                self.assertEqual(results[0],results[1])

    def test_CAP09_workflow_variables(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('run_bot_on_tournament.yaml','run_bot_on_timer.yaml','test_bot.yaml'):
            text=(root/'.github/workflows'/name).read_text()
            section=text.split('      - name: Run bot',1)[1].split('      - name:',1)[0]
            for variable in ('BUDGET_CAP_USD','BUDGET_CAP_OWN_USD'):
                self.assertIn(variable+': ${{ vars.'+variable+' }}',section)

    def test_CAP10_nested_key_data_only(self):
        client,_,_,_,_=self.make(env={'BUDGET_CAP_USD':'80'})
        client.send=lambda *a,**k:(200,{'limit_remaining':100,'data':{'usage':4}})
        self.assertEqual(client.key(),(76,80))
        client,_,_,_,_=self.make()
        client.send=lambda *a,**k:(200,{'limit_remaining':100,'data':{'usage':4}})
        self.assertEqual(client.key(),(None,None))

    def test_CAP11_limit_source_and_parse_once(self):
        client,_,_,_,_=self.make({'limit_remaining':10,'limit':80,'usage':4},{'BUDGET_CAP_USD':'80'})
        client.env['BUDGET_CAP_USD']='FAKEKEY123'
        self.assertEqual(client.key(),(10,80))
        self.assertEqual(client.credit_source,'key_limit')
        client,_,_,_,_=self.make({'usage':0},{'BUDGET_CAP_USD':'10000'})
        self.assertEqual(client.key(),(10000,10000))

    def test_CAP12_own_key_web_reserve_and_measurement(self):
        env={'OWN_KEY_MODE':'primary','OPENROUTER_API_KEY_OWN':'FAKEKEY123','BUDGET_CAP_USD':'80','BUDGET_CAP_OWN_USD':'90'}
        client,pacer,state,data,_=self.make(env=env)
        self.assertEqual(client.key('own'),(85.5,90))
        self.assertEqual(client.credit_sources['own'],'cap_minus_usage')
        self.assertEqual(client.balances['own'],85.5)
        pacer.refresh()
        q=fixture('binary_long')[0]
        self.assertTrue(pacer.reserve_web(q))
        before=pacer.measure_start()
        data['usage']=5
        pacer.measure_end(q,'C',before)
        self.assertEqual(state.credit_after,75)
        self.assertEqual(client.balances['router'],75)

    def test_CAP13_raw_cap_text_never_reaches_logs_or_snapshot(self):
        raw='FAKEKEY123\nSECRET-CAP-XYZ'
        with self.assertLogs('fbot',level='INFO') as captured:
            client,pacer,state,_,_=self.make(env={'BUDGET_CAP_USD':raw})
            pacer.refresh()
        public='\n'.join(captured.output)+json.dumps(state.snapshot())
        self.assertNotIn('FAKEKEY',public)
        self.assertNotIn('SECRET-CAP-XYZ',public)

    def test_CAP14_credit_read_keeps_last_known_balance(self):
        # A model call made while the credit read waits must still see the last known balance.
        env={'OPENROUTER_API_KEY_OWN':'FAKEKEY123','OWN_KEY_MODE':'insurance'}
        client,_,_,_,_=self.make({'limit_remaining':2.5,'limit':80},env)
        client.balances.update(router=2.5,own=50)
        self.assertEqual(client.key_order('season'),['own'])
        seen=[]
        def send(method,url,headers,body,timeout,**kwargs):
            seen.append((client.balances.get('router'),client.key_order('season')))
            return 200,{'data':{'limit_remaining':2.5,'limit':80}}
        client.send=send
        self.assertEqual(client.key(),(2.5,80))
        self.assertEqual(seen,[(2.5,['own'])])
        self.assertEqual(client.key_order('season'),['own'])
        # A failed read keeps the last known balance and marks its source unknown.
        self.assertEqual(client.credit_source,'key_limit')
        def fail(*args,**kwargs):
            raise OSError('FAKEKEY123 provider text')
        client.send=fail
        self.assertEqual(client.key(),(None,None))
        self.assertEqual(client.balances['router'],2.5)
        self.assertEqual((client.credit_source,client.credit_sources['router'],client.key_usage),('unknown','unknown',None))
        self.assertEqual(client.key_order('season'),['own'])
