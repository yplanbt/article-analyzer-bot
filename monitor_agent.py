"""Kevin — the monitoring/orchestration agent.

Runs locally or on the automation MacBook. Polls Google Sheet and orchestrates
the full pipeline: analysis → FOIA request → portal submission → follow-ups.

Usage (local — uses existing OAuth token):
    export SHEET_ID="your-foia-sheet-id"
    export ANTHROPIC_API_KEY="sk-ant-..."
    python monitor_agent.py

Usage (automation MacBook — uses service account):
    export SHEET_ID="your-foia-sheet-id"
    export SA_KEY_PATH="/path/to/service-account-key.json"
    export ANTHROPIC_API_KEY="sk-ant-..."
    export SERPAPI_KEY="..."
    export FOIA_EMAIL="..."
    export FOIA_EMAIL_PASSWORD="..."
    python monitor_agent.py
"""

import os
import sys
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kevin")

# Configuration from environment (falls back to .env file)
SHEET_ID = os.environ.get("SHEET_ID", "")
SA_KEY_PATH = os.environ.get("SA_KEY_PATH", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
FOIA_EMAIL = os.environ.get("FOIA_EMAIL", "")
FOIA_EMAIL_PASSWORD = os.environ.get("FOIA_EMAIL_PASSWORD", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
FULL_SCAN_INTERVAL = int(os.environ.get("FULL_SCAN_INTERVAL", "300"))
AUTO_SEND_EMAILS = os.environ.get("AUTO_SEND_EMAILS", "true").lower() == "true"

from sheets_client import (
    get_service_account_sheets, get_sheets_service,
    get_archive_articles, ensure_archive_headers,
    get_foia_requests, ensure_foia_headers, get_existing_foia_urls,
    write_monitor_heartbeat, log_activity,
    write_kevin_trigger, read_kevin_trigger,
    _normalize_url,
)
from foia_requester import (
    process_single_request, get_requests_needing_followup,
    draft_follow_up, send_email_smtp,
)
from article_scraper import scrape_article
from pd_database import get_pd_database


def validate_config():
    """Check that all required config is set."""
    missing = []
    if not SHEET_ID:
        missing.append("SHEET_ID")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    if SA_KEY_PATH and not os.path.exists(SA_KEY_PATH):
        logger.warning(f"Service account key not found: {SA_KEY_PATH}, falling back to OAuth")


def get_service():
    """Connect to Google Sheets. Uses service account if available, otherwise OAuth."""
    if SA_KEY_PATH and os.path.exists(SA_KEY_PATH):
        logger.info("Using service account authentication")
        return get_service_account_sheets(SA_KEY_PATH)
    else:
        logger.info("Using OAuth authentication (local mode)")
        return get_sheets_service()


def check_high_scoring(service) -> int:
    """Find archive articles with any FOIA score not yet in Requests. Auto-create FOIA requests."""
    if not FOIA_EMAIL or not SERPAPI_KEY:
        logger.info("Skipping FOIA generation (no email or SerpAPI key)")
        return 0

    try:
        archive = get_archive_articles(service, SHEET_ID)
        existing_urls = get_existing_foia_urls(service, SHEET_ID)
        pd_db = get_pd_database(service, SHEET_ID)
    except Exception as e:
        logger.error(f"Failed to load data for FOIA check: {e}")
        return 0

    candidates = []
    for a in archive:
        score_str = a.get("FOIA Score", "").strip()
        if not score_str or not score_str.isdigit():
            continue
        url = a.get("URL", "").strip()
        if not url or _normalize_url(url) in existing_urls:
            continue
        candidates.append({
            "url": url,
            "suspect_name": a.get("Suspect Name", ""),
            "incident_date": a.get("Incident Date", ""),
            "police_dept": a.get("Police Department", ""),
            "state": a.get("State", ""),
            "foia_score": int(score_str),
        })

    if not candidates:
        return 0

    logger.info(f"Found {len(candidates)} articles ready for FOIA requests")
    sender_name = FOIA_EMAIL.split("@")[0].replace(".", " ").title() if FOIA_EMAIL else "Records Requester"
    processed = 0

    for article in candidates[:5]:  # Process max 5 per cycle
        try:
            scraped = scrape_article(article["url"])
            article_text = scraped.get("text", "") if scraped else ""
        except Exception:
            article_text = ""

        try:
            result = process_single_request(
                article=article,
                article_text=article_text,
                sender_name=sender_name,
                anthropic_key=ANTHROPIC_API_KEY,
                serpapi_key=SERPAPI_KEY,
                foia_email=FOIA_EMAIL,
                foia_email_password=FOIA_EMAIL_PASSWORD,
                pd_db=pd_db,
                service=service,
                foia_sheet_id=SHEET_ID,
                portal_credentials=None,
            )
            logger.info(f"FOIA {result['status']}: {article['suspect_name']} — {result['details']}")
            log_activity(service, SHEET_ID, f"FOIA {result['status'].title()}",
                         f"{article['suspect_name']} — {article['police_dept']}", "Monitor")
            processed += 1
        except Exception as e:
            logger.error(f"Failed to process FOIA for {article['suspect_name']}: {e}")

    return processed


def check_follow_ups(service) -> int:
    """Check for FOIA requests needing follow-up and send them."""
    if not FOIA_EMAIL or not FOIA_EMAIL_PASSWORD or not AUTO_SEND_EMAILS:
        return 0

    try:
        requests = get_foia_requests(service, SHEET_ID)
        overdue = get_requests_needing_followup(requests)
    except Exception as e:
        logger.error(f"Failed to check follow-ups: {e}")
        return 0

    if not overdue:
        return 0

    logger.info(f"Found {len(overdue)} requests needing follow-up")
    sent = 0

    for req in overdue[:3]:  # Max 3 follow-ups per cycle
        try:
            follow_up = draft_follow_up(
                request_data=req,
                anthropic_key=ANTHROPIC_API_KEY,
            )
            if follow_up and req.get("Contact Info"):
                send_email_smtp(
                    to_email=req["Contact Info"],
                    subject=f"Follow-Up: FOIA Request {req.get('Request ID', '')}",
                    body=follow_up,
                    from_email=FOIA_EMAIL,
                    password=FOIA_EMAIL_PASSWORD,
                )
                logger.info(f"Follow-up sent for {req.get('Suspect Name', '')}")
                log_activity(service, SHEET_ID, "Follow-Up Sent",
                             f"{req.get('Suspect Name', '')} — {req.get('Police Department', '')}", "Monitor")
                sent += 1
        except Exception as e:
            logger.error(f"Follow-up failed for {req.get('Suspect Name', '')}: {e}")

    return sent


def get_queue_counts(service) -> dict:
    """Get counts of items in various queue states."""
    counts = {"articles": 0, "foia": 0, "portal": 0}
    try:
        archive = get_archive_articles(service, SHEET_ID)
        existing = get_existing_foia_urls(service, SHEET_ID)

        for a in archive:
            score = a.get("FOIA Score", "").strip()
            if score.isdigit():
                url = a.get("URL", "").strip()
                if url and _normalize_url(url) not in existing:
                    counts["foia"] += 1

        requests = get_foia_requests(service, SHEET_ID)
        counts["portal"] = sum(1 for r in requests if r.get("Status") == "Portal Needed")
        counts["articles"] = len(archive)
    except Exception:
        pass
    return counts


def run():
    """Main monitoring loop."""
    validate_config()
    service = get_service()

    # Ensure tabs exist
    try:
        ensure_foia_headers(service, SHEET_ID)
        ensure_archive_headers(service, SHEET_ID)
    except Exception as e:
        logger.error(f"Failed to initialize sheet tabs: {e}")
        sys.exit(1)

    logger.info("Kevin is online. Monitoring started.")
    logger.info(f"Sheet: {SHEET_ID}")
    logger.info(f"Poll interval: {POLL_INTERVAL}s, Full scan: {FULL_SCAN_INTERVAL}s")

    log_activity(service, SHEET_ID, "Monitor Started", "Kevin is online and monitoring", "Monitor")

    last_full_scan = 0
    errors = []

    while True:
        try:
            now = time.time()
            counts = get_queue_counts(service)

            write_monitor_heartbeat(
                service, SHEET_ID, "Running",
                last_action="Checking...",
                articles_queued=counts["articles"],
                foia_queued=counts["foia"],
                portal_queued=counts["portal"],
                errors="; ".join(errors[-3:]) if errors else "",
            )

            # Full scan (heavy operations)
            if now - last_full_scan >= FULL_SCAN_INTERVAL:
                logger.info("Running full pipeline scan...")

                # 1. Check for high-scoring articles needing FOIA
                foia_count = check_high_scoring(service)
                if foia_count:
                    logger.info(f"Processed {foia_count} new FOIA requests")

                # 2. Check for follow-ups
                fu_count = check_follow_ups(service)
                if fu_count:
                    logger.info(f"Sent {fu_count} follow-ups")

                # 3. Signal Kevin if portal submissions are pending
                #    Kevin's Ollama heartbeat checks H3 — "GO" triggers the skill
                updated_counts = get_queue_counts(service)
                if updated_counts["portal"] > 0:
                    current_trigger = read_kevin_trigger(service, SHEET_ID)
                    if current_trigger != "GO":
                        write_kevin_trigger(service, SHEET_ID, "GO")
                        logger.info(f"Signaled Kevin: {updated_counts['portal']} portal submissions pending")
                        log_activity(service, SHEET_ID, "Kevin Triggered",
                                     f"{updated_counts['portal']} portal submissions queued", "Monitor")

                last_full_scan = now

                _action = []
                if foia_count:
                    _action.append(f"{foia_count} FOIA")
                if fu_count:
                    _action.append(f"{fu_count} follow-ups")

                write_monitor_heartbeat(
                    service, SHEET_ID, "Idle",
                    last_action=", ".join(_action) if _action else "Scan complete, nothing to do",
                    articles_queued=counts["articles"],
                    foia_queued=counts["foia"],
                    portal_queued=counts["portal"],
                )
            else:
                write_monitor_heartbeat(
                    service, SHEET_ID, "Idle",
                    articles_queued=counts["articles"],
                    foia_queued=counts["foia"],
                    portal_queued=counts["portal"],
                )

            errors = []  # Clear errors on success

        except KeyboardInterrupt:
            logger.info("Kevin shutting down.")
            try:
                write_monitor_heartbeat(service, SHEET_ID, "Offline", last_action="Shutdown")
                log_activity(service, SHEET_ID, "Monitor Stopped", "Kevin went offline", "Monitor")
            except Exception:
                pass
            break
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:100]}"
            logger.error(f"Monitor loop error: {error_msg}")
            errors.append(error_msg)
            try:
                write_monitor_heartbeat(service, SHEET_ID, "Error", errors=error_msg)
            except Exception:
                pass
            # Reconnect on error
            try:
                service = get_service()
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
