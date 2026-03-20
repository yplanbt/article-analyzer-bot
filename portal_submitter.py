"""Automated FOIA portal submission using Playwright browser automation.

Supports: GovQA, NextRequest, JustFOIA, and generic fallback.
Requires: playwright package + browsers installed locally.
Run `playwright install chromium` once to set up.
"""

import re
import time
from datetime import datetime


def detect_portal_type(url: str) -> str:
    """Detect the portal type from the URL."""
    url_lower = url.lower()
    if "govqa" in url_lower:
        return "govqa"
    if "nextrequest" in url_lower:
        return "nextrequest"
    if "justfoia" in url_lower:
        return "justfoia"
    if "jotform" in url_lower:
        return "jotform"
    return "unknown"


def submit_to_portal(
    portal_url: str,
    request_body: str,
    subject: str,
    requester_name: str,
    requester_email: str,
    police_dept: str = "",
    portal_credentials: dict = None,
    headless: bool = True,
    anthropic_key: str = "",
    proxy: str = "",
) -> dict:
    """Submit a FOIA request through a portal. Returns status dict.

    Hybrid approach:
    1. Known portals (GovQA, NextRequest, JustFOIA) → Playwright hardcoded flows
    2. Unknown portals → AI browser agent (Claude vision)
    3. If Playwright fails on known portal → fallback to AI agent

    Args:
        portal_url: The portal URL
        request_body: The FOIA letter text
        subject: Email subject / request title
        requester_name: Name of the requester
        requester_email: Email for correspondence
        police_dept: Department name (for portal selection)
        portal_credentials: {"email": ..., "password": ...} for portal login
        headless: Run browser without visible window
        anthropic_key: Anthropic API key for AI agent fallback
        proxy: US proxy URL. Falls back to US_PROXY env var.
    """
    import os
    proxy_url = proxy or os.environ.get("US_PROXY", "")

    # Configurable headed mode
    headless_env = os.environ.get("BROWSER_HEADLESS", "").lower()
    if headless_env in ("false", "0", "no"):
        headless = False

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": "Playwright not installed. Run: pip install playwright && playwright install chromium",
        }

    portal_type = detect_portal_type(portal_url)

    # For unknown portals, go straight to AI agent
    if portal_type == "unknown" and anthropic_key:
        return _try_ai_agent(
            portal_url, request_body, subject, requester_name,
            requester_email, police_dept, portal_credentials,
            anthropic_key, headless,
        )

    # For known portals, try Playwright first
    try:
        with sync_playwright() as p:
            launch_args = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = {"server": proxy_url}
            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Inject stealth script to avoid bot detection
            try:
                from ai_browser_agent import _get_stealth_script
                context.add_init_script(_get_stealth_script())
            except ImportError:
                pass

            page = context.new_page()
            page.set_default_timeout(30000)

            if portal_type == "govqa":
                result = _submit_govqa(page, portal_url, request_body, subject,
                                       requester_name, requester_email, police_dept,
                                       portal_credentials)
            elif portal_type == "nextrequest":
                result = _submit_nextrequest(page, portal_url, request_body, subject,
                                              requester_name, requester_email,
                                              portal_credentials)
            elif portal_type == "justfoia":
                result = _submit_justfoia(page, portal_url, request_body, subject,
                                           requester_name, requester_email,
                                           portal_credentials)
            else:
                result = _submit_generic(page, portal_url, request_body, subject,
                                          requester_name, requester_email)

            browser.close()

            # If Playwright failed on a known portal, try AI agent as fallback
            if not result.get("success") and anthropic_key:
                ai_result = _try_ai_agent(
                    portal_url, request_body, subject, requester_name,
                    requester_email, police_dept, portal_credentials,
                    anthropic_key, headless,
                )
                if ai_result.get("success"):
                    return ai_result
                # Return original Playwright error if AI also failed
                result["ai_fallback_error"] = ai_result.get("error", "AI agent also failed")

            return result

    except Exception as e:
        # Playwright crashed — try AI agent
        if anthropic_key:
            return _try_ai_agent(
                portal_url, request_body, subject, requester_name,
                requester_email, police_dept, portal_credentials,
                anthropic_key, headless,
            )
        return {"success": False, "error": f"Browser automation failed: {str(e)}"}


