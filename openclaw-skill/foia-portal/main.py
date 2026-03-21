"""OpenClaw FOIA Portal Submission Skill.

Reads the FOIA Google Sheet for rows with Status = "Portal Needed",
then uses portal_submitter.py (hybrid: hardcoded flows for known portals
like GovQA/NextRequest/JustFOIA + AI vision fallback for unknown ones).

Setup:
  1. pip install gspread google-auth openai anthropic playwright
  2. playwright install chromium
  3. Place Google Service Account key at ~/.openclaw/workspace/google-sa-key.json
  4. Set env vars: ANTHROPIC_API_KEY, US_PROXY, CAPTCHA_API_KEY, BROWSER_HEADLESS=false
  5. Copy portal_submitter.py, ai_browser_agent.py, captcha_solver.py to ~/.openclaw/workspace/
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# Ensure ai_browser_agent.py is importable from workspace root
_WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
_SCRIPT_DIR = str(Path(__file__).resolve().parent.parent.parent)
for _path in [_WORKSPACE, _SCRIPT_DIR]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("Missing dependencies. Run: pip install gspread google-auth")
    sys.exit(1)

try:
    from portal_submitter import submit_to_portal
except ImportError:
    try:
        from ai_browser_agent import ai_submit_portal
        # Fallback wrapper if portal_submitter.py is not available
        def submit_to_portal(portal_url, request_body, subject, requester_name,
                             requester_email, police_dept="", portal_credentials=None,
                             anthropic_key="", proxy="", **kwargs):
            return ai_submit_portal(
                portal_url=portal_url, request_body=request_body, subject=subject,
                requester_name=requester_name, requester_email=requester_email,
                police_dept=police_dept,
                openai_key=os.environ.get("OPENAI_API_KEY", ""),
                anthropic_key=anthropic_key, proxy=proxy,
            )
    except ImportError:
        print("ERROR: Cannot import portal_submitter or ai_browser_agent.")
        print(f"Make sure portal_submitter.py (preferred) or ai_browser_agent.py is in:")
        print(f"  {_WORKSPACE}/")
        print(f"  OR {_SCRIPT_DIR}/")
        sys.exit(1)


# ── Configuration ────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).parent
with open(SKILL_DIR / "skill.json") as f:
    CONFIG = json.load(f)["config"]

SHEET_ID = CONFIG["sheet_id"]
REQUESTER_EMAIL = CONFIG["requester_email"]
REQUESTER_NAME = CONFIG["requester_name"]
SA_KEY_PATH = os.path.expanduser(CONFIG["sa_key_path"])

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Max portal submissions per run (each can take 2-5 minutes)
MAX_PER_RUN = int(os.environ.get("PORTAL_MAX_PER_RUN", "10"))


# ── Rate-limit helpers ───────────────────────────────────────────────────────

def _retry_on_429(fn, max_retries=3):
    """Retry a gspread call with exponential backoff on 429."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 2 ** (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    return fn()


# Header column cache (avoids re-reading row 1 on every update)
_HEADER_CACHE = {}  # type: Dict[int, Dict[str, Optional[int]]]


def _get_col_indices(worksheet):
    # type: (gspread.Worksheet) -> Dict[str, Optional[int]]
    """Cache header->column mappings to avoid repeated API calls."""
    ws_id = id(worksheet)
    if ws_id not in _HEADER_CACHE:
        headers = _retry_on_429(lambda: worksheet.row_values(1))
        _HEADER_CACHE[ws_id] = {
            "status": (headers.index("Status") + 1) if "Status" in headers else None,
            "notes": (headers.index("Notes") + 1) if "Notes" in headers else None,
            "date_sent": (headers.index("Date Sent") + 1) if "Date Sent" in headers else None,
        }
    return _HEADER_CACHE[ws_id]


# ── Google Sheets helpers ────────────────────────────────────────────────────

def get_sheet_client():
    """Authenticate with Google Sheets using Service Account."""
    creds = Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def get_pending_requests(client):
    # type: (gspread.Client) -> Tuple[List[Dict], gspread.Worksheet]
    """Get all rows with Status = 'Portal Needed' from the Requests tab."""
    sheet = client.open_by_key(SHEET_ID)
    worksheet = sheet.worksheet("Requests")
    all_rows = _retry_on_429(lambda: worksheet.get_all_records())

    pending = []
    for i, row in enumerate(all_rows):
        if row.get("Status", "").strip() == "Portal Needed":
            pending.append({
                "row_index": i + 2,  # 1-indexed header + data offset
                "data": row,
            })
    return pending, worksheet


def update_request_status(worksheet, row_index, status, notes="", date_sent=""):
    """Update a request row's status, notes, and date sent (batch update)."""
    cols = _get_col_indices(worksheet)
    cells = []

    if cols["status"]:
        cells.append(gspread.Cell(row_index, cols["status"], status))
    if cols["notes"] and notes:
        try:
            existing = _retry_on_429(
                lambda: worksheet.cell(row_index, cols["notes"]).value
            ) or ""
        except Exception:
            existing = ""
        new_notes = f"{notes} | {existing}" if existing else notes
        cells.append(gspread.Cell(row_index, cols["notes"], new_notes))
    if cols["date_sent"] and date_sent:
        cells.append(gspread.Cell(row_index, cols["date_sent"], date_sent))

    if cells:
        _retry_on_429(lambda: worksheet.update_cells(cells))


