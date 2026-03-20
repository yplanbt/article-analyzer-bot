"""Google Sheets read/write using OAuth2."""

import os
import re
import json
import time
import random
import logging
import threading
from datetime import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Serialize all Google Sheets API calls to prevent concurrent SSL connections
# from triggering OpenSSL memory corruption on Python 3.13 + macOS ARM64.
_api_lock = threading.Lock()


def _retry(fn, max_retries=4, delay=2):
    """Retry a Google API call with exponential backoff + jitter."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = min(delay * (2 ** attempt) + random.uniform(0, 1), 60)
            logger.warning(f"Sheets API retry {attempt+1}/{max_retries}: {e}")
            time.sleep(wait)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
CLIENT_SECRET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_secret.json")


_cached_creds = None


def _get_creds(client_secret_path: str = CLIENT_SECRET_PATH):
    """Get OAuth2 credentials, caching them for reuse by both Sheets and Drive."""
    global _cached_creds

    if _cached_creds and _cached_creds.valid:
        return _cached_creds

    creds = None

    # Mode 1: Streamlit Cloud (secrets-based)
    try:
        import streamlit as st
        if "GOOGLE_TOKEN" in st.secrets:
            token_data = json.loads(st.secrets["GOOGLE_TOKEN"])
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            _cached_creds = creds
            return creds
    except Exception:
        pass

    # Mode 2: Local (file-based)
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_path):
                raise FileNotFoundError(
                    f"client_secret.json not found at {client_secret_path}."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    _cached_creds = creds
    return creds


def get_sheets_service(client_secret_path: str = CLIENT_SECRET_PATH):
    """Authenticate via OAuth2 and return a Google Sheets API service."""
    creds = _get_creds(client_secret_path)
    return build("sheets", "v4", credentials=creds)


def _locked_execute(original_execute):
    """Wrap an HttpRequest.execute() to serialize SSL connections."""
    def wrapper(*args, **kwargs):
        with _api_lock:
            return original_execute(*args, **kwargs)
    return wrapper


# Monkey-patch googleapiclient to serialize all .execute() calls.
# This prevents concurrent SSL connections from crashing Python 3.13 on macOS ARM64.
try:
    from googleapiclient.http import HttpRequest as _HttpRequest
    _orig_init = _HttpRequest.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        self.execute = _locked_execute(self.execute)

    _HttpRequest.__init__ = _patched_init
except Exception:
    logger.warning("Could not patch HttpRequest for SSL thread safety")


def get_all_rows(service, sheet_id: str, tab_name: str = "Sheet1") -> list[list[str]]:
    """Read all rows from a sheet tab."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab_name}",
    ).execute()
    return result.get("values", [])


def _normalize_url(url: str) -> str:
    """Normalize a URL for comparison."""
    url = url.strip().lower()
    url = re.sub(r'[?&](utm_\w+|fbclid|gclid|ref|source|campaign)=[^&]*', '', url)
    url = re.sub(r'\?$', '', url)
    url = re.sub(r'\?&', '?', url)
    url = url.rstrip('/')
    url = re.sub(r'https?://(www\.)?', 'https://', url)
    return url


