"""The shared structured-output call for LangChain chat models (Groq, Gemini)."""

import time
from collections.abc import Callable
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from adsight.llm.base import InvalidLLMOutput
from adsight.llm.providers.errors import map_error
from adsight.llm.types import LLMRequest, LLMResponse


class StructuredRunnable(Protocol):
    async def ainvoke(self, input: list[BaseMessage]) -> Any: ...


# (request, schema with a title) -> a runnable returning {"raw", "parsed", "parsing_error"}
RunnableFactory = Callable[[LLMRequest, dict[str, Any]], StructuredRunnable]


def to_messages(req: LLMRequest) -> list[BaseMessage]:
    out: list[BaseMessage] = [SystemMessage(content=req.system)]
    for m in req.messages:
        out.append(HumanMessage(content=m.text) if m.role == "user" else AIMessage(content=m.text))
    return out


async def structured_call(req: LLMRequest, factory: RunnableFactory, provider: str) -> LLMResponse:
    schema = {"title": req.schema_name, **req.schema_}
    started = time.perf_counter()
    try:
        result = await factory(req, schema).ainvoke(to_messages(req))
    except Exception as exc:
        mapped = map_error(exc, provider, req.model)
        if mapped is exc:
            raise
        raise mapped from exc
    latency_ms = (time.perf_counter() - started) * 1000
    raw = result.get("raw") if isinstance(result, dict) else None
    parsed = result.get("parsed") if isinstance(result, dict) else None
    content = getattr(raw, "content", "")
    raw_text = content if isinstance(content, str) else str(content)
    if not isinstance(parsed, dict):
        raise InvalidLLMOutput(raw_text, "no JSON object matching the schema")
    usage = getattr(raw, "usage_metadata", None) or {}
    return LLMResponse(
        data=parsed,
        raw_text=raw_text,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        latency_ms=latency_ms,
    )
