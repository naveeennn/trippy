import asyncio
import base64
import math
import warnings

import pytest

from fastapi.testclient import TestClient

from tripowner import phone
from tripowner.phone import (RATE, Listener, TwilioCallSession, pcm16_wav, twilio_signature, ulaw_decode,
                             ulaw_encode, wav_to_ulaw8k)
from test_uber import setting

KEYS = dict(twilio_account_sid="AC" + "0" * 32, twilio_auth_token="secret-token", public_demo=False,
            public_base_url="https://example.trycloudflare.com")


def tone(ms, amp=6000, hz=440):
    n = RATE * ms // 1000
    return [int(amp * math.sin(2 * math.pi * hz * i / RATE)) for i in range(n)]


def test_mulaw_matches_the_reference_codec():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        audioop = pytest.importorskip("audioop")       # the reference codec left the standard library in 3.13
    samples = list(range(-32768, 32768, 97))
    raw = b"".join(s.to_bytes(2, "little", signed=True) for s in samples)
    assert ulaw_encode(samples) == audioop.lin2ulaw(raw, 2)
    every = bytes(range(256))
    ref = audioop.ulaw2lin(every, 2)
    assert ulaw_decode(every) == [int.from_bytes(ref[i:i + 2], "little", signed=True) for i in range(0, 512, 2)]


def test_tts_audio_becomes_8k_phone_audio():
    wav = base64.b64encode(pcm16_wav(tone(1000), 22050)).decode()      # 1 s at 22.05 kHz
    out = wav_to_ulaw8k(wav)
    assert abs(len(out) - RATE * 8000 // 22050) <= 1                   # resampled to 8 kHz


def test_listener_finds_where_the_caller_stops():
    lis = Listener()
    frames = [0] * 160 * 20 + tone(800) + [0] * 160 * 40
    got = [u for i in range(0, len(frames), 160) if (u := lis.feed(frames[i:i + 160]))]
    assert len(got) == 1 and len(got[0]) >= RATE * 0.8
    lis2 = Listener()                                                   # a click isn't speech
    clicks = tone(60) + [0] * 160 * 50
    assert not [u for i in range(0, len(clicks), 160) if (u := lis2.feed(clicks[i:i + 160]))]


def test_twilio_signature_checks():
    params = {"CallSid": "CA1", "From": "+919800000000", "To": "+15550000000"}
    with setting(**KEYS):
        sig = twilio_signature(KEYS["public_base_url"] + "/twilio/voice", params, "secret-token")
        assert phone.valid_twilio_request(KEYS["public_base_url"] + "/twilio/voice", params, sig)
        assert not phone.valid_twilio_request(KEYS["public_base_url"] + "/twilio/voice", {**params, "From": "x"}, sig)
        assert not phone.valid_twilio_request(KEYS["public_base_url"] + "/twilio/voice", params, "")


class FakeVoice:
    def __init__(self, heard):
        self.heard, self.synth = list(heard), []

    async def speech_to_english(self, audio, filename="a.wav"):
        assert audio[:4] == b"RIFF"
        return self.heard.pop(0), "hi-IN"

    async def speech_to_native(self, audio, lang):
        return "native words"

    async def synthesize(self, text, lang, force=False, role="agent"):
        self.synth.append(text)
        return base64.b64encode(pcm16_wav(tone(300), 16000)).decode()

    async def from_english(self, text, lang):
        return text

    async def to_english(self, text, lang):
        return text


def media(samples):
    return {"event": "media", "media": {"payload": base64.b64encode(ulaw_encode(samples)).decode()}}


def test_a_whole_call_answered_by_the_agent():
    sent, published, notes = [], [], []
    voice = FakeVoice(["Yes I am the driver. Where exactly are you?", "Okay, I'm coming, reach in two minutes",
                       "No, that's all"])

    async def send(m):
        sent.append(m)

    async def notify(t):
        notes.append(t)

    async def go():
        s = TwilioCallSession(send, voice, None, publish=lambda k, **kw: published.append((k, kw)),
                              notify=notify, lang="hi-IN")
        await s.on_message({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}})
        assert sent[-1]["event"] == "mark"                       # greeting played
        await s.on_message(media(tone(400)))                      # caller talks over the greeting: ignored
        await s.on_message({"event": "mark", "mark": {"name": "greeting"}})
        for line in range(3):
            for i in range(0, 8000, 160):
                await s.on_message(media(tone(1000)[i:i + 160]))
            for _ in range(40):
                await s.on_message(media([0] * 160))
            for _ in range(50):                                   # let the reply be decided and played
                await asyncio.sleep(0.01)
                marks = [m for m in sent if m and m["event"] == "mark"]
                if len(marks) >= 2 + 2 * (line + 1) - (0 if line else 0):
                    break
            for m in [m for m in sent if m and m["event"] == "mark"]:
                await s.on_message({"event": "mark", "mark": m["mark"]})
            s.speaking = 0
        await asyncio.wait_for(s.finished.wait(), 5)
        return s

    s = run(go())
    said = [kw["text_en"] for k, kw in published if k == "chat" and kw["sender"] == "agent"]
    assert said[0].endswith("Are you the cab driver?")
    heard = [kw["text_en"] for k, kw in published if k == "chat" and kw["sender"] == "driver"]
    assert heard == ["Yes I am the driver. Where exactly are you?", "Okay, I'm coming, reach in two minutes",
                     "No, that's all"]
    assert s.call.profile.pickup_landmark.split(",")[0] in said[1]          # gave the pickup point
    assert "anything else" in said[2]                                      # driver coming: asked before hanging up
    assert None in sent                                                    # hung up after "No, that's all"
    assert notes and notes[0].startswith("📞 Call answered for you")
    assert all(m["streamSid"] == "MZ1" for m in sent if m and m["event"] == "media")


