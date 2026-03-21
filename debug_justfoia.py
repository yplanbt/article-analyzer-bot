#!/usr/bin/env python3
"""Self-contained JustFOIA debug script.
Run on Kevin's machine to diagnose portal submission issues.

Usage: python3 debug_justfoia.py
Output: debug_justfoia_report.txt + screenshots in ./debug/
"""
import json
import os
import time
import traceback

PORTALS = [
    ("Rochester", "https://rochesterny.justfoia.com/publicportal"),
    ("Camas", "https://cityofcamas.justfoia.com/publicportal/home/newrequest"),
    ("Escambia", "https://escambiaso.justfoia.com/publicportal"),
]

# Test data
TEST_NAME = "Sarah Jaden"
TEST_EMAIL = "sjaden1993@gmail.com"
TEST_SUBJECT = "Public Records Request - Body Camera Footage"
TEST_BODY = (
    "Pursuant to the Freedom of Information Act, I am requesting copies of "
    "any and all body-worn camera footage related to the arrest on the date "
    "referenced in the subject. Please provide records in electronic format."
)

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_justfoia_report.txt")

os.makedirs(DEBUG_DIR, exist_ok=True)

report_lines = []

def log(msg):
    print(msg)
    report_lines.append(msg)

def dump_page(page, label):
    """Capture full page state."""
    info = {
        "url": page.url,
        "title": page.title(),
    }
    # Get ALL form elements
    elements = []
    for tag in ["input", "textarea", "select", "button"]:
        locs = page.locator(tag).all()
        for loc in locs:
            try:
                el_info = {
                    "tag": tag,
                    "type": loc.get_attribute("type") or "",
                    "id": loc.get_attribute("id") or "",
                    "name": loc.get_attribute("name") or "",
                    "placeholder": loc.get_attribute("placeholder") or "",
                    "value": loc.get_attribute("value") or "",
                    "visible": loc.is_visible(),
                    "text": loc.inner_text()[:100] if tag in ("button", "select") else "",
                }
                elements.append(el_info)
            except Exception:
                pass
    info["elements"] = elements

    # Get all links
    links = []
    for a in page.locator("a").all():
        try:
            href = a.get_attribute("href") or ""
            text = a.inner_text().strip()[:80]
            if text or href:
                links.append({"text": text, "href": href})
        except Exception:
            pass
    info["links"] = links

    # Get labels
    labels = []
    for lbl in page.locator("label").all():
        try:
            labels.append({
                "text": lbl.inner_text().strip()[:80],
                "for": lbl.get_attribute("for") or "",
            })
        except Exception:
            pass
    info["labels"] = labels

    # Save JSON
    json_path = os.path.join(DEBUG_DIR, f"{label}.json")
    with open(json_path, "w") as f:
        json.dump(info, f, indent=2)

    # Save screenshot
    png_path = os.path.join(DEBUG_DIR, f"{label}.png")
    try:
        page.screenshot(path=png_path, full_page=True)
    except Exception:
        pass

    return info


