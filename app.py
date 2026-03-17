"""Article Analyzer Bot — Streamlit App."""

import os
import time
import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from sheets_client import (
    get_sheets_service, get_all_rows, get_master_data,
    check_duplicate, write_results_to_row,
)
from article_scraper import scrape_article
from analyzer import analyze_article

load_dotenv()


def _get_secret(key: str, default: str = "") -> str:
    """Get a config value from Streamlit secrets, env vars, or default."""
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)


# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Article Analyzer Bot", page_icon="📰", layout="wide")
st.title("📰 Article Analyzer Bot")
st.caption("Analyzes news articles: duplicate check, same-day arrest, FOIA score, YouTube score.")

# Check if running in cloud mode (secrets pre-configured)
is_cloud = "ANTHROPIC_API_KEY" in st.secrets if hasattr(st, "secrets") else False

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuration")

    if is_cloud:
        anthropic_key = st.secrets["ANTHROPIC_API_KEY"]
        st.success("API key loaded from secrets")
    else:
        anthropic_key = st.text_input(
            "Anthropic API Key",
            value=os.getenv("ANTHROPIC_API_KEY", ""),
            type="password",
        )

    st.divider()
    st.subheader("Google Sheets")

    working_sheet_id = st.text_input(
        "Working Sheet ID",
        value=_get_secret("WORKING_SHEET_ID", "1uTazaCJuBpgjRG8q-7V0iJ-ZwMUrcI3N_IKtLt1hkos"),
    )
    working_tab = st.text_input("Working Sheet Tab", value=_get_secret("WORKING_SHEET_TAB", "Sheet1"))

    master_sheet_id = st.text_input(
        "Master Sheet ID (All Articles)",
        value=_get_secret("MASTER_SHEET_ID", "1j3aD2gscCTGosJ52gIWl3CUKQ8JphkL2xoQ_Bx7ve20"),
    )
    master_tab = st.text_input("Master Sheet Tab", value=_get_secret("MASTER_SHEET_TAB", "Sheet1"))

    st.divider()
    st.subheader("📋 Column Layout")
    st.markdown(
        "**Your data (A–E):**\n"
        "- **A** — Article URL\n"
        "- **B** — Suspect Name\n"
        "- **C** — Arrest Date\n"
        "- **D** — Police Dept\n"
        "- **E** — State\n\n"
        "**Bot output (F–I):**\n"
        "- **F** — Duplicate?\n"
        "- **G** — Same Day Arrest?\n"
        "- **H** — FOIA Score (0–10)\n"
        "- **I** — YouTube Score (1–10)"
    )

    st.divider()
    st.subheader("📊 How Scoring Works")

    with st.expander("Duplicate Check"):
        st.markdown(
            "**Level 1 — URL match:** Normalized URL comparison "
            "(strips tracking params, www, trailing slashes).\n\n"
            "**Level 2 — Name + State match:** If suspect name matches "
            "anyone in the master sheet with the same state, flags as duplicate."
        )

    with st.expander("Same Day Arrest"):
        st.markdown(
            "Checks if the **arrest** happened on the **same calendar day as the crime/incident**.\n\n"
            "The AI reads the full article to identify:\n"
            "1. The exact date the **crime** occurred\n"
            "2. The exact date the **arrest** was made\n\n"
            "Then compares them:\n"
            "- **Yes** = crime and arrest on same day\n"
            "- **No** = different days\n"
            "- **Unclear** = dates not determinable"
        )

    with st.expander("FOIA Score (0–10)"):
        st.markdown(
            "| Points | Criteria |\n|--------|----------|\n"
            "| +3 | Specific date & location mentioned |\n"
            "| +3 | Police dept clearly named |\n"
            "| +2 | Real narrative with details |\n"
            "| +2 | Enough info to file request |\n"
            "| **0** | **Excluded state (auto-zero)** |\n\n"
            "**Excluded:** AL, AR, DE, KS, KY, ME, MO, MN, VA, TN, NC, SC"
        )

    with st.expander("YouTube Score (1–10)"):
        st.markdown(
            "**8–10:** Extreme — child abuse, officer shootings, deaths in custody, hate crimes\n\n"
            "**5–7:** Moderate — armed robbery + pursuit, serious assault, drug busts, standoffs\n\n"
            "**3–4:** Lower — simple DUI, low-value shoplifting, minor possession, routine warrants\n\n"
            "**1–2:** Minimal — traffic violations, minor misdemeanors, paperwork crimes"
        )

# ── Validation ────────────────────────────────────────────────────────────────
missing = []
if not anthropic_key:
    missing.append("Anthropic API Key")
if not working_sheet_id:
    missing.append("Working Sheet ID")
if not master_sheet_id:
    missing.append("Master Sheet ID")

if missing:
    st.warning(f"Please fill in: {', '.join(missing)} (in the sidebar)")
    st.stop()

if not anthropic_key.startswith("sk-ant-"):
    st.error("Anthropic API key should start with `sk-ant-`. Check your key.")
    st.stop()

