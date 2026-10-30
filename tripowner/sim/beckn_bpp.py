"""A mock Namma Yatri: a Beckn ride provider (BPP) for the simulator.

It speaks the protocol to TripOwner's buyer app exactly as a real provider would (ACK, then on_<action>
callbacks; unsolicited on_update when the ride changes; signature check on every request) and drives the
rides with the same chaos simulator as the rest of TripOwner, so evals and demos stay comparable.
Drivers' phone calls aren't Beckn traffic; they're passed straight to the rider side.
"""
import asyncio
import json

import httpx

from ..models import RideStatus
from ..providers.beckn import CITY, DOMAIN, VERSION, verify

BPP_ID, BPP_URI, PROVIDER = "mock.nammayatri.local", "https://mock.nammayatri.local/beckn", "nammayatri"
AUTO_DISCOUNT = 0.7                     # autos cost less than cabs
STATE = {RideStatus.SEARCHING: "RIDE_CONFIRMED", RideStatus.ASSIGNED: "RIDE_ENROUTE_PICKUP",
         RideStatus.ARRIVED: "RIDE_ARRIVED_PICKUP", RideStatus.IN_TRIP: "RIDE_STARTED",
         RideStatus.COMPLETED: "RIDE_ENDED", RideStatus.CANCELLED: "RIDE_CANCELLED",
         RideStatus.NO_DRIVERS: "RIDE_CANCELLED"}
REASON = {"driver_cancelled": "DRIVER_CANCELLED", "rider_unreachable": "RIDER_NOT_FOUND",
          "rider_cancelled": "CANCELLED_BY_RIDER"}
CATEGORY = {"Swift Dzire": "CAB", "Toyota Etios": "CAB", "WagonR": "CAB", "Hyundai Aura": "CAB",
            "Maruti Ertiga": "CAB", "Honda Amaze": "CAB"}


