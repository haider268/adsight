import asyncio
import json
import random
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from adsight.agent.schemas import AnswerJSON, B0Plan, ReActStep
from adsight.config import Settings
from adsight.llm.base import (
    BudgetExceeded,
    CacheMiss,
    DailyLimitReached,
    InvalidLLMOutput,
    TransientLLMError,
)
from adsight.llm.budget import BudgetGuard
from adsight.llm.cache import CachedProvider, ResponseCache
from adsight.llm.factory import make_provider, parse_spec
from adsight.llm.limits import LimitedProvider, Limits, RateLimiter, backoff_delay, with_retries
from adsight.llm.pricing import Usage, cost_usd, price_of
from adsight.llm.providers.anthropic_batch import BATCH_LIMITS, BatchingAnthropicProvider
from adsight.llm.providers.anthropic_direct import (
    AnthropicDirectProvider,
    build_params,
    map_anthropic_error,
    parse_message,
)
from adsight.llm.providers.errors import map_error
from adsight.llm.providers.fixture import ScriptedProvider
from adsight.llm.providers.gemini import GeminiProvider
from adsight.llm.providers.groq import GroqProvider, groq_max_tokens
from adsight.llm.schema import portable_schema
from adsight.llm.types import LLMRequest, LLMResponse, Msg, request_key

SCHEMA = {"type": "object", "properties": {"echo": {"type": "string"}}, "required": ["echo"]}


def req(text: str = "hi", model: str = "claude-haiku-4-5", **kw: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "provider": "anthropic",
        "model": model,
        "system": "You answer in JSON.",
        "messages": [Msg(role="user", text=text)],
        "schema": SCHEMA,
        "schema_name": "Echo",
        "max_tokens": 200,
    }
    fields.update(kw)
    return LLMRequest(**fields)


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    c = ResponseCache(tmp_path / ".adsight" / "llm_cache.sqlite")
    yield c
    c.close()


# --- types and schemas ------------------------------------------------------------


def test_request_key_is_stable_and_salted() -> None:
    a, b = req("x"), req("x")
    assert request_key(a) == request_key(b)
    assert request_key(a) != request_key(a, "retry1")
    assert request_key(a) != request_key(req("y"))
    assert len(request_key(a)) == 64


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


@pytest.mark.parametrize("model", [AnswerJSON, B0Plan, ReActStep])
def test_portable_schema(model: Any) -> None:
    schema = portable_schema(model)
    for node in _walk(schema):
        assert "$ref" not in node and "$defs" not in node
        assert not {"title", "default", "format"} & set(node)
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert "null" not in json.dumps(schema)


# --- cache ---------------------------------------------------------------------------


def test_cache_hit_costs_nothing(cache: ResponseCache) -> None:
    inner = ScriptedProvider([{"echo": "one"}, {"echo": "two"}])
    provider = CachedProvider(inner, cache)
    first = asyncio.run(provider.complete(req()))
    second = asyncio.run(provider.complete(req()))
    assert inner.calls == 1
    assert second.data == first.data == {"echo": "one"}
    assert second.cached and second.cost_usd == 0
    assert provider.hits == 1 and provider.misses == 1
    salted = asyncio.run(provider.complete(req(), "retry1"))
    assert salted.data == {"echo": "two"} and inner.calls == 2


def test_replay_miss_raises(cache: ResponseCache) -> None:
    inner = ScriptedProvider([{"echo": "never"}])
    provider = CachedProvider(inner, cache, mode="replay")
    with pytest.raises(CacheMiss):
        asyncio.run(provider.complete(req()))
    assert inner.calls == 0


# --- limits and retries ----------------------------------------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_bucket_delays_the_third_call() -> None:
    t = FakeTime()
    limiter = RateLimiter(Limits(2, 1e9, 10), clock=t.clock, sleep=t.sleep)

    async def three() -> None:
        for _ in range(3):
            await limiter.acquire(10)

    asyncio.run(three())
    assert len(t.slept) == 1
    assert t.slept[0] == pytest.approx(30.0)


def test_retry_succeeds_on_third_attempt() -> None:
    t = FakeTime()
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientLLMError("busy")
        return "ok"

    assert asyncio.run(with_retries(flaky, sleep=t.sleep, rng=random.Random(1))) == "ok"
    assert attempts == 3
    assert len(t.slept) == 2
    assert all(0 <= d <= min(60.0, 2.0 * 2**i) for i, d in enumerate(t.slept))