def run(coro):
    return asyncio.run(coro)


def test_webhook_and_stream_are_locked_down():
    from tripowner.server import app
    with setting(**KEYS, phone_mode="stream", twilio_call_from=""):
        c = TestClient(app)
        form = {"CallSid": "CA1", "From": "+919812345678", "To": "+15550000000"}
        assert c.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
        sig = twilio_signature(KEYS["public_base_url"] + "/twilio/voice", form, "secret-token")
        r = c.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200 and 'url="wss://example.trycloudflare.com/twilio/stream"' in r.text
        assert '<Parameter name="token"' in r.text
        with c.websocket_connect("/twilio/stream") as ws:                 # no token: dropped
            ws.send_text('{"event": "start", "start": {"streamSid": "MZ9", "customParameters": {}}}')
            try:
                ws.receive_text()
                assert False, "should have been closed"
            except Exception:
                pass
    with setting(**{**KEYS, "public_demo": True}):
        assert TestClient(app).post("/twilio/voice", data={}).status_code == 404


def test_call_me_rings_only_the_test_number_and_points_at_the_webhook():
    import httpx
    from urllib.parse import parse_qs
    sent = []

    def fake(req):
        sent.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        return httpx.Response(201, json={"sid": "CA9"})

    with setting(**KEYS, twilio_phone_number="+17372508034", phone_test_to="+919800000009"):
        assert asyncio.run(phone.call_me(httpx.MockTransport(fake))) == "CA9"
    assert sent == [{"To": "+919800000009", "From": "+17372508034",          # no extra params: trials refuse them
                     "Url": "https://example.trycloudflare.com/twilio/voice"}]
    with setting(**KEYS, twilio_phone_number="+17372508034", twilio_call_from="+15005550006",
                 phone_test_to="+919800000009"):
        asyncio.run(phone.call_me(httpx.MockTransport(fake)))
    assert sent[-1]["From"] == "+15005550006"                                 # the trial number, when set
    with setting(**KEYS, twilio_phone_number="+17372508034", phone_test_to=""):
        try:
            asyncio.run(phone.call_me(httpx.MockTransport(fake)))
            assert False, "should need PHONE_TEST_TO"
        except RuntimeError as e:
            assert "PHONE_TEST_TO" in str(e)


