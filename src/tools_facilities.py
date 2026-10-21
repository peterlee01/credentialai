"""Matching a cleared clinician to employers that are hiring for their profession.

Once a clinician clears verification (GO), matching starts from real open
roles (Google Jobs via SerpApi, see src/tools_jobs.py) near their practice
address, groups them by employer and scores each employer for fit, so every
match comes with job links. Hospitals are enriched from the free, public CMS
"Hospital General Information" dataset (type, rating), which also tells us
which hospitals to search directly. See
https://data.cms.gov/provider-data/dataset/xubh-q36u

Without job data (no SERPAPI_KEY, or every search failed) matching falls back
to the CMS hospital shortlist, which has no job links.
"""

import contextvars
import functools
import json
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from anthropic import beta_tool

from .tools_jobs import (city_listings, facility_listings, pay_range, _is_facility_job, _job_city, _job_state, _link,
                        _title_fits)
from . import prefs as prefs_mod

CMS_ENDPOINT = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"
CMS_SOURCE = "CMS Hospital General Information (live, public)"
PAGE_SIZE = 500
MAX_PAGES = 4
SHORTLIST_SIZE = 8

STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Illinois": "IL",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Missouri": "MO",
    "New Hampshire": "NH", "New Jersey": "NJ", "New York": "NY", "Ohio": "OH", "Rhode Island": "RI",
    "Tennessee": "TN", "Texas": "TX", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV",
}

# A signed-in physician's preferences, set around their verification so the coordinator's
# match_facilities call searches the way they asked (area, extra cities, contract type).
SEARCH_PREFS = contextvars.ContextVar("search_prefs", default=None)
_SEARCH_VARIANTS = {"locums": "locum tenens", "per-diem": "per diem", "contractor": "contract"}

BEHAVIORAL_PROFESSIONS = {"psychologist", "counselor", "social worker"}
BEHAVIORAL_SPECIALTY_WORDS = ("psych", "behavior", "counsel", "social work", "addiction")


