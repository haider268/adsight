"""Every tunable threshold in one place.

The defaults are tuned on the demo data (about 50 leads a day across 54 ads over six
months). A dataset with very different volumes can override any of them in its semantic
YAML under `tuning:`; unknown keys are errors. With no overrides nothing changes.
"""

from pydantic import BaseModel, ConfigDict


class Tuning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- question linter (generate/lint.py) ---------------------------------------------
    min_conversions: int = 20  # conversions a scope needs for a conversion-based answer
    min_conversions_per_point: int = 10  # the same, per point of a series or comparison
    min_impressions: int = 1000  # impressions a scope needs for an impression-based metric
    min_clicks: int = 100  # clicks a scope needs for a click-based metric
    min_clicks_per_point: int = 50  # the same, per point of a series or comparison
    min_daily_conversions: float = 1.0  # average conversions per day for daily questions
    margin_additive: float = 0.03  # winner's lead over the runner-up, additive metrics
    margin_ratio: float = 0.05  # winner's lead over the runner-up, ratio metrics
    trend_min_change: float = 9.0  # % change from first to last that counts as a clear trend
    trend_max_flat: float = 2.0  # % change at or under which a trend is clearly flat
    min_coverage: float = 0.80  # share of a period's days that must have data
    max_week_share: float = 0.30  # most of a category's period questions that use weeks
    trend_flat_band_pct: float = 5.0  # % change under which a trend counts as flat

    # --- root-cause detectors (generate/causes.py) ----------------------------------------
    material_budget_change: float = 0.20  # |new/old - 1| at or above this is material
    effect_min_scope: float = 0.15  # min relative CPL change on the changed scope
    effect_min_parent: float = 0.10  # min relative CPL change to also ask at parent level
    spend_jump: float = 0.40  # min relative spend rise for a "spend jumped" angle
    decoy_window_days: int = 21  # how far before a cause a decoy change may be
    effect_before_days: int = 14  # days measured before a logged change
    effect_after_days: int = 14  # days measured after a logged change
    effect_after_days_audience: int = 7  # days measured after an audience change
    effect_min_conversions_before: int = 20  # conversions needed before a logged change
    effect_min_conversions_after: int = 10  # conversions needed after a logged change
    tracking_baseline_days: int = 14  # days of baseline before a tracking drop
    tracking_min_baseline: float = 8  # baseline conversions per day
    tracking_max_share: float = 0.15  # conversions at or below this share of baseline
    tracking_min_click_share: float = 0.75  # clicks at or above this share of baseline
    tracking_weekly_baseline_weeks: int = 4  # weeks of baseline for weekly data
    tracking_child_min_baseline: float = 3  # conversions per day, for the child angle
    fatigue_weeks: int = 6  # weeks of frequency and CTR examined
    fatigue_min_frequency: float = 2.0  # frequency the ad must reach
    fatigue_min_frequency_rise: float = 0.5  # rise in frequency over the window
    fatigue_max_ctr_share: float = 0.70  # final CTR at or below this share of the first
    noise_min_jump: float = 0.40  # relative conversion change a noise week must show
    noise_max_input_change: float = 0.07  # most spend and clicks may move in a noise week
    noise_min_baseline: float = 20  # conversions per week before a noise week
    noise_exclusion_days: int = 21  # days around any logged change a noise week avoids
    calendar_max_daily_change: float = 0.02  # most spend per day may move between months
    calendar_min_total_change: float = 0.05  # least the monthly total must move

    # --- agents (architectures/) ------------------------------------------------------
    b1_max_steps: int = 6  # B1 think steps; the last must answer
    max_plan_steps: int = 4  # SQL steps a B2-B4 plan may hold
    max_fixes: int = 3  # B3/B4 fix rounds
    result_rows: int = 25  # rows of each plan step shown to the model (B2-B4)
    b0_answer_rows: int = 25  # rows of B0's query shown to the model
    b1_scratchpad_rows: int = 15  # rows of each B1 step shown to the model


DEFAULT_TUNING = Tuning()
