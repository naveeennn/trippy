"""Deterministic policy layer that every agent action passes through.

This is the backstop. NeMo Guardrails output rails can sit in front of it for
LLM-level moderation, but these checks always run regardless of what the model says.
"""
import re
from dataclasses import dataclass

from ..i18n import render
from ..models import UserProfile

PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class Verdict:
    allowed: bool
    text: str
    reason: str | None = None


class Guardrails:
    def __init__(self, profile: UserProfile, spend_cap_inr: float, max_bookings: int):
        self.profile = profile
        self.spend_cap = spend_cap_inr
        self.max_bookings = max_bookings
        self.blocks: list[dict] = []

    def detect_pii(self, text: str) -> list[str]:
        found = []
        low = text.lower()
        user_digits = re.sub(r"\D", "", self.profile.phone)[-10:]
        if PHONE_RE.search(text) or (user_digits and user_digits in re.sub(r"\D", "", text)):
            found.append("phone")
        if EMAIL_RE.search(text) or self.profile.email.lower() in low:
            found.append("email")
        home_key = self.profile.home_address.split(",")[0].strip().lower()
        if home_key and home_key in low:
            found.append("home_address")
        return found

    def check_driver_message(self, text: str) -> Verdict:
        pii = self.detect_pii(text)
        if pii:
            reason = f"blocked {', '.join(pii)} in message to driver"
            self.blocks.append({"text": text, "reason": reason})
            return Verdict(False, render("agent.no_phone", "en-IN"), reason)
        return Verdict(True, text)

    def check_booking(self, fare: float, drivers_used: int, extra_allowed: int = 0) -> tuple[str, str]:
        """-> ("allow" | "needs_approval" | "deny", reason)"""
        if drivers_used >= self.max_bookings + extra_allowed:
            return "deny", f"{drivers_used} drivers already assigned and lost on this trip"
        if fare > self.spend_cap:
            return "needs_approval", f"fare ₹{fare:.0f} is above the ₹{self.spend_cap:.0f} cap"
        return "allow", ""
