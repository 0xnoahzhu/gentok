#!/usr/bin/env python3
"""
E-Token Generator for Marina East Staging Ground
Automates login, truck entry validation, and token generation.
Saves generated tokens to tokens.json.
"""

import asyncio
import json
import os
import sys
import threading
from datetime import datetime

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from frozen_utils import get_app_data_dir

# Locks for safe concurrent file writes (initialized lazily)
_tokens_lock = None
_activity_lock = None


def _get_tokens_lock():
    global _tokens_lock
    if _tokens_lock is None:
        _tokens_lock = asyncio.Lock()
    return _tokens_lock


def _get_activity_lock():
    global _activity_lock
    if _activity_lock is None:
        _activity_lock = asyncio.Lock()
    return _activity_lock

# Load environment variables
env_path = get_app_data_dir() / ".env"
load_dotenv(dotenv_path=env_path)

# Configuration from .env
ETOKEN_USERNAME = os.getenv("ETOKEN_USERNAME", "")
ETOKEN_PASSWORD = os.getenv("ETOKEN_PASSWORD", "")
TRUCK_PASSWORD = os.getenv("ETOKEN_PASSWORD", "")
MATERIAL = os.getenv("MATERIAL", "GOODEARTH")
TRUCK_NO_LIST = [t.strip() for t in os.getenv("TRUCK_NO", "").split(",") if t.strip()]
CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL", "5"))
START_TIME = os.getenv(
    "START_TIME", ""
)  # e.g. "08:00" — wait until this time before starting cycles
END_TIME = os.getenv("END_TIME", "")  # e.g. "18:00" — stop when this time is reached

# URLs
BASE_URL = "https://marinaeaststagingground.com.sg/etoken/index.php/etokenapp"
CHKENTRY_URL = f"{BASE_URL}/chkentry"
GENTOKEN_URL = f"{BASE_URL}/gentoken"

# Default geolocation for Marina East area, Singapore
DEFAULT_LAT = 1.341453
DEFAULT_LON = 103.906435

# File paths
TOKENS_FILE = get_app_data_dir() / "tokens.json"
ACTIVITY_FILE = get_app_data_dir() / "activity.json"

RESULT_TOKEN_LABEL = "Last Token Generated:"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_PROCESSING = "processing"
STATUS_PENDING_CONFIRMATION = "pending_confirmation"

PENDING_TOKEN_MESSAGE = (
    "Submission appears accepted, but the token is not visible yet. "
    "Keeping this truck in processing."
)
ALREADY_PROCESSED_MESSAGE = (
    "Platform reports this truck is already processed. "
    "Keeping the existing submission in processing."
)


async def safe_query_selector(page, selector, retries=3, delay=0.5):
    """Query selector with retry to handle navigation context destruction."""
    for attempt in range(retries):
        try:
            return await page.query_selector(selector)
        except Exception as e:
            if attempt < retries - 1 and "Execution context was destroyed" in str(e):
                await asyncio.sleep(delay)
                continue
            raise


def _read_json_records(path):
    """Read a JSON list from disk, returning an empty list on failure."""
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return []
    return records if isinstance(records, list) else []


def _write_json_records(path, records):
    """Persist a JSON list to disk."""
    path.write_text(json.dumps(records, indent=2))


def is_already_processed_message(message: str) -> bool:
    """Return True when the server says the truck was already processed."""
    lowered = (message or "").lower()
    return "already" in lowered and "process" in lowered


def has_processing_signal(result: dict) -> bool:
    """Return True when the parsed table looks like a real submission result."""
    for label in ("E-Token Generated @", "Source Site Entry Record:", "Site Code:"):
        if (result.get(label) or "").strip():
            return True
    return False


def classify_generation_result(result: dict) -> str:
    """Classify the parsed result table from the token page."""
    token_value = (result.get(RESULT_TOKEN_LABEL) or "").strip()
    if token_value:
        return STATUS_SUCCESS
    if has_processing_signal(result):
        return STATUS_PENDING_CONFIRMATION
    return STATUS_FAILED


