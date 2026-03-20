"""AI-powered article analysis using Claude."""

import anthropic
import json
import logging
import re
import time

logger = logging.getLogger(__name__)


def _extract_json(raw: str, required_fields: list = None) -> dict | None:
    """Extract the first valid JSON object from raw text using balanced-brace scanning."""
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            if not required_fields or all(f in obj for f in required_fields):
                return obj
    except json.JSONDecodeError:
        pass
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
                try:
                    obj = json.loads(raw[start:i + 1])
                    if isinstance(obj, dict):
                        if not required_fields or all(f in obj for f in required_fields):
                            return obj
                except json.JSONDecodeError:
                    pass
                start = None
    return None


EXCLUDED_STATES = {
    "alabama", "arkansas", "delaware", "kansas", "kentucky",
    "maine", "missouri", "minnesota", "virginia", "tennessee",
    "north carolina", "south carolina",
}

ANALYSIS_PROMPT = """\
You are an expert crime/law enforcement news analyst. Accuracy is critical — your analysis drives real operational decisions.

═══════════════════════════════════════
ARTICLE INFORMATION
═══════════════════════════════════════
Title: {title}
URL: {url}
Article publish date: {publish_date}

Spreadsheet data (pre-collected):
- Suspect name: {sheet_suspect}
- Arrest date: {sheet_arrest_date}
- Police department: {sheet_police_dept}
- State: {sheet_state}

═══════════════════════════════════════
ARTICLE TEXT
═══════════════════════════════════════
{text}

═══════════════════════════════════════
ANALYSIS TASKS — READ CAREFULLY
═══════════════════════════════════════

TASK 1: SAME DAY ARREST (critical — must be accurate)

Determine whether the suspect was arrested on the SAME CALENDAR DAY that the crime/incident occurred.

Step-by-step process you MUST follow:

STEP A — Find the CRIME DATE:
Read the full article and determine when the crime/incident happened. Look for:
- Explicit dates: "on March 4", "January 9", "last Friday"
- Time references tied to the crime: "Tuesday morning", "that afternoon"
- If the article describes the crime and arrest as part of ONE continuous event happening on the same day (e.g., "police responded to a robbery... pursued the suspect... arrested them"), then the crime date is the same as the arrest/article date
- If the article says the crime was "recent" or "earlier" without a specific date, BUT the article describes an active same-day police response (police are actively searching, pursuing, and arresting during one continuous event), infer the crime likely happened that same day or very recently
- If the article publish date and the described events clearly all happen on the same day, the crime date is that day

STEP B — Find the ARREST DATE:
- Look in the article for when the arrest happened
- If not clearly stated in the article, use the spreadsheet arrest date: "{sheet_arrest_date}"
- Also consider the article publish date ({publish_date}) as context — if the article is reporting on a just-happened arrest with present-tense language, the arrest likely happened on or near the publish date

STEP C — Compare:
- Same calendar day → "Yes"
- Different days → "No"
- ONLY say "Unclear" if you genuinely cannot determine EITHER the crime date OR arrest date even after considering all context clues above

IMPORTANT RULES:
- If police responded to a crime AND arrested the suspect during that same response/pursuit → "Yes"
- If the article describes everything as one continuous same-day event → "Yes"
- If the crime happened on Day 1 but the suspect was caught on Day 2+ → "No"
- Do NOT compare against the article publish date — compare crime date vs arrest date
- If there are multiple crimes on different dates, use the date of the crime that led directly to the arrest
- When in doubt between "Yes" and "Unclear", lean toward making a determination based on context clues

You must report: crime_date (the date you identified or your best determination), arrest_date (the date you identified), and same_day_arrest (Yes/No/Unclear).

TASK 2: FOIA SCORE (integer, 0-10)

Scoring criteria:
- Specific incident date AND location clearly mentioned? → +3 points
- Police department or law enforcement agency clearly named? → +3 points
- Real narrative with specific details (suspect name, charges, circumstances)? → +2 points
- Enough identifying information to file a records request? → +2 points

EXCLUDED STATES RULE:
Excluded states: {excluded_states}
This article's state: "{sheet_state}"
→ If "{sheet_state}" matches any excluded state → score MUST be 0
→ If "{sheet_state}" does NOT match any excluded state → score normally (never give 0 to non-excluded states)
→ States like Ohio, Georgia, New York, California, Florida, Texas, Wisconsin, Illinois, etc. are NOT excluded

TASK 3: FOIA REASONING — One sentence.

TASK 4: YOUTUBE SCORE (integer, 1-10)

Score based on severity and viral potential:
- 8-10 (EXTREME): child abuse/murder, officer-involved shootings, deaths in custody, hate crimes, serial crimes, mass incidents, severe police misconduct, high-speed chase ending in death/serious injury
- 5-7 (MODERATE): armed robbery with pursuit/weapons, assault with serious injury, drug trafficking busts, domestic violence with weapons, kidnapping, fleeing with children at risk, standoffs, stolen vehicle pursuits
- 3-4 (LOWER): simple DUI, low-value shoplifting (under ~$500), minor drug possession, simple trespassing, routine warrant arrests, basic resisting arrest without major incident
- 1-2 (MINIMAL): traffic violations, minor misdemeanors, paperwork crimes, very routine arrests

Boost +1-2 for: bodycam/dashcam likely exists, high-profile department/officer, strong accountability angle, bizarre/unusual circumstances, case went viral, high-value theft

TASK 5: YOUTUBE REASONING — One sentence.

TASK 6: SUMMARY — One concise sentence.

═══════════════════════════════════════
RESPONSE FORMAT
═══════════════════════════════════════
Return ONLY valid JSON. No markdown fences. No extra text.
Example format:
{{"crime_date": "March 4, 2026", "arrest_date": "March 4, 2026", "same_day_arrest": "Yes", "foia_score": 8, "foia_reasoning": "...", "youtube_score": 5, "youtube_reasoning": "...", "summary": "..."}}
"""


