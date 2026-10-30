"""Telegram alerts: an instant push with sound, a map pin, and an "I'm on it" button.

Setup: talk to @BotFather -> /newbot -> copy the token into TELEGRAM_BOT_TOKEN. Each emergency contact
opens the bot and presses Start. GET /api/telegram/chats lists who has, so you can put their chat IDs in
TELEGRAM_CHAT_IDS. Alerts only ever go to those IDs: anyone can find a bot by name, so "everyone who
messaged the bot" would leak alerts to strangers.
"""
import asyncio
import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org"


class TelegramClient:
    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.token = token if token is not None else settings.telegram_bot_token
        self._client = httpx.AsyncClient(base_url=f"{API}/bot{self.token}/", timeout=20, transport=transport)

    async def _call(self, method: str, **params) -> dict:
        r = await self._client.post(method, json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
        return data["result"]

    async def me(self) -> dict:
        return await self._call("getMe")

    async def send_message(self, chat_id: str, text: str, ack_button: str | None = None,
                           keyboard: list[list[dict]] | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
        if ack_button:
            params["reply_markup"] = {"inline_keyboard": [[{"text": "✅ I'm on it", "callback_data": ack_button}]]}
        elif keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return await self._call("sendMessage", **params)

    async def send_location(self, chat_id: str, lat: float, lon: float) -> dict:
        return await self._call("sendLocation", chat_id=chat_id, latitude=lat, longitude=lon)

    async def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", **params)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def chats(self) -> list[dict]:
        """People who have messaged the bot recently (for finding chat IDs)."""
        seen = {}
        for u in await self.updates():
            chat = (u.get("message") or {}).get("chat") or ((u.get("callback_query") or {}).get("message") or {}).get("chat")
            if chat and chat.get("type") == "private":
                seen[str(chat["id"])] = {"chat_id": str(chat["id"]),
                                         "name": " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])),
                                         "username": chat.get("username")}
        return list(seen.values())
