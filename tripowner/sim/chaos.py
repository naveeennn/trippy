"""Scenarios: who's going where, when, and what will go wrong."""
import random
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import Meeting, UserProfile
from ..planner import ARRIVAL_BUFFER_MIN, PICKUP_ALLOWANCE_MIN
from ..providers.maps import PLACES, MockMaps, TrafficSpike

DAY = datetime(2026, 10, 15)
TITLES = ["Design review", "Client demo", "Investor prep", "Interview panel", "Quarterly sync"]


@dataclass
class Chaos:
    cancel_prob: float = 0.20        # driver cancels before pickup
    no_driver_prob: float = 0.08     # nobody accepts
    confusion_prob: float = 0.45     # driver can't find the pickup and asks
    pii_prob: float = 0.30           # driver asks for the rider's phone number
    stuck_prob: float = 0.15         # driver stops moving towards pickup
    spike_prob: float = 0.35         # traffic jam around departure time
    move_prob: float = 0.15          # meeting rescheduled
    cancel_meeting_prob: float = 0.05
    driver_languages: tuple = (("kn-IN", 0.5), ("hi-IN", 0.3), ("en-IN", 0.2))
    # Per-ride overrides by booking number (1-based), for scripted demos.
    script: dict[int, dict] = field(default_factory=dict)


@dataclass
class MeetingChange:
    at: datetime
    new_start: datetime | None = None
    cancelled: bool = False


@dataclass
class Scenario:
    seed: int
    profile: UserProfile
    meeting: Meeting
    sim_start: datetime
    chaos: Chaos
    spikes: list[TrafficSpike] = field(default_factory=list)
    change: MeetingChange | None = None

    def summary(self) -> dict:
        return {
            "seed": self.seed,
            "from": self.profile.location,
            "to": self.meeting.location,
            "meeting": self.meeting.title,
            "start": self.meeting.start.strftime("%H:%M"),
            "sim_start": self.sim_start.strftime("%H:%M"),
        }


def make_profile(location: str, name: str = "Asha") -> UserProfile:
    return UserProfile(
        name=name,
        phone="+91 98450 12345",
        email="asha.demo@example.com",
        home_address=f"Flat 7B, 12th Cross, {location}",
        location=location,
        pickup_landmark=f"Gate 2, {location} Metro Station",
    )


def random_scenario(seed: int, chaos: Chaos | None = None) -> Scenario:
    chaos = chaos or Chaos()
    rng = random.Random(seed)
    origin, dest = rng.sample(sorted(PLACES), 2)
    start = DAY.replace(hour=9) + timedelta(minutes=15 * rng.randint(0, 40))
    travel = MockMaps().predict_minutes(origin, dest, start)
    lead = travel + ARRIVAL_BUFFER_MIN + PICKUP_ALLOWANCE_MIN
    sim_start = start - timedelta(minutes=lead + 30)

    spikes = []
    if rng.random() < chaos.spike_prob:
        s = start - timedelta(minutes=travel + rng.randint(-5, 30))
        spikes.append(TrafficSpike(s, s + timedelta(minutes=rng.randint(20, 50)), rng.uniform(1.3, 1.9)))

    change = None
    r = rng.random()
    if r < chaos.cancel_meeting_prob:
        change = MeetingChange(at=sim_start + timedelta(minutes=rng.randint(10, lead + 20)), cancelled=True)
    elif r < chaos.cancel_meeting_prob + chaos.move_prob:
        shift = rng.choice([-15, 30, 45])
        change = MeetingChange(at=sim_start + timedelta(minutes=rng.randint(5, 40)),
                               new_start=start + timedelta(minutes=shift))

    meeting = Meeting(id=f"M{seed}", title=rng.choice(TITLES), start=start, location=dest,
                      attendees=["priya@example.com", "rahul@example.com"])
    return Scenario(seed, make_profile(origin), meeting, sim_start, chaos, spikes, change)


def demo_scenario(call_lang: str = "kn-IN") -> Scenario:
    """The stage demo: first driver (Hindi) cancels, second driver (Kannada) can't find
    the pickup and asks for the phone number, then traffic makes the trip run late."""
    origin, dest = "Indiranagar", "Koramangala"
    start = DAY.replace(hour=10, minute=30)
    travel = MockMaps().predict_minutes(origin, dest, start)
    book_at = start - timedelta(minutes=travel + ARRIVAL_BUFFER_MIN + PICKUP_ALLOWANCE_MIN)
    chaos = Chaos(script={
        1: {"language": "hi-IN", "driver_idx": 1, "match_delay": 1, "pickup_eta": 6, "cancel_at_eta": 4,
            "no_drivers": False, "ask_location": False, "ask_phone": False, "stuck_at_eta": None},
        2: {"language": call_lang, "driver_idx": 0, "match_delay": 1, "pickup_eta": 5, "cancel_at_eta": None,
            "no_drivers": False, "ask_location": True, "ask_phone": True, "stuck_at_eta": None,
            "calls": True},
    })
    spike = TrafficSpike(book_at + timedelta(minutes=8), start + timedelta(minutes=40), 1.35)
    meeting = Meeting(id="DEMO", title="Client demo", start=start, location=dest,
                      attendees=["priya@example.com", "rahul@example.com"])
    return Scenario(0, make_profile(origin), meeting, book_at - timedelta(minutes=3), chaos, [spike])


