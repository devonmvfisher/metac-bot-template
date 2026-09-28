# Forecast bot v0.3

This fork targets the Fall 2026 season and MiniBench with explicit model IDs, budget tiers, output checks, and private reasoning comments. Forecasting and the optional run chain are off by default. Follow RUNBOOK.md and the reviewed setup guide before enabling them.

Repository Actions variables now support MODEL_PRESET and PRESET_RESERVE_USD. Leave MODEL_PRESET unset or set it to auto to keep the existing pacer. Set A, B or C to select that tier for season questions while available credit after pending reservations and FLOOR_CREDIT covers PRESET_RESERVE_USD plus the tier cost. PRESET_RESERVE_USD defaults to 10 dollars. Below that threshold, or when credit is unknown, the existing auto pacer takes over. MiniBench and Test Bot keep their existing rules; the optional bridge route is unchanged.

For example, with no pending reservations, preset A needs at least 14 dollars of remaining credit: the 2-dollar floor, 10-dollar reserve and 2-dollar Tier A reservation. An invalid preset uses auto and raises PRESET_CONFIG_INVALID without echoing the supplied text. An invalid reserve uses 10 and raises the same alert. Reserve values must be finite, nonnegative numbers. At run end, one line reports the effective preset and season tiers selected: PRESET A tier=A,C means the run selected A and later auto C; tier=none means no season tier was selected.

Offline tests use the Python standard library:

```text
python -B tests/run_offline.py
python -B tests/mutate.py --tmp <job-temp-folder> --out <mutation-report.json>
python -B -m tests.dry_run --output <fixture-report.md>
```

Set TEMP and TMP to your job's temporary folder before running those commands. Tests deny external socket connections and subprocess execution; the mutation runner starts one guarded test process at a time. Windows asyncio's local wake-up socket pairs are permitted.

Do not run main.py locally with tournament credentials to inspect forecasts. All dry runs use fictional fixtures. Framework-dependent integration is marked NOT EXECUTED offline on every adapter line; only an authorized Test Bot run checks the locked SDK and live service contracts.
