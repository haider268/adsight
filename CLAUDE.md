# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English about ad
performance goes in; a DuckDB SQL query and its answer come out. The project benchmarks
agent architectures against the same question set and synthetic ad data. Status: data,
semantic layer, benchmark generator, LLM layer, agents B0-B4, LLM judge, run plans.

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
uv run adsight bench resume --plan benchmark/plans/haiku-pilot.yaml   # one tick of a plan
uv run adsight bench export --plan <yaml> --out benchmark/results/<plan>/
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
| `src/adsight/agent/` | Schema card, answer schemas, prompts, result checks, diagnose tool, number check. |
| `src/adsight/architectures/` | B0-B4 graphs and `run_question`. |
| `src/adsight/bench_run.py`, `judge.py`, `plans.py` | Resumable runner, root-cause judge, run plans / ticks / export. |
| `benchmark/plans/`, `.github/workflows/bench-tick.yml` | Run plans and the scheduled tick workflow. |
| `semantic/demo.yaml`, `semantic/flat-demo.yaml` | Mappings drafted by `adsight inspect` for the two demo databases. |
| `benchmark/questions.jsonl`, `benchmark/reference_sql/` | The hand-written GOLD SET (100 questions). A test fixture only; never edit. |
| `benchmark/generated/adsight-demo/`, `adsight-flat-demo/` | Published benchmarks: questions, reference SQL, context, lint report. |

## Data contract

- Window 2026-01-01 to 2026-06-29, USD. 3 accounts, 9 campaigns, 18 ad sets, 54 ads; 8,534
  metric rows, 18 events. RNG per ad-day: `random.Random(f"{seed}:{ad_id}:{day}")`; state
  comes from events (add an event, never edit the generator).
- Incidents (effects measured into `manifest.json`, never asserted by hand):
  I1 top_ad_paused AD019 03-16; I2 budget_doubled S13 04-20; I3 creative_fatigue AD001
  05-04 (hidden); I4 audience_edit S09 05-18..24; I5 tracking_outage A3 06-08..09 (hidden).
  Hidden incidents have no events_log row: inventing a change event is a failure.
- Flat weekly export: one table `perf` (campaign x ad group x full Monday week); ad group
  names repeat across campaigns ("Video Viewers"), as Google allows.

## Benchmark contract

- Seed 7; today is 2026-06-30. 100 questions: 15 each lookup, aggregation, comparison,
  trend; 20 root_cause; 10 each ambiguous, unanswerable.
- `AgentAnswer`: kind (answer / clarify / cannot_answer), summary, values, entities,
  direction, assumptions, sql. Matching: within 1% or a unit floor, one-to-one; ratios
  also as percents. root_cause: required regexes on the summary, plus the judge (below).
- Expected values come only from reference SQL; never type them by hand.

## Semantic layer and generator

- A YAML mapping names the fact table, date and grain, canonical measures, levels top to
  bottom, attributes (`time_varying` = end-of-data snapshot) and an optional change log.
  Metrics are defined once in `semantic/metrics.py`.
- `bench generate` reads any database through its model; same seed, same bytes. Linter
  rules R1-R8; shortfalls go to `lint_report.json` (`--strict` exits 1).
- Root causes start from a logged material change or a strict detector, never a raw jump.
  Detector questions start `review: pending`; `bench review` records the human decision,
  which survives regeneration. Runs use review None or approved.

## SQL safety

- `validate_sql` (sqlglot, duckdb) allows one SELECT / set operation / WITH over known
  tables; it rejects writes, DDL, COPY, PRAGMA, SET, ATTACH, USE, transactions, SELECT
  INTO, table functions, file and environment functions, and unparsable SQL.
- `run_sql` runs the normalised SQL with a row cap and a timeout and never raises (errors
  come back in `SqlResult.error`), on `open_agent_connection` (read-only, no file access).

## LLM layer

- One interface: `LLMProvider.complete(LLMRequest, salt="")`, ONE JSON object per call.
- Providers: `fixture` (ScriptedProvider, tests and demos), `groq` and `gemini` (free tiers,
  via LangChain), `anthropic` direct or batched. Specs look like `groq:openai/gpt-oss-120b`.
- `make_provider` always wraps: CachedProvider(LimitedProvider(inner)). The SQLite cache
  (`.adsight/llm_cache.sqlite`) means a re-run never pays twice; `--replay` never calls out.
