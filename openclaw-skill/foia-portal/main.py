"""OpenClaw FOIA Portal Submission Skill.

Reads the FOIA Google Sheet for rows with Status = "Portal Needed",
then uses OpenClaw's browser to navigate to the portal, fill the form,
and submit the FOIA request.

Setup:
  1. pip install gspread google-auth
  2. Place Google Service Account key at ~/.openclaw/workspace/google-sa-key.json
  3. Share the FOIA sheet with the service account email
  4. Copy this skill to ~/.openclaw/workspace/skills/foia-portal/
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("Missing dependencies. Run: pip install gspread google-auth")
    sys.exit(1)


# ── Configuration ────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).parent
with open(SKILL_DIR / "skill.json") as f:
    CONFIG = json.load(f)["config"]

SHEET_ID = CONFIG["sheet_id"]
REQUESTER_EMAIL = CONFIG["requester_email"]
REQUESTER_NAME = CONFIG["requester_name"]
SA_KEY_PATH = os.path.expanduser(CONFIG["sa_key_path"])

# Google Sheets scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ── Google Sheets helpers ────────────────────────────────────────────────────

def get_sheet_client():
    """Authenticate with Google Sheets using Service Account."""
    creds = Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def get_pending_requests(client):
    """Get all rows with Status = 'Portal Needed' from the Requests tab."""
    sheet = client.open_by_key(SHEET_ID)
    worksheet = sheet.worksheet("Requests")
    all_rows = worksheet.get_all_records()

    pending = []
    for i, row in enumerate(all_rows):
        if row.get("Status", "").strip() == "Portal Needed":
            pending.append({
                "row_index": i + 2,  # 1-indexed header + data offset
                "data": row,
            })
    return pending, worksheet


def update_request_status(worksheet, row_index, status, notes="", date_sent=""):
    """Update a request row's status, notes, and date sent."""
    # Find column indices from headers
    headers = worksheet.row_values(1)
    status_col = headers.index("Status") + 1 if "Status" in headers else None
    notes_col = headers.index("Notes") + 1 if "Notes" in headers else None
    date_sent_col = headers.index("Date Sent") + 1 if "Date Sent" in headers else None

    if status_col:
        worksheet.update_cell(row_index, status_col, status)
    if notes_col and notes:
        existing_notes = worksheet.cell(row_index, notes_col).value or ""
        new_notes = f"{notes} | {existing_notes}" if existing_notes else notes
        worksheet.update_cell(row_index, notes_col, new_notes)
    if date_sent_col and date_sent:
        worksheet.update_cell(row_index, date_sent_col, date_sent)


# ── Activity logging & heartbeat ─────────────────────────────────────────────

def log_to_activity(client, action, details):
    """Write an entry to the Activity Log tab so the dashboard can see OpenClaw's work."""
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
    """Write OpenClaw heartbeat to Monitor tab row 3 (row 2 is for monitor_agent.py)."""
    try:
        sheet = client.open_by_key(SHEET_ID)
        try:
            ws = sheet.worksheet("Monitor")
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet("Monitor", rows=10, cols=7)
            ws.update('A1:G1', [["Timestamp", "Status", "Last Action", "Articles Queued", "FOIA Queued", "Portal Queued", "Errors"]])
        ws.update('A3:G3', [[
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            status, last_action, "", "", "", ""
        ]])
    except Exception as e:
        print(f"Warning: Could not write heartbeat: {e}")


# ── Portal detection ─────────────────────────────────────────────────────────

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


# ── Browser instructions for OpenClaw ─────────────────────────────────────────

