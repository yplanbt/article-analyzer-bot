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
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


MAX_STEPS = 20
STEP_TIMEOUT = 10  # seconds between actions


def _get_stealth_script() -> str:
    """Return JS to inject into browser contexts to avoid bot detection."""
    return """
    // Hide webdriver flag (biggest giveaway)
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Fake plugins array (headless Chrome has empty plugins)
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ];
            plugins.length = 3;
            return plugins;
        }
    });

    // Fake languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Stub chrome.runtime (Cloudflare checks this)
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = { connect: () => {}, sendMessage: () => {} };

    // Override WebGL renderer to look like real GPU
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';           // UNMASKED_VENDOR
        if (param === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER
        return getParameter.call(this, param);
    };

    // Fix Permissions API (some sites check this)
    const origQuery = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(params) {
        if (params.name === 'notifications') {
            return Promise.resolve({ state: Notification.permission });
        }
        return origQuery.call(this, params);
    };

    // Hide automation-related properties
    delete navigator.__proto__.webdriver;

    // Override connection info to look non-automated
    Object.defineProperty(navigator, 'connection', {
        get: () => ({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false })
    });
    """


def _detect_captcha_js(page) -> dict | None:
    """Detect CAPTCHAs via JavaScript (catches invisible ones Claude can't see)."""
    try:
        return page.evaluate("""
            (() => {
                // reCAPTCHA v2 (visible checkbox)
                const v2 = document.querySelector('.g-recaptcha, [data-sitekey]:not([data-size="invisible"])');
                if (v2) {
                    const key = v2.getAttribute('data-sitekey');
                    if (key) return {type: 'recaptcha', sitekey: key};
                }
                // reCAPTCHA v3 or invisible v2 (from script tag)
                const scripts = document.querySelectorAll('script[src*="recaptcha/api.js"]');
                for (const s of scripts) {
                    const m = s.src.match(/render=([^&]+)/);
                    if (m && m[1] !== 'explicit') return {type: 'recaptcha_v3', sitekey: m[1]};
                }
                // Invisible v2 (data-size="invisible")
                const inv = document.querySelector('[data-sitekey][data-size="invisible"]');
                if (inv) {
                    const key = inv.getAttribute('data-sitekey');
                    if (key) return {type: 'recaptcha_v3', sitekey: key};
                }
                // hCaptcha
                const hc = document.querySelector('.h-captcha, [data-hcaptcha-widget-id]');
                if (hc) {
                    const key = hc.getAttribute('data-sitekey');
                    if (key) return {type: 'hcaptcha', sitekey: key};
                }
                return null;
            })()
        """)
    except Exception:
        return None


def _take_screenshot(page) -> str:
    """Take a screenshot and return as base64."""
    screenshot_bytes = page.screenshot(full_page=False)
    return base64.b64encode(screenshot_bytes).decode("utf-8")


VISION_PROMPT = """You are a browser automation agent. Look at this screenshot and decide what to do next.

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
- Return ONLY the JSON action, no other text"""


def _send_to_vision(client, screenshot_b64: str, task_context: str, history: list, provider: str = "openai") -> dict:
    """Send screenshot to vision model (OpenAI or Anthropic) and get next action."""
    prompt_text = VISION_PROMPT.format(task_context=task_context)

    if provider == "anthropic":
        return _send_to_claude(client, screenshot_b64, prompt_text, history)
    else:
        return _send_to_openai(client, screenshot_b64, prompt_text, history)


def _send_to_openai(client, screenshot_b64: str, prompt_text: str, history: list) -> dict:
    """Send screenshot to OpenAI GPT-4o/GPT-5.4 vision and get next action."""
    messages = []

    for entry in history[-6:]:
        messages.append({"role": "user", "content": entry["user"]})
        messages.append({"role": "assistant", "content": entry["assistant"]})

    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
        {"type": "text", "text": prompt_text},
    ]
    messages.append({"role": "user", "content": user_content})

    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_VISION_MODEL", "gpt-4o"),
        max_tokens=500,
        messages=messages,
    )

    raw = response.choices[0].message.content.strip()
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if json_match:
        return json.loads(json_match.group())
    return {"action": "done", "success": False, "message": f"Could not parse action: {raw[:200]}"}


