"""Article Finder — searches Google News for crime articles using SerpAPI."""

import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from dateutil import parser as dateutil_parser
from serpapi import GoogleSearch

logger = logging.getLogger(__name__)

# Track API usage across calls within a session
_api_call_count = 0
_API_WARN_THRESHOLD = 80  # warn when this many calls have been made


def _reset_api_counter() -> None:
    """Reset the session API call counter (useful for testing)."""
    global _api_call_count
    _api_call_count = 0


def _increment_api_counter() -> int:
    """Increment and return the current API call count."""
    global _api_call_count
    _api_call_count += 1
    if _api_call_count == _API_WARN_THRESHOLD:
        logger.warning(
            "SerpAPI: %d calls made this session — approaching typical free-tier limits.",
            _api_call_count,
        )
    return _api_call_count


def _serpapi_search_with_retry(params: dict, max_retries: int = 3) -> dict:
    """Run a SerpAPI search with exponential backoff on transient failures.

    Returns the result dict on success, raises on permanent failure.
    """
    delay = 1.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            _increment_api_counter()
            search = GoogleSearch(params)
            result = search.get_dict()
            # SerpAPI embeds errors in the response body
            if "error" in result:
                err_msg = result["error"]
                # Rate-limit / quota errors should not be retried endlessly
                if "rate" in err_msg.lower() or "quota" in err_msg.lower():
                    logger.error("SerpAPI quota/rate error: %s", err_msg)
                    raise RuntimeError(f"SerpAPI quota/rate error: {err_msg}")
                logger.warning(
                    "SerpAPI error on attempt %d/%d: %s", attempt + 1, max_retries, err_msg
                )
                last_exc = RuntimeError(err_msg)
            else:
                return result
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "SerpAPI request failed on attempt %d/%d: %s",
                attempt + 1,
                max_retries,
                exc,
            )

        if attempt < max_retries - 1:
            logger.info("Retrying after %.1fs …", delay)
            time.sleep(delay)
            delay *= 2  # exponential backoff

    raise RuntimeError(f"SerpAPI failed after {max_retries} attempts") from last_exc


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
    """Take user-typed custom charges and return a list of search terms."""
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
    for key, terms in RELATED_TERMS.items():
        if charge_lower in key or key in charge_lower:
            return terms
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
    """Build a diverse list of search queries to maximize article coverage."""
    queries = []

    # Build focused location string — use AND logic, not OR
    # This produces more relevant results
    location_parts = []
    if city:
        location_parts.append(f'"{city}"')
    if state:
        location_parts.append(f'"{state}"')
    location_str = " ".join(location_parts) if location_parts else ""

    # Also build broader location variants
    location_variants = [location_str] if location_str else [""]
    if county:
        location_variants.append(f'"{county}" "{state}"' if state else f'"{county}"')
    if police_dept:
        location_variants.append(f'"{police_dept}"')
    # State-only variant for broader search
    if state and city:
        location_variants.append(f'"{state}"')

    # Gender keyword
    gender_term = ""
    if gender == "Male":
        gender_term = "man"
    elif gender == "Female":
        gender_term = "woman"

    # Action verbs — the key to finding more articles
    action_verbs = ['"arrested"', '"charged"', '"accused"', '"indicted"', '"booked"']

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
            # Top 3 terms to keep query focused
            top_terms = terms[:3]
            charge_str = " OR ".join(f'"{t}"' for t in top_terms)

            for loc in location_variants:
                for verb in action_verbs[:3]:  # arrested, charged, accused
                    q = f'{verb} ({charge_str})'
                    if loc:
                        q += f' {loc}'
                    if gender_term:
                        q = f'{gender_term} {q}'
                    queries.append(q)

            # Also add a "police" variant to find press releases
            if location_variants[0]:
                q = f'police ({charge_str}) {location_variants[0]}'
                queries.append(q)

            # Press release queries — government sites publish arrest PRs frequently
            if location_variants[0]:
                q = f'site:*.gov "press release" arrested ({charge_str}) {location_variants[0]}'
                queries.append(q)
                if police_dept:
                    q = f'site:*.gov "press release" arrested ({charge_str}) "{police_dept}"'
                    queries.append(q)

            # Police department press release queries
            if police_dept:
                q = f'"{police_dept}" "press release" arrested ({charge_str})'
                queries.append(q)
                q = f'"{police_dept}" arrested ({charge_str})'
                queries.append(q)

    else:
        # No charges specified — use broad crime terms
        broad_terms = [
            '"arrested" "charged"',
            '"arrested" "police"',
            '"suspect" "charged"',
            '"police" "arrest"',
            '"booked" "charges"',
        ]
        for bt in broad_terms:
            for loc in location_variants[:2]:  # Don't over-multiply
                q = bt
                if loc:
                    q += f' {loc}'
                if gender_term:
                    q = f'{gender_term} {q}'
                queries.append(q)

        # Broad press release queries even without specific charges
        if location_variants[0]:
            queries.append(
                f'site:*.gov "press release" arrested {location_variants[0]}'
            )
        if police_dept:
            queries.append(f'"{police_dept}" "press release" arrested')

    # ── Local news aggregator queries ──────────────────────────────────────
    # These sites syndicate local crime coverage that general queries miss.
    local_aggregators = [
        "patch.com",
        "crimeonline.com",
        "localnews8.com",
        "nbc*.com",
        "abc*.com",
    ]
    if location_variants[0] and all_charge_groups:
        for terms in all_charge_groups[:2]:  # limit to first 2 charge groups
            top_terms = terms[:2]
            charge_str = " OR ".join(f'"{t}"' for t in top_terms)
            for agg in local_aggregators[:3]:
                q = f'site:{agg} arrested ({charge_str}) {location_variants[0]}'
                queries.append(q)

    # Deduplicate while preserving order
    seen = set()
    unique_queries = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique_queries.append(q)

    return unique_queries