def build_token_record(
    truck_no: str,
    material: str,
    result: dict,
    *,
    status: str,
    message: str = "",
    timestamp: str = None,
) -> dict:
    """Build the token record written to tokens.json."""
    token_value = (result.get(RESULT_TOKEN_LABEL) or "").strip()
    return {
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
        "truck_no": truck_no,
        "material": material,
        "token": token_value,
        "site": result.get("Site Code:", "CR202"),
        "generated_at": result.get("E-Token Generated @", ""),
        "entry_record": result.get("Source Site Entry Record:", ""),
        "status": status,
        "message": message or token_value,
    }


def find_processing_token_record(truck_no: str, material: str):
    """Return the newest processing token record for this truck/material, if any."""
    for record in reversed(_read_json_records(TOKENS_FILE)):
        if (
            record.get("truck_no") == truck_no
            and record.get("material") == material
            and record.get("status") == STATUS_PROCESSING
        ):
            return record
    return None


def _find_matching_token_index(tokens, token_data):
    """Find the newest token row we should update instead of appending."""
    for idx in range(len(tokens) - 1, -1, -1):
        record = tokens[idx]
        if record.get("truck_no") != token_data.get("truck_no"):
            continue
        if record.get("material") != token_data.get("material"):
            continue

        incoming_entry = token_data.get("entry_record") or ""
        existing_entry = record.get("entry_record") or ""
        if incoming_entry and existing_entry and incoming_entry == existing_entry:
            return idx

        if record.get("status") == STATUS_PROCESSING or not record.get("token"):
            return idx
    return None


def _merge_token_records(existing, incoming):
    """Merge token rows without overwriting good data with empty placeholders."""
    merged = dict(existing)
    for key, value in incoming.items():
        if key in {"timestamp", "status", "message"}:
            merged[key] = value
            continue
        if value not in ("", None):
            merged[key] = value
    return merged


def validate_env():
    """Check that all required env vars are set."""
    missing = []
    for name, val in [
        ("ETOKEN_USERNAME", ETOKEN_USERNAME),
        ("ETOKEN_PASSWORD", ETOKEN_PASSWORD),
        ("TRUCK_NO", ",".join(TRUCK_NO_LIST) if TRUCK_NO_LIST else ""),
        ("TRUCK_PASSWORD", ETOKEN_PASSWORD),
    ]:
        if not val or val.startswith("your_"):
            missing.append(name)
    if missing:
        print(f"ERROR: Missing or placeholder values in .env for: {', '.join(missing)}")
        print(f"Please edit {env_path} with real credentials.")
        sys.exit(1)
    if MATERIAL not in ("GOODEARTH", "SOFTCLAY"):
        print(f"ERROR: MATERIAL must be GOODEARTH or SOFTCLAY, got '{MATERIAL}'")
        sys.exit(1)


async def save_token(token_data: dict):
    """Insert or update a token row in tokens.json."""
    async with _get_tokens_lock():
        tokens = _read_json_records(TOKENS_FILE)
        match_idx = _find_matching_token_index(tokens, token_data)
        if match_idx is None:
            tokens.append(token_data)
        else:
            tokens[match_idx] = _merge_token_records(tokens[match_idx], token_data)
        _write_json_records(TOKENS_FILE, tokens)
    print(f"Token saved to {TOKENS_FILE}")


async def save_activity(activity_data: dict):
    """Append activity record to activity.json."""
    async with _get_activity_lock():
        activities = _read_json_records(ACTIVITY_FILE)
        activities.append(activity_data)
        _write_json_records(ACTIVITY_FILE, activities)