def test_retry_gives_up_and_honours_retry_after() -> None:
    t = FakeTime()
    calls = 0

    async def always() -> str:
        nonlocal calls
        calls += 1
        raise TransientLLMError("busy", retry_after=7.0)

    with pytest.raises(TransientLLMError):
        asyncio.run(with_retries(always, sleep=t.sleep, rng=random.Random(1)))
    assert calls == 5 and t.slept == [7.0] * 4
    rng = random.Random(3)
    assert all(0 <= backoff_delay(10, rng) <= 60.0 for _ in range(200))


def test_limited_provider_retries_transient_errors() -> None:
    t = FakeTime()
    inner = ScriptedProvider([TransientLLMError("busy"), {"echo": "ok"}])
    limiter = RateLimiter(Limits(100, 1e9, 2), clock=t.clock, sleep=t.sleep)
    provider = LimitedProvider(inner, limiter, sleep=t.sleep, rng=random.Random(1))
    assert asyncio.run(provider.complete(req())).data == {"echo": "ok"}
    assert inner.calls == 2


# --- error mapping (Groq, Gemini) ----------------------------------------------------


class FakeHTTPError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


GROQ_TPD = (
    "Error code: 429 - Rate limit reached for model `openai/gpt-oss-20b` in organization "
    "`org_x` on tokens per day (TPD): Limit 200000, Used 199500, Requested 900. "
    "Please try again in 7m12.5s."
)
GROQ_TPM = (
    "Error code: 429 - Rate limit reached for model `openai/gpt-oss-20b` on tokens per "
    "minute (TPM): Limit 8000, Used 7900, Requested 900. Please try again in 1.2s."
)


def test_groq_daily_limit_vs_transient() -> None:
    daily = map_error(FakeHTTPError(GROQ_TPD, 429), "groq", "openai/gpt-oss-20b")
    assert isinstance(daily, DailyLimitReached)
    assert daily.reset_hint == "try again in 7m12.5s"
    minute = map_error(FakeHTTPError(GROQ_TPM, 429), "groq", "openai/gpt-oss-20b")
    assert isinstance(minute, TransientLLMError)
    assert isinstance(map_error(FakeHTTPError("down", 503), "groq", "m"), TransientLLMError)
    assert isinstance(map_error(TimeoutError("slow"), "groq", "m"), TransientLLMError)
    bad = FakeHTTPError("bad request", 400)
    assert map_error(bad, "groq", "m") is bad


def test_gemini_daily_limit_through_a_wrapper() -> None:
    class FakeClientError(Exception):
        code = 429

    class Wrapper(Exception):
        pass

    def wrapped(message: str) -> Exception:
        try:
            raise FakeClientError(message)
        except FakeClientError as inner:
            try:
                raise Wrapper("Error calling model") from inner
            except Wrapper as outer:
                return outer

    daily = wrapped("429 RESOURCE_EXHAUSTED quota GenerateRequestsPerDayPerProjectPerModel")
    assert isinstance(map_error(daily, "gemini", "g"), DailyLimitReached)
    minute = wrapped("429 RESOURCE_EXHAUSTED quota GenerateRequestsPerMinutePerProject")
    assert isinstance(map_error(minute, "gemini", "g"), TransientLLMError)


class FakeRunnable:
    def __init__(self, result: Any = None, error: Exception | None = None):
        self.result, self.error = result, error
        self.seen: list[Any] = []

    async def ainvoke(self, input: Any) -> Any:
        self.seen.append(input)
        if self.error is not None:
            raise self.error
        return self.result


def test_groq_provider_with_fake_runnable() -> None:
    ok = FakeRunnable(
        {
            "raw": AIMessage(
                content='{"echo": "x"}',
                usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            ),
            "parsed": {"echo": "x"},
            "parsing_error": None,
        }
    )
    schemas: list[dict[str, Any]] = []

    def factory(r: LLMRequest, schema: dict[str, Any]) -> FakeRunnable:
        schemas.append(schema)
        return ok

    provider = GroqProvider(SecretStr("k"), factory)
    resp = asyncio.run(provider.complete(req(provider="groq", model="openai/gpt-oss-120b")))
    assert resp.data == {"echo": "x"} and resp.input_tokens == 12 and resp.cost_usd == 0
    assert schemas[0]["title"] == "Echo"
    assert [m.type for m in ok.seen[0]] == ["system", "human"]
    unparsed = FakeRunnable({"raw": AIMessage(content="nope"), "parsed": None})
    with pytest.raises(InvalidLLMOutput):
        asyncio.run(GroqProvider(None, lambda r, s: unparsed).complete(req(provider="groq")))
    failing = FakeRunnable(error=FakeHTTPError(GROQ_TPD, 429))
    with pytest.raises(DailyLimitReached):
        asyncio.run(GroqProvider(None, lambda r, s: failing).complete(req(provider="groq")))
    assert groq_max_tokens("qwen/qwen3.8-27b", 2000) == 900
    assert groq_max_tokens("openai/gpt-oss-120b", 2000) == 2000


