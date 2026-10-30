"""One-tap booking through Telegram, for when there's no booking API.

The agent does everything up to the booking: it watches the calendar, works out when to leave, and at that
moment sends you a Telegram message with buttons that open Uber or Ola with pickup and drop already filled
in. You tap Confirm in the app, then tap "✅ Booked" (or "❌ No cab") in Telegram so the agent knows:
  ✅ Booked  -> ride_update "assigned": the agent keeps watching the clock and warns attendees if you'll be late
  ❌ No cab  -> ride_update "no_drivers": the agent backs off, sends a fresh link, or suggests the metro
  no answer  -> one reminder after REMIND_AFTER_MIN

Only used for real-clock trips from your calendar (watch mode), never in simulations or the public demo.
"""
import itertools
import logging
from urllib.parse import urlencode

from ..config import settings
from ..models import Event, Ride, RideStatus
from ..safety import PLACE_COORDS
from .rides import RideProvider

log = logging.getLogger(__name__)
REMIND_AFTER_MIN = 5
ASSUMED_PICKUP_MIN = 6        # we can't see the app, so assume a typical pickup time once you've booked


def handoff_enabled() -> bool:
    return (settings.ride_provider == "link" and bool(settings.telegram_bot_token and settings.telegram_chat_ids)
            and not settings.public_demo)


def uber_link(pickup: str, dropoff: str, pickup_note: str = "") -> str:
    """Uber's documented universal link: opens the app (or m.uber.com) with both ends set."""
    (a, b), (c, d) = PLACE_COORDS[pickup], PLACE_COORDS[dropoff]
    return "https://m.uber.com/ul/?" + urlencode({
        "action": "setPickup", "pickup[latitude]": a, "pickup[longitude]": b,
        "pickup[nickname]": pickup_note or pickup,
        "dropoff[latitude]": c, "dropoff[longitude]": d, "dropoff[nickname]": dropoff})


def ola_link(pickup: str, dropoff: str) -> str:
    """Ola's web booking link (undocumented by Ola, but checked: it opens with pickup and drop filled in)."""
    (a, b), (c, d) = PLACE_COORDS[pickup], PLACE_COORDS[dropoff]
    return "https://book.olacabs.com/?" + urlencode({"lat": a, "lng": b, "drop_lat": c, "drop_lng": d,
                                                     "pickup_name": pickup, "drop_name": dropoff})


