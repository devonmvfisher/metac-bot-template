"""Strict extraction from FINAL sections, never inferred missing probabilities."""
import math
import re
from .config import PERCENTILES

NUMBER = r"[+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)"


def final_text(text):
    sections = re.split(r"(?im)^\s*FINAL\s*:?\s*", text)
    return sections[-1].strip()


def probability(text):
    match = re.fullmatch(r"\s*(?:Probability\s*:\s*)?(" + NUMBER + r")\s*(%)?\s*", text, re.I)
    if not match:
        raise ValueError("probability format")
    value = float(match[1].replace(",", ""))
    if match[2]:
        value /= 100
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("probability range")
    return value


def number(text, unit=""):
    match = re.fullmatch(r"\s*[$£€]?\s*(" + NUMBER + r")\s*(thousand|million|billion|[KMB])?\s*", text, re.I)
    if not match:
        raise ValueError("numeric format")
    value = float(match[1].replace(",", ""))
    suffix = (match[2] or "").lower()
    scales = {"k": 1000, "thousand": 1000, "m": 1000000, "million": 1000000,
              "b": 1000000000, "billion": 1000000000}
    if suffix:
        value *= scales[suffix]
        normalized_unit = unit.lower().strip()
        if "billion" in normalized_unit or normalized_unit.startswith("b "):
            value /= 1000000000
        elif "million" in normalized_unit or normalized_unit.startswith("m "):
            value /= 1000000
        elif "thousand" in normalized_unit or normalized_unit.startswith("k "):
            value /= 1000
    if not math.isfinite(value):
        raise ValueError("nonfinite")
    return value


def label(text):
    return " ".join(text.split()).casefold()


def base_rate(text):
    outside = re.search(r"(?im)^OUTSIDE VIEW\s*:\s*(.*)", text)
    if not outside:
        return None
    match = re.search(r"(?i)base rate\s*:?\s*(" + NUMBER + r"\s*%?)", outside[1])
    return match[1].strip() if match else None


def parse(question, text):
    text = final_text(text)
    if question.kind == "binary":
        matches = re.findall(r"(?im)^\s*(?:Probability\s*:\s*)?(" + NUMBER + r"\s*%?)\s*$", text)
        if len(matches) != 1:
            raise ValueError("missing or duplicate probability")
        return probability(matches[0])
    if question.kind == "multiple_choice":
        wanted = {label(v): v for v in question.options}
        if len(wanted) != len(question.options):
            raise ValueError("ambiguous options")
        result = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            name, sep, amount = line.rpartition(":")
            key = wanted.get(label(name))
            if not sep or key is None or key in result:
                raise ValueError("unknown or duplicate option")
            result[key] = probability(amount)
        if set(result) != set(question.options) or sum(result.values()) <= 0:
            raise ValueError("missing options")
        total = sum(result.values())
        if abs(total - 1) > 0.02:
            raise ValueError("invalid model probability sum")
        return {k: v / total for k, v in result.items()}
    if question.kind in ("numeric", "discrete"):
        result = {}
        for line in text.splitlines():
            match = re.fullmatch(r"\s*Percentile\s+(\d+)\s*:\s*(.+)\s*", line, re.I)
            if not match:
                if line.strip():
                    raise ValueError("numeric line format")
                continue
            percentile = int(match[1])
            if percentile in result:
                raise ValueError("duplicate percentile")
            result[percentile] = number(match[2], question.unit)
        if set(result) != set(PERCENTILES):
            raise ValueError("missing percentile")
        return result
    raise ValueError("unsupported type")