def _parse_serp_date(date_str: str) -> datetime | None:
    """Parse SerpAPI date string to datetime using dateutil for robust format handling.

    Handles:
    - Relative: "3 days ago", "2 hours ago", "a week ago"
    - Named relative: "today", "yesterday", "last night"
    - RFC-style: "Mon, 15 Mar 2026 10:30:00 +0000"
    - ISO / US / fuzzy: anything dateutil can parse
    """
    if not date_str:
        return None

    now = datetime.now()
    s = date_str.strip().lower()

    # ── Named relative anchors ──────────────────────────────────────────
    if re.search(r'\btoday\b', s):
        return now.replace(hour=12, minute=0, second=0, microsecond=0)
    if re.search(r'\byesterday\b', s):
        return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    if re.search(r'\blast\s+night\b', s):
        return (now - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    if re.search(r'\bthis\s+morning\b', s):
        return now.replace(hour=8, minute=0, second=0, microsecond=0)
    if re.search(r'\bthis\s+week\b', s):
        return now - timedelta(days=3)
    if re.search(r'\blast\s+week\b', s):
        return now - timedelta(weeks=1)

    # ── Numeric relative: "3 days ago", "a week ago", "an hour ago" ─────
    # Support "a/an" as 1
    match = re.search(
        r'\b(a|an|\d+)\s*(minute|hour|day|week|month)s?\s*ago\b', date_str, re.IGNORECASE
    )
    if match:
        raw_num = match.group(1).lower()
        num = 1 if raw_num in ("a", "an") else int(raw_num)
        unit = match.group(2).lower()
        deltas = {
            "minute": timedelta(minutes=num),
            "hour": timedelta(hours=num),
            "day": timedelta(days=num),
            "week": timedelta(weeks=num),
            "month": timedelta(days=num * 30),
        }
        return now - deltas.get(unit, timedelta())

    # ── Strip noise before handing to dateutil ──────────────────────────
    # Remove timezone offsets like "+0000" / "-0500 UTC" that confuse dateutil
    clean = re.sub(r',?\s*[+-]\d{4}(\s*UTC)?\s*$', '', date_str).strip()
    # Remove trailing UTC/GMT label without offset
    clean = re.sub(r'\s*(UTC|GMT)\s*$', '', clean, flags=re.IGNORECASE).strip()

    try:
        return dateutil_parser.parse(clean, fuzzy=True)
    except (ValueError, OverflowError, TypeError) as exc:
        logger.debug("_parse_serp_date could not parse %r: %s", date_str, exc)
        return None


def _extract_date_from_url(url: str) -> datetime | None:
    """Try to extract a date from the URL path."""
    for pattern in [
        r'/(\d{4})/(\d{1,2})/(\d{1,2})/',
        r'/(\d{4})-(\d{1,2})-(\d{1,2})/',
        r'/(\d{4})-(\d{1,2})-(\d{1,2})\b',
    ]:
        match = re.search(pattern, url)
        if match:
            try:
                return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    match = re.search(r'[/-](\d{4})(\d{2})(\d{2})[/-]', url)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    match = re.search(r'[?&]date=(\d{4})-(\d{1,2})-(\d{1,2})', url)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "referrer", "source", "_ga",
    "mc_cid", "mc_eid", "yclid", "dclid",
})


