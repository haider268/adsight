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
) -> CachedProvider:
    """The wrapped provider for `spec`. `script` feeds fixture:*; `client` replaces the
    Anthropic SDK client (tests); `cache` defaults to Settings.llm_cache_path."""
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
            inner, limits = BatchingAnthropicProvider(client, cache, guard), BATCH_LIMITS
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
    ):
        self.settings, self.batch, self.mode = settings, batch, mode
        self.script, self.client = script, client
        self.cache = cache or ResponseCache(settings.llm_cache_path)
        self.providers: dict[str, CachedProvider] = {}

    def get(self, spec: str) -> CachedProvider:
        vendor = parse_spec(spec).provider
        if vendor not in self.providers:
            self.providers[vendor] = make_provider(
                spec,
                self.settings,
                self.batch,
                mode=self.mode,
                script=self.script,
                client=self.client,
                cache=self.cache,
            )
        return self.providers[vendor]

    @property
    def hits(self) -> int:
        return sum(p.hits for p in self.providers.values())

    def close(self) -> None:
        self.cache.close()
