"""Synthetic fixtures through actual decisions and the fake posting boundary."""
import argparse
import json
from pathlib import Path
from fbot.config import OPUS, SOL, FLASH
from fbot.pipeline import forecast
from fbot.types import Research
from .fakes import FakeMetaculus, deps_for, fixture, fixture_prepare


def document():
    names = tuple(path.stem for path in sorted((Path(__file__).parent / "fixtures").glob("*.json")))
    rows = ["# Fixture dry run", "", "All fixture questions and model responses are synthetic. Nothing was sent.",
            "Numeric CDFs come from explicit SDK fixtures; this does not test framework interpolation.",
            "All private comments below passed the exact-payload gate and output validator.", ""]
    for index, name in enumerate(names):
        question, data = fixture(name)
        tier = "C" if name == "binary_urgent" else ("A", "B", "C")[index % 3]
        deps = deps_for({slot[0]: data["model_text"] for slot in (OPUS, SOL, FLASH)}, fixture_prepare(data))
        result = forecast(question, Research(), tier, deps)
        api = FakeMetaculus(deps.state)
        api.submit(question, result)
        rows += [f"## {index + 1}. {name}", "", f"Tier {tier}; validation PASS; fixture qid={question.qid}, post={question.post_id}.",
                 "", "Forecast payload:", "```json", json.dumps(api.requests[0][1], indent=2), "```", "",
                 "Private comment summary (is_private=true; included_forecast=true):", "```text", result.summary, "```", ""]
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
