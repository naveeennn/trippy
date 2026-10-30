import asyncio
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx

from tripowner.events import EventBus
from tripowner.safety import Alert, CallChannel, EmergencyAlerter, GpsGuard, Location, SafetyPrompt
from test_uber import setting

TWILIO = dict(twilio_account_sid="AC" + "1" * 32, twilio_auth_token="tok", twilio_phone_number="+17372508034",
              emergency_phones=("+919800000001",), public_base_url="", twilio_call_from="")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 27, 23, 10)

    def __call__(self):
        return self.t


class Prompt(SafetyPrompt):
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def check_in(self, question, timeout_s):
        self.asked.append(question)
        return self.answer


class Recorder:
    name = "recorder"

    def __init__(self):
        self.alerts = []

    def ready(self):
        return True

    async def send(self, alert):
        self.alerts.append(alert)
        return "ok"


def route(n, start=(12.9784, 77.6408), step_m=15):
    """n fixes heading south at ~15 m per fix."""
    return [(start[0] - i * step_m / 111_320, start[1]) for i in range(n)]


def drive_then_stop(guard, clock, moving=30, stopped=40, every_s=5):
    """Feed fixes every 5 s: moving, then parked with GPS jitter. Returns the parked point."""
    pts = route(moving)
    for lat, lon in pts:
        guard.on_location(lat, lon)
        clock.t += timedelta(seconds=every_s)
    park = pts[-1]
    answers = []
    for i in range(stopped):
        jitter = ((i % 3) - 1) * 8 / 111_320               # +/- 8 m, like a real phone's GPS
        guard.on_location(park[0] + jitter, park[1] + jitter)
        clock.t += timedelta(seconds=every_s)
        answers.append(asyncio.run(guard.check()))
    return park, [a for a in answers if a]


def make(answer, stall_min=2):
    clock, rec, prompt = Clock(), Recorder(), Prompt(answer)
    guard = GpsGuard(EventBus(keep_history=False), prompt, EmergencyAlerter(EventBus(keep_history=False), [rec]),
                     "Jishnu", stall_min=stall_min, radius_m=60, timeout_s=60, now=clock)
    guard.start("Uber Indiranagar -> Koramangala")
    return guard, clock, rec, prompt


def test_moving_never_asks():
    guard, clock, rec, prompt = make("timeout")
    for lat, lon in route(80):
        guard.on_location(lat, lon)
        clock.t += timedelta(seconds=5)
        assert asyncio.run(guard.check()) is None
    assert not prompt.asked and not rec.alerts


def test_stopped_and_no_answer_alerts_with_the_stop_location():
    guard, clock, rec, prompt = make("timeout")
    park, answers = drive_then_stop(guard, clock)
    assert answers == ["timeout"] and len(prompt.asked) == 1            # asked once, not every 15 s
    a = rec.alerts[0]
    assert "didn't answer a safety check" in a.reason and not a.drill
    assert abs(a.location.lat - park[0]) < 0.0002 and abs(a.location.lon - park[1]) < 0.0002
    assert a.trip["from"].startswith("Uber")


def test_rider_says_ok_no_alert_and_help_alerts():
    guard, clock, rec, _ = make("ok")
    drive_then_stop(guard, clock)
    assert not rec.alerts
    guard, clock, rec, _ = make("help")
    drive_then_stop(guard, clock)
    assert "asked for help" in rec.alerts[0].reason


def test_rehearsal_is_labelled_and_uses_its_own_threshold():
    guard, clock, rec, _ = make("timeout", stall_min=5)
    guard.start("test", rehearsal=True, stall_min=1)
    _, answers = drive_then_stop(guard, clock, stopped=20)                 # 100 s parked: > 1 min
    assert answers == ["timeout"] and rec.alerts[0].drill and rec.alerts[0].rehearsal
    guard.start("real ride")                                               # back to the normal 5 min
    assert guard.stall_s == 300