def _normalize_name(name: str) -> str:
    """Normalize a suspect name for comparison."""
    name = name.strip().lower()
    name = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def get_master_data(service, sheet_id: str, tab_name: str = "Sheet1") -> dict:
    """Return master sheet data for duplicate checking."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab_name}",
    ).execute()
    rows = result.get("values", [])

    urls = set()
    names = {}

    for row in rows[1:]:
        if not row:
            continue
        while len(row) < 5:
            row.append("")

        url = row[0].strip()
        suspect_name = row[1].strip()
        police_dept = row[3].strip() if len(row) > 3 else ""
        state = row[4].strip() if len(row) > 4 else ""

        if url:
            urls.add(_normalize_url(url))

        if suspect_name:
            for single_name in suspect_name.split("/"):
                norm_name = _normalize_name(single_name)
                if norm_name and len(norm_name) > 2:
                    if norm_name not in names:
                        names[norm_name] = []
                    names[norm_name].append({
                        "url": url,
                        "name": single_name.strip(),
                        "police_dept": police_dept,
                        "state": state,
                    })

    return {"urls": urls, "names": names}


def check_duplicate(master_data: dict, url: str, suspect_name: str, state: str) -> tuple[bool, str]:
    """Check if an article is a duplicate."""
    norm_url = _normalize_url(url)
    if norm_url in master_data["urls"]:
        return True, "URL already in master sheet"

    if suspect_name:
        for single_name in suspect_name.split("/"):
            norm_name = _normalize_name(single_name)
            if norm_name in master_data["names"]:
                matches = master_data["names"][norm_name]
                for match in matches:
                    if match["state"].strip().lower() == state.strip().lower():
                        return True, f"Same suspect '{single_name.strip()}' already in master sheet ({match['state']}, {match['police_dept']})"

    return False, ""


def write_results_to_row(service, sheet_id: str, tab_name: str, row_num: int, results: dict):
    """Write analysis results to columns F-I of the given row (1-indexed)."""
    values = [
        str(results.get("duplicate", "")),
        str(results.get("same_day_arrest", "")),
        str(results.get("foia_score", "")),
        str(results.get("youtube_score", "")),
    ]
    range_name = f"{tab_name}!F{row_num}:I{row_num}"
    _retry(lambda: service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="RAW",
        body={"values": [values]},
    ).execute())


def create_new_sheet(service, title: str, make_public: bool = True) -> str:
    """Create a new Google Sheet and return its ID. Optionally make it public."""
    body = {"properties": {"title": title}}
    sheet = service.spreadsheets().create(body=body).execute()
    sheet_id = sheet["spreadsheetId"]

    if make_public:
        try:
            creds = _get_creds()
            drive = build("drive", "v3", credentials=creds)
            drive.permissions().create(
                fileId=sheet_id,
                body={
                    "type": "anyone",
                    "role": "writer",
                },
                fields="id",
            ).execute()
        except Exception as e:
            # Log but don't fail
            import sys
            print(f"Warning: Could not make sheet public: {e}", file=sys.stderr)

    return sheet_id


def append_rows_to_sheet(service, sheet_id: str, tab_name: str, rows: list[list[str]]):
    """Write rows to a sheet starting from A1. Writes in chunks for reliability."""
    CHUNK_SIZE = 50
    for i in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[i:i + CHUNK_SIZE]
        start_row = i + 1  # 1-indexed
        _retry(lambda c=chunk, sr=start_row: service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A{sr}",
            valueInputOption="RAW",
            body={"values": c},
        ).execute())


# ═══════════════════════════════════════════════════════════════════════════════
# FOIA Request Tracking
# ═══════════════════════════════════════════════════════════════════════════════

FOIA_TAB = "Requests"
FOIA_HEADERS = [
    "Request ID", "Article URL", "Suspect Name", "Incident Date",
    "Police Department", "State", "FOIA Score", "Request Method",
    "Contact Info", "Status", "Date Created", "Date Sent",
    "Last Follow-Up", "Follow-Up Count", "Notes", "Request Body",
]


def ensure_foia_headers(service, sheet_id: str) -> None:
    """Ensure the Requests tab exists with proper headers."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{FOIA_TAB}!A1:P1")
            .execute()
        )
        if not result.get("values"):
            _write_foia_headers(service, sheet_id)
    except Exception:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": FOIA_TAB}}}]},
            ).execute()
        except Exception:
            pass
        _write_foia_headers(service, sheet_id)


def _write_foia_headers(service, sheet_id: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{FOIA_TAB}!A1:P1",
        valueInputOption="RAW",
        body={"values": [FOIA_HEADERS]},
    ).execute()


def get_foia_requests(service, sheet_id: str) -> list[dict]:
    """Read all FOIA requests as dicts."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{FOIA_TAB}!A:P")
        .execute()
    )
    rows = result.get("values", [])
    if len(rows) <= 1:
        return []
    headers = rows[0]
    return [
        {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
        for row in rows[1:]
    ]


def write_foia_request(service, sheet_id: str, request_data: dict) -> None:
    """Append a new FOIA request to the Requests tab."""
    row = [request_data.get(h, "") for h in FOIA_HEADERS]
    _retry(lambda: service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{FOIA_TAB}!A:P",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute())


def update_foia_row(service, sheet_id: str, row_num: int, updates: dict) -> None:
    """Update specific columns of a FOIA request row (row_num is 1-indexed, data row)."""
    # Map header names to column letters
    col_map = {h: chr(65 + i) for i, h in enumerate(FOIA_HEADERS)}
    for field, value in updates.items():
        if field in col_map:
            col = col_map[field]
            _retry(lambda c=col, v=value: service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{FOIA_TAB}!{c}{row_num}",
                valueInputOption="RAW",
                body={"values": [[str(v)]]},
            ).execute())


def get_existing_foia_urls(service, sheet_id: str) -> set:
    """Get all article URLs already in the FOIA tracking sheet."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{FOIA_TAB}!B:B")
            .execute()
        )
        rows = result.get("values", [])
        return {_normalize_url(row[0]) for row in rows[1:] if row}
    except Exception:
        return set()


# ═══════════════════════════════════════════════════════════════════════════════
# Article Archive
# ═══════════════════════════════════════════════════════════════════════════════

