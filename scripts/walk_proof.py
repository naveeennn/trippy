"""Record a real-world safety test (the rider's own phone GPS) as proof: listens to a running TripOwner and
logs every safety event until the alert has gone out and the emergency call has finished, then writes
proof/walk_<time>.md and .json. Location fixes are counted, never stored (no personal coordinates in proof).

  python scripts/walk_proof.py            # waits up to 40 minutes
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import websockets

KEEP = {"gps_guard", "safety_checkin", "safety_answer", "sos_sent", "sos_call", "sos_ack", "gps_stale"}
FINAL_CALL = {"completed", "busy", "no-answer", "failed", "canceled"}


async def main(server: str = "http://localhost:8765", max_s: int = 2400) -> int:
    events, t0 = [], time.time()

    def note(kind, **kw):
        kw.pop("location", None)                      # a map link of where the rider was: keep it out of git
        events.append({"t": round(time.time() - t0, 1), "at": datetime.now().strftime("%H:%M:%S"), "event": kind, **kw})
        print(f"[{events[-1]['at']}] {kind}: {json.dumps(kw, ensure_ascii=False)[:180]}", flush=True)

    ws = await websockets.connect(server.replace("http", "ws", 1) + "/ws")
    live = asyncio.Event()
    deadline = time.time() + max_s

    async def listen():
        async for raw in ws:
            e = json.loads(raw)
            if live.is_set() and e.get("kind") in KEEP:
                note(e["kind"], **{k: v for k, v in e.items() if k not in ("kind", "ts")})

    task = asyncio.create_task(listen())
    await asyncio.sleep(1)
    live.set()
    print("Recording. Waiting for the rider's phone...", flush=True)
    while time.time() < deadline:
        await asyncio.sleep(3)
        sent = any(e["event"] == "sos_sent" for e in events)
        calls = [e for e in events if e["event"] == "sos_call"]
        if sent and (not calls and time.time() - next(e["t"] for e in events if e["event"] == "sos_sent") - t0 > 60
                     or any(c.get("status") in FINAL_CALL for c in calls)):
            break
        if any(e["event"] == "safety_answer" and e.get("answer") == "ok" for e in events):
            await asyncio.sleep(10)
            break
    task.cancel()
    await ws.close()

    out = Path(__file__).resolve().parent.parent / "proof"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"walk_{stamp}.json").write_text(json.dumps(events, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = [f"# Real-world safety test with the rider's own phone ({datetime.now():%d %b %Y %H:%M})", "",
             "The rider opened TripOwner on their phone, tapped \"I'm in a cab\", walked, then stood still and "
             "did not answer \"Are you OK?\". Location came from the phone's own GPS (coordinates are not stored here).",
             "", "| time | event | details |", "|---|---|---|"]
    for e in events:
        d = {k: v for k, v in e.items() if k not in ("t", "at", "event")}
        lines.append(f"| {e['at']} | {e['event']} | {json.dumps(d, ensure_ascii=False)[:220].replace('|', '/')} |")
    (out / f"walk_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport: proof/walk_{stamp}.md", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
