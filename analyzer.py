"""AI-powered article analysis using Claude."""

import anthropic
import json
import re

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

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            result = json.loads(json_match.group())
        else:
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

        return result

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}
    except anthropic.AuthenticationError:
        return {"error": "Invalid Anthropic API key."}
    except anthropic.APIConnectionError:
        return {"error": "Cannot connect to Anthropic API. Check internet."}
    except anthropic.RateLimitError:
        return {"error": "Rate limit hit. Wait a moment and retry."}
    except anthropic.APIError as e:
        return {"error": f"Claude API error: {e}"}
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}
