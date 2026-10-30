"""The agent's hands. Every action, rule-based or LLM, goes through Toolbox,
which applies guardrails, approvals and translation, and logs to the event bus."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import Meeting, Ride, RideStatus, UserProfile
from ..planner import PICKUP_ALLOWANCE_MIN, Plan, make_plan

RENOTIFY_STEP_MIN = 5
LATE_THRESHOLD_MIN = 5
# Don't warn attendees earlier than this before the meeting. Lateness projected further out is mostly
# a traffic jam that will clear: in the chaos eval, alerts sent earlier were false alarms far more often
# (21 -> 4 on the tuning set, 9 -> 0 on unseen trips) while late trips still got ~30 min notice.
ALERT_WINDOW_MIN = 30
MAX_NOTIFICATIONS = 3
NO_DRIVER_BACKOFF_MIN = 2
# Switching away from cabs: only offered after this many cab failures or this much projected lateness,
# and only to an option that beats the cab by SWITCH_GAIN_MIN (the guardrail allows no less than
# SWITCH_MIN_GAIN_MIN, whatever the LLM says).
SWITCH_AFTER_FAILURES = 2
SWITCH_AFTER_LATE_MIN = 10
SWITCH_GAIN_MIN = 5
SWITCH_MIN_GAIN_MIN = 3
# Mid-trip safety: if the cab makes no progress for this long, check on the rider.
CHECKIN_AFTER_MIN = 3
# A driver more than this far from the pickup (by the app's ETA, or by their own word on a call) is too late:
# cancel and book another cab. At most MAX_LATE_REPLACEMENTS times a trip, so it never churns drivers.
MAX_PICKUP_WAIT_MIN = 10
MAX_LATE_REPLACEMENTS = 2


@dataclass
class TripContext:
    profile: UserProfile
    meeting: Meeting
    plan: Plan | None = None
    ride: Ride | None = None
    bookings: int = 0                 # ride requests made (incl. ones no driver accepted)
    ride_ids: list[str] = field(default_factory=list)
    extra_bookings: int = 0           # granted by the user after hitting the limit
    limit_escalated: bool = False
    retry_after: datetime | None = None
    notifications: int = 0
    trip_approved: bool = False
    declined: bool = False
    done: bool = False
    arrived_at: datetime | None = None
    notified_late: int | None = None
    first_notified_at: datetime | None = None
    approvals_asked: int = 0
    user_actions: int = 0   # times the human had to touch their phone
    # ride_id -> (best pickup ETA seen, when it was seen); detects drivers who stop moving
    eta_progress: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    # Switching to the metro / a bike taxi when cabs keep failing
    cab_failures: int = 0             # driver cancellations, "no drivers", unreachable
    late_replacements: int = 0        # drivers replaced for being more than MAX_PICKUP_WAIT_MIN away
    mode: str = "cab"
    mode_eta: datetime | None = None  # what the agent expects after switching
    mode_done_at: datetime | None = None   # when the rider actually gets there (simulation)
    switch_declined: bool = False
    switch_considered: tuple[int, int] | None = None   # (failures, lateness) when last weighed up
    # Mid-trip safety
    trip_started_at: datetime | None = None
    trip_progress: tuple[float, datetime] | None = None   # (least km left seen, when)
    safety_checked: bool = False
    alerted_contacts: bool = False
    # Night route choice
    route: dict | None = None             # the chosen RouteScore, as a dict
    route_decided: bool = False
    route_told: set = field(default_factory=set)   # ride ids whose driver was told the route
    # Driver phone call that the assistant answered (ride_id, driver, lang, notes, takeover)
    call: dict | None = None


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "request_ride",
        "description": "Book a new ride from the user's pickup point to the meeting. Use to rebook after a cancellation.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    }},
    {"type": "function", "function": {
        "name": "cancel_ride",
        "description": "Cancel the current ride.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    }},
    {"type": "function", "function": {
        "name": "message_driver",
        "description": "Send the driver a message. Write short simple English; it is translated to the driver's language.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "notify_attendees",
        "description": "Email meeting attendees, e.g. that the user is running late.",
        "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    }},
    {"type": "function", "function": {
        "name": "switch_mode",
        "description": ("Stop waiting for cabs and send the rider another way (one of STATE.alternatives). "
                        "Cancels any cab that hasn't picked them up yet. The rider is asked to approve."),
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["metro", "bike_taxi"]},
            "reason": {"type": "string"}}, "required": ["mode", "reason"]},
    }},
    {"type": "function", "function": {
        "name": "no_action",
        "description": "Nothing needs doing for this event.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    }},
]


class Toolbox:
    def __init__(self, ctx: TripContext, *, clock, maps, rides, voice, notifier, approval,
                 guardrails, bus, require_approval: bool = True, transit=None,
                 safety_prompt=None, alerter=None, locations=None, drill: bool = True,
                 route_chooser=None, real_routes: bool = False, updates=None, auto_book: bool = False):
        self.ctx = ctx
        self.updates = updates           # async fn(text): FYI messages to the rider (e.g. Telegram), real trips only
        self.auto_book = auto_book
        self.route_chooser = route_chooser
        self.real_routes = real_routes
        self.transit = transit
        self.safety_prompt = safety_prompt
        self.alerter = alerter
        self.locations = locations
        self.drill = drill
        self.clock = clock
        self.maps = maps
        self.rides = rides
        self.voice = voice
        self.notifier = notifier
        self.approval = approval
        self.guardrails = guardrails
        self.bus = bus
        self.require_approval = require_approval

    @property
    def now(self) -> datetime:
        return self.clock.now

    def _log(self, action: str, **detail) -> None:
        self.bus.publish("agent_action", ts=self.clock.iso(), action=action, **detail)

    # ---- read-only helpers ------------------------------------------------
    async def travel_minutes(self, depart_at: datetime | None = None) -> int:
        depart_at = max(depart_at or self.now, self.now)
        return await self.maps.travel_minutes(
            self.ctx.profile.location, self.ctx.meeting.location, depart_at, self.now)

    async def refresh_plan(self) -> Plan:
        old = self.ctx.plan
        # Two passes: leave time depends on travel time, which depends on traffic at leave time.
        first = make_plan(self.ctx.meeting.start, await self.travel_minutes())
        plan = make_plan(self.ctx.meeting.start, await self.travel_minutes(first.leave_by))
        self.ctx.plan = plan
        if old is None or old.book_at != plan.book_at:
            self.bus.publish("plan", ts=self.clock.iso(), **plan.as_dict())
        return plan

    async def projected_arrival(self) -> datetime:
        ctx, now = self.ctx, self.now
        if ctx.arrived_at:
            return ctx.arrived_at
        if ctx.mode != "cab" and ctx.mode_eta:
            return max(ctx.mode_eta, now)
        return await self.projected_cab_arrival()

    async def projected_cab_arrival(self) -> datetime:
        ctx, now = self.ctx, self.now
        r = ctx.ride
        if r is not None and r.status == RideStatus.IN_TRIP:
            return now + timedelta(minutes=r.trip_eta_min or 0)
        if r is None or not r.active:
            book = max(now, ctx.plan.book_at) if ctx.plan else now
            pickup_at = book + timedelta(minutes=PICKUP_ALLOWANCE_MIN)
        elif r.status == RideStatus.SEARCHING:
            pickup_at = now + timedelta(minutes=PICKUP_ALLOWANCE_MIN)
        elif r.status == RideStatus.ASSIGNED:
            pickup_at = now + timedelta(minutes=(r.pickup_eta_min or 0) + 1)
        else:  # arrived, rider walking out
            pickup_at = now + timedelta(minutes=1)
        return pickup_at + timedelta(minutes=await self.travel_minutes(pickup_at))

    async def minutes_late(self) -> int:
        delta = (await self.projected_arrival() - self.ctx.meeting.start).total_seconds() / 60
        return round(delta)

    async def state(self) -> dict:
        ctx, r = self.ctx, self.ctx.ride
        return {
            "now": self.clock.iso(),
            "user": ctx.profile.name,
            "pickup_landmark": ctx.profile.pickup_landmark,
            "meeting": {"title": ctx.meeting.title, "start": ctx.meeting.start.strftime("%H:%M"),
                        "location": ctx.meeting.location, "cancelled": ctx.meeting.cancelled},
            "plan": ctx.plan.as_dict() if ctx.plan else None,
            "ride": None if r is None else {
                "id": r.id, "status": r.status.value, "pickup_eta_min": r.pickup_eta_min,
                "trip_eta_min": r.trip_eta_min, "cancel_reason": r.cancel_reason,
                "driver": r.driver.name if r.driver else None,
                "driver_language": r.driver.language if r.driver else None,
            },
            "projected_arrival": (await self.projected_arrival()).strftime("%H:%M"),
            "minutes_late": await self.minutes_late(),
            "notified_late": ctx.notified_late,
            "bookings_so_far": ctx.bookings,
            "mode": ctx.mode,
            "cab_failures": ctx.cab_failures,
            "alternatives": [o.as_dict() for o in await self.alternatives()] if ctx.mode == "cab" else [],
        }

    async def alternatives(self) -> list:
        """Other ways to get there from here, soonest first. Empty once the rider is in a cab."""
        ctx, r = self.ctx, self.ctx.ride
        if self.transit is None or ctx.meeting.cancelled or ctx.mode != "cab":
            return []
        if r is not None and r.status in (RideStatus.IN_TRIP, RideStatus.ARRIVED, RideStatus.COMPLETED):
            return []
        fare = await self.rides.estimate_fare(ctx.profile.location, ctx.meeting.location)
        return self.transit.options(ctx.profile.location, ctx.meeting.location, self.now, fare)

    def drivers_used(self) -> int:
        """Rides that actually got a driver -- the ones that cost money or a cancellation fee."""
        return sum(1 for rid in self.ctx.ride_ids if (r := self.rides.get(rid)) and r.driver)

    # ---- actions ----------------------------------------------------------
    async def request_ride(self, reason: str = "") -> dict:
        ctx = self.ctx
        if ctx.ride and ctx.ride.active:
            return {"error": f"ride {ctx.ride.id} is already active"}
        if ctx.meeting.cancelled:
            return {"error": "meeting is cancelled"}
        p, d = ctx.profile.location, ctx.meeting.location
        fare = await self.rides.estimate_fare(p, d)
        decision, why = self.guardrails.check_booking(fare, self.drivers_used(), ctx.extra_bookings)
        if decision == "deny":
            self.bus.publish("guardrail", ts=self.clock.iso(), rule="booking_limit", detail=why)
            # Escalate once instead of silently giving up or looping.
            if ctx.limit_escalated or not self.require_approval:
                ctx.declined = True
                return {"error": why}
            ctx.limit_escalated = True
            ctx.approvals_asked += 1
            ctx.user_actions += 1
            if not await self.approval.request(f"{why}. Keep trying for '{ctx.meeting.title}'?", {"fare": fare}):
                ctx.declined = True
                return {"error": "user stopped rebooking"}
            ctx.extra_bookings += 2
        if not self.require_approval:
            ctx.user_actions += 1   # the human is booking it themselves
        elif self.auto_book and decision == "allow":
            ctx.trip_approved = True   # standing permission: within the spend cap, no question asked
        elif decision == "needs_approval" or not ctx.trip_approved:
            ctx.approvals_asked += 1
            ctx.user_actions += 1
            summary = f"Book a cab {p} → {d} for '{ctx.meeting.title}' at {ctx.meeting.start:%H:%M}? ~₹{fare:.0f}"
            if why:
                summary += f" ({why})"
            if not await self.approval.request(summary, {"fare": fare, "pickup": p, "dropoff": d}):
                ctx.declined = True
                self._log("booking_declined", reason="user declined")
                return {"error": "user declined"}
            ctx.trip_approved = True
        ride = await self.rides.request_ride(p, d, pickup_note=ctx.profile.pickup_landmark)
        ctx.ride = ride
        ctx.ride_ids.append(ride.id)
        ctx.bookings += 1
        self._log("request_ride", ride_id=ride.id, fare=fare, reason=reason)
        if not getattr(self.rides, "handoff", False):   # one-tap mode already sent you the booking buttons
            await self.update(f"🚕 Booked a cab for '{ctx.meeting.title}' ({ctx.meeting.start:%H:%M}): ~₹{fare:.0f}. "
                              f"Pickup: {ctx.profile.pickup_landmark}. ({reason})")
        return {"ok": True, "ride_id": ride.id, "fare": fare}

    async def update(self, text: str) -> None:
        """Tell the rider what just happened, without asking anything."""
        if self.updates is None:
            return
        try:
            await self.updates(text)
        except Exception as e:
            self.bus.publish("agent_error", ts=self.clock.iso(), error=f"update not sent: {e}"[:200])

    async def cancel_ride(self, reason: str = "") -> dict:
        r = self.ctx.ride
        if not r or not r.active:
            return {"error": "no active ride"}
        await self.rides.cancel_ride(r.id)
        await self.update(f"Cancelled the cab ({reason}).")
        if not self.require_approval:
            self.ctx.user_actions += 1
        self._log("cancel_ride", ride_id=r.id, reason=reason)
        return {"ok": True}

    async def message_driver(self, text: str) -> dict:
        r = self.ctx.ride
        if not r or not r.active or not r.driver:
            return {"error": "no driver to message"}
        verdict = self.guardrails.check_driver_message(text)
        if not verdict.allowed:
            self.bus.publish("guardrail", ts=self.clock.iso(), rule="pii", detail=verdict.reason, original=text)
        lang = r.driver.language
        native = await self.voice.from_english(verdict.text, lang)
        on_call = self.on_call(r.id)
        if on_call and self.ctx.call.get("takeover"):
            # The rider took the call over: show what the assistant would have said, don't say it.
            self.bus.publish("chat", ts=self.clock.iso(), sender="suggestion", text_en=verdict.text,
                             text=native, lang=lang, spoken=False)
            return {"ok": True, "suggested_only": True}
        await self.rides.send_message(r.id, native, lang, text_en=verdict.text)
        audio = await self.voice.synthesize(native, lang, force=on_call)
        self.bus.publish("chat", ts=self.clock.iso(), sender="agent", text_en=verdict.text,
                         text=native, lang=lang, audio=audio, spoken=on_call)
        if on_call:
            self.ctx.call["notes"].append(("agent", verdict.text))
        return {"ok": True, "sent_en": verdict.text, "sent_native": native}

    def on_call(self, ride_id: str) -> bool:
        c = self.ctx.call
        return bool(c and c.get("active") and c["ride_id"] == ride_id)

    async def start_call(self, ride_id: str, driver: str, lang: str) -> None:
        """The driver is phoning the rider; the assistant picks up and says who it is."""
        from ..i18n import render
        self.ctx.call = {"ride_id": ride_id, "driver": driver, "lang": lang, "notes": [],
                         "active": True, "takeover": False}
        self.bus.publish("call_start", ts=self.clock.iso(), driver=driver, lang=lang, rider=self.ctx.profile.name)
        greet_en = render("agent.greeting", "en-IN", name=self.ctx.profile.name)
        native = render("agent.greeting", lang, name=self.ctx.profile.name)
        audio = await self.voice.synthesize(native, lang, force=True)
        self.bus.publish("chat", ts=self.clock.iso(), sender="agent", text_en=greet_en, text=native,
                         lang=lang, audio=audio, spoken=True)
        self._log("call_answered", driver=driver)

    def end_call(self, why: str = "") -> str | None:
        c = self.ctx.call
        if not c or not c.get("active"):
            return None
        c["active"] = False
        summary = call_summary(c["driver"], c["notes"], c.get("takeover", False))
        self.bus.publish("call_end", ts=self.clock.iso(), driver=c["driver"], summary=summary, why=why)
        return summary

    def take_over_call(self) -> bool:
        c = self.ctx.call
        if not c or not c.get("active"):
            return False
        c["takeover"] = True
        self.bus.publish("call_takeover", ts=self.clock.iso(), driver=c["driver"])
        return True

    async def send_raw_driver_message(self, text: str) -> None:
        """What a human does: type English straight into the app. No guardrail, no translation."""
        r = self.ctx.ride
        if not r or not r.active:
            return
        await self.rides.send_message(r.id, text, "en-IN", text_en=text)
        self.ctx.user_actions += 1
        self.bus.publish("chat", ts=self.clock.iso(), sender="user", text_en=text, text=text, lang="en-IN")

    async def notify_attendees(self, message: str) -> dict:
        ctx = self.ctx
        late = await self.minutes_late()
        if late < LATE_THRESHOLD_MIN:
            return {"skipped": f"projected only {late} min late"}
        if ctx.meeting.start - self.now > timedelta(minutes=ALERT_WINDOW_MIN):
            return {"skipped": f"too early to be sure; attendees are warned from {ALERT_WINDOW_MIN} min before the meeting"}
        if ctx.notifications >= MAX_NOTIFICATIONS:
            return {"skipped": "attendees already updated enough times"}
        if ctx.notified_late is not None and late < ctx.notified_late + RENOTIFY_STEP_MIN:
            return {"skipped": f"already told attendees ~{ctx.notified_late} min late"}
        await self.notifier.notify_attendees(ctx.meeting, message)
        await self.update(f"✉ Told the attendees of '{ctx.meeting.title}' you're ~{late} min late.")
        ctx.notified_late = late
        ctx.notifications += 1
        ctx.first_notified_at = ctx.first_notified_at or self.now
        self._log("notify_attendees", minutes_late=late)
        return {"ok": True}

    async def switch_mode(self, mode: str, reason: str = "") -> dict:
        ctx = self.ctx
        option = next((o for o in await self.alternatives() if o.mode == mode), None)
        if option is None:
            return {"error": f"{mode} isn't available from here right now"}
        cab_eta = await self.projected_cab_arrival()
        gain = round((cab_eta - option.arrive_at).total_seconds() / 60)
        if gain < SWITCH_MIN_GAIN_MIN:
            why = f"{mode} would only save {gain} min over the cab; staying with the cab"
            self.bus.publish("guardrail", ts=self.clock.iso(), rule="switch_mode", detail=why)
            return {"error": why}
        label = {"metro": "the metro", "bike_taxi": "a bike taxi"}[mode]
        ctx.approvals_asked += 1
        ctx.user_actions += 1
        summary = (f"Cabs aren't working out ({reason}). Take {label} instead? Arrive ~{option.arrive_at:%H:%M} "
                   f"vs ~{cab_eta:%H:%M} by cab, ₹{option.fare_inr}. {option.steps}.")
        if not await self.approval.request(summary, {"mode": mode, "gain_min": gain}):
            ctx.switch_declined = True
            self._log("switch_declined", mode=mode)
            return {"error": "rider wants to keep trying cabs"}
        if ctx.ride and ctx.ride.active:
            await self.cancel_ride(f"rider is taking {label} instead")
        actual = self.transit.actual_minutes(option)
        ctx.mode, ctx.mode_eta = mode, option.arrive_at
        ctx.mode_done_at = self.now + timedelta(minutes=actual)
        self._log("switch_mode", mode=mode, eta=f"{option.arrive_at:%H:%M}", gain_min=gain, reason=reason,
                  steps=option.steps, fare=option.fare_inr)
        return {"ok": True, "mode": mode, "arrive": f"{option.arrive_at:%H:%M}", "minutes_saved": gain}

    async def choose_route(self) -> dict | None:
        """Night trips: compare routes by how busy and watched they are, and let the rider pick."""
        from ..providers.routes import get_route_options, is_night, recommend
        ctx = self.ctx
        if ctx.route_decided:
            return ctx.route
        ctx.route_decided = True
        leave_at = ctx.plan.leave_by if ctx.plan else self.now
        if self.route_chooser is None or not is_night(leave_at):
            return None
        options, note = await get_route_options(ctx.profile.location, ctx.meeting.location, leave_at, self.maps,
                                                real=self.real_routes)
        if note:
            self.bus.publish("agent_error", ts=self.clock.iso(), error=note)
        rec = recommend(options)
        name, how = await self.route_chooser.choose(options, rec)
        chosen = next(o for o in options if o.name == name)
        ctx.route = chosen.as_dict()
        if hasattr(self.rides, "route_km"):
            self.rides.route_km = chosen.km           # the cab drives the chosen route's distance
        self._log("route_chosen", name=chosen.name, via=chosen.via, how=how, minutes=chosen.minutes,
                  score=chosen.score, gap_km=chosen.longest_gap_km)
        return ctx.route

    async def tell_driver_route(self) -> None:
        ctx, r = self.ctx, self.ctx.ride
        if not ctx.route or not r or not r.driver or r.id in ctx.route_told:
            return
        ctx.route_told.add(r.id)
        await self.message_driver(f"Please take {ctx.route['name']}, via {ctx.route['via']}. Thank you.")

    def trip_info(self) -> dict:
        ctx, r = self.ctx, self.ctx.ride
        info = {"from": ctx.profile.location, "to": ctx.meeting.location, "meeting": ctx.meeting.title}
        if ctx.route:
            info["route"] = f"{ctx.route['name']} (via {ctx.route['via']})"
        if r and r.driver:
            info.update(driver=r.driver.name, vehicle=r.driver.vehicle, plate=r.driver.plate)
        return info

    def trip_progress_fraction(self) -> float:
        ctx, r = self.ctx, self.ctx.ride
        if not (ctx.trip_started_at and r and r.trip_eta_min is not None):
            return 0.0
        done = (self.now - ctx.trip_started_at).total_seconds() / 60
        return done / max(done + r.trip_eta_min, 1)

    async def safety_checkin(self, reason: str) -> str:
        """Ask the rider if they're OK; alert their contacts on "help" or no answer. Deterministic."""
        ctx = self.ctx
        if ctx.safety_checked or self.safety_prompt is None:
            return "skipped"
        ctx.safety_checked = True
        self._log("safety_checkin", reason=reason)
        timeout = getattr(self, "checkin_timeout_s", 60)
        answer = await self.safety_prompt.check_in(f"{reason}. Are you OK?", timeout)
        self._log("safety_answer", answer=answer)
        if answer in ("help", "timeout") and self.alerter is not None:
            from ..safety import Alert
            why = ("asked for help during a cab ride" if answer == "help"
                   else f"didn't answer a safety check during a cab ride ({reason.lower()})")
            loc = self.locations.best(ctx.profile.location, ctx.meeting.location, self.trip_progress_fraction())                 if self.locations else None
            await self.alerter.alert(Alert(ctx.profile.name, why, self.drill, loc, self.trip_info()))
            ctx.alerted_contacts = True
        return answer

    async def call(self, name: str, args: dict) -> dict:
        """Dispatch an LLM tool call."""
        if name == "request_ride":
            return await self.request_ride(args.get("reason", ""))
        if name == "cancel_ride":
            # The LLM may only drop an active ride if the meeting is off, or moved so much later that a fresh
            # booking is still 10+ min away. Otherwise a new ride can only arrive later than the current one.
            ctx, r = self.ctx, self.ctx.ride
            if r and r.active and not ctx.meeting.cancelled:
                plan = await self.refresh_plan()
                if plan.book_at <= self.now + timedelta(minutes=10):
                    why = "keeping the current ride: cancelling now would only make the rider later"
                    self.bus.publish("guardrail", ts=self.clock.iso(), rule="cancel_ride", detail=why)
                    return {"error": why}
            return await self.cancel_ride(args.get("reason", ""))
        if name == "message_driver":
            return await self.message_driver(args.get("text", ""))
        if name == "notify_attendees":
            return await self.notify_attendees(args.get("message", ""))
        if name == "switch_mode":
            return await self.switch_mode(args.get("mode", ""), args.get("reason", ""))
        if name == "no_action":
            self._log("no_action", reason=args.get("reason", ""))
            return {"ok": True}
        return {"error": f"unknown tool {name}"}


