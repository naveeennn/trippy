"""Night route options from real map data.

Routes:  OSRM (router.project-osrm.org), live, with up to three alternatives and road names.
Places:  OpenStreetMap shops, late-night amenities, mapped CCTV and street lamps, from a one-time extract
         (tripowner/data/blr_places.json; see data/fetch_osm.py). (c) OpenStreetMap contributors, ODbL.

Where a place has mapped opening hours we use them. Most don't (in Bengaluru only a few percent do), so the
rest are estimated from the kind of place, and the result says how much was known vs estimated.
OSRM has no live traffic: times are scaled by typical traffic for the hour, and labelled so.
"""
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

import httpx

from .maps import rush_hour_level
from .routes import SEGMENT_KM, RouteScore

log = logging.getLogger(__name__)

PLACES_FILE = Path(__file__).resolve().parent.parent / "data" / "blr_places.json"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
HEADERS = {"User-Agent": "TripOwner-hackathon/0.1 (night route scoring)"}
NEAR_M = 60                      # a place counts for the route within this distance
CELL = 0.002                     # spatial index cell (~220 m)

# Typical closing time by kind when OpenStreetMap has no opening hours (None = open all night).
DEFAULT_CLOSE = {
    "hospital": None, "police": None, "fuel": time(23, 0), "pharmacy": time(22, 30), "clinic": time(21, 0),
    "restaurant": time(23, 0), "fast_food": time(23, 0), "cafe": time(22, 0), "bar": time(23, 30),
    "pub": time(23, 30), "convenience": time(22, 0), "shop:convenience": time(22, 0),
    "shop:supermarket": time(22, 0), "shop:bakery": time(22, 0),
}
SHOP_DEFAULT_CLOSE = time(21, 0)
DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


# ---- opening hours ------------------------------------------------------------------------