class FakeTwilio:
    def __init__(self):
        self.calls, self.polls = [], 0

    def __call__(self, req: httpx.Request):
        if req.method == "POST":
            self.calls.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
            return httpx.Response(201, json={"sid": "CA123", "status": "queued"})
        self.polls += 1
        status = ["ringing", "in-progress", "completed"][min(self.polls - 1, 2)]
        return httpx.Response(200, json={"sid": "CA123", "status": status, "duration": "21" if status == "completed" else None})


def alert(drill=False, rehearsal=False):
    return Alert("Jishnu", "didn't answer a safety check after not moving for 5 min during a cab ride", drill,
                 Location(12.97, 77.64, "live (shared from your device)"), rehearsal=rehearsal)


def test_emergency_call_reads_the_alert_and_reports_progress():
    fake, events = FakeTwilio(), []
    with setting(**TWILIO):
        ch = CallChannel(transport=httpx.MockTransport(fake))
        CallChannel.FOLLOW_S = 30
        assert ch.ready()
        out = asyncio.run(ch.send(alert()))
        orig_sleep = asyncio.sleep

        async def fast(_):
            await orig_sleep(0)
        asyncio.sleep = fast
        try:
            asyncio.run(ch.watch_acks(alert(), lambda k, **kw: events.append(kw["status"])))
        finally:
            asyncio.sleep = orig_sleep
    call = fake.calls[0]
    assert call["To"] == "+919800000001" and call["From"] == "+17372508034"
    assert "urgent automatic alert" in call["Twiml"] and "Telegram" in call["Twiml"] and "1 1 2" in call["Twiml"]
    assert out.startswith("calling +91")
    assert events == ["ringing", "in-progress", "completed"]


