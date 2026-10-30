import asyncio
from contextlib import contextmanager
from datetime import datetime

from tripowner.config import settings
from tripowner.events import EventBus, SimClock
from tripowner.models import Meeting
from tripowner.providers.email import EmailNotifier, is_real_address


@contextmanager
def setting(**kw):
    old = {k: getattr(settings, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            object.__setattr__(settings, k, v)


MEETING = Meeting("m", "Client demo", datetime(2026, 10, 24, 10, 30), "Koramangala",
                  ["priya@example.com", "ravi@realco.in"])


def notifier(sent, fail=False):
    async def send(to, subject, body):
        if fail:
            raise OSError("network down")
        sent.append((to, subject, body))
    bus = EventBus()
    return EmailNotifier(bus, SimClock(datetime(2026, 10, 24, 10, 0)), "Naveen", send=send), bus


def test_test_mode_emails_only_you():
    sent = []
    with setting(smtp_user="me@gmail.com", email_to_attendees=False):
        n, bus = notifier(sent)
        asyncio.run(n.notify_attendees(MEETING, "Running 5 min late"))
    to, subject, body = sent[0]
    assert to == ["me@gmail.com"] and subject.startswith("[TripOwner test]")
    assert "ravi@realco.in" in body                              # says who it would have gone to
    assert bus.history[-1]["test_mode"] is True


def test_attendee_mode_skips_placeholder_addresses():
    sent = []
    with setting(smtp_user="me@gmail.com", email_to_attendees=True):
        n, _ = notifier(sent)
        asyncio.run(n.notify_attendees(MEETING, "Running 5 min late"))
    assert sent[0][0] == ["ravi@realco.in"]


def test_failed_send_never_breaks_the_trip():
    with setting(smtp_user="me@gmail.com", email_to_attendees=False):
        n, bus = notifier([], fail=True)
        asyncio.run(n.notify_attendees(MEETING, "late"))
    assert "network down" in bus.history[-1]["email_error"]


def test_placeholder_detection():
    assert not is_real_address("priya@example.com") and not is_real_address("x@demo.test")
    assert is_real_address("someone@gmail.com")
