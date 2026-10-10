"""Google Gemini (free tier) through langchain_google_genai with JSON-schema output.

The model id comes from configuration (the CLI default is "gemini-flash-lite-latest").
"""

from typing import Any

from pydantic import SecretStr

from adsight.llm.limits import Limits
from adsight.llm.providers.langchain_chat import (
    RunnableFactory,
    StructuredRunnable,
    structured_call,
)
from adsight.llm.types import LLMRequest, LLMResponse

GEMINI_LIMITS = Limits(requests_per_min=15, tokens_per_min=250000, concurrency=2)


class GeminiProvider:
    def __init__(self, api_key: SecretStr | None, factory: RunnableFactory | None = None):
        self.api_key = api_key
        self.factory = factory or self._chat

    def _chat(self, req: LLMRequest, schema: dict[str, Any]) -> StructuredRunnable:
        from langchain_google_genai import ChatGoogleGenerativeAI

        if self.api_key is None:
            raise RuntimeError("GOOGLE_API_KEY is not set")
        chat = ChatGoogleGenerativeAI(
            model=req.model,
            google_api_key=self.api_key,
            max_output_tokens=req.max_tokens,
            max_retries=0,
        )
        runnable: StructuredRunnable = chat.with_structured_output(
            schema, method="json_schema", include_raw=True
        )
        return runnable

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        return await structured_call(req, self.factory, "gemini")
