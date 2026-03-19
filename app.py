"""Article Analyzer Bot — Streamlit App with Finder + Analyzer tabs."""

import os
import time
import hmac
from datetime import datetime, timedelta
import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from sheets_client import (
    get_sheets_service, get_all_rows, get_master_data,
    check_duplicate, write_results_to_row, create_new_sheet,
    append_rows_to_sheet, ensure_foia_headers, get_foia_requests,
    write_foia_request, update_foia_row, get_existing_foia_urls,
)
from foia_requester import (
    generate_foia_request_ai, generate_foia_request_simple,
    generate_request_id, send_email_smtp,
    draft_follow_up, get_requests_needing_followup,
    search_pd_contact_web, process_single_request,
)
from pd_database import (
    get_pd_database, lookup_department, add_department,
    ensure_pd_db_headers,
)
from article_scraper import scrape_article
from analyzer import analyze_article, analyze_found_article, EXCLUDED_STATES
from article_finder import search_articles, CHARGE_CATEGORIES
from title_generator import (
    extract_video_id, get_youtube_transcript, analyze_video_with_gemini,
    search_similar_videos, generate_titles,
)

load_dotenv()

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
SESSION_TIMEOUT_HOURS = 12

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

# ── Custom CSS ───────────────────────────────────────────────────────────────
CUSTOM_CSS = """
<style>
    /* Clean up main container */
    .block-container { padding-top: 2rem; max-width: 1100px; }

    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        background: rgba(255,255,255,0.03);
        border-radius: 12px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 10px;
        padding: 12px 28px;
        font-weight: 500;
        font-size: 15px;
        letter-spacing: 0.3px;
    }
    .stTabs [aria-selected="true"] {
        background: rgba(108, 99, 255, 0.15) !important;
        border-bottom-color: transparent !important;
    }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 12px;
        padding: 16px 20px;
    }
    [data-testid="stMetricLabel"] { font-size: 13px; opacity: 0.6; }
    [data-testid="stMetricValue"] { font-size: 28px; font-weight: 600; }

    /* Buttons */
    .stButton > button {
        border-radius: 10px;
        font-weight: 500;
        letter-spacing: 0.3px;
        transition: all 0.2s ease;
    }
    .stButton > button:hover { transform: translateY(-1px); }
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #6C63FF, #5a52d5);
        border: none;
    }

    /* Dataframes */
    [data-testid="stDataFrame"] {
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 12px;
        overflow: hidden;
    }

    /* Success/info/warning boxes */
    .stAlert { border-radius: 10px; border: none; }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: #0d0d14;
        border-right: 1px solid rgba(255,255,255,0.05);
    }
    section[data-testid="stSidebar"] .stTextInput label { font-size: 13px; opacity: 0.7; }

    /* Inputs */
    .stTextInput input, .stSelectbox > div > div {
        border-radius: 8px !important;
        border-color: rgba(255,255,255,0.08) !important;
    }

    /* Progress bar */
    .stProgress > div > div { border-radius: 8px; }
    .stProgress > div > div > div { background: linear-gradient(90deg, #6C63FF, #8B83FF); border-radius: 8px; }

    /* Multiselect */
    .stMultiSelect [data-baseweb="tag"] {
        background: rgba(108, 99, 255, 0.2);
        border-radius: 6px;
    }

    /* Expander */
    .streamlit-expanderHeader { font-size: 14px; font-weight: 500; }

    /* Divider */
    hr { border-color: rgba(255,255,255,0.05) !important; }

    /* Header */
    .app-header {
        text-align: center;
        padding: 0.5rem 0 1.5rem 0;
    }
    .app-header h1 {
        font-size: 28px;
        font-weight: 700;
        letter-spacing: -0.5px;
        margin-bottom: 4px;
    }
    .app-header p {
        font-size: 14px;
        opacity: 0.5;
        margin-top: 0;
    }
    .status-bar {
        display: flex;
        justify-content: center;
        gap: 16px;
        margin: 0.5rem 0 1rem 0;
    }
    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 14px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 500;
    }
    .status-ok {
        background: rgba(46, 204, 113, 0.1);
        color: #2ecc71;
        border: 1px solid rgba(46, 204, 113, 0.2);
    }
</style>
"""


def _get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)


def _check_password(input_pw: str, stored_pw: str) -> bool:
    return hmac.compare_digest(input_pw.encode(), stored_pw.encode())


# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Article Analyzer Bot", page_icon="📰", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ── Password protection ──────────────────────────────────────────────────────
APP_PASSWORD = _get_secret("APP_PASSWORD", "")

if APP_PASSWORD:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "login_attempts" not in st.session_state:
        st.session_state.login_attempts = 0
    if "lockout_until" not in st.session_state:
        st.session_state.lockout_until = None
    if "auth_time" not in st.session_state:
        st.session_state.auth_time = None

    if st.session_state.authenticated and st.session_state.auth_time:
        if datetime.now() - st.session_state.auth_time > timedelta(hours=SESSION_TIMEOUT_HOURS):
            st.session_state.authenticated = False
            st.session_state.auth_time = None

    if not st.session_state.authenticated:
        st.markdown("<div style='text-align:center; padding-top: 80px;'>", unsafe_allow_html=True)
        st.markdown("### 🔒")
        st.markdown("**Article Analyzer Bot**")
        st.markdown("<p style='opacity:0.5; font-size:14px;'>Enter your password to continue</p>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        col_l, col_c, col_r = st.columns([1, 1, 1])
        with col_c:
            if st.session_state.lockout_until and datetime.now() < st.session_state.lockout_until:
                remaining = (st.session_state.lockout_until - datetime.now()).seconds // 60 + 1
                st.error(f"Locked out for {remaining} more minute(s).")
                st.stop()
            elif st.session_state.lockout_until:
                st.session_state.lockout_until = None
                st.session_state.login_attempts = 0

            password_input = st.text_input("Password", type="password", label_visibility="collapsed",
                                           placeholder="Password")
            if st.button("Login", type="primary", use_container_width=True):
                if _check_password(password_input, APP_PASSWORD):
                    st.session_state.authenticated = True
                    st.session_state.login_attempts = 0
                    st.session_state.auth_time = datetime.now()
                    st.rerun()
                else:
                    st.session_state.login_attempts += 1
                    remaining = MAX_LOGIN_ATTEMPTS - st.session_state.login_attempts
                    if remaining <= 0:
                        st.session_state.lockout_until = datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)
                        st.error(f"Locked out for {LOCKOUT_MINUTES} minutes.")
                    else:
                        st.error(f"Incorrect password. {remaining} attempt(s) left.")
        st.stop()

