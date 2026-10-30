"""Real rides through Uber for Business: the Guest Rides API.

Implements the same RideProvider interface as the simulator, so the agent doesn't change:
  estimate_fare  -> POST /v1/guests/trips/estimates   (picks the ride type, keeps its fare_id)
  request_ride   -> POST /v1/guests/trips             (guest = you; pickup landmark goes in note_for_driver)
  step           -> GET  /v1/guests/trips/{id}        (polled; status changes become ride_update events)
  cancel_ride    -> DELETE /v1/guests/trips/{id}
  send_message   -> not available in this API (the driver gets the note at booking, and can call you)

Needs an Uber for Business organisation with API access (the guests.trips scope). Sandbox first:
set UBER_SANDBOX_RUN_ID and requests carry Uber's sandbox header, so no real cars are dispatched.
Only used for real-clock trips from your calendar, never in simulations or the public demo.
Docs: https://developer.uber.com/docs/guest-rides/introduction
"""
import logging
import time
from datetime import datetime

import httpx

from ..config import settings
from ..models import Driver, Event, Ride, RideStatus
from ..safety import PLACE_COORDS
from .rides import RideProvider

log = logging.getLogger(__name__)

STATUS = {
    "processing": RideStatus.SEARCHING, "driver_redispatched": RideStatus.SEARCHING, "offered": RideStatus.SEARCHING,
    "scheduled": RideStatus.SEARCHING,
    "no_drivers_available": RideStatus.NO_DRIVERS,
    "accepted": RideStatus.ASSIGNED,
    "arriving": RideStatus.ARRIVED,
    "in_progress": RideStatus.IN_TRIP,
    "completed": RideStatus.COMPLETED,
    "driver_canceled": RideStatus.CANCELLED, "rider_canceled": RideStatus.CANCELLED,
    "failed": RideStatus.CANCELLED, "expired": RideStatus.CANCELLED,
}
REASON = {"driver_canceled": "driver_cancelled", "rider_canceled": "rider_cancelled",
          "failed": "uber_failed", "expired": "uber_expired"}


def uber_enabled() -> bool:
    return (settings.ride_provider == "uber" and bool(settings.uber_client_id and settings.uber_client_secret)
            and not settings.public_demo)


