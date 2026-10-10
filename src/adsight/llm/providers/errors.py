"""Map vendor exceptions to the LLM layer's errors, by status code and message.

Works on any exception shape (LangChain wrappers, vendor SDKs) by reading status_code /
code / status attributes along the exception's cause chain.
"""

import re
from collections.abc import Iterator

from adsight.llm.base import DailyLimitReached, TransientLLMError

DAILY_MARKERS = (
    "per day",
    "perday",
    "(tpd)",
    "(rpd)",
    "daily",
)
_TRY_AGAIN = re.compile(r"try again in ([0-9][0-9hms.]*[0-9hms])", re.IGNORECASE)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        yield node
        node = node.__cause__ or node.__context__


def _status(exc: BaseException) -> int | None:
    for node in _chain(exc):
        for attr in ("status_code", "code", "status"):
            value = getattr(node, attr, None)
            if isinstance(value, int) and 100 <= value < 600:
                return value
        value = getattr(getattr(node, "response", None), "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _retry_after(exc: BaseException) -> float | None:
    for node in _chain(exc):
        headers = getattr(getattr(node, "response", None), "headers", None)
        if headers is not None:
            value = headers.get("retry-after")
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _is_timeout_or_connection(exc: BaseException) -> bool:
    for node in _chain(exc):
        if isinstance(node, (TimeoutError, ConnectionError)):
            return True
        name = type(node).__name__
        if "Timeout" in name or "Connection" in name:
            return True
    return False


def map_error(exc: BaseException, provider: str, model: str) -> BaseException:
    """The error to raise for `exc`: DailyLimitReached, TransientLLMError, or `exc` itself
    when it is not retryable (a bad request, a missing key)."""
    text = " ".join(str(node) for node in _chain(exc))
    lowered = text.lower()
    status = _status(exc)
    if status == 429 or "resource_exhausted" in lowered or "rate limit" in lowered:
        if any(marker in lowered for marker in DAILY_MARKERS):
            match = _TRY_AGAIN.search(text)
            hint = f"try again in {match.group(1)}" if match else "after the daily reset"
            return DailyLimitReached(provider, model, hint)
        return TransientLLMError(f"rate limited: {text[:200]}", _retry_after(exc))
    if status is not None and status >= 500:
        return TransientLLMError(f"server error {status}: {text[:200]}", _retry_after(exc))
    if _is_timeout_or_connection(exc):
        return TransientLLMError(f"{type(exc).__name__}: {text[:200]}")
    return exc
