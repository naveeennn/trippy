"""Safety: check on the rider when a trip goes wrong, and alert their emergency contacts.

Two ways in:
  - SOS button: the rider asks for help. Always sent as a real alert.
  - Automatic check-in: the cab stops making progress mid-trip. The rider is asked "Are you OK?";
    "I need help" or no answer within the timeout alerts their contacts.

Alerts from simulated demo trips are labelled as drills, so contacts aren't frightened while you test.
This never calls 112 by itself; the dashboard offers a one-tap "Call 112" for that.
"""
import asyncio
import itertools
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from .config import settings

log = logging.getLogger(__name__)

# Approximate centres of the planner's areas (lat, lon), for a map link when there's no live location.
PLACE_COORDS = {
    "MG Road": (12.9756, 77.6066), "Indiranagar": (12.9784, 77.6408), "Koramangala": (12.9352, 77.6245),
    "HSR Layout": (12.9116, 77.6474), "Whitefield": (12.9698, 77.7500), "Marathahalli": (12.9569, 77.7011),
    "Hebbal": (13.0358, 77.5970), "Jayanagar": (12.9250, 77.5938),
}


@dataclass
class Location:
    lat: float
    lon: float
    source: str                    # "live (shared from your device)" | "approximate (simulated ride)"
    at: datetime | None = None

    @property
    def map_link(self) -> str:
        return f"https://maps.google.com/?q={self.lat:.5f},{self.lon:.5f}"


@dataclass
class LocationStore:
    """Latest real location the dashboard shared (browser geolocation, with the user's permission)."""
    live: Location | None = None

    def update(self, lat: float, lon: float) -> None:
        self.live = Location(lat, lon, "live (shared from your device)", datetime.now())

    def best(self, origin: str | None = None, dest: str | None = None, progress: float = 0.0) -> Location | None:
        if self.live and self.live.at and (datetime.now() - self.live.at).total_seconds() < 600:
            return self.live
        if origin in PLACE_COORDS and dest in PLACE_COORDS:
            (a, b), (c, d) = PLACE_COORDS[origin], PLACE_COORDS[dest]
            p = min(max(progress, 0.0), 1.0)
            return Location(a + (c - a) * p, b + (d - b) * p, "approximate (simulated ride)")
        return None


# ---- asking the rider ---------------------------------------------------------------------

class SafetyPrompt(ABC):
    @abstractmethod
    async def check_in(self, question: str, timeout_s: float) -> str:
        """-> "ok" | "help" | "timeout" """


class AssumeOK(SafetyPrompt):
    """Simulations and evals: the simulated rider is always fine."""

    async def check_in(self, question: str, timeout_s: float) -> str:
        return "ok"


