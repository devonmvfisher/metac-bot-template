"""Serial source mutations in fresh copies below an explicit temporary root."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ACCEPT = "tests.test_numeric_accept"
OWN = "tests.test_numeric"
MUTATIONS = [('PB01', 'STRICT_MIN = 1.05', 'STRICT_MIN = 1.0', 'tests.test_numeric_accept'),
 ('PB02',
  'cdf = [_median(list(zip(column, weights))) for column in zip(*cdfs)]',
  'cdf = [sum(v * w for v, w in zip(column, weights)) / sum(weights) for column in zip(*cdfs)]',
  'tests.test_numeric_accept'),
 ('PB03',
  '    result = []\n    for t in points:',
  '    slopes = [0.0] * len(xs)\n    result = []\n    for t in points:',
  'tests.test_numeric_accept'),
 ('PB04',
  '    if question.kind == "discrete":\n        snapped =',
  '    if False and question.kind == "discrete":\n        snapped =',
  'tests.test_numeric_accept'),
 ('PB05', '        bins[0] += low_raw', '        bins[0] += 0.0', 'tests.test_numeric_accept'),
 ('PB06', 'OPEN_TAIL_MIN = 0.002', 'OPEN_TAIL_MIN = 0.0', 'tests.test_numeric_accept'),
 ('PB07', 'MIX_MIN = 0.01', 'MIX_MIN = 0.2', 'tests.test_numeric_accept'),
 ('PB08',
  '    if wrong_fraction or wrong_percent:',
  '    if False and (wrong_fraction or wrong_percent):',
  'tests.test_numeric_accept'),
 ('PB09', '    if not -2 <= t50 <= 3:', '    if not -20 <= t50 <= 30:', 'tests.test_numeric_accept'),
 ('PB10',
  '            if not is_percent_unit(question.unit):',
  '            if False and not is_percent_unit(question.unit):',
  'tests.test_numeric_accept'),
 ('PB11',
  '        if level not in LEVELS:\n            continue',
  '        if level not in LEVELS:\n            raise NumericError("parse")',
  'tests.test_numeric_accept'),
 ('PB12', '    text = text.translate(str.maketrans("", "", "*_#`"))', '    text = text', 'tests.test_numeric_accept'),
 ('PB13',
  '    if any(p > cap for p in bins):',
  '    if False and any(p > cap for p in bins):',
  'tests.test_numeric_accept'),
 ('PB14',
  'LEVELS = (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99)',
  'LEVELS = (1, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99)',
  'tests.test_numeric_accept'),
 ('PB15',
  'return _switch(env) not in ("false", "0", "off", "no")',
  'return _switch(env) not in ("", "false", "0", "off", "no")',
  'tests.test_numeric_accept'),
 ('PB16',
  '    if any(b < a for a, b in zip(ordered, ordered[1:])):',
  '    if False and any(b < a for a, b in zip(ordered, ordered[1:])):',
  'tests.test_numeric_accept'),
 ('PB17', 'TAIL_TOTAL_MAX = 0.7', 'TAIL_TOTAL_MAX = 0.99', 'tests.test_numeric_accept'),
 ('PB18', '        if count >= 2:', '        if False and count >= 2:', 'tests.test_numeric_accept'),
 ('PB19',
  '    if question.kind != "discrete" and ordered[0] == ordered[-1]:',
  '    if False and question.kind != "discrete" and ordered[0] == ordered[-1]:',
  'tests.test_numeric_accept'),
 ('PB20',
  '            return question.lower + step / 2, question.upper - step / 2',
  '            return question.lower, question.upper',
  'tests.test_numeric_accept'),
 ('PB21', '    cdf = [round(value, 10) for value in cdf]', '    cdf = list(cdf)', 'tests.test_numeric_accept'),
 ('PB22', 'TAIL_K = 4 / 3', 'TAIL_K = 4', 'tests.test_numeric_accept'),
 ('PB23', '                    bins[j] += available', '                    bins[j] = cap', 'tests.test_numeric_accept'),
 ('PB24',
  '        if unit and len(value_text) > len(unit) and value_text.lower().endswith(unit.lower()):',
  '        if len(value_text) > len(unit) and value_text.lower().endswith(unit.lower()):',
  'tests.test_numeric_accept'),
 ('PB25',
  '        locations[i] = max(locations[i], locations[i - 1] + width / 1000)',
  '        pass',
  'tests.test_numeric_accept'),
 ('PB26',
  '        except ValueError:\n            continue',
  '        except ValueError:\n            raise NumericError("parse")',
  'tests.test_numeric_accept'),
 ('PB27',
  '    chosen = candidates[-1] if candidates else parts[-1]',
  '    chosen = parts[-1]',
  'tests.test_numeric_accept'),
 ('PB28',
  '        if cdf[0] < fraction < cdf[-1]:',
  '        if cdf[0] <= fraction < cdf[-1]:',
  'tests.test_numeric_accept'),
 ('PB29', '    if len(result) < 2:', '    if False and len(result) < 2:', 'tests.test_numeric_accept'),
 ('NM01', '    weights = [w / largest for w in weights]', '    weights = [1] * len(weights)', 'tests.test_numeric'),
 ('NM02',
  '    chosen = candidates[-1] if candidates else parts[-1]',
  '    chosen = candidates[0] if candidates else parts[-1]',
  'tests.test_numeric'),
 ('NM03',
  '        lines.append("Values should increase from percentile 1 to percentile 99.")\n    return "\\n".join(lines)',
  '        lines.append("Values should increase from percentile 1 to percentile 99.")\n'
  '    lines.append("Units: duplicate")\n'
  '    return "\\n".join(lines)',
  'tests.test_numeric'),
 ('NM04', '                return (value + pairs[i + 1][0]) / 2', '                return value', 'tests.test_numeric'),
 ('NM05', '        bins[-1] += high_raw', '        bins[-1] += 0.0', 'tests.test_numeric'),
 ('NM06', 'STRICT_MAX = 0.95', 'STRICT_MAX = 1.0', 'tests.test_numeric'),
 ('NM07',
  '        if not isinstance(reason, str) or reason not in REASONS:',
  '        if reason not in REASONS:',
  'tests.test_numeric'),
 ('NM08',
  '    try:\n'
  '        points = iter(points)\n'
  '    except TypeError as error:\n'
  '        raise NumericError("input") from error\n',
  '',
  'tests.test_numeric'),
 ('NM09',
  '    except (KeyError, TypeError, IndexError) as error:',
  '    except (KeyError, TypeError) as error:',
  'tests.test_numeric'),
 ('NM10',
  'lines = (" ".join(line.split()) for line in text.splitlines())',
  'lines = text.splitlines()',
  'tests.test_numeric')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmp", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    temp = Path(args.tmp).resolve()
    if not temp.is_dir() or temp == root or temp.is_relative_to(root):
        raise SystemExit("Use an existing temp directory outside the source tree.")
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1")
    original = (root / "fbot/numeric.py").read_text(encoding="utf-8")
    prepared = []
    for name, old, new, suite in MUTATIONS:
        if original.count(old) != 1 or old == new:
            raise SystemExit("Mutation anchor is not unique or changed: " + name)
        changed = original.replace(old, new, 1)
        ast.parse(changed, feature_version=(3, 11))
        prepared.append((name, old, suite, changed))
    baseline = subprocess.run([sys.executable, "-B", "tests/run_offline.py"], cwd=root,
                              env=env, capture_output=True, text=True, timeout=60)
    if baseline.returncode != 0:
        raise SystemExit("The unmodified suite must pass before mutation checks.\n" + baseline.stdout + baseline.stderr)
    records = []
    for name, anchor, suite, changed in prepared:
        container = Path(tempfile.mkdtemp(prefix="numeric-mut-" + name + "-", dir=temp)).resolve()
        if not container.is_relative_to(temp):
            raise SystemExit("Mutation path escaped temp root.")
        fork = container / "fork"
        shutil.copytree(root, fork, ignore=shutil.ignore_patterns("__pycache__"))
        (fork / "fbot/numeric.py").write_text(changed, encoding="utf-8", newline="\n")
        completed = subprocess.run([sys.executable, "-B", "tests/run_offline.py", suite], cwd=fork,
                                   env=env, capture_output=True, text=True, timeout=60)
        output = completed.stdout + completed.stderr
        (container / "output.txt").write_text(output, encoding="utf-8", newline="\n")
        failed = re.findall(r"^(?:FAIL|ERROR): ([^\n]+)", output, re.M)
        killed = completed.returncode != 0 and bool(failed)
        records.append({"name": name, "anchor": anchor, "suite": suite, "killed": killed,
                        "returncode": completed.returncode, "failing_tests": failed,
                        "mutated_sha256": hashlib.sha256(changed.encode()).hexdigest()})
        Path(args.out).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(name + ": " + ("KILLED" if killed else "SURVIVED"), flush=True)
    print(f"Mutations killed: {sum(r['killed'] for r in records)}/{len(records)}")
    raise SystemExit(not all(record["killed"] for record in records))


if __name__ == "__main__":
    main()