client_secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_secret.json")
if not is_cloud and not os.path.exists(client_secret_path):
    st.error("**client_secret.json not found!** Place it in the project folder.")
    st.stop()

# ── Connect ──────────────────────────────────────────────────────────────────
@st.cache_resource
def connect_sheets():
    return get_sheets_service(client_secret_path)

try:
    service = connect_sheets()
except Exception as e:
    st.error(f"Google auth failed: {e}")
    st.stop()

# ── Test Anthropic key ────────────────────────────────────────────────────────
import anthropic as _anthropic

@st.cache_data(ttl=600)
def test_anthropic_key(key):
    try:
        client = _anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=5,
            messages=[{"role": "user", "content": "Say OK"}],
        )
        return True, ""
    except _anthropic.AuthenticationError:
        return False, "Invalid API key"
    except _anthropic.APIConnectionError:
        return False, "Cannot connect to Anthropic API"
    except Exception as e:
        return False, str(e)

with st.spinner("Verifying Anthropic API key..."):
    key_ok, key_err = test_anthropic_key(anthropic_key)

if not key_ok:
    st.error(f"Anthropic API error: {key_err}")
    st.stop()

st.success("✅ Google Sheets connected  |  ✅ Anthropic API verified")

# ── Load sheet data ───────────────────────────────────────────────────────────
col_refresh, col_clear = st.columns(2)

with col_refresh:
    if st.button("🔄 Refresh Sheet Data", use_container_width=True):
        st.rerun()

with col_clear:
    clear_results = st.button("🧹 Clear Old Results (F–I)", use_container_width=True)

try:
    rows = get_all_rows(service, working_sheet_id, working_tab)
except Exception as e:
    st.error(f"Failed to open working sheet: {e}")
    st.stop()

if len(rows) < 2:
    st.info("Working sheet is empty or has only a header row.")
    st.stop()

# ── Write headers to sheet if needed ─────────────────────────────────────────
header = rows[0]
while len(header) < 9:
    header.append("")

RESULT_HEADERS = ["Duplicate?", "Same Day Arrest?", "FOIA Score", "YouTube Score"]
if header[5:9] != RESULT_HEADERS:
    try:
        service.spreadsheets().values().update(
            spreadsheetId=working_sheet_id,
            range=f"{working_tab}!F1:I1",
            valueInputOption="RAW",
            body={"values": [RESULT_HEADERS]},
        ).execute()
    except Exception:
        pass

data_rows = rows[1:]

# Pad all rows to 9 columns
padded_rows = []
for r in data_rows:
    padded = r + [""] * (9 - len(r))
    padded_rows.append(padded[:9])

# ── Clear old results ────────────────────────────────────────────────────────
if clear_results:
    with st.spinner("Clearing columns F–I..."):
        clear_data = [["", "", "", ""] for _ in range(len(padded_rows))]
        try:
            service.spreadsheets().values().update(
                spreadsheetId=working_sheet_id,
                range=f"{working_tab}!F2:I{len(padded_rows) + 1}",
                valueInputOption="RAW",
                body={"values": clear_data},
            ).execute()
            # Also clear column J (old notes) if it has data
            clear_j = [[""] for _ in range(len(padded_rows))]
            service.spreadsheets().values().update(
                spreadsheetId=working_sheet_id,
                range=f"{working_tab}!J2:J{len(padded_rows) + 1}",
                valueInputOption="RAW",
                body={"values": clear_j},
            ).execute()
            st.success("Cleared! Refreshing...")
            time.sleep(1)
            st.rerun()
        except Exception as e:
            st.error(f"Failed to clear: {e}")

# ── Display data ─────────────────────────────────────────────────────────────
display_header = ["Article URL", "Suspect Name", "Arrest Date", "Police Dept", "State",
                   "Duplicate?", "Same Day Arrest?", "FOIA Score", "YouTube Score"]
df = pd.DataFrame(padded_rows, columns=display_header)

st.subheader("📋 Current Sheet Data")
st.dataframe(df, use_container_width=True, height=350)

# ── Summary metrics ──────────────────────────────────────────────────────────
urls = [r[0].strip() for r in padded_rows if r[0].strip()]
already_processed = sum(1 for r in padded_rows if r[5].strip())
to_process = len(urls) - already_processed

m1, m2, m3 = st.columns(3)
m1.metric("Total Articles", len(urls))
m2.metric("Already Processed", already_processed)
m3.metric("To Analyze", to_process)

