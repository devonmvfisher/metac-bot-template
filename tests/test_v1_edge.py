"""EDGE-2 exact off paths, registered title rule and monthly search cap."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import config, edge, ledger, prompts_v1
from fbot.pipeline import forecast
from fbot.research import Service
from fbot.types import Research
from .adapter_fakes import load_adapter
from .fakes import FakeClock, deps_for, fixture, narration


class EdgeTests(unittest.TestCase):
    def test_E1_off_has_exact_existing_text_and_search_arguments(self):
        q, _ = fixture('binary_long')
        for env in ({}, {'ASKNEWS_PARITY': 'false'}, {'ASKNEWS_ARCHIVE': 'true'}):
            deps = deps_for({})
            calls = []
            def fetch(*args, **kwargs):
                calls.append((args, kwargs))
                return 'UNCHANGED\nTEXT', 6
            service = Service(dict(env, ASKNEWS_API_KEY='FAKEKEY'), deps.state, fetch)
            value = service.get(q)
            self.assertEqual(value, Research('UNCHANGED\nTEXT', 6, True))
            self.assertEqual(calls, [((q.title, {'api_key': 'FAKEKEY'}), {'timeout': 60, 'strategy': 'latest news', 'n_articles': 6})])

    def test_E1_template_dates_sources_and_unknown_fields(self):
        row = {'eng_title': 'Headline', 'summary': 'Fact', 'pub_date': '2026-10-01T01:00:00+01:00', 'source_id': 'source', 'article_url': 'https://example.org/fact', 'language': 'en'}
        self.assertIn('Publish date: October 01, 2026 12:00 AM UTC', edge.article(row))
        self.assertIn('Source:[source](https://example.org/fact)', edge.article(row))
        self.assertIn('Publish date: unknown', edge.article({}))
        self.assertIn('Source:[unknown]', edge.article({}))
        with self.assertLogs('fbot', level='INFO') as logs:
            edge.article_counts([row], FakeClock('2026-10-01T02:00:00Z').now())
        self.assertEqual(logs.output, ['INFO:fbot:ASKNEWS articles=1 oldest_h=2.0'])

    def test_E1_sdk_rows_and_off_bytes(self):
        import sys
        module, _ = load_adapter()
        row = {'eng_title': 'Title', 'summary': 'Fact', 'article_url': 'https://example.org', 'source_id': 'source'}
        searches = []
        def search(**kwargs):
            searches.append(kwargs)
            return SimpleNamespace(as_dicts=[row])
        fake = SimpleNamespace(AskNewsSDK=lambda **kwargs: SimpleNamespace(news=SimpleNamespace(search_news=search)))
        with patch.dict(sys.modules, asknews_sdk=fake):
            self.assertEqual(module.ask_news('question', {}, 60, 'latest news', 6), ('Title\nFact\nhttps://example.org', 1))
            on, count = module.ask_news('question', {}, 60, 'latest news', 6, parity=True, now=FakeClock().now())
        self.assertEqual(count, 1)
        self.assertIn('Source:[source]', on)
        self.assertEqual(searches, [{'query': 'question', 'n_articles': 6, 'return_type': 'both', 'strategy': 'latest news'}] * 2)

    def test_E1_archive_requires_both_switches_and_loaded_ledger(self):
        q, _ = fixture('binary_long')
        for parity in (False, True):
            for archive in (False, True):
                for loaded in (False, True):
                    for target in ('season', 'minibench', 'test'):
                        deps = deps_for({})
                        deps.state.attach_book(ledger.Ledger(available=loaded))
                        calls = []
                        def fetch(*args, **kwargs):
                            calls.append(kwargs)
                            return kwargs['strategy'], kwargs['n_articles']
                        env = {'ASKNEWS_API_KEY': 'FAKEKEY', 'ASKNEWS_PARITY': str(parity), 'ASKNEWS_ARCHIVE': str(archive)}
                        service = Service(env, deps.state, fetch)
                        service.get(replace(q, target=target))
                        expected = parity and archive and loaded and target == 'season'
                        self.assertEqual(len(calls), 2 if expected else 1)
                        if expected:
                            self.assertEqual((calls[1]['strategy'], calls[1]['n_articles']), ('news knowledge', 10))

    def test_E1_month_limit_recent_continues_and_rollover(self):
        q, _ = fixture('binary_long')
        deps, calls = deps_for({}), []
        deps.state.attach_book(ledger.Ledger({'asknews': {'2026-10': 894}}, available=True))
        def fetch(*args, **kwargs):
            calls.append(kwargs['strategy'])
            return 'FACT', kwargs['n_articles']
        service = Service({'ASKNEWS_API_KEY': 'FAKEKEY', 'ASKNEWS_PARITY': 'true', 'ASKNEWS_ARCHIVE': 'true'}, deps.state, fetch)
        service.get(q)
        self.assertEqual(deps.state.book.data['asknews']['2026-10'], 900)
        service.get(replace(q, qid=q.qid + 1))
        self.assertEqual(calls, ['latest news', 'news knowledge', 'latest news'])
        self.assertEqual(deps.state.book.data['asknews']['2026-10'], 901)
        deps.clock.date = deps.clock.now().replace(month=11)
        service.get(replace(q, qid=q.qid + 2))
        self.assertEqual(deps.state.book.data['asknews']['2026-11'], 6)

    def test_E2_all_159_registered_titles(self):
        data = json.loads((Path(__file__).parent / 'fixtures/edge2/deadline_titles.json').read_text())
        self.assertEqual(edge.DEADLINE.pattern, data['regex'])
        self.assertEqual(len(data['deadline']), 88)
        self.assertEqual(len(data['not_deadline']), 71)
        for group, expected in (('deadline', True), ('not_deadline', False)):
            for row in data[group]:
                self.assertIs(bool(edge.DEADLINE.search(row['title'].lower())), expected, str(row['post']))

    def test_E2_titles_file_is_seeded_synthetic(self):
        from collections import Counter
        from .edge2_titles import build, render
        path = Path(__file__).parent / 'fixtures/edge2/deadline_titles.json'
        raw = path.read_bytes()
        self.assertEqual(raw, render(build()).encode('ascii'))
        self.assertTrue(raw.isascii())
        data = json.loads(raw)
        self.assertEqual(data['_label'], 'SYNTHETIC')
        self.assertEqual(Counter(r['group'] for r in data['deadline']), {'spring':39,'summer':22,'minibench':27})
        self.assertEqual(Counter(r['group'] for r in data['not_deadline']), {'spring':26,'summer':20,'minibench':25})
        rows = data['deadline'] + data['not_deadline']
        self.assertEqual(len({r['post'] for r in rows}), 159)
        self.assertEqual(len({r['title'] for r in rows}), 159)
        self.assertTrue(all(930000 <= r['post'] <= 933999 for r in rows))
        def numbers(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from numbers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from numbers(item)
            elif isinstance(value, int) and not isinstance(value, bool):
                yield value
        for other in sorted((Path(__file__).parent / 'fixtures').rglob('*.json')):
            if other != path:
                self.assertFalse(any(930000 <= number <= 933999 for number in numbers(json.loads(other.read_text(encoding='utf-8')))), other.name)
        for name, expected in (('deadline',True),('not_deadline',False)):
            ids = [r['post'] for r in data[name]]
            self.assertEqual(ids, sorted(ids))
            for row in data[name]:
                self.assertEqual(row['title'].count('SYNTHETIC'), 1)
                self.assertEqual(row['title'].count('?'), 1)
                self.assertEqual(bool(edge.DEADLINE.search(row['title'].lower())), expected)

    def test_E2_titles_cover_every_branch(self):
        import re
        data = json.loads((Path(__file__).parent / 'fixtures/edge2/deadline_titles.json').read_text())
        months = 'january february march april may june july august september october november december'.split()
        month = r'\b(?:' + '|'.join(months) + r')\b'
        date = r'\b(?:20\d\d|' + '|'.join(months) + r')\b'
        keyword = r'\b(before|by)\b'
        yes = [r['title'].lower() for r in data['deadline']]
        no = [r['title'].lower() for r in data['not_deadline']]
        for title in yes:
            self.assertTrue(edge.DEADLINE.search(title))
        for title in no:
            self.assertFalse(edge.DEADLINE.search(title))
        self.assertTrue(any(re.search(keyword,t)[1] == 'by' for t in yes))
        suffixes = [t[re.search(keyword,t).end():] for t in yes]
        month_only = [t for t in suffixes if re.search(month,t) and not re.search(r'\b20\d\d\b',t)]
        self.assertGreaterEqual(len(month_only),2)
        self.assertLessEqual(len(month_only),5)
        self.assertTrue(any(re.search(r'\bmay\b',t) for t in month_only))
        self.assertTrue(all(any(re.search(r'\b'+m+r'\b',t) for t in suffixes) for m in months))
        self.assertTrue(any(re.search(r'\b20\d\d\b',t[:re.search(keyword,t).start()]) and re.search(r'\b20\d\d\b',t[re.search(keyword,t).end():]) for t in yes))
        self.assertTrue(any(re.search(r'\bby\b',t) and not re.search(date,t[re.search(r'\bby\b',t).end():]) for t in no))
        self.assertTrue(any(re.search(keyword,t.split('?')[0]) and re.search(date,t.split('?')[1]) and t.endswith(')') for t in no))
        self.assertTrue(any(not re.search(keyword,t) and re.search(r'\b(?:nearby|standby|whereby)\b',t) and re.search(date,t[re.search(r'\b(?:nearby|standby|whereby)\b',t).end():]) for t in no))
        self.assertTrue(any(re.search(r'\b\w*may\w+\b',t) and re.search(r'\b20\d\d\b',t) and not re.search(keyword,t) for t in no))

    def test_E2_registered_values_and_target_type_scope(self):
        q, _ = fixture('binary_long')
        q = replace(q, title='Will it happen before May 2027?')
        env = {'DEADLINE_SHIFT': 'true'}
        for value, expected in ((.5, .4444), (.2, .1667), (.1, .0816)):
            result, caps = edge.shift(q, value, env)
            self.assertEqual(round(result, 4), expected)
            self.assertEqual(caps, ['deadline-x0.8'])
        for question in (replace(q, target='minibench'), replace(q, kind='numeric'), replace(q, kind='multiple_choice')):
            self.assertEqual(edge.shift(question, .37, env), (.37, []))

    def test_E2_normal_fast_bridge_apply_before_caps(self):
        q, _ = fixture('binary_long')
        q = replace(q, title='Will it happen before May 2027?')
        for tier in ('A', 'B', 'C', 'M', 'BRIDGE'):
            for probability in (50, 99):
                text = narration(f'Probability: {probability}%')
                deps = deps_for({model: text for model in config.PROBES + config.BRIDGE})
                deps.env['DEADLINE_SHIFT'] = 'true'
                result = forecast(q, Research(), tier, deps)
                expected = .4444444444444444 if probability == 50 else (.95 if tier == 'BRIDGE' else .98)
                self.assertAlmostEqual(result.value, expected)
                self.assertIn('deadline-x0.8', result.caps)

    def test_E2_off_exact_prompt_and_forecast_identity(self):
        q, _ = fixture('binary_long')
        q = replace(q, title='Will it happen before May 2027?')
        records = []
        for setting in (None, 'false'):
            deps = deps_for({config.SOL[0]: narration('Probability: 50%')})
            if setting is not None:
                deps.env['DEADLINE_SHIFT'] = setting
            result = forecast(q, Research(), 'C', deps)
            records.append((result.value, result.comment, deps.client.calls[0][1]))
            self.assertEqual(result.value, .5)
            self.assertIn(prompts_v1.BASE_RATE_BINARY, deps.client.calls[0][1])
        self.assertEqual(records[0], records[1])

    def test_E2_prompt_reminder_changes_only_season_binary(self):
        q, _ = fixture('binary_long')
        clock = FakeClock()
        for target in ('season', 'minibench'):
            off = prompts_v1.build(replace(q, target=target), Research(), clock.now())
            on = prompts_v1.build(replace(q, target=target), Research(), clock.now(), deadline_shift=True)
            if target == 'minibench':
                self.assertEqual(off, on)
            else:
                self.assertIn(edge.BASE_RATE, on)
                self.assertNotIn(prompts_v1.BASE_RATE_BINARY, on)
