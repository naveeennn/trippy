"""Core domain types shared by every layer."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RideStatus(str, Enum):
    SEARCHING = "searching"
    ASSIGNED = "assigned"
    ARRIVED = "arrived"
    IN_TRIP = "in_trip"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_DRIVERS = "no_drivers"


ACTIVE_STATUSES = {RideStatus.SEARCHING, RideStatus.ASSIGNED, RideStatus.ARRIVED, RideStatus.IN_TRIP}
PRE_PICKUP_STATUSES = {RideStatus.SEARCHING, RideStatus.ASSIGNED}


@dataclass
class UserProfile:
    name: str
    phone: str
    email: str
    home_address: str
    location: str           # place key the user is at
    pickup_landmark: str    # what we tell drivers -- never the home address
    languages: tuple[str, ...] = ("en-IN", "hi-IN")


@dataclass
class Meeting:
    id: str
    title: str
    start: datetime
    location: str
    attendees: list[str]
    cancelled: bool = False


@dataclass
class Driver:
    name: str
    language: str
    vehicle: str
    plate: str


@dataclass
class Ride:
    id: str
    pickup: str
    dropoff: str
    fare_estimate: float
    requested_at: datetime
    status: RideStatus = RideStatus.SEARCHING
    driver: Driver | None = None
    pickup_eta_min: int | None = None
    trip_eta_min: int | None = None
    cancel_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass
class Event:
    kind: str               # tick | ride_update | driver_message | meeting_changed
    at: datetime
    data: dict = field(default_factory=dict)
