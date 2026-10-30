"""Live call: the driver phones, TripOwner answers, and nothing is scripted.

The "driver" is either you (speak into the mic, or type) or Nemotron improvising a driver with a hidden
goal. Whatever the driver says is transcribed (Sarvam speech-to-text), translated to English, and
Nemotron decides the reply from the whole conversation plus the ride context. The reply passes the same
guardrails as the trip agent (never the rider's number, email or home address), then is translated and
spoken back in the driver's language.

Without an LLM the assistant falls back to the rule agent's replies; without Sarvam, text works and
speech uses the browser's voice.
"""
import json
import logging
import re
import unicodedata

from .agent.guardrails import Guardrails
from .agent.policies import RuleAgent
from .agent.tools import MAX_PICKUP_WAIT_MIN, call_summary
from .config import settings
from .i18n import LANG_NAMES, render
from .sim.chaos import make_profile

log = logging.getLogger(__name__)

ASSISTANT_PROMPT = """You are TripOwner, {name}'s assistant, answering a phone call from their cab driver
because {name} can't take it. You speak FOR {name}, TO the driver. Ride: {driver} is driving a {vehicle}
({plate}) to pick {name} up at "{landmark}", about {eta} min away. {name} is waiting there.

Rules:
- Understand what the driver actually means, even if it's informal or mixed language, and answer that.
  You get the driver's own words plus a machine translation; the translation can be wrong (e.g. "saamaan"
  means luggage, not goods), so trust the original words when they disagree.
- You know only the ride details above. You do NOT know {name}'s luggage, clothes, how many people are coming,
  the OTP, or anything else not written here. If asked, say plainly that you don't know and {name} will tell
  them at the pickup. Never guess or make up a detail, not even a likely one like "just a small bag".
- Keep each reply to one or two short spoken sentences of simple English (it is translated for the driver).
- Give the pickup point exactly as "{landmark}" when the driver can't find it. If they ask {name} to walk
  somewhere else, say {name} will wait at the pickup point unless it's very close and safe.
- Only state what you know. Don't invent details about the surroundings (shops, colours, which side of the
  road) or claim the pickup is near a landmark the driver mentions; you can't see where they are. If the
  driver describes their location, say you can't see it from here and repeat the pickup point.
- You are the rider's assistant, never the driver: don't say things like "heading there now". If the driver
  says they're on the way, thank them and say {name} will be waiting.
- Never share {name}'s phone number, email or home address. They can stay on this line or call back this way.
- If the driver says they still need more than 10 minutes to reach the pickup, ask them to come within 10
  minutes; only if they can't, say you'll cancel in the app and book another cab, and set "end_call" to true.
- If the driver says they're far or asks {name} to cancel, without saying how long they need, don't agree
  straight away: first ask whether they can reach the pickup within 10 minutes.
- If the driver is already at the pickup and waiting, {name} is the one who's late: apologise for the wait, say
  {name} is coming to "{landmark}" now, and never threaten to cancel.
- Never agree to cancel the ride, pay cash, go "offline", or change the fare outside the app. Say politely
  that everything stays in the app.
- Never agree to pay anything extra on {name}'s behalf (AC charge, night charge, "far area" charge, coming
  back empty, a fixed tip). The app fare is what's paid. Don't bring up tips; if the driver asks, say a tip is
  up to {name} (never "your choice": you are talking to the driver).
  Stay polite and don't argue; if the driver keeps insisting or threatens to cancel, set "handover" to true.
- If the driver is rude, threatening, or asks something you shouldn't decide, say {name} will call back,
  and set "handover" to true.
- Talk like a person, not a recording: react to what the driver just said (traffic, delay, a question).
  Once the driver knows the pickup point, don't repeat it in every reply; say it again only if they ask
  or seem confused. Never give the same reply twice in a row.
- Set "end_call" to true once the driver is clearly on the way or has arrived and knows where to go.
Answer only with JSON: {{"reply": "...", "end_call": false, "handover": false}}"""

