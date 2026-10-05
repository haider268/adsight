# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English
about ad performance goes in; a DuckDB SQL query and its answer come out. The
project benchmarks five agent architectures against the same question set and
synthetic ad dataset. Status: dataset, semantic layer and benchmark generator built;
no agent yet.

## How to run

```
uv sync                       # create .venv and install everything
uv run adsight data           # build data/adsight.duckdb + data/manifest.json
uv run adsight bench refs     # check the gold set's expected answers against its SQL
uv run adsight bench generate # write benchmark/generated/adsight-demo/
uv run adsight bench review benchmark/generated/adsight-demo   # pending root causes
uv run adsight semantic check semantic/demo.yaml   # mapping fits the database
uv run pytest                 # offline tests; /check also runs ruff check,
                              # ruff format --check and mypy src
```

## Layout

| Path | What it holds |
|---|---|
| `src/adsight/config.py`, `log.py`, `cli.py` | Settings (`ADSIGHT_*`), JSON-lines logging, the Typer CLI: `version`, `data`, `load`, `inspect`, `semantic check`, `bench refs`, `bench generate`. |
| `src/adsight/data/` | `schema.sql`, `catalog.py` (fictional entities, constants), `events.py` (state from events), `incidents.py`, `generator.py` (`generate(seed, events, incidents)`), `manifest.py`, `build.py` (CSV + `COPY` load, `open_readonly`). |
| `src/adsight/benchmark/` | `answer.py` (`AgentAnswer`), `questions.py` (`Question`, scoring types, JSONL), `refs.py` (reference SQL -> `Expected`), `score.py` (`matches`, `score`, `reference_answer`). |
| `src/adsight/semantic/` | `model.py` (the YAML mapping), `sql.py` (FROM/JOIN chains, column refs), `metrics.py` (the metric library), `inspect.py` (`draft_model`), `check.py` (`check_model`). |
| `src/adsight/generate/` | `context.py`, `periods.py`, `wording.py`, `templates.py`, `lint.py`, `generator.py` (`generate_benchmark`). |
| `semantic/demo.yaml` | The mapping for the demo database, drafted by `adsight inspect`. |
| `benchmark/questions.jsonl`, `benchmark/reference_sql/` | The hand-written GOLD SET (100 questions, 60 SQL files). A test fixture only. |
| `benchmark/generated/adsight-demo/` | The published benchmark: `questions.jsonl`, `reference_sql/`, `context.json`, `lint_report.json`. |

## Data contract

- Window: 2026-01-01 to 2026-06-29 (180 days). All money is USD.
- 3 accounts, 9 campaigns, 18 ad sets, 54 ads (AD001..AD054).
- Full dataset: 8,534 metrics rows, 18 events. Clean baseline (`incidents=[]`): 8,640
  rows, 15 events (9 launches, 6 routine budget changes under 20%), byte-identical to M1a.
- RNG rule: every ad-day draws from its own `random.Random(f"{seed}:{ad_id}:{day}")`,
  so adding an event changes only the ad-days that event affects.
- State comes from events; add an event, never edit the generator, to change history.
- Learning profiles: an audience edit restarts learning harder than a launch or budget change.
- `adsight data [--seed N] [--data-dir DIR]` writes `adsight.duckdb` and `manifest.json`
  (defaults: `ADSIGHT_SEED` or 7, `ADSIGHT_DATA_DIR` or `data`). `data/` is git-ignored.

| Incident | Kind | Scope | Date | In events_log |
|---|---|---|---|---|
| I1 | top_ad_paused | ad AD019 (S07) | 2026-03-16 | yes |
| I2 | budget_doubled | ad set S13 | 2026-04-20 | yes |
| I3 | creative_fatigue | ad AD001 | 2026-05-04 | no |
| I4 | audience_edit | ad set S09 | 2026-05-18 to 05-24 | yes |
| I5 | tracking_outage | account A3 | 2026-06-08 to 06-09 | no |

Hidden incidents (I3, I5) have no events_log row on purpose: a correct diagnosis must
come from the metrics, and inventing a change event is a failure. Each incident's effect
is measured from the generated data into `manifest.json`, never asserted by hand.

## Benchmark contract

- Defined on seed 7. `BENCHMARK_TODAY` is 2026-06-30; data covers 2026-01-01 to 2026-06-29.
- Gold set: 100 questions (15 each of lookup, aggregation, comparison, trend; 20
  root_cause; 10 each of ambiguous, unanswerable).
- `AgentAnswer`: `kind` (answer / clarify / cannot_answer), `summary`, `values`, `entities`
  (best first), `direction` (up / down / flat), `assumptions`, `sql` (informational).
- Matching: within 1% or a floor (usd 0.01, count 0.5, number 0.01, ratio 0.0001), ratios
  also as percents, one-to-one. numeric / numeric_set: every expected value matched.
  entity: first entity is the expected id or name. trend: right direction, first and last
  matched. clarify: kind clarify, or an answer with a stated assumption. refuse: kind
  cannot_answer with no values.