def test_gemini_provider_maps_transient() -> None:
    failing = FakeRunnable(error=FakeHTTPError("503 UNAVAILABLE", 503))
    with pytest.raises(TransientLLMError):
        asyncio.run(GeminiProvider(None, lambda r, s: failing).complete(req(provider="gemini")))


# --- Anthropic direct ------------------------------------------------------------------


def message(text: str, stop: str = "end_turn", inp: int = 100, out: int = 20) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeMessages:
    def __init__(self, stop: str = "end_turn"):
        self.calls: list[dict[str, Any]] = []
        self.stop = stop

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        echo = kwargs["messages"][-1]["content"]
        return message(json.dumps({"echo": echo}), self.stop)


def test_direct_request_building() -> None:
    params = build_params(req(model="claude-opus-5", effort="high"))
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert params["output_config"]["effort"] == "high"
    assert params["system"] == [
        {"type": "text", "text": "You answer in JSON.", "cache_control": {"type": "ephemeral"}}
    ]
    assert not {"thinking", "temperature", "top_p", "top_k"} & set(params)
    assert "effort" in build_params(req(model="claude-sonnet-5", effort="low"))["output_config"]
    haiku = build_params(req(model="claude-haiku-4-5", effort="high"))
    assert "effort" not in haiku["output_config"]
    assert "effort" not in build_params(req(model="claude-opus-5"))["output_config"]


def test_direct_complete_and_refusal(cache: ResponseCache) -> None:
    client = SimpleNamespace(messages=FakeMessages())
    provider = AnthropicDirectProvider(client, BudgetGuard(1.0, cache))
    resp = asyncio.run(provider.complete(req("hello")))
    assert resp.data == {"echo": "hello"}
    assert resp.cost_usd == pytest.approx((100 * 1.0 + 20 * 5.0) / 1e6)
    refusing = AnthropicDirectProvider(
        SimpleNamespace(messages=FakeMessages("refusal")), BudgetGuard(1.0, cache)
    )
    with pytest.raises(InvalidLLMOutput):
        asyncio.run(refusing.complete(req()))
    with pytest.raises(InvalidLLMOutput):
        parse_message(message("{}", "max_tokens"), "claude-haiku-4-5", batched=False)
    with pytest.raises(InvalidLLMOutput):
        parse_message(message("not json"), "claude-haiku-4-5", batched=False)


def test_anthropic_error_mapping() -> None:
    def response(status: int) -> Any:
        return SimpleNamespace(status_code=status, headers={"retry-after": "3"}, request=None)

    limited = anthropic.RateLimitError("slow down", response=response(429), body=None)
    mapped = map_anthropic_error(limited)
    assert isinstance(mapped, TransientLLMError) and mapped.retry_after == 3.0
    server = anthropic.InternalServerError("oops", response=response(500), body=None)
    assert isinstance(map_anthropic_error(server), TransientLLMError)
    conn = anthropic.APIConnectionError(message="reset", request=SimpleNamespace())
    assert isinstance(map_anthropic_error(conn), TransientLLMError)
    bad = anthropic.BadRequestError("bad", response=response(400), body=None)
    assert map_anthropic_error(bad) is bad


# --- pricing and budget ----------------------------------------------------------------


def test_pricing() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-opus-5", usage, batched=False) == pytest.approx(30.0)
    assert cost_usd("claude-opus-5", usage, batched=True) == pytest.approx(15.0)
    cached = Usage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5", cached, batched=False) == pytest.approx(0.2 + 2.5)
    assert price_of("claude-haiku-4-5-20251001") == (1.0, 5.0)
    with pytest.raises(ValueError):
        price_of("claude-unknown-9")


def _store(cache: ResponseCache, text: str, cost: float) -> None:
    resp = LLMResponse(data={}, raw_text="{}", input_tokens=0, output_tokens=0, cost_usd=cost)
    cache.put(request_key(req(text)), req(text), resp)