def test_trial_accounts_fetch_the_words_from_our_server():
    """With PUBLIC_BASE_URL, the call carries only a Url (trial accounts refuse Twiml); the words are served once."""
    from fastapi.testclient import TestClient
    from tripowner.phone import twilio_signature
    from tripowner.server import app
    fake = FakeTwilio()
    with setting(**{**TWILIO, "public_base_url": "https://x.trycloudflare.com", "public_demo": False}):
        asyncio.run(CallChannel(transport=httpx.MockTransport(fake)).send(alert()))
        call = fake.calls[0]
        assert set(call) == {"To", "From", "Url"} and call["Url"].startswith("https://x.trycloudflare.com/twilio/alert?t=")
        c = TestClient(app)
        path = call["Url"].split("trycloudflare.com", 1)[1]
        form = {"CallSid": "CA123"}
        assert c.post(path, data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
        sig = twilio_signature(call["Url"], form, "tok")
        r = c.post(path, data=form, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200 and "urgent automatic alert" in r.text
        assert c.post(path, data=form, headers={"X-Twilio-Signature": sig}).status_code == 200   # retries ok


def test_drills_never_phone_anyone_but_rehearsals_do():
    fake = FakeTwilio()
    with setting(**TWILIO):
        ch = CallChannel(transport=httpx.MockTransport(fake))
        assert asyncio.run(ch.send(alert(drill=True))).startswith("skipped")
        assert not fake.calls
        asyncio.run(ch.send(alert(drill=True, rehearsal=True)))
    assert "This is a test" in fake.calls[0]["Twiml"]


def test_stopping_or_restarting_voids_an_open_check():
    """A check still waiting when watching stops (or restarts) must never alert."""
    clock, rec = Clock(), Recorder()

    class Slow(SafetyPrompt):
        def __init__(self):
            self.cancelled = False

        async def check_in(self, question, timeout_s):
            await asyncio.sleep(0.05)              # meanwhile the rider ends the ride
            return "timeout"

        def cancel_all(self):
            self.cancelled = True

    prompt = Slow()
    guard = GpsGuard(EventBus(keep_history=False), prompt, EmergencyAlerter(EventBus(keep_history=False), [rec]),
                     "Jishnu", stall_min=1, now=clock)

    async def go(restart):
        guard.start("ride")
        guard._task.cancel()                       # drive the checks by hand
        for i in range(30):
            guard.on_location(12.97, 77.64)
            clock.t += timedelta(seconds=5)
        task = asyncio.create_task(guard.check())
        await asyncio.sleep(0.01)
        guard.start("next ride") if restart else guard.stop()
        return await task

    assert asyncio.run(go(restart=False)) == "cancelled"
    assert asyncio.run(go(restart=True)) == "cancelled"
    assert not rec.alerts and prompt.cancelled


def test_phone_going_silent_warns_then_checks_in_once():
    clock, rec, prompt = Clock(), Recorder(), Prompt("timeout")
    bus = EventBus()
    guard = GpsGuard(bus, prompt, EmergencyAlerter(EventBus(keep_history=False), [rec]),
                     "Jishnu", stall_min=5, now=clock, silent_min=4)
    guard.start("Uber Indiranagar -> Koramangala")
    for lat, lon in route(10):                                    # driving, then the phone goes quiet
        guard.on_location(lat, lon)
        clock.t += timedelta(seconds=5)
    answers = []
    for _ in range(20):                                           # 5 min of silence, checked every 15 s
        clock.t += timedelta(seconds=15)
        answers.append(asyncio.run(guard.check()))
    assert [a for a in answers if a] == ["timeout"] and len(prompt.asked) == 1
    assert "haven't had your location" in prompt.asked[0]
    assert "stopped sending their location" in rec.alerts[0].reason
    assert rec.alerts[0].location.source.startswith("last known")
    stale = [e for e in bus.history if e["kind"] == "gps_stale"]
    assert len(stale) == 1 and stale[0]["minutes"] >= 2           # warned at half the limit, once


def test_hearing_from_the_phone_again_rearms_the_silence_check():
    guard, clock, rec, prompt = make("ok", stall_min=10)
    guard.silent_s = 120
    guard.on_location(12.97, 77.64)
    clock.t += timedelta(seconds=150)
    assert asyncio.run(guard.check()) == "ok"
    clock.t += timedelta(seconds=150)
    assert asyncio.run(guard.check()) is None                     # same silence: not asked again
    guard.on_location(12.98, 77.64)
    clock.t += timedelta(seconds=150)
    assert asyncio.run(guard.check()) == "ok" and len(prompt.asked) == 2 and not rec.alerts


def test_no_fix_yet_is_not_silence():
    guard, clock, rec, prompt = make("timeout")
    clock.t += timedelta(minutes=30)
    assert asyncio.run(guard.check()) is None and not prompt.asked


def test_trial_calls_come_from_the_assigned_trial_number():
    fake = FakeTwilio()
    with setting(**{**TWILIO, "twilio_call_from": "+15550001111"}):
        asyncio.run(CallChannel(transport=httpx.MockTransport(fake)).send(alert()))
    assert fake.calls[0]["From"] == "+15550001111"


def test_unsigned_trial_fetch_is_checked_with_twilio(monkeypatch):
    """Trial calls arrive without a signature: serve the words only if Twilio confirms the call is live."""
    from fastapi.testclient import TestClient
    from tripowner import phone
    from tripowner.server import app
    fake = FakeTwilio()
    live = {"ok": True}

    async def is_live(sid, transport=None):
        return live["ok"] and sid == "CA" + "1" * 32

    monkeypatch.setattr(phone, "live_call_on_account", is_live)
    with setting(**{**TWILIO, "public_base_url": "https://x.trycloudflare.com", "public_demo": False,
                    "twilio_call_from": "+15550001111"}):
        asyncio.run(CallChannel(transport=httpx.MockTransport(fake)).send(alert()))
        path = fake.calls[0]["Url"].split("trycloudflare.com", 1)[1]
        c = TestClient(app)
        assert c.post(path, data={"CallSid": "CA" + "2" * 32}).status_code == 403     # not our live call
        r = c.post(path, data={"CallSid": "CA" + "1" * 32})
        assert r.status_code == 200 and "urgent automatic alert" in r.text
