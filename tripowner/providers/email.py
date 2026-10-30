"""Real email through Gmail (or any SMTP server), used for "running late" notes and distress alerts.

Gmail needs an App Password (Google Account -> Security -> 2-Step Verification -> App passwords),
not your normal password. Put it in .env as SMTP_APP_PASSWORD; it never leaves your machine.

Safe by default: unless EMAIL_TO_ATTENDEES=1, attendee emails go only to you (SMTP_USER), saying
who they would have gone to. Placeholder addresses (example.com and friends) are never emailed.
"""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import settings
from ..models import Meeting
from .notify import LogNotifier

log = logging.getLogger(__name__)

PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net", ".test", ".invalid", ".example")


def is_real_address(addr: str) -> bool:
    a = addr.strip().lower()
    return "@" in a and not any(a.endswith(d) for d in PLACEHOLDER_DOMAINS)


def email_enabled() -> bool:
    return bool(settings.smtp_user and settings.smtp_app_password)


def _send_sync(to: list[str], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = f"TripOwner <{settings.smtp_user}>"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if settings.smtp_port == 465:                     # implicit TLS
        server = smtplib.SMTP_SSL(settings.smtp_host, 465, context=ctx, timeout=20)
    else:                                             # 587: plain connect, then STARTTLS (465 is often blocked)
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        server.starttls(context=ctx)
    with server as s:
        s.login(settings.smtp_user, settings.smtp_app_password)
        s.send_message(msg)


async def send_email(to: list[str], subject: str, body: str) -> None:
    """Send in a worker thread so the trip loop never blocks on the network."""
    await asyncio.to_thread(_send_sync, to, subject, body)


class EmailNotifier(LogNotifier):
    """Logs like LogNotifier, and actually emails. A failed send is logged and never breaks the trip."""

    def __init__(self, bus, clock, rider_name: str = "", send=send_email):
        super().__init__(bus, clock)
        self.rider_name = rider_name
        self._send = send

    def recipients(self, meeting: Meeting) -> tuple[list[str], bool]:
        """-> (who gets the email, whether it's the real attendees)."""
        real = [a for a in meeting.attendees if is_real_address(a)]
        if settings.email_to_attendees and real:
            return real, True
        return [settings.smtp_user], False

    async def notify_attendees(self, meeting: Meeting, message: str) -> None:
        to, to_attendees = self.recipients(meeting)
        subject = f"Running late: {meeting.title} ({meeting.start:%H:%M})"
        body = message
        if not to_attendees:
            subject = "[TripOwner test] " + subject
            body = (f"{message}\n\n---\nTest mode: this would have gone to {', '.join(meeting.attendees) or 'nobody'}."
                    f"\nSet EMAIL_TO_ATTENDEES=1 to email real attendees.")
        self.sent.append({"at": self.clock.now, "to": to, "message": message})
        try:
            await self._send(to, subject, body)
            self.bus.publish("notify", ts=self.clock.iso(), to=meeting.attendees, message=message,
                             delivered_to=to, test_mode=not to_attendees)
        except Exception as e:
            log.warning("email failed: %s", e)
            self.bus.publish("notify", ts=self.clock.iso(), to=meeting.attendees, message=message,
                             email_error=f"{type(e).__name__}: {str(e)[:120]}")
