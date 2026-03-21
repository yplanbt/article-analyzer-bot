"""Police Department database — CRUD + fuzzy matching for FOIA contact info."""

from difflib import SequenceMatcher

PD_DB_TAB = "PD Database"
PD_DB_HEADERS = [
    "Department Name", "State", "Method", "Email Address",
    "Portal URL", "Portal Type", "Notes", "Last Used", "Has CAPTCHA",
    "Portal Username", "Portal Password",
]


def _normalize_dept(name: str) -> str:
    """Normalize department name for matching."""
    n = name.lower().strip()
    for suffix in [
        "police department", "police dept", "police dept.",
        "sheriff's office", "sheriffs office", "sheriff office",
        "pd", "so", "department of police", "dept of police",
    ]:
        n = n.replace(suffix, "").strip()
    # Remove extra whitespace
    return " ".join(n.split())


def get_pd_database(service, sheet_id: str) -> list:
    """Read all departments from the PD Database tab."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{PD_DB_TAB}!A:K")
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
    except Exception:
        return []


def lookup_department(pd_db: list, dept_name: str, state: str) -> "dict | None":
    """Fuzzy-match a department against the database. Returns best match or None."""
    if not pd_db or not dept_name:
        return None

    norm_query = _normalize_dept(dept_name)
    query_state = state.lower().strip()
    best_match = None
    best_score = 0.0

    for entry in pd_db:
        entry_state = entry.get("State", "").lower().strip()
        if query_state and entry_state and query_state != entry_state:
            continue

        norm_entry = _normalize_dept(entry.get("Department Name", ""))
        score = SequenceMatcher(None, norm_query, norm_entry).ratio()
        if score > best_score:
            best_score = score
            best_match = entry

    if best_score >= 0.6:
        return best_match
    return None


def add_department(service, sheet_id: str, dept_info: dict) -> None:
    """Add a new department to the PD Database tab."""
    row = [
        dept_info.get("name", ""),
        dept_info.get("state", ""),
        dept_info.get("method", "email"),
        dept_info.get("email", ""),
        dept_info.get("portal_url", ""),
        dept_info.get("portal_type", ""),
        dept_info.get("notes", ""),
        "",  # Last Used
        dept_info.get("has_captcha", ""),
        dept_info.get("portal_username", ""),
        dept_info.get("portal_password", ""),
    ]
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{PD_DB_TAB}!A:K",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def update_department_last_used(service, sheet_id: str, row_num: int, date_str: str) -> None:
    """Update the Last Used column for a department."""
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{PD_DB_TAB}!H{row_num}",
        valueInputOption="RAW",
        body={"values": [[date_str]]},
    ).execute()


def mark_department_captcha(service, sheet_id: str, dept_name: str, state: str) -> None:
    """Mark a department as having CAPTCHA on its portal."""
    pd_db = get_pd_database(service, sheet_id)
    for i, entry in enumerate(pd_db):
        if (entry.get("Department Name", "").lower().strip() == dept_name.lower().strip()
                and entry.get("State", "").lower().strip() == state.lower().strip()):
            row_num = i + 2  # 1-indexed + header
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{PD_DB_TAB}!I{row_num}",
                valueInputOption="RAW",
                body={"values": [["TRUE"]]},
            ).execute()
            return


def ensure_pd_db_headers(service, sheet_id: str) -> None:
    """Create PD Database tab with headers if it doesn't exist."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{PD_DB_TAB}!A1:K1")
            .execute()
        )
        if not result.get("values"):
            _write_headers(service, sheet_id)
    except Exception:
        # Tab might not exist — create it
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": PD_DB_TAB}}}]},
            ).execute()
        except Exception:
            pass  # Tab might already exist
        _write_headers(service, sheet_id)


def _write_headers(service, sheet_id: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{PD_DB_TAB}!A1:K1",
        valueInputOption="RAW",
        body={"values": [PD_DB_HEADERS]},
    ).execute()


def get_portal_credentials(pd_db: list, portal_url: str) -> "dict | None":
    """Look up saved portal credentials by portal URL.

    Returns {"username": ..., "password": ...} or None.
    """
    if not pd_db or not portal_url:
        return None
    url_lower = portal_url.lower().strip()
    # Match by domain (e.g., rochesterny.justfoia.com)
    from urllib.parse import urlparse
    try:
        query_domain = urlparse(url_lower).hostname or ""
    except Exception:
        query_domain = ""

    for entry in pd_db:
        entry_url = entry.get("Portal URL", "").lower().strip()
        if not entry_url:
            continue
        try:
            entry_domain = urlparse(entry_url).hostname or ""
        except Exception:
            entry_domain = ""
        if query_domain and entry_domain and query_domain == entry_domain:
            username = entry.get("Portal Username", "").strip()
            password = entry.get("Portal Password", "").strip()
            if username and password:
                return {"username": username, "password": password}
    return None


def save_portal_credentials(service, sheet_id: str, portal_url: str,
                            dept_name: str, state: str,
                            username: str, password: str) -> None:
    """Save portal credentials to PD Database. Updates existing row or adds new one."""
    pd_db = get_pd_database(service, sheet_id)

    # Try to find existing row by portal URL domain
    from urllib.parse import urlparse
    try:
        query_domain = urlparse(portal_url.lower()).hostname or ""
    except Exception:
        query_domain = ""

    for i, entry in enumerate(pd_db):
        entry_url = entry.get("Portal URL", "").lower().strip()
        try:
            entry_domain = urlparse(entry_url).hostname or ""
        except Exception:
            entry_domain = ""
        if query_domain and entry_domain and query_domain == entry_domain:
            row_num = i + 2  # 1-indexed + header
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{PD_DB_TAB}!J{row_num}:K{row_num}",
                valueInputOption="RAW",
                body={"values": [[username, password]]},
            ).execute()
            return

    # No existing row — add new department entry
    add_department(service, sheet_id, {
        "name": dept_name,
        "state": state,
        "method": "portal",
        "portal_url": portal_url,
        "portal_type": "justfoia",
        "portal_username": username,
        "portal_password": password,
    })
