"""Guard every agent-written query: parse it, allow only plain reads, run it read-only.

`validate_sql` rejects anything that is not a single SELECT over known tables;
`run_sql` validates, executes with a row cap and a timeout, and never raises.
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    return root.sql(dialect="duckdb")


@dataclass
class SqlResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0  # rows returned, at most max_rows
    truncated: bool = False  # the query had more rows than max_rows
    error: str | None = None


def run_sql(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    allowed_tables: set[str],
    max_rows: int = 200,
    timeout_s: float = 10.0,
) -> SqlResult:
    """Validate and run `sql` on a read-only connection. Never raises: a rejection, a
    database error or a timeout ("timeout") comes back in `error`.

    Every call runs on its own cursor, never on `con` itself: questions run concurrently
    (sync graph nodes run in a thread pool) and a shared connection hands one query's
    result to another."""
    try:
        safe = validate_sql(sql, allowed_tables)
    except UnsafeSQL as exc:
        return SqlResult(error=f"unsafe SQL: {exc.reason}")
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
        fetched = cur.fetchmany(max_rows + 1)
    except duckdb.Error as exc:
        if timed_out.is_set():
            return SqlResult(error="timeout")
        return SqlResult(error=f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
    finally:
        timer.cancel()
        cur.close()
    if timed_out.is_set():
        return SqlResult(error="timeout")
    rows = [tuple(r) for r in fetched[:max_rows]]
    return SqlResult(
        columns=columns, rows=rows, row_count=len(rows), truncated=len(fetched) > max_rows
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
    return duckdb.connect(str(db_path), read_only=True, config={"enable_external_access": False})
