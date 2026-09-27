# Changelog

## 2026-09-27 - v0.2.2

From the first live Test Bot runs (Claude, on the HP): R27 retries a read-back that hits a rate limit or loses its reply (2 s, then 4 s) before it counts as unknown, so a burst no longer skips a question; R28 adds a provider-free reason (exception class and HTTP status only) to each RESEARCH log line, to diagnose AskNews returning no articles. Tests added for both. No other change.

## 2026-09-27 - v0.2.1

Go-live fixes from Claude's v0.2 review (REVIEW-A5-v0.2.md), applied by Claude on the HP to Astra's v0.2 so Sextant can go live before the season opens: R24 posts the registered prediction as-is when there is one prediction (the SDK re-aggregation re-standardized numeric and discrete CDFs and the post gate blocked every one); R25 sends a Sextant User-Agent on fbot's own HTTP reads; R26 turns off the unused SDK research summarizer. Tests: 129 unit tests, 24/24 mutations. No other change. Astra folds R24-R26 into v0.3.

## 2026-09-27 - v0.2

Applied R17-R23: one private comment per group sub-question with qid headers; lenient MiniBench mode and corrected slack reservation; total HTTP response deadline and HTTPException mapping; exact prompt header and guards; close-time priority with five forecasts and two research tasks; configurable run/poll lengths; literal tournament concurrency group. Comment header schema stays v0.1 as specified. No feature v1 work.

## 2026-09-27 - v0.1

Applied review R01-R16: API CDF validation and rounded values; direct prediction constructors; tolerant gate with exact registered payload; gate alerts, duplicate read-back and failure caps; main-loop SDK calls; run/poll/comment P0 alerts; comment retries; Test Bot read-back/probes; long comments; SDK field fallbacks; text cleaning; scoped MIT license and Sextant operating docs.

Deliberate pin: forecasting-tools stays at 0.2.92; pyproject.toml, poetry.lock and bot_helpers.py remain byte-identical to v0. The Fall ID remains explicit. The Gemini Pro forecaster is replaced by the Flash 3.8 then 3.6 chain; live availability is UNVERIFIED.

R17-R23 follow in v0.2. Feature v1 remains gated on its separate GO.

## 2026-09-27 - v0

Initial offline bot, fixtures, workflow replacements and operations controls.
