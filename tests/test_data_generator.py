import dataclasses
import hashlib
import random
import statistics
from collections import defaultdict
from datetime import date

import pytest

from adsight.data.catalog import (
    AD_SETS_BY_ID,
    ADS_BY_ID,
    CAMPAIGNS_BY_ID,
    FREQ_CAP,
    WINDOW_END,
    campaign_for,
)
from adsight.data.events import ChangeEvent, baseline_events, budget_on
from adsight.data.generator import Dataset, MetricRow, clip, cpm_base, generate, poisson

# sha256 of the seed-7 metrics as committed in M1a (98940d0), one repr(astuple(row)) per line.
M1A_METRICS_SHA256 = "c4925bc036d304553cc8f7ab47a416894ec6734e45df36c36fc8c45ef7b622fd"

# --- helpers ---------------------------------------------------------------


def test_clip_bounds() -> None:
    assert clip(0.5, 0.85, 1.15) == 0.85
    assert clip(2.0, 0.85, 1.15) == 1.15
    assert clip(1.0, 0.85, 1.15) == 1.0


def test_poisson_zero_for_non_positive_lambda() -> None:
    rng = random.Random(1)
    assert poisson(rng, 0) == 0
    assert poisson(rng, -3.5) == 0


def test_poisson_deterministic_for_seeded_rng() -> None:
    a = [poisson(random.Random("x"), 4) for _ in range(3)]
    b = [poisson(random.Random("x"), 4) for _ in range(3)]
    assert a == b
    rng1, rng2 = random.Random(5), random.Random(5)
    assert [poisson(rng1, 800) for _ in range(20)] == [poisson(rng2, 800) for _ in range(20)]


@pytest.mark.parametrize("lam", [4.0, 800.0])
def test_poisson_mean_within_3_percent(lam: float) -> None:
    rng = random.Random(42)
    mean = statistics.fmean(poisson(rng, lam) for _ in range(5000))
    assert abs(mean - lam) / lam < 0.03


def test_doubling_budget_raises_cpm_by_2_pow_0_3() -> None:
    base = cpm_base(14.0, 60.0, 60.0)
    assert base == pytest.approx(14.0)
    assert cpm_base(14.0, 120.0, 60.0) / base == pytest.approx(2**0.3)


# --- determinism and the RNG rule ------------------------------------------


def test_same_seed_same_metrics(baseline7: Dataset) -> None:
    assert generate(7, incidents=[]).metrics == baseline7.metrics


def test_baseline_matches_m1a_hash(baseline7: Dataset) -> None:
    """The clean baseline is byte-for-byte the M1a dataset; incidents must not leak into it."""
    text = "\n".join(repr(dataclasses.astuple(m)) for m in baseline7.metrics)
    assert hashlib.sha256(text.encode()).hexdigest() == M1A_METRICS_SHA256


def test_different_seed_differs(baseline7: Dataset) -> None:
    assert generate(8, incidents=[]).metrics != baseline7.metrics


def test_extra_event_changes_only_what_it_affects(baseline7: Dataset) -> None:
    change_day = date(2026, 6, 20)
    extra = ChangeEvent(
        date=change_day,
        entity_type="ad_set",
        entity_id="S07",
        change_type="budget_change",
        old_value="70.00",
        new_value="77.00",
    )
    changed = generate(7, [*baseline_events(), extra], incidents=[])
    assert len(changed.events) == 16
    before = {(m.date, m.ad_id): m for m in baseline7.metrics}
    after = {(m.date, m.ad_id): m for m in changed.metrics}
    assert before.keys() == after.keys()

    affected_differs = False
    for key, row in before.items():
        day, ad_id = key
        in_s07 = ADS_BY_ID[ad_id].ad_set_id == "S07"
        if day < change_day:
            assert after[key] == row, key
        if not in_s07:
            assert after[key] == row, key
        if in_s07 and day >= change_day and after[key] != row:
            affected_differs = True
    assert affected_differs, "the S07 budget change had no effect from 2026-06-20 onward"


# --- shape and integrity ---------------------------------------------------


def test_row_count(baseline7: Dataset) -> None:
    assert len(baseline7.metrics) == 8640


def test_rows_within_ad_lifetime(baseline7: Dataset) -> None:
    for m in baseline7.metrics:
        assert ADS_BY_ID[m.ad_id].launched_date <= m.date <= WINDOW_END


def test_late_campaign_first_dates(baseline7: Dataset) -> None:
    first: dict[str, date] = {}
    for m in baseline7.metrics:
        cid = campaign_for(AD_SETS_BY_ID[ADS_BY_ID[m.ad_id].ad_set_id]).campaign_id
        first[cid] = min(first.get(cid, m.date), m.date)
    assert first["C3"] == date(2026, 2, 1)
    assert first["C6"] == date(2026, 3, 1)
    assert first["C9"] == date(2026, 4, 1)


def test_row_integrity(baseline7: Dataset) -> None:
    for m in baseline7.metrics:
        assert m.reach <= m.impressions
        assert m.link_clicks <= m.clicks
        assert m.leads <= m.link_clicks
        assert m.spend >= 0
        assert 1.0 <= m.frequency <= FREQ_CAP * 1.1


def test_pacing_within_15_percent_of_budget(baseline7: Dataset) -> None:
    events = baseline_events()
    spend: dict[tuple[str, date], float] = defaultdict(float)
    for m in baseline7.metrics:
        spend[(ADS_BY_ID[m.ad_id].ad_set_id, m.date)] += m.spend
    for (ad_set_id, day), total in spend.items():
        budget = budget_on(ad_set_id, day, events)
        assert abs(total - budget) <= 0.15 * budget + 0.02, (ad_set_id, day, total, budget)


# --- patterns --------------------------------------------------------------


def _ad_set(m: MetricRow) -> str:
    return ADS_BY_ID[m.ad_id].ad_set_id


def _account(m: MetricRow) -> str:
    return CAMPAIGNS_BY_ID[AD_SETS_BY_ID[_ad_set(m)].campaign_id].account_id


def _age(m: MetricRow) -> int:
    return (m.date - ADS_BY_ID[m.ad_id].launched_date).days


def test_weekend_cvr_lower_for_a1(baseline7: Dataset) -> None:
    a1 = [m for m in baseline7.metrics if _account(m) == "A1"]
    weekend = [m for m in a1 if m.date.weekday() >= 5]
    weekday = [m for m in a1 if m.date.weekday() < 5]
    rate_weekend = sum(m.leads for m in weekend) / sum(m.link_clicks for m in weekend)
    rate_weekday = sum(m.leads for m in weekday) / sum(m.link_clicks for m in weekday)
    assert rate_weekend < rate_weekday


def test_retargeting_ctr_fatigues(baseline7: Dataset) -> None:
    retargeting = [
        m
        for m in baseline7.metrics
        if AD_SETS_BY_ID[_ad_set(m)].audience.startswith("retargeting_")
    ]
    early = [m for m in retargeting if 8 <= _age(m) <= 37]
    late = [m for m in retargeting if 120 <= _age(m) <= 149]
    ctr_early = sum(m.clicks for m in early) / sum(m.impressions for m in early)
    ctr_late = sum(m.clicks for m in late) / sum(m.impressions for m in late)
    assert ctr_late < ctr_early


def test_learning_phase_is_noisier(baseline7: Dataset) -> None:
    def rates(lo: int, hi: int) -> list[float]:
        return [
            m.leads / m.link_clicks
            for m in baseline7.metrics
            if lo <= _age(m) <= hi and m.link_clicks > 0
        ]

    assert statistics.stdev(rates(0, 6)) > statistics.stdev(rates(30, 36))
