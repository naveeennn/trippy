---
title: TripOwner
emoji: 🚕
colorFrom: green
colorTo: gray
sdk: docker
app_port: 8765
pinned: false
license: mit
short_description: An agent that owns your cab trip until you arrive
---

# TripOwner: public demo

An agent that reads your calendar, books the cab at the right time, rebooks when drivers cancel, talks to drivers
in their language, switches to the metro when cabs keep failing, picks busier routes at night, and checks on you
if the cab stops. Reasoning by **NVIDIA Nemotron 3 Super on Nebius Token Factory**.

**This is the public demo:** rides are simulated, and emails, Telegram and SOS alerts are shown on the dashboard but
never sent. Nemotron has a daily budget; once it's used, the rule-based agent takes over.

Try **Stage demo**, **Stage demo: cabs keep failing**, and **Safety demo: late night, cab stops**.
Pick **Agent (Nemotron)** to have Nemotron make the decisions.

Code, evaluation and setup: see the GitHub repository linked in the project submission.
Map data © OpenStreetMap contributors (ODbL).
