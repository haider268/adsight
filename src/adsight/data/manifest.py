"""Ground-truth manifest: each incident with its effect MEASURED from the generated metrics."""

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adsight.data.catalog import (
    ADS_BY_ID,
    WINDOW_END,
    WINDOW_START,
    account_for,
    ad_set_for,
    campaign_for,
)
from adsight.data.generator import Dataset, MetricRow
from adsight.data.incidents import INCIDENTS, Incident


class Measurement(BaseModel):
    before_value: float | None
    after_value: float | None
    change_pct: float | None


def in_scope(row: MetricRow, scope_type: str, scope_id: str) -> bool:
    """Whether a metrics row belongs to an ad, to an ad set's ads, or to an account's ads."""
    ad = ADS_BY_ID[row.ad_id]
    if scope_type == "ad":
        return ad.ad_id == scope_id
    ad_set = ad_set_for(ad)
    if scope_type == "ad_set":
        return ad_set.ad_set_id == scope_id
    if scope_type == "account":
        return account_for(campaign_for(ad_set)).account_id == scope_id
    raise ValueError(f"unknown scope type: {scope_type!r}")


def window_rows(
    dataset: Dataset, scope_type: str, scope_id: str, window: tuple[date, date]
) -> list[MetricRow]:
    start, end = window
    return [
        m for m in dataset.metrics if start <= m.date <= end and in_scope(m, scope_type, scope_id)
    ]


def metric_value(metric: str, rows: Iterable[MetricRow]) -> float | None:
    """cpl = spend / leads, ctr = clicks / impressions, lead_rate = leads / link_clicks."""
    rows = list(rows)
    if metric == "cpl":
        num, den = sum(m.spend for m in rows), sum(m.leads for m in rows)
    elif metric == "ctr":
        num, den = sum(m.clicks for m in rows), sum(m.impressions for m in rows)
    elif metric == "lead_rate":
        num, den = sum(m.leads for m in rows), sum(m.link_clicks for m in rows)
    else:
        raise ValueError(f"unknown metric: {metric!r}")
    return num / den if den else None


def measure(incident: Incident, dataset: Dataset) -> Measurement:
    before = metric_value(
        incident.metric,
        window_rows(dataset, incident.scope_type, incident.scope_id, incident.before),
    )
    after = metric_value(
        incident.metric,
        window_rows(dataset, incident.scope_type, incident.scope_id, incident.after),
    )
    change = None
    if before is not None and after is not None and before != 0:
        change = round((after - before) / before * 100, 1)
    return Measurement(
        before_value=None if before is None else round(before, 4),
        after_value=None if after is None else round(after, 4),
        change_pct=change,
    )


def frequency_crossed_2_on(incident: Incident, dataset: Dataset) -> date | None:
    """First day on or after the incident start on which the ad's frequency exceeded 2.0."""
    days = sorted(
        m.date
        for m in dataset.metrics
        if m.ad_id == incident.ad_id and m.date >= incident.start_date and m.frequency > 2.0
    )
    return days[0] if days else None


def build_manifest(
    dataset: Dataset, seed: int, incidents: Iterable[Incident] = INCIDENTS
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for incident in incidents:
        entry = incident.model_dump(mode="json")
        measured: dict[str, Any] = measure(incident, dataset).model_dump()
        if incident.kind == "creative_fatigue":
            crossed = frequency_crossed_2_on(incident, dataset)
            measured["frequency_crossed_2_on"] = None if crossed is None else crossed.isoformat()
        entry["measured"] = measured
        entries.append(entry)
    return {
        "seed": seed,
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "incidents": entries,
    }


def write_manifest(manifest: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
