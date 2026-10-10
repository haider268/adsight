"""Number verification: every number an answer states must be traceable to the evidence."""

import re

from adsight.agent.schemas import AnswerJSON
from adsight.benchmark.score import matches

MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_DAY_MONTH = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?={MONTHS}\b)", re.I)
_MONTH_DAY = re.compile(rf"\b((?:{MONTHS})\.?)\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+")


def _strip_dates(text: str) -> str:
    """Drop ISO dates and the day-of-month of written dates, keeping the rest."""
    text = _ISO_DATE.sub(" ", text)
    text = _DAY_MONTH.sub(" ", text)
    return _MONTH_DAY.sub(r"\1 ", text)


def numbers_in(text: str) -> list[float]:
    """Every number in `text` (thousands separators allowed), as absolute values."""
    return [float(m.group().replace(",", "")) for m in _NUMBER.finditer(text)]


def _is_year(value: float) -> bool:
    return value.is_integer() and 1900 <= value <= 2100


def _traceable(value: float, sources: list[float]) -> bool:
    """Within 1% (or 0.01) of some source; a ratio source (below 1) may be stated as a
    percent, compared as src * 100 so the tolerance stays relative to the percent."""
    return any(
        matches(src, value, "number") or (0 < src < 1 and matches(src * 100, value, "number"))
        for src in sources
    )


def untraceable_numbers(answer: AnswerJSON, question: str, evidence: str) -> list[float]:
    """Numbers stated in the summary or values that no number in `evidence` supports.

    Ignored: years 1900-2100, day-of-month numbers inside a date, and numbers that already
    appear in the question. A ratio may be stated as a percent.
    """
    asked = set(numbers_in(question))
    sources = numbers_in(evidence)
    stated = numbers_in(_strip_dates(answer.summary)) + [abs(v.value) for v in answer.values]
    missing: list[float] = []
    for value in stated:
        if _is_year(value) or value in asked or value in missing:
            continue
        if not _traceable(value, sources):
            missing.append(value)
    return missing
