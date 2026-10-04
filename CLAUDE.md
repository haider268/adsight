# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English
about ad performance goes in; a DuckDB SQL query and its answer come out. The
project benchmarks five agent architectures against the same question set and
synthetic ad dataset. Status: synthetic dataset built; no agent or benchmark yet.

## How to run

```
uv sync                       # create .venv and install everything
uv run adsight version        # prints "adsight 0.1.0"
uv run adsight data           # build data/adsight.duckdb + data/manifest.json
uv run adsight bench refs     # check stored expected answers against reference SQL
uv run adsight bench refs --write   # (re)compute and store them
uv run adsight semantic check semantic/demo.yaml   # mapping fits the database
uv run pytest                 # offline test suite with coverage
uv run ruff check             # lint
uv run ruff format --check    # formatting
uv run mypy src               # strict type check
```

`/check` runs all four checks in order.

## Layout so far

| File | What it does |
|---|---|
| `src/adsight/config.py` | `Settings` from `ADSIGHT_*` env vars and optional `.env`: `data_dir`, `seed`, `log_level`, `db_path`. |
| `src/adsight/log.py` | JSON-lines logging. `configure_logging`, `reset_logging`, `get_logger`, `set_run_id`/`get_run_id`. Extra fields via `extra={"fields": {...}}`. |
| `src/adsight/cli.py` | Typer app, entry point `adsight`. Commands: `version`, `data`. |
| `src/adsight/data/schema.sql` | The six DuckDB tables, loaded via `importlib.resources`. |
| `src/adsight/data/catalog.py` | Fixed fictional catalog (accounts, campaigns, ad sets, ads), vertical parameters, model constants, lookup helpers. |
| `src/adsight/data/events.py` | `ChangeEvent`, `baseline_events()`, and state from events: `budget_on`, `audience_on`, `ad_active_on`, `learning_phase`/`learning_start`. |
| `src/adsight/data/incidents.py` | The five `INCIDENTS`, `incident_events()`, and hidden-effect helpers: `winner_multiplier`, `extra_frequency`, `lead_multiplier`. |
| `src/adsight/data/generator.py` | `generate(seed, events, incidents)` -> `Dataset`: one metrics row per active ad per day; `clip`, `poisson`, `cpm_base`. |
| `src/adsight/data/manifest.py` | `measure()` each incident from the metrics, `build_manifest()`, `write_manifest()`. |
| `src/adsight/benchmark/answer.py` | `AgentAnswer` and `NamedValue`: the contract every architecture returns. |
| `src/adsight/benchmark/questions.py` | `Question`, the seven `Scoring` types, `Expected`, `load_questions`/`save_questions`, benchmark constants. |
| `src/adsight/benchmark/refs.py` | `expected_from_rows` and `compute_expected`: reference SQL result -> `Expected`. |
| `src/adsight/benchmark/score.py` | `matches`, rule-based `score`, and `reference_answer` (the perfect answer). |
| `benchmark/questions.jsonl` | The questions, one JSON object per line. |
| `benchmark/reference_sql/` | One `<ID>.sql` per question that has a numeric, entity, numeric_set or trend answer. |
| `src/adsight/semantic/model.py` | `SemanticModel` (fact, measures, levels, attributes, change log); `load_model`/`dump_model`. |
| `src/adsight/semantic/sql.py` | `from_clause` (fact `f` + LEFT JOINs up the hierarchy), `level_key`, `level_label`, `attribute_col`, `date_col`. |
| `src/adsight/semantic/metrics.py` | The metric library: `METRICS`, `available_metrics`, `sql_expr`. |
| `src/adsight/semantic/inspect.py` | `draft_model`: propose a mapping for an unknown database, with notes. |
| `src/adsight/semantic/check.py` | `check_model`: tables, columns, types, orphan keys and date spacing. |
| `semantic/demo.yaml` | The mapping for the demo database, drafted by `adsight inspect`. |
| `src/adsight/data/build.py` | `build_database(dataset, path)` (fresh file, CSV + `COPY` load) and `open_readonly(path)`. |

