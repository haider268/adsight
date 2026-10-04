"""The metric library: every metric is defined once here, from canonical measures only."""

from dataclasses import dataclass
from typing import Literal

from adsight.semantic.model import MeasureName, SemanticModel
from adsight.semantic.sql import measure_col

MetricUnit = Literal["usd", "count", "ratio", "number"]


@dataclass(frozen=True)
class Metric:
    name: str
    unit: MetricUnit
    requires: frozenset[MeasureName]
    description: str


def _m(name: str, unit: MetricUnit, requires: set[MeasureName], description: str) -> Metric:
    return Metric(name, unit, frozenset(requires), description)


METRICS: dict[str, Metric] = {
    m.name: m
    for m in (
        _m("spend", "usd", {"spend"}, "Total amount spent."),
        _m("impressions", "count", {"impressions"}, "Times the ads were shown."),
        _m("clicks", "count", {"clicks"}, "All clicks."),
        _m("link_clicks", "count", {"link_clicks"}, "Clicks that left the platform."),
        _m("conversions", "count", {"conversions"}, "Conversions (leads in the demo data)."),
        _m("cpl", "usd", {"spend", "conversions"}, "Cost per conversion: spend / conversions."),
        _m("ctr", "ratio", {"clicks", "impressions"}, "Click-through rate: clicks / impressions."),
        _m("cpc", "usd", {"spend", "clicks"}, "Cost per click: spend / clicks."),
        _m("cpm", "usd", {"spend", "impressions"}, "Cost per 1,000 impressions."),
        _m(
            "cvr",
            "ratio",
            {"conversions", "clicks"},
            "Conversion rate: conversions per link click (per click if link clicks are unmapped).",
        ),
        _m(
            "frequency",
            "number",
            {"impressions", "reach"},
            "Average times each person saw the ads: impressions / reach.",
        ),
    )
}


_PLAIN_SUMS: dict[str, MeasureName] = {
    "spend": "spend",
    "impressions": "impressions",
    "clicks": "clicks",
    "link_clicks": "link_clicks",
    "conversions": "conversions",
}


def _sum(model: SemanticModel, measure: MeasureName) -> str:
    return f"SUM({measure_col(model, measure)})"


def _dsum(model: SemanticModel, measure: MeasureName) -> str:
    """A sum cast to DOUBLE, so integer division never truncates."""
    return f"CAST(SUM({measure_col(model, measure)}) AS DOUBLE)"


def _ratio(model: SemanticModel, num: MeasureName, den: MeasureName, scale: str = "") -> str:
    return f"{_dsum(model, num)}{scale} / NULLIF({_dsum(model, den)}, 0)"


def _cvr_denominator(model: SemanticModel) -> MeasureName:
    return "link_clicks" if "link_clicks" in model.fact.measures else "clicks"


def _requires(model: SemanticModel, metric: Metric) -> frozenset[MeasureName]:
    if metric.name == "cvr":
        return frozenset({"conversions", _cvr_denominator(model)})
    return metric.requires


def available_metrics(model: SemanticModel) -> list[Metric]:
    """Metrics whose required measures are all mapped in this model."""
    mapped = set(model.fact.measures)
    return [m for m in METRICS.values() if _requires(model, m) <= mapped]


def sql_expr(model: SemanticModel, metric: Metric | str) -> str:
    """The aggregate SQL expression for a metric, built only from the model's mapping.

    Reach is never summed on its own: it is not additive across days or entities.
    """
    m = METRICS[metric] if isinstance(metric, str) else metric
    missing = _requires(model, m) - set(model.fact.measures)
    if missing:
        raise ValueError(f"metric {m.name!r} needs unmapped measures: {sorted(missing)}")
    if m.name in _PLAIN_SUMS:
        return _sum(model, _PLAIN_SUMS[m.name])
    if m.name == "cpl":
        return _ratio(model, "spend", "conversions")
    if m.name == "ctr":
        return _ratio(model, "clicks", "impressions")
    if m.name == "cpc":
        return _ratio(model, "spend", "clicks")
    if m.name == "cpm":
        return _ratio(model, "spend", "impressions", scale=" * 1000.0")
    if m.name == "cvr":
        return _ratio(model, "conversions", _cvr_denominator(model))
    if m.name == "frequency":
        return _ratio(model, "impressions", "reach")
    raise ValueError(f"no SQL for metric {m.name!r}")
