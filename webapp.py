#!/usr/bin/env python3
"""
E-Token Webapp — Configuration panel + token history viewer.
Runs alongside etoken_monitor.py (which reads .env and writes tokens.json).
"""

import json
import os
import threading
import asyncio
from flask import Flask, render_template, request, jsonify
from etoken_monitor import run_monitor
from frozen_utils import is_frozen, get_app_data_dir, get_bundled_resource_dir, ensure_browsers_installed

APP_DATA_DIR = get_app_data_dir()
TOKENS_FILE = APP_DATA_DIR / "tokens.json"
ACTIVITY_FILE = APP_DATA_DIR / "activity.json"

# In-memory config — blank on each startup, populated when monitor starts
_current_config = {}

# In frozen mode, templates are inside sys._MEIPASS; otherwise use default
_template_folder = str(get_bundled_resource_dir() / "templates") if is_frozen() else "templates"
app = Flask(__name__, template_folder=_template_folder)

# Set Playwright browsers path when running frozen
if is_frozen():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(APP_DATA_DIR / "browsers")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_tokens() -> list:
    """Read tokens.json, return list of dicts (newest first)."""
    if not TOKENS_FILE.exists():
        return []
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return []
    return list(reversed(tokens))


def read_activity() -> list:
    """Read activity.json, return list of dicts (newest first)."""
    if not ACTIVITY_FILE.exists():
        return []
    try:
        activities = json.loads(ACTIVITY_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return []
    return list(reversed(activities))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", config=_current_config)


@app.route("/tokens")
def get_tokens():
    tokens = read_tokens()
    # Only return entries with a non-empty token
    successful = [t for t in tokens if t.get("token")]
    return jsonify(successful)


@app.route("/tokens/clear", methods=["POST"])
def clear_tokens():
    if TOKENS_FILE.exists():
        TOKENS_FILE.write_text("[]")
    return jsonify({"status": "ok"})


@app.route("/activity")
def get_activity():
    return jsonify(read_activity())


@app.route("/activity/clear", methods=["POST"])
def clear_activity():
    if ACTIVITY_FILE.exists():
        ACTIVITY_FILE.write_text("[]")
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Monitor control
# ---------------------------------------------------------------------------

_stop_event = None
_monitor_thread = None


def _run_monitor_thread():
    """Target function for the monitor background thread."""
    global _stop_event
    _stop_event = threading.Event()
    # Set env vars from in-memory config so the monitor picks them up
    for key, value in _current_config.items():
        os.environ[key] = value
    asyncio.run(run_monitor(headless=True, stop_event=_stop_event))


@app.route("/monitor/start", methods=["POST"])
def monitor_start():
    global _monitor_thread, _current_config
    if _monitor_thread and _monitor_thread.is_alive():
        return jsonify({"status": "already_running"})

    # Read config from the form submission
    _current_config = {
        "ETOKEN_USERNAME": request.form.get("username", "").strip(),
        "ETOKEN_PASSWORD": request.form.get("password", "").strip(),
        "TRUCK_NO": request.form.get("trucks", "").strip(),
        "MATERIAL": request.form.get("material", "GOODEARTH").strip(),
        "CYCLE_INTERVAL": request.form.get("cycle_interval", "20").strip(),
        "START_TIME": request.form.get("start_time", "").strip(),
        "END_TIME": request.form.get("end_time", "").strip(),
    }

    start_time = _current_config["START_TIME"]
    end_time = _current_config["END_TIME"]
    if start_time and end_time and end_time <= start_time:
        return jsonify({"status": "error", "message": "End Time must be after Start Time."}), 400

    _monitor_thread = threading.Thread(target=_run_monitor_thread, daemon=True)
    _monitor_thread.start()
    return jsonify({"status": "started"})


@app.route("/monitor/stop", methods=["POST"])
def monitor_stop():
    global _stop_event
    if _stop_event:
        _stop_event.set()
    return jsonify({"status": "stopped"})


@app.route("/monitor/status")
def monitor_status():
    running = _monitor_thread is not None and _monitor_thread.is_alive()
    return jsonify({"running": running})


if __name__ == "__main__":
    if is_frozen():
        ensure_browsers_installed()
        app.run(debug=False, port=5000)
    else:
        app.run(debug=True, port=5000)
