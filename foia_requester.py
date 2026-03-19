"""FOIA bodycam footage request system — template generation, email, follow-ups."""

import smtplib
import ssl
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic

# State FOIA response deadlines (business days)
STATE_FOIA_DEADLINES = {
    "alabama": 0, "alaska": 10, "arizona": 0, "arkansas": 3,
    "california": 10, "colorado": 3, "connecticut": 4, "delaware": 15,
    "florida": 0, "georgia": 3, "hawaii": 10, "idaho": 3,
    "illinois": 5, "indiana": 0, "iowa": 10, "kansas": 3,
    "kentucky": 5, "louisiana": 3, "maine": 5, "maryland": 30,
    "massachusetts": 10, "michigan": 5, "minnesota": 0, "mississippi": 7,
    "missouri": 3, "montana": 0, "nebraska": 4, "nevada": 5,
    "new hampshire": 5, "new jersey": 7, "new mexico": 15, "new york": 5,
    "north carolina": 0, "north dakota": 0, "ohio": 0, "oklahoma": 0,
    "oregon": 5, "pennsylvania": 5, "rhode island": 10, "south carolina": 15,
    "south dakota": 0, "tennessee": 7, "texas": 10, "utah": 5,
    "vermont": 3, "virginia": 5, "washington": 5, "west virginia": 5,
    "wisconsin": 0, "wyoming": 0, "district of columbia": 15,
}
# 0 means "promptly" or no specific statutory deadline


FOIA_TEMPLATE = """Dear Records Custodian,

I am requesting copies of body-worn camera footage related to the following incident:

Date: {incident_date}
Location: {location}
Incident Description: {description}
Involved Officer(s) (if known): {officers}
Time frame requested: {timeframe}

Please include any body-worn camera recordings from the officers who responded during this timeframe, as well as the incident report associated with this event.

Electronic delivery is preferred. Please let me know if estimated fees exceed $200 before processing.

Thank you for your time.

{sender_name}"""


def generate_foia_request(
    suspect_name: str,
    incident_date: str,
    police_dept: str,
    state: str,
    location: str = "",
    officers: str = "",
    timeframe: str = "",
    sender_name: str = "",
    summary: str = "",
) -> dict:
    """Generate a FOIA request from article data. Returns subject + body."""
    description = summary if summary else f"Arrest of {suspect_name}"
    if not location:
        location = f"{police_dept} jurisdiction, {state}"
    if not timeframe:
        timeframe = "Full duration of the incident"

    subject = f"Records Request – Body-Worn Camera Footage ({incident_date})"
    body = FOIA_TEMPLATE.format(
        incident_date=incident_date,
        location=location,
        description=description,
        officers=officers if officers else "Unknown",
        timeframe=timeframe,
        sender_name=sender_name,
    )
    return {"subject": subject, "body": body}


def generate_request_id() -> str:
    """Generate FOIA-YYYYMMDD-NNN format ID."""
    return f"FOIA-{datetime.now().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"


def send_email_smtp(
    smtp_host: str,
    smtp_port: int,
    email: str,
    password: str,
    to_addr: str,
    subject: str,
    body: str,
) -> dict:
    """Send an email via SMTP. Returns {success, message_id/error}."""
    try:
        msg = MIMEMultipart()
        msg["From"] = email
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=context)
            server.login(email, password)
            server.sendmail(email, to_addr, msg.as_string())

        return {"success": True, "message": f"Sent to {to_addr}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_requests_needing_followup(
    requests: list[dict], sent_days: int = 10, progress_days: int = 14
) -> list[dict]:
    """Find requests that need follow-up based on status and elapsed time."""
    today = date.today()
    needing = []
    for req in requests:
        status = req.get("Status", "").strip()
        if status not in ("Sent", "In Progress", "Acknowledged"):
            continue

        # Check date sent
        date_sent_str = req.get("Date Sent", "").strip()
        if not date_sent_str:
            continue
        try:
            date_sent = datetime.strptime(date_sent_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        days_elapsed = (today - date_sent).days
        threshold = progress_days if status == "In Progress" else sent_days

        # Check last follow-up date
        last_fu = req.get("Last Follow-Up", "").strip()
        if last_fu:
            try:
                last_fu_date = datetime.strptime(last_fu, "%Y-%m-%d").date()
                days_since_fu = (today - last_fu_date).days
                if days_since_fu < 7:
                    continue  # Too soon for another follow-up
            except ValueError:
                pass

        if days_elapsed >= threshold:
            req["_days_elapsed"] = days_elapsed
            needing.append(req)

    return needing


def draft_follow_up(
    original_request: dict,
    anthropic_key: str,
) -> str:
    """Use Claude to draft a follow-up email for a FOIA request."""
    state = original_request.get("State", "").lower().strip()
    deadline = STATE_FOIA_DEADLINES.get(state, 0)
    deadline_text = (
        f"{state.title()} law requires an initial response within {deadline} business days."
        if deadline > 0
        else f"{state.title()} requires a prompt response but has no specific statutory deadline."
    )

    days_elapsed = original_request.get("_days_elapsed", "unknown")
    follow_up_count = int(original_request.get("Follow-Up Count", "0") or "0")
    dept = original_request.get("Police Department", "the department")
    date_sent = original_request.get("Date Sent", "recently")
    suspect = original_request.get("Suspect Name", "")
    incident_date = original_request.get("Incident Date", "")

    prompt = f"""Draft a brief, professional FOIA follow-up email. Be polite but firm.

Original request details:
- Sent to: {dept}
- Date sent: {date_sent}
- Days elapsed: {days_elapsed}
- Prior follow-ups sent: {follow_up_count}
- Incident: {suspect} on {incident_date}
- State deadline: {deadline_text}

Rules:
- Keep it under 150 words
- Reference the original request date
- If overdue per state law, politely note this
- If multiple follow-ups already sent, escalate tone slightly
- Do NOT include a subject line — just the email body
- Sign off with just "Thank you" (the name will be added separately)"""

    client = anthropic.Anthropic(api_key=anthropic_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text
