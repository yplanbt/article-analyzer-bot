"""Automated FOIA portal submission using Playwright browser automation.

Supports: GovQA, NextRequest, JustFOIA, FormCenter (CivicPlus), JotForm,
and a smart generic fallback. AI vision is optional last resort.
Requires: playwright package + browsers installed locally.
Run `playwright install chromium` once to set up.
"""

import os
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
    if "formcenter" in url_lower or "civicplus" in url_lower:
        return "formcenter"
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
    use_ai_fallback: bool = False,
) -> dict:
    """Submit a FOIA request through a portal. Returns status dict.

    Layered approach (cheapest first):
    1. Known portals (GovQA, NextRequest, JustFOIA, FormCenter) -> hardcoded Playwright flows ($0)
    2. Unknown portals -> smart generic form filler ($0)
    3. If use_ai_fallback=True and everything fails -> AI browser agent (costs ~$0.10)
    """
    import os
    from urllib.parse import urlparse as _urlparse
    proxy_url = proxy or os.environ.get("US_PROXY", "")

    # Skip proxy for .gov/.us/.mil domains — they don't need it and proxy causes failures
    _domain = (_urlparse(portal_url).hostname or "").lower()
    if proxy_url and any(_domain.endswith(s) for s in (".gov", ".us", ".mil")):
        print(f"  Skipping proxy for government domain: {_domain}", flush=True)
        proxy_url = ""

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

    try:
        with sync_playwright() as p:
            launch_args = {"headless": headless}
            if proxy_url:
                # Parse proxy URL to extract username/password if embedded
                # Format: http://username:password@host:port
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
                if parsed.username:
                    proxy_config["username"] = parsed.username
                if parsed.password:
                    proxy_config["password"] = parsed.password
                launch_args["proxy"] = proxy_config
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
            elif portal_type == "formcenter":
                result = _submit_formcenter(page, portal_url, request_body, subject,
                                             requester_name, requester_email,
                                             portal_credentials)
            elif portal_type == "jotform":
                result = _submit_generic(page, portal_url, request_body, subject,
                                          requester_name, requester_email)
            else:
                # Unknown portal — try smart generic filler
                result = _submit_generic(page, portal_url, request_body, subject,
                                          requester_name, requester_email)

            # Tier 2: If generic filler failed, try DOM-based AI filler (cheap)
            if not result.get("success") and result.get("portal_type") == "unknown":
                gemini_key = os.environ.get("GEMINI_API_KEY", "")
                if gemini_key:
                    print(f"  Trying DOM-based AI filler (Gemini Flash)...")
                    try:
                        ai_dom_result = _ai_fill_form_dom(
                            page, request_body, subject, requester_name,
                            requester_email, police_dept, gemini_key
                        )
                        if ai_dom_result.get("success"):
                            browser.close()
                            return ai_dom_result
                        print(f"  DOM AI filler: {ai_dom_result.get('error', 'failed')}")
                    except Exception as e:
                        print(f"  DOM AI filler error: {e}")

            browser.close()

            # Tier 3: If still failed and AI vision fallback is enabled (expensive)
            if not result.get("success") and use_ai_fallback and anthropic_key:
                ai_result = _try_ai_agent(
                    portal_url, request_body, subject, requester_name,
                    requester_email, police_dept, portal_credentials,
                    anthropic_key, headless,
                )
                if ai_result.get("success"):
                    return ai_result
                result["ai_fallback_error"] = ai_result.get("error", "AI agent also failed")

            return result

    except Exception as e:
        if use_ai_fallback and anthropic_key:
            return _try_ai_agent(
                portal_url, request_body, subject, requester_name,
                requester_email, police_dept, portal_credentials,
                anthropic_key, headless,
            )
        return {"success": False, "error": f"Browser automation failed: {str(e)}"}


