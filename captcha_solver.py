"""CAPTCHA solving via 2captcha API.

Supports reCAPTCHA v2, hCaptcha, and image CAPTCHAs.
Cost: ~$2-3 per 1000 solves (~$0.003 each).

Setup:
  1. Sign up at https://2captcha.com
  2. Add your API key to .env as CAPTCHA_API_KEY
"""

import time
import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://2captcha.com"
POLL_INTERVAL = 5  # seconds between status checks
MAX_WAIT = 120  # max seconds to wait for a solution


def solve_recaptcha_v2(api_key: str, site_key: str, page_url: str) -> dict:
    """Solve a reCAPTCHA v2 challenge.

    Args:
        api_key: 2captcha API key
        site_key: The reCAPTCHA site key (from data-sitekey attribute)
        page_url: The URL of the page with the CAPTCHA

    Returns:
        {"success": bool, "solution": str, "error": str}
    """
    logger.info("Solving reCAPTCHA v2 for %s", page_url)
    try:
        # Submit the CAPTCHA
        resp = requests.post(f"{BASE_URL}/in.php", data={
            "key": api_key,
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }, timeout=30)
        data = resp.json()

        if data.get("status") != 1:
            return {"success": False, "solution": "", "error": data.get("request", "Submit failed")}

        task_id = data["request"]
        logger.info("reCAPTCHA task submitted: %s", task_id)

        # Poll for result
        return _poll_result(api_key, task_id)

    except Exception as e:
        logger.error("reCAPTCHA solve failed: %s", e)
        return {"success": False, "solution": "", "error": str(e)}


def solve_hcaptcha(api_key: str, site_key: str, page_url: str) -> dict:
    """Solve an hCaptcha challenge.

    Args:
        api_key: 2captcha API key
        site_key: The hCaptcha site key (from data-sitekey attribute)
        page_url: The URL of the page with the CAPTCHA

    Returns:
        {"success": bool, "solution": str, "error": str}
    """
    logger.info("Solving hCaptcha for %s", page_url)
    try:
        resp = requests.post(f"{BASE_URL}/in.php", data={
            "key": api_key,
            "method": "hcaptcha",
            "sitekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }, timeout=30)
        data = resp.json()

        if data.get("status") != 1:
            return {"success": False, "solution": "", "error": data.get("request", "Submit failed")}

        task_id = data["request"]
        logger.info("hCaptcha task submitted: %s", task_id)

        return _poll_result(api_key, task_id)

    except Exception as e:
        logger.error("hCaptcha solve failed: %s", e)
        return {"success": False, "solution": "", "error": str(e)}


def solve_image_captcha(api_key: str, image_base64: str) -> dict:
    """Solve an image-based CAPTCHA (distorted text, etc).

    Args:
        api_key: 2captcha API key
        image_base64: Base64-encoded image data

    Returns:
        {"success": bool, "solution": str, "error": str}
    """
    logger.info("Solving image CAPTCHA")
    try:
        resp = requests.post(f"{BASE_URL}/in.php", data={
            "key": api_key,
            "method": "base64",
            "body": image_base64,
            "json": 1,
        }, timeout=30)
        data = resp.json()

        if data.get("status") != 1:
            return {"success": False, "solution": "", "error": data.get("request", "Submit failed")}

        task_id = data["request"]
        logger.info("Image CAPTCHA task submitted: %s", task_id)

        return _poll_result(api_key, task_id)

    except Exception as e:
        logger.error("Image CAPTCHA solve failed: %s", e)
        return {"success": False, "solution": "", "error": str(e)}


def _poll_result(api_key: str, task_id: str) -> dict:
    """Poll 2captcha for a task result."""
    elapsed = 0
    while elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            resp = requests.get(f"{BASE_URL}/res.php", params={
                "key": api_key,
                "action": "get",
                "id": task_id,
                "json": 1,
            }, timeout=15)
            data = resp.json()

            if data.get("status") == 1:
                logger.info("CAPTCHA solved successfully (took %ds)", elapsed)
                return {"success": True, "solution": data["request"], "error": ""}

            if data.get("request") != "CAPCHA_NOT_READY":
                logger.warning("CAPTCHA solve error: %s", data.get("request"))
                return {"success": False, "solution": "", "error": data.get("request", "Unknown error")}

        except Exception as e:
            logger.warning("Poll error: %s", e)

    logger.warning("CAPTCHA solve timed out after %ds", MAX_WAIT)
    return {"success": False, "solution": "", "error": "Timed out waiting for solution"}


def get_balance(api_key: str) -> float:
    """Check 2captcha account balance."""
    try:
        resp = requests.get(f"{BASE_URL}/res.php", params={
            "key": api_key,
            "action": "getbalance",
            "json": 1,
        }, timeout=10)
        data = resp.json()
        return float(data.get("request", 0))
    except Exception:
        return -1.0
