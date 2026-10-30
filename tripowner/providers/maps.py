"""Travel-time provider. MockMaps models Bengaluru-ish distances and rush-hour traffic.

Swap in a Google Maps Routes API implementation of MapsProvider for real use
(departure_time = depart_at, traffic_model = best_guess).
"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

# Rough km offsets from MG Road. Good enough for believable travel times.
PLACES: dict[str, tuple[float, float]] = {
    "MG Road": (0, 0),
    "Indiranagar": (4, 1),
    "Koramangala": (2, -5),
    "HSR Layout": (5, -8),
    "Whitefield": (13, 2),
    "Marathahalli": (9, -1),
    "Hebbal": (1, 9),
    "Jayanagar": (-2, -6),
}

ROAD_FACTOR = 1.3       # roads aren't straight lines
MIN_PER_KM = 2.6        # free-ish flow in the city
FIXED_OVERHEAD_MIN = 2  # getting out of the lane, parking


@dataclass
class TrafficSpike:
    start: datetime
    end: datetime
    multiplier: float


class MapsProvider(ABC):
    @abstractmethod
    async def travel_minutes(self, origin: str, dest: str, depart_at: datetime, now: datetime) -> int:
        """Predicted door-to-door minutes leaving at depart_at, using what's known at `now`."""

    @abstractmethod
    def distance_km(self, origin: str, dest: str) -> float: ...


def rush_hour_level(at: datetime) -> float:
    h = at.hour + at.minute / 60
    return 1.45 if (8.5 <= h < 11 or 17 <= h < 20.5) else 1.1


class MockMaps(MapsProvider):
    """Rush hour is predictable (like real Maps). Spikes aren't: a prediction only
    includes a spike once it has started, and assumes it lasts the whole trip."""

    def __init__(self, spikes: list[TrafficSpike] | None = None):
        self.spikes = list(spikes or [])

    def distance_km(self, origin: str, dest: str) -> float:
        (x1, y1), (x2, y2) = PLACES[origin], PLACES[dest]
        return math.hypot(x2 - x1, y2 - y1) * ROAD_FACTOR

    def _spike_factor(self, at: datetime) -> float:
        f = 1.0
        for s in self.spikes:
            if s.start <= at < s.end:
                f *= s.multiplier
        return f

    def congestion(self, at: datetime) -> float:
        """Actual congestion at a moment (used by the ride simulation)."""
        return rush_hour_level(at) * self._spike_factor(at)

    def minutes_per_km(self, at: datetime) -> float:
        return MIN_PER_KM * self.congestion(at)

    def predict_minutes(self, origin: str, dest: str, depart_at: datetime, now: datetime | None = None) -> int:
        known_spike = self._spike_factor(now) if now else 1.0
        remaining, t, minutes = self.distance_km(origin, dest), depart_at, 0
        while remaining > 0:
            remaining -= 1 / (MIN_PER_KM * rush_hour_level(t) * known_spike)
            t += timedelta(minutes=1)
            minutes += 1
        return max(5, minutes + FIXED_OVERHEAD_MIN)

    async def travel_minutes(self, origin: str, dest: str, depart_at: datetime, now: datetime) -> int:
        return self.predict_minutes(origin, dest, depart_at, now)
