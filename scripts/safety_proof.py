"""Live proof of the safety chain, end to end, against a running TripOwner server.

It behaves exactly like the rider's phone during a cab ride: it switches on the GPS guard ("I'm in a cab"),
then sends location fixes along a real road route (OpenStreetMap/OSRM, Indiranagar -> Koramangala), and
then parks: the same spot with realistic GPS jitter. Nobody answers "Are you safe?", so TripOwner must alert
the emergency contacts: Telegram (with a map pin) and a phone call. Every event is recorded with a timestamp
and written to proof/safety_<time>.md and .json.

  python scripts/safety_proof.py                     # against http://localhost:8765, 1-minute threshold
  python scripts/safety_proof.py --answer ok          # answer "I'm OK" instead: no alert must go out
  python scripts/safety_proof.py --stall-min 5        # the real threshold (takes longer)

Alerts go out for real, labelled as a test ("TripOwner DRILL: no action needed").
"""
import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tripowner.providers.osm_routes import osrm_routes  # noqa: E402
from tripowner.safety import PLACE_COORDS  # noqa: E402

KEEP = {"gps_guard", "safety_checkin", "safety_answer", "sos_sent", "sos_call", "sos_ack", "gps_stale"}


def along(coords: list, step_m: float) -> list[tuple[float, float]]:
    """Points every step_m metres along an OSRM [lon, lat] polyline."""
    out, carry = [], 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        seg = ((lat2 - lat1) * 111_320) ** 2 + ((lon2 - lon1) * 108_600) ** 2
        seg **= 0.5
        d = carry
        while d < seg:
            f = d / seg
            out.append((lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f))
            d += step_m
        carry = d - seg
    return out


async def send_fix(http, lat: float, lon: float) -> None:
    """Like a phone: if one location update doesn't get through, the next one will."""
    try:
        await http.post("/api/location", json={"lat": lat, "lon": lon})
    except httpx.HTTPError as e:
        print(f"  (a location update didn't get through: {type(e).__name__}; carrying on)", flush=True)