async def capture_result_table(page, retries=5, delay=1):
    """Parse the result table, retrying for slow-rendering DOM updates."""
    result = {}
    for attempt in range(retries):
        try:
            await page.wait_for_selector("table td em", timeout=5000)
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(delay)
            continue

        result = await page.evaluate(
            """() => {
                const cells = document.querySelectorAll('table td em');
                const data = {};
                const labels = [];
                cells.forEach((em, i) => {
                    const text = em.textContent.trim();
                    if (i % 2 === 0) {
                        labels.push(text);
                    } else {
                        data[labels[labels.length - 1]] = text;
                    }
                });
                return data;
            }"""
        )

        if classify_generation_result(result) == STATUS_SUCCESS:
            return result

        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return result


async def record_processing_state(truck_no: str, material: str, result: dict, message: str):
    """Persist a submission that looks accepted but still lacks a visible token."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    token_data = build_token_record(
        truck_no,
        material,
        result,
        status=STATUS_PROCESSING,
        message=message,
        timestamp=timestamp,
    )
    await save_token(token_data)
    await save_activity(
        {
            "timestamp": timestamp,
            "truck_no": truck_no,
            "material": material,
            "status": STATUS_PROCESSING,
            "message": token_data["message"],
            "token": token_data["token"],
        }
    )
    return token_data


async def ensure_token_page(page):
    """Navigate to the submission page and re-login if the session expired."""
    on_token_page = await safe_query_selector(page, 'form[name="frmgo"]')
    if on_token_page:
        return True

    await page.goto(BASE_URL, wait_until="networkidle")
    return await do_login(page)


async def reconcile_pending_submission(page, truck_no: str, material: str):
    """Try to recover a token from the current page before re-submitting."""
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no}: reconciling pending submission..."
    )
    result = await capture_result_table(page, retries=6, delay=1)
    outcome = classify_generation_result(result)

    if outcome == STATUS_SUCCESS:
        timestamp = datetime.now().isoformat(timespec="seconds")
        token_data = build_token_record(
            truck_no,
            material,
            result,
            status=STATUS_SUCCESS,
            timestamp=timestamp,
        )
        await save_token(token_data)
        await save_activity(
            {
                "timestamp": timestamp,
                "truck_no": truck_no,
                "material": material,
                "status": STATUS_SUCCESS,
                "message": f"Recovered delayed token {token_data['token']}",
                "token": token_data["token"],
            }
        )
        return {"status": STATUS_SUCCESS, "token_data": token_data}

    if outcome == STATUS_PENDING_CONFIRMATION:
        token_data = await record_processing_state(
            truck_no,
            material,
            result,
            PENDING_TOKEN_MESSAGE,
        )
        return {"status": STATUS_PROCESSING, "token_data": token_data}

    return {
        "status": STATUS_FAILED,
        "message": "Previous submission page no longer exposes recoverable token data.",
    }


async def wait_and_check_login(page, timeout_sec=3):
    """Wait for the login to complete by checking for token generation page elements."""
    try:
        # After login, the page should have the frmgo form
        await page.wait_for_selector('form[name="frmgo"]', timeout=timeout_sec * 1000)
        return True
    except Exception:
        # Double-check: maybe the form is already there but wasn't detected
        frmgo = await safe_query_selector(page, 'form[name="frmgo"]')
        if frmgo:
            return True
        return False


async def debug_page(page, label="debug"):
    """Save screenshot and HTML for debugging."""
    ts = datetime.now().strftime("%H%M%S")
    screenshot_path = get_app_data_dir() / f"debug_{label}_{ts}.png"
    html_path = get_app_data_dir() / f"debug_{label}_{ts}.html"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    html_content = await page.content()
    html_path.write_text(html_content)
    print(f"DEBUG: Screenshot saved to {screenshot_path}")
    print(f"DEBUG: HTML saved to {html_path}")
    # Also print all form fields found on the page
    forms_info = await page.evaluate(
        """() => {
        const forms = document.querySelectorAll('form');
        return Array.from(forms).map(f => {
            const inputs = f.querySelectorAll('input, select, button, textarea');
            return {
                name: f.name || '(unnamed)',
                action: f.action,
                method: f.method,
                fields: Array.from(inputs).map(i => ({
                    tag: i.tagName,
                    name: i.name,
                    type: i.type,
                    id: i.id,
                }))
            };
        });
    }"""
    )
    print(f"DEBUG: Forms on page: {json.dumps(forms_info, indent=2)}")


async def do_login(page):
    """Perform login if needed. Returns True on success, False on failure."""
    on_token_page = await safe_query_selector(page, 'form[name="frmgo"]')

    if not on_token_page:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Login page detected. Logging in..."
        )
        await debug_page(page, "login_before")

        username_selectors = [
            'input[name="username"]',
            'input[name="user"]',
            'input[name="uname"]',
            'input[name="login"]',
            'input[name="email"]',
            'input[type="text"]:not([name="vehno"])',
            'input:not([type="hidden"]):not([type="submit"]):not([type="password"]):not([type="reset"]):not([name="vehno"])',
        ]
        username_field = None
        for sel in username_selectors:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                username_field = loc
                print(f"  Found username field via: {sel}")
                break

        password_field = page.locator(
            'form[action*="login"] input[type="password"]'
        ).first

        if not username_field:
            print("ERROR: Could not find a username/login input field.")
            print("  Check debug_login_before_*.html to see the page structure.")
            return False

        try:
            await username_field.fill(os.getenv("ETOKEN_USERNAME", ""))
            await password_field.fill(os.getenv("ETOKEN_PASSWORD", ""))
        except Exception as e:
            print(f"ERROR: Could not fill login fields: {e}")
            await debug_page(page, "login_fill_error")
            return False

        submit_btn = page.locator('input[type="submit"], button[type="submit"]').first
        try:
            await submit_btn.click(timeout=5000)
        except Exception:
            print("  No submit button found, trying form.submit()...")
            await page.evaluate(
                """() => {
                const forms = document.querySelectorAll('form');
                for (const f of forms) {
                    const pw = f.querySelector('input[type="password"]');
                    if (pw) { f.submit(); return; }
                }
            }"""
            )

        logged_in = await wait_and_check_login(page)
        if not logged_in:
            print(
                "ERROR: Login may have failed. Could not reach token generation page."
            )
            await debug_page(page, "login_failed")
            return False

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Login successful.")
    else:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Already on token page (session active)."
        )

    return True


async def generate_token_cycle(page, truck_no, material, pending_recovery=False):
    """Perform one truck-entry + token-generation cycle for a single truck.

    Args:
        page: Playwright page object.
        truck_no: The truck number to use for this cycle.
        pending_recovery: True when a previous submission likely succeeded but
            the token was never captured.

    Returns a result dict with a status key.
    """
    # --- Truck entry validation ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Filling truck entry form...")
    print(f"  Truck No: {truck_no}")

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for form fields to render..."
    )
    await page.wait_for_selector('input[name="vehno"][type="text"]', timeout=15000)
    await page.wait_for_selector('input[name="passwd"]', timeout=5000)

    vehno_input = page.locator('input[name="vehno"][type="text"]').first
    passwd_input = page.locator('input[name="passwd"]').last

    await vehno_input.fill(truck_no)
    await passwd_input.fill(os.getenv("ETOKEN_PASSWORD", ""))

    await page.evaluate(
        """(truckNo) => {
            document.querySelectorAll('input[name="vehno"][type="hidden"]').forEach(el => {
                el.value = truckNo;
            });
        }""",
        truck_no,
    )

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Submitting truck entry form...")
    await page.evaluate(
        """() => {
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = 'https://marinaeaststagingground.com.sg/etoken/index.php/etokenapp/chkentry';

            const fields = ['site', 'status', 'tknoff', 'minfo', 'vehno'];
            fields.forEach(name => {
                const inputs = document.querySelectorAll('input[name="' + name + '"]');
                inputs.forEach(input => {
                    if (input.type === 'hidden') {
                        const inp = document.createElement('input');
                        inp.type = 'hidden';
                        inp.name = name;
                        inp.value = input.value;
                        form.appendChild(inp);
                    }
                });
            });

            const passwd = document.querySelector('input[name="passwd"]');
            if (passwd) {
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = 'passwd';
                inp.value = passwd.value;
                form.appendChild(inp);
            }

            document.body.appendChild(form);
            form.submit();
        }"""
    )

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Validating truck entry...")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(3)

    error_title = await safe_query_selector(page, "#swal2-title, .swal2-title")
    has_error_icon = await page.evaluate(
        '() => document.querySelector(".swal2-icon.swal2-error") !== null'
    )
    if error_title and has_error_icon:
        error_text = (await error_title.inner_text()).strip()
        if error_text:
            print(f"ERROR: Truck entry validation failed: {error_text}")
            if is_already_processed_message(error_text):
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Truck already processed, skipping to next."
                )
                if pending_recovery or find_processing_token_record(truck_no, material):
                    token_data = await record_processing_state(
                        truck_no,
                        material,
                        {},
                        ALREADY_PROCESSED_MESSAGE,
                    )
                    return {
                        "status": STATUS_PROCESSING,
                        "token_data": token_data,
                        "message": error_text,
                    }

                await save_activity(
                    {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "truck_no": truck_no,
                        "material": material,
                        "status": STATUS_SKIPPED,
                        "message": error_text,
                    }
                )
                return {"status": STATUS_SKIPPED, "message": error_text}
            await save_activity(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "truck_no": truck_no,
                    "material": material,
                    "status": STATUS_FAILED,
                    "message": error_text,
                }
            )
            return {"status": STATUS_FAILED, "message": error_text}

    # --- Token generation ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Generating token...")
    print(f"  Material: {material}")

    material_select = page.locator('select[name="material"]').first
    await material_select.select_option(value=material)

    await page.evaluate(
        """([lat, lon]) => {
            const latEl = document.getElementById('geolat');
            const lonEl = document.getElementById('geolon');
            if (latEl) latEl.value = lat;
            if (lonEl) lonEl.value = lon;
        }""",
        [str(DEFAULT_LAT), str(DEFAULT_LON)],
    )

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Submitting token generation form..."
    )
    await page.evaluate(
        """() => {
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = 'https://marinaeaststagingground.com.sg/etoken/index.php/etokenapp/gentoken';

            const fields = ['mnum', 'vehno', 'site', 'status', 'hcontractor',
                           'tknoff', 'entryrec', 'vehlatlon', 'vtsvendor', 'tknlat', 'tknlon'];
            fields.forEach(name => {
                const input = document.querySelector('input[name="' + name + '"]');
                if (input) {
                    const inp = document.createElement('input');
                    inp.type = 'hidden';
                    inp.name = name;
                    inp.value = input.value;
                    form.appendChild(inp);
                }
            });

            const material = document.querySelector('select[name="material"]');
            if (material) {
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = 'material';
                inp.value = material.value;
                form.appendChild(inp);
            }

            document.body.appendChild(form);
            form.submit();
        }"""
    )

    await page.wait_for_load_state("networkidle")

    # --- Capture the result (retry to handle slow-rendering pages) ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Capturing result...")
    result = await capture_result_table(page, retries=8, delay=1)

    print(f"\n{'=' * 60}")
    print("TOKEN GENERATION RESULT:")
    print(f"{'=' * 60}")
    for key, val in result.items():
        print(f"  {key} {val}")
    print(f"{'=' * 60}\n")

    timestamp = datetime.now().isoformat(timespec="seconds")
    outcome = classify_generation_result(result)
    token_data = build_token_record(
        truck_no,
        material,
        result,
        status=STATUS_SUCCESS if outcome == STATUS_SUCCESS else STATUS_PROCESSING,
        timestamp=timestamp,
    )

    if outcome == STATUS_PENDING_CONFIRMATION:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Token not visible yet; storing processing state."
        )
        token_data = await record_processing_state(
            truck_no,
            material,
            result,
            PENDING_TOKEN_MESSAGE,
        )
        return {
            "status": STATUS_PENDING_CONFIRMATION,
            "token_data": token_data,
            "message": token_data["message"],
        }

    if outcome == STATUS_FAILED:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Token generation returned no usable result."
        )
        await save_activity(
            {
                "timestamp": timestamp,
                "truck_no": truck_no,
                "material": material,
                "status": STATUS_FAILED,
                "message": "No token or recoverable result returned",
                "token": "",
            }
        )
        return {"status": STATUS_FAILED, "message": "No token or recoverable result"}

    await save_token(token_data)
    await save_activity(
        {
            "timestamp": timestamp,
            "truck_no": truck_no,
            "material": material,
            "status": STATUS_SUCCESS,
            "message": token_data["token"],
            "token": token_data["token"],
        }
    )
    return {"status": STATUS_SUCCESS, "token_data": token_data}


async def run_monitor(headless=False, stop_event=None):
    """Main loop: login once, then generate a token for each truck in round-robin.

    Args:
        headless: Run browser in headless mode.
        stop_event: Optional threading.Event; when set, the loop exits.
    """
    # Re-read truck list from env (fresh config when started from webapp)
    trucks = [t.strip() for t in os.getenv("TRUCK_NO", "").split(",") if t.strip()]
    if not trucks:
        print("ERROR: No truck numbers configured. Set TRUCK_NO in .env.")
        return
    cycle_interval = int(os.getenv("CYCLE_INTERVAL", "5"))
    start_time = os.getenv("START_TIME", "").strip()
    end_time = os.getenv("END_TIME", "").strip()
    material = os.getenv("MATERIAL", "GOODEARTH")
    if material not in ("GOODEARTH", "SOFTCLAY"):
        print(f"ERROR: MATERIAL must be GOODEARTH or SOFTCLAY, got '{material}'")
        return

    # --- Wait until START_TIME if configured ---
    if start_time:
        try:
            from datetime import time as dt_time

            hour, minute = map(int, start_time.split(":"))
            target = dt_time(hour, minute)
            now = datetime.now().time()
            if now < target:
                wait_secs = (
                    datetime.combine(datetime.today(), target)
                    - datetime.combine(datetime.today(), now)
                ).total_seconds()
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Waiting until {start_time} "
                    f"({int(wait_secs)}s) before starting..."
                )
                # Interruptible wait
                for _ in range(int(wait_secs * 10)):
                    if stop_event and stop_event.is_set():
                        print("Stopped while waiting for start time.")
                        return
                    await asyncio.sleep(0.1)
        except (ValueError, TypeError):
            print(f"WARNING: Invalid START_TIME '{start_time}', ignoring.")

    # --- Check if we're already past END_TIME before starting ---
    if end_time:
        try:
            from datetime import time as dt_time

            hour, minute = map(int, end_time.split(":"))
            end_dt = dt_time(hour, minute)
            if datetime.now().time() >= end_dt:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Current time is past END_TIME {end_time}. Not starting."
                )
                return
        except (ValueError, TypeError):
            print(f"WARNING: Invalid END_TIME '{end_time}', ignoring.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        # --- Repeated token generation (one long-lived session per truck) ---
        cycle = 1
        completed_trucks = set()
        truck_sessions = {}

        async def build_truck_session(truck_no):
            """Create and log into a persistent session for one truck."""
            truck_ctx = await browser.new_context(
                geolocation={"latitude": DEFAULT_LAT, "longitude": DEFAULT_LON},
                permissions=["geolocation"],
            )
            truck_page = await truck_ctx.new_page()
            try:
                if not await ensure_token_page(truck_page):
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no}: login failed."
                    )
                    await truck_page.close()
                    await truck_ctx.close()
                    return None
            except Exception:
                await truck_page.close()
                await truck_ctx.close()
                raise

            return {
                "context": truck_ctx,
                "page": truck_page,
                "awaiting_confirmation": bool(
                    find_processing_token_record(truck_no, material)
                ),
            }

        async def process_truck(truck_no, cycle_num):
            """Process a single truck in its own long-lived session."""
            session = truck_sessions.get(truck_no)
            if session is None:
                session = await build_truck_session(truck_no)
                if session is None:
                    return {"status": STATUS_FAILED, "message": "login failed"}
                truck_sessions[truck_no] = session

            truck_page = session["page"]
            if session["awaiting_confirmation"]:
                recovered = await reconcile_pending_submission(
                    truck_page,
                    truck_no,
                    material,
                )
                if recovered["status"] in (STATUS_SUCCESS, STATUS_PROCESSING):
                    session["awaiting_confirmation"] = False
                    return recovered
                session["awaiting_confirmation"] = False

            if not await ensure_token_page(truck_page):
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no}: login failed."
                )
                return {"status": STATUS_FAILED, "message": "login failed"}

            print(f"\n--- Cycle #{cycle_num} | Truck: {truck_no} ---")
            try:
                result = await generate_token_cycle(
                    truck_page,
                    truck_no,
                    material,
                    pending_recovery=bool(
                        find_processing_token_record(truck_no, material)
                    ),
                )
                if result["status"] == STATUS_PENDING_CONFIRMATION:
                    session["awaiting_confirmation"] = True
                else:
                    session["awaiting_confirmation"] = False

                if result["status"] == STATUS_FAILED:
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no} failed."
                    )
                return result
            except Exception as e:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no} error: {e}"
                )
                return {"status": STATUS_FAILED, "message": str(e)}

        while not (stop_event and stop_event.is_set()):
            pending_trucks = [t for t in trucks if t not in completed_trucks]
            if not pending_trucks:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] All trucks completed successfully. Stopping.")
                break

            print(f"\n{'=' * 60}")
            print(f"TOKEN GENERATION CYCLE #{cycle} | Pending: {pending_trucks}")
            print(f"{'=' * 60}")

            # Process all pending trucks concurrently (each keeps its own session)
            results = await asyncio.gather(
                *(process_truck(t, cycle) for t in pending_trucks),
                return_exceptions=True,
            )

            # Mark trucks that either succeeded or are now safely in processing
            for truck_no, result in zip(pending_trucks, results):
                if isinstance(result, Exception):
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no} exception: {result}"
                    )
                elif result["status"] in (
                    STATUS_SUCCESS,
                    STATUS_PROCESSING,
                    STATUS_SKIPPED,
                ):
                    completed_trucks.add(truck_no)
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Truck {truck_no} completed. ({len(completed_trucks)}/{len(trucks)} done)"
                    )

            cycle += 1

            # --- Check END_TIME: stop if past the configured end time ---
            if end_time:
                try:
                    from datetime import time as dt_time

                    hour_e, minute_e = map(int, end_time.split(":"))
                    end_dt = dt_time(hour_e, minute_e)
                    if datetime.now().time() >= end_dt:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] END_TIME {end_time} reached. Stopping monitor."
                        )
                        break
                except (ValueError, TypeError):
                    pass

            if stop_event and stop_event.is_set():
                break

            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Cycle complete. Next cycle in {cycle_interval} seconds... (Ctrl+C to stop)"
            )

            # Interruptible sleep: check stop_event every 0.1s
            if stop_event:
                for _ in range(cycle_interval * 10):
                    if stop_event.is_set():
                        break
                    await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(cycle_interval)

        for session in truck_sessions.values():
            try:
                await session["page"].close()
            except Exception:
                pass
            try:
                await session["context"].close()
            except Exception:
                pass

        await browser.close()


if __name__ == "__main__":
    validate_env()

    headless_mode = "--headless" in sys.argv

    try:
        asyncio.run(run_monitor(headless=headless_mode))
    except KeyboardInterrupt:
        print("\nStopped by user.")