class DashboardSafetyPrompt(SafetyPrompt):
    def __init__(self, bus):
        self.bus = bus
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)

    async def check_in(self, question: str, timeout_s: float) -> str:
        cid = f"S{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        self.bus.publish("safety_checkin", id=cid, question=question, timeout_s=timeout_s)
        try:
            answer = await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            answer = "timeout"
        finally:
            self._pending.pop(cid, None)
        self.bus.publish("safety_answer", id=cid, answer=answer)
        return answer

    def resolve(self, cid: str, answer: str) -> bool:
        fut = self._pending.get(cid)
        if fut is None or fut.done() or answer not in ("ok", "help"):
            return False
        fut.set_result(answer)
        return True

    def cancel_all(self) -> None:
        """The ride ended or watching restarted: close any open "Are you OK?" without alerting."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result("cancelled")


# ---- telling the contacts -----------------------------------------------------------------

@dataclass
class Alert:
    rider: str
    reason: str
    drill: bool
    location: Location | None
    trip: dict = field(default_factory=dict)    # from, to, meeting, driver, vehicle, plate
    rehearsal: bool = False                     # a test the rider started on purpose: deliver everywhere, labelled
    id: str = field(default_factory=lambda: __import__("uuid").uuid4().hex[:10])

    def subject(self) -> str:
        head = "[TripOwner DRILL: no action needed] " if self.drill else "TripOwner ALERT: "
        return f"{head}{self.rider} may need help"

    def body(self) -> str:
        lines = []
        if self.drill:
            lines += ["THIS IS A TEST from a simulated trip. No action is needed.", ""]
        lines += [f"{self.rider} {self.reason}.", "", f"Time: {datetime.now():%d %b %Y, %H:%M}"]
        t = self.trip
        if t.get("from") or t.get("to"):
            lines.append(f"Trip: {t.get('from', '?')} -> {t.get('to', '?')}" + (f" for '{t['meeting']}'" if t.get("meeting") else ""))
        if t.get("driver"):
            lines.append(f"Cab: {t['driver']}, {t.get('vehicle', '')} {t.get('plate', '')}".rstrip())
        if self.location:
            lines.append(f"Last known location ({self.location.source}): {self.location.map_link}")
        lines += ["", f"Please call {self.rider} now. If you can't reach them, call 112 (India emergency).",
                  "", "Sent automatically by TripOwner."]
        return "\n".join(lines)


class AlertChannel(ABC):
    name = "channel"

    @abstractmethod
    def ready(self) -> bool: ...

    @abstractmethod
    async def send(self, alert: Alert) -> str:
        """-> short description of what was sent (raises on failure)."""


class EmailChannel(AlertChannel):
    name = "email"

    def __init__(self, send=None):
        from .providers.email import send_email
        self._send = send or send_email

    def ready(self) -> bool:
        from .providers.email import email_enabled
        return email_enabled() and bool(settings.emergency_emails)

    async def send(self, alert: Alert) -> str:
        await self._send(list(settings.emergency_emails), alert.subject(), alert.body())
        return f"emailed {len(settings.emergency_emails)} contact(s)"


class TelegramChannel(AlertChannel):
    """Instant push to each contact: the alert text with an "I'm on it" button, then a map pin."""
    name = "telegram"
    ACK_WATCH_S = 600

    def __init__(self, client=None):
        self._client = client

    def client(self):
        if self._client is None:
            from .providers.telegram import TelegramClient
            self._client = TelegramClient()
        return self._client

    def ready(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_ids)

    async def send(self, alert: Alert) -> str:
        c, ok, failed = self.client(), 0, []
        text = ("🚨 " if not alert.drill else "🧪 ") + alert.subject() + "\n\n" + alert.body()
        for chat in settings.telegram_chat_ids:
            try:
                await c.send_message(chat, text, ack_button=f"ack:{alert.id}")
                if alert.location:
                    await c.send_location(chat, alert.location.lat, alert.location.lon)
                ok += 1
            except Exception as e:
                failed.append(f"{chat}: {e}")
        if not ok:
            raise RuntimeError("; ".join(failed)[:200])
        return f"sent to {ok} contact(s)" + (f", {len(failed)} failed" if failed else "")

    async def watch_acks(self, alert: Alert, publish) -> None:
        """Tell the dashboard who pressed "I'm on it". Runs in the background for a few minutes."""
        c, offset, deadline = self.client(), None, asyncio.get_running_loop().time() + self.ACK_WATCH_S
        try:
            while asyncio.get_running_loop().time() < deadline:
                for u in await c.updates(offset=offset, timeout=20):
                    offset = u["update_id"] + 1
                    q = u.get("callback_query")
                    if q and q.get("data") == f"ack:{alert.id}":
                        who = q.get("from", {})
                        name = " ".join(filter(None, [who.get("first_name"), who.get("last_name")])) or "A contact"
                        await c.answer_callback(q["id"], "Thank you. Please call them now.")
                        publish("sos_ack", name=name, channel="telegram")
        except Exception as e:
            log.warning("stopped watching Telegram acknowledgements: %s", e)


ALERT_SCRIPTS: dict[str, tuple[str, float]] = {}      # one-time token -> (TwiML, expiry), served at /twilio/alert


def take_alert_script(token: str | None) -> str | None:
    import time
    now = time.time()
    for t in [t for t, (_, exp) in ALERT_SCRIPTS.items() if exp < now]:
        ALERT_SCRIPTS.pop(t, None)
    script, exp = ALERT_SCRIPTS.get(token or "", (None, 0))
    return script if exp >= now else None


class CallChannel(AlertChannel):
    """Phones each emergency contact (Twilio) and reads the alert out, twice. With PUBLIC_BASE_URL set, Twilio
    fetches the words from /twilio/alert (works on trial accounts, which refuse words sent with the request);
    without it, the words go with the request. Simulated-trip drills don't ring anyone; rehearsals do."""
    name = "phone call"
    FOLLOW_S = 180

    def __init__(self, transport=None):
        self._transport = transport
        self.calls: list[tuple[str, str]] = []          # (call sid, masked number)

    def ready(self) -> bool:
        return bool(settings.twilio_account_sid and settings.twilio_auth_token
                    and (settings.twilio_call_from or settings.twilio_phone_number) and settings.emergency_phones)

    def _client(self):
        import httpx
        return httpx.AsyncClient(base_url=f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/",
                                 auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=20,
                                 transport=self._transport)

    @staticmethod
    def script(alert: Alert) -> str:
        from xml.sax.saxutils import escape
        head = ("This is a test of TripOwner's safety alert. No action is needed. " if alert.drill
                else "This is an urgent automatic alert from TripOwner. ")
        where = " Their live location has been sent to you on Telegram." if alert.location else ""
        words = escape(f"{head}{alert.rider} {alert.reason}.{where} Please call {alert.rider} now. "
                       f"If you cannot reach them, call 1 1 2.")
        say = f'<Say voice="Polly.Raveena" language="en-IN">{words}</Say>'
        return f'<Response>{say}<Pause length="1"/>{say}</Response>'

    def _words(self, alert: Alert) -> dict:
        if not settings.public_base_url:
            return {"Twiml": self.script(alert), "Timeout": "30"}
        import secrets
        import time
        token = secrets.token_urlsafe(18)
        ALERT_SCRIPTS[token] = (self.script(alert), time.time() + 900)
        return {"Url": f"{settings.public_base_url}/twilio/alert?t={token}"}   # Url only: trial accounts

    async def send(self, alert: Alert) -> str:
        if alert.drill and not alert.rehearsal:
            return "skipped (simulated trip: nobody is phoned for a drill)"
        placed, failed = [], []
        async with self._client() as c:
            for number in settings.emergency_phones:
                # Trial accounts: calls to a verified phone must come from the trial number assigned to it
                r = await c.post("Calls.json", data={"To": number,
                                                     "From": settings.twilio_call_from or settings.twilio_phone_number,
                                                     **self._words(alert)})
                masked = number[:3] + "…" + number[-3:]
                if r.status_code < 300:
                    self.calls.append((r.json()["sid"], masked))
                    placed.append(masked)
                else:
                    failed.append(f"{masked}: {r.json().get('message', r.status_code)}")
        if not placed:
            raise RuntimeError("; ".join(failed)[:200])
        return f"calling {', '.join(placed)}" + (f"; failed {'; '.join(failed)}" if failed else "")

    async def watch_acks(self, alert: Alert, publish) -> None:
        """Report each call's progress (ringing, answered, no answer...) to the dashboard."""
        pending, self.calls = list(self.calls), []
        last: dict[str, str] = {}
        deadline = asyncio.get_running_loop().time() + self.FOLLOW_S
        try:
            async with self._client() as c:
                while pending and asyncio.get_running_loop().time() < deadline:
                    for sid, who in list(pending):
                        j = (await c.get(f"Calls/{sid}.json")).json()
                        status = j.get("status", "?")
                        if last.get(sid) != status:
                            last[sid] = status
                            publish("sos_call", number=who, status=status, sid=sid, duration=j.get("duration"))
                        if status in ("completed", "busy", "no-answer", "failed", "canceled"):
                            pending.remove((sid, who))
                    await asyncio.sleep(3)
        except Exception as e:
            log.warning("stopped following emergency calls: %s", e)


