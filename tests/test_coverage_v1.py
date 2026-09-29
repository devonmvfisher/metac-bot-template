"""Acceptance tests for fbot.coverage: forfeits, group sub-questions, the bounded coverage reader
and the weekly-status rows (v1 item 3 and H5; review L02).

Written by Claude Opus 5.5 (workflow sextant-and-time-check, Sun Sep 27 2026 14:10 CT; reviewed 15:20 CT).
Contract: INTERFACE.md section 3. The Metaculus API now answers anonymous calls with 403, so every
page here is synthetic, shaped from Metaculus's public serializer code (see the fixture's _source).
Like the site, the fake API never sends a null "next", so the reader must stop on its own rules.
"""
from datetime import timedelta
import math
import random
import unittest
from urllib.parse import parse_qs, urlsplit
from fbot import coverage, ledger
from fbot.targets import utc
from .vclock import FakePostsAPI, iso, make_post, ops_data, sub_question

NOW = utc("2026-10-20T12:00:00Z")
ENV = {"METACULUS_TOKEN": "FAKEKEY123"}
SEASON = (("season", 33121),)


def recent_and_old(recent, old=250):
    posts = [make_post(10000 + i, [sub_question(20000 + i, NOW - timedelta(minutes=60 + 6 * i))])
             for i in range(recent)]
    posts += [make_post(50000 + i, [sub_question(60000 + i, NOW - timedelta(days=8, hours=i))])
              for i in range(old)]
    return posts


