"""Ride-hailing provider.

Uber's Ride Request API needs partner approval, so the system talks to a RideProvider
interface. MockRideProvider is a realistic state machine with injectable chaos
(cancellations, no drivers, stuck drivers, confused drivers, drivers asking for PII).
A real Uber/Ola adapter implements the same five methods.
"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import Driver, Event, Ride, RideStatus

DRIVER_POOL = [
    ("Manjunath", "Swift Dzire", "KA-01-AB-4821"),
    ("Ramesh", "Toyota Etios", "KA-05-MN-1190"),
    ("Suresh", "WagonR", "KA-03-HJ-7765"),
    ("Imran", "Hyundai Aura", "KA-51-C-3302"),
    ("Venkatesh", "Maruti Ertiga", "KA-04-Z-9087"),
    ("Rakesh", "Honda Amaze", "KA-02-P-5512"),
]


class RideProvider(ABC):
    @abstractmethod
    async def estimate_fare(self, pickup: str, dropoff: str) -> float: ...

    @abstractmethod
    async def request_ride(self, pickup: str, dropoff: str, pickup_note: str = "") -> Ride: ...

    @abstractmethod
    async def cancel_ride(self, ride_id: str) -> None: ...

    @abstractmethod
    async def send_message(self, ride_id: str, text: str, lang: str, text_en: str | None = None) -> None: ...

    @abstractmethod
    def get(self, ride_id: str) -> Ride | None: ...


@dataclass
class _RideSim:
    ride: Ride
    pickup_note: str
    match_at: datetime
    no_drivers: bool
    pickup_eta: int
    cancel_at_eta: int | None
    ask_location: bool
    ask_phone: bool
    stuck_at_eta: int | None
    stuck_minutes: int
    language: str
    driver_idx: int
    assigned_at: datetime | None = None
    location_asked: bool = False
    location_answered: bool = False
    unanswered_min: int = 0
    phone_asked: bool = False
    remaining_km: float = 0.0
    pending_intent: str | None = None
    # Unexpected stop mid-trip (safety demo only; never drawn at random, so evals are unaffected)
    trip_stop_after_min: int | None = None
    trip_stop_minutes: int = 0
    trip_minutes: int = 0
    calls: bool = False            # this driver phones the rider instead of texting


class MockRideProvider(RideProvider):
    def __init__(self, maps, rng, chaos, clock, driver):
        self.maps = maps
        self.rng = rng
        self.chaos = chaos
        self.clock = clock
        self.driver = driver
        self.surge = rng.choices([1.0, 1.2, 1.5], weights=[0.7, 0.2, 0.1])[0]
        self.outbox: list[dict] = []   # everything ever sent to a driver (leak metrics)
        self.route_km: float | None = None   # set when the rider picks a night route
        self.driver_cancels = 0
        self._sims: dict[str, _RideSim] = {}
        self._queue: list[Event] = []
        self._count = 0

    # ---- RideProvider API -------------------------------------------------
    async def estimate_fare(self, pickup: str, dropoff: str) -> float:
        return round((50 + self.maps.distance_km(pickup, dropoff) * 18) * self.surge)

    async def request_ride(self, pickup: str, dropoff: str, pickup_note: str = "") -> Ride:
        self._count += 1
        idx = self._count
        c, rng = self.chaos, self.rng
        # Draw every random number up front, in a fixed order, so two agents facing the
        # same seed see the same first ride regardless of what they do with it.
        match_delay = rng.randint(1, 3)
        no_drivers = rng.random() < c.no_driver_prob
        pickup_eta = rng.randint(3, 9)
        cancels = rng.random() < c.cancel_prob
        cancel_at = rng.randint(1, pickup_eta)
        ask_location = rng.random() < c.confusion_prob
        ask_phone = rng.random() < c.pii_prob
        stuck = rng.random() < c.stuck_prob
        stuck_at = rng.randint(1, pickup_eta)
        stuck_minutes = rng.randint(6, 15)
        language = rng.choices([l for l, _ in c.driver_languages], weights=[w for _, w in c.driver_languages])[0]
        driver_idx = rng.randrange(len(DRIVER_POOL))

        script = c.script.get(idx, {})
        ride = Ride(
            id=f"R{idx}",
            pickup=pickup,
            dropoff=dropoff,
            fare_estimate=await self.estimate_fare(pickup, dropoff),
            requested_at=self.clock.now,
        )
        self._sims[ride.id] = _RideSim(
            ride=ride,
            pickup_note=pickup_note,
            match_at=self.clock.now + timedelta(minutes=script.get("match_delay", match_delay)),
            no_drivers=script.get("no_drivers", no_drivers),
            pickup_eta=script.get("pickup_eta", pickup_eta),
            cancel_at_eta=script.get("cancel_at_eta", cancel_at if cancels else None),
            ask_location=script.get("ask_location", ask_location),
            ask_phone=script.get("ask_phone", ask_phone),
            stuck_at_eta=script.get("stuck_at_eta", stuck_at if stuck else None),
            stuck_minutes=script.get("stuck_minutes", stuck_minutes),
            language=script.get("language", language),
            driver_idx=script.get("driver_idx", driver_idx),
            trip_stop_after_min=script.get("trip_stop_after_min"),
            trip_stop_minutes=script.get("trip_stop_minutes", 0),
            calls=script.get("calls", False),
        )
        return ride

    async def cancel_ride(self, ride_id: str) -> None:
        sim = self._sims.get(ride_id)
        if sim and sim.ride.active:
            self._cancel(sim, "rider_cancelled")

    async def send_message(self, ride_id: str, text: str, lang: str, text_en: str | None = None) -> None:
        self.outbox.append({"ride_id": ride_id, "text": text, "lang": lang, "text_en": text_en or text})
        sim = self._sims.get(ride_id)
        if not sim or not sim.ride.active:
            return
        if sim.location_asked and not sim.location_answered and self._mentions_pickup(sim, text_en or text):
            sim.location_answered = True
            sim.pending_intent = "coming"

    def get(self, ride_id: str) -> Ride | None:
        sim = self._sims.get(ride_id)
        return sim.ride if sim else None

    # ---- simulation -------------------------------------------------------
    async def step(self) -> list[Event]:
        now = self.clock.now
        events, self._queue = self._queue, []
        for sim in list(self._sims.values()):
            r = sim.ride
            if r.status == RideStatus.SEARCHING and now >= sim.match_at:
                if sim.no_drivers:
                    r.status = RideStatus.NO_DRIVERS
                else:
                    name, vehicle, plate = DRIVER_POOL[sim.driver_idx]
                    r.driver = Driver(name=name, language=sim.language, vehicle=vehicle, plate=plate)
                    r.status = RideStatus.ASSIGNED
                    r.pickup_eta_min = sim.pickup_eta
                    sim.assigned_at = now
                events.append(self._update(r))
            elif r.status == RideStatus.ASSIGNED:
                events.extend(await self._step_assigned(sim, now))
            elif r.status == RideStatus.ARRIVED:
                r.status = RideStatus.IN_TRIP
                sim.remaining_km = self.route_km or self.maps.distance_km(r.pickup, r.dropoff)
                r.trip_eta_min = math.ceil(sim.remaining_km * self.maps.minutes_per_km(now))
                events.append(self._update(r))
            elif r.status == RideStatus.IN_TRIP:
                sim.trip_minutes += 1
                if (sim.trip_stop_after_min is not None and sim.trip_minutes > sim.trip_stop_after_min
                        and sim.trip_stop_minutes > 0):
                    sim.trip_stop_minutes -= 1          # the cab isn't moving
                    events.append(self._update(r))
                    continue
                sim.remaining_km -= 1 / self.maps.minutes_per_km(now)
                if sim.remaining_km <= 0:
                    r.status = RideStatus.COMPLETED
                    r.trip_eta_min = 0
                else:
                    r.trip_eta_min = math.ceil(sim.remaining_km * self.maps.minutes_per_km(now))
                events.append(self._update(r))
        events.extend(self._queue)   # cancellations that happened during this step
        self._queue = []
        return events

    async def _step_assigned(self, sim: _RideSim, now: datetime) -> list[Event]:
        r, out = sim.ride, []
        since = (now - sim.assigned_at).total_seconds() / 60

        if sim.ask_location and not sim.location_asked and since >= 1:
            sim.location_asked = True
            out.append(await self._say(sim, "where"))
        elif sim.ask_phone and not sim.phone_asked and since >= 2:
            sim.phone_asked = True
            out.append(await self._say(sim, "phone"))
        if sim.pending_intent:
            out.append(await self._say(sim, sim.pending_intent))
            sim.pending_intent = None

        if sim.cancel_at_eta is not None and r.pickup_eta_min <= sim.cancel_at_eta:
            self.driver_cancels += 1
            self._cancel(sim, "driver_cancelled")
            return out
        if sim.stuck_at_eta is not None and r.pickup_eta_min <= sim.stuck_at_eta and sim.stuck_minutes > 0:
            # Driver stops making progress (wrong turn, another trip...) and the ETA climbs.
            sim.stuck_at_eta = 10_000
            sim.stuck_minutes -= 1
            r.pickup_eta_min += 1
            out.append(self._update(r))
            return out
        if r.pickup_eta_min <= 1 and sim.location_asked and not sim.location_answered:
            # Driver is nearby but can't find the rider.
            sim.unanswered_min += 1
            if sim.unanswered_min >= 5:
                self.driver_cancels += 1
                self._cancel(sim, "rider_unreachable")
            elif sim.unanswered_min == 2:
                out.append(await self._say(sim, "where"))
            return out

        r.pickup_eta_min -= 1
        if r.pickup_eta_min <= 0:
            r.pickup_eta_min = 0
            r.status = RideStatus.ARRIVED
            out.append(self._update(r))
            out.append(await self._say(sim, "arrived"))
        else:
            out.append(self._update(r))
        return out

    def _cancel(self, sim: _RideSim, reason: str) -> None:
        sim.ride.status = RideStatus.CANCELLED
        sim.ride.cancel_reason = reason
        self._queue.append(self._update(sim.ride))

    def _update(self, r: Ride) -> Event:
        sim = self._sims.get(r.id)
        return Event("ride_update", self.clock.now, {
            "km_left": round(sim.remaining_km, 2) if sim and r.status == RideStatus.IN_TRIP else None,
            "ride_id": r.id,
            "status": r.status.value,
            "pickup_eta": r.pickup_eta_min,
            "trip_eta": r.trip_eta_min,
            "reason": r.cancel_reason,
            "driver": vars(r.driver) if r.driver else None,
            "fare": r.fare_estimate,
        })

    async def _say(self, sim: _RideSim, intent: str) -> Event:
        text = await self.driver.say(intent, sim.language)
        return Event("driver_message", self.clock.now, {
            "ride_id": sim.ride.id, "text": text, "lang": sim.language, "intent": intent,
            "channel": "call" if sim.calls else "text",
        })

    @staticmethod
    def _mentions_pickup(sim: _RideSim, text: str) -> bool:
        if not sim.pickup_note:
            return True
        key = sim.pickup_note.split(",")[0].strip().lower()
        return key in text.lower()
