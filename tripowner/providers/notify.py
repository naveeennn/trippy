"""Notifying meeting attendees. Swap LogNotifier for Gmail / Slack."""
from abc import ABC, abstractmethod

from ..models import Meeting


class Notifier(ABC):
    @abstractmethod
    async def notify_attendees(self, meeting: Meeting, message: str) -> None: ...


class LogNotifier(Notifier):
    def __init__(self, bus, clock):
        self.bus = bus
        self.clock = clock
        self.sent: list[dict] = []

    async def notify_attendees(self, meeting: Meeting, message: str) -> None:
        self.sent.append({"at": self.clock.now, "to": meeting.attendees, "message": message})
        self.bus.publish("notify", ts=self.clock.iso(), to=meeting.attendees, message=message)
