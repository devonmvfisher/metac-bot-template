# Sextant v1

Sextant is an autonomous forecasting bot entered in Metaculus's Fall 2026 AI forecasting tournament (fall-futureeval-2026) and MiniBench. For each question it reads recent news, runs three to five forecasts with GPT-6 Sol, Claude Opus 5.5 and Gemini Flash, combines them (a weighted median of log-odds for yes/no, an unweighted median of the forecast curves for numeric, a weighted mean for multiple choice) and posts the result with a private comment. It uses fewer and cheaper runs when credit or time is short, and it skips a question rather than post a forecast it cannot verify.

**Live record, as of 2026-10-04:** running since 2026-09-27, each run starting the next. 194 runs completed and 0 failed (29 scheduled starts were cancelled by design because a run was already going). 8 season forecasts posted (6 of them since per-run counts began, each read back and commented); 0 blocked by the post gate. Every run ends with a counts-only `SEXTANT_VITALS v1` notice, a check-run annotation that GitHub's public API serves without sign-in: questions open, seen, posted and skipped with reasons, model calls and spend. Its own log lines are built to carry counts, not forecast values (a scan of a real run log is still to be done), and nobody looks at forecasts while questions are open.

**What was tested, and what was not:** 574 offline tests pass and 296 of 296 planted bugs are caught on this version (standard library only, no network, about 35 seconds). Test Bot runs posted to Metaculus test questions on 2026-10-02 (presets A and C, 8 forecasts each, all read back). Sextant was never run on past (closed) tournament questions, so its accuracy is unknown until this season's questions resolve. The entry form said it would be tested on about 100 closed Summer 2026 questions; that test was not run.

**Known limits:** accuracy is unknown until questions resolve; date and conditional questions are not supported; it forecasts MiniBench only while credit stays above a set floor, and that path first runs live when round 1 opens (2026-10-05 00:00 UTC).

**Reading the repo:** lines in main.py marked `GLUE (NOT EXECUTED offline)` are live SDK calls the offline suite cannot exercise; they run on every live run. main_with_no_framework.py and integrations/ come from the Metaculus template and are not used. DEVON-GITHUB-STEPS.md is the owner's setup checklist. Every change is dated in CHANGELOG.md.

## Operator notes

Sextant forecasts the configured season and MiniBench with shared budget reservations, validated outputs and private comments. Forecasting and chaining start disabled. Use [RUNBOOK.md](RUNBOOK.md) and [DEVON-GITHUB-STEPS.md](DEVON-GITHUB-STEPS.md) for the reviewed upload and Test Bot procedure.

MODEL_PRESET is auto/A/B/C; PRESET_RESERVE_USD defaults to 10. Season overrides require remaining credit after pending reservations and FLOOR_CREDIT to cover that reserve plus the tier cost. The auto pacer takes over below it. Questions closing within 12 minutes use M. MiniBench retains its auto budget rule plus the requested Flash floor. v1.1 adds a MiniBench dial with neutral defaults: MINIBENCH_MODE=skip stops MiniBench forecasting, MINIBENCH_PRESET (auto/A/B/C) picks its tier above the PRESET_RESERVE_USD reserve, and MINIBENCH_FLOOR_USD skips MiniBench below a credit floor. Season questions ignore all three.

| Tier | Runs | Config cost before safety | Reserved cost (x1.5) |
| --- | --- | --- | --- |
| A | Opus x2, Sol x2, Flash | 1.20 | 1.80 |
| B | Opus, Sol, Flash x2 | 0.77 | 1.155 |
| C | Sol, Flash x2; Sol x2 when Flash is down | 0.36 | 0.54 |
| M | Flash x3, urgent or affordability floor | 0.24 | 0.36 |

Flash tries 3.8, then 3.6, then Sol. Binary runs combine by weighted log-odds median (Opus/Sol 2, Flash 1); categories use weighted means and the option floor; NUMERIC_V1 uses a pointwise CDF median. A single successful run gets the single-run clamp. At the combination deadline minus 120 seconds, finished runs can post and unfinished runs appear in DROPPED. The post gate still enforces close minus 60 seconds and the job end.

After 20 measured questions in a tier, the ledger supplies a rolling seven-day cost with a conservative floor. Missing ledger data uses config costs. Concurrent before/after credit samples can include another question's spend; that can overestimate costs conservatively. Real provider accounting latency remains unverified.

OWN_KEY_MODE defaults to off. insurance or primary can use OPENROUTER_API_KEY_OWN for season questions. MiniBench never uses that key. Weekly status includes both remaining balances. Web research always uses the sponsored key.

The numeric, research, prompt, probe and concurrent-target switches default true. ASKNEWS_PARITY, ASKNEWS_ARCHIVE and DEADLINE_SHIFT (EDGE-2) are built but stay off for the Fall 2026 season (decided Sep 29). The resolution-check prompt line proposed with them was not built. If a switch is ever turned on, the change is dated here and in the bot's description first. All controls and alerts are in the runbook.

Offline verification uses the standard library:

```text
python -B tests/run_offline.py
python -B tests/mutate.py --tmp <job-temp-folder> --out <main-mutations.json>
python -B tests/mutate_numeric.py --tmp <job-temp-folder> --out <numeric-mutations.json>
python -B tests/mutate_r2.py --tmp <job-temp-folder> --out <research-mutations.json>
python -B tests/mutate_ops.py --tmp <job-temp-folder> --out <ops-mutations.json>
python -B -m tests.dry_run --output <fixture-report.md>
```

Set TEMP/TMP to the job temporary folder. Tests block external sockets and subprocesses; mutation runners start guarded test processes serially. No dependencies were installed and no service was contacted for this release. The pinned SDK is forecasting-tools 0.2.92; pyproject.toml, poetry.lock and bot_helpers.py retain their original bytes. Every main.py line marks live SDK glue as NOT EXECUTED offline. Only the operator's later Test Bot runs can verify that boundary.