def calendar_scenario(event, place: str, origin: str, chaos: Chaos | None = None,
                      start_at: datetime | None = None) -> Scenario:
    """A trip to a real calendar event. The meeting, time, place and attendees are real; the ride,
    drivers and traffic are still simulated (the chaos is seeded from the event, so re-runs match)."""
    chaos = chaos or Chaos()
    seed = zlib.crc32(event.uid.encode() or event.title.encode())
    rng = random.Random(seed)
    travel = MockMaps().predict_minutes(origin, place, event.start)
    lead = travel + ARRIVAL_BUFFER_MIN + PICKUP_ALLOWANCE_MIN
    spikes = []
    if rng.random() < chaos.spike_prob:
        s = event.start - timedelta(minutes=travel + rng.randint(-5, 30))
        spikes.append(TrafficSpike(s, s + timedelta(minutes=rng.randint(20, 50)), rng.uniform(1.3, 1.9)))
    meeting = Meeting(id=event.uid or f"CAL{seed}", title=event.title, start=event.start, location=place,
                      attendees=event.attendees or ["(no attendees on the invite)"])
    sim_start = start_at.replace(second=0, microsecond=0) if start_at else event.start - timedelta(minutes=lead + 30)
    from ..config import settings      # real calendar trips carry the real user's name into emails
    return Scenario(seed, make_profile(origin, settings.user_name), meeting, sim_start, chaos, spikes)


def cabs_fail_scenario() -> Scenario:
    """The mode-switch demo: Indiranagar -> Whitefield (both on the metro's Purple line). The first driver
    cancels, the next search finds nobody, and traffic builds. The agent should offer the metro."""
    origin, dest = "Indiranagar", "Whitefield"
    start = DAY.replace(hour=10, minute=30)
    travel = MockMaps().predict_minutes(origin, dest, start)
    book_at = start - timedelta(minutes=travel + ARRIVAL_BUFFER_MIN + PICKUP_ALLOWANCE_MIN)
    chaos = Chaos(script={
        1: {"language": "hi-IN", "driver_idx": 1, "match_delay": 1, "pickup_eta": 7, "cancel_at_eta": 5,
            "no_drivers": False, "ask_location": True, "ask_phone": False, "stuck_at_eta": None},
        2: {"match_delay": 2, "no_drivers": True},
        3: {"language": "kn-IN", "driver_idx": 0, "match_delay": 2, "pickup_eta": 8, "cancel_at_eta": None,
            "no_drivers": False, "ask_location": True, "ask_phone": True, "stuck_at_eta": None},
    })
    spike = TrafficSpike(book_at + timedelta(minutes=4), start + timedelta(minutes=30), 1.4)
    meeting = Meeting(id="CABS", title="Partner meeting", start=start, location=dest,
                      attendees=["priya@example.com", "rahul@example.com"])
    return Scenario(1, make_profile(origin), meeting, book_at - timedelta(minutes=3), chaos, [spike])


def night_stop_scenario() -> Scenario:
    """Safety demo: a late-night ride, Indiranagar -> Whitefield. Mid-trip the cab stops and doesn't
    move. The agent checks on the rider; no answer (or "I need help") alerts emergency contacts."""
    origin, dest = "Indiranagar", "Whitefield"
    start = DAY.replace(hour=23, minute=30)
    travel = MockMaps().predict_minutes(origin, dest, start)
    book_at = start - timedelta(minutes=travel + ARRIVAL_BUFFER_MIN + PICKUP_ALLOWANCE_MIN)
    chaos = Chaos(script={
        1: {"language": "kn-IN", "driver_idx": 4, "match_delay": 1, "pickup_eta": 4, "cancel_at_eta": None,
            "no_drivers": False, "ask_location": False, "ask_phone": False, "stuck_at_eta": None,
            "trip_stop_after_min": 8, "trip_stop_minutes": 12},
    })
    meeting = Meeting(id="NIGHT", title="Late-night ride home", start=start, location=dest,
                      attendees=["family@example.com"])
    return Scenario(2, make_profile(origin), meeting, book_at - timedelta(minutes=3), chaos, [])