- root_cause: every required regex matches the summary. Ground truth is `incident_ids`
  (manifest causes joined) or a `reference_summary`. Invented causes are judged later by
  an LLM, not by regex.
- Expected values come only from reference SQL (`bench refs --write` or the generator);
  never type them by hand.

## Semantic layer

- A YAML mapping says what each column of ANY ad dataset means: the fact table, its date
  column and grain (day or week), the canonical measures, the entity levels top to bottom
  (table, key, label, parent_key), group-by attributes, and an optional change log. A
  level whose table is the fact table is flat data, with no join. A `time_varying`
  attribute (a state column, or one whose values appear in the change log) is an
  end-of-data snapshot and never groups past periods.
- Canonical measures: spend, impressions and conversions are required; clicks,
  link_clicks and reach are optional. Reach is never summed on its own.
- Metrics: spend, impressions, clicks, link_clicks, conversions, cpl, ctr, cpc, cpm, cvr
  (over link clicks, or clicks when unmapped) and frequency; available only when mapped.
- Commands: `adsight load <csv...> --db <path> [--force]`, `adsight inspect --db <path>
  --out <yaml> [--name]`, `adsight semantic check <yaml> --db <path>`.
- Metrics are defined once here. Generated questions and the agents use this library,
  never raw column names. The gold set's hand-written SQL deliberately does not, so it
  checks the library independently.

## Benchmark generator

- `adsight bench generate --semantic <yaml> --db <path> --out <dir> --seed 7 [--strict]`
  reads any database through its semantic model and writes questions, reference SQL,
  expected answers, `context.json` and `lint_report.json`. Same seed, same bytes.
- Targets: lookup 15, aggregation 15, comparison 15, trend 15 (5 up, 5 down, 5 flat),
  root_cause 20, ambiguous 10, unanswerable 10 (100 questions).
- Templates: lookup L_TOTAL, L_RATIO, L_ALL, L_DAILY; aggregation A_RANK, A_ATTR,
  A_MONTH, A_WEEKDAY; comparison C_LEVEL, C_ATTR, C_MONTHS; trend T_WEEKLY, T_MONTHLY.
  Ambiguous phrasings and unanswerable probes are offered only when the data allows.
- Per template: shuffle slots by seed (period slots interleaved by kind: month, quarter,
  last30, whole, week), lint at most 60; select round-robin, no template above 40% of its
  category unless the others ran out, week periods at most 30% of a category's
  period-based questions. `--strict` exits 1 on a shortfall.
- Linter, first failure rejects:
  - R1_EXECUTES: SQL runs, returns rows, no NULLs.
  - R2_VOLUME: >= 20 conversions for cpl/cvr/conversions (10 per point or member),
    >= 100 clicks for cpc/clicks/link clicks (50 per point or member), >= 1000
    impressions, spend > 0, and >= 1 conversion a day for daily averages.
  - R3_MARGIN: winner beats runner-up by >= 3% (additive) or 5% (ratio).
  - R4_DEFINITION: ctr/cvr winner unchanged under the alternative click definition.
  - R5_TREND: |change| >= 9% or <= 2%.
  - R6_DISTINCT: no two comparison values match each other.
  - R7_COVERAGE: every member and point present, each with data on >= 80% of the dates.
  - R8_DUPLICATE: no repeated (template, params).
- The gold set (benchmark/questions.jsonl) is a test fixture, never the published
  benchmark.

## Root-cause generation

- Event first: weekly cost per lead is so noisy that 38 ad-set week-on-week jumps above
  30% exist on the demo and only 3 come from real changes. So questions never start from
  a jump; they start from a logged material change with a measured effect, or from a
  strict detector. Routine changes are never causes, only decoys.
- Material: paused, audience_edit, any other change except launched, or a budget change
  of |new/old - 1| >= 20% with a CPL effect of >= 15% on its scope.
- Detectors: tracking (account conversions fall to <= 15% of baseline while clicks hold,
  2+ days); fatigue (frequency >= 2 and up 0.5+ while CTR falls to <= 70%, no logged
  change); calendar (monthly spend differs only by month length); noise (conversions
  move 40%+ while spend and clicks stay within 7%, nothing else nearby).
- Tracking, fatigue and noise questions are written with `review: pending`; `adsight
  bench review <dir> [--approve ID ...] [--reject ID ...]` lists or decides them.
- The published benchmark is the effective set (review None or approved).

## Rules

- Git identity is `haider268 <haider268@users.noreply.github.com>` only, set
  with `git config --local`. The machine's global identity must never appear.
- No `Co-Authored-By` lines and no Claude/Anthropic trailers in commits.
- No secrets in any file. Keys live only in the git-ignored `.env`;
  `.env.example` ships blank placeholders.
- No real people and no real ad account data. All data is synthetic.
- Tests are offline: no network, no sockets. Tests that need a key carry the
  `live` marker and are skipped by default.