def test_trial_mode_takes_turns_with_gather_and_say(monkeypatch):
    """A free trial blocks live audio: Twilio hears and speaks, TripOwner decides each reply."""
    from tripowner import server
    from tripowner.providers.voice import MockVoice
    notes = []

    async def notify(t):
        notes.append(t)

    monkeypatch.setattr(server, "voice", MockVoice())
    monkeypatch.setattr(server, "get_llm", lambda: None)               # rules decide: no network in tests
    monkeypatch.setattr(server, "_telegram_update", notify)

    def post(c, path, form):
        sig = twilio_signature(KEYS["public_base_url"] + path, form, "secret-token")
        return c.post(path, data=form, headers={"X-Twilio-Signature": sig})

    with setting(**KEYS, phone_mode="gather"):
        c = TestClient(server.app)
        call = {"CallSid": "CA7", "From": "+15550000000", "To": "+919800000009"}
        r = post(c, "/twilio/voice", call)
        assert r.status_code == 200 and "<Stream" not in r.text
        assert '<Gather input="speech" language="en-IN"' in r.text and "Are you the cab driver?" in r.text
        assert 'action="https://example.trycloudflare.com/twilio/turn"' in r.text
        r = post(c, "/twilio/turn", {**call, "SpeechResult": "Yes I am the driver. Where exactly are you?"})
        landmark = server._gather_calls["CA7"].call.profile.pickup_landmark.split(",")[0]
        assert "<Gather" in r.text and landmark in r.text                  # gave the pickup point, keeps listening
        r = post(c, "/twilio/turn", {**call, "SpeechResult": "Give me the customer's phone number"})
        assert "<Gather" in r.text and "+91" not in r.text
        r = post(c, "/twilio/turn", call)                                   # silence: asks again
        assert "didn't catch that" in r.text and "<Gather" in r.text
        r = post(c, "/twilio/turn", {**call, "SpeechResult": "Okay, I'm coming, reach in two minutes"})
        assert "anything else" in r.text and "<Gather" in r.text           # driver on the way: one more chance
        r = post(c, "/twilio/turn", {**call, "SpeechResult": "No, that's all"})
        assert "<Hangup/>" in r.text and "<Gather" not in r.text           # nothing else: ends the call
        sig = twilio_signature(KEYS["public_base_url"] + "/twilio/turn", call, "secret-token")
        tampered = {**call, "SpeechResult": "injected"}
        assert c.post("/twilio/turn", data=tampered, headers={"X-Twilio-Signature": sig}).status_code == 403
        assert "CA7" not in server._gather_calls
    assert notes and notes[0].startswith("📞 Call answered for you")


def test_trial_proxy_calls_without_a_signature_are_checked_with_twilio(monkeypatch):
    """Twilio's trial proxy strips X-Twilio-Signature: accept only a live call on our own account."""
    from tripowner import server
    from tripowner.providers.voice import MockVoice
    live = {"CA" + "1" * 32}

    async def lookup(sid, transport=None):
        return sid in live

    async def notify(t):
        pass

    monkeypatch.setattr(phone, "live_call_on_account", lookup)
    monkeypatch.setattr(server, "voice", MockVoice())
    monkeypatch.setattr(server, "get_llm", lambda: None)
    monkeypatch.setattr(server, "_telegram_update", notify)
    with setting(**KEYS, phone_mode="gather", twilio_call_from="+17372508034"):
        c = TestClient(server.app)
        good = {"CallSid": "CA" + "1" * 32, "From": "+17372508034", "To": "+919800000009"}
        assert c.post("/twilio/voice", data={**good, "CallSid": "CA" + "2" * 32}).status_code == 403  # not live
        assert c.post("/twilio/turn", data={**good, "SpeechResult": "hi"}).status_code == 403          # no call yet
        r = c.post("/twilio/voice", data=good)
        assert r.status_code == 200 and "<Gather" in r.text
        r = c.post("/twilio/turn", data={**good, "SpeechResult": "Yes I am the driver. Where are you?"})
        assert r.status_code == 200 and "<Say" in r.text
        assert c.post("/twilio/voice", data=good, headers={"X-Twilio-Signature": "forged"}).status_code == 403
    with setting(**KEYS, phone_mode="gather", twilio_call_from=""):              # not on a trial: signature needed
        assert TestClient(server.app).post("/twilio/voice", data=good).status_code == 403


