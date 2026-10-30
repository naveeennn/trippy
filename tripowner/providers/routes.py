"""Route options for night trips: not just the fastest way, but how busy and watched each way is.

For each trip there are a few candidate routes. Each is cut into ~500 m stretches, and each stretch has
shops (with closing times), CCTV cameras, lighting and a road type. At the departure time we work out:

  - shops on the route, and how many are open right now
  - the longest stretch with nothing open (shops spread out beats shops bunched in one place)
  - how much of the route has something open nearby
  - CCTV cameras per km, share of the route that's lit, km on expressways
  - typical traffic for the hour

SIMULATED MAP DATA. The shops, cameras and lighting here are generated, not surveyed. A real version would
read routes from a routing service and places from OpenStreetMap. Scores are about how busy and visible a
route is, never a promise that it is "safe".
"""
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .maps import MockMaps

SEGMENT_KM = 0.5
NIGHT_FROM, NIGHT_UNTIL = time(21, 0), time(6, 0)

# Road types: how dense shops are, how likely a stretch has a camera / lights, speed vs normal traffic.
ROAD = {
    "expressway": {"shops": (0, 1), "cctv": 0.12, "lit": 0.92, "speed": 0.75, "late_share": 0.5},
    "arterial":   {"shops": (1, 6), "cctv": 0.45, "lit": 0.85, "speed": 1.0, "late_share": 0.35},
    "residential": {"shops": (0, 3), "cctv": 0.10, "lit": 0.45, "speed": 1.15, "late_share": 0.15},
}
# Closing times for a shop; None = open all night (petrol pumps, pharmacies, hospitals).
CLOSES = [time(21, 0), time(22, 0), time(23, 0), time(23, 30), time(1, 0), None]


def is_night(at: datetime) -> bool:
    t = at.time()
    return t >= NIGHT_FROM or t < NIGHT_UNTIL


def _open_at(closes: time | None, at: datetime) -> bool:
    """Shops open in the morning; this only decides whether it's still open late."""
    if closes is None:
        return True
    t = at.time()
    if closes < time(6, 0):                  # closes after midnight
        return t >= time(6, 0) or t < closes
    return time(6, 0) <= t < closes


@dataclass
class Segment:
    road: str
    shops: list                              # closing time per shop (None = all night)
    cctv: int
    lit: bool


@dataclass
class Route:
    name: str
    via: str
    km: float
    segments: list[Segment] = field(default_factory=list)

    def road_km(self, road: str) -> float:
        return round(sum(SEGMENT_KM for s in self.segments if s.road == road), 1)


@dataclass
class RouteScore:
    name: str
    via: str
    km: float
    minutes: int
    shops_total: int
    shops_open: int
    longest_gap_km: float           # longest stretch with no open shop
    open_coverage_pct: int          # stretches with something open
    cctv: int
    cctv_per_km: float
    lit_pct: int
    expressway_km: float
    traffic: str                    # light | moderate | heavy
    score: int                      # 0-100, higher = busier and more watched
    badges: list = field(default_factory=list)
    source: str = "simulated map data"
    hours_known_pct: int | None = None   # share of places whose opening hours are mapped (real data only)
    lamps: int | None = None             # mapped street lamps (real data only)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _segments(rng: random.Random, km: float, mix: list[tuple[str, float]]) -> list[Segment]:
    """Lay roads out along the route in order (mix = [(road, share of route)])."""
    n = max(1, math.ceil(km / SEGMENT_KM))
    roads = []
    for road, share in mix:
        roads += [road] * round(n * share)
    roads = (roads + [mix[-1][0]] * n)[:n]
    out = []
    for road in roads:
        p = ROAD[road]
        shops = []
        for _ in range(rng.randint(*p["shops"])):
            late = rng.random() < p["late_share"]
            shops.append(rng.choice(CLOSES[3:]) if late else rng.choice(CLOSES[:3]))
        out.append(Segment(road, shops, 1 if rng.random() < p["cctv"] else 0, rng.random() < p["lit"]))
    return out


# Hand-made options for the demo pair, using real road names (the data on them is still simulated).
NAMED = {
    ("Indiranagar", "Whitefield"): [
        ("Outer Ring Road", "ORR and Marathahalli bridge", 1.08,
         [("arterial", 0.2), ("expressway", 0.55), ("arterial", 0.25)]),
        ("Old Madras Road", "KR Puram, Hoodi", 0.97,
         [("arterial", 0.35), ("residential", 0.45), ("arterial", 0.2)]),
        ("Old Airport Road", "HAL, Marathahalli market, Varthur Road", 1.12,
         [("arterial", 0.85), ("residential", 0.15)]),
    ],
}


