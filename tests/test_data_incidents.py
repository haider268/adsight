import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from adsight.data.catalog import AD_SETS_BY_ID, ADS_BY_ID, CAMPAIGNS_BY_ID
from adsight.data.generator import Dataset, MetricRow, generate
from adsight.data.incidents import (
    INCIDENTS,
    extra_frequency,
    incident_events,
    lead_multiplier,
    winner_multiplier,
)
from adsight.data.manifest import (
    build_manifest,
    metric_value,
    window_rows,
    write_manifest,
)


def _ad_set(m: MetricRow) -> str:
    return ADS_BY_ID[m.ad_id].ad_set_id


def _account(m: MetricRow) -> str:
    return CAMPAIGNS_BY_ID[AD_SETS_BY_ID[_ad_set(m)].campaign_id].account_id


@pytest.fixture(scope="module")
def manifest7(dataset7: Dataset) -> dict[str, Any]:
    return build_manifest(dataset7, 7)


def _measured(manifest: dict[str, Any], incident_id: str) -> dict[str, Any]:
    entry = next(i for i in manifest["incidents"] if i["incident_id"] == incident_id)
    measured: dict[str, Any] = entry["measured"]
    return measured


# --- catalog of incidents and their helpers --------------------------------


def test_five_incidents_in_order() -> None:
    assert [i.incident_id for i in INCIDENTS] == ["I1", "I2", "I3", "I4", "I5"]
    assert [i.in_events_log for i in INCIDENTS] == [True, True, False, True, False]


def test_incident_events_only_for_visible_incidents() -> None:
    events = incident_events(INCIDENTS)
    assert [(e.date.isoformat(), e.entity_id, e.change_type) for e in events] == [
        ("2026-03-16", "AD019", "paused"),
        ("2026-04-20", "S13", "budget_change"),
        ("2026-05-18", "S09", "audience_edit"),
    ]
    assert incident_events([]) == []


def test_hidden_effect_helpers() -> None:
    assert winner_multiplier("AD019", INCIDENTS) == 1.65
    assert winner_multiplier("AD020", INCIDENTS) == 1.0
    assert extra_frequency("AD001", date(2026, 5, 3), INCIDENTS) == (0.0, None)
    extra, cap = extra_frequency("AD001", date(2026, 5, 14), INCIDENTS)
    assert extra == pytest.approx(0.4)
    assert cap == 4.5
    assert extra_frequency("AD002", date(2026, 6, 1), INCIDENTS) == (0.0, None)
    assert lead_multiplier("A3", date(2026, 6, 7), INCIDENTS) == 1.0
    assert lead_multiplier("A3", date(2026, 6, 8), INCIDENTS) == 0.05
    assert lead_multiplier("A3", date(2026, 6, 9), INCIDENTS) == 0.05
    assert lead_multiplier("A3", date(2026, 6, 10), INCIDENTS) == 1.0
    assert lead_multiplier("A2", date(2026, 6, 8), INCIDENTS) == 1.0


# --- the full dataset ------------------------------------------------------


def test_full_dataset_shape(dataset7: Dataset, baseline7: Dataset) -> None:
    assert len(dataset7.metrics) == 8534
    assert len(dataset7.events) == 18
    assert len(baseline7.metrics) == 8640
    assert len(baseline7.events) == 15
    ad019 = sorted(m.date for m in dataset7.metrics if m.ad_id == "AD019")
    assert ad019[0] == date(2026, 1, 1)
    assert ad019[-1] == date(2026, 3, 15)
    assert len(ad019) == 74


def test_full_dataset_end_state(dataset7: Dataset) -> None:
    assert [a.ad_id for a in dataset7.ads if a.status == "paused"] == ["AD019"]
    ad_sets = {s.ad_set_id: s for s in dataset7.ad_sets}
    assert ad_sets["S09"].audience == "lookalike_3pct"
    assert ad_sets["S13"].daily_budget == 110.0


# --- isolation: incidents touch only their own zones -----------------------


def _affected(day: date, ad_id: str) -> bool:
    ad_set_id = ADS_BY_ID[ad_id].ad_set_id
    account_id = CAMPAIGNS_BY_ID[AD_SETS_BY_ID[ad_set_id].campaign_id].account_id
    return (
        ad_id == "AD019"
        or (ad_set_id == "S07" and day >= date(2026, 3, 16))
        or (ad_set_id == "S13" and day >= date(2026, 4, 20))
        or (ad_id == "AD001" and day >= date(2026, 5, 4))
        or (ad_set_id == "S09" and day >= date(2026, 5, 18))
        or (account_id == "A3" and day in (date(2026, 6, 8), date(2026, 6, 9)))
    )