def test_budget_zero_blocks_everything(cache: ResponseCache) -> None:
    fake = FakeMessages()
    provider = AnthropicDirectProvider(SimpleNamespace(messages=fake), BudgetGuard(0.0, cache))
    with pytest.raises(BudgetExceeded):
        asyncio.run(provider.complete(req()))
    assert fake.calls == []


def test_budget_allows_until_spend_reaches_it(cache: ResponseCache) -> None:
    guard = BudgetGuard(1.00, cache)
    _store(cache, "a", 0.99)
    guard.check()
    fake = FakeMessages()
    provider = AnthropicDirectProvider(SimpleNamespace(messages=fake), guard)
    asyncio.run(provider.complete(req("allowed")))
    _store(cache, "b", 0.01)
    with pytest.raises(BudgetExceeded) as caught:
        asyncio.run(provider.complete(req("blocked")))
    assert caught.value.spent_usd == pytest.approx(1.00)
    assert len(fake.calls) == 1


# --- Anthropic batching ------------------------------------------------------------------


class FakeBatches:
    """Batches that end after one poll and return results in reverse order."""

    def __init__(self, cache: ResponseCache, fail: dict[str, int] | None = None):
        self.cache = cache
        self.created: list[tuple[str, list[dict[str, Any]]]] = []
        self.events: list[str] = []
        self.fail = dict(fail or {})  # echo text -> times to error (api_error)
        self.invalid: set[str] = set()
        self.preset: dict[str, list[tuple[str, str]]] = {}  # batch id -> [(custom_id, echo)]
        self.statuses_at_create: list[list[tuple[str, str]]] = []

    async def create(self, requests: list[dict[str, Any]]) -> Any:
        self.statuses_at_create.append(
            [(b, s) for b, s in self.cache.con.execute("SELECT batch_id, status FROM batches")]
        )
        batch_id = f"msgbatch_{len(self.created) + 1:03d}"
        self.created.append((batch_id, requests))
        self.events.append(f"create {batch_id}")
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    async def retrieve(self, batch_id: str) -> Any:
        self.events.append(f"retrieve {batch_id}")
        return SimpleNamespace(id=batch_id, processing_status="ended", request_counts=None)

    async def results(self, batch_id: str) -> AsyncIterator[Any]:
        self.events.append(f"results {batch_id}")
        if batch_id in self.preset:
            pairs = self.preset[batch_id]
        else:
            requests = dict(self.created)[batch_id]
            pairs = [(r["custom_id"], r["params"]["messages"][-1]["content"]) for r in requests]
        entries = [self._entry(cid, echo) for cid, echo in reversed(pairs)]

        async def gen() -> AsyncIterator[Any]:
            for entry in entries:
                yield entry

        return gen()

    def _entry(self, custom_id: str, echo: str) -> Any:
        if self.fail.get(echo, 0) > 0:
            self.fail[echo] -= 1
            error = SimpleNamespace(type="error", error=SimpleNamespace(type="api_error"))
            return SimpleNamespace(
                custom_id=custom_id, result=SimpleNamespace(type="errored", error=error)
            )
        if echo in self.invalid:
            error = SimpleNamespace(
                type="error", error=SimpleNamespace(type="invalid_request_error")
            )
            return SimpleNamespace(
                custom_id=custom_id, result=SimpleNamespace(type="errored", error=error)
            )
        result = SimpleNamespace(type="succeeded", message=message(json.dumps({"echo": echo})))
        return SimpleNamespace(custom_id=custom_id, result=result)


def batching(cache: ResponseCache, batches: FakeBatches, budget: float = 10.0) -> Any:
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    return BatchingAnthropicProvider(
        client, cache, BudgetGuard(budget, cache), idle_flush_s=0.05, poll_s=0.01
    )


