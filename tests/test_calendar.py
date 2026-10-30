import asyncio
from datetime import datetime
from pathlib import Path

from tripowner.providers.calendar import load_ics, match_place, next_trip_event, parse_ics
from tripowner.providers.maps import PLACES
from tripowner.sim.chaos import calendar_scenario
from tripowner.sim.world import World

ICS = Path(__file__).parent / "fixtures" / "sample.ics"
NOW = datetime(2026, 10, 22, 9, 0)


def events():
    return parse_ics(ICS.read_text(encoding="utf-8"))


def test_parse_skips_all_day_and_cancelled_and_converts_to_india_time():
    evs = events()
    titles = [e.title for e in evs]
    assert "Holiday" not in titles and "Cancelled review" not in titles
    client = next(e for e in evs if e.uid == "client-1@google.com")
    assert client.start == datetime(2026, 10, 23, 10, 30)        # 05:00 UTC -> 10:30 IST
    assert client.title == "Client demo, round 2"                 # escaped comma
    assert "Koramangala" in client.location                       # folded line rejoined
    assert client.attendees == ["priya@example.com", "rahul@example.com"]
    tz = next(e for e in evs if e.uid == "tzid-1@google.com")
    assert tz.start == datetime(2026, 10, 23, 16, 0)


def test_next_trip_skips_past_and_unroutable_events():
    event, place, skipped = next_trip_event(events(), NOW, PLACES, "Indiranagar")
    assert event.title == "Client demo, round 2" and place == "Koramangala"
    assert any("Online sync" in s for s in skipped)              # a Zoom link isn't a place


def test_match_place():
    assert match_place("Prestige Tech Park, Marathahalli, Bengaluru", PLACES) == "Marathahalli"
    assert match_place("https://meet.google.com/abc", PLACES) is None


def test_trip_to_a_real_calendar_event_runs():
    event, place, _ = next_trip_event(events(), NOW, PLACES, "Indiranagar")
    sc = calendar_scenario(event, place, "Indiranagar")
    r = asyncio.run(World(sc, "agent").run())
    assert r.arrived and r.pii_leaks == 0
    assert sc.meeting.title == "Client demo, round 2" and sc.meeting.start == datetime(2026, 10, 23, 10, 30)


def test_load_from_local_path():
    assert len(asyncio.run(load_ics(str(ICS)))) == len(events())