class DemoChannel(AlertChannel):
    """Public demo: shows what would be sent, sends nothing."""
    name = "demo (not sent)"

    def ready(self) -> bool:
        return True

    async def send(self, alert: Alert) -> str:
        return "would alert the rider's emergency contacts (public demo: nothing sent)"


class EmergencyAlerter:
    def __init__(self, bus, channels: list[AlertChannel] | None = None):
        self.bus = bus
        self.channels = channels if channels is not None else [TelegramChannel(), CallChannel(), EmailChannel()]

    def ready_channels(self) -> list[str]:
        return [c.name for c in self.channels if c.ready()]

    async def alert(self, alert: Alert) -> dict:
        results = {}
        for c in self.channels:
            if not c.ready():
                results[c.name] = "not set up"
                continue
            try:
                results[c.name] = await c.send(alert)
            except Exception as e:
                log.warning("alert via %s failed: %s", c.name, e)
                results[c.name] = f"FAILED: {type(e).__name__}: {str(e)[:100]}"
        self.bus.publish("sos_sent", reason=alert.reason, drill=alert.drill, results=results,
                         location=alert.location.map_link if alert.location else None,
                         location_source=alert.location.source if alert.location else None)
        for c in self.channels:
            if hasattr(c, "watch_acks") and c.ready() and not str(results.get(c.name, "")).startswith(("FAILED", "not set")):
                asyncio.ensure_future(c.watch_acks(alert, self.bus.publish))
        return results


