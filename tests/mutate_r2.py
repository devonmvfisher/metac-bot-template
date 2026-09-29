"""Run the named contract mutations serially in disposable copies."""
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
import time

SUITES = [
    "tests.test_r2_webread", "tests.test_r2_sources", "tests.test_r2_research2",
    "tests.test_r2_markets", "tests.test_r2_pipeline", "tests.test_r2_release",
    "tests.test_r2_extra_webread", "tests.test_r2_extra_sources",
    "tests.test_r2_extra_research2", "tests.test_r2_extra_markets", "tests.test_r2_extra_review", "tests.test_r2_f5",
    "tests.test_r3_terms",
]
MUTATIONS = [('PB01',
  'webread',
  'every address public',
  'return address.is_global and not address.is_multicast  # PB01',
  'return True  # PB01'),
 ('PB02',
  'webread',
  'redirect destinations bypass address checks',
  'reason = check_url(current, resolve)  # PB02',
  'reason = check_url(current, resolve) if hop == 0 else None  # PB02'),
 ('PB03',
  'webread',
  'extra request header',
  'headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(accept)}  # PB03',
  'headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(accept), "Cookie": "extra"}  # PB03'),
 ('PB04', 'webread', 'ignore declared oversize', 'if declared > max_bytes:  # PB04', 'if False:  # PB04'),
 ('PB05', 'webread', 'body exceeds streaming cap', 'if len(data) > max_bytes:  # PB05', 'if False:  # PB05'),
 ('PB06',
  'webread',
  'no deadline check between reads',
  'if deadline is not None and clock.monotonic() > deadline:  # PB06',
  'if False:  # PB06'),
 ('PB07', 'webread', 'no media type check', 'if content_type not in accept:  # PB07', 'if False:  # PB07'),
 ('PB08', 'webread', 'HTTP failures become evidence', 'if not 200 <= status < 300:  # PB08', 'if False:  # PB08'),
 ('PB09',
  'webread',
  'keep all-caps closing markers',
  'is_end = stripped.startswith("END ") and stripped == stripped.upper()  # PB09',
  'is_end = False  # PB09'),
 ('PB10',
  'research2',
  'omit native search engine',
  '"plugins": [{"id": "web", "engine": ENGINE, "max_results": MAX_RESULTS}],  # PB10',
  '"plugins": [{"id": "web", "max_results": MAX_RESULTS}],  # PB10'),
 ('PB11', 'research2', 'oversized brief', 'MAX_CHARS = 6000  # PB11', 'MAX_CHARS = 60000  # PB11'),
 ('PB12', 'research2', 'unexpected exceptions escape', 'except Exception:  # PB12', 'except ModelFailure:  # PB12'),
 ('PB13',
  'research2',
  'retry another model after credit exhaustion',
  'if status in (400, 404):  # PB13',
  'if status in (400, 402, 404):  # PB13'),
 ('PB14',
  'markets',
  'standalone forecast-looking price line',
  'f" | {_space(m.url)[:200]}")  # PB14',
  'f" | {_space(m.url)[:200]}\\nProbability: 43.5%")  # PB14'),
 ('PB15', 'markets', 'show unrelated markets', 'MIN_SCORE = 2  # PB15', 'MIN_SCORE = 0  # PB15'),
 ('PB16',
  'markets',
  'keep closed markets',
  'if market.get("closed") or market.get("active") is False:  # PB16',
  'if market.get("active") is False:  # PB16'),
 ('PB17', 'markets', 'call after budget expired', 'if remaining <= 0:  # PB17', 'if False:  # PB17'),
 ('PB18',
  'sources',
  'keep trailing punctuation in URLs',
  'url = match.group().rstrip(".,;:!?\'\\"")  # PB18',
  'url = match.group()  # PB18'),
 ('PB19', 'sources', 'ignore robots policy', 'elif not self._robots_allow(url, end):  # PB19', 'elif False:  # PB19'),
 ('PB20', 'webread', 'unset switches default off', 'return default  # PB20', 'return False  # PB20'),
 ('PB21',
  'research2',
  'log credential',
  '            usd = "unknown" if result.cost_usd is None else f"{result.cost_usd:.4f}"',
  '            logger.info("RESEARCH2 key=%s", key)\n'
  '            usd = "unknown" if result.cost_usd is None else f"{result.cost_usd:.4f}"'),
 ('PB22', 'research2', 'remove question cache', 'if qid in self._cache:  # PB22', 'if False:  # PB22'),
 ('PB23',
  'webread',
  'alert before five questions',
  'return tried >= 5 and failed * 5 > tried  # PB23 PB24',
  'return tried >= 1 and failed * 5 > tried  # PB23 PB24'),
 ('PB24',
  'webread',
  'alert at exactly twenty percent',
  'return tried >= 5 and failed * 5 > tried  # PB23 PB24',
  'return tried >= 5 and failed * 5 >= tried  # PB23 PB24'),
 ('PB25', 'sources', 'ignore blocked-host memo', 'if host in self._blocked:  # PB25', 'if False:  # PB25'),
 ('PB27',
  'markets',
  'keep resolved markets',
  'if not isinstance(item, dict) or item.get("isResolved"):  # PB27',
  'if not isinstance(item, dict):  # PB27'),
 ('PB28',
  'sources',
  'rank by raw hits instead of density',
  'key=lambda i: (-hits[i] / (len(lines[i]) + 40), i))  # PB28',
  'key=lambda i: (-hits[i], i))  # PB28'),
 ('PB29', 'webread', 'retain navigation and footer text', '"template", "nav", "footer",', '"template",'),
 ('PB30',
  'research2',
  'retain forecast-looking lines',
  'if not FORECAST_LINE.match(line)]  # PB30',
  'if True]  # PB30'),
 ('PB31',
  'sources',
  'robots request drops injected adapters',
  'open_url=self.open_url, resolve=self.resolve, allow=self.hosts)  # PB31',
  'open_url=None, resolve=None, allow=self.hosts)  # PB31'),
 ('PB32', 'research2', 'omit reasoning effort', '        "reasoning": dict(REASONING),  # PB32\n', ''),
 ('PB33',
  'research2',
  'fresh lock permits duplicate in-flight request',
  'qid_lock = self._qid_locks.setdefault(qid, threading.Lock())  # PB33',
  'qid_lock = threading.Lock()  # PB33'),
 ('PB34',
  'webread',
  'section keeps closing markers',
  'return heading, clip(neutralise(text, (heading, "END SECTION")), SECTION_CHARS)  # PB34',
  'return heading, clip(text, SECTION_CHARS)  # PB34'),
 ('OWN01',
  'webread',
  'HTML source whitespace splits table rows',
  'self.parts.append(re.sub(r"\\s+", " ", data))  # OWN01',
  'self.parts.append(data)  # OWN01'),
 ('OWN02', 'sources', 'cache transient timeout', 'if result[0] != "timeout":', 'if True:'),
 ('OWN03',
  'research2',
  'accept boolean cost',
  'not isinstance(value, bool) and isinstance(value, (int, float))',
  'isinstance(value, (int, float))'),
 ('OWN04',
  'markets',
  'accept a price greater than one',
  'return number if number is not None and number <= 1 else None',
  'return number'),
 ('REV01',
  'webread',
  'review regression',
  '            if self.line_text:\n',
  '            if "".join(self.parts).rsplit("\\n", 1)[-1].strip():\n'),
 ('REV02',
  'webread',
  'review regression',
  '                reader = getattr(response, "read1", None) or response.read\n',
  '                reader = response.read\n'),
 ('REV03',
  'sources',
  'review regression',
  '            reply = webread.get(url, clock=self.clock, deadline=end,\n',
  '            with self._lock:\n              reply = webread.get(url, clock=self.clock, deadline=end,\n'),
 ('REV04',
  'markets',
  'review regression',
  'qid_lock = self._qid_locks.setdefault(question.qid, threading.Lock())',
  'qid_lock = self._qid_locks.setdefault(0, threading.Lock())'),
 ('F5',
  'research2',
  'citation whitespace',
  '            if any(ch.isspace() for ch in url):\n                continue\n',
  ''),
 ('R3UA', 'webread', 'user agent without contact',
  'USER_AGENT = "SextantBot/1.0 (+https://github.com/devonmvfisher/metac-bot-template)"',
  'USER_AGENT = "SextantBot/1.0"'),
 ('R3D', 'webread', 'denied hosts reachable',
  'return host_in(host, DENIED_HOSTS) or (allow is not None and not host_in(host, allow))  # R3D',
  'return allow is not None and not host_in(host, allow)  # R3D'),
 ('R3H', 'webread', 'deny checked on the first hop only',
  'if denied(current):  # R3H', 'if hop == 0 and denied(current):  # R3H'),
 ('R3L', 'webread', 'caller host list ignored',
  'if allow is not None and denied(current, allow):  # R3L', 'if False:  # R3L'),
 ('R3C', 'sources', 'uncleared hosts read',
  'webread.host_in(host, SKIP_HOSTS) or not webread.host_in(host, hosts)  # R3C',
  'webread.host_in(host, SKIP_HOSTS)  # R3C'),
 ('R3K', 'sources', 'a caller allowlist beats the skip list',
  'webread.host_in(host, SKIP_HOSTS) or not webread.host_in(host, hosts)  # R3C',
  'not webread.host_in(host, hosts)  # R3C'),
 ('R3A', 'sources', 'page redirects ignore the cleared hosts',
  'open_url=self.open_url, resolve=self.resolve, allow=self.hosts)\n            reason, text = reply.reason, ""',
  'open_url=self.open_url, resolve=self.resolve)\n            reason, text = reply.reason, ""'),
 ('R3B', 'sources', 'robots refusal by denial treated as allow',
  'elif reply.reason == "denied" or reply.reason == "http" and reply.status in (401, 403):',
  'elif reply.reason == "http" and reply.status in (401, 403):'),
 ('R3P', 'markets', 'Polymarket on by default',
  'SWITCHES["Polymarket"], False):  # R3P', 'SWITCHES["Polymarket"], True):  # R3P'),
 ('R3M', 'markets', 'Manifold on by default',
  'SWITCHES["Manifold"], False):  # R3M', 'SWITCHES["Manifold"], True):  # R3M')]


