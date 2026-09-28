# Changelog

## 2026-09-27 - v0.3-r3

R31 fixes the open-question counter and the coverage reader. The site's post list never marks a last page (its "next" link is never null), so before R31 both readers kept reading past the last page to the page cap and ended unknown; now each stops on an empty page or a page shorter than the limit it asked for, under the same cap. The coverage reader asks for our own forecasts (with_cp=true), so it can tell answered questions from missed ones; a question that comes back without them makes that target's coverage unknown, not all missed. Both readers use the site's include_descriptions name (include_description was ignored). Group sub-questions are counted; sub-questions still upcoming or open are skipped; a question repeated across pages counts once; closes before launch (Sep 28 00:00 UTC) are not counted. A failed read logs its reason as an HTTP status or error class (TARGET open=unknown:403, COVERAGE target=minibench unknown reason=403); POLL_FAILING still fires when both counts are unknown. After each run, Ops logs one COVERAGE line per target with closed, forecasted and missed question IDs, and the SKIPS and NO_FALL_QUESTIONS alerts carry those COVERAGE lines. Filter keys are sent repeated, as the SDK sends them; that is a request-shape change, not a fix. No other change.

## 2026-09-27 - v0.3-r2

Astra's v0.3-r1 (MODEL_PRESET strong start, R24-R26) merged by Claude with the live v0.2.2-v0.2.4 fixes: R27 read-back retry on rate limits, R28 research error codes in logs, R29 AskNews import (asknews_sdk), R30 AskNews 429 backoff. No other change.

## 2026-09-27 - v0.3 revision 1

Carry forward R24-R26 from v0.2.1 into v0.3 and every later version:

- R24: return a single prediction unchanged from FBot._aggregate_predictions, preserving object identity and the validated numeric/discrete CDF. This bypasses the single-prediction re-standardization reported in forecasting-tools 0.2.92. Other list lengths still await the base aggregator. New adapter lines retain the GLUE (NOT EXECUTED offline) marker.
- R25: transport() adds the default User-Agent `Sextant/0.2 (+https://github.com/devonmvfisher/metac-bot-template)` and preserves caller headers, including an explicit User-Agent override.
- R26: construct FBot with enable_summarize_research=False.

Add regression tests and mutations for all three fixes. The v0.3 preset controls are unchanged. Live SDK integration remains NOT EXECUTED; adapter checks use fake external imports.

## 2026-09-27 - v0.3

Add MODEL_PRESET (auto/A/B/C) and PRESET_RESERVE_USD (default 10) as repository variables. Season-only overrides respect pending reservations, FLOOR_CREDIT and the selected tier cost, then fall back to the unchanged auto pacer. MiniBench is unchanged. Add a sanitized configuration alert and one PRESET line per run, with offline tests and mutations. No other forecasting or workflow behavior changed.

## 2026-09-27 - v0.2

Applied R17-R23: one private comment per group sub-question with qid headers; lenient MiniBench mode and corrected slack reservation; total HTTP response deadline and HTTPException mapping; exact prompt header and guards; close-time priority with five forecasts and two research tasks; configurable run/poll lengths; literal tournament concurrency group. Comment header schema stays v0.1 as specified. No feature v1 work.

## 2026-09-27 - v0.1

Applied review R01-R16: API CDF validation and rounded values; direct prediction constructors; tolerant gate with exact registered payload; gate alerts, duplicate read-back and failure caps; main-loop SDK calls; run/poll/comment P0 alerts; comment retries; Test Bot read-back/probes; long comments; SDK field fallbacks; text cleaning; scoped MIT license and Sextant operating docs.

Deliberate pin: forecasting-tools stays at 0.2.92; pyproject.toml, poetry.lock and bot_helpers.py remain byte-identical to v0. The Fall ID remains explicit. The Gemini Pro forecaster is replaced by the Flash 3.8 then 3.6 chain; live availability is UNVERIFIED.

R17-R23 follow in v0.2. Feature v1 remains gated on its separate GO.

## 2026-09-27 - v0

Initial offline bot, fixtures, workflow replacements and operations controls.
