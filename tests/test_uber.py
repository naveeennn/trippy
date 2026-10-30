import asyncio
import json
from contextlib import contextmanager
from datetime import datetime

import httpx

from tripowner.config import settings
from tripowner.events import SimClock
from tripowner.models import RideStatus
from tripowner.providers.maps import MockMaps
from tripowner.providers.uber import UberGuestRides
from tripowner.sim.chaos import demo_scenario
from tripowner.sim.world import World


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


class FakeUber:
    """Just enough of Uber's Guest Rides API: token, estimates, create, status (scripted), cancel."""

    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else None
        self.calls.append((req.method, req.url.path, dict(req.headers), body))
        if req.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 2592000, "token_type": "Bearer"})
        if req.url.path == "/v1/guests/trips/estimates":
            return httpx.Response(200, json={"product_estimates": [
                {"product": {"product_id": "p-premier", "display_name": "Premier"},
                 "estimate_info": {"fare_id": "f1", "fare": {"value": 420.0, "currency_code": "INR"}}},
                {"product": {"product_id": "p-go", "display_name": "Uber Go"},
                 "estimate_info": {"fare_id": "f2", "fare": {"value": 260.0, "currency_code": "INR"}}}]})
        if req.method == "POST" and req.url.path == "/v1/guests/trips":
            return httpx.Response(200, json={"request_id": "trip-1", "status": "processing"})
        if req.method == "GET":
            return httpx.Response(200, json=self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)


def make(statuses, **kw):
    fake = FakeUber(statuses)
    clock = SimClock(datetime(2026, 10, 27, 9, 0))
    return UberGuestRides(clock, MockMaps(), transport=httpx.MockTransport(fake)), fake, clock


def run(c):
    return asyncio.run(c)


SANDBOX = dict(uber_client_id="id", uber_client_secret="secret", uber_sandbox_run_id="run-123",
               uber_org_uuid="org-9", uber_guest_phone="+919800000000", uber_product="", user_name="Jishnu Kumar",
               uber_api_base="https://api.uber.com", uber_auth_url="https://auth.uber.com/oauth/v2/token")


def test_books_the_cheapest_ride_with_the_right_request():
    with setting(**SANDBOX):
        u, fake, _ = make([{"status": "processing"}])
        assert run(u.estimate_fare("Indiranagar", "Koramangala")) == 260          # cheapest: Uber Go
        ride = run(u.request_ride("Indiranagar", "Koramangala", pickup_note="Gate 2, Indiranagar Metro Station"))
    assert ride.id == "trip-1" and ride.status == RideStatus.SEARCHING
    method, path, headers, body = [c for c in fake.calls if c[1] == "/v1/guests/trips"][0]
    assert body["product_id"] == "p-go" and body["fare_id"] == "f2"
    assert body["guest"] == {"first_name": "Jishnu", "last_name": "Kumar", "phone_number": "+919800000000"}
    assert "Gate 2" in body["note_for_driver"] and body["call_enabled"] is True
    assert headers["x-uber-sandbox-runuuid"] == "run-123" and headers["x-uber-organizationuuid"] == "org-9"
    assert headers["authorization"] == "Bearer tok"


def test_preferred_product_by_name():
    with setting(**{**SANDBOX, "uber_product": "premier"}):
        u, _, _ = make([{"status": "processing"}])
        assert run(u.estimate_fare("Indiranagar", "Koramangala")) == 420


def test_status_changes_become_agent_events():
    accepted = {"status": "accepted", "pickup": {"eta": 6},
                "driver": {"name": "Ravi", "rating": 4.9}, "vehicle": {"make": "Maruti", "model": "Dzire", "license_plate": "KA01AB1234"}}
    with setting(**SANDBOX):
        u, _, clock = make([{"status": "processing"}, accepted, {"status": "driver_canceled"}])
        run(u.request_ride("Indiranagar", "Koramangala"))
        assert run(u.step()) == []                                   # still processing: nothing new to report
        ev = run(u.step())[0]
        assert ev.data["status"] == "assigned" and ev.data["pickup_eta"] == 6
        assert ev.data["driver"]["plate"] == "KA01AB1234" and ev.data["driver"]["vehicle"] == "Maruti Dzire"
        ev = run(u.step())[0]
        assert ev.data["status"] == "cancelled" and ev.data["reason"] == "driver_cancelled" and u.driver_cancels == 1


def test_cancel_calls_delete():
    with setting(**SANDBOX):
        u, fake, _ = make([{"status": "processing"}])
        run(u.request_ride("Indiranagar", "Koramangala"))
        run(u.cancel_ride("trip-1"))
    assert ("DELETE", "/v1/guests/trips/trip-1") in [(c[0], c[1]) for c in fake.calls]


def test_auto_book_skips_the_question_within_the_spend_cap():
    w = World(demo_scenario(), "agent")
    w.tb.auto_book = True
    r = run(w.run())
    assert r.approvals_asked == 0 and r.arrived                    # booked and rebooked without asking


def test_never_real_rides_in_public_demo_or_simulations():
    from tripowner.providers.uber import uber_enabled
    with setting(**SANDBOX, ride_provider="uber", public_demo=True):
        assert not uber_enabled()
    with setting(**SANDBOX, ride_provider="uber", public_demo=False):
        assert uber_enabled()
        assert type(World(demo_scenario(), "agent").rides).__name__ == "MockRideProvider"   # demos stay simulated
