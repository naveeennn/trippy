"""Exercises the LLM tool-calling loop with a scripted fake model (no network)."""
import asyncio
import json

from tripowner.sim.chaos import demo_scenario
from tripowner.sim.world import World


class FakeLLM:
    """Behaves like a careless model: it tries to give the driver the rider's phone number."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, **_):
        self.calls += 1
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "done"}
        payload = json.loads(messages[-1]["content"])
        ev, state = payload["EVENT"], payload["STATE"]
        if ev["kind"] == "driver_message":
            text = ev["text_en"].lower()
            if "phone" in text:
                return self._call("message_driver", text="Sure! My number is +91 98450 12345")
            if "where" in text:
                return self._call("message_driver", text=f"I am at {state['pickup_landmark']}. Please come there.")
            return self._call("no_action", reason="nothing to say")
        if ev["kind"] == "ride_update" and ev["status"] == "cancelled":
            return self._call("request_ride", reason="driver cancelled")
        return self._call("no_action", reason="ok")

    def _call(self, name, **args):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": f"c{self.calls}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_llm_agent_rebooks_and_guardrail_blocks_phone_leak():
    llm = FakeLLM()
    world = World(demo_scenario(), "llm", llm=llm)
    r = asyncio.run(world.run())
    assert llm.calls > 0
    assert r.bookings == 2                 # LLM rebooked after the driver cancelled
    assert r.guardrail_blocks == 1         # the leak attempt was caught...
    assert r.pii_leaks == 0                # ...and never reached the driver
    assert r.arrived


class BrokenLLM:
    async def chat(self, *a, **k):
        raise RuntimeError("endpoint down")


def test_llm_failure_falls_back_to_rules():
    r = asyncio.run(World(demo_scenario(), "llm", llm=BrokenLLM()).run())
    assert r.arrived and r.bookings == 2 and r.pii_leaks == 0


class ChattyLLM:
    """Answers in prose and never calls a tool."""

    def __init__(self):
        self.calls = 0

    async def chat(self, *a, **k):
        self.calls += 1
        return {"role": "assistant", "content": "I would tell the driver where the rider is."}


def test_llm_without_tool_call_falls_back_to_rules():
    llm = ChattyLLM()
    world = World(demo_scenario(), "llm", llm=llm)
    r = asyncio.run(world.run())
    assert llm.calls > 0
    # The driver's "where are you?" still gets answered, so they don't cancel as unreachable.
    assert r.arrived and r.bookings == 2 and r.pii_leaks == 0
    assert any("Gate 2" in m["text_en"] for m in world.rides.outbox)


class CancelHappyLLM(FakeLLM):
    """Cancels the ride whenever the meeting changes, like Nemotron did once when a meeting moved earlier."""

    async def chat(self, messages, tools=None, **_):
        if messages[-1]["role"] != "tool":
            ev = json.loads(messages[-1]["content"])["EVENT"]
            if ev["kind"] == "meeting_changed" and not ev["cancelled"]:
                self.calls += 1
                return self._call("cancel_ride", reason="meeting moved")
        return await super().chat(messages, tools)


def test_guardrail_keeps_ride_when_meeting_moves_earlier():
    from tripowner.events import EventBus
    from tripowner.sim.chaos import random_scenario
    sc = random_scenario(109)                       # meeting moves 15 min earlier while a ride is on its way
    assert sc.change and sc.change.new_start < sc.meeting.start
    bus = EventBus()
    r = asyncio.run(World(sc, "llm", llm=CancelHappyLLM(), bus=bus).run())
    blocked = [e for e in bus.history if e["kind"] == "guardrail" and e.get("rule") == "cancel_ride"]
    assert blocked, "the cancel should have been blocked"
    assert r.arrived


def test_llm_client_retries_when_nemotron_is_busy(monkeypatch):
    """Token Factory sometimes says 529 (overloaded); a live call should retry, not fail."""
    import httpx
    from tripowner import llm as llm_mod
    from tripowner.llm import LLMClient
    statuses = [529, 503, 200]

    def fake(req):
        code = statuses.pop(0)
        body = {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}} if code == 200 else {}
        return httpx.Response(code, json=body)

    monkeypatch.setattr(llm_mod, "RETRY_WAITS_S", (0, 0))
    c = LLMClient()
    c._client = httpx.AsyncClient(base_url="https://llm.test/", transport=httpx.MockTransport(fake))
    assert asyncio.run(c.chat([{"role": "user", "content": "x"}]))["content"] == "hi" and not statuses