def test_portal(browser, portal_name, portal_url):
    log(f"\n{'='*60}")
    log(f"TESTING: {portal_name}")
    log(f"URL: {portal_url}")
    log(f"{'='*60}")

    # Get proxy from env
    proxy_url = os.environ.get("US_PROXY", "")
    proxy_config = None
    if proxy_url:
        log(f"Using proxy: {proxy_url[:30]}...")
        try:
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_config["username"] = parsed.username
                proxy_config["password"] = parsed.password or ""
        except Exception as e:
            log(f"Proxy parse error: {e}")
    else:
        log("No proxy configured")

    ctx_opts = {"ignore_https_errors": True}
    if proxy_config:
        ctx_opts["proxy"] = proxy_config

    context = browser.new_context(**ctx_opts)
    page = context.new_page()

    base_url = portal_url.split("/publicportal")[0] if "/publicportal" in portal_url else portal_url.rstrip("/")

    try:
        # ---- TEST 1: Public portal homepage ----
        log(f"\n--- Test 1: Public Portal Homepage ---")
        homepage_url = base_url + "/publicportal"
        page.goto(homepage_url, wait_until="networkidle", timeout=30000)
        time.sleep(3)
        info = dump_page(page, f"{portal_name}_01_homepage")
        log(f"Title: {info['title']}")
        log(f"URL: {info['url']}")
        log(f"Form elements: {len(info['elements'])}")
        log(f"Links: {len(info['links'])}")
        visible_elements = [e for e in info["elements"] if e["visible"]]
        log(f"Visible form elements: {len(visible_elements)}")
        for e in visible_elements:
            log(f"  {e['tag']} type={e['type']} id={e['id']} name={e['name']} placeholder={e['placeholder']}")
        # Show key links
        for lnk in info["links"]:
            if any(kw in lnk["text"].lower() for kw in ["request", "submit", "new", "register", "login", "sign", "anonymous"]):
                log(f"  LINK: '{lnk['text']}' -> {lnk['href']}")

        # ---- TEST 2: New Request page ----
        log(f"\n--- Test 2: New Request Page ---")
        new_req_url = base_url + "/publicportal/home/newrequest"
        page.goto(new_req_url, wait_until="networkidle", timeout=30000)
        time.sleep(3)
        info = dump_page(page, f"{portal_name}_02_newrequest")
        log(f"Title: {info['title']}")
        log(f"URL: {info['url']}")
        visible_elements = [e for e in info["elements"] if e["visible"]]
        log(f"Visible form elements: {len(visible_elements)}")
        for e in visible_elements:
            log(f"  {e['tag']} type={e['type']} id={e['id']} name={e['name']} placeholder={e['placeholder']}")
        for lbl in info.get("labels", []):
            log(f"  LABEL: '{lbl['text']}' for={lbl['for']}")
        for lnk in info["links"]:
            if any(kw in lnk["text"].lower() for kw in ["request", "submit", "new", "register", "login", "sign", "anonymous"]):
                log(f"  LINK: '{lnk['text']}' -> {lnk['href']}")

        # ---- TEST 3: Registration page (multiple URLs) ----
        log(f"\n--- Test 3: Registration Pages ---")
        reg_urls = [
            base_url + "/account/register",
            base_url + "/publicportal/home/register",
            base_url + "/publicportal/account/register",
        ]
        for i, reg_url in enumerate(reg_urls):
            log(f"\n  Trying: {reg_url}")
            try:
                page.goto(reg_url, wait_until="networkidle", timeout=15000)
                time.sleep(2)
                info = dump_page(page, f"{portal_name}_03_register_{i}")
                log(f"  Title: {info['title']}")
                log(f"  URL: {info['url']}")
                visible_elements = [e for e in info["elements"] if e["visible"]]
                log(f"  Visible form elements: {len(visible_elements)}")
                for e in visible_elements:
                    log(f"    {e['tag']} type={e['type']} id={e['id']} name={e['name']} placeholder={e['placeholder']}")
                if "restricted" not in info["title"].lower():
                    log(f"  ** THIS REGISTRATION URL WORKS **")
            except Exception as e:
                log(f"  Error: {e}")

        # ---- TEST 4: Try without proxy ----
        if proxy_config:
            log(f"\n--- Test 4: Without Proxy ---")
            ctx2 = browser.new_context(ignore_https_errors=True)
            page2 = ctx2.new_page()
            try:
                page2.goto(new_req_url, wait_until="networkidle", timeout=30000)
                time.sleep(3)
                info = dump_page(page2, f"{portal_name}_04_noproxy")
                log(f"Title (no proxy): {info['title']}")
                log(f"URL (no proxy): {info['url']}")
                visible_elements = [e for e in info["elements"] if e["visible"]]
                log(f"Visible form elements (no proxy): {len(visible_elements)}")
                for e in visible_elements:
                    log(f"  {e['tag']} type={e['type']} id={e['id']} name={e['name']} placeholder={e['placeholder']}")
            except Exception as e:
                log(f"No-proxy error: {e}")
            finally:
                ctx2.close()

    except Exception as e:
        log(f"ERROR: {e}")
        log(traceback.format_exc())
    finally:
        context.close()


def main():
    log("JustFOIA Debug Script")
    log(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Proxy: {os.environ.get('US_PROXY', 'NOT SET')[:40]}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("ERROR: playwright not installed")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for name, url in PORTALS:
            try:
                test_portal(browser, name, url)
            except Exception as e:
                log(f"\nFATAL ERROR testing {name}: {e}")
                log(traceback.format_exc())

        browser.close()

    # Write report
    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(report_lines))

    log(f"\n{'='*60}")
    log(f"REPORT SAVED: {REPORT_PATH}")
    log(f"SCREENSHOTS: {DEBUG_DIR}/")
    log(f"{'='*60}")


if __name__ == "__main__":
    main()