# ── Cloud detection ──────────────────────────────────────────────────────────
is_cloud = False
try:
    is_cloud = "ANTHROPIC_API_KEY" in st.secrets
except Exception:
    pass

# ── Sidebar config ───────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("#### Settings")

    if is_cloud:
        anthropic_key = st.secrets["ANTHROPIC_API_KEY"]
        st.caption("✓ API key loaded")
    else:
        anthropic_key = st.text_input(
            "Anthropic API Key",
            value=os.getenv("ANTHROPIC_API_KEY", ""),
            type="password",
        )

    serpapi_key = st.text_input(
        "SerpAPI Key",
        value=_get_secret("SERPAPI_KEY", ""),
        type="password",
    )

    gemini_key = st.text_input(
        "Gemini API Key",
        value=_get_secret("GEMINI_API_KEY", ""),
        type="password",
    )

    youtube_key = st.text_input(
        "YouTube API Key",
        value=_get_secret("YOUTUBE_API_KEY", ""),
        type="password",
    )

    st.markdown("---")
    st.markdown("#### Sheets")

    working_sheet_id = st.text_input(
        "Working Sheet ID",
        value=_get_secret("WORKING_SHEET_ID", "1uTazaCJuBpgjRG8q-7V0iJ-ZwMUrcI3N_IKtLt1hkos"),
    )
    working_tab = st.text_input("Working Tab", value=_get_secret("WORKING_SHEET_TAB", "Sheet1"))

    master_sheet_id = st.text_input(
        "Master Sheet ID",
        value=_get_secret("MASTER_SHEET_ID", "1j3aD2gscCTGosJ52gIWl3CUKQ8JphkL2xoQ_Bx7ve20"),
    )
    master_tab = st.text_input("Master Tab", value=_get_secret("MASTER_SHEET_TAB", "Sheet1"))

    st.markdown("---")
    st.markdown("#### FOIA Requests")

    foia_sheet_id = st.text_input(
        "FOIA Sheet ID",
        value=_get_secret("FOIA_SHEET_ID", ""),
    )
    foia_email = st.text_input(
        "FOIA Email Address",
        value=_get_secret("FOIA_EMAIL", ""),
    )
    foia_email_password = st.text_input(
        "FOIA Email App Password",
        value=_get_secret("FOIA_EMAIL_PASSWORD", ""),
        type="password",
    )

# ── Validation ───────────────────────────────────────────────────────────────
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
    st.error("Anthropic API key should start with `sk-ant-`.")
    st.stop()

client_secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_secret.json")
if not is_cloud and not os.path.exists(client_secret_path):
    st.error("**client_secret.json** not found.")
    st.stop()

# ── Connect services ─────────────────────────────────────────────────────────
@st.cache_resource
def connect_sheets():
    return get_sheets_service(client_secret_path)

try:
    service = connect_sheets()
except Exception as e:
    st.error(f"Google auth failed: {e}")
    st.stop()

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

with st.spinner("Verifying API key..."):
    key_ok, key_err = test_anthropic_key(anthropic_key)

if not key_ok:
    st.error(f"Anthropic API: {key_err}")
    st.stop()

