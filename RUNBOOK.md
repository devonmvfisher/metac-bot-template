# Sextant v1 runbook

Sextant must remain the first and only bot on the account. Keep the existing METACULUS_TOKEN; never create another bot to troubleshoot. Keep the existing live BOT_ENABLED setting while reviewing v1-test. On a new fork, leave it false until Test Bot and the release review pass. Never preview or tune on open season or MiniBench questions.

## Repository variables

Settings > Secrets and variables > Actions > Variables. Empty values use defaults. Unknown switches log one CONFIG line and use the stated default. Unknown MODEL_PRESET uses auto with PRESET_CONFIG_INVALID; an invalid reserve uses 10. Unknown OWN_KEY_MODE uses off with OWN_KEY_CONFIG_INVALID. Input values are never echoed.

| Variable | Default | Effect |
| --- | --- | --- |
| BOT_ENABLED | false | Enables tournament forecasting. Preserve the existing live setting while testing v1-test; a new fork stays false until review and Test Bot pass. |
| CHAIN_ENABLED | false | Queues the next tournament run after every active run. CLI failure gets one REST retry. |
| USE_OPENAI_BRIDGE | false | Allows the existing direct OpenAI bridge for season questions; MiniBench never uses it. |
| MINIBENCH_MODE | always | always selects C; slack reserves season coverage; off reserves the season budget. Before a budget skip, the Flash floor may still run M if sponsored credit covers it. skip (v1.1) never forecasts MiniBench at any credit (skip reason BUDGET_MINIBENCH) and leaves season pacing as off does; it beats MINIBENCH_PRESET and the fast path. With skip, the weekly coverage check counts MiniBench as missed and may open a SKIPS issue; that is expected. On v1-r3, skip is unknown and means always. Any other value (a typo such as skipp) runs as always and raises PRESET_CONFIG_INVALID, so check the next run after you change it. |
| MINIBENCH_PRESET | blank | Blank or auto keeps the MINIBENCH_MODE rule. A, B or C runs MiniBench at that tier while credit after pending reservations covers the 2-dollar floor plus PRESET_RESERVE_USD plus the tier cost (about 13.80 for A at default costs); below that the MINIBENCH_MODE rule applies. It overrides always, slack and off, never skip; the fast path still wins with M. Season and Test Bot questions ignore it. Invalid values use auto with PRESET_CONFIG_INVALID. Never set it without MINIBENCH_FLOOR_USD: the preset protects only its own reserve, not the season's money. |
| MINIBENCH_FLOOR_USD | blank | No floor by default. A number of US dollars: MiniBench is skipped (BUDGET_MINIBENCH) whenever credit after pending reservations is below it, or unknown. Season and Test Bot questions ignore it. A top-up also needs BUDGET_CAP_USD raised, or credit stays capped. Invalid values (negative, not a number) use no floor with PRESET_CONFIG_INVALID. 0 is not the same as blank: 0 skips MiniBench whenever credit is unknown. The only live sign that the floor is in force is a BUDGET_MINIBENCH skip in the vitals line once credit is below it. |
| MODEL_PRESET | auto | auto uses the pacer. A/B/C override season pacing only above the reserve. The fast path wins with M. MiniBench ignores the preset; see MINIBENCH_PRESET. |
| PRESET_RESERVE_USD | 10 | Extra dollars above the per-key 2-dollar floor and pending reservations, plus the selected tier cost. Invalid values use 10. |
| BUDGET_CAP_USD | blank | No cap by default. The total US dollars this key may ever spend; it is compared with the key's all-time usage. Caps the sponsored key. |
| BUDGET_CAP_OWN_USD | blank | No cap by default. The total US dollars this key may ever spend; it is compared with the key's all-time usage. Caps the own key. |
| OWN_KEY_MODE | off | off ignores the own secret. insurance allows own credit for season questions after sponsored exhaustion or sponsored spendable credit below C. primary paces combined eligible credit and spends sponsored first. MiniBench never uses own credit. Unknown values use off with a counts-only alert. |
| RESEARCH2_ENABLED | true | Season web brief, outside the urgent path, only above the sponsored research reserve. Set it to false (2 min) to disable. |
| MARKETS_ENABLED | true | Master switch for market evidence, which is separately labelled and never a mathematical blend. Each platform also needs its own switch below; with both unset no market is called. Set it to false (2 min) to disable. |
| POLYMARKET_ENABLED | false | Polymarket prices through its public API; its web pages are never read. Leave unset until Claude writes POLYMARKET CLEARED. |
| MANIFOLD_ENABLED | false | Manifold prices through its public API; its web pages are never read. Leave unset until Claude writes MANIFOLD CLEARED. |
| SOURCES_ENABLED | true | Reads bounded resolution-source pages on cleared hosts only (federalreserve.gov, bls.gov); never kalshi.com, stlouisfed.org or unhcr.org. Set it to false (2 min) to disable. |
| NUMERIC_V1 | true | Thirteen percentiles, validated CDFs and pointwise median. Internal failures fall back to the legacy numeric route. Set it to false (2 min) to disable. |
| PROMPTS_V1 | true | Per-run prompts, guarded evidence and misread checks. Set it to false (2 min) for the legacy prompt route. |
| DAILY_PROBE | true | Bounded model probes after polling and comment retries. Set it to false (2 min) to disable. |
| CONCURRENT_TARGETS | true | Season and MiniBench poll concurrently with shared process limits. Set it to false (2 min) for sequential polling. |
| ASKNEWS_PARITY | false | EDGE-2: article dates in UTC and source names. Leave unset until Claude writes EDGE-2 approved. |
| ASKNEWS_ARCHIVE | false | EDGE-2: season archive search requires parity and a loaded ledger; stops before monthly credits exceed 900. Recent queries continue. Leave unset until Claude writes EDGE-2 approved. |
| DEADLINE_SHIFT | false | EDGE-2: registered season yes/no titles get odds multiplied by 0.8 before caps. Leave unset until Claude writes EDGE-2 approved. |
| SEASON_ID | 33121 | Explicit season tournament ID; validate it with slug and end time. |
| SEASON_SLUG | fall-futureeval-2026 | Explicit season slug; never use a current-tournament alias. |
| SEASON_END_UTC | 2027-01-07T00:00:00Z | Stops forecasting, chaining and heartbeat at the season boundary. |
| ALLOW_NEW_SEASON | false | Allows a new, non-forbidden ID/slug/end tuple. A mismatch otherwise raises TARGET_MISMATCH and leaves MiniBench only. |
| SKIP_WITHOUT_RESEARCH | false | When true, skips only after both AskNews and the web brief are unavailable. Module exceptions still fail softly. |
| LOOP_MINUTES | 45 | Positive loop length up to 45 minutes; empty or invalid uses 45. |
| POLL_MINUTES | 10 | Positive poll interval up to 10 minutes; empty or invalid uses 10. |
| ALERT_MENTION | empty | Optional GitHub mention for issue notifications. |

