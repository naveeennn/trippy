"""Calendar providers: MockCalendar for simulations, plus a reader for real calendars in iCal format."""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ..models import Meeting

log = logging.getLogger(__name__)


class CalendarProvider(ABC):
    @abstractmethod
    async def upcoming(self, now: datetime) -> list[Meeting]: ...

    @abstractmethod
    def get(self, meeting_id: str) -> Meeting | None: ...


class MockCalendar(CalendarProvider):
    def __init__(self, meetings: list[Meeting]):
        self._meetings = {m.id: m for m in meetings}

    async def upcoming(self, now: datetime) -> list[Meeting]:
        return sorted(
            (m for m in self._meetings.values() if not m.cancelled and m.start > now),
            key=lambda m: m.start,
        )

    def get(self, meeting_id: str) -> Meeting | None:
        return self._meetings.get(meeting_id)

    def apply_change(self, meeting_id: str, new_start: datetime | None = None, cancelled: bool = False) -> Meeting:
        m = self._meetings[meeting_id]
        if new_start is not None:
            m.start = new_start
        m.cancelled = m.cancelled or cancelled
        return m


# ---- Real calendars via iCal (.ics) -------------------------------------------------------------
# Google Calendar: Settings -> your calendar -> "Secret address in iCal format". Outlook and Apple
# Calendar publish the same format. Read-only; no OAuth app needed. The URL is a secret: keep it in .env.

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class IcsEvent:
    uid: str
    title: str
    start: datetime          # naive, India time
    location: str
    attendees: list[str]


def _unfold(text: str) -> list[str]:
    """RFC 5545: a line starting with a space or tab continues the previous one."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    for esc, plain in ((r"\n", " "), (r"\N", " "), (r"\,", ","), (r"\;", ";"), ("\\\\", "\\")):
        v = v.replace(esc, plain)
    return v


def _parse_dt(params: dict[str, str], value: str) -> datetime | None:
    """-> naive India time, or None for all-day events (nobody needs a cab to an all-day event)."""
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return None
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        dt = dt.replace(tzinfo=timezone.utc)
    elif "TZID" in params and params["TZID"] not in ("Asia/Kolkata", "Asia/Calcutta"):
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"]))
        except Exception:
            log.warning("unknown time zone %s; assuming India time", params["TZID"])
    return dt.astimezone(IST).replace(tzinfo=None) if dt.tzinfo else dt


def parse_ics(text: str) -> list[IcsEvent]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"attendees": []}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("start") and cur.get("status") != "CANCELLED":
                events.append(IcsEvent(cur.get("uid", ""), cur.get("title", "(no title)"), cur["start"],
                                       cur.get("location", ""), cur["attendees"]))
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *rest = head.split(";")
            params = dict(p.split("=", 1) for p in rest if "=" in p)
            if name == "UID":
                cur["uid"] = value
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "STATUS":
                cur["status"] = value.upper()
            elif name == "DTSTART":
                cur["start"] = _parse_dt(params, value)
            elif name == "ATTENDEE" and value.lower().startswith("mailto:"):
                cur["attendees"].append(value[7:])
    return sorted(events, key=lambda e: e.start)


async def load_ics(source: str) -> list[IcsEvent]:
    """`source` is an https iCal URL or a local .ics path."""
    if source.startswith(("http://", "https://", "webcal://")):
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(source.replace("webcal://", "https://", 1))
            r.raise_for_status()
            text = r.text
    else:
        text = Path(source).read_text(encoding="utf-8")
    return parse_ics(text)


def match_place(location: str, places) -> str | None:
    """Map a free-text calendar location onto a place the maps provider knows."""
    low = location.lower()
    hits = [p for p in places if p.lower() in low]
    return max(hits, key=len) if hits else None


def next_trip_event(events: list[IcsEvent], now: datetime, places, origin: str) -> tuple[IcsEvent | None, str, list[str]]:
    """-> (first upcoming event we can route to, its place key, reasons other upcoming events were skipped)."""
    skipped = []
    for e in events:
        if e.start <= now:
            continue
        place = match_place(e.location, places)
        if not place:
            skipped.append(f"'{e.title}': location '{e.location or 'none'}' isn't a place the planner knows")
        elif place == origin:
            skipped.append(f"'{e.title}': it's where you already are ({origin})")
        else:
            return e, place, skipped
    return None, "", skipped