PERSONAS = {
    "lost": ("You can't find the rider. You are near a different exit, close to a big pillar and a juice shop. "
             "You keep describing landmarks and asking where exactly they are."),
    "impatient": ("You are in a hurry. You want the rider to walk to the main road because turning in is slow, "
                  "and you complain about traffic. You settle down if they're polite and clear."),
    "cash": ("You try to get the rider to cancel the ride in the app and pay you cash directly, saying the app "
             "is giving problems and you'll give a discount. You push a bit, then accept if refused."),
    "extra": ("You ask for extra money on top of the app fare: first an AC charge of 100 rupees, then you say "
              "the area is far and you'll come back empty so add 150 more, maybe hint you'll cancel otherwise. "
              "Keep it realistic; accept and come if they politely stay firm."),
    "chatty": ("You are friendly and talkative. You confirm the pickup, ask whether they have luggage, and "
               "mention you'll arrive in a few minutes."),
}

DRIVER_PROMPT = """You are a Bengaluru cab driver on a phone call with your rider's assistant. {persona}
Speak casual everyday {language} the way drivers really talk (mixing English words is fine), written in
{language} script. One or two short sentences per turn: only the words you say aloud, no notes, no stage
directions, no emojis. Answer ONLY with JSON: {{"say": "<your words>", "done": false}}. Set "done" to true
(with "say" empty) once the conversation has clearly finished."""


_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*(?:conversation|END|continue)[^)]*\)", re.IGNORECASE)
_EMOJI = re.compile("[🀀-🫿☀-➿️‍]")


_NUM_WORDS = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "twenty-five": 25,
              "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45, "fifty": 50, "sixty": 60}
_NUM = r"(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_MINUTES = re.compile(_NUM + r"\s*(?:(?:-|to|or)\s*" + _NUM + r"\s*)?(?:min|mins|minute|minutes)\b", re.IGNORECASE)
_HOURS = re.compile(r"\b(half an hour|half hour|an hour|one hour|1 hour|(\d)\s*hours?|two hours)\b", re.IGNORECASE)


def driver_delay_min(text: str) -> int | None:
    """How long the driver says they'll take to reach the pickup, in minutes, if they said so.
    "Stuck in traffic, half an hour" -> 30; "reach in 5 min" -> 5. Ignores time already spent waiting."""
    t = (text or "").lower()
    if re.search(r"\b(waiting|waited|been here|since)\b", t):
        return None
    found = []
    for m in _HOURS.finditer(t):
        w = m.group(1)
        found.append(30 if "half" in w else 120 if "two" in w else int(m.group(2)) * 60 if m.group(2) else 60)
    for m in _MINUTES.finditer(t):
        w = (m.group(2) or m.group(1)).lower()            # "15-20 min": plan for the worst, 20
        found.append(_NUM_WORDS.get(w) or int(w))
    return max(found) if found else None


_YES = re.compile(r"\b(no problem|not a problem|yes|yeah|yep|ok|okay|sure|fine|done|i'll try|i will try|"
                  r"i'll come|i will come|coming|on my way|fast|quickly|right away|haan|theek|sari)\b")
_NO = re.compile(r"\b(no|nope|can't|cannot|can not|not possible|impossible|won't|will not|difficult|"
                 r"no way|nahi|nahin|aagalla)\b")


def driver_agrees(text: str) -> str | None:
    """The driver's answer to "can you come within 10 minutes?": "yes", "no", or None if it's unclear."""
    t = (text or "").lower()
    t_no = re.sub(r"\b(no problem|not a problem)\b", "", t)       # "no problem" means yes
    if _NO.search(t_no):
        return "no"
    if _YES.search(t):
        return "yes"
    return None


