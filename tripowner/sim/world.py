"""Discrete-time world: one step = one simulated minute.

Each step: the ride provider advances, scheduled calendar changes fire, then a tick.
Every event goes to the agent, which acts through its Toolbox.
"""
import asyncio
import copy
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from ..agent import BaselineHuman, LLMAgent, RuleAgent
from ..agent.guardrails import Guardrails
from ..agent.tools import Toolbox, TripContext
from ..config import settings
from ..driver import ScriptedDriver
from ..events import EventBus, SimClock
from ..models import Event, RideStatus
from ..providers.approval import AutoApprove
from ..providers.calendar import MockCalendar
from ..providers.maps import MockMaps
from ..providers.notify import LogNotifier
from ..providers.rides import MockRideProvider
from ..providers.transit import TransitOptions
from ..providers.voice import MockVoice
from .chaos import Scenario

GIVE_UP_AFTER_MIN = 120


@dataclass
class TripResult:
    seed: int
    agent: str
    origin: str
    dest: str
    meeting_start: str
    meeting_cancelled: bool
    arrived: bool
    minutes_late: int | None
    warned_5min_ahead: bool
    false_alarm: bool
    bookings: int
    driver_cancels: int
    approvals_asked: int
    user_actions: int
    pii_leaks: int
    guardrail_blocks: int
    wasted_ride: bool
    mode: str = "cab"                 # how the rider finally got there

    def as_dict(self) -> dict:
        return asdict(self)