async def main(a) -> int:
    base = a.server.rstrip("/")
    events: list[dict] = []
    t0 = time.time()

    def note(kind: str, **kw):
        events.append({"t": round(time.time() - t0, 1), "at": datetime.now().strftime("%H:%M:%S"), "event": kind, **kw})
        print(f"[{events[-1]['at']} +{events[-1]['t']:>6}s] {kind}: {json.dumps(kw, ensure_ascii=False)[:160]}")

    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        health = (await http.get("/api/health")).json()
        channels = health.get("alert_channels", [])
        note("server", channels=channels, public_demo=health.get("public_demo"))
        if health.get("public_demo"):
            print("This server is the public demo: it never sends real alerts. Run it locally.")
            return 2

        routes = await osrm_routes(PLACE_COORDS["Indiranagar"], PLACE_COORDS["Koramangala"])
        path = along(routes[0].coords, a.speed_kmh / 3.6 * a.every)
        note("route", source="OSRM / OpenStreetMap", name=routes[0].name, km=routes[0].km, fixes=len(path))

        ws = await websockets.connect(base.replace("http", "ws", 1) + "/ws")

        live = asyncio.Event()                        # the server first replays old events: skip those

        async def listen():
            async for raw in ws:
                e = json.loads(raw)
                if live.is_set() and e.get("kind") in KEEP:
                    note(e["kind"], **{k: v for k, v in e.items() if k not in ("kind", "ts")})
                    if e["kind"] == "safety_checkin" and a.answer:
                        await asyncio.sleep(3)
                        r = await http.post(f"/api/safety/{e['id']}", json={"answer": a.answer})
                        note("rider_answered", answer=a.answer, status=r.status_code)

        listener = asyncio.create_task(listen())
        await asyncio.sleep(1)
        live.set()
        r = await http.post("/api/gps-guard", json={"enabled": True, "rehearsal": True, "stall_min": a.stall_min,
                                                    "note": "safety proof run: Indiranagar -> Koramangala"})
        if r.status_code != 200:
            note("error", detail=r.json().get("detail"))
            return 1

        moving = path[: int(a.drive_s / a.every)]
        note("phase", phase=f"driving: {len(moving)} fixes, every {a.every}s, ~{a.speed_kmh} km/h on real roads")
        for lat, lon in moving:
            await send_fix(http, lat, lon)
            await asyncio.sleep(a.every)

        park = moving[-1]
        if a.silent:
            note("phase", phase="phone goes silent: no more location fixes (screen off / no signal)")
            deadline = time.time() + a.stall_min * 60 + 240
            while time.time() < deadline:
                await asyncio.sleep(5)
                done = [e for e in events if e["event"] == "sos_call" and e.get("status") in
                        ("completed", "busy", "no-answer", "failed", "canceled")]
                if any(e["event"] == "sos_sent" for e in events) and ("phone call" not in channels or done):
                    break
            moving = []                                 # skip the parked phase below

        note("phase", phase=f"parked at {park[0]:.5f},{park[1]:.5f}: same spot with +/-10 m GPS jitter, nobody answers"
             if not a.answer else f"parked; the rider will answer '{a.answer}'")
        deadline = time.time() + (0 if a.silent else a.stall_min * 60 + 240)
        rng = random.Random(7)
        while time.time() < deadline:
            j = lambda: rng.uniform(-10, 10) / 111_320           # noqa: E731
            await send_fix(http, park[0] + j(), park[1] + j())
            await asyncio.sleep(5)
            calls_done = [e for e in events if e["event"] == "sos_call" and e.get("status") in
                          ("completed", "busy", "no-answer", "failed", "canceled")]
            sent = any(e["event"] == "sos_sent" for e in events)
            if a.answer and any(e["event"] == "safety_answer" or e["event"] == "rider_answered" for e in events):
                await asyncio.sleep(20)                       # make sure nothing goes out after "I'm OK"
                break
            if sent and ("phone call" not in channels or calls_done):
                break
        await http.post("/api/gps-guard", json={"enabled": False})
        await asyncio.sleep(1)
        listener.cancel()
        await ws.close()

    sent = next((e for e in events if e["event"] == "sos_sent"), None)
    asked = next((e for e in events if e["event"] == "safety_checkin"), None)
    parked_at = next(e["t"] for e in events if e["event"] == "phase" and ("parked" in e["phase"] or "silent" in e["phase"]))
    ok = (sent is None and asked is not None) if a.answer == "ok" else (asked is not None and sent is not None)
    verdict = "PASS" if ok else "FAIL"
    note("verdict", result=verdict)

    out = Path(__file__).resolve().parent.parent / "proof"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"safety_{stamp}.json").write_text(json.dumps(events, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = [f"# Safety chain proof: {verdict}", "",
             f"Run {datetime.now():%d %b %Y %H:%M}, server {base}. Route: real roads from OpenStreetMap (OSRM). "
             f"Threshold: no movement beyond 60 m for {a.stall_min} min; answer window from the server's settings.", ""]
    if asked:
        lines.append(f"- Parked at +{parked_at}s; \"Are you safe?\" asked at +{asked['t']}s "
                     f"({asked['t'] - parked_at:.0f}s after the car stopped)")
    if sent:
        lines.append(f"- Alert sent at +{sent['t']}s: " + "; ".join(f"{k}: {v}" for k, v in sent["results"].items()))
        if sent.get("location"):
            lines.append(f"- Location shared: {sent['location']}")
    for e in events:
        if e["event"] == "sos_call":
            lines.append(f"- Emergency call {e.get('number')}: {e.get('status')}"
                         + (f" ({e.get('duration')}s)" if e.get("duration") else "") + f" at +{e['t']}s")
    if a.answer:
        lines.append(f"- Rider answered '{a.answer}': " + ("no alert went out" if not sent else "alert went out"))
    lines += ["", "## Full timeline", "", "| time | +s | event | details |", "|---|---|---|---|"]
    for e in events:
        details = {k: v for k, v in e.items() if k not in ("t", "at", "event")}
        lines.append(f"| {e['at']} | {e['t']} | {e['event']} | {json.dumps(details, ensure_ascii=False)[:200].replace('|', '/')} |")
    (out / f"safety_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{verdict}. Report: proof/safety_{stamp}.md")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="http://localhost:8765")
    p.add_argument("--stall-min", type=float, default=1)
    p.add_argument("--answer", choices=["ok", "help"], default=None)
    p.add_argument("--silent", action="store_true", help="the phone stops sending its location instead of parking")
    p.add_argument("--drive-s", type=float, default=60)
    p.add_argument("--every", type=float, default=3, help="seconds between GPS fixes")
    p.add_argument("--speed-kmh", type=float, default=25)
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(main(args)))
    except Exception:
        httpx.post(args.server.rstrip("/") + "/api/gps-guard", json={"enabled": False}, timeout=10)   # never leave it on
        raise