def _try_ai_agent(portal_url, request_body, subject, requester_name,
                   requester_email, police_dept, portal_credentials,
                   anthropic_key, headless):
    """Attempt submission using the AI browser agent (expensive fallback)."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# Known portal handlers
# ═══════════════════════════════════════════════════════════════════════════════

def _submit_govqa(page, url, body, subject, name, email, dept, creds):
    """Submit through GovQA portal."""
    page.goto(url, wait_until="networkidle")
    time.sleep(2)

    try:
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

        if page.locator("input[type='email'], input[name='email'], #email").count() > 0:
            if creds and creds.get("email"):
                _try_login(page, creds["email"], creds.get("password", ""))
                time.sleep(2)
            else:
                for selector in ["text=Continue as Guest", "text=Guest", "text=Anonymous"]:
                    try:
                        if page.locator(selector).count() > 0:
                            page.locator(selector).first.click()
                            time.sleep(1)
                            break
                    except Exception:
                        continue

        _fill_form_fields(page, body, subject, name, email)
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            conf_num = _extract_confirmation(page)
            return {
                "success": True,
                "message": "Submitted via GovQA",
                "confirmation": conf_num,
                "portal_type": "govqa",
            }
        else:
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

        if creds and creds.get("email"):
            _try_login(page, creds["email"], creds.get("password", ""))
            time.sleep(2)
        else:
            for selector in ["text=Continue without", "text=Skip", "text=Guest"]:
                try:
                    if page.locator(selector).count() > 0:
                        page.locator(selector).first.click()
                        time.sleep(1)
                        break
                except Exception:
                    continue

        _fill_form_fields(page, body, subject, name, email)
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            conf_num = _extract_confirmation(page)
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


def _justfoia_base_url(url):
    """Extract JustFOIA base URL (e.g., https://rochesterny.justfoia.com)."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}"


def _justfoia_login(page, base_url, username, password):
    """Log into a JustFOIA portal. Returns True if login succeeded."""
    login_url = base_url + "/publicportal/home/login"
    page.goto(login_url, wait_until="networkidle")
    time.sleep(2)

    # Fill email/username
    for selector in ["input[type='email']", "input[name*='email' i]", "input[id*='email' i]",
                      "input[name*='user' i]", "#Email", "#username"]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(username)
                break
        except Exception:
            continue

    # Fill password
    for selector in ["input[type='password']", "input[name*='pass' i]", "#Password"]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(password)
                break
        except Exception:
            continue

    # Click login
    for selector in ["button[type='submit']", "button:has-text('Log')", "button:has-text('Sign')",
                      "input[type='submit']", "a:has-text('Log In')"]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.click()
                time.sleep(3)
                break
        except Exception:
            continue

    # Check if login succeeded (should redirect away from login page)
    return "login" not in page.url.lower() and "restricted" not in page.title().lower()


def _register_justfoia(page, base_url, name, email):
    """Auto-register on a JustFOIA portal.

    Returns {"success": True, "password": "..."} or {"success": False, "error": "..."}.
    """
    import hashlib
    # Generate deterministic password from portal domain
    domain = base_url.replace("https://", "").replace("http://", "")
    password = "Foia" + hashlib.md5(domain.encode()).hexdigest()[:8] + "!1"

    # Try multiple known JustFOIA registration URL patterns
    register_urls = [
        base_url + "/account/register",
        base_url + "/publicportal/home/register",
        base_url + "/publicportal/account/register",
    ]

    page_loaded = False
    for register_url in register_urls:
        page.goto(register_url, wait_until="networkidle")
        time.sleep(3)
        title = page.title().lower()
        if "restricted" not in title and "error" not in title:
            page_loaded = True
            break

    try:
        if not page_loaded:
            _save_debug_screenshot(page, "justfoia_register")
            return {"success": False, "error": "Registration page not accessible at any known URL"}

        name_parts = name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else first_name

        # Fill First Name
        for selector in ["input[id*='first' i]", "input[name*='first' i]",
                          "input[placeholder*='first' i]"]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.fill(first_name)
                    break
            except Exception:
                continue

        # Fill Last Name
        for selector in ["input[id*='last' i]", "input[name*='last' i]",
                          "input[placeholder*='last' i]"]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.fill(last_name)
                    break
            except Exception:
                continue

        # Fill Email
        for selector in ["input[type='email']", "input[id*='email' i]",
                          "input[name*='email' i]"]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.fill(email)
                    break
            except Exception:
                continue

        # Fill Password + Confirm Password
        password_fields = page.locator("input[type='password']")
        pw_count = password_fields.count()
        if pw_count >= 1:
            password_fields.nth(0).fill(password)
        if pw_count >= 2:
            password_fields.nth(1).fill(password)

        # Fill phone if present
        _fill_phone_field(page, "000-000-0000")

        # Check any required boxes (terms, etc.)
        _check_required_boxes(page)

        # Click register/submit
        for selector in ["button[type='submit']", "button:has-text('Register')",
                          "button:has-text('Sign Up')", "button:has-text('Create')",
                          "input[type='submit']", "button:has-text('Submit')"]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.click()
                    time.sleep(3)
                    break
            except Exception:
                continue

        # Check result
        page_text = page.content().lower()
        if any(kw in page_text for kw in ["verification", "confirm your email",
                                            "check your email", "sent you an email",
                                            "registered", "account created"]):
            print("  Registration submitted — verification email expected")
            return {"success": True, "password": password, "needs_verification": True}
        elif any(kw in page_text for kw in ["already registered", "already exists",
                                              "account exists"]):
            print("  Account already exists — trying login instead")
            return {"success": True, "password": password, "already_exists": True}
        else:
            _save_debug_screenshot(page, "justfoia_register")
            return {"success": False, "error": "Registration outcome unclear",
                    "password": password}

    except Exception as e:
        _save_debug_screenshot(page, "justfoia_register")
        return {"success": False, "error": f"Registration error: {str(e)}"}


def _check_verification_email(email_addr, email_password, sender_pattern="justfoia", timeout=90):
    """Check Gmail IMAP for a JustFOIA verification email and extract the link.

    Returns the verification URL or None.
    """
    import imaplib
    import email as email_lib
    from email.header import decode_header

    print(f"  Checking email for verification link (timeout {timeout}s)...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(email_addr, email_password)
            mail.select("INBOX")

            # Search for recent emails from JustFOIA
            _, messages = mail.search(None, f'(UNSEEN FROM "{sender_pattern}")')
            msg_nums = messages[0].split()

            for num in reversed(msg_nums):  # Most recent first
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email_lib.message_from_bytes(msg_data[0][1])

                # Check subject for verification keywords
                subject = str(decode_header(msg["Subject"])[0][0] or "")
                if isinstance(subject, bytes):
                    subject = subject.decode("utf-8", errors="ignore")

                if not any(kw in subject.lower() for kw in
                           ["verif", "confirm", "activate", "welcome", "registration"]):
                    continue

                # Extract verification link from email body
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        if ctype in ("text/plain", "text/html"):
                            payload = part.get_payload(decode=True)
                            if payload:
                                body_text += payload.decode("utf-8", errors="ignore")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body_text = payload.decode("utf-8", errors="ignore")

                # Find verification link
                import re as _re
                links = _re.findall(r'https?://[^\s<>"\']+(?:verif|confirm|activate|token)[^\s<>"\']*',
                                     body_text, _re.IGNORECASE)
                if links:
                    mail.logout()
                    print(f"  Found verification link")
                    return links[0]

                # Fallback: any link from justfoia domain
                links = _re.findall(r'https?://[^\s<>"\']*justfoia[^\s<>"\']*', body_text, _re.IGNORECASE)
                if links:
                    # Filter out obvious non-verification links
                    for link in links:
                        if any(kw in link.lower() for kw in ["verif", "confirm", "activate", "token", "click"]):
                            mail.logout()
                            print(f"  Found verification link")
                            return link

            mail.logout()
        except Exception as e:
            print(f"  IMAP check error: {e}")

        # Wait before checking again
        if time.time() < deadline:
            time.sleep(10)

    print("  No verification email found within timeout")
    return None


def _submit_justfoia(page, url, body, subject, name, email, creds):
    """Submit through JustFOIA portal with auto-registration.

    Flow:
    0. Try anonymous submission first (many JustFOIA portals allow it)
    1. If credentials exist → login → submit
    2. If no credentials → register → verify email → login → submit
    3. If registration fails → return needs_registration for manual handling
    """
    base_url = _justfoia_base_url(url)

    # Build new request URL
    if "/newrequest" not in url.lower():
        if "/publicportal" in url:
            new_request_url = url.rsplit("/publicportal", 1)[0] + "/publicportal/home/newrequest"
        else:
            new_request_url = base_url + "/publicportal/home/newrequest"
    else:
        new_request_url = url

    try:
        # Step 0: Try anonymous submission first
        print(f"  Trying anonymous submission at {new_request_url}...")
        page.goto(new_request_url, wait_until="networkidle")
        time.sleep(3)

        title = page.title().lower()
        if "restricted" not in title:
            # Page loaded — check if there's a form we can fill
            form_elements = page.locator("input, textarea, select").count()
            if form_elements > 2:
                print(f"  Anonymous access works — found {form_elements} form elements")
                _fill_justfoia_form(page, body, subject, name, email)
                submitted = _click_submit(page)
                if submitted:
                    time.sleep(3)
                    conf = _extract_confirmation(page)
                    return {
                        "success": True,
                        "message": "Submitted via JustFOIA (anonymous)",
                        "confirmation": conf,
                        "portal_type": "justfoia",
                    }
                else:
                    print(f"  Anonymous form fill succeeded but submit button not found, trying login flow...")
            else:
                print(f"  Anonymous page has {form_elements} form elements — not enough, trying login flow...")
        else:
            print(f"  Anonymous access restricted — trying login flow...")

        logged_in = False
        new_credentials = None

        # Step 1: Try login with existing credentials
        if creds and creds.get("password"):
            cred_user = creds.get("email") or creds.get("username", email)
            cred_pass = creds["password"]
            print(f"  Logging in with saved credentials...")
            logged_in = _justfoia_login(page, base_url, cred_user, cred_pass)
            if logged_in:
                print(f"  Login successful")

        # Step 2: No credentials or login failed → auto-register
        if not logged_in:
            print(f"  No valid credentials — attempting auto-registration...")
            reg_result = _register_justfoia(page, base_url, name, email)

            if reg_result.get("success"):
                reg_password = reg_result["password"]

                # Check for email verification if needed
                if reg_result.get("needs_verification"):
                    email_password = os.environ.get("FOIA_EMAIL_PASSWORD", "")
                    if email_password:
                        verify_link = _check_verification_email(email, email_password)
                        if verify_link:
                            print(f"  Clicking verification link...")
                            page.goto(verify_link, wait_until="networkidle")
                            time.sleep(3)
                        else:
                            return {
                                "success": False,
                                "error": "Registration submitted but verification email not received",
                                "portal_type": "justfoia",
                                "needs_registration": True,
                            }
                    else:
                        return {
                            "success": False,
                            "error": "Registration needs email verification but FOIA_EMAIL_PASSWORD not set",
                            "portal_type": "justfoia",
                            "needs_registration": True,
                        }

                # Try logging in with new credentials
                print(f"  Logging in with new credentials...")
                logged_in = _justfoia_login(page, base_url, email, reg_password)
                if logged_in:
                    print(f"  Login successful after registration")
                    new_credentials = {"username": email, "password": reg_password}
            else:
                _save_debug_screenshot(page, "justfoia_reg_fail")
                return {
                    "success": False,
                    "error": f"Registration failed: {reg_result.get('error', 'unknown')}",
                    "portal_type": "justfoia",
                    "needs_registration": True,
                }

        if not logged_in:
            _save_debug_screenshot(page, "justfoia_login_fail")
            return {
                "success": False,
                "error": "Could not log into JustFOIA portal",
                "portal_type": "justfoia",
                "needs_registration": True,
            }

        # Step 3: Navigate to new request form and fill it
        page.goto(new_request_url, wait_until="networkidle")
        time.sleep(3)

        # Check if we actually reached the form (not redirected to access restricted)
        title = page.title().lower()
        if "restricted" in title:
            _save_debug_screenshot(page, "justfoia_restricted")
            return {
                "success": False,
                "error": "Still access restricted after login",
                "portal_type": "justfoia",
                "needs_registration": True,
            }

        # Fill form fields
        _fill_justfoia_form(page, body, subject, name, email)

        # Submit
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            conf = _extract_confirmation(page)
            result = {
                "success": True,
                "message": "Submitted via JustFOIA",
                "confirmation": conf,
                "portal_type": "justfoia",
            }
            if new_credentials:
                result["new_credentials"] = new_credentials
            return result
        else:
            _save_debug_screenshot(page, "justfoia_submit")
            return {
                "success": False,
                "error": "Could not find submit button on JustFOIA form",
                "portal_type": "justfoia",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        _save_debug_screenshot(page, "justfoia_error")
        return {"success": False, "error": f"JustFOIA error: {str(e)}", "portal_type": "justfoia"}


def _fill_justfoia_form(page, body, subject, name, email):
    """Fill the JustFOIA new request form fields."""
    # Request Description — main textarea
    for selector in [
        "textarea[id*='description' i]", "textarea[id*='request' i]",
        "textarea[name*='description' i]", "textarea[name*='request' i]",
        "textarea[placeholder*='description' i]", "textarea[placeholder*='request' i]",
        "textarea",
    ]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(body)
                break
        except Exception:
            continue

    # Label-based filling
    _fill_by_labels(page, body, subject, name, email)

    # First Name / Last Name (JustFOIA splits name)
    name_parts = name.strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    for selector in ["input[id*='first' i]", "input[name*='first' i]", "input[placeholder*='first' i]"]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(first_name)
                break
        except Exception:
            continue

    for selector in ["input[id*='last' i]", "input[name*='last' i]", "input[placeholder*='last' i]"]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(last_name)
                break
        except Exception:
            continue

    # Email (may be pre-filled if logged in, but fill anyway)
    for selector in ["input[type='email']", "input[id*='email' i]",
                      "input[name*='email' i]", "input[placeholder*='email' i]"]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(email)
                break
        except Exception:
            continue

    # Phone (optional but often required)
    _fill_phone_field(page, "000-000-0000")

    # Check required boxes
    _check_required_boxes(page)


def _submit_formcenter(page, url, body, subject, name, email, creds):
    """Submit through CivicPlus FormCenter portal.

    FormCenter uses Field_[ID] naming for inputs. We match fields by
    scanning <label> text and filling the associated input.
    """
    page.goto(url, wait_until="networkidle")
    time.sleep(3)  # FormCenter loads forms via JS, needs extra wait

    try:
        # FormCenter renders forms dynamically — wait for form elements (increased from 10s)
        page.wait_for_selector("form, .formContent, #FormCenter", timeout=15000)
        time.sleep(1)
    except Exception:
        # FormCenter layout not detected — fall back to generic handler
        print(f"  FormCenter layout not detected, falling back to generic handler", flush=True)
        return _submit_generic(page, url, body, subject, name, email)

    try:
        # Step 1: Try label-based field matching (FormCenter's primary pattern)
        filled_any = _fill_by_labels(page, body, subject, name, email)

        # Step 2: Fallback to standard selectors if label matching didn't work
        if not filled_any:
            _fill_form_fields(page, body, subject, name, email)

        # Step 3: Fill phone field if visible (many FormCenter forms require it)
        _fill_phone_field(page, "000-000-0000")

        # Step 4: Handle required checkboxes (acknowledgment, terms, etc.)
        _check_required_boxes(page)

        # Step 5: Submit
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            conf_num = _extract_confirmation(page)
            return {
                "success": True,
                "message": "Submitted via FormCenter",
                "confirmation": conf_num,
                "portal_type": "formcenter",
            }
        else:
            return {
                "success": False,
                "error": "Could not find submit button on FormCenter form",
                "portal_type": "formcenter",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        return {"success": False, "error": f"FormCenter error: {str(e)}", "portal_type": "formcenter"}


def _submit_generic(page, url, body, subject, name, email):
    """Smart generic portal handler — tries multiple strategies to fill and submit."""
    print(f"  Generic handler: loading {url}", flush=True)
    page.goto(url, wait_until="networkidle")
    time.sleep(3)  # Extra wait for JS-rendered forms

    try:
        # Count visible form elements for diagnostics
        form_count = page.locator("input:visible, textarea:visible, select:visible").count()
        print(f"  Found {form_count} visible form elements on page", flush=True)

        if form_count == 0:
            _save_debug_screenshot(page, "generic_noform")
            return {
                "success": False,
                "error": "No visible form elements found on page",
                "portal_type": "unknown",
                "needs_manual": True,
                "page_url": page.url,
            }

        # Check for iframes that might contain the form
        iframe_page = _find_form_iframe(page)
        target = iframe_page if iframe_page else page
        if iframe_page:
            print(f"  Found form inside iframe", flush=True)

        # Strategy 1: Label-based matching (works on most CMS forms)
        label_count = _fill_by_labels(target, body, subject, name, email)
        print(f"  Strategy 1 (labels): filled {label_count} fields", flush=True)

        # Strategy 2: Standard CSS selectors (always run as supplement)
        css_count = _fill_form_fields(target, body, subject, name, email)
        print(f"  Strategy 2 (CSS selectors): filled {css_count} fields", flush=True)

        total_filled = label_count + css_count

        # Fill phone if visible
        _fill_phone_field(target, "000-000-0000")

        # Check required boxes
        _check_required_boxes(target)

        if total_filled == 0:
            print(f"  WARNING: 0 fields filled out of {form_count} visible elements", flush=True)
            _save_debug_screenshot(page, "generic_nofill")
            return {
                "success": False,
                "error": f"No fields could be filled ({form_count} elements visible but none matched)",
                "portal_type": "unknown",
                "needs_manual": True,
                "page_url": page.url,
            }

        submitted = _click_submit(target)

        if submitted:
            time.sleep(3)
            conf_num = _extract_confirmation(page)  # Check main page for confirmation
            return {
                "success": True,
                "message": f"Submitted via portal form ({total_filled} fields filled)",
                "confirmation": conf_num,
                "portal_type": "generic",
            }
        else:
            _save_debug_screenshot(page, "generic_nosubmit")
            return {
                "success": False,
                "error": f"Filled {total_filled} fields but could not find submit button",
                "portal_type": "unknown",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        _save_debug_screenshot(page, "generic_error")
        return {"success": False, "error": f"Generic portal error: {str(e)}", "portal_type": "unknown"}


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 2: DOM-based AI form filler (cheap, uses GPT-4o-mini text-only)
# ═══════════════════════════════════════════════════════════════════════════════

def _ai_fill_form_dom(page, body, subject, name, email, police_dept, gemini_key):
    """Use Gemini Flash with DOM extraction to fill unknown portal forms.

    Instead of expensive screenshots, extracts form structure as text and asks
    a cheap LLM to map fields to values. Essentially free with Gemini Flash.
    """
    import json as _json
    import urllib.request
    import urllib.error

    # Step 1: Extract all visible form elements from the page
    form_elements = page.evaluate("""() => {
        const els = document.querySelectorAll('input, textarea, select, button[type="submit"], input[type="submit"]');
        return Array.from(els).map(el => {
            const rect = el.getBoundingClientRect();
            const labelEl = el.labels && el.labels[0] ? el.labels[0] : null;
            let labelText = '';
            if (labelEl) {
                labelText = labelEl.textContent.trim();
            } else if (el.id) {
                const forLabel = document.querySelector('label[for="' + el.id + '"]');
                if (forLabel) labelText = forLabel.textContent.trim();
            }
            if (!labelText && el.getAttribute('aria-label')) {
                labelText = el.getAttribute('aria-label');
            }
            return {
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                label: labelText,
                required: el.required || false,
                visible: rect.width > 0 && rect.height > 0,
                value: el.value || '',
                options: el.tagName === 'SELECT' ?
                    Array.from(el.options).map(o => ({value: o.value, text: o.text})) : []
            };
        }).filter(e => e.visible);
    }""")

    if not form_elements or len(form_elements) < 2:
        return {"success": False, "error": f"Only {len(form_elements) if form_elements else 0} visible form elements found on page"}

    # Log what we found so debug is easier
    print(f"  DOM AI: found {len(form_elements)} form elements:", flush=True)
    for i, fe in enumerate(form_elements[:10]):
        print(f"    [{i}] <{fe['tag']} type={fe.get('type','')} name={fe.get('name','')} id={fe.get('id','')} label={fe.get('label','')[:40]}>", flush=True)

    # Step 2: Get page title and URL for context
    page_title = page.title()
    page_url = page.url

    # Step 3: Ask GPT-4o-mini to map fields to values (TEXT ONLY — no vision)
    prompt = f"""You are filling out a government public records request form.

Page: {page_title} ({page_url})
Department: {police_dept}

Form fields found on page:
{_json.dumps(form_elements, indent=2)}

Values to fill:
- Requester Name: {name}
- Requester Email: {email}
- Subject: {subject}
- Request Description: {body[:1500]}
- Phone (if required): 000-000-0000
- Address (if required): 123 Main St, New York, NY 10001

Return a JSON array of actions to fill the form. Each action should be:
{{"selector": "#id or [name=value]", "value": "text to fill", "action": "fill"}}

For select dropdowns, use:
{{"selector": "#id", "value": "option value", "action": "select"}}

For checkboxes that need checking:
{{"selector": "#id", "value": "true", "action": "check"}}

Rules:
- Use the most specific selector: prefer #id, then [name=x], then [placeholder=x]
- For name fields: if there's first/last split, split the name appropriately
- For the main text/description field: use the full request body
- Skip hidden fields and submit buttons
- Return ONLY the JSON array, no other text"""

    try:
        gemini_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent?key=" + gemini_key
        )
        payload = _json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 800},
        }).encode()
        req = urllib.request.Request(
            gemini_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = _json.loads(resp.read().decode())

        raw = resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Parse JSON from response
        json_match = re.search(r'\[[\s\S]*\]', raw)
        if not json_match:
            return {"success": False, "error": "AI returned unparseable response"}

        actions = _json.loads(json_match.group())
    except Exception as e:
        return {"success": False, "error": f"AI API call failed: {str(e)[:200]}"}

    # Step 4: Execute each action
    print(f"  Gemini returned {len(actions)} action(s):", flush=True)
    for i, a in enumerate(actions):
        print(f"    [{i}] {a.get('action','fill')} {a.get('selector','')} = {str(a.get('value',''))[:60]}", flush=True)

    filled_count = 0
    for action in actions:
        selector = action.get("selector", "")
        value = action.get("value", "")
        act_type = action.get("action", "fill")

        if not selector or not value:
            print(f"    SKIP: empty selector or value", flush=True)
            continue

        try:
            el = page.locator(selector).first
            if not el.is_visible():
                print(f"    SKIP: {selector} not visible", flush=True)
                continue

            if act_type == "select":
                el.select_option(value=value)
                filled_count += 1
            elif act_type == "check":
                if not el.is_checked():
                    el.check()
                filled_count += 1
            else:  # fill
                el.click()
                el.fill(value)
                filled_count += 1
            print(f"    Filled {selector}: {value[:50]}...")
        except Exception as e:
            print(f"    Could not fill {selector}: {e}")

    if filled_count == 0:
        return {"success": False, "error": "AI mapped fields but none could be filled"}

    print(f"  DOM AI filled {filled_count} fields, clicking submit...")

    # Step 5: Click submit
    submitted = _click_submit(page)

    if submitted:
        time.sleep(3)
        conf = _extract_confirmation(page)
        return {
            "success": True,
            "message": f"Submitted via DOM AI filler ({model}, {filled_count} fields)",
            "confirmation": conf,
            "portal_type": "ai_dom",
        }

    _save_debug_screenshot(page, "ai_dom")
    return {
        "success": False,
        "error": f"DOM AI filled {filled_count} fields but submit button not found",
        "portal_type": "ai_dom",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════════

def _save_debug_screenshot(page, portal_type):
    """Save a screenshot + page info for debugging failed submissions."""
    try:
        debug_dir = os.path.expanduser("~/.openclaw/workspace/debug")
        os.makedirs(debug_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{debug_dir}/{portal_type}_{timestamp}"
        page.screenshot(path=f"{filename}.png", full_page=True)
        # Also dump visible form elements for debugging
        forms_info = page.evaluate("""() => {
            const inputs = document.querySelectorAll('input, textarea, select, button[type=submit]');
            return Array.from(inputs).slice(0, 30).map(el => ({
                tag: el.tagName, type: el.type || '', name: el.name || '',
                id: el.id || '', placeholder: el.placeholder || '',
                visible: el.offsetParent !== null
            }));
        }""")
        with open(f"{filename}.json", "w") as f:
            import json
            json.dump({"url": page.url, "title": page.title(), "forms": forms_info}, f, indent=2)
        print(f"  Debug saved: {filename}.png + .json")
    except Exception as e:
        print(f"  Could not save debug screenshot: {e}")


def _try_login(page, email, password):
    """Try to log into a portal."""
    for selector in ["input[type='email']", "input[name='email']", "#email", "input[placeholder*='email' i]"]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(email)
                break
        except Exception:
            continue

    if password:
        for selector in ["input[type='password']", "input[name='password']", "#password"]:
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).first.fill(password)
                    break
            except Exception:
                continue

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


def _fill_by_labels(page, body, subject, name, email):
    """Fill form fields by finding labels and their associated inputs.

    Works on FormCenter (Field_[ID]) and other CMS forms that use
    <label for="fieldId">Label Text</label> pattern.
    Returns count of fields filled.
    """
    filled = 0

    # Map of label text patterns to values
    field_map = [
        (["description", "request", "details", "message", "information",
          "records requested", "nature of request", "body", "comment"], body, "textarea"),
        (["subject", "title", "regarding", "re:"], subject, "input"),
        (["name", "full name", "your name", "requester name",
          "first name", "contact name", "requestor"], name, "input"),
        (["email", "e-mail", "email address", "your email",
          "requester email", "contact email"], email, "input"),
    ]

    try:
        labels = page.locator("label").all()
        for label in labels:
            try:
                label_text = label.text_content().strip().lower()
                if not label_text:
                    continue

                # Find the associated input via 'for' attribute
                for_attr = label.get_attribute("for")

                for patterns, value, field_type in field_map:
                    if any(p in label_text for p in patterns):
                        el = None
                        if for_attr:
                            # Method 1: Use the 'for' attribute
                            if field_type == "textarea":
                                el = page.locator(f"textarea#{for_attr}, textarea[name='{for_attr}']")
                            else:
                                el = page.locator(f"#{for_attr}, input[name='{for_attr}']")
                        if not el or el.count() == 0:
                            # Method 2: Find input that is a sibling/child of the label
                            if field_type == "textarea":
                                el = label.locator(".. >> textarea")
                            else:
                                el = label.locator(".. >> input:not([type='hidden']):not([type='submit'])")

                        if el and el.count() > 0 and el.first.is_visible():
                            el.first.click()
                            el.first.fill(value)
                            print(f"    Label fill: '{label_text[:30]}' -> {value[:40]}...", flush=True)
                            filled += 1
                            break
            except Exception:
                continue
    except Exception:
        pass

    return filled


def _fill_form_fields(page, body, subject, name, email):
    """Try to fill common form fields using CSS selectors. Returns count of fields filled."""
    filled = 0

    def _try_fill(selectors, value, field_name):
        for selector in selectors:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.click()
                    el.first.fill(value)
                    print(f"    CSS fill: {field_name} via {selector}", flush=True)
                    return 1
            except Exception:
                continue
        return 0

    # Subject / Title field
    filled += _try_fill([
        "input[name*='subject' i]", "input[name*='title' i]",
        "input[placeholder*='subject' i]", "input[placeholder*='title' i]",
        "input[aria-label*='subject' i]", "input[aria-label*='title' i]",
        "#subject", "#title", "#request_title",
        "input[id*='subject' i]", "input[id*='title' i]",
    ], subject, "subject")

    # Main request body / description
    filled += _try_fill([
        "textarea[name*='description' i]", "textarea[name*='body' i]",
        "textarea[name*='request' i]", "textarea[name*='message' i]",
        "textarea[name*='details' i]", "textarea[name*='content' i]",
        "textarea[placeholder*='describe' i]", "textarea[placeholder*='request' i]",
        "textarea[aria-label*='description' i]", "textarea[aria-label*='request' i]",
        "#description", "#request_body", "#message", "#details",
        "textarea[id*='description' i]", "textarea[id*='request' i]",
        "textarea[id*='details' i]", "textarea[id*='message' i]",
        # FormCenter-style fields
        "textarea[id^='Field_']",
        # Last resort: first textarea on page
        "textarea",
    ], body, "body/description")

    # Name field
    filled += _try_fill([
        "input[name*='name' i]:not([name*='email']):not([name*='user']):not([name*='pass'])",
        "input[placeholder*='name' i]:not([placeholder*='email'])",
        "input[aria-label*='name' i]:not([aria-label*='email'])",
        "#name", "#requester_name", "#full_name", "#contact_name",
        "input[id*='name' i]:not([id*='email']):not([id*='user'])",
    ], name, "name")

    # Email field
    filled += _try_fill([
        "input[type='email']", "input[name*='email' i]",
        "input[placeholder*='email' i]", "#email", "#requester_email",
        "input[id*='email' i]", "input[aria-label*='email' i]",
    ], email, "email")

    return filled


def _fill_phone_field(page, phone):
    """Fill phone field if visible (many portals require it)."""
    for selector in [
        "input[type='tel']", "input[name*='phone' i]",
        "input[placeholder*='phone' i]", "input[id*='phone' i]",
        "input[name*='telephone' i]", "input[aria-label*='phone' i]",
    ]:
        try:
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                el.first.fill(phone)
                return
        except Exception:
            continue


def _check_required_boxes(page):
    """Check any required checkboxes (terms, acknowledgment, etc.)."""
    try:
        checkboxes = page.locator("input[type='checkbox'][required], input[type='checkbox'][aria-required='true']")
        for i in range(checkboxes.count()):
            try:
                if not checkboxes.nth(i).is_checked():
                    checkboxes.nth(i).check()
            except Exception:
                continue
    except Exception:
        pass


def _click_submit(page) -> bool:
    """Try to find and click a submit button. Returns True if clicked."""
    for selector in [
        "button[type='submit']",
        "button:has-text('Submit')", "button:has-text('Send')",
        "button:has-text('Create')", "button:has-text('File Request')",
        "input[type='submit']",
        "a:has-text('Submit')", "a:has-text('Send Request')",
        ".submit-btn", "#submit", "#submit-request",
        "button[name*='submit' i]", "button[id*='submit' i]",
        # FormCenter submit buttons
        "button.formSubmit", "input.formSubmit",
        "button:has-text('Submit Request')", "button:has-text('Send Request')",
        # JustFOIA / Angular / React app buttons
        "button:has-text('Save')", "button:has-text('Continue')",
        "button:has-text('Next')", "button:has-text('Complete')",
        "button.btn-primary", "button.btn-success",
        "a.btn:has-text('Submit')", "a.btn-primary:has-text('Submit')",
        # Any visible button that looks like submit
        "button[class*='submit' i]", "button[class*='send' i]",
        "input[value*='Submit' i]", "input[value*='Send' i]",
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


def _extract_confirmation(page) -> str:
    """Try to extract a confirmation/tracking number from the current page."""
    try:
        page_text = page.content()
        conf_match = re.search(
            r'(?:request|confirmation|tracking|reference|case)\s*(?:#|number|id|no\.?)?\s*[:\s]*([A-Z0-9][\w-]{3,20})',
            page_text, re.IGNORECASE
        )
        return conf_match.group(1) if conf_match else ""
    except Exception:
        return ""


def _find_form_iframe(page):
    """Check if the form is inside an iframe and return the frame's page."""
    try:
        iframes = page.locator("iframe").all()
        for iframe in iframes:
            try:
                src = iframe.get_attribute("src") or ""
                name = iframe.get_attribute("name") or ""
                # Look for form-related iframes
                if any(kw in (src + name).lower() for kw in ["form", "request", "foia", "record"]):
                    frame = iframe.content_frame()
                    if frame:
                        return frame
            except Exception:
                continue
    except Exception:
        pass
    return None
