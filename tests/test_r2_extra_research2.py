"""Extra checks for untrusted reply accounting and citations."""
import unittest
from fbot import research2, webread
from .fakes import FakeClock
from .r2_fakes import FakeSend, SENTINEL, question, reply


class ExtraResearchTests(unittest.TestCase):
    def test_invalid_usage_cost_is_unknown_and_not_charged(self):
        for cost in (True, False, -1, float("nan"), float("inf"), "0.02", None):
            with self.subTest(cost=repr(cost)):
                status, data = reply("ok")
                data["usage"]["cost"] = cost
                clock = FakeClock()
                tally = webread.Tally()
                service = research2.WebResearch({"OPENROUTER_API_KEY": SENTINEL}, clock,
                                               send=FakeSend(clock, [(status, data)]), tally=tally)
                result = service.brief(question())
                self.assertEqual(result.status, "ok")
                self.assertIsNone(result.cost_usd)
                self.assertEqual(tally.spend(), {"research2_usd": 0.0})

    def test_citations_ignore_bad_shapes_and_keep_unique_http_urls(self):
        valid = {"type": "url_citation", "url_citation": {"url": "https://example.org/a", "title": " A \n B "}}
        annotations = [None, 7, {}, {"type": "other"}, valid, valid,
                       {"type": "url_citation", "url_citation": {"url": "file:///data", "title": "bad"}},
                       {"type": "url_citation", "url_citation": {"url": "https://["}}]
        self.assertEqual(research2.sources_of({"annotations": annotations}), [("https://example.org/a", "A B")])
