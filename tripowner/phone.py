"""Real phone calls: TripOwner answers when the driver rings and you can't pick up.

  your phone --(forward when unanswered)--> Twilio number --webhook--> /twilio/voice
  Twilio streams the call audio (8 kHz mu-law) over a WebSocket --> /twilio/stream
    listen   : detect when the caller stops talking (energy-based), send that stretch to Sarvam speech-to-text
    think    : LiveCall (Nemotron + guardrails): "Are you the driver?", pickup help, no cash / extra charges /
               phone number; not the driver -> take a message
    speak    : Sarvam text-to-speech -> 8 kHz mu-law, streamed back into the call
  after the call: a one-line summary to you on Telegram (and on the dashboard)

Every webhook is checked against Twilio's signature, and the audio stream needs a one-time token from it.
Never active in the public demo.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import math
import secrets
import time
import wave
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

from .config import settings
from .i18n import LANG_NAMES, render
from .live_call import ASSISTANT_PROMPT, LiveCall

log = logging.getLogger(__name__)

RATE = 8000                       # Twilio media streams: 8 kHz, 8-bit mu-law, mono
FRAME = 160                       # 20 ms
MAX_CALL_S = 240
MAX_UTTERANCE_S = 12
END_SILENCE_MS = 700              # this much quiet after speech = the caller finished
MIN_SPEECH_MS = 300


def phone_enabled() -> bool:
    return bool(settings.twilio_account_sid and settings.twilio_auth_token) and not settings.public_demo


# ---- G.711 mu-law -------------------------------------------------------------------------------
def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if u & 0x80 else (t - 0x84)


def _linear_to_ulaw(x: int) -> int:
    """16-bit sample -> mu-law byte (the classic Sun g711.c algorithm, on the 14-bit value)."""
    v = x >> 2
    mask = 0x7F if v < 0 else 0xFF
    v = min(-v if v < 0 else v, 8159) + (0x84 >> 2)
    seg = next((i for i, end in enumerate((0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)) if v <= end), 8)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((v >> (seg + 1)) & 0xF)) ^ mask


DECODE = [_ulaw_to_linear(i) for i in range(256)]
_ENCODE = bytes(_linear_to_ulaw(x - 32768) for x in range(65536))


def ulaw_decode(data: bytes) -> list[int]:
    return [DECODE[b] for b in data]


def ulaw_encode(samples: list[int]) -> bytes:
    return bytes(_ENCODE[max(-32768, min(32767, s)) + 32768] for s in samples)


def resample(samples: list[int], src: int, dst: int) -> list[int]:
    if src == dst or not samples:
        return samples
    n = int(len(samples) * dst / src)
    step, last = src / dst, len(samples) - 1
    out = []
    for i in range(n):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a, b = samples[min(j, last)], samples[min(j + 1, last)]
        out.append(int(a + (b - a) * frac))
    return out


def pcm16_wav(samples: list[int], rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return buf.getvalue()


def wav_to_ulaw8k(wav_b64: str) -> bytes:
    """Sarvam's base64 WAV (any rate, 16-bit) -> 8 kHz mu-law for the phone line."""
    with wave.open(io.BytesIO(base64.b64decode(wav_b64))) as w:
        rate, ch, width, raw = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit audio")
    samples = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2 * ch)]
    return ulaw_encode(resample(samples, rate, RATE))


def rms(samples: list[int]) -> float:
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


# ---- Twilio webhook security -------------------------------------------------------------------
def twilio_signature(url: str, params: dict, token: str) -> str:
    data = url + "".join(k + str(params[k]) for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def valid_twilio_request(url: str, params: dict, signature: str) -> bool:
    expected = twilio_signature(url, params, settings.twilio_auth_token)
    return bool(signature) and hmac.compare_digest(expected, signature)


_tokens: dict[str, float] = {}          # one-time stream tokens -> expiry


def stream_twiml(ws_url: str) -> str:
    token = secrets.token_urlsafe(24)
    _tokens[token] = time.time() + 60
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f'<Stream url={quoteattr(ws_url)}><Parameter name="token" value={quoteattr(token)}/></Stream>'
            '</Connect></Response>')


async def live_call_on_account(call_sid: str, transport=None) -> bool:
    """Trial calls reach us through Twilio's proxy without a signature. Check with Twilio instead: is this
    a call on our account that's ringing or in progress right now? A guessed or old CallSid fails."""
    import httpx
    if not (call_sid.startswith("CA") and len(call_sid) == 34 and call_sid.isalnum()):
        return False
    async with httpx.AsyncClient(base_url=f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/",
                                 auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=5,
                                 transport=transport) as c:
        for attempt in range(2):                      # a flaky connection: one quick retry, then give up
            try:
                r = await c.get(f"Calls/{call_sid}.json")
                break
            except httpx.TransportError:
                if attempt:
                    raise
                await asyncio.sleep(0.5)
    return r.status_code == 200 and r.json().get("status") in ("queued", "ringing", "in-progress")


