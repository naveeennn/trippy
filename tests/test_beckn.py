import asyncio
import random
from datetime import datetime

from tripowner.events import SimClock
from tripowner.models import RideStatus
from tripowner.providers.beckn import BecknRides, beckn_enabled, new_keypair, sign, verify
from tripowner.providers.maps import MockMaps
from tripowner.providers.rides import MockRideProvider
from tripowner.sim.beckn_bpp import MockNammaYatri
from tripowner.sim.chaos import Chaos, demo_scenario, random_scenario
from tripowner.sim.world import World
from tripowner.driver import ScriptedDriver
from test_uber import setting


def run(coro):
    return asyncio.run(coro)


def network(script=None, signed=True, vehicle="CAB"):
    """TripOwner's buyer app wired to a mock Namma Yatri that checks every signature."""
    priv, pub = new_keypair()
    clock, maps = SimClock(datetime(2026, 10, 27, 9, 0)), MockMaps()
    chaos = Chaos(no_driver_prob=0, cancel_prob=0, confusion_prob=0, pii_prob=0, stuck_prob=0,
                  script=script or {})
    bpp = MockNammaYatri(MockRideProvider(maps, random.Random(1), chaos, clock, ScriptedDriver()),
                         bap_public_key=pub)
    with setting(beckn_vehicle=vehicle):
        bap = BecknRides(clock, maps, transport=bpp.transport, bpp_sim=bpp, private_key=priv if signed else "")
    bpp.bap = bap
    return bap, bpp, clock


def test_signature_round_trip_and_tamper():
    priv, pub = new_keypair()
    body = b'{"context":{"action":"search"}}'
    header = sign(body, "tripowner.example.com", "k1", priv)
    assert header.startswith('Signature keyId="tripowner.example.com|k1|ed25519"')
    assert verify(body, header, pub)
    assert not verify(body + b" ", header, pub)
    assert not verify(body, header, new_keypair()[1])


def test_books_through_the_full_beckn_flow():
    bap, bpp, _ = network()

    async def go():
        fare = await bap.estimate_fare("Indiranagar", "Koramangala")
        ride = await bap.request_ride("Indiranagar", "Koramangala", "Near the metro gate")
        return fare, ride

    fare, ride = run(go())
    assert [p["context"]["action"] for p in bpp.received] == ["search", "select", "init", "confirm"]
    assert len({p["context"]["transaction_id"] for p in bpp.received}) == 1        # one transaction
    confirm = bpp.received[-1]["message"]["order"]["fulfillments"][0]
    assert confirm["stops"][0]["location"]["address"] == "Near the metro gate"
    assert bpp.received[0]["context"]["domain"] == "ONDC:TRV10"
    assert ride.status == RideStatus.SEARCHING and ride.fare_estimate == fare


def test_unsigned_requests_are_rejected():
    bap, _, _ = network(signed=False)
    try:
        run(bap.estimate_fare("Indiranagar", "Koramangala"))
        assert False, "should have been rejected"
    except RuntimeError as e:
        assert "401" in str(e)


def test_picks_an_auto_when_asked():
    bap, bpp, _ = network(vehicle="AUTO_RICKSHAW")

    fare = run(bap.estimate_fare("Indiranagar", "Koramangala"))
    assert fare == int(bpp._prices["auto"]) < int(bpp._prices["cab"])


def test_ride_lifecycle_arrives_as_agent_events():
    bap, bpp, clock = network(script={1: {"match_delay": 1, "pickup_eta": 2, "language": "kn-IN"}})

    async def go():
        ride = await bap.request_ride("Indiranagar", "Koramangala")
        seen = []
        for _ in range(40):
            clock.advance(1)
            for e in await bap.step():
                if e.kind == "ride_update":
                    seen.append(e.data["status"])
            if seen and seen[-1] == "completed":
                break
        return ride, seen

    ride, seen = run(go())
    assert seen[0] == "assigned" and "arrived" in seen and "in_trip" in seen and seen[-1] == "completed"
    assert ride.driver and ride.driver.language == "kn-IN" and ride.driver.plate.startswith("KA")


def test_no_drivers_and_driver_cancel_map_to_what_the_agent_knows():
    bap, _, clock = network(script={1: {"no_drivers": True, "match_delay": 1},
                                    2: {"match_delay": 1, "pickup_eta": 5, "cancel_at_eta": 4}})

    async def statuses(n):
        out = []
        for _ in range(n):
            clock.advance(1)
            out += [(e.data["status"], e.data["reason"]) for e in await bap.step() if e.kind == "ride_update"]
        return out

    async def go():
        await bap.request_ride("Indiranagar", "Koramangala")
        first = await statuses(2)
        await bap.request_ride("Indiranagar", "Koramangala")
        return first, await statuses(4)

    first, second = run(go())
    assert [s for s, _ in first] == ["no_drivers"]
    assert ("cancelled", "driver_cancelled") in second
    assert bap.driver_cancels == 1


def test_rider_cancel_goes_over_beckn():
    bap, bpp, _ = network()

    async def go():
        ride = await bap.request_ride("Indiranagar", "Koramangala")
        await bap.cancel_ride(ride.id)
        return ride

    ride = run(go())
    assert bpp.received[-1]["context"]["action"] == "cancel"
    assert ride.status == RideStatus.CANCELLED and ride.cancel_reason == "rider_cancelled"


def test_trips_come_out_the_same_as_the_app_simulator():
    for sc in (demo_scenario(), random_scenario(3), random_scenario(11)):
        a = run(World(sc, "agent").run()).as_dict()
        b = run(World(sc, "agent", ride_backend="beckn").run()).as_dict()
        assert a == b


def test_real_network_only_when_registered_and_never_in_the_demo():
    keys = dict(beckn_subscriber_id="tripowner.example.com", beckn_private_key=new_keypair()[0],
                beckn_gateway_uri="https://gateway.example/beckn", ride_provider="beckn")
    with setting(**keys, public_demo=True):
        assert not beckn_enabled()
    with setting(**keys, public_demo=False):
        assert beckn_enabled()
        assert type(World(demo_scenario(), "agent").rides).__name__ == "MockRideProvider"   # demos stay simulated
