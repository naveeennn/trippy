"""Decision policies.

RuleAgent      -- the deterministic agent. Fast, free, used for 100-trip evals and as the
                  LLM agent's fallback and its handler for routine ticks.
BaselineHuman  -- a careful-but-busy human using the normal app. The comparison point.
"""
import random
from abc import ABC, abstractmethod
from datetime import timedelta

from ..i18n import render
from ..models import Event, RideStatus
from ..planner import ARRIVAL_BUFFER_MIN, PICKUP_ALLOWANCE_MIN
from .tools import (CHECKIN_AFTER_MIN, LATE_THRESHOLD_MIN, MAX_LATE_REPLACEMENTS, MAX_PICKUP_WAIT_MIN,
                    NO_DRIVER_BACKOFF_MIN, SWITCH_AFTER_FAILURES, SWITCH_AFTER_LATE_MIN, SWITCH_GAIN_MIN, Toolbox)

STALL_MIN = 3  # pickup ETA hasn't improved for this long => driver isn't really coming


def late_message(tb: Toolbox, minutes: int, eta) -> str:
    m = tb.ctx.meeting
    return (f"Hi, {tb.ctx.profile.name} is running about {minutes} min late for '{m.title}' "
            f"(ETA {eta:%H:%M}). Sorry! — sent by their assistant")


class Agent(ABC):
    name = "agent"

    @abstractmethod
    async def handle(self, ev: Event, tb: Toolbox) -> None: ...


