# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English
about ad performance goes in; a DuckDB SQL query and its answer come out. The
project benchmarks five agent architectures against the same question set and
synthetic ad dataset. Status: synthetic dataset built; no agent or benchmark yet.

## How to run

```
uv sync                       # create .venv and install everything
uv run adsight version        # prints "adsight 0.1.0"
uv run adsight data           # generate + build data/adsight.duckdb
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
| `src/adsight/data/events.py` | `ChangeEvent`, `baseline_events()`, and state from events: `budget_on`, `learning_start`. |
| `src/adsight/data/generator.py` | `generate(seed, events)` -> `Dataset`: one metrics row per ad per day; `clip`, `poisson`, `cpm_base`. |
| `src/adsight/data/build.py` | `build_database(dataset, path)` (fresh file, CSV + `COPY` load) and `open_readonly(path)`. |

## Data contract

- Window: 2026-01-01 to 2026-06-29 (180 days). All money is USD.
- 3 accounts, 9 campaigns, 18 ad sets, 54 ads (AD001..AD054), 8,640 metrics rows,
  15 baseline events (9 launches, 6 routine budget changes under 20%).
- RNG rule: every ad-day draws from its own `random.Random(f"{seed}:{ad_id}:{day}")`,
  so adding an event changes only the ad-days that event affects.
- State comes from events; add an event, never edit the generator, to change history.
- `adsight data [--seed N] [--data-dir DIR]` writes `<data-dir>/adsight.duckdb`
  (defaults: `ADSIGHT_SEED` or 7, `ADSIGHT_DATA_DIR` or `data`). `data/` is git-ignored.

## Rules

- Git identity is `haider268 <haider268@users.noreply.github.com>` only, set
  with `git config --local`. The machine's global identity must never appear.
- No `Co-Authored-By` lines and no Claude/Anthropic trailers in commits.
- No secrets in any file. Keys live only in the git-ignored `.env`;
  `.env.example` ships blank placeholders.
- No real people and no real ad account data. All data is synthetic.
- Tests are offline: no network, no sockets. Tests that need a key carry the
  `live` marker and are skipped by default.