class TelegramHandoff(RideProvider):
    handoff = True                 # the toolbox doesn't claim "booked" when this provider only sent a link

    def __init__(self, clock, maps, telegram=None, chat_id: str | None = None):
        if telegram is None:
            from .telegram import TelegramClient
            telegram = TelegramClient()
        self.clock, self.maps, self.tg = clock, maps, telegram
        self.chat_id = chat_id or settings.telegram_chat_ids[0]
        self._ids = itertools.count(1)
        self._rides: dict[str, Ride] = {}
        self._sent_at: dict[str, object] = {}
        self._notes: dict[str, str] = {}
        self._reminded: set[str] = set()
        self._seen: set[int] = set()      # Telegram update ids already handled (we never confirm, see step())
        self._queue: list[Event] = []
        self.outbox: list[dict] = []
        self.driver_cancels = 0
        self.route_km = None

    async def estimate_fare(self, pickup: str, dropoff: str) -> float:
        # Rough Bengaluru cab fare; the app shows the real price before you confirm.
        return round(50 + self.maps.distance_km(pickup, dropoff) * 18)

    async def request_ride(self, pickup: str, dropoff: str, pickup_note: str = "") -> Ride:
        ride = Ride(id=f"tap-{self.clock.now:%H%M}-{next(self._ids)}", pickup=pickup, dropoff=dropoff,
                    fare_estimate=await self.estimate_fare(pickup, dropoff), requested_at=self.clock.now,
                    status=RideStatus.SEARCHING)
        self._rides[ride.id] = ride
        await self._send(ride, pickup_note, reminder=False)
        return ride

    async def _send(self, ride: Ride, pickup_note: str, reminder: bool) -> None:
        head = "⏰ Still need a cab:" if reminder else "🚕 Time to book your cab."
        text = (f"TripOwner: {head} {ride.pickup} → {ride.dropoff}, about ₹{ride.fare_estimate:.0f}.\n"
                f"Tap Uber or Ola (pickup and drop are filled in), confirm there, then tell me how it went.")
        if pickup_note:
            text += f"\nPickup spot: {pickup_note}"
        keyboard = [[{"text": "Open Uber", "url": uber_link(ride.pickup, ride.dropoff, pickup_note)},
                     {"text": "Open Ola", "url": ola_link(ride.pickup, ride.dropoff)}],
                    [{"text": "✅ Booked", "callback_data": f"{ride.id}:ok"},
                     {"text": "❌ No cab", "callback_data": f"{ride.id}:fail"}]]
        await self.tg.send_message(self.chat_id, text, keyboard=keyboard)
        self._sent_at[ride.id] = self.clock.now
        self._notes[ride.id] = pickup_note

    async def cancel_ride(self, ride_id: str) -> None:
        ride = self._rides.get(ride_id)
        if not ride:
            return
        was_booked = ride.status == RideStatus.ASSIGNED
        ride.status, ride.cancel_reason = RideStatus.CANCELLED, "rider_cancelled"
        msg = ("TripOwner: plans changed. Please cancel the cab you booked in the app."
               if was_booked else "TripOwner: ignore that booking link, plans changed.")
        await self.tg.send_message(self.chat_id, msg)

    async def send_message(self, ride_id: str, text: str, lang: str, text_en: str | None = None) -> None:
        # No line to the driver here: the app's own chat and calls handle that.
        self.outbox.append({"ride_id": ride_id, "text": text, "lang": lang, "text_en": text_en or text,
                            "delivered": False})

    def get(self, ride_id: str) -> Ride | None:
        return self._rides.get(ride_id)

    def _update(self, ride: Ride) -> Event:
        return Event("ride_update", self.clock.now, {
            "ride_id": ride.id, "status": ride.status.value, "pickup_eta": ride.pickup_eta_min,
            "trip_eta": None, "reason": ride.cancel_reason, "km_left": None, "driver": None,
            "fare": ride.fare_estimate})

    async def step(self) -> list[Event]:
        events, self._queue = self._queue, []
        waiting = [r for r in self._rides.values() if r.status == RideStatus.SEARCHING]
        if not waiting:
            return events
        # Read button presses without confirming them, so the safety alerter's own polling isn't disturbed.
        try:
            updates = await self.tg.updates()
        except Exception as e:
            log.warning("Telegram check failed: %s", e)
            updates = []
        for u in updates:
            q = u.get("callback_query")
            if not q or u.get("update_id") in self._seen:
                continue
            self._seen.add(u.get("update_id"))
            ride_id, _, answer = (q.get("data") or "").rpartition(":")
            ride = self._rides.get(ride_id)
            if not ride or ride.status != RideStatus.SEARCHING or str((q.get("from") or {}).get("id")) != self.chat_id:
                continue
            if answer == "ok":
                ride.status, ride.pickup_eta_min = RideStatus.ASSIGNED, ASSUMED_PICKUP_MIN
                reply = "Got it. I'll keep an eye on the time."
            else:
                ride.status = RideStatus.NO_DRIVERS
                reply = "OK, I'll try again shortly or find another way."
            try:
                await self.tg.answer_callback(q["id"], reply)
            except Exception:
                pass
            events.append(self._update(ride))
        for ride in waiting:
            if ride.status == RideStatus.SEARCHING and ride.id not in self._reminded and \
                    (self.clock.now - self._sent_at[ride.id]).total_seconds() >= REMIND_AFTER_MIN * 60:
                self._reminded.add(ride.id)
                await self._send(ride, self._notes[ride.id], reminder=True)
        return events
