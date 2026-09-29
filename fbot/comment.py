"""Compact private summaries; FINAL starts within the public stub."""
import os
import re
from . import SkipQuestion
from .config import VERSION, SHORT
from .guards import nominal
from .parse import base_rate


def clean(text, env):
    text = str(text)
    for name, value in env.items():
        if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?<![A-Za-z])[A-Za-z]:[\\/](?!/)[^\s]*|(?<![\w.:/-])/(?:home|Users)/[^\s]*", "[path]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[address]", text)
    text = re.sub(r"(?i)\b(?:sk)[-][A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"[0-9a-fA-F]{40,}", "[redacted]", text)
    return text.replace("\r", "")


def quantile(question, cdf, fraction):
    if fraction <= cdf[0]:
        return nominal(question, 0)
    if fraction >= cdf[-1]:
        return nominal(question, 1)
    for index, (left, right) in enumerate(zip(cdf, cdf[1:])):
        if left <= fraction <= right:
            return nominal(question, (index + (fraction - left) / (right - left)) / (len(cdf) - 1))
    raise SkipQuestion("INVALID_OUTPUT")


def quantile_label(question, cdf, fraction):
    if question.open_lower and fraction < cdf[0]:
        return f"<{question.lower:.6g}"
    if question.open_upper and fraction > cdf[-1]:
        return f">{question.upper:.6g}"
    return f"{quantile(question, cdf, fraction):.6g}"


def final(question, result, room):
    if question.kind == "binary":
        return f"FINAL {result.value * 100:.1f}%", ""
    if question.kind in ("numeric", "discrete"):
        entries = [f"p{p}={quantile_label(question, result.cdf, p / 100)}" for p in (10, 50, 90)]
        return "FINAL " + ",".join(entries) + (" " + question.unit[:40] if question.unit else ""), ""
    entries = [f"o{i + 1}={result.value[option] * 100:.1f}%" for i, option in enumerate(question.options)]
    first = []
    for i, entry in enumerate(entries):
        tail = f";+{len(entries) - i - 1} more" if i < len(entries) - 1 else ""
        if len("FINAL " + ";".join(first + [entry]) + tail) > room:
            break
        first.append(entry)
    remaining = entries[len(first):]
    line = "FINAL " + ";".join(first)
    if remaining:
        line += f";+{len(remaining)} more"
        line += "\nFINAL+ " + ";".join(remaining)
    return line, "OPTIONS " + ";".join(f"{i + 1}:{label}" for i, label in enumerate(question.options))


def section(text, name):
    match = re.search(r"(?im)^" + name + r"\s*:\s*(.*)", text)
    return " ".join(match[1].split()) if match else "unavailable"


def build(question, result, attempted, research, env=None):
    env = os.environ if env is None else env
    names = "/".join(SHORT.get(m, "model") for m in result.models)
    header = clean(f"FBOT {VERSION} | q{question.qid} | {question.target} | tier {result.tier} | "
                   f"runs ok {len(result.models)}/{attempted} | models {names}", env)[:170]
    final_line, mapping = final(question, result, 199 - len(header))
    final_line = clean(final_line, env)
    rationale = result.rationales[0] if result.rationales else ""
    outside = clean(section(rationale, "OUTSIDE VIEW"), env)[:100]
    inside = clean(section(rationale, "INSIDE VIEW"), env)[:125]
    method = {"binary": "weighted log-odds median", "multiple_choice": "weighted option mean + floor",
              "numeric": "percentile mean, sorted", "discrete": "percentile mean, sorted"}[question.kind]
    if result.numeric_v1:
        from .numeric import METHOD
        method = METHOD
    research_line = f"AskNews latest news, {research.articles} articles" if research.available else "NONE (unavailable)"
    research_line = result.research_label or research_line
    dropped = "; ".join(result.dropped) or "none"
    caps = ",".join(result.caps) or "none"
    fixed = [header, final_line, clean(f"HOW {method}; caps applied:{caps}; {len(result.models)}/{attempted} ok", env),
             clean(f"RESEARCH {research_line}", env), clean(f"DROPPED {dropped}", env)]
    outside = clean(f"OUTSIDE VIEW {outside}; base rate {base_rate(rationale) or 'unavailable'}", env)
    inside = "INSIDE VIEW " + inside
    labels = [clean(" ".join(label.split()), env) for label in question.options]
    def mapped(width):
        return "OPTIONS " + ";".join(f"{i + 1}:{label[:width]}" for i, label in enumerate(labels)) if mapping else ""
    option_row = mapped(max((len(label) for label in labels), default=0))
    def joined():
        return "\n".join(row for row in [*fixed[:2], option_row, fixed[2], outside, inside, *fixed[3:]] if row)
    # Trim inside then outside before shortening option labels.
    overflow = max(0, len(joined()) - 1000)
    inside = inside[:max(0, len(inside) - overflow)]
    overflow = max(0, len(joined()) - 1000)
    outside = outside[:max(0, len(outside) - overflow)]
    width = max((len(label) for label in labels), default=0)
    while len(joined()) > 1000 and width > 1:
        width -= 1
        option_row = mapped(width)
    # Fixed metadata is bounded too; it never displaces a forecast value.
    if len(joined()) > 1000:
        for index in (4, 3, 2):
            overflow = max(0, len(joined()) - 1000)
            fixed[index] = fixed[index][:max(0, len(fixed[index]) - overflow)]
    summary = joined()[:1000]
    from .hardening import cap_rationales
    result.summary = summary
    result.rationales = cap_rationales(summary, [clean(text, env) for text in result.rationales])
    return result
