from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from adsight.sql_safety import UnsafeSQL, open_agent_connection, run_sql, validate_sql

TABLES = {"t", "u"}
REPO_ROOT = Path(__file__).resolve().parents[1]

REJECTED = [
    ("INSERT INTO t VALUES (1, 2)", "INSERT is not allowed"),
    ("UPDATE t SET a = 1", "UPDATE is not allowed"),
    ("DELETE FROM t", "DELETE is not allowed"),
    ("DROP TABLE t", "DROP is not allowed"),
    ("CREATE TABLE x AS SELECT * FROM t", "CREATE is not allowed"),
    ("ALTER TABLE t ADD COLUMN c INT", "ALTER is not allowed"),
    ("ATTACH 'other.db' AS other", "ATTACH is not allowed"),
    ("COPY t TO 'out.csv'", "COPY is not allowed"),
    ("PRAGMA database_list", "PRAGMA is not allowed"),
    ("SET threads = 1", "SET is not allowed"),
    ("SELECT 1; DROP TABLE x", "exactly one statement is allowed, got 2"),
    ("SELECT read_csv('x.csv')", "function read_csv() is not allowed"),
    ("SELECT * FROM read_parquet('x.parquet')", "table function read_parquet() is not allowed"),
    ("SELECT * FROM nope", "unknown table: nope"),
    ("garbage string here !!", "unparsable SQL"),
    ("SELECT * INTO new_t FROM t", "INTO is not allowed"),
    ("SELECT getenv('HOME')", "function getenv() is not allowed"),
    ("SELECT * FROM t JOIN glob('*') g ON true", "table function glob() is not allowed"),
    ("SELECT * FROM 'x.csv'", "unknown table: x.csv"),
    ("SELECT * FROM other.t", "qualified table other.t is not allowed"),
    ("LOAD httpfs", "COMMAND is not allowed"),
    ("", "empty query"),
]

ALLOWED = [
    "SELECT a, b FROM t WHERE a > 1",
    "WITH x AS (SELECT a FROM t) SELECT * FROM x",
    "SELECT a FROM t UNION SELECT a FROM u",
    "SELECT * FROM t WHERE a IN (SELECT a FROM u)",
    "SELECT a, SUM(b) OVER (PARTITION BY a ORDER BY b) FROM t",
]


@pytest.mark.parametrize(("sql", "reason"), REJECTED)
def test_forbidden_forms_are_rejected(sql: str, reason: str) -> None:
    with pytest.raises(UnsafeSQL) as caught:
        validate_sql(sql, TABLES)
    assert reason in caught.value.reason


@pytest.mark.parametrize("sql", ALLOWED)
def test_plain_reads_are_allowed(sql: str) -> None:
    assert validate_sql(sql, TABLES)


def test_every_reference_query_validates_and_keeps_its_result(bench_data_dir: Path) -> None:
    tables = {"accounts", "campaigns", "ad_sets", "ads", "daily_ad_metrics", "events_log"}
    con = duckdb.connect(str(bench_data_dir / "adsight.duckdb"), read_only=True)
    try:
        folders = [REPO_ROOT / "benchmark" / "reference_sql"]
        folders.append(REPO_ROOT / "benchmark" / "generated" / "adsight-demo" / "reference_sql")
        for folder in folders:
            for path in sorted(folder.glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                normalised = validate_sql(sql, tables)
                assert con.execute(normalised).fetchall() == con.execute(sql).fetchall(), path
    finally:
        con.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "small.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t AS SELECT i AS a, i * 2 AS b FROM range(500) r(i)")
    con.execute("CREATE TABLE u AS SELECT i AS a FROM range(10) r(i)")
    con.close()
    return path


@pytest.fixture
def agent_con(db: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = open_agent_connection(db)
    yield con
    con.close()


def test_run_sql_truncates(agent_con: duckdb.DuckDBPyConnection) -> None:
    result = run_sql(agent_con, "SELECT a, b FROM t ORDER BY a", TABLES, max_rows=50)
    assert result.error is None
    assert result.columns == ["a", "b"]
    assert result.row_count == 50 and len(result.rows) == 50 and result.truncated
    small = run_sql(agent_con, "SELECT a FROM u", TABLES, max_rows=50)
    assert small.row_count == 10 and not small.truncated


def test_run_sql_returns_errors(agent_con: duckdb.DuckDBPyConnection) -> None:
    result = run_sql(agent_con, "SELECT no_such_column FROM t", TABLES)
    assert result.error is not None and "no_such_column" in result.error
    assert result.rows == []
    blocked = run_sql(agent_con, "DROP TABLE t", TABLES)
    assert blocked.error is not None and blocked.error.startswith("unsafe SQL: DROP")


def test_writes_never_reach_the_database(db: Path) -> None:
    writable = duckdb.connect(str(db))
    try:
        for sql in ("DELETE FROM t", "DROP TABLE u", "INSERT INTO t VALUES (1, 1)"):
            assert run_sql(writable, sql, TABLES).error is not None
        counts = [writable.execute(f"SELECT COUNT(*) FROM {n}").fetchone() for n in ("t", "u")]
    finally:
        writable.close()
    assert counts == [(500,), (10,)]


def test_run_sql_times_out(agent_con: duckdb.DuckDBPyConnection) -> None:
    slow = "SELECT COUNT(*) FROM t a, t b, t c, t d WHERE a.a + b.a + c.a + d.a = -1"
    result = run_sql(agent_con, slow, TABLES, timeout_s=0.2)
    assert result.error == "timeout"
    assert run_sql(agent_con, "SELECT COUNT(*) FROM u", TABLES).rows == [(10,)]


def test_agent_connection_is_read_only_without_file_access(
    agent_con: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(duckdb.Error):
        agent_con.execute("CREATE TABLE x (a INT)")
    with pytest.raises(duckdb.Error):
        agent_con.execute("SELECT * FROM read_csv('pyproject.toml')").fetchall()
