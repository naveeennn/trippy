"""Watch mode: keep an eye on the real calendar and start trips by themselves.

Every POLL interval the watcher re-reads the iCal feed. When the next meeting it can route to is
close enough (LEAD minutes before the planned booking time), it starts a trip on the real clock and
the dashboard asks the user to approve the booking. While that trip runs, later polls compare the
meeting with the calendar: if the user moved or cancelled it, the change goes to the agent as a
`meeting_changed` event, the same way the simulator's reschedules do.

The ride itself is still simulated, and attendee emails are still only logged.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .planner import make_plan
from .providers.calendar import IST, IcsEvent, next_trip_event
from .providers.maps import PLACES, MockMaps

log = logging.getLogger(__name__)


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


@dataclass
class ActiveTrip:
    uid: str
    title: str
    start: datetime
    world: object              # sim.world.World; needs change_meeting()
    task: asyncio.Task | None = None


@dataclass
class CalendarWatcher:
    load: Callable[[], Awaitable[list[IcsEvent]]]
    start_trip: Callable[[IcsEvent, str], Awaitable[ActiveTrip | None]]
    origin: str
    poll_s: float = 120
    lead_min: int = 30
    publish: Callable[..., None] = lambda kind, **data: None
    enabled: bool = False
    status: str = "off"
    last_poll: datetime | None = None
    next_event: dict | None = None
    active: ActiveTrip | None = None
    handled: set[str] = field(default_factory=set)
    _task: asyncio.Task | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    def trigger_at(self, e: IcsEvent, place: str) -> datetime:
        """When to start owning the trip: LEAD minutes before the planned booking time."""
        plan = make_plan(e.start, MockMaps().predict_minutes(self.origin, place, e.start))
        return plan.book_at - timedelta(minutes=self.lead_min)

    def _set(self, status: str) -> None:
        self.status = status
        self.publish("watch", **self.snapshot())

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "last_poll": f"{self.last_poll:%H:%M}" if self.last_poll else None,
            "next": self.next_event,
            "active": None if self.active is None else
                {"title": self.active.title, "start": f"{self.active.start:%a %d %b, %H:%M}"},
        }

    async def poll(self, now: datetime | None = None, force: bool = False) -> None:
        """One check of the calendar. `force` starts the next trip right away (for demos)."""
        now = now or ist_now()
        self.last_poll = now
        try:
            events = await self.load()
        except Exception as e:
            self._set(f"couldn't read the calendar ({type(e).__name__}); will retry")
            return
        if self.active and self.active.task and self.active.task.done():
            self.handled.add(self.active.uid)
            self.active = None
        if self.active:
            self._follow(events, now)
            return
        upcoming = [e for e in events if e.uid not in self.handled]
        event, place, skipped = next_trip_event(upcoming, now, PLACES, self.origin)
        if event is None:
            self.next_event = None
            self._set("no upcoming meeting at a place the planner knows" + (f" ({skipped[0]})" if skipped else ""))
            return
        at = self.trigger_at(event, place)
        self.next_event = {"title": event.title, "start": f"{event.start:%a %d %b, %H:%M}", "to": place,
                           "trip_starts": f"{at:%a %d %b, %H:%M}"}
        if force or now >= at:
            self.active = await self.start_trip(event, place)
            if self.active is None:
                self._set("a demo is running; will start the trip when it ends")
            else:
                self._set(f"owning the trip to '{event.title}'")
        else:
            self._set(f"watching; next trip starts {at:%a %H:%M}")

    def _follow(self, events: list[IcsEvent], now: datetime) -> None:
        """Pass real calendar edits on to the running trip."""
        a = self.active
        current = next((e for e in events if e.uid == a.uid), None)
        if current is None:
            a.world.change_meeting(cancelled=True)
            self.handled.add(a.uid)
            self._set(f"'{a.title}' was cancelled in your calendar; told the agent")
        elif current.start != a.start:
            a.world.change_meeting(new_start=current.start)
            self._set(f"'{a.title}' moved {a.start:%H:%M} -> {current.start:%H:%M} in your calendar; told the agent")
            a.start = current.start
        else:
            self._set(f"owning the trip to '{a.title}'")

    async def _loop(self) -> None:
        while self.enabled:
            await self.poll()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_s)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self.enabled:
            return
        self.enabled = True
        self._task = asyncio.create_task(self._loop())
        self._set("starting")

    def stop(self) -> None:
        self.enabled = False
        if self._task:
            self._task.cancel()
        self._set("off")

    def check_now(self) -> None:
        self._wake.set()
