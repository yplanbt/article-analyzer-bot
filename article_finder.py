"""Article Finder — searches Google News for crime articles using SerpAPI."""

import re
import time
from datetime import datetime, timedelta
from serpapi import GoogleSearch


# Common charge categories for building search queries
CHARGE_CATEGORIES = {
    "Robbery": ["robbery", "armed robbery", "bank robbery"],
    "Assault": ["assault", "aggravated assault", "battery"],
    "Child Abuse": ["child abuse", "child neglect", "child endangerment"],
    "DUI / DWI": ["DUI", "DWI", "drunk driving", "impaired driving"],
    "Drug Offenses": ["drug trafficking", "drug possession", "narcotics"],
    "Domestic Violence": ["domestic violence", "domestic assault"],
    "Murder / Homicide": ["murder", "homicide", "manslaughter"],
    "Kidnapping": ["kidnapping", "abduction"],
    "Sexual Assault": ["sexual assault", "rape", "sex crime"],
    "Theft / Burglary": ["theft", "burglary", "shoplifting", "larceny"],
    "Fraud": ["fraud", "embezzlement", "identity theft"],
    "Weapons": ["weapons charge", "illegal firearm", "gun charge"],
    "Police Misconduct": ["police misconduct", "excessive force", "officer charged"],
    "Fleeing / Eluding": ["fleeing", "eluding", "police chase", "pursuit"],
    "Arson": ["arson", "fire"],
    "Hit and Run": ["hit and run", "leaving the scene"],
}


def build_search_queries(
    state: str,
    city: str = "",
    county: str = "",
    police_dept: str = "",
    charges: list[str] = None,
    gender: str = "Any",
    days_back: int = 30,
) -> list[str]:
    """Build a list of Google News search queries from the user's parameters."""
    queries = []

    # Build location part
    location_parts = []
    if city:
        location_parts.append(f'"{city}"')
    if county:
        location_parts.append(f'"{county}"')
    if police_dept:
        location_parts.append(f'"{police_dept}"')
    if state and not city and not county and not police_dept:
        location_parts.append(f'"{state}"')

    location_str = " OR ".join(location_parts) if location_parts else f'"{state}"'

    # Gender keyword
    gender_term = ""
    if gender == "Male":
        gender_term = "man"
    elif gender == "Female":
        gender_term = "woman"

    # Build charge-based queries
    if charges:
        for charge in charges:
            charge_terms = CHARGE_CATEGORIES.get(charge, [charge.lower()])
            charge_str = " OR ".join(f'"{t}"' for t in charge_terms)
            q = f'"arrested" ({charge_str}) ({location_str})'
            if gender_term:
                q = f'{gender_term} {q}'
            queries.append(q)
    else:
        # Generic arrest query
        q = f'"arrested" "charged" ({location_str})'
        if gender_term:
            q = f'{gender_term} {q}'
        queries.append(q)

    return queries


def _parse_serp_date(date_str: str) -> datetime | None:
    """Parse SerpAPI date string to datetime."""
    if not date_str:
        return None
    try:
        # Format: "03/06/2026, 08:00 AM, +0000 UTC"
        clean = re.sub(r',\s*[+-]\d{4}\s*UTC', '', date_str).strip()
        return datetime.strptime(clean, "%m/%d/%Y, %I:%M %p")
    except ValueError:
        pass
    # Try relative dates like "2 days ago", "1 hour ago"
    match = re.search(r'(\d+)\s*(hour|day|week|month)s?\s*ago', date_str, re.IGNORECASE)
    if match:
        num = int(match.group(1))
        unit = match.group(2).lower()
        if unit == "hour":
            return datetime.now() - timedelta(hours=num)
        elif unit == "day":
            return datetime.now() - timedelta(days=num)
        elif unit == "week":
            return datetime.now() - timedelta(weeks=num)
        elif unit == "month":
            return datetime.now() - timedelta(days=num * 30)
    return None


def _extract_articles_from_results(news_results: list, seen_urls: set) -> list[dict]:
    """Extract and deduplicate articles from SerpAPI results."""
    articles = []
    for item in news_results:
        link = item.get("link", "")
        if not link or link in seen_urls:
            continue

        # Skip non-article links (video-only, social media, etc.)
        skip_domains = ["youtube.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com"]
        if any(d in link.lower() for d in skip_domains):
            continue

        seen_urls.add(link)
        pub_date = _parse_serp_date(item.get("date", ""))

        articles.append({
            "title": item.get("title", ""),
            "url": link,
            "source": item.get("source", {}).get("name", "") if isinstance(item.get("source"), dict) else str(item.get("source", "")),
            "snippet": item.get("snippet", ""),
            "date": pub_date,
            "date_str": item.get("date", ""),
        })
    return articles


def search_articles(
    api_key: str,
    state: str,
    city: str = "",
    county: str = "",
    police_dept: str = "",
    charges: list[str] = None,
    gender: str = "Any",
    days_back: int = 30,
    max_results: int = 150,
    progress_callback=None,
) -> list[dict]:
    """Search Google News for crime articles matching the given criteria.

    Returns a list of article dicts with: title, url, source, snippet, date.
    """
    queries = build_search_queries(
        state=state,
        city=city,
        county=county,
        police_dept=police_dept,
        charges=charges,
        gender=gender,
        days_back=days_back,
    )

    # Calculate date range for tbs parameter
    when_param = None
    if days_back <= 7:
        when_param = "qdr:w"
    elif days_back <= 30:
        when_param = "qdr:m"
    elif days_back <= 90:
        when_param = "qdr:m3"
    elif days_back <= 365:
        when_param = "qdr:y"

    all_articles = []
    seen_urls = set()
    total_queries = len(queries)

    for qi, query in enumerate(queries):
        if len(all_articles) >= max_results:
            break

        if progress_callback:
            progress_callback(qi, total_queries, query, len(all_articles))

        params = {
            "engine": "google_news",
            "q": query,
            "gl": "us",
            "hl": "en",
            "api_key": api_key,
        }
        if when_param:
            params["tbs"] = when_param

        try:
            search = GoogleSearch(params)
            results = search.get_dict()
            news = results.get("news_results", [])
            new_articles = _extract_articles_from_results(news, seen_urls)
            all_articles.extend(new_articles)
        except Exception as e:
            if progress_callback:
                progress_callback(qi, total_queries, f"Error: {e}", len(all_articles))
            continue

        # Rate limit between queries
        if qi < total_queries - 1:
            time.sleep(1)

    # Sort by date (newest first)
    all_articles.sort(key=lambda a: a.get("date") or datetime.min, reverse=True)

    # Trim to max
    return all_articles[:max_results]
