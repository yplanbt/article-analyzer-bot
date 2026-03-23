"""Article Analyzer Bot — Streamlit App with Finder + Analyzer tabs."""

import os
import time
import hmac
from datetime import datetime, timedelta
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dotenv import load_dotenv

from sheets_client import (
    get_sheets_service, get_all_rows, get_master_data,
    check_duplicate, write_results_to_row, create_new_sheet,
    append_rows_to_sheet, ensure_foia_headers, get_foia_requests,
    write_foia_request, update_foia_row, get_existing_foia_urls,
    _normalize_url,
    ensure_archive_headers, append_to_archive, get_archive_articles,
    update_archive_foia_status, append_articles_to_working_sheet,
    ensure_activity_headers, log_activity, get_recent_activity,
    read_monitor_status,
)
from foia_requester import (
    generate_foia_request_ai, generate_foia_request_simple,
    generate_request_id, send_email_smtp,
    draft_follow_up, get_requests_needing_followup,
    search_pd_contact_web, process_single_request,
    check_bounced_emails,
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
    :root {
        --accent: #3B82F6;
        --accent-glow: rgba(59,130,246,0.25);
        --success: #22c55e;
        --warning: #f59e0b;
        --danger: #ef4444;
        --purple: #8B5CF6;
        --text-muted: rgba(255,255,255,0.45);
        --border-subtle: rgba(255,255,255,0.06);
        --radius: 10px;
    }

    /* Hide default Streamlit header for cleaner look */
    .stApp > header {
        background: transparent !important;
        border-bottom: 1px solid rgba(255,255,255,0.04);
    }

    /* Base */
    .block-container { padding-top: 0.5rem; max-width: 1100px; }

    /* Push content below Streamlit header */
    .stMainBlockContainer { padding-top: 2rem; }

    /* Tabs — elevated bar */
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
        background: rgba(255,255,255,0.02);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 10px;
        padding: 4px 6px;
        margin-bottom: 16px;
    }
    .stTabs [data-baseweb="tab"] {
        padding: 8px 18px;
        font-weight: 500;
        font-size: 13px;
        line-height: 1.4;
        color: rgba(255,255,255,0.45);
        border-radius: 7px;
        transition: color 0.2s, background 0.2s;
        border-bottom: none !important;
        white-space: nowrap;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: rgba(255,255,255,0.7);
        background: rgba(255,255,255,0.03);
    }
    .stTabs [aria-selected="true"] {
        color: #fff !important;
        background: rgba(59,130,246,0.15) !important;
        border-bottom: none !important;
        box-shadow: 0 0 12px rgba(59,130,246,0.1);
    }
    .stTabs [data-baseweb="tab-highlight"] {
        display: none;
    }

    /* Metrics — glassmorphism cards */
    [data-testid="metric-container"] {
        background: rgba(255,255,255,0.03);
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 10px;
        padding: 14px 16px 12px 16px;
        transition: border-color 0.2s, background 0.2s;
    }
    [data-testid="metric-container"]:hover {
        background: rgba(255,255,255,0.05);
        border-color: rgba(59,130,246,0.25);
    }
    [data-testid="stMetricLabel"] { font-size: 11px; opacity: 0.45; text-transform: uppercase; letter-spacing: 0.6px; }
    [data-testid="stMetricValue"] { font-size: 24px; font-weight: 600; }
    [data-testid="stMetricDelta"] { font-size: 12px; }

    /* Buttons — flat */
    .stButton > button {
        border-radius: 6px;
        font-weight: 500;
        font-size: 14px;
        transition: opacity 0.15s, transform 0.1s;
    }
    .stButton > button:hover { opacity: 0.85; transform: translateY(-1px); }
    .stButton > button:active { transform: translateY(0px); }
    .stButton > button[kind="primary"] {
        background: #3B82F6;
        border: none;
    }

    /* Dataframes — clean with hover */
    [data-testid="stDataFrame"] {
        border-radius: 8px;
        overflow: hidden;
        border: 1px solid rgba(255,255,255,0.05);
        transition: border-color 0.2s;
    }
    [data-testid="stDataFrame"]:hover {
        border-color: rgba(59,130,246,0.15);
    }
    /* Row hover effect via iframe body */
    [data-testid="stDataFrame"] iframe {
        border-radius: 8px;
    }

    /* Alerts — subtle */
    .stAlert { border-radius: 8px; transition: opacity 0.2s; }

    /* Sidebar — compact */
    section[data-testid="stSidebar"] {
        border-right: 1px solid rgba(255,255,255,0.04);
    }
    section[data-testid="stSidebar"] .stTextInput label,
    section[data-testid="stSidebar"] .stSelectbox label {
        font-size: 12px;
        opacity: 0.6;
        text-transform: uppercase;
        letter-spacing: 0.3px;
    }

    /* Inputs */
    .stTextInput input, .stSelectbox > div > div, .stMultiSelect > div > div {
        border-radius: 6px !important;
        border-color: rgba(255,255,255,0.08) !important;
        transition: border-color 0.15s !important;
    }
    .stTextInput input:focus, .stSelectbox > div > div:focus-within {
        border-color: rgba(59,130,246,0.5) !important;
    }

    /* Progress */
    .stProgress > div > div { border-radius: 4px; }
    .stProgress > div > div > div { background: #3B82F6; border-radius: 4px; transition: width 0.3s; }

    /* Tags */
    .stMultiSelect [data-baseweb="tag"] {
        background: rgba(59, 130, 246, 0.15);
        border-radius: 4px;
    }

    /* Expanders */
    .streamlit-expanderHeader { font-size: 13px; font-weight: 500; transition: opacity 0.15s; }
    .streamlit-expanderHeader:hover { opacity: 0.8; }

    /* Dividers */
    hr { border-color: rgba(255,255,255,0.04) !important; }

    /* Section label helper */
    .section-label {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: rgba(255,255,255,0.35);
        margin: 1.5rem 0 0.75rem 0;
        font-weight: 600;
    }

    /* Status dot indicators */
    .status-ok { color: #22c55e; }
    .status-err { color: #ef4444; }
    .status-warn { color: #f59e0b; }

    /* Pipeline flow */
    .pipeline-row {
        display: flex;
        align-items: stretch;
        gap: 0;
        margin: 8px 0 16px 0;
    }
    .pipeline-box {
        background: rgba(255,255,255,0.025);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 16px 10px 14px 10px;
        text-align: center;
        font-size: 12px;
        font-weight: 500;
        color: rgba(255,255,255,0.85);
        flex: 1;
        position: relative;
        transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s;
    }
    .pipeline-box:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    }
    .pipeline-box .count {
        font-size: 28px;
        font-weight: 700;
        display: block;
        margin-bottom: 4px;
        line-height: 1;
    }
    .pipeline-box .stage-name {
        font-size: 12px;
        font-weight: 600;
        display: block;
        margin-bottom: 4px;
    }
    .pipeline-box .label {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        opacity: 0.45;
        line-height: 1.3;
    }
    .pipeline-box .top-bar {
        position: absolute;
        top: 0; left: 12px; right: 12px;
        height: 3px;
        border-radius: 0 0 3px 3px;
    }
    .pipeline-arrow {
        color: rgba(255,255,255,0.15);
        font-size: 18px;
        padding: 0 4px;
        flex-shrink: 0;
        display: flex;
        align-items: center;
    }

    /* Per-stage colors */
    .pipe-found { border-color: rgba(59,130,246,0.2); }
    .pipe-found:hover { border-color: rgba(59,130,246,0.4); }
    .pipe-found .count { color: #3B82F6; }
    .pipe-found .top-bar { background: #3B82F6; }

    .pipe-ready { border-color: rgba(139,92,246,0.2); }
    .pipe-ready:hover { border-color: rgba(139,92,246,0.4); }
    .pipe-ready .count { color: #8B5CF6; }
    .pipe-ready .top-bar { background: #8B5CF6; }

    .pipe-requests { border-color: rgba(14,165,233,0.2); }
    .pipe-requests:hover { border-color: rgba(14,165,233,0.4); }
    .pipe-requests .count { color: #0EA5E9; }
    .pipe-requests .top-bar { background: #0EA5E9; }

    .pipe-sent { border-color: rgba(34,197,94,0.2); }
    .pipe-sent:hover { border-color: rgba(34,197,94,0.4); }
    .pipe-sent .count { color: #22C55E; }
    .pipe-sent .top-bar { background: #22C55E; }

    .pipe-followup { border-color: rgba(245,158,11,0.2); }
    .pipe-followup:hover { border-color: rgba(245,158,11,0.4); }
    .pipe-followup .count { color: #F59E0B; }
    .pipe-followup .top-bar { background: #F59E0B; }

    .pipe-alert { border-color: rgba(239,68,68,0.3) !important; background: rgba(239,68,68,0.05) !important; }
    .pipe-alert .count { color: #EF4444 !important; }
    .pipe-alert .top-bar { background: #EF4444 !important; }

    /* System health cards */
    .health-card {
        background: rgba(255,255,255,0.02);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 10px;
        padding: 12px 16px;
        margin-bottom: 6px;
        display: flex;
        align-items: center;
        gap: 12px;
        font-size: 13px;
        transition: background 0.2s, border-color 0.2s;
    }
    .health-card:hover { background: rgba(255,255,255,0.04); border-color: rgba(255,255,255,0.1); }
    .health-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .health-dot-ok { background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,0.6); }
    .health-dot-err { background: #ef4444; box-shadow: 0 0 8px rgba(239,68,68,0.5); }
    .health-dot-warn { background: #f59e0b; box-shadow: 0 0 8px rgba(245,158,11,0.4); }

    /* Hero stats bar */
    .hero-stats {
        display: flex;
        gap: 12px;
        margin: 0 0 16px 0;
    }
    .hero-stat {
        flex: 1;
        background: linear-gradient(135deg, rgba(59,130,246,0.08), rgba(139,92,246,0.06));
        border: 1px solid rgba(59,130,246,0.12);
        border-radius: 12px;
        padding: 20px 16px;
        text-align: center;
        transition: transform 0.2s, box-shadow 0.2s;
    }
    .hero-stat:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.25); }
    .hero-stat .hero-num { font-size: 32px; font-weight: 700; color: #fff; display: block; line-height: 1; }
    .hero-stat .hero-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: rgba(255,255,255,0.4); margin-top: 6px; display: block; }
    .hero-stat .hero-sub { font-size: 11px; color: rgba(255,255,255,0.3); margin-top: 4px; display: block; }

    /* Smooth section transitions */
    .stMarkdown, .stMetric, .stDataFrame, .stAlert {
        animation: fadeInUp 0.25s ease-out;
    }
    @keyframes fadeInUp {
        from { opacity: 0; transform: translateY(6px); }
        to   { opacity: 1; transform: translateY(0); }
    }

    /* Status pills */
    .status-pill {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.3px;
        text-transform: uppercase;
    }
    .pill-draft { background: rgba(156,163,175,0.2); color: #9CA3AF; }
    .pill-portal { background: rgba(245,158,11,0.15); color: #F59E0B; }
    .pill-sent { background: rgba(59,130,246,0.15); color: #3B82F6; }
    .pill-ack { background: rgba(139,92,246,0.15); color: #8B5CF6; }
    .pill-progress { background: rgba(14,165,233,0.15); color: #0EA5E9; }
    .pill-received { background: rgba(34,197,94,0.15); color: #22C55E; }
    .pill-denied { background: rgba(239,68,68,0.15); color: #EF4444; }

    /* Kevin (monitor agent) panel */
    .kevin-panel {
        background: rgba(139,92,246,0.06);
        border: 1px solid rgba(139,92,246,0.2);
        border-radius: var(--radius);
        padding: 16px;
        margin: 8px 0;
    }
    .kevin-header {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 10px;
    }
    .kevin-dot {
        width: 10px; height: 10px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    .kevin-dot-online {
        background: var(--success);
        box-shadow: 0 0 8px rgba(34,197,94,0.6);
        animation: pulse 2s infinite;
    }
    .kevin-dot-stale {
        background: var(--warning);
        box-shadow: 0 0 6px rgba(245,158,11,0.4);
    }
    .kevin-dot-offline {
        background: var(--danger);
        box-shadow: 0 0 6px rgba(239,68,68,0.4);
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.7; transform: scale(1.3); }
    }
    .kevin-stat {
        font-size: 12px;
        color: var(--text-muted);
        margin-top: 4px;
    }

    /* Activity feed */
    .activity-item {
        display: flex;
        align-items: flex-start;
        gap: 10px;
        padding: 8px 12px;
        border-left: 2px solid var(--border-subtle);
        margin-left: 6px;
        margin-bottom: 2px;
        transition: background 0.15s;
    }
    .activity-item:hover {
        background: rgba(255,255,255,0.02);
    }
    .activity-dot {
        width: 6px; height: 6px;
        border-radius: 50%;
        flex-shrink: 0;
        margin-top: 6px;
    }
    .activity-time {
        font-size: 10px;
        color: var(--text-muted);
        min-width: 55px;
    }
    .activity-text {
        font-size: 12px;
        color: rgba(255,255,255,0.75);
        flex: 1;
    }
    .activity-source {
        font-size: 10px;
        padding: 1px 6px;
        border-radius: 4px;
        background: rgba(255,255,255,0.05);
        color: var(--text-muted);
    }

    /* Dashboard card container */
    .dash-card {
        background: rgba(255,255,255,0.02);
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius);
        padding: 16px;
        margin-bottom: 12px;
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
        st.markdown("**Article Analyzer**")
        st.markdown("<p style='opacity:0.4; font-size:13px;'>Enter your password to continue</p>", unsafe_allow_html=True)
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
            if st.button("Login", type="primary", width="stretch"):
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
    with st.expander("API Keys", expanded=False):
        if is_cloud:
            anthropic_key = st.secrets["ANTHROPIC_API_KEY"]
            st.caption("Anthropic key loaded from secrets")
        else:
            anthropic_key = st.text_input(
                "Anthropic",
                value=os.getenv("ANTHROPIC_API_KEY", ""),
                type="password",
            )
        serpapi_key = st.text_input(
            "SerpAPI",
            value=_get_secret("SERPAPI_KEY", ""),
            type="password",
        )
        gemini_key = st.text_input(
            "Gemini",
            value=_get_secret("GEMINI_API_KEY", ""),
            type="password",
        )
        youtube_key = st.text_input(
            "YouTube",
            value=_get_secret("YOUTUBE_API_KEY", ""),
            type="password",
        )

    with st.expander("Google Sheets", expanded=False):
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

    with st.expander("FOIA Settings", expanded=False):
        foia_sheet_id = st.text_input(
            "FOIA Sheet ID",
            value=_get_secret("FOIA_SHEET_ID", "1ZoEAj_w-IxpY45GDKK3gNSqgAf0dHK75ihjT3IbPbf8"),
        )
        foia_email = st.text_input(
            "Email Address",
            value=_get_secret("FOIA_EMAIL", ""),
        )
        foia_email_password = st.text_input(
            "Email App Password",
            value=_get_secret("FOIA_EMAIL_PASSWORD", ""),
            type="password",
        )
        portal_login_email = st.text_input(
            "Portal Email",
            value=_get_secret("PORTAL_EMAIL", ""),
        )
        portal_login_password = st.text_input(
            "Portal Password",
            value=_get_secret("PORTAL_PASSWORD", ""),
            type="password",
        )
        openclaw_gateway = st.text_input(
            "OpenClaw Gateway URL",
            value=_get_secret("OPENCLAW_GATEWAY", ""),
            placeholder="e.g. http://192.168.1.50:62847",
        )
        captcha_api_key = st.text_input(
            "2captcha API Key",
            value=_get_secret("CAPTCHA_API_KEY", ""),
            type="password",
            help="For auto-solving CAPTCHAs on PD portals. Get a key at 2captcha.com (~$3/1000 solves)",
        )
        if captcha_api_key:
            os.environ["CAPTCHA_API_KEY"] = captcha_api_key
    portal_credentials = (
        {"email": portal_login_email, "password": portal_login_password}
        if portal_login_email
        else None
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

def _reconnect_sheets():
    """Force reconnect to Google Sheets (clears cached connection)."""
    connect_sheets.clear()
    return connect_sheets()

try:
    service = connect_sheets()
except Exception as e:
    st.error(f"Google auth failed: {e}")
    st.stop()


import anthropic as _anthropic

@st.cache_data(ttl=3600)
def test_anthropic_key(key):
    """Validate API key without spending tokens — uses count_tokens endpoint."""
    try:
        client = _anthropic.Anthropic(api_key=key)
        client.messages.count_tokens(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "test"}],
        )
        return True, ""
    except _anthropic.AuthenticationError:
        return False, "Invalid API key"
    except _anthropic.APIConnectionError:
        return False, "Cannot connect to Anthropic API"
    except Exception as e:
        # count_tokens may not exist in older SDK — fallback to format check
        if key.startswith("sk-ant-"):
            return True, ""
        return False, str(e)

key_ok, key_err = test_anthropic_key(anthropic_key)

if not key_ok:
    st.error(f"Anthropic API: {key_err}")
    st.stop()

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
tab_dash, tab_finder, tab_analyzer, tab_foia, tab_archive, tab_titles = st.tabs(["Dashboard", "Finder", "Analyzer", "FOIA Requests", "Archive", "Titles"])


def _status_dot(ok, label):
    color = "#22c55e" if ok else "#ef4444"
    return f'<span style="color:{color}; font-size:16px;">&#9679;</span>&ensp;{label}'


def _warn_dot(label):
    return f'<span style="color:#f59e0b; font-size:16px;">&#9679;</span>&ensp;{label}'


# ══════════════════════════════════════════════════════════════════════════════
# TAB 0: DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab_dash:
    # ── System Status ─────────────────────────────────────────────────────
    # ── Gather data for entire dashboard ──────────────────────────────────
    _dash_working_rows = []
    _dash_foia_requests = []
    _dash_pd_db = []
    _dash_needing_fu = []
    _dash_sheets_ok = False
    _dash_foia_sheet_ok = False

    try:
        if service and working_sheet_id:
            _dash_working_rows = get_all_rows(service, working_sheet_id, working_tab)
            _dash_sheets_ok = True
    except Exception as _e:
        _dash_sheets_ok = False
    try:
        if service and foia_sheet_id:
            ensure_foia_headers(service, foia_sheet_id)
            _dash_foia_requests = get_foia_requests(service, foia_sheet_id)
            _dash_foia_sheet_ok = True
    except Exception:
        _dash_foia_sheet_ok = False
    try:
        if service and foia_sheet_id:
            _dash_pd_db = get_pd_database(service, foia_sheet_id)
    except Exception:
        pass
    try:
        if _dash_foia_requests:
            _dash_needing_fu = get_requests_needing_followup(_dash_foia_requests)
    except Exception:
        pass

    # Compute working sheet stats
    _total_articles = max(len(_dash_working_rows) - 1, 0) if _dash_working_rows else 0
    _foia_ready = 0
    _yt_ready = 0
    _same_day = 0
    for _row in _dash_working_rows[1:] if _dash_working_rows else []:
        while len(_row) < 9:
            _row.append("")
        try:
            if int(float(_row[7])) > 0:
                _foia_ready += 1
        except (ValueError, TypeError, IndexError):
            pass
        try:
            if int(float(_row[8])) >= 7:
                _yt_ready += 1
        except (ValueError, TypeError, IndexError):
            pass
        try:
            if _row[6].strip().lower() == "yes":
                _same_day += 1
        except (IndexError, AttributeError):
            pass

    # Compute FOIA stats
    _foia_statuses = [r.get("Status", "") for r in _dash_foia_requests]
    _n_draft = _foia_statuses.count("Draft")
    _n_portal = _foia_statuses.count("Portal Needed")
    _n_sent = _foia_statuses.count("Sent")
    _n_ack = _foia_statuses.count("Acknowledged")
    _n_prog = _foia_statuses.count("In Progress")
    _n_recv = _foia_statuses.count("Received")
    _n_denied = _foia_statuses.count("Denied")
    _n_total_sent = _n_sent + _n_ack + _n_prog + _n_recv + _n_denied
    _unique_states = len(set(r.get("State", "").strip() for r in _dash_foia_requests if r.get("State", "").strip()))
    _n_pd = len(_dash_pd_db) - 1 if len(_dash_pd_db) > 1 else 0

    # FOIA success rate (received out of sent)
    _foia_success_rate = 0
    if _n_total_sent > 0:
        _foia_success_rate = round((_n_recv / _n_total_sent) * 100)

    # Last activity timestamps
    _last_article_date = ""
    _last_foia_date = ""
    if _dash_working_rows and len(_dash_working_rows) > 1:
        _dates = [r[2].strip() for r in _dash_working_rows[1:] if len(r) > 2 and r[2].strip()]
        if _dates:
            _last_article_date = sorted(_dates)[-1]
    if _dash_foia_requests:
        _foia_dates = [r.get("Date Created", "").strip() for r in _dash_foia_requests if r.get("Date Created", "").strip()]
        if _foia_dates:
            _last_foia_date = sorted(_foia_dates)[-1]

    # ── Hero Stats Bar ────────────────────────────────────────────────────
    st.markdown(f"""
<div class="hero-stats">
    <div class="hero-stat">
        <span class="hero-num">{_total_articles}</span>
        <span class="hero-label">Articles Found</span>
        <span class="hero-sub">{_same_day} same-day arrests</span>
    </div>
    <div class="hero-stat" style="background: linear-gradient(135deg, rgba(139,92,246,0.08), rgba(59,130,246,0.06)); border-color: rgba(139,92,246,0.12);">
        <span class="hero-num">{_foia_ready}</span>
        <span class="hero-label">FOIA Ready</span>
        <span class="hero-sub">Scored articles</span>
    </div>
    <div class="hero-stat" style="background: linear-gradient(135deg, rgba(34,197,94,0.08), rgba(14,165,233,0.06)); border-color: rgba(34,197,94,0.12);">
        <span class="hero-num">{len(_dash_foia_requests)}</span>
        <span class="hero-label">FOIA Requests</span>
        <span class="hero-sub">{_unique_states} states &middot; {_n_pd} departments</span>
    </div>
    <div class="hero-stat" style="background: linear-gradient(135deg, rgba(245,158,11,0.08), rgba(239,68,68,0.05)); border-color: rgba(245,158,11,0.12);">
        <span class="hero-num">{_foia_success_rate}%</span>
        <span class="hero-label">Success Rate</span>
        <span class="hero-sub">{_n_recv} received of {_n_total_sent} sent</span>
    </div>
</div>
""", unsafe_allow_html=True)

    # ── System Health (collapsed by default) ──────────────────────────────
    with st.expander("System Health", expanded=False):
        def _health_card(ok, label, detail="", warn=False):
            dot_cls = "health-dot-warn" if warn else ("health-dot-ok" if ok else "health-dot-err")
            status_text = detail if detail else ("Connected" if ok else "Not configured")
            return (
                f'<div class="health-card">'
                f'<div class="health-dot {dot_cls}"></div>'
                f'<div style="flex:1"><span style="font-weight:600">{label}</span>'
                f'<span style="opacity:0.45; font-size:12px; margin-left:8px">{status_text}</span></div>'
                f'</div>'
            )

        hc1, hc2 = st.columns(2)
        with hc1:
            st.markdown(
                _health_card(bool(anthropic_key and key_ok), "Anthropic API",
                             "Verified" if (anthropic_key and key_ok) else ("Key present but unverified" if anthropic_key else "No key")) +
                _health_card(bool(serpapi_key), "SerpAPI",
                             "Key configured" if serpapi_key else "No key — Finder disabled") +
                _health_card(bool(gemini_key), "Gemini API",
                             "Key configured" if gemini_key else "No key — Titles disabled"),
                unsafe_allow_html=True,
            )
        with hc2:
            st.markdown(
                _health_card(_dash_sheets_ok, "Google Sheets (Working)",
                             f"{_total_articles} articles" if _dash_sheets_ok else "Connection failed") +
                _health_card(_dash_foia_sheet_ok or not foia_sheet_id, "Google Sheets (FOIA)",
                             f"{len(_dash_foia_requests)} requests" if _dash_foia_sheet_ok else ("No sheet configured" if not foia_sheet_id else "Connection failed"),
                             warn=not foia_sheet_id) +
                _health_card(bool(foia_email and foia_email_password), "FOIA Email (SMTP)",
                             foia_email if (foia_email and foia_email_password) else "Not configured"),
                unsafe_allow_html=True,
            )

    # ── Pipeline Flow — color-coded stages ─────────────────────────────────
    st.markdown('<p class="section-label">Pipeline</p>', unsafe_allow_html=True)

    _conv_rate = f"{round((_foia_ready / _total_articles) * 100)}%" if _total_articles > 0 else "—"
    _fu_cls = "pipe-followup pipe-alert" if len(_dash_needing_fu) > 0 else "pipe-followup"

    st.markdown(f"""
<div class="pipeline-row">
    <div class="pipeline-box pipe-found">
        <div class="top-bar"></div>
        <span class="count">{_total_articles}</span>
        <span class="stage-name">Found</span>
        <div class="label">Working sheet</div>
    </div>
    <div class="pipeline-arrow">&#10132;</div>
    <div class="pipeline-box pipe-ready">
        <div class="top-bar"></div>
        <span class="count">{_foia_ready}</span>
        <span class="stage-name">FOIA Ready</span>
        <div class="label">{_conv_rate} yield</div>
    </div>
    <div class="pipeline-arrow">&#10132;</div>
    <div class="pipeline-box pipe-requests">
        <div class="top-bar"></div>
        <span class="count">{len(_dash_foia_requests)}</span>
        <span class="stage-name">Requests</span>
        <div class="label">{_n_draft} draft &middot; {_n_portal} portal</div>
    </div>
    <div class="pipeline-arrow">&#10132;</div>
    <div class="pipeline-box pipe-sent">
        <div class="top-bar"></div>
        <span class="count">{_n_total_sent}</span>
        <span class="stage-name">Sent</span>
        <div class="label">{_n_recv} recv &middot; {_n_denied} denied</div>
    </div>
    <div class="pipeline-arrow">&#10132;</div>
    <div class="pipeline-box {_fu_cls}">
        <div class="top-bar"></div>
        <span class="count">{len(_dash_needing_fu)}</span>
        <span class="stage-name">Follow-Ups</span>
        <div class="label">Overdue</div>
    </div>
</div>
""", unsafe_allow_html=True)

    # ── FOIA Status Breakdown — Donut Chart + Pills ──────────────────────
    st.markdown('<p class="section-label">Request Status Breakdown</p>', unsafe_allow_html=True)

    _chart_col, _pills_col = st.columns([1, 1])

    with _chart_col:
        _status_data = {
            "Draft": _n_draft, "Portal Needed": _n_portal, "Sent": _n_sent,
            "Acknowledged": _n_ack, "In Progress": _n_prog,
            "Received": _n_recv, "Denied": _n_denied,
        }
        _status_data = {k: v for k, v in _status_data.items() if v > 0}
        if _status_data:
            _status_colors = {
                "Draft": "#6B7280", "Portal Needed": "#F59E0B", "Sent": "#3B82F6",
                "Acknowledged": "#8B5CF6", "In Progress": "#0EA5E9",
                "Received": "#22C55E", "Denied": "#EF4444",
            }
            fig_donut = go.Figure(data=[go.Pie(
                labels=list(_status_data.keys()),
                values=list(_status_data.values()),
                hole=0.6,
                marker_colors=[_status_colors.get(s, "#6B7280") for s in _status_data.keys()],
                textinfo="value",
                textfont_size=13,
                hovertemplate="%{label}: %{value}<extra></extra>",
            )])
            fig_donut.update_layout(
                showlegend=True,
                legend=dict(font=dict(size=11, color="rgba(255,255,255,0.6)"), bgcolor="rgba(0,0,0,0)"),
                margin=dict(t=10, b=10, l=10, r=10),
                height=220,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="rgba(255,255,255,0.7)",
                annotations=[dict(text=f"{len(_dash_foia_requests)}", x=0.5, y=0.5, font_size=28, font_color="white", showarrow=False)],
            )
            st.plotly_chart(fig_donut, width="stretch", key="dash_donut")
        else:
            st.info("No requests yet")

    with _pills_col:
        _pill_map = {
            "Draft": "pill-draft", "Portal Needed": "pill-portal", "Sent": "pill-sent",
            "Acknowledged": "pill-ack", "In Progress": "pill-progress",
            "Received": "pill-received", "Denied": "pill-denied",
        }
        _pills_html = ""
        for status, count in [("Draft", _n_draft), ("Portal Needed", _n_portal), ("Sent", _n_sent),
                               ("Acknowledged", _n_ack), ("In Progress", _n_prog),
                               ("Received", _n_recv), ("Denied", _n_denied)]:
            _pills_html += f'<span class="status-pill {_pill_map[status]}" style="margin: 3px 4px;">{status}: {count}</span> '
        st.markdown(f'<div style="padding: 10px 0;">{_pills_html}</div>', unsafe_allow_html=True)

        if _n_total_sent > 0:
            st.caption(f"Success rate: **{_foia_success_rate}%** ({_n_recv} received of {_n_total_sent} sent)")

        if _n_portal > 0:
            st.info(f"{_n_portal} request(s) queued for Kevin (OpenClaw) portal submission.")

    st.divider()

    # ── Kevin (OpenClaw + Monitor) Status ─────────────────────────────────
    _kevin_col, _activity_col = st.columns([1, 1])

    with _kevin_col:
        st.markdown('<p class="section-label">Kevin (AI Agent)</p>', unsafe_allow_html=True)

        # --- Detect OpenClaw via sheet data (no HTTP calls = no timeouts) ---
        _openclaw_online = False
        _openclaw_detail = "Not detected"

        if foia_sheet_id:
            # Method 1: Check Monitor tab row 3 for OpenClaw heartbeat
            try:
                _oc_hb_result = (
                    service.spreadsheets()
                    .values()
                    .get(spreadsheetId=foia_sheet_id, range="Monitor!A3:G3")
                    .execute()
                )
                _oc_hb_rows = _oc_hb_result.get("values", [])
                if _oc_hb_rows and _oc_hb_rows[0] and _oc_hb_rows[0][0]:
                    _oc_hb_ts = datetime.strptime(_oc_hb_rows[0][0], "%Y-%m-%d %H:%M:%S")
                    _oc_hb_age = (datetime.now() - _oc_hb_ts).total_seconds() / 60
                    if _oc_hb_age < 10:
                        _openclaw_online = True
                        _openclaw_detail = f"Heartbeat {int(_oc_hb_age)}m ago"
                    elif _oc_hb_age < 60:
                        _openclaw_detail = f"Last heartbeat {int(_oc_hb_age)}m ago"
                    else:
                        _openclaw_detail = f"Last seen {int(_oc_hb_age / 60)}h ago"
            except Exception:
                pass

            # Method 2: Check Activity Log for recent OpenClaw entries
            if not _openclaw_online:
                try:
                    _oc_activities = get_recent_activity(service, foia_sheet_id, limit=30)
                    for _act in _oc_activities:
                        if _act.get("Source", "").strip() == "OpenClaw":
                            try:
                                _oc_ts = datetime.strptime(_act["Timestamp"], "%Y-%m-%d %H:%M:%S")
                                _oc_age_min = (datetime.now() - _oc_ts).total_seconds() / 60
                                if _oc_age_min < 30:
                                    _openclaw_online = True
                                    _openclaw_detail = f"Active {int(_oc_age_min)}m ago"
                                elif _oc_age_min < 1440:
                                    _openclaw_detail = f"Last seen {int(_oc_age_min / 60)}h ago"
                                else:
                                    _openclaw_detail = f"Last seen {int(_oc_age_min / 1440)}d ago"
                                break
                            except (ValueError, TypeError):
                                continue
                except Exception:
                    pass

            # Method 3: Check FOIA requests for portal submissions (OpenClaw evidence)
            if not _openclaw_online and _openclaw_detail == "Not detected":
                try:
                    for _req in _dash_foia_requests:
                        _notes = _req.get("Notes", "").lower()
                        if "openclaw" in _notes or "submitted via" in _notes:
                            _openclaw_detail = "Has submitted portals (not currently active)"
                            break
                except Exception:
                    pass

        # Method 4: Gateway URL ping (only if configured — avoids slow timeouts)
        if not _openclaw_online and openclaw_gateway:
            try:
                import requests as _req
                _gw_url = openclaw_gateway.rstrip("/")
                _oc_resp = _req.get(f"{_gw_url}/", timeout=2)
                if _oc_resp.ok:
                    _openclaw_online = True
                    _openclaw_detail = f"Connected via gateway"
            except Exception:
                _openclaw_detail = f"Gateway unreachable ({openclaw_gateway})"

        # --- Detect Monitor Agent via sheet heartbeat (row 2) ---
        _monitor_status = {}
        try:
            if service and foia_sheet_id:
                _monitor_status = read_monitor_status(service, foia_sheet_id)
        except Exception:
            pass

        _monitor_online = False
        _monitor_minutes_ago = 999
        if _monitor_status and _monitor_status.get("timestamp"):
            try:
                _last_hb = datetime.strptime(_monitor_status["timestamp"], "%Y-%m-%d %H:%M:%S")
                _monitor_minutes_ago = (datetime.now() - _last_hb).total_seconds() / 60
                _monitor_online = _monitor_minutes_ago < 5
            except (ValueError, TypeError):
                pass

        # --- Overall Kevin status ---
        if _openclaw_online and _monitor_online:
            _kevin_dot = "kevin-dot-online"
            _kevin_label = "Fully Online"
        elif _openclaw_online or _monitor_online:
            _kevin_dot = "kevin-dot-online"
            _kevin_label = "Online"
        elif _monitor_status and _monitor_minutes_ago < 15:
            _kevin_dot = "kevin-dot-stale"
            _kevin_label = "Stale"
        else:
            _kevin_dot = "kevin-dot-offline"
            _kevin_label = "Offline"

        # Build sub-status rows
        _oc_dot = "kevin-dot-online" if _openclaw_online else "kevin-dot-offline"
        _mon_dot = "kevin-dot-online" if _monitor_online else ("kevin-dot-stale" if _monitor_minutes_ago < 15 else "kevin-dot-offline")
        if _monitor_online:
            _mon_text = f"Running &middot; {_monitor_status.get('status', '')} &middot; {int(_monitor_minutes_ago)}m ago"
        elif _monitor_minutes_ago < 15:
            _mon_text = f"Stale &middot; last seen {int(_monitor_minutes_ago)}m ago"
        else:
            _mon_text = "Not running"

        _queued_html = ""
        if _monitor_status and _monitor_status.get("articles_queued"):
            _queued_html = f"""<div class="kevin-stat">Queued: {_monitor_status.get('articles_queued', '0')} articles &middot; {_monitor_status.get('foia_queued', '0')} FOIA &middot; {_monitor_status.get('portal_queued', '0')} portal</div>"""
        if _monitor_status and _monitor_status.get("last_action"):
            _queued_html += f"""<div class="kevin-stat">Last action: {_monitor_status['last_action']}</div>"""
        if _monitor_status and _monitor_status.get("errors"):
            _queued_html += f"""<div class="kevin-stat" style="color:var(--danger);">Errors: {_monitor_status['errors']}</div>"""

        st.markdown(f"""
<div class="kevin-panel">
    <div class="kevin-header">
        <div class="kevin-dot {_kevin_dot}"></div>
        <span style="font-weight:600; font-size:14px;">Kevin</span>
        <span style="font-size:12px; color:var(--text-muted);">{_kevin_label}</span>
    </div>
    <div style="display:flex; align-items:center; gap:8px; margin: 8px 0 4px 0;">
        <div class="kevin-dot {_oc_dot}" style="width:6px; height:6px;"></div>
        <span style="font-size:12px;"><strong>OpenClaw</strong> &nbsp; <span style="color:var(--text-muted);">{_openclaw_detail}</span></span>
    </div>
    <div style="display:flex; align-items:center; gap:8px; margin: 4px 0;">
        <div class="kevin-dot {_mon_dot}" style="width:6px; height:6px;"></div>
        <span style="font-size:12px;"><strong>Monitor Agent</strong> &nbsp; <span style="color:var(--text-muted);">{_mon_text}</span></span>
    </div>
    {_queued_html}
</div>
""", unsafe_allow_html=True)

        if not _openclaw_online and not _monitor_online:
            st.caption("Set your OpenClaw Gateway URL in FOIA Settings, or run `monitor_agent.py` locally.")

    # ── Activity Feed ─────────────────────────────────────────────────────
    with _activity_col:
        st.markdown('<p class="section-label">Recent Activity</p>', unsafe_allow_html=True)
        _activities = []
        try:
            if service and foia_sheet_id:
                _activities = get_recent_activity(service, foia_sheet_id, limit=15)
        except Exception:
            pass

        if _activities:
            _source_colors = {
                "Finder": "#3B82F6", "Analyzer": "#8B5CF6", "FOIA": "#22C55E",
                "Monitor": "#F59E0B", "App": "#6B7280",
            }
            _feed_html = ""
            for act in _activities[:10]:
                _src = act.get("Source", "App")
                _color = _source_colors.get(_src, "#6B7280")
                _ts = act.get("Timestamp", "")
                # Show just time portion
                _time_part = _ts.split(" ")[-1][:5] if " " in _ts else _ts[:5]
                _feed_html += f"""
<div class="activity-item">
    <div class="activity-dot" style="background:{_color};"></div>
    <span class="activity-time">{_time_part}</span>
    <span class="activity-text"><strong>{act.get('Action', '')}</strong> {act.get('Details', '')}</span>
    <span class="activity-source">{_src}</span>
</div>"""
            st.markdown(_feed_html, unsafe_allow_html=True)
        else:
            st.caption("No activity logged yet. Activity will appear as you use the system.")

    st.divider()

    # ── Charts Row ────────────────────────────────────────────────────────
    if _dash_foia_requests:
        _chart1_col, _chart2_col = st.columns(2)

        with _chart1_col:
            st.markdown('<p class="section-label">FOIA Requests Over Time</p>', unsafe_allow_html=True)
            _req_dates = [r.get("Date Created", "").strip() for r in _dash_foia_requests if r.get("Date Created", "").strip()]
            if _req_dates:
                _date_df = pd.DataFrame({"Date": _req_dates})
                _date_df["Date"] = pd.to_datetime(_date_df["Date"], errors="coerce")
                _date_df = _date_df.dropna()
                if not _date_df.empty:
                    _date_counts = _date_df.groupby(_date_df["Date"].dt.date).size().reset_index(name="Count")
                    _date_counts.columns = ["Date", "Count"]
                    _date_counts["Cumulative"] = _date_counts["Count"].cumsum()
                    fig_timeline = px.area(
                        _date_counts, x="Date", y="Cumulative",
                        color_discrete_sequence=["#3B82F6"],
                    )
                    fig_timeline.update_layout(
                        margin=dict(t=10, b=30, l=40, r=10), height=200,
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                        font_color="rgba(255,255,255,0.6)",
                        xaxis=dict(gridcolor="rgba(255,255,255,0.04)", title=""),
                        yaxis=dict(gridcolor="rgba(255,255,255,0.04)", title=""),
                    )
                    st.plotly_chart(fig_timeline, width="stretch", key="dash_timeline")

        with _chart2_col:
            st.markdown('<p class="section-label">Requests by State</p>', unsafe_allow_html=True)
            _state_counts = {}
            for r in _dash_foia_requests:
                s = r.get("State", "").strip()
                if s:
                    _state_counts[s] = _state_counts.get(s, 0) + 1
            if _state_counts:
                _sorted_states = sorted(_state_counts.items(), key=lambda x: x[1], reverse=True)[:10]
                fig_states = px.bar(
                    x=[s[1] for s in _sorted_states], y=[s[0] for s in _sorted_states],
                    orientation="h", color_discrete_sequence=["#8B5CF6"],
                )
                fig_states.update_layout(
                    margin=dict(t=10, b=10, l=10, r=10), height=200,
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font_color="rgba(255,255,255,0.6)",
                    xaxis=dict(gridcolor="rgba(255,255,255,0.04)", title=""),
                    yaxis=dict(title="", autorange="reversed"),
                    showlegend=False,
                )
                st.plotly_chart(fig_states, width="stretch", key="dash_states")

        st.divider()

    # ── Recent Requests Table ─────────────────────────────────────────────
    st.markdown('<p class="section-label">Recent Requests</p>', unsafe_allow_html=True)
    if _dash_foia_requests:
        _recent = sorted(_dash_foia_requests, key=lambda r: r.get("Date Created", ""), reverse=True)[:10]
        _recent_df = pd.DataFrame(_recent)
        _show_cols = ["Date Created", "Suspect Name", "Police Department", "State", "Status", "Request Method"]
        _show_cols = [c for c in _show_cols if c in _recent_df.columns]
        st.dataframe(_recent_df[_show_cols], width="stretch", hide_index=True)
    else:
        st.info("No FOIA requests yet. Use the Finder to find articles, then process them in the FOIA tab.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: ARTICLE FINDER
# ══════════════════════════════════════════════════════════════════════════════
with tab_finder:
    if not serpapi_key:
        st.info("Add your SerpAPI key in the sidebar to enable the Article Finder.")
    else:
        st.markdown('<p class="section-label">Search Parameters</p>', unsafe_allow_html=True)

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
        st.markdown('<p class="section-label">Date Range</p>', unsafe_allow_html=True)
        col_from, col_to = st.columns(2)
        with col_from:
            date_from = st.date_input("From", value=datetime.now().date() - timedelta(days=30))
        with col_to:
            date_to = st.date_input("To", value=datetime.now().date())

        if date_from > date_to:
            st.error("'From' date must be before 'To' date.")
            st.stop()

        # Charges — preset categories + custom input
        st.markdown('<p class="section-label">Charges</p>', unsafe_allow_html=True)
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

        if st.button("Find Articles", type="primary", width="stretch"):
            with st.spinner("Loading master sheet..."):
                try:
                    master_data = get_master_data(service, master_sheet_id, master_tab)
                except Exception as e:
                    st.error(f"Failed to load master sheet: {e}")
                    st.stop()

            # ── Step 1: Search ────────────────────────────────────────────
            st.divider()
            st.markdown('<p class="section-label">Step 1 — Searching</p>', unsafe_allow_html=True)
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
                st.dataframe(raw_df, width="stretch", hide_index=True)

            # ── Step 2: Deduplicate ───────────────────────────────────────
            st.markdown('<p class="section-label">Step 2 — Removing duplicates</p>', unsafe_allow_html=True)
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
            st.markdown('<p class="section-label">Step 3 — Analyzing articles</p>', unsafe_allow_html=True)
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
                    st.dataframe(live_df, width="stretch", hide_index=True)

                time.sleep(1.5)

            analyze_progress.progress(1.0)
            analyze_status.empty()

            if not analyzed:
                st.warning("No articles could be analyzed.")
                st.stop()

            # ── Step 4: Rank & Filter ─────────────────────────────────────
            st.markdown('<p class="section-label">Step 4 — Ranking & exporting</p>', unsafe_allow_html=True)

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

            st.dataframe(final_df, width="stretch", hide_index=True, height=400)

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

            export_ok = False
            for attempt in range(3):
                with st.spinner(f"Exporting to Google Sheet..."):
                    try:
                        svc = _reconnect_sheets() if attempt > 0 else service
                        if attempt == 0 or not locals().get("new_sheet_id"):
                            new_sheet_id = create_new_sheet(svc, sheet_title, make_public=True)
                        append_rows_to_sheet(svc, new_sheet_id, "Sheet1", [header_row] + data_rows)
                        sheet_url = f"https://docs.google.com/spreadsheets/d/{new_sheet_id}"
                        st.success(f"Exported {len(top_articles)} articles to sheet")
                        st.markdown(f"[Open Google Sheet]({sheet_url})")
                        export_ok = True
                        break
                    except Exception:
                        if attempt < 2:
                            time.sleep(2)
                        else:
                            st.error("Sheet export failed. Try again or download CSV below.")
            if not export_ok:
                csv = final_df.to_csv(index=False)
                st.download_button("Download CSV (backup)", csv, "found_articles.csv", "text/csv")

            # ── Save to Archive ───────────────────────────────────────────
            if foia_sheet_id:
                try:
                    _search_query = f"{finder_state or 'All States'} {finder_city} {finder_county}".strip()
                    _archive_articles = []
                    for r in valid:
                        _archive_articles.append({
                            "url": r.get("url", ""),
                            "title": r.get("title", ""),
                            "source": r.get("source", ""),
                            "suspect_name": r.get("suspect_name", ""),
                            "incident_date": r.get("incident_date", ""),
                            "police_dept": r.get("police_dept", ""),
                            "state": r.get("state", ""),
                            "search_query": _search_query,
                            "foia_score": r.get("foia_score", ""),
                            "youtube_score": r.get("youtube_score", ""),
                            "same_day_arrest": r.get("same_day_arrest", ""),
                            "charges": r.get("charges", ""),
                            "incident_location": r.get("incident_location", ""),
                            "officer_names": r.get("officer_names", ""),
                            "case_number": r.get("case_number", ""),
                        })
                    ensure_archive_headers(service, foia_sheet_id)
                    _archived_count = append_to_archive(service, foia_sheet_id, _archive_articles)
                    if _archived_count > 0:
                        st.caption(f"Saved {_archived_count} new articles to Archive")
                        log_activity(service, foia_sheet_id, "Articles Archived",
                                     f"{_archived_count} articles from search: {_search_query}", "Finder")
                except Exception as e:
                    st.caption(f"Archive save skipped: {e}")

            # ── Send FOIA Requests + Import to Working Sheet ─────────────
            st.divider()
            st.markdown('<p class="section-label">Next Steps</p>', unsafe_allow_html=True)

            # ── Primary action: Send FOIA Requests ──
            _foia_eligible = list(top_articles)
            _can_auto_foia = bool(foia_sheet_id and foia_email and foia_email_password and serpapi_key)

            if not _can_auto_foia:
                _missing_foia = []
                if not foia_sheet_id:
                    _missing_foia.append("FOIA Sheet ID")
                if not foia_email:
                    _missing_foia.append("FOIA Email")
                if not foia_email_password:
                    _missing_foia.append("Email App Password")
                if not serpapi_key:
                    _missing_foia.append("SerpAPI Key")
                st.warning(f"To send FOIA requests, set in sidebar: {', '.join(_missing_foia)}")

            if st.button(
                f"Send {len(_foia_eligible)} FOIA Requests Now",
                type="primary",
                width="stretch",
                disabled=not (_foia_eligible and _can_auto_foia),
                help="Find PD contacts, generate FOIA letters, send emails or queue portal requests for Kevin",
            ):
                _existing_foia = get_existing_foia_urls(service, foia_sheet_id)
                _sender_name = foia_email.split("@")[0].replace(".", " ").title() if foia_email else "Records Requester"
                _pd_db = get_pd_database(service, foia_sheet_id)
                _foia_progress = st.progress(0)
                _foia_status = st.empty()
                _foia_results = {"sent": 0, "portal_draft": 0, "draft": 0, "failed": 0, "skipped": 0}

                for fi, fart in enumerate(_foia_eligible):
                    _foia_progress.progress((fi + 1) / len(_foia_eligible))
                    _foia_status.caption(f"Processing {fi+1}/{len(_foia_eligible)}: **{fart.get('suspect_name', 'Unknown')}** — {fart.get('police_dept', '')}")

                    if _normalize_url(fart.get("url", "")) in _existing_foia:
                        _foia_results["skipped"] += 1
                        continue

                    try:
                        _scraped = scrape_article(fart["url"])
                        _art_text = _scraped.get("text", "") if _scraped else ""
                    except Exception:
                        _art_text = ""

                    _result = process_single_request(
                        article=fart, article_text=_art_text,
                        sender_name=_sender_name, anthropic_key=anthropic_key,
                        serpapi_key=serpapi_key, foia_email=foia_email,
                        foia_email_password=foia_email_password, pd_db=_pd_db,
                        service=service, foia_sheet_id=foia_sheet_id,
                        portal_credentials=portal_credentials,
                    )
                    _foia_results[_result["status"]] = _foia_results.get(_result["status"], 0) + 1
                    if _result["status"] == "sent":
                        st.success(f"{fart.get('suspect_name', '')}: {_result['details']}")
                    elif _result["status"] == "portal_draft":
                        st.info(f"{fart.get('suspect_name', '')}: {_result['details']}")
                    elif _result["status"] == "draft":
                        st.warning(f"{fart.get('suspect_name', '')}: {_result['details']}")
                    else:
                        st.error(f"{fart.get('suspect_name', '')}: {_result['details']}")

                    if foia_sheet_id:
                        log_activity(service, foia_sheet_id, f"FOIA {_result['status'].title()}",
                                     f"{fart.get('suspect_name', '')} — {fart.get('police_dept', '')}", "FOIA")

                _foia_status.empty()
                st.markdown("---")
                _sc1, _sc2, _sc3, _sc4, _sc5 = st.columns(5)
                _sc1.metric("Emailed", _foia_results.get("sent", 0))
                _sc2.metric("Portal (Kevin)", _foia_results.get("portal_draft", 0))
                _sc3.metric("Draft", _foia_results.get("draft", 0))
                _sc4.metric("Failed", _foia_results.get("failed", 0))
                _sc5.metric("Already Done", _foia_results.get("skipped", 0))

                # Auto-signal Kevin if portal requests were queued
                if _foia_results.get("portal_draft", 0) > 0 and foia_sheet_id:
                    try:
                        from sheets_client import write_kevin_trigger
                        write_kevin_trigger(service, foia_sheet_id, "GO")
                        st.info(f"{_foia_results['portal_draft']} portal request(s) sent to Kevin for browser submission.")
                    except Exception:
                        pass

                if _foia_results.get("draft", 0) > 0:
                    st.warning(f"{_foia_results['draft']} request(s) saved as Draft — no verified email found. Check the FOIA Requests tab to review and manually send.")

            # ── Secondary action: Import to Working Sheet ──
            if st.button("Import to Working Sheet", type="secondary", width="stretch",
                         help="Add articles to Working Sheet for review in the Analyzer tab"):
                with st.spinner("Importing articles to working sheet..."):
                    try:
                        _import_data = []
                        for r in top_articles:
                            _import_data.append({
                                "url": r.get("url", ""),
                                "suspect_name": r.get("suspect_name", ""),
                                "incident_date": r.get("incident_date", ""),
                                "police_dept": r.get("police_dept", ""),
                                "state": r.get("state", ""),
                                "duplicate": r.get("duplicate", ""),
                                "same_day_arrest": r.get("same_day_arrest", ""),
                                "foia_score": r.get("foia_score", ""),
                                "youtube_score": r.get("youtube_score", ""),
                            })
                        _imported = append_articles_to_working_sheet(service, working_sheet_id, working_tab, _import_data)
                        st.success(f"Imported {_imported} new articles to Working Sheet")
                        if foia_sheet_id:
                            log_activity(service, foia_sheet_id, "Articles Imported",
                                         f"{_imported} articles added to working sheet", "Finder")
                    except Exception as e:
                        st.error(f"Import failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: ARTICLE ANALYZER
# ══════════════════════════════════════════════════════════════════════════════
with tab_analyzer:
    col_refresh, col_clear = st.columns(2)

    with col_refresh:
        if st.button("Refresh", width="stretch"):
            st.rerun()

    with col_clear:
        clear_results = st.button("Clear Results", width="stretch")

    try:
        rows = get_all_rows(service, working_sheet_id, working_tab)
    except Exception as e:
        # Auto-reconnect on broken pipe / stale connection
        try:
            service = _reconnect_sheets()
            rows = get_all_rows(service, working_sheet_id, working_tab)
        except Exception as e2:
            st.error(f"Failed to open working sheet: {e2}")
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

    st.dataframe(df, width="stretch", height=320, hide_index=True)

    urls = [r[0].strip() for r in padded_rows if r[0].strip()]
    already_processed = sum(1 for r in padded_rows if r[5].strip())
    to_process = len(urls) - already_processed

    m1, m2, m3 = st.columns(3)
    m1.metric("Total", len(urls))
    m2.metric("Done", already_processed)
    m3.metric("Remaining", to_process)

    st.markdown("")

    if st.button("Run Analysis", type="primary", width="stretch", disabled=to_process == 0):
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
                    st.dataframe(pd.DataFrame(results_table), width="stretch", hide_index=True)
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
                    st.dataframe(pd.DataFrame(results_table), width="stretch", hide_index=True)
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
                st.dataframe(pd.DataFrame(results_table), width="stretch", hide_index=True)

            time.sleep(1.5)

        progress_bar.progress(1.0)
        status_text.empty()

        st.divider()
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Analyzed", success_count)
        s2.metric("Duplicates", dup_count)
        s3.metric("Errors", error_count)
        s4.metric("Skipped", skip_count)
        st.success("Analysis complete. Results written to sheet.")

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
            ensure_foia_headers(service, foia_sheet_id)
            ensure_pd_db_headers(service, foia_sheet_id)
        except Exception:
            try:
                service = _reconnect_sheets()
                ensure_foia_headers(service, foia_sheet_id)
                ensure_pd_db_headers(service, foia_sheet_id)
            except Exception as e:
                st.error(f"Failed to connect to FOIA sheet: {e}")
                st.stop()

        # ── Load data ─────────────────────────────────────────────────────
        try:
            working_rows = get_all_rows(service, working_sheet_id, working_tab)
            existing_urls = get_existing_foia_urls(service, foia_sheet_id)
            pd_db = get_pd_database(service, foia_sheet_id)
        except Exception as e:
            st.error(f"Failed to load data: {e}")
            working_rows = []
            existing_urls = set()
            pd_db = []

        try:
            all_requests = get_foia_requests(service, foia_sheet_id)
        except Exception:
            all_requests = []

        # Filter requestable articles
        requestable = []
        if working_rows and len(working_rows) > 1:
            for i, row in enumerate(working_rows[1:], start=2):
                while len(row) < 9:
                    row.append("")
                url = row[0].strip()
                foia_score_str = row[7].strip() if len(row) > 7 else ""
                if not foia_score_str:
                    continue
                try:
                    foia_score = int(float(foia_score_str))
                except (ValueError, TypeError):
                    continue
                if _normalize_url(url) not in existing_urls:
                    requestable.append({
                        "row_num": i, "url": url,
                        "suspect_name": row[1].strip() if len(row) > 1 else "",
                        "incident_date": row[2].strip() if len(row) > 2 else "",
                        "police_dept": row[3].strip() if len(row) > 3 else "",
                        "state": row[4].strip() if len(row) > 4 else "",
                        "foia_score": foia_score,
                    })

        sender_name = foia_email.split("@")[0].replace(".", " ").title() if foia_email else "Records Requester"
        if "foia_drafts" not in st.session_state:
            st.session_state.foia_drafts = {}
        if "pd_suggestions" not in st.session_state:
            st.session_state.pd_suggestions = {}

        # ── Overview metrics ──────────────────────────────────────────────
        statuses = [r.get("Status", "Unknown") for r in all_requests]
        status_labels = ["Sent", "Acknowledged", "Portal Needed", "Draft", "In Progress", "Received", "Denied", "Failed", "Manual Needed"]
        m_cols = st.columns(5)
        m_cols[0].metric("New to Send", len(requestable))
        m_cols[1].metric("Emailed", statuses.count("Sent") + statuses.count("Acknowledged"))
        m_cols[2].metric("Portal (Kevin)", statuses.count("Portal Needed"))
        m_cols[3].metric("Drafts", statuses.count("Draft"))
        m_cols[4].metric("Received", statuses.count("Received"))

        # ── Section A: Process New Requests ───────────────────────────────
        st.markdown('<p class="section-label">Send New Requests</p>', unsafe_allow_html=True)

        if not requestable:
            total_articles = len(working_rows) - 1 if working_rows and len(working_rows) > 1 else 0
            if total_articles == 0:
                st.info("Working sheet is empty. Add articles and run the Analyzer first.")
            else:
                scored = sum(1 for r in working_rows[1:] if len(r) > 7 and r[7].strip())
                if scored == 0:
                    st.info(f"{total_articles} articles but none scored yet. Run the Analyzer.")
                else:
                    st.success("All scored articles have been requested.")
        else:
            can_auto = bool(foia_email and foia_email_password and serpapi_key)
            if not can_auto:
                missing = []
                if not foia_email: missing.append("FOIA Email")
                if not foia_email_password: missing.append("Email App Password")
                if not serpapi_key: missing.append("SerpAPI Key")
                st.warning(f"Missing: {', '.join(missing)}")

            proc_col1, proc_col2 = st.columns([3, 1])
            with proc_col1:
                st.caption(f"{len(requestable)} articles ready — will search for department emails, generate letters, and send automatically")
            with proc_col2:
                _do_process = st.button(
                    f"Process All {len(requestable)}",
                    type="primary",
                    disabled=not can_auto,
                )

            if _do_process:
                progress_bar = st.progress(0)
                log_container = st.container()
                results_summary = {"sent": 0, "portal_draft": 0, "draft": 0, "failed": 0}

                for i, article in enumerate(requestable):
                    progress_bar.progress((i + 1) / len(requestable))
                    with log_container:
                        st.caption(f"[{i+1}/{len(requestable)}] {article['suspect_name']} — {article['police_dept']}, {article['state']}")

                    try:
                        scraped = scrape_article(article["url"])
                        article_text = scraped.get("text", "") if scraped else ""
                    except Exception:
                        article_text = ""

                    result = process_single_request(
                        article=article, article_text=article_text,
                        sender_name=sender_name, anthropic_key=anthropic_key,
                        serpapi_key=serpapi_key, foia_email=foia_email,
                        foia_email_password=foia_email_password, pd_db=pd_db,
                        service=service, foia_sheet_id=foia_sheet_id,
                        portal_credentials=portal_credentials,
                    )

                    results_summary[result["status"]] = results_summary.get(result["status"], 0) + 1
                    with log_container:
                        if result["status"] == "sent":
                            st.success(f"{article['suspect_name']}: {result['details']}")
                        elif result["status"] == "portal_draft":
                            st.info(f"{article['suspect_name']}: Queued for Kevin (portal)")
                        elif result["status"] == "draft":
                            st.warning(f"{article['suspect_name']}: Saved as draft — {result['details']}")
                        else:
                            st.error(f"{article['suspect_name']}: {result['details']}")

                st.markdown("---")
                r_cols = st.columns(4)
                r_cols[0].metric("Emailed", results_summary.get("sent", 0))
                r_cols[1].metric("Portal (Kevin)", results_summary.get("portal_draft", 0))
                r_cols[2].metric("Draft", results_summary.get("draft", 0))
                r_cols[3].metric("Failed", results_summary.get("failed", 0))

                if results_summary.get("portal_draft", 0) > 0:
                    try:
                        from sheets_client import write_kevin_trigger
                        write_kevin_trigger(service, foia_sheet_id, "GO")
                        st.info(f"Signaled Kevin for {results_summary['portal_draft']} portal request(s).")
                    except Exception:
                        pass

        # ── Section B: Request Pipeline ───────────────────────────────────
        st.divider()
        st.markdown('<p class="section-label">Request Pipeline</p>', unsafe_allow_html=True)

        if not all_requests:
            st.info("No FOIA requests yet. Process articles above to create them.")
        else:
            # Bulk actions
            drafts = [r for r in all_requests if r.get("Status") == "Draft"]
            portal_needed = [r for r in all_requests if r.get("Status") == "Portal Needed"]
            _email_drafts = [d for d in drafts if d.get("Contact Info", "").strip() and "@" in d.get("Contact Info", "")]

            bulk_col1, bulk_col2 = st.columns(2)
            with bulk_col1:
                _can_send = bool(_email_drafts and foia_email and foia_email_password)
                if st.button(
                    f"Send {len(_email_drafts)} Drafts via Email" if _email_drafts else "No Drafts with Email",
                    type="primary",
                    disabled=not _can_send,
                ):
                    _prog = st.progress(0)
                    _sent = 0
                    _fail = 0
                    for ei, draft in enumerate(_email_drafts):
                        _prog.progress((ei + 1) / len(_email_drafts))
                        row_idx = next((i for i, r in enumerate(all_requests) if r.get("Request ID") == draft.get("Request ID")), None)
                        to_addr = draft.get("Contact Info", "").strip()
                        body = draft.get("Request Body", "")
                        subject = f"Public Records Request – {draft.get('Suspect Name', '')} ({draft.get('Incident Date', '')})"
                        send_result = send_email_smtp(
                            smtp_host="smtp.gmail.com", smtp_port=587,
                            email=foia_email, password=foia_email_password,
                            to_addr=to_addr, subject=subject, body=body,
                        )
                        if send_result["success"]:
                            today = datetime.now().strftime("%Y-%m-%d")
                            if row_idx is not None:
                                update_foia_row(service, foia_sheet_id, row_idx + 2, {
                                    "Status": "Sent", "Date Sent": today,
                                    "Contact Info": to_addr, "Request Method": "email",
                                })
                            _sent += 1
                        else:
                            _fail += 1
                        time.sleep(1)
                    st.success(f"Sent {_sent}" + (f", {_fail} failed" if _fail else ""))
                    log_activity(service, foia_sheet_id, "Bulk Email", f"{_sent} sent, {_fail} failed", "FOIA")
                    st.rerun()

            with bulk_col2:
                _n_portal = len(portal_needed)
                if st.button(
                    f"Signal Kevin ({_n_portal} portal)" if portal_needed else "No Portal Requests",
                    type="secondary",
                    disabled=not portal_needed,
                ):
                    try:
                        from sheets_client import write_kevin_trigger
                        write_kevin_trigger(service, foia_sheet_id, "GO")
                        st.success(f"Kevin signaled for {_n_portal} portal request(s).")
                    except Exception as e:
                        st.error(f"Failed: {e}")
                    st.rerun()

            # Bounce check
            if foia_email and foia_email_password:
                if st.button("Check for Bounced Emails", type="secondary"):
                    with st.spinner("Checking Gmail for bounce notifications..."):
                        bounced = check_bounced_emails(foia_email, foia_email_password)
                    if bounced:
                        st.warning(f"Found {len(bounced)} bounced address(es): {', '.join(bounced)}")
                        _bounced_count = 0
                        for i, req in enumerate(all_requests):
                            contact = req.get("Contact Info", "").strip().lower()
                            if contact in bounced and req.get("Status") == "Sent":
                                update_foia_row(service, foia_sheet_id, i + 2, {
                                    "Status": "Bounced",
                                    "Notes": f"Email bounced — {contact} is invalid",
                                })
                                _bounced_count += 1
                        if _bounced_count:
                            st.error(f"Marked {_bounced_count} request(s) as Bounced")
                            st.rerun()
                    else:
                        st.success("No bounced emails found")

            # Request table
            df = pd.DataFrame(all_requests)
            display_cols = ["Request ID", "Suspect Name", "Police Department", "State",
                            "Status", "Request Method", "Contact Info", "Date Sent", "Notes"]
            display_cols = [c for c in display_cols if c in df.columns]
            st.dataframe(df[display_cols], height=400)

            # Drafts missing email
            _drafts_no_email = [d for d in drafts if not (d.get("Contact Info", "").strip() and "@" in d.get("Contact Info", ""))]
            if _drafts_no_email:
                st.markdown('<p class="section-label">Drafts Missing Email</p>', unsafe_allow_html=True)
                st.caption(f"{len(_drafts_no_email)} drafts need an email address")
                for d_idx, draft in enumerate(_drafts_no_email):
                    row_idx = next((i for i, r in enumerate(all_requests) if r.get("Request ID") == draft.get("Request ID")), None)
                    with st.expander(f"{draft.get('Suspect Name', '?')} — {draft.get('Police Department', '?')}"):
                        st.caption(draft.get("Notes", ""))
                        override_email = st.text_input(
                            "Email address",
                            value=draft.get("Contact Info", ""),
                            key=f"draft_email_{d_idx}",
                            placeholder="records@department.gov",
                        )
                        if st.button("Send Now", key=f"send_draft_{d_idx}", type="primary"):
                            if not override_email or "@" not in override_email:
                                st.error("Enter a valid email address")
                            elif not foia_email or not foia_email_password:
                                st.error("Set FOIA email and app password in sidebar")
                            else:
                                body = draft.get("Request Body", "")
                                subject = f"Public Records Request – {draft.get('Suspect Name', '')} ({draft.get('Incident Date', '')})"
                                send_result = send_email_smtp(
                                    smtp_host="smtp.gmail.com", smtp_port=587,
                                    email=foia_email, password=foia_email_password,
                                    to_addr=override_email, subject=subject, body=body,
                                )
                                if send_result["success"]:
                                    today = datetime.now().strftime("%Y-%m-%d")
                                    if row_idx is not None:
                                        update_foia_row(service, foia_sheet_id, row_idx + 2, {
                                            "Status": "Sent", "Date Sent": today,
                                            "Contact Info": override_email, "Request Method": "email",
                                        })
                                    st.success(f"Sent to {override_email}")
                                    st.rerun()
                                else:
                                    st.error(f"Failed: {send_result['error']}")

            # Status update
            st.markdown('<p class="section-label">Update Status</p>', unsafe_allow_html=True)
            u_col1, u_col2, u_col3 = st.columns([2, 1, 1])
            with u_col1:
                req_ids = [r.get("Request ID", "") for r in all_requests]
                selected_req = st.selectbox("Request", req_ids, key="status_update_req")
            with u_col2:
                new_status = st.selectbox("New Status", status_labels, key="status_update_val")
            with u_col3:
                if st.button("Update", key="status_update_btn"):
                    for i, r in enumerate(all_requests):
                        if r.get("Request ID") == selected_req:
                            update_foia_row(service, foia_sheet_id, i + 2, {"Status": new_status})
                            st.success(f"Updated {selected_req} to {new_status}")
                            st.rerun()

        # ── Section C: Follow-Ups ─────────────────────────────────────────
        st.divider()
        st.markdown('<p class="section-label">Follow-Ups Needed</p>', unsafe_allow_html=True)

        if all_requests:
            needing_fu = get_requests_needing_followup(all_requests)
            if not needing_fu:
                st.info("All requests up to date — no follow-ups needed.")
            else:
                st.warning(f"**{len(needing_fu)}** requests need follow-up")
                for fu_idx, req in enumerate(needing_fu):
                    days = req.get("_days_elapsed", "?")
                    fu_count = req.get("Follow-Up Count", "0")
                    with st.expander(f"**{req.get('Police Department', '?')}** — {days} days, {fu_count} prior"):
                        st.caption(f"Suspect: {req.get('Suspect Name', '?')} | Sent: {req.get('Date Sent', '?')}")
                        fu_draft_key = f"fu_draft_{req.get('Request ID', fu_idx)}"
                        if st.button("Draft Follow-Up", key=f"fu_btn_{fu_idx}"):
                            with st.spinner("Drafting..."):
                                draft_text = draft_follow_up(req, anthropic_key)
                                draft_text += f"\n\n{sender_name}"
                                st.session_state[fu_draft_key] = draft_text
                        if fu_draft_key in st.session_state:
                            edited_fu = st.text_area("Follow-up", value=st.session_state[fu_draft_key], height=200, key=f"fu_text_{fu_idx}")
                            contact_info = req.get("Contact Info", "")
                            if contact_info and "@" in contact_info:
                                if st.button(f"Send to {contact_info}", key=f"fu_send_{fu_idx}"):
                                    subject = f"Follow-Up: Records Request – {req.get('Suspect Name', '')} ({req.get('Incident Date', '')})"
                                    result = send_email_smtp(
                                        smtp_host="smtp.gmail.com", smtp_port=587,
                                        email=foia_email, password=foia_email_password,
                                        to_addr=contact_info, subject=subject, body=edited_fu,
                                    )
                                    if result["success"]:
                                        today = datetime.now().strftime("%Y-%m-%d")
                                        row_idx = next((i for i, r in enumerate(all_requests) if r.get("Request ID") == req.get("Request ID")), None)
                                        if row_idx is not None:
                                            new_count = int(fu_count or "0") + 1
                                            update_foia_row(service, foia_sheet_id, row_idx + 2, {
                                                "Last Follow-Up": today, "Follow-Up Count": str(new_count),
                                            })
                                        st.success(f"Follow-up sent to {contact_info}")
                                    else:
                                        st.error(f"Failed: {result['error']}")
        else:
            st.info("No requests to follow up on yet.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: ARTICLE ARCHIVE
# ══════════════════════════════════════════════════════════════════════════════
with tab_archive:
    if not foia_sheet_id:
        st.info("Add your **FOIA Sheet ID** in the sidebar to access the Article Archive.")
    else:
        try:
            ensure_archive_headers(service, foia_sheet_id)
            _archive_data = get_archive_articles(service, foia_sheet_id)
        except Exception as e:
            st.error(f"Failed to load archive: {e}")
            _archive_data = []

        st.markdown('<p class="section-label">Article Archive</p>', unsafe_allow_html=True)

        # Stats row
        _arc_total = len(_archive_data)
        _arc_this_week = 0
        _arc_this_month = 0
        _arc_states = set()
        _arc_depts = set()
        _today = datetime.now()
        for _a in _archive_data:
            _arc_states.add(_a.get("State", "").strip())
            _arc_depts.add(_a.get("Police Department", "").strip())
            try:
                _df_date = datetime.strptime(_a.get("Date Found", ""), "%Y-%m-%d")
                if (_today - _df_date).days <= 7:
                    _arc_this_week += 1
                if (_today - _df_date).days <= 30:
                    _arc_this_month += 1
            except (ValueError, TypeError):
                pass
        _arc_states.discard("")
        _arc_depts.discard("")

        _am1, _am2, _am3, _am4 = st.columns(4)
        _am1.metric("Total Articles", _arc_total)
        _am2.metric("This Week", _arc_this_week)
        _am3.metric("This Month", _arc_this_month)
        _am4.metric("Departments", len(_arc_depts))

        st.divider()

        # Filters
        _filter_col1, _filter_col2, _filter_col3 = st.columns(3)
        with _filter_col1:
            _arc_state_filter = st.selectbox("Filter by State", ["All"] + sorted(_arc_states), key="arc_state_filter")
        with _filter_col2:
            _arc_score_min = st.slider("Min FOIA Score", 0, 10, 0, key="arc_score_min")
        with _filter_col3:
            _arc_search = st.text_input("Search", placeholder="Suspect name, department...", key="arc_search")

        # Apply filters
        _filtered = _archive_data
        if _arc_state_filter != "All":
            _filtered = [a for a in _filtered if a.get("State", "").strip() == _arc_state_filter]
        if _arc_score_min > 0:
            _filtered = [a for a in _filtered
                         if a.get("FOIA Score", "").strip().isdigit() and int(a["FOIA Score"]) >= _arc_score_min]
        if _arc_search:
            _q = _arc_search.lower()
            _filtered = [a for a in _filtered
                         if _q in a.get("Suspect Name", "").lower()
                         or _q in a.get("Police Department", "").lower()
                         or _q in a.get("Title", "").lower()
                         or _q in a.get("URL", "").lower()]

        st.caption(f"Showing {len(_filtered)} of {_arc_total} articles")

        if _filtered:
            _arc_df = pd.DataFrame(_filtered)
            _arc_show_cols = ["Date Found", "Suspect Name", "Police Department", "State",
                              "FOIA Score", "YouTube Score", "Same Day Arrest", "FOIA Status", "URL"]
            _arc_show_cols = [c for c in _arc_show_cols if c in _arc_df.columns]
            st.dataframe(_arc_df[_arc_show_cols], width="stretch", hide_index=True, height=500)
        else:
            st.info("No articles match your filters. Run the Finder to populate the archive.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: TITLE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
with tab_titles:
    if not gemini_key or not youtube_key:
        st.info("Add your **Gemini API Key** and **YouTube API Key** in the sidebar to enable the Title Generator.")
    else:
        st.markdown('<p class="section-label">YouTube Video</p>', unsafe_allow_html=True)

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

        if st.button("Generate Titles", type="primary", width="stretch", disabled=not video_url.strip()):
            video_id = extract_video_id(video_url.strip())
            if not video_id:
                st.error("Invalid YouTube URL. Please paste a valid YouTube link.")
                st.stop()

            # ── Step 1: Analyze the video ────────────────────────────────
            st.divider()
            st.markdown('<p class="section-label">Step 1 — Understanding your video</p>', unsafe_allow_html=True)
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
            st.markdown('<p class="section-label">Step 2 — Researching similar videos</p>', unsafe_allow_html=True)
            with st.spinner("Searching YouTube for top-performing similar videos..."):
                similar_videos = search_similar_videos(video_analysis, youtube_key)

            st.caption(f"Found **{len(similar_videos)}** similar videos for reference")

            with st.expander(f"Similar videos ({len(similar_videos)})", expanded=False):
                for sv in similar_videos[:10]:
                    st.markdown(f"- **{sv['title']}** — {sv['views']:,} views ({sv['channel']})")

            # ── Step 3: Generate titles ──────────────────────────────────
            st.markdown('<p class="section-label">Step 3 — Generating titles</p>', unsafe_allow_html=True)
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

            st.divider()
            st.markdown('<p class="section-label">Your Titles</p>', unsafe_allow_html=True)

            for i, t in enumerate(titles):
                confidence = t.get("confidence", 0)
                if confidence >= 8:
                    color = "#10B981"
                elif confidence >= 6:
                    color = "#F59E0B"
                else:
                    color = "#EF4444"

                col_title, col_conf = st.columns([5, 1])
                with col_title:
                    st.markdown(f"**{i+1}. {t['title']}**")
                    st.caption(f"{t.get('structure', '')} · {t.get('hook', '')} · {t.get('reasoning', '')}")
                with col_conf:
                    st.markdown(f'<p style="font-size:20px;font-weight:600;color:{color};text-align:right;margin-top:8px;">{confidence}/10</p>', unsafe_allow_html=True)

                if i < len(titles) - 1:
                    st.markdown("<hr style='margin: 4px 0; opacity: 0.05;'>", unsafe_allow_html=True)

            # Copy-friendly list
            st.divider()
            with st.expander("Copy-paste list", expanded=True):
                title_list = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(titles))
                st.code(title_list, language=None)
