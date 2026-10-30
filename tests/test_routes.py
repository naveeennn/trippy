import asyncio
from datetime import datetime, time

from tripowner.events import EventBus
from tripowner.providers.maps import MockMaps
from tripowner.providers.routes import (AutoChoose, DashboardRouteChoice, Route, Segment, _open_at, is_night,
                                        recommend, route_options, score_route)
from tripowner.sim.chaos import demo_scenario, night_stop_scenario
from tripowner.sim.world import World

NIGHT = datetime(2026, 10, 23, 23, 0)


def test_night_window_and_late_closing_shops():
    assert is_night(NIGHT) and is_night(datetime(2026, 10, 23, 2, 0)) and not is_night(datetime(2026, 10, 23, 18, 0))
    assert _open_at(time(1, 0), NIGHT)                   # closes at 1 AM: still open at 11 PM
    assert not _open_at(time(22, 0), NIGHT)
    assert _open_at(None, datetime(2026, 10, 23, 3, 0))  # all-night pharmacy


def test_spread_beats_bunched():
    open_late = [None]
    bunched = Route("bunched", "", 3.0, [Segment("arterial", open_late * 3, 0, True)] * 2 +
                    [Segment("arterial", [], 0, True)] * 4)
    spread = Route("spread", "", 3.0, [Segment("arterial", open_late, 0, True),
                                       Segment("arterial", [], 0, True)] * 3)
    b, s = score_route(bunched, NIGHT, MockMaps()), score_route(spread, NIGHT, MockMaps())
    assert b.shops_open > s.shops_open                   # more shops overall...
    assert b.longest_gap_km == 2.0 and s.longest_gap_km == 0.5
    assert s.score > b.score                             # ...but the spread-out route scores higher


def test_options_have_badges_and_recommendation_limits_extra_time():
    opts = route_options("Indiranagar", "Whitefield", NIGHT, MockMaps())
    badges = {b for o in opts for b in o.badges}
    assert {"fastest", "shortest", "busiest at night"} <= badges
    fastest = min(opts, key=lambda o: o.minutes)
    assert recommend(opts).minutes - fastest.minutes <= 10
    assert recommend(opts, max_extra_min=0).name == fastest.name


def test_night_trip_picks_a_route_and_tells_the_driver():
    bus = EventBus()
    w = World(night_stop_scenario(), "agent", bus=bus, route_chooser=AutoChoose())
    asyncio.run(w.run())
    assert w.ctx.route and w.ctx.route["name"] == "Old Airport Road"
    told = [m["text_en"] for m in w.rides.outbox if "Old Airport Road" in m["text_en"]]
    assert told and w.rides.route_km == w.ctx.route["km"]


def test_daytime_trip_skips_route_choice():
    w = World(demo_scenario(), "agent", route_chooser=AutoChoose())
    asyncio.run(w.run())
    assert w.ctx.route is None


def test_dashboard_choice_and_timeout():
    async def go():
        c = DashboardRouteChoice(EventBus(), timeout_s=0.05)
        opts = route_options("Indiranagar", "Whitefield", NIGHT, MockMaps())
        rec = recommend(opts)
        timed_out = await c.choose(opts, rec)
        task = asyncio.ensure_future(c.choose(opts, rec))
        await asyncio.sleep(0)
        assert not c.resolve("R2", "Nonexistent Road")
        assert c.resolve("R2", "Outer Ring Road")
        return timed_out, await task
    timed_out, picked = asyncio.run(go())
    assert timed_out == ("Old Airport Road", "recommended (no answer)")
    assert picked == ("Outer Ring Road", "you chose")
