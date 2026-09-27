# Forecast bot v0

This fork targets the Fall 2026 season and MiniBench with explicit model IDs, budget tiers, output checks, and private reasoning comments. Forecasting and the optional run chain are off by default. Follow RUNBOOK.md and the reviewed setup guide before enabling them.

Offline tests use the Python standard library:

```text
python -B tests/run_offline.py
python -B tests/mutate.py --tmp <job-temp-folder> --out <mutation-report.json>
python -B -m tests.dry_run --output <fixture-report.md>
```

Set TEMP and TMP to your job's temporary folder before running those commands. Tests deny external socket connections and subprocess execution; the mutation runner starts one guarded test process at a time. Windows asyncio's local wake-up socket pairs are permitted.

Do not run main.py locally with tournament credentials to inspect forecasts. All dry runs use fictional fixtures. Framework-dependent integration is marked NOT EXECUTED offline on every adapter line; only an authorized Test Bot run checks the locked SDK and live service contracts.