@functools.lru_cache(maxsize=16)
def _fetch_state_facilities(state_code: str) -> tuple:
    """All CMS hospital records for one state. Cached per warm instance."""
    rows = []
    for page in range(MAX_PAGES):
        params = {
            "conditions[0][property]": "state",
            "conditions[0][value]": state_code,
            "conditions[0][operator]": "=",
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "count": "true",
            "results": "true",
            "schema": "false",
            "keys": "true",
        }
        resp = requests.get(CMS_ENDPOINT, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("results", [])
        rows.extend(batch)
        if len(batch) < PAGE_SIZE or len(rows) >= int(data.get("count") or 0):
            break
    return tuple(rows)


def _rating(row: dict):
    try:
        return int(row.get("hospital_overall_rating") or "")
    except ValueError:
        return None


def _score(row: dict, profession: str, specialty: str, city: str, zip_code: str):
    """Return (score, signals) for one facility. Signals are the plain-language
    facts the score is built from, so every ranking can be explained."""
    score = 0.0
    signals = []
    hospital_type = (row.get("hospital_type") or "").lower()
    facility_zip = (row.get("zip_code") or "")[:5]

    if city and (row.get("citytown") or "").strip().lower() == city:
        score += 4
        signals.append("Same city as practice address")
    elif zip_code and facility_zip[:3] == zip_code[:3]:
        score += 3
        signals.append("Same ZIP area as practice address")
    elif zip_code and facility_zip.isdigit() and zip_code[:5].isdigit():
        # Rough closeness only: nearby ZIP codes are usually, not always, nearby places.
        score += max(0.0, 2 - abs(int(facility_zip) - int(zip_code[:5])) / 250)

    behavioral = profession in BEHAVIORAL_PROFESSIONS or any(w in specialty for w in BEHAVIORAL_SPECIALTY_WORDS)
    if behavioral and "psychiatric" in hospital_type:
        score += 3
        signals.append("Psychiatric facility matches profession")
    elif "pediatric" in specialty and "children" in hospital_type:
        score += 3
        signals.append("Children's hospital matches specialty")
    elif not behavioral and ("acute care" in hospital_type or "critical access" in hospital_type):
        score += 1
        signals.append("General hospital")

    if "emergency" in specialty and row.get("emergency_services") == "Yes":
        score += 2
        signals.append("Has emergency services")

    rating = _rating(row)
    if rating:
        score += rating * 0.3
        signals.append(f"CMS overall rating {rating} of 5")

    return score, signals


def _max_score(profession: str, specialty: str) -> float:
    """Best score a facility could get for this clinician: closest location, best type fit,
    emergency services when the specialty needs them, and a 5-star CMS rating."""
    return 4 + 3 + (2 if "emergency" in specialty else 0) + 5 * 0.3


def shortlist_facilities(profession: str, state: str, specialty: str = "",
                         practice_city: str = "", practice_zip: str = "") -> list:
    """Top facilities in the clinician's state, best fit first."""
    state_code = STATE_CODES.get(state.strip(), state.strip().upper())
    prof = profession.strip().lower()
    spec = specialty.strip().lower()
    city = practice_city.strip().lower()
    zip_code = "".join(ch for ch in practice_zip if ch.isdigit())

    scored = []
    for row in _fetch_state_facilities(state_code):
        score, signals = _score(row, prof, spec, city, zip_code)
        scored.append((score, row.get("facility_name") or "", row, signals))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best = _max_score(prof, spec)

    return [
        {
            "facility_id": row.get("facility_id"),
            "name": (row.get("facility_name") or "").title(),
            "city": (row.get("citytown") or "").title(),
            "state": row.get("state"),
            "hospital_type": row.get("hospital_type"),
            "emergency_services": row.get("emergency_services") == "Yes",
            "cms_rating": _rating(row),
            "match_pct": min(100, round(100 * score / best)),
            "signals": signals,
        }
        for score, _, row, signals in scored[:SHORTLIST_SIZE]
    ]


# Agencies that post roles at other organisations' sites; a match should be the employer itself.
_STAFFING = r"staffing|locum|recruit|\bamn\b|\baya\b|sunbelt|healthstaff|travel (nurse|therap)|placement|" \
    r"\btalent\b|search group|workforce"
_BEHAVIORAL_EMPLOYER = r"behavior|mental|psych|counsel|therap|wellness|recovery|mind"
_PHYSICIAN_EMPLOYER = r"medical|clinic|health|hospital|physician|medicine|care"
HIRING_SHORTLIST = 8
CITY_SEARCHES = 3
FACILITY_SEARCHES = 3


def _employer_key(name: str) -> str:
    name = re.sub(r"\(.*?\)|\s+-\s+\d+$|,?\s+(llc|inc|pllc|pc|corp)\.?$", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def _display_name(name: str) -> str:
    return re.sub(r"\s+-\s+\d+$", "", (name or "").strip())


def hiring_candidates(profession: str, state: str, specialty: str = "", practice_city: str = "",
                      practice_zip: str = "", notes: list = None, prefs: dict = None):
    """Employers near the clinician with open roles in their profession, best fit first.

    Each candidate has at least one real listing. Returns None when job data isn't available
    (no SERPAPI_KEY, or every search failed). Appends plain lines about the searches to `notes`.
    Preferences can widen the searches to the whole state and to extra cities, and aim them at
    contractor, per diem or locums roles."""
    notes = [] if notes is None else notes
    prefs = prefs or {}
    prof = profession.strip().lower()
    state_code = STATE_CODES.get(state.strip(), state.strip().upper())
    state_name = next((name for name, code in STATE_CODES.items() if code == state_code), state.strip())
    hospitals = shortlist_facilities(profession, state, specialty, practice_city, practice_zip)
    home = practice_city.strip().title() or (hospitals[0]["city"] if hospitals else "")
    variant = _SEARCH_VARIANTS.get(prefs.get("contract"), "")

    cities = list(dict.fromkeys([c for c in [home] + [h["city"] for h in hospitals] if c]))[:CITY_SEARCHES]
    searches = [(c, state_code) for c in cities]
    if prefs.get("area") == "state":
        searches.append(("", state_name))
    chosen = [prefs_mod.parse_city(c) for c in prefs.get("also_cities") or []]
    chosen = [c for c in chosen if c]
    searches += [c for c in chosen if c not in searches]
    with ThreadPoolExecutor(max_workers=4) as pool:
        by_city = list(pool.map(lambda s: city_listings(s[0], s[1], prof, notes, variant), searches))
    if all(r is None for r in by_city):
        return None
    jobs = [j for r in by_city for j in (r or [])]

    # Search the best-fitting hospitals directly when the city searches found nothing there.
    targets = [h for h in hospitals[:FACILITY_SEARCHES] if not any(_is_facility_job(h, j) for j in jobs)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs += [j for r in pool.map(lambda h: facility_listings(h, prof, notes), targets) for j in (r or [])]

    groups = {}
    for job in jobs:
        link, company = _link(job), job.get("company_name") or ""
        if not link or not company or not _title_fits(job.get("title"), prof):
            continue
        # Locums roles are mostly posted by agencies, so they stay in for a locums preference.
        if prefs.get("contract") != "locums" and re.search(_STAFFING, company.lower()):
            continue
        hospital = next((h for h in hospitals if _is_facility_job(h, job)), None)
        if hospital:
            key = "cms-" + str(hospital["facility_id"])
            base = {k: hospital[k] for k in ("name", "city", "state", "hospital_type", "emergency_services", "cms_rating")}
        else:
            key = "emp-" + re.sub(r"\s+", "-", _employer_key(company))
            city = _job_city(job) or home.lower()
            base = {"name": _display_name(company), "city": "Remote" if city in ("anywhere", "remote") else city.title(),
                    "state": _job_state(job) or state_code, "hospital_type": None, "emergency_services": False,
                    "cms_rating": None}
        group = groups.setdefault(key, {"facility_id": key, **base, "listings": [], "_links": set()})
        if link not in group["_links"]:
            group["_links"].add(link)
            ext = job.get("detected_extensions") or {}
            pay = pay_range(job)
            group["listings"].append({
                "title": job.get("title"), "link": link, "pay": pay,
                "pay_annual_min": prefs_mod.pay_annual_min(pay),
                "schedule": ext.get("schedule_type"),
                "remote": bool(ext.get("work_from_home")) or _job_city(job) in ("anywhere", "remote"),
                "terms": prefs_mod.contract_terms(job),
            })

    behavioral = prof in BEHAVIORAL_PROFESSIONS
    best = 4 + 3 + 1.5 + 1.5
    candidates = []
    for g in groups.values():
        signals, score = [], 0.0
        if g["city"].lower() == home.lower() and g["state"] == state_code:
            score += 4
            signals.append("Same city as practice address")
        elif (g["city"], g["state"]) in chosen:
            score += 4
            signals.append(f"In {g['city']}, a city you chose")
        elif g["state"] != state_code:
            score += 2
            signals.append(f"In {g['city']}, {g['state']}")
        elif prefs.get("area") == "state" and g["city"] not in cities and g["city"] != "Remote":
            score += 1.5
            signals.append(f"In {g['city']}, elsewhere in {state_name}")
        else:
            score += 2
            signals.append(f"Near the practice address ({g['city']})")
        hospital_type = (g["hospital_type"] or "").lower()
        if behavioral and "psychiatric" in hospital_type:
            score += 3
            signals.append("Psychiatric hospital")
        elif not behavioral and ("acute care" in hospital_type or "critical access" in hospital_type):
            score += 3
            signals.append("General hospital")
        elif re.search(_BEHAVIORAL_EMPLOYER if behavioral else _PHYSICIAN_EMPLOYER, g["name"].lower()):
            score += 2
            signals.append("Setting suits the profession")
        else:
            score += 1
        if g["cms_rating"]:
            score += g["cms_rating"] * 0.3
            signals.append(f"CMS overall rating {g['cms_rating']} of 5")
        roles = len(g["listings"])
        score += min(roles, 3) * 0.5
        signals.append(f"{roles} open {prof} {'role' if roles == 1 else 'roles'}")
        candidates.append({**{k: v for k, v in g.items() if k != "_links"},
                           "match_pct": min(100, round(100 * score / best)), "signals": signals, "_score": score})

    candidates.sort(key=lambda c: (-c["_score"], c["name"]))
    for c in candidates:
        c.pop("_score")
        notes.append(f"{c['name']} ({c['city']}): {len(c['listings'])} open roles")
    return candidates[:HIRING_SHORTLIST]


def _candidates(match_input: dict, notes: list = None, prefs: dict = None):
    """Hiring employers, or the CMS hospital shortlist when there's no job data."""
    hiring = hiring_candidates(**match_input, notes=notes, prefs=prefs)
    if hiring:
        return hiring, True
    return shortlist_facilities(**match_input), False


@beta_tool
def match_facilities(profession: str, state: str, specialty: str = "",
                     practice_city: str = "", practice_zip: str = "") -> str:
    """Shortlist employers near a cleared clinician that are hiring for their profession.

    Only call this after a clinician has already received a GO verdict.

    Args:
        profession: The clinician's profession, e.g. "physician".
        state: Full state name where the clinician practices, e.g. "Colorado".
        specialty: primary_taxonomy_desc from the NPPES identity result, e.g. "Internal Medicine".
        practice_city: practice_city from the NPPES identity result, if available.
        practice_zip: practice_zip from the NPPES identity result, if available.
    """
    candidates, hiring = _candidates({"profession": profession, "state": state, "specialty": specialty,
                                      "practice_city": practice_city, "practice_zip": practice_zip},
                                     prefs=SEARCH_PREFS.get())
    return json.dumps({
        "source": "Google Jobs open roles; hospital details from " + CMS_SOURCE if hiring else CMS_SOURCE,
        "note": ("Every candidate has open roles in this profession (titles listed)." if hiring else
                 "No job data available: hospital records only, which do not say who is hiring."),
        "candidate_count": len(candidates),
        "candidates": [{**{k: v for k, v in c.items() if k != "listings"},
                        "open_role_titles": [l["title"] for l in c.get("listings", [])][:5]} for c in candidates],
    })


@beta_tool
def rank_facility_matches(ranked: list[dict]) -> str:
    """Record the final best-fit order of matches for a cleared clinician.

    Call this once, after match_facilities, with up to 5 of its candidates.

    Args:
        ranked: Best fit first. Each item is {"facility_id": "<id from match_facilities>",
            "reason": "<one short sentence, under 15 words, using only facts from the
            match_facilities result>"}.
    """
    return json.dumps({"recorded": len(ranked)})


def apply_prefs(candidates: list, prefs: dict) -> list:
    """Drop listings that clearly fail the physician's preferences, drop employers left with
    none, add a setting bonus to the match %, and re-sort best fit first."""
    out = []
    for c in candidates:
        listings = [l for l in c.get("listings", []) if prefs_mod.listing_fits(c, l, prefs)]
        if c.get("listings") and not listings:
            continue
        c = {**c, "listings": listings}
        bonus = prefs_mod.setting_bonus(c, prefs)
        if bonus:
            c["match_pct"] = max(0, min(100, round(c["match_pct"] + 100 * bonus / 10)))
            if bonus > 0:
                c["signals"] = c["signals"] + [f"Suits your {prefs['setting']} preference"]
        out.append(c)
    return sorted(out, key=lambda c: -c["match_pct"])


def build_facility_matches(tool_calls: list, limit: int = 5, notes: list = None, prefs: dict = None):
    """Turn the coordinator's tool calls into the list the product UI shows.

    Returns None when matching did not run (any verdict other than GO). Uses the
    coordinator's ranking and reasons when it supplied valid ones, and falls back
    to the scored order otherwise. Searches are cached, so rebuilding the
    candidates here doesn't repeat them. With a physician's preferences, matches
    are filtered and ordered by preference-adjusted fit instead of the AI ranking.
    """
    match_call = next((t for t in tool_calls if t["name"] == "match_facilities"), None)
    if not match_call:
        return None
    prefs = prefs_mod.clean(prefs)
    candidates, _ = _candidates(match_call["input"], notes, prefs)
    if prefs:
        candidates = apply_prefs(candidates, prefs)
    by_id = {c["facility_id"]: c for c in candidates}

    ordered = []
    rank_call = None if prefs else next((t for t in reversed(tool_calls) if t["name"] == "rank_facility_matches"), None)
    for item in ((rank_call or {}).get("input", {}).get("ranked") or []):
        if not isinstance(item, dict):
            continue
        candidate = by_id.pop(str(item.get("facility_id")), None)
        if candidate:
            reason = str(item.get("reason") or "").strip()
            ordered.append({**candidate, "reason": reason or "; ".join(candidate["signals"]), "ranked_by": "ai"})

    for candidate in candidates:
        if len(ordered) >= limit:
            break
        if candidate["facility_id"] in by_id:
            ordered.append({**candidate, "reason": "; ".join(candidate["signals"]), "ranked_by": "score"})

    for match in ordered:
        listings = [{k: l.get(k) for k in ("title", "link", "pay", "terms")} for l in (match.pop("listings", None) or [])]
        match["openings"] = {"count": len(listings), "listings": listings[:3]} if listings else None
    return ordered[:limit]