async def call_me(transport=None) -> str:
    """Testing without call forwarding: Twilio rings PHONE_TEST_TO, and when you pick up, TripOwner answers you
    exactly as it would a forwarded call (same /twilio/voice webhook). Only ever calls that one number."""
    import httpx
    if not phone_enabled():
        raise RuntimeError("Twilio isn't set up (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)")
    missing = [n for n, v in (("TWILIO_PHONE_NUMBER", settings.twilio_phone_number),
                              ("PUBLIC_BASE_URL", settings.public_base_url),
                              ("PHONE_TEST_TO", settings.phone_test_to)) if not v]
    if missing:
        raise RuntimeError("set " + ", ".join(missing) + " in .env")
    async with httpx.AsyncClient(base_url=f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/",
                                 auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=20,
                                 transport=transport) as c:
        # Url only: a trial account refuses extra parameters (Twilio posts to Url by default)
        r = await c.post("Calls.json", data={"To": settings.phone_test_to,
                                             "From": settings.twilio_call_from or settings.twilio_phone_number,
                                             "Url": settings.public_base_url + "/twilio/voice"})
    if r.status_code >= 300:
        raise RuntimeError(r.json().get("message", f"Twilio said {r.status_code}"))
    return r.json()["sid"]


def take_token(token: str | None) -> bool:
    now = time.time()
    for t in [t for t, exp in _tokens.items() if exp < now]:
        _tokens.pop(t, None)
    return bool(token) and _tokens.pop(token, 0) >= now


# ---- the conversation ----------------------------------------------------------------------------
PHONE_INTRO = """You are TripOwner, {name}'s assistant, answering {name}'s phone because {name} can't take it.
{ride} You don't know yet who is calling: it is usually the cab driver, but could be anyone.
If the caller talks about the pickup, waiting, a ride, traffic or where {name} is, they are the driver: help them
right away, don't ask them to confirm who they are.
If you can't make out what the caller said or who they are (an unclear line, a garbled or half-translated
sentence, a language you didn't catch), don't guess and don't offer to take a message: ask once, simply,
"Sorry, I didn't catch that. Are you the cab driver?"
If the caller is NOT the driver (a delivery, a friend, a sales call...), don't discuss the ride: politely take
a short message. In the reply where you confirm you'll pass their message on (or they say there's none), say
{name} will call back and set "end_call" to true in that same reply. If it is the driver, help as below.
"""


class PhoneCall(LiveCall):
    """LiveCall for a real caller: unknown caller, real ride details when TripOwner is owning a trip."""

    def __init__(self, voice, llm, lang: str, ride: dict | None = None):
        super().__init__(voice, llm, lang, persona="you")
        self.real_ride = ride

    def _system(self) -> str:
        name, landmark = self.profile.name, self.profile.pickup_landmark
        if self.real_ride:
            r = self.real_ride
            ride = (f'{name} has a cab booked: {r["driver"]} in a {r["vehicle"]} ({r["plate"]}), picking {name} up at '
                    f'"{landmark}". The app says the driver is about {r["eta"]} min from the pickup (the ETA of the '
                    f'driver, not of {name}).')
        else:
            ride = f'If {name} has a cab coming, the pickup point is "{landmark}"; you don\'t know the driver or car.'
        rules = ASSISTANT_PROMPT.split("Rules:", 1)[1]
        return PHONE_INTRO.format(name=name, ride=ride) + "\nRules:" + rules.replace(
            "{name}", name).replace("{landmark}", landmark).replace("{{", "{").replace("}}", "}")

    async def hear(self, audio: bytes | None = None, text: str | None = None) -> dict:
        """Both transcripts at once: every second counts on a phone line."""
        if not audio:
            return await super().hear(audio, text)
        (english, detected), native = await asyncio.gather(
            self.voice.speech_to_english(audio, "caller.wav"), self._native_text(audio))
        if detected and detected in LANG_NAMES:
            self.lang = detected
        return {"native": native or english, "english": english, "lang": self.lang}

    async def _native_text(self, audio: bytes) -> str:
        try:
            return await self.voice.speech_to_native(audio, self.lang)
        except Exception:
            return ""

    async def greeting(self) -> dict:
        en = render("agent.phone_greeting", "en-IN", name=self.profile.name)
        native = render("agent.phone_greeting", self.lang, name=self.profile.name)
        audio = await self.voice.synthesize(native, self.lang, force=True, role="agent")
        self.history.append(("agent", en))
        return {"text": native, "text_en": en, "audio": audio, "lang": self.lang}


