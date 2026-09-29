"""Synthetic fixtures through actual decisions and the fake posting boundary."""
import argparse
import json
from pathlib import Path
from fbot.config import OPUS, SOL, FLASH
from fbot.pipeline import forecast
from fbot.types import Research
from .fakes import FakeMetaculus, deps_for, fixture, fixture_prepare
from fbot import markets, research2, sources


def document():
    names = tuple(path.stem for path in sorted((Path(__file__).parent / "fixtures").glob("*.json")))
    rows = ["# Fixture dry run", "", "All fixture questions and model responses are synthetic. Nothing was sent.",
            "The twenty legacy fixtures retain SDK CDF fixtures; the v1 soak below builds its numeric CDFs locally.",
            "All private comments below passed the exact-payload gate and output validator.", ""]
    for index, name in enumerate(names):
        question, data = fixture(name)
        tier = "M" if name == "binary_urgent" else ("A", "B", "C")[index % 3]
        deps = deps_for({slot[0]: data["model_text"] for slot in (OPUS, SOL, FLASH)}, fixture_prepare(data))
        deps.evidence = (research2.WebBrief('Synthetic dated web fact.', 1, True, 'ok', .03),
                         sources.PagesBrief('Synthetic resolution-source fact.', 1, 1, ('ok',)),
                         markets.MarketsBrief('Synthetic related market evidence; never blended.', 1, 1, 1))
        result = forecast(question, Research(), tier, deps)
        api = FakeMetaculus(deps.state)
        api.submit(question, result)
        rows += [f"## {index + 1}. {name}", "", f"Tier {tier}; validation PASS; fixture qid={question.qid}, post={question.post_id}.",
                 "", "Forecast payload:", "```json", json.dumps(api.requests[0][1], indent=2), "```", "",
                 "Private comment summary (is_private=true; included_forecast=true):", "```text", result.summary, "```", ""]
    from .test_v1_soak import run_soak
    from .test_v1_credit import partial_forecast
    from .fakes import FakeClock, FakeGit, FakeGitHub
    from fbot import ops
    import io
    import logging
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    logger, previous = logging.getLogger('fbot'), logging.getLogger('fbot').level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        report, summaries = run_soak()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    clock = FakeClock()
    schedule_rows = []
    ops.run({'BOT_ENABLED': 'true', 'CHAIN_ENABLED': 'true', 'JOB_START': str(clock.now().timestamp()-1000)},
            {'counts': {}, 'skips': {}, 'alerts': []}, clock, FakeGitHub(clock.now()), FakeGit(clock.now()), emit=schedule_rows.append)
    rows += ['## v1 soak', '', json.dumps({k:v for k,v in report.items() if k != 'rows'}, sort_keys=True), '',
             'Counts-only run lines:', '```text', captured.getvalue().rstrip(), *schedule_rows, '```', '',
             'Selected synthetic v1 summaries:', '```text', '\n\n'.join(summaries), '```', '',
             'Evidence headings in each fixture prompt:', '```text', research2.HEADING, sources.HEADING, markets.HEADING, '```', '',
             '## Partial ensemble at the deadline', '',
             'Two finished runs post through the real gate; the pending Opus and failed Flash are listed below.',
             '```text', partial_forecast()[1].summary, '```', '']
    return "\n".join(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    text = document()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text)
