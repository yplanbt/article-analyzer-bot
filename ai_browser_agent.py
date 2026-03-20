"""AI-powered browser agent using Claude vision + Playwright.

Takes screenshots, sends them to Claude, and executes Claude's instructions
to navigate portals, create accounts, fill forms, and submit FOIA requests.
Integrates with 2captcha for automatic CAPTCHA solving.
"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)


MAX_STEPS = 20
STEP_TIMEOUT = 10  # seconds between actions


def _take_screenshot(page) -> str:
    """Take a screenshot and return as base64."""
    screenshot_bytes = page.screenshot(full_page=False)
    return base64.b64encode(screenshot_bytes).decode("utf-8")


def _send_to_claude(client, screenshot_b64: str, task_context: str, history: list) -> dict:
    """Send screenshot to Claude and get next action."""
    messages = []

    # Build conversation history
    for entry in history[-6:]:  # Keep last 6 exchanges to stay in context
        messages.append({"role": "user", "content": entry["user"]})
        messages.append({"role": "assistant", "content": entry["assistant"]})

    # Current step
    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            },
        },
        {
            "type": "text",
            "text": f"""You are a browser automation agent. Look at this screenshot and decide what to do next.

TASK: {task_context}

Based on what you see on the screen, return a JSON action. Available actions:

1. {{"action": "click", "selector": "CSS selector or text content", "description": "what you're clicking"}}
2. {{"action": "type", "selector": "CSS selector", "text": "text to type", "description": "what field"}}
3. {{"action": "select", "selector": "CSS selector", "value": "option value", "description": "what dropdown"}}
4. {{"action": "scroll", "direction": "down" or "up", "description": "why scrolling"}}
5. {{"action": "wait", "seconds": 2, "description": "why waiting"}}
6. {{"action": "navigate", "url": "URL to go to", "description": "why navigating"}}
7. {{"action": "done", "success": true/false, "message": "what happened", "confirmation": "confirmation number if any"}}
8. {{"action": "captcha_detected", "captcha_type": "recaptcha" or "hcaptcha" or "image", "selector": "CSS selector of captcha iframe/element", "description": "what type of CAPTCHA you see"}}