FINDER_PROMPT = """\
You are an expert crime/law enforcement news analyst. Read this article carefully and extract all key details.

═══════════════════════════════════════
ARTICLE
═══════════════════════════════════════
Title: {title}
URL: {url}
Article publish date: {publish_date}
Search context: Looking for crimes in {search_state}, {search_city} {search_county}

═══════════════════════════════════════
ARTICLE TEXT
═══════════════════════════════════════
{text}

═══════════════════════════════════════
EXTRACTION TASKS
═══════════════════════════════════════

1. SUSPECT NAME — Full name of the person arrested. Multiple suspects: separate with " / ". If unknown: "Unknown".

2. INCIDENT DATE — When the crime/incident occurred (NOT arrest date). Format: "Month Day, Year".

3. INCIDENT TIME — The time of day the incident occurred. Be as specific as possible:
   - Exact time if stated: "3:45 PM", "11:30 AM"
   - Approximate if implied: "early morning", "around midnight", "afternoon"
   - If the article says "Tuesday evening" → "evening"
   - If truly unknown: "Unknown"

4. INCIDENT LOCATION — The specific location where the incident occurred:
   - Street address if stated: "1200 block of Main Street"
   - Intersection: "near Main St and 5th Ave"
   - Landmark/business: "Walmart on Route 30", "outside the 7-Eleven at 450 Broad St"
   - Area description: "parking lot of Eastland Mall"
   - If only a city/area: "downtown Columbus" or "east side of Springfield"
   - NEVER leave blank — always provide the most specific location available

5. ARREST DATE — When the suspect was arrested. Format: "Month Day, Year".

6. POLICE DEPARTMENT — Full name of the law enforcement agency.

7. STATE — Full US state name (e.g. "Ohio", not "OH").

8. CHARGES — Specific charges filed against the suspect. List all mentioned:
   - e.g. "aggravated assault, resisting arrest, possession of a firearm"
   - If charges aren't specified: "Not specified"

9. OFFICER NAMES — Names of officers involved (arresting, responding, or mentioned):
   - e.g. "Officer John Smith, Sergeant Jane Doe"
   - If not mentioned: "Not identified"

10. CASE NUMBER — Any case number, incident report number, or reference number mentioned:
    - e.g. "Case #2026-12345", "Report #26-0315-001"
    - If not mentioned: ""

11. VICTIM NAME — Name of the victim if mentioned. If not mentioned: ""

12. SAME DAY ARREST — Did the arrest happen on the same calendar day as the crime?
    - Same response/pursuit → "Yes"
    - One continuous event → "Yes"
    - Crime Day 1, arrest Day 2+ → "No"
    - Cannot determine → "Unclear"

13. FOIA SCORE (0-10):
    - +3 for specific date AND location
    - +3 for police department clearly named
    - +2 for real narrative with specific details
    - +2 for enough identifying info to file a request
    - EXCLUDED STATES get automatic 0: {excluded_states}
    - Non-excluded states: NEVER give 0

14. YOUTUBE SCORE (1-10):
    - 8-10: child abuse/murder, officer shootings, deaths in custody, serial crimes
    - 5-7: armed robbery with pursuit, serious assault, kidnapping, standoffs
    - 3-4: simple DUI, low-value shoplifting, minor possession
    - 1-2: traffic violations, minor misdemeanors

═══════════════════════════════════════
RESPONSE FORMAT
═══════════════════════════════════════
Return ONLY valid JSON:
{{"suspect_name": "...", "incident_date": "...", "incident_time": "...", "incident_location": "...", "arrest_date": "...", "police_dept": "...", "state": "...", "charges": "...", "officer_names": "...", "case_number": "...", "victim_name": "...", "same_day_arrest": "Yes/No/Unclear", "foia_score": 8, "youtube_score": 5}}
"""


