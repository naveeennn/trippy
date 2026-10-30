import asyncio
from contextlib import contextmanager

import pytest

from tripowner.config import settings
from tripowner.events import EventBus
from tripowner.llm import BudgetExceeded, LLMClient, spend
from tripowner.safety import Alert, DemoChannel, EmergencyAlerter
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


def test_budget_stops_calls_before_the_network():
    with setting(llm_daily_budget_usd=0.01, llm_base_url="http://127.0.0.1:9/v1", llm_api_key=""):
        saved = spend.usd
        spend.usd = 0.02
        try:
            with pytest.raises(BudgetExceeded):
                asyncio.run(LLMClient().chat([{"role": "user", "content": "hi"}]))
        finally:
            spend.usd = saved


def test_over_budget_trip_falls_back_to_rules():
    class OverBudget:
        async def chat(self, *a, **k):
            raise BudgetExceeded("budget used up")
    r = asyncio.run(World(demo_scenario(), "llm", llm=OverBudget()).run())
    assert r.arrived and r.bookings == 2 and r.pii_leaks == 0


def test_demo_channel_never_sends():
    bus = EventBus()
    out = asyncio.run(EmergencyAlerter(bus, [DemoChannel()]).alert(Alert("Judge", "pressed SOS", False, None)))
    assert "nothing sent" in list(out.values())[0]
