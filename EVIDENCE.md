# TripOwner: evidence for judges

Every claim we make, and exactly where to see it proven: video timestamps, proof files in this repo, and
commands you can run yourself. We say plainly what is **real**, what is **simulated**, and what is **not done yet**.

- **Video (2:58):** https://youtu.be/-XViHnzgjlA (the real phone call is at **0:37**)
- **Code:** https://github.com/naveeennn/trippy
- **Live demo (public, safe mode; simulated rides, alerts shown but never sent):** https://tripowner.onrender.com
- **Model:** NVIDIA Nemotron 3 Super (`nvidia/nemotron-3-super-120b-a12b`) on Nebius Token Factory

---

## 1. The video, scene by scene

App screens in the video were captured from the live public demo (above), not mocked up. The exception is the "Are you OK?" screenshot labelled "Recorded test run" at about 2:30: it comes from the recorded laptop GPS replay on real roads ([`proof/safety_popup.jpg`](proof/safety_popup.jpg)), not the phone walk test, which is proven in section 2.

| Time | What you see | What it proves | Real or simulated |
|---|---|---|---|
| 0:00 | The problem: cancelled, can't find you, asks your number, 30 min late | – | – |
| 0:16 | The dashboard owning a trip | The agent runs the whole trip | Simulated ride |
| 0:24 | Intro to the real call | – | – |
| **0:37** | **A real phone call:** a person playing the driver, TripOwner answering | Understands the delay, negotiates 30 → 10 minutes, keeps the ride, answers a follow-up | **Real call** (Twilio trial line; pauses trimmed, every word kept) |
| 1:23 | "Approve booking?" | Plans from the calendar with traffic; asks the rider **once** | Simulated ride |
| 1:33 | Agent log: R1 cancelled → R2 booked, same minute | Rebooks a cancelled driver without asking again | Simulated ride |
| 1:41 | Driver asks for your phone number → refused | Guardrail: never shares the rider's number | Simulated driver, real agent + guardrails |
| 1:52 | "Emailed 2 attendees… running about 5 min late" | Warns the people you're meeting before you're late | Simulated (demo never sends email) |
| 1:59 | "Switch to the metro?" → arrives 15 min early, ₹30 | Leaves cabs behind when they keep failing | Simulated ride + metro times |
| 2:07 | Night route options | Routes compared on shops open, dark gaps, CCTV, lamps | **Real OSRM routes + OpenStreetMap data** |
| 2:21 | "Are you OK?" → no answer → alert log | Safety check-in and alert | Simulated trip plus a recorded test run; real-world proof in section 2 |
| 2:38 | Bar chart: on time 74.2% vs 57.7%; phone leaks 0 vs 18 | The chaos eval (100 trips on the public demo) | Simulator results |
| 2:54 | End card | – | – |

Notes on the video: the narration is a synthetic voice (edge-tts). In the real call, the subtitles fix two words
the speech-to-text misheard ("Jishnu", "Indiranagar"); the audio itself is untouched apart from shortened pauses.

---

## 2. Real-world proof (not simulated)

| Claim | Proof in this repo | What it shows |
|---|---|---|
| TripOwner answers a real phone call | Video **0:37–1:23** | A 46-second real call: "I will be bit late" → "How many minutes?" → "Around thirty" → "Can you make it within ten minutes? Otherwise I'll cancel and book another cab" → "That's not a problem" → pickup point repeated |
| It replies fast enough for a live call | [`proof/call_20261028-155237.md`](proof/call_20261028-155237.md) | Real call log (28 Oct 2026): Sarvam speech-to-text and voice, Nemotron replies **2.0–2.9 s** after the caller stops; asks again when it can't make out the caller |
| Safety check-in works on a real phone with its own GPS | [`proof/walk_20261028-205843.md`](proof/walk_20261028-205843.md) | Rider tapped "I'm in a cab", walked, stood still: "Are you OK?" after 1 min still, no answer → **Telegram alert sent** |
| The full alert chain, including an emergency phone call | [`proof/safety_20261028-212530.md`](proof/safety_20261028-212530.md), [`proof/safety_20261028-211928.md`](proof/safety_20261028-211928.md) | **PASS**: GPS fixes along a real OSRM route, then parked with ±10 m jitter → check-in at +125 s → alert: **Telegram sent, email sent, emergency call rang and completed (23 s)** |
| "I'm OK" sends nothing | [`proof/safety_20261026-192119.md`](proof/safety_20261026-192119.md) | Rider answered "I'm OK": no alert |
| What the contact receives | [`proof/safety_popup.jpg`](proof/safety_popup.jpg), [`proof/safety_alert_log.jpg`](proof/safety_alert_log.jpg) | The check-in popup and the alert log |

