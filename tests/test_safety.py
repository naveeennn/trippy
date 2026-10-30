import asyncio

from tripowner.events import EventBus
from tripowner.safety import Alert, AlertChannel, EmergencyAlerter, Location, LocationStore, SafetyPrompt
from tripowner.sim.chaos import demo_scenario, night_stop_scenario
from tripowner.sim.world import World


class Answer(SafetyPrompt):
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def check_in(self, question, timeout_s):
        self.asked.append(question)
        return self.answer


class FakeChannel(AlertChannel):
    name = "fake"

    def __init__(self):
        self.sent = []

    def ready(self):
        return True

    async def send(self, alert):
        self.sent.append(alert)
        return "sent"


def night(answer, drill=True):
    bus, ch = EventBus(), FakeChannel()
    prompt = Answer(answer)
    w = World(night_stop_scenario(), "agent", bus=bus, safety_prompt=prompt,
              alerter=EmergencyAlerter(bus, [ch]), locations=LocationStore(), drill=drill)
    r = asyncio.run(w.run())
    return w, r, prompt, ch, bus


def test_stopped_cab_triggers_a_check_in():
    w, r, prompt, ch, _ = night("ok")
    assert prompt.asked and "hasn't moved" in prompt.asked[0]
    assert ch.sent == [] and r.arrived                     # "I'm OK": nobody is alerted, trip continues


def test_no_answer_alerts_contacts_with_trip_details():
    w, r, prompt, ch, bus = night("timeout")
    a = ch.sent[0]
    assert a.drill and "DRILL" in a.subject()              # demo trips are labelled as drills
    body = a.body()
    assert "Indiranagar" in body and "Whitefield" in body and "KA-" in body   # route and plate
    assert "maps.google.com" in body and "112" in body
    assert any(e["kind"] == "sos_sent" for e in bus.history)


def test_help_on_a_real_trip_is_a_real_alert():
    _, _, _, ch, _ = night("help", drill=False)
    assert not ch.sent[0].drill and "ALERT" in ch.sent[0].subject()


def test_normal_trip_never_checks_in():
    prompt = Answer("ok")
    asyncio.run(World(demo_scenario(), "agent", safety_prompt=prompt).run())
    assert prompt.asked == []


def test_location_prefers_live_then_falls_back_to_route():
    store = LocationStore()
    approx = store.best("Indiranagar", "Whitefield", 0.5)
    assert approx.source.startswith("approximate") and 12.9 < approx.lat < 13.0
    store.update(12.97, 77.64)
    assert store.best("Indiranagar", "Whitefield", 0.5).source.startswith("live")


def test_sos_endpoint_sends_a_real_alert():
    from fastapi.testclient import TestClient
    import tripowner.server as srv
    ch = FakeChannel()
    srv.alerter.channels = [ch]
    r = TestClient(srv.app).post("/api/sos")
    assert r.status_code == 200 and ch.sent and not ch.sent[0].drill


def test_gps_guard_checks_in_when_the_phone_stops_moving():
    from datetime import datetime, timedelta
    from tripowner.safety import GpsGuard
    clock = [datetime(2026, 10, 23, 23, 0)]
    ch, prompt = FakeChannel(), Answer("timeout")
    g = GpsGuard(EventBus(), prompt, EmergencyAlerter(EventBus(), [ch]), "Naveen", stall_min=5, radius_m=60,
                 now=lambda: clock[0])
    g.start("Uber to Whitefield, KA-01-AB-1234")

    def at(minute, lat, lon):
        clock[0] = datetime(2026, 10, 23, 23, 0) + timedelta(minutes=minute)
        g.on_location(lat, lon)

    for m in range(0, 6):                       # moving ~400 m a minute
        at(m, 12.978, 77.640 + 0.004 * m)
    assert asyncio.run(g.check()) is None and not g.stalled()
    for m in range(6, 13):                      # stuck in one spot (GPS jitter of a few metres)
        at(m, 12.978 + (m % 2) * 0.0001, 77.660)
    assert g.stalled()
    assert asyncio.run(g.check()) == "timeout"
    a = ch.sent[0]
    assert not a.drill and a.location.source.startswith("live") and "KA-01-AB-1234" in a.body()
    assert asyncio.run(g.check()) is None       # doesn't ask again until moved 300 m


def test_gps_guard_ok_answer_sends_nothing_and_slow_traffic_is_not_a_stall():
    from datetime import datetime, timedelta
    from tripowner.safety import GpsGuard
    clock = [datetime(2026, 10, 23, 23, 0)]
    ch = FakeChannel()
    g = GpsGuard(EventBus(), Answer("ok"), EmergencyAlerter(EventBus(), [ch]), "Naveen", now=lambda: clock[0])
    g.start()
    for m in range(0, 12):                      # crawling ~80 m a minute in a jam: not a stall
        clock[0] = datetime(2026, 10, 23, 23, 0) + timedelta(minutes=m)
        g.on_location(12.978, 77.640 + 0.00075 * m)
    assert not g.stalled()
