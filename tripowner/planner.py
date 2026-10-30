"""When to leave, when to book."""
from dataclasses import dataclass
from datetime import datetime, timedelta

# Expected minutes from tapping "book" to sitting in the car: match + driver pickup + walk.
PICKUP_ALLOWANCE_MIN = 10
ARRIVAL_BUFFER_MIN = 5


@dataclass
class Plan:
    travel_min: int
    leave_by: datetime
    book_at: datetime

    def as_dict(self) -> dict:
        return {
            "travel_min": self.travel_min,
            "leave_by": self.leave_by.strftime("%H:%M"),
            "book_at": self.book_at.strftime("%H:%M"),
        }


def make_plan(
    meeting_start: datetime,
    travel_min: int,
    buffer_min: int = ARRIVAL_BUFFER_MIN,
    pickup_allowance_min: int = PICKUP_ALLOWANCE_MIN,
) -> Plan:
    leave_by = meeting_start - timedelta(minutes=travel_min + buffer_min)
    book_at = leave_by - timedelta(minutes=pickup_allowance_min)
    return Plan(travel_min=travel_min, leave_by=leave_by, book_at=book_at)
