"""Open-role lookup for matched facilities via SerpApi's Google Jobs search.

Google Jobs aggregates listings from job boards and hospitals' own career
sites. For each matched facility this searches for the clinician's profession
at that facility and keeps only listings whose employer name fits the
facility, so "hiring" is only shown when a real listing names that employer.

Needs SERPAPI_KEY in the environment. Without it (or if SerpApi fails), every
lookup returns None and the site falls back to a plain job-search link.
Results are cached per warm instance for a day to stay inside the free tier.
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_LISTINGS = 3

# Words too generic to tell two employers apart.
_GENERIC_WORDS = {
    "the", "of", "and", "at", "inc", "llc", "hospital", "hospitals", "medical", "center",
    "centers", "centre", "health", "healthcare", "system", "systems", "regional", "clinic",
    "campus", "services", "behavioral", "community", "general", "memorial",
}

_cache = {}


def _distinct_words(name: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _GENERIC_WORDS}


def _same_employer(facility_name: str, company_name: str) -> bool:
    facility, company = _distinct_words(facility_name), _distinct_words(company_name)
    if not facility or not company:
        return False
    # The listing's employer must be (part of) the facility's name, e.g. "Denver Health" for
    # "Denver Health Medical Center" -- not just share a city word like "Denver".
    return company <= facility or len(facility & company) >= 2


def find_openings(facility_name: str, city: str, state: str, profession: str):
    """Open roles for a profession at one facility, or None if the lookup can't run."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return None

    key = (facility_name.lower(), city.lower(), state.lower(), profession.lower())
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    params = {
        "engine": "google_jobs",
        "q": f"{profession} {facility_name}",
        "location": f"{city}, {state}",
        "hl": "en",
        "api_key": api_key,
    }
    try:
        resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=10)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if resp.status_code != 200 or ("error" in data and "jobs_results" not in data):
        # SerpApi reports "no results" as an error message; anything else means the lookup failed.
        if "hasn't returned any results" not in str(data.get("error", "")):
            return None
        data = {"jobs_results": []}

    listings = []
    for job in data.get("jobs_results", []):
        if not _same_employer(facility_name, job.get("company_name")):
            continue
        apply_links = job.get("apply_options") or []
        link = (apply_links[0].get("link") if apply_links else None) or job.get("share_link")
        if not link:
            continue
        listings.append({"title": job.get("title"), "link": link, "via": job.get("via")})

    result = {"source": "Google Jobs via SerpApi", "count": len(listings), "listings": listings[:MAX_LISTINGS]}
    _cache[key] = (time.time(), result)
    return result


def add_openings(facility_matches, profession: str):
    """Attach open roles to each matched facility, looking them up in parallel."""
    if not facility_matches or not os.environ.get("SERPAPI_KEY"):
        return facility_matches
    with ThreadPoolExecutor(max_workers=5) as pool:
        openings = list(pool.map(
            lambda m: find_openings(m["name"], m["city"], m["state"], profession), facility_matches))
    return [{**m, "openings": o} for m, o in zip(facility_matches, openings)]
