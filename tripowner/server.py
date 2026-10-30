"""FastAPI app: runs live demo trips and streams every event to the dashboard.

    uvicorn tripowner.server:app --reload --port 8000
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .config import settings
from .driver import make_driver
from .events import EventBus
from .llm import get_llm
from .providers.approval import AutoApprove, DashboardApproval
from .providers.email import email_enabled, send_email
from .providers.beckn import beckn_enabled
from .providers.handoff import handoff_enabled
from .providers.uber import uber_enabled
from .providers.voice import make_voice
from .providers.calendar import IST, load_ics, next_trip_event
from .providers.maps import PLACES
from .sim.chaos import cabs_fail_scenario, calendar_scenario, demo_scenario, night_stop_scenario, random_scenario
from .safety import Alert, DashboardSafetyPrompt, DemoChannel, EmergencyAlerter, GpsGuard, LocationStore
from .llm import spend
from .providers.routes import DashboardRouteChoice
from .sim.eval import run_eval
from .sim.world import World
from .watcher import ActiveTrip, CalendarWatcher, ist_now

logging.basicConfig(level=logging.INFO)
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"

log = logging.getLogger(__name__)
app = FastAPI(title="TripOwner")
bus = EventBus()
approvals = DashboardApproval(bus)
voice = make_voice()
_run_task: asyncio.Task | None = None
safety_prompt = DashboardSafetyPrompt(bus)
alerter = EmergencyAlerter(bus, [DemoChannel()]) if settings.public_demo else EmergencyAlerter(bus)
REAL_EMAIL = not settings.public_demo
CALENDAR_ON = bool(settings.calendar_ics) and not settings.public_demo
locations = LocationStore()
route_chooser = DashboardRouteChoice(bus)
gps_guard = GpsGuard(bus, safety_prompt, alerter, settings.user_name, settings.gps_stall_min,
                     settings.gps_stall_radius_m, settings.safety_checkin_timeout_s,
                     silent_min=settings.gps_silent_min)
_world: World | None = None          # the trip currently running, for SOS details


class RunRequest(BaseModel):
    agent: str = "agent"            # agent | llm | baseline
    scenario: str = "demo"          # demo | cabs_fail | random | calendar
    seed: int = 1
    sec_per_min: float = 1.0        # playback speed: real seconds per simulated minute
    manual_approval: bool = True
    driver_lang: str = "kn-IN"      # stage demo: the language of the driver who calls (kn-IN | hi-IN | en-IN)
    rides: str = "app"              # app = simulated ride app | beckn = mock Namma Yatri over the Beckn protocol


class ApprovalBody(BaseModel):
    approved: bool


class WatchBody(BaseModel):
    enabled: bool


class LocationBody(BaseModel):
    lat: float
    lon: float


class SafetyBody(BaseModel):
    answer: str                      # ok | help


class RouteBody(BaseModel):
    name: str


class GuardBody(BaseModel):
    enabled: bool
    note: str = ""                   # e.g. "Uber from Indiranagar to Whitefield, KA-01-AB-1234"
    rehearsal: bool = False          # test the whole chain on purpose: real alerts, labelled as a test
    stall_min: float | None = None   # override GPS_STALL_MIN (min 1) for a rehearsal


async def _start_watch_trip(event, place: str) -> ActiveTrip | None:
    """Called by the watcher when it's time to own a real trip. Runs on the real clock."""
    global _run_task
    if _run_task and not _run_task.done():
        return None                      # a demo is playing; the watcher retries on its next poll
    bus.history.clear()
    sc = calendar_scenario(event, place, settings.user_location, start_at=ist_now())
    global _world
    world = World(sc, "llm" if settings.llm_enabled else "agent", bus=bus, voice=voice,
                  driver=make_driver(), llm=get_llm(), approval=approvals, real_email=REAL_EMAIL,
                  safety_prompt=safety_prompt, alerter=alerter, locations=locations, drill=False,
                  route_chooser=route_chooser, real_routes=True, real_rides=True, updates=_telegram_update)
    _world = world
    _run_task = asyncio.create_task(world.run(sec_per_min=60))   # one simulated minute = one real minute
    return ActiveTrip(event.uid, event.title, event.start, world, _run_task)


