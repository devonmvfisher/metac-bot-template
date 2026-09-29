"""v1-r3 test helpers: reserved example hosts for the page reader, and both market platforms switched on.

Written by Claude Opus 5.5 (v1-r3 build, Mon Sep 28 2026).
Production reads only sources.CLEARED_HOSTS and leaves both platforms off; tests that exercise the reader or
the market parsers opt in explicitly with these two values.
"""
from fbot import sources

# RFC 2606 names only, so no test can ever name a real third party.
TEST_HOSTS = sources.CLEARED_HOSTS + ("example.org", "example.com", "example")
MARKETS_ON = {"POLYMARKET_ENABLED": "true", "MANIFOLD_ENABLED": "true"}
