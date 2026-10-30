"""Record a real phone call with TripOwner as proof: rings PHONE_TEST_TO via /api/phone/call-me, then logs
every event (who said what, in which language, with timestamps) until the call ends, and writes
proof/call_<time>.md and .json.

  python scripts/call_proof.py
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

KEEP = {"phone", "chat", "call_start", "call_end", "call_takeover"}


async def main(server: str = "http://localhost:8765", max_s: int = 300) -> int:
    events, t0 = [], time.time()

    def note(kind, **kw):
        events.append({"t": round(time.time() - t0, 1), "at": datetime.now().strftime("%H:%M:%S"), "event": kind, **kw})
        print(f"[{events[-1]['at']} +{events[-1]['t']:>6}s] {kind}: {json.dumps(kw, ensure_ascii=False)[:200]}", flush=True)

    ws = await websockets.connect(server.replace("http", "ws", 1) + "/ws")
    live, ended = asyncio.Event(), asyncio.Event()

    async def listen():
        async for raw in ws:
            e = json.loads(raw)
            if not live.is_set() or e.get("kind") not in KEEP:
                continue
            data = {k: v for k, v in e.items() if k not in ("kind", "ts", "audio", "caller")}   # no phone digits in proof
            note(e["kind"], **data)
            if e["kind"] == "phone" and e.get("state") == "ended":
                ended.set()

    task = asyncio.create_task(listen())
    await asyncio.sleep(1)
    live.set()
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(server + "/api/phone/call-me")
        note("call_placed", status=r.status_code, **({k: v for k, v in r.json().items() if k != "sid"}))
        if r.status_code != 200:
            return 1
    try:
        await asyncio.wait_for(ended.wait(), max_s)
    except asyncio.TimeoutError:
        note("timeout", detail=f"no end of call within {max_s}s")
    await asyncio.sleep(2)
    task.cancel()
    await ws.close()

    turns = [e for e in events if e["event"] == "chat"]
    out = Path(__file__).resolve().parent.parent / "proof"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"call_{stamp}.json").write_text(json.dumps(events, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = [f"# Real phone call with TripOwner ({datetime.now():%d %b %Y %H:%M})", "",
             "Twilio rang a real phone; the person answering played the cab driver. TripOwner heard them with "
             "Sarvam speech-to-text, decided each reply with Nemotron under its guardrails, and spoke it back with "
             "Sarvam text-to-speech, live on the call.", "", "## Conversation", ""]
    last_driver = None
    for e in turns:
        who = "Driver (caller)" if e.get("sender") == "driver" else "TripOwner"
        gap = ""
        if e.get("sender") == "agent" and last_driver is not None:
            gap = f" _(replied {e['t'] - last_driver:.1f}s after the caller finished)_"
        if e.get("sender") == "driver":
            last_driver = e["t"]
        native, english = e.get("text", ""), e.get("text_en", "")
        lines.append(f"- **{who}** [{e.get('lang', '')}] +{e['t']}s{gap}: {native}")
        if english and english != native:
            lines.append(f"  - _English: {english}_")
    summary = next((e.get("summary") for e in events if e["event"] == "phone" and e.get("state") == "ended"), None)
    if summary:
        lines += ["", f"**Summary sent to the rider on Telegram:** {summary}"]
    lines += ["", "## Full event log", "", "| time | +s | event | details |", "|---|---|---|---|"]
    for e in events:
        d = {k: v for k, v in e.items() if k not in ("t", "at", "event")}
        lines.append(f"| {e['at']} | {e['t']} | {e['event']} | {json.dumps(d, ensure_ascii=False)[:220].replace('|', '/')} |")
    (out / f"call_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport: proof/call_{stamp}.md ({len(turns)} lines spoken)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*(sys.argv[1:2] or []))))
