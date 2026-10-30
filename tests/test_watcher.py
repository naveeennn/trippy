import asyncio
from datetime import datetime, timedelta

from tripowner.providers.calendar import IcsEvent
from tripowner.sim.chaos import calendar_scenario, demo_scenario
from tripowner.sim.world import World
from tripowner.watcher import ActiveTrip, CalendarWatcher

MEETING = datetime(2026, 10, 23, 10, 30)


class FakeWorld:
    def __init__(self):
        self.changes = []

    def change_meeting(self, new_start=None, cancelled=False, at=None):
        self.changes.append((new_start, cancelled))


def make_watcher(calendar):
    started, world = [], FakeWorld()

    async def load():
        return list(calendar)

    async def start_trip(event, place):
        started.append((event.title, place))
        return ActiveTrip(event.uid, event.title, event.start, world)

    w = CalendarWatcher(load=load, start_trip=start_trip, origin="Indiranagar", lead_min=30)
    return w, started, world


def ev(start=MEETING, uid="m1", location="WeWork, Koramangala"):
    return IcsEvent(uid, "Client demo", start, location, ["priya@example.com"])


def run(c):
    return asyncio.run(c)


def test_waits_until_lead_time_then_starts_once():
    cal = [ev()]
    w, started, _ = make_watcher(cal)
    at = w.trigger_at(cal[0], "Koramangala")
    run(w.poll(now=at - timedelta(minutes=5)))
    assert started == [] and "watching" in w.status and w.next_event["to"] == "Koramangala"
    run(w.poll(now=at))
    assert started == [("Client demo", "Koramangala")]
    run(w.poll(now=at + timedelta(minutes=2)))       # already owning it: no second trip
    assert len(started) == 1


def test_force_starts_immediately():
    w, started, _ = make_watcher([ev()])
    run(w.poll(now=MEETING - timedelta(hours=5), force=True))
    assert len(started) == 1


def test_real_calendar_edits_reach_the_running_trip():
    cal = [ev()]
    w, _, world = make_watcher(cal)
    run(w.poll(now=MEETING - timedelta(hours=1), force=True))
    cal[0] = ev(start=MEETING + timedelta(minutes=30))             # user moves the meeting
    run(w.poll(now=MEETING - timedelta(minutes=50)))
    assert world.changes == [(MEETING + timedelta(minutes=30), False)]
    cal.clear()                                                   # user deletes it
    run(w.poll(now=MEETING - timedelta(minutes=48)))
    assert world.changes[-1] == (None, True)


def test_skips_meetings_it_cannot_route():
    w, started, _ = make_watcher([ev(location="https://zoom.us/j/1")])
    run(w.poll(now=MEETING - timedelta(minutes=30), force=True))
    assert started == [] and "no upcoming meeting" in w.status


def test_world_applies_external_change():
    world = World(demo_scenario(), "agent")
    world.change_meeting(cancelled=True, at=world.sc.sim_start + timedelta(minutes=8))
    r = run(world.run())
    assert world.ctx.meeting.cancelled and not r.wasted_ride


def test_live_calendar_scenario_starts_now():
    now = datetime(2026, 10, 23, 8, 47, 31)
    sc = calendar_scenario(ev(), "Koramangala", "Indiranagar", start_at=now)
    assert sc.sim_start == datetime(2026, 10, 23, 8, 47)