# ── Header ───────────────────────────────────────────────────────────────────
st.markdown("""
<div class="app-header">
    <h1>Article Analyzer Bot</h1>
    <p>Find, analyze, and score crime articles for FOIA requests & YouTube content</p>
</div>
<div class="status-bar">
    <span class="status-pill status-ok">● Sheets connected</span>
    <span class="status-pill status-ok">● AI verified</span>
</div>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
tab_finder, tab_analyzer, tab_foia, tab_titles = st.tabs(["  🔍  Finder  ", "  📊  Analyzer  ", "  📋  FOIA Requests  ", "  🎬  Title Generator  "])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: ARTICLE FINDER
# ══════════════════════════════════════════════════════════════════════════════
with tab_finder:
    if not serpapi_key:
        st.info("Add your SerpAPI key in the sidebar to enable the Article Finder.")
    else:
        st.markdown("##### Search Parameters")

        col1, col2, col3 = st.columns(3)

        with col1:
            state_options = ["All States"] + US_STATES
            finder_state_selection = st.selectbox("State", state_options, index=0)
            finder_state = "" if finder_state_selection == "All States" else finder_state_selection
            finder_city = st.text_input("City", placeholder="e.g. Canton")

        with col2:
            finder_county = st.text_input("County", placeholder="e.g. Stark County")
            finder_dept = st.text_input("Police Department", placeholder="e.g. Canton PD")

        with col3:
            finder_gender = st.selectbox("Gender", ["Any", "Male", "Female"])
            same_day_only = st.toggle("Same Day Arrests Only", value=False,
                                       help="Only include articles where the arrest happened on the same day as the crime")

        # Date range
        st.markdown("##### Date Range")
        col_from, col_to = st.columns(2)
        with col_from:
            date_from = st.date_input("From", value=datetime.now().date() - timedelta(days=30))
        with col_to:
            date_to = st.date_input("To", value=datetime.now().date())

        if date_from > date_to:
            st.error("'From' date must be before 'To' date.")
            st.stop()

        # Charges — preset categories + custom input
        st.markdown("##### Charges")
        finder_charges = st.multiselect(
            "Select from common charges",
            sorted(CHARGE_CATEGORIES.keys()),
            placeholder="Pick charge categories...",
        )

        custom_charge_input = st.text_input(
            "Add custom charges",
            placeholder="e.g. carjacking, stalking, manslaughter (comma-separated)",
            help="Type any charge — the system will automatically find related/similar charges",
        )
        custom_charges_list = []
        if custom_charge_input.strip():
            custom_charges_list = [c.strip() for c in custom_charge_input.split(",") if c.strip()]
            # Show expanded terms
            from article_finder import _expand_custom_charges
            with st.expander("Expanded search terms", expanded=False):
                for custom in custom_charges_list:
                    expanded = _expand_custom_charges(custom)
                    st.caption(f"**{custom}** → {', '.join(expanded)}")

        col_max, col_top, _ = st.columns([1, 1, 1])
        with col_max:
            max_search = st.number_input("Max to search", min_value=10, max_value=500, value=150)
        with col_top:
            top_n = st.number_input("Top to export", min_value=5, max_value=200, value=50)

        st.markdown("")

        if st.button("🔍  Find Articles", type="primary", use_container_width=True):
            with st.spinner("Loading master sheet..."):
                try:
                    master_data = get_master_data(service, master_sheet_id, master_tab)
                except Exception as e:
                    st.error(f"Failed to load master sheet: {e}")
                    st.stop()

            # ── Step 1: Search ────────────────────────────────────────────
            st.markdown("---")
            st.markdown("##### Step 1 — Searching Google News")
            date_range_str = f"{date_from.strftime('%b %d')} – {date_to.strftime('%b %d, %Y')}"
            st.caption(f"Date range: {date_range_str}")
            search_progress = st.progress(0)
            search_status = st.empty()

            def on_search_progress(qi, total, query, found):
                search_progress.progress((qi + 1) / total if total > 0 else 1.0)
                search_status.caption(f"Query {qi+1}/{total} — {found} articles found")

            found_articles = search_articles(
                api_key=serpapi_key,
                state=finder_state,
                city=finder_city,
                county=finder_county,
                police_dept=finder_dept,
                charges=finder_charges if finder_charges else None,
                custom_charges=custom_charges_list if custom_charges_list else None,
                gender=finder_gender,
                date_from=datetime.combine(date_from, datetime.min.time()),
                date_to=datetime.combine(date_to, datetime.max.time()),
                max_results=max_search,
                progress_callback=on_search_progress,
            )

            search_progress.progress(1.0)
            search_status.empty()

            if not found_articles:
                st.warning("No articles found. Try broader search terms or a wider date range.")
                st.stop()

            st.caption(f"Found **{len(found_articles)}** articles")

            with st.expander(f"Raw results ({len(found_articles)})", expanded=False):
                raw_df = pd.DataFrame([{
                    "Title": a["title"][:80],
                    "Source": a["source"],
                    "Date": a.get("date_str", ""),
                } for a in found_articles])
                st.dataframe(raw_df, use_container_width=True, hide_index=True)

            # ── Step 2: Deduplicate ───────────────────────────────────────
            st.markdown("##### Step 2 — Removing duplicates")
            new_articles = []
            dup_count = 0
            for a in found_articles:
                is_dup, _ = check_duplicate(master_data, a["url"], "", finder_state)
                if is_dup:
                    dup_count += 1
                else:
                    new_articles.append(a)

            col_d1, col_d2 = st.columns(2)
            col_d1.metric("Duplicates removed", dup_count)
            col_d2.metric("New articles", len(new_articles))

            if not new_articles:
                st.warning("All articles are duplicates.")
                st.stop()

            # ── Step 3: Analyze ───────────────────────────────────────────
            st.markdown("##### Step 3 — Analyzing articles")
            analyze_progress = st.progress(0)
            analyze_status = st.empty()
            results_placeholder = st.empty()

            analyzed = []
            total = len(new_articles)

            for i, article_info in enumerate(new_articles):
                analyze_progress.progress((i + 1) / total)
                analyze_status.caption(f"[{i+1}/{total}] Analyzing...")

                scraped = scrape_article(article_info["url"])

                if scraped.get("error") and not scraped.get("text"):
                    continue

                if not scraped.get("title"):
                    scraped["title"] = article_info.get("title", "")

                result = analyze_found_article(
                    api_key=anthropic_key,
                    article=scraped,
                    search_state=finder_state,
                    search_city=finder_city,
                    search_county=finder_county,
                )

                if result.get("error"):
                    continue

                result["url"] = article_info["url"]
                result["title"] = scraped.get("title", article_info.get("title", ""))
                result["source"] = article_info.get("source", "")
                analyzed.append(result)

                with results_placeholder.container():
                    live_df = pd.DataFrame([{
                        "Suspect": r.get("suspect_name", ""),
                        "Incident Date": r.get("incident_date", ""),
                        "Dept": r.get("police_dept", ""),
                        "State": r.get("state", ""),
                        "Same Day": r.get("same_day_arrest", ""),
                        "FOIA": r.get("foia_score", ""),
                        "YT": r.get("youtube_score", ""),
                    } for r in analyzed])
                    st.dataframe(live_df, use_container_width=True, hide_index=True)

                time.sleep(1.5)

            analyze_progress.progress(1.0)
            analyze_status.empty()

            if not analyzed:
                st.warning("No articles could be analyzed.")
                st.stop()

            # ── Step 4: Rank & Filter ─────────────────────────────────────
            st.markdown("##### Step 4 — Ranking & exporting")

            valid = []
            seen_names = set()
            same_day_filtered = 0
            for r in analyzed:
                state_lower = r.get("state", "").strip().lower()
                if state_lower in EXCLUDED_STATES:
                    continue
                # Same day arrest filter
                if same_day_only and r.get("same_day_arrest", "").strip().lower() != "yes":
                    same_day_filtered += 1
                    continue
                name = r.get("suspect_name", "").strip().lower()
                if name and name in seen_names:
                    continue
                if name:
                    seen_names.add(name)
                valid.append(r)

            if same_day_only and same_day_filtered > 0:
                st.caption(f"Filtered out **{same_day_filtered}** articles that were not same-day arrests")

            def sort_key(r):
                try:
                    return int(r.get("foia_score", 0)) + int(r.get("youtube_score", 0))
                except (ValueError, TypeError):
                    return 0

            valid.sort(key=sort_key, reverse=True)
            top_articles = valid[:top_n]

            if not top_articles:
                st.warning("No articles passed the filters.")
                st.stop()

            final_df = pd.DataFrame([{
                "#": i + 1,
                "Suspect": r.get("suspect_name", "Unknown"),
                "Incident Date": r.get("incident_date", ""),
                "Police Dept": r.get("police_dept", ""),
                "State": r.get("state", ""),
                "Same Day": r.get("same_day_arrest", ""),
                "FOIA": r.get("foia_score", ""),
                "YT": r.get("youtube_score", ""),
            } for i, r in enumerate(top_articles)])

            st.dataframe(final_df, use_container_width=True, hide_index=True, height=400)

            # Build descriptive sheet name
            name_parts = [finder_state if finder_state else "All States"]
            if finder_city:
                name_parts.append(finder_city)
            elif finder_county:
                name_parts.append(finder_county)
            charge_label = ""
            if finder_charges:
                charge_label = ", ".join(finder_charges[:2])
                if len(finder_charges) > 2:
                    charge_label += f" +{len(finder_charges) - 2}"
            elif custom_charges_list:
                charge_label = ", ".join(custom_charges_list[:2])
            if charge_label:
                name_parts.append(charge_label)
            name_parts.append(f"{date_from.strftime('%b %d')}-{date_to.strftime('%b %d')}")

            sheet_title = f"Articles — {' · '.join(name_parts)}"
            if len(sheet_title) > 100:
                sheet_title = sheet_title[:97] + "..."

            # Export
            with st.spinner("Creating Google Sheet..."):
                try:
                    new_sheet_id = create_new_sheet(service, sheet_title, make_public=True)

                    header_row = [
                        "LINK TO WEBSITE", "NAME OF SUSPECT(S)", "DATE OF INCIDENT",
                        "POLICE DEPT", "STATE", "SAME DAY ARREST", "FOIA SCORE", "YOUTUBE SCORE",
                    ]

                    data_rows = []
                    for r in top_articles:
                        data_rows.append([
                            r.get("url", ""),
                            r.get("suspect_name", ""),
                            r.get("incident_date", ""),
                            r.get("police_dept", ""),
                            r.get("state", ""),
                            r.get("same_day_arrest", ""),
                            str(r.get("foia_score", "")),
                            str(r.get("youtube_score", "")),
                        ])

                    append_rows_to_sheet(service, new_sheet_id, "Sheet1", [header_row] + data_rows)

                    sheet_url = f"https://docs.google.com/spreadsheets/d/{new_sheet_id}"
                    st.success(f"Exported **{len(top_articles)}** articles to a public sheet!")
                    st.markdown(f"[Open Google Sheet →]({sheet_url})")
                    st.caption("Anyone with the link can view and edit this sheet.")
                    st.balloons()

                except Exception as e:
                    st.error(f"Sheet export failed: {e}")
                    csv = final_df.to_csv(index=False)
                    st.download_button("Download CSV", csv, "found_articles.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: ARTICLE ANALYZER
# ══════════════════════════════════════════════════════════════════════════════
with tab_analyzer:
    col_refresh, col_clear = st.columns(2)

    with col_refresh:
        if st.button("🔄  Refresh", use_container_width=True):
            st.rerun()

    with col_clear:
        clear_results = st.button("🧹  Clear Results", use_container_width=True)

    try:
        rows = get_all_rows(service, working_sheet_id, working_tab)
    except Exception as e:
        st.error(f"Failed to open working sheet: {e}")
        st.stop()

    if len(rows) < 2:
        st.info("Working sheet is empty.")
        st.stop()

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
    padded_rows = []
    for r in data_rows:
        padded = r + [""] * (9 - len(r))
        padded_rows.append(padded[:9])

    if clear_results:
        with st.spinner("Clearing..."):
            clear_data = [["", "", "", ""] for _ in range(len(padded_rows))]
            try:
                service.spreadsheets().values().update(
                    spreadsheetId=working_sheet_id,
                    range=f"{working_tab}!F2:I{len(padded_rows) + 1}",
                    valueInputOption="RAW",
                    body={"values": clear_data},
                ).execute()
                clear_j = [[""] for _ in range(len(padded_rows))]
                service.spreadsheets().values().update(
                    spreadsheetId=working_sheet_id,
                    range=f"{working_tab}!J2:J{len(padded_rows) + 1}",
                    valueInputOption="RAW",
                    body={"values": clear_j},
                ).execute()
                st.success("Cleared!")
                time.sleep(0.5)
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    display_header = ["URL", "Suspect", "Arrest Date", "Dept", "State",
                       "Dup?", "Same Day?", "FOIA", "YT"]
    df = pd.DataFrame(padded_rows, columns=display_header)

    st.dataframe(df, use_container_width=True, height=320, hide_index=True)

    urls = [r[0].strip() for r in padded_rows if r[0].strip()]
    already_processed = sum(1 for r in padded_rows if r[5].strip())
    to_process = len(urls) - already_processed

    m1, m2, m3 = st.columns(3)
    m1.metric("Total", len(urls))
    m2.metric("Done", already_processed)
    m3.metric("Remaining", to_process)

    st.markdown("")

    if st.button("🚀  Run Analysis", type="primary", use_container_width=True, disabled=to_process == 0):
        with st.spinner("Loading master sheet..."):
            try:
                master_data = get_master_data(service, master_sheet_id, master_tab)
            except Exception as e:
                st.error(f"Failed: {e}")
                st.stop()

        st.caption(
            f"Loaded {len(master_data['urls'])} URLs and "
            f"{len(master_data['names'])} suspect names from master sheet."
        )

        progress_bar = st.progress(0)
        status_text = st.empty()
        results_placeholder = st.empty()

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

            if row[5].strip():
                skip_count += 1
                results_table.append({
                    "#": row_num, "Suspect": suspect_name,
                    "Dup?": row[5], "Same Day?": row[6],
                    "FOIA": row[7], "YT": row[8],
                    "Status": "⏭️ Done",
                })
                continue

            if not url:
                continue

            status_text.caption(f"[{idx+1}/{total}] {suspect_name}")

            is_dup, dup_reason = check_duplicate(master_data, url, suspect_name, state)

            if is_dup:
                dup_count += 1
                results = {
                    "duplicate": "DUPLICATE", "same_day_arrest": "—",
                    "foia_score": "—", "youtube_score": "—",
                }
                write_results_to_row(service, working_sheet_id, working_tab, row_num, results)
                results_table.append({
                    "#": row_num, "Suspect": suspect_name,
                    "Dup?": "DUP", "Same Day?": "—",
                    "FOIA": "—", "YT": "—",
                    "Status": f"🔁 {dup_reason[:40]}",
                })
                with results_placeholder.container():
                    st.dataframe(pd.DataFrame(results_table), use_container_width=True, hide_index=True)
                continue

            article = scrape_article(url)
            sheet_data = {
                "suspect_name": suspect_name, "arrest_date": arrest_date,
                "police_dept": police_dept, "state": state,
            }

            if article.get("error"):
                article_for_ai = {
                    "title": f"Arrest of {suspect_name}",
                    "text": (
                        f"[Article could not be scraped: {article['error']}]\n\n"
                        f"From spreadsheet: {suspect_name} was arrested on {arrest_date} "
                        f"by {police_dept} in {state}."
                    ),
                    "publish_date": None, "url": url,
                }
                analysis = analyze_article(anthropic_key, article_for_ai, sheet_data)

                if analysis.get("error"):
                    error_count += 1
                    results = {
                        "duplicate": "No", "same_day_arrest": "Unclear",
                        "foia_score": "Error", "youtube_score": "Error",
                    }
                    status_icon = "❌"
                else:
                    success_count += 1
                    results = {
                        "duplicate": "No",
                        "same_day_arrest": analysis.get("same_day_arrest", "Unclear"),
                        "foia_score": str(analysis.get("foia_score", "?")),
                        "youtube_score": str(analysis.get("youtube_score", "?")),
                    }
                    status_icon = "⚠️"

                write_results_to_row(service, working_sheet_id, working_tab, row_num, results)
                results_table.append({
                    "#": row_num, "Suspect": suspect_name,
                    "Dup?": results["duplicate"], "Same Day?": results["same_day_arrest"],
                    "FOIA": results["foia_score"], "YT": results["youtube_score"],
                    "Status": status_icon,
                })
                with results_placeholder.container():
                    st.dataframe(pd.DataFrame(results_table), use_container_width=True, hide_index=True)
                time.sleep(1)
                continue

            analysis = analyze_article(anthropic_key, article, sheet_data)

            if analysis.get("error"):
                error_count += 1
                results = {
                    "duplicate": "No", "same_day_arrest": "Error",
                    "foia_score": "Error", "youtube_score": "Error",
                }
                status_icon = "❌"
            else:
                success_count += 1
                results = {
                    "duplicate": "No",
                    "same_day_arrest": analysis.get("same_day_arrest", "Unknown"),
                    "foia_score": str(analysis.get("foia_score", "?")),
                    "youtube_score": str(analysis.get("youtube_score", "?")),
                }
                status_icon = "✅"

            write_results_to_row(service, working_sheet_id, working_tab, row_num, results)
            results_table.append({
                "#": row_num, "Suspect": suspect_name,
                "Dup?": results["duplicate"], "Same Day?": results["same_day_arrest"],
                "FOIA": results["foia_score"], "YT": results["youtube_score"],
                "Status": status_icon,
            })

            with results_placeholder.container():
                st.dataframe(pd.DataFrame(results_table), use_container_width=True, hide_index=True)

            time.sleep(1.5)

        progress_bar.progress(1.0)
        status_text.empty()

        st.markdown("---")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Analyzed", success_count)
        s2.metric("Duplicates", dup_count)
        s3.metric("Errors", error_count)
        s4.metric("Skipped", skip_count)
        st.success("Analysis complete! Results written to your Google Sheet.")
        st.balloons()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: FOIA REQUESTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_foia:
    if not foia_sheet_id:
        st.info("Add your **FOIA Sheet ID** in the sidebar to get started.")
    elif not anthropic_key:
        st.info("Add your **Anthropic API Key** in the sidebar.")
    else:
        try:
            service = get_sheets_service()
            ensure_foia_headers(service, foia_sheet_id)
            ensure_pd_db_headers(service, foia_sheet_id)
        except Exception as e:
            st.error(f"Failed to connect to FOIA sheet: {e}")
            st.stop()

        # ── Section A: Create New Requests ────────────────────────────────
        st.markdown("##### Create New Requests")
        st.caption("Articles with FOIA score ≥ 7 from your Working Sheet. Auto-finds PD contact, generates letter, and sends.")

        try:
            working_rows = get_all_rows(service, working_sheet_id, working_tab)
            existing_urls = get_existing_foia_urls(service, foia_sheet_id)
            pd_db = get_pd_database(service, foia_sheet_id)
        except Exception as e:
            st.error(f"Failed to load data: {e}")
            working_rows = []
            existing_urls = set()
            pd_db = []

        # Filter high-FOIA articles not yet requested
        requestable = []
        if working_rows and len(working_rows) > 1:
            for i, row in enumerate(working_rows[1:], start=2):
                while len(row) < 9:
                    row.append("")
                url = row[0].strip()
                foia_score_str = row[7].strip() if len(row) > 7 else ""
                try:
                    foia_score = int(foia_score_str)
                except (ValueError, TypeError):
                    continue
                if foia_score >= 7 and url.lower() not in existing_urls:
                    requestable.append({
                        "row_num": i,
                        "url": url,
                        "suspect_name": row[1].strip() if len(row) > 1 else "",
                        "incident_date": row[2].strip() if len(row) > 2 else "",
                        "police_dept": row[3].strip() if len(row) > 3 else "",
                        "state": row[4].strip() if len(row) > 4 else "",
                        "foia_score": foia_score,
                    })

        if not requestable:
            st.info("No new articles with FOIA score ≥ 7 to request. Run the Analyzer first.")
        else:
            st.success(f"**{len(requestable)}** articles ready for FOIA requests")

            # Sender name from email
            sender_name = foia_email.split("@")[0].replace(".", " ").title() if foia_email else "Records Requester"

            if "foia_drafts" not in st.session_state:
                st.session_state.foia_drafts = {}
            if "pd_suggestions" not in st.session_state:
                st.session_state.pd_suggestions = {}

            # ── FULLY AUTOMATED: Process All ──
            can_auto = bool(foia_email and foia_email_password and serpapi_key)
            if not can_auto:
                missing_cfg = []
                if not foia_email:
                    missing_cfg.append("FOIA Email")
                if not foia_email_password:
                    missing_cfg.append("FOIA Email App Password")
                if not serpapi_key:
                    missing_cfg.append("SerpAPI Key")
                st.warning(f"Set {', '.join(missing_cfg)} in sidebar to enable full automation.")

            if st.button(
                f"Process All {len(requestable)} Requests Automatically",
                type="primary",
                use_container_width=True,
                disabled=not can_auto,
            ):
                progress_bar = st.progress(0)
                status_container = st.container()
                results_summary = {"sent": 0, "portal_draft": 0, "failed": 0}

                for i, article in enumerate(requestable):
                    progress_bar.progress((i + 1) / len(requestable))
                    with status_container:
                        st.caption(f"Processing {i+1}/{len(requestable)}: **{article['suspect_name']}** — {article['police_dept']}, {article['state']}")

                    # Scrape article for context
                    try:
                        scraped = scrape_article(article["url"])
                        article_text = scraped.get("text", "") if scraped else ""
                    except Exception:
                        article_text = ""

                    result = process_single_request(
                        article=article,
                        article_text=article_text,
                        sender_name=sender_name,
                        anthropic_key=anthropic_key,
                        serpapi_key=serpapi_key,
                        foia_email=foia_email,
                        foia_email_password=foia_email_password,
                        pd_db=pd_db,
                        service=service,
                        foia_sheet_id=foia_sheet_id,
                    )

                    results_summary[result["status"]] = results_summary.get(result["status"], 0) + 1
                    with status_container:
                        if result["status"] == "sent":
                            st.success(f"{article['suspect_name']}: {result['details']}")
                        elif result["status"] == "portal_draft":
                            st.info(f"{article['suspect_name']}: {result['details']}")
                        else:
                            st.error(f"{article['suspect_name']}: {result['details']}")

                # Final summary
                st.markdown("---")
                sum_cols = st.columns(3)
                sum_cols[0].metric("Sent via Email", results_summary.get("sent", 0))
                sum_cols[1].metric("Portal (Manual)", results_summary.get("portal_draft", 0))
                sum_cols[2].metric("Failed", results_summary.get("failed", 0))
                if results_summary.get("sent", 0) > 0:
                    st.balloons()

            st.markdown("---")
            st.markdown("##### Individual Requests")
            st.caption("Or process articles one at a time below:")

            for idx, article in enumerate(requestable):
                dept_name = article["police_dept"]
                art_state = article["state"]
                pd_match = lookup_department(pd_db, dept_name, art_state)

                with st.expander(
                    f"**{article['suspect_name']}** — {dept_name}, {art_state} (FOIA: {article['foia_score']})",
                    expanded=False,
                ):
                    # ── Department contact info ──
                    st.markdown("**Department Contact**")
                    if pd_match:
                        method = pd_match.get("Method", "email")
                        email_addr = pd_match.get("Email Address", "")
                        portal_url = pd_match.get("Portal URL", "")
                        contact = email_addr if method in ("email", "both") else portal_url
                        st.info(f"**{dept_name}** — Method: **{method}** | Email: {email_addr} | Portal: {portal_url}")
                        if pd_match.get("Notes"):
                            st.caption(f"Notes: {pd_match['Notes']}")
                    else:
                        st.warning(f"**{dept_name}** not in database yet.")
                        contact = ""

                        # AI-powered PD contact search via web
                        pd_sug_key = f"pd_sug_{dept_name}_{art_state}"
                        if st.button(f"Search for {dept_name} contact info", key=f"search_pd_{idx}"):
                            with st.spinner("Searching the web for contact info..."):
                                suggestion = search_pd_contact_web(dept_name, art_state, serpapi_key, anthropic_key)
                                st.session_state.pd_suggestions[pd_sug_key] = suggestion

                        if pd_sug_key in st.session_state.pd_suggestions:
                            sug = st.session_state.pd_suggestions[pd_sug_key]
                            st.caption(f"AI suggestion: {sug.get('method', '?')} — Email: {sug.get('email', 'N/A')} — Portal: {sug.get('portal_url', 'N/A')}")
                            if sug.get("notes"):
                                st.caption(f"Notes: {sug['notes']}")

                        with st.form(key=f"add_pd_{idx}"):
                            st.caption("Add this department to your database:")
                            sug = st.session_state.pd_suggestions.get(pd_sug_key, {})
                            add_col1, add_col2 = st.columns(2)
                            with add_col1:
                                new_method = st.selectbox("Method", ["email", "portal", "both"],
                                    index=["email", "portal", "both"].index(sug.get("method", "email")) if sug.get("method") in ["email", "portal", "both"] else 0,
                                    key=f"method_{idx}")
                                new_email = st.text_input("Records Email", value=sug.get("email", ""), key=f"pd_email_{idx}")
                            with add_col2:
                                new_portal = st.text_input("Portal URL", value=sug.get("portal_url", ""), key=f"pd_portal_{idx}")
                                new_notes = st.text_input("Notes", value=sug.get("notes", ""), key=f"pd_notes_{idx}")
                            if st.form_submit_button("Save Department"):
                                add_department(service, foia_sheet_id, {
                                    "name": dept_name,
                                    "state": art_state,
                                    "method": new_method,
                                    "email": new_email,
                                    "portal_url": new_portal,
                                    "notes": new_notes,
                                })
                                st.success(f"Added {dept_name} to database")
                                st.rerun()

                    # ── FOIA Letter ──
                    st.markdown("---")
                    st.markdown("**FOIA Request Letter**")

                    draft_key = f"draft_{article['url']}"

                    # Generate AI-powered letter on demand
                    if draft_key not in st.session_state.foia_drafts:
                        if st.button(f"Generate FOIA Letter", key=f"gen_{idx}", type="primary"):
                            with st.spinner("Scraping article and generating letter with AI..."):
                                try:
                                    scraped = scrape_article(article["url"])
                                    article_text = scraped.get("text", "") if scraped else ""
                                except Exception:
                                    article_text = ""

                                if article_text:
                                    foia_letter = generate_foia_request_ai(
                                        article_text=article_text,
                                        suspect_name=article["suspect_name"],
                                        incident_date=article["incident_date"],
                                        police_dept=dept_name,
                                        state=art_state,
                                        sender_name=sender_name,
                                        anthropic_key=anthropic_key,
                                    )
                                else:
                                    # Fallback: basic template if article can't be scraped
                                    from foia_requester import generate_foia_request_simple
                                    foia_letter = generate_foia_request_simple(
                                        suspect_name=article["suspect_name"],
                                        incident_date=article["incident_date"],
                                        police_dept=dept_name,
                                        state=art_state,
                                        sender_name=sender_name,
                                    )
                                    st.warning("Could not scrape article — using basic template. Edit the letter to add details.")

                                st.session_state.foia_drafts[draft_key] = foia_letter
                                st.rerun()
                        else:
                            st.caption(f"Article: {article['url']}")
                    else:
                        letter = st.session_state.foia_drafts[draft_key]
                        edited_subject = st.text_input(
                            "Subject",
                            value=letter["subject"],
                            key=f"subject_{idx}",
                        )
                        edited_body = st.text_area(
                            "Letter body (edit as needed)",
                            value=letter["body"],
                            height=350,
                            key=f"body_{idx}",
                        )

                        if st.button("Regenerate", key=f"regen_{idx}"):
                            del st.session_state.foia_drafts[draft_key]
                            st.rerun()

                        # Action buttons
                        btn_col1, btn_col2, btn_col3 = st.columns(3)

                        with btn_col1:
                            to_email = None
                            if pd_match and pd_match.get("Email Address"):
                                to_email = pd_match["Email Address"]
                            if to_email:
                                if st.button(f"Send to {to_email}", key=f"send_{idx}", type="primary"):
                                    if not foia_email or not foia_email_password:
                                        st.error("Set FOIA Email and App Password in sidebar")
                                    else:
                                        result = send_email_smtp(
                                            smtp_host="smtp.gmail.com",
                                            smtp_port=587,
                                            email=foia_email,
                                            password=foia_email_password,
                                            to_addr=to_email,
                                            subject=edited_subject,
                                            body=edited_body,
                                        )
                                        if result["success"]:
                                            today = datetime.now().strftime("%Y-%m-%d")
                                            write_foia_request(service, foia_sheet_id, {
                                                "Request ID": generate_request_id(),
                                                "Article URL": article["url"],
                                                "Suspect Name": article["suspect_name"],
                                                "Incident Date": article["incident_date"],
                                                "Police Department": dept_name,
                                                "State": art_state,
                                                "FOIA Score": str(article["foia_score"]),
                                                "Request Method": "email",
                                                "Contact Info": to_email,
                                                "Status": "Sent",
                                                "Date Created": today,
                                                "Date Sent": today,
                                                "Last Follow-Up": "",
                                                "Follow-Up Count": "0",
                                                "Notes": "",
                                                "Request Body": edited_body,
                                            })
                                            st.success(f"Sent to {to_email}")
                                        else:
                                            st.error(f"Failed: {result['error']}")

                        with btn_col2:
                            if st.button("Save as Draft", key=f"draft_btn_{idx}"):
                                today = datetime.now().strftime("%Y-%m-%d")
                                write_foia_request(service, foia_sheet_id, {
                                    "Request ID": generate_request_id(),
                                    "Article URL": article["url"],
                                    "Suspect Name": article["suspect_name"],
                                    "Incident Date": article["incident_date"],
                                    "Police Department": dept_name,
                                    "State": art_state,
                                    "FOIA Score": str(article["foia_score"]),
                                    "Request Method": pd_match.get("Method", "unknown") if pd_match else "unknown",
                                    "Contact Info": contact,
                                    "Status": "Draft",
                                    "Date Created": today,
                                    "Date Sent": "",
                                    "Last Follow-Up": "",
                                    "Follow-Up Count": "0",
                                    "Notes": "",
                                    "Request Body": edited_body,
                                })
                                st.success("Saved as draft")

                        with btn_col3:
                            if pd_match and pd_match.get("Portal URL"):
                                st.link_button("Open Portal", pd_match["Portal URL"], use_container_width=True)

        # ── Section B: Request Pipeline ───────────────────────────────────
        st.markdown("---")
        st.markdown("##### Request Pipeline")

        try:
            all_requests = get_foia_requests(service, foia_sheet_id)
        except Exception:
            all_requests = []

        if not all_requests:
            st.info("No FOIA requests yet. Create one above.")
        else:
            # Status metrics
            statuses = [r.get("Status", "Unknown") for r in all_requests]
            metric_cols = st.columns(6)
            status_labels = ["Draft", "Sent", "Acknowledged", "In Progress", "Received", "Denied"]
            for i, label in enumerate(status_labels):
                count = statuses.count(label)
                metric_cols[i].metric(label, count)

            # Dataframe
            df = pd.DataFrame(all_requests)
            display_cols = ["Request ID", "Suspect Name", "Police Department", "State", "Status", "Date Sent", "Follow-Up Count"]
            display_cols = [c for c in display_cols if c in df.columns]
            st.dataframe(df[display_cols], use_container_width=True)

            # Status update
            st.caption("Update request status:")
            update_col1, update_col2, update_col3 = st.columns([2, 1, 1])
            with update_col1:
                req_ids = [r.get("Request ID", "") for r in all_requests]
                selected_req = st.selectbox("Request", req_ids, key="status_update_req")
            with update_col2:
                new_status = st.selectbox("New Status", status_labels, key="status_update_val")
            with update_col3:
                if st.button("Update", key="status_update_btn"):
                    for i, r in enumerate(all_requests):
                        if r.get("Request ID") == selected_req:
                            update_foia_row(service, foia_sheet_id, i + 2, {"Status": new_status})
                            st.success(f"Updated {selected_req} → {new_status}")
                            st.rerun()

        # ── Section C: Follow-Ups Needed ──────────────────────────────────
        st.markdown("---")
        st.markdown("##### Follow-Ups Needed")

        if all_requests:
            needing_fu = get_requests_needing_followup(all_requests)
            if not needing_fu:
                st.info("All requests are up to date — no follow-ups needed.")
            else:
                st.warning(f"**{len(needing_fu)}** requests need follow-up")
                for fu_idx, req in enumerate(needing_fu):
                    days = req.get("_days_elapsed", "?")
                    fu_count = req.get("Follow-Up Count", "0")
                    with st.expander(
                        f"**{req.get('Police Department', '?')}** — {days} days, {fu_count} prior follow-ups",
                    ):
                        st.caption(f"Suspect: {req.get('Suspect Name', '?')} | Sent: {req.get('Date Sent', '?')}")

                        fu_draft_key = f"fu_draft_{req.get('Request ID', fu_idx)}"
                        if st.button("Draft Follow-Up", key=f"fu_btn_{fu_idx}"):
                            with st.spinner("Drafting with Claude..."):
                                draft_text = draft_follow_up(req, anthropic_key)
                                sender_name_fu = foia_email.split("@")[0].replace(".", " ").title() if foia_email else ""
                                draft_text += f"\n\n{sender_name_fu}"
                                st.session_state[fu_draft_key] = draft_text

                        if fu_draft_key in st.session_state:
                            edited_fu = st.text_area(
                                "Follow-up draft",
                                value=st.session_state[fu_draft_key],
                                height=200,
                                key=f"fu_text_{fu_idx}",
                            )
                            fu_send_col1, fu_send_col2 = st.columns(2)
                            with fu_send_col1:
                                contact_info = req.get("Contact Info", "")
                                if contact_info and "@" in contact_info:
                                    if st.button(f"Send to {contact_info}", key=f"fu_send_{fu_idx}"):
                                        if not foia_email or not foia_email_password:
                                            st.error("Set FOIA Email and App Password in sidebar")
                                        else:
                                            subject = f"Follow-Up: Records Request – {req.get('Suspect Name', '')} ({req.get('Incident Date', '')})"
                                            result = send_email_smtp(
                                                smtp_host="smtp.gmail.com",
                                                smtp_port=587,
                                                email=foia_email,
                                                password=foia_email_password,
                                                to_addr=contact_info,
                                                subject=subject,
                                                body=edited_fu,
                                            )
                                            if result["success"]:
                                                today = datetime.now().strftime("%Y-%m-%d")
                                                row_idx = next(
                                                    (i for i, r in enumerate(all_requests) if r.get("Request ID") == req.get("Request ID")),
                                                    None,
                                                )
                                                if row_idx is not None:
                                                    new_count = int(fu_count or "0") + 1
                                                    update_foia_row(service, foia_sheet_id, row_idx + 2, {
                                                        "Last Follow-Up": today,
                                                        "Follow-Up Count": str(new_count),
                                                    })
                                                st.success(f"Follow-up sent to {contact_info}")
                                            else:
                                                st.error(f"Failed: {result['error']}")
        else:
            st.info("No requests to follow up on yet.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: TITLE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
with tab_titles:
    if not gemini_key or not youtube_key:
        st.info("Add your **Gemini API Key** and **YouTube API Key** in the sidebar to enable the Title Generator.")
    else:
        st.markdown("##### Paste your YouTube video link")

        video_url = st.text_input(
            "YouTube URL",
            placeholder="https://www.youtube.com/watch?v=... or unlisted link",
            label_visibility="collapsed",
        )

        col_num, col_channel, col_spacer = st.columns([1, 1, 1])
        with col_num:
            num_titles = st.slider("Number of titles", min_value=5, max_value=15, value=10)
        with col_channel:
            target_channel = st.selectbox("Target Channel", ["Unpopular", "Vee Cams"])

        st.markdown("")

        if st.button("🎬  Generate Titles", type="primary", use_container_width=True, disabled=not video_url.strip()):
            video_id = extract_video_id(video_url.strip())
            if not video_id:
                st.error("Invalid YouTube URL. Please paste a valid YouTube link.")
                st.stop()

            # ── Step 1: Analyze the video ────────────────────────────────
            st.markdown("---")
            st.markdown("##### Step 1 — Understanding your video")
            with st.spinner("Analyzing video with Gemini AI (transcript + context)..."):
                video_analysis = analyze_video_with_gemini(video_url.strip(), gemini_key, youtube_key)

            if video_analysis.get("error"):
                st.error(f"Video analysis failed: {video_analysis['error']}")
                st.stop()

            with st.expander("Video breakdown", expanded=False):
                st.markdown(f"**What happened:** {video_analysis.get('what_happened', 'N/A')}")
                st.markdown(f"**Severity:** {video_analysis.get('severity', '?')}/10")
                st.markdown(f"**Similar to:** {video_analysis.get('similar_to', 'N/A')}")
                if video_analysis.get("key_moments"):
                    st.markdown("**Key moments:**")
                    for moment in video_analysis["key_moments"]:
                        st.markdown(f"- {moment}")
                if video_analysis.get("clickbait_angles"):
                    st.markdown("**Best angles:**")
                    for angle in video_analysis["clickbait_angles"]:
                        st.markdown(f"- {angle}")

            # ── Step 2: Find similar videos ──────────────────────────────
            st.markdown("##### Step 2 — Researching similar viral videos")
            with st.spinner("Searching YouTube for top-performing similar videos..."):
                similar_videos = search_similar_videos(video_analysis, youtube_key)

            st.caption(f"Found **{len(similar_videos)}** similar videos for reference")

            with st.expander(f"Similar videos ({len(similar_videos)})", expanded=False):
                for sv in similar_videos[:10]:
                    st.markdown(f"- **{sv['title']}** — {sv['views']:,} views ({sv['channel']})")

            # ── Step 3: Generate titles ──────────────────────────────────
            st.markdown("##### Step 3 — Generating titles")
            with st.spinner("Claude is crafting optimized titles..."):
                titles = generate_titles(
                    video_analysis=video_analysis,
                    similar_titles=similar_videos,
                    anthropic_key=anthropic_key,
                    num_titles=num_titles,
                    target_channel=target_channel,
                )

            if titles and titles[0].get("title", "").startswith("Error"):
                st.error(titles[0]["title"])
                st.stop()

            st.markdown("---")
            st.markdown("##### Your Titles")
            st.caption("Ranked by predicted performance (highest confidence first)")

            for i, t in enumerate(titles):
                confidence = t.get("confidence", 0)
                # Color code by confidence
                if confidence >= 8:
                    badge = "🟢"
                elif confidence >= 6:
                    badge = "🟡"
                else:
                    badge = "🔴"

                col_title, col_conf = st.columns([5, 1])
                with col_title:
                    st.markdown(f"**{i+1}. {t['title']}**")
                    st.caption(f"Structure: {t.get('structure', '—')} · Hook: {t.get('hook', '—')} · {t.get('reasoning', '')}")
                with col_conf:
                    st.markdown(f"### {badge} {confidence}/10")

                if i < len(titles) - 1:
                    st.markdown("<hr style='margin: 8px 0; opacity: 0.1;'>", unsafe_allow_html=True)

            # Copy-friendly list
            st.markdown("---")
            with st.expander("Copy-paste list", expanded=True):
                title_list = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(titles))
                st.code(title_list, language=None)
