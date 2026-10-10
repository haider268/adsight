"""Anthropic Messages API, one request at a time, with JSON-schema structured output.

Never sends temperature, top_p, top_k or thinking. The system prompt is one text block
marked for prompt caching, so the shared prefix is cached across questions.
"""

import json
import time
from typing import Any

from adsight.llm.base import InvalidLLMOutput, TransientLLMError
from adsight.llm.budget import BudgetGuard
from adsight.llm.limits import Limits
from adsight.llm.pricing import Usage, cost_usd
from adsight.llm.types import LLMRequest, LLMResponse

ANTHROPIC_LIMITS = Limits(requests_per_min=50, tokens_per_min=40000, concurrency=4)
EFFORT_MODEL_PREFIXES = ("claude-opus", "claude-sonnet")


def build_params(req: LLMRequest) -> dict[str, Any]:
    """The keyword arguments for messages.create (and a batch request's params)."""
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": req.schema_}}
    if req.effort is not None and req.model.startswith(EFFORT_MODEL_PREFIXES):
        output_config["effort"] = req.effort
    return {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": m.role, "content": m.text} for m in req.messages],
        "output_config": output_config,
    }


def _usage(message: Any) -> Usage:
    usage = getattr(message, "usage", None)
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def parse_message(message: Any, model: str, batched: bool, latency_ms: float = 0.0) -> LLMResponse:
    """Turn a Message into an LLMResponse; refusals, truncation and non-JSON raise
    InvalidLLMOutput."""
    text = next(
        (str(b.text) for b in getattr(message, "content", []) if getattr(b, "type", "") == "text"),
        "",
    )
    stop = getattr(message, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        raise InvalidLLMOutput(text, f"stop_reason {stop}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidLLMOutput(text, "not JSON") from exc
    if not isinstance(data, dict):
        raise InvalidLLMOutput(text, "not a JSON object")
    usage = _usage(message)
    return LLMResponse(
        data=data,
        raw_text=text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        latency_ms=latency_ms,
        batched=batched,
        cost_usd=cost_usd(model, usage, batched),
    )


def map_anthropic_error(exc: Exception) -> Exception:
    import anthropic

    retry_after = None
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            value = response.headers.get("retry-after")
            retry_after = float(value) if value is not None else None
        except (AttributeError, TypeError, ValueError):
            retry_after = None
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return TransientLLMError(f"{type(exc).__name__}: {exc}", retry_after)
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return TransientLLMError(f"server error {exc.status_code}: {exc}", retry_after)
    return exc


class AnthropicDirectProvider:
    def __init__(self, client: Any, guard: BudgetGuard):
        self.client, self.guard = client, guard

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        self.guard.check()
        started = time.perf_counter()
        try:
            message = await self.client.messages.create(**build_params(req))
        except Exception as exc:
            mapped = map_anthropic_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        latency_ms = (time.perf_counter() - started) * 1000
        return parse_message(message, req.model, batched=False, latency_ms=latency_ms)
