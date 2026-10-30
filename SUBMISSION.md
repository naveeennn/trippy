# TripOwner: Nebius x NVIDIA Global AI Hackathon 2026

Everything the submission form asks for, in one place. Code, tests and proof reports are in this repository.

| | |
|---|---|
| **Project** | TripOwner |
| **Elevator pitch** | It doesn't just book your cab. It owns the trip until you arrive. |
| **Track** | Best Apps and Agents: an agent people would actually use, powered by Nemotron on Nebius Token Factory |
| **Demo video** | 2:27 on YouTube: _link added after upload_. Also in the repo: [media/TripOwner_demo.mp4](media/TripOwner_demo.mp4). The last section is real footage of the app running a trip. |
| **Live demo** | https://tripowner.onrender.com (public demo mode: rides simulated, nothing is sent) |
| **Repository** | https://github.com/naveeennn/trippy (MIT) |
| **NVIDIA open model** | Nemotron 3 Super, `nvidia/nemotron-3-super-120b-a12b` |
| **Nebius** | Token Factory, OpenAI-compatible API. Self-hosting as a NIM on a Nebius GPU VM: [docs/nebius_nim.md](docs/nebius_nim.md) |

## The problem

Booking a cab in an Indian city is easy. Getting there is not. Drivers cancel, cabs don't show, the driver can't
find you, you don't share a language, you're asked for your phone number or for cash off the app, and at night
you're alone in a stranger's car. Ride apps hand all of this back to you, usually while you're busy.

## What it does

TripOwner takes the trip off your hands, from your calendar to the door:

- **Plans:** reads your calendar, checks traffic for the time you'll travel, and works out when to leave.
- **Books:** on Namma Yatri over Beckn, the open protocol behind India's ONDC network, it books with no tap
  (tested against a simulated Namma Yatri). On real Uber and Ola, which have no open booking API, it sends you a
  Telegram message at the right moment with pickup and drop already filled in: one tap.
- **Recovers:** driver cancels, no drivers, a driver who stops moving, a traffic jam, a meeting that moves. It
  rebooks, and after repeated cab failures it suggests the metro if that gets you there sooner.
- **Talks to the driver in their language:** Kannada, Hindi or English, by text or on a real phone call. It gives
  the exact pickup point, never shares your number, and politely refuses extra cash or off-app deals.
- **Warns the meeting:** emails attendees before you're late, not after.
- **Keeps you safe:** if your cab stops moving at night, it asks "Are you OK?". No answer, and your contact gets
  your location on Telegram plus an automatic phone call.

## How it uses NVIDIA Nemotron and Nebius

- **Nemotron 3 Super on Nebius Token Factory makes the judgement calls:** replying to drivers, handling
  cancellations, reacting to calendar changes, deciding when the metro beats another cab, and every turn of a live
  phone call. It acts only through tools (`request_ride`, `cancel_ride`, `message_driver`, `notify_attendees`,
  `no_action`), never by free text.
- **Deterministic guardrails sit between the model and the world:** no phone number, email or home address to
  drivers, no extra payments, a spend cap and a booking limit, whatever the model says.
- **Rules handle the routine minute-by-minute ticks**, so tokens go only to decisions that need judgement, and if
  the model errors, the rules take that event, so a flaky endpoint can't break a trip.
- **Translation:** Nemotron translates free-form replies into the driver's language; Sarvam does speech.

Measured over 30 evaluation trips: 181 Nemotron decisions, no fallbacks, median 1.6 s per decision, about
$0.003 per trip. Details and the full tables are in the [README](README.md#nemotron-eval-30-random-trips-seed-42).

## What's real and what's simulated

Real, with recorded proof in [`proof/`](proof):

- **A real phone call** answered by TripOwner on a live line: the transcript is in
  [proof/call_20261028-155237.md](proof/call_20261028-155237.md), with replies in about 2 to 3 s.
- **A real phone's GPS** for the safety check: [proof/walk_20261028-205843.md](proof/walk_20261028-205843.md).
- **A real emergency call** when the rider didn't answer, answered in 23 seconds:
  [proof/safety_20261028-212530.md](proof/safety_20261028-212530.md).
- **Real Uber and Ola booking links**, checked on a phone, sent on Telegram.
- **Real calendars** (any iCal feed) and **real email**.

Simulated: rides booked with no tap run on a mock Namma Yatri over Beckn (ONDC registration is the next step), and
the stage demo's rides, drivers and traffic. The chaos evaluation (200 trips) compares the agent with a typical
rider in simulation; the assumptions are written down in the README so they can be challenged.

## How we built it

Python and FastAPI; a small dashboard in plain HTML. NVIDIA Nemotron 3 Super on Nebius Token Factory; Sarvam for
speech-to-text, text-to-speech and translation; Twilio for phone calls; the Telegram Bot API; Beckn (ONDC) as a
signed buyer app; OpenStreetMap data and OSRM for routes; Docker on Render for the live demo. 140 offline tests run
in CI, plus a call bench that rehearses 30 driver scenarios against the call agent.

## Testing

- **Live demo:** https://tripowner.onrender.com, free and open, no login. Free hosting sleeps when idle, so the first
  load can take up to a minute.
- **Locally:** the steps under [Run it](#run-it); no keys needed. `python -m pytest -q` runs the tests offline.

## Built during the hackathon

TripOwner was started from scratch during the submission period: the first commit is on 21 October 2026, and
everything in this repository was built between then and 30 October 2026.

## Challenges

- **Latency on live calls:** 2 to 3 s per reply is fine in chat but noticeable on a phone line, so TripOwner says
  a quick "one moment" while it thinks.
- **No open ride-hailing APIs in India** (Uber's needs business approval, Ola's is invite-only), which is why we
  built the Beckn/ONDC path and the one-tap Telegram links.
- **Phone numbers:** Indian carriers often won't forward calls to a US trial number, and an Indian number needs
  business KYC, so the demo calls the number directly.

## What's next

ONDC registration so Namma Yatri bookings go live, an Indian phone number (for example from Exotel) so it can
answer forwarded calls, and more cities beyond Bengaluru.

## Where Token Factory sped us up

See [How we use NVIDIA Nemotron and Nebius](README.md#how-we-use-nvidia-nemotron-and-nebius): no GPUs to run, the
standard OpenAI client, pay-per-token evaluation ($0.087 for 30 trips), and one model fast enough for chat,
translation and live calls.

## Feedback on Nebius Token Factory, AI Cloud and NVIDIA tools

See [the feedback section of the README](README.md#feedback-on-nebius-token-factory-and-nvidia-tools).

## Run it

```bash
pip install -r requirements.txt
uvicorn tripowner.server:app --port 8765
```

Open http://localhost:8765, pick **Stage demo**, press **Run trip**, then approve the booking. No keys needed.
To turn on Nemotron, copy `.env.example` to `.env` and set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`.
