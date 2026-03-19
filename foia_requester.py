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


def generate_foia_request_simple(
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
    """Generate a basic FOIA request from article data. Returns subject + body."""
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


def generate_foia_request_ai(
    article_text: str,
    suspect_name: str,
    incident_date: str,
    police_dept: str,
    state: str,
    sender_name: str,
    anthropic_key: str,
) -> dict:
    """Use Claude to generate a detailed, case-specific FOIA request letter."""
    client = anthropic.Anthropic(api_key=anthropic_key)

    prompt = f"""You are writing a FOIA / public records request for body-worn camera footage.

ARTICLE ABOUT THE INCIDENT:
{article_text[:6000]}

KNOWN DETAILS:
- Suspect: {suspect_name}
- Date: {incident_date}
- Police Department: {police_dept}
- State: {state}

Write a professional FOIA request letter using this EXACT format. Fill in every field with specific details from the article:

---
Dear Records Custodian,

I am requesting copies of body-worn camera footage related to the following incident:

Date: [exact date from article]
Location: [specific address/location from article, or best available]
Incident Description: [2-3 sentences describing exactly what happened — charges, circumstances, key details from the article]
Involved Officer(s) (if known): [names from article, or "Not identified in public reporting"]
Time frame requested: [estimate based on incident type, e.g. "Approximately 30 minutes covering the arrest and booking" or "Full duration of the traffic stop and subsequent arrest"]

Please include any body-worn camera recordings from the officers who responded during this timeframe, as well as the incident report associated with this event.

Electronic delivery is preferred. Please let me know if estimated fees exceed $200 before processing.

Thank you for your time.

{sender_name}
---

Rules:
- Use ONLY facts from the article. Do not invent details.
- Be specific about the incident — vague requests get denied.
- If the article mentions multiple officers or a specific unit, reference them.
- Output ONLY the letter text, nothing else."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    body = response.content[0].text.strip()

    subject = f"Records Request – Body-Worn Camera Footage ({incident_date})"
    return {"subject": subject, "body": body}


def search_pd_contact_web(police_dept: str, state: str, serpapi_key: str, anthropic_key: str) -> dict:
    """Search the web for a PD's FOIA contact info, then have Claude extract it."""
    import json, re, requests as req

    # Step 1: Search Google via SerpAPI for the PD's records request page
    queries = [
        f"{police_dept} {state} public records request body camera FOIA email",
        f"{police_dept} {state} records custodian FOIA portal",
    ]
    all_snippets = []
    all_links = []

    for query in queries:
        try:
            resp = req.get("https://serpapi.com/search", params={
                "q": query, "api_key": serpapi_key, "num": 5,
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for r in data.get("organic_results", [])[:5]:
                    snippet = f"Title: {r.get('title', '')} | URL: {r.get('link', '')} | Snippet: {r.get('snippet', '')}"
                    all_snippets.append(snippet)
                    all_links.append(r.get("link", ""))
        except Exception:
            pass

    if not all_snippets:
        # Fallback to Claude's knowledge only
        return _search_pd_contact_ai_only(police_dept, state, anthropic_key)

    # Step 2: Have Claude analyze the search results
    client = anthropic.Anthropic(api_key=anthropic_key)
    search_text = "\n".join(all_snippets[:10])

    prompt = f"""I need to find how to submit a FOIA / public records request for body-worn camera footage to: {police_dept}, {state}

Here are Google search results:
{search_text}

Based on these results, determine:
1. The BEST method to submit a records request (email is preferred if available, as it's automatable)
2. The exact email address for records/FOIA requests
3. If they use an online portal (GovQA, NextRequest, JustFOIA), provide the URL
4. Any important notes (fees, turnaround time, specific form requirements)

IMPORTANT:
- If you find an email, prefer that as the method (even if they also have a portal)
- Extract REAL email addresses and URLs from the search results, don't guess
- If no email found in results, check if common patterns apply (records@city.gov, etc.)

Return ONLY valid JSON:
{{"method": "email" or "portal" or "both", "email": "exact email address or empty", "portal_url": "exact URL or empty", "portal_type": "govqa/nextrequest/justfoia/other/none", "notes": "important details about their process", "confidence": "high/medium/low"}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    if match:
        result = json.loads(match.group())
        result["search_links"] = all_links[:3]
        return result
    return {"method": "email", "email": "", "portal_url": "", "notes": "Could not determine", "confidence": "low"}


def _search_pd_contact_ai_only(police_dept: str, state: str, anthropic_key: str) -> dict:
    """Fallback: use Claude's knowledge only when web search fails."""
    import json, re
    client = anthropic.Anthropic(api_key=anthropic_key)

    prompt = f"""I need the records request contact for: {police_dept}, {state}

Provide your best knowledge about:
1. Their email for FOIA/records requests
2. Whether they use GovQA, NextRequest, JustFOIA, or another portal
3. The portal URL if known

Return ONLY valid JSON:
{{"method": "email" or "portal" or "both", "email": "best guess email", "portal_url": "URL or empty", "portal_type": "govqa/nextrequest/justfoia/other/none", "notes": "any info", "confidence": "low"}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    if match:
        return json.loads(match.group())
    return {"method": "email", "email": "", "portal_url": "", "notes": "Could not determine", "confidence": "low"}


def process_single_request(
    article: dict,
    article_text: str,
    sender_name: str,
    anthropic_key: str,
    serpapi_key: str,
    foia_email: str,
    foia_email_password: str,
    pd_db: list,
    service,
    foia_sheet_id: str,
) -> dict:
    """Fully automated: find contact, generate letter, send it. Returns status dict."""
    from pd_database import lookup_department, add_department
    import time

    dept_name = article["police_dept"]
    state = article["state"]
    result = {"article": article, "status": "pending", "details": ""}

    # Step 1: Find department contact
    pd_match = lookup_department(pd_db, dept_name, state)
    if not pd_match or not (pd_match.get("Email Address") or pd_match.get("Portal URL")):
        # Auto-search for contact info
        contact_info = search_pd_contact_web(dept_name, state, serpapi_key, anthropic_key)

        if contact_info.get("email") or contact_info.get("portal_url"):
            # Auto-add to database
            add_department(service, foia_sheet_id, {
                "name": dept_name,
                "state": state,
                "method": contact_info.get("method", "email"),
                "email": contact_info.get("email", ""),
                "portal_url": contact_info.get("portal_url", ""),
                "portal_type": contact_info.get("portal_type", ""),
                "notes": contact_info.get("notes", ""),
            })
            pd_match = {
                "Department Name": dept_name,
                "State": state,
                "Method": contact_info.get("method", "email"),
                "Email Address": contact_info.get("email", ""),
                "Portal URL": contact_info.get("portal_url", ""),
            }
        else:
            result["status"] = "failed"
            result["details"] = f"Could not find contact info for {dept_name}, {state}"
            return result

    # Step 2: Generate FOIA letter
    if article_text:
        letter = generate_foia_request_ai(
            article_text=article_text,
            suspect_name=article["suspect_name"],
            incident_date=article["incident_date"],
            police_dept=dept_name,
            state=state,
            sender_name=sender_name,
            anthropic_key=anthropic_key,
        )
    else:
        letter = generate_foia_request_simple(
            suspect_name=article["suspect_name"],
            incident_date=article["incident_date"],
            police_dept=dept_name,
            state=state,
            sender_name=sender_name,
        )

    today = datetime.now().strftime("%Y-%m-%d")
    method = pd_match.get("Method", "email")
    email_addr = pd_match.get("Email Address", "")
    portal_url = pd_match.get("Portal URL", "")

    # Step 3: Send via best available method
    if email_addr and method in ("email", "both"):
        # Send via email
        send_result = send_email_smtp(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            email=foia_email,
            password=foia_email_password,
            to_addr=email_addr,
            subject=letter["subject"],
            body=letter["body"],
        )
        if send_result["success"]:
            from sheets_client import write_foia_request
            write_foia_request(service, foia_sheet_id, {
                "Request ID": generate_request_id(),
                "Article URL": article["url"],
                "Suspect Name": article["suspect_name"],
                "Incident Date": article["incident_date"],
                "Police Department": dept_name,
                "State": state,
                "FOIA Score": str(article.get("foia_score", "")),
                "Request Method": "email",
                "Contact Info": email_addr,
                "Status": "Sent",
                "Date Created": today,
                "Date Sent": today,
                "Last Follow-Up": "",
                "Follow-Up Count": "0",
                "Notes": "",
                "Request Body": letter["body"],
            })
            result["status"] = "sent"
            result["details"] = f"Emailed to {email_addr}"
            result["method"] = "email"
            result["letter"] = letter
            time.sleep(1)  # Rate limit between emails
        else:
            result["status"] = "failed"
            result["details"] = f"Email failed: {send_result['error']}"
    elif portal_url:
        # Can't auto-submit to portal — save as draft with portal info
        from sheets_client import write_foia_request
        write_foia_request(service, foia_sheet_id, {
            "Request ID": generate_request_id(),
            "Article URL": article["url"],
            "Suspect Name": article["suspect_name"],
            "Incident Date": article["incident_date"],
            "Police Department": dept_name,
            "State": state,
            "FOIA Score": str(article.get("foia_score", "")),
            "Request Method": "portal",
            "Contact Info": portal_url,
            "Status": "Draft",
            "Date Created": today,
            "Date Sent": "",
            "Last Follow-Up": "",
            "Follow-Up Count": "0",
            "Notes": f"Submit via portal: {portal_url}",
            "Request Body": letter["body"],
        })
        result["status"] = "portal_draft"
        result["details"] = f"Portal-only: {portal_url} — letter saved as draft"
        result["method"] = "portal"
        result["portal_url"] = portal_url
        result["letter"] = letter
    else:
        result["status"] = "failed"
        result["details"] = f"No email or portal found for {dept_name}"

    return result


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
