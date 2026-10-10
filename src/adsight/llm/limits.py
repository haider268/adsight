"""Per-(provider, model) rate limits and retries with exponential backoff and full jitter."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from adsight.llm.base import LLMProvider, TransientLLMError
from adsight.llm.types import LLMRequest, LLMResponse
from adsight.log import get_logger

T = TypeVar("T")
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

RETRY_BASE_S = 2.0
RETRY_CAP_S = 60.0
RETRY_ATTEMPTS = 5

log = get_logger("llm.limits")


@dataclass(frozen=True)
class Limits:
    requests_per_min: float
    tokens_per_min: float
    concurrency: int


class TokenBucket:
    """A bucket of `capacity` units, refilled continuously at `capacity` per minute."""

    def __init__(self, per_minute: float, clock: Clock):
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self.level = per_minute
        self.clock = clock
        self.stamp = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.level = min(self.capacity, self.level + (now - self.stamp) * self.rate)
        self.stamp = now

    def wait_for(self, amount: float) -> float:
        """Seconds until `amount` units are available (0 when they are now)."""
        self._refill()
        amount = min(amount, self.capacity)
        return 0.0 if self.level >= amount else (amount - self.level) / self.rate

    def take(self, amount: float) -> None:
        self._refill()
        self.level -= min(amount, self.capacity)


class RateLimiter:
    """Requests/min and tokens/min buckets plus a concurrency semaphore."""

    def __init__(self, limits: Limits, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep):
        self.limits = limits
        self.requests = TokenBucket(limits.requests_per_min, clock)
        self.tokens = TokenBucket(limits.tokens_per_min, clock)
        self.sleep = sleep
        self._lock: asyncio.Lock | None = None
        self._slots: asyncio.Semaphore | None = None

    async def acquire(self, tokens: float) -> None:
        """Wait until one request and `tokens` tokens fit, then take them."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                delay = max(self.requests.wait_for(1), self.tokens.wait_for(tokens))
                if delay <= 0:
                    break
                await self.sleep(delay)
            self.requests.take(1)
            self.tokens.take(tokens)

    def slots(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.limits.concurrency)
        return self._slots


def backoff_delay(attempt: int, rng: random.Random, retry_after: float | None = None) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**attempt)); a retry-after hint wins."""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, RETRY_CAP_S)
    return rng.uniform(0.0, min(RETRY_CAP_S, RETRY_BASE_S * 2**attempt))


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    attempts: int = RETRY_ATTEMPTS,
) -> T:
    """Call `fn`, retrying TransientLLMError, at most `attempts` calls in total."""
    rng = rng or random.Random()
    for attempt in range(attempts):
        try:
            return await fn()
        except TransientLLMError as exc:
            if attempt == attempts - 1:
                raise
            delay = backoff_delay(attempt, rng, exc.retry_after)
            fields = {"attempt": attempt + 1, "delay_s": round(delay, 3), "error": str(exc)}
            log.info("transient error; retrying", extra={"fields": fields})
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def estimate_tokens(req: LLMRequest) -> int:
    """A cheap estimate: prompt characters / 4 plus the output allowance."""
    chars = len(req.system) + sum(len(m.text) for m in req.messages)
    return chars // 4 + req.max_tokens


class LimitedProvider:
    """Rate-limits and retries calls to `inner`."""

    def __init__(
        self,
        inner: LLMProvider,
        limiter: RateLimiter,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.inner, self.limiter, self.sleep = inner, limiter, sleep
        self.rng = rng or random.Random()

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        async def attempt() -> LLMResponse:
            async with self.limiter.slots():
                await self.limiter.acquire(estimate_tokens(req))
                return await self.inner.complete(req, salt)

        return await with_retries(attempt, sleep=self.sleep, rng=self.rng)