MAX_CLOSING_ASKS = 2      # "anything else for {name}?" before hanging up, at most this many times a call
_DONE = re.compile(r"\b(no|nope|nothing|that's all|that is all|that's it|thats all|no thanks|bye|goodbye|ok|okay|"
                   r"fine|thanks|thank you|nahi|bas|illa|saku)\b")
_MORE = re.compile(r"\b(but|fare|extra|money|rupees|charge|want|need|pay|cash|minutes?|where|why|how|can you|tell)\b")


_SIGN_OFF = re.compile(r"\b(that's all|that is all|thats all|that's it|nothing else|nothing more|no thanks|bye|goodbye)\b")


def driver_is_done(text: str, strict: bool = False) -> bool:
    """The answer to "anything else?" was a short no / ok / bye, with nothing new in it. strict: only an
    unmistakable sign-off ("that's all", "bye"), not a bare "okay" that may just be agreeing."""
    t = (text or "").lower()
    return (len(t.split()) <= 6 and bool((_SIGN_OFF if strict else _DONE).search(t)) and not _MORE.search(t))


def clean_spoken(text: str) -> str:
    """Strip stage notes the model sometimes adds, and emojis, before anything is spoken or shown."""
    return re.sub(r"\s+", " ", _EMOJI.sub("", _BRACKETS.sub("", text or ""))).strip()


def _too_similar(a: str, b: str) -> bool:
    """Near-identical replies (the 'broken record' problem)."""
    from difflib import SequenceMatcher
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.75


