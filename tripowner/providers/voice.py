"""Voice / language layer. Sarvam for Indic speech + translation, mock for offline.

The agent always reasons in English. This layer sits between it and the driver:
  driver (kn/hi, text or audio) -> to_english -> agent -> from_english -> driver
"""
import logging
from abc import ABC, abstractmethod

import httpx

from ..config import settings
from ..i18n import translate_known

log = logging.getLogger(__name__)


class VoiceProvider(ABC):
    name = "base"

    @abstractmethod
    async def to_english(self, text: str, lang: str) -> str: ...

    @abstractmethod
    async def from_english(self, text: str, lang: str) -> str: ...

    async def speech_to_english(self, audio: bytes, filename: str = "audio.wav") -> tuple[str, str | None]:
        """Audio in any Indic language -> (English transcript, detected language)."""
        raise NotImplementedError(f"{self.name} has no speech input")

    async def speech_to_native(self, audio: bytes, lang: str) -> str:
        """Audio -> transcript in the speaker's own language (for display)."""
        raise NotImplementedError(f"{self.name} has no speech input")

    async def synthesize(self, text: str, lang: str, force: bool = False, role: str = "agent") -> str | None:
        """Text -> base64 WAV, or None if TTS isn't available (the dashboard then uses the browser's voice).
        force=True: speak even when VOICE_TTS is off (phone calls are always spoken).
        role: "agent" or "driver", so the two sides of a call sound different."""
        return None


class MockVoice(VoiceProvider):
    """Phrasebook lookups. Works for scripted drivers and rule-agent replies."""

    name = "mock"

    async def to_english(self, text: str, lang: str) -> str:
        if lang == "en-IN":
            return text
        return translate_known(text, lang, "en-IN") or f"[{lang}, untranslated] {text}"

    async def from_english(self, text: str, lang: str) -> str:
        if lang == "en-IN":
            return text
        return translate_known(text, "en-IN", lang) or text


class PoolVoice(MockVoice):
    """Offline translation for PoolDriver lines, using the English that was generated with them."""

    name = "pool"

    def __init__(self, pool: dict):
        self._en = {x["text"]: x["en"] for by_lang in pool.values() for lines in by_lang.values() for x in lines}

    async def to_english(self, text: str, lang: str) -> str:
        return self._en.get(text) or await super().to_english(text, lang)


# Letters allowed per language besides ASCII; anything else means the model garbled the script.
_SCRIPTS = {"kn-IN": (0x0C80, 0x0CFF), "hi-IN": (0x0900, 0x097F), "en-IN": None}
_LANG_NAMES = {"kn-IN": "Kannada", "hi-IN": "Hindi", "en-IN": "English"}


def clean_script(text: str, lang: str) -> bool:
    block = _SCRIPTS.get(lang)
    for ch in text:
        if ch.isascii() or not (ch.isalpha() or 0x0300 <= ord(ch) <= 0x036F):
            continue
        if block is None or not (block[0] <= ord(ch) <= block[1]):
            return False
    return True


class NemotronVoice(VoiceProvider):
    """Translation through the reasoning LLM, for when there's no Sarvam key.

    Known phrasebook (and pool) sentences are looked up first, so only free-form lines cost tokens.
    Replies that come back in the wrong script are retried once, then sent in English rather than garbled.
    Nemotron's Kannada is noticeably weaker than its Hindi; Sarvam is the better choice for real riders.
    """

    name = "nemotron"

    def __init__(self, llm, base: VoiceProvider | None = None, model: str | None = None):
        self.llm = llm
        self._base = base or MockVoice()
        self.model = model if model is not None else settings.llm_fast_model
        self._cache: dict[tuple[str, str, str], str] = {}

    async def _translate(self, text: str, source: str, target: str) -> str | None:
        key = (text, source, target)
        if key in self._cache:
            return self._cache[key]
        for _ in range(2):
            try:
                msg = await self.llm.chat([
                    {"role": "system", "content": (
                        f"Translate the message from {_LANG_NAMES[source]} to {_LANG_NAMES[target]}. Use simple, "
                        f"polite, everyday words a cab driver would use, written in {_LANG_NAMES[target]} script. "
                        f"Keep place names (like 'Gate 2, Indiranagar Metro Station') exactly as written. "
                        f"Output only the translation."
                    )},
                    {"role": "user", "content": text},
                ], temperature=0.0, max_tokens=3000, model=self.model or None)
            except Exception as e:
                log.warning("nemotron translate failed: %s", e)
                return None
            out = (msg.get("content") or "").strip().strip('"')
            if out and clean_script(out, target):
                self._cache[key] = out
                return out
        return None

    async def to_english(self, text: str, lang: str) -> str:
        known = await self._base.to_english(text, lang)
        if lang == "en-IN" or not known.startswith(f"[{lang}, untranslated]"):
            return known
        return await self._translate(text, lang, "en-IN") or known

    async def from_english(self, text: str, lang: str) -> str:
        if lang == "en-IN":
            return text
        known = translate_known(text, "en-IN", lang)
        if known:
            return known
        return await self._translate(text, "en-IN", lang) or text


