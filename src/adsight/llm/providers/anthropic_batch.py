"""Anthropic Message Batches behind the same one-request interface.

`complete()` enqueues the request and awaits a future. A coordinator task flushes the
queue as ONE batch once no new request has arrived for `idle_flush_s` (or the queue
holds `max_batch` requests), polls the batch until it ends, and resolves each future
from the results, which arrive in any order and are matched by custom_id.

Crash safety: every batch is recorded in the cache DB (`batches`, `batch_items`) before
it is submitted. The API assigns the batch id, so the row is written under a provisional
"local-..." id with status "submitting" and renamed to the real id once the create call
returns. On start-up every recorded batch that is not ingested is polled and ingested
into the response cache before any new batch is submitted, so a stopped run never pays
twice. A row still "submitting" at start-up (a crash inside the create call) cannot be
re-attached and is marked "abandoned".
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from adsight.llm.base import BatchPending, InvalidLLMOutput, TransientLLMError
from adsight.llm.budget import BudgetGuard
from adsight.llm.cache import ResponseCache
from adsight.llm.limits import Limits
from adsight.llm.providers.anthropic_direct import (
    build_params,
    map_anthropic_error,
    parse_message,
)
from adsight.llm.types import LLMRequest, LLMResponse, canonical_json, request_key
from adsight.log import get_logger

BATCH_LIMITS = Limits(requests_per_min=1_000_000, tokens_per_min=1e12, concurrency=100_000)
MAX_BATCH = 5000
MAX_POLL_FAILURES = 10
CUSTOM_ID_CHARS = 40

log = get_logger("llm.batch")


@dataclass
class _Item:
    key: str
    req: LLMRequest
    futures: list["asyncio.Future[LLMResponse]"] = field(default_factory=list)
    attempts: int = 0

    @property
    def custom_id(self) -> str:
        return self.key[:CUSTOM_ID_CHARS]

    @property
    def awaited(self) -> bool:
        """Whether any caller still waits for this request."""
        return any(not f.done() for f in self.futures)


def _error_type(result: Any) -> str:
    error = getattr(result, "error", None)
    inner = getattr(error, "error", error)
    return str(getattr(inner, "type", "") or "")


class BatchingAnthropicProvider:
    def __init__(
        self,
        client: Any,
        cache: ResponseCache,
        guard: BudgetGuard,
        idle_flush_s: float = 3.0,
        poll_s: float = 30.0,
        max_batch: int = MAX_BATCH,
        wait_s: float | None = None,
    ):
        """`wait_s` is tick mode: once that many seconds have passed since this provider was
        created, a batch that has not ended stops the run (BatchPending) instead of being
        waited for. None waits forever."""
        self.client, self.cache, self.guard = client, cache, guard
        self.wait_s = wait_s
        self.deadline = None if wait_s is None else time.monotonic() + wait_s
        self.waiting = False  # a batch outlived the deadline: accept no new requests
        self.idle_flush_s, self.poll_s, self.max_batch = idle_flush_s, poll_s, max_batch
        self.submitted: list[str] = []  # batch ids this instance created
        self._queue: dict[str, _Item] = {}
        self._inflight: dict[str, _Item] = {}
        self._collectors: set[asyncio.Task[None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._reattached = False
        self._last_arrival = 0.0
        self._pending_id = ""

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        """One future per key: a request whose key is already queued or in a batch in flight
        awaits that request's result; nothing new is queued or paid for."""
        key = request_key(req, salt)
        stored = self.cache.get(key)
        if stored is not None:
            return stored.model_copy(update={"cached": True, "cost_usd": 0.0})
        if self.waiting:
            raise BatchPending(self._pending_id)
        self.guard.check()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[LLMResponse] = loop.create_future()
        item = self._inflight.get(key) or self._queue.get(key)
        if item is None:
            self._queue[key] = _Item(key, req, [future])
        else:
            item.futures.append(future)
        self._last_arrival = loop.time()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())
        return await future

    # --- coordinator --------------------------------------------------------------

    async def reattach(self) -> None:
        """Poll and ingest every recorded batch not yet ingested (once per instance)."""
        if not self._reattached:
            await self._reattach()
            self._reattached = True

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self.reattach()
            while self._queue or self._collectors:
                if self.waiting and self._queue:
                    for item in list(self._queue.values()):
                        self._finish(item, exc=BatchPending(self._pending_id))
                    self._queue.clear()
                    continue
                if not self._queue:
                    await asyncio.wait(
                        set(self._collectors),
                        timeout=self.idle_flush_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue
                idle = loop.time() - self._last_arrival
                if len(self._queue) >= self.max_batch or idle >= self.idle_flush_s:
                    await self._submit()
                else:
                    await asyncio.sleep(self.idle_flush_s - idle)
        except Exception as exc:
            log.error("batch coordinator failed", extra={"fields": {"error": repr(exc)}})
            for item in list(self._queue.values()):
                self._finish(item, exc=TransientLLMError(f"batch coordinator failed: {exc}"))
            self._queue.clear()

    async def _reattach(self) -> None:
        for batch_id, status in self.cache.open_batches():
            if status == "submitting":
                log.warning(
                    "batch abandoned: crashed while submitting",
                    extra={"fields": {"batch_id": batch_id}},
                )
                self.cache.set_batch_status(batch_id, "abandoned", ended=True)
                continue
            items: dict[str, _Item] = {}
            for custom_id, (key, req) in self.cache.batch_items(batch_id).items():
                item = self._queue.pop(key, None) or _Item(key, req)
                self._inflight[key] = item
                items[custom_id] = item
            log.info("batch reattach", extra={"fields": {"batch_id": batch_id, "n": len(items)}})
            await self._collect(batch_id, items)

    async def drain(self) -> None:
        """Wait until every submitted batch has been ingested (or its wait has run out)."""
        while self._collectors:
            await asyncio.wait(set(self._collectors))

    async def _submit(self) -> None:
        """Flush the queue as one batch. An item whose response reached the cache meanwhile
        is answered from it, an item nobody waits for any more is dropped, and an empty
        result submits nothing."""
        pending: list[_Item] = []
        for item in self._queue.values():
            stored = self.cache.get(item.key)
            if stored is not None:
                self._finish(item, resp=stored.model_copy(update={"cached": True, "cost_usd": 0}))
            elif not item.awaited:
                log.info("batch item dropped: nobody waits", extra={"fields": {"key": item.key}})
            else:
                pending.append(item)
        self._queue.clear()
        if not pending:
            return
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        local_id = f"local-{uuid.uuid4().hex}"
        self.cache.record_batch(
            local_id, "submitting", [(i.custom_id, i.key, canonical_json(i.req)) for i in pending]
        )
        for item in pending:
            self._inflight[item.key] = item
        requests = [
            Request(
                custom_id=i.custom_id,
                params=cast(MessageCreateParamsNonStreaming, build_params(i.req)),
            )
            for i in pending
        ]
        try:
            batch = await self.client.messages.batches.create(requests=requests)
        except Exception as exc:
            self.cache.set_batch_status(local_id, "failed", ended=True)
            mapped = map_anthropic_error(exc)
            log.error("batch submit failed", extra={"fields": {"error": repr(exc)}})
            for item in pending:
                self._finish(item, exc=mapped)
            return
        batch_id = str(batch.id)
        self.cache.rename_batch(local_id, batch_id, str(batch.processing_status))
        self.submitted.append(batch_id)
        log.info("batch submit", extra={"fields": {"batch_id": batch_id, "n": len(pending)}})
        task = asyncio.get_running_loop().create_task(
            self._collect(batch_id, {i.custom_id: i for i in pending})
        )
        self._collectors.add(task)
        task.add_done_callback(self._collectors.discard)

    async def _collect(self, batch_id: str, items: dict[str, _Item]) -> None:
        failures = 0
        while True:
            try:
                batch = await self.client.messages.batches.retrieve(batch_id)
            except Exception as exc:
                failures += 1
                log.warning(
                    "batch poll failed",
                    extra={"fields": {"batch_id": batch_id, "error": repr(exc)}},
                )
                if failures >= MAX_POLL_FAILURES:
                    for item in items.values():
                        self._finish(item, exc=TransientLLMError(f"cannot poll {batch_id}"))
                    return
                await asyncio.sleep(self.poll_s)
                continue
            failures = 0
            status = str(batch.processing_status)
            counts = getattr(batch, "request_counts", None)
            fields = {"batch_id": batch_id, "status": status, "counts": str(counts)}
            log.info("batch poll", extra={"fields": fields})
            if status == "ended":
                break
            self.cache.set_batch_status(batch_id, status)
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self.waiting, self._pending_id = True, batch_id
                log.info("batch wait over", extra={"fields": {"batch_id": batch_id}})
                for item in items.values():
                    self._finish(item, exc=BatchPending(batch_id))
                return
            await asyncio.sleep(self.poll_s)

        seen: set[str] = set()
        outcomes: dict[str, int] = {}
        try:
            async for entry in await self.client.messages.batches.results(batch_id):
                found = items.get(str(entry.custom_id))
                if found is None:
                    continue
                seen.add(found.custom_id)
                kind = self._settle(found, entry.result)
                outcomes[kind] = outcomes.get(kind, 0) + 1
        except Exception as exc:
            log.error(
                "batch results failed", extra={"fields": {"batch_id": batch_id, "error": repr(exc)}}
            )
            for custom_id, item in items.items():
                if custom_id not in seen:
                    self._finish(item, exc=TransientLLMError(f"cannot read results of {batch_id}"))
            return
        for custom_id, item in items.items():
            if custom_id not in seen:
                self._retry_or_fail(item, "missing from results")
        self.cache.set_batch_status(batch_id, "ingested", ended=True)
        log.info("batch ingest", extra={"fields": {"batch_id": batch_id, "outcomes": outcomes}})

    def _settle(self, item: _Item, result: Any) -> str:
        kind = str(getattr(result, "type", "unknown"))
        if kind == "succeeded":
            try:
                resp = parse_message(result.message, item.req.model, batched=True)
            except InvalidLLMOutput as exc:
                self._finish(item, exc=exc)
                return "invalid_output"
            self.cache.put(item.key, item.req, resp)
            self._finish(item, resp=resp)
        elif kind == "errored" and "invalid_request" in _error_type(result):
            self._finish(item, exc=InvalidLLMOutput("", f"invalid_request: {_error_type(result)}"))
        else:
            self._retry_or_fail(item, kind)
        return kind

    def _retry_or_fail(self, item: _Item, why: str) -> None:
        """Errored (not invalid), canceled, expired or missing: requeue once, then fail."""
        self._inflight.pop(item.key, None)
        if not item.awaited:
            return  # nobody waiting (e.g. re-attached): a later request simply resubmits
        if item.attempts < 1:
            item.attempts += 1
            queued = self._queue.get(item.key)
            if queued is None:
                self._queue[item.key] = item
            else:
                queued.futures.extend(item.futures)
            self._last_arrival = asyncio.get_running_loop().time()
            return
        self._finish(item, exc=TransientLLMError(f"batch request {why} twice"))

    def _finish(
        self,
        item: _Item,
        resp: LLMResponse | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._inflight.pop(item.key, None)
        for future in item.futures:
            if future.done():
                continue
            if exc is not None:
                future.set_exception(exc)
            elif resp is not None:
                future.set_result(resp)
