"""A scripted provider: a real implementation for tests and the offline demo."""

import json
from collections.abc import Callable
from typing import Any

from adsight.llm.types import LLMRequest, LLMResponse

Script = list[dict[str, Any] | BaseException] | Callable[[LLMRequest], dict[str, Any]]


class ScriptedProvider:
    """Returns scripted JSON objects in order (or from a function of the request).

    A list entry that is an exception is raised instead, so tests can script failures.
    Token counts are len(text) // 4.
    """

    def __init__(self, responses: Script):
        self.responses = responses
        self.calls = 0
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, salt: str = "") -> LLMResponse:
        self.requests.append(req)
        item: dict[str, Any] | BaseException
        if callable(self.responses):
            item = self.responses(req)
        else:
            if self.calls >= len(self.responses):
                raise RuntimeError(f"script exhausted after {self.calls} responses")
            item = self.responses[self.calls]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        raw = json.dumps(item)
        prompt = req.system + "".join(m.text for m in req.messages)
        return LLMResponse(
            data=item,
            raw_text=raw,
            input_tokens=len(prompt) // 4,
            output_tokens=len(raw) // 4,
        )