@dataclass
class Listener:
    """Splits the caller's audio into utterances: speech, then END_SILENCE_MS of quiet."""
    noise: float = 200.0
    speech: list[int] = field(default_factory=list)
    voiced_ms: int = 0
    quiet_ms: int = 0

    def feed(self, samples: list[int]) -> list[int] | None:
        level = rms(samples)
        threshold = max(600.0, self.noise * 3)
        if level > threshold:
            self.speech.extend(samples)
            self.voiced_ms += 20
            self.quiet_ms = 0
        elif self.speech:
            self.speech.extend(samples)
            self.quiet_ms += 20
        else:
            self.noise = 0.95 * self.noise + 0.05 * level          # learn the line's background noise
        done = self.speech and (self.quiet_ms >= END_SILENCE_MS or len(self.speech) >= MAX_UTTERANCE_S * RATE)
        if not done:
            return None
        out, enough = self.speech, self.voiced_ms >= MIN_SPEECH_MS
        self.speech, self.voiced_ms, self.quiet_ms = [], 0, 0
        return out if enough else None


class TwilioCallSession:
    """One live call over a Twilio media stream. `send` puts a JSON message on the WebSocket."""

    def __init__(self, send, voice, llm, publish=None, notify=None, ride: dict | None = None,
                 lang: str | None = None):
        self.send, self.publish, self.notify = send, publish or (lambda *a, **k: None), notify
        self.call = PhoneCall(voice, llm, lang or settings.phone_lang, ride)
        self.voice = voice
        self.listener = Listener()
        self.stream_sid: str | None = None
        self.speaking = 0                      # replies queued on the line; we don't listen over ourselves
        self.utterances: asyncio.Queue = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.started = time.time()
        self.finished = asyncio.Event()
        self._filler: dict[str, bytes] = {}

    # ---- audio out ------------------------------------------------------------------------------
    async def play(self, ulaw: bytes, mark: str) -> None:
        self.speaking += 1
        for i in range(0, len(ulaw), RATE):            # one-second chunks
            await self.send({"event": "media", "streamSid": self.stream_sid,
                             "media": {"payload": base64.b64encode(ulaw[i:i + RATE]).decode()}})
        await self.send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": mark}})

    async def say(self, line: dict, mark: str = "reply") -> None:
        if line.get("audio"):
            await self.play(wav_to_ulaw8k(line["audio"]), mark)
        self.publish("chat", sender="agent", text=line.get("text", ""), text_en=line.get("text_en", ""),
                     lang=line.get("lang", self.call.lang), audio=None, spoken=True)

    async def filler(self) -> None:
        lang = self.call.lang
        if lang not in self._filler:
            audio = await self.voice.synthesize(render("agent.one_moment", lang), lang, force=True, role="agent")
            self._filler[lang] = wav_to_ulaw8k(audio) if audio else b""
        if self._filler[lang]:
            await self.play(self._filler[lang], "filler")

    # ---- events from Twilio ---------------------------------------------------------------------
    async def on_message(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "start":
            self.stream_sid = msg["start"]["streamSid"]
            self.publish("phone", state="answered", lang=self.call.lang)
            self.worker = asyncio.create_task(self._work())
            await self.say(await self.call.greeting(), "greeting")
        elif ev == "media" and self.stream_sid:
            if time.time() - self.started > MAX_CALL_S:
                await self.hang_up("time limit")
                return
            if self.speaking:                          # it's our turn to talk; ignore echo/background
                return
            utterance = self.listener.feed(ulaw_decode(base64.b64decode(msg["media"]["payload"])))
            if utterance:
                self.speaking += 1                     # hold the floor until the reply has played
                await self.utterances.put(utterance)
        elif ev == "mark":
            self.speaking = max(0, self.speaking - 1)
        elif ev == "stop":
            await self.finish()

    async def _work(self) -> None:
        while True:
            utterance = await self.utterances.get()
            try:
                await self._turn(utterance)
            except Exception as e:
                log.warning("phone turn failed: %s", e)
                self.publish("phone", state="error", error=str(e)[:200])
            finally:
                self.speaking = max(0, self.speaking - 1)
            if self.call.ended or self.call.handover:
                await self.hang_up("handover" if self.call.handover else "done")
                return

    async def _turn(self, utterance: list[int]) -> None:
        filler = asyncio.create_task(self.filler())
        heard = await self.call.hear(audio=pcm16_wav(resample(utterance, RATE, 16000), 16000))
        self.publish("chat", sender="driver", text=heard["native"], text_en=heard["english"], lang=heard["lang"],
                     audio=None, spoken=True)
        if not heard["english"].strip():
            await filler
            return
        reply = await self.call.respond(heard["english"], heard["native"])
        await filler
        await self.say(reply)

    async def hang_up(self, why: str) -> None:
        # Let the last reply finish playing, then end the call by closing the stream.
        for _ in range(150):
            if not self.speaking:
                break
            await asyncio.sleep(0.1)
        self.publish("phone", state="ending", why=why)
        await self.send(None)
        await self.finish()

    async def finish(self) -> None:
        if self.finished.is_set():
            return
        self.finished.set()
        if self.worker:
            self.worker.cancel()
        summary = await self.call.summary_llm() if len(self.call.history) > 1 else "Missed call: the caller didn't speak."
        if self.call.handover:
            summary += " They want you to call back."
        self.publish("phone", state="ended", summary=summary)
        if self.notify:
            try:
                await self.notify(f"📞 Call answered for you: {summary}")
            except Exception as e:
                log.warning("call summary not sent: %s", e)


# ---- trial accounts: no media streams, so take turns with <Gather> and <Say> ---------------------
# Twilio's free trial blocks <Stream>. Here Twilio does the listening (speech recognition) and the speaking
# (its own voice); TripOwner only decides each reply, with the same Nemotron prompt and guardrails. English only.
GATHER_LANG = "en-IN"
MAX_SILENCES = 2


class _NoAudio:
    """Twilio reads the replies out itself, so skip text-to-speech."""

    def __init__(self, voice):
        self._voice = voice

    def __getattr__(self, name):
        return getattr(self._voice, name)

    async def synthesize(self, *args, **kwargs):
        return None


def _say(text: str) -> str:
    return f'<Say voice="Polly.Raveena" language="{GATHER_LANG}">{escape(text)}</Say>'


def gather_twiml(text: str, action: str, hang_up: bool = False) -> str:
    """Say `text`, then listen for the caller's answer and post it to `action` (or hang up)."""
    head = '<?xml version="1.0" encoding="UTF-8"?><Response>'
    if hang_up:
        return f"{head}{_say(text)}<Hangup/></Response>"
    return (f'{head}<Gather input="speech" language="{GATHER_LANG}" speechTimeout="auto" '
            f'action={quoteattr(action)} method="POST">{_say(text)}</Gather>'
            f'<Redirect method="POST">{escape(action)}</Redirect></Response>')     # no answer: same URL, no speech


class GatherCall:
    """One call in turn-taking mode. Twilio posts each thing the caller says; we answer with the next TwiML."""

    def __init__(self, voice, llm, publish=None, notify=None, ride: dict | None = None):
        self.publish, self.notify = publish or (lambda *a, **k: None), notify
        self.call = PhoneCall(_NoAudio(voice), llm, GATHER_LANG, ride)
        self.silences = 0
        self.started = time.time()
        self.finished = False
        self.pending: asyncio.Task | None = None   # the reply being worked out (trial mode polls for it)

    async def start(self) -> str:
        line = await self.call.greeting()
        self.publish("phone", state="answered", lang=GATHER_LANG)
        self.publish("chat", sender="agent", text=line["text_en"], text_en=line["text_en"], lang=GATHER_LANG,
                     audio=None, spoken=True)
        return line["text_en"]

    async def turn(self, speech: str) -> tuple[str, bool]:
        """What the caller said -> (what to say back, hang up after it)."""
        speech = (speech or "").strip()
        name = self.call.profile.name
        if not speech and self.call.awaiting_more:        # silence after "anything else?" means no
            self.call.awaiting_more, self.call.ended = False, True
            return self.call.goodbye(), True
        if not speech:
            self.silences += 1
            if self.silences >= MAX_SILENCES:
                return f"Sorry, I can't hear you. {name} will call you back. Goodbye.", True
            return "Sorry, I didn't catch that. Could you say it again?", False
        self.silences = 0
        self.publish("chat", sender="driver", text=speech, text_en=speech, lang=GATHER_LANG, audio=None, spoken=True)
        reply = await self.call.respond(speech)
        self.publish("chat", sender="agent", text=reply["text_en"], text_en=reply["text_en"], lang=GATHER_LANG,
                     audio=None, spoken=True)
        done = self.call.ended or self.call.handover or time.time() - self.started > MAX_CALL_S
        return reply["text_en"], done

    async def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        summary = await self.call.summary_llm() if len(self.call.history) > 1 else "Missed call: the caller didn't speak."
        if self.call.handover:
            summary += " They want you to call back."
        self.publish("phone", state="ended", summary=summary)
        if self.notify:
            try:
                await self.notify(f"📞 Call answered for you: {summary}")
            except Exception as e:
                log.warning("call summary not sent: %s", e)
