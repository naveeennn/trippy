"""Tiny pub/sub bus (dashboard subscribes over WebSocket) and the simulated clock."""
import asyncio
from datetime import datetime, timedelta
from typing import Any


class EventBus:
    def __init__(self, keep_history: bool = True):
        self._subs: set[asyncio.Queue] = set()
        self.keep_history = keep_history
        self.history: list[dict[str, Any]] = []

    def publish(self, kind: str, **data: Any) -> None:
        record = {"kind": kind, **data}
        if self.keep_history:
            self.history.append(record)
        for q in self._subs:
            q.put_nowait(record)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


class SimClock:
    """Simulated wall clock, advanced one minute per world step."""

    def __init__(self, start: datetime):
        self.now = start

    def advance(self, minutes: int = 1) -> None:
        self.now += timedelta(minutes=minutes)

    def iso(self) -> str:
        return self.now.strftime("%H:%M")
