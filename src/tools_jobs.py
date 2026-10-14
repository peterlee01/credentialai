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
PAGES = 1  # Google Jobs returns 10 listings per page; facility searches fill the gaps

# Words too generic to tell two employers apart.
_GENERIC_WORDS = {
    "the", "of", "and", "at", "inc", "llc", "hospital", "hospitals", "medical", "center",
    "centers", "centre", "health", "healthcare", "system", "systems", "regional", "clinic",
    "campus", "services", "behavioral", "community", "general", "memorial",
}

# A listing only counts if its title is a job in the clinician's profession.
_PROFESSION_TITLE_PATTERNS = {
    "psychologist": r"psychologist|\bpsy\.?d\b",
    "counselor": r"counsel|therapist",
    "social worker": r"social work|\blcsw\b|\bmsw\b",
    "physician": r"physician|doctor|hospitalist|psychiatrist|surgeon|\bm\.?d\b|\bd\.?o\b",
}

_cache = {}


# Support and non-clinical roles that mention a profession but aren't that job.
_NOT_THE_ROLE = r"assistant|\btech(nician)?\b|\baide\b|coordinator|scheduler|receptionist|admissions|" \
    r"financial|recruit|billing|sales|camp\b|career|student|volunteer"


def _title_fits(title: str, profession: str) -> bool:
    title = (title or "").lower()
    pattern = _PROFESSION_TITLE_PATTERNS.get(profession.strip().lower())
    if not title or re.search(_NOT_THE_ROLE, title):
        return False
    return pattern is None or re.search(pattern, title) is not None


def _distinct_words(name: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _GENERIC_WORDS}


def _same_employer(facility_name: str, company_name: str) -> bool:
    facility, company = _distinct_words(facility_name), _distinct_words(company_name)
    if not facility or not company:
        return False
    # The listing's employer must be (part of) the facility's name, e.g. "Denver Health" for
    # "Denver Health Medical Center" -- not just share a city word like "Denver".
    return company <= facility or len(facility & company) >= 2


def _search(params: dict, label: str, notes: list):
    """One SerpApi Google Jobs request. Returns its listings, [] for no results, None on failure."""
    try:
        resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=15)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        notes.append(f"{label}: request failed ({exc.__class__.__name__})")
        print(f"[openings] SerpApi request failed for {label}: {exc!r}")
        return None, None
    if resp.status_code != 200 or ("error" in data and "jobs_results" not in data):
        # SerpApi reports "no results" as an error message; anything else means the lookup failed.
        if "hasn't returned any results" in str(data.get("error", "")):
            return [], None
        notes.append(f"{label}: SerpApi error {resp.status_code} - {data.get('error')}")
        print(f"[openings] SerpApi error for {label}: HTTP {resp.status_code} {data.get('error')}")
        return None, None
    return data.get("jobs_results", []), (data.get("serpapi_pagination") or {}).get("next_page_token")


def city_listings(city: str, state: str, profession: str, notes: list = None):
    """Open roles for a profession in one city (up to PAGES pages), or None if the lookup can't run."""
    notes = [] if notes is None else notes
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        notes.append("SERPAPI_KEY is not set")
        return None

    key = (city.lower(), state.lower(), profession.lower())
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        notes.append(f"{profession} in {city}, {state}: {len(cached[1])} listings (cached) from {_employers(cached[1])}")
        return cached[1]

    # The place goes in the query: SerpApi's location parameter only accepts names from its own
    # canonical list and rejects others (e.g. "Denver, CO") with an error.
    params = {"engine": "google_jobs", "q": f"{profession} jobs in {city}, {state}",
              "hl": "en", "gl": "us", "api_key": api_key}
    label = f"{profession} in {city}, {state}"
    jobs, next_token = _search(params, label, notes)
    if jobs is None:
        return None
    for _ in range(PAGES - 1):
        if not next_token:
            break
        more, next_token = _search({**params, "next_page_token": next_token}, label, notes)
        jobs += more or []

    notes.append(f"{label}: {len(jobs)} listings from {_employers(jobs)}")
    print(f"[openings] {label}: {len(jobs)} listings from {_employers(jobs)}")
    _cache[key] = (time.time(), jobs)
    return jobs


def _employers(jobs) -> str:
    return ", ".join(sorted({j.get("company_name") or "?" for j in jobs})) or "no employers"


# Health systems that post jobs under the parent company's name rather than the
# hospital's. A parent's listing is only credited to a hospital it owns AND only when
# the listing is in that hospital's city, so it can't be pinned on the wrong site.
# Colorado for now (where license checks are live); facility patterns are matched
# against CMS facility names, lowercased.
PARENT_SYSTEMS = [
    {   # Universal Health Services
        "employer": r"universal health services|\buhs\b",
        "facilities": ("highlands behavioral health", "centennial peaks", "cedar springs"),
    },
    {   # HCA HealthONE
        "employer": r"healthone|\bhca\b",
        "facilities": ("presbyterian/st", "presbyterian st", "rose medical center", "swedish medical center",
                       "sky ridge", "north suburban", "medical center of aurora"),
    },
    {   # AdventHealth
        "employer": r"adventhealth|advent health",
        "facilities": ("avista adventist", "castle rock adventist", "littleton adventist",
                       "parker adventist", "porter adventist"),
    },
    {   # Intermountain Health
        "employer": r"intermountain",
        "facilities": ("saint joseph hospital", "good samaritan", "lutheran", "platte valley"),
    },
    {   # CommonSpirit Health
        "employer": r"commonspirit|common spirit",
        "facilities": ("penrose", "st francis", "st. francis", "st anthony", "st. anthony", "st mary corwin",
                       "st. mary-corwin"),
    },
    {   # UCHealth
        "employer": r"uchealth|uc health|university of colorado health",
        "facilities": ("university of colorado hospital", "poudre valley", "medical center of the rockies",
                       "memorial hospital", "highlands ranch hospital", "longs peak", "broomfield hospital",
                       "greeley hospital"),
    },
]


