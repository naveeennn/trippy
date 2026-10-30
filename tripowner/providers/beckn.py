"""Namma Yatri / ONDC rides over the Beckn protocol: TripOwner as a buyer app (BAP).

Beckn is the open protocol behind ONDC; Namma Yatri (Bengaluru autos and cabs, zero commission) is a ride
provider (BPP) on it. Every call is asynchronous: we POST an action, the provider replies ACK, and the real
answer arrives later as a callback (on_<action>) on our own endpoint (/beckn/on_<action> in server.py).

  estimate_fare -> search  / on_search    ride options and prices (cab or auto)
  request_ride  -> select  / on_select    -> init / on_init -> confirm / on_confirm   (order id = ride id)
  step          -> on_update / on_status / on_cancel pushed by the provider: driver assigned, arrived,
                   ride started, ended, cancelled (with a reason), pickup ETA
  cancel_ride   -> cancel  / on_cancel

Messages follow ONDC's ride-hailing domain (TRV10, Beckn 2.0) and are signed the Beckn way (Ed25519 over a
BLAKE2b-512 digest of the body). Field-level conformance isn't certified: going live needs registration on
ONDC as a buyer app (domain, keys, staging, a demo to ONDC). Until then the simulator runs this exact client
against a mock Namma Yatri (sim/beckn_bpp.py).
"""
import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from ..config import settings
from ..models import Driver, Event, Ride, RideStatus
from ..safety import PLACE_COORDS
from .rides import RideProvider

log = logging.getLogger(__name__)

DOMAIN, VERSION, CITY = "ONDC:TRV10", "2.0.1", "std:080"      # ride hailing, Bengaluru
REPLY_TIMEOUT_S = 15
QUOTE_FRESH_MIN = 5            # reuse a search this long for fare checks; book on a fresh one

# Fulfillment states (TRV10) -> our ride status
FULFILLMENT = {
    "RIDE_CONFIRMED": RideStatus.SEARCHING, "RIDE_ASSIGNED": RideStatus.ASSIGNED,
    "RIDE_ENROUTE_PICKUP": RideStatus.ASSIGNED, "RIDE_ARRIVED_PICKUP": RideStatus.ARRIVED,
    "RIDE_STARTED": RideStatus.IN_TRIP, "RIDE_ENDED": RideStatus.COMPLETED, "RIDE_CANCELLED": RideStatus.CANCELLED,
}
# Cancellation reason codes from the provider -> the reasons the agent already understands
CANCEL_REASON = {"DRIVER_CANCELLED": "driver_cancelled", "RIDER_NOT_FOUND": "rider_unreachable",
                 "CANCELLED_BY_RIDER": "rider_cancelled"}


def beckn_enabled() -> bool:
    return (settings.ride_provider == "beckn" and bool(settings.beckn_subscriber_id and settings.beckn_private_key
            and settings.beckn_gateway_uri) and not settings.public_demo)


# ---- signing ------------------------------------------------------------------------------------
def body_digest(body: bytes) -> str:
    return base64.b64encode(hashlib.blake2b(body, digest_size=64).digest()).decode()


def signing_string(created: int, expires: int, digest: str) -> str:
    return f"(created): {created}\n(expires): {expires}\ndigest: BLAKE-512={digest}"


def sign(body: bytes, subscriber_id: str, key_id: str, private_key_b64: str, now: int | None = None) -> str:
    """Beckn Authorization header for a request body."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.b64decode(private_key_b64)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])          # accepts 32-byte seeds or 64-byte keys
    created = now or int(time.time())
    expires = created + 300
    sig = base64.b64encode(key.sign(signing_string(created, expires, body_digest(body)).encode())).decode()
    return (f'Signature keyId="{subscriber_id}|{key_id}|ed25519",algorithm="ed25519",created="{created}",'
            f'expires="{expires}",headers="(created) (expires) digest",signature="{sig}"')


def verify(body: bytes, header: str, public_key_b64: str) -> bool:
    """Check a Beckn Authorization header against the sender's public key (from the ONDC registry)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        parts = dict(p.split("=", 1) for p in header.removeprefix("Signature ").split(","))
        parts = {k: v.strip('"') for k, v in parts.items()}
        created, expires = int(parts["created"]), int(parts["expires"])
        if not created - 60 <= time.time() <= expires:
            return False
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(
            base64.b64decode(parts["signature"]), signing_string(created, expires, body_digest(body)).encode())
        return True
    except (InvalidSignature, KeyError, ValueError):
        return False


