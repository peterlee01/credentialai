"""Facility matching against the real, free, public CMS hospital dataset.

Once a clinician clears verification (GO), the coordinator shortlists real
facilities in their state from CMS "Hospital General Information" and ranks
them for fit. No API key required. See
https://data.cms.gov/provider-data/dataset/xubh-q36u

This matches a cleared clinician to facilities that fit their location,
profession and specialty. It does not know which facilities are hiring: CMS
publishes facility records, not job openings.
"""

import functools
import json

import requests
from anthropic import beta_tool

CMS_ENDPOINT = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"
CMS_SOURCE = "CMS Hospital General Information (live, public)"
PAGE_SIZE = 500
MAX_PAGES = 4
SHORTLIST_SIZE = 8

STATE_CODES = {
    "Arizona": "AZ", "California": "CA", "Colorado": "CO", "Florida": "FL", "Georgia": "GA",
    "Illinois": "IL", "New York": "NY", "Ohio": "OH", "Texas": "TX", "Virginia": "VA",
}

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

    return [
        {
            "facility_id": row.get("facility_id"),
            "name": (row.get("facility_name") or "").title(),
            "city": (row.get("citytown") or "").title(),
            "state": row.get("state"),
            "hospital_type": row.get("hospital_type"),
            "emergency_services": row.get("emergency_services") == "Yes",
            "cms_rating": _rating(row),
            "signals": signals,
        }
        for _, _, row, signals in scored[:SHORTLIST_SIZE]
    ]


@beta_tool
def match_facilities(profession: str, state: str, specialty: str = "",
                     practice_city: str = "", practice_zip: str = "") -> str:
    """Shortlist real facilities that fit a cleared clinician.

    Only call this after a clinician has already received a GO verdict.

    Args:
        profession: The clinician's profession, e.g. "physician".
        state: Full state name where the clinician practices, e.g. "Colorado".
        specialty: primary_taxonomy_desc from the NPPES identity result, e.g. "Internal Medicine".
        practice_city: practice_city from the NPPES identity result, if available.
        practice_zip: practice_zip from the NPPES identity result, if available.
    """
    candidates = shortlist_facilities(profession, state, specialty, practice_city, practice_zip)
    return json.dumps({
        "source": CMS_SOURCE,
        "note": "Facility records only. This dataset does not say which facilities are hiring.",
        "candidate_count": len(candidates),
        "candidates": candidates,
    })


@beta_tool
def rank_facility_matches(ranked: list[dict]) -> str:
    """Record the final best-fit order of facilities for a cleared clinician.

    Call this once, after match_facilities, with up to 5 of its candidates.

    Args:
        ranked: Best fit first. Each item is {"facility_id": "<id from match_facilities>",
            "reason": "<one short sentence, under 15 words, using only facts from the
            match_facilities result>"}.
    """
    return json.dumps({"recorded": len(ranked)})


def build_facility_matches(tool_calls: list, limit: int = 5):
    """Turn the coordinator's tool calls into the list the product UI shows.

    Returns None when matching did not run (any verdict other than GO). Uses the
    coordinator's ranking and reasons when it supplied valid ones, and falls back
    to the scored shortlist order otherwise, so a cleared clinician always gets a
    list.
    """
    match_call = next((t for t in tool_calls if t["name"] == "match_facilities"), None)
    if not match_call:
        return None
    candidates = shortlist_facilities(**match_call["input"])
    by_id = {c["facility_id"]: c for c in candidates}

    ordered = []
    rank_call = next((t for t in reversed(tool_calls) if t["name"] == "rank_facility_matches"), None)
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

    return ordered[:limit]
