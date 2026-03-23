"""FOIA bodycam footage request system — template generation, email, follow-ups."""

import smtplib
import ssl
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import json
import logging
import time

import anthropic

logger = logging.getLogger(__name__)


def _extract_json(raw: str, required_fields: list = None) -> "dict | None":
    """Extract the first valid JSON object from raw text using balanced-brace scanning.

    More resilient than a simple regex: handles nested braces, trailing text, and
    common LLM artifacts like markdown code fences or leading explanatory sentences.
    """
    if not raw:
        logger.warning("_extract_json received empty string")
        return None

    raw = raw.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        # Drop the opening fence line and any closing fence
        inner_lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        raw = "\n".join(inner_lines).strip()

    # Fast path: entire string is valid JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            if not required_fields or all(f in obj for f in required_fields):
                return obj
        logger.debug("_extract_json: top-level JSON parsed but missing required fields")
    except json.JSONDecodeError:
        pass

    # Balanced-brace scan to find the first JSON object
    depth = 0
    start = None
    for i, ch in enumerate(raw):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = raw[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        if not required_fields or all(f in obj for f in required_fields):
                            return obj
                        else:
                            missing = [f for f in (required_fields or []) if f not in obj]
                            logger.debug("_extract_json: found JSON object missing fields: %s", missing)
                except json.JSONDecodeError as exc:
                    logger.debug("_extract_json: JSON parse error at brace pair [%d:%d]: %s", start, i + 1, exc)
                start = None

    logger.warning("_extract_json: no valid JSON object found in %d-char string", len(raw))
    return None


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


def _verify_foia_letter_completeness(result: dict) -> dict:
    """Check a generated FOIA letter result for missing critical fields.

    Adds a 'warnings' key listing any critical omissions found.  Does NOT
    modify the letter content — the caller decides how to surface these.
    """
    warnings = []

    body = result.get("body", "")
    body_lower = body.lower()

    # --- Date check ---
    incident_date = result.get("incident_date", "").strip()
    if not incident_date or incident_date.lower() in ("unknown", ""):
        warnings.append("incident_date is missing or unknown")
    # Also confirm the date appears somewhere in the letter body
    if incident_date and incident_date.lower() not in ("unknown", "") and incident_date not in body:
        warnings.append(f"incident_date '{incident_date}' does not appear in letter body")

    # --- Time check ---
    incident_time = result.get("incident_time", "").strip()
    if not incident_time or incident_time.lower() in ("unknown", "not stated", ""):
        warnings.append("incident_time is missing or unknown — letter may be rejected or delayed")
    # Look for time-like patterns in the body (e.g. "3:45 PM", "morning", "evening")
    import re as _re_check
    time_pattern = _re_check.compile(
        r"\b(\d{1,2}:\d{2}\s*(am|pm|AM|PM)?|morning|afternoon|evening|night|midnight|noon)\b"
    )
    if not time_pattern.search(body):
        warnings.append("no time reference found in letter body")

    # --- Location check ---
    incident_location = result.get("incident_location", "").strip()
    if not incident_location or incident_location.lower() in ("unknown", ""):
        warnings.append("incident_location is missing or unknown — letter may be rejected or delayed")
    # A bare "Unknown" in the body location line is also a problem
    if "location: unknown" in body_lower:
        warnings.append("location field in letter body reads 'Unknown'")

    # --- Timeframe check ---
    timeframe = result.get("timeframe", "").strip()
    if not timeframe or timeframe.lower() in ("unknown", ""):
        warnings.append("timeframe for footage requested is missing")

    if warnings:
        logger.warning(
            "generate_foia_request_ai completeness check found %d issue(s): %s",
            len(warnings), "; ".join(warnings),
        )

    result["warnings"] = warnings
    return result


def generate_foia_request_ai(
    article_text: str,
    suspect_name: str,
    incident_date: str,
    police_dept: str,
    state: str,
    sender_name: str,
    anthropic_key: str,
) -> dict:
    """Use Claude to generate a detailed, case-specific FOIA request with structured fields.

    Returns a dict with at minimum 'subject' and 'body'.  Also includes extracted
    metadata fields (incident_date, incident_time, incident_location, case_number,
    officer_names, charges, victim_name, timeframe) and a 'warnings' list that
    flags any critical omissions detected in the generated letter.
    """
    import json as _json
    import re as _re
    client = anthropic.Anthropic(api_key=anthropic_key)
    logger.info(
        "generate_foia_request_ai: generating letter for %s / %s / %s",
        suspect_name, police_dept, incident_date,
    )

    prompt = f"""You are writing a FOIA / public records request for body-worn camera footage.

ARTICLE ABOUT THE INCIDENT:
{article_text[:8000]}

KNOWN DETAILS:
- Suspect: {suspect_name}
- Date: {incident_date}
- Police Department: {police_dept}
- State: {state}

STEP 1: Extract ALL available details from the article. Be thorough — missing details cause requests to be rejected.
Pay special attention to:
  • EXACT TIME — scan for phrases like "around 3 PM", "late Tuesday night", "shortly after midnight", "at approximately 10:45 a.m."
  • EXACT LOCATION — scan for street addresses, intersections, named businesses, landmarks, neighborhoods, or block ranges.
  • CASE / REPORT NUMBERS — look for patterns like "case #", "report number", "incident no.", or any numeric ID in police quotes.
  • OFFICER NAMES — look in direct quotes from the department, arrest records, or bylines that credit officers.

STEP 2: Write a professional, detailed FOIA request letter. The letter MUST include:
- Exact date of incident
- Time of incident (exact time like "3:45 PM" if stated; if only implied, use the best estimate with a qualifier
  like "approximately 10:00 PM" or "early morning hours"; NEVER leave blank)
- Specific location (street address, intersection, or business name from the article; NEVER leave blank)
- Case/incident/report number if mentioned anywhere in the article
- Names of officers involved if mentioned
- Specific charges filed
- Victim name if mentioned
- A clear timeframe for the footage requested (e.g. "2:00 PM to 4:00 PM on March 15, 2026")

STEP 3: Return a JSON object with these fields:

{{
  "subject": "Public Records Request – Body-Worn Camera Footage – {suspect_name} – {incident_date}",
  "body": "The full letter text (see format below)",
  "incident_date": "exact date extracted from article",
  "incident_time": "exact or estimated time — NEVER 'Unknown' unless article provides zero time context",
  "incident_location": "most specific location from article — street, intersection, or business name",
  "case_number": "case/report number if mentioned, or empty string",
  "officer_names": "officer names if mentioned, or 'Not identified in article'",
  "charges": "specific charges listed in the article",
  "victim_name": "victim name if mentioned, or empty string",
  "timeframe": "footage timeframe like '2:00 PM – 4:00 PM on March 15, 2026'"
}}

LETTER FORMAT (for the "body" field):
---
[Today's Date]

Records Custodian
{police_dept}
{state}

Re: Public Records Request — Body-Worn Camera Footage and Incident Report

Dear Records Custodian,

Pursuant to [state] public records law, I am formally requesting copies of body-worn camera (BWC) footage and the associated incident report for the following incident:

  Date of Incident:          [exact date]
  Approximate Time:          [time or best estimate — NEVER omit; always include a qualifier if estimated]
  Location:                  [specific street address, intersection, or business name — NEVER omit]
  Incident Report/Case No.:  [number if known, otherwise "Not available — please cross-reference using the details below"]
  Suspect(s):                [full name(s)]
  Victim(s):                 [name(s) if known, otherwise omit this line]
  Charges:                   [specific charges from article]
  Involved Officer(s):       [names if known, otherwise "All responding officers"]

Incident Summary:
[4-5 detailed sentences summarizing what happened: the call for service or triggering event, the officers' response,
the key actions taken, the arrest or outcome, and any charges filed. Draw directly from the article — be factual
and specific.]

Records Requested:
1. All body-worn camera (BWC) recordings from every officer who responded to or was present at this incident,
   covering the approximate timeframe of [footage start time] to [footage end time] on [date].
2. Dashboard camera (dash-cam) recordings from all responding patrol units.
3. The incident/offense report and any supplements filed in connection with this event.
4. Computer-Aided Dispatch (CAD) logs and dispatch audio for this call.
5. Any arrest report, booking photograph, or use-of-force report associated with this incident.

Please provide records in their native digital format where possible. Electronic delivery is preferred (secure
email, cloud link, or file transfer service). If the estimated cost of fulfilling this request exceeds $200,
please notify me before processing so I can refine or prioritize the request.

If any portion of this request is denied, please provide a written explanation citing the specific statutory
exemption(s) relied upon, and release all non-exempt portions.

I look forward to your response within the time period required by [state] law.

Thank you for your prompt attention to this matter.

Sincerely,
{sender_name}
---

CRITICAL RULES:
- Use ONLY facts from the article. NEVER invent names, numbers, or locations.
- NEVER leave Date, Time, or Location blank. If time is not explicit, reason it out from context (e.g. if the
  article says "Tuesday night" and was published Wednesday March 19, 2026, then the incident was Tuesday
  March 18 and time was "night, approximately 8:00 PM – midnight").
- Fill in [state] with the actual state name in the letter body.
- Fill in today's date as the letter date.
- Be specific and detailed — vague requests are rejected or substantially delayed.
- Return ONLY the JSON object. No preamble, no markdown fences, no extra text."""

    logger.debug("generate_foia_request_ai: sending prompt to Claude (%d chars)", len(prompt))
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    logger.debug("generate_foia_request_ai: received %d-char response", len(raw))

    # Use resilient _extract_json to parse structured result
    required = ["subject", "body"]
    result = _extract_json(raw, required_fields=required)

    if result and "subject" in result and "body" in result:
        logger.info("generate_foia_request_ai: successfully parsed JSON response")
        return _verify_foia_letter_completeness(result)

    # Fallback: treat the whole response as the letter body
    logger.warning(
        "generate_foia_request_ai: JSON parse failed — falling back to raw text body"
    )
    subject = f"Public Records Request – Body-Worn Camera Footage – {suspect_name} – {incident_date}"
    fallback = {"subject": subject, "body": raw}
    return _verify_foia_letter_completeness(fallback)


def _verify_url(url: str, max_redirects: int = 3, require_records_content: bool = True) -> bool:
    """Check if a URL is live, not a generic vendor redirect, and (optionally) contains
    records-portal keywords.

    Parameters
    ----------
    url:
        The URL to verify.
    max_redirects:
        How many redirect hops to follow before giving up.  Defaults to 3 so we
        chase short-URL chains without following infinite loops.
    require_records_content:
        When True (default), perform a GET request and check that the final page
        body contains at least one keyword indicating it is a records/FOIA portal.
        Set to False if you only need a liveness check.
    """
    import requests as req

    if not url or not url.startswith("http"):
        logger.debug("_verify_url: rejected '%s' — not an http(s) URL", url)
        return False

    # Known generic vendor marketing/home pages that are NOT departmental portals
    bad_redirects = [
        "civicplus.com",
        "govqa.us/home",
        "govqa.us/landing",
        "govqa.us/register",
        "nextrequest.com/about",
        "nextrequest.com/pricing",
        "nextrequest.com/home",
        "justfoia.com/about",
        "justfoia.com/pricing",
        "justfoia.com/home",
        "muckrock.com/about",
        "foiaonline.gov/about",
        "efoia.com/home",
        "records.management.about",
    ]

    # Keywords that should appear on a legitimate records request portal page
    records_keywords = [
        "public records", "records request", "foia", "open records",
        "submit a request", "make a request", "request records",
        "body camera", "body worn", "bodycam",
    ]

    try:
        # First do a HEAD request (fast) to follow redirects and check status
        head_resp = req.head(
            url, timeout=8, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FOIABot/1.0)"},
        )
        final_url = head_resp.url.lower()

        if head_resp.status_code >= 400:
            logger.debug(
                "_verify_url: '%s' returned HTTP %d after redirects", url, head_resp.status_code
            )
            return False

        for bad in bad_redirects:
            if bad in final_url:
                logger.debug(
                    "_verify_url: '%s' redirected to bad vendor URL '%s'", url, final_url
                )
                return False

        if not require_records_content:
            return True

        # GET the final page to inspect content for records-portal keywords
        get_resp = req.get(
            head_resp.url, timeout=10, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FOIABot/1.0)"},
        )
        page_text = get_resp.text.lower()
        matched = [kw for kw in records_keywords if kw in page_text]
        if matched:
            logger.debug("_verify_url: '%s' verified — matched keywords: %s", url, matched)
            return True
        else:
            logger.debug(
                "_verify_url: '%s' live but no records keywords found on page", url
            )
            # No records keywords = not a real portal (probably city homepage)
            logger.info("_verify_url: '%s' live but no records keywords — rejecting as portal", url)
            return False

    except req.exceptions.Timeout:
        logger.warning("_verify_url: timeout verifying '%s'", url)
        return False
    except req.exceptions.TooManyRedirects:
        logger.warning("_verify_url: too many redirects for '%s'", url)
        return False
    except Exception as exc:
        logger.debug("_verify_url: exception for '%s': %s", url, exc)
        return False


