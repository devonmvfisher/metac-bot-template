# Changelog

## 2026-10-01 - v1.1

config.VERSION stays "v1", so the dry run stays byte-identical; runs are told apart by commit sha.

Workflows: every job runs on ubuntu-24.04 instead of ubuntu-latest, which GitHub moves to Ubuntu 26 from 2026-10-19. Today's ubuntu-latest is already 24.04, so nothing changes until then. The two posting workflows also pass MINIBENCH_PRESET and MINIBENCH_FLOOR_USD (blank unless set).

## 2026-09-28 - v1-r3

Terms rebuild. Every request the bot's own code makes (market APIs, robots.txt, resolution pages, model calls, Metaculus reads and posts, GitHub) now sends the User-Agent SextantBot/1.0 with the repository URL as the contact; it replaces the R25 Sextant/0.2 default. Requests made inside the forecasting-tools and AskNews SDKs keep their library User-Agent. No module may fetch kalshi.com, stlouisfed.org or unhcr.org: the shared reader refuses them on every redirect hop, before any address lookup. The resolution-page reader reads cleared hosts only (federalreserve.gov, bls.gov), always skips metaculus.com, kalshi.com, polymarket.com, manifold.markets, stlouisfed.org and unhcr.org, and refuses a redirect or robots.txt that leaves a cleared host. Kalshi is removed from the market reader. Polymarket and Manifold are each off unless their own repository variable (POLYMARKET_ENABLED, MANIFOLD_ENABLED) is true; MARKETS_ENABLED stays the master switch, and the three bot workflows pass both variables. Third-party test fixtures are now synthetic, except the BLS and Federal Reserve Board pages, which carry a source credit (one third-party script in the Board page is replaced with a synthetic one). F09: when every model fails with no reply, 408, 429 or a 5xx, the skip reason is MODEL_TRANSIENT; the question is still capped for the rest of that run, but it stays out of the 2-day failure memory, and after 3 such runs within 3 hours it sits out until 3 hours after the last one. Every other failure keeps the 2-day memory. main.py is unchanged.

## 2026-09-28 - v1-r2

The credit read keeps the last known balance while it waits for the key endpoint, so a model call made during the read still sees that balance and does not pick a nearly empty key; a failed read keeps the last known balance and marks its source unknown. The coverage reader carries forward the live v0.3-r3 R31 fix: it asks for our own forecasts (with_cp=true), so closed questions count as forecasted or forfeited instead of unknown; sub-questions still upcoming or open are skipped; closes before the season start (2026-09-28 00:00 UTC) are not counted. The v1-r1 note moves to its own heading. No other change.

## 2026-09-28 - v1-r1

2026-09-28 v1-r1: the deadline-rule and two numeric-shape test fixtures are now fully synthetic; new BUDGET_CAP_USD and BUDGET_CAP_OWN_USD settings let the credit pacer work when a key has no credit limit (alert BUDGET_CAP_INVALID); Ops prints one counts-only SEXTANT_VITALS v1 notice per run. EDGE-2 switches stay off.

## 2026-09-28 - v1

Built from live v0.3-r2. L1-L5 repair dispatch, counting/read-back, AskNews spacing and repeated paid post failures. M1-M3 integrate the reviewed operations, numeric and research modules and their fixes. Items 1-2 add own-key modes, target isolation, a single tier table, repeated model runs, weighted aggregation, deadline combination, Flash floor and measured costs. H1-H9 add durable counts, validated season settings, Test Bot presets/costs, switch bindings, weekly reporting, rationale caps, season-end handling and release documentation. E1 ASKNEWS_PARITY/ASKNEWS_ARCHIVE and E2 DEADLINE_SHIFT are built with defaults off.

R24-R26 remain from v0.2.1: single SDK prediction identity, the exact Sextant/0.2 User-Agent and disabled SDK research summarization. R27-R30 and the strict tournament double-post guards remain. SDK research_reports_per_question and predictions_per_research_report stay 1. Pins remain byte-identical with forecasting-tools 0.2.92. Live execution remains NOT EXECUTED offline.

## 2026-09-27 - v0.3-r2

The v0.3-r1 release (MODEL_PRESET strong start, R24-R26) merged with the live v0.2.2-v0.2.4 fixes: R27 read-back retry on rate limits, R28 research error codes in logs, R29 AskNews import (asknews_sdk), R30 AskNews 429 backoff. No other change.

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