## Secrets

METACULUS_TOKEN, OPENROUTER_API_KEY, optional OPENAI_API_KEY, optional OPENROUTER_API_KEY_OWN, and either ASKNEWS_API_KEY or ASKNEWS_CLIENT_ID plus ASKNEWS_SECRET. The own key is the only new secret in v1. Enter secrets only in GitHub's secret form. Never put them in a file, issue, log, comment or chat. Do not set REVIEW_BOT_ENABLED; the review and Cup workflows are disabled stubs.

## Alerts

For a code fault, if no helper is available, set BOT_ENABLED=false. For CREDITS_EXHAUSTED, assume no more credit is coming; write any credit request yourself. A permanent COMMENT_FAILED needs help, never a second forecast.

| Alert key | What to do |
| --- | --- |
| BUDGET_CAP_INVALID | Set BUDGET_CAP_USD (or BUDGET_CAP_OWN_USD for the own key) to the total dollars that key may spend, for example 80, or leave it blank. |
| API_REJECTED | Check Test Bot on the test branch; ask a helper to inspect the rejected payload path. A Metaculus 429 (rate limit) never raises it; see POST_RATE_LIMITED below. |
| COMMENT_FAILED | The forecast is posted but its private comment is missing. Ask a helper. Never re-run the forecast. After `POST_RATE_LIMITED qid=<id> kind=comment` lines in the run log, or in a run whose vitals line shows POST_RATE_LIMITED skips, it is a Metaculus throttle and not a rollback trigger: the forecast is posted, and the bot tries the comment again after each poll of that run. |
| CREDITS_EXHAUSTED | Assume no more credit is coming; submit another credit form yourself, in your own words; check the configured bridge or leave the bot stopped, or raise BUDGET_CAP_USD after a top-up. |
| CREDITS_LOW | Check the remaining credit and the MiniBench setting. |
| CREDIT_UNKNOWN | Check the key status in Test Bot; forecasting uses Tier C. |
| GATE_BLOCKED | The bot refused to send a forecast it could not verify. If it repeats and no helper is available, set BOT_ENABLED=false. Exception (a throttle): when the SEXTANT_VITALS line of the same run shows POST_RATE_LIMITED skips (`q.season.skip.POST_RATE_LIMITED` or `q.minibench.skip.POST_RATE_LIMITED` above 0), a GATE_BLOCKED from that run is a Metaculus throttle, not a code fault and not a rollback trigger. In the run log it is a `GATE_BLOCK qid=<id> kind=forecast` line with `rule=readback` (the read-back on the SDK's retry was refused, or the READBACK line just before it says `why=rate_limited`) or `rule=shape` (the post deadline passed while the SDK waited after a 429), for a qid with an earlier `POST_RATE_LIMITED qid=<id> kind=forecast` line in the same poll. A GATE_BLOCKED that comes with READBACK_UNKNOWN skips (the read-back before research could not tell; that code is the same as v1-r3) is no reason to roll back either; if it lasts for hours, ask a helper. Read it, then close the GATE_BLOCKED issue so the next one can open. A GATE_BLOCKED in a run with no POST_RATE_LIMITED skips follows the first rule. |
| HEARTBEAT_FAILED | Edit .github/heartbeat.txt in the browser and check Actions permissions. |
| INSTALL_FAILING | Run Test Bot and ask a helper to inspect the install step. |
| MODEL_UNAVAILABLE | Check the PROBE lines in Test Bot and request a reviewed model fix. |
| NO_FALL_QUESTIONS | Ask a helper to check the season target and coverage reader. |
| NO_LLM_KEY | Check the secret names and run Test Bot. |
| NUMERIC_FALLBACK | Set NUMERIC_V1=false (2 min) and ask a helper to inspect Test Bot's numeric path. |
| OWN_KEY_CONFIG_INVALID | Set OWN_KEY_MODE to off, insurance or primary; default off. |
| POLL_FAILING | If it repeats and no helper is available, set BOT_ENABLED=false. |
| PRESET_CONFIG_INVALID | Set MODEL_PRESET to auto, A, B or C and PRESET_RESERVE_USD to a nonnegative number (default 10). Invalid preset uses auto; invalid reserve uses 10. The same alert covers MINIBENCH_PRESET (auto, A, B or C; invalid uses auto) and MINIBENCH_FLOOR_USD (blank or a nonnegative number; invalid uses no floor) and MINIBENCH_MODE (always, slack, off or skip; invalid uses always); the run log's CONFIG line names the variable. If it appears right after you change a MINIBENCH_ variable, that variable is the bad one, even though the issue text names MODEL_PRESET. |
| RESEARCH_UNAVAILABLE | Check the AskNews secret names; see the resources link in RUNBOOK.md. If only web search fails, set RESEARCH2_ENABLED=false. |
| RUN_FAILED | If it repeats and no helper is available, set BOT_ENABLED=false. |
| SCHEDULER_GAPS | Check that a run started in the last 75 minutes. If none did, set CHAIN_ENABLED=true or check the outside timer's log, and start one run by hand now (Actions > Forecast on new AI tournament questions > Run workflow). |
| SEASON_OVER | Leave the fork, secrets and Test Bot working until any prize is paid. |
| SKIPS | Read the skip counts; ask a helper about recurring misses. |
| TARGET_MISMATCH | Check SEASON_ID, SEASON_SLUG and SEASON_END_UTC; MiniBench continues while the season is blocked. |
| USING_OWN_KEY | Season forecasts are using own credit. Check both remaining balances in the weekly status. |

Newly posted P0 alerts fail automatic schedule or timer runs; manual and chain runs rely on issue notifications. Most alerts have a 24-hour cooldown; USING_OWN_KEY has a seven-day cooldown, including closed issues. SEASON_OVER is posted only once and is checked by exact title across open and closed issues. Check counts and reason codes; never inspect live forecast values or reasoning to tune the bot.

## Scheduling and visibility

Check that a run started in the last 75 minutes. If none did, set CHAIN_ENABLED=true or check the outside timer's log, and start one run by hand now (Actions > Forecast on new AI tournament questions > Run workflow).

The chain uses the job token through gh, then one REST retry. An outside timer dispatches Forecast on timer; its token belongs only in that timer service and needs Actions (write) for this repository, expiring after the season. Both routes can run together: both posting workflows use literal group fbot-post-tournament and identical jobs. Test Bot has a separate group.

Each ops run reports CHAIN dispatched=0|1 via=gh|rest|none error=Class|- and SCHEDULE runs_24h schedule=n dispatch=n gap_max_min=m|unknown. PRESET reports the effective setting and season tiers used once per run. TARGET open=unknown includes a reason; the page cap alone is not a P0. Weekly coverage is read only when status is due and counts individual group members. Status includes forfeits, unknown statuses, missed IDs, group read-back skips, research counts, preset and both credit balances. Logs, run state and ledger contain counts, IDs, tiers and spend only.

The ledger caches question failure counts for a full 48 hours, per-tier spend, comment-failure IDs, forfeits and monthly AskNews credits. A provider outage (MODEL_TRANSIENT: no reply, 408, 429 or 5xx from every model) caps the question for the rest of that run only; it is marked once per run for 3 hours, and a question with 3 such runs waits until 3 hours after the last one. A wrong key or a retired model name (401, 403 or 404 from every model) counts as a question failure. A Metaculus 429 on a forecast or comment post follows the same outage rule: the bot logs `POST_RATE_LIMITED qid=<id> kind=forecast` (or `kind=comment`), never raises API_REJECTED and never sets the 48-hour memory. The SDK retries a forecast post up to 3 more times, each after a fresh read-back: about 2 to 3 minutes of waiting in all, plus the read-backs and posts, and the whole bot waits meanwhile. A short throttle leaves no trace. If no try in that poll lands (a 429 followed by 5xx answers or dropped connections counts the same), the question gets one POST_RATE_LIMITED skip (shown in the vitals skip counts and the SKIPS issue) and is tried again at the next poll, at most twice per run; a post deadline that passes while the SDK waits is the same single skip. Nothing needs clearing. Each new try runs the models again, so a throttle on posts that lasts for hours while reads still work costs model money with nothing posted: up to about 34 paid tries a day for a season question, and up to about 6 for a MiniBench question in its open window (tests/test_v11_review_post429.py pins both). If POST_RATE_LIMITED skips show in 2 runs in a row, set MINIBENCH_MODE=skip, and BOT_ENABLED=false if season questions show them too, until it clears; never roll back for it, because v1-r3 parks the question for 48 hours and raises API_REJECTED instead. MINIBENCH_MODE=skip stops MiniBench spend and posts, not reads: the bot still reads each open MiniBench question back once per poll. Do not touch the ledger for a throttle. The ledger lives in the Actions caches named fbot-ledger-*; deleting them clears that memory but also every other count above, so ask a helper first. Missing or corrupt ledger data falls back to config costs. AskNews allows two simultaneous calls with starts at least two seconds apart. A 429 waits 5/10/15 seconds or a larger retry_after, bounded by the research deadline. Recent searches charge one credit; archive searches charge five and require both EDGE-2 switches plus a loaded ledger. Unknown SDK article fields display unknown.

## New season

Use a reviewed known-season tuple for SEASON_ID, SEASON_SLUG and SEASON_END_UTC. Empty values keep current defaults. For a newly approved season absent from KNOWN_SEASONS, set all three values and ALLOW_NEW_SEASON=true. Never change just the known ID's slug or date to bypass validation. A mismatch raises TARGET_MISMATCH and schedules only MiniBench. At the configured end, forecasts, chain and heartbeat stop; the workflow exits early. Disable the tournament and outside timer afterwards and keep Test Bot available until any prize is paid.

## Test Bot and fixes

Upload the reviewed fork-files to v1-test first. Close a pull-request page without creating a pull request. Run Test Bot on v1-test once for each preset input auto, A, B, C (default C). Confirm green runs with binary, numeric, discrete and a group pair; PROBE and COST for every model; RESEARCH2, PAGES, MARKETS and COST research2; and POSTED with http=2xx, comment=posted, readback=found. While POLYMARKET_ENABLED and MANIFOLD_ENABLED are unset, MARKETS shows platforms_ok=0/0, and PAGES usually shows tried=0 because only federalreserve.gov and bls.gov pages are read; both are expected. A group member may instead show unverified_group after the retry, but at least one question must read found. A missing forecast or an unknown single-question read-back fails the test. Tournament unknown read-backs always block before model spending and again at the post gate.

Only after review GO upload the same tested files to main. Never click Sync fork. Do not change pinned dependencies by guesswork. Use v1-test for every crash fix. Date and conditional questions remain unsupported. No constant forecast fallback exists.

UNVERIFIED: live SDK CDF construction and posting, single-prediction aggregation, separate forecast/comment calls, numeric field names, AskNews metadata and auth, provider model availability/accounting, public market/page behavior, Actions cache major versions and token-triggered dispatch. Fake adapters test the local decisions, not these service contracts. Python cannot forcibly kill a stuck external thread; bounded HTTP deadlines and the workflow limit remain required.

The heartbeat makes a small commit after 25 days without a repository commit, before season end. Make the planned browser heartbeat edit on November 20; November 28 is the backup. The built-in token's effect on GitHub inactivity is unverified.

## Vitals line

Ops ends each run with one counts-only `SEXTANT_VITALS v1` notice and a plain `VITALS` line, plus a short job-summary table. They contain target, model, parser and AskNews counts, credit source and spend, and the next-dispatch result. Model HTTP attempts include retries; parser SDK calls are counted separately. Unknown model names are combined as `other`. Credit cap usage is all-time usage; request usage supplies token and cost counters. A vitals error prints `VITALS UNAVAILABLE` and keeps the original Ops exit code. Live accounting and annotation visibility, size limits and retention remain UNVERIFIED.

## Prize steps

The owner writes every message to Metaculus or AskNews staff himself. Complete the survey each season, reply to a winner email within 30 days, expect identity checks and likely a W-8BEN, and keep the secrets and Test Bot working until payment through Ramp. An inspection means showing the code and running Test Bot.