ARCHIVE_TAB = "Archive"
ARCHIVE_HEADERS = [
    "URL", "Title", "Source", "Suspect Name", "Incident Date",
    "Police Department", "State", "Search Query", "Date Found",
    "FOIA Score", "YouTube Score", "Same Day Arrest", "FOIA Status",
    "Charges", "Incident Location", "Officer Names", "Case Number",
]


def ensure_archive_headers(service, sheet_id: str) -> None:
    """Ensure the Archive tab exists with proper headers."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{ARCHIVE_TAB}!A1:Q1")
            .execute()
        )
        if not result.get("values"):
            _write_archive_headers(service, sheet_id)
    except Exception:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": ARCHIVE_TAB}}}]},
            ).execute()
        except Exception:
            pass
        _write_archive_headers(service, sheet_id)


def _write_archive_headers(service, sheet_id: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{ARCHIVE_TAB}!A1:Q1",
        valueInputOption="RAW",
        body={"values": [ARCHIVE_HEADERS]},
    ).execute()


def append_to_archive(service, sheet_id: str, articles: list[dict]) -> int:
    """Append articles to the Archive tab, deduplicating by URL. Returns count added."""
    existing_urls = set()
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{ARCHIVE_TAB}!A:A")
            .execute()
        )
        rows = result.get("values", [])
        existing_urls = {_normalize_url(row[0]) for row in rows[1:] if row}
    except Exception:
        pass

    new_rows = []
    for a in articles:
        url = a.get("url", "").strip()
        if not url or _normalize_url(url) in existing_urls:
            continue
        existing_urls.add(_normalize_url(url))
        new_rows.append([
            url,
            a.get("title", ""),
            a.get("source", ""),
            a.get("suspect_name", ""),
            a.get("incident_date", ""),
            a.get("police_dept", ""),
            a.get("state", ""),
            a.get("search_query", ""),
            a.get("date_found", datetime.now().strftime("%Y-%m-%d")),
            str(a.get("foia_score", "")),
            str(a.get("youtube_score", "")),
            a.get("same_day_arrest", ""),
            a.get("foia_status", ""),
            a.get("charges", ""),
            a.get("incident_location", ""),
            a.get("officer_names", ""),
            a.get("case_number", ""),
        ])

    if new_rows:
        _retry(lambda: service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{ARCHIVE_TAB}!A:Q",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute())

    return len(new_rows)


def get_archive_articles(service, sheet_id: str) -> list[dict]:
    """Read all archived articles as dicts."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{ARCHIVE_TAB}!A:Q")
        .execute()
    )
    rows = result.get("values", [])
    if len(rows) <= 1:
        return []
    headers = rows[0]
    return [
        {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
        for row in rows[1:]
    ]


def update_archive_foia_status(service, sheet_id: str, url: str, status: str) -> None:
    """Update the FOIA Status column for a given URL in the archive."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{ARCHIVE_TAB}!A:A")
            .execute()
        )
        rows = result.get("values", [])
        norm_target = _normalize_url(url)
        for i, row in enumerate(rows[1:], start=2):
            if row and _normalize_url(row[0]) == norm_target:
                # Column M = FOIA Status (13th column)
                _retry(lambda: service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=f"{ARCHIVE_TAB}!M{i}",
                    valueInputOption="RAW",
                    body={"values": [[status]]},
                ).execute())
                return
    except Exception as e:
        logger.warning(f"Failed to update archive FOIA status: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Append Articles to Working Sheet (without overwriting)
# ═══════════════════════════════════════════════════════════════════════════════

def append_articles_to_working_sheet(service, sheet_id: str, tab_name: str, articles: list[dict]) -> int:
    """Append analyzed articles to the working sheet without overwriting existing data. Returns count added."""
    existing_urls = set()
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{tab_name}!A:A")
            .execute()
        )
        rows = result.get("values", [])
        existing_urls = {_normalize_url(row[0]) for row in rows[1:] if row}
    except Exception:
        pass

    new_rows = []
    for a in articles:
        url = a.get("url", "").strip()
        if not url or _normalize_url(url) in existing_urls:
            continue
        existing_urls.add(_normalize_url(url))
        new_rows.append([
            url,
            a.get("suspect_name", ""),
            a.get("incident_date", ""),
            a.get("police_dept", ""),
            a.get("state", ""),
            a.get("duplicate", ""),
            a.get("same_day_arrest", ""),
            str(a.get("foia_score", "")),
            str(a.get("youtube_score", "")),
        ])

    if new_rows:
        _retry(lambda: service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A:I",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute())

    return len(new_rows)


# ═══════════════════════════════════════════════════════════════════════════════
# Activity Log
# ═══════════════════════════════════════════════════════════════════════════════

ACTIVITY_TAB = "Activity Log"
ACTIVITY_HEADERS = ["Timestamp", "Action", "Details", "Source"]


def ensure_activity_headers(service, sheet_id: str) -> None:
    """Ensure the Activity Log tab exists with proper headers."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{ACTIVITY_TAB}!A1:D1")
            .execute()
        )
        if not result.get("values"):
            _write_activity_headers(service, sheet_id)
    except Exception:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": ACTIVITY_TAB}}}]},
            ).execute()
        except Exception:
            pass
        _write_activity_headers(service, sheet_id)