The safety runs are driven by [`scripts/safety_proof.py`](scripts/safety_proof.py) against a running server; each report
has the full timestamped event log. Alerts from tests are labelled as drills or tests so contacts aren't alarmed.

---

## 3. Measured results (reproducible)

All of these are deterministic simulations or test suites: you can rerun them. (Raw result files live in
`results/`, which is not committed, so rerun to see them.)

| Result | Number | Rerun it |
|---|---|---|
| Chaos eval, 200 random trips, seed 42 (rules agent vs a typical rider) | On time **75.0% vs 57.8%**; avg lateness **1.9 vs 5.7 min**; phone leaks **0 vs 37**; phone touches **1.0 vs 2.2** | `python -m tripowner.sim.eval --trips 200 --seed 42` (free, offline) |
| Nemotron vs rules on 100 unseen trips (seeds 1000–1099) | On time **74.2% vs 71.0%**; on the 17 trips that differed, Nemotron arrived earlier on **all 17**; 512 decisions, **0 fallbacks**, about **$0.003/trip** | `python -m tripowner.sim.eval --trips 100 --seed 1000 --llm --driver pool` (needs a Token Factory key, ≈$0.25) |
| Guardrail fix (a model mistake we caught) | Seed 109: Nemotron cancelled a ride already coming → 35 min late; now blocked in code, matches the rules | covered by `tests/test_llm_agent.py` |
| Call bench: 30 English driver scenarios through the real call pipeline | **60/60** passed with Nemotron (each scenario twice) | `python -m tripowner.sim.call_bench --mode text --llm` |
| Automated tests | **140 passing** | `pytest -q` (offline, no keys needed) |

The call bench covers: delays (and the 10-minute negotiation's edge cases), finding the pickup, phone number / home
address / OTP requests, cash, UPI and extra charges, a driver asking the rider to cancel, a breakdown, rude
drivers, and non-driver callers (delivery, bank, a friend). Script: [`tripowner/sim/call_bench.py`](tripowner/sim/call_bench.py).

---

## 4. Where Nemotron and Nebius are used

- **Nemotron makes the judgement calls** (driver conversations, cancellations, calendar changes, the metro switch)
  only through tools: `request_ride`, `cancel_ride`, `message_driver`, `notify_attendees`.
  See [`tripowner/agent/llm_agent.py`](tripowner/agent/llm_agent.py), [`tripowner/live_call.py`](tripowner/live_call.py).
- **Guardrails sit between the model and the world**, whatever it says: no phone number, email or address to drivers;
  spend cap; booking limit; no cancellation that makes the rider later. See [`tripowner/agent/guardrails.py`](tripowner/agent/guardrails.py).
- **Hard rules are code, not model decisions:** the 10-minute negotiation, "anything else?" before hanging up, safety alerts.
- **Nebius Token Factory** serves Nemotron through its OpenAI-compatible API ([`tripowner/llm.py`](tripowner/llm.py)),
  with retries on overload (HTTP 429/5xx/529). Self-hosting as a NIM on a Nebius GPU VM is documented in
  [`docs/nebius_nim.md`](docs/nebius_nim.md).

---

## 5. What is simulated, and what isn't done yet

We'd rather you hear this from us:

- **Ride booking is simulated** in the demo (a ride simulator with cancellations, no-drivers, stalled drivers, jams).
- **Namma Yatri / ONDC (Beckn):** a real, signed buyer-app client, tested against a **mock** network. Going live needs ONDC registration.
- **Uber:** a Guest Rides adapter tested against a **fake** Uber server; needs Uber for Business API access.
- **Real today:** one-tap Telegram mode with pre-filled Uber/Ola links; phone calls on a Twilio **trial** line
  (English only on the trial; Hindi/Kannada need a paid line); safety alerts by Telegram, email and phone call.
- **Night-route data:** real OSRM routes and OpenStreetMap places, but only a few percent of Bengaluru shops have
  opening hours mapped (the rest are estimated by kind of place), and OSM has little CCTV data. The UI says so,
  and scores describe how busy and visible a route is, never that it is "safe".
- **Traffic** uses typical traffic for the hour, not live traffic.

---

## 6. Five-minute check for judges

1. Watch the video from **0:37** (the real call).
2. Open the live demo, pick **Stage demo**, press **Run trip**, approve: watch the cancel → rebook and the driver call.
3. Open **📞 Live call**, type *"I will be late by 30 minutes"*, then *"Okay, 10 minutes"*, then ask for 100 rupees more.
4. Skim [`proof/safety_20261028-212530.md`](proof/safety_20261028-212530.md) (the real alert chain, emergency call included).
5. Locally: `pip install -r requirements.txt && pytest -q` (140 tests, no keys needed).
