"""Open-role lookup for matched facilities via SerpApi's Google Jobs search.

Google Jobs aggregates listings from job boards and hospitals' own career
sites. For each city with a matched facility this searches once for the
clinician's profession in that city, then keeps, per facility, only listings
whose employer name fits it, so "hiring" is only shown when a real listing
names that employer.

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
PAGES = 2  # Google Jobs returns 10 listings per page

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


def _search(params: dict, label: str):
    """One SerpApi Google Jobs request. Returns its listings, [] for no results, None on failure."""
    try:
        resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=15)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[openings] SerpApi request failed for {label}: {exc!r}")
        return None, None
    if resp.status_code != 200 or ("error" in data and "jobs_results" not in data):
        # SerpApi reports "no results" as an error message; anything else means the lookup failed.
        if "hasn't returned any results" in str(data.get("error", "")):
            return [], None
        print(f"[openings] SerpApi error for {label}: HTTP {resp.status_code} {data.get('error')}")
        return None, None
    return data.get("jobs_results", []), (data.get("serpapi_pagination") or {}).get("next_page_token")


def city_listings(city: str, state: str, profession: str):
    """Open roles for a profession in one city (up to PAGES pages), or None if the lookup can't run."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return None

    key = (city.lower(), state.lower(), profession.lower())
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    # The place goes in the query: SerpApi's location parameter only accepts names from its own
    # canonical list and rejects others (e.g. "Denver, CO") with an error.
    params = {"engine": "google_jobs", "q": f"{profession} jobs in {city}, {state}",
              "hl": "en", "gl": "us", "api_key": api_key}
    label = f"{profession} in {city}, {state}"
    jobs, next_token = _search(params, label)
    if jobs is None:
        return None
    for _ in range(PAGES - 1):
        if not next_token:
            break
        more, next_token = _search({**params, "next_page_token": next_token}, label)
        jobs += more or []

    employers = sorted({j.get("company_name") or "?" for j in jobs})
    print(f"[openings] {label}: {len(jobs)} listings from {employers}")
    _cache[key] = (time.time(), jobs)
    return jobs


def _openings_for(facility: dict, jobs):
    if jobs is None:
        return None
    listings = []
    for job in jobs:
        if not _same_employer(facility["name"], job.get("company_name")):
            continue
        apply_links = job.get("apply_options") or []
        link = (apply_links[0].get("link") if apply_links else None) or job.get("share_link")
        if link:
            listings.append({"title": job.get("title"), "link": link, "via": job.get("via")})
    return {"source": "Google Jobs via SerpApi", "count": len(listings), "listings": listings[:MAX_LISTINGS]}


def add_openings(facility_matches, profession: str):
    """Attach open roles to each matched facility: one search per city, matched by employer name."""
    if not facility_matches or not os.environ.get("SERPAPI_KEY"):
        return facility_matches
    cities = sorted({(m["city"], m["state"]) for m in facility_matches})
    with ThreadPoolExecutor(max_workers=4) as pool:
        by_city = dict(zip(cities, pool.map(lambda c: city_listings(c[0], c[1], profession), cities)))
    return [{**m, "openings": _openings_for(m, by_city[(m["city"], m["state"])])} for m in facility_matches]
