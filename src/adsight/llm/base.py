"""The provider interface and the errors every provider maps its failures to."""

from typing import Protocol

from adsight.llm.types import LLMRequest, LLMResponse


class LLMProvider(Protocol):
    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        """Answer `req`. `salt` only changes the cache key (a deliberate fresh attempt)."""
        ...


class LLMError(Exception):
    """Base class for the errors below."""


class TransientLLMError(LLMError):
    """Retry later (rate limit, overload, timeout, connection drop)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class DailyLimitReached(LLMError):
    """A daily quota is spent: stop the run cleanly and resume after the reset."""

    def __init__(self, provider: str, model: str, reset_hint: str):
        super().__init__(f"{provider}:{model} daily limit reached ({reset_hint})")
        self.provider, self.model, self.reset_hint = provider, model, reset_hint


class BudgetExceeded(LLMError):
    def __init__(self, spent_usd: float, budget_usd: float):
        super().__init__(f"Anthropic budget spent: {spent_usd:.4f} of {budget_usd:.4f} USD")
        self.spent_usd, self.budget_usd = spent_usd, budget_usd


class InvalidLLMOutput(LLMError):
    """The model answered, but not with a JSON object matching the schema."""

    def __init__(self, raw_text: str, reason: str = "invalid output"):
        super().__init__(f"{reason}: {raw_text[:200]}")
        self.raw_text = raw_text


class CacheMiss(LLMError):
    """Replay mode found no stored response for this request."""

    def __init__(self, key: str):
        super().__init__(f"no cached response for {key}")
        self.key = key
