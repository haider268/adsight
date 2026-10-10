"""Build a ready provider from a "<provider>:<model>" spec.

Every provider comes wrapped the same way: CachedProvider(LimitedProvider(inner)), so a
cache hit never touches the rate limiter and a re-run never pays twice.
"""

from dataclasses import dataclass
from typing import Any, Literal

from adsight.config import Settings
from adsight.llm.base import LLMProvider
from adsight.llm.budget import BudgetGuard
from adsight.llm.cache import CachedProvider, ResponseCache
from adsight.llm.limits import LimitedProvider, Limits, RateLimiter
from adsight.llm.providers.fixture import Script, ScriptedProvider

PROVIDERS = ("groq", "gemini", "anthropic", "fixture")
FIXTURE_LIMITS = Limits(requests_per_min=1_000_000, tokens_per_min=1e12, concurrency=64)


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    @property
    def slug(self) -> str:
        return f"{self.provider}-{self.model}".replace("/", "-").replace(":", "-")


def parse_spec(spec: str) -> ModelSpec:
    provider, sep, model = spec.partition(":")
    if not sep or not model or provider not in PROVIDERS:
        raise ValueError(f"model spec must be <provider>:<model> with provider in {PROVIDERS}")
    return ModelSpec(provider, model)


def make_provider(
    spec: str,
    settings: Settings,
    batch: bool = False,
    *,
    mode: Literal["normal", "replay"] = "normal",
    script: Script | None = None,
    client: Any = None,
    cache: ResponseCache | None = None,
    wait_s: float | None = None,
    batch_options: dict[str, float] | None = None,
) -> CachedProvider:
    """The wrapped provider for `spec`. `script` feeds fixture:*; `client` replaces the
    Anthropic SDK client (tests); `cache` defaults to Settings.llm_cache_path. `wait_s`
    (tick mode) and `batch_options` (idle_flush_s, poll_s) apply to Anthropic batching."""
    parsed = parse_spec(spec)
    cache = cache or ResponseCache(settings.llm_cache_path)
    inner: LLMProvider
    limits: Limits
    if parsed.provider == "fixture":
        if script is None:
            raise ValueError("the fixture provider needs a script")
        inner, limits = ScriptedProvider(script), FIXTURE_LIMITS
    elif parsed.provider == "groq":
        from adsight.llm.providers.groq import GROQ_LIMITS, GroqProvider

        inner, limits = GroqProvider(settings.groq_api_key), GROQ_LIMITS
    elif parsed.provider == "gemini":
        from adsight.llm.providers.gemini import GEMINI_LIMITS, GeminiProvider

        inner, limits = GeminiProvider(settings.google_api_key), GEMINI_LIMITS
    else:
        from adsight.llm.providers.anthropic_batch import BATCH_LIMITS, BatchingAnthropicProvider
        from adsight.llm.providers.anthropic_direct import (
            ANTHROPIC_LIMITS,
            AnthropicDirectProvider,
        )

        guard = BudgetGuard(settings.anthropic_budget_usd, cache)
        if client is None:
            import anthropic

            key = settings.anthropic_api_key
            client = anthropic.AsyncAnthropic(
                api_key=key.get_secret_value() if key else None, max_retries=0
            )
        if batch:
            options = batch_options or {}
            inner = BatchingAnthropicProvider(
                client,
                cache,
                guard,
                idle_flush_s=options.get("idle_flush_s", 3.0),
                poll_s=options.get("poll_s", 30.0),
                wait_s=wait_s,
            )
            limits = BATCH_LIMITS
        else:
            inner, limits = AnthropicDirectProvider(client, guard), ANTHROPIC_LIMITS
    return CachedProvider(LimitedProvider(inner, RateLimiter(limits)), cache, mode)


class ProviderPool:
    """One wrapped provider per vendor, shared by every model spec of that vendor (the
    model travels in each request), so agent and judge calls share caches, limits,
    budget and Anthropic batch waves."""

    def __init__(
        self,
        settings: Settings,
        batch: bool = False,
        *,
        mode: Literal["normal", "replay"] = "normal",
        script: Script | None = None,
        client: Any = None,
        cache: ResponseCache | None = None,
        wait_s: float | None = None,
        batch_options: dict[str, float] | None = None,
    ):
        self.settings, self.batch, self.mode = settings, batch, mode
        self.script, self.client = script, client
        self.wait_s, self.batch_options = wait_s, batch_options
        self.cache = cache or ResponseCache(settings.llm_cache_path)
        self.providers: dict[tuple[str, bool], CachedProvider] = {}

    def get(self, spec: str, batch: bool | None = None) -> CachedProvider:
        """The shared provider for this spec's vendor (and batch flag; default the pool's)."""
        vendor = parse_spec(spec).provider
        use_batch = self.batch if batch is None else batch
        key = (vendor, use_batch and vendor == "anthropic")
        if key not in self.providers:
            self.providers[key] = make_provider(
                spec,
                self.settings,
                use_batch,
                mode=self.mode,
                script=self.script,
                client=self.client,
                cache=self.cache,
                wait_s=self.wait_s,
                batch_options=self.batch_options,
            )
        return self.providers[key]

    def batchers(self) -> list[Any]:
        """The Anthropic batching providers this pool has built."""
        from adsight.llm.providers.anthropic_batch import BatchingAnthropicProvider

        found = []
        for provider in self.providers.values():
            inner = getattr(provider.inner, "inner", None)
            if isinstance(inner, BatchingAnthropicProvider):
                found.append(inner)
        return found

    @property
    def hits(self) -> int:
        return sum(p.hits for p in self.providers.values())

    def close(self) -> None:
        self.cache.close()