async def _telegram_update(text: str) -> None:
    """FYI messages to you (first Telegram chat) during real trips. Nothing in the public demo."""
    if settings.public_demo or not (settings.telegram_bot_token and settings.telegram_chat_ids):
        return
    from .providers.telegram import TelegramClient
    await TelegramClient().send_message(settings.telegram_chat_ids[0], "TripOwner: " + text)


async def _load_calendar():
    return await load_ics(settings.calendar_ics)


watcher = CalendarWatcher(load=_load_calendar, start_trip=_start_watch_trip, origin=settings.user_location,
                          poll_s=settings.watch_poll_sec, lead_min=settings.watch_lead_min,
                          publish=bus.publish)


@app.on_event("startup")
async def _maybe_watch():
    if settings.watch_on_start and CALENDAR_ON:
        watcher.start()


@app.get("/")
async def index():
    return FileResponse(DASHBOARD, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health():
    return {
        "llm": settings.llm_model if settings.llm_enabled else None,
        "voice": voice.name,
        "tts": settings.voice_tts and settings.sarvam_enabled,
        "driver": settings.driver_mode,
        "running": bool(_run_task and not _run_task.done()),
        "calendar": CALENDAR_ON,
        "public_demo": settings.public_demo,
        "alert_channels": alerter.ready_channels(),
        "phone": __import__("tripowner.phone", fromlist=["x"]).phone_enabled(),
        "llm_spend": spend.snapshot(),
        "watch": watcher.snapshot(),
        "email": None if settings.public_demo else
                 (("attendees" if settings.email_to_attendees else "you only") if email_enabled() else None),
        "safety": alerter.ready_channels(),
        "gps_guard": gps_guard.snapshot(),
        "rides": ("uber-sandbox" if settings.uber_sandbox_run_id else "uber-PRODUCTION") if uber_enabled()
                 else "telegram one-tap" if handoff_enabled() else "namma yatri (beckn)" if beckn_enabled()
                 else "simulated",
        "auto_book": settings.auto_book,
    }


@app.post("/api/location")
async def share_location(body: LocationBody):
    """The dashboard shares the rider's real location (browser geolocation, only with their permission)."""
    locations.update(body.lat, body.lon)
    gps_guard.on_location(body.lat, body.lon)
    return {"ok": True, "guard": gps_guard.active}


@app.post("/api/gps-guard")
async def gps_guard_toggle(body: GuardBody):
    """Real cab rides: watch the rider's own GPS and check on them if it stops moving."""
    if body.enabled:
        if not alerter.ready_channels():
            raise HTTPException(400, "Set up an emergency channel first (EMERGENCY_EMAILS and/or Telegram)")
        gps_guard.start(body.note, rehearsal=body.rehearsal, stall_min=body.stall_min if body.rehearsal else None)
    else:
        gps_guard.stop()
    return gps_guard.snapshot()


@app.post("/api/safety/{cid}")
async def safety_answer(cid: str, body: SafetyBody):
    if not safety_prompt.resolve(cid, body.answer):
        raise HTTPException(404, "no pending safety check with that id")
    return {"ok": True}


_live = None        # the current live call (one at a time)


class LiveStart(BaseModel):
    lang: str = "hi-IN"
    persona: str = "you"            # you | lost | impatient | cash | chatty


@app.post("/api/livecall/start")
async def livecall_start(body: LiveStart):
    """Start a live call: you (or an improvising AI) play the driver; TripOwner answers."""
    from .live_call import LiveCall
    global _live
    lang = body.lang if body.lang in ("hi-IN", "kn-IN", "en-IN") else "hi-IN"
    _live = LiveCall(voice, get_llm(), lang, body.persona)
    return {"greeting": await _live.greeting(), "llm": settings.llm_model if get_llm() else None,
            "speech_input": voice.name == "sarvam", "rider": _live.profile.name,
            "pickup": _live.profile.pickup_landmark, "ride": _live.ride}


def _report_driver_delay(minutes: int | None) -> None:
    """A driver said on a call they'll take too long: the trip being owned cancels and books another cab."""
    if not minutes:
        return
    world = watcher.active.world if watcher.active else (_world if _run_task and not _run_task.done() else None)
    if world and world.ctx.ride and world.ctx.ride.active:
        world.report_driver_delay(minutes)
        bus.publish("phone", state="driver too late", why=f"driver needs {minutes} min: cancelling and rebooking")
    else:
        bus.publish("phone", state="driver too late", why=f"driver needs {minutes} min; no ride running to rebook")


@app.post("/api/livecall/say")
async def livecall_say(text: str = Form(""), audio: UploadFile | None = File(None)):
    """The driver speaks (audio) or types (text); TripOwner understands it and answers."""
    if _live is None or _live.ended:
        raise HTTPException(400, "Start a live call first")
    data = await audio.read() if audio is not None else None
    if not data and not text.strip():
        raise HTTPException(400, "Say or type something")
    if data and voice.name != "sarvam":
        raise HTTPException(400, "Speech input needs SARVAM_API_KEY; type what the driver says instead")
    try:
        heard = await _live.hear(audio=data or None, text=text or None)
    except Exception as e:
        raise HTTPException(502, f"Couldn't understand the audio: {type(e).__name__}") from e
    if not heard["english"].strip():
        raise HTTPException(422, "Didn't catch that. Try again, a little louder or longer")
    reply = await _live.respond(heard["english"], heard.get("native"))
    _report_driver_delay(_live.too_late)
    return {"heard": heard, "reply": reply, "summary": await _live.summary_llm() if _live.ended else None}


@app.post("/api/livecall/ai-turn")
async def livecall_ai_turn():
    """Nemotron improvises the driver's next line; TripOwner answers it."""
    if _live is None or _live.ended:
        raise HTTPException(400, "Start a live call first")
    if not get_llm():
        raise HTTPException(400, "The AI driver needs Nemotron (LLM_API_KEY)")
    line = await _live.ai_driver_line()
    if line is None:
        _live.ended = True
        return {"done": True, "summary": await _live.summary_llm()}
    from .live_call import clean_spoken
    english = clean_spoken(await voice.to_english(line, _live.lang)) if _live.lang != "en-IN" else line
    driver_audio = await voice.synthesize(line, _live.lang, force=True, role="driver")
    reply = await _live.respond(english, line)
    return {"done": False, "driver": {"text": line, "text_en": english, "audio": driver_audio, "lang": _live.lang},
            "reply": reply, "summary": await _live.summary_llm() if _live.ended else None}


@app.post("/api/livecall/end")
async def livecall_end():
    global _live
    s = await _live.summary_llm() if _live else None
    if _live:
        _live.ended = True
    return {"summary": s}


@app.post("/api/call/takeover")
async def call_takeover():
    """The rider takes the driver's call over; the assistant stops talking and only suggests."""
    if not (_world and _run_task and not _run_task.done() and _world.tb.take_over_call()):
        raise HTTPException(404, "no call in progress")
    return {"ok": True}


@app.post("/api/route/{rid}")
async def pick_route(rid: str, body: RouteBody):
    if not route_chooser.resolve(rid, body.name):
        raise HTTPException(404, "no pending route choice with that id, or unknown route")
    return {"ok": True}


@app.post("/api/sos")
async def sos():
    """The rider pressed SOS. Always a real alert, with the current trip's details if one is running."""
    w = _world if (_run_task and not _run_task.done()) else None
    trip, loc = {}, locations.best()
    if w is not None:
        trip = w.tb.trip_info()
        loc = locations.best(w.ctx.profile.location, w.ctx.meeting.location, w.tb.trip_progress_fraction())
    name = w.ctx.profile.name if w is not None else settings.user_name
    if not alerter.ready_channels():
        raise HTTPException(400, "No emergency channel is set up: add EMERGENCY_EMAILS (and Gmail) to .env")
    results = await alerter.alert(Alert(name, "pressed the SOS button in TripOwner", False, loc, trip))
    return {"results": results}


@app.get("/api/telegram/chats")
async def telegram_chats():
    """Who has messaged your bot (press Start), with their chat IDs for TELEGRAM_CHAT_IDS."""
    _no_public()
    if not settings.telegram_bot_token:
        raise HTTPException(400, "Set TELEGRAM_BOT_TOKEN in .env (from @BotFather)")
    from .providers.telegram import TelegramClient
    c = TelegramClient()
    try:
        me = await c.me()
        chats = await c.chats()
    except Exception as e:
        raise HTTPException(502, f"Telegram: {e}") from e
    listed = set(settings.telegram_chat_ids)
    return {"bot": "@" + me.get("username", ""), "chats": [dict(ch, listed=ch["chat_id"] in listed) for ch in chats]}


@app.post("/api/telegram/test")
async def telegram_test():
    _no_public()
    if not (settings.telegram_bot_token and settings.telegram_chat_ids):
        raise HTTPException(400, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS in .env")
    from .providers.telegram import TelegramClient
    c, sent = TelegramClient(), []
    for chat in settings.telegram_chat_ids:
        try:
            await c.send_message(chat, "✅ TripOwner test: you'll get safety alerts here.")
            sent.append(chat)
        except Exception as e:
            raise HTTPException(502, f"Telegram refused chat {chat}: {e}") from e
    return {"sent_to": len(sent)}


@app.post("/api/email/test")
async def email_test():
    """Send yourself one email to check the Gmail App Password works."""
    _no_public()
    if not email_enabled():
        raise HTTPException(400, "Set SMTP_USER and SMTP_APP_PASSWORD in .env (see README)")
    try:
        await send_email([settings.smtp_user], "TripOwner test email",
                         "If you can read this, TripOwner can send real emails from your account.")
    except Exception as e:
        raise HTTPException(502, f"Gmail refused it: {type(e).__name__}: {str(e)[:160]}") from e
    return {"sent_to": settings.smtp_user}


@app.get("/api/watch")
async def watch_status():
    return watcher.snapshot()


@app.post("/api/watch")
async def watch_toggle(body: WatchBody):
    _no_public()
    if body.enabled:
        if not settings.calendar_ics:
            raise HTTPException(400, "Set CALENDAR_ICS in .env to watch your calendar")
        watcher.start()
    else:
        watcher.stop()
    return watcher.snapshot()


@app.post("/api/watch/check")
async def watch_check(force: bool = False):
    """Check the calendar now. force=true starts owning the next trip immediately (for demos)."""
    _no_public()
    if not settings.calendar_ics:
        raise HTTPException(400, "Set CALENDAR_ICS in .env to watch your calendar")
    await watcher.poll(force=force)
    return watcher.snapshot()


def _no_public():
    if settings.public_demo:
        raise HTTPException(403, "Not available in the public demo")


async def _next_calendar_trip():
    _no_public()
    if not settings.calendar_ics:
        raise HTTPException(400, "Set CALENDAR_ICS in .env to your calendar's iCal address (see README)")
    if settings.user_location not in PLACES:
        raise HTTPException(400, f"USER_LOCATION must be one of: {', '.join(sorted(PLACES))}")
    try:
        events = await load_ics(settings.calendar_ics)
    except Exception as e:
        raise HTTPException(502, f"Couldn't read the calendar: {type(e).__name__}") from e
    now = datetime.now(IST).replace(tzinfo=None)
    return (*next_trip_event(events, now, PLACES, settings.user_location), len(events))


@app.get("/api/calendar")
async def calendar_preview():
    """What the agent sees in your real calendar. Titles and places only, never the iCal address."""
    event, place, skipped, total = await _next_calendar_trip()
    return {
        "events_in_calendar": total,
        "from": settings.user_location,
        "next_trip": None if event is None else
            {"title": event.title, "start": f"{event.start:%a %d %b, %H:%M}", "to": place,
             "location": event.location, "attendees": len(event.attendees)},
        "skipped": skipped[:10],
    }


@app.post("/api/run")
async def run(req: RunRequest):
    global _run_task
    if watcher.active and watcher.active.task and not watcher.active.task.done():
        raise HTTPException(409, f"TripOwner is owning your real trip to '{watcher.active.title}' right now")
    if _run_task and not _run_task.done():
        _run_task.cancel()
    if req.agent == "llm" and not settings.llm_enabled:
        raise HTTPException(400, "Set LLM_BASE_URL in .env to use the Nemotron agent")
    bus.history.clear()
    if req.scenario == "calendar":
        event, place, skipped, _ = await _next_calendar_trip()
        if event is None:
            raise HTTPException(404, "No upcoming meeting at a place the planner knows. "
                                     + (" / ".join(skipped[:3]) if skipped else "Your calendar has no upcoming events."))
        sc = calendar_scenario(event, place, settings.user_location)
    else:
        lang = req.driver_lang if req.driver_lang in ("kn-IN", "hi-IN", "en-IN") else "kn-IN"
        sc = {"demo": lambda: demo_scenario(lang), "cabs_fail": cabs_fail_scenario, "night": night_stop_scenario}.get(
            req.scenario, lambda: random_scenario(req.seed))()
    world = World(
        sc, req.agent, bus=bus, voice=voice, driver=make_driver(), llm=get_llm(),
        approval=approvals if req.manual_approval else AutoApprove(),
        real_email=REAL_EMAIL, safety_prompt=safety_prompt, alerter=alerter, locations=locations,
        route_chooser=route_chooser, real_routes=True,
        drill=req.scenario != "calendar",    # alerts from simulated demo trips are labelled as drills
        ride_backend="beckn" if req.rides == "beckn" else "app",
    )
    global _world
    _world = world
    _run_task = asyncio.create_task(world.run(sec_per_min=max(0.0, req.sec_per_min)))
    return {"started": True, "scenario": sc.summary()}


@app.post("/twilio/alert")
async def twilio_alert(request: Request, t: str = ""):
    """The words of an emergency call: Twilio fetches them when the contact picks up (one-time token)."""
    from fastapi.responses import Response
    from .phone import live_call_on_account, phone_enabled, valid_twilio_request
    from .safety import take_alert_script
    if not phone_enabled():
        raise HTTPException(404)
    form = dict((await request.form()).items())
    url = f"{settings.public_base_url}/twilio/alert?t={t}" if settings.public_base_url else str(request.url)
    signature = request.headers.get("x-twilio-signature")
    if signature is not None:
        ok = valid_twilio_request(url, form, signature)
    else:                                 # trial number: Twilio's proxy drops the signature, so ask Twilio
        try:
            ok = bool(settings.twilio_call_from) and await live_call_on_account(form.get("CallSid", ""))
        except Exception as e:
            log.warning("couldn't verify the emergency call with Twilio: %s", e)
            ok = False
    if not ok:
        raise HTTPException(403, "not from Twilio")
    script = take_alert_script(t)
    if script is None:
        raise HTTPException(404, "unknown or expired alert")
    return Response('<?xml version="1.0" encoding="UTF-8"?>' + script, media_type="application/xml")


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """Twilio calls this when your number rings: answer by streaming the call audio to /twilio/stream."""
    from fastapi.responses import Response
    from .phone import live_call_on_account, phone_enabled, stream_twiml, valid_twilio_request
    if not phone_enabled():
        raise HTTPException(404)
    form = dict((await request.form()).items())
    url = (settings.public_base_url + "/twilio/voice") if settings.public_base_url else str(request.url)
    signature = request.headers.get("x-twilio-signature")
    if signature is not None:
        ok = valid_twilio_request(url, form, signature)
    else:                                 # trial number: Twilio's proxy drops the signature, so ask Twilio
        try:
            ok = bool(settings.twilio_call_from) and await live_call_on_account(form.get("CallSid", ""))
        except Exception as e:            # can't reach Twilio to check: never run an unchecked call, but end it politely
            log.warning("couldn't verify the call with Twilio: %s", e)
            bus.publish("phone", state="error", error="couldn't verify the call with Twilio (network problem)")
            from .phone import gather_twiml
            return Response(gather_twiml("Sorry, there's a problem on the line. Please call back in a minute.",
                                         url, hang_up=True), media_type="application/xml")
    if not ok:
        raise HTTPException(403, "not from Twilio")
    base = url.rsplit("/twilio/voice", 1)[0]
    bus.publish("phone", state="ringing", caller=form.get("From", "")[-4:])
    if settings.phone_mode == "gather":
        from .phone import MAX_CALL_S, GatherCall, gather_twiml
        call = GatherCall(voice, get_llm(), publish=bus.publish, notify=_telegram_update, ride=_current_ride())
        _gather_calls[form.get("CallSid", "")] = call
        # The trial gives no hang-up signal: wrap up (summary to Telegram) once the call is surely over
        asyncio.get_running_loop().call_later(MAX_CALL_S + 60, lambda: asyncio.ensure_future(_end_gather(form.get("CallSid", ""))))
        return Response(gather_twiml(await call.start(), base + "/twilio/turn"), media_type="application/xml")
    ws = base.replace("https://", "wss://").replace("http://", "ws://")
    return Response(stream_twiml(ws + "/twilio/stream"), media_type="application/xml")


_gather_calls: dict = {}                 # CallSid -> GatherCall (trial mode: one HTTP request per turn)


async def _end_gather(sid: str) -> None:
    call = _gather_calls.pop(sid, None)
    if call:
        await call.finish()


async def _twilio_form(request: Request, path: str) -> tuple[dict, str, object]:
    """Trial-mode request: (form, its URL, the GatherCall). Signed requests are verified; unsigned ones (Twilio's
    trial proxy drops the signature) only get an answer for a call that already passed the check in /twilio/voice."""
    from .phone import phone_enabled, valid_twilio_request
    if not phone_enabled():
        raise HTTPException(404)
    form = dict((await request.form()).items())
    url = (settings.public_base_url + path) if settings.public_base_url else str(request.url)
    sid = form.get("CallSid", "")
    signature = request.headers.get("x-twilio-signature")
    if not (valid_twilio_request(url, form, signature) if signature is not None else sid in _gather_calls):
        raise HTTPException(403, "not from Twilio")
    return form, url, _gather_calls.get(sid)


def _xml(twiml: str):
    from fastapi.responses import Response
    return Response(twiml, media_type="application/xml")


# Twilio's trial proxy drops a request it waits on for more than a second or two, and Nemotron takes 2-4 s.
# So /twilio/turn answers at once ("One moment.") and starts the reply in the background; /twilio/reply is
# polled (each poll answers within REPLY_POLL_S, with a short pause in between) until the reply is ready.
REPLY_POLL_S = 0.8


@app.post("/twilio/turn")
async def twilio_turn(request: Request):
    """Trial mode: Twilio posts what the caller said (SpeechResult)."""
    from .phone import gather_twiml
    form, url, call = await _twilio_form(request, "/twilio/turn")
    if call is None:
        return _xml(gather_twiml("Sorry, this call has ended. Goodbye.", url, hang_up=True))
    call.pending = asyncio.ensure_future(call.turn(form.get("SpeechResult", "")))
    reply_url = url.rsplit("/twilio/turn", 1)[0] + "/twilio/reply"
    return _xml(await _reply_or_wait(form["CallSid"], call, reply_url, filler=True))


@app.post("/twilio/reply")
async def twilio_reply(request: Request):
    """Trial mode: Twilio checks back for the reply that /twilio/turn started."""
    from .phone import gather_twiml
    form, url, call = await _twilio_form(request, "/twilio/reply")
    if call is None or call.pending is None:
        return _xml(gather_twiml("Sorry, this call has ended. Goodbye.", url, hang_up=True))
    return _xml(await _reply_or_wait(form["CallSid"], call, url))


async def _reply_or_wait(sid: str, call, reply_url: str, filler: bool = False) -> str:
    """The reply as TwiML if it's ready within REPLY_POLL_S; otherwise "one moment" / a pause, then poll again."""
    from xml.sax.saxutils import escape
    from .phone import _say, gather_twiml
    done_now, _ = await asyncio.wait({call.pending}, timeout=REPLY_POLL_S)
    if not done_now:
        wait = _say("One moment.") if filler else '<Pause length="1"/>'
        return (f'<?xml version="1.0" encoding="UTF-8"?><Response>{wait}'
                f'<Redirect method="POST">{escape(reply_url)}</Redirect></Response>')
    try:
        text, done = call.pending.result()
    except Exception as e:
        log.warning("phone turn failed: %s", e)
        bus.publish("phone", state="error", error=str(e)[:200])
        text, done = "Sorry, something went wrong on my side. Please message in the app.", True
    call.pending = None
    if done:
        bus.publish("phone", state="ending", why="done")
        _report_driver_delay(call.call.too_late)
        asyncio.ensure_future(_end_gather(sid))
    turn_url = reply_url.rsplit("/twilio/reply", 1)[0] + "/twilio/turn"
    return gather_twiml(text, turn_url, hang_up=done)


@app.post("/api/phone/call-me")
async def phone_call_me():
    """Test the phone agent: Twilio rings PHONE_TEST_TO and TripOwner answers you as if you were the driver."""
    from .phone import call_me
    try:
        sid = await call_me()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    to = settings.phone_test_to
    bus.publish("phone", state="calling you", caller=to[-4:])
    return {"ok": True, "sid": sid, "to": to[:3] + "…" + to[-3:]}


@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    from .phone import TwilioCallSession, phone_enabled, take_token
    await ws.accept()
    if not phone_enabled():
        await ws.close()
        return
    session = None

    async def send(msg):
        if msg is None:
            await ws.close()
        else:
            await ws.send_text(json.dumps(msg))

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("event") == "start":
                if not take_token((msg["start"].get("customParameters") or {}).get("token")):
                    await ws.close()
                    return
                session = TwilioCallSession(send, voice, get_llm(), publish=bus.publish,
                                            notify=_telegram_update, ride=_current_ride())
            if session:
                await session.on_message(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if session:
            _report_driver_delay(session.call.too_late)
            await session.finish()


def _current_ride() -> dict | None:
    """The real ride TripOwner is owning right now, so the phone agent knows the driver and car."""
    world = watcher.active.world if watcher.active else None
    r = getattr(getattr(world, "ctx", None), "ride", None)
    if not r or not r.driver:
        return None
    return {"driver": r.driver.name, "vehicle": r.driver.vehicle, "plate": r.driver.plate, "eta": r.pickup_eta_min or "a few"}


@app.post("/beckn/{action}")
async def beckn_callback(action: str, request: Request):
    """Callbacks from the ONDC network (on_search, on_confirm, on_update...) for real Namma Yatri trips."""
    from .providers.beckn import BecknRides
    body = await request.body()
    rides = getattr(watcher.active.world, "rides", None) if watcher.active else None
    if not isinstance(rides, BecknRides):
        return JSONResponse({"message": {"ack": {"status": "NACK"}}, "error": {"code": "30000", "message": "no active trip"}})
    # Production needs the sender's key from the ONDC registry to verify `Authorization`; not wired up yet.
    payload = json.loads(body)
    if payload.get("context", {}).get("action") != action:
        return JSONResponse({"message": {"ack": {"status": "NACK"}}})
    return JSONResponse(await rides.deliver(payload))


@app.post("/api/stop")
async def stop():
    if _run_task and not _run_task.done():
        _run_task.cancel()
        bus.publish("stopped")
    return {"stopped": True}


@app.post("/api/approval/{req_id}")
async def approve(req_id: str, body: ApprovalBody):
    if not approvals.resolve(req_id, body.approved):
        raise HTTPException(404, "no pending approval with that id")
    return {"ok": True}


@app.get("/api/eval")
async def evaluate(trips: int = 100, seed: int = 42):
    report = await run_eval(min(trips, 500), seed)
    return {k: report[k] for k in ("trips", "seed", "summary", "baseline_assumptions")}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    q = bus.subscribe()
    try:
        for record in list(bus.history):            # catch up a late-joining browser
            await socket.send_text(json.dumps(record, default=str))
        while True:
            record = await q.get()
            await socket.send_text(json.dumps(record, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)
