import asyncio
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from tripowner.events import SimClock
from tripowner.models import RideStatus
from tripowner.providers.handoff import TelegramHandoff, handoff_enabled, uber_link
from tripowner.providers.maps import MockMaps
from tripowner.sim.chaos import demo_scenario
from tripowner.sim.world import World
from test_uber import setting

ME = "111"


class FakeTelegram:
    """Records what the bot sends; `press` queues a button press the next getUpdates returns."""

    def __init__(self):
        self.sent, self.answered, self._updates, self._n = [], [], [], 0

    async def send_message(self, chat_id, text, ack_button=None, keyboard=None):
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})
        return {}

    async def updates(self, offset=None, timeout=0):
        return list(self._updates)          # never confirmed, like the real thing without an offset

    async def answer_callback(self, callback_id, text):
        self.answered.append(text)

    def press(self, data, user=ME):
        self._n += 1
        self._updates.append({"update_id": self._n, "callback_query": {"id": f"cb{self._n}", "data": data,
                                                                       "from": {"id": int(user)}}})

    def last_ride_id(self):
        return self.sent[-1]["keyboard"][1][0]["callback_data"].rsplit(":", 1)[0]


def run(coro):
    return asyncio.run(coro)


def make():
    tg, clock = FakeTelegram(), SimClock(datetime(2026, 10, 27, 9, 0))
    return TelegramHandoff(clock, MockMaps(), telegram=tg, chat_id=ME), tg, clock


def test_sends_prefilled_uber_and_ola_buttons():
    h, tg, _ = make()
    run(h.request_ride("Indiranagar", "Koramangala", "Near the metro gate"))
    msg = tg.sent[0]
    assert msg["chat_id"] == ME and "Near the metro gate" in msg["text"]
    uber, ola = msg["keyboard"][0]
    q = parse_qs(urlparse(uber["url"]).query)
    assert uber["url"].startswith("https://m.uber.com/ul/?") and q["action"] == ["setPickup"]
    assert q["dropoff[nickname]"] == ["Koramangala"] and float(q["pickup[latitude]"][0]) == 12.9784
    assert ola["url"].startswith("https://book.olacabs.com/")
    assert [b["text"] for b in msg["keyboard"][1]] == ["✅ Booked", "❌ No cab"]


def test_booked_and_no_cab_buttons_become_agent_events():
    h, tg, _ = make()
    run(h.request_ride("Indiranagar", "Koramangala"))
    assert run(h.step()) == []
    tg.press(f"{tg.last_ride_id()}:ok")
    ev = run(h.step())
    assert [e.data["status"] for e in ev] == ["assigned"] and ev[0].data["pickup_eta"] > 0
    assert run(h.step()) == []                                  # the same press isn't handled twice
    run(h.request_ride("Indiranagar", "Koramangala"))
    tg.press(f"{tg.last_ride_id()}:fail")
    assert [e.data["status"] for e in run(h.step())] == ["no_drivers"]


def test_ignores_presses_from_other_people_and_reminds_once():
    h, tg, clock = make()
    run(h.request_ride("Indiranagar", "Koramangala"))
    tg.press(f"{tg.last_ride_id()}:ok", user="999")
    assert run(h.step()) == []
    clock.advance(5)
    run(h.step())
    clock.advance(5)
    run(h.step())
    assert len(tg.sent) == 2 and tg.sent[1]["text"].startswith("TripOwner: ⏰")


def test_cancel_tells_you_what_to_do():
    h, tg, _ = make()
    ride = run(h.request_ride("Indiranagar", "Koramangala"))
    tg.press(f"{ride.id}:ok")
    run(h.step())
    run(h.cancel_ride(ride.id))
    assert "cancel the cab you booked" in tg.sent[-1]["text"]


def test_agent_runs_a_trip_without_asking_you_anything():
    """No-drivers on the first link, a fresh link after the backoff, then booked: zero dashboard approvals."""
    w = World(demo_scenario(), "agent")
    h, tg, _ = make()
    h.clock = w.clock
    w.rides = w.tb.rides = h
    w.tb.auto_book = True
    pressed = {}

    async def go():
        task = asyncio.create_task(w.run(sec_per_min=0.001))
        while not task.done():
            await asyncio.sleep(0.002)
            if tg.sent and tg.sent[-1]["keyboard"] and tg.last_ride_id() not in pressed:
                rid = tg.last_ride_id()
                pressed[rid] = "fail" if not pressed else "ok"
                tg.press(f"{rid}:{pressed[rid]}")
        return await task

    r = run(go())
    assert r.approvals_asked == 0
    assert list(pressed.values())[:2] == ["fail", "ok"]
    assert all("Booked a cab" not in m["text"] for m in tg.sent)   # never claims it booked by itself


def test_link_mode_only_for_real_trips():
    with setting(ride_provider="link", telegram_bot_token="t", telegram_chat_ids=[ME], public_demo=True):
        assert not handoff_enabled()
    with setting(ride_provider="link", telegram_bot_token="t", telegram_chat_ids=[ME], public_demo=False):
        assert handoff_enabled()
        assert type(World(demo_scenario(), "agent").rides).__name__ == "MockRideProvider"
    assert "Koramangala" in uber_link("Indiranagar", "Koramangala")