def test_live_call_lookup_only_accepts_ringing_or_live_calls():
    import httpx
    statuses = {"CA" + "1" * 32: "in-progress", "CA" + "2" * 32: "completed"}

    def fake(req):
        sid = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(200, json={"status": statuses[sid]}) if sid in statuses else httpx.Response(404, json={})

    t = httpx.MockTransport(fake)
    with setting(**KEYS):
        assert asyncio.run(phone.live_call_on_account("CA" + "1" * 32, t))
        assert not asyncio.run(phone.live_call_on_account("CA" + "2" * 32, t))      # ended
        assert not asyncio.run(phone.live_call_on_account("CA" + "3" * 32, t))      # not ours
        assert not asyncio.run(phone.live_call_on_account("../Accounts", t))         # not a CallSid at all


def test_network_blip_while_verifying_a_trial_call_hangs_up_politely(monkeypatch):
    """A real call failed when the hotspot dropped: verifying the call timed out and the server crashed (500)."""
    import httpx
    from tripowner import server
    calls = []

    async def flaky(sid, transport=None):
        calls.append(sid)
        raise httpx.ConnectTimeout("no network")

    monkeypatch.setattr(phone, "live_call_on_account", flaky)
    with setting(**KEYS, phone_mode="gather", twilio_call_from="+17372508034"):
        r = TestClient(server.app).post("/twilio/voice", data={"CallSid": "CA" + "1" * 32, "From": "+1"})
    assert r.status_code == 200 and "<Hangup/>" in r.text and "call back" in r.text and "<Gather" not in r.text


def test_live_call_lookup_retries_once_on_a_dropped_connection():
    import httpx
    tries = []

    def fake(req):
        tries.append(1)
        if len(tries) == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"status": "in-progress"})

    with setting(**KEYS):
        assert asyncio.run(phone.live_call_on_account("CA" + "1" * 32, httpx.MockTransport(fake)))
    assert len(tries) == 2


def test_trial_mode_never_keeps_twilio_waiting_for_a_slow_reply(monkeypatch):
    """Twilio's trial proxy hung up on replies that took Nemotron ~2 s. Now /twilio/turn answers at once with
    "One moment.", and /twilio/reply is polled (short pause each time) until the reply is ready."""
    import time as _time
    from tripowner import server
    from tripowner.phone import GatherCall
    from tripowner.providers.voice import MockVoice

    async def notify(t):
        pass

    async def slow_turn(self, speech):
        await asyncio.sleep(0.3)                                          # "Nemotron thinking"
        return "Asha will be waiting at Gate 2.", False

    async def lookup(sid, transport=None):
        return True

    monkeypatch.setattr(server, "voice", MockVoice())
    monkeypatch.setattr(server, "get_llm", lambda: None)
    monkeypatch.setattr(server, "_telegram_update", notify)
    monkeypatch.setattr(server, "REPLY_POLL_S", 0.05)
    monkeypatch.setattr(phone, "live_call_on_account", lookup)
    monkeypatch.setattr(GatherCall, "turn", slow_turn)
    with setting(**KEYS, phone_mode="gather", twilio_call_from="+17372508034"), TestClient(server.app) as c:
        call = {"CallSid": "CA" + "5" * 32, "From": "+17372508034", "To": "+919800000009"}
        assert c.post("/twilio/voice", data=call).status_code == 200
        t0 = _time.time()
        r = c.post("/twilio/turn", data={**call, "SpeechResult": "Where are you?"})
        assert _time.time() - t0 < 0.25                                   # answered at once, not after 0.3 s
        assert "One moment." in r.text and 'twilio/reply' in r.text and "<Gather" not in r.text
        r = c.post("/twilio/reply", data=call)
        assert '<Pause length="1"/>' in r.text and "twilio/reply" in r.text   # still thinking: pause, ask again
        for _ in range(20):
            if "<Gather" in r.text:
                break
            _time.sleep(0.05)
            r = c.post("/twilio/reply", data=call)
        assert "Gate 2" in r.text and 'action="https://example.trycloudflare.com/twilio/turn"' in r.text
        assert c.post("/twilio/reply", data={**call, "CallSid": "CA" + "6" * 32}).status_code == 403   # not our call
