"""Nemotron-driven agent.

Routine ticks and ETA updates are handled by RuleAgent (no point burning tokens every
simulated minute). Anything that needs judgement -- a driver talking, a cancellation,
a calendar change -- goes to the LLM with tools. If the LLM errors, RuleAgent takes over.
"""
import json
import logging

from ..models import Event
from .policies import RuleAgent
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SPECS, Toolbox

log = logging.getLogger(__name__)
MAX_ROUNDS = 4


class LLMAgent(RuleAgent):
    name = "agent+llm"

    def __init__(self, llm):
        self.llm = llm
        self.calls = 0
        self.failures = 0

    def _needs_judgement(self, ev: Event, tb: Toolbox) -> bool:
        if ev.kind in ("driver_message", "meeting_changed"):
            return True
        if ev.kind == "ride_update":
            r = tb.ctx.ride
            return (r is not None and ev.data["ride_id"] == r.id
                    and ev.data["status"] == "cancelled"
                    and ev.data.get("reason") != "rider_cancelled")
        return False

    async def handle(self, ev: Event, tb: Toolbox) -> None:
        if tb.ctx.done or tb.ctx.declined:
            return
        if self._needs_judgement(ev, tb):
            try:
                if await self._think(ev, tb):
                    return
                # The model answered in prose without calling a tool; let the rules act.
                self.failures += 1
                tb.bus.publish("agent_error", ts=tb.clock.iso(), error="LLM made no tool call; used rules")
            except Exception as e:
                self.failures += 1
                log.warning("LLM agent failed, falling back to rules: %s", e)
                tb.bus.publish("agent_error", ts=tb.clock.iso(), error=str(e)[:300])
        await super().handle(ev, tb)

    async def on_switch_opportunity(self, tb: Toolbox, best, why: str, late: int) -> None:
        """Let Nemotron decide whether to leave cabs behind. Asked again only if things get worse."""
        ctx = tb.ctx
        key = (ctx.cab_failures, late)
        prev = ctx.switch_considered
        if prev and ctx.cab_failures <= prev[0] and late < prev[1] + 5:
            return
        ctx.switch_considered = key
        ev = Event("cab_trouble", tb.now, {"why": why, "cab_failures": ctx.cab_failures,
                                            "projected_minutes_late_by_cab": late})
        try:
            if await self._think(ev, tb):
                return
        except Exception as e:
            self.failures += 1
            log.warning("LLM agent failed on cab trouble, falling back to rules: %s", e)
        await super().on_switch_opportunity(tb, best, why, late)

    async def _think(self, ev: Event, tb: Toolbox) -> bool:
        """-> False if the model never called a tool, so the caller can fall back to rules."""
        event = {"kind": ev.kind, **{k: v for k, v in ev.data.items() if not k.startswith("_")}}
        if ev.kind == "driver_message":
            event.pop("text", None)  # the agent works in English; native text stays in the voice layer
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(name=tb.ctx.profile.name)},
            {"role": "user", "content": json.dumps({"EVENT": event, "STATE": await tb.state()}, default=str)},
        ]
        for round_no in range(MAX_ROUNDS):
            self.calls += 1
            msg = await self.llm.chat(messages, tools=TOOL_SPECS)
            if msg.get("content"):
                tb.bus.publish("agent_thought", ts=tb.clock.iso(), text=msg["content"][:500])
            calls = msg.get("tool_calls") or []
            if not calls:
                return round_no > 0
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await tb.call(fn["name"], args)
                messages.append({"role": "tool", "tool_call_id": c.get("id", fn["name"]),
                                 "content": json.dumps(result, default=str)})
            if all(c["function"]["name"] == "no_action" for c in calls):
                return True
        return True