class SarvamUsage:
    """Characters sent to Sarvam today, for the public demo's daily cap."""

    def __init__(self):
        from datetime import date
        self.day, self.chars = date.today(), 0

    def allow(self, n: int) -> bool:
        from datetime import date
        if date.today() != self.day:
            self.day, self.chars = date.today(), 0
        if settings.sarvam_daily_chars and self.chars + n > settings.sarvam_daily_chars:
            return False
        self.chars += n
        return True


sarvam_usage = SarvamUsage()


class SarvamVoice(VoiceProvider):
    name = "sarvam"

    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.sarvam_base_url,
            headers={"api-subscription-key": settings.sarvam_api_key},
            timeout=20,
        )
        self._cache: dict[tuple[str, str, str], str] = {}
        self._fallback = MockVoice()

    async def _translate(self, text: str, source: str, target: str) -> str:
        if source == target:
            return text
        key = (text, source, target)
        if key in self._cache:
            return self._cache[key]
        if not sarvam_usage.allow(len(text)):
            raise RuntimeError("today's Sarvam budget is used up")
        body = {
            "input": text,
            "source_language_code": source,
            "target_language_code": target,
            "model": settings.sarvam_translate_model,
        }
        if settings.sarvam_translate_model.startswith("mayura"):
            body["mode"] = "modern-colloquial"   # how drivers actually talk
        r = await self._client.post("/translate", json=body)
        r.raise_for_status()
        out = r.json()["translated_text"]
        self._cache[key] = out
        return out

    async def to_english(self, text: str, lang: str) -> str:
        try:
            return await self._translate(text, lang, "en-IN")
        except Exception as e:  # never let a voice hiccup kill the trip
            log.warning("sarvam to_english failed: %s", e)
            return await self._fallback.to_english(text, lang)

    async def from_english(self, text: str, lang: str) -> str:
        try:
            return await self._translate(text, "en-IN", lang)
        except Exception as e:
            log.warning("sarvam from_english failed: %s", e)
            return await self._fallback.from_english(text, lang)

    async def speech_to_english(self, audio: bytes, filename: str = "audio.wav") -> tuple[str, str | None]:
        if not sarvam_usage.allow(300):               # ~ a short utterance's worth of budget
            raise RuntimeError("today's Sarvam budget is used up")
        r = await self._client.post(
            "/speech-to-text-translate",
            files={"file": (filename, audio)},
            data={"model": settings.sarvam_stt_model},
        )
        r.raise_for_status()
        j = r.json()
        return j["transcript"], j.get("language_code")

    async def speech_to_native(self, audio: bytes, lang: str) -> str:
        r = await self._client.post("/speech-to-text", files={"file": ("audio.wav", audio)},
                                    data={"model": "saarika:v2.5", "language_code": lang})
        r.raise_for_status()
        return r.json()["transcript"]

    async def synthesize(self, text: str, lang: str, force: bool = False, role: str = "agent") -> str | None:
        if not (settings.voice_tts or force):
            return None
        if not sarvam_usage.allow(len(text)):
            return None                      # over today's budget: the dashboard uses the browser's voice
        try:
            r = await self._client.post("/text-to-speech", json={
                "text": text,
                "language_code": lang,
                "model": settings.sarvam_tts_model,
                "speaker": settings.sarvam_agent_speaker if role == "agent" else settings.sarvam_tts_speaker,
            })
            r.raise_for_status()
            return r.json()["audios"][0]
        except Exception as e:
            log.warning("sarvam tts failed: %s", e)
            return None


def make_voice() -> VoiceProvider:
    if settings.sarvam_enabled:
        return SarvamVoice()
    base: VoiceProvider = MockVoice()
    if settings.driver_mode == "pool":
        from ..driver import load_pool
        base = PoolVoice(load_pool())
    from ..llm import get_llm   # local import: llm imports config, keep module import order simple
    llm = get_llm()
    return NemotronVoice(llm, base) if llm else base
