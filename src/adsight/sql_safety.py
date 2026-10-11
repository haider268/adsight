"""Guard every agent-written query: parse it, allow only plain reads, run it read-only.

`validate_sql` rejects anything that is not a single SELECT over known tables;
`run_sql` validates, executes with a row cap and a timeout, and never raises.
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Pragma,
    exp.Set,
    exp.Attach,
    exp.Detach,
    exp.Use,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Merge,
    exp.Into,
    exp.Install,
    exp.Describe,
)
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_text",
        "read_blob",
        "glob",
        "query",
        "query_table",
        "getenv",
        "current_setting",
    }
)
ROOT_TYPES: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation, exp.Subquery)


class UnsafeSQL(Exception):
    """The query was rejected; `reason` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _from_position_function(node: exp.Expression) -> str | None:
    """The function name when `node` (a FROM or JOIN target) is a table function."""
    target = node.this if isinstance(node, (exp.From, exp.Join)) else node
    if isinstance(target, exp.Table) and isinstance(target.this, exp.Func):
        return _function_name(target.this)
    if isinstance(target, exp.Func):
        return _function_name(target)
    return None


def validate_sql(sql: str, allowed_tables: set[str]) -> str:
    """Return the normalised SQL, or raise UnsafeSQL with a specific reason."""
    return _validated(sql, allowed_tables).sql(dialect="duckdb")


def has_top_level_order(root: exp.Expression) -> bool:
    """Whether the outermost query (after any CTEs) has an ORDER BY."""
    while isinstance(root, exp.Subquery):
        root = root.this
    return bool(root.args.get("order"))


def row_sort_key(row: tuple[Any, ...]) -> tuple[tuple[bool, str, str], ...]:
    """A total, run-independent order for rows of any types."""
    return tuple((v is None, type(v).__name__, str(v)) for v in row)


Render = Literal["v3", "v4"]
"""How query results become prompt text. "v3": rows sorted when the top-level SELECT has no
ORDER BY, database order otherwise (the published v3 run). "v4": the same, and rows that
tie on every ORDER BY key are put in a fixed order, because DuckDB does not always return
tied rows in the same order."""
RENDERS: tuple[Render, ...] = ("v3", "v4")


def order_key_columns(root: exp.Expression, columns: list[str]) -> list[int] | None:
    """Output positions of the top-level ORDER BY keys (by position, output name, or the
    same expression as an output column); None when any key is not an output column."""
    while isinstance(root, exp.Subquery):
        root = root.this
    order = root.args.get("order")
    if order is None:
        return []
    selects = list(getattr(root, "selects", []))
    texts = [s.unalias().sql(dialect="duckdb") for s in selects]
    lowered = [c.casefold() for c in columns]
    found: list[int] = []
    for ordered in order.expressions:
        key = ordered.this
        if isinstance(key, exp.Literal) and key.is_int:
            idx = int(key.this) - 1
        elif isinstance(key, exp.Column) and lowered.count(key.name.casefold()) == 1:
            idx = lowered.index(key.name.casefold())
        elif key.sql(dialect="duckdb") in texts and len(texts) == len(columns):
            idx = texts.index(key.sql(dialect="duckdb"))
        else:
            return None
        if not 0 <= idx < len(columns):
            return None
        found.append(idx)
    return found


def break_ties(rows: list[tuple[Any, ...]], keys: list[int]) -> list[tuple[Any, ...]]:
    """Keep the database order, but sort each run of rows equal on every key column."""
    out: list[tuple[Any, ...]] = []
    group: list[tuple[Any, ...]] = []
    for row in rows:
        if group and [row[i] for i in keys] != [group[0][i] for i in keys]:
            out += sorted(group, key=row_sort_key)
            group = []
        group.append(row)
    return out + sorted(group, key=row_sort_key)


