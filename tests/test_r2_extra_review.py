"""Review regressions: large tables parse fast, a slow page or market call never holds up another question,
and the reader uses read1 so the deadline is checked after each network read."""
import threading
import time
import unittest
from fbot import markets, sources, webread
from fbot.types import Clock
from .fakes import FakeClock
from .r2_fakes import FakeResolver, Headers, question
from .r3_fakes import MARKETS_ON, TEST_HOSTS


class _Response:
    def __init__(self, body, ctype="text/html"):
        self.status = 200
        self.headers = Headers({"Content-Type": ctype})
        self.body = body

    def read(self, size=-1):
        data, self.body = self.body, b""
        return data

    def close(self):
        pass


class ReviewRegressionTests(unittest.TestCase):
    def test_large_table_parses_in_linear_time(self):
        rows = "".join("<tr><td>2026-09-%02d</td><td>%d</td><td>3.75-4.00</td></tr>\n" % (i % 28 + 1, i)
                       for i in range(8000))
        body = ("<html><body><table>" + rows + "</table></body></html>").encode("ascii")
        started = time.perf_counter()
        text = webread.html_text(body)
        self.assertLess(time.perf_counter() - started, 3)
        self.assertEqual(text.splitlines()[1], "2026-09-02 | 1 | 3.75-4.00")

    def test_slow_page_does_not_hold_up_another_question(self):
        entered, release = threading.Event(), threading.Event()

        def open_url(url, headers, timeout):
            if url.endswith("/robots.txt"):
                return _Response(b"User-agent: *\nAllow: /\n", "text/plain")
            if "slow.example" in url:
                entered.set()
                release.wait(5)
            return _Response(b"<p>September 2026 unemployment rate 4.3</p>")

        reader = sources.Reader({}, Clock(), open_url=open_url, resolve=FakeResolver(), hosts=TEST_HOSTS)
        first = threading.Thread(target=reader.read, args=(question(1, resolution="https://slow.example/a"),))
        first.start()
        self.assertTrue(entered.wait(5))
        started = time.monotonic()
        other = reader.read(question(2, resolution="https://fast.example/b"))
        waited = time.monotonic() - started
        release.set()
        first.join(10)
        self.assertEqual(other.reasons, ("ok",))
        self.assertLess(waited, 2)

    def test_slow_market_call_does_not_hold_up_another_question(self):
        entered, release = threading.Event(), threading.Event()

        def get_json(url, timeout):
            if "manifold" in url and "slow" in url:
                entered.set()
                release.wait(5)
            return 200, ([] if "manifold" in url else {"events": [], "markets": []})

        mk = markets.Markets(MARKETS_ON, Clock(), get_json=get_json)
        first = threading.Thread(target=mk.snapshot, args=(question(1, title="slow unemployment rate question"),))
        first.start()
        self.assertTrue(entered.wait(5))
        started = time.monotonic()
        other = mk.snapshot(question(2, title="US unemployment rate September 2026"))
        waited = time.monotonic() - started
        release.set()
        first.join(10)
        self.assertEqual(other.platforms_ok, other.platforms_tried)
        self.assertLess(waited, 2)

    def test_reader_uses_read1_so_the_deadline_is_checked_per_network_read(self):
        clock = FakeClock()

        class Trickle(_Response):
            def read(self, size=-1):          # a blocking read waits for the whole chunk: 60 s here
                clock.sleep(60)
                return super().read(size)

            def read1(self, size=-1):         # read1 returns what has arrived: 1 s here
                clock.sleep(1)
                return super().read(size)

        reply = webread.get("https://example.org/", clock=clock, deadline=clock.monotonic() + 15,
                            open_url=lambda url, headers, timeout: Trickle(b"<p>value 4.3</p>"),
                            resolve=FakeResolver())
        self.assertEqual(reply.reason, "ok")


if __name__ == "__main__":
    unittest.main()