def new_keypair() -> tuple[str, str]:
    """(private, public) base64 Ed25519 keys, for registering on ONDC or for the mock network."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def gps(place: str) -> str:
    lat, lon = PLACE_COORDS[place]
    return f"{lat:.6f}, {lon:.6f}"


def _lang(langs) -> str:
    """Beckn Person.languages: [{"code": "kn-IN", ...}] (plain strings accepted too)."""
    if not langs:
        return "en-IN"
    first = langs[0]
    return first.get("code", "en-IN") if isinstance(first, dict) else str(first)


def iso_minutes(text: str | None) -> int | None:
    """'PT7M' -> 7"""
    if not text or not text.startswith("PT") or not text.endswith("M"):
        return None
    try:
        return int(text[2:-1])
    except ValueError:
        return None


# ---- the buyer app ------------------------------------------------------------------------------
class BecknRides(RideProvider):
    def __init__(self, clock, maps, transport: httpx.AsyncBaseTransport | None = None, bpp_sim=None,
                 publish=None, subscriber_id: str | None = None, private_key: str | None = None,
                 gateway_uri: str | None = None):
        self.clock, self.maps = clock, maps
        self.bap_id = subscriber_id or settings.beckn_subscriber_id or "tripowner.local"
        self.bap_uri = settings.beckn_bap_uri or f"https://{self.bap_id}/beckn"
        self.key_id = settings.beckn_key_id or "k1"
        self.private_key = private_key or settings.beckn_private_key
        self.gateway_uri = gateway_uri or settings.beckn_gateway_uri or "https://mock.nammayatri.local/beckn"
        self.vehicle = settings.beckn_vehicle.upper()                  # CAB | AUTO_RICKSHAW
        self._http = httpx.AsyncClient(timeout=20, transport=transport)
        self.bpp_sim = bpp_sim                                         # the mock Namma Yatri, in simulations
        self.publish = publish or (lambda **kw: None)
        self._waiting: dict[tuple[str, str], asyncio.Future] = {}      # (message_id, on_action) -> reply
        self._pushed: list[dict] = []                                  # unsolicited on_update/on_status/on_cancel
        self._side: list[Event] = []                                   # driver phone calls (outside Beckn)
        self._quote: dict[tuple[str, str], dict] = {}                  # (from, to) -> chosen offer
        self._rides: dict[str, Ride] = {}
        self._bpp: dict[str, tuple[str, str]] = {}                     # ride id -> (bpp_id, bpp_uri)
        self._txn: dict[str, str] = {}                                 # ride id -> transaction id
        self.messages: list[dict] = []                                 # protocol log (dashboard + tests)
        self.outbox: list[dict] = []
        self._driver_cancels = 0

    # Simulation hooks the toolbox/world read and set on any ride provider
    @property
    def driver_cancels(self) -> int:
        return self.bpp_sim.mock.driver_cancels if self.bpp_sim else self._driver_cancels

    @property
    def route_km(self):
        return self.bpp_sim.mock.route_km if self.bpp_sim else None

    @route_km.setter
    def route_km(self, km):
        if self.bpp_sim:
            self.bpp_sim.mock.route_km = km

    # ---- transport ------------------------------------------------------------------------------
    def _context(self, action: str, txn: str, bpp: tuple[str, str] | None = None) -> dict:
        ctx = {"domain": DOMAIN, "action": action, "version": VERSION, "bap_id": self.bap_id, "bap_uri": self.bap_uri,
               "transaction_id": txn, "message_id": str(uuid.uuid4()),
               "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "ttl": "PT30S", "location": {"city": {"code": CITY}, "country": {"code": "IND"}}}
        if bpp:
            ctx["bpp_id"], ctx["bpp_uri"] = bpp
        return ctx

    def _note(self, direction: str, action: str, summary: str) -> None:
        self.messages.append({"dir": direction, "action": action, "summary": summary})
        self.publish(dir=direction, action=action, summary=summary)

    async def _call(self, action: str, txn: str, message: dict, bpp: tuple[str, str] | None = None,
                    summary: str = "") -> dict:
        """POST an action, check the ACK, then wait for the matching on_<action> callback."""
        ctx = self._context(action, txn, bpp)
        body = json.dumps({"context": ctx, "message": message}, separators=(",", ":")).encode()
        headers = {"content-type": "application/json"}
        if self.private_key:
            headers["authorization"] = sign(body, self.bap_id, self.key_id, self.private_key)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[(ctx["message_id"], f"on_{action}")] = fut
        url = (bpp[1] if bpp else self.gateway_uri).rstrip("/") + f"/{action}"
        self._note("→", action, summary)
        try:
            r = await self._http.post(url, content=body, headers=headers)
            ack = r.json().get("message", {}).get("ack", {}).get("status")
            if r.status_code >= 400 or ack != "ACK":
                raise RuntimeError(f"{action} not accepted: {r.status_code} {r.text[:200]}")
            return await asyncio.wait_for(fut, REPLY_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RuntimeError(f"no on_{action} from the ride provider within {REPLY_TIMEOUT_S}s") from None
        finally:
            self._waiting.pop((ctx["message_id"], f"on_{action}"), None)

    async def deliver(self, payload: dict) -> dict:
        """A callback from the network (server endpoint or mock). Returns the ACK to send back."""
        ctx, action = payload.get("context", {}), payload.get("context", {}).get("action", "")
        fut = self._waiting.get((ctx.get("message_id"), action))
        if fut and not fut.done():
            fut.set_result(payload)
        elif action in ("on_update", "on_status", "on_cancel", "on_confirm"):
            self._pushed.append(payload)                 # the provider telling us something changed
        else:
            return {"message": {"ack": {"status": "NACK"}}, "error": {"code": "20002", "message": "unknown message"}}
        return {"message": {"ack": {"status": "ACK"}}}

    def driver_call(self, ev: Event) -> None:
        """Drivers on Namma Yatri phone the rider's masked number: that conversation isn't Beckn traffic."""
        self._side.append(ev)

    # ---- RideProvider ---------------------------------------------------------------------------
    async def estimate_fare(self, pickup: str, dropoff: str) -> float:
        q = self._quote.get((pickup, dropoff))
        if q and (self.clock.now - q["at"]).total_seconds() < QUOTE_FRESH_MIN * 60:
            return round(q["price"])                  # the agent re-checks fares every minute while planning
        return await self._search(pickup, dropoff)

    async def _search(self, pickup: str, dropoff: str) -> float:
        txn = str(uuid.uuid4())
        reply = await self._call("search", txn, {"intent": {"fulfillment": {
            "stops": [{"type": "START", "location": {"gps": gps(pickup)}},
                      {"type": "END", "location": {"gps": gps(dropoff)}}]}}},
            summary=f"{pickup} → {dropoff}")
        offers = []
        bpp = (reply["context"].get("bpp_id"), reply["context"].get("bpp_uri"))
        for prov in reply.get("message", {}).get("catalog", {}).get("providers", []):
            fulfillments = {f["id"]: f for f in prov.get("fulfillments", [])}
            for item in prov.get("items", []):
                ful = fulfillments.get((item.get("fulfillment_ids") or [None])[0], {})
                offers.append({"provider": prov["id"], "item": item["id"], "fulfillment": ful.get("id"),
                               "vehicle": (ful.get("vehicle") or {}).get("category", ""),
                               "price": float(item["price"]["value"]), "bpp": bpp, "txn": txn,
                               "at": self.clock.now, "used": False})
        if not offers:
            raise RuntimeError("no rides offered right now")
        same = [o for o in offers if o["vehicle"] == self.vehicle] or offers
        chosen = min(same, key=lambda o: o["price"])
        self._quote[(pickup, dropoff)] = chosen
        self._note("←", "on_search", f"{len(offers)} offers; chose {chosen['vehicle'].lower()} ₹{chosen['price']:.0f}")
        return round(chosen["price"])

    async def request_ride(self, pickup: str, dropoff: str, pickup_note: str = "") -> Ride:
        q = self._quote.get((pickup, dropoff))
        if not q or q["used"] or (self.clock.now - q["at"]).total_seconds() >= QUOTE_FRESH_MIN * 60:
            await self._search(pickup, dropoff)                     # each booking is its own transaction
            q = self._quote[(pickup, dropoff)]
        q["used"] = True
        order = {"provider": {"id": q["provider"]}, "items": [{"id": q["item"]}]}
        sel = await self._call("select", q["txn"], {"order": order}, q["bpp"], summary="select the offer")
        price = float(sel["message"]["order"].get("quote", {}).get("price", {}).get("value", q["price"]))
        self._note("←", "on_select", f"quote ₹{price:.0f}")
        first, *rest = (settings.user_name or "Rider").split()
        order = {**order, "fulfillments": [{"id": q["fulfillment"], "customer": {
                     "person": {"name": " ".join([first, *rest])},
                     "contact": {"phone": settings.beckn_rider_phone or "0000000000"}},
                     "stops": [{"type": "START", "location": {"gps": gps(pickup), "address": pickup_note or pickup}},
                               {"type": "END", "location": {"gps": gps(dropoff), "address": dropoff}}]}],
                 "payments": [{"collected_by": "BPP", "status": "NOT-PAID", "type": "ON-FULFILLMENT"}]}
        await self._call("init", q["txn"], {"order": order}, q["bpp"], summary="init: rider, stops, pay the driver")
        self._note("←", "on_init", "order drafted")
        conf = await self._call("confirm", q["txn"], {"order": order}, q["bpp"], summary="confirm")
        o = conf["message"]["order"]
        ride = Ride(id=o["id"], pickup=pickup, dropoff=dropoff, fare_estimate=price, requested_at=self.clock.now)
        self._apply(ride, o)
        self._rides[ride.id], self._bpp[ride.id] = ride, q["bpp"]
        self._txn[ride.id] = q["txn"]
        self._note("←", "on_confirm", f"order {ride.id}: {ride.status.value}")
        return ride

    async def cancel_ride(self, ride_id: str) -> None:
        ride = self._rides.get(ride_id)
        if not ride or not ride.active:
            return
        reply = await self._call("cancel", self._txn[ride_id], {"order_id": ride_id, "cancellation_reason_id": "000"},
                                 self._bpp[ride_id], summary=f"cancel {ride_id}")
        self._apply(ride, reply["message"]["order"])
        ride.cancel_reason = "rider_cancelled"
        self._note("←", "on_cancel", f"order {ride_id} cancelled")

    async def send_message(self, ride_id: str, text: str, lang: str, text_en: str | None = None) -> None:
        # Beckn has no chat; in the simulator this stands for the phone call with the driver.
        delivered = False
        if self.bpp_sim:
            await self.bpp_sim.mock.send_message(ride_id, text, lang, text_en)
            delivered = True
        self.outbox.append({"ride_id": ride_id, "text": text, "lang": lang, "text_en": text_en or text,
                            "delivered": delivered})

    def get(self, ride_id: str) -> Ride | None:
        return self._rides.get(ride_id)

    def _apply(self, ride: Ride, order: dict) -> dict:
        ful = (order.get("fulfillments") or [{}])[0]
        code = ((ful.get("state") or {}).get("descriptor") or {}).get("code", "")
        ride.status = FULFILLMENT.get(code, RideStatus.CANCELLED if order.get("status") == "CANCELLED" else ride.status)
        agent, vehicle = ful.get("agent") or {}, ful.get("vehicle") or {}
        if agent and ride.driver is None:
            ride.driver = Driver(name=(agent.get("person") or {}).get("name", "Driver"),
                                 language=_lang((agent.get("person") or {}).get("languages")),
                                 vehicle=vehicle.get("model") or vehicle.get("category", "Cab").title(),
                                 plate=vehicle.get("registration", ""))
        tags = {t["descriptor"]["code"]: t.get("value") for g in ful.get("tags", []) for t in g.get("list", [])}
        eta = iso_minutes(((ful.get("stops") or [{}])[0].get("time") or {}).get("duration"))
        if eta is not None:
            ride.pickup_eta_min = eta
        if "TRIP_ETA_MIN" in tags:
            ride.trip_eta_min = int(tags["TRIP_ETA_MIN"])
        if ride.status == RideStatus.CANCELLED:
            reason = (order.get("cancellation") or {}).get("reason", {}).get("descriptor", {}).get("code")
            ride.cancel_reason = CANCEL_REASON.get(reason, "provider_cancelled")
            if reason == "NO_DRIVER_ASSIGNED":
                ride.status = RideStatus.NO_DRIVERS
        return tags

    async def step(self) -> list[Event]:
        if self.bpp_sim:
            await self.bpp_sim.tick()
        events, self._side = self._side, []
        pushed, self._pushed = self._pushed, []
        for p in pushed:
            order = p.get("message", {}).get("order", {})
            ride = self._rides.get(order.get("id"))
            if not ride:
                continue
            tags = self._apply(ride, order)
            if not self.bpp_sim and ride.cancel_reason == "driver_cancelled":
                self._driver_cancels += 1
            code = (((order.get("fulfillments") or [{}])[0].get("state") or {}).get("descriptor") or {}).get("code")
            if code not in ("RIDE_ENROUTE_PICKUP", "RIDE_STARTED") or not self.bpp_sim:
                self._note("←", p["context"]["action"], f"{ride.id}: {code}")
            km = tags.get("KM_LEFT")
            events.append(Event("ride_update", self.clock.now, {
                "ride_id": ride.id, "status": ride.status.value, "pickup_eta": ride.pickup_eta_min,
                "trip_eta": ride.trip_eta_min, "reason": ride.cancel_reason,
                "km_left": float(km) if km is not None else None,
                "driver": vars(ride.driver) if ride.driver else None, "fare": ride.fare_estimate}))
        return sorted(events, key=lambda e: e.kind != "ride_update")
