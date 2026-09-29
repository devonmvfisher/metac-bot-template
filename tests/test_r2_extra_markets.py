"""Extra checks for invalid prices. (The duplicate-subtitle check left with the Kalshi parser in v1-r3.)"""
import unittest
from fbot import markets
from . import r2_fakes


class ExtraMarketTests(unittest.TestCase):
    def test_prices_outside_the_unit_interval_are_not_evidence_prices(self):
        for price in ("1.01", "-0.01", "nan", "inf", True, None):
            with self.subTest(price=price):
                poly = {"events": [{"slug": "sample", "markets": [
                    {"question": "Synthetic threshold", "outcomes": ["Yes", "No"],
                     "outcomePrices": [price, "0"], "active": True}]}]}
                manifold = [{"question": "Synthetic threshold", "outcomeType": "BINARY",
                             "probability": price, "url": "https://manifold.markets/sample"}]
                self.assertIsNone(markets.parse_polymarket(poly)[0].yes)
                self.assertIsNone(markets.parse_manifold(manifold)[0].yes)
