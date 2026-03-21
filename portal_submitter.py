"""Automated FOIA portal submission using Playwright browser automation.

Supports: GovQA, NextRequest, JustFOIA, FormCenter (CivicPlus), JotForm,
and a smart generic fallback. AI vision is optional last resort.
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
    proxy_url = proxy or os.environ.get("US_PROXY", "")

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

            browser.close()

            # If Playwright failed and AI fallback is enabled, try AI agent
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


def _submit_justfoia(page, url, body, subject, name, email, creds):
    """Submit through JustFOIA portal.

    JustFOIA portals follow pattern: *.justfoia.com/publicportal
    The new request form is at /publicportal/home/newrequest
    Fields: Request Description (textarea), First Name, Last Name, Email, Phone, Address
    """
    # Navigate directly to the new request form
    if "/newrequest" not in url.lower():
        base = url.rstrip("/")
        if "/publicportal" in base:
            new_request_url = base.rsplit("/publicportal", 1)[0] + "/publicportal/home/newrequest"
        else:
            new_request_url = base + "/publicportal/home/newrequest"
    else:
        new_request_url = url

    page.goto(new_request_url, wait_until="networkidle")
    time.sleep(3)

    try:
        # JustFOIA uses specific field patterns
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

        # Also try label-based filling
        _fill_by_labels(page, body, subject, name, email)

        # First Name / Last Name (JustFOIA splits name)
        name_parts = name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        for selector in [
            "input[id*='first' i]", "input[name*='first' i]",
            "input[placeholder*='first' i]",
        ]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.fill(first_name)
                    break
            except Exception:
                continue

        for selector in [
            "input[id*='last' i]", "input[name*='last' i]",
            "input[placeholder*='last' i]",
        ]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.fill(last_name)
                    break
            except Exception:
                continue

        # Email
        for selector in [
            "input[type='email']", "input[id*='email' i]",
            "input[name*='email' i]", "input[placeholder*='email' i]",
        ]:
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

        # Submit
        submitted = _click_submit(page)

        if submitted:
            time.sleep(3)
            conf = _extract_confirmation(page)
            return {
                "success": True,
                "message": "Submitted via JustFOIA",
                "confirmation": conf,
                "portal_type": "justfoia",
            }
        else:
            _save_debug_screenshot(page, "justfoia")
            return {
                "success": False,
                "error": "Could not complete JustFOIA submission — no submit button found",
                "portal_type": "justfoia",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        _save_debug_screenshot(page, "justfoia")
        return {"success": False, "error": f"JustFOIA error: {str(e)}", "portal_type": "justfoia"}


def _submit_formcenter(page, url, body, subject, name, email, creds):
    """Submit through CivicPlus FormCenter portal.

    FormCenter uses Field_[ID] naming for inputs. We match fields by
    scanning <label> text and filling the associated input.
    """
    page.goto(url, wait_until="networkidle")
    time.sleep(3)  # FormCenter loads forms via JS, needs extra wait

    try:
        # FormCenter renders forms dynamically — wait for form elements
        page.wait_for_selector("form, .formContent, #FormCenter", timeout=10000)
        time.sleep(1)

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
    page.goto(url, wait_until="networkidle")
    time.sleep(3)  # Extra wait for JS-rendered forms

    try:
        # Check for iframes that might contain the form
        iframe_page = _find_form_iframe(page)
        target = iframe_page if iframe_page else page

        # Strategy 1: Label-based matching (works on most CMS forms)
        filled_labels = _fill_by_labels(target, body, subject, name, email)

        # Strategy 2: Standard CSS selectors
        if not filled_labels:
            _fill_form_fields(target, body, subject, name, email)

        # Fill phone if visible
        _fill_phone_field(target, "000-000-0000")

        # Check required boxes
        _check_required_boxes(target)

        submitted = _click_submit(target)

        if submitted:
            time.sleep(3)
            conf_num = _extract_confirmation(page)  # Check main page for confirmation
            return {
                "success": True,
                "message": "Submitted via portal form",
                "confirmation": conf_num,
                "portal_type": "generic",
            }
        else:
            _save_debug_screenshot(page, "generic")
            return {
                "success": False,
                "error": "Could not auto-submit — unknown portal layout",
                "portal_type": "unknown",
                "needs_manual": True,
                "page_url": page.url,
            }

    except Exception as e:
        _save_debug_screenshot(page, "generic")
        return {"success": False, "error": f"Generic portal error: {str(e)}", "portal_type": "unknown"}


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


def _fill_by_labels(page, body, subject, name, email) -> bool:
    """Fill form fields by finding labels and their associated inputs.

    Works on FormCenter (Field_[ID]) and other CMS forms that use
    <label for="fieldId">Label Text</label> pattern.
    Returns True if at least one field was filled.
    """
    filled = 0

    # Map of label text patterns to values
    field_map = [
        (["description", "request", "details", "message", "information",
          "records requested", "nature of request", "body"], body, "textarea"),
        (["subject", "title", "regarding", "re:"], subject, "input"),
        (["name", "full name", "your name", "requester name",
          "first name", "contact name"], name, "input"),
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
                if not for_attr:
                    continue

                for patterns, value, field_type in field_map:
                    if any(p in label_text for p in patterns):
                        if field_type == "textarea":
                            el = page.locator(f"textarea#{for_attr}, textarea[name='{for_attr}']")
                        else:
                            el = page.locator(f"#{for_attr}, input[name='{for_attr}']")
                        if el.count() > 0 and el.first.is_visible():
                            el.first.fill(value)
                            filled += 1
                            break
            except Exception:
                continue
    except Exception:
        pass

    return filled > 0


def _fill_form_fields(page, body, subject, name, email):
    """Try to fill common form fields using CSS selectors."""
    # Subject / Title field
    for selector in [
        "input[name*='subject' i]", "input[name*='title' i]",
        "input[placeholder*='subject' i]", "input[placeholder*='title' i]",
        "input[aria-label*='subject' i]", "input[aria-label*='title' i]",
        "#subject", "#title", "#request_title",
        "input[id*='subject' i]", "input[id*='title' i]",
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
        "textarea[id*='description' i]", "textarea[id*='request' i]",
        "textarea[id*='details' i]", "textarea[id*='message' i]",
        # FormCenter-style fields
        "textarea[id^='Field_']",
        # Last resort: first textarea on page
        "textarea",
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(body)
                break
        except Exception:
            continue

    # Name field
    for selector in [
        "input[name*='name' i]:not([name*='email']):not([name*='user']):not([name*='pass'])",
        "input[placeholder*='name' i]:not([placeholder*='email'])",
        "input[aria-label*='name' i]:not([aria-label*='email'])",
        "#name", "#requester_name", "#full_name", "#contact_name",
        "input[id*='name' i]:not([id*='email']):not([id*='user'])",
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
        "input[id*='email' i]", "input[aria-label*='email' i]",
    ]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(email)
                break
        except Exception:
            continue


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