def candidate_routes(origin: str, dest: str, maps: MockMaps) -> list[Route]:
    base = maps.distance_km(origin, dest)
    named = NAMED.get((origin, dest)) or NAMED.get((dest, origin))
    if not named:
        named = [("Main road", "the arterial roads", 1.05, [("arterial", 0.8), ("residential", 0.2)]),
                 ("Expressway", "the faster highway stretch", 1.15, [("arterial", 0.25), ("expressway", 0.75)]),
                 ("Inner roads", "residential shortcuts", 0.95, [("residential", 0.7), ("arterial", 0.3)])]
    routes = []
    for name, via, factor, mix in named:
        rng = random.Random(f"{origin}>{dest}>{name}")
        km = round(base * factor, 1)
        routes.append(Route(name, via, km, _segments(rng, km, mix)))
    return routes


def score_route(route: Route, at: datetime, maps: MockMaps) -> RouteScore:
    open_flags = [any(_open_at(c, at) for c in s.shops) for s in route.segments]
    gap = best_gap = 0.0
    for has_open in open_flags:
        gap = 0.0 if has_open else gap + SEGMENT_KM
        best_gap = max(best_gap, gap)
    congestion = maps.congestion(at)
    minutes = round(sum(SEGMENT_KM * 2.6 * congestion * ROAD[s.road]["speed"] for s in route.segments)) + 2
    shops_total = sum(len(s.shops) for s in route.segments)
    shops_open = sum(1 for s in route.segments for c in s.shops if _open_at(c, at))
    cctv = sum(s.cctv for s in route.segments)
    n = len(route.segments)
    coverage = sum(open_flags) / n
    lit = sum(s.lit for s in route.segments) / n
    cctv_per_km = cctv / max(route.km, 0.1)
    exp_share = route.road_km("expressway") / max(route.km, 0.1)
    # Transparent score: coverage and short gaps matter most; cameras and light next; long empty expressway
    # stretches count slightly against at night (fast, but nowhere to stop if something goes wrong).
    score = (45 * coverage + 20 * max(0.0, 1 - best_gap / 2.0) + 15 * min(cctv_per_km / 0.6, 1.0)
             + 15 * lit + 5 * (1 - exp_share))
    traffic = "light" if congestion < 1.2 else "moderate" if congestion < 1.6 else "heavy"
    return RouteScore(route.name, route.via, route.km, minutes, shops_total, shops_open, round(best_gap, 1),
                      round(100 * coverage), cctv, round(cctv_per_km, 2), round(100 * lit),
                      route.road_km("expressway"), traffic, round(score))


def route_options(origin: str, dest: str, at: datetime, maps: MockMaps) -> list[RouteScore]:
    scores = [score_route(r, at, maps) for r in candidate_routes(origin, dest, maps)]
    min(scores, key=lambda s: s.minutes).badges.append("fastest")
    min(scores, key=lambda s: s.km).badges.append("shortest")
    max(scores, key=lambda s: s.score).badges.append("busiest at night")
    return scores


def recommend(scores: list[RouteScore], max_extra_min: int = 10) -> RouteScore:
    """The busiest, best-watched route, unless it costs more than max_extra_min over the fastest."""
    fastest = min(scores, key=lambda s: s.minutes)
    ok = [s for s in scores if s.minutes - fastest.minutes <= max_extra_min]
    return max(ok, key=lambda s: (s.score, -s.minutes))


# ---- letting the rider choose ---------------------------------------------------------------

class AutoChoose:
    """Simulations and evals: take the recommendation."""

    async def choose(self, options: list[RouteScore], recommended: RouteScore) -> tuple[str, str]:
        return recommended.name, "recommended"


class DashboardRouteChoice:
    """Show the options on the dashboard; the rider picks, or the recommendation wins after a timeout."""

    def __init__(self, bus, timeout_s: float = 20):
        import itertools
        self.bus, self.timeout_s = bus, timeout_s
        self._pending: dict = {}
        self._ids = itertools.count(1)

    async def choose(self, options: list[RouteScore], recommended: RouteScore) -> tuple[str, str]:
        import asyncio
        rid = f"R{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = (fut, {o.name for o in options})
        self.bus.publish("route_choice", id=rid, options=[o.as_dict() for o in options],
                         recommended=recommended.name, timeout_s=self.timeout_s)
        try:
            return await asyncio.wait_for(fut, self.timeout_s), "you chose"
        except asyncio.TimeoutError:
            return recommended.name, "recommended (no answer)"
        finally:
            self._pending.pop(rid, None)

    def resolve(self, rid: str, name: str) -> bool:
        item = self._pending.get(rid)
        if not item or item[0].done() or name not in item[1]:
            return False
        item[0].set_result(name)
        return True


async def get_route_options(origin: str, dest: str, at: datetime, maps: MockMaps,
                            real: bool = False) -> tuple[list[RouteScore], str | None]:
    """Real routes and places when asked and available, else the simulated ones. -> (options, fallback note)"""
    if real:
        from ..safety import PLACE_COORDS
        from .osm_routes import osm_route_options
        a, b = PLACE_COORDS.get(origin), PLACE_COORDS.get(dest)
        if a and b:
            try:
                return await osm_route_options(a, b, at), None
            except Exception as e:
                return route_options(origin, dest, at, maps), f"real map data unavailable ({type(e).__name__}); showing simulated"
    return route_options(origin, dest, at, maps), None
