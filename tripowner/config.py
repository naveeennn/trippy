"""Runtime settings, read from environment / .env.

Everything has a working default so the whole system runs offline in mock mode.
Set LLM_* to use Nemotron (NIM on Nebius) and SARVAM_API_KEY to use Sarvam voice.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # LLM: any OpenAI-compatible endpoint. Self-hosted NIM on Nebius, or Nebius' hosted API.
    llm_base_url: str = _env("LLM_BASE_URL")
    llm_api_key: str = _env("LLM_API_KEY")
    llm_model: str = _env("LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
    # Optional separate model for translation (empty = LLM_MODEL). We tried Nemotron 3 Nano here: on our
    # driver replies it was slower (3.4 s vs 2.0 s median) and once reversed a meaning, so Super stays the default.
    llm_fast_model: str = _env("LLM_FAST_MODEL")
    # Some Nemotron versions toggle reasoning via the system prompt, e.g. "/no_think".
    llm_system_prefix: str = _env("LLM_SYSTEM_PREFIX")
    llm_timeout_s: float = float(_env("LLM_TIMEOUT_S", "60"))
    # Reasoning models spend tokens thinking before the tool call; leave them room.
    llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", "2000"))

    # Sarvam: Indic speech + translation
    sarvam_api_key: str = _env("SARVAM_API_KEY")
    sarvam_base_url: str = _env("SARVAM_BASE_URL", "https://api.sarvam.ai")
    sarvam_translate_model: str = _env("SARVAM_TRANSLATE_MODEL", "mayura:v1")
    sarvam_tts_model: str = _env("SARVAM_TTS_MODEL", "bulbul:v3")
    sarvam_tts_speaker: str = _env("SARVAM_TTS_SPEAKER", "shubh")          # the driver's voice
    sarvam_agent_speaker: str = _env("SARVAM_AGENT_SPEAKER", "kavya")     # TripOwner's voice
    sarvam_stt_model: str = _env("SARVAM_STT_MODEL", "saaras:v2.5")
    voice_tts: bool = _env("VOICE_TTS", "0") == "1"
    # Public demo: max characters sent to Sarvam per day (translation + speech); 0 = no cap.
    # Over it, translation falls back to the phrasebook and calls use the browser's voice.
    sarvam_daily_chars: int = int(_env("SARVAM_DAILY_CHARS", "0"))

    # Driver persona: "scripted" (offline phrasebook) or "llm" (needs LLM + Sarvam)
    driver_mode: str = _env("DRIVER_MODE", "scripted")

    # Real calendar: an iCal URL (e.g. Google Calendar's "secret address in iCal format") or a local .ics path.
    calendar_ics: str = _env("CALENDAR_ICS")
    # Where you start from. Must be one of the places in providers/maps.py.
    user_location: str = _env("USER_LOCATION", "Indiranagar")
    # Your name, as it appears in real emails and alerts for trips from your calendar.
    user_name: str = _env("USER_NAME", "Asha")
    # Watch mode: re-read the calendar every WATCH_POLL_SEC, and start owning a trip WATCH_LEAD_MIN
    # before its planned booking time. WATCH_ON_START=1 turns it on when the server starts.
    watch_poll_sec: float = float(_env("WATCH_POLL_SEC", "120"))
    watch_lead_min: int = int(_env("WATCH_LEAD_MIN", "30"))
    watch_on_start: bool = _env("WATCH_ON_START", "0") == "1"

    # Other ways to get there when cabs keep failing. The metro is always considered where there's a
    # station; bike taxis only if the rider opts in (not everyone wants one on the way to a meeting).
    allow_bike_taxi: bool = _env("ALLOW_BIKE_TAXI", "0") == "1"

    # Real email (Gmail: use an App Password, not your normal password). Attendee emails go only to
    # SMTP_USER (you) unless EMAIL_TO_ATTENDEES=1.
    smtp_host: str = _env("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(_env("SMTP_PORT", "587"))   # 587 + STARTTLS; many networks block 465
    smtp_user: str = _env("SMTP_USER")
    smtp_app_password: str = _env("SMTP_APP_PASSWORD").replace(" ", "")
    email_to_attendees: bool = _env("EMAIL_TO_ATTENDEES", "0") == "1"

    # Safety: who to alert, and how long to wait for "Are you OK?" before alerting them.
    # Emergency contacts' phones (E.164, comma-separated), called by Twilio from TWILIO_PHONE_NUMBER.
    # On a Twilio trial each one must be a verified caller ID.
    emergency_phones: tuple = tuple(p.strip() for p in _env("EMERGENCY_PHONES").split(",") if p.strip())
    emergency_emails: tuple = tuple(e.strip() for e in _env("EMERGENCY_EMAILS").split(",") if e.strip())
    safety_checkin_timeout_s: float = float(_env("SAFETY_CHECKIN_TIMEOUT_S", "60"))
    # Real rides: check on the rider if their phone's GPS stays within GPS_STALL_RADIUS_M for GPS_STALL_MIN.
    gps_stall_min: float = float(_env("GPS_STALL_MIN", "5"))
    gps_silent_min: float = float(_env("GPS_SILENT_MIN", "5"))     # no location this long mid-ride -> check in
    gps_stall_radius_m: float = float(_env("GPS_STALL_RADIUS_M", "60"))
    # Telegram alerts: a bot token from @BotFather, and the chat IDs of contacts who've messaged the bot.
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_ids: tuple = tuple(c.strip() for c in _env("TELEGRAM_CHAT_IDS").split(",") if c.strip())

    # Public demo (hosted for judges): never send real email / Telegram / alerts, ignore the personal
    # calendar, and cap what Nemotron can spend per day (falls back to the rule agent when reached).
    public_demo: bool = _env("PUBLIC_DEMO", "0") == "1"
    llm_daily_budget_usd: float = float(_env("LLM_DAILY_BUDGET_USD", "0"))   # 0 = no cap
    llm_price_in_per_m: float = float(_env("LLM_PRICE_IN_PER_M", "0.30"))
    llm_price_out_per_m: float = float(_env("LLM_PRICE_OUT_PER_M", "0.90"))

    # Real rides for trips from your calendar: RIDE_PROVIDER=link sends one-tap Uber/Ola booking buttons on Telegram;
    # RIDE_PROVIDER=uber books through Uber for Business Guest Rides
    # (watch mode, real clock). Sandbox while UBER_SANDBOX_RUN_ID is set. Never used by simulations or the demo.
    ride_provider: str = _env("RIDE_PROVIDER", "sim")
    uber_client_id: str = _env("UBER_CLIENT_ID")
    uber_client_secret: str = _env("UBER_CLIENT_SECRET")
    uber_org_uuid: str = _env("UBER_ORG_UUID")
    uber_sandbox_run_id: str = _env("UBER_SANDBOX_RUN_ID")
    uber_guest_phone: str = _env("UBER_GUEST_PHONE")          # your phone in E.164, e.g. +9198xxxxxxxx
    uber_product: str = _env("UBER_PRODUCT", "")              # e.g. "Uber Go"; empty = cheapest
    uber_api_base: str = _env("UBER_API_BASE", "https://api.uber.com")
    uber_auth_url: str = _env("UBER_AUTH_URL", "https://auth.uber.com/oauth/v2/token")
    # Namma Yatri / ONDC over Beckn (RIDE_PROVIDER=beckn): needs registration on ONDC as a buyer app
    beckn_subscriber_id: str = _env("BECKN_SUBSCRIBER_ID")      # your registered domain, e.g. tripowner.example.com
    beckn_key_id: str = _env("BECKN_KEY_ID", "k1")
    beckn_private_key: str = _env("BECKN_PRIVATE_KEY")          # base64 Ed25519 signing key
    beckn_bap_uri: str = _env("BECKN_BAP_URI")                  # public URL of this server's /beckn endpoint
    beckn_gateway_uri: str = _env("BECKN_GATEWAY_URI")
    beckn_vehicle: str = _env("BECKN_VEHICLE", "CAB")           # CAB | AUTO_RICKSHAW
    beckn_rider_phone: str = _env("BECKN_RIDER_PHONE")
    # Real phone calls (Twilio): forward unanswered calls to your Twilio number and TripOwner answers
    twilio_account_sid: str = _env("TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = _env("TWILIO_AUTH_TOKEN")
    twilio_phone_number: str = _env("TWILIO_PHONE_NUMBER")       # your Twilio number, e.g. +17372508034
    public_base_url: str = _env("PUBLIC_BASE_URL").rstrip("/")   # e.g. https://xyz.trycloudflare.com
    phone_lang: str = _env("PHONE_LANG", "hi-IN")                # greeting language; switches to the caller's
    phone_test_to: str = _env("PHONE_TEST_TO")                   # "Call me" test: the only number it rings
    # stream = live audio (Sarvam voices, any language); gather = Twilio listens and speaks, English only.
    # A free trial blocks live audio, so use gather there.
    phone_mode: str = _env("PHONE_MODE", "stream")
    twilio_call_from: str = _env("TWILIO_CALL_FROM")             # trial: the trial number Twilio assigned you
    # Standing permission: book without asking when the fare is within SPEND_CAP_INR (you still get updates).
    auto_book: bool = _env("AUTO_BOOK", "0") == "1"

    # Policy
    spend_cap_inr: float = float(_env("SPEND_CAP_INR", "600"))
    max_bookings_per_trip: int = int(_env("MAX_BOOKINGS_PER_TRIP", "4"))

    @property
    def llm_enabled(self) -> bool:
        # Hosted endpoints need a key; a self-hosted NIM over plain http usually doesn't.
        return bool(self.llm_base_url) and (bool(self.llm_api_key) or self.llm_base_url.startswith("http://"))

    @property
    def sarvam_enabled(self) -> bool:
        return bool(self.sarvam_api_key)


settings = Settings()
