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
from frozen_utils import is_frozen, get_app_data_dir, get_bundled_resource_dir, get_playwright_browsers_path, ensure_browsers_installed

APP_DATA_DIR = get_app_data_dir()
TOKENS_FILE = APP_DATA_DIR / "tokens.json"
ACTIVITY_FILE = APP_DATA_DIR / "activity.json"
ENV_FILE = APP_DATA_DIR / ".env"
VISIBLE_TOKEN_STATUSES = {"success", "processing"}
PERSISTED_CONFIG_KEYS = (
    "ETOKEN_USERNAME",
    "ETOKEN_PASSWORD",
    "TRUCK_NO",
    "MATERIAL",
    "CYCLE_INTERVAL",
    "START_TIME",
    "END_TIME",
)

# In-memory config — loaded from .env on startup, updated when monitor starts
_current_config = {}

# In frozen mode, templates are inside sys._MEIPASS; otherwise use default
_template_folder = str(get_bundled_resource_dir() / "templates") if is_frozen() else "templates"
app = Flask(__name__, template_folder=_template_folder)

# Set Playwright browsers path when running frozen
if is_frozen():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_playwright_browsers_path())


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


def _parse_env_value(raw: str) -> str:
    """Parse a persisted dotenv value written by save_persisted_config."""
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def _parse_env_line(line: str):
    """Return (key, raw_value) for a dotenv line, or None for comments/blanks."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        return None
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return key, raw_value.rstrip("\n")


def load_persisted_config() -> dict:
    """Load saved monitor config from the writable .env file."""
    config = {}
    if not ENV_FILE.exists():
        return config

    for line in ENV_FILE.read_text().splitlines():
        parsed = _parse_env_line(line)
        if not parsed:
            continue
        key, raw_value = parsed
        if key in PERSISTED_CONFIG_KEYS:
            config[key] = _parse_env_value(raw_value)
    return config


def save_persisted_config(config: dict):
    """Persist monitor config back to the writable .env file."""
    existing_lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    updated_lines = []
    seen_keys = set()

    for line in existing_lines:
        parsed = _parse_env_line(line)
        if not parsed:
            updated_lines.append(line)
            continue

        key, _raw_value = parsed
        if key in PERSISTED_CONFIG_KEYS:
            updated_lines.append(f"{key}={json.dumps(config.get(key, ''))}")
            seen_keys.add(key)
        else:
            updated_lines.append(line)

    for key in PERSISTED_CONFIG_KEYS:
        if key not in seen_keys:
            updated_lines.append(f"{key}={json.dumps(config.get(key, ''))}")

    ENV_FILE.write_text("\n".join(updated_lines) + "\n")


_current_config = load_persisted_config()


def should_include_token_record(record: dict) -> bool:
    """Show real tokens and in-flight processing rows in the dashboard."""
    return bool(record.get("token") or record.get("status") in VISIBLE_TOKEN_STATUSES)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", config=_current_config)


@app.route("/tokens")
def get_tokens():
    tokens = read_tokens()
    visible = [t for t in tokens if should_include_token_record(t)]
    return jsonify(visible)


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


def _run_monitor_thread(stop_evt):
    """Target function for the monitor background thread."""
    # Set env vars from in-memory config so the monitor picks them up
    for key, value in _current_config.items():
        os.environ[key] = value
    asyncio.run(run_monitor(headless=True, stop_event=stop_evt))


@app.route("/monitor/start", methods=["POST"])
def monitor_start():
    global _monitor_thread, _current_config, _stop_event
    if _monitor_thread and _monitor_thread.is_alive():
        return jsonify({"status": "already_running"})

    # Read config from the form submission
    _current_config = {
        "ETOKEN_USERNAME": request.form.get("username", "").strip(),
        "ETOKEN_PASSWORD": request.form.get("password", "").strip(),
        "TRUCK_NO": request.form.get("trucks", "").strip(),
        "MATERIAL": request.form.get("material", "GOODEARTH").strip(),
        "CYCLE_INTERVAL": request.form.get("cycle_interval", "5").strip(),
        "START_TIME": request.form.get("start_time", "").strip(),
        "END_TIME": request.form.get("end_time", "").strip(),
    }

    start_time = _current_config["START_TIME"]
    end_time = _current_config["END_TIME"]
    if start_time and end_time and end_time <= start_time:
        return jsonify({"status": "error", "message": "End Time must be after Start Time."}), 400

    save_persisted_config(_current_config)

    _stop_event = threading.Event()
    _monitor_thread = threading.Thread(target=_run_monitor_thread, args=(_stop_event,), daemon=True)
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