def _send_to_claude(client, screenshot_b64: str, prompt_text: str, history: list) -> dict:
    """Send screenshot to Claude Sonnet vision and get next action."""
    messages = []

    for entry in history[-6:]:
        messages.append({"role": "user", "content": entry["user"]})
        messages.append({"role": "assistant", "content": entry["assistant"]})

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            },
        },
        {"type": "text", "text": prompt_text},
    ]
    messages.append({"role": "user", "content": user_content})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=messages,
    )

    raw = response.content[0].text.strip()
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
    # Allow sitekey to be passed directly (from JS detection) or extracted from page
    provided_sitekey = action.get("sitekey", "")
    captcha_key = os.environ.get("CAPTCHA_API_KEY", "")

    if not captcha_key:
        logger.warning("CAPTCHA detected but no CAPTCHA_API_KEY set — cannot solve")
        return "CAPTCHA detected but no solver API key configured"

    try:
        from captcha_solver import solve_recaptcha_v2, solve_recaptcha_v3, solve_hcaptcha, solve_image_captcha
    except ImportError:
        return "CAPTCHA detected but captcha_solver module not found"

    page_url = page.url
    logger.info("Handling %s CAPTCHA on %s", captcha_type, page_url)

    if captcha_type == "recaptcha":
        site_key = provided_sitekey or _extract_recaptcha_sitekey(page)
        if not site_key:
            return "reCAPTCHA detected but could not extract site key"

        result = solve_recaptcha_v2(captcha_key, site_key, page_url)
        if result["success"]:
            _inject_recaptcha_solution(page, result["solution"])
            return "reCAPTCHA v2 solved and injected"
        return f"reCAPTCHA solve failed: {result['error']}"

    elif captcha_type == "recaptcha_v3":
        site_key = provided_sitekey or _extract_recaptcha_sitekey(page)
        if not site_key:
            return "reCAPTCHA v3 detected but could not extract site key"

        result = solve_recaptcha_v3(captcha_key, site_key, page_url)
        if result["success"]:
            _inject_recaptcha_solution(page, result["solution"])
            return "reCAPTCHA v3 solved and injected"
        return f"reCAPTCHA v3 solve failed: {result['error']}"

    elif captcha_type == "hcaptcha":
        site_key = provided_sitekey or _extract_hcaptcha_sitekey(page)
        if not site_key:
            return "hCaptcha detected but could not extract site key"

        result = solve_hcaptcha(captcha_key, site_key, page_url)
        if result["success"]:
            _inject_hcaptcha_solution(page, result["solution"])
            return "hCaptcha solved and injected"
        return f"hCaptcha solve failed: {result['error']}"

    elif captcha_type == "image":
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
            try:
                captcha_input = page.locator("input[name*='captcha' i], input[id*='captcha' i], input[placeholder*='captcha' i]").first
                captcha_input.fill(result["solution"])
                return f"Image CAPTCHA solved: typed '{result['solution']}'"
            except Exception:
                return f"Image CAPTCHA solved ({result['solution']}) but could not find input field"
        return f"Image CAPTCHA solve failed: {result['error']}"

    return f"Unknown CAPTCHA type: {captcha_type}"


def _inject_recaptcha_solution(page, token: str) -> None:
    """Inject reCAPTCHA solution with robust multi-method callback triggering."""
    page.evaluate("""(token) => {
        // Method 1: Set textarea value (required for form submission)
        const ta = document.getElementById('g-recaptcha-response');
        if (ta) { ta.value = token; ta.style.display = 'block'; }
        // Also set any hidden textareas in iframes
        document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => { el.value = token; });

        // Method 2: Deep callback traversal through ___grecaptcha_cfg
        try {
            if (typeof ___grecaptcha_cfg !== 'undefined') {
                const clients = ___grecaptcha_cfg.clients;
                for (const cid of Object.keys(clients)) {
                    const c = clients[cid];
                    for (const k of Object.keys(c)) {
                        const v = c[k];
                        if (v && typeof v === 'object') {
                            // Direct callback
                            if (typeof v.callback === 'function') { v.callback(token); return; }
                            // Nested one level deeper
                            for (const kk of Object.keys(v)) {
                                if (v[kk] && typeof v[kk] === 'object' && typeof v[kk].callback === 'function') {
                                    v[kk].callback(token); return;
                                }
                            }
                        }
                    }
                }
            }
        } catch(e) {}

        // Method 3: Try form submit
        try {
            const recaptchaEl = document.querySelector('.g-recaptcha');
            if (recaptchaEl) {
                const form = recaptchaEl.closest('form');
                if (form) { form.submit(); return; }
            }
        } catch(e) {}

        // Method 4: Click submit button
        try {
            const btn = document.querySelector('button[type="submit"], input[type="submit"]');
            if (btn) btn.click();
        } catch(e) {}
    }""", token)