# ---- real rides: watch the rider's own GPS ----------------------------------------------------

def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    k = 111_320
    return math.hypot((b[0] - a[0]) * k, (b[1] - a[1]) * k * math.cos(math.radians(a[0])))


class GpsGuard:
    """During a real cab ride, the rider's phone shares its location. If it stops moving (within
    radius_m for stall_min minutes), check on the rider; no answer or "help" alerts their contacts.

    5 min by default (not 3 as in the simulation): real traffic can stand still for minutes. After an
    "I'm OK" it re-arms only once the rider has moved 300 m, so one long jam doesn't keep asking."""

    REARM_M = 300
    FRESH_S = 60                                  # the dashboard re-sends its fix every 20 s while open

    def __init__(self, bus, prompt: SafetyPrompt, alerter: "EmergencyAlerter", rider: str = "",
                 stall_min: float = 5, radius_m: float = 60, timeout_s: float = 60, now=datetime.now,
                 silent_min: float = 5):
        self.bus, self.prompt, self.alerter, self.rider = bus, prompt, alerter, rider
        self.stall_s, self.radius_m, self.timeout_s, self._now = stall_min * 60, radius_m, timeout_s, now
        self.default_stall_s = self.stall_s
        self.active = False
        self.points: list[tuple[datetime, float, float]] = []
        self.anchor: tuple[float, float] | None = None     # where the last check happened
        self.busy = False
        self.stale_warned = False
        self.note = ""
        self.rehearsal = False
        self.silent_s = self.default_silent_s = silent_min * 60
        self.silent_handled = False
        self.generation = 0                       # bumped on start/stop; a check from an older ride is void
        self._task = None

    def snapshot(self) -> dict:
        last = self.points[-1][0] if self.points else None
        return {"active": self.active, "points": len(self.points), "note": self.note,
                "last_fix": f"{last:%H:%M:%S}" if last else None,
                "stall_min": self.stall_s / 60, "radius_m": self.radius_m}

    def start(self, note: str = "", rehearsal: bool = False, stall_min: float | None = None) -> None:
        """rehearsal: you're testing the whole chain on purpose; alerts go out for real, labelled as a test."""
        self.active, self.points, self.anchor, self.note, self.stale_warned = True, [], None, note, False
        self.rehearsal = rehearsal
        self._void_pending()
        self.stall_s = max(1.0, stall_min or self.default_stall_s / 60) * 60
        self.silent_s = max(1.0, stall_min or self.default_silent_s / 60) * 60   # a rehearsal shortens both
        self.silent_handled = False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass                                  # no event loop (tests): checks are driven by hand
        else:
            if self._task is None or self._task.done():
                self._task = asyncio.ensure_future(self._loop())
        self.bus.publish("gps_guard", **self.snapshot())

    def _void_pending(self) -> None:
        self.generation += 1
        if hasattr(self.prompt, "cancel_all"):
            self.prompt.cancel_all()

    def stop(self) -> None:
        self.active = False
        self._void_pending()
        self.bus.publish("gps_guard", **self.snapshot())

    def on_location(self, lat: float, lon: float) -> None:
        if not self.active:
            return
        now = self._now()
        self.points.append((now, lat, lon))
        self.points = [p for p in self.points if (now - p[0]).total_seconds() <= 900]
        self.stale_warned = False
        self.silent_handled = False               # heard from the phone again
        if self.anchor and _dist_m(self.anchor, (lat, lon)) > self.REARM_M:
            self.anchor = None                    # moved on: watch again

    def stalled(self) -> bool:
        if not self.points or self.anchor is not None:
            return False
        now = self._now()
        if (now - self.points[-1][0]).total_seconds() > self.FRESH_S:
            return False                          # the phone isn't reporting: that's the silence check's job
        recent = [p for p in self.points if (now - p[0]).total_seconds() <= self.stall_s]
        older = [p for p in self.points if (now - p[0]).total_seconds() > self.stall_s]
        if not older or not recent:
            return False                          # not enough history yet
        ref = (older[-1][1], older[-1][2])
        return all(_dist_m(ref, (la, lo)) <= self.radius_m for _, la, lo in recent)

    async def check(self) -> str | None:
        """One check; returns the rider's answer if a check-in happened."""
        if not self.active or self.busy:
            return None
        quiet_s = (self._now() - self.points[-1][0]).total_seconds() if self.points else 0
        if quiet_s > self.silent_s / 2 and not self.stale_warned:
            self.stale_warned = True
            self.bus.publish("gps_stale", minutes=round(quiet_s / 60, 1))
        if quiet_s >= self.silent_s and not self.silent_handled:
            # Phone off, page closed, no signal... or something worse. Losing contact mid-ride is a warning sign.
            self.silent_handled = True
            mins = round(quiet_s / 60)
            return await self._ask_then_alert(
                f"We haven't had your location for {mins} min during your cab ride",
                f"stopped sending their location {mins} min ago during a cab ride and didn't answer a safety check",
                "last known (their phone went silent)")
        if not self.stalled():
            return None
        last = self.points[-1]
        self.anchor = (last[1], last[2])
        mins = round(self.stall_s / 60)
        return await self._ask_then_alert(
            f"You haven't moved for {mins} min during your cab ride",
            f"didn't answer a safety check after not moving for {mins} min during a cab ride",
            "live (shared from your device)")

    async def _ask_then_alert(self, question: str, why_no_answer: str, source: str) -> str:
        self.busy = True
        try:
            last = self.points[-1]
            gen = self.generation
            answer = await self.prompt.check_in(question, self.timeout_s)
            if gen != self.generation or not self.active:
                return "cancelled"                # watching stopped or restarted meanwhile: never alert for it
            if answer in ("help", "timeout"):
                why = "asked for help during a cab ride" if answer == "help" else why_no_answer
                trip = {"from": self.note} if self.note else {}
                await self.alerter.alert(Alert(self.rider, why, self.rehearsal, Location(last[1], last[2], source, last[0]),
                                               trip, rehearsal=self.rehearsal))
            return answer
        finally:
            self.busy = False

    async def _loop(self) -> None:
        while self.active:
            try:
                await self.check()
            except Exception as e:
                log.warning("gps guard check failed: %s", e)
            await asyncio.sleep(15)
