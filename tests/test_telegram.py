import asyncio
import json
from contextlib import contextmanager

import httpx

from tripowner.config import settings
from tripowner.providers.telegram import TelegramClient
from tripowner.safety import Alert, Location, TelegramChannel


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


def fake_telegram(calls, updates=()):
    def handler(request: httpx.Request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        calls.append((method, body))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": list(updates) if body.get("offset") is None else []})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "tripowner_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {}})
    return TelegramClient("TEST", transport=httpx.MockTransport(handler))


ALERT = Alert("Naveen", "didn't answer a safety check", False, Location(12.97, 77.64, "live from your phone"),
              {"from": "Indiranagar", "to": "Whitefield", "driver": "Venkatesh", "plate": "KA-04-Z-9087"})


def test_alert_sends_text_with_ack_button_and_map_pin():
    calls = []
    with setting(telegram_bot_token="TEST", telegram_chat_ids=("111", "222")):
        ch = TelegramChannel(fake_telegram(calls))
        assert ch.ready()
        out = asyncio.run(ch.send(ALERT))
    assert out == "sent to 2 contact(s)"
    msgs = [b for m, b in calls if m == "sendMessage"]
    pins = [b for m, b in calls if m == "sendLocation"]
    assert len(msgs) == 2 and len(pins) == 2
    assert "KA-04-Z-9087" in msgs[0]["text"] and "112" in msgs[0]["text"]
    assert msgs[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ack:{ALERT.id}"
    assert pins[0]["latitude"] == 12.97


def test_ack_button_press_is_reported():
    calls, seen = [], []
    press = {"update_id": 5, "callback_query": {"id": "q1", "data": f"ack:{ALERT.id}",
                                                "from": {"first_name": "Priya"}}}
    ch = TelegramChannel(fake_telegram(calls, [press]))
    ch.ACK_WATCH_S = 0.2
    asyncio.run(ch.watch_acks(ALERT, lambda kind, **d: seen.append((kind, d))))
    assert ("sos_ack", {"name": "Priya", "channel": "telegram"}) in seen
    assert any(m == "answerCallbackQuery" for m, _ in calls)


def test_not_ready_without_chat_ids():
    with setting(telegram_bot_token="TEST", telegram_chat_ids=()):
        assert not TelegramChannel().ready()


def test_chat_discovery_lists_private_chats():
    upd = [{"update_id": 1, "message": {"chat": {"id": 999, "type": "private", "first_name": "Priya"}}}]
    chats = asyncio.run(fake_telegram([], upd).chats())
    assert chats == [{"chat_id": "999", "name": "Priya", "username": None}]
