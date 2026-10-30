@echo off
rem Double-click to start the TripOwner demo, then open http://localhost:8765
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)
start "" http://localhost:8765
.venv\Scripts\python.exe -m uvicorn tripowner.server:app --port 8765