def _write_activity_headers(service, sheet_id: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{ACTIVITY_TAB}!A1:D1",
        valueInputOption="RAW",
        body={"values": [ACTIVITY_HEADERS]},
    ).execute()


def log_activity(service, sheet_id: str, action: str, details: str, source: str = "App") -> None:
    """Log an activity event to the Activity Log tab."""
    try:
        ensure_activity_headers(service, sheet_id)
        row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, details, source]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{ACTIVITY_TAB}!A:D",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as e:
        logger.warning(f"Failed to log activity: {e}")


def get_recent_activity(service, sheet_id: str, limit: int = 20) -> list[dict]:
    """Read the most recent activity log entries."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{ACTIVITY_TAB}!A:D")
            .execute()
        )
        rows = result.get("values", [])
        if len(rows) <= 1:
            return []
        headers = rows[0]
        entries = [
            {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
            for row in rows[1:]
        ]
        # Return most recent first
        entries.reverse()
        return entries[:limit]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Monitor Agent Status
# ═══════════════════════════════════════════════════════════════════════════════

MONITOR_TAB = "Monitor"
MONITOR_HEADERS = ["Timestamp", "Status", "Last Action", "Articles Queued", "FOIA Queued", "Portal Queued", "Errors", "Kevin Trigger"]


def ensure_monitor_headers(service, sheet_id: str) -> None:
    """Ensure the Monitor tab exists with proper headers."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{MONITOR_TAB}!A1:H1")
            .execute()
        )
        if not result.get("values"):
            _write_monitor_headers(service, sheet_id)
    except Exception:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": MONITOR_TAB}}}]},
            ).execute()
        except Exception:
            pass
        _write_monitor_headers(service, sheet_id)


def _write_monitor_headers(service, sheet_id: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{MONITOR_TAB}!A1:H1",
        valueInputOption="RAW",
        body={"values": [MONITOR_HEADERS]},
    ).execute()


def write_monitor_heartbeat(service, sheet_id: str, status: str, last_action: str = "",
                            articles_queued: int = 0, foia_queued: int = 0,
                            portal_queued: int = 0, errors: str = "") -> None:
    """Write monitor agent heartbeat to row 2 (overwrites previous)."""
    try:
        ensure_monitor_headers(service, sheet_id)
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            status, last_action,
            str(articles_queued), str(foia_queued), str(portal_queued), errors,
        ]
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{MONITOR_TAB}!A2:G2",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()
    except Exception as e:
        logger.warning(f"Failed to write monitor heartbeat: {e}")


def read_monitor_status(service, sheet_id: str) -> dict:
    """Read the current monitor agent status."""
    try:
        ensure_monitor_headers(service, sheet_id)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{MONITOR_TAB}!A2:G2")
            .execute()
        )
        rows = result.get("values", [])
        if not rows or not rows[0]:
            return {}
        row = rows[0]
        while len(row) < 7:
            row.append("")
        return {
            "timestamp": row[0],
            "status": row[1],
            "last_action": row[2],
            "articles_queued": row[3],
            "foia_queued": row[4],
            "portal_queued": row[5],
            "errors": row[6],
        }
    except Exception:
        return {}


def write_kevin_trigger(service, sheet_id: str, value: str = "GO") -> None:
    """Write a trigger signal to the Monitor tab for Kevin's Ollama heartbeat to pick up.
    Kevin's local Ollama checks H3 — if 'GO', it triggers the OpenClaw skill manually."""
    try:
        ensure_monitor_headers(service, sheet_id)
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{MONITOR_TAB}!H3",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()
    except Exception as e:
        logger.warning(f"Failed to write Kevin trigger: {e}")


def read_kevin_trigger(service, sheet_id: str) -> str:
    """Read the Kevin trigger cell. Returns the cell value or empty string."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{MONITOR_TAB}!H3")
            .execute()
        )
        rows = result.get("values", [])
        return rows[0][0] if rows and rows[0] else ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Service Account Auth (for headless operation)
# ═══════════════════════════════════════════════════════════════════════════════

def get_service_account_sheets(sa_key_path: str):
    """Authenticate via service account and return a Google Sheets API service."""
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        sa_key_path, scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=creds)
