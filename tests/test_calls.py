import asyncio

from tripowner.agent.tools import call_summary
from tripowner.events import EventBus
from tripowner.sim.chaos import demo_scenario
from tripowner.sim.world import World


def run_demo(kind="agent", takeover=False):
    bus = EventBus()
    w = World(demo_scenario(), kind, bus=bus)
    if takeover:
        orig = w.tb.start_call

        async def start_then_take_over(*a, **k):
            await orig(*a, **k)
            w.tb.take_over_call()
        w.tb.start_call = start_then_take_over
    r = asyncio.run(w.run())
    return w, r, bus.history


def test_assistant_answers_the_drivers_call_and_says_who_it_is():
    w, r, hist = run_demo()
    starts = [e for e in hist if e["kind"] == "call_start"]
    spoken = [e for e in hist if e["kind"] == "chat" and e.get("spoken")]
    assert starts and starts[0]["driver"] == "Manjunath"
    assert "assistant" in spoken[0]["text_en"] and spoken[0]["sender"] == "agent"      # discloses first
    agent_lines = [e["text_en"] for e in spoken if e["sender"] == "agent"]
    assert any("Gate 2" in t for t in agent_lines)                                  # gave the landmark
    assert any("call back this way" in t for t in agent_lines) and r.pii_leaks == 0
    ends = [e for e in hist if e["kind"] == "call_end"]
    assert ends and "pickup point" in ends[0]["summary"] and "number private" in ends[0]["summary"]
    assert r.arrived


def test_take_over_stops_the_assistant_speaking():
    w, r, hist = run_demo(takeover=True)
    assert any(e["kind"] == "call_takeover" for e in hist)
    suggestions = [e for e in hist if e["kind"] == "chat" and e.get("sender") == "suggestion"]
    assert suggestions                                              # replies shown, not said
    said_after = [m for m in w.rides.outbox if "Gate 2" in m["text_en"]]
    assert not said_after                                           # nothing sent to the driver


def test_typical_rider_gets_no_assistant_on_the_call():
    _, _, hist = run_demo("baseline")
    assert not any(e["kind"] == "call_start" for e in hist)


def test_summary_wording():
    s = call_summary("Ravi", [("driver", "Where are you?"), ("agent", "I am at Gate 2, MG Road Metro Station.")], False)
    assert s == "Ravi couldn't find you; assistant gave the pickup point."