Rules:
- If you see a login/signup page and we need to create an account, do it
- If you see a CAPTCHA (reCAPTCHA checkbox, hCaptcha, distorted text image), use the captcha_detected action — we have an automated solver
- If you see a form, fill it out with the provided information
- If you see a success/confirmation page, return done with success=true
- If you're stuck or the page looks wrong, return done with success=false
- For click actions, describe the element clearly (e.g., "button with text 'Submit'")
- Prefer using visible text content for selectors: "text=Submit Request"
- Return ONLY the JSON action, no other text""",
        },
    ]
    messages.append({"role": "user", "content": user_content})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=messages,
    )

    raw = response.content[0].text.strip()
    # Extract JSON from response
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if json_match:
        return json.loads(json_match.group())
    return {"action": "done", "success": False, "message": f"Could not parse action: {raw[:200]}"}


def _execute_action(page, action: dict) -> str:
    """Execute a browser action. Returns status message."""
    act = action.get("action", "")
    desc = action.get("description", "")

    try:
        if act == "click":
            selector = action.get("selector", "")
            # Try text-based selector first
            if selector.startswith("text="):
                page.locator(selector).first.click(timeout=5000)
            else:
                # Try as CSS selector
                try:
                    page.locator(selector).first.click(timeout=5000)
                except Exception:
                    # Fallback: try as text content
                    page.locator(f"text={selector}").first.click(timeout=5000)
            return f"Clicked: {desc}"

        elif act == "type":
            selector = action.get("selector", "")
            text = action.get("text", "")
            try:
                el = page.locator(selector).first
                el.click(timeout=3000)
                el.fill(text)
            except Exception:
                # Try finding by placeholder or label
                for alt in [
                    f"input[placeholder*='{selector}' i]",
                    f"textarea[placeholder*='{selector}' i]",
                    f"input[name*='{selector}' i]",
                    f"textarea[name*='{selector}' i]",
                ]:
                    try:
                        el = page.locator(alt).first
                        el.click(timeout=2000)
                        el.fill(text)
                        break
                    except Exception:
                        continue
            return f"Typed in: {desc}"

        elif act == "select":
            selector = action.get("selector", "")
            value = action.get("value", "")
            page.locator(selector).first.select_option(value=value, timeout=5000)
            return f"Selected: {desc}"

        elif act == "scroll":
            direction = action.get("direction", "down")
            if direction == "down":
                page.mouse.wheel(0, 500)
            else:
                page.mouse.wheel(0, -500)
            return f"Scrolled {direction}: {desc}"

        elif act == "wait":
            seconds = min(action.get("seconds", 2), 5)
            time.sleep(seconds)
            return f"Waited {seconds}s: {desc}"

        elif act == "navigate":
            url = action.get("url", "")
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return f"Navigated to: {desc}"

        elif act == "captcha_detected":
            return _handle_captcha(page, action)

        elif act == "done":
            return "DONE"

        else:
            return f"Unknown action: {act}"

    except Exception as e:
        return f"Action failed ({act}): {str(e)[:100]}"


def _handle_captcha(page, action: dict) -> str:
    """Handle CAPTCHA using 2captcha service."""
    captcha_type = action.get("captcha_type", "unknown")
    captcha_key = os.environ.get("CAPTCHA_API_KEY", "")

    if not captcha_key:
        logger.warning("CAPTCHA detected but no CAPTCHA_API_KEY set — cannot solve")
        return "CAPTCHA detected but no solver API key configured"

    try:
        from captcha_solver import solve_recaptcha_v2, solve_hcaptcha, solve_image_captcha
    except ImportError:
        return "CAPTCHA detected but captcha_solver module not found"

    page_url = page.url
    logger.info("Handling %s CAPTCHA on %s", captcha_type, page_url)

    if captcha_type == "recaptcha":
        # Extract the reCAPTCHA site key from the page
        site_key = _extract_recaptcha_sitekey(page)
        if not site_key:
            return "reCAPTCHA detected but could not extract site key"

        result = solve_recaptcha_v2(captcha_key, site_key, page_url)
        if result["success"]:
            # Inject the solution into the page
            page.evaluate(f"""
                document.getElementById('g-recaptcha-response').value = '{result["solution"]}';
                document.getElementById('g-recaptcha-response').style.display = 'block';
            """)
            # Try to trigger the callback
            page.evaluate("""
                try {
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        Object.keys(___grecaptcha_cfg.clients).forEach(key => {
                            const client = ___grecaptcha_cfg.clients[key];
                            Object.keys(client).forEach(k => {
                                const item = client[k];
                                if (item && item.callback) item.callback(arguments[0]);
                            });
                        });
                    }
                } catch(e) {}
            """)
            return f"reCAPTCHA solved and injected"
        return f"reCAPTCHA solve failed: {result['error']}"

    elif captcha_type == "hcaptcha":
        site_key = _extract_hcaptcha_sitekey(page)
        if not site_key:
            return "hCaptcha detected but could not extract site key"

        result = solve_hcaptcha(captcha_key, site_key, page_url)
        if result["success"]:
            page.evaluate(f"""
                document.querySelector('[name="h-captcha-response"]').value = '{result["solution"]}';
                document.querySelector('[name="g-recaptcha-response"]').value = '{result["solution"]}';
            """)
            return f"hCaptcha solved and injected"
        return f"hCaptcha solve failed: {result['error']}"

    elif captcha_type == "image":
        # Take screenshot of just the CAPTCHA element
        selector = action.get("selector", "")
        try:
            if selector:
                el = page.locator(selector).first
                img_bytes = el.screenshot()
            else:
                img_bytes = page.screenshot(full_page=False)
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        except Exception:
            img_bytes = page.screenshot(full_page=False)
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")

        result = solve_image_captcha(captcha_key, img_b64)
        if result["success"]:
            # Type the solution into the CAPTCHA input field
            try:
                captcha_input = page.locator("input[name*='captcha' i], input[id*='captcha' i], input[placeholder*='captcha' i]").first
                captcha_input.fill(result["solution"])
                return f"Image CAPTCHA solved: typed '{result['solution']}'"
            except Exception:
                return f"Image CAPTCHA solved ({result['solution']}) but could not find input field"
        return f"Image CAPTCHA solve failed: {result['error']}"

    return f"Unknown CAPTCHA type: {captcha_type}"


def _extract_recaptcha_sitekey(page) -> str:
    """Extract reCAPTCHA site key from the page."""
    try:
        return page.evaluate("""
            (() => {
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                const iframe = document.querySelector('iframe[src*="recaptcha"]');
                if (iframe) {
                    const match = iframe.src.match(/[?&]k=([^&]+)/);
                    if (match) return match[1];
                }
                return '';
            })()
        """)
    except Exception:
        return ""


def _extract_hcaptcha_sitekey(page) -> str:
    """Extract hCaptcha site key from the page."""
    try:
        return page.evaluate("""
            (() => {
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                const iframe = document.querySelector('iframe[src*="hcaptcha"]');
                if (iframe) {
                    const match = iframe.src.match(/sitekey=([^&]+)/);
                    if (match) return match[1];
                }
                return '';
            })()
        """)
    except Exception:
        return ""


def ai_submit_portal(
    portal_url: str,
    request_body: str,
    subject: str,
    requester_name: str,
    requester_email: str,
    requester_password: str = "",
    police_dept: str = "",
    anthropic_key: str = "",
    headless: bool = True,
) -> dict:
    """Use AI vision agent to navigate and submit through any portal.

    Args:
        portal_url: The FOIA portal URL
        request_body: The FOIA letter text
        subject: Request subject line
        requester_name: Name for the request
        requester_email: Email for the request and account creation
        requester_password: Password for account creation/login
        police_dept: Department name for context
        anthropic_key: Anthropic API key for Claude vision
        headless: Run browser without visible window

    Returns:
        dict with success, message, confirmation keys
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": "Playwright not installed. Run: pip install playwright && playwright install chromium",
        }

    import anthropic
    client = anthropic.Anthropic(api_key=anthropic_key)

    # Build the task context for Claude
    task_context = f"""Submit a FOIA/public records request to {police_dept} through their portal at {portal_url}.

REQUEST DETAILS:
- Subject: {subject}
- Requester Name: {requester_name}
- Requester Email: {requester_email}
- Request Body (paste into the main text field/description):
{request_body[:2000]}

INSTRUCTIONS:
1. If there's a "Submit Request" or "New Request" button, click it
2. If login is required, try logging in with email: {requester_email} and password: {requester_password}
3. If no account exists and signup is available, create one with the email and password above
4. Fill out ALL required form fields — use the request details above
5. For any required fields you don't have data for, use reasonable defaults
6. Look for and click the final Submit/Send button
7. If you see a confirmation page or number, capture it
8. If you encounter a CAPTCHA (reCAPTCHA, hCaptcha, image), use the captcha_detected action — we'll solve it automatically"""

    history = []
    steps_log = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.set_default_timeout(15000)

            # Navigate to the portal
            try:
                page.goto(portal_url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                browser.close()
                return {"success": False, "error": f"Could not load portal: {str(e)[:100]}"}

            time.sleep(3)  # Let page fully render

            for step in range(MAX_STEPS):
                # Take screenshot
                try:
                    screenshot_b64 = _take_screenshot(page)
                except Exception:
                    steps_log.append(f"Step {step}: Screenshot failed")
                    break

                # Ask Claude what to do
                try:
                    action = _send_to_claude(client, screenshot_b64, task_context, history)
                except Exception as e:
                    steps_log.append(f"Step {step}: Claude error: {str(e)[:100]}")
                    break

                steps_log.append(f"Step {step}: {action.get('action', '?')} — {action.get('description', '')}")

                # Check if done
                if action.get("action") == "done":
                    browser.close()
                    return {
                        "success": action.get("success", False),
                        "message": action.get("message", ""),
                        "confirmation": action.get("confirmation", ""),
                        "steps": steps_log,
                        "portal_type": "ai_agent",
                    }

                # Execute the action
                result_msg = _execute_action(page, action)
                steps_log.append(f"  → {result_msg}")

                # Save to history for context
                history.append({
                    "user": f"[Screenshot of current page state]",
                    "assistant": json.dumps(action),
                })

                time.sleep(STEP_TIMEOUT)

            # Ran out of steps
            browser.close()
            return {
                "success": False,
                "error": f"Agent reached max steps ({MAX_STEPS}) without completing",
                "steps": steps_log,
                "portal_type": "ai_agent",
            }

    except Exception as e:
        return {
            "success": False,
            "error": f"AI browser agent error: {str(e)[:200]}",
            "steps": steps_log,
            "portal_type": "ai_agent",
        }
