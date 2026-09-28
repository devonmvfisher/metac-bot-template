# Bot runbook

Sextant is the first and only bot on the account. The bot whose token is in METACULUS_TOKEN must stay the first and only bot on the account; never create another bot.

The bot starts off. Leave BOT_ENABLED unset or false until Test Bot passes and the review packet is approved. CHAIN_ENABLED is also off by default. Do not preview, tune, or manually re-run forecasts on open season or MiniBench questions. Use the bot-testing-area workflow for live checks; use synthetic fixtures for dry runs.

## Controls

Repository Settings > Secrets and variables > Actions > Variables holds BOT_ENABLED, CHAIN_ENABLED, USE_OPENAI_BRIDGE, MINIBENCH_MODE, MODEL_PRESET, PRESET_RESERVE_USD, SKIP_WITHOUT_RESEARCH and ALERT_MENTION. Do not set REVIEW_BOT_ENABLED; its workflow is a disabled stub.

| Variable | Default | Effect |
| --- | --- | --- |
| BOT_ENABLED | false | Stops forecasting and the chain when false or unset. |
| CHAIN_ENABLED | false | When true, a completed run queues the next run; requires BOT_ENABLED too. |
| USE_OPENAI_BRIDGE | false | Permits an OpenAI key to cover season forecasts when OpenRouter is absent or exhausted. |
| MINIBENCH_MODE | always | always attempts Tier C; slack reserves season credit first; off stops MiniBench. |
| MODEL_PRESET | auto | auto keeps the existing pacer. A, B or C selects that season tier while the reserve check passes, including on urgent season questions. MiniBench, Test Bot and the optional bridge keep their existing rules. Blank is auto; surrounding spaces and letter case are normalized. An unknown value uses auto and raises PRESET_CONFIG_INVALID. |
| PRESET_RESERVE_USD | 10 | Extra credit reserve for a season preset. The check is remaining credit minus pending reservations minus FLOOR_CREDIT >= this reserve plus the preset's tier cost. Below the threshold or with unknown credit, use the auto pacer. Blank is 10; invalid, negative or nonfinite values use 10 and raise PRESET_CONFIG_INVALID. |
| SKIP_WITHOUT_RESEARCH | false | false permits an outside-view forecast labelled RESEARCH NONE; true skips it. |
| LOOP_MINUTES | unset (45) | Positive run length up to 45 minutes; blank, invalid or out-of-range uses 45. |
| POLL_MINUTES | unset (10) | Positive poll interval up to 10 minutes; blank, invalid or out-of-range uses 10. |
| ALERT_MENTION | empty | Set to your GitHub mention to receive issue notifications. |

Secrets are METACULUS_TOKEN, OPENROUTER_API_KEY, optional OPENAI_API_KEY, and either ASKNEWS_API_KEY or the pair ASKNEWS_CLIENT_ID and ASKNEWS_SECRET. Enter values only in GitHub's secret form. Do not paste them into files, issues, logs, comments, or chat. Never regenerate the bot key merely to troubleshoot this code.

## Responding to alerts

Every issue names its key. Check counts and skip reasons; do not inspect live forecast values or rationales. For any code fault, **if no helper is available, set BOT_ENABLED to false**.

