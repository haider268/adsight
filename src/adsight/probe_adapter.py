"""convoprobe adapter: `convoprobe run ... --adapter adsight.probe_adapter:agent`.

convoprobe calls `agent(history)` with its list of messages (objects with `.role`, "user"
or "agent", and `.text`). convoprobe is never imported here: any objects with those two
attributes work. The model comes from ADSIGHT_CHAT_MODEL, read on the first call.
"""

import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from adsight.chat import ChatSession

DEFAULT_CHAT_MODEL = "groq:openai/gpt-oss-120b"

Agent = Callable[[Sequence[Any]], Awaitable[str]]


def make_agent(
    model_spec: str | None = None, session_factory: Callable[[str], ChatSession] = ChatSession
) -> Agent:
    """An async `agent(history) -> str` backed by one ChatSession, built on the first call."""
    session: ChatSession | None = None

    async def agent(history: Sequence[Any]) -> str:
        nonlocal session
        if session is None:
            spec = model_spec or os.environ.get("ADSIGHT_CHAT_MODEL") or DEFAULT_CHAT_MODEL
            session = session_factory(spec)
        turns = [(str(m.role), str(m.text)) for m in history]
        return await session.reply(turns)

    return agent


agent = make_agent()
