"""Structured JSON-lines logging on the stdlib logging module."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)

_ROOT = "adsight"
_HANDLER_TAG = "_adsight_handler"


def set_run_id(value: str | None) -> None:
    _run_id.set(value)


def get_run_id() -> str | None:
    return _run_id.get()


class JsonFormatter(logging.Formatter):
    """Formats each record as one JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": get_run_id(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", path: Path | None = None) -> None:
    """Send the adsight logger to stderr as JSON lines, and also to `path` when given.

    Safe to call repeatedly: handlers from earlier calls are replaced, not duplicated.
    """
    logger = logging.getLogger(_ROOT)
    reset_logging()

    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, mode="a", encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        logger.addHandler(handler)

    logger.setLevel(level.upper())
    logger.propagate = False


def adsight_handlers() -> list[logging.Handler]:
    """The handlers configure_logging installed; other handlers (e.g. pytest's) are ignored."""
    return [h for h in logging.getLogger(_ROOT).handlers if getattr(h, _HANDLER_TAG, False)]


def reset_logging() -> None:
    """Remove and close the handlers configure_logging installed."""
    logger = logging.getLogger(_ROOT)
    for handler in adsight_handlers():
        logger.removeHandler(handler)
        handler.close()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_ROOT}.{name}")