## Data contract

- Window: 2026-01-01 to 2026-06-29 (180 days). All money is USD.
- 3 accounts, 9 campaigns, 18 ad sets, 54 ads (AD001..AD054).
- Full dataset: 8,534 metrics rows, 18 events. Clean baseline (`incidents=[]`): 8,640
  rows, 15 events (9 launches, 6 routine budget changes under 20%), byte-identical to M1a.
- RNG rule: every ad-day draws from its own `random.Random(f"{seed}:{ad_id}:{day}")`,
  so adding an event changes only the ad-days that event affects.
- State comes from events; add an event, never edit the generator, to change history.
- Learning profiles: an audience edit restarts learning harder than a launch or a
  budget change.
- `adsight data [--seed N] [--data-dir DIR]` writes `<data-dir>/adsight.duckdb` and `manifest.json`
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
- 100 questions: lookup 15, aggregation 15, comparison 15, trend 15, root_cause 20,
  ambiguous 10, unanswerable 10. The 60 SQL-scored questions have reference SQL.
- `AgentAnswer`: `kind` (answer / clarify / cannot_answer), `summary`, `values`
  (label + value), `entities` (best first), `direction` (up / down / flat), `assumptions`,
  `sql` (informational only).
- Matching: within 1% or a floor (usd 0.01, count 0.5, number 0.01, ratio 0.0001); a
  ratio may also be given as a percent. Matching is one-to-one: each answer value can
  satisfy only one expected value (maximum bipartite matching).
- numeric / numeric_set: kind answer, every expected value matched by an answer value.
- entity: kind answer, first entity equals the expected id or name (case-insensitive).
- trend: kind answer, direction right, and both first and last values matched.
- root_cause: kind answer and every required regex matches the summary. Ground truth is
  `incident_ids` (reference = their manifest causes joined) or a `reference_summary` for
  no-incident questions. Regexes cannot tell "it was X" from "it was not X", so invented
  causes are judged later by an LLM, not by regex.
- clarify: kind clarify, or kind answer with at least one stated assumption.
- refuse: kind cannot_answer with no values.
- Expected values come only from reference SQL via `adsight bench refs --write`; never
  type them by hand.

## Semantic layer

- A YAML mapping says what each column of ANY ad dataset means: the fact table and its
  date column and grain (day or week), the canonical measures, the entity levels top to
  bottom (each with its table, key, label and parent_key), group-by attributes, and an
  optional change log. A level whose table is the fact table is flat data, with no join.
- Canonical measures: spend, impressions and conversions are required; clicks,
  link_clicks and reach are optional. Reach is never summed on its own.
- Metrics: spend, impressions, clicks, link_clicks, conversions, cpl, ctr, cpc, cpm,
  cvr (over link clicks, or over clicks when link clicks are unmapped) and frequency.
  A metric is available only when every measure it needs is mapped.
- Commands: `adsight load <csv...> --db <path>` (one table per file; `--force` to
  overwrite), `adsight inspect --db <path> --out <yaml> [--name]` (draft + notes),
  `adsight semantic check <yaml> --db <path>` (exit 1 lists the problems).
- Metrics are defined once here; templates, reference SQL and agents read them, never raw
  column names.

## Rules

- Git identity is `haider268 <haider268@users.noreply.github.com>` only, set
  with `git config --local`. The machine's global identity must never appear.
- No `Co-Authored-By` lines and no Claude/Anthropic trailers in commits.
- No secrets in any file. Keys live only in the git-ignored `.env`;
  `.env.example` ships blank placeholders.
- No real people and no real ad account data. All data is synthetic.
- Tests are offline: no network, no sockets. Tests that need a key carry the
  `live` marker and are skipped by default.