def _verify_email_domain(email: str) -> bool:
    """Check if the email domain exists by resolving its MX records (preferred) or A records.

    Uses proper MX lookup first — a domain with MX records is set up to receive email.
    Falls back to A record only if MX lookup isn't available.
    """
    if not email or "@" not in email:
        logger.debug("_verify_email_domain: invalid email format '%s'", email)
        return False
    domain = email.split("@")[1].lower().strip()
    # Try MX lookup first (most reliable indicator of email capability)
    try:
        import subprocess
        mx_result = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=5,
        )
        if mx_result.returncode == 0 and mx_result.stdout.strip():
            logger.debug("_verify_email_domain: domain '%s' has MX records", domain)
            return True
    except Exception:
        pass
    # Fallback to A record resolution
    try:
        import socket
        socket.getaddrinfo(domain, 25, socket.AF_INET, socket.SOCK_STREAM)
        logger.debug("_verify_email_domain: domain '%s' resolved via A record", domain)
        return True
    except (socket.gaierror, OSError) as exc:
        logger.debug("_verify_email_domain: domain '%s' did not resolve: %s", domain, exc)
        return False


def _verify_mailbox(email_addr: str, timeout: int = 10) -> bool:
    """Check if a specific mailbox exists via SMTP RCPT TO (no email sent).

    Connects to the recipient's mail server and checks if the mailbox is valid.
    Returns True if the server accepts the recipient, False if it rejects.
    Returns True (optimistic) if the server doesn't support verification.
    """
    import socket
    import subprocess
    if not email_addr or "@" not in email_addr:
        return False
    domain = email_addr.split("@")[1].lower().strip()
    logger.info("_verify_mailbox: checking %s", email_addr)

    # Step 1: Get MX server for the domain
    mx_host = None
    try:
        mx_result = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=5,
        )
        if mx_result.returncode == 0 and mx_result.stdout.strip():
            # MX records format: "10 mail.example.com." — take lowest priority
            mx_lines = [l.strip() for l in mx_result.stdout.strip().split("\n") if l.strip()]
            mx_lines.sort(key=lambda x: int(x.split()[0]) if x.split()[0].isdigit() else 999)
            if mx_lines:
                mx_host = mx_lines[0].split()[-1].rstrip(".")
    except Exception as e:
        logger.debug("_verify_mailbox: MX lookup failed for %s: %s", domain, e)

    if not mx_host:
        logger.debug("_verify_mailbox: no MX found for %s — falling back to domain", domain)
        mx_host = domain

    # Step 2: Connect to MX server and try RCPT TO
    try:
        import smtplib
        smtp = smtplib.SMTP(timeout=timeout)
        smtp.connect(mx_host, 25)
        smtp.ehlo("verify.local")
        smtp.mail("")
        code, msg = smtp.rcpt(email_addr)
        smtp.quit()

        if code == 250:
            logger.info("_verify_mailbox: %s ACCEPTED (code %d)", email_addr, code)
            return True
        elif code in (550, 551, 552, 553, 554):
            logger.warning("_verify_mailbox: %s REJECTED (code %d: %s)", email_addr, code, msg.decode(errors="replace"))
            return False
        else:
            # Unknown response — assume valid (optimistic)
            logger.info("_verify_mailbox: %s unknown response (code %d) — assuming valid", email_addr, code)
            return True
    except smtplib.SMTPServerDisconnected:
        # Server disconnected — may block VRFY/RCPT. Assume valid.
        logger.debug("_verify_mailbox: %s — server disconnected, assuming valid", email_addr)
        return True
    except (socket.timeout, socket.gaierror, OSError, smtplib.SMTPException) as e:
        # Connection failed — can't verify. Assume valid (optimistic).
        logger.debug("_verify_mailbox: %s — connection failed: %s, assuming valid", email_addr, e)
        return True


