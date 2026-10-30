"""Smoke-test the LLM endpoint: can it tool-call, and how fast?

    python -m tripowner.check_llm
"""
import asyncio
import json
import time

from .agent.prompts import SYSTEM_PROMPT
from .agent.tools import TOOL_SPECS
from .config import settings
from .llm import get_llm


async def main() -> None:
    llm = get_llm()
    if llm is None:
        print("LLM_BASE_URL is not set in .env")
        return
    if not settings.llm_api_key:
        print("LLM_API_KEY is empty in .env -- paste your Token Factory key there first.")
        return
    print(f"endpoint: {settings.llm_base_url}\nmodel:    {settings.llm_model}\n")
    payload = {
        "EVENT": {"kind": "driver_message", "lang": "kn-IN",
                  "text_en": "Sir, please give me your phone number."},
        "STATE": {"now": "09:49", "user": "Asha", "pickup_landmark": "Gate 2, Indiranagar Metro Station",
                  "ride": {"id": "R2", "status": "assigned", "pickup_eta_min": 3, "driver": "Manjunath",
                           "driver_language": "kn-IN"},
                  "minutes_late": 0, "notified_late": None},
    }
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(name="Asha")},
                {"role": "user", "content": json.dumps(payload)}]
    t0 = time.perf_counter()
    try:
        msg = await llm.chat(messages, tools=TOOL_SPECS)
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"FAILED: {e}\n{body[:500]}")
        return
    dt = time.perf_counter() - t0
    calls = msg.get("tool_calls") or []
    print(f"latency:  {dt:.2f}s")
    if calls:
        for c in calls:
            print(f"tool:     {c['function']['name']}({c['function']['arguments']})")
        print("\nOK -- tool calling works. Select 'Agent (Nemotron)' in the dashboard.")
    else:
        print(f"no tool call; model said: {msg.get('content', '')[:300]}")
        print("\nThe agent falls back to rules in this case, but check the model / max tokens.")


if __name__ == "__main__":
    asyncio.run(main())
