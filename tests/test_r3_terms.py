"""v1-r3 terms rules (DECISIONS.md): one honest User-Agent with a contact URL, hosts the bot never reads,
cleared hosts only for resolution pages, no Kalshi, and each market platform off until its own variable is true.

Written by Claude Opus 5.5 (v1-r3 build, Mon Sep 28 2026).
"""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fbot import llm, markets, sources, webread
from .fakes import FakeClock
from .r2_fakes import FakeJSON, FakeResolver, FakeWeb, captured_logs, market_routes, question
from .r3_fakes import MARKETS_ON

ROOT = Path(__file__).resolve().parents[1]
UA = "SextantBot/1.0 (+https://github.com/devonmvfisher/metac-bot-template)"
TITLE = "U.S. unemployment rate for September 2026, as first reported by the BLS: what will it be?"
FED = "https://www.federalreserve.gov/monetarypolicy/openmarket.htm"


class UserAgentTests(unittest.TestCase):
    def test_one_user_agent_for_pages_markets_and_models(self):
        self.assertEqual(webread.USER_AGENT, UA)
        seen = []

        def opened(request, **kwargs):
            seen.append(request.get_header("User-agent"))
            raise OSError("offline")
        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=opened)):
            with self.assertRaises(Exception):
                llm.transport("GET", "https://example.invalid/fixture", None, None, 5)
        self.assertEqual(seen, [UA])
        clock = FakeClock()
        web = FakeWeb(clock)
        web.add("https://example.org/p", status=200, headers={"Content-Type": "text/html"}, body="<p>x</p>")
        webread.get("https://example.org/p", clock=clock, open_url=web, resolve=FakeResolver())
        self.assertEqual(web.calls[0][1]["User-Agent"], UA)

    def test_robots_rules_written_for_sextantbot_apply(self):
        clock = FakeClock()
        web = FakeWeb(clock)
        web.add("https://www.bls.gov/robots.txt", status=200, headers={"Content-Type": "text/plain"},
                body="User-agent: SextantBot\nDisallow: /\n")
        reader = sources.Reader({}, clock, open_url=web, resolve=FakeResolver())
        brief = reader.read(question(resolution="Per https://www.bls.gov/news.release/x.htm."))
        self.assertEqual(brief.reasons, ("robots",))
        self.assertEqual(web.urls(), ["https://www.bls.gov/robots.txt"])


class DeniedHostTests(unittest.TestCase):
    def test_denied_hosts_get_no_request_or_lookup(self):
        self.assertEqual(webread.DENIED_HOSTS, ("kalshi.com", "stlouisfed.org", "unhcr.org"))
        self.assertIn("denied", webread.REASONS)
        clock = FakeClock()
        web = FakeWeb(clock)
        resolver = FakeResolver()
        for url in ("https://api.elections.kalshi.com/trade-api/v2/markets", "https://kalshi.com/markets/x",
                    "https://fred.stlouisfed.org/series/X", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=X",
                    "https://www.stlouisfed.org/", "https://data.unhcr.org/en/x", "https://www.unhcr.org/x",
                    "HTTPS://FRED.STLOUISFED.ORG/x"):
            self.assertEqual(webread.get(url, clock=clock, open_url=web, resolve=resolver).reason, "denied", url)
        self.assertEqual((web.calls, resolver.calls), ([], []))

    def test_a_redirect_into_a_denied_host_stops_before_the_request(self):
        clock = FakeClock()
        web = FakeWeb(clock)
        web.add("https://example.org/moved", status=302, headers={"Location": "https://fred.stlouisfed.org/series/X"})
        reply = webread.get("https://example.org/moved", clock=clock, open_url=web, resolve=FakeResolver())
        self.assertEqual(reply.reason, "denied")
        self.assertEqual(web.urls(), ["https://example.org/moved"])

    def test_only_whole_host_names_match(self):
        for url in ("https://notkalshi.example/x", "https://stlouisfed.org.example/x", "https://notunhcr.example/x"):
            self.assertFalse(webread.denied(url), url)
        self.assertTrue(webread.denied("https://example.org/x", ("federalreserve.gov",)))
        self.assertFalse(webread.denied("https://www.federalreserve.gov/x", ("federalreserve.gov",)))

    def test_look_alike_names_and_a_trailing_dot(self):
        # v1-r3 build: a name that only ends with a listed name is another host; a trailing dot is the same host.
        self.assertFalse(webread.host_in("notexample.org", ("example.org",)))
        self.assertTrue(webread.host_in("www.example.org.", ("example.org",)))
        self.assertTrue(webread.denied("https://notexample.org/x", ("example.org",)))
        self.assertEqual(sources.find_urls(question(resolution="https://notexample.org/x https://www.example.org/y"),
                                           ("example.org",)), ["https://www.example.org/y"])
        clock = FakeClock()
        web = FakeWeb(clock)
        resolver = FakeResolver()
        for url in ("https://fred.stlouisfed.org./series/X", "https://kalshi.com./x", "https://data.unhcr.org./x"):
            self.assertEqual(webread.get(url, clock=clock, open_url=web, resolve=resolver).reason, "denied", url)
        self.assertEqual((web.calls, resolver.calls), ([], []))


