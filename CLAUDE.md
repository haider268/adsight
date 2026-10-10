# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English about ad
performance goes in; a DuckDB SQL query and its answer come out. The project benchmarks
agent architectures against the same question set and synthetic ad data. Status: data,
semantic layer, benchmark generator, LLM layer, agents B0 and B1, resumable runner.

## How to run

```
uv sync                       # create .venv and install everything
uv run adsight data           # data/adsight.duckdb + manifest.json (--shape flat-weekly
                              # writes data/flat/adsight_flat.duckdb, one Google-style table)
uv run adsight bench refs     # check expected answers against reference SQL (--db for any file)
uv run adsight bench generate # write benchmark/generated/adsight-demo/
uv run adsight bench review benchmark/generated/adsight-demo   # pending root causes
uv run adsight bench run --questions-dir benchmark/generated/adsight-demo --arch b0 --model groq:openai/gpt-oss-120b
uv run adsight bench status   # runs with done / retry / error counts and cost
uv run pytest                 # offline tests; /check also runs ruff and mypy src
```

## Layout

| Path | What it holds |
|---|---|
| `src/adsight/config.py`, `log.py`, `cli.py` | Settings (`ADSIGHT_*`, provider keys), JSON-lines logs, the Typer CLI. |
| `src/adsight/data/` | Catalog, events, incidents, `generate(seed)`, manifest, `build.py` (CSV + COPY load, flat weekly export). |
| `src/adsight/benchmark/` | `AgentAnswer`, `Question` + scoring types, `refs.py`, `score.py`. |
| `src/adsight/semantic/` | YAML model, SQL fragments, metric library, `inspect`, `check`. |
| `src/adsight/generate/` | Benchmark generator: periods, templates, linter, causes, `generate_benchmark`. |
| `src/adsight/sql_safety.py` | `validate_sql`, `run_sql`, `open_agent_connection`. |
| `src/adsight/llm/` | Types, portable schemas, cache, limits, pricing, budget, factory, `providers/`. |
| `src/adsight/agent/`, `architectures/` | Schema card, answer schemas, prompts; B0 and B1 graphs, `run_question`. |
| `src/adsight/bench_run.py` | The resumable runner (`runs.sqlite`, reports). |
| `semantic/demo.yaml`, `semantic/flat-demo.yaml` | Mappings drafted by `adsight inspect` for the two demo databases. |
| `benchmark/questions.jsonl`, `benchmark/reference_sql/` | The hand-written GOLD SET (100 questions). A test fixture only; never edit. |
| `benchmark/generated/adsight-demo/`, `adsight-flat-demo/` | Published benchmarks: questions, reference SQL, context, lint report. |

## Data contract

- Window 2026-01-01 to 2026-06-29 (180 days), USD. 3 accounts, 9 campaigns, 18 ad sets,
  54 ads. Full dataset 8,534 metric rows, 18 events; clean baseline 8,640 rows, 15 events.
- RNG: every ad-day draws from `random.Random(f"{seed}:{ad_id}:{day}")`. State comes from
  events: add an event, never edit the generator, to change history.
- Incidents (effects measured into `manifest.json`, never asserted by hand):
  I1 top_ad_paused AD019 03-16; I2 budget_doubled S13 04-20; I3 creative_fatigue AD001
  05-04 (hidden); I4 audience_edit S09 05-18..24; I5 tracking_outage A3 06-08..09 (hidden).
  Hidden incidents have no events_log row: inventing a change event is a failure.
- Flat weekly export: table `perf(week_start, campaign_id, campaign_name, ad_group, cost,
  impr, clicks, conversions)`, campaign x ad set x full Monday weeks (2026-01-05..06-28).
  Ad group names repeat across campaigns ("Video Viewers"), as Google allows.

## Benchmark contract

- Seed 7; today is 2026-06-30. 100 questions: 15 each lookup, aggregation, comparison,
  trend; 20 root_cause; 10 each ambiguous, unanswerable.
- `AgentAnswer`: kind (answer / clarify / cannot_answer), summary, values, entities,
  direction, assumptions, sql. Matching: within 1% or a unit floor, one-to-one; ratios
  also as percents. root_cause: required regexes on the summary (an LLM judge comes later).
- Expected values come only from reference SQL; never type them by hand.

## Semantic layer and generator

