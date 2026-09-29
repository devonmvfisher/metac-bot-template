"""Extra checks for transient source failures."""
import unittest
from fbot import sources
from .fakes import FakeClock
from .r2_fakes import FakeResolver, FakeWeb, question
from .r3_fakes import TEST_HOSTS


class ExtraSourceTests(unittest.TestCase):
    def test_timeout_does_not_poison_the_url_cache(self):
        clock = FakeClock()
        web = FakeWeb(clock)
        url = "https://example.org/transient"
        web.add(url, **{"raise": TimeoutError("temporary")})
        reader = sources.Reader({}, clock, open_url=web, resolve=FakeResolver(), hosts=TEST_HOSTS)
        self.assertEqual(reader.read(question(1001, resolution=url)).reasons, ("timeout",))
        web.add(url, status=200, headers={"Content-Type": "text/plain"}, body="Published value 7")
        result = reader.read(question(1002, resolution=url))
        self.assertEqual(result.reasons, ("ok",))
        self.assertIn("Published value 7", result.text)
        self.assertEqual(web.urls().count(url), 2)
