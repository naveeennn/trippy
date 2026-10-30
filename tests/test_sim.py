import asyncio
import json

from tripowner.sim.chaos import Chaos, demo_scenario, random_scenario
from tripowner.sim.eval import run_eval
from tripowner.sim.world import World

CALM = Chaos(cancel_prob=0, no_driver_prob=0, confusion_prob=0, pii_prob=0, stuck_prob=0,
             spike_prob=0, move_prob=0, cancel_meeting_prob=0)


def run(coro):
    return asyncio.run(coro)


def test_calm_world_is_always_on_time():
    for seed in range(40):
        r = run(World(random_scenario(seed, CALM), "agent").run())
        assert r.arrived and r.minutes_late <= 0, (seed, r)
        assert r.bookings == 1 and r.approvals_asked == 1


def test_demo_scenario_story():
    agent = run(World(demo_scenario(), "agent").run())
    base = run(World(demo_scenario(), "baseline").run())
    assert agent.bookings == 2 and agent.driver_cancels == 1      # first driver cancelled, rebooked
    assert agent.warned_5min_ahead and agent.pii_leaks == 0
    assert agent.minutes_late < base.minutes_late
    assert agent.user_actions < base.user_actions


def test_cancelled_meeting_wastes_no_ride():
    for seed in range(300):
        sc = random_scenario(seed)
        if sc.change and sc.change.cancelled:
            r = run(World(sc, "agent").run())
            assert not r.wasted_ride, seed


def test_eval_is_deterministic():
    a = run(run_eval(trips=15, seed=3))
    b = run(run_eval(trips=15, seed=3))
    assert json.dumps(a["summary"]) == json.dumps(b["summary"])
    assert a["summary"]["agent"]["pii_leaks"] == 0


def test_pool_driver_eval_is_deterministic_and_offline():
    a = run(run_eval(trips=10, seed=5, driver="pool"))
    b = run(run_eval(trips=10, seed=5, driver="pool"))
    assert json.dumps(a["summary"]) == json.dumps(b["summary"])
    assert a["driver"] == "pool"


def test_pool_driver_lines_are_reproducible():
    from tripowner.driver import PoolDriver, load_pool
    pool = load_pool()

    async def lines(seed):
        d = PoolDriver(pool, seed)
        return [await d.say(i, l) for i in ("where", "phone", "where") for l in ("kn-IN", "en-IN")]

    assert run(lines(3)) == run(lines(3))
    assert run(lines(3)) != run(lines(4))


def test_attendees_are_not_warned_too_early():
    from datetime import timedelta
    from tripowner.agent.tools import ALERT_WINDOW_MIN
    from tripowner.events import EventBus
    bus = EventBus()
    w = World(demo_scenario(), "agent", bus=bus)
    r = run(w.run())
    sent = [e for e in bus.history if e["kind"] == "agent_action" and e.get("action") == "notify_attendees"]
    assert sent and r.warned_5min_ahead
    assert w.ctx.meeting.start - w.ctx.first_notified_at <= timedelta(minutes=ALERT_WINDOW_MIN)


def late_driver_world(*etas, report=None):
    """Rides with the given pickup ETAs, in booking order (everything else calm)."""
    script = {i + 1: {"pickup_eta": eta, "match_delay": 1} for i, eta in enumerate(etas)}
    chaos = Chaos(cancel_prob=0, no_driver_prob=0, confusion_prob=0, pii_prob=0, stuck_prob=0,
                  spike_prob=0, move_prob=0, cancel_meeting_prob=0, script=script)
    return World(random_scenario(4, chaos), "agent")


def test_driver_more_than_10_min_away_is_replaced():
    r = run(late_driver_world(25, 6).run())
    assert r.bookings == 2 and r.arrived                                  # cancelled the 25-min driver, rebooked


def test_late_replacements_are_capped_so_drivers_dont_churn():
    r = run(late_driver_world(25, 25, 25, 25).run())
    assert r.bookings == 3                                                # replaced twice, then kept the third


def test_driver_saying_on_a_call_they_need_too_long_gets_replaced():
    w = late_driver_world(6, 5)
    orig = w.agent.handle
    told = []

    async def handle(ev, tb):
        if ev.kind == "ride_update" and ev.data["status"] == "assigned" and not told:
            told.append(True)
            w.report_driver_delay(30)                                     # "half an hour", on the phone
        await orig(ev, tb)
    w.agent.handle = handle
    r = run(w.run())
    assert r.bookings == 2
    w = late_driver_world(6, 5)
    w.report_driver_delay(8)                                              # within the limit: keeps the driver
    assert run(w.run()).bookings == 1
