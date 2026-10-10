"""Requests and responses every provider speaks."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Effort = Literal["low", "medium", "high"]


class Msg(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    text: str


class LLMRequest(BaseModel):
    """One call that must return ONE JSON object matching `schema` (structured output).

    The schema is stored as `schema_` (pydantic reserves `schema`) but is passed and
    serialised as `schema`.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    provider: str
    model: str
    system: str
    messages: list[Msg]
    schema_: dict[str, Any] = Field(alias="schema")
    schema_name: str
    max_tokens: int
    effort: Effort | None = None


class LLMResponse(BaseModel):
    data: dict[str, Any]
    raw_text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    batched: bool = False
    cost_usd: float = 0.0


def canonical_json(req: LLMRequest) -> str:
    return json.dumps(
        req.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":")
    )


def request_key(req: LLMRequest, salt: str = "") -> str:
    """sha256 of the request's canonical JSON (sorted keys) plus `salt`."""
    return hashlib.sha256((canonical_json(req) + salt).encode("utf-8")).hexdigest()