def _normalize_url(url: str) -> str:
    """Return a canonical form of *url* for deduplication.

    Strips:
    - Tracking query parameters
    - The ``www.`` prefix from the hostname
    - Trailing slashes from the path
    - URL fragments (#…)
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
        # Lowercase scheme + host; drop www prefix
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        # Strip tracking params from query string
        if parsed.query:
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            clean_qs = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
            new_query = urllib.parse.urlencode(clean_qs, doseq=True)
        else:
            new_query = ""
        # Normalize path — strip trailing slash (except bare root)
        path = parsed.path.rstrip("/") or "/"
        canonical = urllib.parse.urlunparse(
            (parsed.scheme.lower(), host, path, parsed.params, new_query, "")
        )
        return canonical
    except Exception:  # noqa: BLE001
        return url


def _title_key(title: str) -> str:
    """Reduce a title to a short fingerprint for near-duplicate detection.

    Lowercases, strips punctuation, collapses whitespace, and takes the
    first 60 characters — enough to catch reprints with minor wording diffs.
    """
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:60]


def _extract_articles_from_results(
    news_results: list,
    seen_urls: set,
    date_from: datetime | None,
    date_to: datetime | None,
    seen_title_keys: set | None = None,
) -> list[dict]:
    """Extract and deduplicate articles from SerpAPI results, filtering by date range.

    *seen_urls* is mutated in-place so callers share state across batches.
    *seen_title_keys* (optional) enables cross-batch title-similarity dedup;
    callers should pass the same set across all batches.
    """
    if seen_title_keys is None:
        seen_title_keys = set()

    articles = []
    for item in news_results:
        link = item.get("link", "")
        if not link:
            continue

        skip_domains = ["youtube.com", "twitter.com", "facebook.com", "instagram.com",
                        "tiktok.com", "reddit.com", "pinterest.com"]
        if any(d in link.lower() for d in skip_domains):
            continue

        # ── URL deduplication (normalized) ─────────────────────────────
        norm_url = _normalize_url(link)
        if norm_url in seen_urls:
            continue

        # ── Title-similarity deduplication ─────────────────────────────
        title = item.get("title", "")
        tkey = _title_key(title)
        if tkey and tkey in seen_title_keys:
            logger.debug("Skipping near-duplicate title: %r", title)
            continue

        seen_urls.add(norm_url)
        if tkey:
            seen_title_keys.add(tkey)

        pub_date = _parse_serp_date(item.get("date", ""))
        url_date = _extract_date_from_url(link)
        best_date = pub_date or url_date

        # Date filtering — but don't skip articles without dates when we have
        # a date range. Google's tbs parameter already filtered server-side,
        # so undated articles from the results are likely within range.
        if best_date:
            if date_from and best_date.date() < date_from.date():
                continue
            if date_to and best_date.date() > date_to.date():
                continue

        articles.append({
            "title": title,
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
    """Search Google News + regular Google for crime articles."""
    queries = build_search_queries(
        state=state,
        city=city,
        county=county,
        police_dept=police_dept,
        charges=charges,
        custom_charges=custom_charges,
        gender=gender,
    )

    tbs_param = None
    if date_from and date_to:
        tbs_param = _make_tbs_param(date_from, date_to)

    all_articles = []
    seen_urls: set = set()
    seen_title_keys: set = set()
    total_queries = len(queries)

    # ── Pass 1: Google News ─────────────────────────────────────────────
    for qi, query in enumerate(queries):
        if len(all_articles) >= max_results:
            break

        if progress_callback:
            progress_callback(qi, total_queries * 2, query, len(all_articles))

        for start in range(0, min(60, max_results - len(all_articles)), 10):
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
                results = _serpapi_search_with_retry(params)
                news = results.get("news_results", [])
                if not news:
                    break
                new_articles = _extract_articles_from_results(
                    news, seen_urls, date_from, date_to, seen_title_keys
                )
                all_articles.extend(new_articles)
                if len(all_articles) >= max_results:
                    break
            except RuntimeError as exc:
                logger.error("Pass 1 query %r failed: %s", query, exc)
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("Pass 1 unexpected error for query %r: %s", query, exc)
                break

            time.sleep(0.3)

        time.sleep(0.5)

    # ── Pass 2: Regular Google search (catches articles News misses) ────
    for qi, query in enumerate(queries):
        if len(all_articles) >= max_results:
            break

        if progress_callback:
            progress_callback(total_queries + qi, total_queries * 2, query, len(all_articles))

        for start in [0, 10, 20]:  # 3 pages of regular results
            if len(all_articles) >= max_results:
                break
            params = {
                "engine": "google",
                "q": query,
                "gl": "us",
                "hl": "en",
                "num": 20,
                "start": start,
                "api_key": api_key,
            }
            if tbs_param:
                params["tbs"] = tbs_param

            try:
                results = _serpapi_search_with_retry(params)
                organic = results.get("organic_results", [])
                if not organic:
                    break
                news_like = [
                    {
                        "link": r.get("link", ""),
                        "title": r.get("title", ""),
                        "source": r.get("displayed_link", ""),
                        "snippet": r.get("snippet", ""),
                        "date": r.get("date", ""),
                    }
                    for r in organic
                ]
                new_articles = _extract_articles_from_results(
                    news_like, seen_urls, date_from, date_to, seen_title_keys
                )
                all_articles.extend(new_articles)
            except RuntimeError as exc:
                logger.error("Pass 2 query %r (start=%d) failed: %s", query, start, exc)
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("Pass 2 unexpected error for query %r (start=%d): %s", query, start, exc)
                break

            time.sleep(0.3)

        time.sleep(0.5)

    # ── Pass 3: Targeted news source searches ──────────────────────────
    # Search specific news aggregators that cover local crime
    if len(all_articles) < max_results and state:
        news_site_queries = []
        loc = f'"{city}" "{state}"' if city else f'"{state}"'

        # Build site-specific queries for local news coverage
        if charges or custom_charges:
            charge_list = []
            if charges:
                for c in charges:
                    terms = CHARGE_CATEGORIES.get(c, [c.lower()])
                    charge_list.extend(terms[:2])
            if custom_charges:
                charge_list.extend(custom_charges)
            charge_sample = " OR ".join(f'"{c}"' for c in charge_list[:4])
            news_site_queries.append(f'arrested ({charge_sample}) {loc}')
            news_site_queries.append(f'police arrest ({charge_sample}) {loc}')
        else:
            news_site_queries.append(f'arrested charged police {loc}')
            news_site_queries.append(f'suspect arrested {loc}')

        for qi, query in enumerate(news_site_queries):
            if len(all_articles) >= max_results:
                break

            if progress_callback:
                progress_callback(
                    total_queries * 2 + qi,
                    total_queries * 2 + len(news_site_queries),
                    f"Deep search: {query[:50]}",
                    len(all_articles),
                )

            params = {
                "engine": "google",
                "q": query,
                "gl": "us",
                "hl": "en",
                "num": 20,
                "tbm": "nws",  # Google News tab in regular search
                "api_key": api_key,
            }
            if tbs_param:
                params["tbs"] = tbs_param

            try:
                results = _serpapi_search_with_retry(params)
                news = results.get("news_results", [])
                if not news:
                    news = results.get("organic_results", [])
                news_like = [
                    {
                        "link": r.get("link", ""),
                        "title": r.get("title", ""),
                        "source": r.get("source", r.get("displayed_link", "")),
                        "snippet": r.get("snippet", ""),
                        "date": r.get("date", ""),
                    }
                    for r in news
                ]
                new_articles = _extract_articles_from_results(
                    news_like, seen_urls, date_from, date_to, seen_title_keys
                )
                all_articles.extend(new_articles)
            except RuntimeError as exc:
                logger.error("Pass 3 deep-search query %r failed: %s", query, exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Pass 3 unexpected error for query %r: %s", query, exc)

            time.sleep(0.5)

    # Sort by date (newest first)
    all_articles.sort(key=lambda a: a.get("date") or datetime.min, reverse=True)

    logger.info(
        "search_articles complete: %d articles found, %d API calls made this session.",
        len(all_articles[:max_results]),
        _api_call_count,
    )
    return all_articles[:max_results]