def _days(spec: str) -> set[int] | None:
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            out |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
        elif part in DAYS:
            out.add(DAYS.index(part))
        else:
            return None
    return out


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_hours(text: str) -> dict[int, list[tuple[int, int]]] | None:
    """OSM opening_hours (common subset) -> {weekday: [(start_min, end_min)]}; end may exceed 1440.
    None if the format isn't one we understand."""
    text = text.strip()
    if text == "24/7":
        return {d: [(0, 1440)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    for rule in [r.strip() for r in text.split(";") if r.strip()]:
        if rule.startswith("PH"):
            continue                                    # public holidays: ignore
        m = re.fullmatch(r"(?:(?P<days>[A-Za-z,\- ]+?)\s+)?(?P<times>(?:\d{1,2}:\d{2}-\d{1,2}:\d{2}(?:\+)?\s*,?\s*)+|off|closed)", rule)
        if not m:
            return None
        days = _days(m.group("days").replace(" ", "")) if m.group("days") else set(range(7))
        if days is None:
            return None
        if m.group("times") in ("off", "closed"):
            for d in days:
                week[d] = []
            continue
        ranges = []
        for a, b in re.findall(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", m.group("times")):
            s, e = _mins(a), _mins(b)
            ranges.append((s, e + 1440 if e <= s else e))
        for d in days:
            week[d] = ranges
    return week


def open_at(hours: str | None, kind: str, at: datetime) -> tuple[bool, bool]:
    """-> (open?, known from mapped hours?)"""
    if hours:
        week = parse_hours(hours)
        if week is not None:
            now = at.hour * 60 + at.minute
            today, yesterday = at.weekday(), (at.weekday() - 1) % 7
            if any(s <= now < e for s, e in week.get(today, [])):
                return True, True
            if any(e > 1440 and now < e - 1440 for s, e in week.get(yesterday, [])):
                return True, True
            return False, True
    close = DEFAULT_CLOSE.get(kind, SHOP_DEFAULT_CLOSE if kind.startswith("shop:") else time(21, 0))
    if close is None:
        return True, False
    t = at.time()
    return (time(7, 0) <= t < close), False


# ---- geometry -----------------------------------------------------------------------------

def _meters(lat1, lon1, lat2, lon2) -> float:
    k = 111_320
    return math.hypot((lat2 - lat1) * k, (lon2 - lon1) * k * math.cos(math.radians(lat1)))


class PlaceIndex:
    def __init__(self, places: list[dict]):
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.places = places
        for i, p in enumerate(places):
            self.grid.setdefault((int(p["lat"] / CELL), int(p["lon"] / CELL)), []).append(i)

    def near(self, lat: float, lon: float, radius_m: float) -> list[int]:
        cy, cx, out = int(lat / CELL), int(lon / CELL), []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for i in self.grid.get((cy + dy, cx + dx), []):
                    p = self.places[i]
                    if _meters(lat, lon, p["lat"], p["lon"]) <= radius_m:
                        out.append(i)
        return out


_index: PlaceIndex | None = None


def place_index() -> PlaceIndex | None:
    global _index
    if _index is None and PLACES_FILE.exists():
        data = json.loads(PLACES_FILE.read_text(encoding="utf-8"))
        _index = PlaceIndex(data["places"])
    return _index


def _samples(coords: list[list[float]], step_m: float = 50) -> list[tuple[float, float, float]]:
    """Points every step_m along the line: (lat, lon, metres from start)."""
    out, dist = [(coords[0][1], coords[0][0], 0.0)], 0.0
    carry = 0.0
    for (lo1, la1), (lo2, la2) in zip(coords, coords[1:]):
        seg = _meters(la1, lo1, la2, lo2)
        pos = step_m - carry
        while pos <= seg:
            f = pos / seg
            out.append((la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f, dist + pos))
            pos += step_m
        carry = (carry + seg) % step_m
        dist += seg
    return out


# ---- routes -------------------------------------------------------------------------------

@dataclass
class OsmRoute:
    name: str
    via: str
    km: float
    minutes_free: float
    coords: list
    expressway_km: float


async def osrm_routes(a: tuple[float, float], b: tuple[float, float]) -> list[OsmRoute]:
    url = f"{OSRM_URL}/{a[1]},{a[0]};{b[1]},{b[0]}"
    params = {"alternatives": "3", "overview": "full", "geometries": "geojson", "steps": "true"}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        data = (await client.get(url, params=params)).json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM: {data.get('code')}")
    routes = []
    for rt in data["routes"]:
        names: dict[str, float] = {}
        exp = 0.0
        for leg in rt["legs"]:
            for st in leg["steps"]:
                if st.get("name"):
                    names[st["name"]] = names.get(st["name"], 0) + st["distance"]
                classes = {c for i in st.get("intersections", []) for c in i.get("classes", [])}
                if "motorway" in classes:
                    exp += st["distance"]
        top = [n for n, _ in sorted(names.items(), key=lambda kv: -kv[1])[:3]]
        routes.append(OsmRoute(top[0] if top else "Route", ", ".join(top[1:]) or "local roads",
                               round(rt["distance"] / 1000, 1), rt["duration"] / 60,
                               rt["geometry"]["coordinates"], round(exp / 1000, 1)))
    seen, unique = set(), []
    for r in routes:                      # alternatives can share their main road name
        name = r.name if r.name not in seen else f"{r.name} ({r.via.split(',')[0]})"
        seen.add(r.name)
        r.name = name
        unique.append(r)
    return unique


def score_osm_route(route: OsmRoute, at: datetime, index: PlaceIndex) -> RouteScore:
    samples = _samples(route.coords)
    n_seg = max(1, math.ceil(route.km / SEGMENT_KM))
    seen: set[int] = set()
    seg_open = [False] * n_seg
    shops_total = shops_open = known = cameras = lamps = 0
    for lat, lon, d in samples:
        seg = min(int(d / 1000 / SEGMENT_KM), n_seg - 1)
        for i in index.near(lat, lon, NEAR_M):
            if i in seen:
                continue
            seen.add(i)
            p = index.places[i]
            if p["kind"] == "camera":
                cameras += 1
            elif p["kind"] == "lamp":
                lamps += 1
            else:
                shops_total += 1
                is_open, is_known = open_at(p.get("hours"), p["kind"], at)
                known += is_known
                if is_open:
                    shops_open += 1
                    seg_open[seg] = True
    gap = best = 0.0
    for o in seg_open:
        gap = 0.0 if o else gap + SEGMENT_KM
        best = max(best, gap)
    coverage = sum(seg_open) / n_seg
    lamps_per_km = lamps / max(route.km, 0.1)
    cam_per_km = cameras / max(route.km, 0.1)
    exp_share = route.expressway_km / max(route.km, 0.1)
    # Same idea as the simulated score, but lighting and cameras only count where OSM has the data.
    parts = [(50, coverage), (25, max(0.0, 1 - best / 2.0)), (5, 1 - exp_share)]
    if lamps >= 5:
        parts.append((10, min(lamps_per_km / 10, 1.0)))
    if cameras >= 2:
        parts.append((10, min(cam_per_km / 0.6, 1.0)))
    score = round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts))
    factor = rush_hour_level(at)
    traffic = "light" if factor < 1.2 else "moderate" if factor < 1.6 else "heavy"
    s = RouteScore(route.name, route.via, route.km, round(route.minutes_free * factor), shops_total, shops_open,
                   round(best, 1), round(100 * coverage), cameras, round(cam_per_km, 2),
                   -1, route.expressway_km, traffic, score)
    s.source = "OpenStreetMap + OSRM"
    s.hours_known_pct = round(100 * known / shops_total) if shops_total else 0
    s.lamps = lamps
    return s


async def osm_route_options(a: tuple[float, float], b: tuple[float, float], at: datetime) -> list[RouteScore]:
    index = place_index()
    if index is None:
        raise RuntimeError("no OpenStreetMap extract (run python -m tripowner.data.fetch_osm)")
    scores = [score_osm_route(r, at, index) for r in await osrm_routes(a, b)]
    min(scores, key=lambda s: s.minutes).badges.append("fastest")
    min(scores, key=lambda s: s.km).badges.append("shortest")
    max(scores, key=lambda s: s.score).badges.append("busiest at night")
    return scores
