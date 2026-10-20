"""Open-role searches via SerpApi's Google Jobs, plus the rules for reading them.

Google Jobs aggregates listings from job boards and employers' own career
sites. city_listings searches a profession in a city; facility_listings
searches a profession at one named facility. The helpers decide whether a
listing is in the clinician's profession and which employer it belongs to.
Matching (src/tools_facilities.py) builds the hiring employers from these.

Needs SERPAPI_KEY in the environment; without it (or if SerpApi fails) a
search returns None. Results are cached per warm instance for a day to stay
inside the free tier.
"""

import os
import re
import time

import requests

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
CACHE_TTL_SECONDS = 24 * 60 * 60
PAGES = 1  # Google Jobs returns 10 listings per page; facility searches fill the gaps

# Words too generic to tell two employers apart.
_GENERIC_WORDS = {
    "the", "of", "and", "at", "inc", "llc", "hospital", "hospitals", "medical", "center",
    "centers", "centre", "health", "healthcare", "system", "systems", "regional", "clinic",
    "campus", "services", "behavioral", "community", "general", "memorial",
}

# A listing only counts if its title is a job in the clinician's profession.
_PROFESSION_TITLE_PATTERNS = {
    "psychologist": r"psychologist|\bpsy\.?d\b|psychology (post|fellow|resident|intern)",
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
        resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=25)
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


def city_listings(city: str, state: str, profession: str, notes: list = None, variant: str = ""):
    """Open roles for a profession in one city (up to PAGES pages), or None if the lookup can't run.
    With no city, searches the whole state (pass the state's full name). `variant` narrows the
    search to a kind of role, e.g. "locum tenens" or "per diem"."""
    notes = [] if notes is None else notes
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        notes.append("SERPAPI_KEY is not set")
        return None

    key = (city.lower(), state.lower(), profession.lower(), variant)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        notes.append(f"{variant} {profession} in {city or state}: {len(cached[1])} listings (cached) "
                     f"from {_employers(cached[1])}".strip())
        return cached[1]

    # The place goes in the query: SerpApi's location parameter only accepts names from its own
    # canonical list and rejects others (e.g. "Denver, CO") with an error.
    place = f"{city}, {state}" if city else state
    params = {"engine": "google_jobs", "q": f"{variant} {profession} jobs in {place}".strip(),
              "hl": "en", "gl": "us", "api_key": api_key}
    label = f"{variant} {profession} in {place}".strip()
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
        "facilities": r"highlands behavioral health|centennial peaks|cedar springs",
    },
    {   # HCA HealthONE (CMS now lists these as "HCA HealthONE <name>")
        "employer": r"healthone|\bhca\b",
        "facilities": r"healthone|presbyterian.st|rose medical|swedish medical|sky ridge|north suburban|"
                      r"medical center of aurora",
    },
    {   # AdventHealth (CMS: "AdventHealth <name>"; formerly "<name> Adventist Hospital")
        "employer": r"adventhealth|advent health",
        "facilities": r"adventhealth|adventist",
    },
    {   # Intermountain Health
        "employer": r"intermountain",
        "facilities": r"intermountain|saint joseph hospital|good samaritan|lutheran|platte valley",
    },
    {   # CommonSpirit Health
        "employer": r"commonspirit|common spirit",
        "facilities": r"commonspirit|penrose|st\.? francis|st\.? anthony|st\.? mary.corwin",
    },
    {   # UCHealth
        "employer": r"uchealth|uc health|university of colorado health",
        "facilities": r"uchealth|university of colorado hospital|poudre valley|medical center of the rockies|"
                      r"memorial hospital|highlands ranch hospital|longs peak|broomfield hospital|greeley hospital",
    },
    {   # State of Colorado runs the state mental health hospitals (Fort Logan, Pueblo)
        "employer": r"state of colorado|colorado department of human services",
        "facilities": r"colorado mental health (hospital|institute)",
    },
]


def _parent_owns(facility_name: str, company_name: str) -> bool:
    facility, company = (facility_name or "").lower(), (company_name or "").lower()
    return any(re.search(p["employer"], company) and re.search(p["facilities"], facility)
               for p in PARENT_SYSTEMS)


def _job_city(job: dict) -> str:
    return (job.get("location") or "").split(",")[0].strip().lower()


def _job_state(job: dict) -> str:
    """Two-letter state from a listing's location such as "Seattle, WA", or ""."""
    parts = [p.strip() for p in (job.get("location") or "").split(",")]
    state = parts[1].split()[0] if len(parts) > 1 and parts[1] else ""
    return state.upper() if len(state) == 2 and state.isalpha() else ""


_PAY_PERIOD = r"a year|an hour|a month|a week|per (year|hour|month|week)|/\s*(yr|hr|year|hour)"


def pay_range(job: dict):
    """The listing's pay as Google Jobs shows it (e.g. "$140K–$165K a year"), or None."""
    pay = (job.get("detected_extensions") or {}).get("salary")
    if not pay:
        pay = next((e for e in job.get("extensions") or [] if isinstance(e, str) and re.search(_PAY_PERIOD, e.lower())
                    and re.search(r"\d", e)), None)
    if not pay:
        return None
    pay = pay.strip()
    if "$" not in pay:  # Google often drops the currency: "140K–165K a year"
        pay = re.sub(r"(?<![\d.,])(\d[\d,.]*\s*[KkMm]?)", r"$\1", pay)
    return pay


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
    _cache[key] = (time.time(), jobs)
    return jobs
