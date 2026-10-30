"""Other ways to get there when cabs keep failing: the metro and bike taxis.

Ride apps only ever offer another cab. The agent can compare the cab it's fighting for with getting
the rider there some other way, and suggest switching when that is clearly faster.

The Bengaluru metro network here is approximate (enough for believable door-to-door times):
Purple line: Whitefield, Indiranagar, MG Road, Majestic. Green line: Majestic, RV Road, Jayanagar.
Yellow line: RV Road, HSR Layout. Koramangala, Marathahalli and Hebbal have no station.

Estimates are what the agent sees. Actual times vary (metro waits, bike pickups, the odd bike-taxi
cancellation) and are drawn from the scenario's random seed, so switching is not a guaranteed win.
"""
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from .maps import MockMaps

# place -> (metro lines, minutes walking to the station)
METRO_ACCESS = {
    "MG Road": ({"purple"}, 4),
    "Indiranagar": ({"purple"}, 3),       # the usual pickup point is Gate 2 of the station itself
    "Whitefield": ({"purple"}, 8),
    "Jayanagar": ({"green"}, 7),
    "HSR Layout": ({"yellow"}, 10),
}
# Changing lines: purple <-> green at Majestic, green <-> yellow at RV Road.
LINE_HOPS = {
    frozenset({"purple"}): [], frozenset({"green"}): [], frozenset({"yellow"}): [],
    frozenset({"purple", "green"}): ["Majestic"],
    frozenset({"green", "yellow"}): ["RV Road"],
    frozenset({"purple", "yellow"}): ["Majestic", "RV Road"],
}
METRO_MIN_PER_KM = 2.0
METRO_WAIT_MIN = 5
TRANSFER_MIN = 6
BIKE_PICKUP_MIN = 5
BIKE_TRAFFIC_SHARE = 0.65     # bikes filter through traffic: ~65% of the car's time
BIKE_CANCEL_PROB = 0.10
BIKE_REBOOK_MIN = 8


@dataclass
class ModeOption:
    mode: str                 # "metro" | "bike_taxi"
    minutes: int              # estimated door-to-door minutes from now
    arrive_at: datetime
    fare_inr: int
    steps: str                # what the rider has to do, in one line

    def as_dict(self) -> dict:
        return {"mode": self.mode, "minutes": self.minutes, "arrive": f"{self.arrive_at:%H:%M}",
                "fare_inr": self.fare_inr, "steps": self.steps}


class TransitOptions:
    def __init__(self, maps: MockMaps, rng: random.Random, allow_bike: bool = True):
        self.maps = maps
        self.rng = rng
        self.allow_bike = allow_bike

    def metro(self, origin: str, dest: str, now: datetime) -> ModeOption | None:
        a, b = METRO_ACCESS.get(origin), METRO_ACCESS.get(dest)
        if not a or not b:
            return None
        hops = LINE_HOPS[frozenset(a[0] | b[0])] if a[0] != b[0] else []
        ride = max(4, round(self.maps.distance_km(origin, dest) * METRO_MIN_PER_KM))
        minutes = a[1] + METRO_WAIT_MIN + ride + TRANSFER_MIN * len(hops) + b[1]
        via = f", change at {' and '.join(hops)}" if hops else ""
        steps = (f"Walk {a[1]} min to {origin} metro, train to {dest}{via}, walk {b[1]} min")
        return ModeOption("metro", minutes, now + timedelta(minutes=minutes), 20 + 10 * (len(hops) + 1), steps)

    def bike_taxi(self, origin: str, dest: str, now: datetime, cab_fare: float) -> ModeOption | None:
        if not self.allow_bike:
            return None
        travel = self.maps.predict_minutes(origin, dest, now + timedelta(minutes=BIKE_PICKUP_MIN), now)
        minutes = BIKE_PICKUP_MIN + round(travel * BIKE_TRAFFIC_SHARE)
        return ModeOption("bike_taxi", minutes, now + timedelta(minutes=minutes), round(cab_fare * 0.45),
                          f"Bike taxi from your pickup point, ~{BIKE_PICKUP_MIN} min away; one rider, no luggage")

    def options(self, origin: str, dest: str, now: datetime, cab_fare: float) -> list[ModeOption]:
        found = [self.metro(origin, dest, now), self.bike_taxi(origin, dest, now, cab_fare)]
        return sorted((o for o in found if o), key=lambda o: o.arrive_at)

    def actual_minutes(self, option: ModeOption) -> int:
        """How long it really takes once the rider commits. Drawn from the scenario's random stream."""
        if option.mode == "metro":
            return option.minutes - METRO_WAIT_MIN + self.rng.randint(2, 9)          # the train you just missed
        extra = self.rng.randint(-2, 4)                                           # pickup varies
        if self.rng.random() < BIKE_CANCEL_PROB:
            extra += BIKE_REBOOK_MIN                                               # first bike cancels
        return option.minutes + extra
