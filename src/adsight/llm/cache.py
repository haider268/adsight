"""A SQLite response cache: the same request never pays twice."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from adsight.llm.base import CacheMiss, LLMProvider
from adsight.llm.types import LLMRequest, LLMResponse, canonical_json, request_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    batched INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    ended_at TEXT,
    n_requests INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL,
    custom_id TEXT NOT NULL,
    key TEXT NOT NULL,
    request_json TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ResponseCache:
    """Responses keyed by request key, plus the Anthropic batch bookkeeping tables."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(str(path))
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def get(self, key: str) -> LLMResponse | None:
        row = self.con.execute(
            "SELECT response_json FROM responses WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else LLMResponse.model_validate_json(row[0])

    def put(self, key: str, req: LLMRequest, resp: LLMResponse) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                req.provider,
                req.model,
                canonical_json(req),
                resp.model_dump_json(),
                resp.input_tokens,
                resp.output_tokens,
                resp.cache_read_tokens,
                resp.cache_write_tokens,
                resp.cost_usd,
                int(resp.batched),
                now_iso(),
            ),
        )
        self.con.commit()

    def spent_usd(self, provider: str) -> float:
        row = self.con.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM responses WHERE provider = ?", (provider,)
        ).fetchone()
        return float(row[0]) if row else 0.0

    # --- batch bookkeeping -----------------------------------------------------------

    def record_batch(self, batch_id: str, status: str, items: list[tuple[str, str, str]]) -> None:
        """Record a batch and its (custom_id, key, request_json) items in one transaction."""
        with self.con:
            self.con.execute(
                "INSERT INTO batches VALUES (?, ?, ?, NULL, ?)",
                (batch_id, status, now_iso(), len(items)),
            )
            self.con.executemany(
                "INSERT INTO batch_items VALUES (?, ?, ?, ?)",
                [(batch_id, cid, key, rj) for cid, key, rj in items],
            )

    def rename_batch(self, old_id: str, new_id: str, status: str) -> None:
        with self.con:
            self.con.execute(
                "UPDATE batches SET batch_id = ?, status = ? WHERE batch_id = ?",
                (new_id, status, old_id),
            )
            self.con.execute(
                "UPDATE batch_items SET batch_id = ? WHERE batch_id = ?", (new_id, old_id)
            )

    def set_batch_status(self, batch_id: str, status: str, ended: bool = False) -> None:
        with self.con:
            if ended:
                self.con.execute(
                    "UPDATE batches SET status = ?, ended_at = ? WHERE batch_id = ?",
                    (status, now_iso(), batch_id),
                )
            else:
                self.con.execute(
                    "UPDATE batches SET status = ? WHERE batch_id = ?", (status, batch_id)
                )

    def open_batches(self) -> list[tuple[str, str]]:
        """(batch_id, status) of every batch not yet ingested, oldest first."""
        rows = self.con.execute(
            "SELECT batch_id, status FROM batches "
            "WHERE status NOT IN ('ingested', 'abandoned', 'failed') "
            "ORDER BY submitted_at, rowid"
        ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def batch_items(self, batch_id: str) -> dict[str, tuple[str, LLMRequest]]:
        """custom_id -> (key, request) for one batch."""
        rows = self.con.execute(
            "SELECT custom_id, key, request_json FROM batch_items WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        return {
            str(cid): (str(key), LLMRequest.model_validate(json.loads(rj))) for cid, key, rj in rows
        }

    def export_rows(self, keys: list[str]) -> list[dict[str, object]]:
        """The stored rows for `keys` (missing keys are skipped), sorted by key."""
        out: list[dict[str, object]] = []
        for key in sorted(set(keys)):
            cursor = self.con.execute("SELECT * FROM responses WHERE key = ?", (key,))
            names = [d[0] for d in cursor.description]
            row = cursor.fetchone()
            if row is not None:
                out.append(dict(zip(names, row, strict=True)))
        return out

    def import_rows(self, rows: list[dict[str, object]]) -> None:
        with self.con:
            for row in rows:
                names = list(row)
                self.con.execute(
                    f"INSERT OR REPLACE INTO responses ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' for _ in names)})",
                    [row[n] for n in names],
                )

    @classmethod
    def from_cassette(cls, path: Path) -> "ResponseCache":
        """An in-memory cache holding exactly the responses in a cassette.jsonl.gz."""
        import gzip

        cache = cls(Path(":memory:"))
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            cache.import_rows([json.loads(line) for line in fh if line.strip()])
        return cache

    def batch_count(self) -> int:
        row = self.con.execute("SELECT COUNT(*) FROM batches").fetchone()
        return int(row[0]) if row else 0


class CachedProvider:
    """Answers from the cache when it can; otherwise asks `inner` and stores the answer.

    In replay mode a miss raises CacheMiss instead of calling `inner`.
    """

    def __init__(
        self,
        inner: LLMProvider,
        cache: ResponseCache,
        mode: Literal["normal", "replay"] = "normal",
    ):
        self.inner, self.cache, self.mode = inner, cache, mode
        self.hits = 0
        self.misses = 0

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        key = request_key(req, salt)
        stored = self.cache.get(key)
        if stored is not None:
            self.hits += 1
            return stored.model_copy(update={"cached": True, "cost_usd": 0.0})
        if self.mode == "replay":
            raise CacheMiss(key)
        self.misses += 1
        resp = await self.inner.complete(req, salt)
        self.cache.put(key, req, resp)
        return resp