def _validated(sql: str, allowed_tables: set[str]) -> exp.Expression:
    try:
        parsed = sqlglot.parse(sql, dialect="duckdb")
    except SqlglotError as exc:
        raise UnsafeSQL(f"unparsable SQL: {str(exc).splitlines()[0]}") from exc
    statements = [p for p in parsed if p is not None]
    if not statements:
        raise UnsafeSQL("empty query")
    if len(statements) != 1:
        raise UnsafeSQL(f"exactly one statement is allowed, got {len(statements)}")
    root = statements[0]

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQL(f"{type(node).__name__.upper()} is not allowed: read-only SELECT only")
    if not isinstance(root, ROOT_TYPES):
        raise UnsafeSQL(f"only SELECT queries are allowed, got {type(root).__name__.upper()}")

    for node in root.find_all(exp.From, exp.Join):
        name = _from_position_function(node)
        if name is not None:
            raise UnsafeSQL(f"table function {name}() is not allowed")
    for func in root.find_all(exp.Func):
        name = _function_name(func)
        if name in FORBIDDEN_FUNCTIONS:
            raise UnsafeSQL(f"function {name}() is not allowed")

    ctes = {str(cte.alias_or_name).lower() for cte in root.find_all(exp.CTE)}
    allowed = {t.lower() for t in allowed_tables}
    for table in root.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            raise UnsafeSQL(f"table function {_function_name(table.this)}() is not allowed")
        name = str(table.name).lower()
        qualifier = [str(p).lower() for p in (table.catalog, table.db) if p]
        if qualifier and qualifier not in (["main"], ["memory", "main"]):
            raise UnsafeSQL(f"qualified table {table.sql(dialect='duckdb')} is not allowed")
        if name not in allowed and name not in ctes:
            raise UnsafeSQL(f"unknown table: {table.name}")
    return root


@dataclass
class SqlResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0  # rows returned, at most max_rows
    truncated: bool = False  # the query had more rows than max_rows
    error: str | None = None
    sorted_by_adsight: bool = False  # no ORDER BY in the SQL: rows sorted for determinism


def run_sql(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    allowed_tables: set[str],
    max_rows: int = 200,
    timeout_s: float = 10.0,
    render: Render = "v4",
) -> SqlResult:
    """Validate and run `sql` on a read-only connection. Never raises: a rejection, a
    database error or a timeout ("timeout") comes back in `error`. `render` picks how rows
    are ordered for the prompt (see Render).

    Every call runs on its own cursor, never on `con` itself: questions run concurrently
    (sync graph nodes run in a thread pool) and a shared connection hands one query's
    result to another."""
    try:
        root = _validated(sql, allowed_tables)
    except UnsafeSQL as exc:
        return SqlResult(error=f"unsafe SQL: {exc.reason}")
    safe = root.sql(dialect="duckdb")
    sort_rows = not has_top_level_order(root)
    timed_out = threading.Event()
    cur = con.cursor()

    def interrupt() -> None:
        timed_out.set()
        cur.interrupt()

    timer = threading.Timer(timeout_s, interrupt)
    timer.daemon = True
    timer.start()
    try:
        cur.execute(safe)
        columns = [d[0] for d in cur.description or []]
        # Without an ORDER BY the database order is arbitrary, and the rows go into the
        # next prompt: fetch them all and sort them so replays see the same text. Under v4
        # ties are broken too, so every row is needed to know where a tie ends.
        fetch_all = sort_rows or render == "v4"
        fetched = cur.fetchall() if fetch_all else cur.fetchmany(max_rows + 1)
    except duckdb.Error as exc:
        if timed_out.is_set():
            return SqlResult(error="timeout")
        return SqlResult(error=f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
    finally:
        timer.cancel()
        cur.close()
    if timed_out.is_set():
        return SqlResult(error="timeout")
    all_rows = [tuple(r) for r in fetched]
    if sort_rows:
        all_rows.sort(key=row_sort_key)
    elif render == "v4":
        keys = order_key_columns(root, columns)
        # None: an ORDER BY key is not an output column, so ties cannot be seen in the
        # rows and the database order stands
        if keys is not None:
            all_rows = break_ties(all_rows, keys)
    rows = all_rows[:max_rows]
    return SqlResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(all_rows) > max_rows,
        sorted_by_adsight=sort_rows,
    )


def query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[tuple[Any, ...]]:
    """Trusted internal SQL (schema card, diagnose tool) on its own cursor, for the same
    reason as run_sql: the connection is shared by concurrent questions."""
    cur = con.cursor()
    try:
        cur.execute(sql, params or [])
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def open_agent_connection(db_path: Path) -> duckdb.DuckDBPyConnection:
    """A read-only connection with no file, network or extension access."""
    config: dict[str, str | bool | int | float | list[str]] = {
        "enable_external_access": False,
        "threads": 1,  # deterministic execution and float summation order
    }
    return duckdb.connect(str(db_path), read_only=True, config=config)