# ── Run analysis ──────────────────────────────────────────────────────────────
if st.button("🚀 Run Analysis", type="primary", use_container_width=True, disabled=to_process == 0):
    with st.spinner("Loading master sheet for duplicate checking (URLs + suspect names)..."):
        try:
            master_data = get_master_data(service, master_sheet_id, master_tab)
        except Exception as e:
            st.error(f"Failed to load master sheet: {e}")
            st.stop()

    st.info(
        f"Loaded **{len(master_data['urls'])}** URLs and "
        f"**{len(master_data['names'])}** unique suspect names from master sheet."
    )

    progress_bar = st.progress(0)
    status_text = st.empty()
    results_container = st.container()

    results_table = []
    total = len(padded_rows)
    success_count = 0
    error_count = 0
    dup_count = 0
    skip_count = 0

    for idx, row in enumerate(padded_rows):
        row_num = idx + 2
        url = row[0].strip()
        suspect_name = row[1].strip()
        arrest_date = row[2].strip()
        police_dept = row[3].strip()
        state = row[4].strip()

        progress_bar.progress((idx + 1) / total)

        # Skip if already processed
        if row[5].strip():
            skip_count += 1
            results_table.append({
                "Row": row_num,
                "Suspect": suspect_name,
                "Duplicate?": row[5],
                "Same Day?": row[6],
                "FOIA": row[7],
                "YouTube": row[8],
                "Status": "⏭️ Already done",
            })
            continue

        if not url:
            continue

        status_text.markdown(f"**[{idx+1}/{total}]** Analyzing: **{suspect_name}** — `{url[:60]}...`")

        # ── Step 1: Smart duplicate check ────────────────────────────────
        is_dup, dup_reason = check_duplicate(master_data, url, suspect_name, state)

        if is_dup:
            dup_count += 1
            results = {
                "duplicate": "DUPLICATE",
                "same_day_arrest": "—",
                "foia_score": "—",
                "youtube_score": "—",
            }
            write_results_to_row(service, working_sheet_id, working_tab, row_num, results)
            results_table.append({
                "Row": row_num,
                "Suspect": suspect_name,
                "Duplicate?": "DUPLICATE",
                "Same Day?": "—",
                "FOIA": "—",
                "YouTube": "—",
                "Status": f"🔁 {dup_reason[:50]}",
            })
            with results_container:
                st.dataframe(pd.DataFrame(results_table), use_container_width=True)
            continue

        # ── Step 2: Scrape article ───────────────────────────────────────
        article = scrape_article(url)
        sheet_data = {
            "suspect_name": suspect_name,
            "arrest_date": arrest_date,
            "police_dept": police_dept,
            "state": state,
        }

        if article.get("error"):
            article_for_ai = {
                "title": f"Arrest of {suspect_name}",
                "text": (
                    f"[Article could not be scraped: {article['error']}]\n\n"
                    f"From spreadsheet: {suspect_name} was arrested on {arrest_date} "
                    f"by {police_dept} in {state}."
                ),
                "publish_date": None,
                "url": url,
            }
            analysis = analyze_article(anthropic_key, article_for_ai, sheet_data)

            if analysis.get("error"):
                error_count += 1
                results = {
                    "duplicate": "No",
                    "same_day_arrest": "Unclear",
                    "foia_score": "Error",
                    "youtube_score": "Error",
                }
                status_icon = "❌ Failed"
            else:
                success_count += 1
                results = {
                    "duplicate": "No",
                    "same_day_arrest": analysis.get("same_day_arrest", "Unclear"),
                    "foia_score": str(analysis.get("foia_score", "?")),
                    "youtube_score": str(analysis.get("youtube_score", "?")),
                }
                status_icon = "⚠️ Partial (no scrape)"

            write_results_to_row(service, working_sheet_id, working_tab, row_num, results)
            results_table.append({
                "Row": row_num,
                "Suspect": suspect_name,
                "Duplicate?": results["duplicate"],
                "Same Day?": results["same_day_arrest"],
                "FOIA": results["foia_score"],
                "YouTube": results["youtube_score"],
                "Status": status_icon,
            })
            with results_container:
                st.dataframe(pd.DataFrame(results_table), use_container_width=True)
            time.sleep(1)
            continue

        # ── Step 3: Full AI analysis ─────────────────────────────────────
        analysis = analyze_article(anthropic_key, article, sheet_data)

        if analysis.get("error"):
            error_count += 1
            results = {
                "duplicate": "No",
                "same_day_arrest": "Error",
                "foia_score": "Error",
                "youtube_score": "Error",
            }
            status_icon = "❌ AI Error"
        else:
            success_count += 1
            results = {
                "duplicate": "No",
                "same_day_arrest": analysis.get("same_day_arrest", "Unknown"),
                "foia_score": str(analysis.get("foia_score", "?")),
                "youtube_score": str(analysis.get("youtube_score", "?")),
            }
            status_icon = "✅ Done"

        write_results_to_row(service, working_sheet_id, working_tab, row_num, results)

        results_table.append({
            "Row": row_num,
            "Suspect": suspect_name,
            "Duplicate?": results["duplicate"],
            "Same Day?": results["same_day_arrest"],
            "FOIA": results["foia_score"],
            "YouTube": results["youtube_score"],
            "Status": status_icon,
        })

        with results_container:
            st.dataframe(pd.DataFrame(results_table), use_container_width=True)

        time.sleep(1.5)

    progress_bar.progress(1.0)
    status_text.empty()

    st.divider()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("✅ Analyzed", success_count)
    s2.metric("🔁 Duplicates", dup_count)
    s3.metric("❌ Errors", error_count)
    s4.metric("⏭️ Skipped", skip_count)
    st.success("**Analysis complete!** Results written to your Google Sheet.")
    st.balloons()
