"""Scrape article text and metadata from a URL."""

import re
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser


def _get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",  # NO brotli — requests can't decode it
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _extract_date_from_url(url: str):
    """Try to extract a publish date from the URL path (e.g. /2026/03/05/)."""
    match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
    if match:
        try:
            from datetime import datetime
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


def _extract_publish_date(soup, url: str):
    """Extract publish date from meta tags, <time> elements, JSON-LD, or URL."""
    # Try meta tags
    date_metas = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"property": "og:article:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publish_date"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "sailthru.date"}),
        ("meta", {"name": "dc.date"}),
        ("meta", {"name": "DC.date.issued"}),
        ("meta", {"itemprop": "datePublished"}),
    ]
    for tag_name, attrs in date_metas:
        tag = soup.find(tag_name, attrs)
        if tag:
            date_str = tag.get("content", "").strip()
            if date_str:
                try:
                    return dateutil_parser.parse(date_str, fuzzy=True)
                except (ValueError, OverflowError):
                    continue

    # Try <time> elements
    for time_tag in soup.find_all("time"):
        date_str = time_tag.get("datetime") or time_tag.get_text(strip=True)
        if date_str:
            try:
                return dateutil_parser.parse(date_str, fuzzy=True)
            except (ValueError, OverflowError):
                continue

    # Try JSON-LD
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            import json
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            date_str = data.get("datePublished") or data.get("dateCreated")
            if date_str:
                return dateutil_parser.parse(date_str, fuzzy=True)
        except (json.JSONDecodeError, ValueError, TypeError, OverflowError):
            continue

    # Fallback: extract from URL
    return _extract_date_from_url(url)


def scrape_article(url: str) -> dict:
    """Fetch an article URL and extract text + publish date."""
    result = {"title": "", "text": "", "publish_date": None, "url": url, "error": None}

    # Attempt 1: normal request
    resp = None
    for attempt in range(2):
        try:
            headers = _get_headers()
            if attempt == 1:
                headers["Referer"] = "https://www.google.com/"
            resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            break
        except requests.exceptions.HTTPError:
            if resp and resp.status_code == 403 and attempt == 0:
                continue  # retry with Referer
            result["error"] = f"HTTP {resp.status_code if resp else 'unknown'} error"
            return result
        except requests.exceptions.Timeout:
            result["error"] = "Request timed out"
            return result
        except Exception as e:
            result["error"] = f"Failed to fetch: {e}"
            return result

    if not resp:
        result["error"] = "Failed to fetch after retries"
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title — try multiple sources
    og_title = soup.find("meta", {"property": "og:title"})
    if og_title and og_title.get("content"):
        result["title"] = og_title["content"].strip()
    else:
        title_tag = soup.find("title")
        if title_tag:
            result["title"] = title_tag.get_text(strip=True)

    # Publish date — comprehensive extraction
    result["publish_date"] = _extract_publish_date(soup, url)

    # Extract main text
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
        tag.decompose()

    article_tag = soup.find("article")
    container = article_tag if article_tag else soup.find("body")
    if container:
        paragraphs = container.find_all("p")
        result["text"] = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    if not result["text"]:
        # Last resort: all visible text
        result["text"] = soup.get_text(separator="\n", strip=True)[:5000]

    # Truncate to keep within reasonable token limits
    if len(result["text"]) > 8000:
        result["text"] = result["text"][:8000]

    return result