def analyze_found_article(api_key: str, article: dict, search_state: str = "",
                          search_city: str = "", search_county: str = "") -> dict:
    """Analyze a found article — extracts suspect name, dates, scores. Used by Article Finder."""
    publish_date_str = "Unknown"
    if article.get("publish_date"):
        publish_date_str = article["publish_date"].strftime("%B %d, %Y")

    prompt = FINDER_PROMPT.format(
        title=article.get("title", "Unknown"),
        publish_date=publish_date_str,
        url=article.get("url", ""),
        text=article.get("text", "No text available"),
        search_state=search_state,
        search_city=search_city,
        search_county=search_county,
        excluded_states=", ".join(sorted(EXCLUDED_STATES)),
    )

    client = anthropic.Anthropic(api_key=api_key)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            result = _extract_json(raw, ["suspect_name", "foia_score"])
            if result is None:
                logger.warning("No valid JSON found in found-article response: %s", raw[:200])
                return {"error": f"No JSON found: {raw[:200]}"}

            # Enforce excluded state rule
            state_lower = result.get("state", "").strip().lower()
            if state_lower in EXCLUDED_STATES:
                result["foia_score"] = 0
            elif result.get("foia_score") == 0:
                result["foia_score"] = 1

            try:
                result["foia_score"] = int(result["foia_score"])
            except (ValueError, TypeError):
                pass
            try:
                result["youtube_score"] = int(result["youtube_score"])
            except (ValueError, TypeError):
                pass

            logger.info("Found-article analysis succeeded for %s", article.get("url", "unknown"))
            return result

        except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("Retryable error (attempt %d/%d), waiting %ds: %s",
                               attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                logger.warning("All %d attempts failed: %s", max_retries, e)
                return {"error": str(e)}
        except Exception as e:
            logger.warning("Non-retryable error in found-article analysis: %s", e)
            return {"error": str(e)}


def analyze_article(api_key: str, article: dict, sheet_row: dict = None) -> dict:
    """Send article to Claude for analysis. Returns parsed result dict."""
    if not sheet_row:
        sheet_row = {}

    publish_date_str = "Unknown"
    if article.get("publish_date"):
        publish_date_str = article["publish_date"].strftime("%B %d, %Y")

    sheet_state = sheet_row.get("state", "Unknown")

    prompt = ANALYSIS_PROMPT.format(
        title=article.get("title", "Unknown"),
        publish_date=publish_date_str,
        url=article.get("url", ""),
        text=article.get("text", "No text available"),
        sheet_arrest_date=sheet_row.get("arrest_date", "Unknown"),
        sheet_state=sheet_state,
        sheet_police_dept=sheet_row.get("police_dept", "Unknown"),
        sheet_suspect=sheet_row.get("suspect_name", "Unknown"),
        excluded_states=", ".join(sorted(EXCLUDED_STATES)),
    )

    client = anthropic.Anthropic(api_key=api_key)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            result = _extract_json(raw, ["foia_score", "youtube_score"])
            if result is None:
                logger.warning("No valid JSON found in analysis response: %s", raw[:200])
                return {"error": f"No JSON found in response: {raw[:200]}"}

            # ENFORCE excluded state rule in code
            state_lower = sheet_state.strip().lower()
            is_excluded = state_lower in EXCLUDED_STATES

            if is_excluded:
                result["foia_score"] = 0
                result["foia_reasoning"] = f"State ({sheet_state}) is on the excluded list — automatic 0."
            else:
                if result.get("foia_score") == 0:
                    result["foia_score"] = 1
                    result["foia_reasoning"] = (
                        result.get("foia_reasoning", "") +
                        " (Adjusted: state not excluded, minimum 1.)"
                    )

            # Ensure scores are integers
            try:
                result["foia_score"] = int(result["foia_score"])
            except (ValueError, TypeError):
                pass
            try:
                result["youtube_score"] = int(result["youtube_score"])
            except (ValueError, TypeError):
                pass

            logger.info("Article analysis succeeded for %s", article.get("url", "unknown"))
            return result

        except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("Retryable error (attempt %d/%d), waiting %ds: %s",
                               attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                logger.warning("All %d attempts failed: %s", max_retries, e)
                return {"error": str(e)}
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error: %s", e)
            return {"error": f"JSON parse error: {e}"}
        except anthropic.AuthenticationError:
            logger.warning("Invalid Anthropic API key")
            return {"error": "Invalid Anthropic API key."}
        except anthropic.APIError as e:
            logger.warning("Claude API error: %s", e)
            return {"error": f"Claude API error: {e}"}
        except Exception as e:
            logger.warning("Analysis failed: %s", e)
            return {"error": f"Analysis failed: {e}"}
