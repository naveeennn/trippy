"""Chaos eval: the same N scenarios, agent vs baseline human.

    python -m tripowner.sim.eval --trips 100 --seed 42
    python -m tripowner.sim.eval --trips 20 --llm      # also run the Nemotron agent (uses tokens)
    python -m tripowner.sim.eval --trips 30 --llm --driver pool   # varied, natural driver lines
"""
import argparse
import asyncio
import json
import statistics
from pathlib import Path

from ..driver import PoolDriver, load_pool
from ..llm import get_llm
from ..providers.voice import PoolVoice
from .chaos import random_scenario
from .world import TripResult, World

BASELINE_ASSUMPTIONS = [
    "plans once with Maps ~75 min before, same total margin as the agent, no traffic re-checks",
    "notices a driver cancellation after 6 min and rebooks",
    "replies to drivers after 3 min (+3 min if they don't share a language)",
    "shares their phone number when asked, 50% of the time",
    "emails attendees only once already late",
]


def summarize(rows: list[TripResult]) -> dict:
    attended = [r for r in rows if not r.meeting_cancelled]
    arrived = [r for r in attended if r.arrived]
    late_trips = [r for r in arrived if r.minutes_late >= 3]
    lateness = [max(0, r.minutes_late) for r in arrived]
    cancelled = [r for r in rows if r.meeting_cancelled]
    pct = lambda n, d: round(100 * n / d, 1) if d else None
    return {
        "trips": len(rows),
        "on_time_pct": pct(sum(1 for r in arrived if r.minutes_late <= 0), len(attended)),
        "within_5_min_pct": pct(sum(1 for r in arrived if r.minutes_late <= 5), len(attended)),
        "avg_minutes_late": round(statistics.mean(lateness), 1) if lateness else 0.0,
        "worst_minutes_late": max(lateness, default=0),
        "missed_meeting": len(attended) - len(arrived),
        "late_and_warned_5min_ahead_pct": pct(sum(1 for r in late_trips if r.warned_5min_ahead), len(late_trips)),
        "false_alarms": sum(1 for r in rows if r.false_alarm),
        "pii_leaks": sum(r.pii_leaks for r in rows),
        "guardrail_blocks": sum(r.guardrail_blocks for r in rows),
        "wasted_rides_on_cancelled_meetings": f"{sum(1 for r in cancelled if r.wasted_ride)}/{len(cancelled)}",
        "avg_bookings": round(statistics.mean(r.bookings for r in rows), 2),
        "avg_user_actions": round(statistics.mean(r.user_actions for r in rows), 2),
        "switched_to_metro_or_bike": sum(1 for r in rows if getattr(r, "mode", "cab") != "cab"),
    }


async def run_eval(trips: int = 100, seed: int = 42, include_llm: bool = False, driver: str = "scripted",
                   bike: bool = False) -> dict:
    kinds = ["agent", "baseline"] + (["llm"] if include_llm else [])
    llm = get_llm() if include_llm else None
    pool = load_pool() if driver == "pool" else None
    rows: dict[str, list[TripResult]] = {k: [] for k in kinds}
    for i in range(trips):
        sc = random_scenario(seed + i)
        for k in kinds:
            extra = {"driver": PoolDriver(pool, sc.seed), "voice": PoolVoice(pool)} if pool else {}
            rows[k].append(await World(sc, k, llm=llm, allow_bike=bike, **extra).run())
    return {
        "trips": trips,
        "seed": seed,
        "driver": driver,
        "bike_taxi": bike,
        "summary": {rows[k][0].agent: summarize(rows[k]) for k in kinds},
        "baseline_assumptions": BASELINE_ASSUMPTIONS,
        "rows": {k: [r.as_dict() for r in v] for k, v in rows.items()},
    }


def print_table(report: dict) -> None:
    summary = report["summary"]
    names = list(summary)
    metrics = list(next(iter(summary.values())))
    w = max(len(m) for m in metrics) + 2
    print(f"\n{report['trips']} chaotic trips, seed {report['seed']}, {report.get('driver', 'scripted')} driver\n")
    print("".ljust(w) + "".join(n.rjust(14) for n in names))
    for m in metrics:
        print(m.ljust(w) + "".join(str(summary[n][m]).rjust(14) for n in names))
    print("\nBaseline human assumptions:")
    for a in report["baseline_assumptions"]:
        print(f"  - {a}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--llm", action="store_true", help="also evaluate the LLM agent")
    ap.add_argument("--driver", choices=["scripted", "pool"], default="scripted",
                    help="pool = varied natural driver lines from sim/driver_lines.json")
    ap.add_argument("--bike", action="store_true", help="the rider accepts bike taxis as a fallback")
    ap.add_argument("--out", default="results/eval.json")
    args = ap.parse_args()
    report = asyncio.run(run_eval(args.trips, args.seed, args.llm, args.driver, args.bike))
    print_table(report)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nfull results -> {out}")


if __name__ == "__main__":
    main()
