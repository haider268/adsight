"""Deterministic generator: catalog plus change events in, one row per ad per day out.

RNG rule: every ad-day draws from its own `random.Random(f"{seed}:{ad_id}:{day}")`, so
adding an event later changes only the ad-days that event actually affects.
"""

import math
import random
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from adsight.data.catalog import (
    ACCOUNTS,
    AD_SETS,
    ADS,
    CAMPAIGNS,
    CREATIVE_CTR,
    CREATIVE_SPEND_SHARE,
    DIMINISHING_EXPONENT,
    FATIGUE_K,
    FATIGUE_START,
    FREQ_CAP,
    LEARNING_DAYS,
    LINK_SHARE,
    OBJECTIVE_CVR,
    WINDOW_END,
    Account,
    Ad,
    AdSet,
    Campaign,
    ad_set_for,
    audience_freq_factor,
    campaign_for,
    vertical_of,
)
from adsight.data.events import ChangeEvent, baseline_events, budget_on, learning_start, sort_events

AccountRow = Account
CampaignRow = Campaign
AdSetRow = AdSet
AdRow = Ad


@dataclass(frozen=True)
class MetricRow:
    date: date
    ad_id: str
    impressions: int
    reach: int
    clicks: int
    link_clicks: int
    spend: float
    leads: int
    frequency: float


@dataclass
class Dataset:
    accounts: list[AccountRow] = field(default_factory=list)
    campaigns: list[CampaignRow] = field(default_factory=list)
    ad_sets: list[AdSetRow] = field(default_factory=list)
    ads: list[AdRow] = field(default_factory=list)
    metrics: list[MetricRow] = field(default_factory=list)
    events: list[ChangeEvent] = field(default_factory=list)


_POISSON_EXACT_MAX = 500.0


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def poisson(rng: random.Random, lam: float) -> int:
    """A Poisson draw: Knuth's method up to lam 500, a rounded normal above."""
    if lam <= 0:
        return 0
    if lam <= _POISSON_EXACT_MAX:
        limit = math.exp(-lam)
        k = 0
        p = 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                return k
            k += 1
    return max(0, round(rng.gauss(lam, math.sqrt(lam))))


def cpm_base(base_cpm: float, budget: float, initial_budget: float) -> float:
    """Expected CPM before noise: pushing budget past its initial level buys pricier reach."""
    return base_cpm * math.pow(budget / initial_budget, DIMINISHING_EXPONENT)


def _metric_row(seed: int, ad: Ad, day: date, events: list[ChangeEvent]) -> MetricRow:
    rng = random.Random(f"{seed}:{ad.ad_id}:{day.isoformat()}")
    ad_set = ad_set_for(ad)
    campaign = campaign_for(ad_set)
    vertical = vertical_of(ad)
    ct = ad.creative_type
    d = (day - ad.launched_date).days
    budget = budget_on(ad_set.ad_set_id, day, events)

    g_spend = clip(rng.gauss(1, 0.05), 0.85, 1.15)
    g_cpm = clip(rng.gauss(1, 0.07), 0.7, 1.3)
    g_freq = clip(rng.gauss(1, 0.03), 0.9, 1.1)
    g_ctr = clip(rng.gauss(1, 0.08), 0.6, 1.4)
    g_link = clip(rng.gauss(1, 0.05), 0.8, 1.2)
    learning_days = (day - learning_start(ad, day, events)).days
    in_learning = learning_days < LEARNING_DAYS
    noise_sd = 0.35 if in_learning else 0.10
    g_cvr = clip(rng.gauss(1, noise_sd), 0.2, 2.0)

    spend = round(budget * CREATIVE_SPEND_SHARE[ct] * g_spend, 2)
    cpm = cpm_base(vertical.base_cpm, budget, ad_set.daily_budget) * g_cpm
    impressions = round(spend / cpm * 1000)
    raw_freq = min(1.05 + 0.004 * d * audience_freq_factor(ad_set.audience), FREQ_CAP)
    frequency = max(1.0, round(raw_freq * g_freq, 3))
    reach = min(impressions, round(impressions / frequency))
    fatigue = math.exp(-FATIGUE_K * max(0.0, frequency - FATIGUE_START))
    ctr = vertical.base_ctr * CREATIVE_CTR[ct] * fatigue * g_ctr
    learn = 0.7 + 0.3 * learning_days / LEARNING_DAYS if in_learning else 1.0
    cvr = (
        vertical.base_cvr
        * OBJECTIVE_CVR[campaign.objective]
        * vertical.weekday_cvr[day.weekday()]
        * learn
        * g_cvr
    )

    clicks = poisson(rng, impressions * ctr)
    link_clicks = min(clicks, round(clicks * LINK_SHARE * g_link))
    leads = min(link_clicks, poisson(rng, link_clicks * cvr))

    return MetricRow(
        date=day,
        ad_id=ad.ad_id,
        impressions=impressions,
        reach=reach,
        clicks=clicks,
        link_clicks=link_clicks,
        spend=spend,
        leads=leads,
        frequency=frequency,
    )


def generate(seed: int, events: list[ChangeEvent] | None = None) -> Dataset:
    """Build the full dataset for `seed`, with state taken from `events` (baseline by default)."""
    history = sort_events(baseline_events() if events is None else list(events))
    metrics = [
        _metric_row(seed, ad, ad.launched_date + timedelta(days=offset), history)
        for ad in ADS
        for offset in range((WINDOW_END - ad.launched_date).days + 1)
    ]
    ad_sets = [
        replace(s, daily_budget=budget_on(s.ad_set_id, WINDOW_END, history)) for s in AD_SETS
    ]
    return Dataset(
        accounts=list(ACCOUNTS),
        campaigns=list(CAMPAIGNS),
        ad_sets=ad_sets,
        ads=list(ADS),
        metrics=metrics,
        events=history,
    )
