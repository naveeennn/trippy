import asyncio
import json
import random
from datetime import datetime

from tripowner.providers.approval import ApprovalProvider
from tripowner.providers.maps import MockMaps
from tripowner.providers.transit import TransitOptions
from tripowner.sim.chaos import cabs_fail_scenario, demo_scenario
from tripowner.sim.world import World

NOW = datetime(2026, 10, 15, 9, 30)


def run(c):
    return asyncio.run(c)


def test_metro_only_where_there_are_stations():
    t = TransitOptions(MockMaps(), random.Random(1))
    assert t.metro("Indiranagar", "Whitefield", NOW) is not None           # same line
    j = t.metro("Indiranagar", "Jayanagar", NOW)
    assert j and "Majestic" in j.steps                                     # one change
    assert t.metro("Indiranagar", "Koramangala", NOW) is None              # no station
    assert t.bike_taxi("Indiranagar", "Koramangala", NOW, 240) is not None
    assert TransitOptions(MockMaps(), random.Random(1), allow_bike=False).bike_taxi(
        "Indiranagar", "Koramangala", NOW, 240) is None


def test_cabs_keep_failing_agent_takes_the_metro():
    agent = run(World(cabs_fail_scenario(), "agent").run())
    base = run(World(cabs_fail_scenario(), "baseline").run())
    assert agent.mode == "metro" and agent.arrived and agent.minutes_late <= 0
    assert base.mode == "cab" and base.minutes_late > agent.minutes_late   # a typical rider keeps trying cabs


def test_stage_demo_stays_in_a_cab():
    assert run(World(demo_scenario(), "agent").run()).mode == "cab"       # one cancellation isn't enough


class Decline(ApprovalProvider):
    async def request(self, summary, details):
        return "mode" not in details                                       # yes to the cab, no to switching


def test_rider_can_say_no_and_is_not_asked_again():
    w = World(cabs_fail_scenario(), "agent", approval=Decline())
    r = run(w.run())
    assert r.mode == "cab" and w.ctx.switch_declined
    assert w.ctx.approvals_asked == 2                                      # booking + one switch offer


def test_guardrail_blocks_a_switch_that_saves_nothing():
    w = World(demo_scenario(), "agent")                                    # Indiranagar -> Koramangala
    async def go():
        w.tb.ctx.plan = await w.tb.refresh_plan()
        return await w.tb.switch_mode("metro", "testing")
    assert "error" in run(go())                                            # no metro to Koramangala at all


class SwitchingLLM:
    def __init__(self, choice):
        self.choice, self.events = choice, []

    async def chat(self, messages, tools=None, **_):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "done"}
        ev = json.loads(messages[-1]["content"])["EVENT"]
        self.events.append(ev["kind"])
        if ev["kind"] == "cab_trouble":
            args = {"mode": "metro", "reason": "cabs failing"} if self.choice else {"reason": "wait"}
            name = "switch_mode" if self.choice else "no_action"
        elif ev["kind"] == "ride_update":
            name, args = "request_ride", {"reason": "rebook"}
        else:
            name, args = "no_action", {"reason": "ok"}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_nemotron_decides_the_switch():
    yes = SwitchingLLM(True)
    assert run(World(cabs_fail_scenario(), "llm", llm=yes).run()).mode == "metro"
    assert "cab_trouble" in yes.events
    no = SwitchingLLM(False)
    assert run(World(cabs_fail_scenario(), "llm", llm=no).run()).mode == "cab"   # its no_action is respected