class UberGuestRides(RideProvider):
    def __init__(self, clock, maps, transport: httpx.AsyncBaseTransport | None = None):
        self.clock, self.maps = clock, maps
        self._http = httpx.AsyncClient(base_url=settings.uber_api_base, timeout=20, transport=transport)
        self._auth = httpx.AsyncClient(timeout=20, transport=transport)
        self._token: tuple[str, float] | None = None
        self._rides: dict[str, Ride] = {}
        self._last: dict[str, tuple] = {}          # request_id -> (status, eta) last reported
        self._trip_start: dict[str, tuple[datetime, int]] = {}
        self._quote: dict[tuple[str, str], tuple[str, str | None, float]] = {}   # (from, to) -> product, fare_id, fare
        self.outbox: list[dict] = []
        self.driver_cancels = 0
        self.route_km = None                       # night routes: Uber chooses its own route

    # ---- plumbing ---------------------------------------------------------------------------
    async def _headers(self) -> dict:
        if not self._token or time.time() > self._token[1] - 300:
            r = await self._auth.post(settings.uber_auth_url, data={
                "client_id": settings.uber_client_id, "client_secret": settings.uber_client_secret,
                "grant_type": "client_credentials", "scope": "guests.trips"})
            r.raise_for_status()
            j = r.json()
            self._token = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        h = {"authorization": f"Bearer {self._token[0]}", "content-type": "application/json"}
        if settings.uber_org_uuid:
            h["x-uber-organizationuuid"] = settings.uber_org_uuid
        if settings.uber_sandbox_run_id:
            h["x-uber-sandbox-runuuid"] = settings.uber_sandbox_run_id
        return h

    @staticmethod
    def _point(place: str) -> dict:
        lat, lon = PLACE_COORDS[place]
        return {"latitude": lat, "longitude": lon}

    # ---- RideProvider -------------------------------------------------------------------------
    async def estimate_fare(self, pickup: str, dropoff: str) -> float:
        r = await self._http.post("/v1/guests/trips/estimates", headers=await self._headers(),
                                  json={"pickup": self._point(pickup), "dropoff": self._point(dropoff)})
        r.raise_for_status()
        options = []
        for p in r.json().get("product_estimates", []):
            info = p.get("estimate_info") or {}
            fare = (info.get("fare") or {}).get("value")
            if fare is None and info.get("estimate"):
                e = info["estimate"]
                fare = (e.get("low_estimate", 0) + e.get("high_estimate", 0)) / 2
            if fare is not None:
                options.append((p["product"].get("display_name", ""), p["product"]["product_id"], info.get("fare_id"), float(fare)))
        if not options:
            raise RuntimeError("Uber returned no ride options here right now")
        want = settings.uber_product.lower()
        chosen = next((o for o in options if want and want in o[0].lower()), min(options, key=lambda o: o[3]))
        self._quote[(pickup, dropoff)] = (chosen[1], chosen[2], chosen[3])
        return round(chosen[3])

    async def request_ride(self, pickup: str, dropoff: str, pickup_note: str = "") -> Ride:
        if (pickup, dropoff) not in self._quote:
            await self.estimate_fare(pickup, dropoff)
        product_id, fare_id, fare = self._quote[(pickup, dropoff)]
        first, *rest = (settings.user_name or "Rider").split()
        body = {
            "guest": {"first_name": first[:45], "last_name": (" ".join(rest) or "-")[:45],
                      "phone_number": settings.uber_guest_phone},
            "pickup": {**self._point(pickup), "address": pickup_note or pickup},
            "dropoff": {**self._point(dropoff), "address": dropoff},
            "product_id": product_id,
            "note_for_driver": (f"Pickup: {pickup_note}" if pickup_note else "")[:600],
            "call_enabled": True,
        }
        if fare_id:
            body["fare_id"] = fare_id
        r = await self._http.post("/v1/guests/trips", headers=await self._headers(), json=body)
        r.raise_for_status()
        j = r.json()
        ride = Ride(id=j["request_id"], pickup=pickup, dropoff=dropoff, fare_estimate=fare,
                    requested_at=self.clock.now, status=STATUS.get(j.get("status", "processing"), RideStatus.SEARCHING))
        self._rides[ride.id] = ride
        self._last[ride.id] = (j.get("status", "processing"), None, None)   # only report changes from here
        log.info("Uber trip requested: %s (%s)", ride.id, "sandbox" if settings.uber_sandbox_run_id else "PRODUCTION")
        return ride

    async def cancel_ride(self, ride_id: str) -> None:
        r = await self._http.delete(f"/v1/guests/trips/{ride_id}", headers=await self._headers())
        r.raise_for_status()

    async def send_message(self, ride_id: str, text: str, lang: str, text_en: str | None = None) -> None:
        # Guest Rides has no messaging endpoint; record it so the dashboard shows what would have been said.
        self.outbox.append({"ride_id": ride_id, "text": text, "lang": lang, "text_en": text_en or text,
                            "delivered": False})

    def get(self, ride_id: str) -> Ride | None:
        return self._rides.get(ride_id)

    async def step(self) -> list[Event]:
        """Poll every active trip once (the world calls this once per real minute)."""
        events = []
        for ride in list(self._rides.values()):
            if not ride.active:
                continue
            try:
                r = await self._http.get(f"/v1/guests/trips/{ride.id}", headers=await self._headers())
                r.raise_for_status()
                j = r.json()
            except Exception as e:
                log.warning("Uber status check failed for %s: %s", ride.id, e)
                continue
            raw = j.get("status", "processing")
            ride.status = STATUS.get(raw, ride.status)
            if raw in REASON:
                ride.cancel_reason = REASON[raw]
                if raw == "driver_canceled":
                    self.driver_cancels += 1
            d, v = j.get("driver") or {}, j.get("vehicle") or {}
            if d and ride.driver is None:
                ride.driver = Driver(name=d.get("name", "Driver"), language="en-IN",
                                     vehicle=" ".join(x for x in (v.get("make"), v.get("model")) if x) or "Car",
                                     plate=v.get("license_plate", ""))
            ride.pickup_eta_min = (j.get("pickup") or {}).get("eta")
            if ride.status == RideStatus.IN_TRIP:
                start = self._trip_start.setdefault(ride.id, (self.clock.now, self.maps.predict_minutes(
                    ride.pickup, ride.dropoff, self.clock.now, self.clock.now)))
                ride.trip_eta_min = max(0, start[1] - int((self.clock.now - start[0]).total_seconds() // 60))
            key = (raw, ride.pickup_eta_min, ride.trip_eta_min)
            if self._last.get(ride.id) != key:
                self._last[ride.id] = key
                events.append(Event("ride_update", self.clock.now, {
                    "ride_id": ride.id, "status": ride.status.value, "pickup_eta": ride.pickup_eta_min,
                    "trip_eta": ride.trip_eta_min, "reason": ride.cancel_reason, "km_left": None,
                    "driver": vars(ride.driver) if ride.driver else None, "fare": ride.fare_estimate,
                    "uber_status": raw}))
        return events