- A YAML mapping names the fact table, date and grain (day / week), canonical measures
  (spend, impressions, conversions required), levels top to bottom, attributes (a
  `time_varying` one is an end-of-data snapshot), and an optional change log.
- Metrics are defined once in `semantic/metrics.py`; generated questions and agents use it.
- `bench generate` reads any database through its model; same seed, same bytes. Linter
  rules R1-R8 (executes, volume, margin, definition, trend, distinct, coverage, duplicate).
  Shortfalls are recorded in `lint_report.json`; `--strict` exits 1 on one.
- Root causes start from a logged material change or a strict detector (tracking, fatigue,
  calendar, noise), never from a raw jump. Tracking, fatigue and noise questions start
  `review: pending`; `bench review --approve/--reject` records the human decision, and a
  regeneration keeps decisions for questions with the same template, params and text.
- Runs use the effective set: review None or approved.

## SQL safety

- `validate_sql(sql, allowed_tables)` parses with sqlglot (duckdb) and rejects: more than
  one statement, any non-SELECT root, any write / DDL / COPY / PRAGMA / SET / ATTACH / USE /
  transaction / SELECT INTO node, any table function in FROM, file and environment
  functions anywhere, unknown or qualified tables, unparsable SQL. It returns normalised SQL.
- `run_sql` validates, runs the normalised SQL with a row cap and a timeout
  (`con.interrupt()`), and never raises: errors come back in `SqlResult.error`.
- Agents only ever use `open_agent_connection` (read-only, no external access).

## LLM layer

- Agents call one interface: `LLMProvider.complete(LLMRequest, salt="") -> LLMResponse`.
  Every request asks for ONE JSON object matching a portable schema.
- Providers: `fixture` (ScriptedProvider, tests and demos), `groq` and `gemini` (free tiers,
  via LangChain), `anthropic` direct or batched. Specs look like `groq:openai/gpt-oss-120b`.
- `make_provider` always wraps: CachedProvider(LimitedProvider(inner)). The SQLite cache
  (`.adsight/llm_cache.sqlite`) means a re-run never pays twice; `--replay` never calls out.
- Limits: token buckets per provider (Groq 30 req/min, 8000 tok/min, 2 at once; Gemini 15,
  250000, 2). Retries: full jitter, base 2 s, cap 60 s, 5 attempts, retry-after honoured.
- Batching: with `--batch` all questions run at once, so each step's calls arrive together
  and flush as one Anthropic batch (lock-step waves). Batches are recorded before submit and
  re-attached on start-up, so a stopped run never pays twice.
- Budget guard: `ADSIGHT_ANTHROPIC_BUDGET_USD` (default 0 = no Anthropic call) is checked
  against the summed cost of stored Anthropic responses before every request.
- No live calls in tests, ever: vendor clients are replaced by small fake classes.

## Agents

- System prompt = role + schema card (tables, typed mapped columns, joins, attribute
  values, metric SQL, rules); identical for every call, so it caches. Per question text
  goes in the user message.
- B0: `plan` -> `finish_direct` (clarify / cannot_answer) or `execute` -> `answer`. Max 2 calls.
- B1: `think` -> `act` (run_sql) -> `think` ... -> final. Max 6 think calls; the 6th must
  answer, else "step limit reached".
- Invalid output is retried once with salt "retry1"; twice is error "invalid_output".

## Running the benchmark

- `adsight bench run --questions-dir <dir> --arch b0|b1 --model <spec> [--batch] [--limit N]
  [--run-id ID] [--replay] [--semantic <yaml>] [--db <path>]`. Results go to
  `.adsight/runs.sqlite`, the report to `.adsight/reports/<run_id>.json`.
- Re-running the same run id skips "done" questions. A daily limit or the budget stops the
  run cleanly: the rest are marked "retry" and the command exits 3 (run it again later).
- `adsight bench status [--run-id ID]` lists runs.

## Rules

- Git identity is `haider268 <haider268@users.noreply.github.com>` only, set with
  `git config --local`. No `Co-Authored-By` lines and no Claude/Anthropic trailers.
- No secrets in any file. Keys live only in the git-ignored `.env`; `.env.example` ships
  blank placeholders. No real people and no real ad account data.
- Tests are offline: no network, no sockets. Tests that need a key carry the `live`
  marker and are skipped by default.