class PageReaderHostTests(unittest.TestCase):
    LINKS = ("https://www.federalreserve.gov/a.htm https://www.bls.gov/b.htm https://en.wikipedia.org/wiki/X "
             "https://example.org/y https://polymarket.com/event/x https://manifold.markets/u/x "
             "https://fred.stlouisfed.org/series/X https://data.unhcr.org/x https://kalshi.com/x "
             "https://www.metaculus.com/x/")

    def test_production_reads_cleared_hosts_only(self):
        self.assertEqual(sources.CLEARED_HOSTS, ("federalreserve.gov", "bls.gov"))
        for host in ("metaculus.com", "kalshi.com", "polymarket.com", "manifold.markets", "stlouisfed.org", "unhcr.org"):
            self.assertIn(host, sources.SKIP_HOSTS)
        q = question(resolution=self.LINKS)
        self.assertEqual(sources.find_urls(q), ["https://www.federalreserve.gov/a.htm", "https://www.bls.gov/b.htm"])

    def test_the_skip_list_wins_over_any_caller_list(self):
        everything = ("federalreserve.gov", "bls.gov", "wikipedia.org", "example.org", "polymarket.com",
                      "manifold.markets", "stlouisfed.org", "unhcr.org", "kalshi.com", "metaculus.com")
        self.assertEqual(sources.find_urls(question(resolution=self.LINKS), everything),
                         ["https://www.federalreserve.gov/a.htm", "https://www.bls.gov/b.htm",
                          "https://en.wikipedia.org/wiki/X", "https://example.org/y"])

    def test_uncleared_links_cost_no_request(self):
        clock = FakeClock()
        web = FakeWeb(clock)
        brief = sources.Reader({}, clock, open_url=web, resolve=FakeResolver()).read(
            question(resolution="See https://example.org/y and https://en.wikipedia.org/wiki/X."))
        self.assertEqual((brief.tried, brief.available, web.calls), (0, False, []))

    def test_a_redirect_off_the_cleared_hosts_is_refused(self):
        clock = FakeClock()
        web = FakeWeb(clock)
        web.add("https://www.federalreserve.gov/robots.txt", status=404, headers={"Content-Type": "text/plain"}, body="")
        web.add(FED, status=302, headers={"Location": "https://example.org/elsewhere"})
        brief = sources.Reader({}, clock, open_url=web, resolve=FakeResolver()).read(question(resolution=FED))
        self.assertEqual(brief.reasons, ("denied",))
        self.assertNotIn("https://example.org/elsewhere", web.urls())

    def test_a_robots_file_that_leaves_the_cleared_hosts_is_a_refusal(self):
        clock = FakeClock()
        web = FakeWeb(clock)
        web.add("https://www.federalreserve.gov/robots.txt", status=301, headers={"Location": "https://example.org/robots.txt"})
        web.add(FED, status=200, headers={"Content-Type": "text/html"}, body="<p>rate 3.75-4.00</p>")
        brief = sources.Reader({}, clock, open_url=web, resolve=FakeResolver()).read(question(resolution=FED))
        self.assertEqual(brief.reasons, ("robots",))
        self.assertNotIn(FED, web.urls())


