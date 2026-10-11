"""The periods questions can ask about: full months and quarters, last 30 days, whole, weeks."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from adsight.generate.context import DatasetContext

PeriodKind = Literal["month", "quarter", "last30", "whole", "week"]

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
QUARTER_ORDINALS = ("first", "second", "third", "fourth")
MIN_WEEK_STARTS_PER_MONTH = 4


@dataclass(frozen=True)
class Period:
    key: str
    text: str
    start: date
    end: date
    kind: PeriodKind


def day_text(d: date) -> str:
    """'9 March 2026': never a leading zero."""
    return f"{d.day} {MONTH_NAMES[d.month - 1]} {d.year}"


def span_text(start: date, end: date) -> str:
    """'16 to 29 March 2026', '20 April to 3 May 2026', '28 December 2025 to 10 January
    2026': the end always carries month and year."""
    if (start.year, start.month) == (end.year, end.month):
        first = str(start.day)
    elif start.year == end.year:
        first = f"{start.day} {MONTH_NAMES[start.month - 1]}"
    else:
        first = day_text(start)
    return f"{first} to {day_text(end)}"


def month_text(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"


def month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, nxt - timedelta(days=1)


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def week_starts(ctx: DatasetContext) -> list[date]:
    """Starts of the full weeks: Mondays for daily data, the data's own week rows otherwise."""
    if ctx.grain == "week":
        first = ctx.data_start
    else:
        first = ctx.data_start + timedelta(days=(7 - ctx.data_start.weekday()) % 7)
    starts: list[date] = []
    current = first
    while current + timedelta(days=6) <= ctx.data_end:
        starts.append(current)
        current += timedelta(days=7)
    return starts


def full_months(ctx: DatasetContext) -> list[Period]:
    months: list[Period] = []
    starts = week_starts(ctx)
    for year, month in _months_between(ctx.data_start, ctx.data_end):
        start, end = _month_bounds(year, month)
        if ctx.grain == "week":
            inside = [s for s in starts if start <= s <= end]
            full = len(inside) >= MIN_WEEK_STARTS_PER_MONTH
        else:
            full = ctx.data_start <= start and end <= ctx.data_end
        if full:
            months.append(
                Period(month_key(year, month), month_text(year, month), start, end, "month")
            )
    return months


def full_quarters(ctx: DatasetContext) -> list[Period]:
    months = {p.key for p in full_months(ctx)}
    quarters: list[Period] = []
    for year in range(ctx.data_start.year, ctx.data_end.year + 1):
        for q in range(4):
            keys = [month_key(year, 3 * q + m) for m in (1, 2, 3)]
            if all(k in months for k in keys):
                start = _month_bounds(year, 3 * q + 1)[0]
                end = _month_bounds(year, 3 * q + 3)[1]
                first, last = MONTH_NAMES[3 * q], MONTH_NAMES[3 * q + 2]
                text = f"the {QUARTER_ORDINALS[q]} quarter of {year} ({first} to {last} {year})"
                quarters.append(Period(f"{year}-Q{q + 1}", text, start, end, "quarter"))
    return quarters


def last30(ctx: DatasetContext) -> Period:
    start = ctx.today - timedelta(days=30)
    text = f"the last 30 days ({span_text(start, ctx.data_end)})"
    return Period("last30", text, start, ctx.data_end, "last30")


def whole(ctx: DatasetContext) -> Period:
    return Period("whole", "the whole period", ctx.data_start, ctx.data_end, "whole")


def full_weeks(ctx: DatasetContext) -> list[Period]:
    """Monday-to-Sunday weeks, for daily data only."""
    if ctx.grain != "day":
        return []
    weeks: list[Period] = []
    for start in week_starts(ctx):
        iso_year, iso_week, _ = start.isocalendar()
        end = start + timedelta(days=6)
        first = f"{start.day} {MONTH_NAMES[start.month - 1]}"
        text = f"the week of {day_text(start)} (Monday {first} to Sunday {day_text(end)})"
        weeks.append(
            Period(f"{iso_year}-W{iso_week:02d}", text, start, start + timedelta(days=6), "week")
        )
    return weeks


def all_periods(ctx: DatasetContext) -> list[Period]:
    return [*full_months(ctx), *full_quarters(ctx), last30(ctx), whole(ctx), *full_weeks(ctx)]


def month_before_start(ctx: DatasetContext) -> str:
    year, month = ctx.data_start.year, ctx.data_start.month
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return month_text(year, month)