def _json_obj(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class LiveCall:
    def __init__(self, voice, llm, lang: str = "hi-IN", persona: str = "you", rider: str | None = None):
        self.voice, self.llm, self.lang, self.persona = voice, llm, lang, persona
        self.profile = make_profile("Indiranagar", rider or settings.user_name)
        self.guard = Guardrails(self.profile, settings.spend_cap_inr, settings.max_bookings_per_trip)
        self.ride = {"driver": "Manjunath", "vehicle": "Swift Dzire", "plate": "KA-01-AB-4821", "eta": 3}
        self.history: list[tuple[str, str]] = []      # ("driver"|"agent", English)
        self.native_driver: list[str] = []            # what the driver said, in their language (for the AI driver)
        self.ended = False
        self.handover = False
        self.blocked = 0
        self.too_late: int | None = None              # minutes, when the driver couldn't come within the limit
        self.negotiating: int | None = None           # asked the driver to hurry (they'd said this many minutes)
        self.hurry_asks = 0
        self.promised: int | None = None              # minutes the driver agreed to after being asked to hurry
        self.closing_asks = 0                         # times we asked "anything else?" instead of hanging up
        self.awaiting_more = False                    # the last thing we said was "anything else?"

    # ---- helpers ------------------------------------------------------------------------------
    def _system(self) -> str:
        return ASSISTANT_PROMPT.format(name=self.profile.name, landmark=self.profile.pickup_landmark, **self.ride)

    async def _say(self, english: str, role: str = "agent") -> dict:
        native = await self.voice.from_english(english, self.lang)
        audio = await self.voice.synthesize(native, self.lang, force=True, role=role)
        return {"text": native, "text_en": english, "audio": audio, "lang": self.lang}

    # ---- the call -----------------------------------------------------------------------------
    async def greeting(self) -> dict:
        en = render("agent.greeting", "en-IN", name=self.profile.name)
        native = render("agent.greeting", self.lang, name=self.profile.name)
        audio = await self.voice.synthesize(native, self.lang, force=True, role="agent")
        self.history.append(("agent", en))
        return {"text": native, "text_en": en, "audio": audio, "lang": self.lang}

    async def hear(self, audio: bytes | None = None, text: str | None = None) -> dict:
        """What the driver said -> {native, english}. Audio is transcribed; text is translated."""
        if audio:
            english, detected = await self.voice.speech_to_english(audio, "driver.wav")
            native = english
            try:
                native = await self.voice.speech_to_native(audio, self.lang)
            except Exception as e:                    # native transcript is for display only
                log.info("native transcript unavailable: %s", e)
            if detected and detected in LANG_NAMES:
                self.lang = detected
            return {"native": native, "english": english, "lang": self.lang}
        english = await self.voice.to_english(text or "", self.lang) if self.lang != "en-IN" else (text or "")
        return {"native": text or "", "english": english, "lang": self.lang}

    async def respond(self, driver_english: str, driver_native: str | None = None) -> dict:
        """Decide the reply from what the driver actually said (original words + translation)."""
        driver_english = clean_spoken(driver_english)
        # After "anything else?" (or any later point in the wrap-up), a plain "no, that's all" ends the call,
        # whatever the model's last question was ("Does that work for you?")
        last_agent = next((t for w, t in reversed(self.history) if w == "agent"), "")
        wrapping_up = self.awaiting_more or self.closing_asks >= 1
        after_statement = bool(last_agent) and not last_agent.rstrip().endswith("?")
        if wrapping_up or after_statement:
            self.awaiting_more = False
            if driver_is_done(driver_english, strict=not wrapping_up):
                self.ended = True
                return await self._rule_reply(driver_english, self.goodbye())
        delay = driver_delay_min(driver_english)
        if self.negotiating is not None:
            return await self._hurry_answer(driver_english, delay)
        if delay is not None and delay > MAX_PICKUP_WAIT_MIN:
            return await self._ask_to_hurry(driver_english, delay)
        self.history.append(("driver", driver_english))
        self._native = getattr(self, "_native", {})
        if driver_native and driver_native != driver_english:
            self._native[len(self.history) - 1] = driver_native
        decision = None
        if self.llm is not None:
            msgs = [{"role": "system", "content": self._system()}]
            for i, (who, t) in enumerate(self.history):
                if who == "driver":
                    orig = self._native.get(i)
                    content = f'Driver said: "{orig}"\n(machine translation: "{t}")' if orig else t
                    msgs.append({"role": "user", "content": content})
                else:
                    msgs.append({"role": "assistant", "content": json.dumps({"reply": t})})
            try:
                msg = await self.llm.chat(msgs, temperature=0.3, max_tokens=2500)
                decision = _json_obj(msg.get("content"))
                last = next((t for w, t in reversed(self.history[:-1]) if w == "agent"), "")
                if decision and decision.get("reply") and _too_similar(decision["reply"], last):
                    msgs.append({"role": "assistant", "content": json.dumps(decision)})
                    msgs.append({"role": "user", "content": (
                        "(You just said almost exactly that. Reply to what the driver said this time, "
                        "in different words, without repeating the pickup point unless they asked.)")})
                    retry = _json_obj((await self.llm.chat(msgs, temperature=0.6, max_tokens=2500)).get("content"))
                    if retry and retry.get("reply"):
                        decision = retry
            except Exception as e:
                log.warning("live call LLM failed, using rules: %s", e)
        if not decision or not decision.get("reply"):
            reply = RuleAgent.compose_reply(driver_english, self.profile.pickup_landmark, on_call=True) \
                or render("agent.ok", "en-IN")
            t = driver_english.lower()
            decision = {"reply": reply, "end_call": any(w in t for w in ("coming", "on my way", "arrived", "reached")),
                        "handover": False, "rules": True}
        # NFKC: the model sometimes writes odd spaces (e.g. "Gate 2") that would slip past text checks
        reply = unicodedata.normalize("NFKC", str(decision["reply"])).strip()
        verdict = self.guard.check_driver_message(reply)
        if not verdict.allowed:                       # the model tried to share something private
            self.blocked += 1
            reply = render("agent.no_phone_call", "en-IN")
        self.history.append(("agent", reply))
        # Once the driver is clearly on the way or there, wrap up (the model sometimes keeps going).
        t = driver_english.lower()
        on_way = any(w in t for w in ("on my way", "i'm coming", "i am coming", "i'll reach", "i will reach",
                                      "reach in", "arrived", "i'm here", "i am here", "already at", "heading",
                                      "get there", "be there", "right there", "leaving now", "i'll be at"))
        drivers_turns = sum(1 for w, _ in self.history if w == "driver")
        there = on_way or any(w in t for w in ("reached", "waiting", "outside"))
        # Never hang up on the driver's first sentence ("Hello, I'm your driver") unless they're on the way or
        # there: the model once did, before the driver could say they'd be 30 minutes late.
        self.ended = (bool(decision.get("end_call")) and (drivers_turns >= 2 or there)) or (on_way and drivers_turns >= 2)
        self.handover = bool(decision.get("handover"))
        reply = self._closing(reply)
        self.history[-1] = ("agent", reply)
        out = await self._say(reply)
        out.update(end_call=self.ended, handover=self.handover, blocked=not verdict.allowed,
                   by="rules" if decision.get("rules") else "nemotron")
        return out

    def goodbye(self) -> str:
        """The last line. Mentions the pickup point only if it already came up: a delivery or a bank caller
        mustn't learn where the rider will be."""
        name, landmark = self.profile.name, self.profile.pickup_landmark
        if any(landmark.split(",")[0].lower() in t.lower() for w, t in self.history if w == "agent"):
            return f"Okay, thank you. {name} will be waiting at {landmark}. Bye!"
        return f"Okay, thank you. I'll pass that on to {name}. Bye!"

    def _closing(self, reply: str) -> str:
        """About to hang up? First give the caller the chance to say more (at most MAX_CLOSING_ASKS times a call):
        a driver who agreed to hurry may still want to raise the fare. Not when cancelling or handing over."""
        if self.handover or self.too_late or self.closing_asks >= MAX_CLOSING_ASKS:
            return reply
        if re.search(r"anything else", reply, re.IGNORECASE):     # the model asked it itself: that's our question
            self.closing_asks += 1
            self.ended, self.awaiting_more = False, True
            return reply
        # Once we've asked "anything else?", a new topic the driver raised (e.g. the fare) gets handled and then
        # the same question again, even if the model didn't mark the conversation finished.
        reopened = self.closing_asks >= 1 and not reply.rstrip().endswith("?")
        if not (self.ended or reopened):
            return reply
        self.closing_asks += 1
        self.ended, self.awaiting_more = False, True
        return f"{reply} Is there anything else you'd like me to tell {self.profile.name}?"

    # ---- the driver needs too long: negotiate first, cancel only if they can't make it --------------
    # A fixed rule, not the model's call. Over MAX_PICKUP_WAIT_MIN: ask them to come within it; a yes (or a
    # time within the limit) keeps the ride, a no (or still too long) cancels it and books another cab.
    async def _rule_reply(self, driver_english: str, reply: str, **extra) -> dict:
        self.history.append(("driver", driver_english))
        self.history.append(("agent", reply))
        out = await self._say(reply)
        out.update(end_call=self.ended, handover=False, blocked=False, by="rules", **extra)
        return out

    async def _ask_to_hurry(self, driver_english: str, minutes: int) -> dict:
        self.negotiating, self.hurry_asks = minutes, 1
        return await self._rule_reply(driver_english, (
            f"{minutes} minutes is too long, {self.profile.name} has to leave soon. Can you make it within "
            f"{MAX_PICKUP_WAIT_MIN} minutes? Otherwise I'll have to cancel and book another cab."))

    async def _hurry_answer(self, driver_english: str, delay: int | None) -> dict:
        name, landmark = self.profile.name, self.profile.pickup_landmark
        verdict = ("yes" if delay <= MAX_PICKUP_WAIT_MIN else "no") if delay is not None else driver_agrees(driver_english)
        if verdict == "yes":
            self.promised = delay if delay is not None else MAX_PICKUP_WAIT_MIN
            self.negotiating, self.ended = None, True
            return await self._rule_reply(driver_english, self._closing(
                f"Thank you! {name} will be waiting at {landmark}. Please come within {MAX_PICKUP_WAIT_MIN} minutes."))
        if verdict is None and self.hurry_asks < 2:
            self.hurry_asks += 1
            return await self._rule_reply(driver_english, (
                f"Sorry, just to confirm: can you reach the pickup within {MAX_PICKUP_WAIT_MIN} minutes, yes or no?"))
        minutes = delay if delay is not None else self.negotiating
        self.too_late, self.negotiating, self.ended = minutes, None, True
        return await self._rule_reply(driver_english, (
            f"Okay, no problem. Since you can't come within {MAX_PICKUP_WAIT_MIN} minutes, I'll cancel this ride "
            f"in the app and book another cab. Thanks for telling us."), too_late=minutes)

    async def ai_driver_line(self) -> str | None:
        """Nemotron improvises the driver's next line (in their language), or None when finished."""
        if self.llm is None:
            return None
        persona = PERSONAS.get(self.persona, PERSONAS["chatty"])
        msgs = [{"role": "system", "content": DRIVER_PROMPT.format(persona=persona, language=LANG_NAMES[self.lang])}]
        for who, t in self.history:
            msgs.append({"role": "assistant" if who == "driver" else "user", "content": t})
        if not self.history or self.history[-1][0] == "driver":
            msgs.append({"role": "user", "content": "(the call has just been answered)"})
        for _ in range(2):          # JSON keeps stray "thinking" out of what's spoken; retry once if unusable
            msg = await self.llm.chat(msgs, temperature=0.9, max_tokens=4000)
            data = _json_obj(msg.get("content"))
            if data is None:
                continue
            if data.get("done"):
                return None
            line = clean_spoken(str(data.get("say", "")))
            if line:
                return line
        return None

    async def summary_llm(self) -> str:
        """One line for the rider, written by Nemotron from the actual conversation (rules if unavailable)."""
        if self.llm is None or len(self.history) < 2:
            return self.summary()
        native = getattr(self, "_native", {})
        convo = "\n".join(
            (f'Driver: "{native[i]}" (machine translation: "{t}")' if w == "driver" and i in native
             else f"{'Driver' if w == 'driver' else 'Assistant'}: {t}")
            for i, (w, t) in enumerate(self.history))
        name = self.profile.name
        try:
            msg = await self.llm.chat([
                {"role": "system", "content": (
                    f"{name} is the rider: a cab is coming to pick {name} up. The caller is usually that cab's "
                    f"driver; the Assistant answered {name}'s phone for them. Write ONE short sentence to {name}, "
                    f"in the second person (\"Your driver...\", \"you\"): who called and what they wanted, what was "
                    f"agreed, and anything {name} must do. Get the roles right: the driver picks {name} up, never "
                    f"the other way round. The outcome below is certain; state it. No preamble.")},
                {"role": "user", "content": f"{convo}\n\nOutcome: {self.outcome()}"}], temperature=0.2, max_tokens=2500)
            return (msg.get("content") or "").strip() or self.summary()
        except Exception:
            return self.summary()

    def outcome(self) -> str:
        """What the call settled, from the fixed rules (the summary must not get this wrong)."""
        if self.too_late:
            return (f"the driver couldn't come within {MAX_PICKUP_WAIT_MIN} minutes, so this ride is being "
                    f"cancelled and another cab booked")
        if self.promised is not None:
            return f"the driver agreed to reach the pickup point within {self.promised} minutes; the ride is kept"
        if self.handover:
            return f"handed over: {self.profile.name} should call the caller back"
        return "no change to the ride"

    def summary(self) -> str:
        return call_summary(self.ride["driver"], self.history, False) + \
            (" Handed over: you should call back." if self.handover else "")