def _extract_structured_fields(request_body):
    """Try to extract structured fields from the FOIA letter body."""
    import re
    fields = {}
    patterns = {
        "incident_time": r"(?:Approximate Time|Time of Incident|Time):\s*(.+)",
        "incident_location": r"(?:Location|Location of Occurrence|Address):\s*(.+)",
        "case_number": r"(?:Incident Report|Case Number|Report Number|Case #):\s*(.+)",
        "officer_names": r"(?:Involved Officer|Officer\(s\)|Officers?):\s*(.+)",
        "charges": r"(?:Charges|Charge\(s\)):\s*(.+)",
        "victim_name": r"(?:Victim|Victim\(s\)):\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, request_body, re.IGNORECASE)
        if match:
            val = match.group(1).strip()
            if val and val.lower() not in ("unknown", "n/a", "not available", "not identified", ""):
                fields[key] = val
    return fields


def build_browser_instructions(request_data, portal_type):
    """Build natural language instructions for OpenClaw's browser agent."""
    portal_url = request_data.get("Contact Info", "")
    suspect = request_data.get("Suspect Name", "Unknown")
    incident_date = request_data.get("Incident Date", "")
    subject = f"Public Records Request - {suspect} ({incident_date})"
    body = request_data.get("Request Body", "")
    dept = request_data.get("Police Department", "")

    # Extract structured fields from the letter body
    fields = _extract_structured_fields(body)

    instructions = f"""
TASK: Submit a FOIA/public records request through a police department portal.

PORTAL URL: {portal_url}
PORTAL TYPE: {portal_type}
DEPARTMENT: {dept}

REQUESTER INFO:
- First Name: {REQUESTER_NAME.split()[0] if ' ' in REQUESTER_NAME else REQUESTER_NAME}
- Last Name: {REQUESTER_NAME.split()[-1] if ' ' in REQUESTER_NAME else ''}
- Email: {REQUESTER_EMAIL}

STRUCTURED INCIDENT DETAILS (use these to fill individual form fields):
- Suspect Name: {suspect}
- Date of Incident: {incident_date}
- Time of Incident: {fields.get('incident_time', 'See description')}
- Location of Incident: {fields.get('incident_location', 'See description')}
- Case/Report Number: {fields.get('case_number', 'Not available')}
- Officer(s) Involved: {fields.get('officer_names', 'All responding officers')}
- Charges: {fields.get('charges', 'See description')}
- Victim Name: {fields.get('victim_name', '')}
- Record Type: Body-Worn Camera (BWC) Footage
- Subject Line: {subject}

FULL REQUEST BODY (use for description/details fields):
{body}

STEPS:
1. Navigate to {portal_url}
2. If the URL doesn't work or redirects to a vendor page, search for "{dept} public records request" to find the correct portal
3. Look for "Submit a Request", "New Request", "File a Request", or similar
4. If login required:
   a. Create account with email: {REQUESTER_EMAIL}, name: {REQUESTER_NAME}
   b. Or log in if already registered
5. Fill the form — IMPORTANT: Map fields carefully:
   - First Name → {REQUESTER_NAME.split()[0] if ' ' in REQUESTER_NAME else REQUESTER_NAME}
   - Last Name → {REQUESTER_NAME.split()[-1] if ' ' in REQUESTER_NAME else ''}
   - Email → {REQUESTER_EMAIL}
   - Date of Incident / Beginning Date → {incident_date}
   - Time of Occurrence → {fields.get('incident_time', '')}
   - Location of Occurrence → {fields.get('incident_location', '')}
   - Incident/Report Number → {fields.get('case_number', '')}
   - Suspect Name → {suspect}
   - Record Type / Category → "Body Camera" or "Video" or "Other: Body-Worn Camera Footage"
   - Description / Additional Info → paste the FULL REQUEST BODY above
   - Preferred delivery → Electronic / Email
6. Submit the form
7. Screenshot the confirmation page
8. Report: confirmation number, reference ID, and any follow-up instructions
"""

    if portal_type == "govqa":
        instructions += "\nGovQA HINTS: 'Submit a Request' in left sidebar. Select 'Police Records' or 'Body Camera'. May have department dropdown.\n"
    elif portal_type == "nextrequest":
        instructions += "\nNextRequest HINTS: 'Make a Request' button top-right. Single text field for description. Check 'Video/Audio' document type.\n"
    elif portal_type == "justfoia":
        instructions += "\nJustFOIA HINTS: Multi-step form. Step 1: agency, Step 2: details, Step 3: contact, Step 4: submit.\n"
    else:
        instructions += "\nUNKNOWN PORTAL: Use judgment. Look for records/FOIA request forms. If it's a general contact form, use it.\n"

    return instructions


# ── Main execution ────────────────────────────────────────────────────────────

def run():
    """Main skill entry point. Called by OpenClaw on heartbeat or manual trigger."""
    print("FOIA Portal Skill: Starting...")

    # Connect to Google Sheets
    try:
        client = get_sheet_client()
        print("Google Sheets: Connected")
    except Exception as e:
        print(f"ERROR: Cannot connect to Google Sheets: {e}")
        print(f"Make sure {SA_KEY_PATH} exists and the sheet is shared with the service account.")
        return

    # Write heartbeat so the dashboard knows we're alive
    write_openclaw_heartbeat(client, "Running", "Starting portal scan")

    # Get pending requests
    pending, worksheet = get_pending_requests(client)

    if not pending:
        print("No pending portal requests found. Nothing to do.")
        write_openclaw_heartbeat(client, "Idle", "No pending requests")
        return

    print(f"Found {len(pending)} portal request(s) to process.")
    log_to_activity(client, "Portal Scan", f"Found {len(pending)} pending request(s)")

    results = {"submitted": 0, "failed": 0}

    for item in pending:
        row_idx = item["row_index"]
        data = item["data"]
        dept = data.get("Police Department", "Unknown")
        suspect = data.get("Suspect Name", "Unknown")
        portal_url = data.get("Contact Info", "")

        print(f"\nProcessing: {suspect} — {dept}")
        print(f"Portal URL: {portal_url}")

        if not portal_url or not portal_url.startswith("http"):
            print(f"  SKIP: No valid portal URL")
            update_request_status(
                worksheet, row_idx,
                status="Portal Failed",
                notes="No valid portal URL found",
            )
            results["failed"] += 1
            continue

        portal_type = detect_portal_type(portal_url)
        print(f"  Portal type: {portal_type}")

        # Build instructions for OpenClaw's browser agent
        instructions = build_browser_instructions(data, portal_type)

        # Mark as in-progress
        update_request_status(worksheet, row_idx, status="Submitting...")

        # ── This is where OpenClaw's browser agent takes over ──
        # The instructions are returned to the OpenClaw agent which will:
        # 1. Open the browser
        # 2. Navigate to the portal
        # 3. Fill and submit the form
        # 4. Report back

        # For now, print the instructions so OpenClaw's agent can execute them
        print(f"\n{'='*60}")
        print("BROWSER INSTRUCTIONS FOR OPENCLAW AGENT:")
        print(f"{'='*60}")
        print(instructions)
        print(f"{'='*60}\n")

        # The OpenClaw agent will handle the browser interaction.
        # After the agent completes, it should call complete_submission()
        # with the result.

        # For automated flow, we update status optimistically.
        # The agent should update it to "Submitted via Portal" on success
        # or "Portal Failed" on failure.

    print(f"\nDone. Processed {len(pending)} request(s).")
    print(f"  The OpenClaw browser agent should now execute the portal submissions.")
    print(f"  Monitor via: openclaw logs --follow")

    return pending


def complete_submission(worksheet, row_index, success, confirmation="", error=""):
    """Called after the browser agent completes a portal submission.

    Args:
        worksheet: The gspread worksheet object
        row_index: Row number in the sheet
        success: Whether the submission succeeded
        confirmation: Confirmation number/text from the portal
        error: Error message if failed
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Log to Activity Log for dashboard visibility
    try:
        client = get_sheet_client()
    except Exception:
        client = None

    if success:
        notes = f"Submitted via OpenClaw browser agent"
        if confirmation:
            notes += f". Confirmation: {confirmation}"
        update_request_status(
            worksheet, row_index,
            status="Sent",
            notes=notes,
            date_sent=today,
        )
        print(f"  Row {row_index}: Submitted successfully")
        if client:
            log_to_activity(client, "Portal Submitted", f"Row {row_index} submitted successfully")
            write_openclaw_heartbeat(client, "Idle", f"Submitted row {row_index}")
    else:
        update_request_status(
            worksheet, row_index,
            status="Portal Failed",
            notes=f"OpenClaw browser error: {error}",
        )
        print(f"  Row {row_index}: Failed — {error}")
        if client:
            log_to_activity(client, "Portal Failed", f"Row {row_index}: {error}")
            write_openclaw_heartbeat(client, "Error", f"Failed row {row_index}")


# ── Standalone execution ──────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