def _parent_owns(facility_name: str, company_name: str) -> bool:
    facility, company = (facility_name or "").lower(), (company_name or "").lower()
    return any(re.search(p["employer"], company) and any(f in facility for f in p["facilities"])
               for p in PARENT_SYSTEMS)


def _job_city(job: dict) -> str:
    return (job.get("location") or "").split(",")[0].strip().lower()


def _link(job: dict):
    apply_links = job.get("apply_options") or []
    return (apply_links[0].get("link") if apply_links else None) or job.get("share_link")


def _is_facility_job(facility: dict, job: dict) -> bool:
    if _same_employer(facility["name"], job.get("company_name")):
        return True
    return (_parent_owns(facility["name"], job.get("company_name"))
            and _job_city(job) == (facility.get("city") or "").strip().lower())


def facility_listings(facility: dict, profession: str, notes: list = None):
    """Listings from a search aimed at one facility, for when the city-wide search found
    none there. Cached per facility for a day. None if the lookup can't run."""
    notes = [] if notes is None else notes
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return None
    name, city, state = facility["name"], facility["city"], facility["state"]
    key = ("facility", name.lower(), city.lower(), state.lower(), profession.lower())
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    label = f"{profession} at {name}"
    jobs, _ = _search({"engine": "google_jobs", "q": f"{profession} jobs at {name} {city}, {state}",
                       "hl": "en", "gl": "us", "api_key": api_key}, label, notes)
    if jobs is None:
        return None
    notes.append(f"{label}: {len(jobs)} listings from {_employers(jobs)}")
    _cache[key] = (time.time(), jobs)
    return jobs


def _openings_for(facility: dict, jobs, profession: str):
    if jobs is None:
        return None
    listings = []
    for job in jobs:
        if _is_facility_job(facility, job) and _title_fits(job.get("title"), profession) and _link(job):
            listings.append({"title": job.get("title"), "link": _link(job)})
    return {"source": "Google Jobs via SerpApi", "count": len(listings), "listings": listings[:MAX_LISTINGS]}


def nearby_openings(jobs_by_city: list, shown_links: set, profession: str, limit: int = 5) -> list:
    """Other open roles in the profession from the same searches: real listings with links,
    not tied to a matched facility. Skips ones already shown under a facility and duplicates."""
    seen, out = set(shown_links), []
    for jobs in jobs_by_city:
        for job in jobs or []:
            link = _link(job)
            key = ((job.get("title") or "").lower(), (job.get("company_name") or "").lower())
            if not link or link in seen or key in seen or not _title_fits(job.get("title"), profession):
                continue
            seen.update({link, key})
            out.append({"title": job.get("title"), "employer": job.get("company_name"),
                        "location": job.get("location"), "link": link})
            if len(out) >= limit:
                return out
    return out


def add_openings(facility_matches, profession: str, notes: list = None):
    """Attach open roles to each matched facility (one search per city, then a search aimed at
    each facility still without one; matched by employer name or owning health system) and
    collect other nearby roles in the profession.
    Returns (facility_matches, nearby). Appends one plain line per city to `notes`."""
    notes = [] if notes is None else notes
    if not facility_matches:
        return facility_matches, []
    if not os.environ.get("SERPAPI_KEY"):
        notes.append("SERPAPI_KEY is not set")
        return facility_matches, []
    # Search the best match's city first so its roles lead the nearby list.
    cities = list(dict.fromkeys((m["city"], m["state"]) for m in facility_matches))
    with ThreadPoolExecutor(max_workers=4) as pool:
        by_city = dict(zip(cities, pool.map(lambda c: city_listings(c[0], c[1], profession, notes), cities)))
    matched = [{**m, "openings": _openings_for(m, by_city[(m["city"], m["state"])], profession)}
               for m in facility_matches]

    # Facilities the city-wide search found nothing for get a search aimed at them.
    gaps = [i for i, m in enumerate(matched) if m["openings"] is not None and not m["openings"]["count"]]
    with ThreadPoolExecutor(max_workers=5) as pool:
        targeted = list(pool.map(lambda i: facility_listings(matched[i], profession, notes), gaps))
    for i, jobs in zip(gaps, targeted):
        found = _openings_for(matched[i], jobs, profession)
        if found and found["count"]:
            matched[i] = {**matched[i], "openings": found}

    shown = {j["link"] for m in matched for j in ((m["openings"] or {}).get("listings") or [])}
    return matched, nearby_openings([by_city[c] for c in cities], shown, profession)