class RuleAgent(Agent):
    name = "agent"

    async def handle(self, ev: Event, tb: Toolbox) -> None:
        if tb.ctx.done or tb.ctx.declined:
            return
        handler = {
            "tick": self.on_tick,
            "ride_update": self.on_ride_update,
            "driver_message": self.on_driver_message,
            "meeting_changed": self.on_meeting_changed,
            "driver_delay": self.on_driver_delay,
        }.get(ev.kind)
        if handler:
            await handler(ev, tb)

    async def on_tick(self, ev: Event, tb: Toolbox) -> None:
        ctx = tb.ctx
        if ctx.meeting.cancelled:
            return
        if ctx.mode != "cab":           # rider is on the metro / a bike taxi; just keep attendees posted
            await self.check_lateness(tb)
            return
        await self.consider_switch(tb)
        if ctx.mode != "cab":
            return
        if not (ctx.ride and ctx.ride.active):
            plan = await tb.refresh_plan()   # re-plan every minute with live traffic
            backing_off = ctx.retry_after is not None and tb.now < ctx.retry_after
            if tb.now >= plan.book_at and not backing_off:
                if ctx.bookings == 0:
                    await tb.choose_route()          # night trips: pick the route before booking
                await tb.request_ride("planned departure" if ctx.bookings == 0 else "rebook")
        await self.check_lateness(tb)

    async def on_ride_update(self, ev: Event, tb: Toolbox) -> None:
        ctx, r = tb.ctx, tb.ctx.ride
        if not r or ev.data["ride_id"] != r.id:
            return
        status = ev.data["status"]
        if status == RideStatus.NO_DRIVERS.value:
            # Nobody nearby right now; hammering retry won't help. on_tick retries after the backoff.
            ctx.retry_after = tb.now + timedelta(minutes=NO_DRIVER_BACKOFF_MIN)
        elif status == RideStatus.CANCELLED.value:
            if ev.data.get("reason") != "rider_cancelled":
                await tb.request_ride(f"rebook after {ev.data.get('reason')}")
        elif status == RideStatus.IN_TRIP.value:
            await self.watch_trip(tb, ev.data.get("km_left"))
        elif status == RideStatus.ASSIGNED.value:
            await tb.tell_driver_route()
            eta = ev.data["pickup_eta"]
            if await self.replace_if_too_late(tb, eta, f"driver is {eta} min from the pickup"):
                return
            best, since = ctx.eta_progress.get(r.id, (eta + 1, tb.now))
            if eta < best:
                ctx.eta_progress[r.id] = (eta, tb.now)
                return
            stalled_min = (tb.now - since).total_seconds() / 60
            # A fresh ride costs ~PICKUP_ALLOWANCE_MIN; only swap when it's clearly better
            # and the arrival buffer is mostly gone.
            if (stalled_min >= STALL_MIN and eta >= PICKUP_ALLOWANCE_MIN - 2
                    and await tb.minutes_late() > -ARRIVAL_BUFFER_MIN // 2):
                await tb.cancel_ride(f"driver hasn't moved closer in {stalled_min:.0f} min (ETA {eta} min)")
                await tb.request_ride("replace stalled driver")

    async def replace_if_too_late(self, tb: Toolbox, minutes: int, why: str) -> bool:
        """More than MAX_PICKUP_WAIT_MIN away: cancel and book another cab (a fresh one is usually 3-9 min)."""
        ctx = tb.ctx
        if minutes <= MAX_PICKUP_WAIT_MIN or ctx.late_replacements >= MAX_LATE_REPLACEMENTS:
            return False
        ctx.late_replacements += 1
        await tb.cancel_ride(f"{why}, over the {MAX_PICKUP_WAIT_MIN} min limit")
        await tb.request_ride("replace late driver")
        return True

    async def on_driver_delay(self, ev: Event, tb: Toolbox) -> None:
        """The driver said on the phone how long they'll take (TripOwner answered the call)."""
        r = tb.ctx.ride
        if r and r.active and r.id == ev.data.get("ride_id", r.id) and r.status == RideStatus.ASSIGNED:
            await self.replace_if_too_late(tb, ev.data["minutes"], f"driver said on the phone they need {ev.data['minutes']} min")

    async def watch_trip(self, tb: Toolbox, km_left) -> None:
        """Rider is in the cab. If it stops covering distance, check on them (safety, not punctuality).
        Distance, not ETA: in a jam the ETA can stall while the cab is still crawling forward."""
        ctx = tb.ctx
        ctx.trip_started_at = ctx.trip_started_at or tb.now
        if km_left is None:
            return
        best, since = ctx.trip_progress or (km_left + 1, tb.now)
        if km_left < best - 0.01:
            ctx.trip_progress = (km_left, tb.now)
            return
        ctx.trip_progress = (best, since)
        stalled = (tb.now - since).total_seconds() / 60
        if stalled >= CHECKIN_AFTER_MIN and not ctx.safety_checked:
            await tb.safety_checkin(f"Your cab hasn't moved for {stalled:.0f} min")

    async def on_driver_message(self, ev: Event, tb: Toolbox) -> None:
        reply = self.compose_reply(ev.data["text_en"], tb.ctx.profile.pickup_landmark,
                                   on_call=ev.data.get("channel") == "call")
        if reply:
            await tb.message_driver(reply)

    @staticmethod
    def compose_reply(text_en: str, landmark: str, on_call: bool = False) -> str | None:
        t = text_en.lower()
        if any(w in t for w in ("number", "phone", "contact")):
            return render("agent.no_phone_call" if on_call else "agent.no_phone", "en-IN")
        if any(w in t for w in ("where", "location", "address", "find")):
            return render("agent.pickup", "en-IN", landmark=landmark)
        if any(w in t for w in ("arrived", "reached", "waiting", "gate")):
            return render("agent.coming_out", "en-IN")
        if "coming" in t:
            return None
        return render("agent.ok", "en-IN")

    async def on_meeting_changed(self, ev: Event, tb: Toolbox) -> None:
        ctx, r = tb.ctx, tb.ctx.ride
        if ctx.meeting.cancelled:
            if r and r.active:
                await tb.cancel_ride("meeting cancelled")
            return
        plan = await tb.refresh_plan()
        ctx.notified_late = None
        if r and r.status in (RideStatus.SEARCHING, RideStatus.ASSIGNED) \
                and plan.book_at > tb.now + timedelta(minutes=10):
            await tb.cancel_ride(f"meeting moved to {ctx.meeting.start:%H:%M}; will rebook at {plan.book_at:%H:%M}")
        await self.check_lateness(tb)

    async def consider_switch(self, tb: Toolbox) -> None:
        """Cabs keep failing or the rider is heading for a late arrival: is another way clearly faster?"""
        ctx = tb.ctx
        if ctx.switch_declined:
            return
        late = await tb.minutes_late()
        if ctx.cab_failures < SWITCH_AFTER_FAILURES and late < SWITCH_AFTER_LATE_MIN:
            return
        options = await tb.alternatives()
        if not options:
            return
        cab_eta = await tb.projected_cab_arrival()
        best = options[0]
        metro = next((o for o in options if o.mode == "metro"), None)
        if metro and metro is not best and (metro.arrive_at - best.arrive_at) <= timedelta(minutes=3):
            best = metro                # nearly as fast and far more predictable than a bike
        if cab_eta - best.arrive_at < timedelta(minutes=SWITCH_GAIN_MIN):
            return
        why = (f"{ctx.cab_failures} cab failures" if ctx.cab_failures >= SWITCH_AFTER_FAILURES
               else f"projected {late} min late by cab")
        await self.on_switch_opportunity(tb, best, why, late)

    async def on_switch_opportunity(self, tb: Toolbox, best, why: str, late: int) -> None:
        await tb.switch_mode(best.mode, why)

    async def check_lateness(self, tb: Toolbox) -> None:
        late = await tb.minutes_late()
        if late >= LATE_THRESHOLD_MIN:
            await tb.notify_attendees(late_message(tb, late, await tb.projected_arrival()))


class BaselineHuman(Agent):
    """What a typical rider does without an agent.

    Assumptions (stated in eval output so judges can challenge them):
      - checks Maps once ~75 min before and books with the same total margin the agent uses
      - doesn't re-check traffic after that
      - notices a cancellation after REACTION_MIN minutes
      - replies to drivers after REPLY_MIN minutes, slower if they don't share a language
      - shares their phone number when asked, half the time
      - emails attendees only once they're already late
    """

    name = "baseline"
    REACTION_MIN = 6
    REPLY_MIN = 3
    LANG_PENALTY_MIN = 3
    SHARES_PHONE_PROB = 0.5

    def __init__(self, seed: int):
        self.rng = random.Random(seed * 7919 + 1)
        self.book_at = None
        self.rebook_at = None
        self.pending: list[tuple] = []  # (due, text)
        self.notified = False

    async def handle(self, ev: Event, tb: Toolbox) -> None:
        ctx = tb.ctx
        if ctx.done:
            return
        if ev.kind == "tick":
            await self._tick(tb)
        elif ev.kind == "ride_update":
            r = ctx.ride
            if r and ev.data["ride_id"] == r.id and ev.data["status"] in ("cancelled", "no_drivers") \
                    and ev.data.get("reason") != "rider_cancelled":
                self.rebook_at = tb.now + timedelta(minutes=self.REACTION_MIN)
        elif ev.kind == "driver_message":
            self._queue_reply(ev, tb)
        elif ev.kind == "meeting_changed":
            if ctx.meeting.cancelled:
                if ctx.ride and ctx.ride.active:
                    await tb.cancel_ride("meeting cancelled")
            elif ctx.bookings == 0:
                self.book_at = None  # they see the calendar change and re-plan

    async def _tick(self, tb: Toolbox) -> None:
        ctx, now = tb.ctx, tb.now
        if ctx.meeting.cancelled:
            return
        if self.book_at is None:
            # Same planner and margin as the agent (Maps "depart at"), but done once, never updated.
            self.book_at = (await tb.refresh_plan()).book_at
        if ctx.bookings == 0 and now >= self.book_at:
            await tb.request_ride("baseline booking")
        if self.rebook_at and now >= self.rebook_at:
            self.rebook_at = None
            await tb.request_ride("baseline rebook")
        for item in [p for p in self.pending if p[0] <= now]:
            self.pending.remove(item)
            await tb.send_raw_driver_message(item[1])
        if not self.notified and now >= ctx.meeting.start and not ctx.arrived_at:
            self.notified = True
            late = await tb.minutes_late()
            if late >= 3:
                await tb.notify_attendees(f"Sorry, running ~{late} min late.")

    def _queue_reply(self, ev: Event, tb: Toolbox) -> None:
        intent = ev.data.get("_intent")
        p = tb.ctx.profile
        if intent == "where":
            text = f"I'm at {p.pickup_landmark}"
        elif intent == "phone":
            text = f"My number is {p.phone}" if self.rng.random() < self.SHARES_PHONE_PROB else "Call me on the app"
        else:
            return
        delay = self.REPLY_MIN + (0 if ev.data["lang"] in p.languages else self.LANG_PENALTY_MIN)
        self.pending.append((tb.now + timedelta(minutes=delay), text))