class MarketSwitchTests(unittest.TestCase):
    def snapshot(self, env):
        clock = FakeClock()
        api = FakeJSON(clock, market_routes())
        tally = webread.Tally()
        with captured_logs() as logs:
            brief = markets.Markets(env, clock, get_json=api, tally=tally).snapshot(question(801, "numeric", TITLE))
        return brief, [url.split("/")[2] for url, _ in api.calls], logs.lines, tally

    def test_by_default_no_market_is_called(self):
        brief, hosts, lines, tally = self.snapshot({})
        self.assertEqual((hosts, brief.found, brief.platforms_ok, brief.platforms_tried), ([], 0, 0, 0))
        self.assertEqual(lines, ["MARKETS qid=801 found=0 platforms_ok=0/0"])
        self.assertEqual(tally.counts()["markets_tried"], 0)

    def test_each_platform_has_its_own_switch(self):
        cases = (({"POLYMARKET_ENABLED": "true"}, ["gamma-api.polymarket.com"]),
                 ({"MANIFOLD_ENABLED": "true"}, ["api.manifold.markets"]),
                 (MARKETS_ON, ["gamma-api.polymarket.com", "api.manifold.markets"]),
                 ({**MARKETS_ON, "MARKETS_ENABLED": "false"}, []),
                 ({"POLYMARKET_ENABLED": "maybe", "MANIFOLD_ENABLED": " "}, []))
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(self.snapshot(env)[1], expected)

    def test_no_kalshi_in_the_market_reader_or_the_glue(self):
        self.assertEqual(markets.PLATFORMS, ("Polymarket", "Manifold"))
        self.assertEqual(markets.SWITCHES, {"Polymarket": "POLYMARKET_ENABLED", "Manifold": "MANIFOLD_ENABLED"})
        for path in (ROOT / "fbot" / "markets.py", ROOT / "main.py"):
            self.assertNotIn("kalshi", path.read_text(encoding="utf-8").lower(), path.name)
        for name in ("KALSHI_URL", "KALSHI_SERIES", "kalshi_series", "kalshi_url", "parse_kalshi"):
            self.assertFalse(hasattr(markets, name), name)
        self.assertFalse((ROOT / "tests" / "fixtures" / "r2" / "markets_kalshi_u3.json").exists())

    def test_workflows_pass_the_two_switches_and_never_set_them(self):
        for name in ("run_bot_on_tournament.yaml", "run_bot_on_timer.yaml", "test_bot.yaml"):
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            for variable in ("POLYMARKET_ENABLED", "MANIFOLD_ENABLED"):
                self.assertEqual(text.count(variable + ":"), 1, name)
                self.assertIn(variable + ": ${{ vars." + variable + " }}", text, name)


class FixtureTests(unittest.TestCase):
    def test_third_party_fixtures_are_synthetic_or_credited(self):
        folder = ROOT / "tests" / "fixtures" / "r2"
        import json
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            label = data["label"]
            if label.startswith("REAL"):
                self.assertIn("attribution", data, path.name)
                self.assertTrue(data["attribution"].startswith("Source: "), path.name)
                self.assertIn("SextantBot", data.get("method_note", ""), path.name)
                self.assertTrue(any(host in data["source_url"] for host in sources.CLEARED_HOSTS), path.name)
            else:
                self.assertTrue(label.startswith(("SYNTHETIC", "UNVERIFIED SHAPE")), path.name)
            text = path.read_text(encoding="utf-8").lower()
            for word in ("kalshi", "unhcr", "stlouisfed", "zaraz"):
                self.assertNotIn(word, text.replace("https://fred.stlouisfed.org/series/unrate", ""), path.name)


if __name__ == "__main__":
    unittest.main()