class CoverageTests(unittest.TestCase):
    def test_questions_of_shapes(self):
        closed = NOW - timedelta(hours=3)
        single = make_post(1, [sub_question(11, closed)])
        group = make_post(2, [sub_question(21, closed), sub_question(22, closed, kind="date"),
                              sub_question(23, closed)], group=True)
        self.assertEqual([q["id"] for q in coverage.questions_of(single)], [11])
        self.assertEqual([q["id"] for q in coverage.questions_of(group)], [21, 23])
        self.assertEqual(coverage.questions_of({"id": 3, "notebook": {"id": 3}}), [])
        self.assertEqual(coverage.questions_of({"id": 4, "conditional": {"question_yes": {}}}), [])
        self.assertEqual(coverage.questions_of({"id": 5, "question": "junk"}), [])
        self.assertEqual(coverage.questions_of(None), [])

    def test_group_subquestions_each_count_l02(self):
        posts = [make_post(1, [sub_question(11, NOW - timedelta(hours=5)),
                               sub_question(12, NOW - timedelta(hours=5), "none"),
                               sub_question(13, NOW - timedelta(hours=5))], group=True),
                 make_post(2, [sub_question(21, NOW - timedelta(hours=6), "none")]),
                 make_post(3, [sub_question(31, NOW - timedelta(hours=7), "absent")]),
                 make_post(4, [sub_question(41, NOW - timedelta(days=9))])]
        result = coverage.count_recent(posts, NOW)
        self.assertEqual((result["closed"], result["forecasted"], result["forfeited"], result["unknown"]),
                         (5, 2, 2, 1))
        self.assertEqual(result["forfeited_ids"], [12, 21])
        self.assertEqual(result["unknown_ids"], [31])
        self.assertEqual(result["missed"], result["forfeited_ids"])
        self.assertAlmostEqual(result["coverage"], 50.0)
        empty = coverage.count_recent([], NOW)
        self.assertEqual((empty["closed"], empty["coverage"]), (0, None))
        unknown_only = coverage.count_recent([make_post(5, [sub_question(51, NOW, "absent")])], NOW)
        self.assertIsNone(unknown_only["coverage"])

    def test_real_shaped_page(self):
        data = ops_data("posts_shape")
        result = coverage.count_recent(data["page"]["results"], utc(data["now"]))
        for key, value in data["expected"].items():
            self.assertEqual(result[key], value, key)

    def test_open_count_includes_group_subquestions_l02(self):
        soon = NOW + timedelta(hours=1)
        posts = [make_post(1, [sub_question(11, soon, status="open")]),
                 make_post(2, [sub_question(21, soon, status="open"), sub_question(22, soon, status="upcoming"),
                               sub_question(23, soon, status="open", kind="date")], group=True),
                 {"id": 3, "notebook": {}}]
        self.assertEqual(coverage.count_open(posts), 2)

    def test_reader_page_bound(self):
        for recent in (0, 1, 99, 100, 101, 250, 399):
            api = FakePostsAPI(recent_and_old(recent))
            data = coverage.read_recent(ENV, NOW, send=api, targets=SEASON)
            self.assertLessEqual(len(api.requests), math.ceil(recent / 100) + 1, recent)
            self.assertEqual(data["season"]["closed"], recent, recent)
            for method, url, headers in api.requests:
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(method, "GET")
                self.assertEqual(query["order_by"], ["-scheduled_close_time"])
                # Repeated parameters: the site's statuses filter is a list field with no comma splitter.
                self.assertEqual(query["statuses"], ["closed", "resolved"])
                self.assertEqual(query["include_descriptions"], ["false"])
                self.assertEqual(query["limit"], ["100"])
                self.assertEqual(query["tournaments"], ["33121"])
                self.assertEqual(headers["Authorization"], "Token FAKEKEY123")

    def test_reader_stops_on_an_empty_page_because_next_is_never_null(self):
        # Only recent posts, so the date rule never fires: the empty page ends the read.
        # The offset grows by the number of posts actually received.
        for recent, offsets in ((0, [0]), (99, [0, 99]), (100, [0, 100]), (230, [0, 100, 200, 230])):
            api = FakePostsAPI(recent_and_old(recent, old=0))
            data = coverage.read_recent(ENV, NOW, send=api, targets=SEASON)
            self.assertEqual(data["season"]["closed"], recent, recent)
            self.assertLessEqual(len(api.requests), math.ceil(recent / 100) + 1, recent)
            sent = [int(parse_qs(urlsplit(url).query)["offset"][0]) for _, url, _ in api.requests]
            self.assertEqual(sent, offsets, recent)

    def test_reader_logs_counts_only(self):
        api = FakePostsAPI(recent_and_old(3))
        with self.assertLogs("fbot", level="INFO") as logs:
            coverage.read_recent(ENV, NOW, send=api, targets=SEASON)
        text = "\n".join(logs.output)
        self.assertIn("COVERAGE target=season pages=1 closed=3 forecasted=3 forfeited=0 unknown=0", text)
        self.assertNotIn("FAKEKEY123", text)

    def test_r31_reader_asks_for_own_forecasts(self):
        # The fake follows the site: without with_cp=true no question carries my_forecasts.
        posts = recent_and_old(3) + [make_post(701, [sub_question(711, NOW - timedelta(hours=2), "none")])]
        api = FakePostsAPI(posts)
        bare = api("GET", f"https://www.metaculus.com/api/posts/?tournaments={SEASON[0][1]}&limit=100&offset=0", {}, None, 30)[1]
        self.assertTrue(bare["results"])
        self.assertFalse(any("my_forecasts" in q for post in bare["results"] for q in coverage.questions_of(post)))
        api.requests.clear()
        data = coverage.read_recent(ENV, NOW, send=api, targets=SEASON)
        self.assertTrue(api.requests)
        for _, url, _ in api.requests:
            self.assertEqual(parse_qs(urlsplit(url).query)["with_cp"], ["true"])
        season = data["season"]
        self.assertEqual((season["closed"], season["forecasted"], season["forfeited"], season["unknown"]), (4, 3, 1, 0))
        self.assertEqual(season["forfeited_ids"], [711])
        self.assertAlmostEqual(season["coverage"], 75.0)

    def test_r31_skips_open_subquestions_and_closes_before_season_start(self):
        start = utc(coverage.SEASON_START)
        self.assertEqual(start, utc("2026-09-28T00:00:00Z"))
        now = start + timedelta(days=3)
        posts = [make_post(701, [sub_question(711, now - timedelta(hours=2)),
                                 sub_question(712, now - timedelta(hours=2), "none", status="open"),
                                 sub_question(713, now - timedelta(hours=3), "none", status="upcoming")], group=True),
                 make_post(702, [sub_question(721, start - timedelta(hours=1), "none")]),
                 make_post(703, [sub_question(731, start + timedelta(minutes=1), "none")]),
                 make_post(704, [sub_question(741, start)])]
        result = coverage.count_recent(posts, now)
        self.assertEqual((result["closed"], result["forecasted"], result["forfeited"], result["unknown"]), (3, 2, 1, 0))
        self.assertEqual(result["forfeited_ids"], [731])
        # A question listed open on one page and closed on a later page still counts once.
        repeat = [make_post(705, [sub_question(751, now - timedelta(hours=1), "none", status="open")]),
                  make_post(705, [sub_question(751, now - timedelta(hours=1), "none")]),
                  make_post(705, [sub_question(751, now - timedelta(hours=1), "none")])]
        self.assertEqual(coverage.count_recent(repeat, now)["forfeited_ids"], [751])

    def test_ignored_order_param_gives_unknown(self):
        posts = recent_and_old(50)
        random.Random(7).shuffle(posts)
        api = FakePostsAPI(posts, honour_order=False)
        self.assertEqual(coverage.read_recent(ENV, NOW, send=api, targets=SEASON), {"season": None})

    def test_errors_give_unknown_per_target(self):
        both = {"season": None, "minibench": None}
        self.assertEqual(coverage.read_recent(ENV, NOW, send=FakePostsAPI([], status=500)), both)

        def boom(*args):
            raise OSError("FAKEKEY123 provider text")

        self.assertEqual(coverage.read_recent(ENV, NOW, send=boom), both)
        self.assertEqual(coverage.read_recent(ENV, NOW, send=lambda *a: (200, {"results": "nope"})), both)
        self.assertEqual(coverage.read_recent(ENV, NOW, send=lambda *a: (200, ["not", "a", "dict"])), both)

    def test_page_cap_gives_unknown(self):
        endless = FakePostsAPI([make_post(i, [sub_question(i, NOW - timedelta(minutes=1))]) for i in range(1, 250)])
        self.assertEqual(coverage.read_recent(ENV, NOW, send=endless, targets=SEASON, max_pages=2),
                         {"season": None})

    def test_not_due_makes_no_request(self):
        api = FakePostsAPI(recent_and_old(5))
        self.assertIsNone(coverage.read_recent(ENV, NOW, send=api, due=False))
        self.assertEqual(api.requests, [])

    def test_weekly_due(self):
        latest = lambda issue: utc(issue["created_at"])
        self.assertTrue(coverage.weekly_due([], latest, NOW))
        issues = [{"title": "Bot weekly status", "created_at": iso(NOW - timedelta(days=6))},
                  {"title": "[BOT ALERT] SKIPS", "created_at": iso(NOW - timedelta(days=30))}]
        self.assertFalse(coverage.weekly_due(issues, latest, NOW))
        issues[0]["created_at"] = iso(NOW - timedelta(days=6, hours=13))
        self.assertTrue(coverage.weekly_due(issues, latest, NOW))

        def broken(issue):
            raise ValueError("unreadable")

        self.assertTrue(coverage.weekly_due(issues, broken, NOW))

    def test_forfeit_split_uses_ledger(self):
        book = ledger.empty()
        book.record_question(12, "season", NOW, reason="TOO_LATE")
        self.assertEqual(coverage.forfeit_split([12, 21], book), {"seen": 1, "never_seen": 1})
        self.assertIsNone(coverage.forfeit_split([12], None))

    def test_weekly_rows_counts_only_h5(self):
        cov = {"season": {"closed": 20, "forecasted": 18, "forfeited": 2, "unknown": 0, "coverage": 90.0,
                          "forfeited_ids": [5, 6], "unknown_ids": [], "missed": [5, 6], "probability": 0.376543},
               "minibench": None}
        book = ledger.empty()
        for _ in range(20):
            book.record_spend("C", 0.29, NOW)
        book.record_question(5, "season", NOW, reason="TOO_LATE")
        rows = coverage.weekly_rows(cov, ledger=book, now=NOW, credit={"metaculus": 41.5, "own": "off"},
                                    comment_failed=1, preset="B",
                                    research={"asknews": (18, 20), "web": (0, 0), "markets": (5, 20),
                                              "EVIL": (1, 1)})
        self.assertEqual(rows, [
            "forfeits season=2 minibench=unknown",
            "forfeit_split season seen=1 never_seen=1",
            "spend_7d tier=C questions=20 usd=5.80 per_q=0.29",
            "limit_remaining metaculus=41.50 own=off",
            "research asknews=90% web=n/a pages=n/a markets_found=25%",
            "comment_failed=1",
            "preset=B",
        ])
        text = "\n".join(coverage.weekly_rows({}, preset="Z", credit={"metaculus": float("nan")}))
        for expected in ("forfeits season=unknown minibench=unknown", "spend_7d unknown",
                         "limit_remaining metaculus=unknown own=unknown", "research unknown",
                         "comment_failed=0", "preset=unknown"):
            self.assertIn(expected, text)
        idle = coverage.weekly_rows({}, ledger=ledger.empty(), now=NOW)
        self.assertIn("spend_7d none", idle)

    def test_research_counts_read_the_research_tally_names(self):
        counts = {"asknews_tried": 20, "asknews_ok": 18, "web_tried": 0, "web_ok": 0, "markets_tried": 20,
                  "markets_ok": 5, "pages_tried": True, "pages_ok": -1, "junk_ok": 3}
        self.assertEqual(coverage.research_counts(counts),
                         {"asknews": (18, 20), "web": (0, 0), "pages": (0, 0), "markets": (5, 20)})
        self.assertIsNone(coverage.research_counts({"season": 3}))
        self.assertIsNone(coverage.research_counts("junk"))
        rows = coverage.weekly_rows({}, research=coverage.research_counts(counts))
        self.assertIn("research asknews=90% web=n/a pages=n/a markets_found=25%", rows)


if __name__ == "__main__":
    unittest.main()
