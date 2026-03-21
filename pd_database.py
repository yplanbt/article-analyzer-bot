"""Police Department database — CRUD + fuzzy matching for FOIA contact info."""

from difflib import SequenceMatcher

PD_DB_TAB = "PD Database"
PD_DB_HEADERS = [
    "Department Name", "State", "Method", "Email Address",
    "Portal URL", "Portal Type", "Notes", "Last Used", "Has CAPTCHA",
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


def get_pd_database(service, sheet_id: str) -> list[dict]:
    """Read all departments from the PD Database tab."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{PD_DB_TAB}!A:I")
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
    ]
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{PD_DB_TAB}!A:I",
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
            .get(spreadsheetId=sheet_id, range=f"{PD_DB_TAB}!A1:I1")
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
        range=f"{PD_DB_TAB}!A1:I1",
        valueInputOption="RAW",
        body={"values": [PD_DB_HEADERS]},
    ).execute()
