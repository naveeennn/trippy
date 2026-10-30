import asyncio
import json

from tripowner.live_call import LiveCall
from tripowner.providers.voice import MockVoice


class ScriptedLLM:
    """Returns given JSON replies in order; records the conversation it was shown."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    async def chat(self, messages, **_):
        self.seen.append(messages)
        r = self.replies.pop(0)
        return {"role": "assistant", "content": r if isinstance(r, str) else json.dumps(r)}


class EchoVoice(MockVoice):
    async def to_english(self, text, lang):
        return text                       # tests write the driver's words in English already

    async def speech_to_english(self, audio, filename="audio.wav"):
        return "Where exactly are you standing?", "hi-IN"

    async def speech_to_native(self, audio, lang):
        return "आप ठीक कहाँ खड़े हो?"


def run(c):
    return asyncio.run(c)


def test_reply_comes_from_what_the_driver_said_with_context():
    llm = ScriptedLLM({"reply": "Everything stays in the app; please come to the pickup point.", "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.greeting())
    r = run(c.respond("Cancel the ride and pay me cash, 50 rupees off"))
    assert "app" in r["text_en"] and r["by"] == "nemotron"
    system, *turns = llm.seen[0]
    assert "Gate 2, Indiranagar Metro Station" in system["content"]          # knows the pickup point
    assert turns[-1]["content"] == "Cancel the ride and pay me cash, 50 rupees off"   # sees the real words


def test_guardrail_rewrites_a_leak():
    llm = ScriptedLLM({"reply": "Sure, her number is +91 98450 12345", "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    r = run(c.respond("Give me her number"))
    assert r["blocked"] and "98450" not in r["text_en"] and "call back" in r["text_en"]


def test_rules_when_no_llm_and_the_call_ends():
    c = LiveCall(EchoVoice(), None, "en-IN", rider="Asha")
    r = run(c.respond("Where are you? I can't find you"))
    assert "Gate 2" in r["text_en"] and r["by"] == "rules"
    r = run(c.respond("Okay, I'm coming now"))
    assert not r["end_call"] and "anything else" in r["text_en"]     # asks before hanging up
    r = run(c.respond("No, that's all"))
    assert r["end_call"] and "Bye" in r["text_en"]


def test_audio_is_transcribed():
    c = LiveCall(EchoVoice(), None, "hi-IN", rider="Asha")
    heard = run(c.hear(audio=b"RIFF....fake"))
    assert heard["english"] == "Where exactly are you standing?" and heard["native"].startswith("आप")


def test_endpoints_typed_turn(monkeypatch):
    from fastapi.testclient import TestClient
    import tripowner.server as srv
    monkeypatch.setattr(srv, "voice", EchoVoice())
    monkeypatch.setattr(srv, "get_llm", lambda: None)
    client = TestClient(srv.app)
    assert client.post("/api/livecall/start", json={"lang": "en-IN"}).status_code == 200
    r = client.post("/api/livecall/say", data={"text": "where are you"})
    assert r.status_code == 200 and "Gate 2" in r.json()["reply"]["text_en"]


def test_does_not_repeat_itself():
    same = {"reply": "Please come to Gate 2, Indiranagar Metro Station. Asha will be waiting there.", "end_call": False}
    llm = ScriptedLLM(same, same, {"reply": "No problem, take your time with the traffic.", "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.respond("Where should I come?"))
    r = run(c.respond("Traffic is heavy, I'll be a few minutes late"))
    assert r["text_en"] == "No problem, take your time with the traffic."     # asked again, answered the new thing


def test_stage_notes_and_emojis_are_never_spoken():
    from tripowner.live_call import clean_spoken
    assert clean_spoken("Okay, coming 🚗😤 [Continue conversation until done; no END yet.]") == "Okay, coming"


def test_extra_money_persona_exists_and_prompt_forbids_paying_extra():
    from tripowner.live_call import ASSISTANT_PROMPT, PERSONAS
    assert "extra" in PERSONAS and "AC charge" in ASSISTANT_PROMPT


def test_driver_too_late_is_a_fixed_rule_not_the_models_call():
    from tripowner.live_call import driver_delay_min
    assert driver_delay_min("Traffic condition, take me a half an hour to come and pick up") == 30
    assert driver_delay_min("stuck, 15-20 mins") == 20                     # a range: plan for the worst
    assert driver_delay_min("reach in 5 min") == 5
    assert driver_delay_min("I have been waiting 15 minutes") is None      # time already spent, not a delay
    assert driver_delay_min("ok ok") is None

    def late_call():
        c = LiveCall(EchoVoice(), ScriptedLLM(), "en-IN", rider="Asha")     # no replies queued: never asked
        run(c.greeting())
        r = run(c.respond("Way to traffic condition take me a half an hour to come and picking up"))
        assert "within 10 minutes" in r["text_en"] and not c.ended and c.too_late is None   # negotiates first
        return c

    c = late_call()                                                        # agrees: ride kept
    r = run(c.respond("Okay okay, I will come fast"))
    assert not c.ended and c.too_late is None and "will be waiting" in r["text_en"] and "anything else" in r["text_en"]
    run(c.respond("No, that's all"))
    assert c.ended and c.too_late is None

    c = late_call()                                                        # a time within the limit: kept
    run(c.respond("Fine, 8 minutes"))
    assert c.awaiting_more and c.too_late is None and c.promised == 8

    c = late_call()                                                        # can't: cancel and rebook
    r = run(c.respond("Not possible, too much traffic"))
    assert c.too_late == 30 and c.ended and r["too_late"] == 30 and "book another cab" in r["text_en"]

    c = late_call()                                                        # still too long: cancel
    run(c.respond("At least 20 minutes"))
    assert c.too_late == 20

    c = late_call()                                                        # unclear twice: asks once more, then cancels
    r = run(c.respond("Hmm, traffic is there"))
    assert "yes or no" in r["text_en"] and not c.ended
    run(c.respond("Let me see"))
    assert c.too_late == 30 and c.ended

    llm = ScriptedLLM({"reply": "Great, Asha will be waiting.", "end_call": True})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    r = run(c.respond("I'll reach in 8 minutes"))                         # within the limit: normal reply
    assert c.too_late is None and r["by"] == "nemotron"


def test_odd_unicode_spaces_in_replies_are_normalised():
    """Nemotron once wrote "Gate 2" (narrow no-break space): it must read as plain "Gate 2"."""
    llm = ScriptedLLM({"reply": "Please come to Gate 2, Indiranagar Metro Station.", "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    r = run(c.respond("Where are you?"))
    assert "Gate 2, Indiranagar" in r["text_en"] and " " not in r["text_en"]


def test_summary_knows_the_roles_and_the_settled_outcome():
    """The real call's summary said "the driver asked Jishnu to pick him up". The summary prompt now states
    who picks up whom, and the outcome from the fixed rules."""
    llm = ScriptedLLM("Your driver will reach Gate 2 within 5 minutes; the ride is kept.")
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.greeting())
    run(c.respond("Traffic is bad, I will be late by 30 minutes."))
    run(c.respond("OK then fine, I'll be there within 5 minutes."))
    assert c.promised == 5 and c.outcome().startswith("the driver agreed to reach the pickup point within 5 minutes")
    run(c.summary_llm())
    system, user = llm.seen[-1][0]["content"], llm.seen[-1][1]["content"]
    assert "the driver picks Asha up, never the other way round" in system and "second person" in system
    assert user.endswith("Outcome: the driver agreed to reach the pickup point within 5 minutes; the ride is kept")

    c = LiveCall(EchoVoice(), ScriptedLLM(), "en-IN", rider="Asha")
    run(c.respond("I need 30 minutes."))
    run(c.respond("Not possible."))
    assert "cancelled and another cab booked" in c.outcome()


def test_never_hangs_up_on_the_drivers_first_hello():
    """Nemotron once set end_call on "Hello, I am your cab driver", before the driver could mention a delay."""
    llm = ScriptedLLM({"reply": "Thanks, Asha will be waiting at the pickup.", "end_call": True})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.respond("Hello, I am your cab driver."))
    assert not c.ended
    llm = ScriptedLLM({"reply": "Great, Asha is coming out.", "end_call": True})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    r = run(c.respond("I have reached Gate 2, I am waiting."))             # there already: fine to wrap up...
    assert c.awaiting_more and "anything else" in r["text_en"]               # ...after asking "anything else?"


def test_driver_gets_to_say_more_before_the_call_ends():
    """The user's case: late by 30 -> agrees to 10 -> "anything else?" -> asks for a higher fare -> refused ->
    "anything else?" again (even if the model didn't end the call) -> "No, that's all" -> goodbye."""
    llm = ScriptedLLM({"reply": "Sorry, the fare is what the app shows; nothing extra can be added.", "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.greeting())
    run(c.respond("Hello, I am your driver. Traffic is bad, I will be late by 30 minutes."))
    r = run(c.respond("Okay, I will come in 10 minutes."))
    assert not c.ended and r["text_en"].endswith("anything else you'd like me to tell Asha?")
    r = run(c.respond("Yes, one more thing. The fare is too low, tell her to pay 100 rupees more."))
    assert not c.ended and "app" in r["text_en"] and r["text_en"].endswith("tell Asha?")     # asked again
    r = run(c.respond("No, that's all."))
    assert c.ended and r["text_en"].endswith("Bye!") and c.closing_asks == 2 and c.promised == 10


def test_the_models_own_anything_else_counts_as_the_closing_question():
    llm = ScriptedLLM({"reply": "Sorry, the fare stays as in the app. Is there anything else I can help you with?",
                       "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.respond("I will be late by 30 minutes."))
    run(c.respond("Okay, 10 minutes."))
    run(c.respond("Tell her to pay 100 rupees more."))
    assert c.awaiting_more and c.closing_asks == 2
    r = run(c.respond("No, that's all."))
    assert c.ended and r["text_en"].endswith("Bye!")                    # no second "anything else?"


def test_thats_all_ends_the_wrap_up_whatever_the_model_last_asked():
    llm = ScriptedLLM({"reply": "Sorry, the fare stays as in the app. Does that work for you?", "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.respond("I will be late by 30 minutes."))
    run(c.respond("Okay, 10 minutes."))
    run(c.respond("Tell her to pay 100 rupees more."))
    r = run(c.respond("No, that's all."))
    assert c.ended and r["text_en"].endswith("Bye!")


def test_goodbye_never_tells_a_stranger_where_the_rider_will_be():
    llm = ScriptedLLM({"reply": "I'll let Asha know the parcel is at the security desk. She'll call you back.",
                       "end_call": False})
    c = LiveCall(EchoVoice(), llm, "en-IN", rider="Asha")
    run(c.greeting())
    run(c.respond("I am calling from Amazon delivery, the parcel is at the security desk."))
    r = run(c.respond("No, that's all, thanks."))                          # after a statement: a sign-off
    assert c.ended and "Gate 2" not in r["text_en"] and "pass that on to Asha" in r["text_en"]
