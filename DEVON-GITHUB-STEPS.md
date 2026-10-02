# Sextant v1 GitHub steps

1. Wait for the v1 packet review. Keep the current live files until that review says to test this packet.
2. Upload every file in fork-files, including .github, to branch v1-test. Do not create another bot. Keep the existing token. Close any pull-request page without making a pull request; never click Sync fork.
3. Settings > Secrets and variables > Actions: keep the existing secret names. OPENROUTER_API_KEY_OWN is the only new optional secret. Put its value only in the secret form, never in a chat or file.
4. Under Variables, review these defaults. Keep the existing live BOT_ENABLED setting while testing v1-test. A new fork defaults to false until review and Test Bot pass. The Test Bot preset input is separate from MODEL_PRESET and defaults to C.

- BOT_ENABLED = false. Enables tournament forecasting. Preserve the existing live setting while testing v1-test; a new fork stays false until review and Test Bot pass.
- CHAIN_ENABLED = false. Queues the next tournament run after every active run. CLI failure gets one REST retry.
- USE_OPENAI_BRIDGE = false. Allows the existing direct OpenAI bridge for season questions; MiniBench never uses it.
- MINIBENCH_MODE = always. always selects C; slack reserves season coverage; off reserves the season budget. Before a budget skip, the Flash floor may still run M if sponsored credit covers it. skip (v1.1) never forecasts MiniBench.
- MINIBENCH_PRESET = blank. Blank keeps the MINIBENCH_MODE rule; A, B or C runs MiniBench at that tier above the same reserve as MODEL_PRESET. Set it only to the value Claude gives you.
- MINIBENCH_FLOOR_USD = blank. Blank means no floor; a number skips MiniBench whenever credit is below it. Set it only to the value Claude gives you.
- MODEL_PRESET = auto. auto uses the pacer. A/B/C override season pacing only above the reserve. The fast path wins with M. MiniBench ignores the preset.
- PRESET_RESERVE_USD = 10. Extra dollars above the per-key 2-dollar floor and pending reservations, plus the selected tier cost. Invalid values use 10.
- BUDGET_CAP_USD = blank. The sponsored key's total lifetime US-dollar allowance; leave blank until Claude gives you the number; a variable never holds a key.
- BUDGET_CAP_OWN_USD = blank. The own key's total lifetime US-dollar allowance; leave blank until Claude gives you the number; a variable never holds a key.
- OWN_KEY_MODE = off. off ignores the own secret. insurance allows own credit for season questions after sponsored exhaustion or sponsored spendable credit below C. primary paces combined eligible credit and spends sponsored first. MiniBench never uses own credit. Unknown values use off with a counts-only alert.
- RESEARCH2_ENABLED = true. Season web brief, outside the urgent path, only above the sponsored research reserve. Set it to false (2 min) to disable.
- MARKETS_ENABLED = true. Master switch for market evidence, which is separately labelled and never a mathematical blend. Each platform also needs its own switch below; with both unset no market is called. Set it to false (2 min) to disable.
- POLYMARKET_ENABLED = false. Polymarket prices through its public API; its web pages are never read. Leave unset until Claude writes POLYMARKET CLEARED.
- MANIFOLD_ENABLED = false. Manifold prices through its public API; its web pages are never read. Leave unset until Claude writes MANIFOLD CLEARED.
- SOURCES_ENABLED = true. Reads bounded resolution-source pages on cleared hosts only (federalreserve.gov, bls.gov); never kalshi.com, stlouisfed.org or unhcr.org. Set it to false (2 min) to disable.
- NUMERIC_V1 = true. Thirteen percentiles, validated CDFs and pointwise median. Internal failures fall back to the legacy numeric route. Set it to false (2 min) to disable.
- PROMPTS_V1 = true. Per-run prompts, guarded evidence and misread checks. Set it to false (2 min) for the legacy prompt route.
- DAILY_PROBE = true. Bounded model probes after polling and comment retries. Set it to false (2 min) to disable.
- CONCURRENT_TARGETS = true. Season and MiniBench poll concurrently with shared process limits. Set it to false (2 min) for sequential polling.
- ASKNEWS_PARITY = false. EDGE-2: article dates in UTC and source names. Leave unset until Claude writes EDGE-2 approved.
- ASKNEWS_ARCHIVE = false. EDGE-2: season archive search requires parity and a loaded ledger; stops before monthly credits exceed 900. Recent queries continue. Leave unset until Claude writes EDGE-2 approved.
- DEADLINE_SHIFT = false. EDGE-2: registered season yes/no titles get odds multiplied by 0.8 before caps. Leave unset until Claude writes EDGE-2 approved.
- SEASON_ID = 33121. Explicit season tournament ID; validate it with slug and end time.
- SEASON_SLUG = fall-futureeval-2026. Explicit season slug; never use a current-tournament alias.
- SEASON_END_UTC = 2027-01-07T00:00:00Z. Stops forecasting, chaining and heartbeat at the season boundary.
- ALLOW_NEW_SEASON = false. Allows a new, non-forbidden ID/slug/end tuple. A mismatch otherwise raises TARGET_MISMATCH and leaves MiniBench only.
- SKIP_WITHOUT_RESEARCH = false. When true, skips only after both AskNews and the web brief are unavailable. Module exceptions still fail softly.
- LOOP_MINUTES = 45. Positive loop length up to 45 minutes; empty or invalid uses 45.
- POLL_MINUTES = 10. Positive poll interval up to 10 minutes; empty or invalid uses 10.
- ALERT_MENTION = empty. Optional GitHub mention for issue notifications.

5. Actions > Test Bot > Run workflow: select v1-test. Run four times, with input preset auto, A, B, C. Confirm each is green and shows PROBE, COST, READBACK and POSTED for the supported questions. Require a numeric, a discrete and a group question. A group read-back may show unverified_group with a warning after retry; a non-group unknown or any missing post/comment fails. At least one read-back must be found. Require RESEARCH2, PAGES, MARKETS and COST research2 lines. While POLYMARKET_ENABLED and MANIFOLD_ENABLED are unset, MARKETS shows platforms_ok=0/0, and PAGES usually shows tried=0 because only federalreserve.gov and bls.gov pages are read; both are expected.
6. After Claude says GO, upload those same files to main. Choose the ongoing MODEL_PRESET and OWN_KEY_MODE variables, then set BOT_ENABLED=true. The first two runs must show both TARGET counts and working CHAIN or timer dispatch. Do not preview live question values.
7. Choose a scheduling route in GitHub settings: set CHAIN_ENABLED=true for the built-in chain, or configure an outside service to dispatch Forecast on timer. Its fine-grained token needs this one repository's Actions (write) permission only, expires after the season and stays only in that timer service. Never store it as a repository secret or in chat. Both routes may be enabled together.
8. If no run starts for 75 minutes, start Forecast on new AI tournament questions by hand and follow SCHEDULER_GAPS in RUNBOOK. If a code fault repeats without a helper, set BOT_ENABLED=false.
9. Before a new season, follow RUNBOOK > New season. At the season end, disable tournament scheduling and the outside timer. Keep the fork and Test Bot for any prize inspection or payment.

Leave ASKNEWS_PARITY, ASKNEWS_ARCHIVE and DEADLINE_SHIFT unset until Claude writes EDGE-2 approved. Leave POLYMARKET_ENABLED and MANIFOLD_ENABLED unset until Claude writes POLYMARKET CLEARED or MANIFOLD CLEARED. Live checks are the operator/reviewer handoff; this packet was built and checked without network access.