class MockNammaYatri:
    def __init__(self, mock, bap_public_key: str | None = None):
        self.mock = mock                         # MockRideProvider: the rides themselves
        self.bap = None                          # BecknRides, set by whoever wires them together
        self.bap_public_key = bap_public_key
        self.transport = httpx.MockTransport(self._handle)
        self._orders: dict[str, dict] = {}       # ride id -> {"stops": (from, to), "item": ...}
        self._assigned_told: set[str] = set()
        self._prices: dict[str, str] = {}
        self.received: list[dict] = []           # every request, for tests

    # ---- incoming requests from the buyer app ----------------------------------------------------
    async def _handle(self, req: httpx.Request) -> httpx.Response:
        body = req.content
        if self.bap_public_key and not verify(body, req.headers.get("authorization", ""), self.bap_public_key):
            return httpx.Response(401, json={"message": {"ack": {"status": "NACK"}},
                                             "error": {"code": "10001", "message": "invalid signature"}})
        payload = json.loads(body)
        self.received.append(payload)
        ctx, msg = payload["context"], payload["message"]
        handler = getattr(self, "_" + ctx["action"], None)
        if handler is None:
            return httpx.Response(200, json={"message": {"ack": {"status": "NACK"}}})
        reply = await handler(msg)
        out = {"context": {**ctx, "action": "on_" + ctx["action"], "bpp_id": BPP_ID, "bpp_uri": BPP_URI},
               "message": reply}
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.bap.deliver(out)))
        return httpx.Response(200, json={"message": {"ack": {"status": "ACK"}}})

    @staticmethod
    def _place(stop: dict) -> str:
        from ..safety import PLACE_COORDS
        lat, lon = (float(x) for x in stop["location"]["gps"].split(","))
        return min(PLACE_COORDS, key=lambda p: (PLACE_COORDS[p][0] - lat) ** 2 + (PLACE_COORDS[p][1] - lon) ** 2)

    async def _search(self, msg: dict) -> dict:
        stops = msg["intent"]["fulfillment"]["stops"]
        a, b = self._place(stops[0]), self._place(stops[1])
        cab = await self.mock.estimate_fare(a, b)
        self._prices = {"cab": f"{cab:.0f}", "auto": f"{cab * AUTO_DISCOUNT:.0f}"}
        return {"catalog": {"descriptor": {"name": "Namma Yatri (mock)"}, "providers": [{
            "id": PROVIDER, "descriptor": {"name": "Namma Yatri"},
            "fulfillments": [{"id": "f-cab", "type": "DELIVERY", "vehicle": {"category": "CAB"}},
                             {"id": "f-auto", "type": "DELIVERY", "vehicle": {"category": "AUTO_RICKSHAW"}}],
            "items": [{"id": "cab", "descriptor": {"code": "RIDE", "name": "Cab"}, "fulfillment_ids": ["f-cab"],
                       "price": {"currency": "INR", "value": self._prices["cab"]}},
                      {"id": "auto", "descriptor": {"code": "RIDE", "name": "Auto"}, "fulfillment_ids": ["f-auto"],
                       "price": {"currency": "INR", "value": self._prices["auto"]}}]}]}}

    async def _select(self, msg: dict) -> dict:
        order = msg["order"]
        return {"order": {**order, "quote": {"price": {"currency": "INR", "value": self._price(order)}}}}

    def _price(self, order: dict) -> str:
        return self._prices[order["items"][0]["id"]]

    async def _init(self, msg: dict) -> dict:
        return {"order": {**msg["order"], "status": "DRAFT"}}

    async def _confirm(self, msg: dict) -> dict:
        order = msg["order"]
        stops = order["fulfillments"][0]["stops"]
        a, b = self._place(stops[0]), self._place(stops[1])
        ride = await self.mock.request_ride(a, b, stops[0]["location"].get("address", ""))
        ride.fare_estimate = float(self._price(order))
        self._orders[ride.id] = {"order": order}
        return {"order": self._order(ride)}

    async def _cancel(self, msg: dict) -> dict:
        ride_id = msg["order_id"]
        await self.mock.cancel_ride(ride_id)
        self.mock._queue = [e for e in self.mock._queue if e.data.get("ride_id") != ride_id]   # answered here
        return {"order": self._order(self.mock.get(ride_id))}

    async def _status(self, msg: dict) -> dict:
        return {"order": self._order(self.mock.get(msg["order_id"]))}

    # ---- the ride, as a Beckn order --------------------------------------------------------------
    def _order(self, ride, km_left=None) -> dict:
        code = STATE[ride.status]
        if ride.status == RideStatus.ASSIGNED and ride.id not in self._assigned_told:
            self._assigned_told.add(ride.id)
            code = "RIDE_ASSIGNED"
        ful = {"id": "f-cab", "state": {"descriptor": {"code": code}},
               "stops": [{"type": "START", "location": {"gps": "", "address": ride.pickup}},
                         {"type": "END", "location": {"gps": "", "address": ride.dropoff}}]}
        if ride.pickup_eta_min is not None:
            ful["stops"][0]["time"] = {"duration": f"PT{ride.pickup_eta_min}M"}
        if ride.driver:
            ful["agent"] = {"person": {"name": ride.driver.name, "languages": [{"code": ride.driver.language}]},
                            "contact": {"phone": "masked"}}
            ful["vehicle"] = {"category": CATEGORY.get(ride.driver.vehicle, "CAB"), "model": ride.driver.vehicle,
                              "registration": ride.driver.plate}
        progress = []
        if ride.trip_eta_min is not None:
            progress.append({"descriptor": {"code": "TRIP_ETA_MIN"}, "value": str(ride.trip_eta_min)})
        if km_left is not None:
            progress.append({"descriptor": {"code": "KM_LEFT"}, "value": str(km_left)})
        if progress:
            ful["tags"] = [{"descriptor": {"code": "TRIP_PROGRESS"}, "list": progress}]
        order = {"id": ride.id, "provider": {"id": PROVIDER}, "fulfillments": [ful],
                 "quote": {"price": {"currency": "INR", "value": f"{ride.fare_estimate:.0f}"}},
                 "status": {RideStatus.COMPLETED: "COMPLETED", RideStatus.CANCELLED: "CANCELLED",
                            RideStatus.NO_DRIVERS: "CANCELLED"}.get(ride.status, "ACTIVE")}
        if ride.status == RideStatus.NO_DRIVERS:
            order["cancellation"] = {"cancelled_by": "PROVIDER", "reason": {"descriptor": {"code": "NO_DRIVER_ASSIGNED"}}}
        elif ride.status == RideStatus.CANCELLED:
            order["cancellation"] = {"cancelled_by": "CONSUMER" if ride.cancel_reason == "rider_cancelled" else "PROVIDER",
                                     "reason": {"descriptor": {"code": REASON.get(ride.cancel_reason, "OTHER")}}}
        return order

    async def tick(self) -> None:
        """One simulated minute: move the rides on and push what changed to the buyer app."""
        for ev in await self.mock.step():
            if ev.kind == "ride_update":
                ride = self.mock.get(ev.data["ride_id"])
                ctx = {"domain": DOMAIN, "action": "on_update", "version": VERSION, "bpp_id": BPP_ID,
                       "bpp_uri": BPP_URI, "location": {"city": {"code": CITY}, "country": {"code": "IND"}}}
                await self.bap.deliver({"context": ctx, "message": {"order": self._order(ride, ev.data.get("km_left"))}})
            else:
                self.bap.driver_call(ev)