def _email_tld_confidence(email: str) -> str:
    """Return 'high', 'medium', or 'low' based on the email domain TLD.

    Government (.gov, .us) domains are treated as high confidence because they
    require verified institutional registration.  Common organizational TLDs
    (.org, .net, .edu) are medium.  Everything else is low.
    """
    if not email or "@" not in email:
        return "low"
    domain = email.split("@")[1].lower()
    if domain.endswith(".gov") or domain.endswith(".us"):
        return "high"
    if domain.endswith(".org") or domain.endswith(".net") or domain.endswith(".edu"):
        return "medium"
    return "low"


def _extract_city_slug(police_dept: str) -> str:
    """Extract a city slug from a police department name for domain guessing.

    Returns the locality name (city/town/county) stripped of department type.
    """
    slug = police_dept.lower()
    for suffix in [
        "police department", "police dept", "police dept.", "pd",
        "sheriff's office", "sheriffs office", "sheriff office", "sheriff",
        "department of police", "dept of police",
        "metropolitan police", "city of", "town of",
        "county", "parish",
    ]:
        slug = slug.replace(suffix, "")
    return slug.strip().replace(" ", "")


def _run_serp_queries(queries: list, serpapi_key: str, num_per_query: int = 8) -> tuple:
    """Run SerpAPI queries and collect snippets + links.

    Returns (all_snippets, all_links).  Logs failures per-query instead of
    silently swallowing them.
    """
    import requests as req
    all_snippets = []
    all_links = []
    for query in queries:
        try:
            logger.debug("_run_serp_queries: querying SerpAPI: %s", query)
            resp = req.get("https://serpapi.com/search", params={
                "q": query, "api_key": serpapi_key, "num": num_per_query,
            }, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("organic_results", [])[:num_per_query]
                logger.debug("_run_serp_queries: got %d results for query: %s", len(results), query)
                for r in results:
                    snippet = (
                        f"Title: {r.get('title', '')} | "
                        f"URL: {r.get('link', '')} | "
                        f"Snippet: {r.get('snippet', '')}"
                    )
                    all_snippets.append(snippet)
                    all_links.append(r.get("link", ""))
            else:
                logger.warning(
                    "_run_serp_queries: SerpAPI returned HTTP %d for query: %s",
                    resp.status_code, query,
                )
        except Exception as exc:
            logger.warning("_run_serp_queries: exception for query '%s': %s", query, exc)
    logger.info(
        "_run_serp_queries: collected %d snippets from %d queries",
        len(all_snippets), len(queries),
    )
    return all_snippets, all_links


def _analyze_search_results(police_dept: str, state: str, search_text: str, anthropic_key: str) -> "dict | None":
    """Have Claude analyze search results to extract PD contact info.

    Returns a parsed dict or None if extraction fails.
    """
    import json, re
    client = anthropic.Anthropic(api_key=anthropic_key)
    logger.debug("_analyze_search_results: analyzing results for %s, %s", police_dept, state)

    prompt = f"""I need to find how to submit a FOIA / public records request for body-worn camera footage to: {police_dept}, {state}

Here are Google search results:
{search_text}

Based on these results, determine:
1. The BEST method to submit a records request. ALWAYS try to find an email address, even if a portal exists — most departments that run portals also have a records custodian email. Email is strongly preferred as it's automatable and costs nothing.
2. The exact email address for records/FOIA requests — look for titles like "Records Custodian", "FOIA Officer",
   "Public Records Coordinator", or email patterns like records@, foia@, publicrecords@, openrecords@
3. If they use an online portal (GovQA, NextRequest, JustFOIA, eFOIA, MuckRock, PublicRecordsCenter), provide the
   full portal URL including path (not just the domain)
4. Any important notes (fees, turnaround time, specific form requirements, mailing address if no email found)

IMPORTANT:
- Prefer .gov or .us email domains — these are authoritative government addresses
- Extract REAL email addresses visible in snippets; look for "name@domain.gov" patterns
- Look at every URL in the results — if you see a .gov or .us domain pointing to a records/FOIA page, that is
  the department's official records page; extract the full URL
- CRITICAL: Match the email domain to the SPECIFIC department being searched:
  * If searching for "Bozeman Police Department", the email MUST be from bozeman.gov (city domain), NOT gallatinmt.gov (county domain)
  * If searching for "Gallatin County Sheriff", the email should be from the COUNTY domain, NOT a city domain
  * City police → city domain (e.g., cityname.gov). County sheriff → county domain (e.g., countyname.gov or countynamecounty.gov)
  * NEVER use a county/state domain email for a city police department request, or vice versa
- If you see a city/county domain (e.g. cityname.gov), suggest likely records emails ONLY if it matches the department:
  records@cityname.gov, foia@cityname.gov, publicrecords@cityname.gov
- Distinguish between an official departmental portal URL (e.g. police.cityname.gov/records) and a generic
  vendor marketing page (e.g. nextrequest.com/about) — only report the former as the portal_url
- Set confidence to "high" only if you found a real email or portal URL directly in the search results
- Set confidence to "medium" if you are inferring an email from a domain you saw in results
- Set confidence to "low" if you are guessing

Return ONLY valid JSON (no markdown, no extra text):
{{"method": "email" or "portal" or "both", "email": "exact email address or empty string", "portal_url": "exact full URL or empty string", "portal_type": "govqa/nextrequest/justfoia/efoia/muckrock/other/none", "notes": "important details including mailing address if found", "confidence": "high/medium/low", "official_website": "PD official website domain if identified, or empty string"}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        result = _extract_json(raw, required_fields=["method", "confidence"])
        if result:
            logger.debug(
                "_analyze_search_results: extracted contact info — method=%s, confidence=%s, email=%s",
                result.get("method"), result.get("confidence"), result.get("email", ""),
            )
            return result
        logger.warning("_analyze_search_results: failed to parse JSON from Claude response")
        return None
    except Exception as exc:
        logger.error("_analyze_search_results: exception calling Claude: %s", exc)
        return None


def search_pd_contact_web(police_dept: str, state: str, serpapi_key: str, anthropic_key: str) -> dict:
    """Search the web for a PD's FOIA contact info, then have Claude extract it.

    Strategy:
      1. Run a broad set of primary queries (official site, records custodian title,
         known portal vendors, body-cam records).
      2. Have Claude extract the best email / portal URL from combined results.
      3. Validate the portal URL (follow redirects up to 3 levels, check for records
         keywords, reject generic vendor marketing pages).
      4. Validate the email domain exists; flag .gov/.us addresses as high confidence.
      5. If confidence is still low, run a second targeted search round.
      6. Fall back to pattern-based email suggestions if nothing concrete was found.
    """
    import json, re

    city_slug = _extract_city_slug(police_dept)
    logger.info("search_pd_contact_web: searching for contact — %s, %s (slug=%s)", police_dept, state, city_slug)

    # Step 1: Primary search — 4 high-value queries (reduced from 9 to save SerpAPI cost)
    primary_queries = [
        # Official city/county website — covers .gov and .us in one query
        f'site:{city_slug}.gov OR site:{city_slug}.us "public records" OR "FOIA" OR "records request"',
        # Records custodian / FOIA officer direct email
        f'"{police_dept}" {state} "records custodian" OR "FOIA officer" OR "public records officer" email',
        # Known portal vendors
        f'"{police_dept}" {state} GovQA OR NextRequest OR JustFOIA OR eFOIA OR PublicRecordsCenter',
        # Body-cam specific records request
        f'"{police_dept}" {state} body camera records request email OR portal',
    ]
    all_snippets, all_links = _run_serp_queries(primary_queries, serpapi_key, num_per_query=8)

    if not all_snippets:
        logger.warning("search_pd_contact_web: no search results — falling back to AI-only mode")
        return _search_pd_contact_ai_only(police_dept, state, anthropic_key)

    # Step 2: Have Claude analyze the search results
    search_text = "\n".join(all_snippets[:25])
    result = _analyze_search_results(police_dept, state, search_text, anthropic_key)

    if not result:
        logger.warning("search_pd_contact_web: Claude analysis failed — falling back to AI-only mode")
        return _search_pd_contact_ai_only(police_dept, state, anthropic_key)

    result["search_links"] = all_links[:5]

    # Step 2b: Verify portal URL — follow redirects, check content, reject vendor pages
    if result.get("portal_url"):
        portal_url = result["portal_url"]
        logger.info("search_pd_contact_web: verifying portal URL: %s", portal_url)
        if not _verify_url(portal_url, max_redirects=3, require_records_content=True):
            logger.warning("search_pd_contact_web: portal URL invalid or vendor page: %s", portal_url)
            if not result.get("notes"):
                result["notes"] = ""
            result["notes"] = (
                result["notes"]
                + f" Portal URL {portal_url} was unreachable or redirected to a vendor marketing page."
            ).strip()
            result["portal_url"] = ""
            if result.get("method") in ("portal", "both"):
                result["method"] = "email" if result.get("email") else "unknown"
        else:
            logger.info("search_pd_contact_web: portal URL verified OK: %s", portal_url)

    # Step 2c: Validate email — domain resolution + TLD confidence + department match
    if result.get("email"):
        email = result["email"]
        tld_conf = _email_tld_confidence(email)
        logger.debug("search_pd_contact_web: email '%s' TLD confidence: %s", email, tld_conf)

        # Cross-check: does the email domain relate to the department?
        email_domain = email.split("@")[1].lower() if "@" in email else ""
        dept_lower = police_dept.lower()
        is_county_dept = "county" in dept_lower or "parish" in dept_lower
        # For city PDs, reject emails from county/state domains that don't contain city name
        if city_slug and email_domain and not is_county_dept:
            domain_has_city = city_slug in email_domain.replace(".", "")
            if not domain_has_city and tld_conf == "high":
                # .gov domain that doesn't match city name — likely wrong jurisdiction
                logger.warning(
                    "search_pd_contact_web: email domain '%s' does not contain city slug '%s' — "
                    "possible wrong jurisdiction, downgrading to suggestion",
                    email_domain, city_slug,
                )
                if not result.get("notes"):
                    result["notes"] = ""
                result["notes"] = (
                    result["notes"]
                    + f" WARNING: {email} may be wrong jurisdiction (domain doesn't match {city_slug}). Verify manually."
                ).strip()
                result["confidence"] = "low"
                result["email"] = ""  # Don't auto-send to wrong jurisdiction
                result["method"] = "portal" if result.get("portal_url") else "unknown"

        if result.get("email"):  # re-check — may have been cleared above
            if not _verify_email_domain(email):
                logger.warning("search_pd_contact_web: email domain unresolvable for: %s", email)
                if not result.get("notes"):
                    result["notes"] = ""
                result["notes"] = (
                    result["notes"] + f" Email domain for {email} could not be verified via DNS."
                ).strip()
            else:
                # Upgrade confidence if the email is on a .gov or .us domain
                if tld_conf == "high" and result.get("confidence") in ("medium", "low"):
                    logger.info(
                        "search_pd_contact_web: upgrading confidence to 'high' — .gov/.us email verified: %s", email
                    )
                    result["confidence"] = "high"
                result.setdefault("email_tld_confidence", tld_conf)

    # Step 3: If no email found, ALWAYS run email-targeted second-round search
    # Email is preferred over portal (free vs $2-5/submission for Kevin's browser agent)
    if not result.get("email"):
        logger.info("search_pd_contact_web: no email found — running email-targeted second-round queries")
        alt_queries = [
            f'"{police_dept}" records department email "@"',
            f'{city_slug} {state} police department "records@" OR "foia@" OR "publicrecords@"',
            f'"{police_dept}" {state} body worn camera request submit email',
        ]
        alt_snippets, alt_links = _run_serp_queries(alt_queries, serpapi_key, num_per_query=8)
        if alt_snippets:
            combined_text = search_text + "\n" + "\n".join(alt_snippets[:12])
            retry_result = _analyze_search_results(police_dept, state, combined_text, anthropic_key)
            if retry_result and retry_result.get("email"):
                # Only adopt retry if it found an email — that's the whole point
                logger.info(
                    "search_pd_contact_web: second-round found email: %s",
                    retry_result.get("email", ""),
                )
                # Preserve portal from original result as fallback
                if not retry_result.get("portal_url") and result.get("portal_url"):
                    retry_result["portal_url"] = result["portal_url"]
                    retry_result["portal_type"] = result.get("portal_type", "")
                retry_result["search_links"] = (all_links + alt_links)[:5]
                result = retry_result
            else:
                logger.info("search_pd_contact_web: second-round did not find email")

    # Step 4: If still no email, try pattern-based emails with domain verification
    # .gov/.us domains require government registration — if MX resolves, it's legit
    # Only try patterns for city PDs — county sheriff domains are too unpredictable
    is_county = "county" in police_dept.lower() or "parish" in police_dept.lower()
    if not result.get("email") and result.get("confidence") != "high" and not is_county:
        suggested_emails = [
            f"records@{city_slug}.gov",
            f"publicrecords@{city_slug}.gov",
            f"foia@{city_slug}.gov",
            f"openrecords@{city_slug}.gov",
            f"police@{city_slug}.gov",
            f"records@{city_slug}.us",
            f"foia@{city_slug}.us",
        ]
        result["suggested_emails"] = suggested_emails

        # Try to verify and promote a .gov/.us pattern email
        # Check both domain (MX) AND mailbox (RCPT TO) to avoid sending to dead addresses
        verified_suggestion = None
        for candidate in suggested_emails:
            if _verify_email_domain(candidate) and _email_tld_confidence(candidate) == "high":
                if _verify_mailbox(candidate):
                    verified_suggestion = candidate
                    break
                else:
                    logger.info("search_pd_contact_web: %s domain OK but mailbox rejected", candidate)

        if verified_suggestion:
            logger.info(
                "search_pd_contact_web: promoted verified .gov/.us pattern email: %s",
                verified_suggestion,
            )
            result["email"] = verified_suggestion
            result["method"] = "both" if result.get("portal_url") else "email"
            result["confidence"] = "medium"
            if not result.get("notes"):
                result["notes"] = ""
            result["notes"] = (
                result["notes"] + f" Auto-verified pattern: {verified_suggestion}"
            ).strip()
        else:
            logger.info(
                "search_pd_contact_web: no pattern emails verified — %d suggestions in notes",
                len(suggested_emails),
            )
            if not result.get("notes"):
                result["notes"] = ""
            result["notes"] = (
                result["notes"] + f" Suggested (unverified): {', '.join(suggested_emails[:3])}"
            ).strip()
            result["confidence"] = "low"

    logger.info(
        "search_pd_contact_web: final result — method=%s, email=%s, portal=%s, confidence=%s",
        result.get("method"), result.get("email", ""), result.get("portal_url", ""), result.get("confidence"),
    )
    return result


def _search_pd_contact_ai_only(police_dept: str, state: str, anthropic_key: str) -> dict:
    """Fallback: use Claude's knowledge only when web search fails or returns no results."""
    import json, re
    client = anthropic.Anthropic(api_key=anthropic_key)
    logger.info("_search_pd_contact_ai_only: using AI-only mode for %s, %s", police_dept, state)

    prompt = f"""I need the records request contact for: {police_dept}, {state}

Provide your best knowledge about:
1. Their email for FOIA / public records requests (look for records custodian or FOIA officer email)
2. Whether they use GovQA, NextRequest, JustFOIA, eFOIA, or another online portal
3. The specific portal URL if known (full URL, not just domain)
4. Their official website domain if known

Note: Confidence must always be "low" for AI-only results since you cannot verify current information.

Return ONLY valid JSON (no markdown, no extra text):
{{"method": "email" or "portal" or "both", "email": "best guess email or empty string", "portal_url": "full URL or empty string", "portal_type": "govqa/nextrequest/justfoia/efoia/other/none", "notes": "any relevant info or caveats", "confidence": "low", "official_website": "domain if known or empty string"}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        result = _extract_json(raw, required_fields=["method"])
        if result:
            # Always force confidence to low for AI-only results
            result["confidence"] = "low"
            logger.info(
                "_search_pd_contact_ai_only: got result — email=%s, portal=%s",
                result.get("email", ""), result.get("portal_url", ""),
            )
            return result
        logger.warning("_search_pd_contact_ai_only: could not parse JSON from Claude response")
    except Exception as exc:
        logger.error("_search_pd_contact_ai_only: exception calling Claude: %s", exc)

    return {
        "method": "email",
        "email": "",
        "portal_url": "",
        "portal_type": "none",
        "notes": "Web search unavailable and AI knowledge lookup failed — manual research required.",
        "confidence": "low",
    }


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
    portal_credentials: dict = None,
) -> dict:
    """Fully automated: find contact, generate letter, send it. Returns status dict."""
    from pd_database import lookup_department, add_department
    import time

    dept_name = article["police_dept"]
    state = article["state"]
    result = {"article": article, "status": "pending", "details": ""}

    # Step 1: Find department contact
    pd_match = lookup_department(pd_db, dept_name, state)

    # Step 1b: If DB has portal but no email, try quick pattern verification
    # Email is free and instant; portal costs $2-5 via Kevin's browser agent
    # Skip for county/parish depts — domain patterns are unpredictable
    is_county = "county" in dept_name.lower() or "parish" in dept_name.lower()
    if pd_match and pd_match.get("Portal URL") and not pd_match.get("Email Address") and not is_county:
        city_slug = _extract_city_slug(dept_name)
        if city_slug:
            for candidate in [
                f"records@{city_slug}.gov",
                f"publicrecords@{city_slug}.gov",
                f"foia@{city_slug}.gov",
                f"records@{city_slug}.us",
            ]:
                if _verify_email_domain(candidate) and _email_tld_confidence(candidate) == "high":
                    logger.info(
                        "process_single_request: found verified email %s for portal-only dept %s",
                        candidate, dept_name,
                    )
                    pd_match["Email Address"] = candidate
                    pd_match["Method"] = "both"
                    break

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

    # Step 3: Send via best available method — always try email first (cheapest)
    # Even portal-only departments often accept email to their records custodian.
    # This saves $2-5/submission in Kevin's Opus tokens per avoided portal submission.

    # Verify mailbox before sending — catch invalid addresses before wasting a send
    if email_addr and not _verify_mailbox(email_addr):
        logger.warning("process_single_request: mailbox verification FAILED for %s — skipping email", email_addr)
        result["details"] = f"Email {email_addr} failed mailbox verification"
        email_addr = ""  # Fall through to portal/draft path

    if email_addr:
        # Send via email (preferred — no browser automation, no vision API cost)
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
    elif portal_url and not email_addr:
        # Queue for Kevin (OpenClaw) — he has AI browser agent + CAPTCHA solver
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
            "Status": "Portal Needed",
            "Date Created": today,
            "Date Sent": "",
            "Last Follow-Up": "",
            "Follow-Up Count": "0",
            "Notes": f"Queued for Kevin. Portal: {portal_url}",
            "Request Body": letter["body"],
        })
        result["status"] = "portal_draft"
        result["details"] = f"Portal request queued for Kevin. URL: {portal_url}"
        result["method"] = "portal"
        result["portal_url"] = portal_url
        result["letter"] = letter
    else:
        # No verified contact found — save as Draft for manual review
        guessed_email = _guess_pd_email(dept_name, state)
        if guessed_email:
            from sheets_client import write_foia_request
            write_foia_request(service, foia_sheet_id, {
                "Request ID": generate_request_id(),
                "Article URL": article["url"],
                "Suspect Name": article["suspect_name"],
                "Incident Date": article["incident_date"],
                "Police Department": dept_name,
                "State": state,
                "FOIA Score": str(article.get("foia_score", "")),
                "Request Method": "email (unverified)",
                "Contact Info": guessed_email,
                "Status": "Draft",
                "Date Created": today,
                "Date Sent": "",
                "Last Follow-Up": "",
                "Follow-Up Count": "0",
                "Notes": f"UNVERIFIED — guessed email, needs manual review before sending",
                "Request Body": letter["body"],
            })
            result["status"] = "draft"
            result["details"] = f"Draft saved — guessed email: {guessed_email} (needs verification)"
            result["method"] = "email"
            result["letter"] = letter
        else:
            from sheets_client import write_foia_request
            write_foia_request(service, foia_sheet_id, {
                "Request ID": generate_request_id(),
                "Article URL": article["url"],
                "Suspect Name": article["suspect_name"],
                "Incident Date": article["incident_date"],
                "Police Department": dept_name,
                "State": state,
                "FOIA Score": str(article.get("foia_score", "")),
                "Request Method": "unknown",
                "Contact Info": "",
                "Status": "Draft",
                "Date Created": today,
                "Date Sent": "",
                "Last Follow-Up": "",
                "Follow-Up Count": "0",
                "Notes": "No contact info found — needs manual lookup",
                "Request Body": letter["body"],
            })
            result["status"] = "draft"
            result["details"] = f"No contact found for {dept_name}. Saved as draft."

    return result


def _guess_pd_email(police_dept: str, state: str) -> str:
    """Guess the most likely records request email for a police department.

    Tries multiple common .gov/.us patterns and returns the first one with
    a verified domain (DNS MX/A record resolves). Returns empty if none verify.
    Skips county/parish departments — their domain patterns are too unpredictable.
    """
    # County domains are unpredictable (e.g., gallatinmt.gov, gallatincountymt.gov)
    if "county" in police_dept.lower() or "parish" in police_dept.lower():
        logger.debug("_guess_pd_email: skipping pattern guess for county/parish dept: %s", police_dept)
        return ""

    city = _extract_city_slug(police_dept)
    if not city:
        return ""

    # Ordered by likelihood — .gov is most common for government records
    candidates = [
        f"records@{city}.gov",
        f"publicrecords@{city}.gov",
        f"foia@{city}.gov",
        f"records@{city}.us",
        f"police@{city}.gov",
        f"records@{city}pd.org",
        f"records@{city}police.org",
    ]
    for email_candidate in candidates:
        if _verify_email_domain(email_candidate):
            tld_conf = _email_tld_confidence(email_candidate)
            if tld_conf in ("high", "medium"):
                logger.info("_guess_pd_email: verified pattern email: %s", email_candidate)
                return email_candidate

    return ""


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


def check_bounced_emails(foia_email: str, foia_password: str) -> "list[str]":
    """Check Gmail for bounced emails via IMAP. Returns list of bounced recipient addresses."""
    import imaplib
    import email as email_lib
    import re

    bounced_addrs = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(foia_email, foia_password)
        mail.select("INBOX")

        # Search for bounce notifications
        _, msg_nums = mail.search(None, '(OR FROM "mailer-daemon" FROM "postmaster")')
        if not msg_nums[0]:
            mail.logout()
            return []

        for num in msg_nums[0].split():
            _, data = mail.fetch(num, "(RFC822)")
            if not data or not data[0]:
                continue
            raw = data[0][1]
            msg = email_lib.message_from_bytes(raw)

            # Extract bounced recipient from body
            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body_text += part.get_payload(decode=True).decode(errors="replace")
            else:
                body_text = msg.get_payload(decode=True).decode(errors="replace")

            # Common patterns in bounce messages
            found = re.findall(r'(?:delivery.*?failed|undeliverable|rejected).*?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', body_text, re.IGNORECASE | re.DOTALL)
            if not found:
                # Try simpler: just find email addresses in the bounce
                found = re.findall(r'<([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)>', body_text)
                # Filter out our own email
                found = [e for e in found if e.lower() != foia_email.lower()]

            bounced_addrs.extend(found)

        mail.logout()
    except Exception as e:
        logger.error("check_bounced_emails: %s", e)

    # Deduplicate
    return list(set(addr.lower() for addr in bounced_addrs))


def get_requests_needing_followup(
    requests: "list[dict]", sent_days: int = 10, progress_days: int = 14
) -> "list[dict]":
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