def inventory(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    temp = Path(args.tmp).resolve()
    out = Path(args.out).resolve()
    if not temp.is_dir() or temp.is_relative_to(root) or out.is_relative_to(root):
        raise SystemExit("Temp and output must be outside the source fork.")
    if out.exists() or not out.parent.is_dir():
        raise SystemExit("Use an existing output folder and a new result filename.")
    if len(MUTATIONS) != 52 or len({m[0] for m in MUTATIONS}) != 52:
        raise SystemExit("Expected 52 distinct mutations.")
    before = inventory(root)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    records = []
    for name, module, description, old, new in MUTATIONS:
        file = "fbot/" + module + ".py"
        original = (root / file).read_text(encoding="ascii")
        if original.count(old) != 1:
            raise SystemExit("Mutation anchor is not unique: " + name)
        changed = original.replace(old, new, 1)
        ast.parse(changed, feature_version=(3, 11))
        container = Path(tempfile.mkdtemp(prefix="r2-mut-" + name + "-", dir=temp)).resolve()
        if not container.is_relative_to(temp):
            raise SystemExit("Mutation path escaped temp root.")
        fork = container / "fork"
        shutil.copytree(root, fork, ignore=shutil.ignore_patterns("__pycache__"))
        (fork / file).write_text(changed, encoding="ascii", newline="\n")
        command = [sys.executable, "-B", "tests/run_offline.py"] + SUITES
        started = time.monotonic()
        completed = subprocess.run(command, cwd=fork, env=env, capture_output=True,
                                   text=True, encoding="utf-8", timeout=50)
        elapsed = time.monotonic() - started
        output = completed.stdout + completed.stderr
        (container / "TEST-OUTPUT.txt").write_text(output, encoding="utf-8", newline="\n")
        failed = re.findall(r"^(?:FAIL|ERROR): ([^\n]+)", output, re.M)
        killed = completed.returncode != 0 and bool(failed)
        records.append({"mutation": name, "file": file, "description": description,
                        "anchor": old, "replacement": new, "suites": SUITES,
                        "status": "KILLED" if killed else "SURVIVED", "killed": killed,
                        "returncode": completed.returncode, "seconds": round(elapsed, 6),
                        "failed_checks": failed, "temp_copy": container.name})
        print(name + ": " + records[-1]["status"], flush=True)
    if before != inventory(root):
        raise SystemExit("Original source fork changed during mutations.")
    out.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Mutations killed: {sum(record['killed'] for record in records)}/{len(records)}")
    raise SystemExit(not all(record["killed"] for record in records))


if __name__ == "__main__":
    main()
