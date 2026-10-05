"""How levels, metrics and entities are named in generated question text."""

from adsight.generate.context import DatasetContext
from adsight.semantic.model import SemanticModel

LEVEL_DISPLAY = {"account": "account", "campaign": "campaign", "ad_set": "ad set", "ad": "ad"}
ADDITIVE_METRICS = ("spend", "impressions", "clicks", "link_clicks", "conversions")


def level_display(level: str, plural: bool = False) -> str:
    text = LEVEL_DISPLAY.get(level, level.replace("_", " "))
    return text + "s" if plural else text


def is_additive(metric: str) -> bool:
    return metric in ADDITIVE_METRICS


def metric_display(metric: str, model: SemanticModel, ctx: DatasetContext) -> str:
    plural = ctx.conversion_plural
    if metric == "cvr":
        per = "link click" if "link_clicks" in model.fact.measures else "click"
        return f"conversion rate ({plural} per {per})"
    fixed = {
        "spend": "spend",
        "impressions": "impressions",
        "clicks": "clicks",
        "link_clicks": "link clicks",
        "conversions": plural,
        "cpl": f"cost per {ctx.conversion_name}",
        "ctr": "click-through rate (clicks divided by impressions)",
        "cpc": "cost per click",
        "cpm": "cost per thousand impressions",
        "frequency": "frequency (impressions divided by reach)",
    }
    return fixed[metric]


def entity_text(level: str, label: str) -> str:
    """'the campaign "Knee Pain PRP"'; pass the key value when the level has no label."""
    return f'the {level_display(level)} "{label}"'


def attribute_display(name: str) -> str:
    return name.replace("_", " ")


COUNT_METRICS = ("impressions", "clicks", "link_clicks", "conversions")


def total_phrase(metric: str, model: SemanticModel, ctx: DatasetContext) -> str:
    """'total spend' or 'total number of leads', for lookups of additive metrics."""
    if metric in COUNT_METRICS:
        return f"total number of {metric_display(metric, model, ctx)}"
    return f"total {metric_display(metric, model, ctx)}"


def daily_phrase(metric: str, model: SemanticModel, ctx: DatasetContext) -> str:
    """'average daily spend' or 'average number of leads per day'."""
    if metric in COUNT_METRICS:
        return f"average number of {metric_display(metric, model, ctx)} per day"
    return f"average daily {metric_display(metric, model, ctx)}"


def rank_phrase(metric: str, direction: str, model: SemanticModel, ctx: DatasetContext) -> str:
    """'the most leads', 'the highest spend' or 'the lowest cost per lead'."""
    if metric in COUNT_METRICS:
        word = "most" if direction == "highest" else "fewest"
        return f"the {word} {metric_display(metric, model, ctx)}"
    return f"the {direction} {metric_display(metric, model, ctx)}"