def _inject_hcaptcha_solution(page, token: str) -> None:
    """Inject hCaptcha solution with callback triggering."""
    page.evaluate("""(token) => {
        // Set response textareas
        document.querySelectorAll('[name="h-captcha-response"]').forEach(el => { el.value = token; });
        document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => { el.value = token; });

        // Try hcaptcha callback
        try {
            if (typeof hcaptcha !== 'undefined') {
                const widgetIds = hcaptcha.getAllResponse ? Object.keys(hcaptcha.getAllResponse()) : [];
                // Trigger via internal API if available
            }
        } catch(e) {}

        // Try form submit as fallback
        try {
            const hcEl = document.querySelector('.h-captcha');
            if (hcEl) {
                const form = hcEl.closest('form');
                if (form) { form.submit(); return; }
            }
        } catch(e) {}

        // Click submit button
        try {
            const btn = document.querySelector('button[type="submit"], input[type="submit"]');
            if (btn) btn.click();
        } catch(e) {}
    }""", token)


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
    openai_key: str = "",
    headless: bool = True,
    proxy: str = "",
) -> dict:
    """Use AI vision agent to navigate and submit through any portal.

    Uses OpenAI (GPT-4o) by default for vision. Falls back to Anthropic (Claude Sonnet)
    if OpenAI key is not available. Set OPENAI_API_KEY env var or pass openai_key.

    Args:
        portal_url: The FOIA portal URL
        request_body: The FOIA letter text
        subject: Request subject line
        requester_name: Name for the request
        requester_email: Email for the request and account creation
        requester_password: Password for account creation/login
        police_dept: Department name for context
        anthropic_key: Anthropic API key for Claude vision (fallback)
        openai_key: OpenAI API key for GPT-4o vision (preferred)
        headless: Run browser without visible window
        proxy: US proxy URL (e.g. http://user:pass@us.proxy.com:8080). Falls back to US_PROXY env var.

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

    # Prefer OpenAI (free via Kevin's subscription), fall back to Anthropic
    _openai_key = openai_key or os.environ.get("OPENAI_API_KEY", "")
    _anthropic_key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY", "")

    if _openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=_openai_key)
            vision_provider = "openai"
            logger.info("Using OpenAI for vision (GPT-4o)")
        except ImportError:
            return {"success": False, "error": "OpenAI package not installed. Run: pip install openai"}
    elif _anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=_anthropic_key)
            vision_provider = "anthropic"
            logger.info("Using Anthropic for vision (Claude Sonnet)")
        except ImportError:
            return {"success": False, "error": "Anthropic package not installed. Run: pip install anthropic"}
    else:
        return {"success": False, "error": "No vision API key. Set OPENAI_API_KEY or ANTHROPIC_API_KEY"}

    # Resolve proxy: parameter > env var > none
    proxy_url = proxy or os.environ.get("US_PROXY", "")

    # Configurable headed mode (BROWSER_HEADLESS=false for Kevin's MacBook)
    headless_env = os.environ.get("BROWSER_HEADLESS", "").lower()
    if headless_env in ("false", "0", "no"):
        headless = False

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

    # Persistent browser profiles — reuse cookies per portal domain
    domain = urlparse(portal_url).netloc
    profiles_dir = os.path.expanduser("~/.captcha_profiles")
    state_file = os.path.join(profiles_dir, f"{domain}.json")

    try:
        with sync_playwright() as p:
            launch_args = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = {"server": proxy_url}
                logger.info("Using US proxy: %s", proxy_url.split('@')[-1] if '@' in proxy_url else 'configured')

            browser = p.chromium.launch(**launch_args)

            # Load persistent profile if available
            context_args = {
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "viewport": {"width": 1280, "height": 900},
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
            if os.path.exists(state_file):
                try:
                    context_args["storage_state"] = state_file
                    logger.info("Loaded browser profile for %s", domain)
                except Exception:
                    pass

            context = browser.new_context(**context_args)

            # Inject stealth script to avoid bot detection
            context.add_init_script(_get_stealth_script())

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
                # JS-based CAPTCHA detection (catches invisible CAPTCHAs Claude can't see)
                js_captcha = _detect_captcha_js(page)
                if js_captcha:
                    captcha_action = {
                        "action": "captcha_detected",
                        "captcha_type": js_captcha["type"],
                        "sitekey": js_captcha.get("sitekey", ""),
                        "description": f"Auto-detected {js_captcha['type']} via JS",
                    }
                    steps_log.append(f"Step {step}: JS detected {js_captcha['type']} CAPTCHA")
                    result_msg = _execute_action(page, captcha_action)
                    steps_log.append(f"  → {result_msg}")
                    time.sleep(2)
                    continue

                # Take screenshot
                try:
                    screenshot_b64 = _take_screenshot(page)
                except Exception:
                    steps_log.append(f"Step {step}: Screenshot failed")
                    break

                # Ask vision model what to do
                try:
                    action = _send_to_vision(client, screenshot_b64, task_context, history, vision_provider)
                except Exception as e:
                    steps_log.append(f"Step {step}: Vision error: {str(e)[:100]}")
                    break

                steps_log.append(f"Step {step}: {action.get('action', '?')} — {action.get('description', '')}")

                # Check if done
                if action.get("action") == "done":
                    # Save browser profile for future visits
                    _save_browser_profile(context, profiles_dir, state_file)
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
                    "user": "[Screenshot of current page state]",
                    "assistant": json.dumps(action),
                })

                time.sleep(STEP_TIMEOUT)

            # Ran out of steps — still save profile
            _save_browser_profile(context, profiles_dir, state_file)
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


def _save_browser_profile(context, profiles_dir: str, state_file: str) -> None:
    """Save browser cookies/storage for reuse on future visits."""
    try:
        os.makedirs(profiles_dir, exist_ok=True)
        context.storage_state(path=state_file)
        logger.info("Saved browser profile to %s", state_file)
    except Exception as e:
        logger.warning("Could not save browser profile: %s", e)
