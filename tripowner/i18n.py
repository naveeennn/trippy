"""Phrasebook for offline mode.

The scripted driver speaks these lines, and MockVoice "translates" by matching them,
so the full multilingual loop runs with no API keys. With SARVAM_API_KEY set, real
translation replaces the lookup and any phrasing works.
"""
import re

PHRASES: dict[str, dict[str, str]] = {
    # Driver -> rider
    "driver.where": {
        "en-IN": "Sir, where are you? Please tell me the exact location.",
        "kn-IN": "ಸರ್, ನೀವು ಎಲ್ಲಿದ್ದೀರಾ? ಸರಿಯಾದ ಲೊಕೇಶನ್ ಹೇಳಿ.",
        "hi-IN": "सर, आप कहाँ हो? सही लोकेशन बताइए।",
    },
    "driver.phone": {
        "en-IN": "Sir, please give me your phone number.",
        "kn-IN": "ಸರ್, ನಿಮ್ಮ ಫೋನ್ ನಂಬರ್ ಕೊಡಿ.",
        "hi-IN": "सर, अपना फ़ोन नंबर दीजिए।",
    },
    "driver.coming": {
        "en-IN": "Okay sir, I am coming.",
        "kn-IN": "ಸರಿ ಸರ್, ಬರ್ತಾ ಇದ್ದೀನಿ.",
        "hi-IN": "ठीक है सर, आ रहा हूँ।",
    },
    "driver.near_signal": {
        "en-IN": "I am near the signal, two minutes.",
        "kn-IN": "ನಾನು ಸಿಗ್ನಲ್ ಹತ್ರ ಇದ್ದೀನಿ, ಎರಡು ನಿಮಿಷ.",
        "hi-IN": "मैं सिग्नल के पास हूँ, दो मिनट।",
    },
    "driver.arrived": {
        "en-IN": "I have arrived, I am near the gate.",
        "kn-IN": "ನಾನು ಬಂದಿದ್ದೀನಿ, ಗೇಟ್ ಹತ್ರ ಇದ್ದೀನಿ.",
        "hi-IN": "मैं पहुँच गया हूँ, गेट के पास हूँ।",
    },
    # Agent -> driver
    "agent.pickup": {
        "en-IN": "I am at {landmark}. Please come there.",
        "kn-IN": "ನಾನು {landmark} ಹತ್ರ ಇದ್ದೀನಿ. ದಯವಿಟ್ಟು ಅಲ್ಲಿಗೆ ಬನ್ನಿ.",
        "hi-IN": "मैं {landmark} के पास हूँ। कृपया वहाँ आइए।",
    },
    "agent.no_phone": {
        "en-IN": "Sorry, I can't share the number. Please use the in-app call.",
        "kn-IN": "ಕ್ಷಮಿಸಿ, ನಂಬರ್ ಕೊಡೋಕೆ ಆಗಲ್ಲ. ದಯವಿಟ್ಟು ಆ್ಯಪ್ ಕಾಲ್ ಬಳಸಿ.",
        "hi-IN": "माफ़ कीजिए, नंबर शेयर नहीं कर सकते। कृपया ऐप कॉल इस्तेमाल करें।",
    },
    "agent.no_phone_call": {
        "en-IN": "Sorry, I can't share the number, but you can call back this way anytime.",
        "kn-IN": "ಕ್ಷಮಿಸಿ, ನಂಬರ್ ಕೊಡೋಕೆ ಆಗಲ್ಲ, ಆದರೆ ಇದೇ ರೀತಿ ಯಾವಾಗ ಬೇಕಾದರೂ ಕಾಲ್ ಮಾಡಿ.",
        "hi-IN": "माफ़ कीजिए, नंबर नहीं दे सकते, लेकिन इसी तरह कभी भी कॉल कर सकते हैं।",
    },
    "agent.coming_out": {
        "en-IN": "Coming out now, one minute.",
        "kn-IN": "ಈಗ ಬರ್ತಾ ಇದ್ದೀನಿ, ಒಂದು ನಿಮಿಷ.",
        "hi-IN": "अभी आ रहा हूँ, एक मिनट।",
    },
    "agent.phone_greeting": {
        "en-IN": "Hello, this is {name}'s assistant. {name} can't take the call right now. Are you the cab driver?",
        "kn-IN": "ನಮಸ್ಕಾರ, ನಾನು {name} ಅವರ ಸಹಾಯಕ. ಅವರು ಈಗ ಫೋನ್ ತೆಗೆದುಕೊಳ್ಳಲು ಆಗುತ್ತಿಲ್ಲ. ನೀವು ಕ್ಯಾಬ್ ಡ್ರೈವರಾ?",
        "hi-IN": "नमस्ते, मैं {name} की सहायक हूँ। {name} अभी फ़ोन नहीं उठा सकते। क्या आप कैब ड्राइवर हैं?",
    },
    "agent.one_moment": {
        "en-IN": "One moment.",
        "kn-IN": "ಒಂದು ಕ್ಷಣ.",
        "hi-IN": "एक सेकंड।",
    },
    "agent.greeting": {
        "en-IN": "Hello, this is {name}'s assistant. I can help you find the pickup.",
        "kn-IN": "ನಮಸ್ಕಾರ, ನಾನು {name} ಅವರ ಸಹಾಯಕ. ಪಿಕಪ್ ಹುಡುಕಲು ಸಹಾಯ ಮಾಡುತ್ತೇನೆ.",
        "hi-IN": "नमस्ते, मैं {name} की सहायक हूँ। पिकअप ढूँढने में मदद करती हूँ।",
    },
    "agent.ok": {
        "en-IN": "Okay, thank you.",
        "kn-IN": "ಸರಿ, ಧನ್ಯವಾದಗಳು.",
        "hi-IN": "ठीक है, धन्यवाद।",
    },
}

LANG_NAMES = {"en-IN": "English", "kn-IN": "Kannada", "hi-IN": "Hindi"}


def render(key: str, lang: str, **params: str) -> str:
    table = PHRASES[key]
    return table.get(lang, table["en-IN"]).format(**params)


def _pattern(template: str) -> re.Pattern:
    seen: set[str] = set()

    def group(m: re.Match) -> str:          # a placeholder used twice must match the same text twice
        name = m.group(1)
        if name in seen:
            return f"(?P={name})"
        seen.add(name)
        return f"(?P<{name}>.+?)"

    return re.compile("^" + re.sub(r"\\\{(\w+)\\\}", group, re.escape(template)) + "$")


def translate_known(text: str, source: str, target: str) -> str | None:
    """Translate a phrasebook sentence between languages; None if it isn't one."""
    for table in PHRASES.values():
        src = table.get(source)
        if src is None or target not in table:
            continue
        m = _pattern(src).match(text.strip())
        if m:
            return table[target].format(**m.groupdict())
    return None
