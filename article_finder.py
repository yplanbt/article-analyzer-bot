"""Article Finder — searches Google News for crime articles using SerpAPI."""

import re
import time
from datetime import datetime, timedelta
from serpapi import GoogleSearch


# Common charge categories for building search queries
CHARGE_CATEGORIES = {
    "Robbery": ["robbery", "armed robbery", "bank robbery"],
    "Assault": ["assault", "aggravated assault", "battery"],
    "Child Abuse": ["child abuse", "child neglect", "child endangerment", "child cruelty"],
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


def _expand_custom_charges(charge_text: str) -> list[str]:
    """Take user-typed custom charges and return a list of search terms.

    The user might type 'child abuse' and we expand it to include related
    terms like 'child endangerment', 'child cruelty', etc.
    """
    RELATED_TERMS = {
        "child abuse": ["child abuse", "child neglect", "child endangerment", "child cruelty", "child maltreatment", "injury to a child", "abuse of a minor"],
        "child endangerment": ["child endangerment", "child abuse", "child neglect", "reckless endangerment of a child", "injury to a child"],
        "domestic violence": ["domestic violence", "domestic assault", "domestic battery", "intimate partner violence", "family violence"],
        "assault": ["assault", "aggravated assault", "battery", "assault and battery", "felonious assault"],
        "robbery": ["robbery", "armed robbery", "aggravated robbery", "bank robbery", "strong-arm robbery"],
        "murder": ["murder", "homicide", "manslaughter", "killing", "second-degree murder", "first-degree murder"],
        "homicide": ["homicide", "murder", "manslaughter", "killing"],
        "kidnapping": ["kidnapping", "abduction", "unlawful restraint", "false imprisonment"],
        "dui": ["DUI", "DWI", "drunk driving", "impaired driving", "operating under influence", "OUI"],
        "sexual assault": ["sexual assault", "rape", "sexual battery", "criminal sexual conduct", "indecent assault"],
        "theft": ["theft", "larceny", "stealing", "shoplifting", "grand theft", "petty theft"],
        "burglary": ["burglary", "breaking and entering", "home invasion", "B&E"],
        "drug": ["drug trafficking", "drug possession", "narcotics", "controlled substance", "drug distribution"],
        "fraud": ["fraud", "embezzlement", "forgery", "identity theft", "wire fraud"],
        "arson": ["arson", "fire", "incendiary"],
        "fleeing": ["fleeing", "eluding", "evading", "police chase", "pursuit", "resisting"],
        "weapons": ["weapons charge", "illegal firearm", "gun charge", "felon in possession", "unlawful weapon"],
        "carjacking": ["carjacking", "car theft", "vehicle theft", "stolen vehicle", "grand theft auto"],
        "manslaughter": ["manslaughter", "vehicular manslaughter", "involuntary manslaughter", "negligent homicide"],
        "hit and run": ["hit and run", "leaving the scene", "leaving scene of accident", "fatal hit and run"],
        "police misconduct": ["police misconduct", "excessive force", "officer charged", "officer arrested", "police brutality"],
        "resisting": ["resisting arrest", "obstruction", "resisting", "fleeing"],
        "shoplifting": ["shoplifting", "retail theft", "petty theft", "larceny"],
        "stalking": ["stalking", "harassment", "criminal harassment", "cyberstalking"],
        "trespassing": ["trespassing", "criminal trespass", "unlawful entry"],
    }

    charge_lower = charge_text.strip().lower()
    # Check for direct match or partial match
    for key, terms in RELATED_TERMS.items():
        if charge_lower in key or key in charge_lower:
            return terms

    # No match found — return the charge as-is plus "arrested" context
    return [charge_text.strip()]


def build_search_queries(
    state: str,
    city: str = "",
    county: str = "",
    police_dept: str = "",
    charges: list[str] = None,
    custom_charges: list[str] = None,
    gender: str = "Any",
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

    location_str = " OR ".join(location_parts) if location_parts else ""

    # Gender keyword
    gender_term = ""
    if gender == "Male":
        gender_term = "man"
    elif gender == "Female":
        gender_term = "woman"

    # Collect all charge terms
    all_charge_groups = []

    if charges:
        for charge in charges:
            charge_terms = CHARGE_CATEGORIES.get(charge, [charge.lower()])
            all_charge_groups.append(charge_terms)

    if custom_charges:
        for custom in custom_charges:
            expanded = _expand_custom_charges(custom)
            all_charge_groups.append(expanded)

    if all_charge_groups:
        for terms in all_charge_groups:
            charge_str = " OR ".join(f'"{t}"' for t in terms)
            if location_str:
                q = f'"arrested" ({charge_str}) ({location_str})'
            else:
                q = f'"arrested" ({charge_str})'
            if gender_term:
                q = f'{gender_term} {q}'
            queries.append(q)
    else:
        if location_str:
            q = f'"arrested" "charged" ({location_str})'
        else:
            q = f'"arrested" "charged"'
        if gender_term:
            q = f'{gender_term} {q}'
        queries.append(q)

    return queries


def _parse_serp_date(date_str: str) -> datetime | None:
    """Parse SerpAPI date string to datetime."""
    if not date_str:
        return None
    try:
        clean = re.sub(r',\s*[+-]\d{4}\s*UTC', '', date_str).strip()
        return datetime.strptime(clean, "%m/%d/%Y, %I:%M %p")
    except ValueError:
        pass
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


def _extract_date_from_url(url: str) -> datetime | None:
    """Try to extract a date from the URL path (many news sites embed dates)."""
    # Match patterns like /2026/03/05/ or /2026-03-05/
    match = re.search(r'/(\d{4})/(\d{1,2})/(\d{1,2})/', url)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    match = re.search(r'/(\d{4})-(\d{1,2})-(\d{1,2})/', url)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


def _extract_articles_from_results(
    news_results: list,
    seen_urls: set,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[dict]:
    """Extract and deduplicate articles from SerpAPI results, filtering by date range."""
    articles = []
    for item in news_results:
        link = item.get("link", "")
        if not link or link in seen_urls:
            continue

        skip_domains = ["youtube.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com"]
        if any(d in link.lower() for d in skip_domains):
            continue

        seen_urls.add(link)
        pub_date = _parse_serp_date(item.get("date", ""))

        # Also try to extract date from URL as fallback/cross-check
        url_date = _extract_date_from_url(link)

        # Use the best available date — prefer SerpAPI date, fallback to URL date
        best_date = pub_date or url_date

        # Strict date filtering — skip articles outside the date range
        if best_date and date_from and best_date.date() < date_from.date():
            continue
        if best_date and date_to and best_date.date() > date_to.date():
            continue

        # If we have a URL date that contradicts, use URL date (more reliable for year)
        if url_date and date_from:
            if url_date.year < date_from.year:
                continue

        articles.append({
            "title": item.get("title", ""),
            "url": link,
            "source": item.get("source", {}).get("name", "") if isinstance(item.get("source"), dict) else str(item.get("source", "")),
            "snippet": item.get("snippet", ""),
            "date": best_date,
            "date_str": item.get("date", ""),
        })
    return articles


def _make_tbs_param(date_from: datetime, date_to: datetime) -> str:
    """Build Google's tbs parameter for exact date range filtering."""
    return f"cdr:1,cd_min:{date_from.strftime('%m/%d/%Y')},cd_max:{date_to.strftime('%m/%d/%Y')}"


def search_articles(
    api_key: str,
    state: str,
    city: str = "",
    county: str = "",
    police_dept: str = "",
    charges: list[str] = None,
    custom_charges: list[str] = None,
    gender: str = "Any",
    date_from: datetime = None,
    date_to: datetime = None,
    max_results: int = 150,
    progress_callback=None,
) -> list[dict]:
    """Search Google News for crime articles matching the given criteria."""
    queries = build_search_queries(
        state=state,
        city=city,
        county=county,
        police_dept=police_dept,
        charges=charges,
        custom_charges=custom_charges,
        gender=gender,
    )

    # Build date range tbs parameter
    tbs_param = None
    if date_from and date_to:
        tbs_param = _make_tbs_param(date_from, date_to)

    all_articles = []
    seen_urls = set()
    total_queries = len(queries)

    for qi, query in enumerate(queries):
        if len(all_articles) >= max_results:
            break

        if progress_callback:
            progress_callback(qi, total_queries, query, len(all_articles))

        # Search multiple pages to get more results
        for start in range(0, min(100, max_results - len(all_articles)), 10):
            params = {
                "engine": "google_news",
                "q": query,
                "gl": "us",
                "hl": "en",
                "api_key": api_key,
            }
            if tbs_param:
                params["tbs"] = tbs_param
            if start > 0:
                params["start"] = start

            try:
                search = GoogleSearch(params)
                results = search.get_dict()
                news = results.get("news_results", [])
                if not news:
                    break
                new_articles = _extract_articles_from_results(news, seen_urls, date_from, date_to)
                all_articles.extend(new_articles)
                if len(all_articles) >= max_results:
                    break
            except Exception as e:
                if progress_callback:
                    progress_callback(qi, total_queries, f"Error: {e}", len(all_articles))
                break

            time.sleep(0.5)

        if qi < total_queries - 1:
            time.sleep(1)

    # Sort by date (newest first)
    all_articles.sort(key=lambda a: a.get("date") or datetime.min, reverse=True)

    return all_articles[:max_results]