def _try_ai_agent(portal_url, request_body, subject, requester_name,
                   requester_email, police_dept, portal_credentials,
                   anthropic_key, headless):
    """Attempt submission using the AI browser agent."""
    try:
        from ai_browser_agent import ai_submit_portal
        return ai_submit_portal(
            portal_url=portal_url,
            request_body=request_body,
            subject=subject,
            requester_name=requester_name,
            requester_email=requester_email,
            requester_password=portal_credentials.get("password", "") if portal_credentials else "",
            police_dept=police_dept,
            anthropic_key=anthropic_key,
            headless=headless,
        )
    except ImportError:
        return {"success": False, "error": "AI browser agent not available"}
    except Exception as e:
        return {"success": False, "error": f"AI agent error: {str(e)[:200]}"}


def _submit_govqa(page, url, body, subject, name, email, dept, creds):
    """Submit through GovQA portal."""
    page.goto(url, wait_until="networkidle")
    time.sleep(2)

    # GovQA typically has a "Submit a Request" or "New Request" button
    try:
        # Try to find and click the new request button
        for selector in [
            "text=Submit a Request", "text=New Request", "text=Submit Request",
            "text=Make a Request", "a:has-text('Request')", "button:has-text('Request')",
        ]:
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).first.click()
                    time.sleep(2)
                    break
            except Exception:
                continue

        # Check if we need to log in first
        if page.locator("input[type='email'], input[name='email'], #email").count() > 0:
            if creds and creds.get("email"):
                _try_login(page, creds["email"], creds.get("password", ""))
                time.sleep(2)
            else:
                # Try guest/anonymous submission
                for selector in ["text=Continue as Guest", "text=Guest", "text=Anonymous"]:
                    try:
                        if page.locator(selector).count() > 0:
                            page.locator(selector).first.click()
                            time.sleep(1)
                            break
                    except Exception:
                        continue

        # Fill in the request form
        _fill_form_fields(page, body, subject, name, email)

        # Try to submit
        submitted = _click_submit(page)

        if submitted:
            # Try to capture confirmation number
            time.sleep(3)
            page_text = page.content()
            conf_match = re.search(r'(?:request|confirmation|tracking|reference)\s*(?:#|number|id)?\s*[:\s]*([A-Z0-9-]+)', page_text, re.IGNORECASE)
            conf_num = conf_match.group(1) if conf_match else ""
            return {
                "success": True,
                "message": f"Submitted via GovQA",
                "confirmation": conf_num,
                "portal_type": "govqa",
            }
        else:
            # Take screenshot for debugging
            return {
                "success": False,
                "error": "Could not find submit button on GovQA form",
                "portal_type": "govqa",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        return {"success": False, "error": f"GovQA error: {str(e)}", "portal_type": "govqa"}


def _submit_nextrequest(page, url, body, subject, name, email, creds):
    """Submit through NextRequest portal."""
    page.goto(url, wait_until="networkidle")
    time.sleep(2)

    try:
        # NextRequest usually has a prominent "Make a Request" button
        for selector in [
            "text=Make a Request", "text=New Request", "text=Submit a Request",
            "a:has-text('Make a Request')", "button:has-text('Request')",
            ".new-request-btn", "#new-request",
        ]:
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).first.click()
                    time.sleep(2)
                    break
            except Exception:
                continue

        # Handle login/signup if needed
        if creds and creds.get("email"):
            _try_login(page, creds["email"], creds.get("password", ""))
            time.sleep(2)
        else:
            # NextRequest often allows requests without login
            for selector in ["text=Continue without", "text=Skip", "text=Guest"]:
                try:
                    if page.locator(selector).count() > 0:
                        page.locator(selector).first.click()
                        time.sleep(1)
                        break
                except Exception:
                    continue

        # Fill in request details
        _fill_form_fields(page, body, subject, name, email)

        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            page_text = page.content()
            conf_match = re.search(r'(?:request|confirmation|tracking)\s*(?:#|number|id)?\s*[:\s]*([A-Z0-9-]+)', page_text, re.IGNORECASE)
            conf_num = conf_match.group(1) if conf_match else ""
            return {
                "success": True,
                "message": "Submitted via NextRequest",
                "confirmation": conf_num,
                "portal_type": "nextrequest",
            }
        else:
            return {
                "success": False,
                "error": "Could not complete NextRequest submission",
                "portal_type": "nextrequest",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        return {"success": False, "error": f"NextRequest error: {str(e)}", "portal_type": "nextrequest"}


def _submit_justfoia(page, url, body, subject, name, email, creds):
    """Submit through JustFOIA portal."""
    page.goto(url, wait_until="networkidle")
    time.sleep(2)

    try:
        # JustFOIA has a simpler form usually
        for selector in [
            "text=Submit Request", "text=New Request", "text=Make a Request",
            "a:has-text('Request')", "button:has-text('Request')",
        ]:
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).first.click()
                    time.sleep(2)
                    break
            except Exception:
                continue

        if creds and creds.get("email"):
            _try_login(page, creds["email"], creds.get("password", ""))
            time.sleep(2)

        _fill_form_fields(page, body, subject, name, email)
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            return {
                "success": True,
                "message": "Submitted via JustFOIA",
                "portal_type": "justfoia",
            }
        else:
            return {
                "success": False,
                "error": "Could not complete JustFOIA submission",
                "portal_type": "justfoia",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        return {"success": False, "error": f"JustFOIA error: {str(e)}", "portal_type": "justfoia"}


def _submit_generic(page, url, body, subject, name, email):
    """Generic portal handler — tries to find and fill common form patterns."""
    page.goto(url, wait_until="networkidle")
    time.sleep(2)

    try:
        _fill_form_fields(page, body, subject, name, email)
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            return {
                "success": True,
                "message": "Submitted via portal form",
                "portal_type": "generic",
            }
        else:
            return {
                "success": False,
                "error": "Unknown portal type — could not auto-submit",
                "portal_type": "unknown",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        return {"success": False, "error": f"Generic portal error: {str(e)}", "portal_type": "unknown"}


# ═══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════════

def _try_login(page, email, password):
    """Try to log into a portal."""
    # Find email field
    for selector in ["input[type='email']", "input[name='email']", "#email", "input[placeholder*='email' i]"]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(email)
                break
        except Exception:
            continue

    # Find password field
    if password:
        for selector in ["input[type='password']", "input[name='password']", "#password"]:
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).first.fill(password)
                    break
            except Exception:
                continue

    # Click login/sign in button
    for selector in [
        "button[type='submit']", "text=Sign In", "text=Log In", "text=Login",
        "button:has-text('Sign')", "button:has-text('Log')",
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click()
                time.sleep(2)
                break
        except Exception:
            continue


def _fill_form_fields(page, body, subject, name, email):
    """Try to fill common form fields on any portal."""
    # Subject / Title field
    for selector in [
        "input[name*='subject' i]", "input[name*='title' i]",
        "input[placeholder*='subject' i]", "input[placeholder*='title' i]",
        "input[aria-label*='subject' i]", "input[aria-label*='title' i]",
        "#subject", "#title", "#request_title",
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(subject)
                break
        except Exception:
            continue

    # Main request body / description
    for selector in [
        "textarea[name*='description' i]", "textarea[name*='body' i]",
        "textarea[name*='request' i]", "textarea[name*='message' i]",
        "textarea[name*='details' i]", "textarea[name*='content' i]",
        "textarea[placeholder*='describe' i]", "textarea[placeholder*='request' i]",
        "textarea[aria-label*='description' i]", "textarea[aria-label*='request' i]",
        "#description", "#request_body", "#message", "#details",
        "textarea",  # Last resort: first textarea on page
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(body)
                break
        except Exception:
            continue

    # Name field
    for selector in [
        "input[name*='name' i]:not([name*='email']):not([name*='user'])",
        "input[placeholder*='name' i]:not([placeholder*='email'])",
        "input[aria-label*='name' i]:not([aria-label*='email'])",
        "#name", "#requester_name", "#full_name",
    ]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(name)
                break
        except Exception:
            continue

    # Email field
    for selector in [
        "input[type='email']", "input[name*='email' i]",
        "input[placeholder*='email' i]", "#email", "#requester_email",
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(email)
                break
        except Exception:
            continue


def _click_submit(page) -> bool:
    """Try to find and click a submit button. Returns True if clicked."""
    for selector in [
        "button[type='submit']",
        "button:has-text('Submit')", "button:has-text('Send')",
        "button:has-text('Create')", "button:has-text('File Request')",
        "input[type='submit']",
        "a:has-text('Submit')", "a:has-text('Send Request')",
        ".submit-btn", "#submit", "#submit-request",
    ]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.click()
                time.sleep(2)
                return True
        except Exception:
            continue
    return False
