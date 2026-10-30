import asyncio

from tripowner.providers.voice import NemotronVoice, clean_script


class FakeTranslator:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    async def chat(self, messages, **_):
        self.calls += 1
        return {"role": "assistant", "content": self.reply}


def run(c):
    return asyncio.run(c)


def test_free_form_reply_is_translated():
    llm = FakeTranslator("ನಾನು Gate 2, Indiranagar Metro Station ಹತ್ರ ಇದ್ದೀನಿ.")
    out = run(NemotronVoice(llm).from_english("You can find me at Gate 2, Indiranagar Metro Station.", "kn-IN"))
    assert out.startswith("ನಾನು") and llm.calls == 1


def test_phrasebook_lines_cost_no_tokens():
    llm = FakeTranslator("should not be used")
    out = run(NemotronVoice(llm).from_english("Okay, thank you.", "kn-IN"))
    assert out == "ಸರಿ, ಧನ್ಯವಾದಗಳು." and llm.calls == 0


def test_garbled_script_falls_back_to_english():
    llm = FakeTranslator("ನಾನು గేట్ 2 ಹತ್ರ 円")      # Telugu + CJK mixed into Kannada
    text = "Please use the in-app call."
    assert run(NemotronVoice(llm).from_english(text, "kn-IN")) == text
    assert llm.calls == 2                            # one retry, then English


def test_clean_script():
    assert clean_script("ಸರ್, Gate 2 ಹತ್ರ ಬನ್ನಿ", "kn-IN")
    assert not clean_script("ಸರ್ గేట్", "kn-IN")
    assert clean_script("सर, गेट 2 पर आइए", "hi-IN")
    assert not clean_script("bâtiment", "en-IN")


def test_sarvam_daily_cap_falls_back_to_browser_voice():
    from tripowner.config import settings
    from tripowner.providers import voice as V
    old = settings.sarvam_daily_chars
    object.__setattr__(settings, "sarvam_daily_chars", 10)
    try:
        V.sarvam_usage.chars = 0
        assert V.sarvam_usage.allow(8) and not V.sarvam_usage.allow(5)
        v = V.SarvamVoice.__new__(V.SarvamVoice)          # no network: budget check comes first
        assert asyncio.run(v.synthesize("long sentence here", "hi-IN", force=True)) is None
    finally:
        object.__setattr__(settings, "sarvam_daily_chars", old)
        V.sarvam_usage.chars = 0