# ── Activity logging & heartbeat ─────────────────────────────────────────────

def log_to_activity(client, action, details):
    """Write an entry to the Activity Log tab."""
    try:
        sheet = client.open_by_key(SHEET_ID)
        try:
            ws = sheet.worksheet("Activity Log")
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet("Activity Log", rows=1000, cols=4)
            ws.update('A1:D1', [["Timestamp", "Action", "Details", "Source"]])
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            action, details, "OpenClaw"
        ])
    except Exception as e:
        print(f"Warning: Could not log activity: {e}")


def write_openclaw_heartbeat(client, status, last_action=""):
    """Write OpenClaw heartbeat to Monitor tab row 3."""
    try:
        sheet = client.open_by_key(SHEET_ID)
        try:
            ws = sheet.worksheet("Monitor")
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet("Monitor", rows=10, cols=8)
            ws.update('A1:H1', [["Timestamp", "Status", "Last Action",
                                  "Articles Queued", "FOIA Queued",
                                  "Portal Queued", "Errors", "Kevin Trigger"]])
        ws.update('A3:G3', [[
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            status, last_action, "", "", "", ""
        ]])
    except Exception as e:
        print(f"Warning: Could not write heartbeat: {e}")


def clear_kevin_trigger(client):
    """Clear the Kevin trigger cell (Monitor!H3) after processing."""
    try:
        sheet = client.open_by_key(SHEET_ID)
        ws = sheet.worksheet("Monitor")
        ws.update("H3", [[""]])
    except Exception as e:
        print(f"Warning: Could not clear Kevin trigger: {e}")


# ── Portal detection (for logging) ──────────────────────────────────────────

def detect_portal_type(url):
    """Detect the portal platform from URL."""
    url_lower = url.lower()
    if "govqa" in url_lower:
        return "govqa"
    if "nextrequest" in url_lower:
        return "nextrequest"
    if "justfoia" in url_lower:
        return "justfoia"
    if "jotform" in url_lower:
        return "jotform"
    return "unknown"


# ── Main execution ───────────────────────────────────────────────────────────

