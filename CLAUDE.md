# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English about ad
performance goes in; a DuckDB SQL query and its answer come out. The project benchmarks
agent architectures against the same question set and synthetic ad data.

## How to run

```
uv sync                       # create .venv and install everything
uv run adsight data           # data/adsight.duckdb + manifest.json (--shape flat-weekly: data/flat/)
uv run adsight bench refs     # check expected answers against reference SQL (--db for any file)
uv run adsight bench generate # write benchmark/generated/adsight-demo/
uv run adsight bench review benchmark/generated/adsight-demo   # pending root causes
uv run adsight bench run --questions-dir benchmark/generated/adsight-demo --arch b0 --model groq:openai/gpt-oss-120b
uv run adsight bench resume --plan benchmark/plans/haiku-pilot-v3.yaml   # one tick; status lists runs
uv run adsight bench export --plan <yaml> --out benchmark/results/<plan>/
uv run adsight bench table --results benchmark/results/<plan> --out docs/results
uv run adsight bench judge-check --model anthropic:claude-haiku-4-5 --batch   # judge vs labels
uv run pytest                 # offline tests; /check also runs ruff and mypy src
```

## Layout

| Path | What it holds |
|---|---|
| `src/adsight/config.py`, `log.py`, `cli.py`, `data/` | Settings, JSON-lines logs, the CLI; synthetic data, manifest, flat export. |
| `src/adsight/benchmark/`, `semantic/` | Answers, questions, refs, scoring; YAML model, SQL fragments, metrics, inspect. |
| `src/adsight/generate/` | Benchmark generator: periods, templates, linter, causes, `generate_benchmark`. |
| `src/adsight/sql_safety.py` | `validate_sql`, `run_sql`, `open_agent_connection`. |
| `src/adsight/llm/` | Types, portable schemas, cache, limits, pricing, budget, factory, `providers/`. |
| `src/adsight/agent/`, `architectures/` | Schema card, prompts, checks, diagnose, number check; B0-B4 graphs. |
| `src/adsight/bench_run.py`, `judge.py`, `judge_check.py`, `plans.py`, `results_table.py` | Runner, judge and its label check, plans / ticks / export, results table. |
| `benchmark/plans/`, `.github/workflows/`, `benchmark/judge/labels.jsonl` | Run plans; tick and export workflows; 30 hand-labelled judge cases. |
| `benchmark/results/<plan>/` | Exported runs: `<job>/report.json`, `<job>/cassette.jsonl.gz`, `summary.json`. |
| `semantic/demo.yaml`, `semantic/flat-demo.yaml` | Mappings drafted by `adsight inspect` for the two demo databases. |
| `benchmark/questions.jsonl`, `benchmark/reference_sql/` | The hand-written GOLD SET (100 questions). Expected answers and SQL never change. |
| `benchmark/generated/adsight-demo/`, `adsight-flat-demo/` | Published benchmarks: questions, reference SQL, context, lint report. |

## Data contract

- 2026-01-01 to 2026-06-29, USD; 3 accounts, 9 campaigns, 18 ad sets, 54 ads, 8,534 metric
  rows, 18 events. RNG per ad-day; state comes from events (add one, never edit the generator).
- Incidents (in `manifest.json`): I1 AD019 paused 03-16; I2 S13 budget doubled 04-20; I3 AD001
  fatigue 05-04 (hidden); I4 S09 audience 05-18..24; I5 A3 tracking 06-08..09 (hidden).
- Flat weekly export: one table `perf` (campaign x ad group x Monday week); names repeat.

## Benchmark contract

- Seed 7; today is 2026-06-30. 100 questions: 15 each lookup, aggregation, comparison, trend;
  20 root_cause; 10 each ambiguous, unanswerable.
- `AgentAnswer`: kind (answer / clarify / cannot_answer), summary, values, entities,
  direction, assumptions, sql. Matching: within 1% or a unit floor, one-to-one; ratios
  also as percents. Entities are normalised (case, quotes, spaces, `_`; months as YYYY-MM)
  and the first one the question does not itself name must be the expected one.
- root_cause: with a judge, the judge's verdict is the pass; the regex rule is still stored
  (`rule_passed`) and reported as "rule check". Without a judge the rule decides.
- Expected values come only from reference SQL; never type them by hand.

## Semantic layer and generator

- A YAML mapping names the fact table, date and grain, measures, levels top to bottom,
  attributes (`time_varying` = end-of-data snapshot) and an optional change log. Metrics
  live once in `semantic/metrics.py`. `bench generate` reads any database through its
  model; same seed, same bytes. Linter rules R1-R8; shortfalls go to `lint_report.json`.
- Root causes start from a logged change or a strict detector, never a raw jump. Detector
  questions and reworded ones start `review: pending`; reviews survive regeneration.

## SQL safety

- `validate_sql` (sqlglot, duckdb) allows one SELECT / set operation / WITH over known
  tables; it rejects writes, DDL, COPY, PRAGMA, SET, ATTACH, USE, transactions, SELECT
  INTO, table functions, file and environment functions, and unparsable SQL.
- `run_sql` runs the normalised SQL with a row cap and a timeout, on its own cursor, and
  never raises (errors in `SqlResult.error`); `open_agent_connection` is read-only.
