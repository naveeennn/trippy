"""Minimal OpenAI-compatible chat client.

Works with a self-hosted NVIDIA NIM on a Nebius GPU VM (http://<vm>:8000/v1) or any
hosted OpenAI-compatible endpoint serving Nemotron.
"""
import asyncio
import logging
import re
from datetime import date

import httpx

from .config import settings

log = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
RETRY_WAITS_S = (0.5, 1.5)          # short: a caller is waiting on the line


class BudgetExceeded(RuntimeError):
    """Today's Nemotron budget is used up; callers fall back to rules."""


class Spend:
    """Tracks today's estimated spend from the token counts Token Factory returns."""

    def __init__(self):
        self.day, self.usd, self.calls = date.today(), 0.0, 0

    def add(self, usage: dict) -> None:
        self._roll()
        self.calls += 1
        self.usd += (usage.get("prompt_tokens", 0) * settings.llm_price_in_per_m
                     + usage.get("completion_tokens", 0) * settings.llm_price_out_per_m) / 1e6

    def _roll(self) -> None:
        if date.today() != self.day:
            self.day, self.usd, self.calls = date.today(), 0.0, 0

    def exhausted(self) -> bool:
        self._roll()
        return settings.llm_daily_budget_usd > 0 and self.usd >= settings.llm_daily_budget_usd

    def snapshot(self) -> dict:
        self._roll()
        return {"today_usd": round(self.usd, 4), "calls": self.calls,
                "daily_budget_usd": settings.llm_daily_budget_usd or None}


spend = Spend()


class LLMClient:
    def __init__(self):
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            headers=headers,
            timeout=settings.llm_timeout_s,
        )

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.2, max_tokens: int | None = None, model: str | None = None) -> dict:
        if settings.llm_system_prefix and messages and messages[0]["role"] == "system":
            messages = [{"role": "system", "content": f"{settings.llm_system_prefix}\n{messages[0]['content']}"},
                        *messages[1:]]
        body = {
            "model": model or settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if spend.exhausted():
            raise BudgetExceeded(f"today's Nemotron budget (${settings.llm_daily_budget_usd}) is used up")
        # Token Factory sometimes answers 429/529 (busy) or a 5xx; on a phone call a quick retry beats an error
        for wait in RETRY_WAITS_S:
            r = await self._client.post("chat/completions", json=body)
            if r.status_code not in RETRY_STATUSES:
                break
            log.info("Nemotron busy (%s), retrying in %.1fs", r.status_code, wait)
            await asyncio.sleep(wait)
        else:
            r = await self._client.post("chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        spend.add(data.get("usage") or {})
        msg = data["choices"][0]["message"]
        if msg.get("content"):
            msg["content"] = _THINK.sub("", msg["content"]).strip()
        return msg


_client: LLMClient | None = None


def get_llm() -> LLMClient | None:
    global _client
    if not settings.llm_enabled:
        return None
    if _client is None:
        _client = LLMClient()
    return _client
