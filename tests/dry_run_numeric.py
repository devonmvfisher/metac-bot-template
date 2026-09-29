"""Render a deterministic numeric report from stored fixtures; optionally time it."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fbot import numeric
from fbot.types import Question

FIXTURES = Path(__file__).parent / "fixtures" / "numeric_v1"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def declared(q, case):
    if "lines" in case:
        return numeric.parse_percentiles(q, "\n".join(case["lines"]))
    return dict(zip(numeric.LEVELS, case["declared"]))


def report():
    questions = load("questions.json")
    rows = ["# Numeric fixture dry run", "",
            "All forecast values below are SYNTHETIC fixture data. Resolved question shapes are metadata only.", "",
            "n is the bin count; there are n + 1 CDF points. Step ratios use the API min 0.01/n and max 0.2*200/n.",
            "Percentiles are read back with sdk_percentiles; outside means that level lies in an omitted tail.", "",
            "| Fixture | n | cdf[0] | cdf[-1] | min / API min | max / API max | Notes | p10 | p50 | p90 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |"]
    errors = []
    for filename in ("build_cases.json", "lane_cases.json"):
        for name, case in load(filename)["cases"].items():
            q = Question(**questions[case["question"]]["question"])
            label = filename + ":" + name
            try:
                built = numeric.build_cdf(q, declared(q, case))
            except numeric.NumericError as error:
                if error.reason != case["expect"]:
                    raise AssertionError((name, error.reason, case["expect"])) from error
                errors.append((label, error.reason))
                continue
            if case["expect"] != "ok":
                raise AssertionError((name, "expected rejection", case["expect"]))
            n = q.inbound_outcome_count
            steps = [b - a for a, b in zip(built.cdf, built.cdf[1:])]
            pairs = dict(numeric.sdk_percentiles(q, built.cdf))
            readback = [numeric.plain(pairs[level / 100]) if level / 100 in pairs else "outside"
                        for level in (10, 50, 90)]
            rows.append(f"| {label} | {n} | {built.cdf[0]:.10f} | {built.cdf[-1]:.10f} | "
                        f"{min(steps) / (0.01 / n):.6f} | {max(steps) / (0.2 * 200 / n):.6f} | "
                        + ", ".join(built.notes) + " | " + " | ".join(readback) + " |")
    rows.extend(["", "## Rejected fixture cases", "", "| Fixture | Reason |", "| --- | --- |"])
    rows.extend(f"| {name} | {reason} |" for name, reason in errors)
    return "\n".join(rows) + "\n"


def timing():
    questions, cases, extra = load("questions.json"), load("build_cases.json")["cases"], load("lane_cases.json")["cases"]
    q = Question(**questions["closed_linear"]["question"])
    selected = [cases["closed_linear"], cases["closed_fold"], extra["near_tie_collision"],
                cases["closed_linear"], cases["closed_fold"]]
    inputs = [declared(q, case) for case in selected]
    samples = []
    for _ in range(20):
        start = time.perf_counter()
        runs = [numeric.build_cdf(q, values).cdf for values in inputs]
        combined = numeric.combine(q, runs)
        samples.append((time.perf_counter() - start) * 1000)
        if not numeric.check_strict(q, combined.cdf):
            raise AssertionError("Timed build produced an invalid CDF")
    return {"tries": 20, "builds_per_try": 5, "combines_per_try": 1, "points": 201,
            "samples_ms": samples, "median_ms": statistics.median(samples), "limit_ms": 50,
            "passed": statistics.median(samples) < 50}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timing-out")
    args = parser.parse_args()
    Path(args.out).write_text(report(), encoding="utf-8", newline="\n")
    if args.timing_out:
        result = timing()
        Path(args.timing_out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"Median of 20 tries, five builds plus combine at 201 points: {result['median_ms']:.3f} ms")
        if not result["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
