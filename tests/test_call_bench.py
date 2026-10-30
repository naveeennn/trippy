"""The call bench, offline: typed English and the rule replies (no Sarvam, no Nemotron, no network)."""
import asyncio

from tripowner.sim.call_bench import SCENARIOS, check, leaks, run_bench
from tripowner.sim.chaos import make_profile


def test_delay_scenarios_pass_on_rules_alone():
    """The 10-minute rule is fixed code, so it must hold even when Nemotron is down."""
    report = asyncio.run(run_bench(mode="text", out=None))
    by_id = {r["scenario"]: r for r in report["results"]}
    for sid in ("delay_agrees", "delay_refuses", "delay_still_long", "delay_range_then_ok", "delay_unclear",
                "short_eta", "waiting_not_delay"):
        assert by_id[sid]["passed"], (sid, by_id[sid]["checks"], by_id[sid]["turns"])
    assert all(r["checks"]["no_leak"] for r in report["results"])


def test_leak_check_catches_private_details():
    p = make_profile("Indiranagar")
    assert leaks("Sure, call her on 98450 12345", p) == ["phone number"]
    assert leaks("It's Flat 7B, near the park", p) == ["home address"]
    assert leaks("She'll wait at Gate 2, Indiranagar Metro Station", p) == []


def test_every_scenario_has_something_to_check():
    for sc in SCENARIOS:
        assert sc.lines and (sc.expect or sc.judge), sc.id
