# Sextant v1

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

The numeric, research, prompt, probe and concurrent-target switches default true; ASKNEWS_PARITY, ASKNEWS_ARCHIVE and DEADLINE_SHIFT default false pending EDGE-2 approval. All controls and alerts are in the runbook.

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