def call_summary(driver: str, notes: list[tuple[str, str]], takeover: bool) -> str:
    """One line for the rider: what the driver wanted and what the assistant said."""
    asked, said = [], []
    for who, text in notes:
        t = text.lower()
        if who == "driver":
            if any(w in t for w in ("cash", "cancel", "offline", "outside the app")):
                asked.append("asked you to cancel and pay cash")
            elif any(w in t for w in ("number", "phone", "contact")):
                asked.append("asked for your number")
            elif any(w in t for w in ("where", "location", "spot", "landmark", "find", "can't see", "cannot see")):
                asked.append("couldn't find you")
            elif any(w in t for w in ("arrived", "reached", "waiting", "gate")):
                asked.append("has arrived")
            elif any(w in t for w in ("coming", "on my way", "heading", "leaving")):
                asked.append("is on the way")
        else:
            if any(w in t for w in ("can't cancel", "cannot cancel", "stay in the app", "in the app", "accept cash")):
                said.append("kept everything in the app")
            if "gate" in t or "metro" in t or "landmark" in t:
                said.append("gave the pickup point")
            elif "number" in t or "in-app" in t or "app" in t:
                said.append("kept your number private")
            elif "coming out" in t:
                said.append("said you're coming out")
    parts = []
    if asked:
        parts.append(f"{driver} " + ", then ".join(dict.fromkeys(asked)))
    if said:
        parts.append("assistant " + " and ".join(dict.fromkeys(said)))
    line = "; ".join(parts) or f"Short call with {driver}"
    return line + (" (you took over the call)" if takeover else "") + "."