class World:
    def __init__(self, scenario: Scenario, agent_kind: str = "agent", *, bus: EventBus | None = None,
                 voice=None, driver=None, approval=None, llm=None, allow_bike: bool | None = None,
                 real_email: bool = False, safety_prompt=None, alerter=None, locations=None,
                 drill: bool = True, route_chooser=None, real_routes: bool = False, real_rides: bool = False,
                 updates=None, ride_backend: str = "app"):
        self.sc = scenario
        self.agent_kind = agent_kind
        self.live = bus is not None
        self.bus = bus or EventBus(keep_history=False)
        self.clock = SimClock(scenario.sim_start)
        self.maps = MockMaps(scenario.spikes)
        meeting = copy.deepcopy(scenario.meeting)      # runs mutate the meeting
        self.calendar = MockCalendar([meeting])
        from ..providers.beckn import BecknRides, beckn_enabled
        from ..providers.handoff import TelegramHandoff, handoff_enabled
        from .beckn_bpp import MockNammaYatri
        from ..providers.uber import UberGuestRides, uber_enabled
        if real_rides and uber_enabled():
            self.rides = UberGuestRides(self.clock, self.maps)       # real Uber (sandbox or production)
        elif real_rides and handoff_enabled():
            self.rides = TelegramHandoff(self.clock, self.maps)      # one-tap Uber/Ola links on Telegram
        elif real_rides and beckn_enabled():
            self.rides = BecknRides(self.clock, self.maps, publish=self._beckn_log)   # Namma Yatri on ONDC
        elif ride_backend == "beckn":
            # Same chaos simulator, but TripOwner books and tracks it over Beckn through a mock Namma Yatri.
            bpp = MockNammaYatri(MockRideProvider(self.maps, random.Random(scenario.seed),
                                                  scenario.chaos, self.clock, driver or ScriptedDriver()))
            self.rides = bpp.bap = BecknRides(self.clock, self.maps, transport=bpp.transport, bpp_sim=bpp,
                                              publish=self._beckn_log)
        else:
            self.rides = MockRideProvider(self.maps, random.Random(scenario.seed),
                                          scenario.chaos, self.clock, driver or ScriptedDriver())
        self.voice = voice or MockVoice()
        self.guardrails = Guardrails(scenario.profile, settings.spend_cap_inr, settings.max_bookings_per_trip)
        self.ctx = TripContext(profile=scenario.profile, meeting=meeting)
        if agent_kind == "baseline":
            self.agent = BaselineHuman(scenario.seed)
        elif agent_kind == "llm":
            if llm is None:
                raise ValueError("llm agent needs an LLM client (set LLM_BASE_URL)")
            self.agent = LLMAgent(llm)
        else:
            self.agent = RuleAgent()
        self.tb = Toolbox(
            self.ctx, clock=self.clock, maps=self.maps, rides=self.rides, voice=self.voice,
            notifier=self._make_notifier(real_email, scenario.profile.name), approval=approval or AutoApprove(),
            guardrails=self.guardrails, bus=self.bus, require_approval=agent_kind != "baseline",
            # Separate random stream, so adding transit doesn't change what the ride simulation draws.
            transit=TransitOptions(self.maps, random.Random(scenario.seed * 31 + 7),
                                   settings.allow_bike_taxi if allow_bike is None else allow_bike),
            safety_prompt=safety_prompt, alerter=alerter, locations=locations, drill=drill,
            route_chooser=route_chooser, real_routes=real_routes,
            # Sending a booking link spends nothing, so one-tap mode never waits for dashboard approval.
            updates=updates, auto_book=real_rides and (settings.auto_book or getattr(self.rides, "handoff", False)),
        )
        self.tb.checkin_timeout_s = settings.safety_checkin_timeout_s
        self._change_applied = False
        self._external_changes: list[tuple[datetime | None, datetime | None, bool]] = []
        self._external_events: list[Event] = []

    def _beckn_log(self, **msg) -> None:
        if self.live:
            self.bus.publish("beckn", ts=self.clock.iso(), **msg)

    def _make_notifier(self, real_email: bool, rider: str):
        """Live trips can send real email; simulations and evals only ever log."""
        from ..providers.email import EmailNotifier, email_enabled
        if real_email and email_enabled():
            return EmailNotifier(self.bus, self.clock, rider)
        return LogNotifier(self.bus, self.clock)

    def change_meeting(self, new_start: datetime | None = None, cancelled: bool = False,
                       at: datetime | None = None) -> None:
        """A meeting change from outside the simulation, e.g. the user edited the real calendar.
        Applied at the next step (or once the clock reaches `at`)."""
        self._external_changes.append((at, new_start, cancelled))

    def report_driver_delay(self, minutes: int) -> None:
        """The driver told TripOwner on a real phone call how long they'll take. Handled at the next step."""
        r = self.ctx.ride
        self._external_events.append(Event("driver_delay", self.clock.now,
                                           {"minutes": minutes, "ride_id": r.id if r else None}))

    def _apply_change(self, new_start: datetime | None, cancelled: bool, events: list) -> None:
        m = self.ctx.meeting
        old = m.start
        self.calendar.apply_change(m.id, new_start, cancelled)
        data = {"cancelled": m.cancelled, "old_start": f"{old:%H:%M}", "new_start": f"{m.start:%H:%M}"}
        self.bus.publish("meeting", ts=self.clock.iso(), **data)
        events.append(Event("meeting_changed", self.clock.now, data))

    async def run(self, sec_per_min: float = 0.0) -> TripResult:
        ctx, m = self.ctx, self.ctx.meeting
        self.bus.publish("run_started", agent=self.agent.name, scenario=self.sc.summary(),
                         voice=self.voice.name, ts=self.clock.iso())
        cancelled_at: datetime | None = None
        while True:
            events = await self.rides.step()
            ch = self.sc.change
            if ch and not self._change_applied and self.clock.now >= ch.at:
                self._change_applied = True
                self._apply_change(ch.new_start, ch.cancelled, events)
            for item in [c for c in self._external_changes if c[0] is None or self.clock.now >= c[0]]:
                self._external_changes.remove(item)
                self._apply_change(item[1], item[2], events)
            events.extend(self._external_events)
            self._external_events.clear()
            events.append(Event("tick", self.clock.now))

            for ev in events:
                await self._dispatch(ev)
                if ctx.done:
                    break
            if ctx.mode != "cab" and ctx.mode_done_at and self.clock.now >= ctx.mode_done_at and not ctx.done:
                ctx.arrived_at, ctx.done = self.clock.now, True
                self.bus.publish("mode_arrived", ts=self.clock.iso(), mode=ctx.mode)
            if self.live:
                self.bus.publish("state", **await self.tb.state())

            if ctx.done or ctx.declined:
                break
            if m.cancelled:
                cancelled_at = cancelled_at or self.clock.now
                if self.clock.now >= cancelled_at + timedelta(minutes=10):
                    break
            if self.clock.now >= m.start + timedelta(minutes=GIVE_UP_AFTER_MIN):
                break
            self.clock.advance(1)
            if sec_per_min:
                await asyncio.sleep(sec_per_min)

        result = self._result()
        self.bus.publish("result", **result.as_dict())
        return result

    async def _dispatch(self, ev: Event) -> None:
        if ev.kind == "ride_update":
            self.bus.publish("ride", ts=self.clock.iso(), **ev.data)
            r = self.ctx.ride
            failed = ev.data["status"] == "no_drivers" or (
                ev.data["status"] == "cancelled" and ev.data.get("reason") != "rider_cancelled")
            if r and ev.data["ride_id"] == r.id and failed:
                self.ctx.cab_failures += 1
            if r and ev.data["ride_id"] == r.id and ev.data["status"] == RideStatus.COMPLETED.value:
                self.ctx.arrived_at = self.clock.now
                self.ctx.done = True
                return
        elif ev.kind == "driver_message":
            ev.data["_intent"] = ev.data.pop("intent")   # ground truth; only the baseline "human" uses it
            ev.data["text_en"] = await self.voice.to_english(ev.data["text"], ev.data["lang"])
            call = ev.data.get("channel") == "call" and self.agent_kind != "baseline"
            r = self.ctx.ride
            if call and not self.tb.on_call(ev.data["ride_id"]):
                await self.tb.start_call(ev.data["ride_id"], r.driver.name if r and r.driver else "Driver",
                                         ev.data["lang"])
            audio = await self.voice.synthesize(ev.data["text"], ev.data["lang"], force=call, role="driver")                 if self.live else None
            self.bus.publish("chat", ts=self.clock.iso(), sender="driver", text=ev.data["text"],
                             text_en=ev.data["text_en"], lang=ev.data["lang"], audio=audio, spoken=call)
            if call:
                self.ctx.call["notes"].append(("driver", ev.data["text_en"]))
        await self.agent.handle(ev, self.tb)
        if ev.kind == "driver_message" and ev.data.get("_intent") in ("coming", "arrived") \
                and self.tb.on_call(ev.data["ride_id"]):
            self.tb.end_call("driver is on the way" if ev.data["_intent"] == "coming" else "driver arrived")
        if ev.kind == "ride_update" and ev.data["status"] in ("in_trip", "cancelled", "completed") \
                and self.tb.on_call(ev.data["ride_id"]):
            self.tb.end_call(ev.data["status"])

    def _result(self) -> TripResult:
        ctx, m = self.ctx, self.ctx.meeting
        late = None
        if ctx.arrived_at and not m.cancelled:
            late = round((ctx.arrived_at - m.start).total_seconds() / 60)
        warned = (late is not None and late >= 3 and ctx.first_notified_at is not None
                  and ctx.first_notified_at <= m.start - timedelta(minutes=5))
        leaks = sum(1 for msg in self.rides.outbox if self.guardrails.detect_pii(msg["text_en"]))
        wasted = m.cancelled and (ctx.arrived_at is not None or bool(ctx.ride and ctx.ride.active))
        return TripResult(
            seed=self.sc.seed,
            agent=self.agent.name,
            origin=self.sc.profile.location,
            dest=m.location,
            meeting_start=f"{m.start:%H:%M}",
            meeting_cancelled=m.cancelled,
            arrived=ctx.arrived_at is not None,
            minutes_late=late,
            warned_5min_ahead=warned,
            false_alarm=ctx.notified_late is not None and late is not None and late <= 0,
            bookings=ctx.bookings,
            driver_cancels=self.rides.driver_cancels,
            approvals_asked=ctx.approvals_asked,
            user_actions=ctx.user_actions,
            pii_leaks=leaks,
            guardrail_blocks=len(self.guardrails.blocks),
            wasted_ride=wasted,
            mode=ctx.mode,
        )