def run():
    """Main skill entry point. Called by OpenClaw on manual trigger."""
    print("FOIA Portal Skill: Starting...")

    # 1. Connect to Google Sheets
    try:
        client = get_sheet_client()
        print("Google Sheets: Connected")
    except Exception as e:
        print(f"ERROR: Cannot connect to Google Sheets: {e}")
        return

    write_openclaw_heartbeat(client, "Running", "Starting portal scan")

    # 2. Find all "Portal Needed" rows
    pending, worksheet = get_pending_requests(client)

    if not pending:
        print("No pending portal requests found.")
        write_openclaw_heartbeat(client, "Idle", "No pending requests")
        return

    # Limit per run to avoid very long executions
    if len(pending) > MAX_PER_RUN:
        print(f"Found {len(pending)} requests, processing first {MAX_PER_RUN}.")
        pending = pending[:MAX_PER_RUN]
    else:
        print(f"Found {len(pending)} portal request(s) to process.")

    log_to_activity(client, "Portal Scan", f"Processing {len(pending)} request(s)")

    submitted = 0
    failed = 0

    # 3. Process each row
    for item in pending:
        row_idx = item["row_index"]
        data = item["data"]
        dept = data.get("Police Department", "Unknown")
        suspect = data.get("Suspect Name", "Unknown")
        portal_url = data.get("Contact Info", "").strip()
        incident_date = data.get("Incident Date", "")
        body = data.get("Request Body", "")
        subject = f"Public Records Request - {suspect} ({incident_date})"

        print(f"\n{'─'*50}")
        print(f"Row {row_idx}: {suspect} — {dept}")
        print(f"Portal: {portal_url}")

        # 3a. Validate URL
        if not portal_url or not portal_url.startswith("http"):
            print("  SKIP: No valid portal URL")
            update_request_status(worksheet, row_idx,
                                  status="Portal Failed",
                                  notes="No valid portal URL")
            failed += 1
            continue

        portal_type = detect_portal_type(portal_url)
        print(f"  Type: {portal_type}")

        # 3b. Mark as "Submitting..."
        update_request_status(worksheet, row_idx, status="Submitting...")

        # 3c. Submit via hybrid approach (hardcoded flows + AI fallback) with retry
        MAX_ATTEMPTS = 2
        result = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"  Attempt {attempt}/{MAX_ATTEMPTS}: Submitting to portal...")
            try:
                result = submit_to_portal(
                    portal_url=portal_url,
                    request_body=body,
                    subject=subject,
                    requester_name=REQUESTER_NAME,
                    requester_email=REQUESTER_EMAIL,
                    police_dept=dept,
                    portal_credentials={"email": REQUESTER_EMAIL, "password": ""},
                    anthropic_key=os.environ.get("ANTHROPIC_API_KEY", ""),
                    proxy=os.environ.get("US_PROXY", ""),
                )
            except Exception as e:
                result = {"success": False, "error": str(e)[:300]}

            if result.get("success"):
                break
            elif attempt < MAX_ATTEMPTS:
                print(f"  Attempt {attempt} failed: {result.get('error', 'unknown')[:100]}")
                print(f"  Retrying in 10s...")
                time.sleep(10)

        # 3d/3e. Update sheet based on result
        today = datetime.now().strftime("%Y-%m-%d")

        if result.get("success"):
            confirmation = result.get("confirmation", "")
            msg = result.get("message", "")
            portal_type = result.get("portal_type", "unknown")
            notes = f"Submitted via {portal_type}"
            if confirmation:
                notes += f". Confirmation: {confirmation}"
            elif msg:
                notes += f". {msg[:150]}"

            update_request_status(worksheet, row_idx,
                                  status="Sent", notes=notes, date_sent=today)
            log_to_activity(client, "Portal Submitted",
                            f"{dept} — {suspect}" + (f": {confirmation}" if confirmation else ""))
            print(f"  SUCCESS: {notes}")
            submitted += 1
        else:
            error = result.get("error", "Unknown error")[:200]
            steps = result.get("steps", [])
            step_summary = f" (steps: {len(steps)})" if steps else ""
            ai_fallback = result.get("ai_fallback_error", "")
            fallback_note = f" | AI fallback: {ai_fallback[:100]}" if ai_fallback else ""

            # System errors keep "Portal Needed" so they auto-retry next run
            SYSTEM_ERRORS = ["playwright", "not installed", "network", "timeout",
                             "connection", "dns", "proxy", "econnrefused",
                             "chromium", "browser", "errno"]
            is_system_error = any(kw in error.lower() for kw in SYSTEM_ERRORS)

            if is_system_error:
                update_request_status(worksheet, row_idx,
                                      status="Portal Needed",
                                      notes=f"System error (will retry): {error[:150]}")
                log_to_activity(client, "Portal System Error",
                                f"{dept} — {suspect}: {error[:100]}")
                print(f"  SYSTEM ERROR (will retry next run): {error}")
            else:
                update_request_status(worksheet, row_idx,
                                      status="Portal Failed",
                                      notes=f"Failed after {MAX_ATTEMPTS} attempts{step_summary}: {error}{fallback_note}")
                log_to_activity(client, "Portal Failed",
                                f"{dept} — {suspect}: {error[:100]}")
                print(f"  FAILED after {MAX_ATTEMPTS} attempts: {error}")
                failed += 1

        # 3f. Rate limiting between submissions
        if item != pending[-1]:
            print("  Waiting 5s before next submission...")
            time.sleep(5)

    # 4. Clear the Kevin trigger cell
    clear_kevin_trigger(client)

    # 5. Log summary
    summary = f"Done: {submitted} submitted, {failed} failed out of {len(pending)}"
    print(f"\n{'═'*50}")
    print(summary)
    log_to_activity(client, "Portal Run Complete", summary)
    write_openclaw_heartbeat(client, "Idle", summary)


# ── Auto-polling daemon ──────────────────────────────────────────────────────

def poll(interval_seconds=120):
    """Poll the Google Sheet for pending portal requests and auto-process them.

    Checks Monitor!H3 for 'GO' signal, OR checks for any 'Portal Needed'
    rows directly. This replaces the manual trigger flow so Kevin doesn't
    need to be messaged to start processing.
    """
    print(f"FOIA Portal Skill: Auto-polling every {interval_seconds}s...")
    print("Press Ctrl+C to stop.\n")

    while True:
        try:
            client = get_sheet_client()

            # Check 1: Is there a "GO" trigger signal?
            triggered = False
            try:
                sheet = client.open_by_key(SHEET_ID)
                ws = sheet.worksheet("Monitor")
                trigger_val = ws.acell("H3").value or ""
                if trigger_val.strip().upper() == "GO":
                    print(f"[{datetime.now():%H:%M:%S}] GO signal detected — running...")
                    triggered = True
            except Exception:
                pass

            # Check 2: Any "Portal Needed" rows? (run even without GO signal)
            if not triggered:
                pending, _ = get_pending_requests(client)
                if pending:
                    print(f"[{datetime.now():%H:%M:%S}] {len(pending)} portal request(s) found — running...")
                    triggered = True

            if triggered:
                run()
            else:
                print(f"[{datetime.now():%H:%M:%S}] No pending requests. Sleeping {interval_seconds}s...")

        except KeyboardInterrupt:
            print("\nStopping auto-poll.")
            break
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Error during poll: {e}")

        time.sleep(interval_seconds)


# ── Standalone execution ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="FOIA Portal Submission Skill")
    parser.add_argument("--poll", action="store_true",
                        help="Run in auto-polling mode (checks for pending requests every 2 minutes)")
    parser.add_argument("--interval", type=int, default=120,
                        help="Polling interval in seconds (default: 120)")
    args = parser.parse_args()

    if args.poll:
        poll(args.interval)
    else:
        run()