| Alert key | What to do |
| --- | --- |
| PRESET_CONFIG_INVALID | Set MODEL_PRESET to auto, A, B or C; set PRESET_RESERVE_USD to a finite, nonnegative number or leave it unset for 10. The bot uses the documented defaults and continues. This is a P1 configuration alert; supplied text is never echoed. |
| CREDITS_EXHAUSTED | **assume no more credit is coming**. Check whether you deliberately enabled the optional prepaid bridge. Submit another credit form yourself, in your own words. Otherwise leave forecasting stopped or arrange funding yourself. MiniBench top-ups and an open-source bonus are possible, not promised. |
| GATE_BLOCKED | The bot refused to send a forecast it could not verify. If it repeats and no helper is available, set BOT_ENABLED=false. |
| RUN_FAILED | If it repeats and no helper is available, set BOT_ENABLED=false. |
| POLL_FAILING | If it repeats and no helper is available, set BOT_ENABLED=false. |
| COMMENT_FAILED | The forecast is posted but its private comment is missing. Ask a helper. Never re-run the forecast. |
| NO_LLM_KEY | Check the secret names. OpenRouter needs OPENROUTER_API_KEY. The bridge also needs USE_OPENAI_BRIDGE=true. Run Test Bot after correcting the setup. |
| NO_FALL_QUESTIONS | After October 12, ask a helper to check the target ID and closed-question reader. A failed API read is reported as unknown, not zero coverage. |
| INSTALL_FAILING | Open the failed install step in Test Bot. Ask a helper to check the pinned dependency files and runner tooling. Do not update dependencies by guesswork. |
| HEARTBEAT_FAILED | Edit .github/heartbeat.txt in GitHub's browser editor and commit the current UTC date. Ask a helper to check contents:write and branch rules. |
| API_REJECTED | A forecast or comment received a 4xx. Ask a helper to inspect Test Bot on a test branch. Check whether private comments are missing; the bot retries comments without resubmitting the forecast. |
| SEASON_OVER | Season forecasting and chaining stop on January 7, 2027 UTC. On January 7: Actions > "Forecast on new AI tournament questions" > ... > Disable workflow. Keep the fork, secrets and Test Bot working until any prize is paid. |
| CREDITS_LOW | Check remaining credit and your MiniBench setting. Estimated tier costs are conservative assumptions; actual spend may differ. |
| CREDIT_UNKNOWN | The credit reader could not obtain limit_remaining. The bot uses Tier C and stops on a credit-exhaustion response. Check Test Bot and the key's status. |
| MODEL_UNAVAILABLE | Read Test Bot's PROBE lines. Ask a helper for a reviewed model-ID change; do not substitute an unsupported provider. |
| RESEARCH_UNAVAILABLE | Check ASKNEWS_API_KEY or the complete client pair. Use the [Metaculus resources page](https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/#getting-asknews-setup) for AskNews help. The bot labels unavailable research and uses no invented news. |
| SKIPS | Read the reason counts and the weekly missed IDs. Repeated INVALID_OUTPUT needs a helper. TOO_LATE means the time margin was too small. ALL_MODELS_FAILED means no valid model forecast survived. |
| SCHEDULER_GAPS | Check Actions and the forecasting workflow are enabled. The measured gap ignores cancelled pending runs. If this repeats on 3 days, consider CHAIN_ENABLED=true; that is your call under GitHub's terms. |

Alerts use GitHub issues, with one new comment per key per 24 hours at most. A newly posted P0 alert fails schedule-started runs. Chain runs rely on issue notifications. If the log says ISSUES DISABLED, enable repository Issues and run Test Bot. Test Bot posts either test run ok or test run failed; it cannot turn a failed install/run into a success notification.

## Routine checks

The weekly issue reports closed-question counts, coverage, missed IDs, remaining credit, tiers, the heartbeat date, and the longest run gap. Coverage fields and GitHub delivery require the first Test Bot/live counts-only check; no live service was used during development.

At the first authorized tournament run, look only for both TARGET lines and CREDIT, then counts/skip reasons. TARGET open=unknown means the count reader failed; it is not a claim that there are no questions. Never request a preview of a tournament forecast.

At run end, expect exactly one PRESET line with the effective setting and season tiers selected. Examples: PRESET auto tier=C; PRESET A tier=A; PRESET A tier=A,C after the reserve forces an auto fallback. Multiple tiers are listed once each in sorted order, with no forecast values or reasoning. tier=none means no season tier was selected. Ordinary insufficient-credit handling is unchanged: the reserve itself never causes a skip, but truly exhausted credit still stops model calls. PRESET_CONFIG_INVALID reports only counters for invalid settings.

The heartbeat checks repository age and, after 25 days without a commit, stages only .github/heartbeat.txt. The built-in token's effect on GitHub's inactivity timer is unverified. Make a manual browser edit to that file on November 20; November 28 is the backup date if missed.

## v1 and every crash fix

Upload the reviewed replacement to a new branch named v1-test. Close any pull-request page without creating a pull request. Select v1-test when running Test Bot. Confirm a green run, every expected supported question newly posted with http=2xx, readback=found and comment=posted, and the issue notification. Only then upload the same files again to main. Never click Sync fork. Keep the existing pinned pyproject.toml and poetry.lock unless a reviewed release replaces them.

Date and conditional questions are intentionally unsupported. Invalid numeric/discrete CDFs are skipped in v0; there is no numeric repair or constant forecast fallback. UNVERIFIED: forecasts and comments are separate API calls; the SDK boundary needs Test Bot confirmation. Under that assumption, a permanent comment failure can leave a forecast awaiting its private comment; this is reported, retried and never hidden as a successful test.

The 45-minute loop, per-question deadlines and 58-minute run-step limit provide time for operations. Python cannot forcibly stop a stuck worker thread; external SDK cancellation and runner timeout behaviour require live verification. The task's ordinary model calls and SDK socket timeouts are bounded, with the Actions step providing the final limit.

## Prize steps

No AI-written messages to Metaculus or AskNews staff; the owner writes every message himself.

- The survey is required every season.
- Reply to the winner email within 30 days.
- Expect identity checks and likely a W-8BEN.
- Payment is by Ramp.
- An inspection means showing the code and running Test Bot.
- Keep the secrets and Test Bot working until paid.

Any future workflow that posts tournament forecasts must use concurrency group fbot-post-tournament. Test Bot keeps its separate group.

For v0.3, upload the reviewed files to v1-test, run Test Bot there, and only after it passes upload those same files to main. Feature v1 still requires its separate GO. The comment header keeps schema version v0.1 and includes each question ID. Set the two new controls under repository Variables, alongside BOT_ENABLED; they are not secrets. Leave MODEL_PRESET=auto to retain v0.2 pacing, or choose the desired preset and reserve before the next automated tournament run.
