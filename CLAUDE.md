# CLAUDE.md — adsight

adsight is a text-to-SQL marketing analytics agent. A question in plain English
about ad performance goes in; a DuckDB SQL query and its answer come out. The
project benchmarks five agent architectures against the same question set and
synthetic ad dataset. Status: scaffold only, no agent or benchmark yet.

## How to run

```
uv sync                       # create .venv and install everything
uv run adsight version        # prints "adsight 0.1.0"
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
| `src/adsight/log.py` | JSON-lines logging. `configure_logging`, `get_logger`, `set_run_id`/`get_run_id`. Extra fields via `extra={"fields": {...}}`. |
| `src/adsight/cli.py` | Typer app, entry point `adsight`. Commands: `version`. |

## Rules

- Git identity is `haider268 <haider268@users.noreply.github.com>` only, set
  with `git config --local`. The machine's global identity must never appear.
- No `Co-Authored-By` lines and no Claude/Anthropic trailers in commits.
- No secrets in any file. Keys live only in the git-ignored `.env`;
  `.env.example` ships blank placeholders.
- No real people and no real ad account data. All data is synthetic.
- Tests are offline: no network, no sockets. Tests that need a key carry the
  `live` marker and are skipped by default.
