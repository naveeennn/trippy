"""Download places for night-route scoring from OpenStreetMap (one-time, then committed as a small file).

    python -m tripowner.data.fetch_osm

Covers the planner's part of Bengaluru in tiles, with retries: the public Overpass server is often busy.
Keeps only what the scoring needs: position, kind (shop / amenity / camera / street lamp) and opening hours.
Data (c) OpenStreetMap contributors, available under the Open Database License (ODbL).
"""
import json
import time
from pathlib import Path

import httpx

OUT = Path(__file__).with_name("blr_places.json")
BBOX = (12.89, 77.57, 13.06, 77.77)            # south, west, north, east: covers the eight planner areas
TILE = 0.05
SERVERS = ["https://overpass-api.de/api/interpreter", "https://overpass.osm.jp/api/interpreter",
           "https://overpass.private.coffee/api/interpreter", "https://overpass.openstreetmap.ru/api/interpreter"]
HEADERS = {"User-Agent": "TripOwner-hackathon/0.1 (night route scoring; one-time extract)"}
AMENITIES = "pharmacy|hospital|clinic|fuel|restaurant|fast_food|cafe|police|convenience|bar|pub"


def tiles():
    s, w, n, e = BBOX
    lat = s
    while lat < n:
        lon = w
        while lon < e:
            t = (round(lat, 3), round(lon, 3), round(min(lat + TILE, n), 3), round(min(lon + TILE, e), 3))
            if t[2] > t[0] + 1e-9 and t[3] > t[1] + 1e-9:     # skip zero-width edge tiles
                yield t
            lon += TILE
        lat += TILE


def query(b) -> str:
    bb = f"{b[0]},{b[1]},{b[2]},{b[3]}"
    return (f'[out:json][timeout:120];(nwr["shop"]({bb});nwr["amenity"~"^({AMENITIES})$"]({bb});'
            f'node["man_made"="surveillance"]({bb});node["highway"="street_lamp"]({bb}););out center tags;')


def slim(el) -> dict | None:
    t = el.get("tags", {})
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None
    if t.get("man_made") == "surveillance":
        kind = "camera"
    elif t.get("highway") == "street_lamp":
        kind = "lamp"
    elif "amenity" in t:
        kind = t["amenity"]
    else:
        kind = "shop:" + t.get("shop", "yes")
    item = {"lat": round(lat, 6), "lon": round(lon, 6), "kind": kind}
    if "opening_hours" in t:
        item["hours"] = t["opening_hours"]
    return item


def fetch_tile(client, b, attempts=12) -> list[dict]:
    for i in range(attempts):
        url = SERVERS[i % len(SERVERS)]
        try:
            r = client.post(url, data={"data": query(b)}, timeout=180)
            if "json" in r.headers.get("content-type", ""):
                return [x for x in (slim(e) for e in r.json().get("elements", [])) if x]
            print(f"  {url.split('/')[2]}: HTTP {r.status_code}, retrying")
        except Exception as e:
            print(f"  {url.split('/')[2]}: {type(e).__name__}, retrying")
        time.sleep(min(60, 8 * (i + 1)))
    raise RuntimeError(f"tile {b} failed after {attempts} attempts")


def main() -> None:
    done = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"tiles": {}, "places": []}
    with httpx.Client(headers=HEADERS) as client:
        for b in tiles():
            key = ",".join(map(str, b))
            if key in done["tiles"]:
                continue
            items = fetch_tile(client, b)
            done["places"] += items
            done["tiles"][key] = len(items)
            done["attribution"] = "(c) OpenStreetMap contributors, ODbL"
            OUT.write_text(json.dumps(done, separators=(",", ":")), encoding="utf-8")
            print(f"tile {key}: {len(items)} items (total {len(done['places'])})")
            time.sleep(5)          # be polite to the public server
    print("done:", OUT, len(done["places"]), "items")


if __name__ == "__main__":
    main()