def test_rows_outside_affected_zones_match_baseline(dataset7: Dataset, baseline7: Dataset) -> None:
    base = {(m.date, m.ad_id): m for m in baseline7.metrics}
    checked = 0
    for row in dataset7.metrics:
        if not _affected(row.date, row.ad_id):
            assert row == base[(row.date, row.ad_id)], (row.date, row.ad_id)
            checked += 1
    assert checked > 7000


def test_ad019_winner_multiplier_touches_only_leads(dataset7: Dataset, baseline7: Dataset) -> None:
    base = {(m.date, m.ad_id): m for m in baseline7.metrics}
    pre = [m for m in dataset7.metrics if m.ad_id == "AD019"]
    assert pre and all(m.date < date(2026, 3, 16) for m in pre)
    assert any(m != base[(m.date, m.ad_id)] for m in pre)
    for m in pre:
        b = base[(m.date, m.ad_id)]
        assert dataclasses.replace(m, leads=b.leads) == b, m.date


def test_ad019_siblings_unchanged_before_pause(dataset7: Dataset, baseline7: Dataset) -> None:
    base = {(m.date, m.ad_id): m for m in baseline7.metrics}
    siblings = [
        m for m in dataset7.metrics if m.ad_id in ("AD020", "AD021") and m.date < date(2026, 3, 16)
    ]
    assert len(siblings) == 2 * 74
    for m in siblings:
        assert m == base[(m.date, m.ad_id)]


# --- each incident's measured effect on seed 7 -----------------------------


def test_i1_top_ad_paused_raises_cpl(manifest7: dict[str, Any]) -> None:
    assert _measured(manifest7, "I1")["change_pct"] >= 25


def test_i2_budget_doubled(manifest7: dict[str, Any], dataset7: Dataset) -> None:
    assert _measured(manifest7, "I2")["change_pct"] >= 10
    i2 = INCIDENTS[1]
    before = sum(m.spend for m in window_rows(dataset7, "ad_set", "S13", i2.before))
    after = sum(m.spend for m in window_rows(dataset7, "ad_set", "S13", i2.after))
    assert 1.8 <= after / before <= 2.2


def test_i3_creative_fatigue(manifest7: dict[str, Any]) -> None:
    measured = _measured(manifest7, "I3")
    assert measured["change_pct"] <= -35
    crossed = date.fromisoformat(measured["frequency_crossed_2_on"])
    assert date(2026, 5, 15) <= crossed <= date(2026, 6, 5)


def test_i4_audience_edit(manifest7: dict[str, Any]) -> None:
    assert _measured(manifest7, "I4")["change_pct"] >= 30


def test_i5_tracking_outage(manifest7: dict[str, Any], dataset7: Dataset) -> None:
    measured = _measured(manifest7, "I5")
    assert measured["after_value"] <= 0.15 * measured["before_value"]
    i5 = INCIDENTS[4]

    def mean_daily_link_clicks(window: tuple[date, date]) -> float:
        rows = window_rows(dataset7, "account", "A3", window)
        days = (window[1] - window[0]).days + 1
        return sum(m.link_clicks for m in rows) / days

    before, after = mean_daily_link_clicks(i5.before), mean_daily_link_clicks(i5.after)
    assert abs(after - before) / before <= 0.25


def test_metric_value_handles_empty_denominator() -> None:
    assert metric_value("cpl", []) is None
    with pytest.raises(ValueError, match="unknown metric"):
        metric_value("roas", [])


# --- manifest --------------------------------------------------------------


def test_manifest_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    first = build_manifest(generate(7), 7)
    assert first == build_manifest(generate(7), 7)
    path = tmp_path / "out" / "manifest.json"
    write_manifest(first, path)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert json.loads(text) == first


def test_manifest_shape(manifest7: dict[str, Any]) -> None:
    assert manifest7["seed"] == 7
    assert manifest7["window"] == {"start": "2026-01-01", "end": "2026-06-29"}
    assert [i["incident_id"] for i in manifest7["incidents"]] == ["I1", "I2", "I3", "I4", "I5"]
    i3 = manifest7["incidents"][2]
    assert i3["start_date"] == "2026-05-04"
    assert i3["before"] == ["2026-04-20", "2026-05-03"]
    assert "frequency_crossed_2_on" in i3["measured"]
    assert "frequency_crossed_2_on" not in manifest7["incidents"][0]["measured"]