- Determinism (so cassettes always replay): results rendered into prompts are sorted when
  the SQL has no top-level ORDER BY (`sorted_by_adsight`); agent DuckDB runs single-threaded;
  floats render as `repr` everywhere a prompt shows a number.

## LLM layer

- One interface: `LLMProvider.complete(LLMRequest, salt="")`, ONE JSON object per call.
  Providers: `fixture` (ScriptedProvider), `groq`, `gemini` (free, via LangChain),
  `anthropic` direct or batched. Specs look like `groq:openai/gpt-oss-120b`.
- Always CachedProvider(LimitedProvider(inner)): the SQLite cache means a re-run never pays
  twice; `--replay` never calls out. Token buckets per provider; retries with full jitter.
- `--batch`: calls flush as Anthropic batches, recorded and re-attached on start-up. Budget
  guard `ADSIGHT_ANTHROPIC_BUDGET_USD` (default 0 = no Anthropic call) checks stored spend.

## Agents

- System prompt = role + schema card (typed columns, unmapped columns as "other columns",
  joins, entity names, attribute values, change-log types, metric SQL, rules including how
  to answer a why question). Byte-identical for every call, so it caches.
- B0 (max 2 calls) plan -> answer. B1 (max 6) think/act loop; the 6th must answer. B2 (max
  2) 1-4 SQL steps -> check -> answer. B3 (max 5) B2 plus up to 3 fixes. B4 (max 7) classify
  -> B3's loop -> `diagnose` (never imports `adsight.generate`) -> synthesize -> verify
  numbers -> one resynthesize if a number is untraceable (`untraceable_numbers`).
- Answer fields: entities list only the answer (winner first), values unrounded (4+
  significant digits). Invalid output is retried once (salt "<salt>:retry1").

## Running the benchmark

- `adsight bench run --questions-dir <dir> --arch b0..b4 --model <spec> [--batch]
  [--judge <spec>] [--salt S] [--sample N | --limit N] [--wait-minutes M] [--run-id ID]
  [--replay [--cassette <file>]] [--semantic <yaml>] [--db <path>]`. Results go to
  `.adsight/runs.sqlite`, reports to `.adsight/reports/<run_id>.json`.
- Re-running a run id skips "done" questions. Exit 0 done; 3 budget or daily limit; 4 a
  batch outlasted `--wait-minutes` (the next run re-attaches it). `--salt`: repeat runs
  are independent samples; `--sample N`: N questions spread over the categories.
- `--judge <spec>`: a root-cause answer passes when the judge says it names the true cause
  and invents none. Stored: `passed`, `rule_passed`, `judge_passed`, `judge_json`.
- `bench judge-check --model <spec> [--batch]`: the judge vs the 30 hand labels.

## Plans, ticks and export

- `benchmark/plans/<name>.yaml`: name, budget_usd (lower of it and env wins), jobs (id = run
  id, questions_dir, semantic, arch, model, batch, judge, sample, salt, render).
- `bench resume --plan <yaml> [--wait-minutes M]` is one tick: re-attach batches, run every
  unfinished job at once, exit 0 / 3 / 4 as above, log progress to `.adsight/progress.log`.
- `bench export --plan <yaml> --out benchmark/results/<plan>/`: per job `report.json` and
  `cassette.jsonl.gz` (every response used), plus `summary.json`. `bench verify-results
  --results <dir>` replays every job offline with its recorded render (up to 3 tries).
- `bench table --results <dir> [--out docs/results]`: `results.md` and two SVG charts.
- Render: new runs use "v4" (ORDER BY ties broken); the published v3 run is "v3".
- `bench-tick.yml` ticks every 30 min on `vars.BENCH_PLAN` (else `haiku-demo-v3.yaml`;
  switches: secret `ANTHROPIC_API_KEY`, vars `ANTHROPIC_BUDGET_USD`, `BENCH_ENABLED`).
  `bench-export.yml` (manual, no secrets) uploads an export.

## Adapting to another dataset

Run `adsight inspect` and check the mapping (`adsight semantic check`). If volumes differ
from the demo, override thresholds under `tuning:` in the YAML (fields and defaults in
`src/adsight/tuning.py`; unknown keys fail). Then `bench generate`, review pending
questions, label 20-30 root-cause answers and run `bench judge-check` before trusting the judge.

## Chat and convoprobe

- `uv run adsight chat --model groq:openai/gpt-oss-120b`: chat; past turns go in the user message.
- `uv sync --extra probe` installs convoprobe (optional, pinned). Then `uv run convoprobe run
  benchmark/convoprobe/marketers.yaml --adapter adsight.probe_adapter:agent --html --out
  results/convoprobe` (model from `ADSIGHT_CHAT_MODEL`; see convoprobe's README for keys).

## Rules

- Git identity `haider268 <haider268@users.noreply.github.com>` only (`git config --local`);
  no `Co-Authored-By` lines and no Claude/Anthropic trailers.
- No secrets in any file: keys live only in the git-ignored `.env`. No real people or data.
- Tests are offline: no sockets, no live LLM call; tests that need a key carry `live`.
