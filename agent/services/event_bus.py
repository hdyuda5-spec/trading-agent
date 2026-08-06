"""Asynchronous event bus — the backbone of the orchestrator.

The bus decouples producers (market feed, portfolio, services) from consumers
(strategy eval, execution, notification). Every component emits typed
``Event`` objects; subscribers register by topic (``"*"`` = catch-all).

Design:

- ``EventBus.publish`` is async and awaits async handlers; sync handlers are
  supported too and invoked directly.
- ``EventBus.publish_sync`` is thread-safe and schedules onto the running
  event loop (used by blocking threads such as the Telegram poller); if no
  loop is running yet it dispatches synchronously (tests / bootstrap).
- Handlers never block each other: exceptions are logged per handler.
"""

import asyncio
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

logger = logging.getLogger("trading-agent")


@dataclass(frozen=True)
class Event:
    """Immutable bus message. ``data`` is a plain dict (JSON-friendly)."""

    topic: str
    data: dict
    ts: float = field(default_factory=time.time)
    source: str = "system"

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> dict:
        return {"topic": self.topic, "data": self.data, "ts": self.ts, "source": self.source}


Handler = Callable[[Event], Any]


class EventBus:
    """Thread-safe async pub/sub. Subscribers may be sync or async."""

    def __init__(self):
        self._handlers: Dict[str, List[Handler]] = {}
        self._wildcards: List[Handler] = []
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- registration ----------------------------------------------------

    def subscribe(self, topic: str, handler: Handler) -> None:
        if topic == "*":
            with self._lock:
                self._wildcards.append(handler)
            return
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            if topic == "*":
                self._wildcards[:] = [h for h in self._wildcards if h is not handler]
            else:
                bucket = self._handlers.get(topic) or []
                self._handlers[topic] = [h for h in bucket if h is not handler]

    def handlers_for(self, topic: str) -> List[Handler]:
        with self._lock:
            return list(self._handlers.get(topic, [])) + list(self._wildcards)

    def has_handlers(self, topic: str) -> bool:
        return bool(self.handlers_for(topic))

    # -- async dispatch --------------------------------------------------

    async def publish(self, event: Event) -> None:
        for handler in self.handlers_for(event.topic):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("event handler failed for topic=%s", event.topic)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the running loop so ``publish_sync`` can schedule onto it."""
        self._loop = loop

    def publish_sync(self, event: Event) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(event), loop)
        else:
            self._dispatch_sync(event)

    def _dispatch_sync(self, event: Event) -> None:
        for handler in self.handlers_for(event.topic):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    raise TypeError(
                        f"async handler for topic={event.topic} requires a running loop"
                    )
            except TypeError:
                raise
            except Exception:
                logger.exception("event handler failed for topic=%s", event.topic)

    # -- convenience factories -------------------------------------------

    @staticmethod
    def emit(topic: str, data: dict, source: str = "system") -> Event:
        return Event(topic=topic, data=data, source=source)