def test_ten_concurrent_requests_become_one_batch(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    provider = batching(cache, batches)

    async def run() -> list[LLMResponse]:
        return await asyncio.gather(*(provider.complete(req(f"q{i}")) for i in range(10)))

    responses = asyncio.run(run())
    assert len(batches.created) == 1 and len(batches.created[0][1]) == 10
    assert [r.data["echo"] for r in responses] == [f"q{i}" for i in range(10)]
    assert all(r.batched for r in responses)
    assert responses[0].cost_usd == pytest.approx((100 * 1.0 + 20 * 5.0) / 1e6 * 0.5)
    custom_ids = [r["custom_id"] for r in batches.created[0][1]]
    assert custom_ids == [request_key(req(f"q{i}"))[:40] for i in range(10)]
    # recorded before submitting, renamed after
    assert batches.statuses_at_create[0][0][1] == "submitting"
    rows = cache.con.execute("SELECT batch_id, status, n_requests FROM batches").fetchall()
    assert rows == [("msgbatch_001", "ingested", 10)]
    assert cache.get(request_key(req("q3"))) is not None


def test_same_key_is_submitted_once(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    provider = batching(cache, batches)

    async def run() -> list[LLMResponse]:
        return await asyncio.gather(provider.complete(req("same")), provider.complete(req("same")))

    first, second = asyncio.run(run())
    assert first.data == second.data == {"echo": "same"}
    assert len(batches.created) == 1 and len(batches.created[0][1]) == 1
    again = asyncio.run(provider.complete(req("same")))
    assert again.cached and len(batches.created) == 1


def test_errored_result_is_requeued_once(cache: ResponseCache) -> None:
    batches = FakeBatches(cache, fail={"flaky": 1, "broken": 2})
    provider = batching(cache, batches)

    async def run() -> list[Any]:
        return await asyncio.gather(
            provider.complete(req("flaky")),
            provider.complete(req("broken")),
            provider.complete(req("fine")),
            return_exceptions=True,
        )

    flaky, broken, fine = asyncio.run(run())
    assert isinstance(flaky, LLMResponse) and flaky.data == {"echo": "flaky"}
    assert isinstance(fine, LLMResponse)
    assert isinstance(broken, TransientLLMError)
    assert [len(r) for _, r in batches.created] == [3, 2]


def test_invalid_request_becomes_invalid_output(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    batches.invalid.add("bad")
    provider = batching(cache, batches)
    with pytest.raises(InvalidLLMOutput):
        asyncio.run(provider.complete(req("bad")))
    assert len(batches.created) == 1


def test_restart_reattaches_before_submitting(cache: ResponseCache) -> None:
    old = req("from before the crash")
    key = request_key(old)
    from adsight.llm.types import canonical_json

    cache.record_batch("msgbatch_old", "in_progress", [(key[:40], key, canonical_json(old))])
    batches = FakeBatches(cache)
    batches.preset["msgbatch_old"] = [(key[:40], "from before the crash")]
    provider = batching(cache, batches)

    async def run() -> list[LLMResponse]:
        return await asyncio.gather(provider.complete(old), provider.complete(req("new")))

    resumed, new = asyncio.run(run())
    assert resumed.data == {"echo": "from before the crash"}
    assert new.data == {"echo": "new"}
    assert batches.events.index("results msgbatch_old") < batches.events.index(
        "create msgbatch_001"
    )
    assert [len(r) for _, r in batches.created] == [1]
    assert batches.created[0][1][0]["params"]["messages"][-1]["content"] == "new"
    statuses = dict(cache.con.execute("SELECT batch_id, status FROM batches").fetchall())
    assert statuses == {"msgbatch_old": "ingested", "msgbatch_001": "ingested"}


def test_restart_abandons_a_batch_that_never_got_an_id(cache: ResponseCache) -> None:
    cache.record_batch("local-dead", "submitting", [])
    batches = FakeBatches(cache)
    provider = batching(cache, batches)
    assert asyncio.run(provider.complete(req("x"))).data == {"echo": "x"}
    statuses = dict(cache.con.execute("SELECT batch_id, status FROM batches").fetchall())
    assert statuses["local-dead"] == "abandoned"


def test_batching_respects_the_budget(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    with pytest.raises(BudgetExceeded):
        asyncio.run(batching(cache, batches, budget=0.0).complete(req()))
    assert batches.created == []


# --- factory ------------------------------------------------------------------------------


def test_factory(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, state_dir=tmp_path / "state")
    assert parse_spec("groq:openai/gpt-oss-120b").model == "openai/gpt-oss-120b"
    assert parse_spec("anthropic:claude-opus-5").slug == "anthropic-claude-opus-5"
    with pytest.raises(ValueError):
        parse_spec("nope")
    with pytest.raises(ValueError):
        make_provider("fixture:scripted", settings)
    provider = make_provider("fixture:scripted", settings, script=[{"echo": "f"}])
    assert asyncio.run(provider.complete(req(provider="fixture"))).data == {"echo": "f"}
    assert (tmp_path / "state" / "llm_cache.sqlite").is_file()
    fake = SimpleNamespace(messages=FakeMessages())
    blocked = make_provider("anthropic:claude-haiku-4-5", settings, client=fake)
    with pytest.raises(BudgetExceeded):
        asyncio.run(blocked.complete(req()))
    for spec in ("groq:openai/gpt-oss-120b", "gemini:gemini-flash-lite-latest"):
        assert isinstance(make_provider(spec, settings), CachedProvider)
    batch = make_provider("anthropic:claude-haiku-4-5", settings, batch=True, client=fake)
    assert isinstance(batch.inner, LimitedProvider)
    assert isinstance(batch.inner.inner, BatchingAnthropicProvider)


# --- sharing requests across batch waves -------------------------------------------------


class HeldBatches(FakeBatches):
    """Batches stay in progress until `release` is set."""

    def __init__(self, cache: ResponseCache):
        super().__init__(cache)
        self.release = asyncio.Event()

    async def retrieve(self, batch_id: str) -> Any:
        self.events.append(f"retrieve {batch_id}")
        status = "ended" if self.release.is_set() else "in_progress"
        return SimpleNamespace(id=batch_id, processing_status=status, request_counts=None)


def stacked(cache: ResponseCache, batches: FakeBatches) -> CachedProvider:
    """The provider stack ProviderPool builds: cache, limits, then the batcher."""
    limited = LimitedProvider(batching(cache, batches), RateLimiter(BATCH_LIMITS))
    return CachedProvider(limited, cache)


def submitted_keys(batches: FakeBatches) -> list[str]:
    return [r["custom_id"] for _, reqs in batches.created for r in reqs]


def test_concurrent_identical_requests_share_one_item(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    provider = stacked(cache, batches)

    async def run() -> list[LLMResponse]:
        return await asyncio.gather(*(provider.complete(req("twin")) for _ in range(3)))

    responses = asyncio.run(run())
    assert submitted_keys(batches) == [request_key(req("twin"))[:40]]
    assert all(r.data == {"echo": "twin"} for r in responses)


def test_identical_request_joins_the_batch_in_flight(cache: ResponseCache) -> None:
    batches = HeldBatches(cache)
    provider = stacked(cache, batches)

    async def run() -> tuple[LLMResponse, LLMResponse]:
        first = asyncio.ensure_future(provider.complete(req("judge")))
        while not batches.created:  # wait until batch 1 is submitted and in flight
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # longer than the idle flush: a new queue would flush now
        second = asyncio.ensure_future(provider.complete(req("judge")))
        await asyncio.sleep(0.1)
        assert len(batches.created) == 1 and not second.done()
        batches.release.set()
        return await first, await second

    first, second = asyncio.run(run())
    assert first.data == second.data == {"echo": "judge"}
    assert len(batches.created) == 1 and len(submitted_keys(batches)) == 1
    assert cache.open_batches() == []


def test_item_cached_before_flush_is_not_submitted(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    provider = batching(cache, batches)
    request = req("cached meanwhile")

    async def run() -> LLMResponse:
        waiting = asyncio.ensure_future(provider.complete(request))
        await asyncio.sleep(0.01)  # queued, not yet flushed (idle flush is 0.05 s)
        stored = LLMResponse(
            data={"echo": "from the cache"}, raw_text="{}", input_tokens=1, output_tokens=1
        )
        cache.put(request_key(request), request, stored)
        return await waiting

    response = asyncio.run(run())
    assert response.data == {"echo": "from the cache"} and response.cost_usd == 0
    assert batches.created == []
    assert cache.open_batches() == []


def test_item_nobody_waits_for_is_not_submitted(cache: ResponseCache) -> None:
    batches = FakeBatches(cache)
    provider = batching(cache, batches)

    async def run() -> LLMResponse:
        gone = asyncio.ensure_future(provider.complete(req("abandoned")))
        await asyncio.sleep(0.01)  # queued, not yet flushed
        gone.cancel()
        return await provider.complete(req("wanted"))

    assert asyncio.run(run()).data == {"echo": "wanted"}
    assert submitted_keys(batches) == [request_key(req("wanted"))[:40]]


def test_drain_ingests_a_batch_nobody_waits_for(cache: ResponseCache) -> None:
    batches = HeldBatches(cache)
    provider = batching(cache, batches)

    async def run() -> None:
        caller = asyncio.ensure_future(provider.complete(req("late")))
        while not batches.created:
            await asyncio.sleep(0.01)
        caller.cancel()  # the batch is already paid for; its result must still be kept
        assert len(cache.open_batches()) == 1
        batches.release.set()
        await provider.drain()

    asyncio.run(run())
    assert cache.open_batches() == []
    assert cache.get(request_key(req("late"))) is not None