- Limits: token buckets per provider (Groq 30 req/min, 8000 tok/min; Gemini 15, 250000).
  Retries: full jitter, base 2 s, cap 60 s, 5 attempts.
- Batching: with `--batch` all questions run at once, so each step's calls flush as one
  Anthropic batch (lock-step waves). Batches are recorded before submit and re-attached on
  start-up, so a stopped run never pays twice.
- Budget guard: `ADSIGHT_ANTHROPIC_BUDGET_USD` (default 0 = no Anthropic call) is checked
  against the summed cost of stored Anthropic responses before every request.
- No live calls in tests, ever: vendor clients are replaced by small fake classes.

## Agents

- System prompt = role + schema card (typed columns, joins, entity counts and first 5
  names, attribute values, metric SQL, rules); the same for every call, so it caches.
- B0 (max 2 calls): `plan` -> `finish_direct` | `execute` -> `answer`.
- B1 (max 6): `think` -> `act` -> `think` ... -> final; the 6th think must answer.
- B2 (max 2): `plan` (1-4 SQL steps) -> `finish_direct` | `execute` -> `check` -> `answer`.
- B3 (max 5): B2 plus `check` -> `fix` -> `execute` -> `check`, at most 3 fixes.
- B4 (max 7): `classify` -> `finish_classified` | B3's loop -> `diagnose` (why questions:
  change log, before/after metrics, daily series, weekly frequency; never imports
  `adsight.generate`) -> `synthesize` -> `verify_numbers` -> one `resynthesize` if a
  number is untraceable (RunResult.untraceable_numbers).
- Invalid output is retried once with salt "<run salt>:retry1"; twice is "invalid_output".

## Running the benchmark

- `adsight bench run --questions-dir <dir> --arch b0..b4 --model <spec> [--batch]
  [--judge <spec>] [--salt S] [--sample N | --limit N] [--wait-minutes M] [--run-id ID]
  [--replay [--cassette <file>]] [--semantic <yaml>] [--db <path>]`. Results go to
  `.adsight/runs.sqlite`, reports to `.adsight/reports/<run_id>.json`.
- Re-running a run id skips "done" questions. Exit 0 done; 3 budget or daily limit; 4 a
  batch outlasted `--wait-minutes` (it stays recorded; the next run re-attaches it and
  replays every finished call from the cache for free). Run it again to continue.
- `--salt` makes repeat runs independent samples; `--sample N` spreads N questions over
  the categories (seeded).
- Judge: with `--judge <spec>` a root-cause answer passes only if the rules pass AND the
  judge says it names the true cause (the reference_summary, or the manifest causes for
  gold questions) and invents none. `results.judge_passed`, `judge_json`; default "off".

## Plans, ticks and export

- `benchmark/plans/<name>.yaml`: name, budget_usd (the lower of it and the env budget
  wins), jobs (id = run id, questions_dir, semantic, arch, model, batch, judge, sample, salt).
- `adsight bench resume --plan <yaml> [--wait-minutes M]` is one tick: re-attach batches,
  run every unfinished job at once (shared batch waves), exit 0 / 3 / 4 as above, append
  one line per job to `.adsight/progress.log` (and `$GITHUB_STEP_SUMMARY`).
- `adsight bench export --plan <yaml> --out benchmark/results/<plan>/` writes per job
  `report.json` and `cassette.jsonl.gz` (every response its results used) plus
  `summary.json`; `bench run --replay --cassette <file>` reproduces a job offline.
- `.github/workflows/bench-tick.yml` runs a tick every 30 minutes, state kept in the
  Actions cache. Switches: secret `ANTHROPIC_API_KEY`, variable `ANTHROPIC_BUDGET_USD`,
  variable `BENCH_ENABLED` (`true` to run, anything else to stop). It never commits.

## Rules

- Git identity is `haider268 <haider268@users.noreply.github.com>` only, set with
  `git config --local`. No `Co-Authored-By` lines and no Claude/Anthropic trailers.
- No secrets in any file. Keys live only in the git-ignored `.env`; `.env.example` ships
  blank placeholders. No real people and no real ad account data.
- Tests are offline: no network, no sockets. Tests that need a key carry the `live`
  marker and are skipped by default.
