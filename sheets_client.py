"""Google Sheets read/write using OAuth2."""

import os
import re
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
CLIENT_SECRET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_secret.json")


def get_sheets_service(client_secret_path: str = CLIENT_SECRET_PATH):
    """Authenticate via OAuth2 and return a Google Sheets API service.

    Supports two modes:
    1. Streamlit Cloud: reads token from st.secrets["GOOGLE_TOKEN"]
    2. Local: reads token.json / client_secret.json from disk
    """
    creds = None

    # Mode 1: Streamlit Cloud (secrets-based)
    try:
        import streamlit as st
        if "GOOGLE_TOKEN" in st.secrets:
            token_data = json.loads(st.secrets["GOOGLE_TOKEN"])
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return build("sheets", "v4", credentials=creds)
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

    return build("sheets", "v4", credentials=creds)


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
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="RAW",
        body={"values": [values]},
    ).execute()


def create_new_sheet(service, title: str, make_public: bool = True) -> str:
    """Create a new Google Sheet and return its ID. Optionally make it public."""
    body = {"properties": {"title": title}}
    sheet = service.spreadsheets().create(body=body).execute()
    sheet_id = sheet["spreadsheetId"]

    if make_public:
        try:
            from googleapiclient.discovery import build as build_service
            # Build Drive service using same credentials
            creds = service._http.credentials
            drive = build_service("drive", "v3", credentials=creds)
            drive.permissions().create(
                fileId=sheet_id,
                body={
                    "type": "anyone",
                    "role": "writer",
                },
                fields="id",
            ).execute()
        except Exception:
            # If Drive permissions fail, still return the sheet
            pass

    return sheet_id


def append_rows_to_sheet(service, sheet_id: str, tab_name: str, rows: list[list[str]]):
    """Write rows to a sheet starting from A1."""
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()
