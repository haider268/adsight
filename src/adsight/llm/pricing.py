"""What an Anthropic call costs.

Prices in USD per million tokens. Source: Anthropic pricing, verified 2026-10-11.
Cache reads cost 0.1 x the input price, cache writes 1.25 x; the Batch API halves
everything. Groq and Gemini are used on their free tiers and cost 0.
"""

import re
from dataclasses import dataclass

PRICES_VERIFIED = "2026-10-11"
PRICES: dict[str, tuple[float, float]] = {  # model -> (input, output) per million tokens
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25
BATCH_FACTOR = 0.5
FREE_PROVIDERS = frozenset({"groq", "gemini", "fixture"})
_DATED = re.compile(r"^(?P<base>.+)-\d{8}$")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def price_of(model: str) -> tuple[float, float]:
    """The (input, output) price for a model id, also with a dated suffix
    (claude-haiku-4-5-20251001). An unknown model raises: never guess a price."""
    if model in PRICES:
        return PRICES[model]
    dated = _DATED.match(model)
    if dated and dated["base"] in PRICES:
        return PRICES[dated["base"]]
    raise ValueError(f"no price for model {model!r}; add it to PRICES with its source")


def cost_usd(model: str, usage: Usage, batched: bool) -> float:
    input_price, output_price = price_of(model)
    total = (
        usage.input_tokens * input_price
        + usage.output_tokens * output_price
        + usage.cache_read_tokens * input_price * CACHE_READ_FACTOR
        + usage.cache_write_tokens * input_price * CACHE_WRITE_FACTOR
    ) / 1_000_000
    return total * BATCH_FACTOR if batched else total
